@echo off
REM ============================================
REM  CommClient Desktop — Production Build
REM  Compiles TypeScript, bundles with Vite,
REM  and packages with electron-builder for Windows
REM ============================================

title CommClient Desktop [BUILD]

cd /d "%~dp0.."

echo ===================================
echo  CommClient Desktop - Build
echo ===================================
echo.

REM Check Node.js
where node >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Node.js not found.
    pause
    exit /b 1
)

REM Install dependencies
if not exist "node_modules\" (
    echo [STEP 1/4] Installing dependencies...
    call npm install
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] npm install failed
        pause
        exit /b 1
    )
) else (
    echo [STEP 1/4] Dependencies OK
)

REM TypeScript check
echo [STEP 2/4] Type checking...
call npx tsc --noEmit
if %ERRORLEVEL% neq 0 (
    echo [WARNING] TypeScript errors detected. Build will continue.
    echo           Fix type errors for production-quality builds.
)

REM Vite build
echo [STEP 3/4] Building renderer + electron main...
call npx vite build
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Vite build failed
    pause
    exit /b 1
)

REM Electron-builder
echo [STEP 4/4] Packaging with electron-builder...
call npx electron-builder --win
if %ERRORLEVEL% neq 0 (
    echo [ERROR] electron-builder failed
    pause
    exit /b 1
)

echo.
echo ===================================
echo  Build Complete!
echo  Output: release\
echo ===================================
echo.

REM Open release folder
explorer release

pause
