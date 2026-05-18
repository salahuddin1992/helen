@echo off
REM ============================================================================
REM CommClient-Server Development Launcher (Batch Version)
REM ============================================================================
REM Starts the server with environment setup, dependency installation,
REM and database migrations. For PowerShell users, use start-server.ps1
REM ============================================================================

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."
set "VENV_DIR=%PROJECT_ROOT%\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"
set "RUN_SCRIPT=%PROJECT_ROOT%\run.py"
set "REQUIREMENTS_FILE=%PROJECT_ROOT%\requirements.txt"
set "ENV_FILE=%PROJECT_ROOT%\.env"
set "MIGRATE_SCRIPT=%SCRIPT_DIR%db_migrate.py"
set "MIN_PYTHON_VERSION=3.10"

set "NO_MIGRATIONS=0"
set "NO_INSTALL=0"

REM Parse command line arguments
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--no-migrations" set "NO_MIGRATIONS=1" & shift & goto parse_args
if /i "%~1"=="--no-install" set "NO_INSTALL=1" & shift & goto parse_args
shift
goto parse_args

:args_done

cls
echo.
echo ======================================================================
echo   CommClient-Server Development Launcher
echo ======================================================================
echo.

REM Validate project structure
echo [*] Validating project structure...
if not exist "%RUN_SCRIPT%" (
    echo [X] Error: run.py not found at %RUN_SCRIPT%
    exit /b 1
)
if not exist "%REQUIREMENTS_FILE%" (
    echo [X] Error: requirements.txt not found at %REQUIREMENTS_FILE%
    exit /b 1
)
if not exist "%ENV_FILE%" (
    echo [X] Error: .env not found at %ENV_FILE%
    exit /b 1
)
echo [O] Project structure validated

REM Find Python
echo.
echo [*] Checking Python installation...
python.exe --version >/dev/null 2>&1
if errorlevel 1 (
    echo [X] Python not found in PATH
    echo     Please install Python 3.10+ from https://www.python.org
    exit /b 1
)

for /f "tokens=2" %%i in ('python.exe --version 2^>^&1') do set "PYTHON_VERSION=%%i"
echo [O] Found Python %PYTHON_VERSION%

REM Create virtual environment if it doesn't exist
if not exist "%VENV_DIR%" (
    echo.
    echo [*] Creating virtual environment...
    python.exe -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [X] Failed to create virtual environment
        exit /b 1
    )
    echo [O] Virtual environment created
) else (
    echo [O] Virtual environment already exists
)

REM Install dependencies
if "%NO_INSTALL%"=="0" (
    echo.
    echo [*] Installing Python dependencies...
    "%VENV_PIP%" install -q -r "%REQUIREMENTS_FILE%"
    if errorlevel 1 (
        echo [X] Failed to install dependencies
        exit /b 1
    )
    echo [O] Dependencies installed
) else (
    echo [!] Skipping dependency installation
)

REM Run migrations
if "%NO_MIGRATIONS%"=="0" (
    echo.
    echo [*] Running database migrations...
    cd /d "%PROJECT_ROOT%"
    "%VENV_PYTHON%" scripts\db_migrate.py upgrade
    if errorlevel 1 (
        echo [!] Migration warnings detected (server may still start)
    ) else (
        echo [O] Migrations completed
    )
) else (
    echo [!] Skipping database migrations
)

REM Display startup info
echo.
echo ======================================================================
echo   Startup Information
echo ======================================================================
echo.
echo   Server Address:    http://localhost:3000
echo   API Docs:          http://localhost:3000/docs
echo   WebSocket:         ws://localhost:3000/socket.io
echo   Project Root:      %PROJECT_ROOT%
echo   Python Venv:       %VENV_DIR%
echo   Database:          ./data/commclient.db
echo.
echo [*] Press Ctrl+C to stop the server
echo.
echo ======================================================================
echo.

REM Start server
cd /d "%PROJECT_ROOT%"
"%VENV_PYTHON%" run.py

REM Capture exit code
set "EXIT_CODE=%errorlevel%"

echo.
echo [*] Server stopped with exit code: %EXIT_CODE%
exit /b %EXIT_CODE%
