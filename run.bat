@echo off
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if not defined PORT set "PORT=8877"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo AutoLearn WebUI starting at http://127.0.0.1:%PORT%
"%PYTHON%" -m core.webui --port %PORT% --open-browser
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo AutoLearn stopped with exit code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%
