"""机构持仓域（schema v49，reports/139 §11.3）的自证用例。

覆盖的是**最容易静默出错**的五件事（全部离线，不打网络）：

1. **列改名 + 百分比单位**：源端 22 个原始字段（`SECURITY_CODE`/`TOTAL_SHARES`/
   `FREESHARES_RATIO`…）必须按**名字**映射到本域列；`ratio_of_float` 进来是
   **百分数**（13.9565 = 13.96%），落库不许缩放、更不许 ÷100 变成 0.1396
   （reports/139 §11.4 的 CPI 100 倍坑是同一类事故）。
2. **比例缺失不写 0**（D49）：持股 1.15 亿股却「占流通股 0%」是不可能的读数 ——
   实测 2020Q2 有 12 行是当日新上市股（流通股本未登记），源端用 0 占位。
   这类必须置 NULL + `confidence=approximate`，下游不许把 NULL 当 0 参与加总。
3. **R6 双向阈值**：少量坏行只隔离并计数；坏行 ≥1/3 整批拒绝，绝不写半张表。
4. **同日重跑幂等**（`replace_scope=period`，先删后插）：
   行数不翻倍，且源端掉出覆盖的股票不会留下陈旧行。
5. **`--limit` 试点不误删其它股票**（`replace_scope=codes`），且**空结果不写库**
   （既不写 0 行占位，也不把「源端没有」当成「大家都持了 0 股」）。

事故编号：reports/139 §11.3（机构持仓可做）、AGENTS R6（逐股/逐期隔离）、
KB D49（空值不写 0）、KB D19/D36（批量写禁 executemany）。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.core.institution_holdings import (
    HOLDER_TYPES,
    holdings_evidence,
    latest_quarter_end,
    normalize,
    quarter_end,
    quarters_back,
    select_top_stocks,
    write_holdings,
)

REPORT_DATE = date(2025, 6, 30)
HOLDER = "基金"


def _raw(rows: list[dict]) -> pd.DataFrame:
    """构造与东财 `dataapi/zlsj/list` **逐字同形**的原始 22 字段表（列名照抄源端）。"""
    base = {
        "SECURITY_INNER_CODE": "1001194744",
        "SECURITY_CODE": "300750",
        "SECURITY_NAME_ABBR": "宁德时代",
        "REPORT_DATE": "2025-06-30 00:00:00",
        "ORG_TYPE": "01",
        "HOULD_NUM": 2598,
        "TOTAL_SHARES": 593448886,
        "HOLD_VALUE": 149679678026.92,
        "FREESHARES_RATIO": 13.95651927,
        "HOLDCHA": "增仓",
        "HOLDCHA_NUM": 13020165,
        "HOLDCHA_RATIO": 2.24,
        "SECUCODE": "300750.SZ",
        "TOTALSHARES_RATIO": 13.01619886,
        "ORG_TYPE_NAME": "基金",
        "QCHANGE_RATE": 1.711236739594,
        "FREE_MARKET_CAP": 149679678026.92,
        "FREE_SHARES": 593448886,
        "SECURITY_TYPE_CODE": "058001001",
        "HOLDCHA_VALUE": 2866037337.17972,
        "FREESHARES_RATIO_CHANGE": -1,
        "TYPE_NUM": 1,
    }
    # 空载荷也必须保留**列名**：`normalize` 要先过「列名漂移」闸门再判「空结果」，
    # `pd.DataFrame.from_records([])` 会连列都没有，测的就不是空结果那条路径了。
    return pd.DataFrame.from_records([{**base, **row} for row in rows], columns=list(base))


def test_columns_renamed_and_percentage_unit_kept() -> None:
    """原始字段 → 本域列（按名映射），且 `ratio_of_float` 保持**百分数**不缩放。"""
    rows, stats = normalize(_raw([{}]), report_date=REPORT_DATE, holder_type=HOLDER)

    assert list(rows.columns)[: len(rows.columns) - 1] == [
        "stock_code", "report_date", "holder_type", "shares", "ratio_of_float",
        "holder_count", "source", "fetch_time", "raw_hash", "confidence", "batch_id",
    ]
    row = rows.iloc[0]
    assert row["stock_code"] == "300750"
    assert row["holder_type"] == "基金"
    assert row["report_date"] == REPORT_DATE
    # 单位：股（不是万股）—— 5.93 亿股，禁止 ×10000
    assert float(row["shares"]) == pytest.approx(593_448_886.0)
    # 单位：百分数 —— 13.9565 表示 13.96%，不是 0.1396、也不是 1395.65
    assert float(row["ratio_of_float"]) == pytest.approx(13.95651927)
    assert float(row["holder_count"]) == pytest.approx(2598.0)
    assert row["confidence"] == "exact"
    assert row["source"] == "eastmoney:zlsj_holder_list"
    assert stats["ratio_coverage_pct"] == 100.0


def test_zero_ratio_with_positive_shares_becomes_null_not_zero() -> None:
    """持股 > 0 而比例恰为 0（新股流通股本未登记）→ NULL + approximate（D49）。"""
    rows, stats = normalize(
        _raw([
            {"SECURITY_CODE": "300846", "TOTAL_SHARES": 3032809, "FREESHARES_RATIO": 0},
            {"SECURITY_CODE": "300842", "TOTAL_SHARES": 1194568, "FREESHARES_RATIO": 4.778272},
        ]),
        report_date=REPORT_DATE, holder_type=HOLDER,
    )

    assert stats["ratio_zero_filled_null"] == 1
    blank = rows[rows["stock_code"] == "300846"].iloc[0]
    assert pd.isna(blank["ratio_of_float"]), "比例缺失不许写成 0"
    assert float(blank["shares"]) == pytest.approx(3_032_809.0), "股数本身是真实读数，必须保留"
    assert blank["confidence"] == "approximate"
    ok = rows[rows["stock_code"] == "300842"].iloc[0]
    assert ok["confidence"] == "exact"
    assert stats["ratio_coverage_pct"] == 50.0


def test_duplicate_rows_deduped_and_sorted() -> None:
    """同代码同载荷的重复行去重（唯一只数才是覆盖率分子），结果按代码升序。"""
    rows, stats = normalize(
        _raw([
            {"SECURITY_CODE": "600519", "TOTAL_SHARES": 11496754},
            {"SECURITY_CODE": "600519", "TOTAL_SHARES": 11496754},  # 页间重叠
            {"SECURITY_CODE": "000001", "TOTAL_SHARES": 200000000},
        ]),
        report_date=REPORT_DATE, holder_type=HOLDER,
    )

    assert stats["rows_in_response"] == 3
    assert stats["duplicate_rows_dropped"] == 1
    assert stats["duplicate_conflicts"] == 0
    assert stats["stocks"] == 2 and len(rows) == 2
    assert list(rows["stock_code"]) == ["000001", "600519"]
    assert not rows.duplicated(["stock_code", "report_date", "holder_type"]).any()


def test_few_bad_rows_are_isolated() -> None:
    """R6 上半：少量坏行只隔离并计数，不否决整批（1/101 两个阈值都不满足）。"""
    payload = [{"SECURITY_CODE": "ABCDEF"}]
    payload += [
        {"SECURITY_CODE": f"{index:06d}", "TOTAL_SHARES": 1000 + index}
        for index in range(1, 101)
    ]
    rows, stats = normalize(_raw(payload), report_date=REPORT_DATE, holder_type=HOLDER)

    assert stats["dropped_invalid_code"] == 1
    assert stats["dropped_invalid_code_examples"] == ["ABCDEF"]
    assert stats["stocks"] == 100
    assert "ABCDEF" not in set(rows["stock_code"])


def test_bad_rows_at_one_third_reject_whole_batch() -> None:
    """R6 下半：坏行 ≥1/3 → 整批拒绝（疑似源端结构性损坏，不许写半张表）。"""
    payload = [{"SECURITY_CODE": "600519"}, {"SECURITY_CODE": "xxxxxx"}]
    with pytest.raises(RuntimeError, match="整批拒绝"):
        normalize(_raw(payload), report_date=REPORT_DATE, holder_type=HOLDER)


def test_period_or_holder_type_mismatch_is_structural_damage() -> None:
    """报告期/持有人类型与请求不符 = 字段串行（错位事故的近亲）→ 整批拒绝。"""
    payload = [
        {"SECURITY_CODE": f"{index:06d}", "REPORT_DATE": "2019-12-31 00:00:00"}
        for index in range(1, 5)
    ]
    with pytest.raises(RuntimeError, match="整批拒绝"):
        normalize(_raw(payload), report_date=REPORT_DATE, holder_type=HOLDER)


def test_empty_response_refuses_to_write() -> None:
    """空结果不写库（不写 0 行占位，也不把「源端没有」当成「大家都持了 0 股」）。"""
    with pytest.raises(RuntimeError, match="空结果不写库"):
        normalize(_raw([]), report_date=REPORT_DATE, holder_type=HOLDER)

    rows, _ = normalize(_raw([{}]), report_date=REPORT_DATE, holder_type=HOLDER)
    with pytest.raises(ValueError, match="拒绝写入 0 行"):
        write_holdings(
            object(),  # type: ignore[arg-type]  # 0 行在碰连接前就该被拒
            rows.iloc[0:0], report_date=REPORT_DATE, holder_type=HOLDER,
        )


def test_same_period_rerun_is_idempotent(duckdb_store) -> None:
    """同期重跑幂等：`replace_scope=period` 先删后插，行数不翻倍、陈旧行被清掉。"""
    first = _raw([
        {"SECURITY_CODE": "600519", "TOTAL_SHARES": 11496754},
        {"SECURITY_CODE": "000001", "TOTAL_SHARES": 200000000},
        {"SECURITY_CODE": "601318", "TOTAL_SHARES": 300000000},
    ])
    rows, _ = normalize(first, report_date=REPORT_DATE, holder_type=HOLDER)
    written = write_holdings(duckdb_store, rows, report_date=REPORT_DATE, holder_type=HOLDER)
    assert written["rows_written"] == 3 and written["rows_deleted"] == 0
    assert holdings_evidence(duckdb_store, [REPORT_DATE])["rows"] == 3

    # ① 同一份数据重跑：先删 3 再插 3，行数恒 3
    again = write_holdings(duckdb_store, rows, report_date=REPORT_DATE, holder_type=HOLDER)
    assert again["rows_deleted"] == 3
    assert holdings_evidence(duckdb_store, [REPORT_DATE])["rows"] == 3

    # ② 源端本期掉出覆盖 000001：period 范围重跑必须把它清掉（不留陈旧行）
    shrunk, _ = normalize(
        first.head(2), report_date=REPORT_DATE, holder_type=HOLDER,
    )
    write_holdings(duckdb_store, shrunk, report_date=REPORT_DATE, holder_type=HOLDER)
    evidence = holdings_evidence(duckdb_store, [REPORT_DATE])
    assert evidence["rows"] == 2 and evidence["stocks"] == 2


def test_limit_scope_does_not_delete_other_stocks(duckdb_store) -> None:
    """`--limit` 试点走 `replace_scope=codes`：不许删掉同期其它股票的已有行。"""
    full, _ = normalize(
        _raw([
            {"SECURITY_CODE": "600519", "TOTAL_SHARES": 11496754},
            {"SECURITY_CODE": "000001", "TOTAL_SHARES": 200000000},
            {"SECURITY_CODE": "601318", "TOTAL_SHARES": 300000000},
        ]),
        report_date=REPORT_DATE, holder_type=HOLDER,
    )
    write_holdings(duckdb_store, full, report_date=REPORT_DATE, holder_type=HOLDER)
    assert holdings_evidence(duckdb_store, [REPORT_DATE])["rows"] == 3

    # `full` 已按代码升序（000001 / 600519 / 601318），持仓市值故意设成 601318 最大
    ranked = full.assign(hold_value=[1.0, 5.0e10, 9.0e10])
    pilot = select_top_stocks(ranked, 1)
    assert list(pilot["stock_code"]) == ["601318"], "按持仓市值（不是代码升序）取前 1 只"
    write_holdings(
        duckdb_store, pilot, report_date=REPORT_DATE, holder_type=HOLDER,
        replace_scope="codes",
    )
    evidence = holdings_evidence(duckdb_store, [REPORT_DATE])
    assert evidence["rows"] == 3, "codes 范围只能删/写 000001，不得波及 600519/601318"
    assert evidence["stocks"] == 3


def test_holder_types_and_quarter_helpers() -> None:
    """6 类持有人固定；报告期只允许四季末，往前数季度不跨年错位。"""
    assert [name for _, name in HOLDER_TYPES] == ["基金", "QFII", "社保", "券商", "保险", "信托"]
    assert quarter_end(2025, 2) == date(2025, 6, 30)
    assert quarter_end(2025, 4) == date(2025, 12, 31)
    assert latest_quarter_end(date(2026, 11, 18)) == date(2026, 9, 30)
    assert latest_quarter_end(date(2026, 1, 5)) == date(2025, 12, 31)
    assert quarters_back(date(2026, 9, 30), 3) == [
        date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30),
    ]
    assert quarters_back(date(2025, 3, 31), 2) == [date(2024, 12, 31), date(2025, 3, 31)]
