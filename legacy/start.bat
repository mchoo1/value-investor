@echo off
title Value Investor App
cd /d "%~dp0"

echo.
echo  ==========================================
echo    Value Investor App - Starting...
echo  ==========================================
echo.

:: Check Python is available
python --version >/dev/null 2>&1
if errorlevel 1 (
    py --version >/dev/null 2>&1
    if errorlevel 1 (
        echo  ERROR: Python not found.
        echo  Please download it from https://python.org
        echo  During install, tick "Add Python to PATH"
        echo.
        pause
        exit /b
    )
    set PYTHON=py
) else (
    set PYTHON=python
)

:: Install dependencies
echo  Installing / checking dependencies...
%PYTHON% -m pip install flask yfinance pandas requests gunicorn -q --disable-pip-version-check

echo.
echo  ==========================================
echo.
echo    PC BROWSER:
echo    http://localhost:5001
echo.
echo    PHONE / TABLET (must be on same WiFi):
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /C:"IPv4 Address"') do (
    for /f "tokens=1" %%B in ("%%A") do (
        echo    http://%%B:5001
    )
)
echo.
echo    FROM ANYWHERE (internet):
echo    See DEPLOY_RAILWAY.md for Railway deployment steps
echo.
echo  ==========================================
echo.
echo  Starting server... Browser opens automatically.
echo  Keep this window open while using the app.
echo  Press Ctrl+C to stop.
echo.

:: Run app
%PYTHON% app.py

pause
