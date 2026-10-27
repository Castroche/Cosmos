@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\cosmos-service.ps1" stop
if errorlevel 1 pause
endlocal
