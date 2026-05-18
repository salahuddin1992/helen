@echo off
REM ============================================================
REM CommClient — Windows Release Build Orchestrator
REM ============================================================
REM Full production pipeline: validate → build server → build client →
REM package installer → verify → stage release artifacts.
REM
REM Prerequisites:
REM   - Python 3.10+ with pyinstaller in PATH (or venv)
REM   - Node.js 18+ with npm
REM   - All dependencies installed (run setup.bat first)
REM   - Icon files in CommClient-Desktop\resources\installer\
REM
REM Usage:
REM   build-release.bat                  — Full release build
REM   build-release.bat --skip-server    — Reuse existing server .exe
REM   build-release.bat --skip-sign      — Skip code signing step
REM   build-release.bat --version 1.2.0  — Override version number
REM   build-release.bat --dir-only       — Unpacked build (no installer)
REM
REM Output:
REM   release\                           — Installer + metadata
REM   release\logs\build-<timestamp>.log — Full build log
REM ============================================================

setlocal enabledelayedexpansion

REM ── Timestamp ──────────────────────────────────────
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value 2^>nul') do set DT=%%I
set TIMESTAMP=%DT:~0,8%_%DT:~8,6%

REM ── Paths ──────────────────────────────────────────
set ROOT_DIR=%~dp0..
set SERVER_DIR=%ROOT_DIR%\CommClient-Server
set DESKTOP_DIR=%ROOT_DIR%\CommClient-Desktop
set RELEASE_DIR=%ROOT_DIR%\release
set BUILD_LOG_DIR=%RELEASE_DIR%\logs
set BUILD_LOG=%BUILD_LOG_DIR%\build-%TIMESTAMP%.log

REM ── Defaults ───────────────────────────────────────
set SKIP_SERVER=0
set SKIP_SIGN=0
set DIR_ONLY=0
set OVERRIDE_VERSION=
set BUILD_OK=1
set STEP=0
set TOTAL_STEPS=6

REM ── Parse arguments ────────────────────────────────
:parse_args
if "%~1"=="" goto done_args
if /i "%~1"=="--skip-server"  ( set SKIP_SERVER=1 & shift & goto parse_args )
if /i "%~1"=="--skip-sign"    ( set SKIP_SIGN=1   & shift & goto parse_args )
if /i "%~1"=="--dir-only"     ( set DIR_ONLY=1    & shift & goto parse_args )
if /i "%~1"=="--version" (
    set OVERRIDE_VERSION=%~2
    shift & shift & goto parse_args
)
shift
goto parse_args
:done_args

REM ── Create log directory ───────────────────────────
if not exist "%BUILD_LOG_DIR%" mkdir "%BUILD_LOG_DIR%"

REM ── Header ─────────────────────────────────────────
call :log "============================================================"
call :log "  CommClient Release Build"
call :log "  Started: %DATE% %TIME%"
call :log "  Root:    %ROOT_DIR%"
call :log "  Options: skip-server=%SKIP_SERVER% skip-sign=%SKIP_SIGN% dir-only=%DIR_ONLY%"
if defined OVERRIDE_VERSION call :log "  Version: %OVERRIDE_VERSION%"
call :log "============================================================"
call :log ""

REM ============================================================
REM  STEP 1 — Validate Prerequisites
REM ============================================================
set /a STEP+=1
call :log "[%STEP%/%TOTAL_STEPS%] Validating prerequisites..."

REM Python
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    call :error "Python not found. Install Python 3.10+ from https://python.org"
    exit /b 1
)
for /f "tokens=*" %%a in ('python --version 2^>^&1') do call :log "  [OK] %%a"

REM PyInstaller (only if building server)
if %SKIP_SERVER%==0 (
    where pyinstaller >nul 2>&1
    if !ERRORLEVEL! neq 0 (
        call :log "  [!] PyInstaller not in PATH — checking venv..."
        if exist "%SERVER_DIR%\venv\Scripts\pyinstaller.exe" (
            call :log "  [OK] PyInstaller found in venv"
        ) else (
            call :error "PyInstaller not found. Run: pip install pyinstaller"
            exit /b 1
        )
    ) else (
        for /f "tokens=*" %%a in ('pyinstaller --version 2^>^&1') do call :log "  [OK] PyInstaller %%a"
    )
)

