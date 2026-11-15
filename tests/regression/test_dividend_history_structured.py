"""分红链改用结构化源（巨潮/东财）后的回归测试（2026-11-15）。

覆盖：
1. 单位换算：源口径"每 10 股" → 落库口径"每股"；
2. 巨潮列名 → 内部字段映射，含 除权日 / 股权登记日 / 派息日 / 报告时间 缺失的降级；
3. 东财备用源映射（除权除息日 / 现金分红-现金分红比例）；
4. ``apply`` 四类判定（matched / deleted_fake / deleted_empty / unavailable）各 1 个用例，
   临时 DuckDB + 最小 ``dividends`` / ``dividends_quarantine``；
5. 幂等：同一个 JSONL 跑两次，第二次四类计数全为 0、行数不变。

隔离纪律：全部 DuckDB 都在 ``--basetemp``（VD_TEST_RUN_ROOT）下新建，
正式库 ``data/valuedashboard.duckdb`` 全程只读/不接触；``.planning/`` 备份目录
在本测试里被 monkeypatch 掉，不落盘。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, VdEnv
from scripts import apply_dividend_history as apply_mod
from scripts import fetch_dividend_history as fetch_mod

# 与 app/core/storage/schema.py 的 dividends / dividends_quarantine 列定义一致的最小副本
# （只建这两张表，避免为单测初始化 20GB 正式库级别的整套 schema）。
_MINIMAL_SCHEMA = """
CREATE TABLE dividends (
    stock_code        VARCHAR NOT NULL,
    ex_date           DATE,
    announcement_date DATE,
    dividend_per_share DOUBLE,
    stock_dividend     DOUBLE,
    transfer_share     DOUBLE,
    rights_issue       DOUBLE,
    rights_issue_price DOUBLE,
    PRIMARY KEY (stock_code, ex_date)
);
CREATE TABLE dividends_quarantine (
    stock_code        VARCHAR NOT NULL,
    ex_date           DATE,
    announcement_date DATE,
    dividend_per_share DOUBLE,
    stock_dividend     DOUBLE,
    transfer_share     DOUBLE,
    rights_issue       DOUBLE,
    rights_issue_price DOUBLE,
    quarantine_reason  VARCHAR NOT NULL,
    quarantined_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (stock_code, ex_date)
);
"""

FETCHED_AT = "2026-11-15T00:00:00+00:00"
PLACEHOLDER_REASON = "unverified_period_end_placeholder"


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    """最小临时 DuckDB（两张表），全程在 pytest basetemp 内。"""
    paths = DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=tmp_path / "valuedashboard.duckdb",
        sqlite_path=tmp_path / "valuedashboard.sqlite",
        run_root=tmp_path,
    ).validate()
    duck = DuckDBStore(paths=paths)
    duck.execute_script(_MINIMAL_SCHEMA)
    return duck


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #
def _event(
    stock_code: str,
    ex_date: str,
    *,
    cash_per_10: float | None = None,
    stock_per_10: float | None = None,
    transfer_per_10: float | None = None,
    report_period: str | None = None,
    announcement_date: str | None = None,
    source: str = "cninfo",
) -> dict[str, object]:
    """一条 apply 阶段要吃的 JSONL 事件（单位是每 10 股）。"""
    return {
        "stock_code": stock_code,
        "ex_date": ex_date,
        "record_date": None,
        "pay_date": None,
        "announcement_date": announcement_date,
        "report_period": report_period,
        "report_period_raw": None,
        "dividend_type": "年度分红",
        "cash_per_10": cash_per_10,
        "stock_per_10": stock_per_10,
        "transfer_per_10": transfer_per_10,
        "source": source,
        "fetched_at": FETCHED_AT,
    }


def _seed_quarantine(store: DuckDBStore, rows: list[tuple[str, str, float | None]]) -> None:
    with store.write_connection() as conn:
        conn.executemany(
            """INSERT INTO dividends_quarantine
               (stock_code, ex_date, dividend_per_share, quarantine_reason, quarantined_at)
               VALUES (?, CAST(? AS DATE), ?, ?, CURRENT_TIMESTAMP)""",
            [[code, ex_date, dps, PLACEHOLDER_REASON] for code, ex_date, dps in rows],
        )


def _seed_dividends(
    store: DuckDBStore, rows: list[tuple[str, str, float | None]]
) -> None:
    with store.write_connection() as conn:
        conn.executemany(
            """INSERT INTO dividends (stock_code, ex_date, dividend_per_share)
               VALUES (?, CAST(? AS DATE), ?)""",
            [[code, ex_date, dps] for code, ex_date, dps in rows],
        )


def _plan(
    store: DuckDBStore,
    events: list[dict[str, object]],
    coverage: dict[str, str],
    **kwargs: object,
) -> dict:
    return apply_mod.plan_apply(
        events=events,
        existing=apply_mod.load_existing_dividends(store),
        quarantine=apply_mod.load_quarantine(store),
        coverage=coverage,
        **kwargs,
    )


def _count(store: DuckDBStore, table: str) -> int:
    return int(store.read_query(f"SELECT count(*) AS n FROM {table}")[0]["n"])


def _quarantine_rows(store: DuckDBStore) -> list[dict]:
    return store.read_query(
        "SELECT stock_code, ex_date, dividend_per_share, quarantine_reason"
        " FROM dividends_quarantine ORDER BY stock_code, ex_date"
    )


def _dividend_rows(store: DuckDBStore) -> list[dict]:
    return store.read_query(
        "SELECT stock_code, ex_date, announcement_date, dividend_per_share,"
        " stock_dividend, transfer_share FROM dividends ORDER BY stock_code, ex_date"
    )


# --------------------------------------------------------------------------- #
# 1. 单位换算：每 10 股 → 每股
# --------------------------------------------------------------------------- #
def test_per_10_to_per_share_divides_by_ten() -> None:
    # 巨潮「派息比例」是每 10 股金额：600519 2001 年报 10转1派6元 → 每股 0.6
    assert apply_mod.per_10_to_per_share(6.0) == pytest.approx(0.6)
    assert apply_mod.per_10_to_per_share(0.02639) == pytest.approx(0.002639)  # 900922
    assert apply_mod.per_10_to_per_share(1.7) == pytest.approx(0.17)  # 600713
    assert apply_mod.per_10_to_per_share("1.7") == pytest.approx(0.17)
    assert apply_mod.per_10_to_per_share(0.0) == 0.0
    assert apply_mod.per_10_to_per_share(None) is None
    assert apply_mod.per_10_to_per_share("") is None


def test_merge_events_applies_unit_conversion_when_building_dividend_rows() -> None:
    events = [
        _event(
            "600519",
            "2002-07-25",
            cash_per_10=6.0,  # 每 10 股派 6 元
            transfer_per_10=1.0,  # 每 10 股转增 1 股
            report_period="2001-12-31",
        )
    ]
    merged, stats = apply_mod.merge_events(events)
    row = merged[("600519", date(2002, 7, 25))]
    assert row["dividend_per_share"] == pytest.approx(0.6)  # 每股派息
    assert row["transfer_share"] == pytest.approx(0.1)  # 每股转增
    assert row["stock_dividend"] == 0.0  # 无送股 → 0，不是 NULL
    assert stats["rows"] == 1


def test_merge_events_sums_two_real_dividends_on_same_ex_date() -> None:
    """同一股票同一天两笔不同分红 → 金额求和；完全重复行 → 丢弃。"""
    events = [
        _event("600713", "2025-06-30", cash_per_10=1.7, report_period="2024-12-31"),
        _event("600713", "2025-06-30", cash_per_10=1.7, report_period="2024-12-31"),  # 源侧重复
        _event("600713", "2025-06-30", cash_per_10=0.9, report_period="2024-06-30"),  # 同日第二笔
    ]
    merged, stats = apply_mod.merge_events(events)
    assert stats["rows"] == 3
    assert stats["duplicate_rows_dropped"] == 1
    assert stats["merged_same_day_groups"] == 1
    assert stats["merged_same_day_rows"] == 2
    row = merged[("600713", date(2025, 6, 30))]
    assert row["dividend_per_share"] == pytest.approx((1.7 + 0.9) / 10)
    assert row["report_periods"] == [date(2024, 6, 30), date(2024, 12, 31)]


# --------------------------------------------------------------------------- #
# 2. 巨潮列名映射 + 缺失降级
# --------------------------------------------------------------------------- #
def test_cninfo_column_mapping_and_missing_field_degradation() -> None:
    records = [
        {
            # 完整行：财报口径 → 内部字段
            "实施方案公告日期": date(2026, 6, 23),
            "分红类型": "年度分红",
            "送股比例": None,
            "转增比例": 1.0,
            "派息比例": 0.18,
            "股权登记日": date(2026, 7, 2),
            "除权日": date(2026, 6, 30),
            "派息日": date(2026, 7, 10),
            "报告时间": "2025年报",
        },
        {
            # 缺「除权日」→ 无法定位事件，丢弃（不能拿股权登记日顶替）
            "实施方案公告日期": date(2020, 5, 1),
            "派息比例": 1.0,
            "股权登记日": date(2020, 5, 6),
            "除权日": None,
            "派息日": date(2020, 5, 8),
            "报告时间": "2019年报",
        },
        {
            # 缺「股权登记日 / 派息日 / 报告时间」→ 降级为 None，事件保留
            "实施方案公告日期": None,
            "送股比例": 3.0,
            "派息比例": 1.7,
            "股权登记日": None,
            "除权日": "2025-06-30",  # 源里也可能是字符串
            "派息日": None,
            "报告时间": None,
        },
    ]
    events, stats = fetch_mod.map_cninfo_records("600713", records, fetched_at=FETCHED_AT)

    assert stats["rows"] == 3
    assert stats["skipped_no_ex_date"] == 1
    assert stats["unmapped_report_period"] == 0
    assert len(events) == 2

    full = events[0]
    assert full["stock_code"] == "600713"
    assert full["ex_date"] == "2026-06-30"
    assert full["record_date"] == "2026-07-02"
    assert full["pay_date"] == "2026-07-10"
    assert full["announcement_date"] == "2026-06-23"
    assert full["report_period"] == "2025-12-31"  # 「2025年报」→ 期末日
    assert full["report_period_raw"] == "2025年报"
    assert full["dividend_type"] == "年度分红"
    assert full["cash_per_10"] == 0.18  # 仍是每 10 股，未在此处 ÷10
    assert full["stock_per_10"] is None
    assert full["transfer_per_10"] == 1.0
    assert full["source"] == "cninfo"
    assert full["fetched_at"] == FETCHED_AT

    degraded = events[1]
    assert degraded["ex_date"] == "2025-06-30"
    assert degraded["record_date"] is None
    assert degraded["pay_date"] is None
    assert degraded["announcement_date"] is None
    assert degraded["report_period"] is None
    assert degraded["report_period_raw"] is None
    assert degraded["cash_per_10"] == 1.7
    assert degraded["stock_per_10"] == 3.0


def test_cninfo_report_period_normalization_covers_report_types() -> None:
    cases = {
        "2025年报": date(2025, 12, 31),
        "2024年度报告": date(2024, 12, 31),
        "2024半年报": date(2024, 6, 30),
        "2025年半年度报告": date(2025, 6, 30),
        "2025三季报": date(2025, 9, 30),
        "2025一季报": date(2025, 3, 31),
        "2025-09-30": date(2025, 9, 30),  # 日期型也能吃
    }
    for raw, expected in cases.items():
        period, unmapped = fetch_mod.normalize_report_period(raw)
        assert (period, unmapped) == (expected, False), raw
    # 认不出的写法必须显式标记 unmapped（而不是静默当成缺失）
    assert fetch_mod.normalize_report_period("特别分红") == (None, True)
    assert fetch_mod.normalize_report_period(None) == (None, False)


# --------------------------------------------------------------------------- #
# 3. 东财备用源映射
# --------------------------------------------------------------------------- #
def test_eastmoney_column_mapping_and_announcement_fallback() -> None:
    records = [
        {
            "报告期": "2001-12-31",
            "业绩披露日期": "2002-07-18",
            "送转股份-送股比例": 0.0,
            "送转股份-转股比例": 1.0,
            "现金分红-现金分红比例": 6.0,
            "股权登记日": "2002-07-24",
            "除权除息日": "2002-07-25",
            "方案进度": "实施分配",
            "预案公告日": None,
            "最新公告日期": "2002-07-18",
        },
        {
            # 最新公告日期缺失 → 回落到预案公告日
            "报告期": "2002-12-31",
            "现金分红-现金分红比例": 2.0,
            "股权登记日": "2003-07-11",
            "除权除息日": "2003-07-14",
            "方案进度": "实施分配",
            "预案公告日": "2003-07-08",
            "最新公告日期": None,
        },
    ]
    events, stats = fetch_mod.map_eastmoney_records("600519", records, fetched_at=FETCHED_AT)

    assert stats["skipped_no_ex_date"] == 0
    assert len(events) == 2
    first = events[0]
    assert first["ex_date"] == "2002-07-25"  # 除权除息日
    assert first["cash_per_10"] == 6.0  # 现金分红-现金分红比例（每 10 股）
    assert first["report_period"] == "2001-12-31"
    assert first["announcement_date"] == "2002-07-18"
    assert first["source"] == "eastmoney"
    assert events[1]["announcement_date"] == "2003-07-08"  # 预案公告日兜底


def test_fetch_stock_records_status_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抓取失败只记状态（apply 靠它判 unavailable），绝不抛穿整个批量。"""

    def boom(stock_code: str, source: str) -> None:
        raise TimeoutError("源没回应")

    monkeypatch.setattr(fetch_mod, "fetch_frame", boom)
    events, coverage = fetch_mod.fetch_stock("600519", "cninfo")
    assert events == []
    assert coverage["status"] == "failed"
    assert "TimeoutError" in coverage["error"]

    # 巨潮对"没有分红记录"的股票返回空 payload，akshare 取列时抛 KeyError
    def empty_payload(stock_code: str, source: str) -> None:
        raise KeyError("实施方案公告日期")

    monkeypatch.setattr(fetch_mod, "fetch_frame", empty_payload)
    events, coverage = fetch_mod.fetch_stock("688981", "cninfo")
    assert events == []
    assert coverage["status"] == "empty"
    assert coverage["error"] is None


