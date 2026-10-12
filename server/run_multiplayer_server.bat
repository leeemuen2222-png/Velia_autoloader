@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (set "PYEXE=py -3") else (set "PYEXE=python")
%PYEXE% -c "import websockets" >nul 2>nul
if errorlevel 1 (
  %PYEXE% -m pip install "websockets>=14,<17"
  if errorlevel 1 goto :failed
)
%PYEXE% -u "server.py"
:failed
echo Server has stopped.
pause
