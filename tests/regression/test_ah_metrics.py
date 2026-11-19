"""A+H 估值/股息率计算回归（reports/120 J12）。

覆盖任务书 D 节要求的 6 组：
- h_share_count（含总股本 < 流通股本的异常、独立基准偏差、strict 模式）
- 市值拆分（手工算例）
- AH 溢价
- 股息率恒等式（含 5% 容差边界）
- `_asof` 日期对齐（H 价早于 A 价、FX 缺失）
- build_ah_snapshot 缺数据时 reason 正确

2026-11-16 追加（模块 docstring §1–§3 的口径裁决）：
- H 股市值 `h_share_market_cap_cny`（= H 价 × H 股数 × 汇率，单位元）手工算例；
- `identity_status` 四态（ok / mismatch / not_comparable / insufficient_data）；
- A 侧缺数据时 H 侧仍出数（各算各的，不得整体返回 None）；
- 断言 `build_ah_snapshot` 不产出行业口径 `total_market_cap`，也不调用
  `app/core/indicators/calculator.py` 的市值计算。

全部使用 `duckdb_store` fixture（隔离在 --basetemp 下），不触正式库。
"""

from __future__ import annotations

import math
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from app.core.ah_metrics import (
    _asof,
    ah_dividend_yield,
    ah_market_cap,
    build_ah_snapshot,
    h_share_count,
)
from app.core.storage.duckdb_store import DuckDBStore
from tests.support.seed import Seed

MAPPING = {
    "601398": {
        "hk_code": "01398",
        "hk_name": "工商银行",
        "a_name": "工商银行",
        "match_type": "exact_name",
    }
}


# ─── 造数工具 ──────────────────────────────────────────────────────


def _seed_meta(
    duck: DuckDBStore,
    code: str,
    name: str,
    total_shares: int | None,
    circ_shares: int | None,
) -> None:
    duck.write_query(
        """INSERT INTO stock_meta
               (stock_code, name, exchange, is_listed, total_shares, circ_shares)
           VALUES (?, ?, 'SSE', TRUE, ?, ?)
           ON CONFLICT (stock_code) DO UPDATE SET
               name = excluded.name, is_listed = TRUE,
               total_shares = excluded.total_shares,
               circ_shares = excluded.circ_shares""",
        [code, name, total_shares, circ_shares],
    )


def _seed_a_price(duck: DuckDBStore, code: str, day: str, close: float) -> None:
    duck.write_query(
        """INSERT INTO price_daily_raw (stock_code, trade_date, close, volume)
           VALUES (?, ?, ?, 1000)
           ON CONFLICT (stock_code, trade_date) DO UPDATE SET close = excluded.close""",
        [code, day, close],
    )


def _seed_h_price(duck: DuckDBStore, hk_code: str, day: str, close: float) -> None:
    duck.write_query(
        """INSERT INTO hk_price_daily (stock_code, trade_date, close, source, fetched_at)
           VALUES (?, ?, ?, 'akshare_hk', CURRENT_TIMESTAMP)
           ON CONFLICT (stock_code, trade_date) DO UPDATE SET close = excluded.close""",
        [hk_code, day, close],
    )


def _seed_fx(duck: DuckDBStore, day: str, rate: float) -> None:
    duck.write_query(
        """INSERT INTO fx_rate_daily (pair, trade_date, rate, source, fetched_at)
           VALUES ('HKDCNY', ?, ?, 'boc_sina', CURRENT_TIMESTAMP)
           ON CONFLICT (pair, trade_date) DO UPDATE SET rate = excluded.rate""",
        [day, rate],
    )


def _seed_a_dividend(duck: DuckDBStore, code: str, day: str, dps: float | None) -> None:
    # 原来是 `ON CONFLICT (stock_code, ex_date) DO UPDATE SET dividend_per_share`：
    # DuckDB 的 `INSERT OR REPLACE` 同样是"只更新本次提供的列"的 upsert，而这四列
    # 每次都全给（公告日 = 除权日）→ `overwrite=True` 语义等价。
    Seed(duck).dividends(code, [(day, day, dps)], overwrite=True)


def _seed_h_dividend(
    duck: DuckDBStore,
    hk_code: str,
    day: str,
    *,
    hkd: float | None,
    cny: float | None,
) -> None:
    duck.write_query(
        """INSERT INTO hk_dividends
               (stock_code, ex_date, plan_explain, dividend_per_share_hkd,
                dividend_per_share_cny, source, fetch_time, raw_response_hash,
                confidence, batch_id)
           VALUES (?, ?, ?, ?, ?, 'eastmoney_hk_dividend', CURRENT_TIMESTAMP,
                   'test-hash', 'approximate', 'test-batch')""",
        [hk_code, day, f"plan-{day}", hkd, cny],
    )


