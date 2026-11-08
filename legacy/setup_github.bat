@echo off
title Value Investor - GitHub Setup
cd /d "%~dp0"

echo.
echo  ============================================
echo    Value Investor - GitHub Push Setup
echo  ============================================
echo.

:: Check git is installed
git --version >/dev/null 2>&1
if errorlevel 1 (
    echo  ERROR: Git not found.
    echo  Download from: https://git-scm.com/download/win
    echo  After installing, close this window and run again.
    pause
    exit /b
)

echo  [1/5] Cleaning up any broken git state...
if exist ".git" rmdir /s /q ".git"

echo  [2/5] Initialising git repository...
git init -b main
git config user.email "mchoo1990@gmail.com"
git config user.name "Ming"

echo  [3/5] Staging all files...
git add .
git commit -m "Initial commit: Value Investor App"

echo.
echo  ============================================
echo.
echo  Now open GitHub in your browser:
echo  https://github.com/new
echo.
echo  Create a NEW PRIVATE repository named:
echo    value-investor
echo.
echo  Do NOT tick "Add README" or "Add .gitignore"
echo  (leave it completely empty)
echo.
echo  Once created, GitHub will show you a URL like:
echo    https://github.com/YOUR_USERNAME/value-investor.git
echo.
echo  ============================================
echo.

set /p REPO_URL="Paste your GitHub repo URL here and press Enter: "

if "%REPO_URL%"=="" (
    echo  No URL entered. Exiting.
    pause
    exit /b
)

echo.
echo  [4/5] Connecting to GitHub...
git remote add origin %REPO_URL%

echo  [5/5] Pushing to GitHub...
echo  (A browser window or login prompt may appear — sign in to GitHub)
echo.
git push -u origin main

if errorlevel 1 (
    echo.
    echo  Push failed. Try these steps:
    echo  1. Go to: https://github.com/settings/tokens/new
    echo  2. Name it "value-investor", tick "repo" scope, click Generate
    echo  3. Copy the token
    echo  4. Run: git push -u origin main
    echo     Username: your GitHub username
    echo     Password: paste the token (not your GitHub password)
) else (
    echo.
    echo  ============================================
    echo    SUCCESS! Code is on GitHub.
    echo.
    echo  Next: Deploy to Render (100%% free):
    echo  1. Go to: https://render.com  (sign in with GitHub)
    echo  2. New + > Web Service > Connect value-investor repo
    echo  3. Settings auto-detected from render.yaml
    echo  4. Click Deploy -- you get a free public URL
    echo  ============================================
)

echo.
pause