# --------------------------------------------------------------------------- #
# 4. apply 四类判定
# --------------------------------------------------------------------------- #
def test_apply_matched_moves_quarantine_row_into_dividends(store: DuckDBStore) -> None:
    """真实除权日被误判隔离：源里同日期且金额对得上 → 移出隔离。"""
    _seed_quarantine(store, [("600713", "2025-06-30", 0.17)])
    events = [_event("600713", "2025-06-30", cash_per_10=1.7, report_period="2024-12-31")]

    plan = _plan(store, events, {"600713": "ok"})
    counts = plan["counts"]
    assert counts["matched"] == 1
    assert counts["dividend_inserted"] == 1
    assert counts["deleted_fake"] == 0
    assert counts["deleted_empty"] == 0
    assert counts["unavailable"] == 0
    assert counts["quarantine_reconciles"] is True

    apply_mod.execute_plan(store, plan)

    rows = _dividend_rows(store)
    assert [(r["stock_code"], r["ex_date"]) for r in rows] == [("600713", date(2025, 6, 30))]
    assert rows[0]["dividend_per_share"] == pytest.approx(0.17)
    assert _count(store, "dividends_quarantine") == 0


def test_apply_deleted_fake_removes_placeholder_date(store: DuckDBStore) -> None:
    """占位日是期末而非除权日：源里没有该日期，但报告期对得上 → 判定假占位并删除。"""
    _seed_quarantine(store, [("300339", "2025-12-31", 0.07)])
    events = [
        # 2025 年报的真除权日在 2026-05-29（报告期 2025-12-31 = 占位日）
        _event("300339", "2026-05-29", cash_per_10=0.7, report_period="2025-12-31"),
    ]

    plan = _plan(store, events, {"300339": "ok"}, listed_stocks={"300339"})
    counts = plan["counts"]
    assert counts["deleted_fake"] == 1
    assert counts["deleted_fake_period_matched"] == 1
    assert counts["deleted_fake_period_missing"] == 0
    assert counts["matched"] == 0
    assert counts["deleted_empty"] == 0
    assert counts["unavailable"] == 0

    apply_mod.execute_plan(store, plan)
    assert _count(store, "dividends_quarantine") == 0
    # 真除权日那一行仍然按源写入 dividends
    assert [r["ex_date"] for r in _dividend_rows(store)] == [date(2026, 5, 29)]


