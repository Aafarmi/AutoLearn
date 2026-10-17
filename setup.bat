@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "PYTHON=%VENV%\Scripts\python.exe"
set "SCRAPLING=%VENV%\Scripts\scrapling.exe"

echo ==========================================
echo AutoLearn one-click setup
echo ==========================================
echo.

rem An existing .venv is only reused when it still runs from this directory:
rem moving the project leaves Scripts\pip.exe pointing at the old absolute path.
if exist "%PYTHON%" (
  call :venv_usable
  if errorlevel 1 (
    echo [1/4] Existing .venv is unusable here; rebuilding it...
    rmdir /s /q "%VENV%"
  ) else (
    echo [1/4] Using existing .venv...
  )
)
if exist "%PYTHON%" goto :venv_ready

echo [1/4] Creating Python 3.12 virtual environment...
call :find_python312
if not defined PYTHON312 (
  echo.
  echo ERROR: Python 3.12 x64 was not found.
  echo Install Python 3.12, or set AUTOLEARN_PYTHON312 to its python.exe,
  echo then run this script again.
  goto :fail
)
echo Using interpreter: %PYTHON312%
%PYTHON312% -m venv "%VENV%"
if errorlevel 1 goto :fail

:venv_ready
if not exist "%PYTHON%" (
  echo ERROR: Failed to create .venv.
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

:venv_usable
rem Returns 0 when .venv is Python 3.12 and its console scripts still work.
"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
"%PYTHON%" -c "import sys, pathlib; raise SystemExit(0 if pathlib.Path(sys.prefix).resolve() == pathlib.Path(r'%VENV%').resolve() else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
"%VENV%\Scripts\pip.exe" --version >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0

:find_python312
rem Sets PYTHON312 to a runnable command line: a quoted interpreter path or "py -3.12".
set "PYTHON312="

if defined AUTOLEARN_PYTHON312 (
  "%AUTOLEARN_PYTHON312%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
  if not errorlevel 1 (
    set PYTHON312="%AUTOLEARN_PYTHON312%"
    goto :eof
  )
)

py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 (
  set "PYTHON312=py -3.12"
  goto :eof
)

py -V:3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 (
  set "PYTHON312=py -V:3.12"
  goto :eof
)

rem uv-managed interpreters are not registered with the py launcher.
for /d %%D in ("%APPDATA%\uv\python\cpython-3.12*") do (
  if not defined PYTHON312 (
    "%%D\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
    if not errorlevel 1 set PYTHON312="%%D\python.exe"
  )
)
if defined PYTHON312 goto :eof

for %%D in (
  "%LOCALAPPDATA%\Programs\Python\Python312"
  "%ProgramFiles%\Python312"
  "C:\Python312"
) do (
  if not defined PYTHON312 (
    if exist "%%~D\python.exe" (
      "%%~D\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
      if not errorlevel 1 set PYTHON312="%%~D\python.exe"
    )
  )
)
if defined PYTHON312 goto :eof

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON312=python"
goto :eof

:fail
echo.
echo Setup failed. Review the error above and try again.
pause
exit /b 1
