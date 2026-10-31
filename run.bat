@echo off
title Property Deal Analyzer
cd /d %~dp0
where py >nul 2>nul
if %errorlevel% neq 0 (
  echo Python was not found on this PC.
  echo Install it free from https://www.python.org/downloads/ - tick "Add python.exe to PATH" during setup - then run this file again.
  pause
  exit /b 1
)
if not exist .venv (
  echo First run: setting up, this takes a minute...
  py -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
start "" http://127.0.0.1:5050
echo.
echo Property Deal Analyzer is running at http://127.0.0.1:5050
echo Keep this window open while you use it. Close it to stop the app.
echo.
python app.py
pause
