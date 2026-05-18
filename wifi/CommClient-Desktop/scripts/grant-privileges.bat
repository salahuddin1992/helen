@echo off
:: grant-privileges.bat — CMD wrapper that relaunches grant-privileges.ps1
:: with admin rights (UAC prompt). Double-click this file or run from cmd.
setlocal
set "PS1=%~dp0grant-privileges.ps1"

:: Self-elevate via PowerShell Start-Process -Verb RunAs.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -Verb RunAs -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%PS1%'"
exit /b %ERRORLEVEL%
