"""时点可见性（point-in-time，「当时可见」口径）查询层。

背景
----
本项目历史研究此前只能采用「最新重述回看」口径：计算 2015 年的市盈率时，
用的是 2015 年的股价，但配的是**今天才知道的、已被公司事后更正过的**财务数字。
`docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md` §8.1 因此只能声明：

    「PE/PB 历史研究序列首期采用『最新重述回看』口径……
      界面、API、导出和规则元数据必须标记『非当时可见、不用于回测』」

公布日来自两段拼接：
1. 历史归档域的年度公布日（1990-2024，74,509 条）
2. 东方财富 datacenter 的 `NOTICE_DATE`（2026-09-20 补入 295,139 条，
   覆盖**全部报告期**：一季报 / 中报 / 三季报 / 年报，1988-2026）

据此可还原「在某一天，投资者能看到的最新一期财报是哪一期」——
即「当时可见」口径，**季度频率**的回测成立。

能力与边界（重要，不得夸大）
----------------------------
- ✅ 支持：给定股票与日期，返回**当时可见的最新年度报告期**及其公布日；
  以及「该期财务数字在 as_of 当天是否已公开」的判定。
- ✅ **季度频率**：`latest_visible_period` 返回最近一期已公开的财报（任意频率）；
  需要年报时用 `frequency="annual"`。
- ✅ Web/回测出口（reports/119 §5 D 类，2026-11-14）：
  `visible_periods` 列出「每期从哪天开始可见」，
  `as_reported_period` + `financial_values` 取「当时可见那期的财务值」。
- ⚠️ 覆盖 6,264 只 / 369,648 条 / 1988-12-31 ~ 2026-06-30。
  极少数股票（新上市或暂停披露）无记录时返回 None，**不做推断**。

纪律
----
本模块**只回答「何时公开」与「那一期的数值是什么」**，不改变任何财务数值口径；
调用方决定如何使用。特别注意：`financial_values` 读的是库里**唯一一份**
income/balance/cash_flow 数值（=今天的最新重述值），本模块**不做时点数值还原**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.core.storage.duckdb_store import DuckDBStore

# 如实披露的边界，供 UI/导出引用
PIT_LIMITATIONS: tuple[str, ...] = (
    "季度频率：覆盖一季报/中报/三季报/年报（2026-09-20 起，此前仅年报）",
    "覆盖 6,264 只 / 369,648 条 / 1988-12-31 ~ 2026-06-30",
    "无公布日记录的报告期不参与判定（不假设未登记即当天可见）",
    "本能力只描述「何时公开」，不改变财务数值口径",
)


@dataclass(frozen=True)
class VisiblePeriod:
    """某股票在某一时点「当时可见」的年报期。"""

    stock_code: str
    report_date: date          # 可见的最新报告期
    announce_date: date        # 该报告期的公布日（<= as_of）
    days_since_announce: int   # 距公布日天数（用于「新鲜度」判断）

    @property
    def is_stale(self) -> bool:
        """超过一年未更新年报（用于提示数据陈旧，非错误）。"""
        return self.days_since_announce > 400


_FREQUENCY_FILTER = {
    "any": "",
    "annual": " AND EXTRACT(month FROM report_date) = 12",
    "interim": " AND EXTRACT(month FROM report_date) IN (6, 9)",
    "quarterly": " AND EXTRACT(month FROM report_date) IN (3, 9)",
}


def latest_visible_period(
    duck: DuckDBStore, stock_code: str, as_of: date, frequency: str = "any"
) -> VisiblePeriod | None:
    """返回 `as_of` 当天「当时可见」的最新报告期；无记录返回 None。

    `frequency`：`any`（默认，任意频率）/ `annual`（仅年报）/ `interim`（中报+三季报）
    / `quarterly`（一季报+三季报）。

    判定条件：`announce_date <= as_of`。缺失公布日的报告期**不参与**判定
    （不能假设未登记公布日就等于当天可见）。
    """
    if frequency not in _FREQUENCY_FILTER:
        raise ValueError(f"未知 frequency: {frequency}")
    rows = duck.read_query(
        """SELECT report_date, announce_date
           FROM financial_report_dates
           WHERE stock_code = ? AND announce_date <= CAST(? AS DATE)"""
        + _FREQUENCY_FILTER[frequency]
        + """
           ORDER BY report_date DESC
           LIMIT 1""",
        [stock_code, str(as_of)],
    )
    if not rows:
        return None
    report_date = rows[0]["report_date"]
    announce_date = rows[0]["announce_date"]
    if isinstance(report_date, str):
        report_date = date.fromisoformat(report_date)
    if isinstance(announce_date, str):
        announce_date = date.fromisoformat(announce_date)
    return VisiblePeriod(
        stock_code=stock_code,
        report_date=report_date,
        announce_date=announce_date,
        days_since_announce=(as_of - announce_date).days,
    )


def latest_visible_annual(
    duck: DuckDBStore, stock_code: str, as_of: date
) -> VisiblePeriod | None:
    """`latest_visible_period(frequency="annual")` 的兼容别名（仅年报）。"""
    return latest_visible_period(duck, stock_code, as_of, frequency="annual")


def was_visible(duck: DuckDBStore, stock_code: str, report_date: date, as_of: date) -> bool | None:
    """判断某报告期的财务数字在 `as_of` 当天是否已公开。

    返回 True/False；该报告期无公布日记录时返回 **None**（未知，不得当作 False）。
    """
    rows = duck.read_query(
        """SELECT announce_date FROM financial_report_dates
           WHERE stock_code = ? AND report_date = CAST(? AS DATE)""",
        [stock_code, str(report_date)],
    )
    if not rows:
        return None
    announce_date = rows[0]["announce_date"]
    if isinstance(announce_date, str):
        announce_date = date.fromisoformat(announce_date)
    return announce_date <= as_of


def coverage(duck: DuckDBStore) -> dict:
    """公布日域覆盖概况（供状态页/文档引用）。"""
    rows = duck.read_query(
        """SELECT COUNT(*) AS rows,
                  COUNT(DISTINCT stock_code) AS stocks,
                  MIN(report_date) AS first_period,
                  MAX(report_date) AS last_period,
                  MIN(announce_date) AS first_announce,
                  MAX(announce_date) AS last_announce
           FROM financial_report_dates"""
    )
    out = dict(rows[0]) if rows else {}
    out["limitations"] = list(PIT_LIMITATIONS)
    return out


# ─── 报告期可见日历 + 当时可见那期的数值（2026-11-14，reports/119 §5 D 类） ───
#
# 上面三个函数回答「as_of 当天最新一期是哪期」；下面这一层回答
# 「每一期从哪天开始可见」和「那一期的数值是什么」，供 Web/回测直接使用。
#
# 两个口径必须写清、不许含糊：
# 1. **可见日**取该报告期**最早**登记的公布日（同报告期多来源时取 MIN）——
#    回测要的是「我第一次能看到它」的日期，而不是某一次补录的日期；
# 2. **数值**是库里唯一的一份（income/balance/cash_flow 按 (stock_code, report_date)
#    只存一行 = 今天的最新重述值）。本模块不做「当时的原始数值」还原，
#    因为库里没有保存历史版本；调用方必须据此标注口径。

# 数值白名单所在的表，顺序即同名列的归属优先级（income 优先）。
FINANCIAL_VALUE_TABLES: tuple[str, ...] = (
    "income_statement",
    "balance_sheet",
    "cash_flow",
)

# 非财务科目列：主键 / 报告类型 / 原始 JSON 一律不进白名单
_BOOKKEEPING_COLUMNS: frozenset[str] = frozenset(
    {"stock_code", "report_date", "report_type", "raw_data"}
)

# 白名单缓存：key = DuckDB 文件路径（进程内多库/多测试夹具不会串味）
_FIELD_CATALOG_CACHE: dict[str, dict[str, str]] = {}


@dataclass(frozen=True)
class ReportVisibility:
    """某报告期「从哪一天开始可见」。"""

    stock_code: str
    report_date: date
    announce_date: date
    source: str                # financial_report_dates.source（公布日来源，非数值来源）

    @property
    def report_type(self) -> str:
        """报告期类型，与 `app/core/supplement.py::report_type_of` 同口径。

        这里只保留一行的映射而不 import 那个模块：它连带拉起适配器/补采域
        （≈0.75s），本模块却被 CLI 与 Web 双方依赖，不值得为三个字符串付这笔启动费。
        """
        return {12: "annual", 6: "semi_annual"}.get(self.report_date.month, "quarterly")


@dataclass(frozen=True)
class ReportValues:
    """某一报告期各字段的数值（含「哪些表命中」的事实）。"""

    stock_code: str
    report_date: date
    values: dict[str, float | None]
    tables: tuple[str, ...]    # 真正返回了行的表；空元组 = 三表都没有该期

    @property
    def available_fields(self) -> tuple[str, ...]:
        return tuple(field for field, value in self.values.items() if value is not None)

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return tuple(field for field, value in self.values.items() if value is None)


def _coerce_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def visible_periods(
    duck: DuckDBStore,
    stock_code: str,
    *,
    as_of: date | None = None,
    frequency: str = "any",
    limit: int | None = None,
) -> list[ReportVisibility]:
    """列出报告期 → 首次可见日，按 **report_date 升序**返回（回测对齐用）。

    `as_of` 给定时只返回「当天已可见」的期；`frequency` 口径与
    `latest_visible_period` 完全一致；`limit` 若给定则先取**最新**的 N 期
    （仍按升序返回）。
    """
    if frequency not in _FREQUENCY_FILTER:
        raise ValueError(f"未知 frequency: {frequency}")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")

    params: list[object] = [stock_code]
    sql = (
        """WITH first_seen AS (
               SELECT report_date, MIN(announce_date) AS announce_date
               FROM financial_report_dates
               WHERE stock_code = ?"""
        + _FREQUENCY_FILTER[frequency]
        + """
               GROUP BY report_date
           )
           SELECT f.report_date, f.announce_date, MIN(d.source) AS source
           FROM first_seen f
           JOIN financial_report_dates d
             ON d.stock_code = ?
            AND d.report_date = f.report_date
            AND d.announce_date = f.announce_date"""
    )
    params.append(stock_code)
    if as_of is not None:
        sql += "\n           WHERE f.announce_date <= CAST(? AS DATE)"
        params.append(str(as_of))
    sql += "\n           GROUP BY f.report_date, f.announce_date\n           ORDER BY f.report_date DESC"
    if limit is not None:
        sql += "\n           LIMIT ?"
        params.append(limit)

    rows = duck.read_query(sql, params)
    periods = [
        ReportVisibility(
            stock_code=stock_code,
            report_date=_coerce_date(row["report_date"]),
            announce_date=_coerce_date(row["announce_date"]),
            source=str(row.get("source") or ""),
        )
        for row in rows
    ]
    periods.reverse()
    return periods


def as_reported_period(
    duck: DuckDBStore, stock_code: str, as_of: date, frequency: str = "any"
) -> ReportVisibility | None:
    """`as_of` 当天可见的最近一期（含公布日与来源）；无记录返回 None。"""
    periods = visible_periods(duck, stock_code, as_of=as_of, frequency=frequency, limit=1)
    return periods[0] if periods else None


def financial_field_catalog(duck: DuckDBStore) -> dict[str, str]:
    """income/balance/cash_flow 的数值列 → 所属表（fields 白名单的单一真相）。

    从 `information_schema` 现读，避免在本模块手抄一份列名清单随时间腐烂；
    只排除主键/报告类型/原始 JSON。同名列按 `FINANCIAL_VALUE_TABLES` 顺序先到先得。
    """
    key = str(duck.db_path)
    cached = _FIELD_CATALOG_CACHE.get(key)
    if cached is not None:
        return dict(cached)

    placeholders = ", ".join("?" for _ in FINANCIAL_VALUE_TABLES)
    rows = duck.read_query(
        f"""SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'main'
              AND table_name IN ({placeholders})""",
        list(FINANCIAL_VALUE_TABLES),
    )
    columns_by_table: dict[str, set[str]] = {name: set() for name in FINANCIAL_VALUE_TABLES}
    for row in rows:
        table = str(row["table_name"])
        column = str(row["column_name"])
        if table in columns_by_table and column not in _BOOKKEEPING_COLUMNS:
            columns_by_table[table].add(column)

    catalog: dict[str, str] = {}
    for table in FINANCIAL_VALUE_TABLES:
        for column in sorted(columns_by_table[table]):
            catalog.setdefault(column, table)
    _FIELD_CATALOG_CACHE[key] = catalog
    return dict(catalog)


def financial_values(
    duck: DuckDBStore,
    stock_code: str,
    report_date: date,
    fields: tuple[str, ...] | list[str],
) -> ReportValues:
    """读某一报告期在 income/balance/cash_flow 的数值。

    字段名必须在 `financial_field_catalog` 白名单内，否则抛 `ValueError`
    （API 层据此返回 400，而不是把列名拼进 SQL）。字段在三表中都不存在时
    返回全 None 且 `tables == ()`——调用方据此区分「该期没数据」与「该字段空值」。
    """
    catalog = financial_field_catalog(duck)
    unknown = tuple(field for field in fields if field not in catalog)
    if unknown:
        raise ValueError(f"字段不在 income/balance/cash_flow 白名单内: {', '.join(unknown)}")

    grouped: dict[str, list[str]] = {}
    for field in fields:
        grouped.setdefault(catalog[field], []).append(field)

    values: dict[str, float | None] = dict.fromkeys(fields, None)
    hits: list[str] = []
    for table in FINANCIAL_VALUE_TABLES:      # 固定顺序 → 输出稳定
        columns = grouped.get(table)
        if not columns:
            continue
        select_list = ", ".join(f'"{column}"' for column in columns)
        rows = duck.read_query(
            f"""SELECT {select_list} FROM {table}
                WHERE stock_code = ? AND report_date = CAST(? AS DATE)
                LIMIT 1""",
            [stock_code, str(report_date)],
        )
        if not rows:
            continue
        hits.append(table)
        for column in columns:
            raw = rows[0].get(column)
            values[column] = float(raw) if raw is not None else None

    return ReportValues(
        stock_code=stock_code,
        report_date=report_date,
        values=values,
        tables=tuple(hits),
    )
