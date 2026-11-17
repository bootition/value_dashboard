"""事故出处：reports/118 / 2026-11-13。

2026-11-13 P1（A）：全历史价格响应的攒批必须**真的**合并成多只一个事务。

生产事实（2026-11-13，第 7 轮更新）：13 次落盘里 12 次 N=1、1 次 N=2。
根因是 `_PriceReplaceBuffer.should_flush()` 的时间判据是"从缓冲开张算起 5 秒"，
而全历史抓取单只 28~635 秒 —— 每次 add 完就立刻超时，"整批事务"实际等价
一股一事务，价格步骤持写锁时长 = 整站 503 时长。

本文件钉住新语义：
* 时间判据改为"距**上一次 add** 的空闲时长"，且必须已攒到 ≥2 只；
* 单只永不按时限提前落盘（由流水线排空时的 finally flush 收尾）；
* 只有 1 只时日志不写"批量"字样；
* 四条不变量（批内原子 / 失败整批回滚+逐股回退 / 内存硬上限 /
  try-finally 收尾 / 整段替换非 upsert）不因新判据退化。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from datetime import UTC, datetime

import pytest

import app.core.update as update_module
from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.update import IncrementalUpdater, _PriceReplaceBuffer


class _FakeClock:
    """Replaces ``app.core.update.time`` so tests can jump 30s without sleeping."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _price_result(adjust: str, dates: list[str], close: float = 10.0) -> FetchResult:
    payload = (adjust + ",".join(dates)).encode()
    return FetchResult(
        data=[
            {"trade_date": trade_date, "close": close, "volume": 100.0}
            for trade_date in dates
        ],
        metadata=SourceMetadata(
            source="local_cache",
            fetch_time=datetime.now(UTC),
            raw_response_hash=hashlib.sha256(payload).hexdigest(),
            confidence="strict",
        ),
        raw_response=payload,
    )


def _seed_stock(duckdb_store, code: str, dates: list[str], close: float = 9.0) -> None:
    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) "
        "VALUES (?, 'x', 'SHSE', true)",
        [code],
    )
    for table in ("price_daily_raw", "price_daily_qfq"):
        duckdb_store.write_query(
            f"INSERT INTO {table} (stock_code, trade_date, close) VALUES "
            + ", ".join("(?, ?, ?)" for _ in dates),
            [value for trade_date in dates for value in (code, trade_date, close)],
        )


def _count_transactions(duckdb_store, monkeypatch) -> list[int]:
    counter = [0]
    original = duckdb_store.transaction

    @contextlib.contextmanager
    def counting(*args, **kwargs):
        counter[0] += 1
        with original(*args, **kwargs) as connection:
            yield connection

    monkeypatch.setattr(duckdb_store, "transaction", counting)
    return counter


def _full_history_item(code: str, dates: list[str]) -> tuple:
    return (code, _price_result("raw", dates), _price_result("qfq", dates), True, True)


LOCAL = ["2026-11-07"]
REMOTE = ["2026-11-07", "2026-11-10", "2026-11-11"]


