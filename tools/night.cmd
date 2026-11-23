@echo off
rem Nightly consolidation (wm/night.py). Called by Windows Task Scheduler at 23:40.
rem Manual dry run: python -m wm.night --day 2026-11-19 --dry-run
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set WM_NIGHT_REPORT=on
"C:\Users\ASUS\AppData\Local\Programs\Python\Python310\python.exe" -m wm.night >> "logs\night.log" 2>&1
