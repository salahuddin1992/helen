@echo off
setlocal enabledelayedexpansion
REM ╔══════════════════════════════════════════════════════════════╗
REM ║    CommClient — Full Production Build (Backend + Desktop)    ║
REM ║    Outputs: release\CommClient Setup x.y.z.exe               ║
REM ╚══════════════════════════════════════════════════════════════╝
REM
REM Prerequisites:
REM   - Node.js 18+          (node --version)
REM   - Python 3.10+         (python --version)
REM   - PyInstaller          (pip install pyinstaller)
REM   - Git                  (git --version)
REM
REM Usage:
REM   build-all.bat           Full build (server + frontend + installer)
REM   build-all.bat --skip-server    Skip backend build
REM   build-all.bat --skip-frontend  Skip frontend build
REM   build-all.bat --clean          Clean previous builds first
REM
REM Output:
REM   release\CommClient Setup 1.0.0.exe   (NSIS installer)
REM ================================================================

title CommClient [FULL BUILD]
cd /d "%~dp0"

set "START_TIME=%TIME%"
set "SKIP_SERVER=0"
set "SKIP_FRONTEND=0"
set "DO_CLEAN=0"
set "ERRORS=0"

REM Parse arguments
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--skip-server" set "SKIP_SERVER=1"
if /i "%~1"=="--skip-frontend" set "SKIP_FRONTEND=1"
if /i "%~1"=="--clean" set "DO_CLEAN=1"
shift
goto parse_args
:args_done

echo.
echo ================================================================
echo   CommClient — Full Production Build Pipeline
echo ================================================================
echo.

REM ── Prerequisites Check ─────────────────────────────────────

echo [CHECK] Verifying prerequisites...

where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   [FAIL] Node.js not found. Install from https://nodejs.org
    set /a ERRORS+=1
) else (
    for /f "tokens=*" %%v in ('node --version') do echo   [OK] Node.js %%v
)

where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   [FAIL] Python not found. Install from https://python.org
    set /a ERRORS+=1
) else (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo   [OK] %%v
)

if !SKIP_SERVER!==0 (
    where pyinstaller >nul 2>&1
    if %ERRORLEVEL% neq 0 (
        echo   [FAIL] PyInstaller not found. Run: pip install pyinstaller
        set /a ERRORS+=1
    ) else (
        for /f "tokens=*" %%v in ('pyinstaller --version') do echo   [OK] PyInstaller %%v
    )
)

REM Check server source exists
if !SKIP_SERVER!==0 (
    if not exist "..\CommClient-Server\CommClient.spec" (
        if not exist "..\CommClient-Server\CommClient-Server.spec" (
            echo   [FAIL] CommClient-Server directory not found at ..\CommClient-Server\
            set /a ERRORS+=1
        ) else (
            echo   [OK] Server source found (CommClient-Server.spec)
        )
    ) else (
        echo   [OK] Server source found (CommClient.spec)
    )
)

if !ERRORS! gtr 0 (
    echo.
    echo   [ABORT] !ERRORS! prerequisite(s) missing. Fix and retry.
    pause
    exit /b 1
)
echo   [OK] All prerequisites satisfied
echo.

REM ── Clean (optional) ────────────────────────────────────────

if !DO_CLEAN!==1 (
    echo ================================================================
    echo   STEP 0: Cleaning previous builds
    echo ================================================================
    if exist "dist-electron" rmdir /s /q "dist-electron"
    if exist "release" rmdir /s /q "release"
    if exist "..\CommClient-Server\dist" rmdir /s /q "..\CommClient-Server\dist"
    if exist "..\CommClient-Server\build" rmdir /s /q "..\CommClient-Server\build"
    echo   [OK] Cleaned
    echo.
)

REM ═══════════════════════════════════════════════════════════════
REM  STEP 1: Build Backend Server (PyInstaller)
REM ═══════════════════════════════════════════════════════════════

if !SKIP_SERVER!==1 (
    echo [SKIP] Backend server build (--skip-server)
    echo.
    goto step2
)

echo ================================================================
echo   STEP 1/3: Building Backend Server (PyInstaller)
echo ================================================================
echo.

pushd "..\CommClient-Server"

REM Install Python dependencies
echo   [1a] Installing Python dependencies...
pip install -r requirements.txt --quiet --disable-pip-version-check
if %ERRORLEVEL% neq 0 (
    echo   [FAIL] pip install failed
    popd
    pause
    exit /b 1
)
echo   [OK] Python dependencies installed

