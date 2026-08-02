@echo off
cd /d "%~dp0"
REM Value Dashboard CLI launcher (ASCII only, CRLF).
REM Development/repository mode is always preferred: an old dist\value-dashboard
REM build must never shadow the repo's python entrypoint (P1 fix).
REM The packaged exe is used only in the release layout, where this launcher
REM sits in the same directory as value-dashboard.exe.
if not exist "%~dp0value-dashboard.exe" (
    if not exist "data" mkdir "data"
    set "VD_ENV=formal"
    set "VD_FORMAL_ACK=confirmed"
    set "VD_DUCKDB_PATH=%CD%\data\valuedashboard.duckdb"
    set "VD_SQLITE_PATH=%CD%\data\valuedashboard.sqlite"
    python -m app.cli.main %*
    goto :eof
)
set "RELEASE_ROOT=%~dp0"
set "EXE_PATH=%~dp0value-dashboard.exe"
if not exist "%RELEASE_ROOT%\data" mkdir "%RELEASE_ROOT%\data"
set "VD_ENV=formal"
set "VD_FORMAL_ACK=confirmed"
set "VD_DUCKDB_PATH=%RELEASE_ROOT%\data\valuedashboard.duckdb"
set "VD_SQLITE_PATH=%RELEASE_ROOT%\data\valuedashboard.sqlite"
if not exist "%RELEASE_ROOT%\data\logs" mkdir "%RELEASE_ROOT%\data\logs"
"%EXE_PATH%" %*
goto :eof
