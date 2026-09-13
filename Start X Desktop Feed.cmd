@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo X Desktop Feed could not start because .venv\Scripts\pythonw.exe is missing.
  echo Create the project virtual environment and install the project dependencies first.
  pause
  exit /b 1
)
start "X Desktop Feed" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" -m xfeed
exit /b 0
