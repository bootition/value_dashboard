@echo off
REM Value Dashboard 一键启动脚本
REM 双击此文件即可启动应用
REM
REM 两种模式:
REM 1. 打包模式: 如果 value-dashboard.exe 存在则直接运行
REM 2. 开发模式: 使用 python -m app.web.main

cd /d "%~dp0"

echo Value Dashboard V1.0

REM 确保日志目录存在
if not exist "data\logs" mkdir "data\logs"

REM 端口检查: 如果 8765 已被占用，给出警告但仍尝试启动
netstat -ano | findstr ":8765" >nul 2>&1
if %errorlevel% equ 0 (
    echo [WARNING] 端口 8765 已被占用，启动可能失败或冲突。
)

set "EXE_PATH=dist\value-dashboard\value-dashboard.exe"

if exist "%EXE_PATH%" (
    echo 启动 Value Dashboard (打包模式)...
    "%EXE_PATH%" 2>>"data\logs\start.log"
) else (
    REM 打包模式不可用时回退到开发模式；若 python 也缺失则报错
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] 未找到可执行文件: %EXE_PATH%
        echo [ERROR] 也未在 PATH 中找到 python，无法回退到开发模式。
        echo 请先打包生成 exe，或安装 Python 并将其加入 PATH。
        pause
        exit /b 1
    )
    echo [INFO] 未找到 exe，回退到开发模式 (python -m app.web.main)...
    python -m app.web.main 2>>"data\logs\start.log"
)
pause
