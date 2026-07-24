# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Value Dashboard (M10)
# PRD §19 E6: 一键启动
# TECH_PLAN §1.4: --onedir 模式 (安装时解压一次, 之后秒启)

import os
import sys

block_cipher = None

# 收集所有数据文件
datas = []

# 前端静态资源
frontend_static = 'app/web/static'
if os.path.isdir(frontend_static):
    datas.append((frontend_static, 'app/web/static'))

# 配置文件
config_dir = 'config'
if os.path.isdir(config_dir):
    datas.append((config_dir, 'config'))

# DSL 语法文件
datas.append(('app/core/dsl/grammar.lark', 'app/core/dsl'))

# OpenCode skill 文件
datas.append(('app/cli/opencode_skill.md', 'app/cli'))

# 隐式导入 (PyInstaller 可能遗漏的库)
hiddenimports = [
    'duckdb',
    'akshare',
    'easy_tdx',
    'baostock',
    'pypinyin',
    'lark',
    'cryptography',
    'httpx',
    'uvicorn.logging',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
]

a = Analysis(
    ['app/web/main.py'],
    pathex=[os.getcwd()],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PIL', 'IPython', 'jupyter'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='value-dashboard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='value-dashboard',
)