def test_apply_deleted_empty_removes_rows_when_source_has_no_dividend(
    store: DuckDBStore,
) -> None:
    """源明确返回"该股没有分红记录" → 隔离行是凭空来的，删除。"""
    _seed_quarantine(store, [("688981", "2024-12-31", 0.1)])

    plan = _plan(store, [], {"688981": "empty"}, listed_stocks={"688981"})
    counts = plan["counts"]
    assert counts["deleted_empty"] == 1
    assert counts["matched"] == 0
    assert counts["deleted_fake"] == 0
    assert counts["unavailable"] == 0

    apply_mod.execute_plan(store, plan)
    assert _count(store, "dividends_quarantine") == 0
    assert _count(store, "dividends") == 0


def test_apply_unavailable_keeps_row_and_relabels_reason(store: DuckDBStore) -> None:
    """源没回应 → 一律不删，只把 reason 改成 unavailable_from_source。"""
    _seed_quarantine(store, [("600001", "2024-12-31", 0.1)])

    plan = _plan(store, [], {"600001": "failed"})
    counts = plan["counts"]
    assert counts["unavailable"] == 1
    assert counts["unavailable_total"] == 1
    assert counts["matched"] == 0
    assert counts["deleted_fake"] == 0
    assert counts["deleted_empty"] == 0

    apply_mod.execute_plan(store, plan)
    rows = _quarantine_rows(store)
    assert len(rows) == 1
    assert rows[0]["quarantine_reason"] == "unavailable_from_source"
    assert _count(store, "dividends") == 0


