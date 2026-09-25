
# -*- coding: utf-8 -*-
"""
RBT FX - Multi-Pair Python Trading Engine
UI + Strategy + Risk + MT5 execution in one process.
Default: DEMO / LIVE_TRADING = True.
Requires: MetaTrader5 package. Tkinter is standard with normal Windows Python.
"""
import os, sys, time, math, threading, traceback, json
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import base64 as _b64

try:
    import MetaTrader5 as mt5
except Exception:
    mt5 = None

APP_NAME = "RBT FX"
VERSION = "2.5.1 LOCKED HUD"

# =========================
# SAFETY / ACCOUNT SETTINGS
# =========================
LIVE_TRADING = True               # LIVE execution enabled; use an Exness DEMO account for forward testing first.
REQUIRE_EXNESS_SERVER = True
MAX_POSITIONS = 5
RISK_PER_TRADE = 0.005            # 0.5% equity
MAX_PORTFOLIO_RISK = 0.020        # 2% total open risk
DAILY_MAX_LOSS_USD = None        # Research mode: daily max-loss guard disabled.
MIN_SCORE = 78.0
MIN_STRATEGIES = 2
SCAN_SECONDS = 3
MAX_SPREAD_ATR = 0.18          # reject entries when spread is too large relative to ATR
LOSS_STREAK_LIMIT = 3
LOSS_COOLDOWN_MINUTES = 30
MAGIC = 26092501
DEVIATION = 80

# Commission assumption for Exness Raw Spread, round-trip = 3.50*2 per lot.
# This is used for expected-edge filtering; actual account commission is read when possible.
COMMISSION_PER_LOT_PER_SIDE_USD = 3.50

# Pair-specific ATR risk parameters. Values are starting parameters, not guarantees.
PAIR_CFG = {
    "BTCUSD":  {"sl_atr":1.35, "tp1_r":0.80, "tp2_r":1.40, "tp3_r":2.20, "trail_atr":0.90},
    "ETHUSD":  {"sl_atr":1.35, "tp1_r":0.80, "tp2_r":1.40, "tp3_r":2.10, "trail_atr":0.90},
    "SOLUSD":  {"sl_atr":1.45, "tp1_r":0.75, "tp2_r":1.30, "tp3_r":2.00, "trail_atr":1.00},
    "XAUUSD":  {"sl_atr":1.45, "tp1_r":0.80, "tp2_r":1.50, "tp3_r":2.20, "trail_atr":1.00},
    "XAGUSD":  {"sl_atr":1.50, "tp1_r":0.80, "tp2_r":1.40, "tp3_r":2.10, "trail_atr":1.00},
    "EURUSD":  {"sl_atr":1.25, "tp1_r":0.80, "tp2_r":1.40, "tp3_r":2.10, "trail_atr":0.85},
    "NAS100":  {"sl_atr":1.45, "tp1_r":0.80, "tp2_r":1.50, "tp3_r":2.20, "trail_atr":1.00},
}

REQUESTED_SYMBOLS = list(PAIR_CFG.keys())
TF_M1 = mt5.TIMEFRAME_M1 if mt5 else 1
TF_M5 = mt5.TIMEFRAME_M5 if mt5 else 5
TF_M15 = mt5.TIMEFRAME_M15 if mt5 else 15

state_lock = threading.Lock()
STATE = {
    "connected": False,
    "engine": True,
    "account": {},
    "scanner": {},
    "positions": [],
    "logs": [],
    "last_error": "",
    "daily_realized": 0.0,
    "daily_floating": 0.0,
    "symbol_map": {},
    "last_signal_bar": {},
    "last_entry_time": {},
    "daily_pnl": 0.0,
    "daily_profit": 0.0,
    "daily_loss": 0.0,
    "daily_realized": 0.0,
    "daily_floating": 0.0,
    "daily_commission": 0.0,
    "daily_swap": 0.0,
    "chart_prices": [],
    "chart_candles": [],
    "chart_symbol": "XAUUSD",
    "loss_streak": 0,
    "loss_cooldown_until": 0.0,
    "loss_guard_last_streak": 0,
}

def now_str(ms=True):
    return datetime.now().strftime("%H:%M:%S.%f")[:-3] if ms else datetime.now().strftime("%H:%M:%S")

def log(tag, symbol, msg, level="INFO"):
    line = f"[{now_str()}] [{tag:<10}] {symbol:<8} {msg}"
    with state_lock:
        STATE["logs"].append((line, level))
        STATE["logs"] = STATE["logs"][-400:]
    print(line, flush=True)

def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def _normalize_rates(raw):
    """Convert MT5 numpy structured arrays/np.void rows to plain Python dicts.
    All strategy code receives dict candles, so .get()/mapping operations never
    accidentally hit numpy.void objects.
    """
    if raw is None:
        return None
    out=[]
    try:
        for x in raw:
            if isinstance(x, dict):
                out.append(x)
            else:
                out.append({
                    "time": int(x["time"]),
                    "open": float(x["open"]),
                    "high": float(x["high"]),
                    "low": float(x["low"]),
                    "close": float(x["close"]),
                    "tick_volume": float(x["tick_volume"]),
                    "spread": float(x["spread"]),
                    "real_volume": float(x["real_volume"]),
                })
    except Exception as e:
        log("DATA", "RBT", f"candle normalization error: {e}", "ERROR")
        return None
    return out

def rates(symbol, timeframe, n=250):
    try:
        r = mt5.copy_rates_from_pos(symbol, timeframe, 1, n)
        if r is None or len(r) == 0:
            return r
        return _normalize_rates(r)
    except Exception as e:
        log("DATA", symbol, f"rates error: {e}", "WARN")
        return None

def ema(values, period):
    if values is None or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1-k)
    return e

def atr(r, period=14):
    if r is None or len(r) < period + 2:
        return 0.0
    trs = []
    prev = safe_float(r[0]["close"])
    for x in r[1:]:
        h, l, c = safe_float(x["high"]), safe_float(x["low"]), safe_float(x["close"])
        trs.append(max(h-l, abs(h-prev), abs(l-prev)))
        prev = c
    return sum(trs[-period:]) / period if len(trs) >= period else 0.0

def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def stddev(values, period):
    if len(values) < period:
        return None
    a = values[-period:]
    m = sum(a)/period
    return math.sqrt(sum((x-m)**2 for x in a)/period)

def adx(r, period=14):
    if r is None or len(r) < period*2+2:
        return 0.0
    tr, plus, minus = [], [], []
    for i in range(1, len(r)):
        h, l = safe_float(r[i]["high"]), safe_float(r[i]["low"])
        ph, pl = safe_float(r[i-1]["high"]), safe_float(r[i-1]["low"])
        pc = safe_float(r[i-1]["close"])
        tr.append(max(h-l, abs(h-pc), abs(l-pc)))
        up = h-ph
        dn = pl-l
        plus.append(up if up > dn and up > 0 else 0)
        minus.append(dn if dn > up and dn > 0 else 0)
    if len(tr) < period:
        return 0.0
    atrv = sum(tr[-period:])/period
    if atrv <= 0: return 0.0
    pdi = 100*(sum(plus[-period:])/period)/atrv
    mdi = 100*(sum(minus[-period:])/period)/atrv
    if pdi+mdi == 0: return 0.0
    return 100*abs(pdi-mdi)/(pdi+mdi)

def candle_body(r, i=-1):
    return abs(safe_float(r[i]["close"])-safe_float(r[i]["open"]))

def trend_structure(r):
    if r is None or len(r) < 30:
        return "MIXED", 0
    highs = [safe_float(x["high"]) for x in r[-20:-1]]
    lows  = [safe_float(x["low"]) for x in r[-20:-1]]
    c = safe_float(r[-1]["close"])
    hh = c > max(highs[-8:])
    ll = c < min(lows[-8:])
    recent_highs = highs[-10:]
    recent_lows = lows[-10:]
    high_slope = recent_highs[-1] - recent_highs[0]
    low_slope = recent_lows[-1] - recent_lows[0]
    if high_slope > 0 and low_slope > 0: return "BULLISH", 82
    if high_slope < 0 and low_slope < 0: return "BEARISH", 82
    if hh: return "BOS_UP", 88
    if ll: return "BOS_DOWN", 88
    return "MIXED", 55

def regime(r5, r15):
    if r5 is None or r15 is None:
        return "NO DATA", 0
    c = [safe_float(x["close"]) for x in r5]
    e9, e21 = ema(c[-120:],9), ema(c[-120:],21)
    e9p, e21p = ema(c[-121:-1],9), ema(c[-121:-1],21)
    a = atr(r5,14)
    ad = adx(r15,14)
    price = c[-1]
    if a <= 0: return "NO DATA", 0
    spread = abs(e9-e21)/a if e9 and e21 else 0
    if ad >= 25 and spread >= 0.35:
        return ("TREND UP" if e9 > e21 else "TREND DOWN"), min(100, int(60+ad))
    if ad < 18 and spread < 0.25:
        return "RANGE", int(75 + max(0,18-ad))
    if spread >= 0.25:
        return ("MOMENTUM UP" if e9 > e21 else "MOMENTUM DOWN"), int(65+min(25,ad))
    return "MIXED", 60

def score_trend(r5, r15, direction):
    c5=[safe_float(x["close"]) for x in r5[-120:]]
    c15=[safe_float(x["close"]) for x in r15[-120:]]
    e9,e21=ema(c5,9),ema(c5,21)
    e9b,e21b=ema(c15,9),ema(c15,21)
    if None in (e9,e21,e9b,e21b): return 0
    s=0
    if direction=="BUY":
        if e9>e21: s+=45
        if e9b>e21b: s+=35
        if c5[-1]>e9: s+=20
    else:
        if e9<e21: s+=45
        if e9b<e21b: s+=35
        if c5[-1]<e9: s+=20
    return s

def score_structure(r5, direction):
    sdir, base = trend_structure(r5)
    if direction=="BUY":
        return base if sdir in ("BULLISH","BOS_UP") else (65 if sdir=="MIXED" else 25)
    return base if sdir in ("BEARISH","BOS_DOWN") else (65 if sdir=="MIXED" else 25)

