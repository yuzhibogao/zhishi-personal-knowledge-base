@echo off
cd /d "%~dp0"
if not exist "venv\Scripts\pythonw.exe" (
  echo Run install_windows.bat first, or download the Windows portable release.
  pause
  exit /b 1
)
start "" "venv\Scripts\pythonw.exe" "launch.py"
