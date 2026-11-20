"""P0-6 守卫**接线**回归（2026-11-17，reports/135）。

为什么要有这个文件
------------------
2026-11-16 的 P0-6 只清了存量数据（74 行置 NULL），**没有修写入路径**：
5 只险企 `contract_liabilities` 的真实来源是 **sina**，而守卫只挂在
`supplement.py`（eastmoney_f10）上。审计判定的结论是"下一期财报必复发"。
本文件钉死四件事：

A. 判定函数 `financial_guards.insurer_contract_liability_decision()`：
   非保险股 / 保险股低占比（601319 的真实合同负债）/ 保险股高占比。
B. 两条**实际写入路径**真的接上了守卫：
   `update._write_sina_financial_table()`（Sina 主路径）与
   `init._upsert_financial_row()`（通用路径）—— 险企 4.98 万亿的
   「保险合同负债」必须落成 `contract_liabilities = NULL` +
   `financial_sector_items.insurance_contract_liability = 4.98 万亿`。
C. `scripts/backfill_insurance_contract_liability.py`：从 `raw_data` JSON 的
   `contract_liabilities` 键取原值 → dry-run 不写 → `--yes` 写对。
D. `scripts/repair_insurance_contract_liability.py::SELECT_SCOPE_BREACH_SQL`
   不再是空转守卫：合成 1 行非保险股违规必须被检出。

全部用临时 DuckDB/SQLite 夹具（`duckdb_store` / `sqlite_store`），
不联网、不碰正式库。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.core.adapters.base import FetchResult, SourceMetadata
from app.core.financial_guards import (
    CONTRACT_LIABILITIES_COLUMN,
    DEFAULT_MAX_LIABILITY_SHARE,
    FINANCIAL_SECTOR_TABLE,
    GUARD_SOURCE,
    INSURANCE_CONTRACT_LIABILITY_COLUMN,
    MAX_LIABILITY_SHARE_ENV,
    REASON_AT_OR_ABOVE_THRESHOLD,
    REASON_BELOW_THRESHOLD,
    REASON_NON_INSURER,
    contract_liability_ratio_threshold,
    insurer_contract_liability_decision,
    is_insurer_industry,
)
from app.core.init import DataInitializer
from app.core.update import IncrementalUpdater
from tests.support.seed import Seed

FETCH_TIME = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)
#: 601318 中国平安 FY2024：审计给的原值（4.98 万亿）与负债合计
PINGAN_CL = 4_984_795_000_000.0
PINGAN_TL = 11_653_115_000_000.0
#: 601319 中国人保 FY2011：**真实**合同负债（占负债 9.15%），必须放行
PICC_CL = 49_156_000_000.0
PICC_TL = 537_217_000_000.0


# ─── 夹具 ─────────────────────────────────────────────────────────────


def _seed_stock(
    duck,
    code: str,
    name: str,
    *,
    sw_level1: str | None = "非银金融",
    sw_level2: str | None = "保险Ⅱ",
    csrc_l1: str | None = "金融业",
) -> None:
    Seed(duck).stock(
        code,
        name=name,
        exchange="SSE",
        listing_date="2010-01-05",
        is_listed=True,
        is_st=False,
        is_suspended=False,
        sw_level1=sw_level1,
        sw_level2=sw_level2,
        csrc_l1=csrc_l1,
        total_shares=100,
        circ_shares=80,
    )


def _seed_balance(
    duck, code: str, report_date: str, *,
    contract_liabilities: float | None = None,
    total_liabilities: float | None = None,
    raw_data: dict | None = None,
) -> None:
    Seed(duck).balance(
        code,
        report_date,
        report_type="annual",
        total_assets=12957827000000,
        total_liabilities=total_liabilities,
        total_equity=1304712000000,
        contract_liabilities=contract_liabilities,
        raw_data=json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None,
    )


def _sina_result(rows: list[dict], *, source: str = "sina") -> FetchResult:
    payload = json.dumps({"result": {"data": rows}}, ensure_ascii=False).encode()
    return FetchResult(
        data=rows,
        metadata=SourceMetadata(
            source=source,
            fetch_time=FETCH_TIME,
            raw_response_hash=hashlib.sha256(payload).hexdigest(),
            confidence="strict",
            api_version="sina-getFinanceReport2022-1",
        ),
        raw_response=payload,
    )


def _specialty_value(duck, code: str, report_date: str) -> tuple[bool, float | None]:
    rows = duck.read_query(
        f"SELECT {INSURANCE_CONTRACT_LIABILITY_COLUMN} AS value "
        f"FROM {FINANCIAL_SECTOR_TABLE} "
        "WHERE stock_code = ? AND report_date = CAST(? AS DATE)",
        [code, report_date],
    )
    return (bool(rows), rows[0]["value"] if rows else None)


# ─── A：判定函数（纯逻辑，三种情形）────────────────────────────────────


def test_decision_non_insurer_is_allowed() -> None:
    """非保险股：即便金额巨大也照常写入（不归本守卫管）。"""
    allowed, reason = insurer_contract_liability_decision(
        "600519", "食品饮料", "白酒", PINGAN_CL, PINGAN_TL,
    )
    assert allowed is True
    assert reason == REASON_NON_INSURER


def test_decision_insurer_below_threshold_is_allowed() -> None:
    """保险股 + 低占比 = 真实合同负债（601319 2011–2013 的 6%–9%）。"""
    allowed, reason = insurer_contract_liability_decision(
        "601319", "非银金融", "保险Ⅱ", PICC_CL, PICC_TL,
    )
    assert allowed is True
    assert reason.startswith(REASON_BELOW_THRESHOLD)
    assert "share=0.0915" in reason


def test_decision_insurer_at_or_above_threshold_is_rejected() -> None:
    """保险股 + 高占比 = CAS25 保险合同负债，禁止写进 contract_liabilities。"""
    allowed, reason = insurer_contract_liability_decision(
        "601318", "非银金融", "保险Ⅱ", PINGAN_CL, PINGAN_TL,
    )
    assert allowed is False
    assert reason.startswith(REASON_AT_OR_ABOVE_THRESHOLD)
    assert "share=0.4278" in reason


def test_decision_threshold_is_configurable_and_has_a_safe_default() -> None:
    """阈值可配置：默认 0.25；环境变量可覆盖；非法值回落默认。"""
    assert contract_liability_ratio_threshold() == DEFAULT_MAX_LIABILITY_SHARE
    # 覆盖成 0.5 → 42.8% 变成"低占比"，放行（证明阈值真的参与判定）
    allowed, reason = insurer_contract_liability_decision(
        "601318", "非银金融", "保险Ⅱ", PINGAN_CL, PINGAN_TL, threshold=0.5,
    )
    assert allowed is True
    assert "threshold=0.5000" in reason
    # 非法阈值（0 / 1 / 负数）一律回落默认，不得静默变成"全放行"
    assert contract_liability_ratio_threshold(0) == DEFAULT_MAX_LIABILITY_SHARE
    assert contract_liability_ratio_threshold(1.5) == DEFAULT_MAX_LIABILITY_SHARE
    assert contract_liability_ratio_threshold(-0.2) == DEFAULT_MAX_LIABILITY_SHARE


def test_decision_threshold_reads_environment(monkeypatch) -> None:
    """环境变量 `VD_INSURER_CONTRACT_LIABILITY_MAX_SHARE` 优先于默认值。"""
    monkeypatch.setenv(MAX_LIABILITY_SHARE_ENV, "0.9")
    assert contract_liability_ratio_threshold() == pytest.approx(0.9)
    allowed, _ = insurer_contract_liability_decision(
        "601318", "非银金融", "保险Ⅱ", PINGAN_CL, PINGAN_TL,
    )
    assert allowed is True, "阈值提到 0.9 后 42.8% 应被放行（证明 env 生效）"


def test_decision_fails_closed_when_denominator_is_missing() -> None:
    """保险股 + 负债合计缺失 → 不许放行（这正是"下一期财报复发"的入口）。"""
    allowed, reason = insurer_contract_liability_decision(
        "601318", "非银金融", "保险Ⅱ", PINGAN_CL, None,
    )
    assert allowed is False
    assert reason.startswith("insurer_share_unknown_fail_closed")


def test_is_insurer_industry_predicates() -> None:
    assert is_insurer_industry(industry_level2="保险Ⅱ")
    assert is_insurer_industry(industry_level1="保险")
    assert is_insurer_industry(company_name="中国平安保险(集团)股份有限公司")
    assert not is_insurer_industry(industry_level1="非银金融", industry_level2="证券Ⅱ")
    assert not is_insurer_industry(industry_level1="食品饮料", industry_level2="白酒")


# ─── B：两条实际写入路径 ───────────────────────────────────────────────


def test_sina_path_rejects_insurer_contract_liability(duckdb_store, sqlite_store) -> None:
    """B 核心：Sina 路径把险企「保险合同负债」拦下并改道专用列。

    构造的正是生产数据形态：`contract_liabilities = 4.98 万亿`，
    `total_liabilities = 11.65 万亿`（占比 42.8%）。
    """
    _seed_stock(duckdb_store, "601318", "中国平安")
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    rows = [{
        "stock_code": "601318",
        "report_date": "2024-12-31",
        "total_assets": 12_957_827_000_000.0,
        "total_liabilities": PINGAN_TL,
        "total_equity": 1_304_712_000_000.0,
        CONTRACT_LIABILITIES_COLUMN: PINGAN_CL,
    }]

    with duckdb_store.transaction() as conn:
        IncrementalUpdater._write_sina_financial_table(
            initializer, conn, "balance_sheet", "601318", _sina_result(rows),
        )

    stored = duckdb_store.read_query(
        "SELECT contract_liabilities, total_liabilities FROM balance_sheet "
        "WHERE stock_code = '601318' AND report_date = DATE '2024-12-31'"
    )
    assert stored, "财务行本身必须写进去（守卫只摘掉被污染的那一列）"
    assert stored[0]["contract_liabilities"] is None
    assert stored[0]["total_liabilities"] == pytest.approx(PINGAN_TL)

    exists, value = _specialty_value(duckdb_store, "601318", "2024-12-31")
    assert exists, "被拒的值必须改道落 financial_sector_items（不能静默丢弃）"
    assert value == pytest.approx(PINGAN_CL)

    # raw_data 必须保留上游原值 —— 回填脚本（C）正是靠它把金额找回专用列的
    raw = duckdb_store.read_query(
        "SELECT raw_data FROM balance_sheet "
        "WHERE stock_code = '601318' AND report_date = DATE '2024-12-31'"
    )[0]["raw_data"]
    assert json.loads(raw)[CONTRACT_LIABILITIES_COLUMN] == pytest.approx(PINGAN_CL)

    # 改道行必须自带可核对来源，且**不能**冒用 eastmoney_f10（否则会被补采删除）
    meta = duckdb_store.read_query(
        f"SELECT source, confidence, batch_id FROM {FINANCIAL_SECTOR_TABLE} "
        "WHERE stock_code = '601318' AND report_date = DATE '2024-12-31'"
    )[0]
    assert meta["source"] == GUARD_SOURCE
    assert meta["batch_id"] == f"{GUARD_SOURCE}:601318:2024-12-31", (
        "改道行必须带确定性批号（可回查股/期），不能留空也不能冒用主路径批次"
    )


def test_sina_path_clears_pre_existing_polluted_value(duckdb_store, sqlite_store) -> None:
    """库里已经有脏值时，Sina 再写一次必须把它清掉（COALESCE 追不到 NULL）。"""
    _seed_stock(duckdb_store, "601628", "中国人寿")
    _seed_balance(
        duckdb_store, "601628", "2024-12-31",
        contract_liabilities=5_825_026_000_000.0,
        total_liabilities=6_248_298_000_000.0,
    )
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    rows = [{
        "stock_code": "601628",
        "report_date": "2024-12-31",
        "total_liabilities": 6_248_298_000_000.0,
        CONTRACT_LIABILITIES_COLUMN: 5_825_026_000_000.0,
    }]

    with duckdb_store.transaction() as conn:
        IncrementalUpdater._write_sina_financial_table(
            initializer, conn, "balance_sheet", "601628", _sina_result(rows),
        )

    stored = duckdb_store.read_query(
        "SELECT contract_liabilities FROM balance_sheet "
        "WHERE stock_code = '601628' AND report_date = DATE '2024-12-31'"
    )
    assert stored[0]["contract_liabilities"] is None
    _, value = _specialty_value(duckdb_store, "601628", "2024-12-31")
    assert value == pytest.approx(5_825_026_000_000.0)


def test_reroute_keeps_supplement_owned_row_provenance(duckdb_store, sqlite_store) -> None:
    """改道只写专用列：既有 `eastmoney_f10` 行的 source / 其它科目不得被改写。

    否则 supplement `replace_source_rows` 的
    `DELETE ... WHERE source = 'eastmoney_f10'` 就命中不到这些行，
    下一轮 `INSERT ... BY NAME` 会直接撞主键（(stock_code, report_date) 是 PK）。
    这道断言把"改道必须是无副作用的部分更新"钉死。
    """
    _seed_stock(duckdb_store, "601318", "中国平安")
    duckdb_store.write_query(
        # 留（第 4 类·多站点缺口）：`financial_sector_items` 全仓 2 处播种，工厂没有方法；
        """INSERT INTO financial_sector_items
               (stock_code, report_date, report_type, earned_premiums,
                source, fetch_time, raw_response_hash, confidence, batch_id)
           VALUES ('601318', DATE '2025-06-30', 'semi_annual', 123.0,
                   'eastmoney_f10', TIMESTAMP '2026-09-01 00:00:00', 'h', 'strict', 'b')"""
    )
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    rows = [{
        "stock_code": "601318",
        "report_date": "2025-06-30",
        "total_liabilities": 12_166_459_000_000.0,
        CONTRACT_LIABILITIES_COLUMN: 5_333_626_000_000.0,
    }]

    with duckdb_store.transaction() as conn:
        IncrementalUpdater._write_sina_financial_table(
            initializer, conn, "balance_sheet", "601318", _sina_result(rows),
        )

    row = duckdb_store.read_query(
        f"SELECT source, earned_premiums, {INSURANCE_CONTRACT_LIABILITY_COLUMN} AS rerouted "
        f"FROM {FINANCIAL_SECTOR_TABLE} "
        "WHERE stock_code = '601318' AND report_date = DATE '2025-06-30'"
    )[0]
    assert row["source"] == "eastmoney_f10", "改道不得改写既有行的来源标签"
    assert row["earned_premiums"] == pytest.approx(123.0), "改道不得清掉其它科目"
    assert row["rerouted"] == pytest.approx(5_333_626_000_000.0)


def test_init_path_rejects_insurer_contract_liability(duckdb_store, sqlite_store) -> None:
    """第二条实际路径 `init._upsert_financial_row` 同样必须接上守卫。"""
    _seed_stock(duckdb_store, "601336", "新华保险")
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    row = {
        "stock_code": "601336",
        "report_date": "2024-12-31",
        "total_liabilities": PINGAN_TL,
        CONTRACT_LIABILITIES_COLUMN: PINGAN_CL,
    }

    with duckdb_store.transaction() as conn:
        initializer._upsert_financial_row(conn, "balance_sheet", "601336", row)

    stored = duckdb_store.read_query(
        "SELECT contract_liabilities FROM balance_sheet "
        "WHERE stock_code = '601336' AND report_date = DATE '2024-12-31'"
    )
    assert stored[0]["contract_liabilities"] is None
    _, value = _specialty_value(duckdb_store, "601336", "2024-12-31")
    assert value == pytest.approx(PINGAN_CL)


def test_no_false_positive_on_genuine_insurer_contract_liability(
    duckdb_store, sqlite_store,
) -> None:
    """误伤保护：601319 式真实合同负债（占负债 9.15%）必须照常写入。

    601319 2011–2013 的 49.2B/50.3B/41.6B 当年就是靠同类判据保下来的；
    守卫如果只看行业，这三行会被误清。
    """
    _seed_stock(duckdb_store, "601319", "中国人保")
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    rows = [{
        "stock_code": "601319",
        "report_date": "2011-12-31",
        "total_liabilities": PICC_TL,
        CONTRACT_LIABILITIES_COLUMN: PICC_CL,
    }]

    with duckdb_store.transaction() as conn:
        IncrementalUpdater._write_sina_financial_table(
            initializer, conn, "balance_sheet", "601319", _sina_result(rows),
        )

    stored = duckdb_store.read_query(
        "SELECT contract_liabilities FROM balance_sheet "
        "WHERE stock_code = '601319' AND report_date = DATE '2011-12-31'"
    )
    assert stored[0]["contract_liabilities"] == pytest.approx(PICC_CL)
    exists, value = _specialty_value(duckdb_store, "601319", "2011-12-31")
    assert not exists or value is None, "真实合同负债不得改道专用列"


def test_non_insurer_writes_are_untouched(duckdb_store, sqlite_store) -> None:
    """同一批里非保险股照常写入（守卫不得误伤全市场）。"""
    _seed_stock(
        duckdb_store, "600519", "贵州茅台",
        sw_level1="食品饮料", sw_level2="白酒", csrc_l1="制造业",
    )
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    rows = [{
        "stock_code": "600519",
        "report_date": "2024-12-31",
        "total_liabilities": 49_875_590_112.37,
        CONTRACT_LIABILITIES_COLUMN: 8_006_739_780.94,
    }]

    with duckdb_store.transaction() as conn:
        IncrementalUpdater._write_sina_financial_table(
            initializer, conn, "balance_sheet", "600519", _sina_result(rows),
        )

    stored = duckdb_store.read_query(
        "SELECT contract_liabilities FROM balance_sheet "
        "WHERE stock_code = '600519' AND report_date = DATE '2024-12-31'"
    )
    assert stored[0]["contract_liabilities"] == pytest.approx(8_006_739_780.94)
    exists, _ = _specialty_value(duckdb_store, "600519", "2024-12-31")
    assert not exists, "非保险股不该产生专用列行"


# ─── C：历史回填脚本 ───────────────────────────────────────────────────


def test_backfill_extracts_value_from_the_probed_raw_data_key() -> None:
    """正式库实测的键名就是 `contract_liabilities`（不是中文名/大写英文名）。"""
    from scripts import backfill_insurance_contract_liability as backfill_script

    value, key = backfill_script.extract_raw_contract_liability(
        json.dumps({CONTRACT_LIABILITIES_COLUMN: PINGAN_CL, "total_assets": 1.0})
    )
    assert (value, key) == (PINGAN_CL, CONTRACT_LIABILITIES_COLUMN)
    # 兼容：未来真有源按中文名/大写英文名写入时也能取到
    assert backfill_script.extract_raw_contract_liability(
        json.dumps({"保险合同负债": 1.0})
    )[1] == "保险合同负债"
    assert backfill_script.extract_raw_contract_liability(
        json.dumps({"INSURANCE_CONTRACT_LIAB": 2.0})
    )[1] == "INSURANCE_CONTRACT_LIAB"
    # 取不到 → (None, None)
    assert backfill_script.extract_raw_contract_liability(json.dumps({"total_assets": 1})) == (
        None, None,
    )


def test_backfill_dry_run_then_yes(duckdb_store, database_paths) -> None:
    """dry-run 一行不写；`--yes` 把 raw_data 原值写进专用列。"""
    from scripts import backfill_insurance_contract_liability as backfill_script

    _seed_stock(duckdb_store, "601318", "中国平安")
    _seed_balance(
        duckdb_store, "601318", "2024-12-31",
        contract_liabilities=None,
        total_liabilities=PINGAN_TL,
        raw_data={
            "stock_code": "601318",
            "report_date": "2024-12-31",
            "total_liabilities": PINGAN_TL,
            CONTRACT_LIABILITIES_COLUMN: PINGAN_CL,
        },
    )
    # 一行"raw_data 里找不到原值"（键名对不上）：
    _seed_stock(duckdb_store, "601628", "中国人寿")
    _seed_balance(
        duckdb_store, "601628", "2024-12-31",
        total_liabilities=6_248_298_000_000.0,
        raw_data={"stock_code": "601628", "total_assets": 1.0},
    )

    collected = backfill_script.collect_backfill_rows(duckdb_store)
    assert collected["stats"]["backfillable_rows"] == 1
    assert collected["stats"]["backfillable_codes"] == ["601318"]
    assert collected["stats"]["raw_missing_rows"] == 1, "找不到原值的行必须如实计数"
    assert collected["stats"]["raw_key_hits"] == {CONTRACT_LIABILITIES_COLUMN: 1}

    dry = backfill_script.backfill(
        duckdb_store, yes=False, lock_path=database_paths.duckdb_path,
    )
    assert dry["mode"] == "dry_run"
    assert dry["written"] == 0
    assert _specialty_value(duckdb_store, "601318", "2024-12-31") == (False, None)

    report = backfill_script.backfill(
        duckdb_store, yes=True, backup=False, lock_path=database_paths.duckdb_path,
    )
    assert report["written"] == 1
    assert report["remaining_after"] == 0
    exists, value = _specialty_value(duckdb_store, "601318", "2024-12-31")
    assert exists and value == pytest.approx(PINGAN_CL)
    # 幂等：再跑一次不重写、也不报错
    again = backfill_script.backfill(
        duckdb_store, yes=True, backup=False, lock_path=database_paths.duckdb_path,
    )
    assert again["written"] == 0
    assert again["selected"]["already_filled_rows"] == 1


def test_backfill_skips_genuine_insurer_contract_liability(duckdb_store) -> None:
    """低占比的真实合同负债即便 raw_data 里有值也不得回填到专用列。

    601319 2011–2013 的 49.2B/50.3B/41.6B（占负债 6%–9%）当年就是靠同一
    量级判据保下来的；脚本若只看行业就会把它们也搬进专用列。这里用 CAS25
    窗口内的同一比例做合成用例（2013 那几行本身被 `report_date >= 2022-12-31`
    的 CAS25 起点排除，属于另一层保护）。
    """
    from scripts import backfill_insurance_contract_liability as backfill_script

    _seed_stock(duckdb_store, "601319", "中国人保")
    _seed_balance(
        duckdb_store, "601319", "2023-12-31",
        contract_liabilities=None,
        total_liabilities=PICC_TL,
        raw_data={
            "stock_code": "601319",
            "report_date": "2023-12-31",
            CONTRACT_LIABILITIES_COLUMN: PICC_CL,
        },
    )
    collected = backfill_script.collect_backfill_rows(duckdb_store)
    assert collected["stats"]["backfillable_rows"] == 0
    assert collected["stats"]["genuine_contract_liability_rows"] == 1
    assert (
        collected["genuine"][0]["reason"].startswith(REASON_BELOW_THRESHOLD)
    ), "低占比必须走'真实合同负债'分支，而不是靠别的巧合被跳过"


# ─── D：一次性脚本的守卫 SQL 不再是空转 ────────────────────────────────


def test_scope_breach_sql_detects_a_synthetic_violation(duckdb_store) -> None:
    """改前恒 0（自相矛盾）；改后必须检出 1 行合成违规。"""
    from scripts import repair_insurance_contract_liability as repair_script

    _seed_stock(duckdb_store, "601318", "中国平安")
    _seed_balance(
        duckdb_store, "601318", "2025-12-31",
        contract_liabilities=PINGAN_CL, total_liabilities=PINGAN_TL,
    )
    counter = lambda: int(  # noqa: E731 - 断言里只出现一次，直接读常量 SQL
        duckdb_store.read_query(repair_script.SELECT_SCOPE_BREACH_SQL)[0]["n"]
    )
    assert counter() == 0, "全是保险股时不该算违规"

    # 合成 1 行非保险股违规（贵州茅台没有占比 25% 的合同负债，纯合成）
    _seed_stock(
        duckdb_store, "600519", "贵州茅台",
        sw_level1="食品饮料", sw_level2="白酒", csrc_l1="制造业",
    )
    _seed_balance(
        duckdb_store, "600519", "2025-12-31",
        contract_liabilities=8_006_739_780.94, total_liabilities=49_875_590_112.37,
    )
    assert counter() == 0, "1605 万 / 4.99 亿 = 16% < 25%，不该命中"

    duckdb_store.write_query(
        "UPDATE balance_sheet SET contract_liabilities = 40000000000 "
        "WHERE stock_code = '600519'"
    )
    assert counter() == 1, "非保险股占比 >= 25% 必须被检出（证明守卫不是摆设）"


def test_scope_breach_sql_is_not_self_contradictory() -> None:
    """静态钉死：WHERE 里不得同时出现 `<> '保险Ⅱ'` 与 `= '保险Ⅱ'`。"""
    from scripts import repair_insurance_contract_liability as repair_script

    sql = repair_script.SELECT_SCOPE_BREACH_SQL
    assert "sw_level2" in sql
    assert "= '保险Ⅱ'" not in sql, (
        "SELECT_SCOPE_BREACH_SQL 若再写回 `m.sw_level2 = '保险Ⅱ'` 就会与 "
        "`<> '保险Ⅱ'` 自相矛盾并恒返回 0（reports/135 第 5 项）"
    )
