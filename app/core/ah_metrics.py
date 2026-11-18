"""A+H 双市场估值/股息率计算（reports/122 §2.7 J12）。

本模块只做**新增计算**，是 `indicator_snapshot` 之外的独立口径。全部函数
尽量保持纯函数（除 `build_ah_snapshot` 只读 DuckDB），便于用临时库回归。

口径纪律（强制，后续改动不得破坏）
==================================
1. **行业口径已核实（2026-11-16 实时行情）：`total_market_cap` 不动、不切换。**
   腾讯/同花顺/东财的 A 股页面「总市值」= **A 股价格 × 总股本**（不按 A/H 分开
   算）。实测工行 31,541.95 亿、平安 9,741.91 亿、中石油 22,676.30 亿，与库内
   `indicator_snapshot.total_market_cap` **完全一致**（工行 601398：8.85 ×
   356,406,257,089 = 3,154,195,375,237.65）。因此该字段**本身就是行业口径**，
   **谁都不得改它**，本模块也**不得**读写它（`app/core/indicators/calculator.py`
   同样不许碰）。修复主链是单独立项，不在本模块范围。
2. **H 股市值单独一列（行业软件也单独显示「港股市值」）。**
   `h_share_market_cap_cny` = `H 股价格 × H 股股本 × HKD→CNY 汇率`，
   **单位：人民币元**。它是独立曝光的明确字段，不是 `total_market_cap` 的拆分，
   也不回写快照。`ah_total_market_cap_cny`（= A 股部分 + H 股部分，与既有
   `total_market_cap_ah` 同值）**只是参考量，不是行业口径的总市值**——行业口径
   见 `indicator_snapshot.total_market_cap`。
3. **AH 股息率「各算各的」，恒等式不是门禁（2026-11-16 裁决，写死在此）。**
   成熟软件从不用「A 股息率 ×(1+溢价) = H 股息率」做约束：A 用 A 的分红与 A 的
   股价、H 用 H 的分红与 H 的股价。本模块因此对 A/H **各自独立**计算——任一侧
   缺数据只让**该侧**为 None，**绝不**因另一侧缺失而整体返回 None，也**绝不**把
   恒等式失败当成错误或丢弃依据。`identity_ok` / `identity_deviation_pct` 已降级
   为「**仅当两端分红笔数一致时才计算**」的参考值：笔数不一致 →
   `identity_status='not_comparable'` 且 `identity_deviation_pct=None`。
   调用方**不得**因 `identity_status ∈ {'mismatch','not_comparable'}` 丢弃该股票
   ——实测 203 只里 55 只恒等式不成立，绝大多数是「A 这段窗口分了 1 次、H 分了
   2 次」这类正常现象，不是数据错误。
4. **日期对齐：H 价/FX 必须取「≤ A 股价格日」的最近一条。** 统一走 `_asof`
   （DuckDB 子查询，`ORDER BY trade_date DESC LIMIT 1`）。H 股停牌时允许用更早
   收盘价，但结果带 `h_price_staleness_days` 供调用方判断；绝不允许用晚于
   A 股价格日的 H 价或汇率（那会引入未来数据）。
5. **税只做参数，不内置裁决。** H 股红利税率默认 `tax_rate=0.20`（港股通个人
   20%），结果同时返回税前 `h_yield` 与税后 `h_yield_after_tax`。本模块不
   叠加公司层面代扣（10%），需要时由调用方传 `tax_rate`。
6. **除权日不同步（H 比 A 中位数早 24 天）→ 用「近 12 个月已实施」口径。**
   A、H **各自按自己的 `ex_date`** 在同一个自然窗口 `(A 股价格日 − 1 年,
   A 股价格日]` 内累计，禁止用 A 股 ex_date 代替 H 股。返回的 `window` 字段
   记录窗口起止、两边事件数与口径说明。
7. **单位（防止第 3 次 100 倍事故）**：市值一律人民币元；`ah_premium_pct`
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
from typing import Any, Literal

from app.core.financial_period import cn_today
from app.core.market_cap import market_cap_from

__all__ = [
    "IDENTITY_STATUS_INSUFFICIENT",
    "IDENTITY_STATUS_MISMATCH",
    "IDENTITY_STATUS_NOT_COMPARABLE",
    "IDENTITY_STATUS_OK",
    "ah_dividend_yield",
    "ah_market_cap",
    "build_ah_snapshot",
    "h_share_count",
]

# 恒等式四态（口径纪律 §3）。`identity_deviation_pct` 只在 ok / mismatch 两态有值。
IdentityStatus = Literal["ok", "mismatch", "not_comparable", "insufficient_data"]
IDENTITY_STATUS_OK = "ok"
IDENTITY_STATUS_MISMATCH = "mismatch"
IDENTITY_STATUS_NOT_COMPARABLE = "not_comparable"
IDENTITY_STATUS_INSUFFICIENT = "insufficient_data"

# 恒等式容差（**仅参考值用，不是门禁**）：
# |H股息率 − A股息率×(1+溢价率)| / |期望值| > 5% 时 identity_status='mismatch'。
# 判定失败**不得**被调用方当成数据错误或丢弃依据（见模块 docstring §3）。
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
    """按真实两地价格拆分 A+H 市值，并计算 A 股相对 H 股的溢价。

    公式（**全部字段单位：人民币元**）::

        a_share_market_cap      = close_a × circ_shares            # A 股部分
        h_share_market_cap_cny  = close_h × h_shares × fx_hkd_cny   # H 股部分（港股市值）
        total_market_cap_ah     = 两者相加
        ah_total_market_cap_cny = 两者相加（与 total_market_cap_ah 同值的显式别名）
        ah_premium_pct          = (close_a / (close_h × fx) − 1) × 100

    字段口径（**不要混淆**）：

    - ``h_share_market_cap_cny``：**H 股市值，单位人民币元**（`H 股价格 ×
      H 股股本 × 汇率`）。行业软件单独显示这一列（「港股市值」），本模块
      也单独稳定曝光，它是 `ah_market_cap` 的一等输出字段。
    - ``total_market_cap_ah`` / ``ah_total_market_cap_cny``：A 股部分 + H 股部分
      的**参考量，不是行业口径的总市值**。行业口径（= A 股价格 × 总股本）见
      `indicator_snapshot.total_market_cap`——本模块既不读也不写它。
    - ``ah_premium_pct`` 是**百分点**（与恒生 AH 溢价指数同方向：正数=A 贵）。

    ``circ_shares`` 是 A 股股本；H 股股本由 :func:`h_share_count` 推导
    （``confidence='derived'``，见模块 docstring 的已知局限）。

    返回 dict 包含上述主字段，外加 ``h_shares``（股）/ ``h_price_cny``
    （元/股）/ ``units``；输入无法计算时主字段为 None，``reason`` 说明原因
    （``missing_h_shares`` / ``invalid_price`` / ``invalid_fx``）。
    """
    out: dict[str, Any] = {
        "a_share_market_cap": None,
        "h_share_market_cap_cny": None,
        "total_market_cap_ah": None,
        "ah_total_market_cap_cny": None,
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
    # A 股部分走总市值唯一实现（app/core/market_cap.py）；H 股部分口径不变。
    # circ_shares == 0 表示纯 H 股公司，A 股部分按旧口径给 0.0（是「没有
    # A 股」，不是「缺数据」），因此不能落到 market_cap_from 的 None 分支。
    a_cap = market_cap_from(price_a, a_shares) if a_shares else 0.0
    h_cap = h_price_cny * h_shares
    total_ah = a_cap + h_cap
    out.update({
        "a_share_market_cap": a_cap,
        "h_share_market_cap_cny": h_cap,
        "total_market_cap_ah": total_ah,
        # 显式别名：仅作参考，**不是**行业口径总市值（行业口径见
        # indicator_snapshot.total_market_cap，本模块不读写它）
        "ah_total_market_cap_cny": total_ah,
        "ah_premium_pct": (price_a / h_price_cny - 1.0) * 100.0,
        "h_shares": h_shares,
        "h_price_cny": h_price_cny,
    })
    return out


# ─── AH 股息率（各算各的）与恒等式参考值 ─────────────────────────────


def _event_count(value: Any) -> int | None:
    """把分红事件笔数归一成非负 int；缺失/非法返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _resolve_event_counts(
    window: Any, a_events: Any, h_events: Any,
) -> tuple[int | None, int | None]:
    """笔数来源：显式参数优先，其次取 ``window`` dict 里的 ``a_events``/``h_events``。"""
    a_count = _event_count(a_events)
    h_count = _event_count(h_events)
    if isinstance(window, dict):
        if a_count is None:
            a_count = _event_count(window.get("a_events"))
        if h_count is None:
            h_count = _event_count(window.get("h_events"))
    return a_count, h_count


