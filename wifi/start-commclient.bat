@echo off
REM ============================================
REM  CommClient — Master Launcher
REM  Starts both Server and Desktop in parallel
REM ============================================

title CommClient Launcher

echo ===================================
echo  CommClient — LAN Communication
echo ===================================
echo.
echo  Starting Server + Desktop...
echo  Press Ctrl+C in each window to stop.
echo.

REM Get the directory this script lives in
set "ROOT=%~dp0"

REM ── Start Server (in new window) ───────────
echo [1/2] Launching Server...
start "CommClient Server" cmd /k "cd /d "%ROOT%CommClient-Server" && call scripts\start-server.bat"

REM Wait for server to be ready
echo [INFO] Waiting 5 seconds for server startup...
timeout /t 5 /nobreak >nul

REM ── Start Desktop (in new window) ──────────
echo [2/2] Launching Desktop...
start "CommClient Desktop" cmd /k "cd /d "%ROOT%CommClient-Desktop" && call scripts\start-desktop.bat"

echo.
echo ===================================
echo  Both processes launched.
echo  Server:  http://localhost:3000
echo  Desktop: Electron window
echo ===================================
echo.
echo  Close this window anytime.
echo  Server and Desktop run independently.
echo.

pause
