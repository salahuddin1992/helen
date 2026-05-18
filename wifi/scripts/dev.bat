@echo off
REM ============================================================
REM CommClient Development Startup Script
REM ============================================================
REM Starts both the Python backend and Electron frontend in dev mode.
REM
REM Prerequisites:
REM   - Python 3.10+ with pip
REM   - Node.js 18+ with npm
REM   - Dependencies installed (run setup.bat first)
REM
REM Usage:
REM   dev.bat              — Start both backend and frontend
REM   dev.bat server       — Start backend only
REM   dev.bat client       — Start frontend only
REM ============================================================

setlocal enabledelayedexpansion

set ROOT_DIR=%~dp0..
set SERVER_DIR=%ROOT_DIR%\CommClient-Server
set DESKTOP_DIR=%ROOT_DIR%\CommClient-Desktop

REM ── Parse argument ──────────────────────────────
set MODE=%1
if "%MODE%"=="" set MODE=all

REM ── Colors ──────────────────────────────────────
echo.
echo ============================================================
echo   CommClient Dev Environment
echo ============================================================
echo.

REM ── Start Backend ───────────────────────────────
if "%MODE%"=="all" goto start_server
if "%MODE%"=="server" goto start_server
goto skip_server

:start_server
echo [1/2] Starting CommClient Server (Python backend)...
echo       Port: 3000
echo       Dir:  %SERVER_DIR%
echo.

cd /d "%SERVER_DIR%"

REM Check Python
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: Python not found in PATH. Install Python 3.10+
    exit /b 1
)

REM Create data directories if needed
if not exist "data" mkdir data
if not exist "data\files" mkdir data\files
if not exist "data\backups" mkdir data\backups

REM Copy .env if missing
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo       Created .env from .env.example
    )
)

REM Start server in background
start "CommClient-Server" cmd /c "cd /d %SERVER_DIR% && set DEBUG=true && set LOG_LEVEL=DEBUG && python run.py"
echo       Server starting...
timeout /t 3 /nobreak >nul
echo.

:skip_server

REM ── Start Frontend ──────────────────────────────
if "%MODE%"=="all" goto start_client
if "%MODE%"=="client" goto start_client
goto skip_client

:start_client
echo [2/2] Starting CommClient Desktop (Electron + Vite)...
echo       Dev server: http://localhost:5173
echo       Dir:  %DESKTOP_DIR%
echo.

cd /d "%DESKTOP_DIR%"

REM Check Node
where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: Node.js not found in PATH. Install Node.js 18+
    exit /b 1
)

REM Copy .env if missing
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo       Created .env from .env.example
    )
)

REM Start Vite + Electron dev
start "CommClient-Desktop" cmd /c "cd /d %DESKTOP_DIR% && npm run dev"
echo       Desktop app starting...
echo.

:skip_client

echo ============================================================
echo   Dev environment is running!
echo.
echo   Backend:  http://localhost:3000
echo   API Docs: http://localhost:3000/docs (DEBUG mode)
echo   Frontend: http://localhost:5173
echo.
echo   Press Ctrl+C in each window to stop.
echo ============================================================
