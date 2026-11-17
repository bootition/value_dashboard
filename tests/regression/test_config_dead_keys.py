"""`config/default.yaml` 不得再有"无人读取"的叶子键。

背景（reports/124 §5 第 10 项）：2026-11-16 审计遍历 `config/default.yaml`
的 136 个叶子键，发现 9 个在 `app/` + `scripts/` + `frontend/src/` 零命中 ——
用户改了也不会有任何效果，属于最恶劣的一类不一致（配置撒谎）。

处置（本文件钉死结果）：
- 接线 5 个：`screening.max_nesting_levels`、`screening.max_conditions`、
  `screening.default_exclude_st`、`screening.default_exclude_suspended`、
  `screening.default_min_listing_years`；
- 删除 4 个（代码里没有对应实现分支）：`database.sqlite_wal_mode`、
  `screening.performance_target_seconds`、`backup.keep_full_backups`、
  `backup.encrypt_personal_data`，并在 yaml 原地留"未实现，已移除"注释。

扫描口径与审计一致：全点号键名优先，退化为叶子名（与历史 `cfg.get("xxx")`
写法兼容）；`app/resources/config/` 是**打包副本**不是读取方，必须排除，
否则每个键都会"命中自己"（这正是上一轮审计脚本的坑）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 构建期同步脚本（reports/136 §6.1）：把"手工 cp"换成一条命令。
# `--check` 的退出码语义与目录布局由下面的测试钉住。
from scripts.sync_packaged_config import (  # noqa: E402
    PACKAGED_CONFIG_RELATIVE,
    REPO_CONFIG_RELATIVE,
    compare_and_sync,
)
from scripts.sync_packaged_config import main as sync_main  # noqa: E402

_DEFAULT_CONFIG = _PROJECT_ROOT / "config" / "default.yaml"

#: 已在本次处置中删除的键（不得回来）。
REMOVED_KEYS = frozenset({
    "database.sqlite_wal_mode",
    "screening.performance_target_seconds",
    "backup.keep_full_backups",
    "backup.encrypt_personal_data",
})

#: 本次接线、必须能在代码里找到读取点的键。
WIRED_KEYS = frozenset({
    "screening.max_nesting_levels",
    "screening.max_conditions",
    "screening.default_exclude_st",
    "screening.default_exclude_suspended",
    "screening.default_min_listing_years",
})

_TEXT_SUFFIXES = frozenset({".py", ".ts", ".vue", ".js"})
_SKIP_PARTS = frozenset({"__pycache__", "node_modules", ".venv"})


def _leaf_keys(node: object, prefix: str = "") -> list[str]:
    if not isinstance(node, dict):
        return []
    keys: list[str] = []
    for key, value in node.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            keys.extend(_leaf_keys(value, dotted))
        else:
            keys.append(dotted)
    return keys


def _consumer_corpus() -> str:
    """拼接所有"读取方"源码（排除打包配置副本与构建产物）。"""
    chunks: list[str] = []
    roots = [
        _PROJECT_ROOT / "app",
        _PROJECT_ROOT / "scripts",
        _PROJECT_ROOT / "frontend" / "src",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
                continue
            if _SKIP_PARTS & set(path.parts):
                continue
            # 打包用的配置副本不是消费者，扫它等于自己命中自己。
            relative = path.relative_to(_PROJECT_ROOT).as_posix()
            if relative.startswith("app/resources/config/"):
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(chunks)


def test_default_config_has_no_unread_leaf_keys() -> None:
    config = yaml.safe_load(_DEFAULT_CONFIG.read_text(encoding="utf-8"))
    leaves = _leaf_keys(config)
    assert len(leaves) >= 120, f"解析出的叶子键太少（{len(leaves)}），yaml 结构可能变了"

    corpus = _consumer_corpus()
    unread: list[str] = []
    for dotted in leaves:
        if dotted in corpus:
            continue
        leaf = dotted.rsplit(".", 1)[-1]
        if leaf in corpus:
            continue
        unread.append(dotted)

    assert unread == [], (
        "以下配置键在 app/ + scripts/ + frontend/src/ 里无人读取（用户改了无效）：\n"
        + "\n".join(f"  - {key}" for key in unread)
        + "\n请接上读取点，或从 config/default.yaml 删除并留'未实现，已移除'注释。"
    )


def test_removed_dead_keys_stay_removed() -> None:
    config = yaml.safe_load(_DEFAULT_CONFIG.read_text(encoding="utf-8"))
    leaves = set(_leaf_keys(config))

    still_present = REMOVED_KEYS & leaves
    assert still_present == set(), f"这些键已裁定删除，不应再出现：{sorted(still_present)}"

    text = _DEFAULT_CONFIG.read_text(encoding="utf-8")
    for key in REMOVED_KEYS:
        assert key.rsplit(".", 1)[-1] in text, f"{key} 需要留一行'未实现，已移除'说明"
    assert text.count("未实现，已移除") >= len(REMOVED_KEYS)


def test_wired_keys_are_present_and_reachable() -> None:
    config = yaml.safe_load(_DEFAULT_CONFIG.read_text(encoding="utf-8"))
    leaves = set(_leaf_keys(config))
    missing = WIRED_KEYS - leaves
    assert missing == set(), f"这些键已被接线，不应被删掉：{sorted(missing)}"

    corpus = _consumer_corpus()
    for key in WIRED_KEYS:
        leaf = key.rsplit(".", 1)[-1]
        assert leaf in corpus, f"{key} 没有任何读取点"


def test_packaged_config_matches_repo_config() -> None:
    """打包兜底配置必须与仓库配置**完全一致**（reports/135）。

    事故背景：`app/resources/config/default.yaml`（wheel/安装版没有仓库 `config/`
    时的兜底副本）在 2026-11 已漂移到 93 行，而仓库版是 269 行——它缺了 51 个
    `update.*` 键与整个 `asset_health:` 段，还把 `adapters.primary.dividends`
    写成了 `[..., "cninfo"]`（仓库版有硬注释明令禁止），并把 DuckDB 内存从
    8GB 砍到 2GB/4GB。

    这些差异在源码运行形态下**完全不生效**，只在 wheel/安装版生效 —— 典型的
    "测试全绿、发布即事故"。因此这里直接把两份文件钉成必须逐字节相等。
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    repo_config = repo_root / "config" / "default.yaml"
    packaged_config = repo_root / "app" / "resources" / "config" / "default.yaml"

    assert repo_config.exists(), f"仓库配置缺失: {repo_config}"
    assert packaged_config.exists(), f"打包兜底配置缺失: {packaged_config}"
    assert repo_config.read_bytes() == packaged_config.read_bytes(), (
        "app/resources/config/default.yaml 已与 config/default.yaml 漂移；"
        "请运行 `uv run --locked python scripts/sync_packaged_config.py` 同步（构建时会自动执行），"
        "否则安装版会用到过期/被禁止的配置（例如 cninfo 分红源、2GB 内存上限）"
    )


