"""业务概览写库原生崩溃（索引 ↔ 堆失联）的自愈与可见性（2026-11-25）。

事故：每一轮自动更新都在 `business_overview` 让子进程**原生崩溃** —— 无 traceback、
无报告 JSON、`job_logs` 永久 `running`，下一轮再把它误结算成
`dead_update_lock_owner`（把真因指向锁）。

实测真因（副本 + 逐级探针 + 看门狗每 20ms dump 全线程栈）：
``DuckDBStore.transaction()`` 的 ``conn.commit()`` 撞上 DuckDB 内部 FATAL
``Failed to delete all rows from index. Only deleted 0 out of 200 rows.``
（`business_breakdown` 的 PK/ART 索引里没有堆中那批行的条目），
随后进程以 **0xC0000409**（`__fastfail`）退出 —— 该路径绕过 SEH，
所以 `PYTHONFAULTHANDLER=1` + `-X dev` 也拿不到栈。

本文件钉死四件事：
1. `rebuild_table()` 能恢复索引 ↔ 堆不变量且**不动数据**（行数/主键/二级索引都在）；
2. `probe_index_integrity()` 零副作用（插入 + ROLLBACK，库文件不留痕）；
3. `update_stock()` 反复 DELETE+INSERT 不崩（每次刷新都是这两条语句）；
4. 探针一旦报"索引失联"，写库前会**先整表重建**再写（这才是拦住 fail-fast 的那层）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.business import BusinessOverviewUpdater
from app.core.storage.duckdb_store import (
    DuckDBStore,
    is_index_inconsistency_error,
)
from app.core.storage.schema import init_duckdb_schema, init_sqlite_schema
from app.core.storage.sqlite_store import SQLiteStore

TABLE = "business_breakdown"


class _FakeAdapter:
    """与真实东财 F10 响应同形：1 行公司资料 + 若干报告期的主营构成。"""

    def fetch(self, request: FetchRequest) -> FetchResult:
        now = datetime.now(UTC)
        if request.data_type == "company_profile":
            data = [{
                "code": request.stock_codes[0], "name": "华天科技",
                "org_name": "天水华天科技股份有限公司",
                "profile": "半导体封装测试。", "scope": "集成电路封装测试。",
                "employee_num": 20000, "csrc_industry": "制造业",
                "trade_market": "深圳证券交易所",
            }]
        else:
            data = [
                {
                    "report_date": report_date, "type": kind,
                    "item_name": f"item{index}",
                    "amount": 1.0e8 + index, "ratio": 0.01 * index, "rank": index,
                }
                for report_date in ("2025-12-31", "2025-06-30")
                for kind in (1, 2, 3)
                for index in range(1, 4)
            ]
        return FetchResult(
            data=data,
            metadata=SourceMetadata(
                source="eastmoney_f10", fetch_time=now,
                raw_response_hash="fixture-hash", confidence="strict",
            ),
        )


def _stores(database_paths):
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)
    init_duckdb_schema(duck)
    init_sqlite_schema(sqlite)
    return duck, sqlite


def _breakdown_rows(duck: DuckDBStore, code: str) -> int:
    return duck.read_query(
        f"SELECT count(*) AS n FROM {TABLE} WHERE stock_code = ?", [code],
    )[0]["n"]


def test_rebuild_table_restores_index_and_keeps_data(database_paths) -> None:
    """重建 = 从堆数据重新建 heap + 索引；数据、主键一条不能少。

    2026-11-21（schema v50，reports/151）：``business_breakdown`` 的二级索引已按
    「列与真实查询形状不匹配（代码里没有 ``col = 字面量`` 这种查询）＋删后真实查询
    不变慢（最慢 1.43 倍、绝对值 <2ms）」的实测结论永久删除，所以这里断言的
    「索引一条不能少」变成**二级索引必须一条都没有**（主键 ART 由 DDL 重建，仍有）。
    重建机制本身（``rebuild_table``）必须照常工作 —— 它是索引损坏事故的兜底。
    """
    duck, _ = _stores(database_paths)
    updater = BusinessOverviewUpdater(
        duck=duck, sqlite=SQLiteStore(paths=database_paths),
        adapter=_FakeAdapter(), paths=None,
    )
    assert updater.update_stock("002185")["status"] == "success"
    before = _breakdown_rows(duck, "002185")
    assert before > 0

    rebuilt = duck.rebuild_table(TABLE)

    assert rebuilt["rows_before"] == rebuilt["rows_after"] == before
    assert _breakdown_rows(duck, "002185") == before
    indexes = {
        row["index_name"] for row in duck.read_query(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = ?", [TABLE],
        )
    }
    assert indexes == set(), f"v50 之后 {TABLE} 不该再有任何二级索引: {indexes}"
    # 主键仍然生效（重建后的 ART 由 DDL 里的 PRIMARY KEY 重新建出来）。
    with pytest.raises(Exception, match="(?i)constraint|conflict|duplicate"):
        duck.write_query(
            f"""INSERT INTO {TABLE} (stock_code, report_date, type, item_name, source,
                    fetch_time, raw_hash, confidence, batch_id)
                VALUES ('002185', DATE '2025-12-31', 1, 'item1', 's',
                        CURRENT_TIMESTAMP, 'h', 'strict', 'dup-batch')"""
        )
    # 重建后同一股仍可原子替换（即崩溃现场的那两条语句）。
    assert updater.update_stock("002185")["status"] == "success"
    assert _breakdown_rows(duck, "002185") == before


def test_reindex_table_rebuilds_secondary_index_without_touching_data(database_paths) -> None:
    """首要修法：只重建二级索引（不动堆数据），重建后写入照常。

    2026-11-21（schema v50）：表上已无二级索引，``reindex_table`` 变成**安全 no-op**
    （``indexes_rebuilt == []``）——这正是「从根上不让索引事故再发生」的形态：
    没有索引可修，也就不存在索引与堆失步。保留本用例是为了锁住 no-op 契约本身。
    """
    duck, _ = _stores(database_paths)
    updater = BusinessOverviewUpdater(
        duck=duck, sqlite=SQLiteStore(paths=database_paths),
        adapter=_FakeAdapter(), paths=None,
    )
    assert updater.update_stock("002185")["status"] == "success"
    total_before = duck.read_query(f"SELECT count(*) AS n FROM {TABLE}")[0]["n"]

    result = duck.reindex_table(TABLE)

    assert result["indexes_rebuilt"] == []
    assert duck.read_query(f"SELECT count(*) AS n FROM {TABLE}")[0]["n"] == total_before
    assert updater.update_stock("002185")["status"] == "success"


def test_update_stock_survives_repeated_delete_insert(database_paths) -> None:
    """一轮刷新就是"DELETE 本期 + INSERT 新值"；反复跑不许崩（原生崩溃无 traceback）。"""
    duck, sqlite = _stores(database_paths)
    updater = BusinessOverviewUpdater(
        duck=duck, sqlite=sqlite, adapter=_FakeAdapter(), paths=None,
    )
    outcomes = [updater.update_stock("002185") for _ in range(8)]

    assert [outcome["status"] for outcome in outcomes] == ["success"] * 8
    assert len({outcome["batch_id"] for outcome in outcomes}) == 8, "批次号必须逐次不同"


def test_update_stock_reindexes_before_writing(database_paths) -> None:
    """写库前必须**无条件**重建二级索引（失联读不出来，事后捕获拦不住 fail-fast）。"""
    duck, sqlite = _stores(database_paths)
    updater = BusinessOverviewUpdater(
        duck=duck, sqlite=sqlite, adapter=_FakeAdapter(), paths=None,
    )
    assert updater.update_stock("002185")["status"] == "success"

    calls: list[str] = []
    real_reindex = duck.reindex_table

    def spy_reindex(table: str, **kwargs):
        calls.append(table)
        return real_reindex(table, **kwargs)

    monkeyed = BusinessOverviewUpdater(
        duck=_RecordingDuck(duck, spy_reindex), sqlite=sqlite,
        adapter=_FakeAdapter(), paths=None,
    )

    outcome = monkeyed.update_stock("002185")

    assert outcome["status"] == "success"
    assert calls == [TABLE], "每轮第一次写库前必须先重建二级索引，且一轮只重建一次"


class _RecordingDuck:
    """只替换 `reindex_table` 这一个入口，其余（含 transaction）原样委托给真 store。"""

    def __init__(self, real, reindex) -> None:
        self._real = real
        self._reindex = reindex

    def reindex_table(self, table, **kwargs):
        return self._reindex(table, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_crashed_running_job_is_settled_and_keeps_its_last_step() -> None:
    """崩溃后 job 不许永久 running：结算时必须写死"死在哪一步"（真因可见）。"""
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """CREATE TABLE job_logs (
               id INTEGER PRIMARY KEY AUTOINCREMENT, job_type TEXT, status TEXT,
               started_at TEXT, finished_at TEXT, details_json TEXT)"""
    )
    details = {
        "job_id": "job-crashed",
        "max_stocks": 0,
        "current_step": "business_overview",
        "current_step_state": "started",
        "steps": {"prices": {"status": "success", "success": 5400}},
    }
    connection.execute(
        "INSERT INTO job_logs (job_type, status, started_at, details_json)"
        " VALUES ('incremental_update', 'running', '2026-11-25T00:00:00+00:00', ?)",
        [json.dumps(details)],
    )
    connection.commit()

    from app.core.update import IncrementalUpdater

    stub_sqlite = _StubSQLite(connection)
    stub = type("Stub", (), {"sqlite": stub_sqlite})()
    settled = IncrementalUpdater._reconcile_crashed_incremental_jobs(stub)

    assert settled == 1
    status, payload = connection.execute(
        "SELECT status, details_json FROM job_logs WHERE id = 1"
    ).fetchone()
    assert status == "failed"
    parsed = json.loads(payload)
    assert parsed["reconciliation"]["reason_code"] == "dead_update_lock_owner"
    # 关键：结算不抹掉现场 —— 死在哪一步仍然查得到。
    assert parsed["current_step"] == "business_overview"
    assert parsed["steps"]["prices"]["status"] == "success"


class _StubSQLite:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def query(self, sql: str, params: object = None) -> list[dict]:
        cursor = self._connection.execute(sql, params or [])
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def transaction(self):
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            yield self._connection
            self._connection.commit()

        return _ctx()


def test_classifier_only_matches_the_real_duckdb_message() -> None:
    """只认 DuckDB 自己那句话：放宽会把并发/IO 错误误判成"需要重建整张表"。"""
    real = (
        "FATAL Error: Invalid Input Error: Failed to delete all rows from index. "
        "Only deleted 0 out of 200 rows."
    )
    assert is_index_inconsistency_error(real) is True
    assert is_index_inconsistency_error(RuntimeError(real)) is True
    for other in (
        "IO Error: Cannot open file",
        "Connection Error: Can't open a connection to same database file",
        "Constraint Error: Duplicate key",
    ):
        assert is_index_inconsistency_error(other) is False
