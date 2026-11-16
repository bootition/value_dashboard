"""总市值单一实现回归（2026-11-16 收敛 + 送转回填）。

钉死四件事：

1. :func:`app.core.market_cap.market_cap_from` —— 唯一算术，NULL 语义不变；
2. :func:`app.core.market_cap.resolve_shares_asof` —— 唯一时点股本规则
   （历史行 / 无历史行 / 历史行晚于 price_date 三种）；
3. **一致性**：同一只送转股票，指标主链（calculator）与 ETF 基本面
   （etf_fundamentals）两条路径算出的总市值**完全相等**（本任务核心）；
4. **兼容性**：``share_capital_history`` 无数据时，两条路径都与旧实现
   ``close × stock_meta.total_shares`` 逐位一致。

外加回填脚本 ``scripts/backfill_share_capital_from_xdxr.py`` 的
「dry-run 不写 / --yes 只插不覆盖」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.etf_fundamentals import market_cap_series
from app.core.indicators.calculator import IndicatorCalculator
from app.core.market_cap import (
    SOURCE_HISTORY,
    SOURCE_META,
    SOURCE_NONE,
    latest_market_cap,
    market_cap_by_date,
    market_cap_from,
    resolve_shares_asof,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore

BONUS_STOCK = "001225"
PRE_BONUS_SHARES = 65_570_800
POST_BONUS_SHARES = 91_799_120  # = 65,570,800 × 1.4（送转 0.4）
BONUS_EX_DATE = "2026-09-23"
HISTORY_DATE = "2026-05-30"
LAST_PRICE_DATE = "2026-09-30"  # 月末最后交易日 → ETF 序列会保留这一点
LAST_CLOSE = 7.86


# ─── 夹具 ───────────────────────────────────────────────────────────


def _seed_stock(
    duck: DuckDBStore,
    *,
    stock_code: str = BONUS_STOCK,
    meta_shares: int = POST_BONUS_SHARES,
    circ_shares: int | None = None,
    history: list[tuple[str, int]] | None = None,
    prices: list[tuple[str, float]] | None = None,
) -> None:
    duck.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed,
                                   total_shares, circ_shares)
           VALUES (?, ?, 'SZSE', TRUE, ?, ?)""",
        [stock_code, stock_code, meta_shares, circ_shares or meta_shares],
    )
    for trade_date, close in prices or [(LAST_PRICE_DATE, LAST_CLOSE)]:
        for table in ("price_daily_raw", "price_daily_qfq"):
            duck.write_query(
                f"INSERT INTO {table} (stock_code, trade_date, close) VALUES (?, ?, ?)",
                [stock_code, trade_date, close],
            )
    for effective_date, total_shares in history or []:
        duck.write_query(
            """INSERT INTO share_capital_history
                   (stock_code, effective_date, total_shares, change_reason,
                    is_anchor, verified, source, raw_hash, batch_id)
               VALUES (?, ?, ?, NULL, TRUE, TRUE, 'cninfo_capital', 'seed', 'seed')""",
            [stock_code, effective_date, total_shares],
        )


def _bonus_stock_with_history(duck: DuckDBStore) -> None:
    """送转前只有一条历史股本；送转后 stock_meta 已更新（= 前值 × 1.4）。"""
    _seed_stock(
        duck,
        history=[(HISTORY_DATE, PRE_BONUS_SHARES)],
        prices=[
            (HISTORY_DATE, 11.0),
            ("2026-09-22", 11.0),
            (LAST_PRICE_DATE, LAST_CLOSE),
        ],
    )
    duck.write_query(
        """INSERT INTO xdxr (stock_code, event_date, category, fenhong, songzhuangu,
                             peigu, peigujia)
           VALUES (?, ?, 1, 0.0, 0.4, 0.0, 0.0)""",
        [BONUS_STOCK, BONUS_EX_DATE],
    )