REM Node.js
where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    call :error "Node.js not found. Install Node.js 18+ from https://nodejs.org"
    exit /b 1
)
for /f "tokens=*" %%a in ('node --version 2^>^&1') do call :log "  [OK] Node.js %%a"

REM npm
where npm >nul 2>&1
if %ERRORLEVEL% neq 0 (
    call :error "npm not found"
    exit /b 1
)
for /f "tokens=*" %%a in ('npm --version 2^>^&1') do call :log "  [OK] npm %%a"

REM Icon check
if not exist "%DESKTOP_DIR%\resources\installer\icon.ico" (
    call :error "Missing icon: %DESKTOP_DIR%\resources\installer\icon.ico"
    exit /b 1
)
call :log "  [OK] Icon files present"

REM Node modules check
if not exist "%DESKTOP_DIR%\node_modules" (
    call :log "  [!] node_modules missing — running npm install..."
    cd /d "%DESKTOP_DIR%"
    call npm install --quiet >>"%BUILD_LOG%" 2>&1
    if !ERRORLEVEL! neq 0 (
        call :error "npm install failed"
        exit /b 1
    )
    call :log "  [OK] Dependencies installed"
)

call :log "  Prerequisites validated."
call :log ""

REM ============================================================
REM  STEP 2 — Version Stamp
REM ============================================================
set /a STEP+=1
call :log "[%STEP%/%TOTAL_STEPS%] Setting version metadata..."

REM Read version from package.json if not overridden
if not defined OVERRIDE_VERSION (
    cd /d "%DESKTOP_DIR%"
    for /f "tokens=2 delims=:, " %%a in ('findstr /C:"\"version\"" package.json') do (
        set OVERRIDE_VERSION=%%~a
    )
)
call :log "  Version: %OVERRIDE_VERSION%"

REM Update electron-builder.yml buildVersion
cd /d "%DESKTOP_DIR%"
powershell -NoProfile -Command "(Get-Content 'electron-builder.yml') -replace 'buildVersion: \".*\"', 'buildVersion: \"%OVERRIDE_VERSION%\"' | Set-Content 'electron-builder.yml'" >>"%BUILD_LOG%" 2>&1
call :log "  [OK] electron-builder.yml updated"

REM Update version_info.py (parse major.minor.patch)
for /f "tokens=1-3 delims=." %%a in ("%OVERRIDE_VERSION%") do (
    set VER_MAJOR=%%a
    set VER_MINOR=%%b
    set VER_PATCH=%%c
)
if not defined VER_PATCH set VER_PATCH=0
call :log "  Version tuple: %VER_MAJOR%.%VER_MINOR%.%VER_PATCH%.0"

cd /d "%SERVER_DIR%"
if exist "version_info.py" (
    powershell -NoProfile -Command "$c = Get-Content 'version_info.py'; $c = $c -replace 'filevers=\(.*?\)', 'filevers=(%VER_MAJOR%, %VER_MINOR%, %VER_PATCH%, 0)'; $c = $c -replace 'prodvers=\(.*?\)', 'prodvers=(%VER_MAJOR%, %VER_MINOR%, %VER_PATCH%, 0)'; $c = $c -replace \"FileVersion'.*?'\", \"FileVersion',      '%OVERRIDE_VERSION%.0'\"; $c = $c -replace \"ProductVersion'.*?'\", \"ProductVersion',   '%OVERRIDE_VERSION%.0'\"; Set-Content 'version_info.py' $c" >>"%BUILD_LOG%" 2>&1
    call :log "  [OK] version_info.py updated"
)

call :log ""

REM ============================================================
REM  STEP 3 — Build Backend (PyInstaller)
REM ============================================================
set /a STEP+=1

if %SKIP_SERVER%==1 (
    call :log "[%STEP%/%TOTAL_STEPS%] Skipping server build (--skip-server)"
    if exist "%SERVER_DIR%\dist\CommClient-Server\CommClient-Server.exe" (
        call :log "  Using existing server build"
    ) else (
        call :warn "No existing server build found — installer will lack embedded backend"
    )
    call :log ""
    goto step4
)

call :log "[%STEP%/%TOTAL_STEPS%] Building CommClient-Server (PyInstaller)..."

cd /d "%SERVER_DIR%"

REM Activate venv if exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
    call :log "  Activated virtual environment"
)

