@echo off
REM ==============================================================
REM  CommClient — Master Build Script
REM  Builds the full platform: Backend + Desktop + NSIS Installer
REM
REM  Prerequisites:
REM    - Python 3.10+ with pip
REM    - Node.js 18+ with npm
REM    - PyInstaller (pip install pyinstaller)
REM
REM  Usage:
REM    build.bat              — full build (server + desktop + installer)
REM    build.bat server       — build server only
REM    build.bat desktop      — build desktop only
REM    build.bat installer    — package installer only (requires prior builds)
REM ==============================================================

@setlocal enabledelayedexpansion
title CommClient [MASTER BUILD]

set "ROOT=%~dp0"
set "SERVER_DIR=%ROOT%CommClient-Server"
set "DESKTOP_DIR=%ROOT%CommClient-Desktop"
REM PyInstaller spec was renamed to "Helen-Server" — keep this in lockstep
REM with CommClient-Server.spec and electron-builder.yml's extraResources.
set "SERVER_DIST=%SERVER_DIR%\dist\Helen-Server"
set "RELEASE_DIR=%DESKTOP_DIR%\release"
set "BUILD_MODE=%~1"
set "START_TIME=%TIME%"
set "ERRORS=0"

echo.
echo  ===============================================
echo   CommClient — Master Build Pipeline
echo   %DATE% %TIME%
echo  ===============================================
echo.

REM ── Validate prerequisites ─────────────────────────
echo [CHECK] Validating prerequisites...

where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python not found in PATH
    set /a ERRORS+=1
    goto :prereq_fail
)
for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo   Python: %%i

where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Node.js not found in PATH
    set /a ERRORS+=1
    goto :prereq_fail
)
for /f "tokens=*" %%i in ('node --version 2^>^&1') do echo   Node: %%i

where npm >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] npm not found in PATH
    set /a ERRORS+=1
    goto :prereq_fail
)
for /f "tokens=*" %%i in ('npm --version 2^>^&1') do echo   npm: %%i

python -c "import PyInstaller" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [WARN] PyInstaller not installed. Installing...
    pip install pyinstaller
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to install PyInstaller
        set /a ERRORS+=1
        goto :prereq_fail
    )
)
echo   PyInstaller: OK
echo.

REM ── Route to build target ──────────────────────────
if "%BUILD_MODE%"=="server" goto :build_server
if "%BUILD_MODE%"=="desktop" goto :build_desktop
if "%BUILD_MODE%"=="installer" goto :build_installer

REM Full build: server → desktop → installer
goto :build_server

:prereq_fail
echo.
echo [FATAL] Prerequisites check failed with %ERRORS% error(s).
echo         Install missing tools and retry.
pause
exit /b 1

REM ==============================================================
REM  STAGE 1: Build Backend Server (PyInstaller)
REM ==============================================================
:build_server
echo ═══════════════════════════════════════════════
echo  STAGE 1/3: Building Backend Server
echo ═══════════════════════════════════════════════
echo.

cd /d "%SERVER_DIR%"

REM Install Python dependencies
echo [1.1] Installing Python dependencies...
pip install -r requirements.txt >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] pip install failed
    set /a ERRORS+=1
)
pip install pyinstaller >nul 2>&1

REM Clean previous build
echo [1.2] Cleaning previous server build...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"

REM Run PyInstaller
echo [1.3] Running PyInstaller (this may take 2-5 minutes)...
pyinstaller CommClient-Server.spec --noconfirm
if %ERRORLEVEL% neq 0 (
    echo [ERROR] PyInstaller build failed!
    set /a ERRORS+=1
    if "%BUILD_MODE%"=="server" goto :done
    goto :done
)

REM Verify output
if not exist "%SERVER_DIST%\Helen-Server.exe" (
    echo [ERROR] Server executable not found after build!
    set /a ERRORS+=1
    goto :done
)

for %%A in ("%SERVER_DIST%\Helen-Server.exe") do (
    echo [1.4] Server build complete: %%~zA bytes
)
echo   Output: %SERVER_DIST%\
echo.

if "%BUILD_MODE%"=="server" goto :done

REM ==============================================================
REM  STAGE 2: Build Desktop Frontend (Vite + Electron)
REM ==============================================================
:build_desktop
echo ═══════════════════════════════════════════════
echo  STAGE 2/3: Building Desktop Frontend
echo ═══════════════════════════════════════════════
echo.

cd /d "%DESKTOP_DIR%"

REM Install Node dependencies
echo [2.1] Installing Node dependencies...
if not exist "node_modules\" (
    call npm install
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] npm install failed
        set /a ERRORS+=1
        if "%BUILD_MODE%"=="desktop" goto :done
        goto :done
    )
) else (
    echo   node_modules exists, skipping install
)

REM TypeScript check (non-blocking)
echo [2.2] Type checking...
call npx tsc --noEmit 2>nul
if %ERRORLEVEL% neq 0 (
    echo [WARN] TypeScript errors detected — continuing build
)

REM Build renderer + main process with Vite
echo [2.3] Building with Vite (renderer + electron main + preload)...
call npx vite build
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Vite build failed!
    set /a ERRORS+=1
    if "%BUILD_MODE%"=="desktop" goto :done
    goto :done
)

echo [2.4] Desktop frontend build complete
echo   Output: %DESKTOP_DIR%\dist-electron\
echo.

if "%BUILD_MODE%"=="desktop" goto :done

REM ==============================================================
REM  STAGE 3: Package Installer (electron-builder + NSIS)
REM ==============================================================
:build_installer
echo ═══════════════════════════════════════════════
echo  STAGE 3/3: Packaging Windows Installer
echo ═══════════════════════════════════════════════
echo.

cd /d "%DESKTOP_DIR%"

REM Verify server build exists
if not exist "%SERVER_DIST%\Helen-Server.exe" (
    echo [ERROR] Server build not found at %SERVER_DIST%
    echo         Run 'build.bat server' first, or run 'build.bat' for full build
    set /a ERRORS+=1
    goto :done
)

REM Clean previous release
echo [3.1] Cleaning previous installer output...
if exist "release" rmdir /s /q "release"

REM Run electron-builder. The `--config` flag MUST be followed by the
REM YAML path; bare `--config` makes electron-builder fall back to
REM package.json which has the wrong product name ("commclient-desktop"
REM instead of "Helen") and wrong output dir.
echo [3.2] Running electron-builder with NSIS target...
call npx electron-builder --win --config electron-builder.yml
if %ERRORLEVEL% neq 0 (
    echo [ERROR] electron-builder failed!
    set /a ERRORS+=1
    goto :done
)

echo [3.3] Installer build complete
echo.

REM List output files
echo ═══════════════════════════════════════════════
echo  Release artifacts:
echo ═══════════════════════════════════════════════
echo.
dir /b "%RELEASE_DIR%\*.exe" 2>nul
dir /b "%RELEASE_DIR%\*.yml" 2>nul
echo.

REM ==============================================================
REM  DONE
REM ==============================================================
:done
echo.
echo ═══════════════════════════════════════════════
if %ERRORS% gtr 0 (
    echo  BUILD FINISHED WITH %ERRORS% ERROR(S)
) else (
    echo  BUILD SUCCESSFUL
)
echo  Started: %START_TIME%
echo  Ended:   %TIME%
echo ═══════════════════════════════════════════════
echo.

if %ERRORS% equ 0 (
    if exist "%RELEASE_DIR%" (
        echo Opening release folder...
        explorer "%RELEASE_DIR%"
    )
)

cd /d "%ROOT%"
pause
exit /b %ERRORS%