def _seed_standard_inputs(duck: DuckDBStore) -> None:
    """601398/01398：A 价 10（2026-01-05），H 价 8.2（2026-01-05），FX 0.9（2026-01-02）。"""
    _seed_meta(duck, "601398", "工商银行", 100, 60)
    _seed_a_price(duck, "601398", "2026-01-05", 10.0)
    _seed_h_price(duck, "01398", "2026-01-05", 8.2)
    _seed_fx(duck, "2026-01-02", 0.9)


# ─── h_share_count ─────────────────────────────────────────────────


def test_h_share_count_derives_icbc_exactly() -> None:
    # 工行实测：356,406,257,089 − 269,612,212,539 = 86,794,044,550（精确）
    assert h_share_count(356_406_257_089, 269_612_212_539) == 86_794_044_550.0


def test_h_share_count_total_equals_circ_is_zero() -> None:
    assert h_share_count(100, 100) == 0.0


def test_h_share_count_rejects_total_below_circ() -> None:
    assert h_share_count(100, 120) is None


@pytest.mark.parametrize(
    ("total", "circ"),
    [
        (None, 10),
        (100, None),
        (float("nan"), 10),
        (100, float("inf")),
        (0, 0),
        (-100, 10),
        (100, -10),
    ],
)
def test_h_share_count_invalid_inputs_return_none(total: object, circ: object) -> None:
    assert h_share_count(total, circ) is None


def test_h_share_count_known_reference_guards_overstatement() -> None:
    # 中国银行推导偏高 33%、建行偏高 4.8%：有独立基准时不应静默返回
    assert h_share_count(
        322_212_411_814, 210_765_514_846, reference_h_shares=83_622_276_395,
    ) is None
    assert h_share_count(
        261_600_000_000, 9_593_650_000, reference_h_shares=240_417_319_880,
    ) is None
    # 工行与基准一致 → 保留
    assert h_share_count(
        356_406_257_089, 269_612_212_539, reference_h_shares=86_794_044_550,
    ) == 86_794_044_550.0


def test_h_share_count_strict_without_reference_returns_none() -> None:
    # |偏差| 无法判定的策略：strict 且无独立基准 → None
    assert h_share_count(100, 60, strict=True) is None


# ─── 市值拆分 / AH 溢价 ─────────────────────────────────────────────


def test_ah_market_cap_manual_example() -> None:
    result = ah_market_cap(10.0, 8.0, 0.9, total_shares=100, circ_shares=60)
    assert result["reason"] is None
    assert result["a_share_market_cap"] == pytest.approx(600.0)
    assert result["h_share_market_cap_cny"] == pytest.approx(288.0)
    assert result["total_market_cap_ah"] == pytest.approx(888.0)
    assert result["ah_premium_pct"] == pytest.approx((10.0 / (8.0 * 0.9) - 1.0) * 100.0)
    assert result["h_shares"] == pytest.approx(40.0)
    assert result["h_price_cny"] == pytest.approx(7.2)


def test_ah_market_cap_negative_premium_when_a_cheaper() -> None:
    result = ah_market_cap(8.0, 10.0, 1.0, total_shares=100, circ_shares=60)
    assert result["ah_premium_pct"] == pytest.approx(-20.0)
    assert result["a_share_market_cap"] == pytest.approx(480.0)
    assert result["h_share_market_cap_cny"] == pytest.approx(400.0)
    assert result["total_market_cap_ah"] == pytest.approx(880.0)


def test_ah_market_cap_missing_h_shares_reason() -> None:
    result = ah_market_cap(10.0, 8.0, 0.9, total_shares=100, circ_shares=120)
    assert result["reason"] == "missing_h_shares"
    assert result["total_market_cap_ah"] is None
    assert result["ah_premium_pct"] is None


@pytest.mark.parametrize(
    ("close_a", "close_h", "fx", "reason"),
    [
        (0, 8, 0.9, "invalid_price"),
        (10, None, 0.9, "invalid_price"),
        (10, 8, 0, "invalid_fx"),
        (10, 8, float("nan"), "invalid_fx"),
    ],
)
def test_ah_market_cap_invalid_inputs(
    close_a: object, close_h: object, fx: object, reason: str,
) -> None:
    result = ah_market_cap(close_a, close_h, fx, total_shares=100, circ_shares=60)
    assert result["reason"] == reason
    assert result["a_share_market_cap"] is None


# ─── 股息率 / 恒等式 ───────────────────────────────────────────────


def test_ah_dividend_yield_identity_holds_for_equal_dps() -> None:
    result = ah_dividend_yield(0.5, 0.5, 10.0, 8.0, 0.9)
    assert result["reason"] is None
    assert result["a_yield"] == pytest.approx(0.05)
    assert result["h_yield"] == pytest.approx(0.5 / 7.2)
    # H = A × (1 + premium)
    assert result["h_yield"] == pytest.approx(
        result["a_yield"] * (1 + result["ah_premium_pct"] / 100.0)
    )
    assert result["identity_deviation_pct"] == pytest.approx(0.0, abs=1e-9)
    assert result["identity_ok"] is True
    assert result["ah_yield_gap"] == pytest.approx(0.05 - 0.5 / 7.2)
    assert result["h_yield_after_tax"] == pytest.approx(result["h_yield"] * 0.8)
    assert result["tax_rate"] == pytest.approx(0.20)
    assert result["window"] == "ttm_12m_implemented"


