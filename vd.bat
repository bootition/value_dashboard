@echo off
setlocal
cd /d "%~dp0"
REM Value Dashboard CLI launcher (ASCII only, CRLF).
REM Development/repository mode is always preferred: an old dist\value-dashboard
REM build must never shadow the repo's python entrypoint (P1 fix).
REM The packaged exe is used only in the release layout, where this launcher
REM sits in the same directory as value-dashboard.exe.
REM Prefer the project venv (uv.lock pinned deps, e.g. akshare 1.18.81 with
REM SECUCODE normalization + full pagination). System python must not run
REM the data path: older akshare (1.18.64) breaks stock_zh_a_gbjg_em and
REM truncates cross-check history at 20 events (2026-08-13).
set "VD_PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "VD_PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0value-dashboard.exe" (
    if not exist "data" mkdir "data"
    set "VD_ENV=formal"
    set "VD_FORMAL_ACK=confirmed"
    set "VD_DUCKDB_PATH=%CD%\data\valuedashboard.duckdb"
    set "VD_SQLITE_PATH=%CD%\data\valuedashboard.sqlite"
    "%VD_PY%" -m app.cli.main %*
    set "CLI_EXIT=%errorlevel%"
    endlocal
    exit /b %CLI_EXIT%
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
set "CLI_EXIT=%errorlevel%"
endlocal
exit /b %CLI_EXIT%
