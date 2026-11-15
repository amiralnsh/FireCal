@echo off
chcp 65001 >nul
title FireCal - local
cd /d "%~dp0"
echo FireCal: http://127.0.0.1:8000/   (Ctrl+C - stop)
start "" http://127.0.0.1:8000/
".venv\Scripts\python.exe" -m uvicorn backend.main:app --port 8000
pause
