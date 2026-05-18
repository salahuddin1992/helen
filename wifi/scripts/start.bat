@echo off
REM ============================================================
REM CommClient Production Start Script
REM ============================================================
REM Starts the production server for LAN deployment.
REM The Electron desktop app will connect to this server.
REM
REM Usage:
REM   start.bat               — Start with default settings
REM   start.bat --debug       — Start with debug logging
REM   start.bat --port 4000   — Start on custom port
REM ============================================================

setlocal enabledelayedexpansion

set ROOT_DIR=%~dp0..
set SERVER_DIR=%ROOT_DIR%\CommClient-Server

echo.
echo ============================================================
echo   CommClient Server — Production Mode
echo ============================================================
echo.

cd /d "%SERVER_DIR%"

REM Parse arguments
set EXTRA_ARGS=
:parse_args
if "%~1"=="" goto done_args
if "%~1"=="--debug" (
    set DEBUG=true
    set LOG_LEVEL=DEBUG
    echo   Mode: DEBUG
)
if "%~1"=="--port" (
    set PORT=%~2
    echo   Port: %~2
    shift
)
shift
goto parse_args
:done_args

REM Create data directories
if not exist "data" mkdir data
if not exist "data\files" mkdir data\files
if not exist "data\backups" mkdir data\backups

REM Check for built exe
if exist "dist\CommClient-Server\CommClient-Server.exe" (
    echo   Starting from compiled executable...
    echo.
    cd /d "%SERVER_DIR%\dist\CommClient-Server"
    CommClient-Server.exe
    goto end
)

REM Fall back to Python
echo   Starting from Python source...
echo.

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

python run.py

:end
echo.
echo   Server stopped.
