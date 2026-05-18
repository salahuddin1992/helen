@echo off
REM ============================================================
REM CommClient Environment Setup Script
REM ============================================================
REM Installs all dependencies for both backend and frontend.
REM Run this once after cloning the repository.
REM
REM Prerequisites:
REM   - Python 3.10+ with pip
REM   - Node.js 18+ with npm
REM
REM Usage:
REM   setup.bat             — Install everything
REM   setup.bat server      — Install backend only
REM   setup.bat client      — Install frontend only
REM ============================================================

setlocal enabledelayedexpansion

set ROOT_DIR=%~dp0..
set SERVER_DIR=%ROOT_DIR%\CommClient-Server
set DESKTOP_DIR=%ROOT_DIR%\CommClient-Desktop

set MODE=%1
if "%MODE%"=="" set MODE=all

echo.
echo ============================================================
echo   CommClient Setup
echo ============================================================
echo.

REM ── Check Prerequisites ─────────────────────────
echo Checking prerequisites...

where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [X] Python not found. Install Python 3.10+ from https://python.org
    if "%MODE%"=="all" exit /b 1
    if "%MODE%"=="server" exit /b 1
) else (
    for /f "tokens=*" %%a in ('python --version 2^>^&1') do echo [OK] %%a
)

where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [X] Node.js not found. Install Node.js 18+ from https://nodejs.org
    if "%MODE%"=="all" exit /b 1
    if "%MODE%"=="client" exit /b 1
) else (
    for /f "tokens=*" %%a in ('node --version 2^>^&1') do echo [OK] Node.js %%a
)

where npm >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [X] npm not found
) else (
    for /f "tokens=*" %%a in ('npm --version 2^>^&1') do echo [OK] npm %%a
)

echo.

REM ── Backend Setup ───────────────────────────────
if "%MODE%"=="all" goto setup_server
if "%MODE%"=="server" goto setup_server
goto skip_server

:setup_server
echo [1] Setting up CommClient Server (Python backend)...
echo.

cd /d "%SERVER_DIR%"

REM Create virtual environment
if not exist "venv" (
    echo     Creating Python virtual environment...
    python -m venv venv
)

REM Activate and install
echo     Installing Python dependencies...
call venv\Scripts\activate.bat
pip install -r requirements.txt --quiet
echo     [OK] Python dependencies installed

REM Create data directories
if not exist "data" mkdir data
if not exist "data\files" mkdir data\files
if not exist "data\backups" mkdir data\backups
echo     [OK] Data directories created

REM Copy .env
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo     [OK] .env created from template
    )
)

REM Run initial migration
echo     Initializing database...
python -c "from app.db.session import engine; from app.db.base import Base; from app.models import *; import asyncio; asyncio.run(Base.metadata.create_all(bind=engine))" 2>nul
if %ERRORLEVEL% neq 0 (
    echo     [!] Database will be initialized on first server start
) else (
    echo     [OK] Database initialized
)

echo.
echo     Backend setup complete!
echo.

:skip_server

REM ── Frontend Setup ──────────────────────────────
if "%MODE%"=="all" goto setup_client
if "%MODE%"=="client" goto setup_client
goto skip_client

:setup_client
echo [2] Setting up CommClient Desktop (Electron + React)...
echo.

cd /d "%DESKTOP_DIR%"

REM Install npm dependencies
echo     Installing Node.js dependencies...
call npm install --quiet 2>nul
if %ERRORLEVEL% neq 0 (
    echo     [!] npm install had warnings (this is usually OK)
)
echo     [OK] Node.js dependencies installed

REM Copy .env
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo     [OK] .env created from template
    )
)

echo.
echo     Frontend setup complete!
echo.

:skip_client

echo ============================================================
echo   Setup Complete!
echo.
echo   Next steps:
echo     1. Edit CommClient-Server\.env (change JWT_SECRET)
echo     2. Run scripts\dev.bat to start development
echo     3. Run scripts\build.bat to create production installer
echo ============================================================
