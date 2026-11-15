"""行业变更历史 / 报告元数据回归（2026-11-15，reports/121 J3）。

覆盖三层：
1. 纯解析函数（不碰库）：CSMAR 行业列 / 报告元数据列 / 证监会记录；
2. 写库幂等（临时 DuckDB）：重复导入不产生重复行；
3. 查询（时间线按日期正序）。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.core import industry_history as ih
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.schema import init_duckdb_schema


@pytest.fixture()
def duck(database_paths) -> DuckDBStore:
    store = DuckDBStore(paths=database_paths)
    init_duckdb_schema(store)
    return store


# ─── 1. 纯解析 ────────────────────────────────────────────────────

def test_parse_csmar_industry_rows_maps_indcd_and_name() -> None:
    frame = pd.DataFrame(
        {
            "Stkcd": ["000001", "000001", "600519"],
            "Accper": ["2001-12-31", "2020-12-31", "2015-12-31"],
            "Indcd": ["I01", "J66", "C15"],
            "Indnme": ["金融业", "货币金融服务", "酒、饮料和精制茶制造业"],
            "Indcd1": ["X", "Y", "Z"],  # 协会口径：必须被忽略
            "Indnme1": ["协会口径", "协会口径", "协会口径"],
        }
    )
    rows = ih.parse_csmar_industry_rows(frame)
    assert [row["stock_code"] for row in rows] == ["000001", "000001", "600519"]
    first = rows[0]
    assert first["effective_date"] == date(2001, 12, 31)
    assert first["level2"] == "I01"
    assert first["level1"] == "金融业"
    # 同一股票跨报告期保留两条（这正是"行业变更历史"的意义）
    assert {row["effective_date"] for row in rows if row["stock_code"] == "000001"} == {
        date(2001, 12, 31), date(2020, 12, 31),
    }


def test_parse_csmar_industry_rows_skips_empty_indcd() -> None:
    counters: dict[str, int] = {}
    frame = pd.DataFrame(
        {
            "Stkcd": ["000001", "000002"],
            "Accper": ["2023-03-31", "2023-03-31"],
            "Indcd": [None, "C39"],
            "Indnme": ["金融业", "计算机"],
        }
    )
    rows = ih.parse_csmar_industry_rows(frame, counters=counters)
    assert len(rows) == 1
    assert rows[0]["stock_code"] == "000002"
    assert counters.get("skipped_empty_indcd") == 1


def test_parse_report_metadata_typrep_mapping_and_earliest_declare_date() -> None:
    frame = pd.DataFrame(
        {
            "Stkcd": ["000001", "000002", "000003"],
            "Accper": ["2020-12-31", "2020-12-31", "2020-12-31"],
            "Typrep": ["A", "B", "C"],
            "IfCorrect": [1, 0, 1],
            "DeclareDate": ["2021-03-01", "2021-04-30", "2021-05-20"],
        }
    )
    rows = {row["stock_code"]: row for row in ih.parse_csmar_report_metadata_rows(frame)}
    assert rows["000001"]["report_type"] == "consolidated"
    assert rows["000001"]["is_corrected"] is True
    assert rows["000001"]["declare_date"] == date(2021, 3, 1)
    assert rows["000002"]["report_type"] == "parent"
    assert rows["000002"]["is_corrected"] is False
    assert rows["000002"]["declare_date"] is None  # 未更正 → 不写披露日
    assert rows["000003"]["report_type"] is None  # 未知 Typrep 不猜


def test_parse_report_metadata_prefers_consolidated_row() -> None:
    frame = pd.DataFrame(
        {
            "Stkcd": ["000001", "000001"],
            "Accper": ["2020-12-31", "2020-12-31"],
            "Typrep": ["B", "A"],
            "IfCorrect": [0, 0],
            "DeclareDate": [None, None],
        }
    )
    rows = ih.parse_csmar_report_metadata_rows(frame)
    assert len(rows) == 1
    assert rows[0]["report_type"] == "consolidated"


def test_csrc_records_to_rows_uses_as_of_date() -> None:
    records = [
        {"stock_code": "600519", "csrc_l1": "制造业", "csrc_l2": "酒、饮料和精制茶制造业",
         "as_of_date": "2012-07-01"},
        {"stock_code": "600519", "csrc_l1": "制造业", "csrc_l2": "酒、饮料和精制茶制造业",
         "as_of_date": "2021-01-01"},
        {"stock_code": "000001", "csrc_l1": "金融业", "csrc_l2": "货币金融服务"},
    ]
    counters: dict[str, int] = {}
    rows = ih.csrc_records_to_rows(records, counters=counters)
    assert {row["effective_date"] for row in rows} == {date(2012, 7, 1), date(2021, 1, 1)}
    assert counters.get("skipped_no_as_of_date") == 1


# ─── 2. 写库幂等 ──────────────────────────────────────────────────

def test_upsert_industry_history_is_idempotent(duck: DuckDBStore) -> None:
    rows = [
        {"stock_code": "600519", "effective_date": date(2012, 7, 1),
         "level1": "制造业", "level2": "C15"},
        {"stock_code": "600519", "effective_date": date(2021, 1, 1),
         "level1": "制造业", "level2": "C15"},
    ]
    first = ih.upsert_industry_history(duck, rows, source=ih.CSMAR_INDUSTRY_SOURCE)
    second = ih.upsert_industry_history(duck, rows, source=ih.CSMAR_INDUSTRY_SOURCE)
    total = duck.read_query("SELECT COUNT(*) AS n FROM industry_history")[0]["n"]
    assert first >= 1
    assert total == 2, f"重复导入不应产生重复行，实际 {total}（second={second}）"


def test_upsert_csrc_history_writes_csrc_source(duck: DuckDBStore) -> None:
    records = [
        {"stock_code": "000001", "csrc_l1": "金融业", "csrc_l2": "货币金融服务",
         "as_of_date": "2012-07-01"},
        {"stock_code": "000001", "csrc_l1": "金融业", "csrc_l2": "货币金融服务",
         "as_of_date": "2021-01-01"},
    ]
    report = ih.upsert_csrc_history(duck, records)
    assert report["written"] == 2
    timeline = ih.read_industry_timeline(duck, "000001")
    assert [item["effective_date"] for item in timeline] == [date(2012, 7, 1), date(2021, 1, 1)]
    assert {item["source"] for item in timeline} == {ih.CSRC_INDUSTRY_SOURCE}


# ─── 3. 查询 ─────────────────────────────────────────────────────

def test_read_industry_timeline_sorted_and_filtered_by_source(duck: DuckDBStore) -> None:
    ih.upsert_industry_history(
        duck,
        [{"stock_code": "600519", "effective_date": date(2012, 7, 1),
          "level1": "制造业", "level2": "C15"}],
        source=ih.CSMAR_INDUSTRY_SOURCE,
    )
    ih.upsert_industry_history(
        duck,
        [{"stock_code": "600519", "effective_date": date(2021, 1, 1),
          "level1": "制造业", "level2": "C15"}],
        source=ih.CSRC_INDUSTRY_SOURCE,
    )
    timeline = ih.read_industry_timeline(duck, "600519")
    assert [item["effective_date"] for item in timeline] == [date(2012, 7, 1), date(2021, 1, 1)]
    csrc_only = ih.read_industry_timeline(duck, "600519", source=ih.CSRC_INDUSTRY_SOURCE)
    assert len(csrc_only) == 1
