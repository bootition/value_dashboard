"""ETF 交易记录 Excel 导入回归（2026-09-05）

覆盖：四 sheet 解析、日期归一、方向映射、跟踪指数/主指标初始映射、
手续费入账、幂等跳过、dry-run 不写库。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.core.etf_import import import_etf_xlsx, preview_etf_xlsx
from app.core.etf_strategy import position_summary
from app.core.storage.sqlite_store import SQLiteStore


@pytest.fixture
def xlsx_path(tmp_path: Path) -> Path:
    trades = pd.DataFrame([
        {"交易日期": "20251230", "ETF代码": "512170", "ETF名称": "医疗ETF",
         "方向": "买入", "成交价格": 0.344, "成交份额": 600, "成交金额": 206.4,
         "手续费": 0.1, "备注": None},
        {"交易日期": "20260202", "ETF代码": "512690", "ETF名称": "酒ETF",
         "方向": "买入", "成交价格": 0.553, "成交份额": 100, "成交金额": 55.3,
         "手续费": 0.1, "备注": "首笔"},
    ])
    cash = pd.DataFrame([
        {"日期": "2026-03-23 00:00:00", "类型(入金/出金)": "入金", "金额": 493.72, "备注": None},
        {"日期": "20260701", "类型(入金/出金)": "入金", "金额": 300.0, "备注": None},
    ])
    board = pd.DataFrame([
        {"字段": "ETF策略总资产(手填/外链)", "值": 4100.99, "资金统计": "累计入金",
         "Unnamed: 3": 1293.72},
    ])
    meta = pd.DataFrame([
        {"ETF代码": "512170", "ETF名称": "医疗ETF", "行业名称": "医药医疗", "备注": None},
        {"ETF代码": "512690", "ETF名称": "酒ETF", "行业名称": "大消费", "备注": None},
        {"ETF代码": "512880", "ETF名称": "证券ETF", "行业名称": "金融", "备注": None},
    ])
    path = tmp_path / "trades.xlsx"
    with pd.ExcelWriter(path) as writer:
        trades.to_excel(writer, sheet_name="交易流水", index=False)
        cash.to_excel(writer, sheet_name="资金流水", index=False)
        board.to_excel(writer, sheet_name="持仓看板", index=False)
        meta.to_excel(writer, sheet_name="ETF基础信息", index=False)
    return path


def test_preview_parses_all_sheets(xlsx_path: Path) -> None:
    preview = preview_etf_xlsx(xlsx_path)
    assert len(preview["trades"]) == 2
    assert preview["trades"][0]["trade_date"] == "2025-12-30"
    assert preview["trades"][0]["direction"] == "buy"
    assert len(preview["cash_flows"]) == 2
    assert preview["cash_flows"][0]["direction"] == "in"
    assert preview["total_assets"] == pytest.approx(4100.99)
    assert len(preview["metas"]) == 3
    assert preview["issues"] == []


def test_import_is_idempotent_and_maps_track_index(
    xlsx_path: Path, sqlite_store: SQLiteStore,
) -> None:
    first = import_etf_xlsx(sqlite_store, xlsx_path)
    assert first["trades_inserted"] == 2
    assert first["cash_inserted"] == 2
    assert first["metas_written"] == 3
    assert first["total_assets"] == pytest.approx(4100.99)

    second = import_etf_xlsx(sqlite_store, xlsx_path)
    assert second["trades_inserted"] == 0
    assert second["trades_skipped"] == 2
    assert second["cash_inserted"] == 0

    position = position_summary(sqlite_store, "512170")
    assert position["shares"] == pytest.approx(600)
    assert position["cost_basis"] == pytest.approx(206.5), "手续费必须计入成本"

    metas = {row["etf_code"]: row for row in sqlite_store.query("SELECT * FROM etf_meta")}
    assert metas["512690"]["track_index_code"] == "SW801120"
    assert metas["512880"]["primary_metric"] == "pb", "证券等强周期默认主指标为 PB"
    assert metas["512170"]["industry_group"] == "医药医疗"

    from app.core.etf_strategy import get_setting
    assert get_setting(sqlite_store, "total_assets") == "4100.99"


def test_dry_run_writes_nothing(xlsx_path: Path, sqlite_store: SQLiteStore) -> None:
    report = import_etf_xlsx(sqlite_store, xlsx_path, dry_run=True)
    assert report["dry_run"] is True
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM etf_trades")[0]["c"] == 0
    assert sqlite_store.query("SELECT COUNT(*) AS c FROM etf_meta")[0]["c"] == 0
