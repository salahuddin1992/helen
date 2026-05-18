@echo off
REM ============================================
REM  CommClient Desktop — Development Launcher
REM  Starts the Electron + Vite dev server
REM ============================================

title CommClient Desktop [DEV]

cd /d "%~dp0.."

echo ===================================
echo  CommClient Desktop - Dev Mode
echo ===================================

REM Check Node.js
where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Node.js not found. Install from https://nodejs.org
    pause
    exit /b 1
)

REM Check node_modules
if not exist "node_modules\" (
    echo [INFO] Installing dependencies...
    call npm install
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] npm install failed
        pause
        exit /b 1
    )
)

echo [INFO] Starting Electron + Vite dev server...
call npm run dev

pause
