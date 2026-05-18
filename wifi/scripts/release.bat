@echo off
REM ============================================================
REM CommClient — Release Staging Script
REM ============================================================
REM Stages the final release artifacts into a distributable folder
REM ready for LAN deployment (USB, shared folder, etc.)
REM
REM This script runs AFTER build-release.bat and:
REM   1. Validates the build output exists
REM   2. Copies the installer to a clean release staging directory
REM   3. Generates SHA256 checksums for integrity verification
REM   4. Creates a deployment README with install instructions
REM   5. Packages everything into a distributable ZIP (optional)
REM
REM Usage:
REM   release.bat                        — Stage release artifacts
REM   release.bat --zip                  — Also create a .zip archive
REM   release.bat --output D:\releases   — Custom output directory
REM ============================================================

setlocal enabledelayedexpansion

REM ── Timestamp ──────────────────────────────────────
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value 2^>nul') do set DT=%%I
set TIMESTAMP=%DT:~0,8%

REM ── Paths ──────────────────────────────────────────
set ROOT_DIR=%~dp0..
set DESKTOP_DIR=%ROOT_DIR%\CommClient-Desktop
set BUILD_RELEASE_DIR=%DESKTOP_DIR%\release

REM ── Defaults ───────────────────────────────────────
set CREATE_ZIP=0
set OUTPUT_DIR=%ROOT_DIR%\release-staging

REM ── Parse arguments ────────────────────────────────
:parse_args
if "%~1"=="" goto done_args
if /i "%~1"=="--zip"    ( set CREATE_ZIP=1 & shift & goto parse_args )
if /i "%~1"=="--output" ( set OUTPUT_DIR=%~2 & shift & shift & goto parse_args )
shift
goto parse_args
:done_args

REM ── Read version from package.json ─────────────────
cd /d "%DESKTOP_DIR%"
for /f "tokens=2 delims=:, " %%a in ('findstr /C:"\"version\"" package.json') do set VERSION=%%~a
if not defined VERSION set VERSION=1.0.0

set STAGING_DIR=%OUTPUT_DIR%\CommClient-v%VERSION%-win-x64

echo.
echo ============================================================
echo   CommClient Release Staging — v%VERSION%
echo ============================================================
echo.

REM ── Validate build output ──────────────────────────
echo [1/5] Validating build output...

set INSTALLER_EXE=
for %%F in ("%BUILD_RELEASE_DIR%\*.exe") do (
    set INSTALLER_EXE=%%F
    echo   Found: %%~nxF
)

if not defined INSTALLER_EXE (
    echo.
    echo   [ERROR] No installer .exe found in %BUILD_RELEASE_DIR%
    echo   Run build-release.bat first!
    exit /b 1
)

echo   [OK] Build output validated
echo.

REM ── Create staging directory ───────────────────────
echo [2/5] Creating staging directory...

if exist "%STAGING_DIR%" (
    echo   Cleaning previous staging: %STAGING_DIR%
    rmdir /s /q "%STAGING_DIR%"
)
mkdir "%STAGING_DIR%"

echo   Staging: %STAGING_DIR%
echo.

REM ── Copy artifacts ─────────────────────────────────
echo [3/5] Copying release artifacts...

REM Copy installer
copy "%INSTALLER_EXE%" "%STAGING_DIR%\" >nul
for %%F in ("%STAGING_DIR%\*.exe") do echo   [OK] %%~nxF

REM Copy blockmap if exists
if exist "%BUILD_RELEASE_DIR%\*.blockmap" (
    copy "%BUILD_RELEASE_DIR%\*.blockmap" "%STAGING_DIR%\" >nul
    echo   [OK] Blockmap copied
)

REM Copy build manifest if exists
if exist "%BUILD_RELEASE_DIR%\BUILD_MANIFEST.txt" (
    copy "%BUILD_RELEASE_DIR%\BUILD_MANIFEST.txt" "%STAGING_DIR%\" >nul
    echo   [OK] Build manifest copied
)

echo.

REM ── Generate checksums ─────────────────────────────
echo [4/5] Generating SHA256 checksums...

set CHECKSUM_FILE=%STAGING_DIR%\SHA256SUMS.txt
echo CommClient v%VERSION% — SHA256 Checksums > "%CHECKSUM_FILE%"
echo Generated: %DATE% %TIME% >> "%CHECKSUM_FILE%"
echo. >> "%CHECKSUM_FILE%"

for %%F in ("%STAGING_DIR%\*.exe") do (
    for /f "tokens=*" %%H in ('certutil -hashfile "%%F" SHA256 ^| findstr /v ":" ^| findstr /v "CertUtil"') do (
        echo %%H  %%~nxF >> "%CHECKSUM_FILE%"
        echo   %%~nxF: %%H
    )
)

echo   [OK] Checksums: SHA256SUMS.txt
echo.

REM ── Create deployment README ───────────────────────
echo [5/5] Creating deployment documentation...

