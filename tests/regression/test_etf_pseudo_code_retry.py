"""伪代码 `ALL_A` 不许进 ETF 行情/重试管道（2026-11-21 线上事故）。

事故现场：`etf_meta` 里 `ALL_A`（全A指数合成代码，`etf_fundamentals.ALL_A_CODE`）
`enabled=1`，ETF 行情步骤照抓 → 同花顺回 `code=3001: Fund not found`（正确）→
`_record_retry` 塞进 `retry_list` → **每轮重试、每轮失败** → 整轮永远 `partial`。
"""

from __future__ import annotations

import pytest

from app.core.etf_prices import (
    _is_definitive_not_found,
    is_price_fetchable,
)


@pytest.mark.parametrize(
    "code,fetchable",
    [
        ("ALL_A", False),          # 合成全A指数
        ("all_a", False),          # 大小写不敏感
        ("  ALL_A  ", False),      # 带空白
        ("159995", True),          # 真实深市 ETF
        ("510300", True),          # 真实沪市 ETF
        ("12345", False),          # 5 位非法
        ("1599956", False),        # 7 位非法
        ("ABCDEF", False),         # 非数字
        ("", False),
        (None, False),
    ],
)
def test_is_price_fetchable(code, fetchable) -> None:
    assert is_price_fetchable(code) is fetchable


@pytest.mark.parametrize(
    "error,definitive",
    [
        ("ths code=3001: Fund not found", True),
        ("Fund Not Found", True),
        ("no such fund", True),
        ("标的不存在", True),
        ("ReadTimeout: timed out", False),      # 瞬时故障 → 仍应重试
        ("ConnectError: [WinError 10054]", False),
        ("HITHINK_FINANCE_API_KEY missing", False),  # 由既有分支单独处理
        ("", False),
        (None, False),
    ],
)
def test_is_definitive_not_found(error, definitive) -> None:
    assert _is_definitive_not_found(error) is definitive


def test_pseudo_code_is_excluded_from_universe_selection(duckdb_store, sqlite_store) -> None:
    """`etf_meta` 里即使有 `ALL_A` 且 enabled，也不许进抓取清单。"""
    from app.core.etf_prices import EtfPriceUpdater

    sqlite_store.execute(
        "INSERT INTO etf_meta (etf_code, name, enabled, updated_at) "
        "VALUES ('ALL_A', '全A指数', 1, CURRENT_TIMESTAMP)"
    )
    sqlite_store.execute(
        "INSERT INTO etf_meta (etf_code, name, enabled, updated_at) "
        "VALUES ('159995', '芯片ETF', 1, CURRENT_TIMESTAMP)"
    )
    updater = EtfPriceUpdater(duck=duckdb_store, sqlite=sqlite_store)
    report = updater.update_all(max_etfs=10)
    assert "ALL_A" not in (report.get("etfs") or {}), report.get("etfs")
    assert report.get("targeted") == 1


def test_update_etf_on_pseudo_code_records_missing_not_retry(duckdb_store, sqlite_store) -> None:
    """直接点名伪代码：登记 missing、**不写 retry_list**、返回 missing。"""
    from app.core.etf_prices import EtfPriceUpdater

    sqlite_store.execute(
        "INSERT INTO etf_meta (etf_code, name, enabled, updated_at) "
        "VALUES ('ALL_A', '全A指数', 1, CURRENT_TIMESTAMP)"
    )
    # 先塞一条历史重试（复刻线上 33189 那条）
    sqlite_store.execute(
        """INSERT INTO retry_list (stock_code, data_type, adapter, error, retry_count, last_attempt, extra_json)
           VALUES ('ALL_A', 'etf_daily', 'ths', 'ths code=3001: Fund not found', 1, CURRENT_TIMESTAMP, '{}')"""
    )
    updater = EtfPriceUpdater(duck=duckdb_store, sqlite=sqlite_store)
    outcome = updater.update_etf("ALL_A")

    assert outcome["status"] == "missing"
    assert outcome.get("reason") == "pseudo_or_invalid_code"
    remaining = sqlite_store.query(
        "SELECT COUNT(*) AS n FROM retry_list WHERE stock_code = 'ALL_A'"
    )
    # 历史重试条目必须被清掉，且不许再写新的
    assert remaining[0]["n"] == 0