def test_apply_keeps_delisted_rows_when_source_returns_nothing(store: DuckDBStore) -> None:
    """退市股在巨潮/东财查不到历史（实测 600087 有 12 行真实派息、源返回 0 行）

    → 源「空返回」不能当成「真不分红」，非在市股票一律保留。
    """
    _seed_quarantine(store, [("600087", "1997-12-31", 0.35), ("600087", "1999-12-31", 0.5)])

    # 600087 不在 listed_stocks 里
    plan = _plan(store, [], {"600087": "empty"}, listed_stocks={"600519"})
    counts = plan["counts"]
    assert counts["deleted_empty"] == 0
    assert counts["kept_coverage_gap"] == 2
    assert counts["kept_coverage_gap_empty"] == 2

    apply_mod.execute_plan(store, plan)
    rows = _quarantine_rows(store)
    assert len(rows) == 2
    assert {r["quarantine_reason"] for r in rows} == {"source_coverage_gap"}
    assert _count(store, "dividends") == 0


def test_apply_keeps_rows_of_stocks_that_were_not_fetched(store: DuckDBStore) -> None:
    """本次没抓到的股票：既不能删也不能标 unavailable（安全阀）。"""
    _seed_quarantine(store, [("600519", "2025-12-31", 1.0)])
    plan = _plan(store, [], {})
    counts = plan["counts"]
    assert counts["kept_not_fetched"] == 1
    assert counts["matched"] == 0
    assert counts["deleted_fake"] == 0
    assert counts["deleted_empty"] == 0
    assert counts["unavailable"] == 0

    apply_mod.execute_plan(store, plan)
    rows = _quarantine_rows(store)
    assert len(rows) == 1
    assert rows[0]["quarantine_reason"] == PLACEHOLDER_REASON  # 保持原样


