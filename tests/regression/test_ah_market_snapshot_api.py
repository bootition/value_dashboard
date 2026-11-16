"""A+H「H 股市值」产品出口回归（DuckDB schema v41，2026-11-22）。

守住的底线（每条都对应一个曾让「算好了但用户看不到」的具体环节）
--------------------------------------------------------------
1. **落库**：`init_duckdb_schema()` 必须建出 `ah_market_snapshot`，且
   `schema_migrations` 有 41 —— 只建表不记账会让 `skip_if_current` 快路径
   漏建表（仓库硬纪律，`test_redteam_2026_08_13_fixes` 也钉这条）；
2. **脚本写入**：`scripts/refresh_ah_market_snapshot.py --yes` 调
   `build_ah_snapshot()` 落库，`h_share_market_cap_cny` = H 价 × H 股本 × 汇率；
3. **单位转换只转一次**：`build_ah_snapshot()` 的 `a_yield`/`h_yield` 是**小数
   比例**，表列是 `*_yield_pct`，落库必须 ×100（0.05 → 5.0），否则前端显示
   100 倍偏差（仓库已发生过 2 次同类事故）；
4. **API 字段齐全 + 单位明确**：`GET /api/stock/{code}/ah-metrics` 返回全部
   快照列 + `units`（逐列单位，不让前端猜）+ `disclaimer`（写明
   「H 股市值 = H 价 × H 股本 × 汇率；总市值口径见 total_market_cap」）；
5. **非 A+H → 404 + reason=not_an_ah_stock**，A+H 但没快照 → 404 +
   reason=ah_snapshot_missing（两种情况前端行为不同，不能混）；
6. **缺数据不许伪造成 0**：计算不出来的股票也写一行，计算列为 NULL，
   原因码进 `note`，API 原样透出 status/reason；
7. **幂等**：同一 `as_of` 重跑行数不变（主键 `stock_code, as_of` upsert）；
8. **dry-run 绝不写库**：不加 `--yes` 跑完表里仍是 0 行。

隔离：`duckdb_store` fixture 建在 `--basetemp` 下的临时库；脚本也指向同一个
临时库（`--db`，只允许 VD_ENV=test/staging），**绝不碰**
`data/valuedashboard.duckdb`。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.storage.duckdb_store import DuckDBStore
from app.web.api.stock_detail import router as stock_detail_router

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "refresh_ah_market_snapshot.py"

# A+H 映射来自 app/core/ah_hk_mapping.py 的内置快照：01398 的名称就是「工商银行」，
# 因此 stock_meta.name 必须精确等于它，否则脚本侧拿不到映射（不是模糊匹配）。
AH_CODE = "601398"
AH_NAME = "工商银行"
AH_HK_CODE = "01398"
PANEL_CODE = "600519"
PANEL_NAME = "贵州茅台"

# 造数口径（手工可验）：A 股 10 元 / 100 股（流通 60）；H 股 8.2 港元 / 40 股；
# HKD→CNY 0.9。→ A 部分 600、H 股市值 8.2×0.9×40 = 295.2、合计 895.2。
AS_OF = "2026-01-05"
A_CLOSE = 10.0
H_CLOSE_HKD = 8.2
FX = 0.9
TOTAL_SHARES = 100
CIRC_SHARES = 60
H_SHARES = TOTAL_SHARES - CIRC_SHARES  # h_share_count 的推导口径
A_DPS = 0.5
H_DPS_HKD = 0.5
H_DPS_CNY = 0.45


def _load_script_module() -> Any:
    """按文件路径加载维护脚本（scripts/ 不是包，不能 import 名字）。"""
    name = "refresh_ah_market_snapshot"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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


def _seed_a_dividend(duck: DuckDBStore, code: str, day: str, dps: float) -> None:
    duck.write_query(
        """INSERT INTO dividends
               (stock_code, ex_date, announcement_date, dividend_per_share)
           VALUES (?, ?, ?, ?)
           ON CONFLICT (stock_code, ex_date) DO UPDATE SET
               dividend_per_share = excluded.dividend_per_share""",
        [code, day, day, dps],
    )


def _seed_h_dividend(
    duck: DuckDBStore, hk_code: str, day: str, *, hkd: float, cny: float,
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


def _seed_indicator_snapshot(duck: DuckDBStore, code: str, total_market_cap: float) -> None:
    """行业口径总市值（本接口只读展示，绝不重算）。"""
    duck.write_query(
        """INSERT INTO indicator_snapshot
               (stock_code, report_date, latest_close, latest_price_date, total_market_cap)
           VALUES (?, '2025-12-31', ?, ?, ?)
           ON CONFLICT (stock_code, report_date) DO UPDATE SET
               latest_close = excluded.latest_close,
               latest_price_date = excluded.latest_price_date,
               total_market_cap = excluded.total_market_cap""",
        [code, A_CLOSE, AS_OF, total_market_cap],
    )


def _seed_full_ah_stock(duck: DuckDBStore) -> None:
    """一只数据齐全的 A+H：算得出 H 股市值 + 双侧股息率。"""
    _seed_meta(duck, AH_CODE, AH_NAME, TOTAL_SHARES, CIRC_SHARES)
    _seed_a_price(duck, AH_CODE, AS_OF, A_CLOSE)
    _seed_h_price(duck, AH_HK_CODE, AS_OF, H_CLOSE_HKD)
    _seed_fx(duck, "2026-01-02", FX)
    _seed_a_dividend(duck, AH_CODE, "2025-06-30", A_DPS)
    _seed_h_dividend(duck, AH_HK_CODE, "2025-06-30", hkd=H_DPS_HKD, cny=H_DPS_CNY)
    # 行业口径总市值：A 价 × 总股本 = 10 × 100 = 1000（不是 AH 拆分合计 895.2）
    _seed_indicator_snapshot(duck, AH_CODE, A_CLOSE * TOTAL_SHARES)


def _run_script(
    db_path: Path, *extra: str, backup_dir: Path | None = None,
) -> int:
    module = _load_script_module()
    argv = ["--db", str(db_path), *extra]
    if backup_dir is not None:
        argv += ["--backup-dir", str(backup_dir)]
    return int(module.main(argv))


def _api_client(duck: DuckDBStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.include_router(stock_detail_router)
    return TestClient(app)


# ─── 1. schema：建表 + 迁移记录 41 ─────────────────────────────────


def test_schema_creates_ah_market_snapshot_and_records_migration_41(
    duckdb_store: DuckDBStore,
) -> None:
    from app.core.storage.schema import DUCKDB_SCHEMA_VERSION

    assert DUCKDB_SCHEMA_VERSION == 41
    tables = {
        row["table_name"]
        for row in duckdb_store.read_query("SELECT table_name FROM duckdb_tables()")
    }
    assert "ah_market_snapshot" in tables

    columns = {
        row["column_name"]: row["data_type"]
        for row in duckdb_store.read_query(
            """SELECT column_name, data_type FROM information_schema.columns
               WHERE table_name = 'ah_market_snapshot'"""
        )
    }
    assert columns["stock_code"] == "VARCHAR"
    assert columns["as_of"] == "DATE"
    assert columns["fetch_time"] == "TIMESTAMP"
    for numeric in (
        "a_close",
        "h_close_hkd",
        "fx_hkd_cny",
        "a_shares",
        "h_shares",
        "total_shares",
        "a_share_market_cap_cny",
        "h_share_market_cap_cny",
        "ah_total_market_cap_cny",
        "ah_premium_pct",
        "a_yield_pct",
        "h_yield_pct",
        "h_yield_after_tax_pct",
        "identity_deviation_pct",
    ):
        assert columns[numeric] == "DOUBLE", numeric

    migrations = duckdb_store.read_query(
        "SELECT version, description FROM schema_migrations WHERE version = 41"
    )
    assert len(migrations) == 1
    assert "ah_market_snapshot" in migrations[0]["description"]
    # 迁移记录必须与版本常量同步，否则 skip_if_current 会漏建表
    assert duckdb_store.read_query(
        "SELECT MAX(version) AS v FROM schema_migrations"
    )[0]["v"] == DUCKDB_SCHEMA_VERSION
    # 只加表，绝不动 indicator_snapshot 的列
    snapshot_columns = {
        row["column_name"]
        for row in duckdb_store.read_query(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'indicator_snapshot'"""
        )
    }
    assert "total_market_cap" in snapshot_columns
    assert not any(column.startswith("h_share") for column in snapshot_columns)


