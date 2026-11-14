"""资产健康检查（asset health）回归测试 —— reports/119 的 12 条规则逐条正/反例。

为什么这样组织
--------------
规则引擎的价值全在"判得准"：漏判等于没写，误判等于给自己制造假红灯。
所以每条规则至少一个**正例**（该报的时候必须报）与一个**反例**
（不该报的时候一个 findings 都不能有），并且硬约束（只读、P1 不进遮蔽
集合、空表跳过、单规则失败不拖垮体检）也各有一条独立测试。

全部用 tmp_path 里的临时 DuckDB/SQLite（S1 隔离），不触碰 data/ 正式库。
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.data_quality import (
    _SCREENING_BLOCKING_WARNINGS,
    _SNAPSHOT_BLOCKING_WARNINGS,
    build_data_quality_status,
    indicator_trust,
)
from app.core.quality import asset_health
from app.core.quality.asset_health import (
    RULE_CODES,
    run_asset_health,
    run_doctor,
    write_field_coverage_history,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

# 固定"现在"：所有超期判定都相对它计算，避免用真实时钟让测试在月末翻车。
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ─────────────────────────────────────────────────────────────────────────────
# 公共夹具与工具
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def health_repo(tmp_path: Path) -> Path:
    """一个只包含 schema.py 的假仓库根（规则 10/11 从这个根读文件）。"""
    root = tmp_path / "repo"
    (root / "app" / "core" / "storage").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    shutil.copyfile(
        PROJECT_ROOT / "app" / "core" / "storage" / "schema.py",
        root / "app" / "core" / "storage" / "schema.py",
    )
    return root


def _payload(
    duck: DuckDBStore,
    sqlite: SQLiteStore | None = None,
    *,
    repo_root: Path | None = None,
    **kwargs: Any,
) -> dict:
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("allow_expensive", False)
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    return run_asset_health(duck, sqlite, **kwargs)


def _findings(payload: dict, code: str) -> list[dict]:
    return [finding for finding in payload["findings"] if finding["code"] == code]


def _rule(payload: dict, code: str) -> dict:
    return next(rule for rule in payload["rules"] if rule["code"] == code)


def _insert_stock(duck: DuckDBStore, stock_code: str = "000001") -> None:
    duck.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES (?, '测试', 'SZSE', TRUE) ON CONFLICT DO NOTHING""",
        [stock_code],
    )


def _snapshot_columns() -> list[str]:
    from app.core.screening.engine import SNAPSHOT_COLUMNS

    return sorted(SNAPSHOT_COLUMNS)


def _extended_columns() -> list[str]:
    from app.core.screening.engine import EXTENDED_COLUMNS

    return sorted(EXTENDED_COLUMNS)