def test_apply_updates_only_amount_field_when_value_differs(store: DuckDBStore) -> None:
    _seed_dividends(store, [("600519", "2024-06-19", 23.0)])  # 旧值错
    events = [_event("600519", "2024-06-19", cash_per_10=238.82)]

    plan = _plan(store, events, {"600519": "ok"})
    counts = plan["counts"]
    assert counts["dividend_updated"] == 1
    assert counts["dividend_inserted"] == 0
    assert counts["dividend_unchanged"] == 0

    apply_mod.execute_plan(store, plan)
    rows = _dividend_rows(store)
    assert rows[0]["dividend_per_share"] == pytest.approx(23.882)
    assert _count(store, "dividends") == 1


def test_apply_fills_null_columns_without_overwriting_present_values(
    store: DuckDBStore,
) -> None:
    """600519 2006-05-19 实况：巨潮两行（10派3元 + 10转增10股）。

    库里派息 0.3 已经对了，但 transfer_share 为空 → 只回填空值，不动已有值。
    """
    _seed_dividends(store, [("600519", "2006-05-19", 0.3)])
    events = [
        _event("600519", "2006-05-19", cash_per_10=3.0, report_period="2005-12-31"),
        _event("600519", "2006-05-19", transfer_per_10=10.0),  # 同日 10 转增 10
    ]

    plan = _plan(store, events, {"600519": "ok"}, listed_stocks={"600519"})
    counts = plan["counts"]
    assert counts["dividend_updated"] == 0  # 金额本来就对
    assert counts["dividend_null_filled"] == 1
    assert counts["dividend_unchanged"] == 0
    assert plan["null_fills"][0]["dividend_per_share"] == pytest.approx(0.3)
    assert plan["null_fills"][0]["transfer_share"] == pytest.approx(1.0)

    apply_mod.execute_plan(store, plan)
    rows = _dividend_rows(store)
    assert len(rows) == 1
    assert rows[0]["dividend_per_share"] == pytest.approx(0.3)
    assert rows[0]["transfer_share"] == pytest.approx(1.0)