# ─── 2. 脚本写入 + API 返回字段齐全/单位正确 ────────────────────────


def test_script_writes_snapshot_and_api_returns_all_fields_with_units(
    duckdb_store: DuckDBStore, database_paths: Any, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VD_ENV", "test")
    _seed_full_ah_stock(duckdb_store)
    backup_dir = tmp_path / "backup"

    code = _run_script(
        database_paths.duckdb_path, "--yes", "--as-of", AS_OF, backup_dir=backup_dir,
    )
    assert code == 0
    # 写前备份必须真的落地
    assert (backup_dir / "ah_market_snapshot.parquet").is_file()

    stored = duckdb_store.read_query(
        "SELECT * FROM ah_market_snapshot WHERE stock_code = ?", [AH_CODE],
    )
    assert len(stored) == 1
    row = stored[0]
    assert str(row["as_of"])[:10] == AS_OF
    assert row["hk_code"] == AH_HK_CODE
    assert row["a_shares"] == pytest.approx(CIRC_SHARES)  # A 股股本 = 流通股本
    assert row["h_shares"] == pytest.approx(H_SHARES)
    assert row["h_share_market_cap_cny"] == pytest.approx(
        H_CLOSE_HKD * FX * H_SHARES
    )
    assert row["a_share_market_cap_cny"] == pytest.approx(A_CLOSE * CIRC_SHARES)
    assert row["ah_total_market_cap_cny"] == pytest.approx(
        A_CLOSE * CIRC_SHARES + H_CLOSE_HKD * FX * H_SHARES
    )
    # 单位纪律：小数比例 → 百分数（只转一次，0.05 → 5.0）
    assert row["a_yield_pct"] == pytest.approx(A_DPS / A_CLOSE * 100.0)
    assert row["h_yield_pct"] == pytest.approx(H_DPS_CNY / (H_CLOSE_HKD * FX) * 100.0)
    assert row["h_shares_confidence"] == "derived"
    assert row["source"] == "ah_metrics.build_ah_snapshot"
    # 恒等式 mismatch 属正常（各算各的），**不得**因此降级该行
    assert row["identity_status"] == "mismatch"
    assert row["note"] == "status=ok"

    response = _api_client(duckdb_store).get(f"/api/stock/{AH_CODE}/ah-metrics")
    assert response.status_code == 200
    payload = response.json()

    for field in (
        "stock_code",
        "hk_code",
        "as_of",
        "a_price_date",
        "h_price_date",
        "a_close",
        "h_close_hkd",
        "fx_hkd_cny",
        "fx_date",
        "a_shares",
        "h_shares",
        "total_shares",
        "a_share_market_cap_cny",
        "h_share_market_cap_cny",
        "ah_total_market_cap_cny",
        "ah_premium_pct",
        "a_yield_pct",
        "h_yield_pct",
        "h_yield_after_tax_pct",
        "identity_status",
        "identity_deviation_pct",
        "h_shares_confidence",
        "note",
        "source",
        "fetch_time",
        "units",
        "disclaimer",
    ):
        assert field in payload, field

    assert payload["as_of"] == AS_OF
    assert payload["a_price_date"] == AS_OF
    assert payload["h_close_hkd"] == pytest.approx(H_CLOSE_HKD)
    assert payload["fx_hkd_cny"] == pytest.approx(FX)
    assert payload["h_share_market_cap_cny"] == pytest.approx(
        H_CLOSE_HKD * FX * H_SHARES
    )
    assert payload["ah_premium_pct"] == pytest.approx(
        (A_CLOSE / (H_CLOSE_HKD * FX) - 1.0) * 100.0
    )
    assert payload["a_yield_pct"] == pytest.approx(A_DPS / A_CLOSE * 100.0)
    assert payload["h_yield_after_tax_pct"] == pytest.approx(
        H_DPS_CNY / (H_CLOSE_HKD * FX) * 100.0 * 0.8
    )
    assert payload["status"] == "ok"
    assert payload["reason"] is None

    # 逐列单位：前端不许猜
    units = payload["units"]
    assert units["h_share_market_cap_cny"] == "CNY_yuan"
    assert units["a_share_market_cap_cny"] == "CNY_yuan"
    assert units["ah_total_market_cap_cny"] == "CNY_yuan"
    assert units["a_close"] == "CNY_per_share"
    assert units["h_close_hkd"] == "HKD_per_share"
    assert units["fx_hkd_cny"] == "CNY_per_HKD"
    assert units["a_shares"] == "shares"
    assert units["a_yield_pct"] == "percent"
    assert units["h_yield_pct"] == "percent"
    assert units["ah_premium_pct"] == "percent_points"
    assert units["as_of"] == "date"

    # 免责声明必须写清 H 股市值口径与总市值口径的分工
    assert "H 股市值 = H 价 × H 股本 × 汇率" in payload["disclaimer"]
    assert "总市值口径见 total_market_cap" in payload["disclaimer"]

    # 行业口径总市值单独透出，且不等于 AH 拆分合计
    total = payload["total_market_cap"]
    assert total["value_cny"] == pytest.approx(A_CLOSE * TOTAL_SHARES)
    assert total["source"] == "indicator_snapshot.total_market_cap"
    assert total["value_cny"] != pytest.approx(payload["ah_total_market_cap_cny"])


# ─── 3. 非 A+H → 404 + reason ──────────────────────────────────────


def test_api_returns_404_not_an_ah_stock(duckdb_store: DuckDBStore) -> None:
    _seed_meta(duckdb_store, PANEL_CODE, PANEL_NAME, 100, 100)
    _seed_a_price(duckdb_store, PANEL_CODE, AS_OF, 1500.0)

    response = _api_client(duckdb_store).get(f"/api/stock/{PANEL_CODE}/ah-metrics")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["reason"] == "not_an_ah_stock"


def test_api_returns_404_snapshot_missing_for_ah_stock(
    duckdb_store: DuckDBStore,
) -> None:
    """是 A+H 但脚本还没跑过 → 必须是另一个 reason（前端行为不同）。"""
    _seed_meta(duckdb_store, AH_CODE, AH_NAME, TOTAL_SHARES, CIRC_SHARES)

    response = _api_client(duckdb_store).get(f"/api/stock/{AH_CODE}/ah-metrics")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["reason"] == "ah_snapshot_missing"
    assert "refresh_ah_market_snapshot" in detail["message"]


# ─── 4. 缺数据不伪造：写一行 NULL + 原因码 ──────────────────────────


def test_missing_h_price_is_persisted_as_null_with_reason(
    duckdb_store: DuckDBStore, database_paths: Any, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VD_ENV", "test")
    _seed_meta(duckdb_store, AH_CODE, AH_NAME, TOTAL_SHARES, CIRC_SHARES)
    _seed_a_price(duckdb_store, AH_CODE, AS_OF, A_CLOSE)
    # 故意不给 H 价 → build_ah_snapshot 该行 reason=missing_h_price
    _seed_fx(duckdb_store, "2026-01-02", FX)

    code = _run_script(
        database_paths.duckdb_path, "--yes", "--as-of", AS_OF,
        backup_dir=tmp_path / "backup",
    )
    assert code == 0

    row = duckdb_store.read_query(
        "SELECT * FROM ah_market_snapshot WHERE stock_code = ?", [AH_CODE],
    )[0]
    assert row["h_share_market_cap_cny"] is None
    assert row["a_close"] == pytest.approx(A_CLOSE)  # 已算出的部分照实落库
    assert row["note"] == "status=incomplete; reason=missing_h_price"

    payload = _api_client(duckdb_store).get(
        f"/api/stock/{AH_CODE}/ah-metrics"
    ).json()
    assert payload["h_share_market_cap_cny"] is None
    assert payload["status"] == "incomplete"
    assert payload["reason"] == "missing_h_price"


# ─── 5. 幂等：同 as_of 重跑行数不变；dry-run 不写 ──────────────────


def test_script_is_idempotent_for_same_as_of(
    duckdb_store: DuckDBStore, database_paths: Any, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VD_ENV", "test")
    _seed_full_ah_stock(duckdb_store)

    # dry-run 默认不写库
    assert _run_script(database_paths.duckdb_path, "--as-of", AS_OF) == 0
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS cnt FROM ah_market_snapshot"
    )[0]["cnt"] == 0

    for run in range(2):
        assert _run_script(
            database_paths.duckdb_path, "--yes", "--as-of", AS_OF,
            backup_dir=tmp_path / f"backup-{run}",
        ) == 0

    rows = duckdb_store.read_query(
        "SELECT stock_code, as_of, h_share_market_cap_cny, a_yield_pct "
        "FROM ah_market_snapshot ORDER BY stock_code"
    )
    assert len(rows) == 1
    assert rows[0]["h_share_market_cap_cny"] == pytest.approx(
        H_CLOSE_HKD * FX * H_SHARES
    )
    assert rows[0]["a_yield_pct"] == pytest.approx(A_DPS / A_CLOSE * 100.0)


# ─── 6. 表未迁移 → 503（前端要能区分「非 A+H」和「暂不可用」）───────


def test_api_returns_503_when_snapshot_table_is_not_migrated(
    database_paths: Any, tmp_path: Path,
) -> None:
    """schema < 41（库在、表不在，就是正式库当前的样子）不能伪装成 404。"""
    bare = DuckDBStore(paths=database_paths)  # 故意不跑 init_duckdb_schema
    duckdb.connect(str(database_paths.duckdb_path)).close()  # 建一个空库文件

    response = _api_client(bare).get(f"/api/stock/{AH_CODE}/ah-metrics")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["reason"] == "ah_snapshot_unavailable"
    assert "refresh_ah_market_snapshot" in detail["message"]
