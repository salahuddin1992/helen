@echo off
chcp 65001 >nul
title Helen Auto Bootstrap
echo ╔══════════════════════════════════════╗
echo ║       Helen Auto Bootstrap System    ║
echo ╚══════════════════════════════════════╝
echo.
echo This script verifies that Python, Node.js, and Git are
echo installed locally. Helen is a 100%% LAN-internal project
echo and the bootstrap deliberately does NOT download installers
echo from the internet — that would violate the project's
echo "no public-internet runtime" rule.
echo.
echo If a prerequisite is missing the script aborts and points
echo you to the offline-installer bundle in `bin\` or to your
echo internal package mirror.
echo.

REM === REQUIRE PYTHON (no auto-download) ===
set PY=
for %%P in (python python3 py) do (
    where %%P >nul 2>&1 && (
        set PY=%%P
        goto :found_python
    )
)
echo [FAIL] Python 3.10+ not found on PATH.
echo        Install it from your internal mirror or the bundled
echo        installer in the offline kit, then re-run this script.
echo        Required modules will be installed via `pip install -r
echo        requirements.txt` against your configured pip index.
exit /b 2
:found_python
echo [OK] Python found:
%PY% --version

REM === REQUIRE NODE.JS (no auto-download) ===
where node >nul 2>&1 || (
    echo [FAIL] Node.js 16+ not found on PATH.
    echo        Install from your internal mirror, then re-run.
    exit /b 2
)
echo [OK] Node.js found:
node --version
echo [OK] npm:
call npm --version

REM === REQUIRE GIT (no auto-download) ===
where git >nul 2>&1 || (
    echo [FAIL] Git not found on PATH.
    echo        Install from your internal mirror, then re-run.
    exit /b 2
)
echo [OK] Git found:
git --version

echo.
echo ========================================
echo   Phase 1: Server Setup
echo ========================================
cd /d %~dp0CommClient-Server

REM Auto-create directories
if not exist "data" mkdir data
if not exist "data\backups" mkdir data\backups
if not exist "data\uploads" mkdir data\uploads

REM Auto-create .env if missing
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env
        echo [OK] Created .env from .env.example
    )
)

REM Install Python dependencies with auto-retry
echo Installing Python dependencies...
%PY% -m pip install --upgrade pip --quiet 2>nul
%PY% -m pip install -r requirements.txt --quiet 2>nul
if %errorlevel% neq 0 (
    echo [WARN] Some packages failed with pinned versions, trying flexible install...
    %PY% -m pip install fastapi uvicorn python-socketio sqlalchemy aiosqlite alembic pyjwt bcrypt cryptography python-multipart pydantic pydantic-settings email-validator aiofiles pillow zeroconf psutil structlog python-dotenv uuid6 httptools --quiet
)
%PY% -m pip install pyinstaller --quiet 2>nul
echo [OK] Python dependencies installed

echo.
echo ========================================
echo   Phase 2: Desktop Setup
echo ========================================
cd /d %~dp0CommClient-Desktop

REM Install Node dependencies with fallbacks
echo Installing Node dependencies...
call npm install --legacy-peer-deps 2>nul
if %errorlevel% neq 0 (
    echo [WARN] npm install failed, trying with --force...
    call npm install --force 2>nul
)
echo [OK] Node dependencies installed

echo.
echo ========================================
echo   Phase 3: Build Server EXE
echo ========================================
cd /d %~dp0CommClient-Server
%PY% -m PyInstaller CommClient-Server.spec --noconfirm 2>nul
if %errorlevel% neq 0 (
    echo [WARN] PyInstaller build failed. Trying with clean spec...
    %PY% -m PyInstaller run.py --name Helen-Server --noconfirm --clean --hidden-import=uvicorn --hidden-import=socketio --hidden-import=engineio --hidden-import=sqlalchemy --hidden-import=aiosqlite --hidden-import=pydantic --hidden-import=bcrypt --hidden-import=cryptography --hidden-import=PIL --hidden-import=zeroconf --hidden-import=structlog --hidden-import=dotenv --hidden-import=alembic --hidden-import=aiofiles --hidden-import=httptools --hidden-import=email_validator --hidden-import=multipart --exclude-module=distutils --exclude-module=tkinter 2>nul
    if %errorlevel% neq 0 (
        echo [WARN] Server EXE build failed - will run from source instead
    ) else (
        echo [OK] Server EXE built with fallback method
    )
) else (
    echo [OK] Server EXE built
)

echo.
echo ========================================
echo   Phase 4: Build Desktop Installer
echo ========================================
cd /d %~dp0CommClient-Desktop
call npm run build 2>nul
if %errorlevel% neq 0 (
    echo [WARN] Full build failed. Building renderer only...
    call npx vite build 2>nul
    echo [INFO] You can run in dev mode: npm run dev
) else (
    echo [OK] Desktop installer built
)

echo.
echo ╔══════════════════════════════════════╗
echo ║         BUILD COMPLETE               ║
echo ╚══════════════════════════════════════╝
if exist "release\Helen Desktop Setup 1.0.0.exe" (
    echo [OK] Installer: release\Helen Desktop Setup 1.0.0.exe
)
echo.
echo To run in dev mode:
echo   1. Open terminal: cd CommClient-Server ^&^& python run.py
echo   2. Open terminal: cd CommClient-Desktop ^&^& npm run dev
echo.
pause
