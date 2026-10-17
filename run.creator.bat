@echo off
setlocal EnableExtensions

if /I "%~1"=="QA_MODE" goto :qa_mode
if /I "%~1"=="--qa-mode" goto :qa_mode

call "%~dp0run_creator.bat" %*
exit /b %ERRORLEVEL%

:qa_mode
cd /d "%~dp0"
title Local QA preflight and configured long QA swipes
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set "QA_PYTHON=%~dp0.venv\Scripts\python.exe"
set "QA_PREFLIGHT=%~dp0misc\qa_scroll_regress.py"
set "QA_SWIPE_LAUNCHER=%~dp0run_qa_scroll.bat"
set "QA_SLOT="
set "QA_SWIPES="

if not "%~2"=="" (
  if /I not "%~2"=="--slot" (
    echo ERROR: Unknown QA option "%~2".
    exit /b 2
  )
  if "%~3"=="" (
    echo ERROR: --slot requires a slot id.
    exit /b 2
  )
  set "QA_SLOT=%~3"
)
if not "%~4"=="" (
  if /I not "%~4"=="--swipes" (
    echo ERROR: Unknown QA option "%~4".
    exit /b 2
  )
  if "%~5"=="" (
    echo ERROR: --swipes requires a count.
    exit /b 2
  )
  set "QA_SWIPES=%~5"
)
if not "%~6"=="" (
  echo ERROR: Unexpected extra QA arguments.
  exit /b 2
)

if not exist "%QA_PYTHON%" (
  echo ERROR: Missing QA Python: "%QA_PYTHON%"
  exit /b 2
)
if not exist "%QA_PREFLIGHT%" (
  echo ERROR: Missing QA preflight: "%QA_PREFLIGHT%"
  exit /b 2
)
if not exist "%QA_SWIPE_LAUNCHER%" (
  echo ERROR: Missing QA swipe launcher: "%QA_SWIPE_LAUNCHER%"
  exit /b 2
)

echo QA MODE: running offline preflight.
"%QA_PYTHON%" "%QA_PREFLIGHT%"
set "QA_PREFLIGHT_EXIT=%ERRORLEVEL%"
if not "%QA_PREFLIGHT_EXIT%"=="0" (
  echo ERROR: QA preflight failed with exit code %QA_PREFLIGHT_EXIT%. Swipes were skipped.
  exit /b %QA_PREFLIGHT_EXIT%
)

echo QA MODE: simulated completion preflight passed.
echo QA MODE: Starting the configured long QA swipes test.
if defined QA_SLOT (
  echo QA MODE: selected slot %QA_SLOT%.
  if defined QA_SWIPES (
    echo QA MODE: configured swipe count %QA_SWIPES%.
    call "%QA_SWIPE_LAUNCHER%" --simulated-success --cleanup-on-error --noninteractive --slot "%QA_SLOT%" --swipes "%QA_SWIPES%"
  ) else (
    call "%QA_SWIPE_LAUNCHER%" --simulated-success --cleanup-on-error --noninteractive --slot "%QA_SLOT%"
  )
) else (
  call "%QA_SWIPE_LAUNCHER%" --simulated-success --cleanup-on-error --noninteractive
)
set "QA_SWIPE_EXIT=%ERRORLEVEL%"
if not "%QA_SWIPE_EXIT%"=="0" (
  echo ERROR: QA swipe stage failed with exit code %QA_SWIPE_EXIT%. Safe cleanup was requested.
  exit /b %QA_SWIPE_EXIT%
)

echo QA MODE: swipe stage and cleanup completed successfully.
exit /b 0