def _insert_snapshot(
    duck: DuckDBStore,
    report_date: date,
    codes: list[str],
    *,
    null_fields: set[str] | None = None,
) -> None:
    """插入若干全字段有值的快照行；`null_fields` 里的列写 NULL。"""
    null_fields = null_fields or set()
    columns = ["stock_code", "report_date", "calculated_at", *_snapshot_columns()]
    sql = (
        f"INSERT INTO indicator_snapshot ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    for code in codes:
        values: list[Any] = [code, report_date, NOW]
        for column in _snapshot_columns():
            values.append(None if column in null_fields else 1.0)
        duck.write_query(sql, values)


def _insert_extended(
    duck: DuckDBStore,
    report_date: date,
    codes: list[str],
    *,
    values: dict[str, float] | None = None,
) -> None:
    values = values or {}
    columns = [
        "stock_code", "report_date", "calculated_at", "source", "data_version",
        *_extended_columns(),
    ]
    sql = (
        f"INSERT INTO indicator_ext ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    for code in codes:
        payload: list[Any] = [code, report_date, NOW, "fixture", 1]
        for column in _extended_columns():
            payload.append(values.get(column, 1.0))
        duck.write_query(sql, payload)


# 覆盖率类规则的最小样本量（与 config asset_health.min_domain_rows 一致）。
# 用 100 行而不是 1 行：否则测的是"小库不判覆盖率"的分支，而不是阈值本身。
DEFAULT_SAMPLE = 100


def _bulk_domain(
    duck: DuckDBStore,
    table: str,
    columns: list[str],
    report_date: date,
    *,
    constants: dict[str, Any] | None = None,
    null_every: dict[str, int] | None = None,
    count: int = DEFAULT_SAMPLE,
) -> None:
    """一条 INSERT ... SELECT 造 count 行；`null_every[field]=k` 表示每 k 行 1 行 NULL。"""
    constants = constants or {}
    null_every = null_every or {}
    head = ["stock_code", "report_date", *constants]
    selects = ["'C' || lpad(i::VARCHAR, 6, '0')", "?", *("?" for _ in constants)]
    for column in columns:
        step = null_every.get(column)
        selects.append(f"CASE WHEN i % {step} = 0 THEN NULL ELSE 1.0 END" if step else "1.0")
    sql = (
        f"INSERT INTO {table} ({', '.join([*head, *columns])}) "
        f"SELECT {', '.join(selects)} FROM range(0, ?) AS t(i)"
    )
    duck.write_query(sql, [report_date, *constants.values(), count])


def _bulk_snapshot(
    duck: DuckDBStore,
    report_date: date,
    *,
    null_every: dict[str, int] | None = None,
    count: int = DEFAULT_SAMPLE,
) -> None:
    _bulk_domain(
        duck, "indicator_snapshot", _snapshot_columns(), report_date,
        constants={"calculated_at": NOW}, null_every=null_every, count=count,
    )


def _bulk_extended(
    duck: DuckDBStore,
    report_date: date,
    *,
    null_every: dict[str, int] | None = None,
    count: int = DEFAULT_SAMPLE,
) -> None:
    _bulk_domain(
        duck, "indicator_ext", _extended_columns(), report_date,
        constants={"calculated_at": NOW, "source": "fixture", "data_version": 1},
        null_every=null_every, count=count,
    )


def _bulk_income(
    duck: DuckDBStore, report_date: date, *, deducted_non_null: int,
    count: int = DEFAULT_SAMPLE,
) -> None:
    duck.write_query(
        """INSERT INTO income_statement
               (stock_code, report_date, revenue, parent_net_profit, deducted_net_profit)
           SELECT 'C' || lpad(i::VARCHAR, 6, '0'), ?, 100, 10,
                  CASE WHEN i < ? THEN 1.0 ELSE NULL END
           FROM range(0, ?) AS t(i)""",
        [report_date, deducted_non_null, count],
    )


def _bulk_register_null_reason(
    duck: DuckDBStore, field: str, report_date: date, count: int
) -> None:
    duck.write_query(
        """INSERT INTO indicator_null_reason
               (stock_code, report_date, field_name, reason_code, detail, computed_at)
           SELECT 'C' || lpad(i::VARCHAR, 6, '0'), ?, ?, 'source_missing', 'fixture', ?
           FROM range(0, ?) AS t(i)""",
        [report_date, field, NOW, count],
    )


def _insert_three_statements(duck: DuckDBStore, report_date: date, code: str = "000001") -> None:
    duck.write_query(
        """INSERT INTO balance_sheet
               (stock_code, report_date, total_assets, total_liabilities, total_equity)
           VALUES (?, ?, 100, 20, 80) ON CONFLICT DO NOTHING""",
        [code, report_date],
    )
    duck.write_query(
        """INSERT INTO income_statement (stock_code, report_date, revenue, parent_net_profit)
           VALUES (?, ?, 100, 10) ON CONFLICT DO NOTHING""",
        [code, report_date],
    )
    duck.write_query(
        """INSERT INTO cash_flow (stock_code, report_date, cf_from_operating)
           VALUES (?, ?, 10) ON CONFLICT DO NOTHING""",
        [code, report_date],
    )


def _set_refresh_state(sqlite: SQLiteStore, key: str, value: str) -> None:
    sqlite.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                          updated_at=excluded.updated_at""",
        [key, value, NOW.isoformat()],
    )


def _write_manifest(repo_root: Path, price_latest: str) -> Path:
    package = repo_root / ".planning" / "vd-datapackage-2026.09-20260920"
    package.mkdir(parents=True, exist_ok=True)
    manifest = package / "manifest.json"
    manifest.write_text(
        json.dumps({"as_of": {"price_latest": price_latest}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


# ─────────────────────────────────────────────────────────────────────────────
# 引擎契约
# ─────────────────────────────────────────────────────────────────────────────


def test_engine_covers_every_documented_rule(duckdb_store: DuckDBStore) -> None:
    """12 条规则一条都不能少——少一条就是少一条自动检查。"""
    payload = _payload(duckdb_store, repo_root=PROJECT_ROOT)
    assert [rule["code"] for rule in payload["rules"]] == list(RULE_CODES)
    assert len(RULE_CODES) == 12
    assert set(payload["level_counts"]) == {"P1", "P2", "P3"}


def test_single_rule_failure_is_reported_not_raised(
    duckdb_store: DuckDBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单条规则炸了必须如实记 error，其余规则照跑（体检不能整体失效）。"""

    def boom(_ctx: Any) -> list[Any]:
        raise RuntimeError("rule blew up")

    monkeypatch.setattr(asset_health, "_rule_deducted_profit_gap", boom)
    monkeypatch.setitem(
        asset_health.__dict__,
        "_RULES",
        tuple(
            (code, level, title, expensive, boom if code == "DEDUCTED_PROFIT_GAP" else check)
            for code, level, title, expensive, check in asset_health._RULES
        ),
    )
    payload = _payload(duckdb_store, repo_root=PROJECT_ROOT)
    assert _rule(payload, "DEDUCTED_PROFIT_GAP")["status"] == "error"
    assert payload["errors"][0]["code"] == "DEDUCTED_PROFIT_GAP"
    assert len(payload["rules"]) == 12


def test_expensive_rule_is_skipped_when_not_allowed(duckdb_store: DuckDBStore) -> None:
    payload = _payload(duckdb_store, repo_root=PROJECT_ROOT, allow_expensive=False)
    skipped = {item["code"] for item in payload["skipped"]}
    assert "CROSSCHECK_BELOW_THRESHOLD" in skipped


# ─────────────────────────────────────────────────────────────────────────────
# 规则 1 EXT_PERIOD_MISMATCH
# ─────────────────────────────────────────────────────────────────────────────


def test_ext_period_mismatch_fires_when_periods_differ(duckdb_store: DuckDBStore) -> None:
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"])
    _insert_extended(duckdb_store, date(2026, 6, 30), ["000001"])

    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "EXT_PERIOD_MISMATCH")

    assert len(findings) == 1
    assert findings[0]["level"] == "P1"
    assert findings[0]["metric"] == "2026-06-30"
    assert findings[0]["threshold"] == "2026-09-30"
    assert "compute-extended-indicators" in findings[0]["suggestion"]


def test_ext_period_mismatch_silent_when_periods_align(duckdb_store: DuckDBStore) -> None:
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"])
    _insert_extended(duckdb_store, date(2026, 9, 30), ["000001"])

    assert _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "EXT_PERIOD_MISMATCH") == []


