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

from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
