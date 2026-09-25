RBT FX v2.5.1 - DASHBOARD BOOT FIX

IMPORTANT FIXES
1. Dashboard web server starts BEFORE MT5 connection.
2. /health is public so launcher can verify readiness.
3. Localhost dashboard access does not require a password.
4. Remote/tunnel access remains password-protected.
5. If MT5 is missing, not logged in, or wrong broker, dashboard still opens and shows the broker error while retrying.
6. Launcher no longer stops merely because terminal64.exe was not found.
7. Launcher starts Python directly and opens http://127.0.0.1:8787 after health passes.
8. Daily Profit/Loss remain realtime.

RUN
- Extract this folder.
- Run START_RBT_FX_V25_LOCKED.bat.
- Use Exness DEMO for forward testing.

DO NOT use an older V25 launcher.
