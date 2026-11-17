@echo off
setlocal EnableDelayedExpansion
title Property Deal Analyzer
cd /d %~dp0

rem Pick a Python version that has prebuilt packages: 3.13 first, then 3.12.
set PY=
py -3.13 --version >nul 2>nul
if not errorlevel 1 set PY=py -3.13
if not defined PY py -3.12 --version >nul 2>nul
if not defined PY if not errorlevel 1 set PY=py -3.12
if not defined PY (
  echo This analyzer needs Python 3.13 or 3.12.
  echo Install Python 3.13 free from https://www.python.org/downloads/
  echo Tick "Add python.exe to PATH" during setup, then run this file again.
  pause
  exit /b 1
)

rem If the setup folder was built with a different Python, rebuild it.
if exist .venv\Scripts\python.exe (
  for /f "tokens=2" %%V in ('%PY% --version') do set WANT_VER=%%V
  for /f "tokens=2" %%V in ('".venv\Scripts\python.exe" --version 2^>nul') do set VENV_VER=%%V
  if not "!VENV_VER!"=="!WANT_VER!" (
    echo Setup was built with Python !VENV_VER! - rebuilding it with Python !WANT_VER!...
    rmdir /s /q .venv
  )
)

if not exist .venv (
  echo First run: setting up, this takes a minute...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not set up. See the error above.
    pause
    exit /b 1
  )
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
if errorlevel 1 (
  echo Install failed - see the error above. The analyzer did not start.
  pause
  exit /b 1
)
echo.
echo Property Deal Analyzer is running at http://127.0.0.1:5050
echo Keep this window open while you use it. Close it to stop the app.
echo.
start "" http://127.0.0.1:5050
python app.py
pause