REM Clean previous artifacts
if exist "build" ( rmdir /s /q build 2>nul )
if exist "dist"  ( rmdir /s /q dist  2>nul )
call :log "  Cleaned previous build artifacts"

REM Run PyInstaller
call :log "  Running PyInstaller... (this may take 2-5 minutes)"
pyinstaller CommClient-Server.spec --noconfirm --clean >>"%BUILD_LOG%" 2>&1
if %ERRORLEVEL% neq 0 (
    call :error "PyInstaller build failed! Check log: %BUILD_LOG%"
    exit /b 1
)

REM Verify output
if not exist "dist\CommClient-Server\CommClient-Server.exe" (
    call :error "CommClient-Server.exe not found in dist\"
    exit /b 1
)

for %%A in ("dist\CommClient-Server\CommClient-Server.exe") do set SERVER_SIZE=%%~zA
set /a SERVER_SIZE_MB=%SERVER_SIZE% / 1048576
call :log "  [OK] Server built: %SERVER_SIZE_MB% MB"

REM Count total files in dist
set FILE_COUNT=0
for /r "dist\CommClient-Server" %%F in (*) do set /a FILE_COUNT+=1
call :log "  Server bundle: %FILE_COUNT% files"
call :log ""

:step4

REM ============================================================
REM  STEP 4 — Build Frontend (TypeScript + Vite + Electron)
REM ============================================================
set /a STEP+=1
call :log "[%STEP%/%TOTAL_STEPS%] Building CommClient-Desktop (Vite + Electron)..."

cd /d "%DESKTOP_DIR%"

REM TypeScript compilation check
call :log "  Running TypeScript type check..."
call npx tsc --noEmit >>"%BUILD_LOG%" 2>&1
if %ERRORLEVEL% neq 0 (
    call :warn "TypeScript type check reported warnings (non-blocking)"
)

REM Clean dist-electron
if exist "dist-electron" ( rmdir /s /q dist-electron 2>nul )
call :log "  Cleaned dist-electron"

REM Vite production build (main + preload + renderer)
call :log "  Running Vite build..."
call npm run build:renderer >>"%BUILD_LOG%" 2>&1
if %ERRORLEVEL% neq 0 (
    call :error "Vite/Electron build failed! Check log: %BUILD_LOG%"
    exit /b 1
)

REM Verify dist-electron output
if not exist "dist-electron\main\index.js" (
    call :error "Main process not compiled: dist-electron\main\index.js missing"
    exit /b 1
)
if not exist "dist-electron\renderer\index.html" (
    call :error "Renderer not compiled: dist-electron\renderer\index.html missing"
    exit /b 1
)
call :log "  [OK] Frontend compiled (main + preload + renderer)"
call :log ""

REM ============================================================
REM  STEP 5 — Package Windows Installer (Electron Builder)
REM ============================================================
set /a STEP+=1
call :log "[%STEP%/%TOTAL_STEPS%] Packaging Windows installer (Electron Builder)..."

cd /d "%DESKTOP_DIR%"

REM Clean previous release
if exist "release" ( rmdir /s /q release 2>nul )

REM Verify server bundle is accessible for extraResources
if exist "%SERVER_DIR%\dist\CommClient-Server\CommClient-Server.exe" (
    call :log "  Server bundle found — will be included in installer"
) else (
    call :warn "Server bundle NOT found — installer will lack embedded backend"
)

REM Run electron-builder
if %DIR_ONLY%==1 (
    call :log "  Building unpacked directory (--dir)..."
    call npx electron-builder --win --dir --config electron-builder.yml >>"%BUILD_LOG%" 2>&1
) else (
    call :log "  Building NSIS installer..."
    call npx electron-builder --win --config electron-builder.yml >>"%BUILD_LOG%" 2>&1
)

if %ERRORLEVEL% neq 0 (
    call :error "Electron Builder failed! Check log: %BUILD_LOG%"
    exit /b 1
)

REM Verify installer output
set INSTALLER_FOUND=0
for %%F in ("release\*.exe") do (
    set INSTALLER_FOUND=1
    for %%A in ("%%F") do set INS_SIZE=%%~zA
    set /a INS_SIZE_MB=!INS_SIZE! / 1048576
    call :log "  [OK] Installer: %%~nxF (!INS_SIZE_MB! MB)"
)