REM Build with PyInstaller — use CommClient.spec (alias) or CommClient-Server.spec
echo   [1b] Running PyInstaller...
if exist "CommClient.spec" (
    pyinstaller CommClient.spec --clean --noconfirm
) else (
    pyinstaller CommClient-Server.spec --clean --noconfirm
)
if %ERRORLEVEL% neq 0 (
    echo   [FAIL] PyInstaller build failed
    popd
    pause
    exit /b 1
)

REM Verify output
if not exist "dist\CommClient-Server\CommClient-Server.exe" (
    echo   [FAIL] Server executable not found at dist\CommClient-Server\CommClient-Server.exe
    popd
    pause
    exit /b 1
)

for %%A in ("dist\CommClient-Server\CommClient-Server.exe") do (
    set "SERVER_SIZE=%%~zA"
    set /a "SERVER_MB=!SERVER_SIZE! / 1048576"
    echo   [OK] Server built: CommClient-Server.exe (!SERVER_MB! MB)
)

popd
echo.

:step2
REM ═══════════════════════════════════════════════════════════════
REM  STEP 2: Build Frontend (Vite + TypeScript + Electron)
REM ═══════════════════════════════════════════════════════════════

if !SKIP_FRONTEND!==1 (
    echo [SKIP] Frontend build (--skip-frontend)
    echo.
    goto step3
)

echo ================================================================
echo   STEP 2/3: Building Frontend (Vite + Electron)
echo ================================================================
echo.

REM Install Node dependencies
echo   [2a] Installing Node.js dependencies...
if not exist "node_modules\" (
    call npm ci
) else (
    call npm ci --prefer-offline
)
if %ERRORLEVEL% neq 0 (
    echo   [FAIL] npm ci failed
    pause
    exit /b 1
)
echo   [OK] Node dependencies installed

REM TypeScript check (warning only, don't block build)
echo   [2b] Type checking...
call npx tsc --noEmit 2>nul
if %ERRORLEVEL% neq 0 (
    echo   [WARN] TypeScript errors detected — build continues (fix for production)
) else (
    echo   [OK] TypeScript check passed
)

REM Vite build (renderer + main + preload)
echo   [2c] Building with Vite...
call npx vite build
if %ERRORLEVEL% neq 0 (
    echo   [FAIL] Vite build failed
    pause
    exit /b 1
)

REM Verify all outputs
set "BUILD_OK=1"
if not exist "dist-electron\main\index.js" (
    echo   [FAIL] Main process bundle missing
    set "BUILD_OK=0"
)
if not exist "dist-electron\preload\index.js" (
    echo   [FAIL] Preload script missing
    set "BUILD_OK=0"
)
if not exist "dist-electron\renderer\index.html" (
    echo   [FAIL] Renderer build missing
    set "BUILD_OK=0"
)
if !BUILD_OK!==0 (
    echo   [FAIL] Vite build outputs incomplete
    pause
    exit /b 1
)
echo   [OK] Frontend built: main + preload + renderer
echo.

:step3
REM ═══════════════════════════════════════════════════════════════
REM  STEP 3: Package Windows Installer (electron-builder + NSIS)
REM ═══════════════════════════════════════════════════════════════

echo ================================================================
echo   STEP 3/3: Packaging Windows Installer (electron-builder)
echo ================================================================
echo.

REM Verify server binary exists before packaging
if not exist "..\CommClient-Server\dist\CommClient-Server\CommClient-Server.exe" (
    echo   [FAIL] Server binary not found. Run without --skip-server first.
    pause
    exit /b 1
)

echo   [3a] Running electron-builder --win...
call npx electron-builder --win --config electron-builder.yml
if %ERRORLEVEL% neq 0 (
    echo   [FAIL] electron-builder failed
    pause
    exit /b 1
)

REM Find and report the installer
set "INSTALLER_FOUND=0"
for %%F in (release\*.exe) do (
    echo %%F | findstr /i "Setup" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set "INSTALLER_PATH=%%F"
        set "INSTALLER_FOUND=1"
        for %%A in ("%%F") do (
            set "INSTALLER_SIZE=%%~zA"
            set /a "INSTALLER_MB=!INSTALLER_SIZE! / 1048576"
        )
    )
)

if !INSTALLER_FOUND!==0 (
    echo   [FAIL] Installer .exe not found in release\
    pause
    exit /b 1
)

echo.
echo ================================================================
echo   BUILD COMPLETE
echo ================================================================
echo.
echo   Installer: !INSTALLER_PATH!
echo   Size:      !INSTALLER_MB! MB
echo.
echo   Install (GUI):    "!INSTALLER_PATH!"
echo   Install (Silent): "!INSTALLER_PATH!" /S
echo   Deploy (LAN):     Copy to \\fileserver\deploy\ and run /S
echo.
echo ================================================================
echo.

REM Open the release folder
explorer release

pause
exit /b 0
