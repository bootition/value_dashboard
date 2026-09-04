"""A股ETF交易记录 Excel 导入（2026-09-05）

一次性导入用户的 `A股ETF交易记录.xlsx`（交易流水/资金流水/持仓看板/ETF基础信息）。
- 幂等：同一 (etf_code, trade_date, direction, price, shares) 已存在则跳过。
- 手续费计入成本（买入 amount+fee）；卖出净收 amount-fee。
- 旧「最新价/下次买入价/持仓市值」不导入，由网格引擎重算。
- 跟踪指数为初始映射（行业 ETF → 申万一级行业代码；特殊标的标 note 待核），
  用户可在界面修改；拿不到估值历史的指数按「不可得」展示，不伪造信号。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.etf_strategy import add_cash_flow, add_etf_trade, set_setting, upsert_etf_meta
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_TRACK_INDEX_MAP", "import_etf_xlsx", "preview_etf_xlsx"]

# 用户现有 16 只持仓 → (跟踪指数代码, 名称, 主指标)
# 行业 ETF 优先映射到申万一级行业（估值历史可得）；红利类以宽基红利代理；
# 港股/中概无申万/乐咕估值历史，暂标 None（后续用同花顺五年分位补充）。
DEFAULT_TRACK_INDEX_MAP: dict[str, tuple[str | None, str | None, str]] = {
    "512690": ("SW801120", "食品饮料(代理:中证酒)", "pe"),
    "515170": ("SW801120", "食品饮料", "pe"),
    "159883": ("SW801150", "医药生物(代理:医疗器械)", "pe"),
    "512200": ("SW801180", "房地产", "pb"),
    "512880": ("SW801790", "非银金融(代理:证券公司)", "pb"),
    "159996": ("SW801110", "家用电器", "pe"),
    "513910": ("000015", "上证红利(代理:央企红利)", "pe"),
    "159758": ("000015", "上证红利(代理:红利50)", "pe"),
    "159825": ("SW801010", "农林牧渔", "pe"),
    "159865": ("SW801010", "农林牧渔(代理:养殖)", "pb"),
    "516970": ("SW801720", "建筑装饰(代理:基建工程)", "pb"),
    "512010": ("SW801150", "医药生物", "pe"),
    "512170": ("SW801150", "医药生物(代理:中证医疗)", "pe"),
    "560080": ("SW801150", "医药生物(代理:中证中药)", "pe"),
    "513130": (None, "恒生科技(估值历史待补)", "pe"),
    "159605": (None, "中国互联网50(估值历史待补)", "pe"),
}

_SHEET_TRADES = "交易流水"
_SHEET_CASH = "资金流水"
_SHEET_BOARD = "持仓看板"
_SHEET_META = "ETF基础信息"


def _parse_date(value: Any) -> str:
    """Excel 日期/20251230 字符串/时间戳 → YYYY-MM-DD。"""
    if value is None or (isinstance(value, float) and value != value):
        raise ValueError("交易日期为空")
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    raise ValueError(f"无法解析日期: {value!r}")


def _read_sheets(path: Path) -> dict[str, Any]:
    import pandas as pd

    workbook = pd.ExcelFile(path)
    required = {_SHEET_TRADES, _SHEET_CASH, _SHEET_BOARD, _SHEET_META}
    missing = required - set(workbook.sheet_names)
    if missing:
        raise ValueError(f"Excel 缺少 sheet: {sorted(missing)}")
    trades = workbook.parse(_SHEET_TRADES)
    cash = workbook.parse(_SHEET_CASH)
    board = workbook.parse(_SHEET_BOARD)
    meta = workbook.parse(_SHEET_META)
    return {"trades": trades, "cash": cash, "board": board, "meta": meta}


def preview_etf_xlsx(path: Path) -> dict[str, Any]:
    """只读解析 + 校验预览（不写库）。"""
    sheets = _read_sheets(Path(path))
    trades: list[dict[str, Any]] = []
    issues: list[str] = []
    for idx, row in sheets["trades"].iterrows():
        try:
            direction_raw = str(row.get("方向", "")).strip()
            direction = "buy" if direction_raw == "买入" else "sell" if direction_raw == "卖出" else None
            if direction is None:
                issues.append(f"交易流水第 {idx + 2} 行方向未知: {direction_raw!r}")
                continue
            price = float(row.get("成交价格"))
            shares = float(row.get("成交份额"))
            amount = row.get("成交金额")
            trades.append({
                "etf_code": str(row.get("ETF代码", "")).strip(),
                "trade_date": _parse_date(row.get("交易日期")),
                "direction": direction,
                "price": price,
                "shares": shares,
                "amount": float(amount) if amount is not None and amount == amount else None,
                "fee": float(row.get("手续费") or 0),
                "note": None if row.get("备注") is None or row.get("备注") != row.get("备注") else str(row.get("备注")),
            })
        except Exception as error:  # noqa: BLE001
            issues.append(f"交易流水第 {idx + 2} 行解析失败: {error}")

    cash_flows: list[dict[str, Any]] = []
    for idx, row in sheets["cash"].iterrows():
        try:
            direction_raw = str(row.get("类型(入金/出金)", "")).strip()
            direction = "in" if direction_raw == "入金" else "out" if direction_raw == "出金" else None
            if direction is None:
                issues.append(f"资金流水第 {idx + 2} 行类型未知: {direction_raw!r}")
                continue
            cash_flows.append({
                "flow_date": _parse_date(row.get("日期")),
                "direction": direction,
                "amount": float(row.get("金额")),
                "note": None if row.get("备注") is None or row.get("备注") != row.get("备注") else str(row.get("备注")),
            })
        except Exception as error:  # noqa: BLE001
            issues.append(f"资金流水第 {idx + 2} 行解析失败: {error}")

    metas: list[dict[str, Any]] = []
    for idx, row in sheets["meta"].iterrows():
        try:
            code = str(row.get("ETF代码", "")).strip()
            track_code, track_name, primary_metric = DEFAULT_TRACK_INDEX_MAP.get(
                code, (None, None, "pe")
            )
            metas.append({
                "etf_code": code,
                "name": str(row.get("ETF名称", "")).strip(),
                "track_index_code": track_code,
                "track_index_name": track_name or str(row.get("行业名称", "")).strip(),
                "primary_metric": primary_metric,
                "industry_group": str(row.get("行业名称", "")).strip() or None,
                "budget": 0.0,
                "step_pct": 5.0,
                "note": str(row.get("备注", "")).strip() or None,
            })
        except Exception as error:  # noqa: BLE001
            issues.append(f"ETF基础信息第 {idx + 2} 行解析失败: {error}")

    total_assets = None
    board_rows = sheets["board"].iloc[:, 0].astype(str)
    for value, label in zip(board_rows, sheets["board"].iloc[:, 1].astype(str), strict=False):
        if "ETF策略总资产" in str(value) or "ETF策略总资产" in str(label):
            try:
                total_assets = float(label if "ETF策略总资产" in str(value) else value)
            except ValueError:
                issues.append(f"持仓看板总资产无法解析: {label!r}")
            break
    return {
        "trades": trades,
        "cash_flows": cash_flows,
        "metas": metas,
        "total_assets": total_assets,
        "issues": issues,
    }


def _trade_exists(sqlite: SQLiteStore, trade: dict[str, Any]) -> bool:
    rows = sqlite.query(
        """SELECT COUNT(*) AS c FROM etf_trades
           WHERE etf_code = ? AND trade_date = ? AND direction = ?
             AND price = ? AND shares = ?""",
        [trade["etf_code"], trade["trade_date"], trade["direction"],
         trade["price"], trade["shares"]],
    )
    return int(rows[0]["c"]) > 0


def import_etf_xlsx(sqlite: SQLiteStore, path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """导入 Excel；dry_run 只预览不写库。重复交易自动跳过（幂等）。"""
    preview = preview_etf_xlsx(Path(path))
    if dry_run:
        return {"dry_run": True, **preview, "written": 0}

    for meta in preview["metas"]:
        upsert_etf_meta(sqlite, **meta)

    if preview["total_assets"] is not None:
        set_setting(sqlite, "total_assets", str(preview["total_assets"]))

    inserted_trades = 0
    skipped_trades = 0
    for trade in sorted(preview["trades"], key=lambda t: (t["trade_date"], t["etf_code"])):
        if _trade_exists(sqlite, trade):
            skipped_trades += 1
            continue
        add_etf_trade(sqlite, **trade)
        inserted_trades += 1

    inserted_cash = 0
    skipped_cash = 0
    for flow in preview["cash_flows"]:
        rows = sqlite.query(
            """SELECT COUNT(*) AS c FROM etf_cash_flows
               WHERE flow_date = ? AND direction = ? AND amount = ?""",
            [flow["flow_date"], flow["direction"], flow["amount"]],
        )
        if int(rows[0]["c"]) > 0:
            skipped_cash += 1
            continue
        add_cash_flow(sqlite, **flow)
        inserted_cash += 1

    return {
        "dry_run": False,
        "trades_total": len(preview["trades"]),
        "trades_inserted": inserted_trades,
        "trades_skipped": skipped_trades,
        "cash_inserted": inserted_cash,
        "cash_skipped": skipped_cash,
        "metas_written": len(preview["metas"]),
        "total_assets": preview["total_assets"],
        "issues": preview["issues"],
    }
