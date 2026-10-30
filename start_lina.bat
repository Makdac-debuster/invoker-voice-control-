@echo off
chcp 65001 >nul
python -c "import vosk, sounddevice" >nul 2>&1
if errorlevel 1 (
    python -m pip install -q -r "%~dp0requirements.txt"
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
python "%~dp0lina_voice.py"
pause
