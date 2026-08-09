@echo off
REM Value Dashboard launcher (ASCII only so CMD parses it in any code page).
REM Double-click this file to start the application.
REM
REM   Packaged mode : run value-dashboard.exe when it sits beside this file.
REM   Source mode   : optionally rebuild the published frontend, then run
REM                   python -m app.web.main.
REM

cd /d "%~dp0"
echo Value Dashboard V1.0

REM Only this application's healthy endpoint counts as an existing instance.
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8765/api/health'; if ($r.StatusCode -eq 200 -and $r.Content -match '\"status\"\s*:\s*\"ok\"') { exit 0 } } catch {}; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Value Dashboard is already running; opening the browser.
    start "" "http://127.0.0.1:8765/"
    goto :end
)

set "RELEASE_ROOT=%CD%"
set "EXE_PATH=value-dashboard.exe"

if exist "%EXE_PATH%" goto :packaged

if not exist "data" mkdir "data"
set "VD_ENV=formal"
set "VD_FORMAL_ACK=confirmed"
set "VD_DUCKDB_PATH=%CD%\data\valuedashboard.duckdb"
set "VD_SQLITE_PATH=%CD%\data\valuedashboard.sqlite"
if not exist "data\logs" mkdir "data\logs"

set "FRONTEND_ROOT=%CD%\frontend"
if not exist "%CD%\.planning" mkdir "%CD%\.planning"
set "FE_STAMP=%CD%\.planning\.vd-fe-stamp.txt"

set "NEED_BUILD="
if not exist "%CD%\app\web\static\index.html" set "NEED_BUILD=1"
if not exist "%FE_STAMP%" set "NEED_BUILD=1"
if not defined NEED_BUILD (
    node "%FRONTEND_ROOT%\scripts\fe-fingerprint.cjs" --check "%FE_STAMP%" >nul 2>&1
    if errorlevel 1 set "NEED_BUILD=1"
)

if defined NEED_BUILD (
    if not exist "frontend\node_modules" (
        echo [INFO] Installing locked frontend dependencies, first run...
        call npm --prefix frontend ci
        if errorlevel 1 (
            echo [ERROR] Frontend dependency installation failed.
            pause
            exit /b 1
        )
    )
    echo [INFO] Building the current frontend, one-time, may take 10-20s...
    call npm --prefix frontend run build
    if errorlevel 1 (
        echo [ERROR] Frontend build failed.
        pause
        exit /b 1
    )
    node "%FRONTEND_ROOT%\scripts\fe-fingerprint.cjs" --stamp "%FE_STAMP%"
)

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH; cannot start the source service.
    echo Install Python 3.12+ and add it to PATH, then run start.bat again.
    pause
    exit /b 1
)
echo [INFO] No packaged exe beside start.bat; starting current source...
python -m app.web.main 2>>"data\logs\start.log"
goto :eof

:packaged
if not exist "%RELEASE_ROOT%\data" mkdir "%RELEASE_ROOT%\data"
set "VD_ENV=formal"
set "VD_FORMAL_ACK=confirmed"
set "VD_DUCKDB_PATH=%RELEASE_ROOT%\data\valuedashboard.duckdb"
set "VD_SQLITE_PATH=%RELEASE_ROOT%\data\valuedashboard.sqlite"
if not exist "%RELEASE_ROOT%\data\logs" mkdir "%RELEASE_ROOT%\data\logs"
echo Starting Value Dashboard - packaged mode
"%EXE_PATH%" 2>>"%RELEASE_ROOT%\data\logs\start.log"

:end
pause
goto :eof
