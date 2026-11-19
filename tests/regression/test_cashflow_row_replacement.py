"""现金流量补充域「替换式落库」纪律（2026-11-17，reports/135 事故回归）。

事故
----
FY2025 现金流量补充域回填时，`_persist_rows` 在 `replace_source_rows` 模式下
**先按报告期删、再插入"至少一个非空值"的行**。某一期返回全空/缺值时，旧行
已经被删掉、却没有任何新行补回 —— 2025-06-30 的 4 只股票
（000016 / 301139 / 601059 / 601198）的真实数据就是这样丢失的。

本文件钉死五件事
----------------
1. 旧行有值 + 新批次同 (stock, report_date) 返回**全空** → 旧行必须仍在；
2. 旧行有值 + 新批次返回**部分非空** → 旧行被替换、新值生效；
3. 上面两条对 `delete_scope='all'` 与 `'batch_periods'` **两种模式**都成立；
4. 恢复脚本：临时库删掉 2 行 → dry-run 一行不写 → `--yes` 插回 → 再跑 0 行；
5. 结构性缺失登记：`cash_flow_indirect` / `cash_flow_activity` 的
   `CASH_FLOW_STRUCTURAL_MISSING` 列按 `report_date` 写进
   `indicator_null_reason`（`SOURCE_EMPTY`），`other_cash_effect` 的语义写 detail。

全部使用临时 DuckDB/SQLite 夹具，网络一律 monkeypatch，**绝不触碰正式库**。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from app.core.quality import null_reason_sweeper as sweeper
from app.core.supplement import (
    ACTIVITY_COLUMNS,
    ACTIVITY_VALUE_COLUMNS,
    CASHFLOW_SUPPLEMENT,
    INDIRECT_COLUMNS,
    INDIRECT_VALUE_COLUMNS,
    SPECS,
    SupplementFetcher,
)
from tests.support.seed import Seed

# `stock_meta` 与主链三表（`_seed_stock` / `_seed_trio`）仍内联：这几张表在本文件
# 各只出现一次，没有重复样板可收敛。

FETCH_TIME = datetime(2026, 11, 17, 12, 0, 0)
LATEST = "2026-09-30"
INCIDENT_PERIOD = "2025-06-30"

#: 一只制造业股票的东财 `xjllbAjaxNew` 行（只需要两条映射：净利润 + 资本支出）
_EM_ROW_MIN = {
    "REPORT_DATE": f"{LATEST} 00:00:00",
    "ORG_TYPE": "通用",
    "NETPROFIT": 85_310_324_833.67,
    "CONSTRUCT_LONG_ASSET": 3_127_594_916.41,
}
#: 同一接口返回"该期存在、但一个数值都没有"的行（正是事故形态）
_EM_ROW_EMPTY = {"REPORT_DATE": f"{INCIDENT_PERIOD} 00:00:00", "ORG_TYPE": "通用"}

_OLD_INDIRECT_NET_PROFIT = 777_000_000.0
_OLD_ACTIVITY_CAPEX = 888_000_000.0


# ─── 夹具 ─────────────────────────────────────────────────────────────


def _null_reason_rows(duck, field_name: str, report_date: str | None = None) -> list[dict]:
    """稀疏表里的空值原因明细（生产侧只保留聚合读入口 `reason_distribution`）。

    这些用例验的是**清扫器写进库里的内容**，所以直读表而不是走查询层。
    """
    sql = (
        "SELECT stock_code, report_date, field_name, reason_code, detail "
        "FROM indicator_null_reason WHERE field_name = ?"
    )
    params: list[object] = [field_name]
    if report_date is not None:
        sql += " AND report_date = ?"
        params.append(report_date)
    return duck.read_query(sql + " ORDER BY stock_code", params)


def _seed_stock(duck, code: str = "600519", name: str = "贵州茅台") -> None:
    duck.write_query(
        """INSERT INTO stock_meta
               (stock_code, name, exchange, listing_date, is_listed, is_st,
                is_suspended, sw_level1, sw_level2, csrc_l1, total_shares, circ_shares)
           VALUES (?, ?, 'SSE', '2010-01-05', TRUE, FALSE, FALSE,
                   '食品饮料', '白酒', '制造业', 100, 80)""",
        [code, name],
    )


def _seed_trio(duck, code: str = "600519", report_date: str = INCIDENT_PERIOD) -> None:
    """主链三表各一行（`pending_codes` 的股票池要 JOIN income_statement）。"""
    duck.write_query(
        """INSERT INTO income_statement (stock_code, report_date, report_type, revenue)
           VALUES (?, CAST(? AS DATE), 'semi_annual', 1000)""",
        [code, report_date],
    )
    duck.write_query(
        """INSERT INTO balance_sheet (stock_code, report_date, total_assets,
                                     total_liabilities, total_equity)
           VALUES (?, CAST(? AS DATE), 5000, 2000, 3000)""",
        [code, report_date],
    )
    duck.write_query(
        """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
           VALUES (?, CAST(? AS DATE), 100)""",
        [code, report_date],
    )


def _seed_old_source_rows(duck, code: str = "600519") -> None:
    """上一轮写入的本源行（两张表各一行，都有真实值）。

    provenance 五件套逐字写死成"上一批"（`oldhash`/`oldbatch`）：`replace_source_rows`
    的删旧/写新判断就看 `source` 与批号，这与工厂的默认值**不是**同一件事。
    """
    seed = Seed(duck)
    seed.cash_indirect(
        code, INCIDENT_PERIOD, report_type="semi_annual", net_profit=_OLD_INDIRECT_NET_PROFIT,
        source="eastmoney_f10", fetch_time="2026-06-01 00:00:00",
        raw_response_hash="oldhash", confidence="strict", batch_id="oldbatch",
    )
    seed.cash_activity(
        code, INCIDENT_PERIOD, report_type="semi_annual", capex=_OLD_ACTIVITY_CAPEX,
        source="eastmoney_f10", fetch_time="2026-06-01 00:00:00",
        raw_response_hash="oldhash", confidence="strict", batch_id="oldbatch",
    )


def _row_for(duck, table: str, code: str, report_date: str) -> dict | None:
    rows = duck.read_query(
        f"SELECT * FROM {table} WHERE stock_code = ? AND report_date = CAST(? AS DATE)",
        [code, report_date],
    )
    return rows[0] if rows else None


def _use_fetch_rows(monkeypatch, rows: list[dict] | dict[str, list[dict]]) -> None:
    """monkeypatch 取数层：`rows` 可以是全股票共用的一份，也可以按股票给。"""

    def fake_fetch(self, spec, code, exchange, dates, **kwargs):
        selected = rows[code] if isinstance(rows, dict) else rows
        return [dict(row) for row in selected]

    monkeypatch.setattr(SupplementFetcher, "_fetch_rows", fake_fetch)


def _use_delete_scope(monkeypatch, scope: str) -> None:
    """把 spec 换成指定 delete_scope（refresh 每轮从 SPECS 读取）。"""
    monkeypatch.setitem(
        SPECS, CASHFLOW_SUPPLEMENT,
        replace(SPECS[CASHFLOW_SUPPLEMENT], delete_scope=scope),
    )


# ─── A1/A2：空值保护（两种 delete_scope 各一条）──────────────────────


def test_empty_fetch_keeps_old_rows_delete_scope_all(
    duckdb_store, monkeypatch,
) -> None:
    """事故复现（all 模式）：同股同期返回全空 → 旧的两行真实数据必须仍在。"""
    _seed_stock(duckdb_store)
    _seed_trio(duckdb_store)
    _seed_old_source_rows(duckdb_store)
    _use_delete_scope(monkeypatch, "all")
    # 同一只股票：事故期返回全空 + 新期有值。旧实现按"股票"整删，事故期一起没。
    _use_fetch_rows(monkeypatch, [_EM_ROW_MIN, _EM_ROW_EMPTY])

    report = SupplementFetcher(duck=duckdb_store).refresh(
        CASHFLOW_SUPPLEMENT, periods=[LATEST], deadline_seconds=30,
    )

    indirect = _row_for(duckdb_store, "cash_flow_indirect", "600519", INCIDENT_PERIOD)
    activity = _row_for(duckdb_store, "cash_flow_activity", "600519", INCIDENT_PERIOD)
    assert indirect is not None, "返回全空时旧行被删掉了 —— 正是 2025-06-30 事故"
    assert indirect["net_profit"] == pytest.approx(_OLD_INDIRECT_NET_PROFIT)
    assert indirect["batch_id"] == "oldbatch", "旧行必须原样保留（含历史批次）"
    assert activity is not None
    assert activity["capex"] == pytest.approx(_OLD_ACTIVITY_CAPEX)
    # 有新数据的那一期正常写入
    new_activity = _row_for(duckdb_store, "cash_flow_activity", "600519", LATEST)
    assert new_activity["capex"] == pytest.approx(3_127_594_916.41)
    assert report["skipped_empty_rows"] == 2, report
    assert report["rows_written"] == {
        "cash_flow_indirect": 1, "cash_flow_activity": 1,
    }, report


def test_empty_fetch_keeps_old_rows_delete_scope_batch_periods(
    duckdb_store, monkeypatch,
) -> None:
    """事故复现（batch_periods 模式）：同批跨期时，空期旧行不能被"股票×期次"误删。

    旧 SQL 是 `stock_code IN (batch) AND report_date IN (batch)` 的笛卡尔组合：
    只要该股在**另一期**有新数据，空期的旧行也会被删。本用例同时返回
    `2025-06-30`（全空）与 `2026-09-30`（有值），旧实现必然删掉前者。
    """
    _seed_stock(duckdb_store)
    _seed_trio(duckdb_store)
    _seed_old_source_rows(duckdb_store)
    # 同批第二只股票在事故期有值 —— 它的存在把 2025-06-30 塞进批次的
    # report_date 集合，旧 SQL 的"股票集合 × 期次集合"就会误删 600519 的旧行。
    _seed_stock(duckdb_store, "000016", "深康佳A")
    _seed_trio(duckdb_store, "000016", LATEST)
    _use_delete_scope(monkeypatch, "batch_periods")
    _use_fetch_rows(monkeypatch, {
        "600519": [_EM_ROW_MIN, _EM_ROW_EMPTY],
        "000016": [{**_EM_ROW_MIN, "REPORT_DATE": f"{INCIDENT_PERIOD} 00:00:00"}],
    })

    report = SupplementFetcher(duck=duckdb_store).refresh(
        CASHFLOW_SUPPLEMENT, periods=[LATEST], deadline_seconds=30,
    )

    assert report["status"] == "success", report
    # 空期旧行：必须仍在且值未变
    indirect = _row_for(duckdb_store, "cash_flow_indirect", "600519", INCIDENT_PERIOD)
    activity = _row_for(duckdb_store, "cash_flow_activity", "600519", INCIDENT_PERIOD)
    assert indirect is not None, "空期旧行被同批另一期的数据误删 —— 事故根因"
    assert indirect["net_profit"] == pytest.approx(_OLD_INDIRECT_NET_PROFIT)
    assert activity is not None
    assert activity["capex"] == pytest.approx(_OLD_ACTIVITY_CAPEX)
    # 有值的新期正常写入
    new_indirect = _row_for(duckdb_store, "cash_flow_indirect", "600519", LATEST)
    new_activity = _row_for(duckdb_store, "cash_flow_activity", "600519", LATEST)
    assert new_indirect["net_profit"] == pytest.approx(85_310_324_833.67)
    assert new_activity["capex"] == pytest.approx(3_127_594_916.41)
    # 第二只股票事故期的行也正常写入（它证明期次集合里确实有 2025-06-30）
    witness = _row_for(duckdb_store, "cash_flow_activity", "000016", INCIDENT_PERIOD)
    assert witness["capex"] == pytest.approx(3_127_594_916.41)
    assert report["rows_written"] == {
        "cash_flow_indirect": 2, "cash_flow_activity": 2,
    }, report
    assert report["skipped_empty_rows"] == 2, report


# ─── A1：部分非空 → 旧行被替换（两种 delete_scope）────────────────────


@pytest.mark.parametrize("scope", ["all", "batch_periods"])
def test_non_empty_fetch_replaces_old_row(
    duckdb_store, monkeypatch, scope: str,
) -> None:
    """同股同期返回部分非空 → 旧行被整行替换、新值生效。"""
    _seed_stock(duckdb_store)
    _seed_trio(duckdb_store)
    _seed_old_source_rows(duckdb_store)
    _use_delete_scope(monkeypatch, scope)
    _use_fetch_rows(monkeypatch, [
        {**_EM_ROW_MIN, "REPORT_DATE": f"{INCIDENT_PERIOD} 00:00:00"},
    ])

    report = SupplementFetcher(duck=duckdb_store).refresh(
        CASHFLOW_SUPPLEMENT, periods=[LATEST], deadline_seconds=30,
    )

    assert report["status"] == "success", report
    assert report["skipped_empty_rows"] == 0, report
    assert report["rows_written"] == {
        "cash_flow_indirect": 1, "cash_flow_activity": 1,
    }, report
    indirect = _row_for(duckdb_store, "cash_flow_indirect", "600519", INCIDENT_PERIOD)
    activity = _row_for(duckdb_store, "cash_flow_activity", "600519", INCIDENT_PERIOD)
    assert indirect["net_profit"] == pytest.approx(85_310_324_833.67)
    assert indirect["batch_id"] != "oldbatch", "旧行必须被整行替换（不能留旧批次）"
    assert activity["capex"] == pytest.approx(3_127_594_916.41)
    assert activity["batch_id"] != "oldbatch"
    # 主键唯一：一张表同 (stock, report_date) 只能有一行
    for table in ("cash_flow_indirect", "cash_flow_activity"):
        count = duckdb_store.read_query(
            f"SELECT COUNT(*) AS n FROM {table} WHERE stock_code = '600519'",
        )[0]["n"]
        assert count == 1, f"{table} 出现重复行：{count}"


# ─── B：恢复脚本（临时库，不碰正式库）────────────────────────────────


def _write_backup_parquet(backup_dir: Path) -> dict[str, pd.DataFrame]:
    """按正式表列集合造一份小备份（列名与真实备份完全一致）。"""
    codes = ("000016", "002731", "301139")
    indirect_rows = []
    activity_rows = []
    for index, code in enumerate(codes):
        base = {
            "stock_code": code,
            "report_date": pd.Timestamp(INCIDENT_PERIOD),
            "report_type": "semi_annual",
            "source": "eastmoney_f10",
            "fetch_time": pd.Timestamp("2026-05-01 00:00:00"),
            "raw_response_hash": "b33c040776273ef83893fc2ec99644a1",
            "confidence": "strict",
            "batch_id": "627a22a4243048aabfcb23fedb1b950c",
        }
        indirect = {column: None for column in INDIRECT_COLUMNS}
        indirect.update(base)
        indirect["cf_from_operating_indirect"] = -100_000_000.0 * (index + 1)
        indirect["fixed_asset_depreciation"] = 10_000_000.0 * (index + 1)
        indirect["raw_data"] = "{}"
        indirect_rows.append(indirect)
        activity = {column: None for column in ACTIVITY_COLUMNS}
        activity.update(base)
        activity["capex"] = 200_000_000.0 * (index + 1)
        activity_rows.append(activity)
    indirect_frame = pd.DataFrame(indirect_rows, columns=list(INDIRECT_COLUMNS))
    activity_frame = pd.DataFrame(activity_rows, columns=list(ACTIVITY_COLUMNS))
    backup_dir.mkdir(parents=True, exist_ok=True)
    indirect_frame.to_parquet(backup_dir / "cash_flow_indirect.parquet", index=False)
    activity_frame.to_parquet(backup_dir / "cash_flow_activity.parquet", index=False)
    return {"cash_flow_indirect": indirect_frame, "cash_flow_activity": activity_frame}


def _count(duck, table: str) -> int:
    return int(duck.read_query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])


def test_restore_script_dry_run_then_yes_then_idempotent(
    duckdb_store, database_paths, tmp_path,
) -> None:
    """删 2 行 → dry-run 不写 → `--yes` 插回 2 行 → 再跑 0 行。"""
    from scripts import restore_cashflow_from_backup as restore_script

    backup_dir = tmp_path / "backup"
    _write_backup_parquet(backup_dir)
    lock_path = database_paths.duckdb_path

    # 先让 3 只股票全部入库（脚本本身就是合法的灌库途径）
    seeded = restore_script.restore(
        duckdb_store, backup_dir=backup_dir, yes=True, backup=False,
        lock_path=lock_path,
    )
    assert seeded["restored_total"] == 6, seeded  # 3 行 × 2 表
    assert _count(duckdb_store, "cash_flow_indirect") == 3
    assert _count(duckdb_store, "cash_flow_activity") == 3

    # 删掉 2 只股票（每表 2 行 —— 正是事故形态）
    for table in ("cash_flow_indirect", "cash_flow_activity"):
        duckdb_store.write_query(
            f"DELETE FROM {table} WHERE stock_code IN ('000016', '301139')"
        )
    # 另把 002731 的 capex 改掉，验证"已存在但值不同"只报冲突、绝不覆盖
    duckdb_store.write_query(
        "UPDATE cash_flow_activity SET capex = 1.0 WHERE stock_code = '002731'"
    )
    before = {
        table: _count(duckdb_store, table)
        for table in ("cash_flow_indirect", "cash_flow_activity")
    }

    dry = restore_script.restore(
        duckdb_store, backup_dir=backup_dir, yes=False, backup=False,
        lock_path=lock_path,
    )
    assert dry["mode"] == "dry_run", dry
    assert dry["restored_total"] == 0
    assert dry["plan"]["totals"]["missing"] == 4, dry  # 2 只 × 2 表
    assert dry["plan"]["totals"]["conflict"] == 1, dry  # 002731 activity capex 被改
    assert {
        table: _count(duckdb_store, table)
        for table in ("cash_flow_indirect", "cash_flow_activity")
    } == before, "dry-run 绝不能写库"

    written = restore_script.restore(
        duckdb_store, backup_dir=backup_dir, yes=True, backup=False,
        lock_path=lock_path,
    )
    assert written["restored_total"] == 4, written
    assert written["restored_by_table"] == {
        "cash_flow_indirect": 2, "cash_flow_activity": 2,
    }, written
    assert _count(duckdb_store, "cash_flow_indirect") == 3
    assert _count(duckdb_store, "cash_flow_activity") == 3
    # 历史事实保留：插回的行必须带备份里的旧 batch_id / raw_response_hash
    restored = _row_for(duckdb_store, "cash_flow_activity", "000016", INCIDENT_PERIOD)
    assert restored["batch_id"] == "627a22a4243048aabfcb23fedb1b950c"
    assert restored["raw_response_hash"] == "b33c040776273ef83893fc2ec99644a1"
    assert restored["capex"] == pytest.approx(200_000_000.0)
    # 冲突行没有被覆盖
    conflicted = _row_for(duckdb_store, "cash_flow_activity", "002731", INCIDENT_PERIOD)
    assert conflicted["capex"] == pytest.approx(1.0)

    again = restore_script.restore(
        duckdb_store, backup_dir=backup_dir, yes=True, backup=False,
        lock_path=lock_path,
    )
    assert again["restored_total"] == 0, again
    assert again["plan"]["totals"]["missing"] == 0, again
    assert _count(duckdb_store, "cash_flow_indirect") == 3
    assert _count(duckdb_store, "cash_flow_activity") == 3


def test_restore_script_rejects_unknown_backup_dir(duckdb_store, tmp_path) -> None:
    """备份目录/parquet 缺失时必须立刻中止，而不是"恢复 0 行、看起来成功"。"""
    from scripts import restore_cashflow_from_backup as restore_script

    with pytest.raises(restore_script.BackupMissing):
        restore_script.restore(
            duckdb_store, backup_dir=tmp_path / "nope", yes=False, backup=False,
        )


def test_restore_script_keeps_parquet_columns_in_sync() -> None:
    """备份 parquet 的列名必须与落库列集合一致（防止备份格式漂移）。"""
    assert len(INDIRECT_VALUE_COLUMNS) == 29
    assert len(ACTIVITY_VALUE_COLUMNS) == 21
    assert set(INDIRECT_COLUMNS) >= set(INDIRECT_VALUE_COLUMNS)
    assert set(ACTIVITY_COLUMNS) >= set(ACTIVITY_VALUE_COLUMNS)


# ─── C：结构性缺失登记进 indicator_null_reason（按 report_date）────────


STRUCTURAL_INDIRECT_FIELD = "cash_flow_indirect.unconfirmed_investment_loss"
STRUCTURAL_ACTIVITY_FIELD = "cash_flow_activity.other_cash_effect"


def _seed_cashflow_supplement_rows(
    duck, code: str, report_date: str, *, net_profit: float = 1.0, capex: float = 1.0,
) -> None:
    """两张补充表各一行：有行、但结构性缺失列（如未确认投资损失）恒为 NULL。"""
    seed = Seed(duck)
    seed.cash_indirect(
        code, report_date, report_type="annual", net_profit=net_profit,
        source="eastmoney_f10", fetch_time="2026-11-17 00:00:00",
        raw_response_hash="h", confidence="strict", batch_id="b",
    )
    seed.cash_activity(
        code, report_date, report_type="annual", capex=capex,
        source="eastmoney_f10", fetch_time="2026-11-17 00:00:00",
        raw_response_hash="h", confidence="strict", batch_id="b",
    )


def test_structural_missing_indirect_registered_as_source_empty(
    duckdb_store,
) -> None:
    """`unconfirmed_investment_loss` @ 2025-12-31 必须能查到 SOURCE_EMPTY。

    审计原状：`indicator_null_reason` 只有 2026-09-30 一期、且**没有任何**
    `cash_flow*` 字段名 —— FY2025 的这类 NULL 是"无声 NULL"。
    """
    report_date = "2025-12-31"
    for code in ("000016", "002731"):
        _seed_cashflow_supplement_rows(duckdb_store, code, report_date)

    result = sweeper.sweep(
        duckdb_store, domain="cash_flow_indirect", report_date=report_date, write=True,
    )
    assert result.report_date == date(2025, 12, 31)
    assert result.by_field[STRUCTURAL_INDIRECT_FIELD] == {
        "SOURCE_EMPTY": 2,
    }, result.by_field

    rows = _null_reason_rows(duckdb_store, STRUCTURAL_INDIRECT_FIELD, report_date)
    assert len(rows) == 2, rows
    assert {row["reason_code"] for row in rows} == {"SOURCE_EMPTY"}
    assert {str(row["report_date"])[:10] for row in rows} == {report_date}
    assert all("结构性缺失" in (row["detail"] or "") for row in rows)


def test_other_cash_effect_reason_is_semantic_mismatch_not_source_missing(
    duckdb_store,
) -> None:
    """`other_cash_effect` 的真因是"口径不符·主动不映射"，detail 必须说清。

    `NullReason` 枚举没有 SEMANTIC_MISMATCH / NOT_MAPPED，且新增取值会破坏既有
    契约 —— 所以原因码仍是 SOURCE_EMPTY，语义写进 detail 交主控裁决。
    """
    report_date = "2025-12-31"
    for code in ("000016", "002731"):
        _seed_cashflow_supplement_rows(duckdb_store, code, report_date)

    result = sweeper.sweep(
        duckdb_store, domain="cash_flow_activity", report_date=report_date, write=True,
    )
    assert result.by_field[STRUCTURAL_ACTIVITY_FIELD] == {"SOURCE_EMPTY": 2}

    rows = _null_reason_rows(duckdb_store, STRUCTURAL_ACTIVITY_FIELD, report_date)
    assert rows
    for row in rows:
        detail = row["detail"] or ""
        assert "主动不映射" in detail
        assert "RATE_CHANGE_EFFECT" in detail
        assert "SEMANTIC_MISMATCH" in detail or "NOT_MAPPED" in detail


def test_structural_missing_is_stored_per_report_date(duckdb_store) -> None:
    """按 report_date 存：2025-06-30 与 2025-12-31 各自有记录，不是只存最新一期。"""
    periods = ("2025-06-30", "2025-12-31")
    for report_date in periods:
        _seed_cashflow_supplement_rows(duckdb_store, "000016", report_date)
        sweeper.sweep(
            duckdb_store,
            domain="cash_flow_indirect",
            report_date=report_date,
            write=True,
        )

    rows = _null_reason_rows(duckdb_store, STRUCTURAL_INDIRECT_FIELD)
    assert {str(row["report_date"])[:10] for row in rows} == set(periods), rows
    assert len(rows) == len(periods)


# ─── A3：部分期次补采不得删掉其他期次（reports/147 事故）──────────────

OTHER_ANNUAL = "2025-12-31"
OTHER_SEMI = "2026-06-30"


def _seed_other_periods(duck, code: str = "600519") -> None:
    """同源、同股票、**非本轮请求期次**的旧真实行（年报 + 半年报各一行）。"""
    seed = Seed(duck)
    for report_date, report_type, net_profit in (
        (OTHER_ANNUAL, "annual", 11_000_000_000.0),
        (OTHER_SEMI, "semi_annual", 12_000_000_000.0),
    ):
        seed.cash_indirect(
            code, report_date, report_type=report_type, net_profit=net_profit,
            source="eastmoney_f10", fetch_time="2026-06-01 00:00:00",
            raw_response_hash="oldhash", confidence="strict", batch_id="oldbatch",
        )


def test_partial_period_backfill_keeps_other_periods(
    duckdb_store, monkeypatch,
) -> None:
    """**只补一期**不得删掉其他期次的旧行（reports/147 实测事故）。

    事故经过：为补 `2025-06-30` 的折旧摊销断档，按 `periods=["2025-06-30"]`
    调 `refresh(force_codes=上市全部 5,580 只)`。`delete_scope='all'` 的删除
    条件只按 `stock_code` 匹配，于是把同一批股票**其他期次**的同源行一起删了 ——
    一次丢掉 5,575 行 2025 年报 + 5,579 行 2026 半年报，只能全量重采救回。

    修法：`all` 模式的删除范围加 `t.report_date IN (SELECT report_date FROM _supp_rows)`，
    把"窗口外旧行"的清理限制在**本轮真正请求的期次**内。
    """
    _seed_stock(duckdb_store)
    _seed_trio(duckdb_store)
    _seed_other_periods(duckdb_store)
    _use_delete_scope(monkeypatch, "all")
    # 本轮只请求事故期（2025-06-30），返回一条有值的行
    _use_fetch_rows(monkeypatch, [
        {**_EM_ROW_MIN, "REPORT_DATE": f"{INCIDENT_PERIOD} 00:00:00"},
    ])

    report = SupplementFetcher(duck=duckdb_store).refresh(
        CASHFLOW_SUPPLEMENT, periods=[INCIDENT_PERIOD], deadline_seconds=30,
    )

    assert report["status"] == "success", report
    # 本轮期次：新值写入
    written = _row_for(duckdb_store, "cash_flow_indirect", "600519", INCIDENT_PERIOD)
    assert written is not None
    assert written["net_profit"] == pytest.approx(_EM_ROW_MIN["NETPROFIT"])
    # 非本轮期次：**必须原样保留**
    for period, expected in (
        (OTHER_ANNUAL, 11_000_000_000.0),
        (OTHER_SEMI, 12_000_000_000.0),
    ):
        row = _row_for(duckdb_store, "cash_flow_indirect", "600519", period)
        assert row is not None, f"{period} 的旧行被部分期次补采误删 —— reports/147 事故"
        assert row["net_profit"] == pytest.approx(expected)
