from __future__ import annotations

import os
from pathlib import Path

from app.core.storage.duckdb_store import _is_external_file_lock
from app.core.storage.update_lock import (
    _lock_path,
    update_lock_owned_by_current_process,
)


def _write_lock(db_path: Path, pid: int) -> None:
    _lock_path(db_path).write_text(
        f"pid={pid}\ntime=1792474256.33\ntoken=test\n", encoding="ascii",
    )


def test_update_lock_owned_by_current_process(tmp_path: Path) -> None:
    db_path = tmp_path / "t.duckdb"
    db_path.write_bytes(b"")

    assert update_lock_owned_by_current_process(db_path) is False

    _write_lock(db_path, os.getpid())
    assert update_lock_owned_by_current_process(db_path) is True

    _write_lock(db_path, 2**31 - 1)
    assert update_lock_owned_by_current_process(db_path) is False

    _lock_path(db_path).write_text("garbage", encoding="ascii")
    assert update_lock_owned_by_current_process(db_path) is False


def test_external_file_lock_only_when_owner_is_foreign() -> None:
    """"Cannot open file" 只在有外部写锁时才算外部锁；本进程属主应走重试。"""
    # 明确的 Windows 占用文案：无论锁文件是否在，都属于外部占用
    specific = 'IO Error: Cannot open file "x": 另一个程序正在使用此文件，进程无法访问。'
    assert _is_external_file_lock(specific, lock_active=False) is True
    assert _is_external_file_lock(specific, lock_active=True) is True
    # 泛化 Cannot open file：只有锁文件显示外部写进程时才当作锁竞争
    generic = 'IO Error: Cannot open file "x"'
    assert _is_external_file_lock(generic, lock_active=False) is False
    assert _is_external_file_lock(generic, lock_active=True) is True
    assert _is_external_file_lock("File is already open in pid 123", lock_active=False) is True