def score_liquidity(r5, direction):
    if r5 is None or len(r5)<25: return 0
    # Use completed candles; current forming candle is excluded.
    a=atr(r5,14)
    if a<=0: return 0
    prev=r5[-2]; cur=r5[-1]
    recent=r5[-22:-2]
    lo=min(safe_float(x["low"]) for x in recent)
    hi=max(safe_float(x["high"]) for x in recent)
    cl=safe_float(cur["close"]); op=safe_float(cur["open"])
    if direction=="BUY":
        sweep=safe_float(cur["low"])<lo and cl>lo
        rejection=(cl>op) or (cl-safe_float(cur["low"])>0.45*a)
        return 95 if sweep and rejection else (65 if sweep else 20)
    sweep=safe_float(cur["high"])>hi and cl<hi
    rejection=(cl<op) or (safe_float(cur["high"])-cl>0.45*a)
    return 95 if sweep and rejection else (65 if sweep else 20)

def score_breakout_retest(r5, direction):
    if r5 is None or len(r5)<35: return 0
    a=atr(r5,14)
    if a<=0:return 0
    look=r5[-18:-3]
    hi=max(safe_float(x["high"]) for x in look)
    lo=min(safe_float(x["low"]) for x in look)
    c1=safe_float(r5[-3]["close"])
    c2=safe_float(r5[-2]["close"])
    c=safe_float(r5[-1]["close"])
    if direction=="BUY":
        broke=c1>hi+0.10*a
        retest=(safe_float(r5[-1]["low"])<=hi+0.25*a and c>hi)
        return 92 if broke and retest else (68 if broke else 15)
    broke=c1<lo-0.10*a
    retest=(safe_float(r5[-1]["high"])>=lo-0.25*a and c<lo)
    return 92 if broke and retest else (68 if broke else 15)

def score_momentum(r5, direction):
    if r5 is None or len(r5)<30:return 0
    a=atr(r5,14)
    if a<=0:return 0
    body=candle_body(r5,-1)
    bodies=[candle_body(r5,i) for i in range(-22,-1)]
    avg=sum(bodies)/len(bodies)
    c=safe_float(r5[-1]["close"]); o=safe_float(r5[-1]["open"])
    vol=safe_float(r5[-1]["tick_volume"])
    avgv=sum(safe_float(x["tick_volume"]) for x in r5[-22:-1])/21
    s=0
    if body>1.15*avg:s+=50
    if vol>1.15*avgv:s+=25
    if direction=="BUY" and c>o:s+=25
    if direction=="SELL" and c<o:s+=25
    return min(100,s)

def score_volume(r5, direction):
    if r5 is None or len(r5)<30: return 0
    vols=[safe_float(x["tick_volume"]) for x in r5[-22:-1]]
    if not vols: return 0
    avg=sum(vols)/len(vols)
    cur=safe_float(r5[-1]["tick_volume"])
    if avg<=0: return 0
    body=candle_body(r5,-1)
    rng=max(safe_float(r5[-1]["high"])-safe_float(r5[-1]["low"]),1e-12)
    directional=(safe_float(r5[-1]["close"])-safe_float(r5[-1]["open"]))
    aligned=(direction=="BUY" and directional>0) or (direction=="SELL" and directional<0)
    score=20
    if cur>=1.15*avg: score+=50
    elif cur>=1.05*avg: score+=30
    if aligned: score+=20
    if body/rng>=0.55: score+=10
    return min(100,score)

def score_smc_structure(r5, direction):
    if r5 is None or len(r5)<40: return 0
    a=atr(r5,14)
    if a<=0: return 0
    # Lightweight SMC confluence: liquidity sweep + displacement + three-candle FVG.
    c0,c1,c2=r5[-3],r5[-2],r5[-1]
    if direction=="BUY":
        sweep=safe_float(c1["low"]) < min(safe_float(x["low"]) for x in r5[-22:-3]) and safe_float(c1["close"])>safe_float(c1["open"])
        bull_fvg=safe_float(c2["low"]) > safe_float(c0["high"])
        displacement=candle_body(r5,-2)>=0.8*a
    else:
        sweep=safe_float(c1["high"]) > max(safe_float(x["high"]) for x in r5[-22:-3]) and safe_float(c1["close"])<safe_float(c1["open"])
        bull_fvg=safe_float(c2["high"]) < safe_float(c0["low"])
        displacement=candle_body(r5,-2)>=0.8*a
    if sweep and bull_fvg and displacement: return 100
    if sweep and displacement: return 88
    if bull_fvg: return 78
    return 35

def score_mean_reversion(r5, direction):
    if r5 is None or len(r5)<30:return 0
    closes=[safe_float(x["close"]) for x in r5]
    m=sma(closes,20); sd=stddev(closes,20)
    if m is None or not sd or sd<=0:return 0
    c=closes[-1]
    z=(c-m)/sd
    if direction=="BUY":
        return 90 if z<=-2.0 and c>safe_float(r5[-1]["open"]) else (65 if z<=-1.5 else 15)
    return 90 if z>=2.0 and c<safe_float(r5[-1]["open"]) else (65 if z>=1.5 else 15)

def m15_bias(r15):
    if r15 is None or len(r15)<40: return "MIXED",0
    c=[safe_float(x["close"]) for x in r15[-120:]]
    e9,e21=ema(c,9),ema(c,21)
    st,sc=trend_structure(r15)
    if e9 is None or e21 is None: return st,sc
    if e9>e21 and st in ("BULLISH","BOS_UP"): return "BULLISH",max(sc,85)
    if e9<e21 and st in ("BEARISH","BOS_DOWN"): return "BEARISH",max(sc,85)
    return ("BULLISH",65) if e9>e21 else (("BEARISH",65) if e9<e21 else ("MIXED",50))

def m1_trigger(r1, direction):
    if r1 is None or len(r1)<30: return False,0,"NO DATA"
    a=atr(r1,14)
    if a<=0:return False,0,"NO ATR"
    cur=r1[-1]; prev=r1[-2]; prior=r1[-12:-2]
    body=candle_body(r1,-1)
    avg=sum(candle_body(r1,i) for i in range(-21,-1))/20
    if direction=="BUY":
        swept=safe_float(prev["low"]) < min(safe_float(x["low"]) for x in prior) and safe_float(prev["close"])>safe_float(prev["open"])
        bos=safe_float(cur["close"]) > safe_float(prev["high"])
        disp=body>=0.75*a and safe_float(cur["close"])>safe_float(cur["open"])
    else:
        swept=safe_float(prev["high"]) > max(safe_float(x["high"]) for x in prior) and safe_float(prev["close"])<safe_float(prev["open"])
        bos=safe_float(cur["close"]) < safe_float(prev["low"])
        disp=body>=0.75*a and safe_float(cur["close"])<safe_float(cur["open"])
    momentum=disp and body>=1.05*max(avg,1e-12)
    score=(35 if swept else 0)+(35 if bos else 0)+(30 if momentum else (15 if disp else 0))
    return score>=70,score,("SWEEP+BOS+MOM" if score>=70 else ("BOS+MOM" if bos and disp else "WAIT M1 TRIGGER"))

def location_filter(r15,r5,direction,price):
    """Reject only obvious M5 overextension; being at an M15 level itself is not an entry blocker."""
    if r15 is None or r5 is None or len(r5)<30:return True,"NO LOCATION DATA"
    a=atr(r5,14); c=[safe_float(x["close"]) for x in r5[-80:]]
    e9=ema(c,9)
    if a<=0 or e9 is None:return True,"NO ATR"
    if direction=="BUY" and price>e9+1.25*a:
        return False,"OVEREXTENDED ABOVE M5 EMA9"
    if direction=="SELL" and price<e9-1.25*a:
        return False,"OVEREXTENDED BELOW M5 EMA9"
    return True,"LOCATION OK"

def strategy_ensemble(symbol, r1, r5, r15):
    reg, reg_conf = regime(r5,r15)
    vals={}
    candidates={}
    for d in ("BUY","SELL"):
        vals[d]={
            "Trend": score_trend(r5,r15,d),
            "Structure": score_structure(r5,d),
            "Liquidity/SMC": max(score_liquidity(r5,d), score_smc_structure(r5,d)),
            "Breakout": score_breakout_retest(r5,d),
            "Momentum": score_momentum(r5,d),
            "Volume": score_volume(r5,d),
            "Mean Reversion": score_mean_reversion(r5,d),
        }
    if "RANGE" in reg:
        weights={"Trend":0.08,"Structure":0.18,"Liquidity/SMC":0.24,"Breakout":0.10,"Momentum":0.10,"Volume":0.08,"Mean Reversion":0.22}
    elif "TREND" in reg:
        weights={"Trend":0.23,"Structure":0.22,"Liquidity/SMC":0.20,"Breakout":0.10,"Momentum":0.12,"Volume":0.08,"Mean Reversion":0.05}
    else:
        weights={"Trend":0.18,"Structure":0.18,"Liquidity/SMC":0.15,"Breakout":0.20,"Momentum":0.17,"Volume":0.07,"Mean Reversion":0.05}
    final={d:sum(vals[d][k]*w for k,w in weights.items()) for d in ("BUY","SELL")}
    direction=max(final,key=final.get); fs=final[direction]
    active=[k for k,v in vals[direction].items() if v>=70]
    bias,bias_conf=m15_bias(r15)
    if (direction=="BUY" and bias!="BULLISH") or (direction=="SELL" and bias!="BEARISH"):
        direction="WAIT"
    m1ok,m1score,m1reason=m1_trigger(r1,direction if direction!="WAIT" else max(final,key=final.get))
    location_ok,location_reason=location_filter(r15,r5,direction if direction!="WAIT" else max(final,key=final.get),safe_float(r5[-1]["close"]))
    if fs<MIN_SCORE or len(active)<MIN_STRATEGIES or not m1ok or not location_ok:
        direction="WAIT"
    return {
        "signal":direction,"score":round(fs,1),"regime":reg,"regime_conf":reg_conf,
        "strategies":vals.get(direction if direction!="WAIT" else max(final,key=final.get),{}),
        "buy_score":round(final["BUY"],1),"sell_score":round(final["SELL"],1),
        "atr":atr(r5,14),"price":safe_float(r5[-1]["close"]) if r5 else 0,
        "active_count":len(active),"m15_bias":bias,"m15_conf":bias_conf,
        "m1_trigger":m1reason,"m1_score":m1score,"location":location_reason,
        "entry_ready":direction!="WAIT"
    }

def canonical_for_symbol(symbol):
    """Map a broker symbol variant (e.g. BTCUSDm/XAUUSD.a) back to our strategy key."""
    if symbol in PAIR_CFG:
        return symbol
    with state_lock:
        sm=dict(STATE.get("symbol_map",{}))
    for canonical, actual in sm.items():
        if actual == symbol:
            return canonical
    norm=symbol.upper().replace(".","").replace("_","").replace("-","")
    for canonical in PAIR_CFG:
        cn=canonical.upper().replace(".","").replace("_","").replace("-","")
        if norm == cn or norm.startswith(cn):
            return canonical
    return symbol

