"""价格域批量预取（依据 S10 / S11）的回归用例。

守的四件事
----------
1. **预取不改变结果**：改后（分批预取）与改前（逐股查询 / HEAD 实现）逐只逐列
   `==`，对拍股票数 ≥ 20、mismatch 必须是 0（同时跑 `scripts/
   compare_price_domain_before_after.py` 的 smoke）。
2. **miss 回落原路径**：缓存里没有这只股票 / 没有这个 as_of 时，必须回落原来的
   逐股 SQL（真的发出查询），而不是返回别的股票或别的日期的值。
3. **缓存不许跨批次存活**：批次结束（含异常）缓存必须清空；第二批必须看到批次
   之间写入的新数据（陈旧数据 = 假绿）。
4. **每类查询每批只发一次**：批量路径下 SQL 条数由"每股 13 条"降到"每批每类
   1 条 + 每股 1 条（market_cap 的 as-of 股本）"。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from app.core.indicators.calculator import IndicatorCalculator
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import Seed

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: smoke 对拍要求的股票数下限（任务书：≥20）。
SMOKE_CODES = [f"6000{index:02d}" for index in range(20, 40)]


def _load_compare_script() -> Any:
    """按文件路径导入对拍脚本（`scripts/` 不是包）。"""
    path = PROJECT_ROOT / "scripts" / "compare_price_domain_before_after.py"
    spec = importlib.util.spec_from_file_location("_vd_compare_price_domain", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _QueryCounter:
    """记录 `_read_query` 实际发出的 SQL（用来证明"每类只发一次 / 真的回落"）。"""

    def __init__(self, calculator: IndicatorCalculator) -> None:
        self.sqls: list[str] = []
        self._original = calculator._read_query

    def install(self, monkeypatch: pytest.MonkeyPatch, calculator: IndicatorCalculator) -> None:
        def counting(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
            self.sqls.append(" ".join(sql.split()))
            return self._original(sql, params)

        monkeypatch.setattr(calculator, "_read_query", counting)

    def count_containing(self, needle: str) -> int:
        return sum(1 for sql in self.sqls if needle in sql)

    def reset(self) -> None:
        self.sqls.clear()


@pytest.fixture
def price_domain_calculator(duckdb_store, sqlite_store) -> IndicatorCalculator:
    """3 只股票的隔离库：财报/行情/分红/股本/交易日历齐备。"""
    codes = ["600519", "000001", "300750"]
    with duckdb_store.write_connection() as connection:
        Seed(duckdb_store).join(connection).stocks([
            (code, code, {"exchange": "SSE"}) for code in codes
        ])
    for code in codes:
        insert_minimum_screenable_data(duckdb_store, code)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)
    return IndicatorCalculator(duck=duckdb_store, sqlite=sqlite_store)


def _report_date(calculator: IndicatorCalculator, code: str) -> Any:
    rows = calculator.duck.read_query(
        "SELECT report_date FROM indicator_snapshot WHERE stock_code = ?", [code],
    )
    assert rows, f"snapshot 缺少 {code}"
    return rows[0]["report_date"]


def _strip_timestamp(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "calculated_at"}


# ─── ① 对拍 smoke（≥20 只、mismatch=0）──────────────────────────────


def test_compare_script_smoke_twenty_stocks_zero_mismatch(
    duckdb_store, sqlite_store, database_paths, tmp_path: Path,
) -> None:
    """`scripts/compare_price_domain_before_after.py` 的 smoke：≥20 只逐列对拍。"""
    with duckdb_store.write_connection() as connection:
        Seed(duckdb_store).join(connection).stocks([
            (code, code, {"exchange": "SSE"}) for code in SMOKE_CODES
        ])
    for code in SMOKE_CODES:
        insert_minimum_screenable_data(duckdb_store, code)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)

    script = _load_compare_script()
    report = script.compare_price_domain(
        database_paths.duckdb_path,
        database_paths.sqlite_path,
        codes=SMOKE_CODES,
        baseline="auto",          # 能取到 HEAD 就用真·改前，取不到退 miss 回落路径
        use_temp_copy=False,      # fixture 本身就是校验过的隔离根，不必再拷一份
        temp_root=tmp_path / "compare-work",
    )
    comparison = report["comparison"]
    assert comparison["stocks_before"] >= 20
    assert comparison["mismatch_count"] == 0, comparison["mismatches"][:5]
    # 对拍必须真的覆盖到值，而不是"全 NULL 对全 NULL"
    assert comparison["non_null_by_column"]["latest_close"] >= 20
    assert comparison["non_null_by_column"]["ma250"] >= 20
    assert comparison["non_null_by_column"]["total_market_cap"] >= 20


def test_prefetch_path_matches_fallback_path_field_by_field(
    price_domain_calculator: IndicatorCalculator,
) -> None:
    """同一只股票：进预取批次 与 不进批次（走原逐股 SQL）结果逐列相等。"""
    calculator = price_domain_calculator
    codes = ["600519", "000001", "300750"]
    without = {
        code: _strip_timestamp(
            calculator.compute_price_fields_for_stock(code, _report_date(calculator, code)),
        )
        for code in codes
    }
    with calculator.price_domain_batch(
        codes, report_dates={code: _report_date(calculator, code) for code in codes},
    ):
        with_prefetch = {
            code: _strip_timestamp(
                calculator.compute_price_fields_for_stock(code, _report_date(calculator, code)),
            )
            for code in codes
        }
    for code in codes:
        assert with_prefetch[code].keys() == without[code].keys()
        for column, value in without[code].items():
            assert with_prefetch[code][column] == value, f"{code}.{column}"
            assert type(with_prefetch[code][column]) is type(value)


# ─── ② miss 回落原路径 ─────────────────────────────────────────────


def test_prefetch_miss_falls_back_to_original_query(
    price_domain_calculator: IndicatorCalculator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预取了 A，请求 B → 必须真的回落逐股查询，且结果与不预取时相同。"""
    calculator = price_domain_calculator
    report_dates = {code: _report_date(calculator, code) for code in ["000001", "300750"]}
    counter = _QueryCounter(calculator)
    counter.install(monkeypatch, calculator)

    baseline_ttm = calculator._get_ttm_data("300750", report_dates["300750"])
    baseline_financials = calculator._get_latest_financials("300750")
    baseline_technical = calculator._calc_technical("300750")

    with calculator.price_domain_batch(["000001"], report_dates={"000001": report_dates["000001"]}):
        counter.reset()
        # 300750 不在本批 → 每次都该真的发 SQL
        assert calculator._get_ttm_data("300750", report_dates["300750"]) == baseline_ttm
        assert calculator._get_latest_financials("300750") == baseline_financials
        assert calculator._calc_technical("300750") == baseline_technical
        assert counter.count_containing("FROM income_statement ic") >= 1
        assert counter.count_containing("FROM balance_sheet bs") >= 1
        assert counter.count_containing("FROM price_daily_raw raw") >= 1

        # 同一批内 000001 命中缓存 → 一条 SQL 都不该发
        counter.reset()
        calculator._get_ttm_data("000001", report_dates["000001"])
        calculator._get_latest_financials("000001")
        calculator._calc_technical("000001")
        assert counter.sqls == [], counter.sqls