# ─────────────────────────────────────────────────────────────────────────────
# 规则 2 FIELD_COVERAGE_LOW
# ─────────────────────────────────────────────────────────────────────────────


def test_field_coverage_low_fires_below_threshold(duckdb_store: DuckDBStore) -> None:
    _bulk_snapshot(duckdb_store, date(2026, 9, 30), null_every={"roe": 2})  # roe 50%

    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "FIELD_COVERAGE_LOW")

    roe = [item for item in findings if item["field"] == "roe"]
    assert len(roe) == 1
    assert roe[0]["metric"] == 50.0
    assert roe[0]["threshold"] == 90.0
    assert roe[0]["domain"] == "indicator_snapshot"


def test_field_coverage_low_flags_structural_null_fields(duckdb_store: DuckDBStore) -> None:
    """结构性可空的字段照样告警，但 detail 要说清"NULL 是正确表达"。

    reports/119 的教训之一：一个永远红且无法变绿的告警等于没有告警。
    """
    _bulk_extended(duckdb_store, date(2026, 6, 30), null_every={"leverage_total": 4})

    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "FIELD_COVERAGE_LOW")

    leverage = [item for item in findings if item["field"] == "leverage_total"]
    assert len(leverage) == 1
    assert "结构性可空" in leverage[0]["detail"]


def test_field_coverage_low_silent_at_full_coverage(duckdb_store: DuckDBStore) -> None:
    _bulk_snapshot(duckdb_store, date(2026, 9, 30))

    assert _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "FIELD_COVERAGE_LOW") == []


def test_field_coverage_low_covers_extended_domain(duckdb_store: DuckDBStore) -> None:
    """扩展域按**自己**的最新报告期算覆盖率（跨域错位是规则 1 的职责）。"""
    _bulk_extended(duckdb_store, date(2026, 6, 30), null_every={"free_cash_flow": 4})  # 25%

    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "FIELD_COVERAGE_LOW")

    assert any(item["domain"] == "indicator_ext" and item["field"] == "free_cash_flow"
               for item in findings)


# ─────────────────────────────────────────────────────────────────────────────
# 规则 3 FIELD_COVERAGE_DROP
# ─────────────────────────────────────────────────────────────────────────────


def test_field_coverage_low_skips_tiny_sample(duckdb_store: DuckDBStore) -> None:
    """小库（行数 < min_domain_rows）不判覆盖率：1 行数据只能是 0%/100%。"""
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"], null_fields={"roe"})

    assert _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "FIELD_COVERAGE_LOW") == []


def _write_baseline(
    duck: DuckDBStore,
    run_id: str,
    field_name: str,
    coverage_pct: float,
    *,
    domain: str = "indicator_snapshot",
    days_ago: int = 7,
) -> None:
    duck.write_query(
        """INSERT INTO field_coverage_history
               (recorded_at, run_id, domain, field_name, report_date,
                non_null_count, total_count, coverage_pct)
           VALUES (?, ?, ?, ?, ?, 100, 100, ?)""",
        [NOW - timedelta(days=days_ago), run_id, domain, field_name, date(2026, 9, 30),
         coverage_pct],
    )