# ---------------------------------------------------------------------------
# 同步脚本本体（reports/136 §6.1）：把"手工 cp"变成"构建时同步 + 一条命令可修"。
# 上面那条断言只能"抓到漂移"，抓到之后还得人肉修；这里钉住修复工具本身的
# 行为——`--check` 一致时退出 0、漂移时退出 1，同步模式必须逐字节还原（含 CRLF）。
# ---------------------------------------------------------------------------

#: 造临时仓库用的源配置。故意用 CRLF：换行风格也是"逐字节相等"的一部分，
#: 同步若走了文本模式（换行会被平台转换），这里的断言会当场抓到。
_SYNC_SOURCE_BYTES = (
    b"server:\r\n"
    b"  host: 127.0.0.1\r\n"
    b"update:\r\n"
    b"  check_interval_minutes: 30\r\n"
    b"  sources:\r\n"
    b"    - eastmoney\r\n"
)

#: 事故形态的漂移副本：缺 `update.*` 段 + 换行风格被改成 LF（reports/135）。
_SYNC_DRIFTED_BYTES = b"server:\n  host: 127.0.0.1\n"


def _make_fake_repo(tmp_path: Path, packaged: bytes | None) -> Path:
    """在 `tmp_path` 下造一个最小"仓库"：源配置 + 可选兜底副本。"""
    root = tmp_path / "repo"
    source = root / REPO_CONFIG_RELATIVE
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_SYNC_SOURCE_BYTES)
    if packaged is not None:
        target = root / PACKAGED_CONFIG_RELATIVE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(packaged)
    return root


