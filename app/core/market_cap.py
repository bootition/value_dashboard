"""总市值唯一实现（2026-11-16 收敛，原先三套各自为政）。

背景（实测 2026-11-16，同一 ``latest_price_date``）
--------------------------------------------------
总市值曾有 **三套独立实现**，彼此口径不一致：

1. ``app/core/indicators/calculator.py``：``close × stock_meta.total_shares``
   （**当前**股本配历史价格 → 增发高估、回购/送转低估）；
2. ``app/core/etf_fundamentals.py``：``close × COALESCE(
   share_capital_history.total_shares, stock_meta.total_shares)`` 配
   ``ASOF JOIN share_capital_history``（**当时**股本，正确口径）；
3. ``app/core/ah_metrics.py``：``ah_market_cap`` 自算 A 股部分。

同一交易日上实现 1 与实现 2 在 5,546 只里有 185 只偏离 >1%，最差
``689009`` −90%、``600962`` −67.7%；全 A 合计差约 4,800 亿元（−0.41%）。

本模块是唯一产地
----------------
- :func:`market_cap_from`：唯一的市值算术（``close × shares``，非法输入 → None）；
- :func:`resolve_shares_asof`：唯一的「时点股本」解析规则
  —— ``<= price_date`` 的最近一条 ``share_capital_history.total_shares``，
  缺失回退 ``stock_meta.total_shares``，并如实返回 ``source``；
- :func:`latest_market_cap`：单点市值（指标主链用）；
- :func:`market_cap_by_date`：按交易日聚合的市值序列（ETF 基本面用）；
- :func:`asof_shares_join_sql` / :func:`asof_shares_value_sql`：同一规则的
  SQL 形态，供已经内嵌该 JOIN 的聚合查询（如指数仪表盘）复用，避免再长出第 N 套。

行为兼容（硬约束）
------------------
``share_capital_history`` 无数据时，结果必须与旧实现 ``close ×
stock_meta.total_shares`` **完全一致**（``tests/regression/
test_market_cap_single_source.py`` 钉死）。因此：

- as-of 查询只在历史表缺行时回退，**绝不插值、绝不用未来行**；
- 字段名/类型/NULL 语义不变：无法计算时一律 ``None``，不返回 0 或负数；
- 历史表在旧库尚不存在（或测试库未建表）时按「无历史」处理，不抛错。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

__all__ = [
    "SOURCE_HISTORY",
    "SOURCE_META",
    "SOURCE_NONE",
    "ShareResolution",
    "asof_shares_join_sql",
    "asof_shares_value_sql",
    "latest_market_cap",
    "market_cap_by_date",
    "market_cap_from",
    "resolve_shares_asof",
]

SOURCE_HISTORY = "history"  # 来自 share_capital_history（当日或之前最近一条）
SOURCE_META = "meta"        # 历史缺失，回退 stock_meta 当前股本
SOURCE_NONE = "none"        # 两处都没有可用股本

# 价格表白名单：只允许这两张（其余一律 ValueError，避免拼接注入）
_PRICE_TABLES = ("price_daily_raw", "price_daily_qfq")

# 唯一的「历史股本有效行」定义：正股本。非正行视为无效 → 回退 stock_meta。
_HISTORY_SOURCE_SQL = (
    "SELECT stock_code, effective_date, total_shares "
    "FROM share_capital_history WHERE total_shares > 0"
)

# 标量 as-of 取数：直查基表（实测比套一层派生表快约 2 倍：
# 300 只 1.15s → 0.60s，即每只 3.8ms → 2.0ms）。
_ASOF_SHARES_SQL = """
    WITH hist AS (
        SELECT total_shares, effective_date FROM share_capital_history
        WHERE stock_code = ? AND total_shares > 0 AND effective_date <= CAST(? AS DATE)
        ORDER BY effective_date DESC
        LIMIT 1
    ), meta AS (
        SELECT total_shares FROM stock_meta WHERE stock_code = ?
    )
    SELECT (SELECT total_shares FROM hist) AS history_shares,
           (SELECT effective_date FROM hist) AS history_date,
           (SELECT total_shares FROM meta) AS meta_shares
"""

_META_ONLY_SQL = "SELECT total_shares AS meta_shares FROM stock_meta WHERE stock_code = ?"


class QuerySource(Protocol):
    """只读查询接口（``DuckDBStore`` 与本项目各计算器都满足）。"""

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]: ...


@dataclass(frozen=True)
class ShareResolution:
    """时点股本解析结果（:func:`resolve_shares_asof` 的返回类型）。"""

    stock_code: str
    shares: float | None
    source: str
    effective_date: date | None = None
    price_date: date | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stock_code": self.stock_code,
            "shares": self.shares,
            "source": self.source,
            "effective_date": self.effective_date,
            "price_date": self.price_date,
        }


# ─── 基础数值归一 ───────────────────────────────────────────────────


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _positive(value: Any) -> float | None:
    """有限正数 → float；其余（None/bool/0/负/NaN/Inf/非数字）→ None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _is_missing_relation(error: Exception) -> bool:
    message = str(error)
    return "does not exist" in message or "Catalog Error" in message


