@echo off
chcp 65001 >nul
title FireCal - public link
cd /d "%~dp0"
".venv\Scripts\python.exe" deploy\run_public.py %*
pause
