"""金融股判据单一真相 + 保险股历史残留清洗守卫。

事故出处：reports/139 / 2026-11-18（§5.1 三份手写判据、§5.3、§6 P0-6 历史残留、§7 前置 1）。

行为契约（按重要性排序）：
1. **判据互斥且 NULL 安全**：`is_financial_sql` / `is_non_financial_sql` 是同一个表达式的
   正反两面，任何一行都必须落进且只落进一侧；`sw_level1 IS NULL` 的股票（正式库实测
   751 行，其中仅 52 行仍在市）必须被 `is_non_financial_sql` **保留**——裸
   `NOT IN ('银行','非银金融')` 的三值逻辑会把它们静默丢掉。
2. **补采股票池用的常量不再手写**：`supplement.FINANCIAL_SECTOR_UNIVERSE_PREDICATE`
   必须由 `sector.is_financial_sql('m')` 生成（含 `COALESCE`、不含裸 `NOT IN`），
   且在原正式库上筛出的股票集合与 `sector` 那份**逐行一致**——历史上这三份
   手写判据集合不一致，正是「同一只股票在不同指标里金融/非金融结论不同」的根因。
3. **清扫器的行业判定改为单一函数**：`null_reason_sweeper` 生成的 `src_is_financial`
   表达式里不许再出现 `'保险'`（`sw_level1` 里没有这个取值，保险是 `sw_level2`），
   也不许再用股票名 LIKE 兜底（实测会把同花顺/指南针/大智慧等软件股判成金融股）。
   同时必须保留 `meta.stock_code IS NULL → NULL` 的「判不出就不写」守卫：
   `is_financial_sql` 对缺失行业返回 FALSE，直接套用会把"判不出"降级成
   "非金融"，凭空写出 NOT_APPLICABLE。
4. **清洗脚本默认 dry-run**：`scripts/repair_insurance_contract_liabilities_history.py`
   不带 `--yes` 时一行都不许改（值不变、原因表不变）；带 `--yes` 时按
   reports/139 §6 的口径把保险股的 `contract_liabilities` 置 NULL 并登记
   `NOT_APPLICABLE`（保险股的「保险合同负债」不是通用合同负债）。
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from app.core.quality import null_reason_sweeper as sweeper
from app.core.quality.null_reasons import NullReason, reasons_for
from app.core.sector import (
    FINANCIAL_CSRC_L1,
    FINANCIAL_SW_LEVEL1,
    is_financial_sql,
    is_financial_sw_level1,
    is_non_financial_sql,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.supplement import FINANCIAL_SECTOR_UNIVERSE_PREDICATE
from scripts import repair_insurance_contract_liabilities_history as history_repair

# reports/139 §6 实测的 `stock_meta.sw_level1` 分布（2026-11-18 正式库）。
SW_LEVEL1_BANK = "银行"
SW_LEVEL1_NONBANK_FINANCE = "非银金融"
# 清洗目标（reports/131 §9.2 P0-6 / reports/139 §6）
INSURANCE_CODE = "601319"
INSURANCE_NAME = "中国人保"
#: 正式库实测的 3 行历史残留（FY2011/2012/2013，合计 1,411.1 亿元）
LEGACY_ROWS = {
    date(2011, 12, 31): 49_156_000_000.0,
    date(2012, 12, 31): 50_312_000_000.0,
    date(2013, 12, 31): 41_640_000_000.0,
}


# ─── 夹具 ──────────────────────────────────────────────────────────


def _meta_rows() -> list[tuple[str, str, str | None, str | None]]:
    """(代码, 名称, sw_level1, csrc_l1) —— 覆盖正式库里会被判错/漏判的几类。

    顺序对应 reports/139 §6 的口径：银行 42 只 / 非银金融 79 只 / NULL 751 只。
    """
    return [
        ("601398", "工商银行", SW_LEVEL1_BANK, "金融业"),
        ("600030", "中信证券", SW_LEVEL1_NONBANK_FINANCE, "金融业"),
        # 申万缺失 + 证监会门类兜底（正式库这种行只有 11 行，且 csrc 不是金融业）
        ("600816", "建元信托", None, FINANCIAL_CSRC_L1),
        ("601628", "中国人寿", SW_LEVEL1_NONBANK_FINANCE, "金融业"),
        # 申万缺失且 csrc 也不是金融业 → 非金融（NULL 行必须保留，不许被 NOT IN 吞掉）
        ("000562", "宏源证券", None, "制造业"),
        # csrc='金融业' 但申万分类是计算机 —— 同花顺/指南针/大智慧这一族，
        # 旧判据的 `csrc_l1 LIKE '%金融%'` + 名称兜底会把它们判成金融股。
        ("300033", "同花顺", "计算机", "金融业"),
        ("601519", "大智慧", "计算机", "金融业"),
        ("000002", "万科A", "房地产", "房地产业"),
    ]


def _build_meta_db() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE stock_meta (stock_code VARCHAR, name VARCHAR, sw_level1 VARCHAR, csrc_l1 VARCHAR)")
    con.executemany(
        "INSERT INTO stock_meta VALUES (?, ?, ?, ?)",
        [list(row) for row in _meta_rows()],
    )
    return con


def _insert(store: DuckDBStore, table: str, row: dict) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    with store.write_connection() as connection:
        connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(row.values())
        )


def _seed_insured(store: DuckDBStore, code: str = INSURANCE_CODE, name: str = INSURANCE_NAME) -> None:
    _insert(
        store,
        "stock_meta",
        {
            "stock_code": code,
            "name": name,
            "exchange": "SSE",
            "is_listed": True,
            "sw_level1": SW_LEVEL1_NONBANK_FINANCE,
            "sw_level2": "保险Ⅱ",
            "csrc_l1": "金融业",
        },
    )


def _seed_balance(store: DuckDBStore, code: str, report_date: date, contract_liabilities: float) -> None:
    _insert(
        store,
        "balance_sheet",
        {
            "stock_code": code,
            "report_date": report_date,
            "contract_liabilities": contract_liabilities,
            "total_assets": 1_000_000.0,
        },
    )


def _contract_rows(store: DuckDBStore) -> int:
    return int(
        store.read_query(
            "SELECT COUNT(*) AS n FROM balance_sheet "
            "WHERE contract_liabilities IS NOT NULL AND contract_liabilities <> 0"
        )[0]["n"]
    )


# ─── 1. 判据互斥 + NULL 覆盖 ───────────────────────────────────────


def test_sector_predicates_are_mutually_exclusive_and_keep_null_industry_rows() -> None:
    """①金融/非金融互补、永不为 NULL；`sw_level1 IS NULL` 的行落在「非金融」侧。"""
    con = _build_meta_db()
    try:
        rows = con.execute(
            f"""
            SELECT stock_code,
                   {is_financial_sql('m')}      AS is_fin,
                   {is_non_financial_sql('m')}  AS is_non_fin,
                   m.sw_level1                  AS sw_level1,
                   m.csrc_l1                    AS csrc_l1
            FROM stock_meta m
            ORDER BY stock_code
            """
        ).fetchall()
    finally:
        con.close()

    by_code = {
        code: (is_fin, is_non_fin, sw_level1, csrc_l1)
        for code, is_fin, is_non_fin, sw_level1, csrc_l1 in rows
    }
    assert len(by_code) == len(_meta_rows())

    for code, (is_fin, is_non_fin, _sw, _csrc) in by_code.items():
        assert is_fin is not None and is_non_fin is not None, f"{code}: 判据不得返回 NULL（三值逻辑会静默丢行）"
        assert bool(is_fin) != bool(is_non_fin), f"{code}: 金融/非金融必须互斥且穷尽"

    # 金融股：申万一类命中
    assert by_code["601398"][0] is True
    assert by_code["600030"][0] is True
    # 金融股：申万缺失 + 证监会门类兜底
    assert by_code["600816"][0] is True
    # 非金融股：csrc='金融业' 但申万是计算机（旧 csrc LIKE 兜底的误判族）
    assert by_code["300033"][0] is False
    assert by_code["601519"][0] is False
    assert by_code["000002"][0] is False

    # 核心：`sw_level1 IS NULL` 的行必须留在「非金融」侧（不许被 NOT IN 的三值逻辑吞掉）
    null_industry = [code for code, (_, _, sw, _) in by_code.items() if sw is None]
    assert null_industry, "夹具必须包含 sw_level1 IS NULL 的行"
    for code in null_industry:
        csrc = by_code[code][3]
        if csrc == FINANCIAL_CSRC_L1:
            # 申万缺失但证监会门类兜底命中 → 仍然是金融股（reports/139 §6：这类仅 11 行）
            assert by_code[code][0] is True, f"{code}: csrc 兜底必须命中"
        else:
            assert by_code[code][1] is True, f"{code}: NULL 行业行必须被 is_non_financial_sql 保留"

    # Python 侧判定与 SQL 侧同口径（只覆盖申万命中，不含 csrc 兜底——见 sector 文档）
    assert is_financial_sw_level1(SW_LEVEL1_BANK) is True
    assert is_financial_sw_level1(SW_LEVEL1_NONBANK_FINANCE) is True
    assert is_financial_sw_level1("计算机") is False
    assert is_financial_sw_level1(None) is False
    assert set(FINANCIAL_SW_LEVEL1) == {SW_LEVEL1_BANK, SW_LEVEL1_NONBANK_FINANCE}


# ─── 2. 补采股票池常量 = 单一真相 ───────────────────────────────────


def test_financial_sector_universe_predicate_comes_from_sector_module() -> None:
    """②`FINANCIAL_SECTOR_UNIVERSE_PREDICATE` 由 `sector.is_financial_sql('m')` 生成。

    常量名必须保留（外部按名 import，见 `supplement.FINANCIAL_SECTOR` 的
    `universe_predicate`）；内容必须是 NULL 安全写法，不能退回手写 `IN (...)`。
    """
    assert is_financial_sql("m") == FINANCIAL_SECTOR_UNIVERSE_PREDICATE
    assert "COALESCE" in FINANCIAL_SECTOR_UNIVERSE_PREDICATE
    assert "NOT IN" not in FINANCIAL_SECTOR_UNIVERSE_PREDICATE
    # 旧手写判据（无 COALESCE 的裸 IN）不许复活
    assert "m.sw_level1 IN" not in FINANCIAL_SECTOR_UNIVERSE_PREDICATE

    # 语义一致不是"字符串长得像"：同一批 stock_meta 上逐行比结果
    con = _build_meta_db()
    try:
        rows = con.execute(
            f"""
            SELECT stock_code,
                   {FINANCIAL_SECTOR_UNIVERSE_PREDICATE}   AS via_supplement,
                   {is_financial_sql('m')}                 AS via_sector
            FROM stock_meta m
            ORDER BY stock_code
            """
        ).fetchall()
    finally:
        con.close()

    mismatches = [
        (code, via_supplement, via_sector)
        for code, via_supplement, via_sector in rows
        if via_supplement is not via_sector
    ]
    assert mismatches == [], f"两处判据筛出的股票集合必须逐行一致：{mismatches}"
    # 至少筛掉了软件股、保住了兜底股，否则这个断言在空集上也成立
    selected = {code for code, value, _ in rows if value}
    assert selected == {"601398", "600030", "600816", "601628"}


# ─── 3. 清扫器行业判定 ─────────────────────────────────────────────


def test_null_reason_sweeper_uses_single_sector_predicate() -> None:
    """③ `src_is_financial` 不再包含 `'保险'` 取值与股票名 LIKE 兜底。"""
    sql = sweeper._cell_frame_sql("balance", ["contract_liabilities"])

    # 单一真相：表达式中出现 sector 模块的 NULL 安全判据
    assert is_financial_sql("meta") in sql
    assert "COALESCE(meta.sw_level1, '')" in sql
    # `sw_level1` 里没有 '保险'（保险是 sw_level2='保险Ⅱ'），这段旧代码是死逻辑
    assert "'保险'" not in sql
    # 名称兜底会把同花顺/指南针/大智慧判成金融股（reports/139 §5.3）
    # 金融股判据本身不允许用股票名兜底（那是旧 bug）；但**银行**判据
    # （is_bank_sql，用于「只有银行才有」的科目）允许用 %银行% 兜名称，
    # 因为它只用于把 N/A 归因收窄到银行、不参与金融股剔除。
    assert "src_is_bank" in sql
    assert sql.count("LIKE '%银行%'") == 1
    # 「判不出就不写」：没有行业记录时仍是 NULL，不能降级成"非金融"
    assert "CASE WHEN meta.stock_code IS NULL THEN NULL" in sql

    # 导出证据：单独跑一遍表达式，确认软件股落 0、银行落 1、缺行业落 NULL
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            "CREATE TABLE stock_meta (stock_code VARCHAR, name VARCHAR, sw_level1 VARCHAR, "
            "sw_level2 VARCHAR, csrc_l1 VARCHAR)"
        )
        con.executemany(
            "INSERT INTO stock_meta VALUES (?, ?, ?, ?, ?)",
            [
                ["601398", "工商银行", "银行", None, "金融业"],
                ["300033", "同花顺", "计算机", "软件开发", "金融业"],
                ["601628", "中国人寿", "非银金融", "保险Ⅱ", "金融业"],
            ],
        )
        con.execute(
            "CREATE TABLE cell AS "
            "SELECT stock_code, CASE WHEN meta.stock_code IS NULL THEN NULL "
            f"WHEN {is_financial_sql('meta')} THEN 1 ELSE 0 END AS src_is_financial "
            "FROM (SELECT '601398' AS stock_code UNION ALL SELECT '300033' AS stock_code "
            "UNION ALL SELECT '601628' AS stock_code UNION ALL SELECT '999999' AS stock_code) "
            "codes LEFT JOIN stock_meta meta USING (stock_code)"
        )
        flags = dict(con.execute("SELECT stock_code, src_is_financial FROM cell").fetchall())
    finally:
        con.close()

    assert flags["601398"] == 1
    assert flags["601628"] == 1
    assert flags["300033"] == 0, "软件股有 csrc='金融业' 但不是金融股（旧名称/csrc 兜底会判成 1）"
    assert flags["999999"] is None, "没有行业记录的股票必须返回 NULL（判不出就不写）"


# ─── 4. 清洗脚本默认 dry-run + 写路径口径 ──────────────────────────


def test_history_repair_dry_run_touches_nothing(duckdb_store: DuckDBStore) -> None:
    """④ 不带 `--yes` 时：值不变、原因表不变，只输出「将改 N 行」。"""
    _seed_insured(duckdb_store)
    for report_date, amount in LEGACY_ROWS.items():
        _seed_balance(duckdb_store, INSURANCE_CODE, report_date, amount)
    values_before = _contract_rows(duckdb_store)
    reasons_before = int(
        duckdb_store.read_query("SELECT COUNT(*) AS n FROM indicator_null_reason")[0]["n"]
    )

    report = history_repair.repair(duckdb_store, yes=False)

    assert report["dry_run"] is True
    assert report["mode"] == "dry_run"
    assert report["plan"]["before_rows"] == len(LEGACY_ROWS) == 3
    assert report["plan"]["planned_reason_rows"] == len(LEGACY_ROWS)
    # reports/139 §6 的合计：1,411.1 亿
    assert report["plan"]["before_amount_yi"] == pytest.approx(1411.08, abs=0.01)
    assert report["written_rows"] == 0
    assert report["reason_rows_written"] == 0
    # 核心断言：dry-run 一个单元格都没动
    assert _contract_rows(duckdb_store) == values_before == 3
    assert int(
        duckdb_store.read_query("SELECT COUNT(*) AS n FROM indicator_null_reason")[0]["n"]
    ) == reasons_before == 0


def test_history_repair_writes_nulls_and_not_applicable_reasons(duckdb_store: DuckDBStore) -> None:
    """`--yes` 路径（临时库）：置 NULL + 写 NOT_APPLICABLE，改后再数一遍应为 0。"""
    _seed_insured(duckdb_store)
    for report_date, amount in LEGACY_ROWS.items():
        _seed_balance(duckdb_store, INSURANCE_CODE, report_date, amount)
    # 非点名股票不许被误伤（判据必须按代码收窄）
    _insert(
        duckdb_store,
        "stock_meta",
        {"stock_code": "000002", "name": "万科A", "exchange": "SZSE", "sw_level1": "房地产"},
    )
    _seed_balance(duckdb_store, "000002", date(2011, 12, 31), 12_345.0)

    report = history_repair.repair(duckdb_store, yes=True, backup=False)

    assert report["written_rows"] == 3
    assert report["after_rows"] == 0
    assert report["reason_rows_written"] == 3
    assert report["plan"]["market_share_before"][0]["share"] == pytest.approx(1.0)
    # 全市场那一行（非点名股票）必须原样保留
    remaining = duckdb_store.read_query(
        "SELECT stock_code, contract_liabilities FROM balance_sheet "
        "WHERE contract_liabilities IS NOT NULL ORDER BY stock_code"
    )
    assert [row["stock_code"] for row in remaining] == ["000002"]

    reasons = reasons_for(
        duckdb_store,
        domain="balance",
        fields=[history_repair.TARGET_FIELD_NAME],
    )
    assert len(reasons) == 3
    assert {row["reason_code"] for row in reasons} == {NullReason.NOT_APPLICABLE.value}
    assert {str(row["report_date"])[:10] for row in reasons} == {
        "2011-12-31", "2012-12-31", "2013-12-31"
    }
    assert all("保险合同负债" in (row["detail"] or "") for row in reasons)


def test_history_repair_rejects_unknown_codes() -> None:
    """`--codes` 只接受 6 位数字代码（绝不把未校验字符串拼进 SQL）。"""
    with pytest.raises(ValueError):
        history_repair.normalize_codes(["60131'; DROP TABLE balance_sheet; --"])
    assert history_repair.normalize_codes(None) == sorted(history_repair.AFFECTED_CODES)
    assert history_repair.normalize_codes(["601319"]) == ["601319"]