# ─── 唯一算术 ───────────────────────────────────────────────────────


def market_cap_from(close: Any, shares: Any) -> float | None:
    """总市值 = 收盘价 × 总股本（元）。

    任一输入缺失/非正/非有限（None、0、负数、NaN、Inf、非数字）→ ``None``；
    与旧实现 ``if close and shares: close * shares`` 的 NULL 语义一致（旧实现
    对负价格会算出负市值，这里按「非法输入」收紧为 ``None``）。
    """
    price = _positive(close)
    count = _positive(shares)
    if price is None or count is None:
        return None
    return price * count


# ─── 唯一时点股本规则 ───────────────────────────────────────────────


def resolve_shares_asof(
    duck: QuerySource,
    stock_code: str,
    price_date: Any = None,
) -> ShareResolution:
    """解析 ``price_date`` 当日的总股本。

    规则（唯一口径，不要在各调用点重写）：

    1. 取 ``effective_date <= price_date`` 的最近一条
       ``share_capital_history.total_shares``（严格 as-of，绝不用未来行）→
       ``source='history'``；
    2. 该股票历史缺行（或历史表尚未建立）→ 回退
       ``stock_meta.total_shares`` → ``source='meta'``；
    3. 两处都没有可用正股本 → ``shares=None``, ``source='none'``。

    ``price_date`` 缺失（None/无法解析）时**不做 as-of**，直接走第 2 步的
    stock_meta 回退：没有价格日就没有「时点」，用任意历史行都可能引入未来
    数据泄漏。
    """
    code = str(stock_code or "").strip()
    if not code:
        return ShareResolution(stock_code=code, shares=None, source=SOURCE_NONE)

    as_of = _to_date(price_date)
    rows: list[dict] = []
    if as_of is not None:
        try:
            rows = duck.read_query(_ASOF_SHARES_SQL, [code, as_of, code])
        except Exception as error:  # noqa: BLE001 - 旧库没有该表时按「无历史」处理
            if not _is_missing_relation(error):
                raise
            rows = []
    row = rows[0] if rows else {}

    history = _positive(row.get("history_shares"))
    if history is not None:
        return ShareResolution(
            stock_code=code,
            shares=history,
            source=SOURCE_HISTORY,
            effective_date=_to_date(row.get("history_date")),
            price_date=as_of,
        )

    meta = _positive(row.get("meta_shares"))
    if meta is None:
        meta_rows = duck.read_query(_META_ONLY_SQL, [code])
        meta = _positive(meta_rows[0].get("meta_shares")) if meta_rows else None
    if meta is None:
        return ShareResolution(
            stock_code=code, shares=None, source=SOURCE_NONE, price_date=as_of,
        )
    return ShareResolution(
        stock_code=code, shares=meta, source=SOURCE_META, price_date=as_of,
    )


# ─── 单点市值（指标主链） ───────────────────────────────────────────


def _validate_price_table(price_table: str) -> str:
    if price_table not in _PRICE_TABLES:
        raise ValueError(f"unsupported price table: {price_table!r}")
    return price_table


def _latest_close_row(
    duck: QuerySource, stock_code: str, price_date: date | None, price_table: str,
) -> dict[str, Any]:
    table = _validate_price_table(price_table)
    if price_date is None:
        sql = (
            f"SELECT trade_date, close FROM {table} "
            "WHERE stock_code = ? AND close IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT 1"
        )
        rows = duck.read_query(sql, [stock_code])
    else:
        sql = (
            f"SELECT trade_date, close FROM {table} "
            "WHERE stock_code = ? AND close IS NOT NULL AND trade_date <= CAST(? AS DATE) "
            "ORDER BY trade_date DESC LIMIT 1"
        )
        rows = duck.read_query(sql, [stock_code, price_date])
    return rows[0] if rows else {}


