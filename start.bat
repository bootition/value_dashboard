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

REM Port conflicts are fatal. Continuing would leave the browser connected to
REM an older process while this launcher silently fails to bind.
netstat -ano | findstr ":8765" >nul 2>&1
if not errorlevel 1 (
    echo [ERROR] Port 8765 is already in use.
    echo [ERROR] Close the existing Value Dashboard window/process, then run start.bat again.
    netstat -ano | findstr ":8765"
    pause
    exit /b 1
)

REM A repository checkout must always run current source. A stale dist build
REM must never shadow it. Packaged mode exists only when the exe is beside this
REM launcher in the release directory.
set "RELEASE_ROOT=%CD%"
set "EXE_PATH=value-dashboard.exe"

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
where npm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] npm not found in PATH; cannot build the current frontend source.
    echo Install Node.js 20.19 or newer, then run start.bat again.
    pause
    exit /b 1
)
if not exist "frontend\node_modules" (
    echo [INFO] Installing locked frontend dependencies...
    call npm --prefix frontend ci
    if errorlevel 1 (
        echo [ERROR] Frontend dependency installation failed.
        pause
        exit /b 1
    )
)
echo [INFO] Building and publishing the current frontend...
call npm --prefix frontend run build
if errorlevel 1 (
    echo [ERROR] Frontend build failed; server was not started.
    pause
    exit /b 1
)
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Release directory not found: %EXE_PATH%
    echo [ERROR] Python not found in PATH; cannot fall back to development mode.
    echo Build a release with the packaged exe, or install Python and add it to PATH.
    pause
    exit /b 1
)
echo [INFO] No packaged exe beside start.bat; starting current source...
python -m app.web.main 2>>"data\logs\start.log"

:end
pause
goto :eof
