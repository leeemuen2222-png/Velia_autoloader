@echo off
setlocal
cd /d "%~dp0"
title Velia Multiplayer Server

echo ============================================================
echo  Velia Multiplayer Server
echo ============================================================
echo Folder: %CD%
echo.

set "PYEXE="

where py >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py -3"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=python"
    )
)

if "%PYEXE%"=="" (
    echo [ERROR] Python was not found in PATH.
    echo Your music player may be using a different Python installation.
    echo Install Python or add it to PATH, then retry.
    echo.
    pause
    exit /b 1
)

echo [1/4] Python:
%PYEXE% --version
if errorlevel 1 goto :failed

echo.
echo [2/4] Checking pip...
%PYEXE% -m pip --version
if errorlevel 1 goto :failed

echo.
echo [3/4] Checking websockets...
%PYEXE% -c "import websockets; print('websockets', websockets.__version__)"
if errorlevel 1 (
    echo websockets is missing. Installing it now...
    %PYEXE% -m pip install "websockets>=14,<16"
    if errorlevel 1 goto :failed
)

echo.
echo [4/4] Starting server...
echo The window MUST stay open after SERVER READY appears.
echo.
%PYEXE% -u server.py
set "EXITCODE=%ERRORLEVEL%"

echo.
echo ============================================================
echo Server process exited with code %EXITCODE%.
echo If SERVER READY never appeared, copy everything above this line.
echo ============================================================
pause
exit /b %EXITCODE%

:failed
echo.
echo ============================================================
echo Startup failed. Copy the error text above if you need help.
echo ============================================================
pause
exit /b 1
