# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Value Dashboard (M10)
# PRD §19 E6: 一键启动
# TECH_PLAN §1.4: --onedir 模式 (安装时解压一次, 之后秒启)

import os
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

PROJECT_ROOT = os.path.abspath(SPECPATH)


def required_directory(relative_path):
    path = os.path.join(PROJECT_ROOT, relative_path)
    if not os.path.isdir(path):
        raise SystemExit(f"Required release directory is missing: {path}")
    return path


def required_file(relative_path):
    path = os.path.join(PROJECT_ROOT, relative_path)
    if not os.path.isfile(path):
        raise SystemExit(f"Required release file is missing: {path}")
    return path


# Release contract: code resources only. Mutable user data is deliberately
# excluded and must be provisioned beside the installed executable.
datas = [
    (required_directory('app/web/static'), 'app/web/static'),
    (required_file('config/default.yaml'), 'config'),
    (required_file('app/core/dsl/grammar.lark'), 'app/core/dsl'),
    (required_file('app/cli/opencode_skill.md'), 'app/cli'),
]
# AKShare loads bundled calendars and lookup files via package-relative paths.
# Hidden imports alone do not include these resources in a frozen empty-profile
# install, causing the first-run stock-universe adapter to disappear.
datas += collect_data_files('akshare')

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
    ['app/launcher.py'],
    pathex=[PROJECT_ROOT],
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
    icon=os.path.join(PROJECT_ROOT, 'resources', 'app-icon.ico'),
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
