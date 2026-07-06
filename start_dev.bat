@echo off
rem PatentGeyser launcher - double-click to start both servers.
rem Uses the production frontend server (stable); builds first if needed.
setlocal
set ROOT=%~dp0

echo Starting PatentGeyser backend (http://127.0.0.1:8011)...
start "patentgeyser-api" /min cmd /c "cd /d "%ROOT%backend" && .venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8011 > server.log 2>&1"

if not exist "%ROOT%frontend\.next\BUILD_ID" (
    echo No production build found - building frontend once...
    cd /d "%ROOT%frontend"
    call npm run build
)

echo Starting PatentGeyser frontend (http://localhost:3000)...
start "patentgeyser-ui" /min cmd /c "cd /d "%ROOT%frontend" && npm start > server-prod.log 2>&1"

echo.
echo Both servers launching. App: http://localhost:3000
echo Close the two minimized windows (patentgeyser-api / patentgeyser-ui) to stop them.
pause
