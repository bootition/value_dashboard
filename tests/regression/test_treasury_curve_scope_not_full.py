"""国债曲线变化**不得**升级成全量重算（2026-11-23 生产事故）。

现场：更新每天要跑 2.4 小时（39 只/分钟 × 5,575 只）。根因不是算得慢，而是
国债曲线**每个交易日都会新增一行**（`treasury_yield_curve` 每天一条 curve_date），
全表内容指纹因此**每交易日必变** → `_decide_full_reason` 返回
`treasury_curve_changed` → 「5,575 只 × 149 列」全量重算。

而这批变化真正影响的只有 `ttm_dividend_yield` + `div_yield_spread_*`，
早就有秒级快路径 `calculator.refresh_treasury_spreads()`。

本用例钉住：
1. 曲线变化**不再**单独触发全量裁决；
2. 但曲线变化仍必须被**如实记录**（不能为了快而假装没变）；
3. 日历变化仍然触发全量（MA/量能整列断窗口，是真的 149 列全变）；
4. 快路径必须覆盖**全市场**（传 None），不能只刷一小撮。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.update_parts import indicator_scope as scope_module
from app.core.update_parts.indicator_scope import (
    COMPUTATION_VERSION_KEY,
    SOURCE_DOMAIN,
    current_computation_version,
)


def _owner_with_snapshot(duckdb_store, sqlite_store):
    """`_full_reason` 会先查快照行数与版本水位：两者都要"已就绪"，
    否则会提前返回 `first_run` / `computation_version`，测不到曲线判定。"""
    duckdb_store.write_query(
        "INSERT INTO indicator_snapshot (stock_code, report_date) "
        "VALUES (?, ?)",
        ["600519", "2026-09-30"],
    )
    # 周期兜底水位也要"刚跑过"：否则去掉曲线分支后 `periodic_full_refresh`
    # 会接上下一个分支，测到的就不是曲线判定了。
    from datetime import UTC, datetime

    now_iso = datetime.now(UTC).isoformat()

    def _read_refresh_state(key: str) -> str | None:
        if key == COMPUTATION_VERSION_KEY:
            return current_computation_version()
        if key == scope_module.PERIODIC_FULL_KEY:
            return now_iso
        return None

    return SimpleNamespace(
        duck=duckdb_store,
        sqlite=sqlite_store,
        _read_refresh_state=_read_refresh_state,
    )


def test_treasury_curve_change_no_longer_forces_full(duckdb_store, sqlite_store) -> None:
    """`_decide_full_reason(treasury_curve_changed=True)` 必须返回 None。

    改前：返回 `"treasury_curve_changed"` → 全量 5,575×149 列 ≈ 2.4 小时/天。
    改后：不返回任何全量理由，利差交给 `refresh_treasury_spreads()` 秒级快路径。
    """
    owner = _owner_with_snapshot(duckdb_store, sqlite_store)
    reason, payload = scope_module._full_reason(
        owner,
        force_full=False,
        treasury_curve_changed=True,
        trading_calendar_changed=False,
    )
    assert reason is None, (
        f"国债曲线变化不得触发全量重算（实际 {reason!r}）——"
        "曲线每交易日新增一行，升格全量等于每天白烧 2.4 小时"
    )
    # 仍要如实记录版本号，方便事后追溯（只是不再是全量理由）
    assert "computation_version" in payload


def test_trading_calendar_change_still_forces_full(duckdb_store, sqlite_store) -> None:
    """对照组：日历变化**必须**保持全量（连续窗口整列断开，是真的 149 列全变）。"""
    owner = _owner_with_snapshot(duckdb_store, sqlite_store)
    reason, _ = scope_module._full_reason(
        owner,
        force_full=False,
        treasury_curve_changed=False,
        trading_calendar_changed=True,
    )
    assert reason == "trading_calendar_changed"


def test_treasury_curve_changed_is_registered_in_source_domain() -> None:
    """`treasury_curve_changed` 仍要有明确归属（自证纪律），但**不能是 full**。"""
    assert "treasury_curve_changed" in SOURCE_DOMAIN
    assert SOURCE_DOMAIN["treasury_curve_changed"] != "full", (
        "归 full 会让曲线每日新增行触发全量重算"
    )


def test_scope_plan_flag_is_what_flow_uses_for_all_market_refresh() -> None:
    """flow 依赖 `scope_plan.treasury_curve_changed` 决定走快路径；字段必须存在。"""
    plan = scope_module.IndicatorScopePlan(
        mode="skipped", reason=None, computation_version="v", listed_count=0,
    )
    assert hasattr(plan, "treasury_curve_changed")
    assert plan.treasury_curve_changed is False