def test_apply_never_zeroes_cash_when_source_amount_is_missing(
    store: DuckDBStore,
) -> None:
    """200541 2007-06-08 实况：巨潮「派息比例」为空（说明却写着 10转增3股派5元），
    库里 0.5 是真的 → 现金口径未知时绝不能用 0 覆盖，只能补别的空字段。
    """
    with store.write_connection() as conn:
        conn.executemany(
            """INSERT INTO dividends
               (stock_code, ex_date, announcement_date, dividend_per_share,
                stock_dividend, transfer_share)
               VALUES ('200541', CAST(? AS DATE), CAST(? AS DATE), ?, ?, ?)""",
            [
                # transfer_share 为空 → 走"回填空值"通道
                ("2007-06-08", None, 0.5, None, None),
                # 字段齐全 → 只能整行跳过；派息 0.585 必须保持
                ("2008-06-06", "2008-05-30", 0.585, 0.0, 0.5),
            ],
        )
    events = [
        _event("200541", "2007-06-08", transfer_per_10=3.0, report_period="2006-12-31"),
        _event("200541", "2008-06-06", transfer_per_10=5.0, report_period="2007-12-31"),
    ]

    plan = _plan(store, events, {"200541": "ok"}, listed_stocks={"200541"})
    counts = plan["counts"]
    assert counts["dividend_updated"] == 0  # 绝不能把 0.5/0.585 改成 0
    assert counts["dividend_null_filled"] == 1  # 2007-06-08 补 transfer_share
    assert counts["skipped_cash_unknown"] == 1  # 2008-06-06 无空可补 → 跳过

    apply_mod.execute_plan(store, plan)
    rows = {r["ex_date"]: r for r in _dividend_rows(store)}
    assert rows[date(2007, 6, 8)]["dividend_per_share"] == pytest.approx(0.5)
    assert rows[date(2007, 6, 8)]["transfer_share"] == pytest.approx(0.3)
    assert rows[date(2008, 6, 6)]["dividend_per_share"] == pytest.approx(0.585)
    assert rows[date(2008, 6, 6)]["transfer_share"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# 5. 幂等：同一份 JSONL 跑两次
# --------------------------------------------------------------------------- #
def test_apply_same_jsonl_twice_is_idempotent(store: DuckDBStore, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    coverage_path = tmp_path / "events.coverage.jsonl"

    events = [
        _event("600713", "2025-06-30", cash_per_10=1.7, report_period="2024-12-31"),  # matched
        _event("300339", "2026-05-29", cash_per_10=0.7, report_period="2025-12-31"),  # fake
        _event("600519", "2024-06-19", cash_per_10=238.82),  # 更新金额
        _event("000002", "2025-07-10", cash_per_10=1.0),  # 纯插入
    ]
    events_path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    coverage = [
        {"stock_code": "600713", "status": "ok"},
        {"stock_code": "300339", "status": "ok"},
        {"stock_code": "600519", "status": "ok"},
        {"stock_code": "000002", "status": "ok"},
        {"stock_code": "688981", "status": "empty"},
        {"stock_code": "600001", "status": "failed"},
    ]
    coverage_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in coverage) + "\n",
        encoding="utf-8",
    )

    _seed_dividends(store, [("600519", "2024-06-19", 23.0)])
    _seed_quarantine(
        store,
        [
            ("600713", "2025-06-30", 0.17),  # matched
            ("300339", "2025-12-31", 0.07),  # deleted_fake（报告期命中）
            ("688981", "2024-12-31", 0.10),  # deleted_empty
            ("600001", "2024-12-31", 0.20),  # unavailable
        ],
    )

    def run_plan() -> dict:
        return apply_mod.plan_apply(
            events=apply_mod.load_events(events_path),
            existing=apply_mod.load_existing_dividends(store),
            quarantine=apply_mod.load_quarantine(store),
            coverage=apply_mod.load_coverage(coverage_path),
            listed_stocks={"600713", "300339", "600519", "000002", "688981", "600001"},
        )

    first = run_plan()
    assert first["counts"]["matched"] == 1
    assert first["counts"]["deleted_fake"] == 1
    assert first["counts"]["deleted_empty"] == 1
    assert first["counts"]["unavailable"] == 1
    assert first["counts"]["dividend_inserted"] == 3
    assert first["counts"]["dividend_updated"] == 1
    apply_mod.execute_plan(store, first)

    dividends_before = _count(store, "dividends")
    quarantine_before = _count(store, "dividends_quarantine")
    assert dividends_before == 4
    assert quarantine_before == 1  # 只剩 unavailable 那一行

    second = run_plan()
    counts = second["counts"]
    assert counts["matched"] == 0
    assert counts["deleted_fake"] == 0
    assert counts["deleted_empty"] == 0
    assert counts["unavailable"] == 0
    assert counts["dividend_inserted"] == 0
    assert counts["dividend_updated"] == 0
    assert counts["dividend_null_filled"] == 0

    apply_mod.execute_plan(store, second)
    assert _count(store, "dividends") == dividends_before
    assert _count(store, "dividends_quarantine") == quarantine_before
    assert _quarantine_rows(store)[0]["quarantine_reason"] == "unavailable_from_source"


