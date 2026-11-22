"""补采 `refresh()` 不传 `periods` 时必须不崩（2026-11-22 job 270 实测缺陷）。

现象：`balance_sheet_ext` / `deducted_profit` / `fixed_assets` 三个补采步骤
同时变成 `failed`，`reason` 只有一句
`exception: TypeError: 'NoneType' object is not iterable`。

根因：`refresh()` 里拼 `lineage_payload` 时写的是**入参** `periods`（默认 `None`），
而不是解析后的 `resolved_periods` → `for p in None` 直接崩。
这个坑长期没暴露，是因为四个补采数据集长期处于 `deferred / all_pending_in_cooldown`
（7 天冷却）**在到达那一行之前就 return 了**；冷却期一过、真有待补工作，就立刻全崩。

本用例钉住：**不传 periods** 也必须能走到抓取阶段（不再 TypeError）。
"""

from __future__ import annotations

import pytest

from app.core.supplement import SPECS, SupplementFetcher


class _DryRunFetcher(SupplementFetcher):
    """只跑到"拼 lineage / 发请求"之前：抓取层整个换掉，避免真打网络。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.periods_seen: list[str] = []

    def _fetch_rows(self, spec, codes, periods, **kwargs):  # type: ignore[override]
        self.periods_seen = list(periods)
        return {}


def _seed_pending(duckdb_store, key: str) -> None:
    """造一只"已上市但目标表缺该期"的股票，确保 `pending_codes` 非空。

    ⚠️ 这一步是**必须的**：如果没有待补股票，`refresh()` 会在拼 `lineage_payload`
    之前就 return（`no_pending_codes` / `all_pending_in_cooldown`），
    于是那个 `for p in None` 的行**根本执行不到** —— 变异验证会假绿（实测踩过）。
    """
    from tests.support.seed import Seed

    seed = Seed(duckdb_store)
    seed.stock(
        "600519", name="贵州茅台", exchange="SSE",
        listing_date="2020-01-01", is_st=False, is_suspended=False,
    )
    # 三个数据集都靠"三表里有没有该期行"判待补：只塞一行很早的期，
    # 保证最新期一定缺；不同数据集 join 的是不同的表。
    seed.income("600519", "2019-12-31", revenue=1.0)
    seed.balance("600519", "2019-12-31", total_assets=1.0)


@pytest.mark.parametrize(
    "key",
    ["balance_sheet_ext", "deducted_profit", "fixed_assets_supplement"],
)
def test_refresh_without_explicit_periods_does_not_raise(duckdb_store, sqlite_store, key: str) -> None:
    _seed_pending(duckdb_store, key)
    fetcher = _DryRunFetcher(duck=duckdb_store, sqlite=sqlite_store)
    # 不传 periods（生产调用方的真实形状）
    report = fetcher.refresh(key, max_stocks=5, retry_days=0)

    # 关键断言：不能是 "exception: TypeError: 'NoneType' object is not iterable"
    assert "TypeError" not in str(report.get("reason") or ""), report.get("reason")
    # 而且确实用上了"解析后的默认期"（resolve_periods 按 backfill_periods 算出来的）
    expected = SupplementFetcher.resolve_periods(key)
    assert report["periods"] == expected, (report["periods"], expected)
    assert expected, "resolve_periods 不应为空"


def test_lineage_payload_still_carries_resolved_periods(duckdb_store, sqlite_store) -> None:
    """`lineage_payload` 里的 periods 必须是**解析后**的，不是入参 None。"""
    key = "balance_sheet_ext"
    _seed_pending(duckdb_store, key)
    fetcher = _DryRunFetcher(duck=duckdb_store, sqlite=sqlite_store)
    report = fetcher.refresh(key, max_stocks=5, retry_days=0)
    assert report["periods"] == SupplementFetcher.resolve_periods(key)
    # spec 有 period_floor 的（deducted_profit）也不能把更早的期塞进 payload
    if SPECS[key].period_floor is not None:
        floor = str(SPECS[key].period_floor)[:10]
        assert all(str(p)[:10] > floor for p in report["periods"])
