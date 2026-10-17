@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title iPhone Reddit poster
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>&1
  if errorlevel 1 (
    echo Python launcher py.exe is missing. Install Python 3.11 from python.org.
    pause
    exit /b 1
  )
  echo Creating .venv with Python 3.11...
  py -3.11 -m venv .venv
  if errorlevel 1 (
    echo Could not create venv. Install Python 3.11.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo pip install failed.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" "%~dp0misc\poster.py" %*
if errorlevel 1 (
  echo.
  echo Poster exited with an error.
  pause
)
