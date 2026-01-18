@echo off
:start
echo Bot ishga tushirilmoqda...
set PYTHON_EXE=python
if exist ".venv\Scripts\python.exe" set PYTHON_EXE=.venv\Scripts\python.exe
%PYTHON_EXE% telegram_bot.py
echo Bot to'xtadi. 5 soniyadan keyin qayta ishga tushadi...
timeout /t 5
goto start