def test_field_coverage_drop_fires_on_cliff(duckdb_store: DuckDBStore) -> None:
    _bulk_snapshot(duckdb_store, date(2026, 9, 30), null_every={"roe": 2})  # 50%
    _write_baseline(duckdb_store, "baseline-1", "roe", 99.9)

    findings = _findings(
        _payload(duckdb_store, repo_root=PROJECT_ROOT, run_id="doctor-current"),
        "FIELD_COVERAGE_DROP",
    )

    roe = [item for item in findings if item["field"] == "roe"]
    assert len(roe) == 1
    assert roe[0]["level"] == "P1"
    assert roe[0]["metric"] == 49.9
    assert "baseline-1" in roe[0]["detail"]


def test_field_coverage_drop_silent_for_small_drift(duckdb_store: DuckDBStore) -> None:
    _bulk_snapshot(duckdb_store, date(2026, 9, 30), null_every={"roe": 20})  # 95%
    _write_baseline(duckdb_store, "baseline-1", "roe", 99.9)  # 跌幅 4.9pp

    findings = _findings(
        _payload(duckdb_store, repo_root=PROJECT_ROOT, run_id="doctor-current"),
        "FIELD_COVERAGE_DROP",
    )

    assert [item for item in findings if item["field"] == "roe"] == []


def test_field_coverage_drop_skips_without_baseline(duckdb_store: DuckDBStore) -> None:
    _bulk_snapshot(duckdb_store, date(2026, 9, 30))

    payload = _payload(duckdb_store, repo_root=PROJECT_ROOT, run_id="doctor-current")

    assert _rule(payload, "FIELD_COVERAGE_DROP")["status"] == "skipped"
    assert _findings(payload, "FIELD_COVERAGE_DROP") == []


def test_field_coverage_drop_ignores_own_run(duckdb_store: DuckDBStore) -> None:
    """同一次体检写的基线不能给自己当对照（否则永远比出 0 跌幅）。"""
    _bulk_snapshot(duckdb_store, date(2026, 9, 30))
    _write_baseline(duckdb_store, "doctor-current", "roe", 99.9)

    payload = _payload(duckdb_store, repo_root=PROJECT_ROOT, run_id="doctor-current")

    assert _rule(payload, "FIELD_COVERAGE_DROP")["status"] == "skipped"


# ─────────────────────────────────────────────────────────────────────────────
# 规则 4 MANUAL_CHAIN_STALE
# ─────────────────────────────────────────────────────────────────────────────


def test_manual_chain_stale_fires_on_expired_marker(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    _set_refresh_state(sqlite_store, "hk_dividends_last_success_at",
                       (NOW - timedelta(days=200)).isoformat())

    findings = _findings(
        _payload(duckdb_store, sqlite_store, repo_root=PROJECT_ROOT), "MANUAL_CHAIN_STALE"
    )

    hk = [item for item in findings if item["domain"] == "hk_dividends"]
    assert len(hk) == 1
    assert hk[0]["level"] == "P1"
    assert hk[0]["metric"] == 200
    assert "vd data hk-dividends" in hk[0]["suggestion"]


def test_manual_chain_stale_silent_on_fresh_marker(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    _set_refresh_state(sqlite_store, "hk_dividends_last_success_at",
                       (NOW - timedelta(days=3)).isoformat())

    assert _findings(
        _payload(duckdb_store, sqlite_store, repo_root=PROJECT_ROOT), "MANUAL_CHAIN_STALE"
    ) == []


def test_manual_chain_stale_uses_legacy_marker_key(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """正式库现役键名是 `<chain>_last_refresh`（如 hk_dividends_last_refresh）。"""
    _set_refresh_state(sqlite_store, "hk_dividends_last_refresh",
                       (NOW - timedelta(days=200)).isoformat())

    findings = _findings(
        _payload(duckdb_store, sqlite_store, repo_root=PROJECT_ROOT), "MANUAL_CHAIN_STALE"
    )

    assert any(item["domain"] == "hk_dividends" for item in findings)


def test_manual_chain_stale_falls_back_to_data_probe(duckdb_store: DuckDBStore) -> None:
    """没有记账的链靠数据侧探针自愈：ETF 行情停了 30 天必须报出来。"""
    duckdb_store.write_query(
        """INSERT INTO etf_daily (etf_code, trade_date, close_price, source, fetch_time,
                                  raw_hash, confidence, batch_id)
           VALUES ('510300', ?, 1.0, 'fixture', ?, 'h', 'strict', 'b')""",
        [date(2026, 10, 15), NOW],
    )

    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "MANUAL_CHAIN_STALE")

    etf = [item for item in findings if item["domain"] == "etf_prices"]
    assert len(etf) == 1
    assert etf[0]["metric"] == 30
    assert etf[0]["threshold"] == 10


def test_manual_chain_stale_silent_without_any_evidence(duckdb_store: DuckDBStore) -> None:
    """空库（探针为空、无记账）不判超期：属于"还没开始"，不是"停摆"。"""
    assert _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "MANUAL_CHAIN_STALE") == []


# ─────────────────────────────────────────────────────────────────────────────
# 规则 5 SNAPSHOT_PERIOD_GAP
# ─────────────────────────────────────────────────────────────────────────────


def test_snapshot_period_gap_reuses_shared_judgment(duckdb_store: DuckDBStore) -> None:
    _insert_stock(duckdb_store)
    _insert_three_statements(duckdb_store, date(2026, 9, 30))
    _insert_snapshot(duckdb_store, date(2026, 6, 30), ["000001"])

    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "SNAPSHOT_PERIOD_GAP")

    assert len(findings) == 1
    assert findings[0]["metric"] == 1
    assert "000001" in findings[0]["detail"]


