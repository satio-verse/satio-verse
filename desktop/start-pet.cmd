@echo off
rem Double-click to start the desktop pet. With --services it also starts the local services in the background
rem (server on 8790, local model, TTS; ones already running are left alone; tray > local services shows them).
rem The pet stands on the taskbar at the bottom-right of the primary monitor; it has no taskbar button,
rem only a tray icon (right-click it for the menu: talk, microphone, hide, exit). Log: desktop\build\wmpet.log
rem 2026-11-17: also opens the terminal (workbench) right away (--workbench). --workbench-query gives the page a per-launch token (splash=...):
rem the terminal plays its start screen only when it sees a token it has not played yet (cold start), so closing and reopening it later does not replay it.
start "" "%~dp0build\bin\wmpet.exe" --server http://127.0.0.1:8790 --services --workbench --workbench-query "splash=%RANDOM%%RANDOM%"
