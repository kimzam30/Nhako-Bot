@echo off
title NhakoBot Server
cd /d D:\Projects\KimProjects\Nhako-Bot
call .\.venv\Scripts\activate
start pythonw src/main.py
echo 🤖 NhakoBot is starting... (Press Ctrl+C to stop)

:loop
".venv\Scripts\python.exe" src/main.py

//autrestart logic
echo ⚠️ CRASH DETECTED! Reviving in 5 seconds...
timeout /t 5 >nul
goto loop

