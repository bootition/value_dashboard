from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.update import IncrementalUpdater
from tests.support.seed import Seed
from tests.support.seed_sqlite import CURRENT_TIMESTAMP, SeedSqlite


class PriceAdapter:
    def fetch(self, request: FetchRequest) -> FetchResult:
        assert request.data_type == "price_daily"
        assert request.adjust == "qfq"
        payload = b'{"trade_date":"2026-07-20","close":11.0}'
        return FetchResult(
            data=[
                {
                    "trade_date": "2026-07-20",
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.0,
                    "close": 11.0,
                    "volume": 100.0,
                    "turnover": 1100.0,
                    "turnover_rate": 1.5,
                }
            ],
            metadata=SourceMetadata(
                source="akshare_eastmoney",
                fetch_time=datetime.now(UTC),
                raw_response_hash=hashlib.sha256(payload).hexdigest(),
                confidence="approximate",
            ),
            raw_response=payload,
        )


def test_retry_schema_records_request_metadata(sqlite_store: SQLiteStore) -> None:
    columns = sqlite_store.query("PRAGMA table_info(retry_list)")

    assert "extra_json" in {row["name"] for row in columns}


def test_qfq_retry_persists_before_queue_removal(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    # 第 2 类（lastrowid 回捞）：下面这条 `retry_list` 随后要靠 `cursor.lastrowid`
    # 拿到 id 再断言"该行被删除"；工厂刻意不返回 id，所以这行留内联。
    with sqlite_store.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO retry_list
                (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            [
                "600519",
                "price_daily",
                "akshare_eastmoney",
                "temporary",
                datetime.now(UTC).isoformat(),
                json.dumps({"adjust": "qfq"}),
            ],
        )
        retry_id = cursor.lastrowid

    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.adapter_mgr = PriceAdapter()
    updater.duck = duckdb_store
    updater.sqlite = sqlite_store

    tasks = sqlite_store.query(
        "SELECT id, stock_code, data_type, adapter, error, retry_count, extra_json FROM retry_list"
    )
    report = updater._retry_failed_tasks(tasks)

    prices = duckdb_store.read_query(
        """
        SELECT close, turnover_rate FROM price_daily_qfq
        WHERE stock_code = '600519' AND trade_date = '2026-07-20'
        """
    )
    remaining = sqlite_store.query("SELECT id FROM retry_list WHERE id = ?", [retry_id])
    assert report["status"] == "success"
    assert prices == [{"close": 11.0, "turnover_rate": 1.5}]
    assert remaining == []


def test_unretryable_domain_entry_is_retained_without_dead_loop(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """无逐股重试路径的条目（stock_list 等）保留为标记，但不进入重试循环反复失败。"""
    # 第 2 类（lastrowid 回捞）：本条 `retry_list` 的 id 用于下方查 `retry_count`，
    # 工厂不返回 id，故留内联。
    with sqlite_store.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO retry_list
                (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
            VALUES ('600519', 'stock_list', 'akshare_eastmoney', 'temporary', 0, ?, '{}')
            """,
            [datetime.now(UTC).isoformat()],
        )
        retry_id = cursor.lastrowid

    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.adapter_mgr = PriceAdapter()
    updater.duck = duckdb_store
    updater.sqlite = sqlite_store

    tasks = sqlite_store.query(
        "SELECT id, stock_code, data_type, adapter, error, retry_count, extra_json FROM retry_list"
    )
    report = updater._retry_failed_tasks(tasks)

    remaining = sqlite_store.query(
        "SELECT retry_count FROM retry_list WHERE id = ?",
        [retry_id],
    )
    assert report["status"] == "success"
    assert remaining == [{"retry_count": 0}], "条目保留且不得反复递增 retry_count（死循环）"


def test_price_retry_refetches_incrementally_from_local_latest(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    seen_requests: list[FetchRequest] = []

    class TrackingAdapter:
        def fetch(self, request: FetchRequest) -> FetchResult:
            seen_requests.append(request)
            payload = b'{"trade_date":"2026-08-05","close":11.0}'
            return FetchResult(
                data=[
                    {
                        "trade_date": "2026-08-05",
                        "open": 10.0, "high": 12.0, "low": 9.0,
                        "close": 11.0, "volume": 100.0,
                        "turnover": 1100.0, "turnover_rate": 1.5,
                    }
                ],
                metadata=SourceMetadata(
                    source="tencent",
                    fetch_time=datetime.now(UTC),
                    raw_response_hash=hashlib.sha256(payload).hexdigest(),
                    confidence="approximate",
                ),
                raw_response=payload,
            )

    # 第 2 类（lastrowid 回捞）：本函数三行种子（stock_meta / price_daily_qfq /
    # retry_list）都服务于"按 `retry_id` 回捞并断言已删除"，工厂不返回 id。
    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange) VALUES ('600519', 'MT', 'SHSE')"
    )
    duckdb_store.write_query(
        "INSERT INTO price_daily_qfq (stock_code, trade_date, close) VALUES "
        "('600519', '2026-08-01', 10)"
    )
    # 第 2 类（lastrowid 回捞）：下面这条 `retry_list` 的 id 也是靠
    # `cursor.lastrowid` 拿到后才断言"已删除"的。
    with sqlite_store.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO retry_list
                (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
            VALUES (?, 'price_daily', 'tencent', 'temporary', 0, ?, ?)
            """,
            [
                "600519",
                datetime.now(UTC).isoformat(),
                json.dumps({"adjust": "qfq"}),
            ],
        )
        retry_id = cursor.lastrowid

    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.adapter_mgr = TrackingAdapter()
    updater.duck = duckdb_store
    updater.sqlite = sqlite_store

    tasks = sqlite_store.query(
        "SELECT id, stock_code, data_type, adapter, error, retry_count, extra_json FROM retry_list"
    )
    report = updater._retry_failed_tasks(tasks)

    assert report["status"] == "success"
    assert len(seen_requests) == 1
    assert seen_requests[0].start_date == "2026-08-01"
    assert seen_requests[0].end_date == datetime.now().strftime("%Y-%m-%d")
    assert sqlite_store.query("SELECT id FROM retry_list WHERE id = ?", [retry_id]) == []


def test_cleanup_redundant_retries_drops_up_to_date_entries(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    def seed(code: str) -> None:
        # 第 1 类：`stock_meta` → `Seed.stock()`；`exchange='SHSE'` 逐字保留
        # （不靠代码前缀推断，避免工厂默认值与未知种子代码不一致）。
        Seed(duckdb_store).stock(code, name=code, exchange="SHSE")
        for table in ("price_daily_raw", "price_daily_qfq"):
            # 第 4 类（多站点缺口，本轮不补 API 且账本按 `{table}` 动态表名登记）：
            # 表名由循环变量生成，逐处按调用点拆开只会把"两张表同形"这件事藏起来；留内联。
            duckdb_store.write_query(
                f"INSERT INTO {table} (stock_code, trade_date, close) VALUES (?, '2026-08-05', 10)",
                [code],
            )
            payload = f"{code}:{table}".encode("ascii")
            digest = hashlib.sha256(payload).hexdigest()
            batch_id = f"b-{code}-{table}"
            # 第 3 类（故意造错/隔离归档）：归档/批次/审计三行要"配对"——这批归档
            # 里放的是哪一份 payload、哪条审计指向哪一批，正是本用例的论点；留内联。
            duckdb_store.write_query(
                "INSERT INTO raw_response_archive (raw_response_hash, source, fetch_time, payload) "
                "VALUES (?, 'tencent', CURRENT_TIMESTAMP, ?)",
                [digest, payload],
            )
            # 第 3 类：同上（`fetch_batch` 是被审计行引用的那一批，留内联）。
            duckdb_store.write_query(
                "INSERT INTO fetch_batch (batch_id, data_type, source, adapter_version, fetch_time, "
                "raw_response_hash, row_count, confidence) VALUES (?, ?, 'tencent', '1', "
                "CURRENT_TIMESTAMP, ?, 1, 'strict')",
                [batch_id, table, digest],
            )
            # 第 3 类：同上（`source_audit` 是配对里的审计行，留内联）。
            duckdb_store.write_query(
                "INSERT INTO source_audit (stock_code, field_name, report_date, value, source, "
                "fetch_batch_id, fetch_time, raw_response_hash, confidence) "
                "VALUES (?, 'latest_close', '2026-08-05', 10, 'tencent', ?, CURRENT_TIMESTAMP, ?, 'strict')",
                [code, batch_id, digest],
            )
        SeedSqlite(sqlite_store).retry(
            code, "price_daily", adapter="local_cache", error="no available adapter",
            last_attempt=datetime.now(UTC).isoformat(),
        )

    seed("600001")
    seed("600002")

    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    cleaned = updater._cleanup_redundant_retries("2026-08-05")

    assert cleaned == 2
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM retry_list")[0]["c"] == 0


def test_retry_exhaustion_moves_entry_to_missing_list(sqlite_store: SQLiteStore) -> None:
    """达到 max_retries 后，除 announcements 外必须转 missing 并移出重试队列。"""
    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.sqlite = sqlite_store
    # 第 2 类（lastrowid 回捞）：`_mark_retry_failed(retry_id, ...)` 要按 id 结算
    # 这一行，工厂刻意不返回 id；两列 `CURRENT_TIMESTAMP` 也随行留在这里。
    with sqlite_store.transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO retry_list
                   (stock_code, data_type, adapter, error, retry_count,
                    last_attempt, next_retry_at, max_retries, extra_json)
               VALUES ('000001', 'balance_sheet', 'sina', 'boom', 1,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 2, '{}')"""
        )
        retry_id = cursor.lastrowid

    updater._mark_retry_failed(retry_id, "still failing")

    rows = sqlite_store.query(
        "SELECT id FROM retry_list WHERE id = ?", [retry_id]
    )
    assert rows == []
    missing = sqlite_store.query(
        """SELECT stock_code, field_name, reason_code FROM missing_list
           WHERE stock_code = '000001'
             AND field_name = 'retry_exhausted:balance_sheet'"""
    )
    assert len(missing) == 1
    assert missing[0]["reason_code"] == "retry_exhausted"


def test_announcement_retry_is_never_removed_by_exhaustion(sqlite_store: SQLiteStore) -> None:
    """announcements 是公告 pending 持久化标记，耗尽后必须保留待重试。"""
    updater = IncrementalUpdater.__new__(IncrementalUpdater)
    updater.sqlite = sqlite_store
    # 第 2 类（lastrowid 回捞）：同上，`retry_id` 用于回查 `retry_count`。
    with sqlite_store.transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO retry_list
                   (stock_code, data_type, adapter, error, retry_count,
                    last_attempt, next_retry_at, max_retries, extra_json)
               VALUES ('603365', 'announcements', 'cninfo', 'pending', 1,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 2, '{}')"""
        )
        retry_id = cursor.lastrowid

    updater._mark_retry_failed(retry_id, "source still not ready")

    rows = sqlite_store.query(
        "SELECT retry_count FROM retry_list WHERE id = ?", [retry_id]
    )
    assert rows and rows[0]["retry_count"] == 2


def test_redundant_index_valuation_retry_is_cleaned_when_local_data_fresh(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """本地指数估值数据足够新时，源风控产生的 retry 不应永久留在队列。"""
    # 第 6 类（库侧相对时间）：`trade_date` 是 `CURRENT_DATE - INTERVAL '2 days'`
    # ——「两天前」必须由库的日历算，哨兵只收 `datetime('now','±N 天')` 形状，
    # 表达不了 DATE 列的日历减法；`index_valuation` 也没有工厂方法。
    duckdb_store.write_query(
        """INSERT INTO index_valuation
               (index_code, trade_date, pe_ttm, pe_metric, pb, div_yield,
                source, fetch_time, raw_hash, confidence, batch_id)
           VALUES ('000852', CURRENT_DATE - INTERVAL '2 days', 25.0, 'ttm', NULL, NULL,
                   'legulegu', CURRENT_TIMESTAMP, 'h', 'approximate', 'b1')"""
    )
    # 第 1 类：`retry_list` → `SeedSqlite.retry()`。原 SQL 的 `last_attempt` 是
    # 库侧 `CURRENT_TIMESTAMP`，按迁移守则显式传哨兵（否则"刚写的那条"会静默
    # 变成确定性的固定过去时间）。
    SeedSqlite(sqlite_store).retry(
        "000852", "index_valuation", adapter="legulegu", error="anti-bot",
        retry_count=2, last_attempt=CURRENT_TIMESTAMP,
    )
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    assert updater._cleanup_redundant_index_valuation_retries() == 1
    assert sqlite_store.query(
        "SELECT * FROM retry_list WHERE stock_code='000852'"
    ) == []
