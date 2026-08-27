"""哈希完整性逻辑测试（合约 §8.5 / §10.1）。

全部使用合成哨兵文件（tmp_path），绝不读取或写入正式 data/ 目录。
覆盖：内层证据捕获（conftest._capture_inner_evidence）的 pre/post 载荷、
逐块哈希与整体哈希一致性、比较器对每个字段的敏感性。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

# 仓库根 conftest.py 与 tests/conftest.py 同名，`import conftest` 在
# prepend 导入模式下解析到 tests/ 版本；按文件路径加载根 conftest 的
# 独立副本（纯函数 + 哨兵目录 monkeypatch，不与 pytest 已加载实例共享状态）。
_ROOT_CONFTEST_PATH = Path(__file__).resolve().parents[2] / "conftest.py"
_spec = importlib.util.spec_from_file_location("root_conftest_under_test", _ROOT_CONFTEST_PATH)
root_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(root_conftest)
_capture_inner_evidence = root_conftest._capture_inner_evidence
_sha256_file = root_conftest._sha256_file
fingerprints_equal = root_conftest.fingerprints_equal

_SENTINEL_NAME = "valuedashboard.sqlite"


@pytest.fixture()
def sentinel_env(tmp_path, monkeypatch):
    formal_root = tmp_path / "sentinel-formal-data"
    formal_root.mkdir()
    evidence_root = tmp_path / "sentinel-evidence"
    monkeypatch.setattr(root_conftest, "_FORMAL_DATA_ROOT", formal_root)
    monkeypatch.setattr(root_conftest, "_FORMAL_FILES", (_SENTINEL_NAME,))
    monkeypatch.setenv("VD_TEST_EVIDENCE_ROOT", str(evidence_root))
    return formal_root, evidence_root


def _load(evidence_root, phase: str) -> dict:
    path = evidence_root / f"inner-formal-fingerprint-{phase}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_sha256_file_matches_stdlib_streaming_hash(tmp_path):
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"hello world")
    assert _sha256_file(sentinel) == hashlib.sha256(b"hello world").hexdigest()


def test_sha256_file_chunked_read_equals_one_shot(tmp_path):
    sentinel = tmp_path / "big.bin"
    sentinel.write_bytes(bytes(range(256)) * (root_conftest._CHUNK_SIZE // 256 + 7))
    assert _sha256_file(sentinel) == hashlib.sha256(sentinel.read_bytes()).hexdigest()


def test_capture_pre_post_detects_mutation(sentinel_env):
    formal_root, evidence_root = sentinel_env
    sentinel = formal_root / _SENTINEL_NAME
    sentinel.write_bytes(b"state-a")

    _capture_inner_evidence(None, phase="pre")
    pre = _load(evidence_root, "pre")

    sentinel.write_bytes(b"state-b-changed")
    _capture_inner_evidence(None, phase="post")
    post = _load(evidence_root, "post")

    assert fingerprints_equal(pre, pre)
    assert not fingerprints_equal(pre, post)
    assert post["files"][_SENTINEL_NAME]["sha256"] == hashlib.sha256(b"state-b-changed").hexdigest()


def test_capture_records_missing_file_as_absent(sentinel_env):
    _, evidence_root = sentinel_env
    _capture_inner_evidence(None, phase="pre")
    pre = _load(evidence_root, "pre")
    entry = pre["files"][_SENTINEL_NAME]
    assert entry == {"exists": False, "length": None, "sha256": None}


def test_capture_skips_when_evidence_root_unset(sentinel_env, monkeypatch):
    formal_root, _ = sentinel_env
    (formal_root / _SENTINEL_NAME).write_bytes(b"x")
    monkeypatch.delenv("VD_TEST_EVIDENCE_ROOT", raising=False)
    # 不应抛错：非 wrapper 手工 pytest 运行同样合法，门禁判断归 wrapper
    _capture_inner_evidence(None, phase="pre")


def test_fingerprints_equal_field_sensitivity():
    base = {"files": {"f": {"exists": True, "length": 3, "sha256": "a" * 64}}}
    assert fingerprints_equal(base, base)

    changed_sha = {"files": {"f": {"exists": True, "length": 3, "sha256": "b" * 64}}}
    assert not fingerprints_equal(base, changed_sha)

    changed_length = {"files": {"f": {"exists": True, "length": 4, "sha256": "a" * 64}}}
    assert not fingerprints_equal(base, changed_length)

    changed_exists = {"files": {"f": {"exists": False, "length": 3, "sha256": "a" * 64}}}
    assert not fingerprints_equal(base, changed_exists)

    missing_key = {"files": {"g": {"exists": True, "length": 3, "sha256": "a" * 64}}}
    assert not fingerprints_equal(base, missing_key)

    no_files = {"schema_version": 1}
    assert not fingerprints_equal(base, no_files)


def test_capture_writes_only_under_evidence_root(sentinel_env):
    formal_root, evidence_root = sentinel_env
    (formal_root / _SENTINEL_NAME).write_bytes(b"x")
    _capture_inner_evidence(None, phase="pre")
    # 哨兵 formal 目录内不得新增任何文件（证据只能进 evidence root）
    assert sorted(p.name for p in formal_root.iterdir()) == [_SENTINEL_NAME]
    assert {p.name for p in evidence_root.iterdir()} == {"inner-formal-fingerprint-pre.json"}
