@echo off
cd /d "%~dp0"
python measure.py
echo exit code: %errorlevel%
pause
