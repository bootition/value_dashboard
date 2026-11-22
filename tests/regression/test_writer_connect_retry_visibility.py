"""写连接退避重试必须留日志（2026-11-22 独立性能审查实测的静默卡顿）。

审查实测：另一个进程持只读句柄 45 秒 → 写连接**静默阻塞 52.65 秒**才成功，
日志里没有任何线索。于是"网络只花 0.7 秒、后面静默 11 秒"这类现象
在日志里完全无法归因。本用例钉住：每次重试都要打 WARNING
（已等时长 + 下次等待 + 原始错误关键字）。
"""

from __future__ import annotations

import logging

from app.core.storage import duckdb_store as store_module

LOG = logging.getLogger(store_module.__name__)


def test_writer_connect_retry_logs_every_attempt(
    duckdb_store, monkeypatch, caplog,
) -> None:
    calls = {"n": 0}

    class FakeConn:
        pass

    def flaky_connect(path, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("another program is using this file")
        return FakeConn()

    monkeypatch.setattr(store_module, "_store_connect", flaky_connect)
    # 退避改成极短（避免用例真睡 0.5+1.0 秒）
    monkeypatch.setattr(store_module.time, "sleep", lambda _seconds: None)

    with caplog.at_level(logging.WARNING, logger=store_module.__name__):
        conn = duckdb_store._connect_writer()

    assert isinstance(conn, FakeConn)
    assert calls["n"] == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    assert "写连接被占用" in message
    assert "已等" in message
    assert "下次再等" in message
    assert "another program is using this file" in message


def test_writer_connect_success_first_try_logs_nothing(
    duckdb_store, monkeypatch, caplog,
) -> None:
    monkeypatch.setattr(store_module, "_store_connect", lambda *a, **k: object())
    with caplog.at_level(logging.WARNING, logger=store_module.__name__):
        duckdb_store._connect_writer()
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
