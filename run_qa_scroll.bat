@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mobile UI QA - 12 long swipes
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "QA_NONINTERACTIVE="
for %%A in (%*) do if /I "%%~A"=="--noninteractive" set "QA_NONINTERACTIVE=1"

if not exist ".venv\Scripts\python.exe" (
  echo The project virtual environment is missing. Run the project setup first.
  pause
  exit /b 1
)

echo The test opens the application from its proven Home-screen icon.
echo This test performs the configured deterministic long vertical feed swipes.
echo Each swipe moves from y=920 to y=620 over approximately 696 ms.
echo It does not tap content or change project state records.
echo Feed continuity is verified after every swipe, even if bottom navigation hides.
echo On success, only the exact application process is stopped and iPhone Home is verified.
if not defined QA_NONINTERACTIVE pause

".venv\Scripts\python.exe" "%~dp0misc\qa_scroll_test.py" %*
set "QA_EXIT=%ERRORLEVEL%"
if not "%QA_EXIT%"=="0" (
  echo.
  echo QA scroll test stopped with an error. The screen was preserved when cleanup was unsafe.
) else (
  echo.
  echo TEST COMPLETE -- cleanup executed
)
if not defined QA_NONINTERACTIVE pause
exit /b %QA_EXIT%