def _calculator(duck: DuckDBStore, sqlite: SQLiteStore) -> IndicatorCalculator:
    return IndicatorCalculator(duck=duck, sqlite=sqlite)


def _calculator_market_cap(
    duck: DuckDBStore, sqlite: SQLiteStore, price_date: str, close: float,
) -> dict:
    meta = duck.read_query(
        "SELECT total_shares, circ_shares FROM stock_meta WHERE stock_code = ?", [BONUS_STOCK],
    )[0]
    return _calculator(duck, sqlite)._calc_valuation(
        BONUS_STOCK,
        {"close": close, "trade_date": price_date},
        meta["total_shares"],
        meta["circ_shares"],
        {},
        {},
        {},
    )


# ─── market_cap_from ────────────────────────────────────────────────


def test_market_cap_from_multiplies_close_by_shares() -> None:
    assert market_cap_from(10.0, 1_000.0) == 10_000.0
    assert market_cap_from(7.86, POST_BONUS_SHARES) == 721_541_083.2


def test_market_cap_from_returns_none_when_shares_zero() -> None:
    assert market_cap_from(10.0, 0) is None
    assert market_cap_from(10.0, 0.0) is None
    assert market_cap_from(10.0, -1) is None


def test_market_cap_from_returns_none_when_inputs_missing() -> None:
    assert market_cap_from(None, 1_000.0) is None
    assert market_cap_from(10.0, None) is None
    assert market_cap_from(None, None) is None
    assert market_cap_from(0, 1_000.0) is None
    assert market_cap_from(float("nan"), 1_000.0) is None
    assert market_cap_from(10.0, float("inf")) is None
    assert market_cap_from(True, 1_000.0) is None  # bool 不是合法价格


# ─── resolve_shares_asof ────────────────────────────────────────────


def test_resolve_shares_asof_uses_history_row(duckdb_store: DuckDBStore) -> None:
    _bonus_stock_with_history(duckdb_store)

    resolved = resolve_shares_asof(duckdb_store, BONUS_STOCK, LAST_PRICE_DATE)

    assert resolved.source == SOURCE_HISTORY
    assert resolved.shares == PRE_BONUS_SHARES
    assert resolved.effective_date.isoformat() == HISTORY_DATE
    assert resolved.price_date.isoformat() == LAST_PRICE_DATE


def test_resolve_shares_asof_falls_back_to_stock_meta(duckdb_store: DuckDBStore) -> None:
    _seed_stock(duckdb_store, history=[])

    resolved = resolve_shares_asof(duckdb_store, BONUS_STOCK, LAST_PRICE_DATE)

    assert resolved.source == SOURCE_META
    assert resolved.shares == POST_BONUS_SHARES
    assert resolved.effective_date is None


def test_resolve_shares_asof_ignores_history_after_price_date(
    duckdb_store: DuckDBStore,
) -> None:
    """历史行日期晚于 price_date → 不得使用（不许未来数据泄漏）。"""
    _seed_stock(duckdb_store, history=[(LAST_PRICE_DATE, PRE_BONUS_SHARES)])

    resolved = resolve_shares_asof(duckdb_store, BONUS_STOCK, "2026-09-22")

    assert resolved.source == SOURCE_META
    assert resolved.shares == POST_BONUS_SHARES


def test_resolve_shares_asof_none_when_nothing_known(duckdb_store: DuckDBStore) -> None:
    resolved = resolve_shares_asof(duckdb_store, "999999", LAST_PRICE_DATE)

    assert resolved.source == SOURCE_NONE
    assert resolved.shares is None


def test_resolve_shares_asof_without_price_date_skips_asof(
    duckdb_store: DuckDBStore,
) -> None:
    """没有价格日就没有「时点」→ 直接 stock_meta，不拿任意历史行充数。"""
    _seed_stock(duckdb_store, history=[(HISTORY_DATE, PRE_BONUS_SHARES)])

    resolved = resolve_shares_asof(duckdb_store, BONUS_STOCK, None)

    assert resolved.source == SOURCE_META
    assert resolved.shares == POST_BONUS_SHARES


