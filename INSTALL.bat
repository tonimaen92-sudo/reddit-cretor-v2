@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Reddit Creator V1 - Windows setup
chcp 65001 >nul

echo ============================================================
echo   Reddit Creator V1 - automatic Windows setup
echo ============================================================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install_windows.ps1"
set "SETUP_EXIT=%ERRORLEVEL%"

echo.
if "%SETUP_EXIT%"=="0" (
  echo Setup completed successfully.
  echo Double-click "START CREATOR PANEL.bat" to start the app.
) else (
  echo Setup failed. Read the error above, fix it, and run INSTALL.bat again.
)
echo.
pause
exit /b %SETUP_EXIT%