def test_snapshot_period_gap_silent_when_aligned(duckdb_store: DuckDBStore) -> None:
    _insert_stock(duckdb_store)
    _insert_three_statements(duckdb_store, date(2026, 9, 30))
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"])

    assert _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "SNAPSHOT_PERIOD_GAP") == []


# ─────────────────────────────────────────────────────────────────────────────
# 规则 6 STATIC_TABLE_STALE
# ─────────────────────────────────────────────────────────────────────────────


def test_static_table_stale_fires_one_period_behind(duckdb_store: DuckDBStore) -> None:
    _insert_three_statements(duckdb_store, date(2026, 9, 30))
    duckdb_store.write_query(
        """INSERT INTO company_employee_history
               (stock_code, report_date, employee_count, source, fetch_time, batch_id)
           VALUES ('000001', ?, 100, 'fixture', ?, 'b')""",
        [date(2026, 6, 30), NOW],
    )

    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "STATIC_TABLE_STALE")

    employee = [item for item in findings if item["domain"] == "company_employee_history"]
    assert len(employee) == 1
    assert employee[0]["level"] == "P2"
    assert employee[0]["metric"] == 1


def test_static_table_stale_silent_when_current(duckdb_store: DuckDBStore) -> None:
    _insert_three_statements(duckdb_store, date(2026, 9, 30))
    duckdb_store.write_query(
        """INSERT INTO company_employee_history
               (stock_code, report_date, employee_count, source, fetch_time, batch_id)
           VALUES ('000001', ?, 100, 'fixture', ?, 'b')""",
        [date(2026, 9, 30), NOW],
    )

    assert _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "STATIC_TABLE_STALE") == []


# ─────────────────────────────────────────────────────────────────────────────
# 规则 7 PACKAGE_STALE
# ─────────────────────────────────────────────────────────────────────────────


def test_package_stale_fires_when_trading_days_exceeded(
    duckdb_store: DuckDBStore, health_repo: Path
) -> None:
    _write_manifest(health_repo, "2026-01-05")
    duckdb_store.write_query(
        """INSERT INTO price_daily_raw (stock_code, trade_date, close)
           VALUES ('000001', ?, 10)""",
        [date(2026, 1, 20)],
    )

    findings = _findings(_payload(duckdb_store, repo_root=health_repo), "PACKAGE_STALE")

    assert len(findings) == 1
    assert findings[0]["level"] == "P2"
    assert findings[0]["metric"] == 11  # 1/5 之间的 11 个工作日
    assert findings[0]["threshold"] == 5


def test_package_stale_silent_when_package_is_current(
    duckdb_store: DuckDBStore, health_repo: Path
) -> None:
    _write_manifest(health_repo, "2026-01-19")
    duckdb_store.write_query(
        """INSERT INTO price_daily_raw (stock_code, trade_date, close)
           VALUES ('000001', ?, 10)""",
        [date(2026, 1, 20)],
    )

    assert _findings(_payload(duckdb_store, repo_root=health_repo), "PACKAGE_STALE") == []


def test_package_stale_skips_without_manifest(duckdb_store: DuckDBStore, health_repo: Path) -> None:
    payload = _payload(duckdb_store, repo_root=health_repo)
    assert _rule(payload, "PACKAGE_STALE")["status"] == "skipped"


# ─────────────────────────────────────────────────────────────────────────────
# 规则 8 NULL_REASON_MISSING
# ─────────────────────────────────────────────────────────────────────────────


def test_null_reason_missing_fires_below_registration_ratio(duckdb_store: DuckDBStore) -> None:
    _bulk_snapshot(duckdb_store, date(2026, 9, 30), null_every={"roe": 1})  # roe 全 NULL
    _bulk_register_null_reason(duckdb_store, "roe", date(2026, 9, 30), DEFAULT_SAMPLE // 2)

    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "NULL_REASON_MISSING")

    roe = [item for item in findings if item["field"] == "roe"]
    assert len(roe) == 1
    assert roe[0]["level"] == "P2"
    assert roe[0]["metric"] == 50.0


def test_null_reason_missing_silent_when_fully_registered(duckdb_store: DuckDBStore) -> None:
    _bulk_snapshot(duckdb_store, date(2026, 9, 30), null_every={"roe": 1})
    _bulk_register_null_reason(duckdb_store, "roe", date(2026, 9, 30), DEFAULT_SAMPLE)

    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "NULL_REASON_MISSING")

    assert [item for item in findings if item["field"] == "roe"] == []


