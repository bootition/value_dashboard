@echo off
REM Value Dashboard launcher (ASCII only so CMD parses it in any code page).
REM Double-click this file to start the application.
REM
REM Two modes:
REM 1. Packaged mode: run value-dashboard.exe when present.
REM 2. Development mode: fall back to python -m app.web.main.
REM
REM CMD expands %VAR% when a statement is parsed, so variables set inside a
REM parenthesized block must not be read inside the same block. RELEASE_ROOT
REM and EXE_PATH are therefore assigned in the first if/else statement only,
REM and read (never re-assigned) in the statements that follow.

cd /d "%~dp0"

echo Value Dashboard V1.0

REM Port check: warn if 8765 is already in use, but still try to start.
netstat -ano | findstr ":8765" >nul 2>&1
if not errorlevel 1 (
    echo [WARNING] Port 8765 is already in use; startup may fail or conflict.
)

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
    if not exist "%RELEASE_ROOT%\data\logs" mkdir "%RELEASE_ROOT%\data\logs"
    echo Starting Value Dashboard - packaged mode
    "%EXE_PATH%" 2>>"%RELEASE_ROOT%\data\logs\start.log"
    goto :end
)

if not exist "data" mkdir "data"
set "VD_ENV=formal"
set "VD_FORMAL_ACK=confirmed"
set "VD_DUCKDB_PATH=%CD%\data\valuedashboard.duckdb"
set "VD_SQLITE_PATH=%CD%\data\valuedashboard.sqlite"
if not exist "data\logs" mkdir "data\logs"
REM Fall back to development mode when the packaged directory is absent.
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Release directory not found: %EXE_PATH%
    echo [ERROR] Python not found in PATH; cannot fall back to development mode.
    echo Build a release with the packaged exe, or install Python and add it to PATH.
    pause
    exit /b 1
)
echo [INFO] Release data not found; falling back to development mode (python -m app.web.main)...
python -m app.web.main 2>>"data\logs\start.log"

:end
pause
goto :eof
