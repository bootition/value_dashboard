"""Release entrypoint contracts that do not require a frozen executable."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]


def test_packaged_entrypoint_routes_arguments_to_cli() -> None:
    source = (Path(__file__).parents[2] / "app" / "launcher.py").read_text(encoding="utf-8")
    assert "from app.cli.main import app" in source
    assert "from app.web.main import run_server" in source


def test_release_scripts_keep_formal_data_beside_the_executable() -> None:
    for relative_path in ("start.bat", "vd.bat"):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert '%RELEASE_ROOT%\\data\\valuedashboard.duckdb' in source
        assert '%RELEASE_ROOT%\\data\\valuedashboard.sqlite' in source


def test_start_bat_uses_current_directory_as_release_root() -> None:
    """start.bat 在打包分支把 RELEASE_ROOT 设为当前目录。"""
    source = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")
    assert 'set "RELEASE_ROOT=%CD%"' in source


def test_start_bat_does_not_allow_stale_dist_to_shadow_source() -> None:
    source = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")

    assert "dist\\value-dashboard\\value-dashboard.exe" not in source
    assert 'set "EXE_PATH=value-dashboard.exe"' in source


def test_development_start_builds_current_frontend_before_server() -> None:
    source = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")

    build = source.index("call npm --prefix frontend run build")
    server = source.index('"%VD_PY%" -m app.web.main 2>>"data\\logs\\start.log"')
    assert build < server
    assert "if errorlevel 1 (" in source[build:server]


def test_development_start_builds_only_when_bundle_is_stale() -> None:
    """启动不再每次无条件执行构建；只在前端指纹或入口缺失时构建。"""
    source = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")

    check = source.index('fe-fingerprint.cjs" --check')
    stamp = source.index('fe-fingerprint.cjs" --stamp')
    server = source.index('"%VD_PY%" -m app.web.main 2>>"data\\logs\\start.log"')

    assert 'set "NEED_BUILD="' in source
    assert 'if defined NEED_BUILD (' in source
    assert check < server
    assert stamp < server


def test_fe_fingerprint_script_is_node_based_and_dep_free() -> None:
    script = (PROJECT_ROOT / "frontend" / "scripts" / "fe-fingerprint.cjs").read_text(encoding="utf-8")

    assert "'use strict'" in script
    assert '--check' in script
    assert '--stamp' in script
    assert "node:fs" in script
    assert "node:crypto" in script


def test_start_bat_validates_the_existing_instance_health_response() -> None:
    """端口被其他服务占用时必须继续启动，而非误打开未知服务。"""
    source = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")

    assert "Invoke-WebRequest" in source
    assert "http://127.0.0.1:8765/api/health" in source
    assert "status\\\"\\s*:\\s*\\\"ok" in source
    assert 'start "" "http://127.0.0.1:8765/"' in source
    block_start = source.index("Invoke-WebRequest")
    block_end = source.index('set "RELEASE_ROOT=%CD%"')
    block = source[block_start:block_end]
    assert 'goto :end' in block
    assert 'exit /b 1' not in block


def test_start_bat_fails_cleanly_on_build_error_without_bare_parens_in_block_echo() -> None:
    """块内 echo 不得含括号，否则 CMD 块解析报「此时不应有 (」导致无法启动。
    这是用户反馈 start.bat 无法正常启动的根因回归保护。"""
    source = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")

    build_block_start = source.index('if defined NEED_BUILD (')
    next_label = source.index("\n:end")
    block = source[build_block_start:next_label]
    assert 'echo ' in block
    for line in block.splitlines():
        if line.strip().startswith("echo "):
            assert "(" not in line
            assert ")" not in line


def test_vd_bat_prefers_development_entrypoint_over_stale_dist_builds() -> None:
    """P1: vd.bat 只在使用发行布局（与 exe 同目录）时走打包模式；
    仓库根目录始终走 python 入口，避免 dist 旧发行遮蔽开发 CLI。
    2026-08-13：数据路径优先项目 venv（uv.lock 锁定依赖），系统 Python
    不得运行数据路径（旧 akshare 破坏东财交叉源并截断事件）。"""
    source = (PROJECT_ROOT / "vd.bat").read_text(encoding="utf-8")
    assert 'if not exist "%~dp0value-dashboard.exe" (' in source
    assert '"%VD_PY%" -m app.cli.main %*' in source
    assert 'set "VD_PY=python"' in source
    assert '%~dp0.venv\\Scripts\\python.exe' in source
    # 打包分支仍把数据放在 exe 旁的 data 目录
    assert 'set "EXE_PATH=%~dp0value-dashboard.exe"' in source


def test_release_scripts_bootstrap_an_empty_distribution_profile() -> None:
    root = Path(__file__).parents[2]
    for relative_path in ("start.bat", "vd.bat"):
        source = (root / relative_path).read_text(encoding="utf-8")
        assert 'set "VD_ENV=formal"' in source
        assert 'set "VD_DUCKDB_PATH=%RELEASE_ROOT%\\data\\valuedashboard.duckdb"' in source
        assert 'set "VD_SQLITE_PATH=%RELEASE_ROOT%\\data\\valuedashboard.sqlite"' in source
        # 2026-08-14 红队 P2：批处理加固后以 exit /b %<code>% 显式终止并
        # 传播退出码（替代 goto :eof/:end 跳转收尾）。
        assert "goto :eof" in source or "goto :end" in source or "exit /b %" in source


def test_launcher_batch_files_are_ascii_without_bom() -> None:
    """P0-6: CMD 以系统代码页解析批处理文件；非 ASCII 注释会吞掉下一个
    字符（实测将首个 REM 解析为 'EM'）。启动脚本必须纯 ASCII、无 BOM，
    且使用 CRLF 行尾（LF-only 会让 CMD 在解析多行块时出错）。"""
    for relative_path in ("start.bat", "vd.bat"):
        raw = (PROJECT_ROOT / relative_path).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{relative_path} must not carry a UTF-8 BOM"
        assert all(byte < 0x80 for byte in raw), (
            f"{relative_path} must be ASCII-only so CMD parses it in any code page"
        )
        for index, byte in enumerate(raw):
            if byte == 0x0A and (index == 0 or raw[index - 1] != 0x0D):
                raise AssertionError(f"{relative_path} must use CRLF line endings")
        if raw and raw[-1:] != b"\n":
            raise AssertionError(f"{relative_path} must end with a newline")


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="start.bat execution requires cmd.exe")
def test_start_bat_executes_under_cmd_and_reaches_packaged_branch(tmp_path: Path) -> None:
    """P0-6: 实测执行 start.bat，确认 REM 解析正常且进入打包分支。

    用无效 PE 文本文件占位 value-dashboard.exe 只会让 CMD 在 exe 行报
    "not recognized" 并继续；旧版编码 bug 会在首个 REM 处直接失败，
    二者可以从 start.log 内容区分。
    """
    release = tmp_path / "release"
    release.mkdir()
    (release / "start.bat").write_bytes((PROJECT_ROOT / "start.bat").read_bytes())
    (release / "value-dashboard.exe").write_bytes(b"not a real executable")
    command_shims = tmp_path / "command-shims"
    command_shims.mkdir()
    (command_shims / "netstat.cmd").write_bytes(b"@exit /b 1\r\n")
    # 本机 8765 常驻服务会让 start.bat 的 health-check 提前走 already-running 分支；
    # shim powershell 固定返回 1，确保测试始终进入 packaged 分支。
    (command_shims / "powershell.cmd").write_bytes(b"@exit /b 1\r\n")

    env = os.environ.copy()
    env["PATH"] = f"{command_shims}{os.pathsep}{env['PATH']}"
    env["VD_SKIP_HEALTH_CHECK"] = "1"

    subprocess.run(
        ["cmd", "/d", "/c", "start.bat < nul"],
        cwd=str(release),
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        check=False,
    )

    log = release / "data" / "logs" / "start.log"
    assert log.exists(), "launcher must create the log path before invoking the exe"
    log_text = log.read_text(encoding="utf-8", errors="replace")
    assert "'EM'" not in log_text, "CMD misparsed a REM comment (encoding regression)"
    assert "value-dashboard.exe" in log_text, "launcher did not reach the packaged branch"


def test_spec_uses_the_unified_packaged_entrypoint() -> None:
    source = (Path(__file__).parents[2] / "value-dashboard.spec").read_text(encoding="utf-8")
    assert "['app/launcher.py']" in source


def test_release_contract_excludes_mutable_formal_data() -> None:
    source = (Path(__file__).parents[2] / "value-dashboard.spec").read_text(encoding="utf-8")
    assert "Mutable user data is deliberately" in source
    assert "required_directory('data')" not in source
    assert "required_file('data/valuedashboard.duckdb')" not in source


def test_release_contract_includes_akshare_runtime_resources() -> None:
    source = (Path(__file__).parents[2] / "value-dashboard.spec").read_text(encoding="utf-8")

    assert "collect_data_files" in source
    assert "collect_data_files('akshare')" in source


def test_clean_release_builder_uses_locked_dependencies() -> None:
    root = Path(__file__).parents[2]
    source = (root / "scripts" / "build-release.ps1").read_text(encoding="utf-8")
    assert "npm ci" in source
    assert "s1-pytest.ps1" in source
    assert "npm run lint" in source
    assert "npm run test" in source
    assert "npm run build" in source
    assert '"uv.lock"' in source
    assert "uv run --locked --extra release python -m PyInstaller" in source
    assert 'release = [' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert (root / "uv.lock").is_file()
    assert "IsPathFullyQualified($OutputDirectory)" in source
    assert "Copy-Item -LiteralPath" in source
    assert "Release must not package formal data" in source
    package = (root / "frontend" / "package.json").read_text(encoding="utf-8")
    # 2026-08-14 红队 P2-10：单元测试 runner 防漂移——不再在 package.json
    # 硬编码测试文件名，改由 scripts/run-unit-tests.mjs 自动扫描 tests/*.test.ts。
    assert '"test": "node scripts/run-unit-tests.mjs && vitest run"' in package
    runner = (root / "frontend" / "scripts" / "run-unit-tests.mjs").read_text(encoding="utf-8")
    assert "--experimental-strip-types" in runner
    assert "--test" in runner
    assert 'readdirSync' in runner