def latest_market_cap(
    duck: QuerySource,
    stock_code: str,
    *,
    close: Any = None,
    price_date: Any = None,
    price_table: str = "price_daily_raw",
) -> dict[str, Any]:
    """单只股票的单点总市值（含股本来源）。

    ``close`` / ``price_date`` 可显式传入（调用方已经取过价格时避免重复查询）；
    缺失的一侧按 ``price_date`` 向前取 ``price_table`` 的最近一条收盘价。

    返回（键名稳定）::

        {
          "stock_code": str,
          "price_date": date | None,
          "close": float | None,
          "shares": float | None,
          "shares_source": "history" / "meta" / "none",
          "shares_effective_date": date | None,
          "total_market_cap": float | None,   # 元
        }
    """
    code = str(stock_code or "").strip()
    as_of = _to_date(price_date)
    price = None if close is None else float(close)
    if price is None or as_of is None:
        row = _latest_close_row(duck, code, as_of, price_table)
        if price is None and row.get("close") is not None:
            try:
                price = float(row["close"])
            except (TypeError, ValueError):
                price = None
        if as_of is None:
            as_of = _to_date(row.get("trade_date"))

    resolution = resolve_shares_asof(duck, code, as_of)
    return {
        "stock_code": code,
        "price_date": as_of,
        "close": price,
        "shares": resolution.shares,
        "shares_source": resolution.source,
        "shares_effective_date": resolution.effective_date,
        "total_market_cap": market_cap_from(price, resolution.shares),
    }


# ─── 市值序列（ETF 基本面等聚合） ───────────────────────────────────


def asof_shares_join_sql(
    price_alias: str = "p",
    *,
    history_alias: str = "sh",
    meta_alias: str = "s",
) -> str:
    """「价格表 ASOF 连接历史股本 + 回退 stock_meta」的规范 JOIN 片段。

    供已经内嵌该 JOIN 的聚合 SQL 复用（单一口径，不要各写一份）::

        f"FROM price_daily_qfq {asof_shares_join_sql('p')} WHERE ..."
        # 再用 asof_shares_value_sql() 取股本表达式
    """
    return (
        f"ASOF LEFT JOIN ({_HISTORY_SOURCE_SQL}) {history_alias} "
        f"ON {price_alias}.stock_code = {history_alias}.stock_code "
        f"AND {price_alias}.trade_date >= {history_alias}.effective_date "
        f"LEFT JOIN stock_meta {meta_alias} "
        f"ON {meta_alias}.stock_code = {price_alias}.stock_code"
    )


def asof_shares_value_sql(*, history_alias: str = "sh", meta_alias: str = "s") -> str:
    """与 :func:`asof_shares_join_sql` 配套的时点股本表达式（股）。"""
    return f"COALESCE({history_alias}.total_shares, {meta_alias}.total_shares)"

def market_cap_by_date(
    duck: QuerySource,
    stock_codes: list[str],
    *,
    since: Any = None,
    price_table: str = "price_daily_qfq",
) -> list[dict[str, Any]]:
    """按交易日聚合 ``∑(收盘价 × 时点股本)``，单位元。

    口径与 :func:`resolve_shares_asof` 完全一致：ASOF 取当日或之前最近的
    历史股本，缺行回退 stock_meta；``shares <= 0`` 的股票该日不计入。

    返回 ``[{"trade_date": date, "total_market_cap": float, "companies": int}]``。

    ⚠️ 聚合值的末位：跨 5,000+ 只股票求和走 DuckDB 并行聚合，**同一条 SQL
    重复执行**本身就有 ≤1e-15 相对量的浮点末位抖动（实测 2026-11-16：同一
    查询连跑 5 次，2,438 个交易日里有 ~1,600 个点末位不同，最大相对差
    8.9e-16）。这是求和顺序而非口径差异；单只单日（见
    :func:`market_cap_from` / :func:`latest_market_cap`）是逐位精确的，
    一致性断言请用单股单点。
    """
    table = _validate_price_table(price_table)
    codes = [str(code) for code in stock_codes if str(code).strip()]
    if not codes:
        return []
    placeholders = ", ".join("?" for _ in codes)
    params: list[Any] = list(codes)
    date_filter = ""
    since_date = _to_date(since)
    if since_date is not None:
        date_filter = "AND p.trade_date >= CAST(? AS DATE)"
        params.append(since_date)
    shares_expr = asof_shares_value_sql()
    sql = f"""
        SELECT p.trade_date AS trade_date,
               SUM(p.close * {shares_expr}) AS total_market_cap,
               COUNT(DISTINCT p.stock_code) AS companies
        FROM {table} p
        {asof_shares_join_sql("p")}
        WHERE p.stock_code IN ({placeholders})
          AND p.close IS NOT NULL
          AND {shares_expr} > 0
          {date_filter}
        GROUP BY p.trade_date
        ORDER BY p.trade_date
    """
    rows = duck.read_query(sql, params)
    return [
        {
            "trade_date": row["trade_date"],
            "total_market_cap": float(row["total_market_cap"]),
            "companies": int(row["companies"]),
        }
        for row in rows
        if row.get("total_market_cap") is not None
    ]
