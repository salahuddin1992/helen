@echo off
:: Helen — Portable USB launcher (Windows side)
:: Runs the deployment menu without requiring an internet connection.
setlocal enabledelayedexpansion
cd /d "%~dp0"

title Helen LAN Deployment - %CD%

:menu
cls
echo =====================================================
echo   Helen LAN Suite - Portable Deployment Launcher
echo =====================================================
echo.
echo   Detected platform: Windows %PROCESSOR_ARCHITECTURE%
echo   Build location:    %CD%
echo.
echo   Choose what to install:
echo.
echo     [1] Helen-Server         (full LAN backend)
echo     [2] Helen-Server + Admin (server + admin web app)
echo     [3] Helen-Rendezvous     (NAT traversal coordinator)
echo     [4] Helen Desktop        (chat client for end users)
echo     [5] Self-sign all .exe   (silence SmartScreen warnings)
echo     [6] Run health check
echo     [7] Open data folder of an existing install
echo.
echo     [0] Exit
echo.
set /p choice=Enter selection:

if "%choice%"=="1" goto install_server
if "%choice%"=="2" goto install_server_admin
if "%choice%"=="3" goto install_rendezvous
if "%choice%"=="4" goto install_desktop
if "%choice%"=="5" goto self_sign
if "%choice%"=="6" goto health_check
if "%choice%"=="7" goto open_data
if "%choice%"=="0" exit /b 0
goto menu

:install_server
echo.
echo Launching Helen-Server installer...
start /wait "" "windows\Helen-Server-Setup-1.0.0.exe"
goto menu

:install_server_admin
echo.
start /wait "" "windows\Helen-Server-Setup-1.0.0.exe"
echo.
start /wait "" "windows\Helen-Admin-Setup-1.0.0.exe"
goto menu

:install_rendezvous
echo.
echo Launching Helen-Rendezvous installer...
start /wait "" "windows\Helen-Rendezvous-Setup-1.0.0.exe"
goto menu

:install_desktop
echo.
echo Launching Helen Desktop installer...
start /wait "" "windows\Helen Desktop Setup 1.0.0.exe"
goto menu

:self_sign
echo.
echo Running self-signing script (requires admin)...
powershell -ExecutionPolicy Bypass -File "scripts\self-sign-helen.ps1" -ImportToTrustedRoot $true
pause
goto menu

:health_check
echo.
powershell -ExecutionPolicy Bypass -File "scripts\health-check.ps1"
pause
goto menu

:open_data
explorer "C:\Program Files\Helen-Server\_internal\data"
goto menu
