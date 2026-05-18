@echo off
REM ============================================================
REM CommClient Production Build Script
REM ============================================================
REM Builds the complete CommClient Windows application:
REM   1. Bundles Python backend into CommClient-Server.exe (PyInstaller)
REM   2. Compiles TypeScript and bundles React frontend (Vite)
REM   3. Packages everything into a Windows installer (Electron Builder)
REM
REM Prerequisites:
REM   - Python 3.10+ with pyinstaller installed (pip install pyinstaller)
REM   - Node.js 18+ with npm
REM   - All dependencies installed (run setup.bat first)
REM
REM Usage:
REM   build.bat              — Full production build
REM   build.bat server       — Build backend only
REM   build.bat client       — Build frontend + installer only
REM   build.bat quick        — Skip server rebuild (use existing .exe)
REM ============================================================

setlocal enabledelayedexpansion

set ROOT_DIR=%~dp0..
set SERVER_DIR=%ROOT_DIR%\CommClient-Server
set DESKTOP_DIR=%ROOT_DIR%\CommClient-Desktop
set TIMESTAMP=%DATE:~10,4%%DATE:~4,2%%DATE:~7,2%_%TIME:~0,2%%TIME:~3,2%
set TIMESTAMP=%TIMESTAMP: =0%

set MODE=%1
if "%MODE%"=="" set MODE=all

echo.
echo ============================================================
echo   CommClient Production Build
echo   Started: %DATE% %TIME%
echo ============================================================
echo.

REM ── Step 1: Build Backend ───────────────────────
if "%MODE%"=="client" goto skip_server_build
if "%MODE%"=="quick" goto skip_server_build

echo [1/3] Building CommClient Server (PyInstaller)...
echo.

cd /d "%SERVER_DIR%"

REM Activate venv if exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
    echo       Using virtual environment
)

REM Check pyinstaller
where pyinstaller >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo       Installing PyInstaller...
    pip install pyinstaller --quiet
)

REM Clean previous build
if exist "build" rmdir /s /q build
if exist "dist" rmdir /s /q dist
echo       Cleaned previous build artifacts

REM Build
echo       Running PyInstaller... (this may take a few minutes)
pyinstaller CommClient-Server.spec --noconfirm --clean 2>&1
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: PyInstaller build failed!
    exit /b 1
)

REM Verify output
if not exist "dist\CommClient-Server\CommClient-Server.exe" (
    echo ERROR: CommClient-Server.exe not found in dist/
    exit /b 1
)

for %%A in ("dist\CommClient-Server\CommClient-Server.exe") do set SERVER_SIZE=%%~zA
set /a SERVER_SIZE_MB=%SERVER_SIZE% / 1048576
echo.
echo       [OK] Server built successfully (%SERVER_SIZE_MB% MB)
echo       Output: %SERVER_DIR%\dist\CommClient-Server\
echo.

:skip_server_build

REM ── Step 2: Build Frontend ──────────────────────
if "%MODE%"=="server" goto skip_client_build

echo [2/3] Building CommClient Desktop (TypeScript + Vite)...
echo.

cd /d "%DESKTOP_DIR%"

REM TypeScript check
echo       Running TypeScript compiler...
call npx tsc --noEmit 2>&1
if %ERRORLEVEL% neq 0 (
    echo       [!] TypeScript warnings found (non-blocking)
)

REM Vite production build
echo       Building renderer (Vite)...
call npm run build:renderer 2>&1
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: Frontend build failed!
    exit /b 1
)

echo       [OK] Frontend compiled
echo.

:skip_client_build

REM ── Step 3: Package Installer ───────────────────
if "%MODE%"=="server" goto skip_package

echo [3/3] Packaging Windows Installer (Electron Builder)...
echo.

cd /d "%DESKTOP_DIR%"

REM Ensure server exe is in extraResources location
set SERVER_EXE_SRC=%SERVER_DIR%\dist\CommClient-Server
set SERVER_EXE_DST=%DESKTOP_DIR%\extraResources\CommClient-Server

if exist "%SERVER_EXE_SRC%\CommClient-Server.exe" (
    echo       Copying server build to extraResources...
    if not exist "%SERVER_EXE_DST%" mkdir "%SERVER_EXE_DST%"
    xcopy "%SERVER_EXE_SRC%\*" "%SERVER_EXE_DST%\" /s /e /y /q >nul
    echo       [OK] Server bundled into Electron app
) else (
    if "%MODE%"=="quick" (
        if exist "%SERVER_EXE_DST%\CommClient-Server.exe" (
            echo       Using existing server build in extraResources
        ) else (
            echo       [!] No server build found — installer won't include backend
        )
    ) else (
        echo       [!] Server exe not found — building without embedded server
    )
)

REM Run electron-builder
echo       Running Electron Builder... (this may take several minutes)
call npx electron-builder --win --config 2>&1
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: Electron Builder failed!
    exit /b 1
)

echo       [OK] Installer created
echo.

:skip_package

REM ── Summary ─────────────────────────────────────
echo ============================================================
echo   Build Complete!
echo   Finished: %DATE% %TIME%
echo.

if exist "%DESKTOP_DIR%\release" (
    echo   Installer location:
    dir /b "%DESKTOP_DIR%\release\*.exe" 2>nul && (
        for %%F in ("%DESKTOP_DIR%\release\*.exe") do (
            for %%A in ("%%F") do set INSTALLER_SIZE=%%~zA
            set /a INSTALLER_SIZE_MB=!INSTALLER_SIZE! / 1048576
            echo     %%F (!INSTALLER_SIZE_MB! MB^)
        )
    )
)

echo.
echo   To test the build:
echo     1. Run the installer
echo     2. Launch CommClient from Start Menu or Desktop
echo     3. The embedded server starts automatically
echo ============================================================
