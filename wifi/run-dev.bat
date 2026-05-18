@echo off
chcp 65001 >nul
title CommClient Dev Mode
set PY=python
where python >nul 2>&1 || set PY=python3
where %PY% >nul 2>&1 || set PY=py

echo Starting CommClient Server...
start "Server" cmd /k "cd /d %~dp0CommClient-Server && %PY% run.py"
echo Waiting for server to start...
timeout /t 4 /nobreak >nul
echo Starting CommClient Desktop...
start "Desktop" cmd /k "cd /d %~dp0CommClient-Desktop && npm run dev"
echo.
echo [OK] Both started. Close this window anytime.
pause
