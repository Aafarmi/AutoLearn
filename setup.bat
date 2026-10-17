@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "SCRAPLING=%~dp0.venv\Scripts\scrapling.exe"

echo ==========================================
echo AutoLearn one-click setup
echo ==========================================
echo.

if not exist "%PYTHON%" (
  echo [1/4] Creating Python 3.12 virtual environment...
  py -3.12 -m venv .venv 2>nul
  if errorlevel 1 (
    echo py launcher not available; trying python...
    python -m venv .venv
  )
  if errorlevel 1 goto :fail
) else (
  echo [1/4] Using existing .venv...
)

if not exist "%PYTHON%" (
  echo ERROR: Failed to create .venv.
  echo Install Python 3.12 x64, then run this script again.
  goto :fail
)

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
if errorlevel 1 (
  echo ERROR: .venv does not use Python 3.12.
  echo Delete .venv and run this script again.
  goto :fail
)

echo [2/4] Updating pip...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/4] Installing AutoLearn dependencies...
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

if not exist "%SCRAPLING%" (
  echo ERROR: scrapling command was not installed.
  goto :fail
)

echo [4/4] Installing Scrapling browser components...
echo This may take several minutes and requires network access.
"%SCRAPLING%" install
if errorlevel 1 goto :fail

echo.
echo Setup completed successfully.
if /i "%~1"=="--install-only" (
  echo Run run.bat to start AutoLearn.
  exit /b 0
)

echo Starting AutoLearn WebUI...
echo.
call "%~dp0run.bat"
exit /b %ERRORLEVEL%

:fail
echo.
echo Setup failed. Review the error above and try again.
pause
exit /b 1