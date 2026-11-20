@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Install Python 3.10 or later from python.org and check "Add python.exe to PATH".
  pause
  exit /b 1
)
rem .venv only works on the PC where it was made. Rebuild it after the folder is copied to another PC.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
  if errorlevel 1 (
    echo Rebuilding .venv for this PC...
    rmdir /s /q .venv
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo First run: installing required packages...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Package installation failed. Check the internet connection and run again.
    pause
    exit /b 1
  )
)
rem Install packages added in a newer version (for existing installs)
".venv\Scripts\python.exe" -c "import flask, waitress, pypdf, olefile" >nul 2>nul
if errorlevel 1 (
  echo Installing packages added in this version...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Package installation failed. Check the internet connection and run again.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" app.py
pause