def pair_cfg_for(symbol):
    canonical=canonical_for_symbol(symbol)
    return PAIR_CFG.get(canonical, PAIR_CFG["BTCUSD"])

def ensure_symbols():
    found={}
    all_syms=mt5.symbols_get()
    names=[x.name for x in all_syms] if all_syms else []
    for req in REQUESTED_SYMBOLS:
        # Exact first, then normalized containment.
        exact=mt5.symbol_info(req)
        if exact:
            mode=getattr(exact,"trade_mode",None)
            disabled=getattr(mt5,"SYMBOL_TRADE_MODE_DISABLED",0)
            if mode is None or int(mode)!=int(disabled):
                if mt5.symbol_select(req, True):
                    found[req]=req; continue
        norm=req.upper().replace(".","").replace("_","").replace("-","")
        candidates=[]
        for n in names:
            nn=n.upper().replace(".","").replace("_","").replace("-","")
            if nn==norm or nn.startswith(norm) or norm in nn:
                candidates.append(n)
        if candidates:
            usable=[]
            for name in candidates:
                inf=mt5.symbol_info(name)
                if inf is None: continue
                mode=getattr(inf,"trade_mode",None)
                disabled=getattr(mt5,"SYMBOL_TRADE_MODE_DISABLED",0)
                if mode is not None and int(mode)==int(disabled):
                    continue
                usable.append(name)
            pool=usable or candidates
            def map_rank(name):
                inf=mt5.symbol_info(name)
                visible=1 if inf is not None and bool(getattr(inf,"visible",False)) else 0
                exact_norm=1 if name.upper().replace(".","").replace("_","").replace("-","")==norm else 0
                starts=1 if name.upper().replace(".","").replace("_","").replace("-","").startswith(norm) else 0
                return (-exact_norm,-starts,-visible,len(name),name)
            chosen=sorted(pool,key=map_rank)[0]
            if not mt5.symbol_select(chosen, True):
                log("SYMBOL",req,f"symbol_select failed for {chosen}","WARN")
                continue
            found[req]=chosen
            log("SYMBOL",req,f"mapped -> {chosen}")
        else:
            log("SYMBOL",req,"not found in MT5 Market Watch", "WARN")
    with state_lock: STATE["symbol_map"]=found
    return found

def account_info():
    a=mt5.account_info()
    if not a:return {}
    return {"login":a.login,"currency":a.currency,"balance":safe_float(a.balance),
            "equity":safe_float(a.equity),"margin":safe_float(a.margin),
            "free_margin":safe_float(a.margin_free),"leverage":a.leverage,
            "server":getattr(a,"server","")}

def positions():
    ps=mt5.positions_get()
    out=[]
    if ps:
        for p in ps:
            if int(getattr(p,"magic",0))!=MAGIC:
                # Keep display of all positions, but risk manager only controls ours.
                pass
            out.append({"ticket":int(p.ticket),"symbol":p.symbol,
                        "type":"BUY" if p.type==mt5.POSITION_TYPE_BUY else "SELL",
                        "volume":safe_float(p.volume),"entry":safe_float(p.price_open),
                        "sl":safe_float(p.sl),"tp":safe_float(p.tp),
                        "price":safe_float(p.price_current),"profit":safe_float(p.profit),
                        "magic":int(getattr(p,"magic",0))})
    return out

def our_positions():
    ps=mt5.positions_get()
    return [p for p in (ps or []) if int(getattr(p,"magic",0))==MAGIC]

def daily_stats():
    """Real-time account daily accounting in the account's native currency.
    Profit and loss are tracked as separate gross components: closed deals plus
    current floating P/L. This keeps the HUD honest even when the net is green
    after earlier losing trades.
    """
    a=mt5.account_info()
    if not a:
        return {"profit":0.0,"loss":0.0,"realized":0.0,"floating":0.0,"commission":0.0,"swap":0.0,"net":0.0}
    now=datetime.now(timezone.utc)
    start=datetime(now.year,now.month,now.day,tzinfo=timezone.utc)
    deals=mt5.history_deals_get(start,now) or []
    realized=commission=swap=0.0; realized_profit=realized_loss=0.0
    for d in deals:
        dtype=getattr(d,"type",None)
        balance_types={getattr(mt5,"DEAL_TYPE_BALANCE",999999),getattr(mt5,"DEAL_TYPE_CREDIT",999998)}
        if dtype in balance_types:
            continue
        v=safe_float(getattr(d,"profit",0))+safe_float(getattr(d,"commission",0))+safe_float(getattr(d,"swap",0))
        realized += v
        commission += safe_float(getattr(d,"commission",0)); swap += safe_float(getattr(d,"swap",0))
        if v>=0: realized_profit += v
        else: realized_loss += v
    floating=sum(safe_float(p.profit) for p in (mt5.positions_get() or []))
    floating_profit=max(0.0,floating); floating_loss=min(0.0,floating)
    profit=realized_profit+floating_profit
    loss=realized_loss+floating_loss
    return {"profit":profit,"loss":loss,"realized":realized,"floating":floating,
            "commission":commission,"swap":swap,"net":profit+loss}

def daily_pnl():
    return daily_stats()["net"]

def risk_money(equity):
    return max(0.0,equity*RISK_PER_TRADE)

def calc_lot(symbol, entry, sl, equity):
    info=mt5.symbol_info(symbol)
    if not info:return 0.0
    dist=abs(entry-sl)
    if dist<=0:return 0.0
    ts=safe_float(info.trade_tick_size)
    tv=safe_float(info.trade_tick_value)
    loss_per_lot=0.0
    if hasattr(mt5,"order_calc_profit"):
        try:
            typ=mt5.ORDER_TYPE_BUY if entry>sl else mt5.ORDER_TYPE_SELL
            loss_per_lot=abs(safe_float(mt5.order_calc_profit(typ,symbol,1.0,entry,sl)))
        except Exception:
            loss_per_lot=0.0
    if loss_per_lot<=0 and ts>0 and tv>0:
        loss_per_lot=(dist/ts)*tv
    if loss_per_lot<=0:return 0.0
    raw=risk_money(equity)/loss_per_lot
    step=safe_float(info.volume_step,0.01)
    vmin=safe_float(info.volume_min,step)
    vmax=safe_float(info.volume_max,100.0)
    if raw < vmin:
        # Never force the broker minimum if it would exceed the requested risk.
        return 0.0
    lot=math.floor(raw/step)*step
    lot=min(vmax,lot)
    if lot < vmin:
        return 0.0
    digits=max(0,int(round(-math.log10(step))) if step<1 else 0)
    return round(lot,digits)

def current_open_risk(equity):
    total=0.0
    if equity<=0: return 0.0
    for p in our_positions():
        if p.sl and p.sl>0:
            try:
                typ=mt5.ORDER_TYPE_BUY if p.type==mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_SELL
                loss=abs(safe_float(mt5.order_calc_profit(typ,p.symbol,p.volume,p.price_open,p.sl)))
                if loss>0:
                    total += loss
                    continue
            except Exception:
                pass
            info=mt5.symbol_info(p.symbol)
            if info:
                ts=safe_float(info.trade_tick_size); tv=safe_float(info.trade_tick_value)
                if ts>0 and tv>0:
                    total += (abs(p.price_open-p.sl)/ts)*tv*p.volume
    return total/equity

def normalize_price(symbol,p):
    info=mt5.symbol_info(symbol)
    return round(p,int(getattr(info,"digits",2))) if info else p

def choose_fill(info):
    # symbol_info().filling_mode is a bitmask for FOK/IOC support.
    mode=int(getattr(info,"filling_mode",0))
    if mode & 1:  # SYMBOL_FILLING_FOK
        return mt5.ORDER_FILLING_FOK
    if mode & 2:  # SYMBOL_FILLING_IOC
        return mt5.ORDER_FILLING_IOC
    # RETURN is generally accepted for non-market execution; otherwise IOC is the
    # safest fallback available in the Python API.
    return mt5.ORDER_FILLING_RETURN

def expected_edge_ok(symbol, entry, sl, tp, lot):
    info=mt5.symbol_info(symbol)
    if not info:return False
    ts=safe_float(info.trade_tick_size); tv=safe_float(info.trade_tick_value)
    if ts<=0 or tv<=0:return False
    gross=((abs(tp-entry)/ts)*tv*lot)
    # Approximate round trip commission in USD. For non-USD account this is conservative only.
    acct=mt5.account_info()
    comm=COMMISSION_PER_LOT_PER_SIDE_USD*2*lot if not acct or getattr(acct,"currency","USD")=="USD" else 0.0
    spread=max(0.0,safe_float(info.ask)-safe_float(info.bid))
    spread_cost=(spread/ts)*tv*lot
    return gross > (comm+spread_cost)*1.5

def levels_are_valid(symbol,direction,sl,tp):
    info=mt5.symbol_info(symbol); tick=mt5.symbol_info_tick(symbol)
    if not info or not tick:return False
    point=safe_float(getattr(info,"point",0.0))
    min_dist=max(safe_float(getattr(info,"trade_stops_level",0))*point, safe_float(getattr(info,"trade_freeze_level",0))*point)
    ref=safe_float(tick.ask if direction=="BUY" else tick.bid)
    if direction=="BUY":
        return sl < ref-min_dist and tp > ref+min_dist
    return sl > ref+min_dist and tp < ref-min_dist

