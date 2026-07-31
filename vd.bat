@echo off
cd /d "%~dp0"
if exist "value-dashboard.exe" (
  set "RELEASE_ROOT=%CD%"
  set "EXE_PATH=value-dashboard.exe"
) else (
  set "RELEASE_ROOT=%CD%\dist\value-dashboard"
  set "EXE_PATH=dist\value-dashboard\value-dashboard.exe"
)
if exist "%EXE_PATH%" (
  if not exist "%RELEASE_ROOT%\data" mkdir "%RELEASE_ROOT%\data"
  set "VD_ENV=formal"
  set "VD_FORMAL_ACK=confirmed"
  set "VD_DUCKDB_PATH=%RELEASE_ROOT%\data\valuedashboard.duckdb"
  set "VD_SQLITE_PATH=%RELEASE_ROOT%\data\valuedashboard.sqlite"
  "%EXE_PATH%" %*
  goto :eof
)
if not exist "data" mkdir "data"
set "VD_ENV=formal"
set "VD_FORMAL_ACK=confirmed"
set "VD_DUCKDB_PATH=%CD%\data\valuedashboard.duckdb"
set "VD_SQLITE_PATH=%CD%\data\valuedashboard.sqlite"
python -m app.cli.main %*
goto :eof