set README=%STAGING_DIR%\INSTALL.txt
(
echo ============================================================
echo   CommClient v%VERSION% — Windows Installation Guide
echo ============================================================
echo.
echo SYSTEM REQUIREMENTS
echo   - Windows 10 x64 or later
echo   - 200 MB free disk space
echo   - LAN/WiFi network connection
echo   - No internet required ^(LAN-only application^)
echo.
echo INSTALLATION
echo   1. Run the installer: CommClient Setup %VERSION%.exe
echo   2. Follow the installation wizard
echo   3. Choose installation directory ^(default: per-user install^)
echo   4. Click "Install"
echo   5. Launch CommClient from the Desktop shortcut or Start Menu
echo.
echo FIRST RUN
echo   - The embedded backend server starts automatically
echo   - Register a new account ^(first user becomes admin^)
echo   - Other users on the same LAN can register and connect
echo   - Server discovery is automatic via mDNS + UDP broadcast
echo.
echo DATA LOCATIONS
echo   - Database:  %%APPDATA%%\CommClient\data\commclient.db
echo   - Uploads:   %%APPDATA%%\CommClient\data\files\
echo   - Logs:      %%APPDATA%%\CommClient\logs\
echo   - Config:    %%APPDATA%%\CommClient\.credentials
echo.
echo FIREWALL
echo   CommClient needs these ports open on your LAN:
echo   - TCP 3000  ^(HTTP API + WebSocket^)
echo   - UDP 41234 ^(LAN server discovery^)
echo   - TCP/UDP 40000-49999 ^(WebRTC media — reserved for mediasoup^)
echo.
echo   The installer does NOT auto-create firewall rules.
echo   On first launch, Windows may show a firewall prompt — click "Allow".
echo   If issues persist, manually add rules:
echo     netsh advfirewall firewall add rule name="CommClient API" ^
echo       dir=in action=allow protocol=tcp localport=3000
echo     netsh advfirewall firewall add rule name="CommClient Discovery" ^
echo       dir=in action=allow protocol=udp localport=41234
echo.
echo UNINSTALLATION
echo   - Use "Add or Remove Programs" in Windows Settings
echo   - User data in %%APPDATA%%\CommClient\ is preserved by default
echo   - To fully remove, delete %%APPDATA%%\CommClient\ manually
echo.
echo LAN DEPLOYMENT ^(Multiple Machines^)
echo   - Install on each machine that needs CommClient
echo   - One machine acts as the server ^(the first to launch^)
echo   - Other clients auto-discover the server via mDNS
echo   - All communication stays within the local network
echo.
echo TROUBLESHOOTING
echo   1. Server won't start:
echo      - Check logs at %%APPDATA%%\CommClient\logs\
echo      - Ensure port 3000 is not in use ^(netstat -an ^| find "3000"^)
echo   2. Clients can't discover server:
echo      - Verify all machines are on the same subnet
echo      - Check Windows Firewall allows UDP 41234
echo      - Try connecting manually via server IP
echo   3. Calls have no audio/video:
echo      - Check microphone/camera permissions in Windows Settings
echo      - Ensure WebRTC ports ^(40000-49999^) aren't blocked
echo.
echo INTEGRITY VERIFICATION
echo   Compare SHA256 checksums from SHA256SUMS.txt:
echo     certutil -hashfile "CommClient Setup %VERSION%.exe" SHA256
echo.
echo ============================================================
echo   CommClient Team — LAN Communication Platform
echo   Build date: %DATE%
echo ============================================================
) > "%README%"

echo   [OK] INSTALL.txt created
echo.

REM ── Optional ZIP ───────────────────────────────────
if %CREATE_ZIP%==1 (
    echo [EXTRA] Creating ZIP archive...
    set ZIP_FILE=%OUTPUT_DIR%\CommClient-v%VERSION%-win-x64-%TIMESTAMP%.zip

    where powershell >nul 2>&1
    if !ERRORLEVEL!==0 (
        powershell -NoProfile -Command "Compress-Archive -Path '%STAGING_DIR%\*' -DestinationPath '!ZIP_FILE!' -Force"
        if exist "!ZIP_FILE!" (
            for %%A in ("!ZIP_FILE!") do set ZIP_SIZE=%%~zA
            set /a ZIP_SIZE_MB=!ZIP_SIZE! / 1048576
            echo   [OK] ZIP: !ZIP_FILE! ^(!ZIP_SIZE_MB! MB^)
        ) else (
            echo   [WARN] ZIP creation failed
        )
    ) else (
        echo   [WARN] PowerShell not available — skipping ZIP
    )
    echo.
)

REM ── Summary ────────────────────────────────────────
echo ============================================================
echo   Release Staging Complete — CommClient v%VERSION%
echo.
echo   Staging directory:
echo     %STAGING_DIR%
echo.
echo   Contents:
dir /b "%STAGING_DIR%" 2>nul | findstr /v /c:"." >nul
for %%F in ("%STAGING_DIR%\*") do (
    for %%A in ("%%F") do set FS=%%~zA
    set /a FS_MB=!FS! / 1048576
    echo     %%~nxF ^(!FS_MB! MB^)
)
echo.
echo   Distribution:
echo     1. Copy the staging folder to a USB drive or shared LAN folder
echo     2. Users run the installer from there
echo     3. No internet required
echo ============================================================

exit /b 0
