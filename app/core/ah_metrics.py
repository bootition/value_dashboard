"""A+H 双市场估值/股息率计算（reports/120 J12）。

本模块只做**新增计算**，是 `indicator_snapshot` 之外的独立口径。全部函数
尽量保持纯函数（除 `build_ah_snapshot` 只读 DuckDB），便于用临时库回归。

口径纪律（强制，后续改动不得破坏）
==================================
1. **不得修改现有 `indicator_snapshot.total_market_cap`。** 该字段当前是
   `A 股收盘价 × 总股本`，而总股本含 H 股，因此 203 只 A+H 的市值/PE/PB/PS
   系统性偏大（工行 601398 实测：8.85 × 356,406,257,089 = 3,154,195,375,237.65，
   与库内 total_market_cap 完全一致）。本任务只新增 `total_market_cap_ah`
   等独立结果，不动主链、不回写快照。修复主链是单独立项，不在本模块范围。
2. **日期对齐：H 价/FX 必须取「≤ A 股价格日」的最近一条。** 统一走 `_asof`
   （DuckDB 子查询，`ORDER BY trade_date DESC LIMIT 1`）。H 股停牌时允许用更早
   收盘价，但结果带 `h_price_staleness_days` 供调用方判断；绝不允许用晚于
   A 股价格日的 H 价或汇率（那会引入未来数据）。
3. **税只做参数，不内置裁决。** H 股红利税率默认 `tax_rate=0.20`（港股通个人
   20%），结果同时返回税前 `h_yield` 与税后 `h_yield_after_tax`。本模块不
   叠加公司层面代扣（10%），需要时由调用方传 `tax_rate`。
4. **除权日不同步（H 比 A 中位数早 24 天）→ 用「近 12 个月已实施」口径。**
   A、H **各自按自己的 `ex_date`** 在同一个自然窗口 `(A 股价格日 − 1 年,
   A 股价格日]` 内累计，禁止用 A 股 ex_date 代替 H 股。返回的 `window` 字段
   记录窗口起止、两边事件数与口径说明。
5. **单位（防止第 3 次 100 倍事故）**：市值一律人民币元；`ah_premium_pct`
   是百分点（38.9 表示 A 股比 H 股贵 38.9%）；所有 `*_yield` 是**小数比例**
   （0.05 = 5%），与 `indicator_snapshot.dividend_yield` 同口径，不是
   `ttm_dividend_yield` 的百分数口径。返回里的 `units` 字段显式标注。

H 股股本推导的已知局限（重要）
------------------------------
`h_share_count()` 用 `total_shares - circ_shares` 近似 H 股股本。PRODUCT_GAPS
§1.1⑤ 实测 7 只银行/能源公司中 5 只精确，但：

- 中国银行 601988：推导 111,446,896,968，实际 83,622,276,395 → **偏高 33%**
  （`total_shares` 含优先股等其他权益工具）；
- 建设银行 601939：推导 252,006,350,000，实际 240,417,319,880 → **偏高 4.8%**。

因此该值只能作为 **approximate / derived** 使用。调用方若掌握独立 H 股股本
基准，应传入 `reference_h_shares` 让函数在偏差超容差时返回 `None`；
`strict=True` 且没有基准时，函数也返回 `None`（因为 |偏差| 无法判定）。
默认非 strict 模式在输入可判定（非空、有限、非负、流通≤总股本）时返回推导值，
调用方必须自己标注 `confidence='derived'`，不得当精确披露值使用。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

__all__ = [
    "ah_dividend_yield",
    "ah_market_cap",
    "build_ah_snapshot",
    "h_share_count",
]

# 恒等式容差：|H股息率 − A股息率×(1+溢价率)| / |期望值| > 5% 判定失败
IDENTITY_TOLERANCE = 0.05
# H 股股本推导默认容差（与独立基准比较时）
H_SHARE_TOLERANCE = 0.02
DEFAULT_TAX_RATE = 0.20
TTM_BASIS = "ttm_12m_implemented"

# _asof 只允许这两张表，避免把表名拼进 SQL 的注入面
_ASOF_SPECS: dict[str, tuple[str, str, str]] = {
    "hk_price_daily": ("stock_code", "trade_date", "close"),
    "fx_rate_daily": ("pair", "trade_date", "rate"),
}


# ─── 基础工具 ──────────────────────────────────────────────────────


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _num(value: Any, *, positive: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    if positive and out <= 0:
        return None
    if not positive and out < 0:
        return None
    return out


def _shift_one_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 2 月 29 日 → 去年 2 月 28 日
        return day.replace(year=day.year - 1, day=28)


# ─── H 股股本推导 ───────────────────────────────────────────────────


def h_share_count(
    total_shares: Any,
    circ_shares: Any,
    *,
    reference_h_shares: Any = None,
    strict: bool = False,
    tolerance: float = H_SHARE_TOLERANCE,
) -> float | None:
    """用 `total_shares - circ_shares` 近似推导 H 股股本（单位：股）。

    Parameters
    ----------
    total_shares:
        A+H 全部总股本（`stock_meta.total_shares`）。
    circ_shares:
        A 股流通股本（`stock_meta.circ_shares`）。
    reference_h_shares:
        可选的独立 H 股股本基准。提供时若相对偏差超过 ``tolerance`` 返回 None。
    strict:
        True 且未提供基准时返回 None——因为此时 |偏差| 无法判定。
    tolerance:
        与基准比较的相对偏差上限（默认 2%）。

    Returns
    -------
    float | None
        通过可判定性检查时返回 ``total − circ``（可能为 0.0，表示纯 A 股）；
        以下情况返回 None：

        - 任一输入缺失 / NaN / Inf / 负数；
        - ``total_shares <= 0``；
        - ``circ_shares > total_shares``（数据异常，绝不返回负数）；
        - 提供 ``reference_h_shares`` 且相对偏差 > ``tolerance``；
        - ``strict=True`` 但没有 ``reference_h_shares``（|偏差| 无法判定）。

    ⚠️ 已知局限（必须随结果一起标注 ``confidence='derived'``）
    ----------------------------------------------------------
    该推导在 7 只样本中 5 只精确（工行、中石油、农行、平安、招行），但
    中国银行 601988 偏高 33%（`total_shares` 含优先股），建设银行 601939
    偏高 4.8%。没有独立 H 股股本基准时，单凭这两个输入**无法判断**偏差
    大小。因此默认模式返回的只是近似值；回测/展示必须允许误差，或改用
    `reference_h_shares` / F10 实际 H 股数。
    """
    total = _num(total_shares, positive=True)
    circ = _num(circ_shares)
    if total is None or circ is None:
        return None
    if circ > total:
        return None
    derived = float(total - circ)

    if reference_h_shares is not None:
        reference = _num(reference_h_shares)
        if reference is None:
            return None
        if reference == 0:
            # 纯 A 股基准：推导值也必须是 0，否则说明推导被优先股等污染
            return derived if derived == 0 else None
        if abs(derived - reference) / reference > tolerance:
            return None
        return derived
    if strict:
        return None
    return derived


# ─── 市值拆分与 AH 溢价 ─────────────────────────────────────────────


def ah_market_cap(
    close_a: Any,
    close_h: Any,
    fx_hkd_cny: Any,
    total_shares: Any,
    circ_shares: Any,
) -> dict[str, Any]:
    """按真实两地价格拆分 A+H 总市值，并计算 A 股相对 H 股的溢价。

    公式（单位：人民币元）::

        a_share_market_cap   = close_a × circ_shares
        h_share_market_cap_cny = close_h × h_shares × fx_hkd_cny
        total_market_cap_ah  = 两者相加
        ah_premium_pct       = (close_a / (close_h × fx) − 1) × 100

    ``circ_shares`` 是 A 股股本；H 股股本由 :func:`h_share_count` 推导。
    ``ah_premium_pct`` 是**百分点**（与恒生 AH 溢价指数同方向：正数=A 贵）。
    ``total_market_cap_ah`` 与 ``indicator_snapshot.total_market_cap`` **无关**，
    不写回、不覆盖。

    返回 dict 包含上述 4 个主字段，外加 ``h_shares`` / ``h_price_cny`` /
    ``units``；输入无法计算时主字段为 None，``reason`` 说明原因
    （``missing_h_shares`` / ``invalid_price`` / ``invalid_fx``）。
    """
    out: dict[str, Any] = {
        "a_share_market_cap": None,
        "h_share_market_cap_cny": None,
        "total_market_cap_ah": None,
        "ah_premium_pct": None,
        "h_shares": None,
        "h_price_cny": None,
        "reason": None,
        "units": {
            "market_cap": "CNY_yuan",
            "price": "CNY_yuan_per_share / HKD_per_share",
            "premium": "percent_points",
        },
    }
    price_a = _num(close_a, positive=True)
    price_h = _num(close_h, positive=True)
    fx = _num(fx_hkd_cny, positive=True)
    if price_a is None or price_h is None:
        out["reason"] = "invalid_price"
        return out
    if fx is None:
        out["reason"] = "invalid_fx"
        return out
    h_shares = h_share_count(total_shares, circ_shares)
    if h_shares is None:
        out["reason"] = "missing_h_shares"
        return out

    a_shares = _num(circ_shares)
    assert a_shares is not None  # h_share_count 已校验
    h_price_cny = price_h * fx
    a_cap = price_a * a_shares
    h_cap = h_price_cny * h_shares
    out.update({
        "a_share_market_cap": a_cap,
        "h_share_market_cap_cny": h_cap,
        "total_market_cap_ah": a_cap + h_cap,
        "ah_premium_pct": (price_a / h_price_cny - 1.0) * 100.0,
        "h_shares": h_shares,
        "h_price_cny": h_price_cny,
    })
    return out


# ─── AH 股息率与恒等式 ──────────────────────────────────────────────


def ah_dividend_yield(
    div_a_ps: Any,
    div_h_ps_cny: Any,
    close_a: Any,
    close_h: Any,
    fx: Any,
    *,
    tax_rate: float = DEFAULT_TAX_RATE,
    window: Any = TTM_BASIS,
    div_h_ps_hkd: Any = None,
) -> dict[str, Any]:
    """计算 A/H 股息率、差额、税后口径，并校验恒等式。

    恒等式（DPS_A == DPS_H_CNY 且窗口内笔数一致时严格成立）::

        h_yield ≈ a_yield × (1 + ah_premium)

    其中 ``ah_premium`` 是小数（0.389 = A 贵 38.9%）。返回
    ``identity_ok`` 与 ``identity_deviation_pct``：相对偏差 > 5% 时 False。

    Parameters
    ----------
    div_a_ps / div_h_ps_cny:
        近 12 个月**已实施**的每股现金分红合计（元/股）。0 是合法值
        （不分红），None 被视为缺数据。
    close_a / close_h / fx:
        A 股收盘价（元）、H 股收盘价（港元）、HKD→CNY（元/港元）。
    tax_rate:
        H 股红利税率，默认 0.20；只影响 ``h_yield_after_tax``。
    window:
        原样回传，建议传 :func:`build_ah_snapshot` 构造的窗口 dict。
    div_h_ps_hkd:
        可选：H 股每股股息港币原值。提供时 ``h_yield_hkd`` 用它计算；
        否则由 ``div_h_ps_cny / fx`` 反推（两者在币种一致时数学等价）。

    返回：``a_yield`` / ``h_yield`` / ``h_yield_hkd`` / ``h_yield_after_tax``
    / ``ah_yield_gap``（A−H，小数）/ ``ah_yield_ratio`` /
    ``ah_premium_pct``（百分点）/ ``expected_h_yield`` /
    ``identity_deviation_pct`` / ``identity_ok`` / ``tax_rate`` / ``window``
    / ``units`` / ``reason``。所有 ``*_yield`` 均为**小数比例**。
    """
    out: dict[str, Any] = {
        "a_yield": None,
        "h_yield": None,
        "h_yield_hkd": None,
        "h_yield_after_tax": None,
        "ah_yield_gap": None,
        "ah_yield_ratio": None,
        "ah_premium_pct": None,
        "expected_h_yield": None,
        "identity_deviation_pct": None,
        "identity_ok": False,
        "tax_rate": None,
        "window": window,
        "reason": None,
        "units": {
            "yields": "decimal_fraction",
            "premium": "percent_points",
            "gap": "decimal_fraction",
        },
    }
    dps_a = _num(div_a_ps)
    dps_h_cny = _num(div_h_ps_cny)
    dps_h_hkd_input = None if div_h_ps_hkd is None else _num(div_h_ps_hkd)
    price_a = _num(close_a, positive=True)
    price_h = _num(close_h, positive=True)
    fx_rate = _num(fx, positive=True)
    tax = _num(tax_rate)
    if tax is None or tax > 1:
        out["reason"] = "invalid_tax_rate"
        return out
    if price_a is None or price_h is None:
        out["reason"] = "invalid_price"
        return out
    if fx_rate is None:
        out["reason"] = "invalid_fx"
        return out
    if dps_h_cny is None and dps_h_hkd_input is not None:
        dps_h_cny = dps_h_hkd_input * fx_rate
    if dps_a is None or dps_h_cny is None:
        out["reason"] = "missing_dividend"
        return out

    a_yield = dps_a / price_a
    h_price_cny = price_h * fx_rate
    h_yield = dps_h_cny / h_price_cny
    if dps_h_hkd_input is not None:
        h_yield_hkd = dps_h_hkd_input / price_h
    else:
        h_yield_hkd = (dps_h_cny / fx_rate) / price_h
    premium = price_a / h_price_cny - 1.0
    expected_h = a_yield * (1.0 + premium)

    if expected_h == 0:
        deviation = 0.0 if h_yield == 0 else float("inf")
    else:
        deviation = abs(h_yield - expected_h) / abs(expected_h)
    identity_ok = bool(math.isfinite(deviation) and deviation <= IDENTITY_TOLERANCE)

    out.update({
        "a_yield": a_yield,
        "h_yield": h_yield,
        "h_yield_hkd": h_yield_hkd,
        "h_yield_after_tax": h_yield * (1.0 - tax),
        "ah_yield_gap": a_yield - h_yield,
        "ah_yield_ratio": (a_yield / h_yield) if h_yield != 0 else None,
        "ah_premium_pct": premium * 100.0,
        "expected_h_yield": expected_h,
        "identity_deviation_pct": deviation * 100.0 if math.isfinite(deviation) else None,
        "identity_ok": identity_ok,
        "tax_rate": tax,
    })
    return out


# ─── 日期对齐辅助 ───────────────────────────────────────────────────


def _asof(
    duck: Any,
    relation: str,
    entity: Any,
    as_of: Any,
    *,
    value_column: str | None = None,
) -> dict[str, Any] | None:
    """取 ``<= as_of`` 的最近一条记录（DuckDB 子查询实现）。

    ``relation`` 只允许 ``hk_price_daily`` / ``fx_rate_daily``（白名单），
    其余抛 ValueError。返回 ``{value, as_of_date, staleness_days}``；没有
    任何不晚于目标日的记录时返回 None。

    这是「日期对齐」纪律的唯一入口：H 价用港股代码，汇率用 ``pair='HKDCNY'``，
    两者都以 **A 股价格日** 为 as_of，避免未来数据泄漏。
    """
    if relation not in _ASOF_SPECS:
        raise ValueError(f"unsupported asof relation: {relation!r}")
    entity_column, date_column, default_value = _ASOF_SPECS[relation]
    value_column = value_column or default_value
    if value_column not in {default_value, "close", "rate"}:
        raise ValueError(f"unsupported asof value column: {value_column!r}")
    target = _to_date(as_of)
    if target is None:
        raise ValueError(f"invalid as_of: {as_of!r}")
    if entity is None or str(entity).strip() == "":
        return None
    sql = (
        f"SELECT {date_column} AS as_of_date, {value_column} AS value "
        f"FROM {relation} "
        f"WHERE {entity_column} = ? AND {date_column} <= ? "
        f"AND {value_column} IS NOT NULL "
        f"ORDER BY {date_column} DESC LIMIT 1"
    )
    try:
        rows = duck.read_query(sql, [str(entity), target])
    except Exception as error:  # noqa: BLE001 - v40 之前的库还没有这两张表
        message = str(error)
        if "does not exist" in message or "Catalog Error" in message:
            return None
        raise
    if not rows:
        return None
    as_of_date = _to_date(rows[0]["as_of_date"])
    value = rows[0]["value"]
    if as_of_date is None or value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return {
        "value": numeric,
        "as_of_date": as_of_date,
        "staleness_days": (target - as_of_date).days,
    }


# ─── 快照构建 ───────────────────────────────────────────────────────


def _resolve_mapping(duck: Any, mapping: dict[str, Any] | None) -> dict[str, Any]:
    if mapping is not None:
        return mapping
    from app.core.ah_hk_mapping import build_ah_hk_mapping

    report = build_ah_hk_mapping(duck)
    return report["mapping"]


def _latest_a_prices(
    duck: Any, codes: list[str], as_of: date,
) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    placeholders = ", ".join("?" for _ in codes)
    rows = duck.read_query(
        f"""
        SELECT stock_code, trade_date, close
        FROM (
            SELECT p.stock_code, p.trade_date, p.close,
                   row_number() OVER (
                       PARTITION BY p.stock_code ORDER BY p.trade_date DESC
                   ) AS rn
            FROM price_daily_raw p
            WHERE p.stock_code IN ({placeholders})
              AND p.close IS NOT NULL
              AND p.trade_date <= ?
        ) t
        WHERE rn = 1
        """,
        [*codes, as_of],
    )
    return {row["stock_code"]: row for row in rows}


def _share_capital(duck: Any, codes: list[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    placeholders = ", ".join("?" for _ in codes)
    rows = duck.read_query(
        f"""SELECT stock_code, total_shares, circ_shares
            FROM stock_meta WHERE stock_code IN ({placeholders})""",
        codes,
    )
    return {row["stock_code"]: row for row in rows}


def _a_dividend_window(
    duck: Any, code: str, start: date, end: date,
) -> tuple[float, int]:
    rows = duck.read_query(
        """SELECT dividend_per_share FROM dividends
           WHERE stock_code = ? AND ex_date >= ? AND ex_date <= ?
             AND dividend_per_share IS NOT NULL""",
        [code, start, end],
    )
    values = [
        value
        for row in rows
        if (value := _num(row["dividend_per_share"])) is not None
    ]
    return float(sum(values)), len(values)


def _h_dividend_window(
    duck: Any,
    hk_code: str,
    start: date,
    end: date,
    *,
    fx_fallback: float,
    fx_pair: str = "HKDCNY",
) -> tuple[float | None, int, int]:
    """H 股 TTM 每股股息（人民币）。返回 (dps_cny, 有效事件数, 港币换算数)。"""
    rows = duck.read_query(
        """SELECT ex_date, dividend_per_share_hkd, dividend_per_share_cny
           FROM hk_dividends
           WHERE stock_code = ? AND ex_date >= ? AND ex_date <= ?
           ORDER BY ex_date""",
        [hk_code, start, end],
    )
    if not rows:
        return 0.0, 0, 0
    total = 0.0
    events = 0
    converted = 0
    for row in rows:
        cny = _num(row["dividend_per_share_cny"])
        if cny is not None:
            total += cny
            events += 1
            continue
        hkd = _num(row["dividend_per_share_hkd"])
        if hkd is None:
            continue
        ex_date = _to_date(row["ex_date"])
        fx_row = None
        if ex_date is not None:
            fx_row = _asof(duck, "fx_rate_daily", fx_pair, ex_date)
        rate = fx_row["value"] if fx_row is not None else fx_fallback
        total += hkd * rate
        events += 1
        converted += 1
    if events == 0:
        return None, 0, 0
    return total, events, converted


def build_ah_snapshot(
    duck: Any,
    as_of: Any = None,
    *,
    mapping: dict[str, Any] | None = None,
    stock_codes: list[str] | None = None,
    tax_rate: float = DEFAULT_TAX_RATE,
) -> list[dict[str, Any]]:
    """逐只 A+H 构建 AH 快照（只读 DuckDB，不写库）。

    对每只股票：

    1. 取 ``as_of``（默认今天）之前最新一条 A 股收盘价（``price_daily_raw``）；
    2. 以该 **A 股价格日** 为锚，用 :func:`_asof` 取 ≤ 锚点的 H 股收盘价与
       HKDCNY 汇率；
    3. 用 ``stock_meta.total_shares / circ_shares`` 推导 H 股股本；
    4. 在 ``(A 股价格日 − 1 年, A 股价格日]`` 内，A/H **各自按自己的
       ex_date** 累计近 12 个月已实施每股现金分红；
    5. 调用 :func:`ah_market_cap` / :func:`ah_dividend_yield`。

    缺数据时该只股票仍返回一行（便于批量定位），但所有计算字段为 None，
    ``reason`` 取以下值之一（按判断顺序）：

    - ``missing_a_price``：A 股价格缺失；
    - ``missing_h_shares``：总股本/流通股本缺失或 ``circ > total``；
    - ``missing_h_price``：H 股价格缺失（或表内无 ≤ 锚点记录）；
    - ``missing_fx``：HKDCNY 汇率缺失；
    - ``missing_h_dividend``：A 股近 12 个月有分红，但 H 股窗口内零有效记录
      （PRODUCT_GAPS §1.1②：这更像抓取失败，不能当「H 股不分红」）。

    返回字段见模块 docstring；``units`` 显式标注市值/溢价/股息率单位。
    """
    anchor = _to_date(as_of) or date.today()
    resolved = _resolve_mapping(duck, mapping)
    codes = sorted(resolved)
    if stock_codes:
        wanted = {str(code).strip() for code in stock_codes}
        codes = [code for code in codes if code in wanted]
    if not codes:
        return []

    a_prices = _latest_a_prices(duck, codes, anchor)
    shares = _share_capital(duck, codes)
    fx_cache: dict[date, dict[str, Any] | None] = {}
    results: list[dict[str, Any]] = []

    for code in codes:
        mapped = resolved[code]
        hk_code = str(mapped.get("hk_code", "")).zfill(5)
        meta = shares.get(code) or {}
        price_row = a_prices.get(code) or {}
        a_date = _to_date(price_row.get("trade_date"))
        window = {
            "basis": TTM_BASIS,
            "start": _shift_one_year(a_date) if a_date else None,
            "end": a_date,
            "a_ex_date_source": "dividends.ex_date",
            "h_ex_date_source": "hk_dividends.ex_date",
            "a_events": 0,
            "h_events": 0,
            "note": (
                "近12个月已实施现金分红；A/H 各自按自己的 ex_date 累计"
                "（两地除权日中位差 24 天，禁止互相代替）"
            ),
        }
        base: dict[str, Any] = {
            "stock_code": code,
            "hk_code": hk_code,
            "a_name": mapped.get("a_name"),
            "hk_name": mapped.get("hk_name"),
            "as_of": anchor,
            "a_price_date": a_date,
            "a_close": _num(price_row.get("close"), positive=True),
            "h_price_date": None,
            "h_close": None,
            "h_price_staleness_days": None,
            "fx_date": None,
            "fx_hkd_cny": None,
            "fx_staleness_days": None,
            "total_shares": _num(meta.get("total_shares")),
            "circ_shares": _num(meta.get("circ_shares")),
            "h_shares": None,
            "h_shares_confidence": "derived",
            "div_a_ps_ttm": None,
            "div_h_ps_cny_ttm": None,
            "h_dividend_converted_events": 0,
            "a_share_market_cap": None,
            "h_share_market_cap_cny": None,
            "total_market_cap_ah": None,
            "ah_premium_pct": None,
            "a_yield": None,
            "h_yield": None,
            "h_yield_hkd": None,
            "h_yield_after_tax": None,
            "ah_yield_gap": None,
            "ah_yield_ratio": None,
            "expected_h_yield": None,
            "identity_deviation_pct": None,
            "identity_ok": None,
            "tax_rate": tax_rate,
            "units": {
                "market_cap": "CNY_yuan",
                "premium": "percent_points",
                "yields": "decimal_fraction",
                "dividend_per_share": "CNY_yuan_per_share",
            },
            "window": window,
            "status": "incomplete",
            "reason": None,
        }
        results.append(base)

        if a_date is None or base["a_close"] is None:
            base["reason"] = "missing_a_price"
            continue
        h_shares = h_share_count(meta.get("total_shares"), meta.get("circ_shares"))
        if h_shares is None:
            base["reason"] = "missing_h_shares"
            continue
        base["h_shares"] = h_shares

        h_price = _asof(duck, "hk_price_daily", hk_code, a_date)
        if h_price is None:
            base["reason"] = "missing_h_price"
            continue
        base["h_price_date"] = h_price["as_of_date"]
        base["h_close"] = h_price["value"]
        base["h_price_staleness_days"] = h_price["staleness_days"]

        if a_date not in fx_cache:
            fx_cache[a_date] = _asof(duck, "fx_rate_daily", "HKDCNY", a_date)
        fx_row = fx_cache[a_date]
        if fx_row is None:
            base["reason"] = "missing_fx"
            continue
        base["fx_date"] = fx_row["as_of_date"]
        base["fx_hkd_cny"] = fx_row["value"]
        base["fx_staleness_days"] = fx_row["staleness_days"]

        start = window["start"]
        div_a, a_events = _a_dividend_window(duck, code, start, a_date)
        div_h, h_events, converted = _h_dividend_window(
            duck, hk_code, start, a_date, fx_fallback=fx_row["value"],
        )
        window["a_events"] = a_events
        window["h_events"] = h_events
        base["div_a_ps_ttm"] = div_a
        base["h_dividend_converted_events"] = converted
        if div_h is None:
            base["reason"] = "missing_h_dividend"
            continue
        if h_events == 0 and div_a > 0:
            base["reason"] = "missing_h_dividend"
            continue
        base["div_h_ps_cny_ttm"] = div_h

        cap = ah_market_cap(
            base["a_close"], base["h_close"], base["fx_hkd_cny"],
            meta.get("total_shares"), meta.get("circ_shares"),
        )
        if cap["reason"] is not None:
            base["reason"] = cap["reason"]
            continue
        yields = ah_dividend_yield(
            div_a, div_h, base["a_close"], base["h_close"], base["fx_hkd_cny"],
            tax_rate=tax_rate, window=window,
        )
        if yields["reason"] is not None:
            base["reason"] = yields["reason"]
            continue

        base.update({
            "a_share_market_cap": cap["a_share_market_cap"],
            "h_share_market_cap_cny": cap["h_share_market_cap_cny"],
            "total_market_cap_ah": cap["total_market_cap_ah"],
            "ah_premium_pct": cap["ah_premium_pct"],
            "a_yield": yields["a_yield"],
            "h_yield": yields["h_yield"],
            "h_yield_hkd": yields["h_yield_hkd"],
            "h_yield_after_tax": yields["h_yield_after_tax"],
            "ah_yield_gap": yields["ah_yield_gap"],
            "ah_yield_ratio": yields["ah_yield_ratio"],
            "expected_h_yield": yields["expected_h_yield"],
            "identity_deviation_pct": yields["identity_deviation_pct"],
            "identity_ok": yields["identity_ok"],
            "tax_rate": yields["tax_rate"],
            "units": {
                "market_cap": "CNY_yuan",
                "premium": "percent_points",
                "yields": "decimal_fraction",
                "dividend_per_share": "CNY_yuan_per_share",
            },
            "status": "ok",
        })
    return results
