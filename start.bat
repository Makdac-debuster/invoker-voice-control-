@echo off
chcp 65001 >nul
python -m pip install -q -r "%~dp0requirements.txt"
python "%~dp0invoker_voice.py"
pause