def test_null_reason_missing_skips_on_empty_table(duckdb_store: DuckDBStore) -> None:
    """空表 = 登记链还没上线，按约定跳过不告警（否则一上线全站 P2）。"""
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"], null_fields={"roe"})

    payload = _payload(duckdb_store, repo_root=PROJECT_ROOT)

    assert _rule(payload, "NULL_REASON_MISSING")["status"] == "skipped"
    assert _findings(payload, "NULL_REASON_MISSING") == []


# ─────────────────────────────────────────────────────────────────────────────
# 规则 9 CROSSCHECK_BELOW_THRESHOLD
# ─────────────────────────────────────────────────────────────────────────────


def test_crosscheck_fires_when_agreement_below_threshold(duckdb_store: DuckDBStore) -> None:
    _insert_extended(duckdb_store, date(2026, 6, 30), ["000001"], values={"debt_to_equity": 1.0})
    duckdb_store.write_query(
        """INSERT INTO csmar_fi_t1
               (stock_code, report_date, "F011701A", source, fetch_time, batch_id)
           VALUES ('000001', ?, 2.0, 'fixture', ?, 'b')""",
        [date(2026, 6, 30), NOW],
    )

    payload = _payload(duckdb_store, repo_root=PROJECT_ROOT, allow_expensive=True)
    findings = _findings(payload, "CROSSCHECK_BELOW_THRESHOLD")

    assert len(findings) == 1
    assert findings[0]["field"] == "debt_to_equity"
    assert findings[0]["metric"] == 0.0
    assert findings[0]["threshold"] == 85.0


def test_crosscheck_silent_when_agreement_matches(duckdb_store: DuckDBStore) -> None:
    _insert_extended(duckdb_store, date(2026, 6, 30), ["000001"], values={"debt_to_equity": 1.0})
    duckdb_store.write_query(
        """INSERT INTO csmar_fi_t1
               (stock_code, report_date, "F011701A", source, fetch_time, batch_id)
           VALUES ('000001', ?, 1.0, 'fixture', ?, 'b')""",
        [date(2026, 6, 30), NOW],
    )

    payload = _payload(duckdb_store, repo_root=PROJECT_ROOT, allow_expensive=True)

    assert _findings(payload, "CROSSCHECK_BELOW_THRESHOLD") == []


# ─────────────────────────────────────────────────────────────────────────────
# 规则 10 SCHEMA_DDL_DRIFT
# ─────────────────────────────────────────────────────────────────────────────


def test_schema_ddl_drift_silent_when_schema_matches(
    duckdb_store: DuckDBStore, health_repo: Path
) -> None:
    """schema.py 的 DDL 与 init_duckdb_schema 建出来的表必须一模一样。"""
    assert _findings(_payload(duckdb_store, repo_root=health_repo), "SCHEMA_DDL_DRIFT") == []


def test_schema_ddl_drift_detects_missing_table(
    duckdb_store: DuckDBStore, health_repo: Path
) -> None:
    duckdb_store.execute_script("DROP TABLE csmar_fi_t6")

    findings = _findings(_payload(duckdb_store, repo_root=health_repo), "SCHEMA_DDL_DRIFT")

    assert len(findings) == 1
    assert findings[0]["field"] == "csmar_fi_t6"
    assert findings[0]["metric"] == "declared_not_in_db"
    assert findings[0]["level"] == "P1"


def test_schema_ddl_drift_detects_orphan_table(
    duckdb_store: DuckDBStore, health_repo: Path
) -> None:
    duckdb_store.execute_script("CREATE TABLE orphan_probe_table (x INTEGER)")

    findings = _findings(_payload(duckdb_store, repo_root=health_repo), "SCHEMA_DDL_DRIFT")

    assert len(findings) == 1
    assert findings[0]["field"] == "orphan_probe_table"
    assert findings[0]["metric"] == "in_db_not_declared"


def test_schema_ddl_drift_ignores_dynamic_archive_tables(
    duckdb_store: DuckDBStore, health_repo: Path
) -> None:
    """轮转/隔离表由维护脚本动态建，不算漂移（否则每次轮转都误报 P1）。"""
    duckdb_store.execute_script(
        """CREATE TABLE raw_response_archive_20260101_000000 AS SELECT 1 AS x;
           CREATE TABLE fetch_batch_quarantine AS SELECT 1 AS x"""
    )

    assert _findings(_payload(duckdb_store, repo_root=health_repo), "SCHEMA_DDL_DRIFT") == []


# ─────────────────────────────────────────────────────────────────────────────
# 规则 11 ZERO_REFERENCE_ASSET
# ─────────────────────────────────────────────────────────────────────────────


