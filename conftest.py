"""Root pytest configuration — shields collection from archived legacy tests.

All discovery-scoped decisions are delegated to the pure policy module
``_pytest_policy`` so that the hook remains testable without a live
pytest runtime.

2026-08-14 红队 80（合约 §8.2）：会话前/后对正式 data/ 文件做纯文件
I/O 的逐块 SHA-256 证据捕获（不导入 Config/Store，不使用 DB 引擎），
证据写入 ``VD_TEST_EVIDENCE_ROOT``（wrapper 提供的运行目录，不在
data/ 下）。本模块不做断言——wrapper 是权威门禁，此处是防御纵深。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from _pytest_policy import is_archived_legacy_test
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError, VdEnv

_REPO_ROOT = Path(__file__).resolve().parent
_FORMAL_DATA_ROOT = _REPO_ROOT / "data"
_FORMAL_FILES = (
    "valuedashboard.duckdb",
    "valuedashboard.sqlite",
    "valuedashboard.duckdb.wal",
    "valuedashboard.sqlite-wal",
    "valuedashboard.sqlite-shm",
)
_CHUNK_SIZE = 1024 * 1024
# wrapper 捕获证据的键名 → 正式文件名（2026-10-20 内层证据复用）
_WRAPPER_FILE_KEYS = {
    "formal_duckdb": "valuedashboard.duckdb",
    "formal_sqlite": "valuedashboard.sqlite",
    "duckdb_wal": "valuedashboard.duckdb.wal",
    "sqlite_wal": "valuedashboard.sqlite-wal",
    "sqlite_shm": "valuedashboard.sqlite-shm",
}


def pytest_ignore_collect(collection_path: Path, /) -> bool:
    """Ignore any path that lives under ``_legacy/legacy_tests/``.

    This hook is called by pytest for every discovered file and directory
    *before* import.  Returning ``True`` prevents both collection and
    execution of the item.
    """
    return is_archived_legacy_test(collection_path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _load_wrapper_fingerprint(path: str, phase: str) -> dict | None:
    """把 wrapper 的 before/after 捕获转换为内层证据载荷。

    2026-10-20 提速：正式库已增至 19GB，wrapper 会话前后各做一次完整内容
    哈希就足够作为权威证据；内层证据（防御纵深）不再重复读取同一份字节。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    wrapper_files = raw.get("files")
    if not isinstance(wrapper_files, dict):
        return None
    # 只在"同一个正式 data/ 根 + 同一组具名文件"时复用；合成哨兵目录
    # （test_hash_preservation）必须继续走真实哈希路径。
    if str(raw.get("formal_data_root") or "") != str(_FORMAL_DATA_ROOT):
        return None
    if set(wrapper_files) != set(_WRAPPER_FILE_KEYS):
        return None
    files: dict[str, dict] = {}
    for name in _FORMAL_FILES:
        if name not in _WRAPPER_FILE_KEYS.values():
            return None
    for wrapper_key, name in _WRAPPER_FILE_KEYS.items():
        entry = wrapper_files.get(wrapper_key) or {}
        files[name] = {
            "exists": bool(entry.get("exists")),
            "length": entry.get("length"),
            "sha256": entry.get("sha256"),
        }
    return {
        "schema_version": 1,
        "phase": phase,
        "timestamp_epoch": time.time(),
        "formal_data_root": str(_FORMAL_DATA_ROOT),
        "files": files,
        "source": "wrapper-before-evidence",
    }


def _capture_inner_evidence(config: pytest.Config, phase: str) -> None:
    """防御纵深证据捕获：正式 data/ 具名文件的存在性/长度/哈希。

    证据根由 wrapper 以 ``VD_TEST_EVIDENCE_ROOT`` 提供；未提供时跳过
    （非 wrapper 手工 pytest 运行同样合法，门禁判断归 wrapper）。

    2026-10-20 提速：wrapper 已提供基线（``VD_S1_BASELINE_FINGERPRINT``）
    时，pre 直接复用该结果；``VD_S1_WRAPPER_OWNS_POST=1`` 时 post 由 wrapper
    在 after 捕获后写出，避免内层重复哈希 19GB 正式库。
    """
    evidence_root = os.environ.get("VD_TEST_EVIDENCE_ROOT")
    if not evidence_root:
        return
    out_dir = Path(evidence_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"inner-formal-fingerprint-{phase}.json"

    baseline_path = os.environ.get("VD_S1_BASELINE_FINGERPRINT")
    if phase == "pre" and baseline_path:
        reused = _load_wrapper_fingerprint(baseline_path, phase)
        if reused is not None:
            target.write_text(
                json.dumps(reused, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            return
    owns_post_root = os.environ.get("VD_S1_WRAPPER_OWNS_POST")
    if phase == "post" and owns_post_root and owns_post_root == str(_FORMAL_DATA_ROOT):
        return

    files: dict[str, dict] = {}
    for name in _FORMAL_FILES:
        path = _FORMAL_DATA_ROOT / name
        if path.is_file():
            files[name] = {
                "exists": True,
                "length": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        else:
            files[name] = {"exists": False, "length": None, "sha256": None}
    payload = {
        "schema_version": 1,
        "phase": phase,
        "timestamp_epoch": time.time(),
        "formal_data_root": str(_FORMAL_DATA_ROOT),
        "files": files,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fingerprints_equal(left: dict, right: dict) -> bool:
    """比较两份内层证据载荷（纯函数，供 §8.5 哈希完整性测试使用）。

    wrapper 的 PowerShell 对比是权威门禁；此处为 Python 侧防御纵深的
    可测试对比语义。
    """
    left_files = left.get("files")
    right_files = right.get("files")
    if left_files is None or right_files is None:
        return False
    if set(left_files) != set(right_files):
        return False
    for key, left_entry in left_files.items():
        right_entry = right_files[key]
        for field in ("exists", "length", "sha256"):
            if left_entry.get(field) != right_entry.get(field):
                return False
    return True


def pytest_configure(config: pytest.Config) -> None:
    """Fail before collection unless pytest is explicitly isolated from formal data."""
    if os.environ.get("VD_FORMAL_ACK"):
        raise pytest.UsageError("VD_FORMAL_ACK is forbidden while running pytest")
    try:
        paths = DatabasePathSet.from_env()
    except PathIsolationError as error:
        raise pytest.UsageError(
            "pytest requires the isolated VD_ENV=test profile; use scripts/s1-pytest.ps1"
        ) from error
    if paths.env is not VdEnv.TEST:
        raise pytest.UsageError("pytest requires VD_ENV=test; formal and staging profiles are forbidden")
    _capture_inner_evidence(config, phase="pre")


def pytest_unconfigure(config: pytest.Config) -> None:
    """防御纵深：全部 teardown 后再次捕获正式文件指纹，供事后比对。

    不在此断言（wrapper 是权威门禁）。
    """
    _capture_inner_evidence(config, phase="post")