@pytest.mark.parametrize(
    ("div_h", "expected_ok"),
    [
        (0.52, True),   # +4%，容差内
        (0.53, False),  # +6%，超过 5%
        (0.48, True),   # −4%
        (0.47, False),  # −6%
    ],
)
def test_ah_dividend_yield_identity_tolerance(div_h: float, expected_ok: bool) -> None:
    result = ah_dividend_yield(0.5, div_h, 10.0, 8.0, 0.9)
    assert result["identity_ok"] is expected_ok
    if not expected_ok:
        assert result["identity_deviation_pct"] > 5.0


def test_ah_dividend_yield_hkd_basis_uses_explicit_hkd_dividend() -> None:
    result = ah_dividend_yield(0.5, 0.45, 10.0, 8.0, 0.9, div_h_ps_hkd=0.5)
    assert result["h_yield_hkd"] == pytest.approx(0.5 / 8.0)
    assert result["h_yield"] == pytest.approx(0.45 / 7.2)


def test_ah_dividend_yield_tax_rate_is_a_parameter() -> None:
    zero_tax = ah_dividend_yield(0.5, 0.5, 10.0, 8.0, 0.9, tax_rate=0.0)
    high_tax = ah_dividend_yield(0.5, 0.5, 10.0, 8.0, 0.9, tax_rate=0.3)
    assert zero_tax["h_yield_after_tax"] == pytest.approx(zero_tax["h_yield"])
    assert high_tax["h_yield_after_tax"] == pytest.approx(high_tax["h_yield"] * 0.7)
    invalid = ah_dividend_yield(0.5, 0.5, 10.0, 8.0, 0.9, tax_rate=1.2)
    assert invalid["reason"] == "invalid_tax_rate"


def test_ah_dividend_yield_zero_dividend_is_legal() -> None:
    result = ah_dividend_yield(0.0, 0.0, 10.0, 8.0, 0.9)
    assert result["reason"] is None
    assert result["a_yield"] == 0.0
    assert result["h_yield"] == 0.0
    assert result["identity_ok"] is True


def test_ah_dividend_yield_missing_a_dividend_keeps_h_side() -> None:
    """A 侧缺分红只让 A 侧为 None，H 侧照常出数（各算各的，模块 docstring §3）。

    2026-11-16 语义变更：旧实现两侧任一缺失即整体返回 None 且
    ``reason='missing_dividend'``——本用例原先断言的就是那个旧语义。
    """
    result = ah_dividend_yield(None, 0.5, 10.0, 8.0, 0.9)
    assert result["reason"] == "missing_a_dividend"
    assert result["a_yield"] is None
    assert result["h_yield"] == pytest.approx(0.5 / (8.0 * 0.9))
    assert result["h_yield_after_tax"] == pytest.approx(0.5 / (8.0 * 0.9) * 0.8)
    assert result["ah_yield_gap"] is None
    assert result["identity_status"] == "insufficient_data"


# ─── _asof 日期对齐 ─────────────────────────────────────────────────


def test_asof_takes_latest_not_after_target(duckdb_store: DuckDBStore) -> None:
    _seed_h_price(duckdb_store, "01398", "2026-01-02", 8.0)
    _seed_h_price(duckdb_store, "01398", "2026-01-05", 8.2)
    _seed_h_price(duckdb_store, "01398", "2026-01-10", 9.0)

    row = _asof(duckdb_store, "hk_price_daily", "01398", date(2026, 1, 7))
    assert row is not None
    assert row["value"] == pytest.approx(8.2)
    assert row["as_of_date"] == date(2026, 1, 5)
    assert row["staleness_days"] == 2


def test_asof_exact_date_and_before_history(duckdb_store: DuckDBStore) -> None:
    _seed_h_price(duckdb_store, "01398", "2026-01-05", 8.2)
    assert _asof(duckdb_store, "hk_price_daily", "01398", "2026-01-05")["value"] == 8.2
    assert _asof(duckdb_store, "hk_price_daily", "01398", "2026-01-04") is None


def test_asof_fx_missing_returns_none(duckdb_store: DuckDBStore) -> None:
    _seed_fx(duckdb_store, "2026-01-02", 0.9)
    assert _asof(duckdb_store, "fx_rate_daily", "HKDCNY", "2026-01-01") is None
    assert _asof(duckdb_store, "fx_rate_daily", "USDCNY", "2026-01-05") is None


def test_asof_rejects_unknown_relation(duckdb_store: DuckDBStore) -> None:
    with pytest.raises(ValueError, match="unsupported asof relation"):
        _asof(duckdb_store, "price_daily_raw", "601398", "2026-01-05")


def test_asof_rejects_invalid_date(duckdb_store: DuckDBStore) -> None:
    with pytest.raises(ValueError, match="invalid as_of"):
        _asof(duckdb_store, "fx_rate_daily", "HKDCNY", "not-a-date")


