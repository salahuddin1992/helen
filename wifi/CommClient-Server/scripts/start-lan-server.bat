@echo off
REM =============================================================================
REM  start-lan-server.bat — double-clickable wrapper around start-lan-server.ps1
REM =============================================================================
setlocal

REM Elevate if not already admin (firewall rules require it).
net session >nul 2>&1
if %errorlevel% NEQ 0 (
    echo [info] Requesting administrator privileges for firewall setup...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0start-lan-server.ps1" %*
set _RC=%errorlevel%

echo.
echo Server stopped with exit code %_RC%.
echo Press any key to close this window...
pause >nul
exit /b %_RC%