def test_sync_check_mode_exits_zero_when_copies_are_identical(tmp_path: Path) -> None:
    root = _make_fake_repo(tmp_path, _SYNC_SOURCE_BYTES)
    assert sync_main(["--check", "--root", str(root)]) == 0


def test_sync_check_mode_exits_one_on_drift_and_never_writes(
    tmp_path: Path, capsys
) -> None:
    root = _make_fake_repo(tmp_path, _SYNC_DRIFTED_BYTES)

    assert sync_main(["--check", "--root", str(root)]) == 1

    target = root / PACKAGED_CONFIG_RELATIVE
    assert target.read_bytes() == _SYNC_DRIFTED_BYTES, "--check 只能比较，绝不能改文件"
    captured = capsys.readouterr()
    assert "sync_packaged_config.py" in captured.err, "漂移时必须给出可执行的修复命令"
    assert "config/default.yaml" in captured.err
    assert "CRLF=" in captured.err, "换行风格差异必须在摘要里可见"


def test_sync_mode_restores_packaged_copy_byte_for_byte(tmp_path: Path) -> None:
    root = _make_fake_repo(tmp_path, _SYNC_DRIFTED_BYTES)

    assert sync_main(["--root", str(root)]) == 0

    target = root / PACKAGED_CONFIG_RELATIVE
    assert target.read_bytes() == (root / REPO_CONFIG_RELATIVE).read_bytes()
    assert b"\r\n" in target.read_bytes(), "二进制复制不得把 CRLF 转成 LF"
    assert sync_main(["--check", "--root", str(root)]) == 0


def test_sync_recreates_a_missing_packaged_copy(tmp_path: Path) -> None:
    root = _make_fake_repo(tmp_path, None)
    target = root / PACKAGED_CONFIG_RELATIVE

    assert sync_main(["--check", "--root", str(root)]) == 1, "副本缺失也算漂移"
    assert not target.exists()

    assert sync_main(["--root", str(root)]) == 0
    assert target.read_bytes() == _SYNC_SOURCE_BYTES


def test_compare_and_sync_core_reports_drift_then_repairs(tmp_path: Path) -> None:
    root = _make_fake_repo(tmp_path, _SYNC_DRIFTED_BYTES)

    assert compare_and_sync(root, check=True) == 1
    assert compare_and_sync(root, check=False) == 0
    assert compare_and_sync(root, check=True) == 0


def test_compare_and_sync_refuses_to_run_without_the_single_source(tmp_path: Path) -> None:
    """源缺失时不允许"反向同步"（否则事故值会被写回正式配置）。"""
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    assert compare_and_sync(empty_root, check=False) == 2
    assert not (empty_root / PACKAGED_CONFIG_RELATIVE).exists()


def test_sync_script_check_mode_passes_on_this_repository() -> None:
    """脚本默认路径必须真的指向这两份配置（路径改名时当场失败）。"""
    assert (_PROJECT_ROOT / REPO_CONFIG_RELATIVE).is_file()
    assert (_PROJECT_ROOT / PACKAGED_CONFIG_RELATIVE).is_file()
    assert sync_main(["--check"]) == 0