# ─── build_ah_snapshot ──────────────────────────────────────────────


def test_build_snapshot_uses_h_price_not_later_than_a_price(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_standard_inputs(duckdb_store)
    _seed_a_price(duckdb_store, "601398", "2026-01-02", 9.0)
    _seed_a_price(duckdb_store, "601398", "2026-01-10", 11.0)
    _seed_h_price(duckdb_store, "01398", "2026-01-20", 9.0)  # 晚于 A 价，不得使用
    _seed_a_dividend(duckdb_store, "601398", "2025-06-30", 0.5)
    _seed_a_dividend(duckdb_store, "601398", "2024-01-01", 9.0)  # 窗口外
    _seed_h_dividend(duckdb_store, "01398", "2025-06-25", hkd=0.55, cny=0.5)

    rows = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok", row["reason"]
    assert row["a_price_date"] == date(2026, 1, 5)
    assert row["a_close"] == pytest.approx(10.0)
    assert row["h_price_date"] == date(2026, 1, 5)  # Jan 20 被排除
    assert row["h_close"] == pytest.approx(8.2)
    assert row["fx_hkd_cny"] == pytest.approx(0.9)
    assert row["fx_date"] == date(2026, 1, 2)
    assert row["h_shares"] == pytest.approx(40.0)
    assert row["h_shares_confidence"] == "derived"
    assert row["a_share_market_cap"] == pytest.approx(600.0)
    assert row["h_share_market_cap_cny"] == pytest.approx(8.2 * 40 * 0.9)
    assert row["total_market_cap_ah"] == pytest.approx(600.0 + 295.2)
    assert row["ah_premium_pct"] == pytest.approx((10.0 / 7.38 - 1.0) * 100.0)
    assert row["div_a_ps_ttm"] == pytest.approx(0.5)
    assert row["div_h_ps_cny_ttm"] == pytest.approx(0.5)
    assert row["window"]["start"] == date(2025, 1, 5)
    assert row["window"]["end"] == date(2026, 1, 5)
    assert row["window"]["a_events"] == 1
    assert row["window"]["h_events"] == 1
    assert row["identity_ok"] is True


def test_build_snapshot_uses_h_price_earlier_than_a_price(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_standard_inputs(duckdb_store)
    duckdb_store.write_query("DELETE FROM hk_price_daily")
    _seed_h_price(duckdb_store, "01398", "2026-01-02", 8.0)
    _seed_a_dividend(duckdb_store, "601398", "2025-06-30", 0.5)
    _seed_h_dividend(duckdb_store, "01398", "2025-06-25", hkd=0.55, cny=0.5)

    row = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )[0]
    assert row["status"] == "ok", row["reason"]
    assert row["h_price_date"] == date(2026, 1, 2)
    assert row["h_close"] == pytest.approx(8.0)
    assert row["h_price_staleness_days"] == 3
    assert row["fx_staleness_days"] == 3


def test_build_snapshot_missing_h_price_reason(duckdb_store: DuckDBStore) -> None:
    _seed_meta(duckdb_store, "601398", "工商银行", 100, 60)
    _seed_a_price(duckdb_store, "601398", "2026-01-05", 10.0)
    _seed_fx(duckdb_store, "2026-01-02", 0.9)
    _seed_a_dividend(duckdb_store, "601398", "2025-06-30", 0.5)

    row = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )[0]
    assert row["reason"] == "missing_h_price"
    assert row["status"] == "incomplete"
    assert row["total_market_cap_ah"] is None
    assert row["ah_premium_pct"] is None
    assert row["a_yield"] is None


def test_build_snapshot_missing_fx_reason(duckdb_store: DuckDBStore) -> None:
    _seed_meta(duckdb_store, "601398", "工商银行", 100, 60)
    _seed_a_price(duckdb_store, "601398", "2026-01-05", 10.0)
    _seed_h_price(duckdb_store, "01398", "2026-01-05", 8.2)
    _seed_a_dividend(duckdb_store, "601398", "2025-06-30", 0.5)
    _seed_h_dividend(duckdb_store, "01398", "2025-06-25", hkd=0.55, cny=0.5)

    row = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )[0]
    assert row["reason"] == "missing_fx"
    assert row["h_close"] == pytest.approx(8.2)
    assert row["fx_hkd_cny"] is None


def test_build_snapshot_missing_h_shares_reason(duckdb_store: DuckDBStore) -> None:
    _seed_meta(duckdb_store, "601398", "工商银行", 100, 120)  # 流通 > 总股本
    _seed_a_price(duckdb_store, "601398", "2026-01-05", 10.0)
    _seed_h_price(duckdb_store, "01398", "2026-01-05", 8.2)
    _seed_fx(duckdb_store, "2026-01-02", 0.9)

    row = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )[0]
    assert row["reason"] == "missing_h_shares"
    assert row["h_shares"] is None
    assert row["total_market_cap_ah"] is None


