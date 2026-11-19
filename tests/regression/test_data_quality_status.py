from __future__ import annotations

import hashlib

import pytest

from app.core.data_quality import _data_dates, build_data_quality_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed


def test_data_quality_status_exposes_untrusted_and_stale_data(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _insert_quality_fixture(duckdb_store, sqlite_store)

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["dates"]["price"] == "2026-07-17"
    assert status["dates"]["balance_sheet"]["latest_record"] == "2026-03-31"
    assert status["dates"]["balance_sheet"]["latest_complete"] == "2025-03-31"
    assert status["dividends"]["unverified_period_end_rows"] == 2
    assert status["dividends"]["active_missing_announcement_rows"] == 2
    assert status["lineage"]["invalid_hash_rows"] == 1
    assert status["lineage"]["orphan_batch_rows"] == 1
    assert status["lineage"]["archive_gap_rows"] == 0
    assert status["operations"]["unpublished_overrides"] == 1
    assert status["operations"]["running_jobs"] == 1
    assert {
        "FINANCIAL_SHELL_ROWS",
        "SNAPSHOT_STALE",
        "DIVIDEND_DATES_UNVERIFIED",
        "LINEAGE_INVALID",
        "UNPUBLISHED_OVERRIDES",
        "STALE_RUNNING_JOBS",
    }.issubset(set(status["warning_codes"]))


def test_data_dates_supports_snapshot_schema_before_freshness_migration(
    database_paths: DatabasePathSet,
) -> None:
    duck = DuckDBStore(paths=database_paths)
    duck.execute_script(
        """
        CREATE TABLE price_daily_raw (trade_date DATE);
        CREATE TABLE balance_sheet (
            report_date DATE,
            total_assets DOUBLE,
            total_liabilities DOUBLE,
            total_equity DOUBLE,
            total_equity_parent DOUBLE
        );
        CREATE TABLE income_statement (
            report_date DATE,
            revenue DOUBLE,
            parent_net_profit DOUBLE
        );
        CREATE TABLE cash_flow (report_date DATE, cf_from_operating DOUBLE);
        CREATE TABLE indicator_snapshot (report_date DATE);
        INSERT INTO indicator_snapshot VALUES ('2025-03-31');
        """
    )

    dates = _data_dates(duck)

    assert dates["indicator_snapshot"] == {
        "latest_complete": "2025-03-31",
        "calculated_at": None,
        "latest_price_date": None,
    }


def test_data_dates_price_uses_latest_available_date(
    duckdb_store: DuckDBStore,
) -> None:
    seed = Seed(duckdb_store)
    seed.stock("000001", name="A", exchange="SZSE", is_listed=True)
    seed.stock("000002", name="B", exchange="SZSE", is_listed=True)
    for table in ("price_daily_raw", "price_daily_qfq"):
        seed.prices("000001", [("2026-08-05", 10)], table=table)
        seed.prices("000002", [("2026-08-04", 20)], table=table)

    dates = _data_dates(duckdb_store)

    assert dates["price"] == "2026-08-05"


def test_quality_status_flags_missing_inputs_for_a_currently_listed_stock(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    # 2026-11-17（reports/135）：必须显式给一个**很久以前**的 listing_date。
    # 本用例的语义是"一只老股完全没有数据 → 必须被标记"；而 readiness 现在把
    # `listing_date IS NULL` 当作"上市日期未知 = 按新股豁免"（否则一只新股就能
    # 让 /api/ready 永久 503，实测 920309）。留空会让本用例变成"新股豁免"场景。
    Seed(duckdb_store).stock(
        "000001", name="incomplete", exchange="SZSE", is_listed=True,
        listing_date="2010-01-04",
    )

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["minimum_data_readiness"]["missing_counts"] == {
        "pool_metadata": 1,
        "raw_history": 1,
        "qfq_history": 1,
        "price_freshness": 1,
        "meaningful_volume": 1,
        "financial_period": 1,
        "share_capital": 1,
        "corporate_action_dividend_lineage": 1,
        "snapshot_input_freshness": 1,
        "snapshot_price_coherence": 1,
        "lineage_coverage": 1,
    }
    assert "MINIMUM_DATA_NOT_READY" in status["warning_codes"]


def test_recent_listing_data_gaps_are_disclosed_but_share_capital_still_blocks(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """新股 90 天豁免 + 股本缺口阈值政策（readiness 与发布门禁同源）。

    2026-11-12 契约 §3（语义变更一）：此前 readiness 用自己的一份 SQL 判
    ``has_share_capital``，把「上市 ≤90 天且 total/circ 皆为 NULL」的新股也写进
    ``missing_counts["share_capital"]``（只在阻断循环里豁免），于是同一时刻
    「门禁报 5 只、readiness 报 26 只」。现在 readiness 复用唯一真相函数
    ``listed_share_capital_gap_codes()``，90 天豁免已内含其中，所以这批新股
    根本不属于 share_capital 缺口（缺口由 price_freshness / financial_period
    等类别披露），断言为 "不在 missing_counts 里"。

    2026-11-13 P1（语义变更二）：readiness 的 share_capital 改用与
    ``snapshot_publish_gate`` 同一判定 ``share_capital_hard_block``：
    「绝对阈值``max(50,1%)`` **或** ≥1/3 上市股违规」。本 fixture 只有 1 只上市
    股票，1 只缺口即 100% ≥ 1/3 → 判为系统性 → 仍阻断 ready（与旧断言一致，
    但理由变成占比判据，而不是"任一缺口即否决"）。缺口计数与
    ``share_capital_policy`` 在任何情况下都如实披露。
    """
    duckdb_store.write_query(
        """INSERT INTO stock_meta
             (stock_code, name, exchange, is_listed, listing_date, is_st, is_suspended)
           VALUES ('301999', 'NEW', 'SZSE', TRUE, CURRENT_DATE, FALSE, FALSE)"""
    )

    readiness = build_data_quality_status(duckdb_store, sqlite_store)["minimum_data_readiness"]

    assert readiness["ready"] is True
    assert readiness["missing_counts"]["price_freshness"] == 1
    assert readiness["missing_counts"]["financial_period"] == 1
    # 90 天豁免：两列皆 NULL 的新股不属于股本缺口（不再重复计入披露项）。
    assert "share_capital" not in readiness["missing_counts"]

    duckdb_store.write_query(
        """UPDATE stock_meta SET total_shares = 100
           WHERE stock_code = '301999'"""
    )

    readiness = build_data_quality_status(duckdb_store, sqlite_store)["minimum_data_readiness"]

    # 豁免失效 → 缺口成立并如实计数；1/1 = 100% ≥ 1/3 → 占比判据阻断。
    assert readiness["missing_counts"]["share_capital"] == 1
    assert readiness["missing"]["share_capital"] == ["301999"]
    assert readiness["share_capital_policy"]["blocking"] is True
    assert readiness["share_capital_policy"]["quarantined"] is False
    assert readiness["ready"] is False

    duckdb_store.write_query(
        """UPDATE stock_meta SET circ_shares = 50
           WHERE stock_code = '301999'"""
    )

    readiness = build_data_quality_status(duckdb_store, sqlite_store)["minimum_data_readiness"]

    assert readiness["ready"] is True
    assert "share_capital" not in readiness["missing_counts"]
    assert readiness["share_capital_policy"]["gap_count"] == 0


def test_quality_status_flags_active_dividend_alias_and_lineage_archive_gaps(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute(
            """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
               VALUES ('000001', 'canonical', 'SZSE', TRUE)"""
        )
        connection.execute(
            """INSERT INTO price_daily_raw (stock_code, trade_date, close)
               VALUES ('SZ000001', CURRENT_DATE, 10)"""
        )
        connection.execute(
            """INSERT INTO dividends (stock_code, ex_date, dividend_per_share)
               VALUES ('000001', '2025-08-15', 1)"""
        )
        connection.execute(
            """INSERT INTO fetch_batch
                (batch_id, data_type, source, adapter_version, fetch_time, raw_response_hash, row_count, confidence)
               VALUES ('batch-1', 'financial', 'test', 'v1', CURRENT_TIMESTAMP, ?, 1, 'verified')""",
            ["a" * 64],
        )
        connection.execute(
            """INSERT INTO source_audit
                (stock_code, field_name, source, fetch_batch_id, fetch_time, raw_response_hash, confidence)
               VALUES ('000001', 'revenue', 'test', 'batch-1', CURRENT_TIMESTAMP, ?, 'verified')""",
            ["b" * 64],
        )

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["dividends"]["active_missing_announcement_rows"] == 1
    assert status["lineage"]["audit_archive_gap_rows"] == 1
    assert status["lineage"]["batch_archive_gap_rows"] == 1
    assert status["lineage"]["archive_gap_rows"] == 2
    assert status["code_identity"]["raw_alias_rows"] == 1
    assert {"DIVIDEND_DATES_UNVERIFIED", "LINEAGE_INVALID", "CODE_IDENTITY_ALIAS"}.issubset(
        status["warning_codes"]
    )


def test_quality_status_reports_incompatible_live_schema_without_querying_it(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.execute_script("DROP TABLE raw_response_archive")

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["minimum_data_readiness"]["ready"] is False
    assert status["warning_codes"] == ["LIVE_SCHEMA_INCOMPATIBLE", "MINIMUM_DATA_NOT_READY"]


def test_quality_status_detects_archive_payload_hash_mismatch(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    with duckdb_store.write_connection() as connection:
        connection.execute(
            """INSERT INTO raw_response_archive (raw_response_hash, source, fetch_time, payload)
               VALUES (?, 'test', CURRENT_TIMESTAMP, ?)""",
            ["a" * 64, b"tampered payload"],
        )

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["lineage"]["hash_mismatch_rows"] == 1
    assert "LINEAGE_INVALID" in status["warning_codes"]


def test_archive_hash_check_is_cached_between_status_calls(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    payload = b"original payload"
    digest = hashlib.sha256(payload).hexdigest()
    duckdb_store.write_query(
        """INSERT INTO raw_response_archive (raw_response_hash, source, fetch_time, payload)
           VALUES (?, 'test', CURRENT_TIMESTAMP, ?)""",
        [digest, payload],
    )

    first = build_data_quality_status(duckdb_store, sqlite_store)
    assert first["lineage"]["hash_mismatch_rows"] == 0
    assert first["lineage"]["archive_hash_check"]["cached"] is False

    duckdb_store.write_query(
        """INSERT INTO raw_response_archive (raw_response_hash, source, fetch_time, payload)
           VALUES (?, 'test', CURRENT_TIMESTAMP, ?)""",
        ["b" * 64, b"tampered payload"],
    )
    cached = build_data_quality_status(duckdb_store, sqlite_store)
    assert cached["lineage"]["hash_mismatch_rows"] == 0
    assert cached["lineage"]["archive_hash_check"] == {"cached": True, "ttl_seconds": 60}

    refreshed = build_data_quality_status(
        duckdb_store, sqlite_store, force_archive_hash_recheck=True
    )
    assert refreshed["lineage"]["hash_mismatch_rows"] == 1
    assert refreshed["lineage"]["archive_hash_check"]["cached"] is False
    assert "LINEAGE_INVALID" in refreshed["warning_codes"]


def test_archive_hash_check_fails_closed_when_archive_is_unreadable(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_read_query = duckdb_store.read_query

    def fail_on_archive_scan(sql: str, params: list | None = None) -> list:
        if "FROM raw_response_archive_all WHERE payload IS NOT NULL" in sql:
            raise RuntimeError("archive unreadable")
        return real_read_query(sql, params)

    monkeypatch.setattr(duckdb_store, "read_query", fail_on_archive_scan)

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["lineage"]["hash_mismatch_rows"] == 1
    assert "LINEAGE_INVALID" in status["warning_codes"]


def _insert_quality_fixture(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    seed = Seed(duck)
    seed.prices("600519", [("2026-07-17", 1500)])
    # 完整期 vs "壳行"（三列全 NULL）必须逐字构造：工厂不给业务列默认值，
    # 少写哪一列就是哪一列缺失，这正是 FINANCIAL_SHELL_ROWS 的判据。
    seed.balance("600519", "2025-03-31", total_assets=1000, total_liabilities=300, total_equity=700)
    seed.balance("600519", "2026-03-31")
    seed.income("600519", "2025-03-31", revenue=500, parent_net_profit=100)
    # 最新期只有 parent_net_profit：revenue 缺失是低覆盖构造本身
    seed.income("600519", "2026-03-31", parent_net_profit=110)
    seed.cash_flow("600519", "2025-03-31", cf_from_operating=120)
    seed.snapshot("600519", "2025-03-31", latest_price_date="2026-07-01")

    with duck.write_connection() as connection:
        connection.execute(
            """
            INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
            VALUES
                ('600519', '2023-12-31', NULL, 1.0),
                ('600519', '2024-06-30', NULL, 2.0)
            """
        )
        connection.execute(
            """
            INSERT INTO source_audit
                (stock_code, field_name, source, fetch_batch_id, fetch_time,
                 raw_response_hash, confidence)
            VALUES
                ('600519', 'total_assets', 'local_cache', 'orphan', now(), '', 'missing')
            """
        )

    with sqlite.transaction() as connection:
        connection.execute(
            """
            INSERT INTO manual_overrides
                (stock_code, field_name, override_value, reason, status)
            VALUES ('600519', 'total_assets', 2000, 'draft', 'active')
            """
        )
        connection.execute(
            """
            INSERT INTO job_logs (job_type, status, started_at)
            VALUES ('backfill', 'running', '2025-01-01T00:00:00')
            """
        )
