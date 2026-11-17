"""reports/131 §9.2 P0-1：科创板成交量被放大 100 倍 —— 适配器守卫 + 修复脚本。

覆盖三件事：

1. 腾讯 `day`/`qfqday` 的 bar[5]：主板/创业板为手数（×100 得股），
   科创板 688/689 已是股数（不可再乘）；
2. 量级自检兜底：归一后换手 > 500%（成交量/流通股本 > 5）物理不可能，
   记 warning 并按未乘 100 处理，防止其他板块未来也改单位；
3. `scripts/repair_star_volume_x100.py` 的历史修复：dry-run 不写、
   `--yes` 只改 688/689 断点之后的行、重复执行影响 0 行（幂等）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.core.adapters.base import FetchRequest
from app.core.adapters.tencent_adapter import TencentAdapter
from scripts.repair_star_volume_x100 import main as repair_main

_ADAPTER_LOGGER = "app.core.adapters.tencent_adapter"


class _KlineResponder:
    """按请求的 symbol 返回一根 bar 的腾讯 K 线假响应。"""

    def __init__(self, bars_by_symbol: dict[str, list[str]]) -> None:
        self._bars = bars_by_symbol
        self.symbols: list[str] = []

    def __call__(self, *args: object, **kwargs: object) -> _KlineResponder:
        param = str(kwargs["params"]["param"])  # type: ignore[index]
        fields = param.split(",")
        symbol, suffix = fields[0], fields[-1]
        self.symbols.append(symbol)
        key = "qfqday" if suffix == "qfq" else "day"
        bar = self._bars.get(symbol)
        return _KlineResponse({symbol: {key: [bar]}} if bar else {})

    def raise_for_status(self) -> None:  # pragma: no cover - 仅满足协议
        return None


class _KlineResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"data": self._payload}


def _fetch(monkeypatch: pytest.MonkeyPatch, bars: dict[str, list[str]], codes: list[str],
           provider=None):
    responder = _KlineResponder(bars)
    monkeypatch.setattr("app.core.adapters.tencent_adapter.requests.Session.get", responder)
    adapter = TencentAdapter(rate_limit=0, circ_shares_provider=provider)
    return adapter.fetch(
        FetchRequest(
            data_type="price_daily",
            stock_codes=codes,
            start_date="2026-07-31",
            end_date="2026-07-31",
            adjust="raw",
        )
    )


def test_star_board_volume_is_already_shares(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一批返回里 688 不乘 100、600 乘 100。"""
    result = _fetch(
        monkeypatch,
        {
            # 腾讯对科创板直接返回股数（688111 金山办公 2026-07-31 实测量级）
            "sh688111": ["2026-07-31", "280.0", "292.0", "295.0", "278.0", "28736925"],
            # 主板仍为手数
            "sh600000": ["2026-07-31", "10.0", "10.2", "10.3", "9.9", "33174"],
        },
        ["688111", "600000"],
        provider=lambda code: 1_200_000_000.0,  # 换手极低，不触发兜底
    )

    volumes = {row["stock_code"]: row["volume"] for row in result.data}
    assert volumes["688111"] == 28_736_925.0  # 不乘 100
    assert volumes["600000"] == 3_317_400.0  # ×100


def test_star_cdr_volume_is_not_scaled(monkeypatch: pytest.MonkeyPatch) -> None:
    """689（科创板 CDR）同样已是股数。"""
    result = _fetch(
        monkeypatch,
        {"sh689009": ["2026-07-31", "45.0", "46.0", "47.0", "44.0", "7358485"]},
        ["689009"],
        provider=lambda code: 735_848_500.0,
    )

    assert result.data[0]["volume"] == 7_358_485.0


