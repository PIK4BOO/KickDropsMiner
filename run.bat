@echo off
setlocal enableextensions
cd /d "%~dp0"

set "PYCMD="
py -3.10 -c "import sys" >NUL 2>&1 && set "PYCMD=py -3.10"
if not defined PYCMD (
  py -3 -c "import sys" >NUL 2>&1 && set "PYCMD=py -3"
)
if not defined PYCMD (
  python -c "import sys" >NUL 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
  echo Python 3.10+ not found. Please install from https://www.python.org/downloads/
  exit /b 1
)

echo Checking Python packages...
%PYCMD% -c "import customtkinter, PIL, selenium, webdriver_manager, undetected_chromedriver" >NUL 2>&1
if errorlevel 1 (
  echo Installing required packages...
  %PYCMD% -m pip install --upgrade pip
  if exist requirements.txt (
    %PYCMD% -m pip install -r requirements.txt
  ) else (
    %PYCMD% -m pip install customtkinter pillow selenium webdriver-manager undetected-chromedriver browser-cookie3
  )
  if errorlevel 1 (
    echo Failed to install dependencies. Check your internet connection and try again.
    exit /b 1
  )
)

echo Launching Kick Drop Miner...
%PYCMD% main.py

endlocal