def test_zero_reference_asset_reports_every_registered_table(
    duckdb_store: DuckDBStore, health_repo: Path
) -> None:
    (health_repo / "config" / "asset_reference.yaml").write_text(
        """zero_reference_tables:
  - table: csmar_fi_t6
    domain: csmar_indicator_archive
    reason: "无人读"
    fulfillment: "口径裁决后登记核验对"
    retirement_candidate: true
  - table: not_in_db_table
    domain: archive_domain
    reason: "表都不在了"
""",
        encoding="utf-8",
    )

    findings = _findings(_payload(duckdb_store, repo_root=health_repo), "ZERO_REFERENCE_ASSET")

    assert {item["field"] for item in findings} == {"csmar_fi_t6", "not_in_db_table"}
    assert all(item["level"] == "P3" for item in findings)
    assert all(item["metric"] == 0 for item in findings)


def test_zero_reference_asset_flags_stale_inventory(
    duckdb_store: DuckDBStore, health_repo: Path
) -> None:
    """表一旦被运行时代码引用，清单就过期了——必须报出来让人回来改 yaml。"""
    (health_repo / "app" / "runtime_user.py").write_text(
        "TABLE = 'csmar_fi_t6'\n", encoding="utf-8"
    )
    (health_repo / "config" / "asset_reference.yaml").write_text(
        """zero_reference_tables:
  - table: csmar_fi_t6
    domain: csmar_indicator_archive
    reason: "无人读"
""",
        encoding="utf-8",
    )

    findings = _findings(_payload(duckdb_store, repo_root=health_repo), "ZERO_REFERENCE_ASSET")

    assert findings[0]["metric"] == 1
    assert "清单已过期" in findings[0]["detail"]


def test_shipped_asset_reference_matches_runtime_code(duckdb_store: DuckDBStore) -> None:
    """仓库里这份清单必须与 `app/` 的真实引用情况一致（12 张表全部零引用）。"""
    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "ZERO_REFERENCE_ASSET")

    assert len(findings) == 12
    assert {item["metric"] for item in findings} == {0}


# ─────────────────────────────────────────────────────────────────────────────
# 规则 12 DEDUCTED_PROFIT_GAP
# ─────────────────────────────────────────────────────────────────────────────


def test_deducted_profit_gap_fires_below_threshold(duckdb_store: DuckDBStore) -> None:
    _bulk_income(duckdb_store, date(2026, 9, 30), deducted_non_null=25)  # 25%

    findings = _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "DEDUCTED_PROFIT_GAP")

    assert len(findings) == 1
    assert findings[0]["level"] == "P1"
    assert findings[0]["metric"] == 25.0
    assert "patch_deducted_profit" in findings[0]["suggestion"]


def test_deducted_profit_gap_skips_tiny_sample(duckdb_store: DuckDBStore) -> None:
    """验收库只有几只股票时不判扣非覆盖率（否则夹具库永远 P1）。"""
    _insert_three_statements(duckdb_store, date(2026, 9, 30))

    payload = _payload(duckdb_store, repo_root=PROJECT_ROOT)

    assert _rule(payload, "DEDUCTED_PROFIT_GAP")["status"] == "skipped"
    assert _findings(payload, "DEDUCTED_PROFIT_GAP") == []


def test_deducted_profit_gap_silent_above_threshold(duckdb_store: DuckDBStore) -> None:
    _bulk_income(duckdb_store, date(2026, 9, 30), deducted_non_null=75)  # 75%

    assert _findings(_payload(duckdb_store, repo_root=PROJECT_ROOT), "DEDUCTED_PROFIT_GAP") == []


# ─────────────────────────────────────────────────────────────────────────────
# 与 data_quality 的集成契约
# ─────────────────────────────────────────────────────────────────────────────


