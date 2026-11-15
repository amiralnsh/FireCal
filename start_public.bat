@echo off
rem AI-assisted: developed with help from the Claude AI assistant (Anthropic); see README, 'Use of AI tools'.
chcp 65001 >nul
title FireCal - public link
cd /d "%~dp0"
".venv\Scripts\python.exe" deploy\run_public.py %*
pause