def test_adds_thirty_seconds_apart_still_merge_into_one_transaction(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """生产形态：每只股票抓取耗时 30s，3 只必须合并成 1 个事务（不是 3 个）。"""
    clock = _FakeClock()
    monkeypatch.setattr(update_module, "time", clock)
    codes = ["600101", "600102", "600103"]
    for code in codes:
        _seed_stock(duckdb_store, code, LOCAL)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._PRICE_FULL_REPLACE_THRESHOLD = 1
    transactions = _count_transactions(duckdb_store, monkeypatch)
    buffer = _PriceReplaceBuffer(max_stocks=8, max_rows=200_000, max_seconds=5.0)
    batch_sizes: list[int] = []
    success, updated, failed = 0, [], 0

    for index, code in enumerate(codes, start=1):
        # 一次 add = 一只股票抓完（28~635s 的生产现实，这里压成 30s 跳表）。
        success, updated, failed = updater._persist_price_batch(
            [_full_history_item(code, REMOTE)], success, updated, failed,
            buffer=buffer, failed_codes=[], batch_sizes=batch_sizes,
        )
        assert len(buffer) == index, (
            f"第 {index} 只不得被提前 flush（时间判据必须按'距上次 add'而非'开张时长'）"
        )
        clock.advance(30.0)

    success, updated, failed = updater._persist_price_batch(
        [], success, updated, failed,
        buffer=buffer, flush_buffer=True, failed_codes=[], batch_sizes=batch_sizes,
    )

    assert failed == 0
    assert success == len(codes)
    assert transactions[0] == 1, "3 只 / 间隔 30s 的慢抓取必须合并成 1 个事务"
    assert batch_sizes == [3]
    rows = duckdb_store.read_query(
        "SELECT stock_code, COUNT(*) AS cnt FROM price_daily_qfq "
        "WHERE stock_code LIKE '6001%' GROUP BY stock_code ORDER BY stock_code"
    )
    assert [row["cnt"] for row in rows] == [len(REMOTE)] * len(codes)


def test_single_pending_item_never_flushes_on_time_alone(
    monkeypatch,
) -> None:
    """只有 1 只时，无论空闲多久都不得按时限落盘（否则永远 N=1）。"""
    clock = _FakeClock()
    monkeypatch.setattr(update_module, "time", clock)
    buffer = _PriceReplaceBuffer(max_stocks=8, max_rows=200_000, max_seconds=5.0)
    buffer.add(_full_history_item("600201", REMOTE))

    clock.advance(30.0)
    assert buffer.should_flush() is False, "单只不得按空闲时限提前落盘"

    buffer.add(_full_history_item("600202", REMOTE))
    assert buffer.should_flush() is False, "刚 add 完（空闲 0s）不得立刻落盘"

    clock.advance(6.0)
    assert buffer.should_flush() is True, "已攒 ≥2 只且空闲超过阈值才允许落盘"


def test_stock_and_row_limits_still_force_flush() -> None:
    """硬上限不变：只数/行数到顶必须立刻落盘，与时间判据无关。"""
    by_stocks = _PriceReplaceBuffer(max_stocks=2, max_rows=10**9, max_seconds=3600.0)
    by_stocks.add(_full_history_item("600301", REMOTE))
    assert by_stocks.should_flush() is False
    by_stocks.add(_full_history_item("600302", REMOTE))
    assert by_stocks.should_flush() is True

    by_rows = _PriceReplaceBuffer(max_stocks=100, max_rows=10, max_seconds=3600.0)
    big = _price_result("raw", [f"2026-10-{day:02d}" for day in range(1, 21)])
    by_rows.add(("600303", big, big, True, True))
    assert by_rows.rows >= 10
    assert by_rows.should_flush() is True


def test_single_stock_flush_does_not_claim_batch_wording(
    duckdb_store, sqlite_store, caplog,
) -> None:
    """只有 1 只时日志必须写"价格全历史写入 1 只"，不得写"批量"（避免证据失真）。"""
    _seed_stock(duckdb_store, "600401", LOCAL)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._PRICE_FULL_REPLACE_THRESHOLD = 1
    buffer = _PriceReplaceBuffer(max_stocks=8, max_rows=200_000, max_seconds=5.0)
    buffer.add(_full_history_item("600401", REMOTE))

    with caplog.at_level(logging.INFO, logger="app.core.update"):
        updater._persist_price_batch(
            [], 0, [], 0, buffer=buffer, flush_buffer=True, failed_codes=[],
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("价格全历史写入 1 只" in message for message in messages), messages
    assert not any("批量写入" in message for message in messages), messages


def test_multi_stock_flush_logs_batch_wording(
    duckdb_store, sqlite_store, caplog,
) -> None:
    _seed_stock(duckdb_store, "600501", LOCAL)
    _seed_stock(duckdb_store, "600502", LOCAL)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._PRICE_FULL_REPLACE_THRESHOLD = 1
    buffer = _PriceReplaceBuffer(max_stocks=8, max_rows=200_000, max_seconds=5.0)
    buffer.add(_full_history_item("600501", REMOTE))
    buffer.add(_full_history_item("600502", REMOTE))

    with caplog.at_level(logging.INFO, logger="app.core.update"):
        updater._persist_price_batch(
            [], 0, [], 0, buffer=buffer, flush_buffer=True, failed_codes=[],
        )

    assert any("价格全历史批量写入 2 只" in record.getMessage() for record in caplog.records)


# ─── 四条不变量在新判据下不退化 ─────────────────────────────────────


def test_batch_write_stays_atomic_and_replaces_whole_history(
    duckdb_store, sqlite_store,
) -> None:
    """批内一个事务 + 每只整段替换（删除源侧不再返回的旧行，不留下重复）。

    截断防线要求远端覆盖本地最老日期，所以"源侧不再返回"的旧行只能落在
    最新端（本地 11-07 已不在远端返回里）——这正是 DELETE+INSERT 与
    upsert 追加的行为分界点。
    """
    local = ["2026-11-05", "2026-11-06", "2026-11-07"]
    remote = ["2026-11-05", "2026-11-06"]
    _seed_stock(duckdb_store, "600601", local)
    _seed_stock(duckdb_store, "600602", local)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    buffer = _PriceReplaceBuffer(max_stocks=8, max_rows=200_000, max_seconds=5.0)
    buffer.add(_full_history_item("600601", remote))
    buffer.add(_full_history_item("600602", remote))

    success, _, failed = updater._persist_price_batch(
        [], 0, [], 0, buffer=buffer, flush_buffer=True, failed_codes=[],
    )

    assert (success, failed) == (2, 0)
    for code in ("600601", "600602"):
        dates = [
            str(row["trade_date"])[:10]
            for row in duckdb_store.read_query(
                "SELECT trade_date FROM price_daily_qfq WHERE stock_code = ? "
                "ORDER BY trade_date",
                [code],
            )
        ]
        assert dates == remote, "整段替换：源侧不再返回的 11-07 必须被删除"
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS c FROM price_daily_qfq WHERE stock_code = '600601'"
    )[0]["c"] == 2, "不得出现重复行"


def test_batch_failure_rolls_back_then_falls_back_per_stock(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """整批事务失败 → 整批回滚（旧行不丢）→ 同一批 items 逐股回退，不漏不重。"""
    codes = ["600701", "600702", "600703"]
    for code in codes:
        _seed_stock(duckdb_store, code, ["2026-11-05", "2026-11-06"])
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    original = updater._insert_price_rows

    def explode_on_last(connection, table, stock_code, result, *, full_replace):
        if stock_code == "600703":
            raise RuntimeError("simulated write failure on the 3rd stock")
        return original(connection, table, stock_code, result, full_replace=full_replace)

    monkeypatch.setattr(updater, "_insert_price_rows", explode_on_last)
    buffer = _PriceReplaceBuffer(max_stocks=8, max_rows=200_000, max_seconds=5.0)
    for code in codes:
        buffer.add(_full_history_item(code, ["2026-11-05", "2026-11-06", "2026-11-07"]))
    failed_codes: list[str] = []

    success, updated, failed = updater._persist_price_batch(
        [], 0, [], 0, buffer=buffer, flush_buffer=True, failed_codes=failed_codes,
    )

    assert (success, failed) == (2, 1)
    assert failed_codes == ["600703"]
    assert sorted(updated) == ["600701", "600702"]
    for code in ("600701", "600702"):
        assert duckdb_store.read_query(
            "SELECT COUNT(*) AS c FROM price_daily_qfq WHERE stock_code = ?", [code],
        )[0]["c"] == 3
    kept = duckdb_store.read_query(
        "SELECT COUNT(*) AS c FROM price_daily_qfq WHERE stock_code = '600703'"
    )[0]["c"]
    assert kept == 2, "失败股票必须保留旧行（整批回滚后没有被删）"


def test_buffer_is_drained_and_released_after_flush(
    duckdb_store, sqlite_store,
) -> None:
    """内存硬上限：drain 之后缓冲必须清空。"""
    _seed_stock(duckdb_store, "600801", LOCAL)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    buffer = _PriceReplaceBuffer(max_stocks=8, max_rows=200_000, max_seconds=5.0)
    buffer.add(_full_history_item("600801", REMOTE))

    updater._persist_price_batch(
        [], 0, [], 0, buffer=buffer, flush_buffer=True, failed_codes=[],
    )

    assert len(buffer) == 0 and buffer.rows == 0


def test_finally_flush_survives_pipeline_exception(duckdb_store, sqlite_store) -> None:
    """try/finally 收尾不因新判据丢失：异常退出也必须把缓冲落盘。"""

    class FullHistoryAdapter:
        def fetch(self, request):
            return _price_result(
                request.adjust, ["2026-11-07", "2026-11-10", "2026-11-11"],
            )

    _seed_stock(duckdb_store, "600901", ["2026-11-07"])
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=FullHistoryAdapter(),
    )
    updater._PRICE_FULL_REPLACE_THRESHOLD = 0
    updater._latest_expected_trading_date = lambda today: "2026-11-11"
    updater._PRICE_WRITE_BUFFER_STOCKS = 100
    updater._PRICE_WRITE_BUFFER_ROWS = 10_000_000
    updater._PRICE_WRITE_BUFFER_SECONDS = 3600.0
    updater.price_fetch_pipeline_depth = 1
    updater.price_fetch_batch_size = 1

    def boom(step: str, info: dict) -> None:
        raise RuntimeError("progress callback aborted the loop")

    with pytest.raises(RuntimeError):
        updater._update_prices_incremental(1, detail_cb=boom)

    dates = [
        str(row["trade_date"])[:10]
        for row in duckdb_store.read_query(
            "SELECT trade_date FROM price_daily_qfq WHERE stock_code = '600901' "
            "ORDER BY trade_date"
        )
    ]
    assert dates == ["2026-11-07", "2026-11-10", "2026-11-11"]