def test_asof_parameter_mismatch_misses_cache(
    price_domain_calculator: IndicatorCalculator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预取用的是报告期 as_of；换一个 as_of 必须 miss 并回落，不能复用旧值。"""
    calculator = price_domain_calculator
    code = "600519"
    report_date = _report_date(calculator, code)
    counter = _QueryCounter(calculator)
    counter.install(monkeypatch, calculator)

    baseline_summary = calculator._get_dividend_summary(code, report_date)
    with calculator.price_domain_batch([code], report_dates={code: report_date}):
        counter.reset()
        # 与预取 as_of 一致 → 命中，不发 SQL
        assert calculator._get_ttm_data(code, report_date) == calculator._get_ttm_data(
            code, report_date,
        )
        counter.reset()
        assert calculator._get_ttm_data(code, report_date) == calculator._get_ttm_data(
            code, report_date,
        )

        counter.reset()
        earlier = "2000-01-01"              # 与预取 as_of 不同 → 必须回落
        assert calculator._get_ttm_data(code, earlier) == {}
        assert len(counter.sqls) == 1

        # 分红汇总是**故意**不预取的（多行聚合求和顺序不可复现，见 S10），
        # 所以它任何时候都走原逐股 SQL，但值必须与不预取时完全一致。
        counter.reset()
        assert calculator._get_dividend_summary(code, report_date) == baseline_summary
        assert len(counter.sqls) == 1
        assert "FROM dividends" in counter.sqls[0]


def test_trailing_dps_multi_row_group_is_not_cached(
    price_domain_calculator: IndicatorCalculator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 as_of 下有 **≥2 条**分红时，预取**故意不缓存**（依据 S10 求和顺序）。

    DuckDB 的 `SUM(DOUBLE)` 是顺序累加，批量 `GROUP BY` 与逐股过滤扫描的行序不同
    → 多行时同一个值能差 1 个 ULP（2026-11-25 实测：1,000 只里 2 只）。这不是
    口径差异，但也**不是"更正确"**，属于漂移 → 不缓存、回落原逐股 SQL，
    结果按构造逐位一致。本用例把"这条限制还在"钉死：
    若有人改成无条件缓存，这里立刻红（不依赖浮点运气）。
    """
    calculator = price_domain_calculator
    code = "300750"
    report_date = _report_date(calculator, code)
    price = calculator._get_latest_price(code)
    as_of = str(price["trade_date"])[:10]
    key = calculator._asof_key(as_of)

    # 在"价格日前 12 个月内"补**两条**分红 → 该 (股, as_of) 变成多行组
    with calculator.duck.write_connection() as connection:
        for days_back, dps in ((30, 0.5), (90, 0.25)):
            connection.execute(
                f"""INSERT INTO dividends (stock_code, ex_date, announcement_date, dividend_per_share)
                   VALUES (?, CAST(CAST(? AS DATE) - INTERVAL '{days_back} days' AS DATE),
                              CAST(CAST(? AS DATE) - INTERVAL '{days_back + 30} days' AS DATE), ?)""",
                [code, as_of, as_of, dps],
            )

    single = calculator._trailing_dps(code, as_of)
    assert single is not None

    with calculator.price_domain_batch([code], report_dates={code: report_date}):
        assert calculator._prefetch_lookup("trailing_dps", code, key)[0] is False, (
            "多行分红组不许进预取缓存（求和顺序不可复现 → 1 ULP 漂移）"
        )
        counter = _QueryCounter(calculator)
        counter.install(monkeypatch, calculator)
        assert calculator._trailing_dps(code, as_of) == single
        assert counter.count_containing("FROM dividends") == 1, counter.sqls


# ─── ③ 缓存不许跨批次存活 ─────────────────────────────────────────


def test_batch_scope_clears_cache_on_exit(price_domain_calculator: IndicatorCalculator) -> None:
    calculator = price_domain_calculator
    code = "600519"
    report_date = _report_date(calculator, code)
    assert calculator._prefetch_cache is None

    with calculator.price_domain_batch([code], report_dates={code: report_date}):
        assert calculator._prefetch_cache, "批次内必须真的建立了缓存"
        assert calculator._prefetch_lookup("financials", code)[0] is True
        assert calculator._prefetch_lookup("technical", code)[0] is True
        assert calculator._prefetch_lookup("treasury_curve")[0] is True
        assert calculator._prefetch_lookup("calendar")[0] is True

    assert calculator._prefetch_cache is None, "批次结束必须丢弃缓存（不许跨批次）"

    # 异常退出同样必须清空
    with (
        pytest.raises(RuntimeError),
        calculator.price_domain_batch([code], report_dates={code: report_date}),
    ):
        assert calculator._prefetch_cache
        raise RuntimeError("boom")
    assert calculator._prefetch_cache is None


def test_second_batch_sees_data_written_between_batches(
    price_domain_calculator: IndicatorCalculator,
) -> None:
    """两批之间改了行情 → 第二批必须读到新价（跨批次缓存 = 假绿）。"""
    calculator = price_domain_calculator
    code = "600519"
    report_date = _report_date(calculator, code)

    first = calculator.compute_price_fields_for_stock(code, report_date)
    assert first["latest_close"] == 10

    with price_domain_calculator.duck.write_connection() as connection:
        connection.execute(
            "UPDATE price_daily_raw SET close = 42 WHERE stock_code = ?", [code],
        )
        connection.execute(
            "UPDATE price_daily_qfq SET close = 42 WHERE stock_code = ?", [code],
        )

    second = calculator.compute_price_fields_for_stock(code, report_date)
    assert second["latest_close"] == 42, "第二批读到了批次之前的旧行情（缓存跨批次了）"
    assert second["total_market_cap"] == pytest.approx(42 * 100)


def test_second_batch_sees_share_capital_written_between_batches(
    price_domain_calculator: IndicatorCalculator,
) -> None:
    """两批之间改了**股本历史** → 第二批必须读到新股本（跨批次缓存 = 假绿）。

    `shares_asof` 是这一轮新加的一类预取：它缓存的是"某只股票在某个价格日的
    时点股本"。若缓存跨批次存活，第二批会继续用批次之前的股本算市值 —— 那正是
    本用例要钉死的假绿形态（市值 = 股本 × 价格，错在这里全市场 PE/PB 一起错）。
    """
    calculator = price_domain_calculator
    code = "600519"
    report_date = _report_date(calculator, code)
    price = calculator._get_latest_price(code)
    price_date = str(price["trade_date"])[:10]
    close = float(price["close"])

    # 第一批：stock_meta 的 100 股（该 fixture 没有历史股本行）→ 市值 10 × 100
    first = calculator.compute_price_fields_for_stock(code, report_date)
    assert first["total_market_cap"] == pytest.approx(close * 100)

    # 批次之间写入一条**生效日早于价格日**的历史股本行（250 股）
    with calculator.duck.write_connection() as connection:
        connection.execute(
            """INSERT INTO share_capital_history
                   (stock_code, effective_date, total_shares, source, raw_hash, batch_id)
               VALUES (?, CAST(? AS DATE), ?, 'cninfo_capital', 'seed', 'seed')""",
            [code, "2020-01-02", 250],
        )

    second = calculator.compute_price_fields_for_stock(code, report_date)
    assert second["total_market_cap"] == pytest.approx(close * 250), (
        "第二批读到了批次之前的旧股本（shares_asof 缓存跨批次了）"
    )

    # 批次内命中不需要再发 SQL；同批换成另一个价格日必须 miss（键含 as_of）
    with calculator.price_domain_batch([code], report_dates={code: report_date}):
        key = calculator._asof_key(price_date)
        hit, cached = calculator._prefetch_lookup("shares_asof", code, key)
        assert hit is True and cached.shares == 250.0
        assert calculator._prefetch_lookup("shares_asof", code, "1990-01-01")[0] is False
        assert calculator._resolve_shares_asof(code, price_date).shares == 250.0
        assert calculator._resolve_shares_asof(code, "1990-01-01").source == "meta"


# ─── ④ 每类查询每批只发一次 ────────────────────────────────────────


def test_batch_issues_each_query_kind_once(
    price_domain_calculator: IndicatorCalculator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 只股票一批：13 条/只 → 每类 1 条/批（含 as-of 股本）。"""
    calculator = price_domain_calculator
    codes = ["600519", "000001", "300750"]
    report_dates = {code: _report_date(calculator, code) for code in codes}
    counter = _QueryCounter(calculator)
    counter.install(monkeypatch, calculator)

    counter.reset()
    with calculator.duck.read_connection() as connection:
        calculator._calculation_read_connection = connection
        try:
            with calculator.price_domain_batch(codes, report_dates=report_dates):
                for code in codes:
                    calculator._compute_price_fields_for_stock(code, report_dates[code])
        finally:
            del calculator._calculation_read_connection

    batch_kinds = {
        "FROM balance_sheet bs": 1,
        "FROM stock_meta WHERE stock_code IN": 1,
        "FROM price_daily_raw WHERE stock_code IN": 1,
        "LEFT JOIN price_daily_qfq": 1,
        "FROM treasury_yield_curve": 1,
        # TTM 窗口 / 滚动 12 个月每股分红 / 时点股本（market_cap 的批量 API）
        "(stock_code, as_of)": 3,
    }
    for needle, expected in batch_kinds.items():
        actual = counter.count_containing(needle)
        assert actual == expected, (needle, actual)

    # 时点股本改走 `market_cap.resolve_shares_asof_batch`（一条 SQL 解一批）：
    # 每股 1 条的热点消失，但**仍然只读** `share_capital_history` 这一张表。
    assert counter.count_containing("share_capital_history") == 1, counter.sqls

    # 按设计仍逐股的只有一类：`_get_dividend_summary` 的历史全表聚合
    # （多行求和顺序不可复现 → 批量 GROUP BY 会漂 1 ULP，依据 S10）。
    assert counter.count_containing("FROM dividends") == len(codes)
    # 总量守门：改前 13 条/只 = 39 条；改后 8 条批量 + 1 条/只 = 11 条。
    assert len(counter.sqls) <= len(codes) + 8, counter.sqls