def test_build_snapshot_missing_a_price_reason(duckdb_store: DuckDBStore) -> None:
    _seed_meta(duckdb_store, "601398", "工商银行", 100, 60)
    row = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )[0]
    assert row["reason"] == "missing_a_price"
    assert row["a_close"] is None


def test_build_snapshot_missing_h_dividend_reason(duckdb_store: DuckDBStore) -> None:
    _seed_standard_inputs(duckdb_store)
    _seed_a_dividend(duckdb_store, "601398", "2025-06-30", 0.5)
    # H 侧零行：A 有分红时判定为抓取失败而不是「H 股不分红」
    row = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )[0]
    assert row["reason"] == "missing_h_dividend"
    assert row["div_h_ps_cny_ttm"] is None


def test_build_snapshot_no_dividend_is_legal_and_computable(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_standard_inputs(duckdb_store)
    row = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )[0]
    assert row["status"] == "ok", row["reason"]
    assert row["div_a_ps_ttm"] == 0.0
    assert row["div_h_ps_cny_ttm"] == 0.0
    assert row["a_yield"] == 0.0
    assert row["h_yield"] == 0.0
    assert row["identity_ok"] is True


def test_build_snapshot_converts_hkd_dividend_with_ex_date_fx(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_standard_inputs(duckdb_store)
    # A 锚点日 fx=0.9；H 分红除净日单独有 0.8 的汇率 → 必须用除净日汇率
    _seed_fx(duckdb_store, "2025-06-25", 0.8)
    _seed_a_dividend(duckdb_store, "601398", "2025-06-30", 0.4)
    _seed_h_dividend(duckdb_store, "01398", "2025-06-25", hkd=0.5, cny=None)

    row = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )[0]
    assert row["status"] == "ok", row["reason"]
    assert row["div_h_ps_cny_ttm"] == pytest.approx(0.4)
    assert row["h_dividend_converted_events"] == 1
    assert row["h_yield"] == pytest.approx(0.4 / (8.2 * 0.9))
    assert row["identity_ok"] is True


def test_build_snapshot_resolves_mapping_from_stock_meta(
    duckdb_store: DuckDBStore,
) -> None:
    # 不传 mapping：走 app/core/ah_hk_mapping.py 的真实映射（601398 名称精确匹配）
    _seed_meta(duckdb_store, "601398", "工商银行", 100, 60)
    rows = build_ah_snapshot(duckdb_store, as_of=date(2026, 1, 6))
    assert len(rows) == 1
    assert rows[0]["stock_code"] == "601398"
    assert rows[0]["hk_code"] == "01398"
    assert rows[0]["reason"] == "missing_a_price"


def test_build_snapshot_stock_codes_filter(duckdb_store: DuckDBStore) -> None:
    _seed_standard_inputs(duckdb_store)
    assert build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
        stock_codes=["600000"],
    ) == []


