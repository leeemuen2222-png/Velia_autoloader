@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
cd /d "%~dp0"

if not exist "music_module.py" (
    echo music_module.py was not found next to this launcher.
    pause
    exit /b 1
)

py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "VELIA_PYTHON=py -3"
) else (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo Python 3 is required. Install Python and try again.
        pause
        exit /b 1
    )
    set "VELIA_PYTHON=python"
)

%VELIA_PYTHON% -c "import PySide6, mutagen" >nul 2>&1
if errorlevel 1 (
    echo Installing player dependencies...
    %VELIA_PYTHON% -m pip install PySide6 mutagen
    if errorlevel 1 (
        echo Could not install player dependencies.
        pause
        exit /b 1
    )
)

if not exist "monster_siren_client.py" (
    echo monster_siren_client.py was not found next to the player.
    pause
    exit /b 1
)

%VELIA_PYTHON% "%~dp0music_module.py"
if errorlevel 1 (
    echo The music player exited with an error.
    pause
    exit /b 1
)

endlocal