def test_latest_market_cap_reports_source_and_value(duckdb_store: DuckDBStore) -> None:
    _seed_stock(duckdb_store, history=[(HISTORY_DATE, PRE_BONUS_SHARES)])

    cap = latest_market_cap(duckdb_store, BONUS_STOCK)

    assert cap["shares_source"] == SOURCE_HISTORY
    assert cap["close"] == LAST_CLOSE
    assert cap["price_date"].isoformat() == LAST_PRICE_DATE
    assert cap["total_market_cap"] == LAST_CLOSE * PRE_BONUS_SHARES


# ─── 一致性：calculator 与 etf_fundamentals 必须完全相等（核心） ────


def test_calculator_and_etf_paths_agree_on_bonus_stock(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """本任务核心断言：同一只有送转的股票，两条路径的市值**完全相等**。"""
    _seed_stock(
        duckdb_store,
        history=[(HISTORY_DATE, PRE_BONUS_SHARES)],
        prices=[
            (HISTORY_DATE, 11.0),
            ("2026-09-22", 11.0),
            (BONUS_EX_DATE, LAST_CLOSE),
            (LAST_PRICE_DATE, LAST_CLOSE),
        ],
    )

    # 路径 1：指标主链（calculator → latest_market_cap → 历史股本）
    calc = _calculator_market_cap(duckdb_store, sqlite_store, LAST_PRICE_DATE, LAST_CLOSE)
    assert calc["total_market_cap"] == LAST_CLOSE * PRE_BONUS_SHARES

    # 路径 2：ETF 基本面（etf_fundamentals → market_cap_by_date → 同一口径）
    series = market_cap_series(duckdb_store, [BONUS_STOCK])
    assert series, "ETF 市值序列不应为空"
    assert series[-1]["trade_date"] == LAST_PRICE_DATE
    # 亿元是展示层单位（`元 / 1e8`）：两边同为「同一个元的除法」，必须完全相等
    assert series[-1]["value"] == calc["total_market_cap"] / 1e8

    # 同一断言的元层证据：两条路径都等于 close × as-of 股本
    by_date = market_cap_by_date(duckdb_store, [BONUS_STOCK])
    assert by_date[-1]["total_market_cap"] == calc["total_market_cap"]


def test_both_paths_track_asof_shares_not_current_shares(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """送转日之后两条路径都用「送转后」股本（证明 as-of 真的生效）。"""
    _bonus_stock_with_history(duckdb_store)

    before = _calculator_market_cap(duckdb_store, sqlite_store, "2026-09-22", 11.0)
    after = _calculator_market_cap(duckdb_store, sqlite_store, LAST_PRICE_DATE, LAST_CLOSE)
    assert before["total_market_cap"] == 11.0 * PRE_BONUS_SHARES

    # 送转事件尚未写进历史表 → 仍是送转前股本（旧行为不变，由回填脚本负责补）
    assert after["total_market_cap"] == LAST_CLOSE * PRE_BONUS_SHARES

    duckdb_store.write_query(
        """INSERT INTO share_capital_history
               (stock_code, effective_date, total_shares, change_reason, is_anchor,
                verified, source, raw_hash, batch_id)
           VALUES (?, ?, ?, 'xdxr_songzhuangu=0.4', FALSE, FALSE,
                   'xdxr_backfill', 'hash', 'batch')""",
        [BONUS_STOCK, BONUS_EX_DATE, POST_BONUS_SHARES],
    )

    after_backfill = _calculator_market_cap(
        duckdb_store, sqlite_store, LAST_PRICE_DATE, LAST_CLOSE,
    )
    etf_after = market_cap_series(duckdb_store, [BONUS_STOCK])
    assert after_backfill["total_market_cap"] == LAST_CLOSE * POST_BONUS_SHARES
    assert etf_after[-1]["value"] == after_backfill["total_market_cap"] / 1e8
    assert market_cap_by_date(duckdb_store, [BONUS_STOCK])[-1]["total_market_cap"] == (
        after_backfill["total_market_cap"]
    )


# ─── 兼容性：无历史股本时必须与旧实现逐位一致 ────────────────────────


def test_no_history_matches_close_times_meta_shares(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, meta_shares=POST_BONUS_SHARES, history=[])

    valuation = _calculator_market_cap(
        duckdb_store, sqlite_store, LAST_PRICE_DATE, LAST_CLOSE,
    )
    series = market_cap_series(duckdb_store, [BONUS_STOCK])

    expected = LAST_CLOSE * POST_BONUS_SHARES
    assert valuation["total_market_cap"] == expected
    assert valuation["circ_market_cap"] == expected  # circ_shares 同值
    assert series[-1]["value"] == expected / 1e8
    assert market_cap_by_date(duckdb_store, [BONUS_STOCK])[-1]["total_market_cap"] == expected


def test_missing_everything_stays_none(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """历史与 stock_meta 都没有股本 → NULL（不是 0、不是异常）。"""
    valuation = _calculator(duckdb_store, sqlite_store)._calc_valuation(
        "999999", {"close": 10.0, "trade_date": LAST_PRICE_DATE},
        None, None, {}, {}, {},
    )
    assert valuation["total_market_cap"] is None
    assert valuation["circ_market_cap"] is None
    assert valuation["pe_ttm"] is None


def test_valuation_ratios_use_the_same_market_cap(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """PE/PB 与总市值同源：pe_ttm == total_market_cap / ttm_profit。"""
    _seed_stock(duckdb_store, history=[(HISTORY_DATE, PRE_BONUS_SHARES)])
    meta = duckdb_store.read_query(
        "SELECT total_shares, circ_shares FROM stock_meta WHERE stock_code = ?", [BONUS_STOCK],
    )[0]

    valuation = _calculator(duckdb_store, sqlite_store)._calc_valuation(
        BONUS_STOCK,
        {"close": LAST_CLOSE, "trade_date": LAST_PRICE_DATE},
        meta["total_shares"],
        meta["circ_shares"],
        {"parent_net_profit": 1_000_000.0, "revenue": 5_000_000.0, "cf_from_operating": 2_000_000.0},
        {"total_equity_parent": 3_000_000.0},
        {},
    )

    market_cap = LAST_CLOSE * PRE_BONUS_SHARES
    assert valuation["total_market_cap"] == market_cap
    assert valuation["pe_ttm"] == market_cap / 1_000_000.0
    assert valuation["pb_mrq"] == market_cap / 3_000_000.0
    assert valuation["ps_ttm"] == market_cap / 5_000_000.0
    assert valuation["pcf_ttm"] == market_cap / 2_000_000.0


# ─── 回填脚本：dry-run 不写 / --yes 只插不覆盖 ───────────────────────


@pytest.fixture
def backfill_env(monkeypatch: pytest.MonkeyPatch, database_paths: DatabasePathSet) -> DatabasePathSet:
    """让回填脚本按 pytest 临时库的隔离 profile 解析路径。"""
    monkeypatch.setenv("VD_ENV", "test")
    monkeypatch.setenv("VD_DUCKDB_PATH", str(database_paths.duckdb_path))
    monkeypatch.setenv("VD_SQLITE_PATH", str(database_paths.sqlite_path))
    monkeypatch.setenv("VD_TEST_RUN_ROOT", str(database_paths.run_root))
    return database_paths


def _seed_backfill_cases(duck: DuckDBStore) -> None:
    """三只股票：可推算 / 同日已有行（必须保留原值） / 缺前值（必须跳过）。"""
    # A: 可推算（链条末端之后的送转）
    _seed_stock(duck, history=[(HISTORY_DATE, PRE_BONUS_SHARES)])
    duck.write_query(
        """INSERT INTO xdxr (stock_code, event_date, category, fenhong, songzhuangu,
                             peigu, peigujia)
           VALUES (?, ?, 1, 0.0, 0.4, 0.0, 0.0)""",
        [BONUS_STOCK, BONUS_EX_DATE],
    )
    # B: 同日期已有行（值可疑，但「只增不改」不许覆盖）
    _seed_stock(duck, stock_code="600000", meta_shares=1_600_000, history=[
        ("2026-03-31", 1_000_000),
        ("2026-06-30", 1_600_000),
    ])
    duck.write_query(
        """INSERT INTO xdxr (stock_code, event_date, category, fenhong, songzhuangu,
                             peigu, peigujia)
           VALUES ('600000', '2026-06-30', 1, 0.0, 0.5, 0.0, 0.0)"""
    )
    # C: 事件早于任何历史行 → 缺前值，必须跳过
    _seed_stock(duck, stock_code="600001", meta_shares=999_000, history=[
        ("2026-08-31", 999_000),
    ])
    duck.write_query(
        """INSERT INTO xdxr (stock_code, event_date, category, fenhong, songzhuangu,
                             peigu, peigujia)
           VALUES ('600001', '2026-06-30', 1, 0.0, 0.2, 0.0, 0.0)"""
    )


def _history_rows(duck: DuckDBStore, stock_code: str) -> list[dict]:
    return duck.read_query(
        """SELECT effective_date, total_shares, source, change_reason, is_anchor
           FROM share_capital_history WHERE stock_code = ? ORDER BY effective_date""",
        [stock_code],
    )


def test_backfill_dry_run_does_not_write(
    duckdb_store: DuckDBStore,
    backfill_env: DatabasePathSet,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.backfill_share_capital_from_xdxr import main as backfill_main

    _seed_backfill_cases(duckdb_store)
    before = {code: _history_rows(duckdb_store, code) for code in (BONUS_STOCK, "600000", "600001")}
    backup_dir = tmp_path / "backup"

    exit_code = backfill_main(["--backup-dir", str(backup_dir)])

    assert exit_code == 0
    after = {code: _history_rows(duckdb_store, code) for code in (BONUS_STOCK, "600000", "600001")}
    assert after == before
    assert not backup_dir.exists(), "dry-run 不得落备份"
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_backfill_yes_inserts_without_overwriting(
    duckdb_store: DuckDBStore,
    backfill_env: DatabasePathSet,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.backfill_share_capital_from_xdxr import main as backfill_main

    _seed_backfill_cases(duckdb_store)
    backup_dir = tmp_path / "backup"

    exit_code = backfill_main(["--yes", "--backup-dir", str(backup_dir)])
    assert exit_code == 0
    capsys.readouterr()

    # A 补上了送转行
    a_rows = _history_rows(duckdb_store, BONUS_STOCK)
    assert [str(row["effective_date"]) for row in a_rows] == [HISTORY_DATE, BONUS_EX_DATE]
    inserted = a_rows[-1]
    assert inserted["total_shares"] == POST_BONUS_SHARES
    assert inserted["source"] == "xdxr_backfill"
    assert inserted["change_reason"] == "xdxr_songzhuangu=0.4"
    assert inserted["is_anchor"] is False

    # B 同日期已有行未被覆盖（只增不改）
    b_rows = _history_rows(duckdb_store, "600000")
    assert b_rows[-1]["total_shares"] == 1_600_000
    assert b_rows[-1]["source"] == "cninfo_capital"
    assert len(b_rows) == 2

    # C 缺前值 → 未写入
    c_rows = _history_rows(duckdb_store, "600001")
    assert len(c_rows) == 1
    assert c_rows[0]["total_shares"] == 999_000

    # 写前备份确实落盘
    assert backup_dir.exists()
    assert list(backup_dir.glob("share_capital_history-*.parquet"))

    # 幂等：第二次跑不应再插入
    assert backfill_main(["--yes", "--backup-dir", str(backup_dir)]) == 0
    capsys.readouterr()
    assert _history_rows(duckdb_store, BONUS_STOCK) == a_rows


def test_backfill_plan_skips_conflicting_and_unverifiable_rows() -> None:
    from scripts.backfill_share_capital_from_xdxr import (
        SKIP_CONFLICT_META,
        SKIP_CONFLICT_NEXT,
        SKIP_EXISTS,
        SKIP_NO_PREV,
        build_plan,
    )

    rows = [
        # 已有同日行 → 只增不改
        {"stock_code": "A", "event_date": "2026-01-01", "songzhuangu": 0.4,
         "prev_shares": 100.0, "prev_date": "2025-12-01", "next_shares": None,
         "next_date": None, "existing_shares": 140.0, "meta_shares": 140.0},
        # 无前值 → 跳过
        {"stock_code": "B", "event_date": "2026-01-01", "songzhuangu": 0.4,
         "prev_shares": None, "prev_date": None, "next_shares": 140.0,
         "next_date": "2026-02-01", "existing_shares": None, "meta_shares": 140.0},
        # 与后一行冲突 → 跳过
        {"stock_code": "C", "event_date": "2026-01-01", "songzhuangu": 0.4,
         "prev_shares": 100.0, "prev_date": "2025-12-01", "next_shares": 200.0,
         "next_date": "2026-02-01", "existing_shares": None, "meta_shares": 200.0},
        # 与 stock_meta 冲突 → 跳过
        {"stock_code": "D", "event_date": "2026-01-01", "songzhuangu": 0.4,
         "prev_shares": 100.0, "prev_date": "2025-12-01", "next_shares": None,
         "next_date": None, "existing_shares": None, "meta_shares": 200.0},
        # 正常：推算与 meta 一致
        {"stock_code": "E", "event_date": "2026-01-01", "songzhuangu": 0.4,
         "prev_shares": 100.0, "prev_date": "2025-12-01", "next_shares": None,
         "next_date": None, "existing_shares": None, "meta_shares": 140.0},
    ]

    plan = build_plan(rows, tolerance=0.01, batch_id="b")

    assert [row["stock_code"] for row in plan.inserts] == ["E"]
    assert plan.inserts[0]["total_shares"] == 140.0
    assert plan.count(SKIP_EXISTS) == 1
    assert plan.count(SKIP_NO_PREV) == 1
    assert plan.count(SKIP_CONFLICT_NEXT) == 1
    assert plan.count(SKIP_CONFLICT_META) == 1
    assert plan.stocks == 1
    assert plan.events == 5


# ─── 静态守卫：三处调用点不得再各自手写市值口径 ──────────────────────


def test_call_sites_do_not_hand_write_market_cap_rules() -> None:
    """总市值口径只能来自 app/core/market_cap.py（防回归）。"""
    import app.core.etf_fundamentals as etf_module
    import app.core.index_dashboard as index_module
    import app.core.indicators.calculator as calculator_module

    calculator_source = Path(calculator_module.__file__).read_text(encoding="utf-8")
    etf_source = Path(etf_module.__file__).read_text(encoding="utf-8")
    index_source = Path(index_module.__file__).read_text(encoding="utf-8")

    # 指标主链：不得再出现「当前股本 × 收盘价」的裸乘法
    assert "from app.core.market_cap import" in calculator_source
    assert "close * shares" not in calculator_source
    # ETF 基本面：不得再手写一份 ASOF + COALESCE 口径
    assert "from app.core.market_cap import" in etf_source
    assert "COALESCE(sh.total_shares, s.total_shares)" not in etf_source
    # 指数仪表盘：内嵌聚合 SQL 复用同一片段，不另写一份
    assert "asof_shares_join_sql" in index_source
    assert "COALESCE(sh.total_shares, s.total_shares)" not in index_source
