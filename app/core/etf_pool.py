"""ETF 池配置与合并结果导入（2026-09-05 用户定稿）

分层口径（用户确认）：
- industry：申万一级行业代表 ETF；无合适工具的行业不加入观察
  （纺织服饰/轻工制造/商贸零售/综合/美容护理 无流动性合格工具 → 剔除）。
- strategy：策略因子 ETF（红利/红利低波/央企红利/A500）。
- market：市场指数 ETF（沪深300/中证500/中证1000/恒生科技——恒生科技
  视为市场 ETF，与沪深300 同层）。

明天用户交付合并调仓结果后，用 `import_etf_merge_csv` 一次性导入；
模板见 `write_merge_template`。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from app.core.etf_strategy import (
    add_cash_flow,
    add_etf_trade,
    upsert_etf_meta,
)
from app.core.storage.sqlite_store import SQLiteStore

__all__ = ["DEFAULT_ETF_POOL", "seed_etf_pool", "import_etf_merge_csv", "write_merge_template"]


# (etf_code, name, category, track_index_code, track_index_name, primary_metric, industry_group)
DEFAULT_ETF_POOL: tuple[dict[str, Any], ...] = tuple([
    # ─── 行业层（申万一级，有合格工具的 26 个） ─────────────────────────
    {"etf_code": "159825", "name": "农业ETF", "category": "industry",
     "track_index_code": "SW801010", "track_index_name": "农林牧渔",
     "primary_metric": "pe", "industry_group": "农林牧渔"},
    {"etf_code": "516020", "name": "化工ETF", "category": "industry",
     "track_index_code": "SW801030", "track_index_name": "基础化工",
     "primary_metric": "pe", "industry_group": "基础化工"},
    {"etf_code": "515210", "name": "钢铁ETF", "category": "industry",
     "track_index_code": "SW801040", "track_index_name": "钢铁",
     "primary_metric": "pb", "industry_group": "钢铁"},
    {"etf_code": "512400", "name": "有色金属ETF", "category": "industry",
     "track_index_code": "SW801050", "track_index_name": "有色金属",
     "primary_metric": "pb", "industry_group": "有色金属"},
    {"etf_code": "159995", "name": "芯片ETF", "category": "industry",
     "track_index_code": "SW801080", "track_index_name": "电子",
     "primary_metric": "pe", "industry_group": "电子"},
    {"etf_code": "516110", "name": "汽车ETF", "category": "industry",
     "track_index_code": "SW801880", "track_index_name": "汽车",
     "primary_metric": "pe", "industry_group": "汽车"},
    {"etf_code": "159996", "name": "家电ETF", "category": "industry",
     "track_index_code": "SW801110", "track_index_name": "家用电器",
     "primary_metric": "pe", "industry_group": "家用电器"},
    {"etf_code": "515170", "name": "食品饮料ETF", "category": "industry",
     "track_index_code": "SW801120", "track_index_name": "食品饮料",
     "primary_metric": "pe", "industry_group": "食品饮料"},
    {"etf_code": "512010", "name": "医药ETF", "category": "industry",
     "track_index_code": "SW801150", "track_index_name": "医药生物",
     "primary_metric": "pe", "industry_group": "医药生物"},
    {"etf_code": "561560", "name": "电力ETF", "category": "industry",
     "track_index_code": "SW801160", "track_index_name": "公用事业",
     "primary_metric": "pb", "industry_group": "公用事业"},
    {"etf_code": "159666", "name": "交通运输ETF", "category": "industry",
     "track_index_code": "SW801170", "track_index_name": "交通运输",
     "primary_metric": "pe", "industry_group": "交通运输"},
    {"etf_code": "512200", "name": "地产ETF", "category": "industry",
     "track_index_code": "SW801180", "track_index_name": "房地产",
     "primary_metric": "pb", "industry_group": "房地产"},
    {"etf_code": "562510", "name": "旅游ETF", "category": "industry",
     "track_index_code": "SW801210", "track_index_name": "社会服务",
     "primary_metric": "pe", "industry_group": "社会服务"},
    {"etf_code": "512800", "name": "银行ETF", "category": "industry",
     "track_index_code": "SW801780", "track_index_name": "银行",
     "primary_metric": "pb", "industry_group": "银行"},
    {"etf_code": "512880", "name": "证券ETF", "category": "industry",
     "track_index_code": "SW801790", "track_index_name": "非银金融(代理:证券公司)",
     "primary_metric": "pb", "industry_group": "非银金融"},
    {"etf_code": "516750", "name": "建材ETF", "category": "industry",
     "track_index_code": "SW801710", "track_index_name": "建筑材料",
     "primary_metric": "pb", "industry_group": "建筑材料"},
    {"etf_code": "516970", "name": "基建50ETF", "category": "industry",
     "track_index_code": "SW801720", "track_index_name": "建筑装饰(代理:基建工程)",
     "primary_metric": "pb", "industry_group": "建筑装饰"},
    {"etf_code": "515790", "name": "光伏ETF", "category": "industry",
     "track_index_code": "SW801730", "track_index_name": "电力设备",
     "primary_metric": "pe", "industry_group": "电力设备"},
    {"etf_code": "516960", "name": "机械ETF", "category": "industry",
     "track_index_code": "SW801890", "track_index_name": "机械设备",
     "primary_metric": "pe", "industry_group": "机械设备"},
    {"etf_code": "512660", "name": "军工ETF", "category": "industry",
     "track_index_code": "SW801740", "track_index_name": "国防军工",
     "primary_metric": "pe", "industry_group": "国防军工"},
    {"etf_code": "512720", "name": "计算机ETF", "category": "industry",
     "track_index_code": "SW801750", "track_index_name": "计算机",
     "primary_metric": "pe", "industry_group": "计算机"},
    {"etf_code": "512980", "name": "传媒ETF", "category": "industry",
     "track_index_code": "SW801760", "track_index_name": "传媒",
     "primary_metric": "pe", "industry_group": "传媒"},
    {"etf_code": "515880", "name": "通信ETF", "category": "industry",
     "track_index_code": "SW801770", "track_index_name": "通信",
     "primary_metric": "pe", "industry_group": "通信"},
    {"etf_code": "515220", "name": "煤炭ETF", "category": "industry",
     "track_index_code": "SW801950", "track_index_name": "煤炭",
     "primary_metric": "pb", "industry_group": "煤炭"},
    {"etf_code": "561360", "name": "石油ETF", "category": "industry",
     "track_index_code": "SW801960", "track_index_name": "石油石化",
     "primary_metric": "pb", "industry_group": "石油石化"},
    {"etf_code": "512580", "name": "环保ETF", "category": "industry",
     "track_index_code": "SW801970", "track_index_name": "环保",
     "primary_metric": "pe", "industry_group": "环保"},
    # ─── 策略层 ────────────────────────────────────────────────────────
    {"etf_code": "510880", "name": "红利ETF", "category": "strategy",
     "track_index_code": "000015", "track_index_name": "上证红利",
     "primary_metric": "pe", "industry_group": "红利"},
    {"etf_code": "512890", "name": "红利低波ETF", "category": "strategy",
     "track_index_code": "000015", "track_index_name": "上证红利(代理:红利低波)",
     "primary_metric": "pe", "industry_group": "红利低波"},
    {"etf_code": "513910", "name": "红利央企ETF", "category": "strategy",
     "track_index_code": "000015", "track_index_name": "上证红利(代理:央企红利)",
     "primary_metric": "pe", "industry_group": "央企红利"},
    {"etf_code": "512050", "name": "中证A500ETF", "category": "strategy",
     "track_index_code": "000906", "track_index_name": "中证800(代理:A500)",
     "primary_metric": "pe", "industry_group": "核心资产"},
    # ─── 市场层（含恒生科技，2026-09-05 用户定稿） ──────────────────────
    {"etf_code": "510300", "name": "沪深300ETF", "category": "market",
     "track_index_code": "000300", "track_index_name": "沪深300",
     "primary_metric": "pe", "industry_group": "市场指数"},
    {"etf_code": "510500", "name": "中证500ETF", "category": "market",
     "track_index_code": "000905", "track_index_name": "中证500",
     "primary_metric": "pe", "industry_group": "市场指数"},
    {"etf_code": "512100", "name": "中证1000ETF", "category": "market",
     "track_index_code": "000852", "track_index_name": "中证1000",
     "primary_metric": "pe", "industry_group": "市场指数"},
    {"etf_code": "513130", "name": "恒生科技", "category": "market",
     "track_index_code": None, "track_index_name": "恒生科技(同花顺5年分位)",
     "primary_metric": "pe", "industry_group": "市场指数"},
])


def seed_etf_pool(sqlite: SQLiteStore, *, apply: bool = False) -> dict[str, Any]:
    """把默认池写入 etf_meta（只插入缺失代码，不覆盖已有预算/启用状态）。"""
    if apply:
        with sqlite.transaction() as conn:
            for item in DEFAULT_ETF_POOL:
                conn.execute(
                    """INSERT OR IGNORE INTO etf_meta
                       (etf_code, name, category, track_index_code, track_index_name,
                        primary_metric, industry_group, budget, step_pct, enabled, note, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, 5, 1, NULL, ?)""",
                    [
                        item["etf_code"], item["name"], item["category"],
                        item["track_index_code"], item["track_index_name"],
                        item["primary_metric"], item["industry_group"],
                        _now_iso(),
                    ],
                )
        return {"status": "applied", "total": len(DEFAULT_ETF_POOL)}
    return {
        "status": "preview",
        "total": len(DEFAULT_ETF_POOL),
        "by_category": {
            category: sum(1 for item in DEFAULT_ETF_POOL if item["category"] == category)
            for category in ("industry", "strategy", "market")
        },
        "items": [dict(item) for item in DEFAULT_ETF_POOL],
    }


def write_merge_template(path: Path) -> dict[str, Any]:
    """生成明天合并结果导入模板（UTF-8 BOM，Excel 直接可开）。"""
    header = [
        "row_type", "etf_code", "name", "category", "track_index_code",
        "track_index_name", "primary_metric", "industry_group", "budget", "step_pct",
        "trade_date", "direction", "price", "shares", "amount", "fee", "note",
        "flow_date", "cash_direction", "cash_amount",
    ]
    rows = [
        # 说明行：row_type=meta 定义 ETF；trade 定义流水；cash 定义资金
        ["meta", "512880", "证券ETF", "industry", "SW801790", "非银金融", "pb",
         "非银金融", "", "5", "", "", "", "", "", "", "", "", "", ""],
        ["trade", "512880", "", "", "", "", "", "", "", "", "2026-09-04",
         "buy", "1.100", "100", "110", "0.1", "合并后首笔", "", "", ""],
        ["cash", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
         "", "2026-09-04", "in", "1000"],
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return {"template": str(path), "columns": header}


def import_etf_merge_csv(sqlite: SQLiteStore, path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """导入合并结果 CSV（meta/trade/cash 三类行，逐行校验，幂等）。"""
    issues: list[str] = []
    metas: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    cash: list[dict[str, Any]] = []

    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            row_type = str(row.get("row_type", "")).strip().lower()
            try:
                if row_type == "meta":
                    category = str(row.get("category") or "industry").strip().lower()
                    budget = _opt_float(row.get("budget")) or 0.0
                    step = _opt_float(row.get("step_pct")) or 5.0
                    metas.append({
                        "etf_code": _req(row, "etf_code"),
                        "name": _req(row, "name"),
                        "category": category,
                        "track_index_code": _opt_str(row.get("track_index_code")),
                        "track_index_name": _opt_str(row.get("track_index_name")),
                        "primary_metric": str(row.get("primary_metric") or "pe").strip().lower(),
                        "industry_group": _opt_str(row.get("industry_group")),
                        "budget": budget,
                        "step_pct": step,
                    })
                elif row_type == "trade":
                    trades.append({
                        "etf_code": _req(row, "etf_code"),
                        "trade_date": _req(row, "trade_date"),
                        "direction": "buy" if str(row.get("direction") or "").strip() == "买入"
                                     else str(row.get("direction") or "").strip().lower(),
                        "price": _req_float(row, "price"),
                        "shares": _req_float(row, "shares"),
                        "amount": _opt_float(row.get("amount")),
                        "fee": _opt_float(row.get("fee")) or 0.0,
                        "note": _opt_str(row.get("note")),
                    })
                elif row_type == "cash":
                    cash.append({
                        "flow_date": _req(row, "flow_date"),
                        "direction": "in" if str(row.get("cash_direction") or "").strip() == "入金"
                                     else str(row.get("cash_direction") or "").strip().lower(),
                        "amount": _req_float(row, "cash_amount"),
                        "note": _opt_str(row.get("note")),
                    })
                else:
                    issues.append(f"第 {line_no} 行 row_type 未知: {row_type!r}")
            except ValueError as error:
                issues.append(f"第 {line_no} 行解析失败: {error}")

    if issues:
        return {"status": "invalid", "dry_run": dry_run, "issues": issues}

    if dry_run:
        return {
            "status": "preview", "dry_run": True,
            "metas": len(metas), "trades": len(trades), "cash": len(cash),
            "issues": issues,
        }

    for meta in metas:
        upsert_etf_meta(sqlite, **meta)
    inserted_trades = 0
    for trade in trades:
        if _trade_exists(sqlite, trade):
            continue
        add_etf_trade(sqlite, **trade)
        inserted_trades += 1
    inserted_cash = 0
    for flow in cash:
        rows = sqlite.query(
            """SELECT COUNT(*) AS c FROM etf_cash_flows
               WHERE flow_date = ? AND direction = ? AND amount = ?""",
            [flow["flow_date"], flow["direction"], flow["amount"]],
        )
        if int(rows[0]["c"]) > 0:
            continue
        add_cash_flow(sqlite, **flow)
        inserted_cash += 1
    return {
        "status": "ok",
        "metas": len(metas),
        "trades_total": len(trades),
        "trades_inserted": inserted_trades,
        "cash_total": len(cash),
        "cash_inserted": inserted_cash,
    }


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _req(row: dict[str, str | None], key: str) -> str:
    value = (row.get(key) or "").strip()
    if not value:
        raise ValueError(f"缺少 {key}")
    return value


def _opt_str(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _req_float(row: dict[str, str | None], key: str) -> float:
    value = _opt_float(row.get(key))
    if value is None:
        raise ValueError(f"缺少 {key}")
    return value


def _opt_float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as error:
        raise ValueError(f"非法数字: {text!r}") from error


def _trade_exists(sqlite: SQLiteStore, trade: dict[str, Any]) -> bool:
    rows = sqlite.query(
        """SELECT COUNT(*) AS c FROM etf_trades
           WHERE etf_code = ? AND trade_date = ? AND direction = ?
             AND price = ? AND shares = ?""",
        [trade["etf_code"], trade["trade_date"], trade["direction"],
         trade["price"], trade["shares"]],
    )
    return int(rows[0]["c"]) > 0
