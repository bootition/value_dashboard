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


def _capture_inner_evidence(config: pytest.Config, phase: str) -> None:
    """防御纵深证据捕获：正式 data/ 具名文件的存在性/长度/哈希。

    证据根由 wrapper 以 ``VD_TEST_EVIDENCE_ROOT`` 提供；未提供时跳过
    （非 wrapper 手工 pytest 运行同样合法，门禁判断归 wrapper）。
    """
    evidence_root = os.environ.get("VD_TEST_EVIDENCE_ROOT")
    if not evidence_root:
        return
    out_dir = Path(evidence_root)
    out_dir.mkdir(parents=True, exist_ok=True)
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
    target = out_dir / f"inner-formal-fingerprint-{phase}.json"
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