def _insufficient_reason(
    dps_a: float | None,
    dps_h_cny: float | None,
    price_a: float | None,
    price_h: float | None,
    fx_rate: float | None,
) -> str:
    """恒等式数据不足时说明缺什么（只描述缺口，不否定已算出的单侧结果）。"""
    if dps_a is None and dps_h_cny is None:
        return "missing_dividend"
    if dps_a is None:
        return "missing_a_dividend"
    if dps_h_cny is None:
        return "missing_h_dividend"
    if price_a is None or price_h is None:
        return "invalid_price"
    return "invalid_fx"


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
    a_events: Any = None,
    h_events: Any = None,
) -> dict[str, Any]:
    """A/H 股息率**各自独立计算**，并给出恒等式的参考偏离（不是门禁）。

    口径（模块 docstring §3，强制）：成熟软件从不用恒等式做约束——

    - A 股息率 ``a_yield = DPS_A / 收盘价_A``（A 的分红 ÷ A 的股价）；
    - H 股息率 ``h_yield = DPS_H(人民币) / (收盘价_H × 汇率)``
      （H 的分红 ÷ H 的股价）。

    因此**任一侧缺数据只让该侧为 None**，另一侧照常出数；整体不会因为某一侧
    缺失而返回全 None。原因（缺什么）写在 ``reason`` 里，供调用方展示，**不**
    代表整只股票不可用。

    恒等式（**仅参考**，判定失败绝不丢弃股票）::

        expected_h_yield = a_yield × (1 + ah_premium)

    ``identity_status`` 四态（见 :data:`IdentityStatus`）：

    - ``'ok'``：两端分红笔数一致，相对偏离 ≤ :data:`IDENTITY_TOLERANCE`（5%）；
    - ``'mismatch'``：两端笔数一致但偏离 > 5%（**正常现象，不是错误**）；
    - ``'not_comparable'``：两端分红笔数**不一致**（如 A 窗口内 1 次、H 2 次）
      → ``identity_deviation_pct`` / ``identity_ok`` 置 None，不计算恒等式；
    - ``'insufficient_data'``：任一端的股息率算不出来（缺分红/价格/汇率）。

    ``identity_deviation_pct``（百分点）**只在 ok / mismatch 两态有值**；唯一例外
    是「期望值恰为 0 而 H 侧不为 0」时偏离无界——此时仍判 ``mismatch``，但不给
    百分点（``None``）。``identity_ok`` 是为向后兼容保留的布尔便捷值：
    ok→True / mismatch→False / 其余→None。

    Parameters
    ----------
    div_a_ps / div_h_ps_cny:
        近 12 个月**已实施**的每股现金分红合计（元/股）。0 是合法值
        （不分红），None 被视为该侧缺数据（只影响该侧）。
    close_a / close_h / fx:
        A 股收盘价（元）、H 股收盘价（港元）、HKD→CNY（元/港元）。
    tax_rate:
        H 股红利税率，默认 0.20；只影响 ``h_yield_after_tax``。
    window:
        原样回传；若为 dict 且含 ``a_events`` / ``h_events``，则作为笔数的
        兜底来源（:func:`build_ah_snapshot` 的窗口就是这个形状）。
    div_h_ps_hkd:
        可选：H 股每股股息港币原值。提供时 ``h_yield_hkd`` 用它计算；
        否则由 ``div_h_ps_cny / fx`` 反推（两者在币种一致时数学等价）。
    a_events / h_events:
        可选：两端窗口内**已实施分红笔数**，用于判定恒等式是否可比。
        两者都提供且不相等 → ``identity_status='not_comparable'``。都没提供
        时按「可比」给出参考值（纯单元调用兼容路径；生产路径
        ``build_ah_snapshot`` 总是传真实笔数）。

    返回：``a_yield`` / ``h_yield`` / ``h_yield_hkd`` / ``h_yield_after_tax``
    / ``ah_yield_gap``（A−H，小数）/ ``ah_yield_ratio`` /
    ``ah_premium_pct``（百分点）/ ``expected_h_yield``（参考）/
    ``identity_status`` / ``identity_deviation_pct`` / ``identity_ok`` /
    ``identity_note`` / ``tax_rate`` / ``window`` / ``units`` / ``reason``。
    所有 ``*_yield`` 均为**小数比例**。
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
        "identity_status": IDENTITY_STATUS_INSUFFICIENT,
        "identity_deviation_pct": None,
        "identity_ok": None,
        "identity_note": None,
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
        out["identity_note"] = "税率非法，全部结果不可用"
        return out
    out["tax_rate"] = tax
    if dps_h_cny is None and dps_h_hkd_input is not None and fx_rate is not None:
        dps_h_cny = dps_h_hkd_input * fx_rate

    # ── 两侧各算各的：任一侧缺数据只让该侧为 None ──────────────────
    h_price_cny = price_h * fx_rate if price_h is not None and fx_rate is not None else None
    a_yield = dps_a / price_a if dps_a is not None and price_a is not None else None
    h_yield = dps_h_cny / h_price_cny if dps_h_cny is not None and h_price_cny is not None else None
    h_yield_hkd: float | None = None
    if price_h is not None:
        if dps_h_hkd_input is not None:
            h_yield_hkd = dps_h_hkd_input / price_h
        elif dps_h_cny is not None and fx_rate is not None:
            h_yield_hkd = (dps_h_cny / fx_rate) / price_h
    premium = (
        price_a / h_price_cny - 1.0
        if price_a is not None and h_price_cny is not None
        else None
    )
    expected_h = a_yield * (1.0 + premium) if a_yield is not None and premium is not None else None
    out.update({
        "a_yield": a_yield,
        "h_yield": h_yield,
        "h_yield_hkd": h_yield_hkd,
        "h_yield_after_tax": h_yield * (1.0 - tax) if h_yield is not None else None,
        "ah_yield_gap": a_yield - h_yield if a_yield is not None and h_yield is not None else None,
        "ah_yield_ratio": (
            a_yield / h_yield
            if a_yield is not None and h_yield is not None and h_yield != 0
            else None
        ),
        "ah_premium_pct": premium * 100.0 if premium is not None else None,
        "expected_h_yield": expected_h,
    })

    # ── 恒等式：仅当两端笔数一致时才计算，且只作为参考值 ──────────
    a_count, h_count = _resolve_event_counts(window, a_events, h_events)
    comparable = None if a_count is None or h_count is None else a_count == h_count
    if h_yield is None or expected_h is None:
        # identity_status 保持 insufficient_data；已算出的单侧结果照常返回
        out["reason"] = _insufficient_reason(dps_a, dps_h_cny, price_a, price_h, fx_rate)
        out["identity_note"] = "A/H 至少一侧缺数据，恒等式不计算（不影响另一侧出数）"
        return out
    if comparable is False:
        out["identity_status"] = IDENTITY_STATUS_NOT_COMPARABLE
        out["identity_note"] = (
            f"两端分红笔数不一致（A {a_count} 次 / H {h_count} 次），"
            "恒等式不可比：各算各的，仅保留两侧股息率"
        )
        return out
    if expected_h == 0:
        deviation = 0.0 if h_yield == 0 else math.inf
    else:
        deviation = abs(h_yield - expected_h) / abs(expected_h)
    if not math.isfinite(deviation):
        out["identity_status"] = IDENTITY_STATUS_MISMATCH
        out["identity_ok"] = False
        out["identity_note"] = "期望 H 股息率为 0 而 H 侧不为 0：偏离无界，不给百分点"
        return out
    identity_ok = deviation <= IDENTITY_TOLERANCE
    out["identity_status"] = IDENTITY_STATUS_OK if identity_ok else IDENTITY_STATUS_MISMATCH
    out["identity_ok"] = identity_ok
    out["identity_deviation_pct"] = deviation * 100.0
    out["identity_note"] = (
        "未提供两端分红笔数，按可比口径给出参考值（仅供展示）"
        if comparable is None
        else "两端笔数一致；偏离仅作参考，不是门禁"
    )
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

    每行**稳定暴露**的市值/股息率字段（向后兼容，未改任何既有键名含义）：

    ===========================  ===========================================
    键                           口径与单位
    ===========================  ===========================================
    ``a_share_market_cap``      A 股部分 = A 收盘价 × A 股股本，**元**
    ``h_share_market_cap_cny``  **H 股市值** = H 收盘价 × H 股股本 × 汇率，
                                **元**（行业软件单列的「港股市值」）
    ``ah_total_market_cap_cny`` A 股部分 + H 股部分，**元**。**参考量**，
                                **不是**行业口径总市值（行业口径见
                                ``indicator_snapshot.total_market_cap``，本函数
                                既不读也不写它）
    ``total_market_cap_ah``     同上，历史键名（保留兼容，勿再新增引用）
    ``ah_premium_pct``          A 相对 H 的溢价，**百分点**
    ``a_yield`` / ``h_yield``   A / H 股息率，**小数比例**，两侧独立
    ``identity_status``         ``ok`` / ``mismatch`` / ``not_comparable`` /
                                ``insufficient_data``（见模块 docstring §3）
    ``identity_deviation_pct``  只有 ``ok`` / ``mismatch`` 两态有值，百分点
    ``identity_ok``             向后兼容布尔便捷值：ok→True、mismatch→False、
                                其余→None
    ===========================  ===========================================

    ``identity_status`` 为 ``mismatch`` / ``not_comparable`` **不代表**这行数据
    有问题：那是「A、H 各自的分红节奏不同」的正常结果，调用方**不得**据此丢弃
    该股票（模块 docstring §3）。``units`` 字段显式标注市值/溢价/股息率单位。
    """
    anchor = _to_date(as_of) or cn_today()
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
            "ah_total_market_cap_cny": None,
            "total_market_cap_ah": None,
            "ah_premium_pct": None,
            "a_yield": None,
            "h_yield": None,
            "h_yield_hkd": None,
            "h_yield_after_tax": None,
            "ah_yield_gap": None,
            "ah_yield_ratio": None,
            "expected_h_yield": None,
            "identity_status": IDENTITY_STATUS_INSUFFICIENT,
            "identity_deviation_pct": None,
            "identity_ok": None,
            "identity_note": None,
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
            # 笔数是恒等式「可比性」的唯一依据：不一致 → not_comparable，
            # 只有一致时才给 identity_deviation_pct（模块 docstring §3）
            a_events=a_events, h_events=h_events,
        )

        base.update({
            "a_share_market_cap": cap["a_share_market_cap"],
            "h_share_market_cap_cny": cap["h_share_market_cap_cny"],
            "ah_total_market_cap_cny": cap["ah_total_market_cap_cny"],
            "total_market_cap_ah": cap["total_market_cap_ah"],
            "ah_premium_pct": cap["ah_premium_pct"],
            "a_yield": yields["a_yield"],
            "h_yield": yields["h_yield"],
            "h_yield_hkd": yields["h_yield_hkd"],
            "h_yield_after_tax": yields["h_yield_after_tax"],
            "ah_yield_gap": yields["ah_yield_gap"],
            "ah_yield_ratio": yields["ah_yield_ratio"],
            "expected_h_yield": yields["expected_h_yield"],
            "identity_status": yields["identity_status"],
            "identity_deviation_pct": yields["identity_deviation_pct"],
            "identity_ok": yields["identity_ok"],
            "identity_note": yields["identity_note"],
            "tax_rate": yields["tax_rate"],
            "units": {
                "market_cap": "CNY_yuan",
                "premium": "percent_points",
                "yields": "decimal_fraction",
                "dividend_per_share": "CNY_yuan_per_share",
            },
        })
        # 只有「某一侧股息率算不出来」才降级为 incomplete；恒等式
        # mismatch / not_comparable **绝不**影响 status（各算各的，§3）。
        if yields["reason"] is None:
            base["status"] = "ok"
        else:
            base["reason"] = yields["reason"]
    return results