if %DIR_ONLY%==1 (
    if exist "release\win-unpacked\CommClient.exe" (
        call :log "  [OK] Unpacked build: release\win-unpacked\"
    )
) else (
    if %INSTALLER_FOUND%==0 (
        call :warn "No .exe installer found in release\"
    )
)

call :log ""

REM ============================================================
REM  STEP 6 — Verify & Stage Release Artifacts
REM ============================================================
set /a STEP+=1
call :log "[%STEP%/%TOTAL_STEPS%] Verifying release artifacts..."

REM Verify installer internal structure (unpacked or NSIS)
if %DIR_ONLY%==1 (
    set VERIFY_DIR=release\win-unpacked
) else (
    set VERIFY_DIR=release\win-unpacked
)

if exist "!VERIFY_DIR!\CommClient.exe" (
    call :log "  [OK] CommClient.exe present"
) else (
    call :log "  [!] CommClient.exe not found in unpacked dir (OK if NSIS-only)"
)

if exist "!VERIFY_DIR!\resources\server\CommClient-Server.exe" (
    call :log "  [OK] Embedded server: CommClient-Server.exe present"
) else (
    if exist "%SERVER_DIR%\dist\CommClient-Server\CommClient-Server.exe" (
        call :warn "Server .exe exists but may not be bundled — check extraResources path"
    ) else (
        call :log "  [!] No embedded server (expected if --skip-server)"
    )
)

REM Generate build manifest
set MANIFEST=%RELEASE_DIR%\BUILD_MANIFEST.txt
echo CommClient Release Build Manifest > "%MANIFEST%"
echo ================================= >> "%MANIFEST%"
echo Build Date:     %DATE% %TIME% >> "%MANIFEST%"
echo Version:        %OVERRIDE_VERSION% >> "%MANIFEST%"
echo Platform:       Windows x64 >> "%MANIFEST%"
echo Architecture:   NSIS installer >> "%MANIFEST%"
echo Skip Server:    %SKIP_SERVER% >> "%MANIFEST%"
echo Dir Only:       %DIR_ONLY% >> "%MANIFEST%"
echo. >> "%MANIFEST%"
echo Files: >> "%MANIFEST%"
dir /b "%RELEASE_DIR%\*.exe" 2>nul >> "%MANIFEST%"
dir /b "%RELEASE_DIR%\*.yml" 2>nul >> "%MANIFEST%"
dir /b "%RELEASE_DIR%\*.yaml" 2>nul >> "%MANIFEST%"
dir /b "%RELEASE_DIR%\*.blockmap" 2>nul >> "%MANIFEST%"
echo. >> "%MANIFEST%"
echo Build Log: logs\build-%TIMESTAMP%.log >> "%MANIFEST%"

call :log "  [OK] Build manifest: %MANIFEST%"
call :log ""

REM ── Final Summary ──────────────────────────────────
call :log "============================================================"
call :log "  BUILD COMPLETE — CommClient v%OVERRIDE_VERSION%"
call :log "  Finished: %DATE% %TIME%"
call :log ""
call :log "  Release artifacts:"

for %%F in ("%RELEASE_DIR%\*.exe") do (
    for %%A in ("%%F") do set FS=%%~zA
    set /a FS_MB=!FS! / 1048576
    call :log "    Installer: %%~nxF (!FS_MB! MB)"
)

if exist "%RELEASE_DIR%\win-unpacked" (
    call :log "    Unpacked:  release\win-unpacked\"
)

call :log ""
call :log "  Build log: %BUILD_LOG%"
call :log "  Manifest:  %MANIFEST%"
call :log ""
call :log "  Next steps:"
call :log "    1. Test: Run the installer on a clean Windows machine"
call :log "    2. Verify: CommClient launches and connects on LAN"
call :log "    3. Distribute: Copy installer to shared LAN folder"
call :log "============================================================"

exit /b 0

REM ── Logging Functions ──────────────────────────────
:log
echo %~1
echo %~1 >>"%BUILD_LOG%" 2>nul
goto :eof

:warn
echo   [WARN] %~1
echo   [WARN] %~1 >>"%BUILD_LOG%" 2>nul
goto :eof

:error
echo.
echo   [ERROR] %~1
echo   [ERROR] %~1 >>"%BUILD_LOG%" 2>nul
set BUILD_OK=0
goto :eof
