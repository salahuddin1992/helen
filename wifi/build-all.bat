@echo off
REM ============================================================
REM  CommClient — one-shot build script
REM ============================================================
REM  Builds the FastAPI backend with PyInstaller, then packages
REM  the Electron desktop client (which bundles the server exe)
REM  into an NSIS installer under CommClient-Desktop\release\.
REM
REM  Auto-compatible with any installed Python (3.8 - 3.13+) and
REM  any installed Node 18+. No environment variables required.
REM
REM  Usage:
REM      build-all.bat
REM ============================================================

setlocal ENABLEDELAYEDEXPANSION
pushd "%~dp0"

echo.
echo === [1/4] Locating Python ===
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYCMD=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PYCMD=python"
    ) else (
        echo ERROR: Python 3.8+ is required but was not found in PATH.
        exit /b 1
    )
)
echo Using Python: !PYCMD!

echo.
echo === [2/4] Bootstrapping server environment ===
pushd CommClient-Server
!PYCMD! auto_setup.py
if errorlevel 1 (
    echo ERROR: auto_setup.py failed
    popd & popd & exit /b 1
)

echo.
echo === [3/4] Building server executable with PyInstaller ===
!PYCMD! -m PyInstaller CommClient-Server.spec --noconfirm --clean
if errorlevel 1 (
    echo ERROR: PyInstaller build failed
    popd & popd & exit /b 1
)
popd

echo.
echo === [4/4] Building desktop installer ===
pushd CommClient-Desktop
where npm >nul 2>nul
if errorlevel 1 (
    echo ERROR: npm not found in PATH. Install Node 18+ first.
    popd & popd & exit /b 1
)
if not exist node_modules (
    echo Installing npm dependencies ^(first run^)...
    call npm install
    if errorlevel 1 (
        echo ERROR: npm install failed
        popd & popd & exit /b 1
    )
)
call npm run build
if errorlevel 1 (
    echo ERROR: desktop build failed
    popd & popd & exit /b 1
)
popd

echo.
echo === Build complete ===
echo Installer: CommClient-Desktop\release\
popd
endlocal
exit /b 0
