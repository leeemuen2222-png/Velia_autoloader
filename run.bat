@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
where py >nul 2>nul
if not errorlevel 1 (set "PYEXE=py -3") else (set "PYEXE=python")
%PYEXE% -c "import PySide6, PySide6.QtWebSockets" >nul 2>nul
if errorlevel 1 (
    echo Installing required Qt framework...
    %PYEXE% -m pip install PySide6
    if errorlevel 1 goto :failed
)
if not exist "velia_playground.py" goto :missing
if not exist "multiplayer_client.py" goto :missing
%PYEXE% "velia_playground.py"
if errorlevel 1 goto :failed
exit /b 0
:missing
echo Required Playground source file is missing.
:failed
echo Playground exited or could not start.
pause
exit /b 1
