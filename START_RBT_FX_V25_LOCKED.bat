@echo off
setlocal EnableExtensions EnableDelayedExpansion
title RBT FX v2.5 - LOCKED HUD / ONE CLICK
cd /d "%~dp0"
set "ROOT=%~dp0"
set "ENGINE=%ROOT%RBT_FX_ENGINE_RBT_FX_RESEARCH_NO_DAILY_MAXLOSS.py"
set "DETECT=%ROOT%detect_mt5.ps1"
set "PORT=8787"
set "USER=rbt"
set "PASSFILE=%ROOT%rbt_web_password.txt"
set "PY="
set "MT5EXE="
color 0A
cls

echo ================================================================
echo                 RBT FX v2.5 LOCKED HUD
echo       MT5 + PYTHON + REALTIME DASHBOARD + MULTI-PAIR
echo ================================================================
echo.

where py >nul 2>&1 && set "PY=py"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [ERROR] Python tidak ditemukan.
  pause
  exit /b 1
)
%PY% -c "import sys; print('[OK] Python',sys.version)"

%PY% -c "import MetaTrader5; print('[OK] MetaTrader5 package')" >nul 2>&1
if errorlevel 1 (
  echo [INFO] Installing MetaTrader5 package...
  %PY% -m pip install --upgrade MetaTrader5
  if errorlevel 1 (echo [ERROR] Install MetaTrader5 gagal.&pause&exit /b 1)
)

if exist "%DETECT%" for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%DETECT%"`) do if not defined MT5EXE set "MT5EXE=%%P"
if defined MT5EXE (
  set "RBT_MT5_PATH=%MT5EXE%"
  echo [OK] MT5 target: %MT5EXE%
) else (
  set "RBT_MT5_PATH="
  echo [WARN] terminal64.exe belum ditemukan.
  echo [INFO] Dashboard tetap akan dibuka; engine akan mencoba koneksi MT5 otomatis.
)

set "MT5RUN="
tasklist /FI "IMAGENAME eq terminal64.exe" 2>nul | find /I "terminal64.exe" >nul && set "MT5RUN=YES"
if defined MT5EXE if not defined MT5RUN (
  start "RBT FX MT5" "%MT5EXE%"
  echo [INFO] Menunggu MT5...
  timeout /t 8 /nobreak >nul
) else if defined MT5RUN echo [OK] MT5 terminal sudah berjalan.

if not exist "%PASSFILE%" powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=[guid]::NewGuid().ToString('N').Substring(0,20);Set-Content -NoNewline -Path '%PASSFILE%' -Value $p"
set /p RBT_WEB_PASSWORD=<"%PASSFILE%"
set "RBT_WEB_PORT=%PORT%"
set "PYTHONUNBUFFERED=1"
set "LIVE_TRADING=True"

echo.
echo [OK] Daily Profit : REALTIME
echo [OK] Daily Loss   : REALTIME
echo [OK] Currency     : AUTO FROM MT5 ACCOUNT
echo [OK] Risk / trade : 0.50%%
echo [OK] Portfolio    : 2.00%%
echo [OK] Max position : 5
echo [OK] Daily MaxLoss: OFF (RESEARCH)
echo [OK] Dashboard    : LOCKED HUD v2.5
 echo.

echo [INFO] Starting Python engine...
start "RBT FX PYTHON ENGINE" %PY% "%ENGINE%"

for /l %%I in (1,1,30) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:%PORT%/health -TimeoutSec 1; if($r.StatusCode -eq 200){exit 0}else{exit 1} } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 goto READY
  timeout /t 1 /nobreak >nul
)
echo [ERROR] Dashboard tidak siap dalam 30 detik.
echo [INFO] Cek jendela "RBT FX PYTHON ENGINE" untuk error Python/MT5.
pause
exit /b 1

:READY
echo [OK] Dashboard ONLINE.
start "" "http://127.0.0.1:%PORT%/"
echo.
echo ================================================================
echo RBT FX v2.5 LOCKED HUD ONLINE
echo ================================================================
echo Local dashboard: http://127.0.0.1:%PORT%/
echo Remote password: %RBT_WEB_PASSWORD%
echo.
echo Gunakan akun Exness DEMO untuk forward test.
exit /b 0
