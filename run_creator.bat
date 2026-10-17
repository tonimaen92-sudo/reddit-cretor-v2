@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title DataLost Creator
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
  if not errorlevel 1 goto :check_dependencies
  echo Existing .venv is unusable or is not Python 3.11. Rebuilding it...
)

where py >nul 2>&1
if errorlevel 1 (
  echo Python launcher py.exe is missing. Install Python 3.11 from python.org.
  pause
  exit /b 1
)

py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 is missing. Install Python 3.11 from python.org.
  pause
  exit /b 1
)

if exist ".venv" (
  rmdir /s /q ".venv"
  if exist ".venv" (
    echo Could not remove the broken .venv folder.
    pause
    exit /b 1
  )
)

echo Creating .venv with Python 3.11...
py -3.11 -m venv .venv
if errorlevel 1 (
  echo Could not create venv with Python 3.11.
  pause
  exit /b 1
)

"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo pip install failed.
  pause
  exit /b 1
)

:check_dependencies
"%VENV_PY%" -c "import requests, socks" >nul 2>&1
if not errorlevel 1 goto :run_creator
echo Installing missing Creator dependencies...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo pip install failed.
  pause
  exit /b 1
)

:run_creator
"%VENV_PY%" "%~dp0misc\creator.py" %*
if errorlevel 1 (
  echo.
  echo Creator exited with an error.
  pause
)
