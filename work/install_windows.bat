@echo off
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  py -3.12 -m venv venv
  if errorlevel 1 goto failed
)
venv\Scripts\python.exe -m pip install -r requirements.lock.txt
if errorlevel 1 goto failed
echo Installed. Double-click start.bat to open Zhishi.
pause
exit /b 0
:failed
echo Installation failed. Install Python 3.12 (64-bit) and retry.
pause
exit /b 1