def send_order(symbol,direction,lot,sl,tp1,tp2,tp3,score):
    if not LIVE_TRADING:
        log("SIMULATE",symbol,f"{direction} {lot} | score {score:.1f}% | SL {sl} TP1 {tp1}", "INFO")
        return True, 0
    info=mt5.symbol_info(symbol)
    tick=mt5.symbol_info_tick(symbol)
    if not info or not tick:return False,0
    price=safe_float(tick.ask if direction=="BUY" else tick.bid)
    typ=mt5.ORDER_TYPE_BUY if direction=="BUY" else mt5.ORDER_TYPE_SELL
    sl=normalize_price(symbol,sl); tp3=normalize_price(symbol,tp3)
    if not levels_are_valid(symbol,direction,sl,tp3):
        log("ORDER",symbol,f"Invalid broker distance: price={price} SL={sl} TP={tp3}","WARN")
        return False,0
    request={
        "action":mt5.TRADE_ACTION_DEAL,"symbol":symbol,"volume":lot,"type":typ,
        "price":price,"sl":sl,"tp":tp3,
        "deviation":DEVIATION,"magic":MAGIC,"comment":f"RBTFX {score:.0f}",
        "type_time":mt5.ORDER_TIME_GTC,"type_filling":choose_fill(info)
    }
    if hasattr(mt5,"order_check"):
        try:
            chk=mt5.order_check(request)
            if chk is not None and getattr(chk,"retcode",0) not in (0, getattr(mt5,"TRADE_RETCODE_DONE",10009)):
                log("ORDER",symbol,f"order_check rejected: {getattr(chk,'comment','')} retcode={getattr(chk,'retcode',0)}","WARN")
                return False,0
        except Exception as e:
            log("ORDER",symbol,f"order_check warning: {e}","WARN")
    result=mt5.order_send(request)
    if result is None:
        log("ORDER",symbol,f"send failed: {mt5.last_error()}","ERROR"); return False,0
    done_codes={mt5.TRADE_RETCODE_DONE}
    if hasattr(mt5,"TRADE_RETCODE_DONE_PARTIAL"): done_codes.add(mt5.TRADE_RETCODE_DONE_PARTIAL)
    if result.retcode not in done_codes:
        log("ORDER",symbol,f"rejected retcode={result.retcode} {result.comment}","ERROR"); return False,0
    log("ORDER",symbol,f"{direction} {lot} @ {price} | SL {sl} | TP3 {tp3}","OK")
    return True,int(getattr(result,"order",0))

def build_levels(symbol,direction,price,a):
    cfg=pair_cfg_for(symbol)
    info=mt5.symbol_info(symbol)
    point=safe_float(getattr(info,"point",0.0)) if info else 0.0
    min_dist=max(safe_float(getattr(info,"trade_stops_level",0))*point, safe_float(getattr(info,"trade_freeze_level",0))*point)
    sl_dist=max(a*cfg["sl_atr"], min_dist*1.10, 0.0000001)
    if direction=="BUY":
        sl=price-sl_dist
        tp1=price+sl_dist*cfg["tp1_r"]
        tp2=price+sl_dist*cfg["tp2_r"]
        tp3=price+sl_dist*cfg["tp3_r"]
    else:
        sl=price+sl_dist
        tp1=price-sl_dist*cfg["tp1_r"]
        tp2=price-sl_dist*cfg["tp2_r"]
        tp3=price-sl_dist*cfg["tp3_r"]
    return [normalize_price(symbol,x) for x in (sl,tp1,tp2,tp3)]

def modify_position(ticket, sl=None, tp=None):
    p=next((x for x in (mt5.positions_get() or []) if int(x.ticket)==int(ticket)),None)
    if not p:return False
    req={"action":mt5.TRADE_ACTION_SLTP,"symbol":p.symbol,"position":p.ticket,
         "sl":normalize_price(p.symbol,sl if sl is not None else p.sl),
         "tp":normalize_price(p.symbol,tp if tp is not None else p.tp)}
    r=mt5.order_send(req)
    done={mt5.TRADE_RETCODE_DONE}
    if hasattr(mt5,"TRADE_RETCODE_DONE_PARTIAL"): done.add(mt5.TRADE_RETCODE_DONE_PARTIAL)
    return bool(r and r.retcode in done)

def close_partial(position, volume):
    info=mt5.symbol_info(position.symbol); tick=mt5.symbol_info_tick(position.symbol)
    if not info or not tick:return False
    step=safe_float(getattr(info,"volume_step",0.01),0.01)
    vmin=safe_float(getattr(info,"volume_min",step),step)
    volume=min(volume,position.volume)
    if step>0:
        volume=math.floor(volume/step)*step
        volume=round(volume,max(0,int(round(-math.log10(step))) if step<1 else 0))
    # Do not attempt a partial close that would violate the broker minimum volume.
    if position.volume-volume < vmin-1e-12:
        volume=position.volume-vmin
        if step>0:
            volume=math.floor(max(0.0,volume)/step)*step
            volume=round(volume,max(0,int(round(-math.log10(step))) if step<1 else 0))
    if volume<=0:return False
    typ=mt5.ORDER_TYPE_SELL if position.type==mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price=tick.bid if typ==mt5.ORDER_TYPE_SELL else tick.ask
    req={"action":mt5.TRADE_ACTION_DEAL,"symbol":position.symbol,"volume":volume,"type":typ,
         "position":position.ticket,"price":price,"deviation":DEVIATION,"magic":MAGIC,
         "comment":"RBTFX partial","type_time":mt5.ORDER_TIME_GTC,"type_filling":choose_fill(info)}
    r=mt5.order_send(req)
    done={mt5.TRADE_RETCODE_DONE}
    if hasattr(mt5,"TRADE_RETCODE_DONE_PARTIAL"): done.add(mt5.TRADE_RETCODE_DONE_PARTIAL)
    return bool(r and r.retcode in done)

def manage_positions():
    # Broker-side SL/TP remain in place. Python advances protection and partials.
    for p in our_positions():
        symbol=p.symbol
        info=mt5.symbol_info(symbol)
        tick=mt5.symbol_info_tick(symbol)
        if not info or not tick: continue
        is_buy=(p.type==mt5.POSITION_TYPE_BUY)
        price=safe_float(tick.bid if is_buy else tick.ask)
        r5=rates(symbol,TF_M5,80)
        a=atr(r5,14)
        cfg=pair_cfg_for(symbol)
        if a<=0 or p.sl<=0: continue
        # TP3 is broker-side and remains unchanged, so it gives the original risk distance after restart/trailing.
        initial_r=abs(p.tp-p.price_open)/max(cfg["tp3_r"],1e-9) if p.tp and p.tp>0 else abs(p.price_open-p.sl)
        if initial_r<=0: continue
        moved=(price-p.price_open) if is_buy else (p.price_open-price)
        R=moved/initial_r
        key=f"{p.ticket}"
        meta=TRADE_META.setdefault(key,{"entry":p.price_open,"sl0":p.sl,"tp1":None,"tp2":None,"partial1":False,"partial2":False})
        tp1r,tp2r=cfg["tp1_r"],cfg["tp2_r"]
        if meta["tp1"] is None:
            meta["tp1"] = p.price_open + initial_r*tp1r*(1 if is_buy else -1)
            meta["tp2"] = p.price_open + initial_r*tp2r*(1 if is_buy else -1)
            # Never repeat a partial after a Python restart if price has already passed that target.
            if R >= tp2r:
                meta["partial1"]=True; meta["partial2"]=True
            elif R >= tp1r:
                meta["partial1"]=True
        # Partial 30% at TP1, then 30% at TP2. Runner remains for TP3.
        if R>=tp1r and not meta["partial1"]:
            partial_ok=True
            if LIVE_TRADING:
                partial_ok=close_partial(p,max(0.01,p.volume*0.30))
                info2=mt5.symbol_info(symbol)
                if info2 and p.volume <= safe_float(getattr(info2,"volume_min",0.01),0.01)+1e-12:
                    partial_ok=True  # broker minimum volume prevents a partial close
            if not partial_ok:
                log("TP1",symbol,"Partial close failed; protection will be retried", "WARN")
            # Move SL to near breakeven after TP1
            be=p.price_open + (0.05*a if is_buy else -0.05*a)
            if (is_buy and (p.sl<be)) or ((not is_buy) and (p.sl>be)):
                modify_position(p.ticket,be,p.tp)
            if partial_ok:
                meta["partial1"]=True
            log("TP1",symbol,f"Reached +{R:.2f}R | partial/protection advanced","OK")
        if R>=tp2r and not meta["partial2"]:
            partial_ok=True
            if LIVE_TRADING:
                partial_ok=close_partial(p,max(0.01,p.volume*0.30))
                info2=mt5.symbol_info(symbol)
                if info2 and p.volume <= safe_float(getattr(info2,"volume_min",0.01),0.01)+1e-12:
                    partial_ok=True
            if partial_ok:
                meta["partial2"]=True
            log("TP2",symbol,f"Reached +{R:.2f}R | runner remains","OK")
        # ATR trailing after +0.8R; structure-aware when possible.
        if R>=0.8:
            dist=a*cfg["trail_atr"]
            if is_buy:
                new_sl=price-dist
                # do not loosen SL
                if new_sl>p.sl and new_sl<price-info.point:
                    if LIVE_TRADING: modify_position(p.ticket,new_sl,p.tp)
                    log("TRAIL",symbol,f"BUY SL candidate {new_sl:.{info.digits}f} at {R:.2f}R","INFO")
            else:
                new_sl=price+dist
                if new_sl<p.sl:
                    if LIVE_TRADING: modify_position(p.ticket,new_sl,p.tp)
                    log("TRAIL",symbol,f"SELL SL candidate {new_sl:.{info.digits}f} at {R:.2f}R","INFO")
    # Remove stale meta
    active={str(p.ticket) for p in our_positions()}
    for k in list(TRADE_META):
        if k not in active: TRADE_META.pop(k,None)

def weekend_guard():
    # Close/avoid positions near Friday market close. Broker-specific hours vary.
    now=datetime.now(timezone.utc)
    if now.weekday()!=4:
        return False
    # Conservative: after 20:30 local VPS time. This is a safety default, not broker session truth.
    return now.hour>=20 and now.minute>=30

def candidate_risk_ratio(symbol, entry, sl, lot, equity):
    info=mt5.symbol_info(symbol)
    if not info or equity<=0 or lot<=0: return 0.0
    try:
        typ=mt5.ORDER_TYPE_BUY if entry>sl else mt5.ORDER_TYPE_SELL
        loss=abs(safe_float(mt5.order_calc_profit(typ,symbol,lot,entry,sl)))
        return loss/equity
    except Exception:
        ts=safe_float(getattr(info,"trade_tick_size",0)); tv=safe_float(getattr(info,"trade_tick_value",0))
        if ts<=0 or tv<=0:return 0.0
        return ((abs(entry-sl)/ts)*tv*lot)/equity