def test_turnover_guard_falls_back_to_unscaled_volume(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """换手 >500% 物理不可能：按未乘 100 兜底，并记录可定位的 warning。"""
    # 600000 流通股本 1,000,000 股；腾讯给 60,000「手」→ ×100 = 6,000,000 股 = 换手 600%
    caplog.set_level(logging.WARNING, logger=_ADAPTER_LOGGER)
    result = _fetch(
        monkeypatch,
        {"sh600000": ["2026-07-31", "10.0", "10.2", "10.3", "9.9", "60000"]},
        ["600000"],
        provider=lambda code: 1_000_000.0,
    )

    assert result.data[0]["volume"] == 60_000.0  # 兜底：不乘 100
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings, "量级自检必须落 warning"
    message = warnings[0].getMessage()
    assert "600000" in message and "2026-07-31" in message
    assert "volume unit guard" in message


def test_missing_circ_shares_keeps_prefix_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """取不到流通股本时护栏退化为只看代码前缀，不得误伤主板。"""
    result = _fetch(
        monkeypatch,
        {"sh600000": ["2026-07-31", "10.0", "10.2", "10.3", "9.9", "33174"]},
        ["600000"],
        provider=lambda code: None,
    )

    assert result.data[0]["volume"] == 3_317_400.0


# ─── 修复脚本：dry-run / 只改 688 断点后 / 幂等 ─────────────────────────


@pytest.fixture
def repair_env(monkeypatch: pytest.MonkeyPatch, database_paths, tmp_path: Path) -> Path:
    """把脚本解析到 pytest 的临时档位（VD_ENV=test），并返回备份目录。"""
    monkeypatch.setenv("VD_ENV", "test")
    monkeypatch.setenv("VD_DUCKDB_PATH", str(database_paths.duckdb_path))
    monkeypatch.setenv("VD_SQLITE_PATH", str(database_paths.sqlite_path))
    monkeypatch.setenv("VD_TEST_RUN_ROOT", str(database_paths.run_root))
    return tmp_path / "backup"


def _seed(duck) -> None:
    with duck.write_connection() as conn:
        conn.executemany(
            """INSERT INTO price_daily_raw (stock_code, trade_date, open, close, volume)
               VALUES (?, CAST(? AS DATE), ?, ?, ?)""",
            [
                # 断点前：单位正确
                ("688111", "2026-07-30", 280.0, 285.0, 12_675_928.0),
                # 断点后：被放大 100 倍（227 倍跳变）
                ("688111", "2026-07-31", 280.0, 292.0, 2_873_692_500.0),
                # 非科创板：不在修复范围
                ("600519", "2026-07-31", 1600.0, 1610.0, 1_234_500.0),
            ],
        )
        conn.execute(
            """INSERT INTO price_daily_qfq (stock_code, trade_date, open, close, volume)
               SELECT stock_code, trade_date, open, close, volume FROM price_daily_raw"""
        )


def _volumes(duck, table: str) -> dict[tuple[str, str], float]:
    rows = duck.read_query(
        f"SELECT stock_code, CAST(trade_date AS VARCHAR) AS d, volume FROM {table}"
    )
    return {(str(row["stock_code"]), str(row["d"])): float(row["volume"]) for row in rows}


def _run(backup_root: Path, *extra: str) -> int:
    return repair_main(["--backup-root", str(backup_root), *extra])


def test_repair_script_dry_run_then_yes_then_idempotent(
    duckdb_store,
    repair_env: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _seed(duckdb_store)
    before = _volumes(duckdb_store, "price_daily_raw")

    # ── 1) dry-run 不写 ───────────────────────────────────────────────
    assert _run(repair_env) == 0
    assert _volumes(duckdb_store, "price_daily_raw") == before
    assert not repair_env.exists() or not list(repair_env.iterdir())

    # ── 2) --yes 只改 688 断点之后那一行 ──────────────────────────────
    assert _run(repair_env, "--yes") == 0
    raw = _volumes(duckdb_store, "price_daily_raw")
    qfq = _volumes(duckdb_store, "price_daily_qfq")
    assert raw[("688111", "2026-07-31")] == pytest.approx(28_736_925.0)
    assert raw[("688111", "2026-07-30")] == pytest.approx(12_675_928.0)  # 断点前不动
    assert raw[("600519", "2026-07-31")] == pytest.approx(1_234_500.0)  # 非科创板不动
    assert qfq[("688111", "2026-07-31")] == pytest.approx(28_736_925.0)
    assert qfq[("600519", "2026-07-31")] == pytest.approx(1_234_500.0)

    # 备份：两张表各一份 parquet + plan.json（记录断点与行数）
    backups = sorted(repair_env.glob("maintenance-backup-star-volume-*"))
    assert len(backups) == 1
    assert (backups[0] / "price_daily_raw.parquet").exists()
    assert (backups[0] / "price_daily_qfq.parquet").exists()
    plan = json.loads((backups[0] / "plan.json").read_text(encoding="utf-8"))
    items = plan["tables"]["price_daily_raw"]["items"]
    assert [(item["stock_code"], item["boundary"]) for item in items] == [("688111", "2026-07-31")]
    assert plan["tables"]["price_daily_raw"]["row_count"] == 1

    # ── 3) 再跑一次：影响 0 行（幂等）────────────────────────────────
    caplog.set_level(logging.INFO, logger="repair_star_volume_x100")
    assert _run(repair_env, "--yes") == 0
    assert _volumes(duckdb_store, "price_daily_raw") == raw
    assert _volumes(duckdb_store, "price_daily_qfq") == qfq
    messages = [record.getMessage() for record in caplog.records]
    assert any("没有需要修复的行" in message for message in messages)
    assert len(list(repair_env.glob("maintenance-backup-star-volume-*"))) == 1
