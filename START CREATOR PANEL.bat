@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0run_creator.bat"
exit /b %ERRORLEVEL%