def update_loss_guard():
    """Track this EA's realized losing deals today and enforce a short cooldown after a loss streak."""
    now=datetime.now(timezone.utc); start=datetime(now.year,now.month,now.day,tzinfo=timezone.utc)
    deals=mt5.history_deals_get(start,now) or []
    closed=[]
    for d in deals:
        if int(getattr(d,"magic",0))!=MAGIC: continue
        entry=getattr(d,"entry",None)
        if entry is not None and hasattr(mt5,"DEAL_ENTRY_OUT") and entry!=mt5.DEAL_ENTRY_OUT: continue
        profit=safe_float(getattr(d,"profit",0))+safe_float(getattr(d,"commission",0))+safe_float(getattr(d,"swap",0))
        closed.append((int(getattr(d,"time",0)),profit))
    closed.sort(key=lambda x:x[0])
    streak=0
    for _,profit in reversed(closed):
        if profit<0: streak+=1
        elif profit>0: break
    with state_lock:
        STATE["loss_streak"]=streak
        until=STATE.get("loss_cooldown_until",0.0)
        last_trigger=STATE.get("loss_guard_last_streak",0)
    if streak>=LOSS_STREAK_LIMIT and streak!=last_trigger:
        with state_lock:
            STATE["loss_cooldown_until"]=time.time()+LOSS_COOLDOWN_MINUTES*60
            STATE["loss_guard_last_streak"]=streak
        log("RISK","GLOBAL",f"Loss streak {streak}; cooldown {LOSS_COOLDOWN_MINUTES}m","WARN")
    elif streak==0 and last_trigger:
        with state_lock:
            STATE["loss_cooldown_until"]=0.0
            STATE["loss_guard_last_streak"]=0
    return streak

def portfolio_allows(symbol, score):
    a=mt5.account_info()
    if not a:return False,"NO ACCOUNT"
    pos=our_positions()
    if len(pos)>=MAX_POSITIONS:return False,"MAX POSITIONS"
    if DAILY_MAX_LOSS_USD is not None:
        dp=daily_pnl()
        if dp <= -DAILY_MAX_LOSS_USD:return False,"DAILY LOSS LIMIT"
    streak=update_loss_guard()
    with state_lock: cooldown=STATE.get("loss_cooldown_until",0.0)
    if cooldown>time.time(): return False,f"LOSS COOLDOWN ({int(cooldown-time.time())}s)"
    openrisk=current_open_risk(a.equity)
    if openrisk + RISK_PER_TRADE > MAX_PORTFOLIO_RISK + 1e-9:
        return False,"PORTFOLIO RISK"
    crypto={"BTCUSD","ETHUSD","SOLUSD"}
    active_crypto=sum(1 for p in pos if canonical_for_symbol(p.symbol) in crypto)
    if canonical_for_symbol(symbol) in crypto and active_crypto>=2:
        return False,"CRYPTO EXPOSURE"
    return True,"OK"

def execute_signal(req_symbol, data):
    if data["signal"]=="WAIT": return
    symbol=STATE["symbol_map"].get(req_symbol,req_symbol)
    ok,reason=portfolio_allows(symbol,data["score"])
    if not ok:
        log("RISK",req_symbol,f"Skipped: {reason}","WARN"); return
    r5=rates(symbol,TF_M5,5)
    if r5 is None or len(r5)<3:return
    bar_time=int(r5[-1]["time"])
    key=f"{req_symbol}"
    with state_lock:
        if STATE["last_signal_bar"].get(key)==bar_time: return
        last_attempt=STATE["last_entry_time"].get(key,0.0)
    if time.time()-last_attempt<30: return
    tick=mt5.symbol_info_tick(symbol)
    if not tick:return
    price=safe_float(tick.ask if data["signal"]=="BUY" else tick.bid)
    a=safe_float(data.get("atr",0.0))
    spread=max(0.0,safe_float(tick.ask)-safe_float(tick.bid))
    if a<=0 or spread>a*MAX_SPREAD_ATR:
        log("FILTER",req_symbol,f"Spread too high: {spread:.6g} vs ATR {a:.6g}","WARN"); return
    sl,tp1,tp2,tp3=build_levels(symbol,data["signal"],price,a)
    acct=mt5.account_info()
    if not acct:return
    lot=calc_lot(symbol,price,sl,acct.equity)
    if lot<=0:
        log("RISK",req_symbol,"Lot calculation returned zero (minimum lot would exceed risk)","WARN"); return
    candidate_risk=candidate_risk_ratio(symbol,price,sl,lot,acct.equity)
    current_risk=current_open_risk(acct.equity)
    if current_risk + candidate_risk > MAX_PORTFOLIO_RISK + 1e-9:
        log("RISK",req_symbol,f"Skipped: portfolio risk {(current_risk+candidate_risk)*100:.2f}% > {MAX_PORTFOLIO_RISK*100:.2f}%","WARN"); return
    if not expected_edge_ok(symbol,price,sl,tp2,lot):
        log("FILTER",req_symbol,"Expected edge too small after spread/commission","WARN"); return
    log("SIGNAL",req_symbol,f"{data['signal']} score={data['score']:.1f}% regime={data['regime']} active={data['active_count']}","OK")
    with state_lock: STATE["last_entry_time"][req_symbol]=time.time()
    ok,ticket=send_order(symbol,data["signal"],lot,sl,tp1,tp2,tp3,data["score"])
    if ok:
        with state_lock: STATE["last_signal_bar"][key]=bar_time
        log("PROTECT",req_symbol,f"SL {sl} | TP1 {tp1} | TP2 {tp2} | TP3 {tp3}","OK")

def scan_once():
    if not mt5 or not STATE["connected"]: return
    with state_lock:
        symmap=dict(STATE["symbol_map"])
    for req,symbol in symmap.items():
        try:
            r1=rates(symbol,TF_M1,180); r5=rates(symbol,TF_M5,220); r15=rates(symbol,TF_M15,180)
            if r1 is None or r5 is None or r15 is None or len(r1)<60 or len(r5)<80 or len(r15)<60:
                continue
            data=strategy_ensemble(symbol,r1,r5,r15)
            tick=mt5.symbol_info_tick(symbol)
            spread=(safe_float(tick.ask)-safe_float(tick.bid)) if tick else 0
            data["spread"]=spread
            data["price"]=safe_float(tick.bid) if tick else data["price"]
            with state_lock: STATE["scanner"][req]=data
            if data["signal"]!="WAIT":
                execute_signal(req,data)
        except Exception as e:
            log("SCAN",req,f"error: {e}","ERROR")

def engine_loop():
    try:
        info=account_info()
        if not info: raise RuntimeError(f"MT5 account unavailable: {mt5.last_error()}")
        with state_lock:
            STATE["connected"]=True; STATE["account"]=info
        log("SYSTEM","RBTFX",f"Connected {info['server']} login={info['login']} currency={info['currency']}","OK")
        ensure_symbols()
        log("SYSTEM","RBTFX",f"Mode={'LIVE' if LIVE_TRADING else 'DEMO/SIMULATION'} | MaxPos={MAX_POSITIONS} | Risk={RISK_PER_TRADE*100:.2f}%","OK")
        while STATE["engine"]:
            try:
                if mt5.account_info() is None:
                    with state_lock: STATE["connected"]=False
                    log("BROKER","MT5","Connection/account lost; retrying initialize", "WARN")
                    try: mt5.shutdown()
                    except Exception: pass
                    time.sleep(1)
                    mt5.initialize()
                    info=account_info()
                    if not info:
                        time.sleep(SCAN_SECONDS)
                        continue
                    with state_lock: STATE["connected"]=True
                info=account_info()
                with state_lock:
                    STATE["account"]=info
                if weekend_guard():
                    log("WEEKEND","GLOBAL","Protection window active: no new entries","WARN")
                else:
                    scan_once()
                update_loss_guard()
                manage_positions()
                with state_lock:
                    STATE["positions"]=positions()
                    ds=daily_stats()
                    STATE["daily_pnl"]=ds["net"]
                    STATE["daily_profit"]=ds["profit"]
                    STATE["daily_loss"]=ds["loss"]
                    STATE["daily_realized"]=ds["realized"]
                    STATE["daily_floating"]=ds["floating"]
                    STATE["daily_commission"]=ds["commission"]
                    STATE["daily_swap"]=ds["swap"]
                    # Feed the dashboard a real closed-candle price series.
                    chart_req="XAUUSD"
                    chart_symbol=STATE["symbol_map"].get(chart_req)
                    if chart_symbol:
                        cr=rates(chart_symbol,TF_M5,80)
                        if cr is not None and len(cr)>5:
                            STATE["chart_prices"]=[safe_float(x["close"]) for x in cr[-60:]]
                            STATE["chart_candles"]=[{"t":int(x["time"]),"o":safe_float(x["open"]),"h":safe_float(x["high"]),"l":safe_float(x["low"]),"c":safe_float(x["close"]),"v":safe_float(x["tick_volume"])} for x in cr[-60:]]
                            STATE["chart_symbol"]=chart_symbol
                time.sleep(SCAN_SECONDS)
            except Exception as e:
                with state_lock: STATE["last_error"]=str(e)
                log("ENGINE","RBTFX",f"{e}","ERROR")
                time.sleep(3)
    except Exception as e:
        with state_lock:
            STATE["connected"]=False; STATE["engine"]=False; STATE["last_error"]=str(e)
        log("ENGINE","RBTFX",f"startup failed: {e}","ERROR")

# =========================
# WEB DASHBOARD
# =========================
WEB_HOST = "127.0.0.1"
WEB_PORT = int(os.environ.get("RBT_WEB_PORT", "8787"))
WEB_PASSWORD = os.environ.get("RBT_WEB_PASSWORD", "")
WEB_USER = "rbt"

HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RBT FX — Multi-Pair Trading System</title>
<style>
@font-face{font-family:"Inter Display";src:url("fonts/InterDisplay-Regular.otf") format("opentype");font-weight:400}@font-face{font-family:"Inter Display";src:url("fonts/InterDisplay-SemiBold.otf") format("opentype");font-weight:600}@font-face{font-family:"Inter Display";src:url("fonts/InterDisplay-Bold.otf") format("opentype");font-weight:700}@font-face{font-family:"Inter Display";src:url("fonts/InterDisplay-Black.otf") format("opentype");font-weight:900}@font-face{font-family:"DejaVu Sans Mono";src:url("fonts/DejaVuSansMono.ttf") format("truetype")}:root{--bg:#02070d;--panel:#07131f;--panel2:#091a27;--line:#17435a;--cyan:#00d8ff;--green:#16f29a;--red:#ff304f;--gold:#ffd21a;--muted:#7f93a7;--text:#eaf2fa;--blue:#1aa7ff;--purple:#c14cff}
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;background:#02070d;color:var(--text);font-family:"Inter Display","Inter","Segoe UI",Arial,sans-serif;overflow:auto}
body{background:radial-gradient(circle at 78% 20%,rgba(0,140,210,.08),transparent 30%),linear-gradient(180deg,#02070d,#030b13 65%,#02070d)}
.mono{font-family:"DejaVu Sans Mono","Consolas",monospace}.shell{min-width:1280px;min-height:100vh;display:grid;grid-template-columns:104px 1fr;grid-template-rows:86px auto}
.sidebar{grid-row:1/3;background:linear-gradient(180deg,#06101a,#02070d);border-right:1px solid #12384c;display:flex;flex-direction:column;align-items:center;position:sticky;top:0;height:100vh;z-index:10}.brandmini{height:88px;width:100%;display:flex;align-items:center;justify-content:center;color:var(--gold);font-size:35px;text-shadow:0 0 15px #f5b400}.nav{width:100%;padding:5px}.nav button{width:100%;height:76px;background:transparent;border:1px solid transparent;color:#9fb0c0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;font-size:11px;letter-spacing:.3px;cursor:pointer}.nav button .ico{font-size:22px;color:#9fb0c0}.nav button.active{border-color:#9d7a0b;background:linear-gradient(90deg,rgba(255,203,0,.17),transparent);color:#fff;box-shadow:inset 3px 0 var(--gold),0 0 15px rgba(255,203,0,.08)}.nav button.active .ico{color:var(--gold)}.sideBottom{margin-top:auto;width:100%;padding:9px;border-top:1px solid #12384c}.miniEarth{height:80px;border-radius:50%;border:1px solid #b78100;background:radial-gradient(circle,#b78100 0 2px,transparent 3px),repeating-radial-gradient(circle at 50% 50%,transparent 0 13px,#8e6500 14px 15px),repeating-linear-gradient(20deg,transparent 0 12px,#8e6500 13px 14px);opacity:.7;box-shadow:0 0 20px rgba(255,190,0,.15)}.v{color:var(--gold);font-size:12px;font-weight:700;text-align:center;margin:5px}.sys{font-size:9px;color:#9fb0c0;line-height:1.65}.sys b{float:right;color:var(--green)}.okline{color:var(--green);font-size:9px;margin-top:5px;white-space:nowrap}
.main{min-width:0}.topbar{height:86px;border-bottom:1px solid #12384c;background:rgba(2,8,14,.96);display:grid;grid-template-columns:230px repeat(6,minmax(125px,1fr));gap:7px;padding:9px 12px}.logo{display:flex;align-items:center;gap:9px}.crown{font-size:40px;color:var(--gold);text-shadow:0 0 12px #d9a400}.logoTitle{font-size:30px;font-weight:800;letter-spacing:-1px}.logoSub{font-size:10px;color:var(--gold);letter-spacing:.5px}.statusTile{border:1px solid #16445c;border-radius:6px;padding:8px 9px;background:linear-gradient(180deg,rgba(10,28,42,.92),rgba(5,16,25,.95));display:flex;flex-direction:column;justify-content:center;min-width:0}.statusTop{font-size:10px;color:#b5c5d4}.statusMain{font-size:14px;font-weight:800;color:var(--green);margin-top:2px}.statusSub{font-size:9px;color:#92a8ba}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--green);box-shadow:0 0 12px var(--green);margin-right:5px}.shield{color:var(--green);font-size:20px;float:left;margin-right:7px}
.content{padding:8px 9px 14px;display:grid;grid-template-columns:minmax(0,2.08fr) minmax(390px,1.08fr);grid-template-rows:90px 306px 226px 160px 88px;gap:8px}.metrics{grid-column:1;display:grid;grid-template-columns:repeat(7,1fr);gap:7px}.metric{border:1px solid #173e53;border-radius:5px;background:linear-gradient(180deg,#081722,#06101a);padding:10px 10px;min-width:0}.metric .lbl{font-size:10px;color:#b5c5d4}.metric .val{font-size:18px;font-weight:800;margin-top:8px;white-space:nowrap}.metric .sub{font-size:10px;margin-top:3px}.cyan{color:#00baff}.green{color:var(--green)}.red{color:var(--red)}.gold{color:var(--gold)}.muted{color:var(--muted)}
.chart{grid-column:2;grid-row:1/3;border:1px solid #17435a;border-radius:6px;background:#030b12;overflow:hidden}.chartHead{height:40px;border-bottom:1px solid #153a4e;display:flex;align-items:center;gap:10px;padding:0 10px}.chartHead b{font-size:14px}.chartHead .tf{border:1px solid #2b5d76;padding:4px 8px;color:#d9e7f2;font-size:11px}.quote{color:var(--red);font-size:13px;margin-left:auto}.tools{color:#9eb4c4;letter-spacing:8px}.chartCanvas{height:calc(100% - 40px);position:relative;background:repeating-linear-gradient(0deg,transparent 0 34px,rgba(38,83,105,.22) 35px),repeating-linear-gradient(90deg,transparent 0 65px,rgba(38,83,105,.16) 66px)}.chartCanvas svg{width:100%;height:100%}
.panel{border:1px solid #17435a;border-radius:6px;background:linear-gradient(180deg,rgba(5,18,28,.98),rgba(3,11,18,.98));overflow:hidden}.panelHead{height:39px;display:flex;align-items:center;padding:0 12px;border-bottom:1px solid #153b50;font-size:15px;font-weight:800}.panelHead .accent{color:var(--gold);margin-right:6px}.scanner{grid-column:1;grid-row:2}.tabs{display:flex;gap:6px;padding:7px 12px}.tab{border:1px solid #176085;background:#071a28;color:#b8c8d7;padding:7px 25px;border-radius:3px;font-size:10px}.tab.active{border-color:#e0aa00;color:#fff;background:linear-gradient(180deg,#6f5100,#2e2500);box-shadow:0 0 10px rgba(255,192,0,.18)}.tableWrap{padding:0 12px;overflow:hidden}table{width:100%;border-collapse:collapse;font-size:10px}th{color:#93a7b8;font-weight:500;background:#07131d;padding:6px;border-bottom:1px solid #15384c}td{padding:6px;border-bottom:1px solid #102b3b;text-align:center;white-space:nowrap}.left{text-align:left}.badge{display:inline-block;min-width:70px;padding:3px 7px;border-radius:3px;font-size:9px}.badge.buy{border:1px solid #078a5c;color:#00f09a;background:rgba(0,220,130,.1)}.badge.sell{border:1px solid #a01832;color:#ff405b;background:rgba(255,30,60,.1)}.score{color:var(--green);font-weight:800}.regTrend{color:var(--green)}.regRange{color:var(--gold)}.statusReady{color:var(--green);border:1px solid #078a5c;padding:4px 20px;background:rgba(0,220,130,.08)}.statusWait{color:#9fb0c0;border:1px solid #385166;padding:4px 20px;background:rgba(80,100,120,.1)}.trendbars{display:flex;gap:3px;justify-content:center}.tb{width:15px;height:8px;background:#152636}.tb.onG{background:var(--green);box-shadow:0 0 5px rgba(0,255,150,.4)}.tb.onR{background:var(--red)}
.conf{grid-column:1;grid-row:3}.confBody{display:grid;grid-template-columns:1.4fr .8fr;padding:8px 12px;height:calc(100% - 39px)}.confRows{padding:2px 8px}.confRow{display:grid;grid-template-columns:145px 1fr 45px;align-items:center;gap:8px;margin:7px 0;font-size:10px}.confName{color:#d6e2eb}.confState{color:var(--red);font-weight:700}.barbg{height:10px;background:#122536}.barfill{height:100%;background:linear-gradient(90deg,#ff304f,#ff1e3d)}.gauge{display:flex;flex-direction:column;align-items:center;justify-content:center}.gaugeRing{width:145px;height:145px;border-radius:50%;display:flex;align-items:center;justify-content:center;border:10px solid rgba(255,32,64,.17);outline:2px solid #ff304f;box-shadow:0 0 20px rgba(255,0,50,.2),inset 0 0 20px rgba(255,0,50,.12)}.gaugeScore{font-size:38px;color:var(--red);font-weight:900}.gaugeSub{font-size:9px;color:#ff5267;text-align:center}.entry{margin-top:8px;border:1px solid #e0a800;color:#fff;background:linear-gradient(180deg,#4d3700,#241d00);padding:7px 32px;font-size:11px;font-weight:800}.sellBtn{color:var(--red);font-size:17px;border:1px solid var(--red);padding:5px 55px;margin-bottom:2px}
.log{grid-column:2;grid-row:3/5}.logBody{height:calc(100% - 39px);padding:8px 10px;overflow:hidden;background:#02080e}.logs{height:100%;overflow:hidden;color:#00ff9c;font:10px/1.55 "DejaVu Sans Mono",Consolas,monospace;white-space:pre}.logHdr{display:flex;align-items:center;gap:7px}.real{margin-left:auto;color:var(--green);font-size:9px}
.positions{grid-column:1;grid-row:4}.modulebar{grid-column:1/3;grid-row:5;display:grid;grid-template-columns:repeat(5,1fr) 2.1fr 1.35fr;gap:8px}.module{border:1px solid #17435a;background:#07131f;border-radius:5px;padding:8px 12px;display:flex;align-items:center;gap:10px}.module .mi{font-size:20px}.module b{font-size:10px}.module span{display:block;color:var(--green);font-weight:800;font-size:13px;margin-top:2px}.world{border:1px solid #17435a;border-radius:5px;background:#06131e;position:relative;overflow:hidden}.world:before{content:'◌ ◌ ◌';position:absolute;inset:18px;color:#00b8ff;opacity:.6;font-size:34px;letter-spacing:14px}.sessions{border:1px solid #17435a;border-radius:5px;padding:8px;font-size:9px;background:#06131e}.session{display:flex;justify-content:space-between;margin:3px}.meter{width:70px;height:6px;background:#122536}.meter i{display:block;height:100%;background:var(--green)}
@media(max-width:1280px){.shell{min-width:1180px}.topbar{grid-template-columns:220px repeat(6,1fr)}.content{grid-template-columns:minmax(0,1.8fr) 390px}.tab{padding:7px 16px}.logoTitle{font-size:27px}}
</style></head><body><div class="shell">
<aside class="sidebar"><div class="brandmini">♛</div><nav class="nav"><button class="active" data-target="top"><div class="ico">▥</div>DASHBOARD</button><button data-target="scanner"><div class="ico">⌗</div>SCANNER</button><button data-target="positions"><div class="ico">↔</div>POSITIONS</button><button data-target="analytics"><div class="ico">◔</div>ANALYTICS</button><button data-target="history"><div class="ico">◷</div>HISTORY</button><button data-target="settings"><div class="ico">⚙</div>SETTINGS</button></nav><div class="sideBottom"><div class="miniEarth"></div><div class="v">RBT FX<br><span class="muted">V2.5.0</span></div><div class="sys">SYSTEM <b>ONLINE</b><br>UPTIME <b id="uptime">--</b><br>CPU <b>--</b><br>RAM <b>--</b><br>PING <b>--</b></div><div class="okline">● ALL SYSTEMS OPERATIONAL.</div></div></aside>
<main class="main" id="top"><header class="topbar"><div class="logo"><div class="crown">♛</div><div><div class="logoTitle">RBT FX</div><div class="logoSub">MULTI-PAIR TRADING SYSTEM</div></div></div><div class="statusTile"><div class="statusTop"><span class="dot"></span>PYTHON ENGINE</div><div class="statusMain">ONLINE</div><div class="statusSub">v2.5.0</div></div><div class="statusTile"><div class="statusTop"><span class="dot"></span>MT5 (EXNESS)</div><div class="statusMain">CONNECTED</div><div class="statusSub" id="ping">Ping: --</div></div><div class="statusTile"><div class="statusTop">🔒 ACCOUNT</div><div class="statusMain" id="acctMode">DEMO</div><div class="statusSub" id="acctLogin"># --</div></div><div class="statusTile"><div class="statusTop">◷ SERVER TIME</div><div class="statusMain" id="clock">--:--:--</div><div class="statusSub" id="date">--</div></div><div class="statusTile"><div class="statusTop"><span class="shield">♢</span>RISK MANAGER</div><div class="statusMain">ACTIVE</div><div class="statusSub">0.5% / Trade</div></div><div class="statusTile"><div class="statusTop"><span class="shield">♢</span>DAILY PROTECTION</div><div class="statusMain">OFF</div><div class="statusSub">RESEARCH / Max Loss OFF</div></div><div class="statusTile"><div class="statusTop"><span class="shield">♢</span>WEEKEND GUARD</div><div class="statusMain">ACTIVE</div><div class="statusSub">No new entries</div></div></header>
<section class="content">
<div class="metrics" id="metrics"></div>
<section class="chart"><div class="chartHead"><b id="chartSymbol">XAUUSD</b><span class="tf">M5</span><span class="quote" id="quote">--</span><span class="tools">✣ + ↗ ⛓</span></div><div class="chartCanvas" id="chart"></div></section>
<section class="panel scanner" id="scanner"><div class="panelHead"><span class="accent">✣</span>MULTI-PAIR SCANNER <span style="margin-left:auto;color:var(--green);font-size:10px">● AUTO SCAN &nbsp; ⚙</span></div><div class="tabs"><span class="tab active">ALL</span><span class="tab">FOREX</span><span class="tab">GOLD/SILVER</span><span class="tab">CRYPTO</span><span class="tab">INDEX</span><span class="tab">COMMODITY</span></div><div class="tableWrap"><table><thead><tr><th>#</th><th>SYMBOL</th><th>PRICE</th><th>SPREAD</th><th>REGIME</th><th>SIGNAL</th><th>SCORE</th><th>TREND</th><th>STATUS</th></tr></thead><tbody id="scannerRows"></tbody></table></div></section>
<section class="panel conf" id="analytics"><div class="panelHead"><span class="accent">◉</span>STRATEGY CONFLUENCE <span id="confTitle" style="color:var(--red);margin-left:6px">( XAUUSD - SELL )</span></div><div class="confBody"><div class="confRows" id="confRows"></div><div class="gauge"><div class="gaugeRing"><div><div class="gaugeScore" id="finalScore">--%</div><div class="gaugeSub">FINAL SCORE</div></div></div><div class="sellBtn" id="signalBtn">WAIT ▼</div><div class="entry" id="entryState">WAITING FOR M1 TRIGGER</div></div></div></section>
<section class="panel log"><div class="panelHead"><span class="accent">✣</span>EXECUTION LOG <span class="muted">:: PYTHON ENGINE</span><span class="real">● REAL-TIME</span></div><div class="logBody"><div class="logs" id="logs">Connecting...</div></div></section>
<section class="panel positions" id="positions"><div class="panelHead"><span class="accent">⊙</span>ACTIVE POSITIONS <span id="posCount" style="color:var(--red);margin-left:5px">(0)</span></div><div class="tableWrap"><table><thead><tr><th>#</th><th>SYMBOL</th><th>TYPE</th><th>LOT</th><th>ENTRY</th><th>SL</th><th>TP1</th><th>TP2</th><th>PRICE</th><th>P/L (USD)</th><th>P/L (R)</th><th>STATUS</th></tr></thead><tbody id="posRows"></tbody></table></div></section>
<div class="modulebar" id="history"><div class="module"><div class="mi">⚙</div><div><b>SCANNER</b><span>ON</span></div></div><div class="module"><div class="mi" style="color:var(--purple)">▣</div><div><b>STRATEGY</b><span>ON</span></div></div><div class="module"><div class="mi" style="color:var(--gold)">✦</div><div><b>RISK MANAGER</b><span>ON</span></div></div><div class="module"><div class="mi" style="color:#22ffb0">♢</div><div><b>TRADE ENGINE</b><span>ON</span></div></div><div class="module"><div class="mi" style="color:#24c9ff">◈</div><div><b>TRAILING SYSTEM</b><span>ON</span></div></div><div class="world"></div><div class="sessions"><b>MARKET SESSION</b><div class="session"><span>SYDNEY</span><span class="green">12:12 (Open)</span></div><div class="session"><span>TOKYO</span><span class="green">11:12 (Open)</span></div><div class="session"><span>LONDON</span><span class="red">03:12 (Closed)</span></div><div class="session"><span>NEW YORK</span><span class="red">22:12 (Closed)</span></div></div></div>
</section></main></div>
<script>
const $=id=>document.getElementById(id);let boot=Date.now();
document.querySelectorAll('.nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));b.classList.add('active');const t=$(b.dataset.target);if(t)t.scrollIntoView({behavior:'smooth',block:'start'});});
function money(v,cur){return (v>=0?'+':'')+cur+' '+Number(v||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}
function esc(s){return String(s??'').replace(/[&<>\"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\\':'&#92;','"':'&quot;'}[m]));}
function drawChart(cs){let el=$('chart');if(!cs||cs.length<2){el.innerHTML='';return}let w=el.clientWidth||500,h=el.clientHeight||260,p={l:8,r:42,t:8,b:20},iw=w-p.l-p.r,ih=h-p.t-p.b;let hi=Math.max(...cs.map(x=>x.h)),lo=Math.min(...cs.map(x=>x.l)),span=Math.max(hi-lo,1e-9);let y=v=>p.t+(hi-v)/span*ih,x=i=>p.l+iw*i/(cs.length-1);let step=Math.max(4,iw/cs.length*.72),svg=[];for(let i=0;i<cs.length;i++){let c=cs[i],cx=x(i),yo=y(c.o),yc=y(c.c),yh=y(c.h),yl=y(c.l),up=c.c>=c.o;let col=up?'#00f0a0':'#ff304f';svg.push(`<line x1="${cx.toFixed(1)}" y1="${yh.toFixed(1)}" x2="${cx.toFixed(1)}" y2="${yl.toFixed(1)}" stroke="${col}" stroke-width="1"/>`);let yy=Math.min(yo,yc),hh=Math.max(2,Math.abs(yc-yo));svg.push(`<rect x="${(cx-step/2).toFixed(1)}" y="${yy.toFixed(1)}" width="${step.toFixed(1)}" height="${hh.toFixed(1)}" fill="${col}" opacity=".9"/>`)}svg.push(`<line x1="0" y1="${y(cs[cs.length-1].c).toFixed(1)}" x2="${w-p.r+8}" y2="${y(cs[cs.length-1].c).toFixed(1)}" stroke="#ff304f" stroke-dasharray="5 5" opacity=".8"/>`);el.innerHTML=`<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${svg.join('')}<text x="${w-40}" y="${y(cs[cs.length-1].c)-5}" fill="#fff" font-size="10">${Number(cs[cs.length-1].c).toFixed(2)}</text></svg>`}
async function refresh(){try{let r=await fetch('/api/state',{cache:'no-store'});if(!r.ok)throw Error();let d=await r.json(),a=d.account||{},ds=d.daily||{},cur=a.currency||'USD',p=d.positions||[];let eq=Number(a.equity||0),bal=Number(a.balance||0),dd=bal?((eq-bal)/bal*100):0;let metrics=[['BALANCE',cur+' '+Number(bal).toLocaleString(undefined,{minimumFractionDigits:2}), ''],['EQUITY',cur+' '+Number(eq).toLocaleString(undefined,{minimumFractionDigits:2}),(eq-bal>=0?'+':'')+((eq-bal)/Math.max(bal,1)*100).toFixed(2)+'%'],['DAILY PROFIT',money(Number(ds.profit||0),cur),(Number(ds.profit||0)/Math.max(bal,1)*100).toFixed(2)+'%'],['DAILY LOSS',money(Number(ds.loss||0),cur),(Number(ds.loss||0)/Math.max(bal,1)*100).toFixed(2)+'%'],['DRAWDOWN',dd.toFixed(2)+'%','Peak: '+cur+' '+Number(Math.max(bal,eq)).toLocaleString(undefined,{minimumFractionDigits:2})],['KOMISI / FEE',money(Number(ds.commission||0)+Number(ds.swap||0),cur),'Fee & Swap'],['OPEN POSITIONS',p.length+' / '+d.max_positions,'Float: '+money(Number(ds.floating||0),cur)]]; $('metrics').innerHTML=metrics.map((m,i)=>`<div class="metric"><div class="lbl">${m[0]}</div><div class="val ${i==2?'green':i==3?'red':i==4?'red':''}">${esc(m[1])}</div><div class="sub ${i==3?'red':'green'}">${esc(m[2])}</div></div>`).join('');$('acctMode').textContent=String(a.server||'').toLowerCase().includes('demo')?'DEMO':'LIVE';$('acctLogin').textContent='# '+(a.login||'--');$('ping').textContent='Ping: realtime';let now=new Date();$('clock').textContent=now.toLocaleTimeString('en-GB');$('date').textContent=now.toLocaleDateString('en-GB',{weekday:'short',day:'2-digit',month:'short',year:'numeric'});$('uptime').textContent=Math.floor((Date.now()-boot)/1000)+'s';
let syms=d.requested||[];$('scannerRows').innerHTML=syms.map((s,i)=>{let q=(d.scanner||{})[s]||{};let sig=q.signal||'WAIT',score=Number(q.score||0),reg=q.regime||'--';return `<tr><td>${i+1}</td><td class="left"><b>${s}</b></td><td>${Number(q.price||0).toFixed(s.includes('EUR')?5:2)}</td><td>${Number(q.spread||0).toFixed(2)}</td><td class="${reg.includes('TREND')?'regTrend':'regRange'}">${esc(reg.replace(' UP','').replace(' DOWN',''))}</td><td><span class="badge ${sig==='BUY'?'buy':sig==='SELL'?'sell':''}">${sig}</span></td><td class="score">${score.toFixed(0)}%</td><td><div class="trendbars"><i class="tb ${sig==='SELL'?'onR':'onG'}"></i><i class="tb ${score>70?'onG':''}"></i><i class="tb ${score>80?'onG':''}"></i></div></td><td><span class="${score>=d.min_score?'statusReady':'statusWait'}">${score>=d.min_score?'READY':'WAIT'}</span></td></tr>`}).join('');
let b=d.best||{},sig=b.signal||'WAIT';$('confTitle').textContent=`( ${b.symbol||d.chart_symbol||'XAUUSD'} - ${sig} )`;let names=['Structure','Liquidity/SMC','Trend','Momentum','Volume','Breakout','Mean Reversion'],ss=b.strategies||{};$('confRows').innerHTML=names.map(n=>{let v=Number(ss[n]||0);let state=v>=70?(n==='Liquidity/SMC'?'Sweep':n==='Trend'?'Downtrend':n==='Structure'?'Bearish':'Confirmed'):'Weak';return `<div class="confRow"><span class="confName">● ${n}</span><span class="confState">${state}<div class="barbg"><div class="barfill" style="width:${Math.min(100,v)}%"></div></div></span><b>${v.toFixed(0)}%</b></div>`}).join('');$('finalScore').textContent=Number(b.score||0).toFixed(0)+'%';$('signalBtn').textContent=sig+' ▼';$('signalBtn').style.color=sig==='BUY'?'#00f0a0':sig==='SELL'?'#ff304f':'#d7e2eb';$('entryState').textContent=b.entry_ready?'ENTRY VALID':(b.m1_trigger||'WAITING FOR M1 TRIGGER');
$('posCount').textContent='('+p.length+')';$('posRows').innerHTML=p.length?p.map((x,i)=>`<tr><td>${i+1}</td><td>${esc(x.symbol)}</td><td><span class="badge ${x.type==='BUY'?'buy':'sell'}">${x.type}</span></td><td>${Number(x.volume).toFixed(2)}</td><td>${Number(x.entry).toFixed(5)}</td><td>${Number(x.sl).toFixed(5)}</td><td>--</td><td>${Number(x.tp).toFixed(5)}</td><td>${Number(x.price).toFixed(5)}</td><td class="${x.profit>=0?'green':'red'}">${Number(x.profit).toFixed(2)}</td><td>--</td><td>${x.profit>0?'Trailing':'Open'}</td></tr>`).join(''):'<tr><td colspan="12" style="padding:24px;color:#7f93a7">NO ACTIVE POSITIONS</td></tr>';
$('logs').textContent=(d.logs||[]).slice(-28).map(x=>x[0]).join('\n');$('chartSymbol').textContent=d.chart_symbol||'XAUUSD';let cs=d.chart_candles||[];if(cs.length)$('quote').textContent=Number(cs[cs.length-1].c).toFixed(2)+' '+(sig==='SELL'?' -0.42 (-0.02%)':'');drawChart(cs);}catch(e){$('logs').textContent='[DASHBOARD] '+e;}$('conn');setTimeout(refresh,1000)}refresh();
</script></body></html>"""

def _auth_ok(handler):
    # Local dashboard access is one-click; remote/tunnel access remains password-protected.
    try:
        remote = handler.client_address[0]
        if remote in ("127.0.0.1", "::1", "localhost"):
            return True
    except Exception:
        pass
    if not WEB_PASSWORD:
        return True
    h=handler.headers.get("Authorization","")
    if not h.startswith("Basic "): return False
    try:
        raw=_b64.b64decode(h[6:]).decode()
        u,p=raw.split(":",1)
        return u==WEB_USER and p==WEB_PASSWORD
    except Exception:return False

def web_state():
    # Build a fresh account snapshot and fresh daily accounting on every API call.
    # This keeps the HUD independent of the 3-second scanner cadence.
    try:
        acct=account_info()
    except Exception:
        acct={}
    try:
        ds=daily_stats()
    except Exception:
        ds={"profit":0.0,"loss":0.0,"realized":0.0,"floating":0.0,"commission":0.0,"swap":0.0,"net":0.0}
    try:
        ps=positions()
    except Exception:
        ps=[]
    with state_lock:
        d={
            "connected":STATE["connected"], "engine":STATE["engine"],
            "account":dict(acct or STATE["account"]), "scanner":dict(STATE["scanner"]),
            "positions":list(ps), "logs":list(STATE["logs"]),
            "daily":dict(ds), "daily_pnl":ds.get("net",0.0),
            "daily_max_loss_usd":DAILY_MAX_LOSS_USD,
            "last_error":STATE.get("last_error",""), "requested":REQUESTED_SYMBOLS,
            "min_score":MIN_SCORE, "max_positions":MAX_POSITIONS,
            "chart_prices":list(STATE.get("chart_prices",[])),
            "chart_candles":list(STATE.get("chart_candles",[])),
            "chart_symbol":STATE.get("chart_symbol",""), "loss_streak":STATE.get("loss_streak",0),
            "version":VERSION
        }
    if d["scanner"]:
        d["best"]=max(d["scanner"].values(),key=lambda x:x.get("score",0))
    else:d["best"]={}
    return d

class WebHandler(BaseHTTPRequestHandler):
    def log_message(self,*args): return
    def send_bytes(self,code,ctype,data):
        self.send_response(code);self.send_header("Content-Type",ctype);self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(data)
    def do_GET(self):
        path=urlparse(self.path).path
        # Health is intentionally public so the one-click launcher can verify the server
        # before the browser opens. Trading/account data remains behind the normal auth path.
        if path == "/health":
            self.send_bytes(200,"application/json",b'{"ok":true,"service":"RBT FX"}')
            return
        if not _auth_ok(self):
            self.send_response(401);self.send_header("WWW-Authenticate",'Basic realm="RBT FX"');self.end_headers();return
        if path=="/":self.send_bytes(200,"text/html; charset=utf-8",HTML.encode())
        elif path=="/api/state":self.send_bytes(200,"application/json; charset=utf-8",json.dumps(web_state(),default=str).encode())
        elif path=="/health":self.send_bytes(200,"application/json",b'{"ok":true}')
        elif path.startswith("/fonts/"):
            import pathlib
            name=pathlib.PurePosixPath(path).name
            allowed={"InterDisplay-Regular.otf":"font/otf","InterDisplay-SemiBold.otf":"font/otf","InterDisplay-Bold.otf":"font/otf","InterDisplay-Black.otf":"font/otf","DejaVuSansMono.ttf":"font/ttf"}
            if name in allowed:
                fp=pathlib.Path(__file__).resolve().parent/"fonts"/name
                if fp.exists(): self.send_bytes(200,allowed[name],fp.read_bytes())
                else:self.send_bytes(404,"text/plain",b"font missing")
            else:self.send_bytes(403,"text/plain",b"forbidden")
        else:self.send_bytes(404,"text/plain",b"404")

def web_server():
    try:
        srv=ThreadingHTTPServer((WEB_HOST,WEB_PORT),WebHandler)
        log("WEB","RBT FX",f"Dashboard listening on http://{WEB_HOST}:{WEB_PORT}","OK")
        srv.serve_forever()
    except Exception as e: log("WEB","RBT FX",f"Web server error: {e}","ERROR")

TRADE_META={}

def main():
    # Start the dashboard FIRST. A broker/login problem must never prevent the UI
    # from opening; the HUD can show MT5 OFFLINE and the engine can retry.
    w=threading.Thread(target=web_server,daemon=True)
    w.start()
    print(f"RBT FX WEB: http://127.0.0.1:{WEB_PORT}",flush=True)

    if mt5 is None:
        with state_lock:
            STATE["connected"]=False
            STATE["last_error"]="MetaTrader5 package belum terpasang."
        log("MT5","BROKER","MetaTrader5 package belum terpasang; dashboard tetap online.","ERROR")
        while STATE["engine"]:
            time.sleep(2)
        return

    mt5_path=os.environ.get("RBT_MT5_PATH","").strip()
    started=False
    while STATE["engine"] and not started:
        try:
            try: mt5.shutdown()
            except Exception: pass
            ok=mt5.initialize(path=mt5_path) if mt5_path else mt5.initialize()
            if not ok:
                err=str(mt5.last_error())
                with state_lock: STATE["connected"]=False; STATE["last_error"]=f"MT5 initialize gagal: {err}"
                log("MT5","BROKER",f"Initialize gagal: {err}. Dashboard tetap online; retry...","WARN")
                time.sleep(4)
                continue
            info=mt5.account_info()
            if info is None:
                err="MT5 aktif tetapi akun belum login."
                with state_lock: STATE["connected"]=False; STATE["last_error"]=err
                log("MT5","BROKER",f"{err} Dashboard tetap online; retry...","WARN")
                time.sleep(4)
                continue
            server=str(getattr(info,"server",""))
            if REQUIRE_EXNESS_SERVER and "exness" not in server.lower():
                err=f"Server '{server}' bukan Exness."
                with state_lock: STATE["connected"]=False; STATE["last_error"]=err
                log("MT5","BROKER",f"{err} Dashboard tetap online; retry...","WARN")
                try: mt5.shutdown()
                except Exception: pass
                time.sleep(4)
                continue
            started=True
            with state_lock:
                STATE["connected"]=True
                STATE["account"]={"login":info.login,"currency":info.currency,"balance":safe_float(info.balance),"equity":safe_float(info.equity),"margin":safe_float(info.margin),"free_margin":safe_float(info.margin_free),"leverage":info.leverage,"server":server}
                STATE["last_error"]=""
            t=threading.Thread(target=engine_loop,daemon=True)
            t.start()
            log("SYSTEM","RBTFX",f"Connected {server} login={info.login} currency={info.currency}","OK")
        except Exception as e:
            with state_lock: STATE["connected"]=False; STATE["last_error"]=str(e)
            log("MT5","BROKER",f"Connection exception: {e}. Dashboard tetap online; retry...","WARN")
            time.sleep(4)

    while STATE["engine"]:
        time.sleep(2)
    try: mt5.shutdown()
    except Exception: pass

if __name__=="__main__":
    main()
