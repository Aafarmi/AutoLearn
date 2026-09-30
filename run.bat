@echo off
setlocal

rem ============================================================
rem  AutoLearn v0.3.0  one-click launcher (source mode, P9)
rem  Reuses the system browser; no bundled Chromium; no PyInstaller.
rem  Requires Python 3.13+ (PATH or Windows py launcher).
rem
rem  NOTE: this file MUST stay ASCII-only. Windows cmd reads .bat
rem  files with the system code page (GBK), so any non-ASCII text
rem  (Chinese comments) corrupts the script.
rem ============================================================

set "APP_NAME=AutoLearn"
set "APP_VERSION=v0.3.0"

echo.
echo   ============================================
echo     %APP_NAME%  %APP_VERSION%
echo     Vision-only page automation (model reads the screen)
echo   ============================================
echo.

cd /d "%~dp0"

rem --- 0. locate a Python 3.13+ interpreter -------------------
rem   priority: AUTOLEARN_PYTHON ^> python(PATH) ^> py -3.13 ^> py -3.14 ^> py(default)
set "PY_CMD="

if defined AUTOLEARN_PYTHON set "PY_CMD=%AUTOLEARN_PYTHON%"

if not defined PY_CMD (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,13) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
    py -3.13 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3.13"
)

if not defined PY_CMD (
    py -3.14 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3.14"
)

if not defined PY_CMD (
    py -c "import sys; raise SystemExit(0 if sys.version_info >= (3,13) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py"
)

if not defined PY_CMD (
    echo [AutoLearn] ERROR: Python 3.13+ not found.
    echo [AutoLearn]        Detected interpreters:
    py -0p 2>nul
    echo.
    echo [AutoLearn] Fix: install Python 3.13+ from https://www.python.org/,
    echo [AutoLearn]       or set AUTOLEARN_PYTHON to a 3.13+ interpreter path.
    pause
    exit /b 1
)

echo [AutoLearn] Using Python: %PY_CMD%
echo.

rem --- 1. virtual environment ---------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [AutoLearn] Creating virtual environment .venv ...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [AutoLearn] ERROR: failed to create venv. Is Python 3.13+ available?
        pause
        exit /b 1
    )
)

set "PY=.venv\Scripts\python.exe"
set "PIP=.venv\Scripts\pip.exe"

rem --- 2. dependencies (fast check) ---------------------------
"%PY%" -c "import fastapi, uvicorn, playwright, keyring" >nul 2>nul
if errorlevel 1 (
    echo [AutoLearn] Installing dependencies from requirements.txt ...
    "%PIP%" install -r requirements.txt
    if errorlevel 1 (
        echo [AutoLearn] ERROR: dependency install failed.
        pause
        exit /b 1
    )
)

rem --- 2b. preflight: the UI port must be free ---------------
rem   A stale UI process is the number one cause of "I edited the
rem   code but nothing changed": the console keeps talking to the OLD
rem   server, whose API no longer matches the files on disk. Refuse
rem   to start silently in that case.
"%PY%" -c "import socket,sys; s=socket.socket(); busy=s.connect_ex(('127.0.0.1',8800))==0; s.close(); sys.exit(1 if busy else 0)"
if errorlevel 1 (
    echo [AutoLearn] ERROR: port 8800 is already in use.
    echo [AutoLearn]        Most likely a PREVIOUS AutoLearn console is still
    echo [AutoLearn]        running and serving STALE code. Close that window
    echo [AutoLearn]        first, then run this script again.
    echo [AutoLearn]        To check what is running: scripts\check_server.py
    pause
    exit /b 1
)

rem --- 3. launch UI (FastAPI + SSE) ---------------------------
echo [AutoLearn] Starting UI: http://127.0.0.1:8800
start "AutoLearn UI" "%PY%" -m uvicorn ui.server:create_app --factory --port 8800

rem --- 5. wait for UI and report ------------------------------
echo [AutoLearn] Waiting for UI to come up ...
"%PY%" -c "import socket,time; time.sleep(4); ok = socket.socket().connect_ex(('127.0.0.1',8800)) == 0; print('[AutoLearn] UI ' + ('ready' if ok else 'NOT ready')); raise SystemExit(0 if ok else 1)"
if errorlevel 1 (
    echo.
    echo [AutoLearn] ERROR: UI did not come up within a few seconds.
    echo [AutoLearn] Run this manually to see the real error:
    echo     %PY% -m uvicorn ui.server:create_app --factory --port 8800
    pause
)

echo.
echo [AutoLearn] All set.
echo     UI console : http://127.0.0.1:8800
echo     API docs   : http://127.0.0.1:8800/api/docs
echo.
echo Stopping: click "Shutdown" in the console (top-right). It releases port
echo            8800 (and any mock-site / managed browser it started),
echo            so you do NOT need to close spawned windows by hand.
echo.
echo (The test mock site is no longer auto-started. Run it manually when
echo  needed: scripts\serve_mock.py  ->  http://127.0.0.1:8899/quiz.html)

timeout /t 3 >nul
endlocal
