"""2026-11-12 P0：价格全历史响应批量写入（契约 §5 / 工单条目 2）。

守住四条不变量：
① 事务边界内原子：批内 DELETE+INSERT 同事务，失败整批回滚、旧行不丢；
② 循环退出/异常路径必须 flush 剩余缓冲（try/finally）；
③ 缓冲有硬上限（只数 / 行数 / 存活秒数）；
④ 单只股票仍是"整段替换"语义，不得退化成 upsert 追加。
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from datetime import UTC, datetime

import pytest

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.update import IncrementalUpdater, _PriceReplaceBuffer


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


def _new_buffer(*, stocks: int = 8, rows: int = 200_000, seconds: float = 5.0):
    return _PriceReplaceBuffer(max_stocks=stocks, max_rows=rows, max_seconds=seconds)


def _count_transactions(duckdb_store, monkeypatch) -> list[int]:
    """Wrap DuckDBStore.transaction so tests can assert commit counts."""
    counter = [0]
    original = duckdb_store.transaction

    @contextlib.contextmanager
    def counting(*args, **kwargs):
        counter[0] += 1
        with original(*args, **kwargs) as connection:
            yield connection

    monkeypatch.setattr(duckdb_store, "transaction", counting)
    return counter


def _full_history_batch(stock_codes, dates):
    return [
        (code, _price_result("raw", dates), _price_result("qfq", dates), True, True)
        for code in stock_codes
    ]


# ─── 8. 事务次数 < 股票数，且数据完整 ────────────────────────────────


def test_full_history_batch_uses_fewer_transactions_than_stocks(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    codes = [f"6000{index:02d}" for index in range(1, 13)]
    local = ["2026-06-30"]
    remote = ["2026-06-30", "2026-07-01", "2026-07-02", "2026-07-03"]
    for code in codes:
        _seed_stock(duckdb_store, code, local)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    updater._PRICE_FULL_REPLACE_THRESHOLD = 1
    transactions = _count_transactions(duckdb_store, monkeypatch)
    buffer = _new_buffer(stocks=8)

    success, updated, failed = updater._persist_price_batch(
        _full_history_batch(codes, remote), 0, [], 0,
        buffer=buffer, flush_buffer=True, failed_codes=[],
    )

    assert (success, failed) == (len(codes), 0)
    assert sorted(updated) == sorted(codes)
    assert transactions[0] == 2, "12 只 / 缓冲上限 8 → 2 个事务，远少于 12"
    assert transactions[0] < len(codes)
    rows = duckdb_store.read_query(
        "SELECT stock_code, COUNT(*) AS rows, COUNT(DISTINCT trade_date) AS distinct_rows "
        "FROM price_daily_qfq WHERE stock_code LIKE '6000%' "
        "GROUP BY stock_code ORDER BY stock_code"
    )
    assert len(rows) == len(codes)
    for row in rows:
        assert row["rows"] == len(remote), f"{row['stock_code']} 数据必须完整"
        assert row["rows"] == row["distinct_rows"], "不得出现重复行"
    audits = duckdb_store.read_query(
        "SELECT stock_code, COUNT(*) AS cnt FROM source_audit "
        "WHERE field_name = 'latest_close' AND stock_code LIKE '6000%' "
        "GROUP BY stock_code"
    )
    assert len(audits) == len(codes), "每只股票仍必须有 latest_close 溯源"


# ─── ① 批内原子：失败整批回滚、旧行不丢 ──────────────────────────────


def test_group_replace_rolls_back_entire_batch_on_failure(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    codes = ["600101", "600102", "600103"]
    local = ["2026-06-01", "2026-06-02"]
    remote = ["2026-06-01", "2026-06-02", "2026-06-03"]
    for code in codes:
        _seed_stock(duckdb_store, code, local)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    original = updater._insert_price_rows

    def explode_on_last(connection, table, stock_code, result, *, full_replace):
        if stock_code == "600103":
            raise RuntimeError("simulated write failure on the 3rd stock")
        return original(connection, table, stock_code, result, full_replace=full_replace)

    monkeypatch.setattr(updater, "_insert_price_rows", explode_on_last)
    items = [
        (code, _price_result("raw", remote), _price_result("qfq", remote), True, True)
        for code in codes
    ]

    with pytest.raises(RuntimeError), duckdb_store.transaction() as connection:
        updater._persist_price_replace_group_in_connection(connection, items)

    for code in codes:
        kept = duckdb_store.read_query(
            "SELECT COUNT(*) AS cnt, MIN(trade_date) AS earliest "
            "FROM price_daily_qfq WHERE stock_code = ?",
            [code],
        )[0]
        assert kept["cnt"] == len(local), f"{code} 旧行必须在整批回滚后完好无损"
        assert str(kept["earliest"])[:10] == local[0]


def test_flush_falls_back_to_per_stock_when_batch_fails(
    duckdb_store, sqlite_store, monkeypatch,
) -> None:
    """整批事务失败后回退逐股：好股票照样落库，坏股票保留旧行并进失败列表。"""
    good = ["600201", "600202"]
    truncated = "600203"
    for code in [*good, truncated]:
        _seed_stock(duckdb_store, code, ["2026-06-01", "2026-06-02", "2026-06-03"])
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    remaining_dates = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]
    items = [
        (code, _price_result("raw", remaining_dates), _price_result("qfq", remaining_dates),
         True, True)
        for code in good
    ]
    # 只回 1 行（本地 3 行）→ 触发"源截断"防线，绝不允许删历史。
    items.append((
        truncated,
        _price_result("raw", ["2026-06-04"]),
        _price_result("qfq", ["2026-06-04"]),
        True, True,
    ))
    buffer = _new_buffer(stocks=8)
    for item in items:
        buffer.add(item)
    failed_codes: list[str] = []

    success, updated, failed = updater._persist_price_batch(
        [], 0, [], 0, buffer=buffer, flush_buffer=True, failed_codes=failed_codes,
    )

    assert (success, failed) == (2, 1)
    assert failed_codes == [truncated]
    assert sorted(updated) == good
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS cnt FROM price_daily_qfq WHERE stock_code = ?", [truncated],
    )[0]["cnt"] == 3, "截断响应不得删除任何旧历史"
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS cnt FROM price_daily_qfq WHERE stock_code = '600201'",
    )[0]["cnt"] == len(remaining_dates)


# ─── ② 异常路径 flush 剩余缓冲 ───────────────────────────────────────


def test_remaining_buffer_is_flushed_when_pipeline_aborts(
    duckdb_store, sqlite_store,
) -> None:
    class FullHistoryAdapter:
        def fetch(self, request):
            return _price_result(
                request.adjust, ["2026-08-03", "2026-08-04", "2026-08-05"],
            )

    _seed_stock(duckdb_store, "600301", ["2026-08-03"])
    updater = IncrementalUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter_mgr=FullHistoryAdapter(),
    )
    updater._PRICE_FULL_REPLACE_THRESHOLD = 0
    updater._latest_expected_trading_date = lambda today: "2026-08-05"
    # 缓冲上限放到很大，保证正常路径不会提前落盘；异常必须靠 finally flush。
    updater._PRICE_WRITE_BUFFER_STOCKS = 100
    updater._PRICE_WRITE_BUFFER_ROWS = 10_000_000
    updater._PRICE_WRITE_BUFFER_SECONDS = 3600.0
    updater.price_fetch_pipeline_depth = 1
    updater.price_fetch_batch_size = 1

    def boom(step: str, info: dict) -> None:
        raise RuntimeError("progress callback aborted the loop")

    with pytest.raises(RuntimeError):
        updater._update_prices_incremental(1, detail_cb=boom)

    rows = duckdb_store.read_query(
        "SELECT trade_date, close FROM price_daily_qfq "
        "WHERE stock_code = '600301' ORDER BY trade_date"
    )
    assert [str(row["trade_date"])[:10] for row in rows] == [
        "2026-08-03", "2026-08-04", "2026-08-05",
    ], "循环异常退出时剩余缓冲必须落盘（已抓到的数据不能丢）"


# ─── ③ 缓冲硬上限 ────────────────────────────────────────────────────


def test_buffer_is_bounded_by_stocks_rows_and_seconds() -> None:
    by_stocks = _new_buffer(stocks=2, rows=10**9, seconds=3600.0)
    item = ("600401", _price_result("raw", ["2026-08-04"]),
            _price_result("qfq", ["2026-08-04"]), True, True)
    by_stocks.add(item)
    assert by_stocks.should_flush() is False
    by_stocks.add(("600402", item[1], item[2], True, True))
    assert by_stocks.should_flush() is True, "只数达上限必须落盘"

    by_rows = _new_buffer(stocks=100, rows=10, seconds=3600.0)
    big = _price_result("raw", [f"2026-07-{day:02d}" for day in range(1, 21)])
    by_rows.add(("600403", big, big, True, True))
    assert by_rows.rows >= 10
    assert by_rows.should_flush() is True, "行数达上限必须落盘（不得把整表读进内存）"

    by_time = _new_buffer(stocks=100, rows=10**9, seconds=0.001)
    by_time.add(item)
    deadline = time.monotonic() + 2.0
    while not by_time.should_flush() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert by_time.should_flush() is True, "存活时间达上限必须落盘"

    assert len(by_stocks.drain()) == 2
    assert len(by_stocks) == 0 and by_stocks.rows == 0, "drain 必须释放缓冲内存"


# ─── ④ 单股整段替换语义 ─────────────────────────────────────────────


def test_full_history_replace_drops_stale_rows_instead_of_appending(
    duckdb_store, sqlite_store,
) -> None:
    # 本地留着一条源侧已不再返回的旧行：整段替换必须把它删掉（upsert 会留着）。
    local = ["2026-06-01", "2026-06-02", "2026-06-03"]
    remote = ["2026-06-01", "2026-06-02"]
    _seed_stock(duckdb_store, "600501", local, close=9.0)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    buffer = _new_buffer(stocks=8)
    buffer.add((
        "600501",
        _price_result("raw", remote, close=11.0),
        _price_result("qfq", remote, close=11.0),
        True, True,
    ))

    updater._persist_price_batch(
        [], 0, [], 0, buffer=buffer, flush_buffer=True, failed_codes=[],
    )

    rows = duckdb_store.read_query(
        "SELECT trade_date, close FROM price_daily_qfq "
        "WHERE stock_code = '600501' ORDER BY trade_date"
    )
    dates = [str(row["trade_date"])[:10] for row in rows]
    assert dates == remote
    assert "2026-06-03" not in dates, "整段替换必须删除源侧不再返回的旧行，不能退化成 upsert 追加"
    assert all(float(row["close"]) == 11.0 for row in rows)


def test_repeated_replace_never_duplicates_rows(duckdb_store, sqlite_store) -> None:
    """同一只股票连续两次全历史替换不得产生重复行（DELETE 必须真的生效）。"""
    dates = ["2026-06-01", "2026-06-02", "2026-06-03"]
    _seed_stock(duckdb_store, "600601", [*dates, "2026-06-04"], close=9.0)
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    for _ in range(2):
        buffer = _new_buffer(stocks=8)
        buffer.add((
            "600601",
            _price_result("raw", dates, close=12.0),
            _price_result("qfq", dates, close=12.0),
            True, True,
        ))
        updater._persist_price_batch(
            [], 0, [], 0, buffer=buffer, flush_buffer=True, failed_codes=[],
        )

    for table in ("price_daily_raw", "price_daily_qfq"):
        rows = duckdb_store.read_query(
            f"SELECT COUNT(*) AS cnt, COUNT(DISTINCT trade_date) AS distinct_cnt "
            f"FROM {table} WHERE stock_code = '600601'"
        )[0]
        assert rows["cnt"] == len(dates)
        assert rows["cnt"] == rows["distinct_cnt"]