# --------------------------------------------------------------------------- #
# 6. CLI 串起来（dry-run 只读 + --yes 备份后单事务写）
# --------------------------------------------------------------------------- #
def test_cli_dry_run_then_write_with_backup(
    store: DuckDBStore,
    database_paths: DatabasePathSet,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events_path = tmp_path / "cli-events.jsonl"
    events_path.write_text(
        json.dumps(_event("600713", "2025-06-30", cash_per_10=1.7), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    fetch_mod.coverage_path_for(events_path).write_text(
        json.dumps({"stock_code": "600713", "status": "ok"}) + "\n", encoding="utf-8"
    )
    _seed_quarantine(store, [("600713", "2025-06-30", 0.17)])

    monkeypatch.setattr(apply_mod, "resolve_and_validate_paths", lambda: database_paths)
    backup_calls: list[list[str]] = []

    def fake_backup(duck: DuckDBStore, tables: list[str], tag: str) -> Path:
        backup_calls.append(list(tables))
        target = tmp_path / f"backup-{tag}"
        target.mkdir(exist_ok=True)
        return target

    monkeypatch.setattr("scripts._maintenance_safety.backup_tables", fake_backup)

    # dry-run：不写库
    assert apply_mod.main(["--in", str(events_path)]) == 0
    dry_output = capsys.readouterr().out
    assert "只读 dry-run" in dry_output
    assert _count(store, "dividends") == 0
    assert _count(store, "dividends_quarantine") == 1
    assert backup_calls == []

    # --yes：备份 → 单事务写
    assert apply_mod.main(["--in", str(events_path), "--yes"]) == 0
    assert backup_calls == [["dividends", "dividends_quarantine"]]
    assert [r["ex_date"] for r in _dividend_rows(store)] == [date(2025, 6, 30)]
    assert _count(store, "dividends_quarantine") == 0

    # 再跑一次：幂等，什么都不写（备份也不做）
    assert apply_mod.main(["--in", str(events_path), "--yes"]) == 0
    assert len(backup_calls) == 1