def test_build_snapshot_does_not_touch_indicator_snapshot(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_standard_inputs(duckdb_store)
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot
               (stock_code, report_date, latest_close, total_market_cap)
           VALUES ('601398', '2026-09-30', 10.0, 1000.0)"""
    )
    build_ah_snapshot(duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING)
    row = duckdb_store.read_query(
        """SELECT total_market_cap, latest_close FROM indicator_snapshot
           WHERE stock_code = '601398'"""
    )[0]
    assert row["total_market_cap"] == pytest.approx(1000.0)
    assert row["latest_close"] == pytest.approx(10.0)


def test_units_are_declared_on_success(duckdb_store: DuckDBStore) -> None:
    _seed_standard_inputs(duckdb_store)
    row = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )[0]
    assert row["units"] == {
        "market_cap": "CNY_yuan",
        "premium": "percent_points",
        "yields": "decimal_fraction",
        "dividend_per_share": "CNY_yuan_per_share",
    }
    assert math.isfinite(row["total_market_cap_ah"])


# ─── 适配器列名归一 / 汇率单位（不联网，用 fake akshare） ─────────────


class _FakeAkshare:
    def __init__(
        self,
        *,
        hist: object = None,
        hist_error: Exception | None = None,
        daily: object = None,
        fx: object = None,
    ) -> None:
        self.hist = hist
        self.hist_error = hist_error
        self.daily = daily
        self.fx = fx

    def stock_hk_hist(self, **_kwargs: object) -> object:
        if self.hist_error is not None:
            raise self.hist_error
        return self.hist

    def stock_hk_daily(self, **_kwargs: object) -> object:
        return self.daily

    def currency_boc_sina(self, **_kwargs: object) -> object:
        return self.fx


def test_fetch_hk_daily_parses_eastmoney_columns() -> None:
    import pandas as pd

    from app.core.adapters.hk_price_adapter import fetch_hk_daily

    frame = pd.DataFrame({
        "日期": ["2026-11-12", "2026-11-13"],
        "开盘": [7.80, 7.85],
        "收盘": [7.83, 7.92],
        "最高": [7.90, 7.95],
    })
    # 2026-11-15：新增腾讯源后 `auto` 会**先试**腾讯（1,500 根/只、0.36s/只），
    # 本用例只测"东财列名解析"，因此显式 prefer="eastmoney" 走 akshare 分支。
    result = fetch_hk_daily(
        "01398", start_date="2026-11-01", end_date="2026-11-14",
        prefer="eastmoney", ak_module=_FakeAkshare(hist=frame),
    )
    assert result.error is None
    assert result.upstream == "eastmoney_stock_hk_hist"
    assert [row["close"] for row in result.rows] == [7.83, 7.92]


def test_fetch_hk_daily_falls_back_to_sina_columns() -> None:
    import pandas as pd

    from app.core.adapters.hk_price_adapter import fetch_hk_daily

    frame = pd.DataFrame({
        "date": [pd.Timestamp("2026-11-12"), pd.Timestamp("2026-11-13")],
        "close": [7.83, 7.92],
        "volume": [1, 2],
    })
    # 本用例测新浪列名解析与降级备注，显式 prefer="sina"（auto 会先试腾讯）。
    result = fetch_hk_daily(
        "01398",
        start_date="2026-11-01",
        end_date="2026-11-14",
        prefer="sina",
        ak_module=_FakeAkshare(
            hist_error=ConnectionError("RemoteDisconnected"), daily=frame,
        ),
    )
    assert result.error is None
    assert result.upstream == "sina_stock_hk_daily"
    assert len(result.rows) == 2


def test_fetch_hk_daily_returns_empty_without_fabrication(monkeypatch) -> None:
    from app.core.adapters import hk_price_adapter
    from app.core.adapters.hk_price_adapter import fetch_hk_daily

    # 腾讯源是新加的 auto 首选，这里打桩成空，验证"三源全失败"时不伪造任何价格。
    monkeypatch.setattr(hk_price_adapter, "_fetch_tencent_hk_daily", lambda *a, **k: [])
    result = fetch_hk_daily(
        "01398",
        ak_module=_FakeAkshare(
            hist_error=ConnectionError("blocked"), daily=None,
        ),
    )
    assert result.rows == []
    assert result.error is not None
    assert "all_sources_failed" in result.error


def test_fetch_fx_divides_boc_per_100_hkd_quotes() -> None:
    import pandas as pd

    from app.core.adapters.hk_price_adapter import fetch_hkd_cny_history

    # 源单位是「人民币元 / 100 港元」；折算价优先，缺失时回退央行中间价
    frame = pd.DataFrame({
        "日期": [1793491200000, 1793577600000],
        "中行汇买价": [84.88, 84.90],
        "中行钞买价": [84.88, 84.90],
        "中行钞卖价/汇卖价": [85.22, 85.24],
        "央行中间价": [None, 91.17],
        "中行折算价": [85.60, None],
    })
    result = fetch_hkd_cny_history(
        start_date="2026-11-01", end_date="2026-11-30",
        ak_module=_FakeAkshare(fx=frame),
    )
    assert result.error is None
    assert result.rows[0]["rate"] == pytest.approx(0.8560)
    assert result.rows[1]["rate"] == pytest.approx(0.9117)
    assert result.mid_source_counts == {"中行折算价": 1, "央行中间价": 1}


def test_to_date_handles_yyyymmdd_and_epoch_ms() -> None:
    from app.core.adapters.hk_price_adapter import to_date

    assert to_date("20261113") == date(2026, 11, 13)
    assert to_date(1794528000000) == date(2026, 11, 13)
    assert to_date("not-a-date") is None


def test_h_share_count_reference_zero_requires_zero_derived() -> None:
    assert h_share_count(100, 100, reference_h_shares=0) == 0.0
    assert h_share_count(100, 60, reference_h_shares=0) is None


def test_asof_tolerates_missing_table_as_missing_data(
    duckdb_store: DuckDBStore,
) -> None:
    duckdb_store.write_query("DROP TABLE fx_rate_daily")
    assert _asof(duckdb_store, "fx_rate_daily", "HKDCNY", "2026-01-05") is None


# ─── 2026-11-16：H 股市值明确字段 + 恒等式降级为参考值 ──────────────────
#
# 三条背景（已核实，见 app/core/ah_metrics.py 模块 docstring §1–§3）：
# 1. 行业口径「总市值」= A 价 × 总股本，与现有 indicator_snapshot.total_market_cap
#    一致 → 本模块不读不写、不切换；
# 2. 要补的是单独一列 H 股市值 h_share_market_cap_cny（元）；
# 3. AH 股息率各算各的，恒等式仅在两端分红笔数一致时作为参考值，永不当门禁。


AH_ROW_EXPLICIT_FIELDS = (
    "h_share_market_cap_cny",
    "ah_total_market_cap_cny",
    "identity_status",
    "identity_deviation_pct",
)


def test_h_share_market_cap_cny_manual_example_to_the_cent() -> None:
    """H 股市值 = H 价 × H 股数 × 汇率，单位人民币元，断言到分。

    用工行 601398 真实股本：总 356,406,257,089 − A 流通 269,612,212,539
    = H 股 86,794,044,550 股；H 价 5.77 港元 × 汇率 0.9153。
    """
    result = ah_market_cap(
        8.85,
        5.77,
        0.9153,
        total_shares=356_406_257_089,
        circ_shares=269_612_212_539,
    )
    assert result["reason"] is None
    assert result["h_shares"] == 86_794_044_550.0
    assert result["h_price_cny"] == pytest.approx(5.77 * 0.9153, rel=1e-12)
    expected = (
        Decimal("5.77") * Decimal("86794044550") * Decimal("0.9153")
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    value = result["h_share_market_cap_cny"]
    assert Decimal(str(round(value, 2))) == expected
    # 单位必须是「元」：≈4,583.84 亿元，不是「亿元/万元」（防 100 倍 / 1 万倍事故）
    assert 4_500e8 < value < 4_700e8


def test_ah_market_cap_ah_total_alias_is_sum_of_two_sides() -> None:
    result = ah_market_cap(10.0, 8.0, 0.9, total_shares=100, circ_shares=60)
    assert result["ah_total_market_cap_cny"] == pytest.approx(888.0)
    # 与历史键 total_market_cap_ah 同值；只是参考量，不是行业口径总市值
    assert result["ah_total_market_cap_cny"] == result["total_market_cap_ah"]
    assert result["ah_total_market_cap_cny"] == pytest.approx(
        result["a_share_market_cap"] + result["h_share_market_cap_cny"]
    )


def test_snapshot_exposes_h_share_market_cap_cny_explicitly(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_standard_inputs(duckdb_store)
    _seed_a_dividend(duckdb_store, "601398", "2025-06-30", 0.5)
    _seed_h_dividend(duckdb_store, "01398", "2025-06-25", hkd=0.55, cny=0.5)

    row = build_ah_snapshot(duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING)[0]
    for key in AH_ROW_EXPLICIT_FIELDS:
        assert key in row, key
    # H 股市值：H 价 8.2 港元 × H 股 40 股 × 汇率 0.9 = 295.20 元
    assert row["h_share_market_cap_cny"] == pytest.approx(295.2)
    assert row["h_share_market_cap_cny"] == pytest.approx(
        row["h_close"] * row["h_shares"] * row["fx_hkd_cny"]
    )
    assert row["ah_total_market_cap_cny"] == pytest.approx(
        row["a_share_market_cap"] + row["h_share_market_cap_cny"]
    )
    assert row["ah_total_market_cap_cny"] == row["total_market_cap_ah"]


def test_snapshot_rows_never_expose_industry_total_market_cap(
    duckdb_store: DuckDBStore,
) -> None:
    """行业口径 total_market_cap 只属于 indicator_snapshot，本模块不得产出该键。"""
    _seed_standard_inputs(duckdb_store)
    ok_row = build_ah_snapshot(duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING)[0]
    duckdb_store.write_query("DELETE FROM hk_price_daily")
    incomplete_row = build_ah_snapshot(
        duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING,
    )[0]

    assert ok_row["status"] == "ok"
    assert incomplete_row["status"] == "incomplete"
    for row in (ok_row, incomplete_row):
        assert "total_market_cap" not in row
        assert "circ_market_cap" not in row
        # 行结构稳定：缺数据行也有同一套键（便于前端/调用方统一取值）
        assert set(row) == set(ok_row)


def test_build_snapshot_does_not_call_indicator_calculator(
    duckdb_store: DuckDBStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_ah_snapshot 不得调用 indicator calculator 里的市值计算。"""
    from app.core.indicators import calculator as calculator_module

    calls: list[str] = []

    def _forbidden(name: str):
        def _stub(*_args: object, **_kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"{name} 不应被 build_ah_snapshot 调用")

        return _stub

    monkeypatch.setattr(
        calculator_module.IndicatorCalculator, "_calc_valuation", _forbidden("_calc_valuation"),
    )
    monkeypatch.setattr(
        calculator_module.IndicatorCalculator,
        "compute_snapshot_for_all",
        _forbidden("compute_snapshot_for_all"),
    )
    _seed_standard_inputs(duckdb_store)

    row = build_ah_snapshot(duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING)[0]
    assert row["status"] == "ok", row["reason"]
    assert row["ah_total_market_cap_cny"] is not None
    assert calls == []


def test_ah_metrics_module_does_not_import_indicator_calculator() -> None:
    """静态守卫：本模块永远不 import 指标主链（total_market_cap 的产地）。"""
    import app.core.ah_metrics as ah_metrics_module

    source = Path(ah_metrics_module.__file__).read_text(encoding="utf-8")
    assert "app.core.indicators" not in source
    assert "indicator_calculator" not in source


def test_identity_status_ok_when_counts_match_and_deviation_within_tolerance() -> None:
    result = ah_dividend_yield(0.5, 0.5, 10.0, 8.0, 0.9, a_events=1, h_events=1)
    assert result["identity_status"] == "ok"
    assert result["identity_ok"] is True
    assert result["identity_deviation_pct"] == pytest.approx(0.0, abs=1e-9)


def test_identity_status_mismatch_when_counts_match_but_identity_fails() -> None:
    result = ah_dividend_yield(0.5, 0.53, 10.0, 8.0, 0.9, a_events=1, h_events=1)
    assert result["identity_status"] == "mismatch"
    assert result["identity_ok"] is False
    assert result["identity_deviation_pct"] > 5.0
    # 各算各的：两侧股息率照常出数，reason 不得置位（不是错误）
    assert result["reason"] is None
    assert result["a_yield"] == pytest.approx(0.05)
    assert result["h_yield"] == pytest.approx(0.53 / (8.0 * 0.9))


def test_identity_status_not_comparable_when_event_counts_differ() -> None:
    result = ah_dividend_yield(0.5, 0.5, 10.0, 8.0, 0.9, a_events=1, h_events=2)
    assert result["identity_status"] == "not_comparable"
    assert result["identity_deviation_pct"] is None
    assert result["identity_ok"] is None
    assert result["reason"] is None  # 笔数不一致不是错误
    assert result["h_yield"] == pytest.approx(0.5 / (8.0 * 0.9))


def test_identity_status_insufficient_data_when_h_side_missing() -> None:
    """H 侧缺分红 → H 侧为 None，A 侧照常出数；恒等式判 insufficient_data。"""
    result = ah_dividend_yield(0.5, None, 10.0, 8.0, 0.9)
    assert result["reason"] == "missing_h_dividend"
    assert result["a_yield"] == pytest.approx(0.05)
    assert result["h_yield"] is None
    assert result["h_yield_after_tax"] is None
    assert result["identity_status"] == "insufficient_data"
    assert result["identity_deviation_pct"] is None
    assert result["identity_ok"] is None


def test_identity_deviation_pct_only_present_for_ok_and_mismatch() -> None:
    cases = (
        (0.5, 1, 1),   # ok
        (0.53, 1, 1),  # mismatch
        (0.5, 1, 2),   # not_comparable
        (0.5, 2, 1),   # not_comparable
    )
    for div_h, a_events, h_events in cases:
        result = ah_dividend_yield(
            0.5, div_h, 10.0, 8.0, 0.9, a_events=a_events, h_events=h_events,
        )
        status = result["identity_status"]
        assert (result["identity_deviation_pct"] is not None) is (status in {"ok", "mismatch"}), (
            status,
            result["identity_deviation_pct"],
        )


def test_snapshot_identity_not_comparable_keeps_row_usable(
    duckdb_store: DuckDBStore,
) -> None:
    """A 1 次 / H 2 次（正常现象）→ not_comparable，但行仍 status=ok 且字段齐全。"""
    _seed_standard_inputs(duckdb_store)
    _seed_a_dividend(duckdb_store, "601398", "2025-06-30", 0.5)
    _seed_h_dividend(duckdb_store, "01398", "2025-06-25", hkd=0.55, cny=0.5)
    _seed_h_dividend(duckdb_store, "01398", "2025-12-20", hkd=0.55, cny=0.5)

    row = build_ah_snapshot(duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING)[0]
    assert row["window"]["a_events"] == 1
    assert row["window"]["h_events"] == 2
    assert row["identity_status"] == "not_comparable"
    assert row["identity_deviation_pct"] is None
    assert row["identity_ok"] is None
    assert row["status"] == "ok", row["reason"]  # 绝不因恒等式不可比而丢弃
    assert row["h_share_market_cap_cny"] is not None
    assert row["a_yield"] == pytest.approx(0.5 / 10.0)
    assert row["h_yield"] == pytest.approx(1.0 / (8.2 * 0.9))  # H 自己 2 笔合计 1.0


def test_snapshot_identity_mismatch_keeps_row_usable(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_standard_inputs(duckdb_store)
    _seed_a_dividend(duckdb_store, "601398", "2025-06-30", 0.5)
    _seed_h_dividend(duckdb_store, "01398", "2025-06-25", hkd=0.59, cny=0.53)

    row = build_ah_snapshot(duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING)[0]
    assert row["identity_status"] == "mismatch"
    assert row["identity_deviation_pct"] > 5.0
    assert row["status"] == "ok", row["reason"]
    assert row["ah_total_market_cap_cny"] is not None


def test_snapshot_identity_insufficient_on_incomplete_row(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_meta(duckdb_store, "601398", "工商银行", 100, 60)
    _seed_a_price(duckdb_store, "601398", "2026-01-05", 10.0)
    _seed_fx(duckdb_store, "2026-01-02", 0.9)

    row = build_ah_snapshot(duckdb_store, as_of=date(2026, 1, 6), mapping=MAPPING)[0]
    assert row["reason"] == "missing_h_price"
    assert row["status"] == "incomplete"
    assert row["identity_status"] == "insufficient_data"
    assert row["identity_deviation_pct"] is None
