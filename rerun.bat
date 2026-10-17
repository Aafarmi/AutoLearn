@echo off
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if not defined PORT set "PORT=8877"
set "AUTOLEARN_PORT=%PORT%"

echo Stopping the existing AutoLearn WebUI on port %PORT%...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$port = [int]$env:AUTOLEARN_PORT; $connections = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue); if ($connections.Count -eq 0) { exit 0 }; $targets = @(); foreach ($processId in @($connections | Select-Object -ExpandProperty OwningProcess -Unique)) { $process = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $processId) -ErrorAction SilentlyContinue; if ($null -eq $process) { Write-Error ('Cannot inspect process ' + $processId); exit 3 }; if ($process.CommandLine -notmatch 'core\.webui') { Write-Error ('Port ' + $port + ' is used by another process: ' + $processId); exit 2 }; $targets += $processId }; foreach ($processId in $targets) { Stop-Process -Id $processId -Force -ErrorAction Stop; Wait-Process -Id $processId -Timeout 5 -ErrorAction SilentlyContinue }; exit 0"
if errorlevel 1 (
  echo.
  echo Unable to stop the existing service. It may be owned by another program.
  pause
  exit /b 1
)

timeout /t 1 /nobreak >nul
call "%~dp0run.bat" %*