def test_p1_codes_enter_warning_codes_but_never_block(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """P1 只披露、不遮蔽：既不进快照阻断集合，也不让 indicator_trust 全遮蔽。"""
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"])
    _insert_extended(duckdb_store, date(2026, 6, 30), ["000001"])

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert "EXT_PERIOD_MISMATCH" in status["warning_codes"]
    assert "asset_health" in status
    assert "EXT_PERIOD_MISMATCH" in status["asset_health"]["p1_codes"]
    assert "EXT_PERIOD_MISMATCH" not in _SNAPSHOT_BLOCKING_WARNINGS
    assert "EXT_PERIOD_MISMATCH" not in _SCREENING_BLOCKING_WARNINGS
    # 新 code 自己绝不遮蔽任何指标（untrusted_all 只能由既有快照阻断码触发）
    assert indicator_trust(["EXT_PERIOD_MISMATCH"])["untrusted_all"] is False
    assert indicator_trust(["EXT_PERIOD_MISMATCH"])["untrusted_fields"] == []


def test_p2_findings_do_not_enter_warning_codes(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    _insert_stock(duckdb_store)
    _insert_three_statements(duckdb_store, date(2026, 9, 30))
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"])
    duckdb_store.write_query(
        """INSERT INTO company_employee_history
               (stock_code, report_date, employee_count, source, fetch_time, batch_id)
           VALUES ('000001', ?, 100, 'fixture', ?, 'b')""",
        [date(2026, 6, 30), NOW],
    )

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["asset_health"]["code_counts"].get("STATIC_TABLE_STALE") == 1
    assert "STATIC_TABLE_STALE" not in status["warning_codes"]


def test_asset_health_runs_against_read_only_database(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """体检只读：跑完不能给库里添一行。"""
    before = duckdb_store.read_query("SELECT COUNT(*) AS cnt FROM field_coverage_history")[0]["cnt"]

    _payload(duckdb_store, sqlite_store, repo_root=PROJECT_ROOT)

    after = duckdb_store.read_query("SELECT COUNT(*) AS cnt FROM field_coverage_history")[0]["cnt"]
    assert before == after == 0


def test_run_doctor_attaches_history_block(duckdb_store: DuckDBStore) -> None:
    """`run_doctor` = 体检 +（可选）写基线；两条路径都要在输出里如实标注。"""
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"])

    written = run_doctor(duckdb_store, now=NOW, write_history=True, allow_expensive=False)
    disabled = run_doctor(duckdb_store, now=NOW, write_history=False, allow_expensive=False)

    assert written["history"]["status"] == "written"
    assert written["history"]["rows"] > 0
    assert disabled["history"]["status"] == "disabled"
    assert disabled["history"]["rows"] == 0


def test_write_history_persists_baseline(duckdb_store: DuckDBStore) -> None:
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"])

    result = write_field_coverage_history(duckdb_store, now=NOW, run_id="doctor-run-1")

    assert result["status"] == "written"
    rows = duckdb_store.read_query(
        "SELECT domain, field_name, coverage_pct FROM field_coverage_history WHERE run_id = ?",
        ["doctor-run-1"],
    )
    assert rows
    assert all(row["coverage_pct"] == 100.0 for row in rows)


def test_write_history_refuses_while_update_lock_active(
    duckdb_store: DuckDBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写锁期间写基线会把半成品覆盖率固化成"正常"，必须拒绝。"""
    monkeypatch.setattr(
        "app.core.storage.update_lock.any_write_lock_active", lambda _path: True
    )

    result = write_field_coverage_history(duckdb_store, now=NOW, run_id="doctor-run-2")

    assert result["status"] == "skipped"
    assert result["reason"] == "update_lock_active"


# ─────────────────────────────────────────────────────────────────────────────
# CLI 契约：退出码 0=无 P1 / 1=有 P1
# ─────────────────────────────────────────────────────────────────────────────


def _configure_cli(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.cli.main._database_stores",
        lambda *, initialize=True: (duckdb_store, sqlite_store),
    )


def test_cli_doctor_exits_zero_without_p1(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import typer

    from app.cli.main import data_doctor

    _configure_cli(duckdb_store, sqlite_store, monkeypatch)
    with pytest.raises(typer.Exit) as excinfo:
        data_doctor(json_output=True, write_history=False, fast=True, max_seconds=0.0)

    assert excinfo.value.exit_code == 0
    response = json.loads(capsys.readouterr().out)
    assert response["result"]["data"]["level_counts"]["P1"] == 0


def test_cli_doctor_exits_one_with_p1(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import typer

    from app.cli.main import data_doctor

    _configure_cli(duckdb_store, sqlite_store, monkeypatch)
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"])
    _insert_extended(duckdb_store, date(2026, 6, 30), ["000001"])
    with pytest.raises(typer.Exit) as excinfo:
        data_doctor(json_output=True, write_history=False, fast=True, max_seconds=0.0)

    assert excinfo.value.exit_code == 1
    response = json.loads(capsys.readouterr().out)
    assert "EXT_PERIOD_MISMATCH" in response["result"]["data"]["p1_codes"]


def test_cli_doctor_text_output_has_suggestion(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import typer

    from app.cli.main import data_doctor

    _configure_cli(duckdb_store, sqlite_store, monkeypatch)
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"])
    _insert_extended(duckdb_store, date(2026, 6, 30), ["000001"])
    with pytest.raises(typer.Exit):
        data_doctor(json_output=False, write_history=False, fast=True, max_seconds=0.0)

    output = capsys.readouterr().out
    assert "EXT_PERIOD_MISMATCH" in output
    assert "compute-extended-indicators" in output


def test_cli_doctor_reports_history_write(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import typer

    from app.cli.main import data_doctor

    _configure_cli(duckdb_store, sqlite_store, monkeypatch)
    _insert_snapshot(duckdb_store, date(2026, 9, 30), ["000001"])
    with pytest.raises(typer.Exit):
        data_doctor(json_output=True, write_history=True, fast=True, max_seconds=0.0)

    payload = json.loads(capsys.readouterr().out)["result"]["data"]
    assert payload["history"]["status"] == "written"
    assert payload["history"]["rows"] > 0
