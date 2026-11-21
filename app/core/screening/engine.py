"""筛选引擎 — 规则 JSON → DuckDB SQL 执行

两阶段执行策略 (TECH_PLAN §2.3.3, 审查问题3修订):
1. 基础股票池: 从 stock_meta 出发，按 ST/停牌/上市年限过滤 + per-stock 最新快照 (LEFT JOIN LATERAL)
2. 横截面排名: DuckDB 窗口函数 (全市场 + 申万行业)
3. 条件过滤: 应用用户条件 (DSL CodeGen 生成，M3 用内建指标子集)

PRD §12.3 (SC11): 排名分母只由基础股票池预设决定，
用户添加的筛选条件不得重新计算或缩小排名分母。
"""

# 依据索引（本模块「为什么」的唯一落点；行内只写「依据 S<n>」，本块登记全部原文小节号）
# S1  reports/68 P3 / §5 / §6 —— 国债基准与股息率利差（P3）；历史研究统计字段（P4 的 §5/§6）：
#     只 join 已发布统计域的最新版本，不触碰快照主路径。
# S2  reports/76 P3-4 —— save-time 字段校验。
# S3  reports/122 §2.3 J9 / §2.6 J5 —— J9 字段级遮蔽（字段当前不可信 → 拒绝执行）；
#     J5 `dividend_yield` 与 `ttm_dividend_yield` 口径分家，废弃字段仍可执行但结果必须自带警告。
# S4  reports/124 —— 换列时的量纲倍数（新值 = 旧值 × 本倍数），2026-11-15 实测。
# S5  reports/131 §9.2 P0-7 / P0-8 —— 跨年口径断点：派生字段与原始列都可引用，
#     但必须带「引用跨年口径断点原始列」警告。
# S6  reports/133 —— 列名必须与实际口径一致（`roce_latest` 不是 TTM）。
# S7  reports/139 §5.1 CB-09 / §11.4 / D58 —— 字段白名单的唯一判定入口（R1 单一实现）；
#     v48 补采科目登记后筛选可引用、空值原因可归因。


from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Iterator
from typing import Any

from app.core.blacklist import blacklisted_codes, exclusion_condition
from app.core.dsl.codegen import quote_identifier
from app.core.dsl.derived_fields import (
    DERIVED_FIELDS,
    series_break_warnings,
)
from app.core.dsl.registry import validate_expression_identifier
from app.core.screening import defaults as screening_defaults
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# ─── 只读探针批量化（2026-11-21，`.planning/2026-screening-latency`）──────
#
# 正式库实测：`DuckDBStore.read_query()` 是 open-per-query，**每次查询新开一条
# 只读连接要 ~75-85ms**；同一条连接上的后续查询只要 0.2-0.8ms。一次筛选以前要
# 开 6-8 条连接（混合报告期 / 基础池 / 数据日期 / 覆盖值配对 / 严格血缘 …），
# 光"开连接"就吃掉 ~500-700ms。
#
# 做法：一次 `run()` 里开**一条**连接，把几条互不依赖的探针挤在同一批里跑。
# 关键约束（别改成"长期持有连接"）：连接生命周期只覆盖一批探针，
# 用完立刻还回去 —— 本进程长期握着只读句柄会让自动更新子进程打不开库文件
# （见 `DuckDBStore._build_read_cache` 的实测记录）。正式环境的连接复用开关
# 仍然关着，这里只是**少开几条**，不是**一直开着**。

def _rows_as_dicts(cursor: Any) -> list[dict]:
    """把 sqlite3 游标的结果整批转成字典列表（列名取 `cursor.description`）。"""
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


class _BatchedSqlite:
    """把多条只读 SQLite 查询挤进同一条连接（同 `_BatchedReader` 的理由）。

    SQLite 侧同样是 open-per-query（`SQLiteStore.query()` 每次开一条只读连接），
    正式库实测**每开一条 ~27-47ms**。只暴露 `query(sql, params)` —— 与
    `SQLiteStore` 同签名，因此可直接喂给 `blacklist.blacklisted_codes()` 这类
    "只调用 `.query()`"的共享函数，不复制判定逻辑。
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        return _rows_as_dicts(self._conn.execute(sql, params or []))


class _BatchedReader:
    """把多条只读查询挤进同一条 DuckDB 连接，语义与 `read_query()` 一致。

    只暴露 `read_query(sql, params)` —— 与 `DuckDBStore` 同签名，因此可以当
    "鸭子类型的 store" 直接喂给只吃 `read_query` 的共享判定函数
    （如 `app.core.data_quality.snapshot_period_mismatches`），保证**只有一处真相**。
    """

    __slots__ = ("_conn", "_opener", "_closed", "_entered")

    def __init__(self, conn: Any = None, *, opener: Any = None) -> None:
        """两种构造方式：

        * `_BatchedReader(conn)` —— 连接已就绪；
        * `_BatchedReader(opener=<contextmanager 工厂>)` —— **惰性**：首次
          `read_query()` 才真的 `__enter__()`。这是必须的：`_read_scope()`
          在 `run()` 早期进入，而"参数全显式给值"的调用（上限校验失败等）
          **一次库查询都不该发**。改成急开连接后，连"库文件不存在"的调用
          都会先去开库 → 把本该是 `ValueError` 的参数校验变成
          `FileNotFoundError`（2026-11-21 S1 实测踩到）。
        """
        self._conn = conn
        self._opener = opener
        self._closed = False
        # 只有真的 `__enter__()` 过才允许 `__exit__()`：`read_connection()` 是
        # `@contextlib.contextmanager` 生成的，**没进过就退出**会抛
        # `RuntimeError: generator didn't yield`（2026-11-21 实测）。
        self._entered = False

    def _ensure_conn(self) -> Any:
        if self._conn is None:
            if self._opener is None:
                raise ValueError("_BatchedReader has neither conn nor opener")
            self._conn = self._opener.__enter__()
            self._entered = True
        return self._conn

    def close(self) -> None:
        """关闭惰性打开的连接；未打开过则纯 no-op。"""
        if self._closed:
            return
        self._closed = True
        opener, self._opener = self._opener, None
        self._conn = None
        if opener is not None and self._entered:
            self._entered = False
            opener.__exit__(None, None, None)

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        # 「恰好一条 SELECT」校验必须与 `DuckDBStore.read_query()` 逐字一致：
        # 2026-11-25 独立审查实测，`SELECT 1 AS a; SELECT 2 AS b` 在这里会被
        # **执行并返回最后一条**（store 则抛 ValueError）。`_read_scope()` 拿的是
        # `read_connection()`，写锁回退窗口里它可能是读写连接 —— 于是"多语句"
        # 就等于多出一条没人审过的可执行语句。成本 <1ms，别为省这点把它删了。
        import duckdb

        conn = self._ensure_conn()
        statements = conn.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise ValueError("read_query accepts exactly one SELECT statement")
        cursor = conn.execute(sql, params or [])
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

# ─── 操作符 → SQL 映射 ─────────────────────────────────────────────

OP_MAP: dict[str, str] = {
    ">": ">",
    "<": "<",
    ">=": ">=",
    "<=": "<=",
    "=": "=",
    "!=": "!=",
    "between": "BETWEEN",
    "in": "IN",
    "is_null": "IS NULL",
    "is_not_null": "IS NOT NULL",
}

# 指标快照表中可用的指标列
SNAPSHOT_COLUMNS: set[str] = {
    "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm", "dividend_yield",
    "total_market_cap", "circ_market_cap",
    "roe", "roa", "gross_margin", "net_margin", "roic", "cf_to_net_profit",
    # ROCE 暴露三个**窗口**（5/10/20 年），每个窗口都必须拿满对应年数才出值，
    # 所以名字与实际口径一致（依据 S6）。`roce_latest` 是详情页的现况对照；
    # `roce_*_years` / `roce_eligible` 是口径内部门禁，不暴露给用户选。
    "roce_5y_median", "roce_10y_median", "roce_20y_median",
    "revenue_yoy", "net_profit_yoy", "deducted_profit_yoy",
    "revenue_cagr3", "revenue_cagr5", "net_profit_cagr3", "net_profit_cagr5",
    "deducted_profit_cagr3", "deducted_profit_cagr5",
    "debt_ratio", "current_ratio", "quick_ratio",
    "interest_bearing_debt", "interest_coverage", "goodwill_ratio",
    "payout_ratio", "dps", "consecutive_div_years",
    "cumulative_dividend_amount", "cumulative_financing_amount",
    "dividend_financing_ratio_pct",
    "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
    "latest_close", "turnover_rate", "avg_volume", "period_return",
    "annualized_volatility", "max_drawdown",
    # 国债基准与股息率利差（P3，依据 S1）
    "ttm_dividend_yield",
    "div_yield_spread_0p25y", "div_yield_spread_0p5y", "div_yield_spread_1y",
    "div_yield_spread_2y", "div_yield_spread_3y", "div_yield_spread_5y",
    "div_yield_spread_7y", "div_yield_spread_10y", "div_yield_spread_30y",
}

# ─── 废弃字段（仍可执行，但不再出现在指标列表里）─────────────────────────────
# 2026-11-14（依据 S3 §2.6 J5）：`dividend_yield` 与 `ttm_dividend_yield` 在
# 正式库实测 **3,605 只精确 100 倍关系** —— 同一口径、只是时点基准不同：
#   dividend_yield     分母用 `now()`（今天）→ 分子分母跨期，非 TTM 口径；
#   ttm_dividend_yield 分母用 `latest_price_date`（快照价日）→ 自洽可复现。
# 两者同时暴露等于让用户在两列几乎一样的数字里挑，且旧列会随"今天"漂移
# （昨天筛出的结果今天不一样，不可复现）。
#
# 处理（不删列、不删口径，避免打断已保存规则）：
#   1. 标 deprecated，`/api/screening/indicators` 不再列出它（进 unavailable）；
#   2. 旧规则引用它 → **仍可执行**，结果里带 warnings。
DEPRECATED_COLUMNS: dict[str, str] = {
    "dividend_yield": "ttm_dividend_yield",
}

# 换列时的**量纲倍数**（新值 = 旧值 × 本倍数）。2026-11-15（依据 S4）实测：
# `dividend_yield` 存小数（0.025 = 2.5%），`ttm_dividend_yield` 存百分数（2.5），
# 正式库 3,735 行**全部恰为 100 倍**。若只提示"改用 ttm_dividend_yield"而不说倍数，
# 用户把 `dividend_yield > 0.03` 直接换成 `ttm_dividend_yield > 0.03` 会命中几乎全市场。
# 注意：这是**存储量纲差**，不是"时点基准差"——两者同时为空/非空的集合也完全相同。
DEPRECATED_SCALE: dict[str, float] = {
    "dividend_yield": 100.0,
}


def deprecated_field_warnings(fields: Any) -> list[str]:
    """把引用到的废弃字段翻译成人话警告（顺序稳定，便于断言与展示）。"""
    warnings: list[str] = []
    for field in sorted({str(item) for item in (fields or ())}):
        replacement = DEPRECATED_COLUMNS.get(field)
        if replacement is None:
            continue
        scale = DEPRECATED_SCALE.get(field)
        if scale is not None:
            warnings.append(
                f"field_deprecated:{field}, 建议改用 {replacement}"
                f"（注意量纲差 {scale:g} 倍：{replacement} = {field} × {scale:g}）"
            )
        else:
            warnings.append(f"field_deprecated:{field}, 建议改用 {replacement}")
    return warnings


def series_break_field_warnings(fields: Any) -> list[str]:
    """引用"跨年口径断点原始列"时的警告（依据 S5 §9.2 P0-7 / P0-8）。

    与 :func:`deprecated_field_warnings` **刻意分开**：
    - ``field_deprecated:*`` 表示整列口径不可复现（分母用 today、量纲漂移）；
    - ``field_semantics_break:*`` 表示列在各自区间内仍然正确，只是**跨断点
      不可直接比较**（2018 减值的符号翻转、2020 预收的重分类）。
    两者混在一起会让用户以为历史数据全废，于是不敢用任何时段的数据。

    规则仍然**可执行**（原始列保留可查），只是结果必须自带警告 —— 静默给出
    跨断点汇总值（尤其"正负相消的假低减值"）正是 P0-7 要消灭的东西。
    """
    return series_break_warnings(fields)


# ─── strict 模式：派生字段豁免（2026-11-23，用户实测"点了严格模式一条都筛不出来"）
#
# `strict_only` 的本意：**不给用户看没把握的数据**。对"抓取型源数据字段"，
# 这条靠字段级 strict 血缘落实（`source_audit.confidence='strict'` 且值与快照一致）。
# 但**本项目自算的派生字段**（`roce_*` 十列、`total_market_cap`、`pe_ttm`、均线、
# YoY、CAGR…）**按定义不存在抓取事件**，永远不会有字段级 strict 血缘 —— 旧实现
# 对它们也要求抓取血缘，等于让 strict 模式对这些指标**永久空集**（正式库实测：
# `roce_10y_median` 在 `source_audit` 里 0 条记录；`total_market_cap` 338,733 条
# **全是** `source='derived_calculator'` 的 approximate）。
#
# 正确的语义：strict 模式卡的是**抓取字段的血缘**，派生字段的可信度由"谁算的"决定。
# 判定只在**有证据**时豁免，缺证据一律照旧卡住（fail-closed）：
#   1. **注册表**（静态、有门禁核对）：`derived_snapshot_domains()`（RG4 在核对）、
#      `EXTENDED_COLUMNS`（indicator_ext 自算域）、`DERIVED_FIELDS`（DSL/SQL 派生）。
#   2. **生产者证据**：该字段在 `source_audit` 里**有记录且全部**由
#      `derived_calculator` 写入（= 唯一生产者是本项目计算器）。
#      **零审计记录的字段不豁免** —— 否则"删掉血缘就能绕过 strict"就成了新的
#      fail-open，正是原 docstring 担心的事。
# SQL 派生字段的**输入原始列**（`DERIVED_FIELDS[*].raw_columns`）仍在快照池里可查，
# 因此豁免时把 strict 检查**下沉到输入列**上继续做（见 `derived_input_fields`）。
DERIVED_AUDIT_SOURCE = "derived_calculator"


def registered_derived_fields() -> frozenset[str]:
    """本项目自算派生字段的静态注册表（strict 豁免的静态依据，与门禁同源）。

    三个来源都是既有登记处，不新增一份平行清单：
    - `derived_snapshot_domains()`：RG4 门禁逐列核对的快照派生域（ROCE 十列）；
    - `EXTENDED_COLUMNS`：indicator_ext 扩展指标域（本模块自己的登记处）；
    - `DERIVED_FIELDS`：跨年口径断点派生字段（SQL 真源在 dsl/derived_fields.py）。
    """
    from app.core.indicators.calculator import derived_snapshot_domains

    fields: set[str] = set(EXTENDED_COLUMNS) | set(DERIVED_FIELDS_BY_KEY)
    for domain in derived_snapshot_domains():
        fields.update(domain.columns)
    return frozenset(fields)


def derived_input_fields(field: str) -> tuple[str, ...]:
    """SQL 派生字段的输入原始列（`derived_from`），用于把 strict 检查下沉一层。

    只返回**能在快照池里查值**的列（`base_pool` 暴露为 `{table}__{column}`），
    因此 `_build_strict_clause` 可以对它们做与源数据字段**完全相同**的值级校验。
    快照派生域（ROCE / 市值 / 均线…）的输入是表达式内部量，无对应列 → 返回空。
    """
    derived = DERIVED_FIELDS_BY_KEY.get(field)
    if derived is None:
        return ()
    candidates = tuple(
        f"{derived.table}.{column}" for column in derived.raw_columns
    )
    return tuple(field_name for field_name in candidates if field_name in NORMALIZED_FIELDS)


def build_strict_mode_warning(
    *,
    total_matched: int,
    missing_fields: list[str],
    exempt_fields: list[str],
    enforced_fields: list[str],
) -> str | None:
    """严格模式无匹配时给用户**可行动**的解释（不再谎报"永远无法满足"）。

    - `missing_fields`：被 strict 卡住的**源数据**字段（没有任何 strict 血缘）；
    - `exempt_fields`：按定义豁免的**派生**字段（本项目自算，无抓取事件）；
    - `enforced_fields`：真正参与 strict 谓词的字段。
    """
    if total_matched > 0:
        return None
    exempt_note = ""
    if exempt_fields:
        exempt_note = (
            "；其中 " + "、".join(exempt_fields) + " 是本项目自算的派生字段，"
            "按设计没有字段级抓取血缘，已豁免字段级 strict 校验（它们的输入字段仍逐个校验）"
        )
    if missing_fields:
        return (
            "严格可信模式排除了全部行：以下源数据字段没有任何 strict 血缘记录，"
            "严格模式会把用到它们的行全部排除 —— "
            + "、".join(missing_fields)
            + "。可以这样解决：① 切换到「包含近似可信数据」先看完整结果；"
            "② 等这些字段的抓取血缘补齐后再用严格模式"
            + exempt_note
            + "。"
        )
    if not enforced_fields:
        return (
            "严格可信模式无匹配：本次规则只用到本项目自算的派生字段（"
            + "、".join(exempt_fields)
            + "），它们已按设计豁免字段级 strict 校验；"
            "因此这次空结果是筛选条件本身没有命中，与可信度模式无关 —— "
            "可以放宽条件，或切换到「包含近似可信数据」再试。"
        )
    return (
        "严格可信模式无匹配：所用源数据字段都有 strict 血缘记录，"
        "但当前没有一只股票的快照值与严格记录的值/报告期完全一致；"
        "可以切换到「包含近似可信数据」查看完整口径结果"
        + exempt_note
        + "。"
    )


class MaskedFieldError(ValueError):
    """规则引用了"当前不可信"的字段 → 拒绝执行（HTTP 409），不静默返回空集。

    继承 ValueError 是刻意的：`/api/screening/run` 之外的调用方（自选刷新、
    CLI）已有 `except ValueError` 的兜底，这样它们得到的是"带原因的失败"
    而不是 500。`/api/screening/run` 自己先捕获本类型并映射成 409。

    为什么必须拒绝而不是返回空结果：字段不可信时，空结果集会被用户读成
    "全市场没有一只股票满足条件"，这是一个**错误的事实陈述**；而 409 +
    reason 能如实说"这个字段现在不可信，原因是什么"。
    """

    def __init__(self, fields: list[str], *, reason: str = "masked_untrusted") -> None:
        self.fields = sorted(dict.fromkeys(str(field) for field in fields))
        self.reason = reason
        super().__init__(
            f"{reason}: 字段当前不可信，拒绝执行筛选（{', '.join(self.fields)}）",
        )

    def as_detail(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason,
            "masked_fields": self.fields,
            "message": (
                "以下字段当前不可信（数据质量规则判定为遮蔽状态），"
                "为避免给出错误结论，本次筛选已拒绝执行："
                + "、".join(self.fields)
                + "。请先修复数据质量告警（见 /api/data-status/summary 的 "
                "asset_health）后重试。"
            ),
        }

# 扩展指标域 indicator_ext（schema v25，2026-09-19 Phase B）。
# 三族此前完全缺失的指标：周转率族、自由现金流族、杠杆族。
# 数值由本项目自算（source='derived_calculator'），口径见 config/csmar_field_verdict.json。
# 与 indicator_snapshot 的报告期按 (stock_code, report_date) 精确对齐（实测 5,542/5,542）。
# 只暴露指标列本身；表内的 calculated_at/source/data_version 与快照同名，不在此暴露。
EXTENDED_COLUMNS: set[str] = {
    # 周转率族（倍）—— 判断「生意质量」：货是否积压、是否被客户占款
    "receivables_turnover", "inventory_turnover", "accounts_payable_turnover",
    "current_asset_turnover", "fixed_asset_turnover", "total_asset_turnover",
    "equity_turnover",
    # 营业周期（天）
    "operating_cycle_days",
    # 自由现金流族 —— 判断「利润是不是真钱」
    "depreciation_amortization", "capex", "operating_cash_flow",
    "free_cash_flow", "fcf_margin",
    # 杠杆族 —— 判断「会不会被债压垮」
    "ebit", "ebitda", "leverage_financial", "leverage_operating", "leverage_total",
    # 每股族（v26）—— 分母用「当时股数」，非当前股本
    "bps", "bps_parent", "revenue_per_share", "ocf_per_share", "retained_earnings_per_share",
    # 费用率族（v26）
    "selling_expense_ratio", "admin_expense_ratio",
    "rd_expense_ratio", "finance_expense_ratio",
    # 结构族（v26）
    "current_asset_ratio", "fixed_asset_ratio", "equity_multiplier",
    # 治理红旗（v27）—— 大股东占款越大越危险
    "shareholder_occupation",
    # 人效族（v28）—— 分母用「最接近报告期」的员工数，非当前快照
    "employee_count", "revenue_per_employee", "profit_per_employee",
    # v37：CSMAR 有价值新概念的自算落地（偿债细分/现金质量/应计/每股明细）
    "cash_ratio",
    "conservative_quick_ratio",
    "debt_to_equity",
    "tangible_net_debt_ratio",
    "ocf_to_liabilities",
    "ebitda_to_liabilities",
    "cash_content_of_revenue",
    "ocf_to_operating_profit",
    "accruals",
    "tangible_asset_per_share",
    "liability_per_share",
    "capital_reserve_per_share",
    # v38：事件类指标（稀释 / 回购 / 送转 / 业务集中度）
    "dilution_3y",
    "buyback_ratio_3y",
    "bonus_share_ratio_3y",
    "top_segment_share",
    # 时点估值族（v30）—— 市值 = 报告期当日原始收盘价 × 报告期时点股本，
    # 绕开 ops-knowledge-base D23「历史市值用当前股本」的已知缺陷。
    "report_date_close", "market_cap_at_report",
    "tobin_q", "book_to_market", "ev_ebitda",
}

# 扩展指标域中【当前报告期数据充足、可直接暴露给用户】的子集。
#
# 2026-09-19 实测（对齐全市场快照的最新报告期，5,542 只）：
#   depreciation_amortization / total_asset_turnover / equity_turnover
#   / operating_cash_flow                                   100.0%
#   capex / free_cash_flow / fcf_margin                      99.5%
#   ebit / ebitda / leverage_*                               97.4%
#   应收/存货/流动资产/应付账款周转率、营业周期                 97.0%~99.0%
#
# 历史沿革（教训记录）：本集合最初只含 11 列。另外 7 列（折旧摊销/资本支出/
# 自由现金流/FCF率/EBITDA/经营杠杆/综合杠杆）当时覆盖率为 0% —— 它们依赖
# CSMAR 的折旧摊销与资本支出，而 CSMAR 数据截止 2025-03-31。
# 若彼时直接暴露，就会变成「能选中但永远筛不出结果」的死条件
# （与 2026-09-17 扣非空洞同类）。现已由 `scripts/fetch_cashflow_supplement.py`
# 从东方财富 F10 补齐 CSMAR 截止后的 5 个报告期（2025-06-30 ~ 2026-06-30），
# 覆盖率恢复到 97%~100%，故全量开放。
#
# 维护约定（判定标准）：新增扩展列进本集合前必须满足下列之一 ——
#   (a) 最新报告期覆盖率 ≥ 90%；或
#   (b) 覆盖率较低但**缺失语义正确**：该指标对某一类合法子群体本就不成立，
#       且剩余样本量足以支撑筛选。
# 反例（绝不允许）：字段全表 NULL、或缺失源于"我们的数据没取到" ——
# 那会变成「能选中但永远筛不出结果」的死条件（2026-09-17 扣非空洞的教训）。
#
# 当前按 (b) 例外放行的 4 列（2026-09-19 实测）：
#   leverage_financial 72.3% / leverage_operating 72.8% / leverage_total 72.5%
#     —— 亏损公司（EBIT 或利润总额 ≤ 0）的杠杆没有业务含义，NULL 是正确表达；
#        留存样本 4,000+ 只，筛选完全可用。
#   ev_ebitda 82.7%
#     —— 需 EBITDA 可用（依赖折旧摊销），留存 4,585 只。
EXTENDED_SCREENING_READY: frozenset[str] = frozenset(EXTENDED_COLUMNS)

# Every normalized statement column is available to screening under its stable
# DSL-style name. SQL only ever sees the generated internal alias below.
STATEMENT_FIELDS: dict[str, set[str]] = {
    "balance": {
        "monetary_funds", "trading_financial_assets", "notes_receivable", "accounts_receivable",
        "prepayments", "other_receivables", "inventory", "contract_assets", "total_current_assets",
        "long_term_equity_investment", "fixed_assets", "construction_in_progress", "right_of_use_assets",
        # v48 补采科目（依据 S7 §11.4 / D58）：登记后筛选可引用、空值原因可归因。
        # 口径：金额单位元；`fixed_assets_gross − accumulated_depreciation − 减值准备 = fixed_assets`
        # （库内无减值准备列，所以勾稽允许残差；原值<净值的行已由 repair 脚本清空）。
        "fixed_assets_gross", "accumulated_depreciation", "other_current_assets",
        "receivables_financing", "long_term_payables", "investment_property",
        "development_expenditure",
        "intangible_assets", "goodwill", "deferred_tax_assets", "total_non_current_assets", "total_assets",
        "short_term_loans", "notes_payable", "accounts_payable", "prepayments_received",
        "contract_liabilities", "employee_benefits_payable", "taxes_payable", "total_current_liabilities",
        "long_term_loans", "bonds_payable", "lease_liabilities", "total_non_current_liabilities",
         "total_liabilities", "paid_in_capital", "capital_reserve", "surplus_reserve",
         "undistributed_profit", "minority_interest", "total_equity", "total_equity_parent",
         "core_tier1_capital_adequacy_ratio", "tier1_capital_adequacy_ratio",
         "capital_adequacy_ratio", "non_performing_loan_ratio", "provision_coverage_ratio",
         "risk_coverage_ratio",
    },
    "income": {
        "total_operating_revenue", "revenue", "total_operating_cost", "cost_of_revenue",
        "taxes_and_surcharges", "selling_expenses", "administrative_expenses", "rd_expenses",
        "financial_expenses", "interest_expense", "interest_income", "asset_impairment_loss",
        "credit_impairment_loss", "exchange_gain", "investment_income", "operating_profit",
        "non_operating_income", "non_operating_expenses", "total_profit", "income_tax", "net_profit",
        "parent_net_profit", "minority_shareholder_profit", "deducted_net_profit", "basic_eps", "diluted_eps",
    },
    "cashflow": {
        "cash_received_sales", "taxes_refunded", "other_operating_cf_in", "total_operating_cf_in",
        "cash_paid_goods", "cash_paid_employees", "cash_paid_taxes", "other_operating_cf_out",
        "total_operating_cf_out", "cf_from_operating", "cf_from_investing", "cf_from_financing",
        "exchange_rate_effect", "cf_net", "cash_beginning", "cash_ending",
    },
}
NORMALIZED_FIELDS = {f"{table}.{field}" for table, fields in STATEMENT_FIELDS.items() for field in fields}

# ─── 跨年口径断点派生字段（依据 S5 §9.2 P0-7 / P0-8，2026-11-16）──────────
# 原始列一行不改，断点归一只发生在派生层。SQL 模板的唯一真源在
# `app/core/dsl/derived_fields.py`，DSL 代码生成与这里共用同一份：
#   income.impairment_loss_positive            资产减值损失（损失为正）
#   income.credit_impairment_loss_positive     信用减值损失（损失为正）
#   balance.advance_from_customers             预收款项 + 合同负债（跨 2020 连续）
#   balance.advance_from_customers_ratio       客户占款率 =(预收+合同负债)/总资产
#
# 为什么登记进 NORMALIZED_FIELDS 而不是 EXTENDED_COLUMNS：
#   - 它们的物理来源是 balance_sheet / income_statement，与快照期严格对齐
#     （`ic.report_date = s.report_date`），不是 indicator_ext 的列；
#   - EXTENDED_COLUMNS 被 indicator_null_reason 清扫器当成"表里的物理列"
#     （`COUNT(col) FROM indicator_ext`），塞进去会让清扫器整域报错。
DERIVED_FIELDS_BY_KEY: dict[str, Any] = dict(DERIVED_FIELDS)
NORMALIZED_FIELDS |= set(DERIVED_FIELDS_BY_KEY)

# 排名指标（需要计算横截面排名的指标）
RANKABLE_INDICATORS: set[str] = {
    "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm", "dividend_yield",
    "roe", "roa", "gross_margin", "net_margin", "roic",
    "roce_5y_median", "roce_10y_median", "roce_20y_median",
    "revenue_yoy", "net_profit_yoy",
    "debt_ratio", "current_ratio", "quick_ratio",
    "payout_ratio", "dps", "consecutive_div_years",
    "cumulative_dividend_amount", "cumulative_financing_amount",
    "dividend_financing_ratio_pct",
    "total_market_cap", "latest_close",
    "ttm_dividend_yield",
    "div_yield_spread_0p25y", "div_yield_spread_0p5y", "div_yield_spread_1y",
    "div_yield_spread_2y", "div_yield_spread_3y", "div_yield_spread_5y",
    "div_yield_spread_7y", "div_yield_spread_10y", "div_yield_spread_30y",
} | NORMALIZED_FIELDS | {
    # 扩展指标域（schema v25）参与横截面排名的一组。
    # 只选 EXTENDED_SCREENING_READY 内的列，避免生成全空的排名列。
    "inventory_turnover", "receivables_turnover", "accounts_payable_turnover",
    "total_asset_turnover", "operating_cycle_days",
    "fcf_margin", "leverage_total",
    "bps", "equity_multiplier", "shareholder_occupation",
    "revenue_per_employee", "profit_per_employee",
    "tobin_q", "market_cap_at_report", "ev_ebitda",
    "cash_ratio", "debt_to_equity", "accruals", "cash_content_of_revenue",
    "dilution_3y", "buyback_ratio_3y", "top_segment_share",
}

# sw1_rank/sw1_percentile 与 industry_rank/industry_percentile 同义（均按
# csrc_l1 分区），保留纯为兼容已保存规则/历史测试；产品与 /indicators 均
# 不再暴露 sw1 后缀，新规则应使用 industry_rank/industry_percentile。
#: 条件级「排名口径」（2026-11-16 方案 C）。
#:
#: 过去每个可排名指标都要在指标下拉里铺 6 条派生字段
#: （`pe_ttm_market_rank`、`pe_ttm_industry_percentile` …），
#: 155 个指标 × 6 = **930 条**，ROCE 一个指标就占 21 条，主指标被埋住。
#: 现在改成**条件上的一个下拉**：`rank_mode` 为空表示用原始值，
#: 否则在后台把字段展开成 `{field}_{rank_mode}`，复用原有（已回归的）排名 SQL。
#:
#: PRD §11.2/§12.2/§12.4 要求的「全市场排名 / CSRC 一级·二级排名 / 百分位」
#: 能力一条不少，只是不再以 930 个扁平字段的形式塞给用户。
RANK_MODES: tuple[str, ...] = (
    "market_rank",
    "market_percentile",
    "industry_rank",
    "industry_percentile",
    "sw2_rank",
    "sw2_percentile",
)
RANK_MODE_LABELS: dict[str, str] = {
    "market_rank": "全市场排名",
    "market_percentile": "全市场分位",
    "industry_rank": "证监会一级排名",
    "industry_percentile": "证监会一级分位",
    "sw2_rank": "证监会二级排名",
    "sw2_percentile": "证监会二级分位",
}


#: 参数化字段家族（2026-11-16，`reports/143` §2 过耦合审计的落地）。
#:
#: 下列字段原本按「同一指标 + 参数后缀」铺成 N 个独立字段，属于过耦合：
#:
#:   div_yield_spread_{0p25y,0p5y,1y,2y,3y,5y,7y,10y,30y}   9 个 → 1 个入口 + 期限参数
#:   {revenue,net_profit,deducted_profit}_cagr{3,5}           6 个 → 3 个入口 + 年数参数
#:
#: 现在只在指标列表里暴露**基名**（`div_yield_spread` / `revenue_cagr` …），
#: 用户在条件/排序/结果列上选一个参数值即可。旧的带后缀字段名仍然可用
#: （向后兼容已保存规则），只是不再出现在下拉里。
#:
#: 为什么不把「统计域 40」和「均线 6」也收进来：
#: - 统计域（`*_stat_{window}y_{method}`）对应 PRD §10.7 的独立研究统计域，
#:   每条条件必须显式保存序列、股息率口径、国债期限、窗口、方法与阈值；收成下拉会丢审计元数据。
#: - 均线（`ma5/10/20/60/120/250`）的典型用法是**跨窗口比较**（MA20 上穿 MA60），
#:   收成参数后同一条件里就无法比较两条均线。
PARAM_FAMILIES: dict[str, dict[str, object]] = {
    "cagr": {
        "bases": ("revenue_cagr", "net_profit_cagr", "deducted_profit_cagr"),
        "key": "cagr_years",
        "label": "年数",
        "options": (3, 5),
        "format": "{base}{value}",
    },
    "tenor": {
        "bases": ("div_yield_spread",),
        "key": "tenor",
        "label": "国债期限",
        "options": ("0p25y", "0p5y", "1y", "2y", "3y", "5y", "7y", "10y", "30y"),
        "format": "{base}_{value}",
    },
}


def param_spec_for(field: str) -> dict[str, object] | None:
    """某**基名**字段的参数规格（供 API 下发、前端渲染参数下拉）。"""
    for kind, spec in PARAM_FAMILIES.items():
        if field in spec["bases"]:
            return {
                "kind": kind,
                "key": spec["key"],
                "label": spec["label"],
                "options": list(spec["options"]),
            }
    return None


def param_field_names() -> set[str]:
    """所有「带参数后缀」的字段名（这些不再出现在指标下拉里）。"""
    out: set[str] = set()
    for spec in PARAM_FAMILIES.values():
        for base in spec["bases"]:
            for value in spec["options"]:
                out.add(str(spec["format"]).format(base=base, value=value))
    return out


def expand_rank_modes(node: object) -> object:
    """把条件树里的 `rank_mode` 展开成字段后缀（原地返回新结构）。

    `{"field": "pe_ttm", "rank_mode": "market_rank", ...}`
        → `{"field": "pe_ttm_market_rank", ...}`

    为什么在入口做这一步而不是改 SQL：排名列的生成、NULLS LAST、
    「csrc_l1 为 NULL 时行业排名返回 NULL」（PRD §12.4）、
    「排名分母只由基础股票池决定」（PRD §12.3）这些语义都已实现并有回归；
    在入口改写字段名可以零风险复用，不会引入第二套排名实现。
    """
    if isinstance(node, list):
        return [expand_rank_modes(item) for item in node]
    if not isinstance(node, dict):
        return node
    # 递归**所有** dict/list 值，不能只认 `rules` 键：
    # `validate_rule_fields()` 拿到的是 `{"conditions": {...}, "sort": [...]}`，
    # 只递归 `rules` 会让 `conditions` 里的 rank_mode 逃过展开与校验。
    out: dict[str, object] = {
        key: expand_rank_modes(value) for key, value in node.items()
    }

    # 参数化字段（`reports/143` §2）：先展开参数，再展开排名口径，
    # 因为排名字段名是「参数展开后 + 排名后缀」（`revenue_cagr5_market_rank`）。
    for spec in PARAM_FAMILIES.values():
        key = str(spec["key"])
        value = out.pop(key, None)
        if value is None:
            continue
        field = out.get("field")
        if not isinstance(field, str) or not field:
            raise ValueError(f"{key} 只能用在带 field 的条件上")
        if field not in spec["bases"]:
            raise ValueError(
                f"{field} 不支持 {key}；只有 {', '.join(str(b) for b in spec['bases'])} 支持"
            )
        allowed = [str(v) for v in spec["options"]]
        if str(value) not in allowed:
            raise ValueError(f"{key} 取值必须是 {', '.join(allowed)}，收到 {value!r}")
        out["field"] = str(spec["format"]).format(base=field, value=value)

    # 参数化字段的基名必须带参数，否则它不是一个真实列，会落成"未知字段"。
    # 这里给出可读错误（PRD §12.2 的错误必须能指导用户怎么改）。
    _field_now = out.get("field")
    if isinstance(_field_now, str):
        for spec in PARAM_FAMILIES.values():
            if _field_now in spec["bases"]:
                raise ValueError(
                    f"{_field_now} 需要指定 {spec['key']}（可选："
                    f"{', '.join(str(v) for v in spec['options'])}）"
                )

    mode = out.pop("rank_mode", None)
    if isinstance(mode, str) and mode:
        if mode not in RANK_MODES:
            raise ValueError(f"未知排名口径：{mode}")
        field = out.get("field")
        if not isinstance(field, str) or not field:
            raise ValueError("rank_mode 只能用在带 field 的条件上")
        if any(field.endswith(f"_{m}") for m in RANK_MODES):
            raise ValueError(f"字段 {field} 已带排名后缀，不要再指定 rank_mode")
        out["field"] = f"{field}_{mode}"
    return out


def normalize_columns(columns_spec: object) -> list[str]:
    """把 `columns` 归一化成纯字段名列表。

    支持两种写法（方案 C，2026-11-16）：

        ["stock_code", "pe_ttm"]                                   # 纯字段
        ["stock_code", {"field": "pe_ttm", "rank_mode": "market_rank"}]  # 排名列

    第二种会展开成 `pe_ttm_market_rank`。结果集里的键就是展开后的字段名，
    前端按 `RANK_SUFFIX_LABELS` 渲染中文表头。

    为什么允许对象形状：结果展示列和筛选条件一样需要「排名口径」，
    但列不是布尔条件、没有 op/value，所以用 `{field, rank_mode}` 而不是复用条件结构。
    """
    if columns_spec is None:
        return []
    if not isinstance(columns_spec, list):
        raise ValueError("结果列必须是数组")
    out: list[str] = []
    for item in columns_spec:
        if isinstance(item, str):
            out.append(item)
            continue
        if not isinstance(item, dict):
            raise ValueError(f"结果列必须是字段名或对象: {item!r}")
        # 先校验 field 再展开，否则 `{"rank_mode": ...}` 会先在
        # expand_rank_modes 里报"rank_mode 只能用在带 field 的条件上"，
        # 对结果列来说语义不清。
        raw_field = item.get("field")
        if not isinstance(raw_field, str) or not raw_field:
            raise ValueError(f"结果列对象缺少 field: {item!r}")
        expanded = expand_rank_modes(dict(item))
        field = expanded.get("field") if isinstance(expanded, dict) else None
        if not isinstance(field, str) or not field:
            raise ValueError(f"结果列对象缺少 field: {item!r}")
        out.append(field)
    return out


RANK_SUFFIXES = (
    "_market_rank", "_market_percentile",
    "_industry_rank", "_industry_percentile",
    "_sw1_rank", "_sw1_percentile", "_sw2_rank", "_sw2_percentile",
)
METADATA_COLUMNS = {
    "stock_code", "name", "exchange", "listing_date", "is_st", "is_suspended",
    "total_shares", "circ_shares", "sw_level1", "sw_level2", "csrc_l1", "csrc_l2",
}
MAX_RULE_LEAVES = 100
MAX_IN_VALUES = 1_000
# P1-C修复: 结果行数上限（内存/导出保护）；超限时显式 truncated 标记
MAX_RESULT_ROWS = 5_000
MAX_POOL_LISTING_YEARS = 100
# 已发布 statement overrides 的 VALUES 上限；超过则拒绝执行而不是无限膨胀 SQL
MAX_OVERRIDE_VALUES = 10_000
# 内容指纹的**耗时预算**（毫秒）。超出就放弃缓存（直接现算）：这条缓存只省
# ~180ms 的探针，令牌本身比它贵就等于白折腾，而"算不准/算不动"更不许变成回放旧值。
#
# 为什么是 90 而不是最初设想的 20（2026-11-25 实测，正式库，每次一条新只读连接
# —— 也就是生产路径的真实形状）：
#   * 旧指纹（行数 + 最大报告期）**本来就要 ~41ms**（36.6–40.9ms 中位）；
#   * 新的**合并**指纹（行数 + 最大期 + 5 张表 `SUM(hash(...))`）**~39ms**
#     （同一条连接上重复跑 ~28ms，单看摘要部分热连接 ~7.6ms）。
# 也就是说：20ms 是"热连接上的摘要本身"，而正式库上任何一条单语句指纹（**包括
# 改造前的旧版**）都跑不进 20ms —— 冷页 I/O 与库大小是绑死的，不是摘要带来的。
# 把预算定在 20ms 的后果是"缓存永远不命中"，等于用 180ms 的探针换 39ms 的白算。
# 于是按**收益的一半**定预算：超过 90ms（≈180ms/2）才算"摘要真的变贵了"→ 不缓存。
MAX_CONTENT_FINGERPRINT_MS = 90.0
#: 进程内**第一个**内容指纹会额外付一次"查询规划 + 绑定 `hash()`"的一次性成本
#: （测试库实测 29–46ms，与库大小无关）。它不属于"这条库的内容摘要太贵"，所以首个
#: 指纹只记账不否决；之后按 `MAX_CONTENT_FINGERPRINT_MS` 严格执行。
_CONTENT_FINGERPRINT_WARMED = False

# ─── 历史研究统计字段（P4，依据 S1 §5/§6）────────────────────────────
# 已发布统计域字段：<metric>_stat_<window>y_<method>；筛选引擎 join 最新版本。
STAT_METRICS: tuple[str, ...] = ("pe_ttm", "pb_mrq", "ttm_dividend_yield", "spread_10y")
STAT_WINDOWS: tuple[int, ...] = (1, 3, 5, 10, 99)
STAT_METHODS: tuple[str, ...] = ("percentile", "zscore")
STAT_FIELDS: frozenset[str] = frozenset(
    f"{metric}_stat_{window}y_{method}"
    for metric in STAT_METRICS
    for window in STAT_WINDOWS
    for method in STAT_METHODS
)

_STAT_FIELD_RE = re.compile(
    r"^(?P<metric>pe_ttm|pb_mrq|ttm_dividend_yield|spread_10y)_stat_(?P<window>\d+)y_(?P<method>percentile|zscore)$"
)


class ScreeningEngine:
    """筛选引擎

    接收规则 JSON，生成并执行 DuckDB SQL，返回筛选结果。
    """

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
    ) -> None:
        if duck is None and paths is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if duck is None and paths is None:
            raise PathIsolationError("ScreeningEngine requires a DuckDB store or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path:
                raise PathIsolationError("ScreeningEngine store does not match injected paths")

        assert duck is not None
        self.duck = duck
        self.sqlite = sqlite
        self._custom_fields: set[str] = set()
        # 已发布 DSL 指标内部引用过的标准化字段（用于把"口径断点原始列"的
        # 警告一并冒泡 —— 用户看到的是指标名，看不到它内部用了哪一列）。
        self._compiled_field_refs: set[str] = set()
        self._mixed_report_date_codes: list[str] = []
        #: 当前读作用域（一次 `run()` 内多条探针共用一条只读连接）。
        #: `None` = 不在作用域里 → 每条查询各自 open-per-query（与改造前一致）。
        self._reader: _BatchedReader | None = None
        #: 本次运行"有没有已发布 statement 覆写"（`run()` 里与黑名单同一条 SQLite
        #: 连接一次读出）；`None` = 还没探过 → `_published_statement_overrides()`
        #: 自己探一次（直调本方法的测试路径保持原样）。
        self._published_overrides_present: bool | None = None
        #: 用户黑名单代码（`app/core/blacklist.py`）；在 `run()` 里从 SQLite 重读，
        #: 不在 `__init__` 里缓存——用户随时可能增删，缓存会造出「刚移出却还看不见」。
        self._blacklist_codes: list[str] = []

    @contextlib.contextmanager
    def _read_scope(self) -> Iterator[_BatchedReader | None]:
        """开一条只读连接，作用域内的探针共用它（一次 `run()` 少开 4-5 条）。

        异常/退出时立刻还连接；`DuckDBReadLockedError` 照旧从 `read_connection()`
        冒出去（`/run` 的 `read_cache` 回退与快照兜底逻辑一行都不用改）。

        **退化分支**：注入的 store 可能是只有 `read_query()` 的鸭子类型
        （`screening_snapshot.SnapshotStore` 就是这样，它读的是物化快照库）。
        没有 `read_connection()` 就**不共用连接**，域内每条查询照旧各开一条 ——
        语义与改造前逐字一致，只是不省那点开销。绝不允许因为"想快"而换一条
        读不到数据的路径。
        """
        previous = self._reader
        # 只对**真** `DuckDBStore` 复用连接：任何鸭子类型封装（物化快照库
        # `SnapshotStore`、测试里的写锁桩 `LockedDuck`）都自带自己的读语义
        # （快照库读的是另一个文件；锁桩故意让"开连接"也失败），
        # 绕过它们唯一暴露的 `read_query()` 去另开连接等于绕过它们的存在理由。
        open_connection = (
            getattr(self.duck, "read_connection", None)
            if isinstance(self.duck, DuckDBStore) else None
        )
        if open_connection is None:
            self._reader = None
            try:
                yield None
            finally:
                self._reader = previous
            return
        # 惰性：`_read_scope()` 在 `run()` 早期进入，而"参数全显式给值"的调用
        # （上限校验失败、规则非法等）**一次库查询都不该发**。急开连接会把
        # 本该是 `ValueError` 的参数校验变成 `FileNotFoundError`（2026-11-21 实测）。
        reader = _BatchedReader(opener=open_connection())
        self._reader = reader
        try:
            yield reader
        finally:
            self._reader = previous
            reader.close()

    def _read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        """当前读作用域内走共用连接；域外退回 store 的 open-per-query。"""
        reader = self._reader
        if reader is not None:
            return reader.read_query(sql, params)
        return self.duck.read_query(sql, params)

    def run(
        self,
        rule: dict[str, Any],
        include_st: bool | None = None,
        include_suspended: bool | None = None,
        min_listing_years: int | None = None,
        strict_only: bool = False,
        locked_indicators: dict[str, dict[str, Any]] | None = None,
        masked_fields: Any = None,
    ) -> dict[str, Any]:
        """执行筛选

        Args:
            rule: 规则 JSON (conditions + sort + columns)
            include_st: 是否包含 ST/*ST 股票；None = 取
                `config.screening.default_exclude_st`（缺省 True → 不含 ST）
            include_suspended: 是否包含停牌股票；None = 取
                `config.screening.default_exclude_suspended`（缺省 True → 不含停牌）
            min_listing_years: 最低上市年限；None = 取
                `config.screening.default_min_listing_years`（缺省 1 年）
            masked_fields: "当前不可信"的字段清单（字段级遮蔽）。传 None 时
                由 `asset_health.masked_fields_for_database()` 现算（2 条聚合
                查询）；命中即抛 :class:`MaskedFieldError`（HTTP 409），
                **不静默返回空集** —— 空集会被读成"全市场没有符合的股票"。

        Returns:
            {
                "results": [...],
                "total": int,
                "execution_time_ms": float,
                "base_pool_size": int,
                "data_date": str,
                "warnings": [...],   # 例如 field_deprecated:dividend_yield
            }
        """
        start_time = time.monotonic()

        # 基础池默认值的唯一真源是配置（`screening.default_*`）；调用方不传
        # 就落到配置值，传了就尊重调用方 —— 绝不静默覆盖用户的显式选择。
        if include_st is None or include_suspended is None or min_listing_years is None:
            pool_defaults = screening_defaults.base_pool_defaults()
            if include_st is None:
                include_st = not pool_defaults["exclude_st"]
            if include_suspended is None:
                include_suspended = not pool_defaults["exclude_suspended"]
            if min_listing_years is None:
                min_listing_years = int(pool_defaults["min_listing_years"])

        # min_listing_years 只参与 INTERVAL 字面量拼接，必须先做整数边界校验
        # （前端已传 int，这里兜底防负值/超大值/非整数输入）。
        if (
            not isinstance(min_listing_years, int)
            or isinstance(min_listing_years, bool)
            or not 0 <= min_listing_years <= MAX_POOL_LISTING_YEARS
        ):
            raise ValueError(
                f"min_listing_years 必须在 0-{MAX_POOL_LISTING_YEARS} 之间"
            )

        # ── 探针批量化（2026-11-21）：以下整段共用**一条**只读连接。
        # 正式库 open-per-query 每条连接 ~80ms，一次筛选原本要开 6-8 条；
        # 这里把「池元数据完整性 / 混合报告期剔除 / strict 派生豁免 / 覆盖值
        # 配对」四条互不依赖的探针挤进同一批。连接只覆盖这一步，退出即关
        # （绝不长期持有：会挡住自动更新子进程打开库文件）。
        with self._read_scope():
            self._require_known_pool_status(
                include_st,
                include_suspended,
                min_listing_years,
            )

            # 1. 解析规则
            #    方案 C（2026-11-16）：条件的 `rank_mode` 在此展开成排名字段后缀，
            #    之后全流程沿用既有的排名 SQL（无需第二套实现）。
            conditions = expand_rank_modes(rule.get("conditions", {}))
            sort_spec = expand_rank_modes(rule.get("sort", []))
            # 结果列支持 `{field, rank_mode}` 对象写法（方案 C 的第三块）
            columns_spec = normalize_columns(rule.get("columns", []))
            if not isinstance(sort_spec, list) or any(
                not isinstance(item, dict) for item in sort_spec
            ):
                raise ValueError("排序项必须是对象")
            # 条件数量上限（`screening.max_conditions`，缺省 20）。此前只有
            # `_build_where` 里一个 100 条的 SQL 保护上限，配置项无人读取。
            self._enforce_condition_budget(conditions)

            self._compiled_field_refs = set()
            # 每次运行的 SQLite 探针结果都要重读（用户随时可能加/删覆写）。
            self._published_overrides_present = None
            compiled_indicators = self._compile_published_indicators(locked_indicators or {})
            self._custom_fields = set(compiled_indicators)

            # 2. 提取条件中引用的排名指标
            strict_referenced_fields = self._extract_fields(conditions)
            strict_referenced_fields.update(item.get("field", "") for item in sort_spec)
            referenced_fields = strict_referenced_fields | set(columns_spec)
            rank_fields = {
                base for field in referenced_fields
                if (base := self._rank_base(field)) is not None
            }
            strict_fields = {
                base
                for field in referenced_fields
                for base in (field, self._rank_base(field))
                if base in SNAPSHOT_COLUMNS or base in NORMALIZED_FIELDS or base in self._custom_fields
            }

            # 2.1 字段级遮蔽（依据 S3 §2.3 J9）：字段当前不可信 → 拒绝执行。
            #     检查 rank 基名（`free_cash_flow_market_rank` 引用的是
            #     `free_cash_flow`），否则遮蔽会被排名列绕过。
            self._reject_masked_fields(referenced_fields, masked_fields)

            # 2.2 废弃字段（依据 S3 §2.6 J5）：仍可执行，但结果必须自带警告，
            #     否则用户不知道自己在用一个会随"今天"漂移的旧口径列。
            # 2.2b 跨年口径断点原始列（依据 S5 §9.2 P0-7 / P0-8）：同样仍可执行，
            #     但必须提示"跨断点不可直接比较"并指向归一化派生字段。
            deprecation_warnings = deprecated_field_warnings(referenced_fields)
            # 已发布 DSL 指标内部的原始列引用也要冒泡（指标名本身看不出用了哪一列）
            break_warnings = series_break_field_warnings(
                referenced_fields | self._compiled_field_refs
            )

            # 2026-08-27：不再因个别股票存在快照/完整期不一致而阻断全市场筛选。
            # 改为把这类股票从本次基础池中排除，其他正常股票照常筛选/导出。
            self._mixed_report_date_codes = self._load_mixed_report_date_codes()
            # 2026-11-22：黑名单股票同样从基础池排除。必须加在池里而不是前端过滤——
            # 排名/分位是跑在 base_pool 之上的窗口函数，池里剔掉才能让排名分母与
            # 页面可见行数自洽（见 app/core/blacklist.py 模块 docstring「与排名口径的关系」）。
            # SQLite 侧同样是 open-per-query（~27-47ms/条）：黑名单与"有没有
            # 已发布覆写"这两条互不依赖的小查询共用**一条**连接读出来。
            # 同理：store 没有 `connection()`（鸭子类型）就退回每次单开一条。
            open_sqlite = (
                getattr(self.sqlite, "connection", None)
                if self.sqlite is not None else None
            )
            if open_sqlite is not None:
                with open_sqlite() as sqlite_conn:
                    sqlite_batch = _BatchedSqlite(sqlite_conn)
                    self._blacklist_codes = blacklisted_codes(sqlite_batch)
                    self._published_overrides_present = (
                        self._probe_published_statement_overrides(sqlite_batch)
                    )
            else:
                self._blacklist_codes = blacklisted_codes(self.sqlite)
                if self.sqlite is not None:
                    self._published_overrides_present = (
                        self._probe_published_statement_overrides(self.sqlite)
                    )

            # 2.4 strict 模式：派生字段豁免（语义与 fail-closed 判据见模块常量
            #     `DERIVED_AUDIT_SOURCE` 的注释）。豁免只发生在**派生**字段上；
            #     源数据字段一个都不放过 —— 那是 strict 模式唯一的安全底线。
            strict_exempt: set[str] = (
                self._strict_exempt_derived_fields(strict_fields) if strict_only else set()
            )
            strict_enforced: set[str] = set(strict_fields) - strict_exempt
            if strict_only:
                # 派生字段的输入原始列仍可查值 → strict 检查下沉一层继续做。
                for field in sorted(strict_exempt):
                    strict_enforced.update(derived_input_fields(field))

            # 3. 构建 SQL
            sql, params = self._build_sql(
                conditions=conditions,
                sort_spec=sort_spec,
                columns_spec=columns_spec,
                include_st=include_st,
                include_suspended=include_suspended,
                min_listing_years=min_listing_years,
                rank_fields=rank_fields,
                strict_fields=strict_enforced if strict_only else set(),
                compiled_indicators=compiled_indicators,
            )

        # 4. 执行 —— 主查询与两条后置探针（基础池 / 数据日期）共用一条只读连接：
        #    open-per-query 下这两条探针各要 ~80ms 开连接（`_BatchedReader` 注释）。
        #    strict 血缘可用性探针同样塞进这一批（它不依赖结果，只依赖字段清单）。
        audit_name_of: dict[str, str] = {}
        audit_rows: list[dict] = []
        with self._read_scope():
            results = self._read_query(sql, params)
            base_pool_size = self._get_base_pool_size(
                include_st, include_suspended, min_listing_years
            )
            data_date = self._get_latest_data_date()
            if strict_only and strict_fields:
                # 审计表里 `field_name` 是裸列名（`income.revenue` → `revenue`），
                # 旧实现拿点号全名去查 → 归一化字段永远"查不到"，误报成缺血缘。
                audit_name_of = {
                    self._audit_field_name(field): field
                    for field in sorted(strict_enforced)
                }
                if audit_name_of:
                    names = sorted(audit_name_of)
                    placeholders = ", ".join(["?"] * len(names))
                    audit_rows = self._read_query(
                        "SELECT field_name, COUNT(*) AS n FROM source_audit "
                        f"WHERE confidence = 'strict' AND field_name IN ({placeholders}) "
                        "GROUP BY field_name",
                        names,
                    )
        # C14修复(报告41): 复用单次窗口计数 COUNT(*) OVER () 替代二次计数查询，
        # total 语义不变（真实匹配数，分母跟随基础池）；行数超上限时显式 truncated。
        total_matched = int(results[0]["_vd_total"]) if results else 0
        for row in results:
            row.pop("_vd_total", None)
        truncated = total_matched > len(results)
        execution_time = (time.monotonic() - start_time) * 1000

        # 2026-08-14 红队 P2-4：strict_only 无结果时给出可行动反馈。
        # 2026-11-23 修正（用户实测）：此前把**派生字段**也列进"没有 strict 血缘、
        # 条件永远无法满足"，既吓人又不准确 —— 派生字段按设计没有抓取血缘，早已
        # 豁免；这里只统计**真正卡住结果**的源数据字段，并把豁免情况如实说明。
        strict_availability: dict[str, int] = {}
        strict_mode_warning: str | None = None
        if strict_only and strict_fields:
            if audit_name_of:
                strict_availability = {
                    audit_name_of[r["field_name"]]: int(r["n"])
                    for r in audit_rows
                    if r["field_name"] in audit_name_of
                }
            enforced_fields = sorted(strict_enforced)
            missing = [f for f in enforced_fields if f not in strict_availability]
            strict_mode_warning = build_strict_mode_warning(
                total_matched=total_matched,
                missing_fields=missing,
                exempt_fields=sorted(strict_exempt),
                enforced_fields=enforced_fields,
            )

        logger.info(
            f"筛选完成: {len(results)} 条结果"
            + (f"（共 {total_matched} 条，已截断）" if truncated else "")
            + f", 基础池 {base_pool_size} 只, 耗时 {execution_time:.1f}ms"
        )

        return {
            "results": results,
            "total": total_matched,
            "truncated": truncated,
            "execution_time_ms": round(execution_time, 1),
            "base_pool_size": base_pool_size,
            "data_date": data_date,
            "strict_only": strict_only,
            # 引用到的可审计字段全集（含被豁免的派生字段），保持既有语义不变。
            "strict_fields": sorted(strict_fields),
            # 真正参与 strict 谓词的字段（源数据字段 + 派生字段下沉后的输入列）。
            # 非 strict 模式不拼谓词 → 恒为空，和 SQL 里发生的事一致。
            "strict_enforced_fields": sorted(strict_enforced) if strict_only else [],
            # 按设计豁免字段级抓取血缘的派生字段：如实披露，不让 strict 模式
            # 假装对"自己算出来的值"有抓取级保证。
            "strict_exempt_fields": sorted(strict_exempt),
            "strict_availability": strict_availability,
            "strict_mode_warning": strict_mode_warning,
            "locked_indicators": locked_indicators or {},
            # 废弃字段警告（例如 dividend_yield → ttm_dividend_yield）
            # + 跨年口径断点警告（例如 income.asset_impairment_loss 跨 2018）
            "warnings": deprecation_warnings + break_warnings,
        }

    def _build_sql(
        self,
        conditions: dict,
        sort_spec: list[dict],
        columns_spec: list[str],
        include_st: bool,
        include_suspended: bool,
        min_listing_years: int,
        rank_fields: set[str],
        strict_fields: set[str],
        compiled_indicators: dict[str, str],
    ) -> tuple[str, list[Any]]:
        """构建完整的筛选 SQL（含单次窗口计数 _vd_total）

        策略 (审查问题3修订):
        - 基础池从 stock_meta 出发（不依赖 indicator_snapshot 的 report_date）
        - per-stock 最新快照用 LEFT JOIN LATERAL
        - 排名窗口函数作用于过滤后的基础池
        - 用户条件在排名之后应用
        """
        params: list[Any] = []

        # ─── 阶段1: 基础池 + per-stock 最新快照 ──────────────────
        sql_parts: list[str] = []

        # 基础池过滤条件（pool_params 只含 mixed_report_date / 黑名单 NOT IN 的值）
        pool_conditions: list[str] = ["m.is_listed IS TRUE"]
        pool_params: list[Any] = []
        if not include_st:
            pool_conditions.append("m.is_st = false")
        if not include_suspended:
            pool_conditions.append("m.is_suspended = false")
        if min_listing_years > 0:
            pool_conditions.append(
                f"m.listing_date <= CURRENT_DATE - INTERVAL '{min_listing_years}' YEAR"
            )
        if self._mixed_report_date_codes:
            excluded_slots = ", ".join("?" for _ in self._mixed_report_date_codes)
            pool_conditions.append(f"m.stock_code NOT IN ({excluded_slots})")
            pool_params.extend(self._mixed_report_date_codes)
        # 黑名单：SQL 片段与绑定参数的唯一生成点在 app/core/blacklist.py。
        # 顺序必须保持「mixed_report_date 之后」，与下方 params 组装顺序一致。
        blacklist_pred, blacklist_params = exclusion_condition(
            "m.stock_code", self._blacklist_codes
        )
        if blacklist_pred:
            pool_conditions.append(blacklist_pred)
            pool_params.extend(blacklist_params)

        pool_where = " AND ".join(pool_conditions) if pool_conditions else "1=1"

        # Statement values are pinned to the snapshot period. Selecting each
        # table's independent latest row would create fabricated cross-period ratios.
        aliases = {"balance": "bs", "income": "ic", "cashflow": "cf"}
        overrides = self._published_statement_overrides(pool_where, pool_params)
        normalized_selects = [
            self._override_aware_field_sql(table, field, aliases[table], bool(overrides))
            for table, fields in STATEMENT_FIELDS.items()
            for field in sorted(fields)
        ]
        # 跨年口径断点派生列（P0-7 / P0-8）：与快照报告期严格对齐，断点判定用
        # 快照行的 report_date（整行都被钉在该期，避免"值取当期、断点判到期"）。
        derived_selects = [
            self._derived_field_select(key, derived, aliases[derived.table], bool(overrides))
            for key, derived in sorted(DERIVED_FIELDS_BY_KEY.items())
        ]
        # 扩展指标域列：与快照报告期严格对齐（ex.report_date = s.report_date），
        # 不取「各自最近一期」，避免与主链快照产生跨期拼接。
        extended_selects = [f"ex.{column}" for column in sorted(EXTENDED_COLUMNS)]
        if overrides:
            placeholders = ", ".join("(?, ?, ?, ?)" for _ in overrides)
            sql_parts.append(
                "WITH overrides(stock_code, report_date, field_name, override_value) AS "
                f"(VALUES {placeholders}),"
            )
            params.extend(value for row in overrides for value in row)
        else:
            sql_parts.append("WITH")
        # 占位符在最终 SQL 文本中的出现顺序是：
        # overrides VALUES → base_pool 的 mixed_report_date NOT IN → 用户条件 WHERE。
        # params 必须严格按同一顺序组装，否则 DuckDB 参数绑定会错位。
        params.extend(pool_params)
        sql_parts.append(f"""
base_pool AS (
    SELECT
        m.stock_code, m.name, m.pinyin, m.exchange, m.sw_level1, m.sw_level2,
        m.csrc_l1, m.csrc_l2,
        m.is_st, m.is_suspended, m.listing_date,
        m.total_shares, m.circ_shares,
        s.*, {', '.join(extended_selects)}, {', '.join(normalized_selects)},
        {', '.join(derived_selects)}
    FROM stock_meta m
    LEFT JOIN LATERAL (
        SELECT * FROM indicator_snapshot s
        WHERE s.stock_code = m.stock_code ORDER BY s.report_date DESC LIMIT 1
    ) s ON true
    LEFT JOIN LATERAL (
        SELECT * FROM balance_sheet bs
        WHERE bs.stock_code = m.stock_code AND bs.report_date = s.report_date
    ) bs ON true
    LEFT JOIN LATERAL (
        SELECT * FROM income_statement ic
        WHERE ic.stock_code = m.stock_code AND ic.report_date = s.report_date
    ) ic ON true
    LEFT JOIN LATERAL (
        SELECT * FROM cash_flow cf
        WHERE cf.stock_code = m.stock_code AND cf.report_date = s.report_date
    ) cf ON true
    LEFT JOIN LATERAL (
        SELECT * FROM indicator_ext ex
        WHERE ex.stock_code = m.stock_code AND ex.report_date = s.report_date
    ) ex ON true
    WHERE {pool_where}
)""")

        if compiled_indicators:
            sql_parts.append(
                ", computed AS (SELECT base_pool.*, "
                + ", ".join(
                    f"{expression} AS {quote_identifier(name)}"
                    for name, expression in compiled_indicators.items()
                )
                + " FROM base_pool)"
            )
            rank_source = "computed"
        else:
            rank_source = "base_pool"

        # ─── 阶段2: 横截面排名 ──────────────────────────────────
        # P1-21修复: NULLS LAST 避免NULL值获得rank=1
        # P1-22修复: csrc_l1为NULL时行业排名返回NULL (PRD §12.4)
        # PRD §24: 行业口径切换为 CSRC（证监会），申万列不再消费
        rank_cols: list[str] = []
        if rank_fields:
            for field in sorted(rank_fields):
                # 全市场排名 (NULLS LAST)
                sql_field = self._field_sql_name(field)
                rank_cols.append(
                    f"CASE WHEN {sql_field} IS NULL THEN NULL ELSE "
                    f"RANK() OVER (ORDER BY {sql_field} NULLS LAST) END AS {self._field_sql_name(field + '_market_rank')}"
                )
                rank_cols.append(
                    f"CASE WHEN {sql_field} IS NULL THEN NULL ELSE "
                    f"PERCENT_RANK() OVER (ORDER BY {sql_field} NULLS LAST) END AS {self._field_sql_name(field + '_market_percentile')}"
                )
                # CSRC 一级排名 (csrc_l1为NULL时返回NULL, PRD §12.4)
                rank_cols.append(
                    f"CASE WHEN csrc_l1 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE RANK() OVER (PARTITION BY csrc_l1 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_industry_rank')}"
                )
                rank_cols.append(
                    f"CASE WHEN csrc_l1 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE PERCENT_RANK() OVER (PARTITION BY csrc_l1 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_industry_percentile')}"
                )
                rank_cols.append(
                    f"CASE WHEN csrc_l1 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE RANK() OVER (PARTITION BY csrc_l1 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_sw1_rank')}"
                )
                rank_cols.append(
                    f"CASE WHEN csrc_l1 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE PERCENT_RANK() OVER (PARTITION BY csrc_l1 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_sw1_percentile')}"
                )
                rank_cols.append(
                    f"CASE WHEN csrc_l1 IS NULL OR csrc_l2 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE RANK() OVER (PARTITION BY csrc_l1, csrc_l2 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_sw2_rank')}"
                )
                rank_cols.append(
                    f"CASE WHEN csrc_l1 IS NULL OR csrc_l2 IS NULL OR {sql_field} IS NULL THEN NULL "
                    f"ELSE PERCENT_RANK() OVER (PARTITION BY csrc_l1, csrc_l2 ORDER BY {sql_field} NULLS LAST) END "
                    f"AS {self._field_sql_name(field + '_sw2_percentile')}"
                )

        if rank_cols:
            sql_parts.append(f"""
, ranked AS (
    SELECT {rank_source}.*,
        {', '.join(rank_cols)}
    FROM {rank_source}
)""")
            source_table = "ranked"
        else:
            source_table = rank_source

        # ─── 阶段2.5: 历史研究统计字段 join（P4）──────────────────
        # 只 join 已发布统计域的最新版本；不触碰快照主路径（依据 S1 §6）。
        stat_fields_used = self._collect_stat_fields(conditions, sort_spec, columns_spec, rank_fields)
        if stat_fields_used:
            stat_joins: list[str] = []
            stat_selects: list[str] = []
            for field in sorted(stat_fields_used):
                parsed = _STAT_FIELD_RE.match(field)
                if parsed is None:
                    continue
                alias = f"rs_{parsed.group('metric')}_{parsed.group('window')}_{parsed.group('method')}"
                stat_joins.append(
                    f"""LEFT JOIN research_statistics {alias}
                        ON {alias}.stock_code = {source_table}.stock_code
                       AND {alias}.metric = '{parsed.group('metric')}'
                       AND {alias}.window_years = {int(parsed.group('window'))}
                       AND {alias}.method = '{parsed.group('method')}'
                       AND {alias}.version = (
                           SELECT MAX(version) FROM research_statistics rv
                           WHERE rv.stock_code = {source_table}.stock_code
                             AND rv.metric = '{parsed.group('metric')}'
                             AND rv.window_years = {int(parsed.group('window'))}
                             AND rv.method = '{parsed.group('method')}'
                       )"""
                )
                stat_selects.append(f"{alias}.value AS {self._field_sql_name(field)}")
            if stat_selects:
                sql_parts.append(f"""
, stat_join AS (
    SELECT {source_table}.*,
        {', '.join(stat_selects)}
    FROM {source_table}
    {''.join(stat_joins)}
)""")
                source_table = "stat_join"

        # ─── 阶段3: 条件过滤 ────────────────────────────────────
        where_clause, where_params = self._build_where(conditions)
        params.extend(where_params)
        strict_clause = self._build_strict_clause(strict_fields, source_table)
        if strict_clause:
            where_clause = f"({where_clause}) AND ({strict_clause})"

        # ─── 排序 ────────────────────────────────────────────────
        order_clause = self._build_order(sort_spec)

        # ─── 列选择 ──────────────────────────────────────────────
        select_cols = self._build_select(columns_spec)

        sql_parts.append(f"""
SELECT {select_cols}, COUNT(*) OVER () AS _vd_total
FROM {source_table}
WHERE {where_clause if where_clause else '1=1'}
{order_clause}
LIMIT {MAX_RESULT_ROWS}
""")

        return "".join(sql_parts), params

    def _build_where(self, node: dict, level: int = 1) -> tuple[str, list[Any]]:
        """递归构建 WHERE 子句

        支持嵌套 AND/OR (最多3层, PRD SC3)。level 从 1 开始：
        根组=第1层，最多到第3层，与前端 ScreeningRuleEditor max-depth=3 一致。
        """
        if level > 3:
            raise ValueError("规则嵌套超过3层限制 (PRD §12.2)")

        if not isinstance(node, dict):
            raise ValueError("规则节点必须是对象")
        logic = node.get("logic", "AND").upper()
        if logic not in {"AND", "OR"}:
            raise ValueError("规则逻辑只能是 AND 或 OR")
        rules = node.get("rules", [])
        if not isinstance(rules, list):
            raise ValueError("规则 rules 必须是数组")

        if not rules:
            return "1=1", []
        if self._count_rule_leaves(node) > MAX_RULE_LEAVES:
            raise ValueError(f"筛选条件超过{MAX_RULE_LEAVES}项限制")

        parts: list[str] = []
        params: list[Any] = []

        for rule in rules:
            if "logic" in rule:
                # 嵌套节点
                sub_where, sub_params = self._build_where(rule, level + 1)
                if sub_where != "1=1":
                    parts.append(f"({sub_where})")
                    params.extend(sub_params)
            else:
                # 叶子条件
                cond_sql, cond_params = self._build_condition(rule)
                parts.append(cond_sql)
                params.extend(cond_params)

        if not parts:
            raise ValueError("规则组不能为空")

        joiner = f" {logic} "
        return joiner.join(parts), params

    def _build_condition(self, cond: dict) -> tuple[str, list[Any]]:
        """构建单个条件"""
        if not isinstance(cond, dict):
            raise ValueError("筛选条件必须是对象")
        field = cond.get("field", "")
        op = cond.get("op", "")
        value = cond.get("value")
        right_field = cond.get("right_field")

        # 验证字段名安全（防注入）
        if not self._is_known_field(field):
            # 2026-11-16：区分「名字拼错」与「该字段不支持排名口径」。
            # 后者典型是 `revenue_cagr3_market_rank`——基名存在但不在可排名清单，
            # 只报"未知筛选字段"会让用户以为自己拼错了。
            base = unrankable_rank_base(field, self._custom_fields)
            if base is not None:
                raise ValueError(
                    f"该字段不支持排名口径（不在可排名清单里）: {field}（基名 {base}）"
                )
            raise ValueError(f"未知筛选字段: {field}")
        if right_field is not None:
            if op in {"between", "in", "is_null", "is_not_null"}:
                raise ValueError(f"{op} 条件不能比较另一字段")
            if not isinstance(right_field, str) or not self._is_known_field(right_field):
                raise ValueError(f"未知比较字段: {right_field}")
            if not self._compatible_fields(field, right_field):
                raise ValueError(f"不兼容的字段比较: {field} 与 {right_field}")

        sql_op = OP_MAP.get(op)
        if not sql_op:
            raise ValueError(f"未知筛选操作符: {op}")

        if op == "is_null":
            return f"{self._field_sql_name(field)} IS NULL", []
        elif op == "is_not_null":
            return f"{self._field_sql_name(field)} IS NOT NULL", []
        elif op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("between 条件必须提供两个值")
            return f"{self._field_sql_name(field)} BETWEEN ? AND ?", [value[0], value[1]]
        elif op == "in":
            if not isinstance(value, list) or not value:
                raise ValueError("in 条件必须提供非空数组")
            if len(value) > MAX_IN_VALUES:
                raise ValueError(f"in 条件最多允许 {MAX_IN_VALUES} 个值")
            placeholders = ", ".join(["?"] * len(value))
            return f"{self._field_sql_name(field)} IN ({placeholders})", value
        else:
            if right_field is not None:
                return f"{self._field_sql_name(field)} {sql_op} {self._field_sql_name(right_field)}", []
            if value is None:
                raise ValueError(f"{op} 条件必须提供值")
            return f"{self._field_sql_name(field)} {sql_op} ?", [value]

    @staticmethod
    def _count_rule_leaves(node: dict) -> int:
        return sum(
            ScreeningEngine._count_rule_leaves(child) if isinstance(child, dict) and "logic" in child else 1
            for child in node.get("rules", [])
        )

    @staticmethod
    def _enforce_condition_budget(conditions: Any) -> None:
        """拒绝条件数超过 `screening.max_conditions` 的规则（缺省 20）。

        为什么要有上限：条件树会直接展开成 SQL，前端编辑器把上限写成 20
        （`ScreeningRuleEditor` 的 max-conditions），后端却完全没有对应校验 ——
        绕过前端（直接 POST /api/screening/run 或保存规则）就能塞进上百条，
        单条规则的编译/执行成本无界。这里在**执行前**用明确错误挡住，而不是
        等 SQL 变慢。

        与 `MAX_RULE_LEAVES=100` 的关系：那个是 SQL 生成/内存的硬保护线，
        本项是产品口径（可配置）的上限；两者取更严者生效。
        """
        if not isinstance(conditions, dict):
            # 结构错误交给 `_build_where` 报"规则节点必须是对象"
            return
        limit = screening_defaults.max_conditions()
        leaves = ScreeningEngine._count_rule_leaves(conditions)
        if leaves > limit:
            raise ValueError(
                f"筛选条件 {leaves} 项超过上限 {limit} 项"
                f"（配置项 screening.max_conditions）"
            )

    def _build_order(self, sort_spec: list[dict]) -> str:
        """构建 ORDER BY。

        无排序或排序全部无效时回退到 stock_code 稳定排序，保证 LIMIT 5000
        截断结果可复现；有排序时追加 stock_code 作为最终 tie-breaker。
        """
        # P0#13修复: 白名单验证排序字段, 防止 SQL 注入
        # 允许的排序字段: 字母/数字/下划线, 可选表别名前缀 (如 s.stock_code)
        safe_field_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?$")
        if not sort_spec:
            return "ORDER BY stock_code"
        if not isinstance(sort_spec, list):
            raise ValueError("排序项必须是对象")
        parts: list[str] = []
        for s in sort_spec:
            if not isinstance(s, dict):
                raise ValueError("排序项必须是对象")
            field = s.get("field", "")
            if not isinstance(field, str):
                raise ValueError("排序字段必须是字符串")
            direction = s.get("direction", "asc")
            if not isinstance(direction, str):
                direction = "asc"
            direction = direction.upper()
            if direction not in ("ASC", "DESC"):
                direction = "ASC"
            if field and safe_field_pattern.match(field) and self._is_known_field(field):
                parts.append(f"{self._field_sql_name(field)} {direction}")
        if not parts:
            return "ORDER BY stock_code"
        if "stock_code" not in {part.split()[0] for part in parts}:
            parts.append("stock_code ASC")
        return "ORDER BY " + ", ".join(parts)

    def _build_select(self, columns_spec: list[str]) -> str:
        """构建 SELECT 列。

        未知/不安全的列不再静默丢弃：直接 ValueError，由 /run 映射为 400，
        避免用户看到缺列但不知道原因的结果。
        """
        # P0#13修复: 白名单验证列名, 防止 SQL 注入。
        # 2026-08-14 红队 P3：移除死代码——正则里的 `AS 别名` 分支因
        # _is_known_field 校验永不可能通过（别名不是已知字段名），
        # 且未使用的 rank_fields 参数一并删除。
        safe_col_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
        if not columns_spec:
            # 默认列
            cols = [
                "stock_code", "name", "exchange", "csrc_l1",
                "latest_close", "pe_ttm", "pb_mrq", "roe",
                "gross_margin", "net_margin", "debt_ratio",
            ]
        else:
            rejected: list[str] = []
            cols: list[str] = []
            for raw in columns_spec:
                if not isinstance(raw, str):
                    rejected.append(str(raw))
                    continue
                column = raw.strip()
                if not safe_col_pattern.match(column) or not self._is_known_field(column):
                    rejected.append(raw)
                    continue
                if column not in cols:
                    cols.append(column)
            if rejected:
                # 2026-11-16：区分「字段不存在」与「该字段不支持排名口径」。
                # 后者典型是 `revenue_cagr3_market_rank` —— 基名真实存在但不在
                # 可排名清单里；只报"未知结果字段"会让用户以为自己拼错了名字。
                not_rankable = [
                    (item, base) for item in rejected
                    if (base := unrankable_rank_base(item, self._custom_fields)) is not None
                ]
                if not_rankable:
                    raise ValueError(
                        "该字段不支持排名口径（不在可排名清单里）: "
                        + ", ".join(f"{item}（基名 {base}）" for item, base in not_rankable)
                    )
                raise ValueError(
                    f"未知结果字段: {', '.join(repr(item) for item in rejected)}"
                )

        # 确保关键列总是包含
        for essential in ("stock_code", "name"):
            if essential not in cols:
                cols.insert(0, essential)

        return ", ".join(
            column if column in METADATA_COLUMNS or column in SNAPSHOT_COLUMNS
            else f'{self._field_sql_name(column)} AS "{column}"'
            for column in cols
        )

    def _extract_fields(self, node: dict) -> set[str]:
        """递归提取条件中引用的所有字段名"""
        fields: set[str] = set()
        rules = node.get("rules", [])
        for rule in rules:
            if "logic" in rule:
                fields.update(self._extract_fields(rule))
            else:
                f = rule.get("field")
                if f:
                    fields.add(f)
                right_field = rule.get("right_field")
                if right_field:
                    fields.add(right_field)
        return fields

    def _rank_base(self, field: str) -> str | None:
        """Return the base indicator for a generated rank field, if valid."""
        return _rank_base_for(field, self._custom_fields)

    @staticmethod
    def _field_sql_name(field: str) -> str:
        """Map a validated public field to its generated SQL identifier."""
        return field.replace(".", "__")

    def _effective_masked_fields(self, masked_fields: Any) -> set[str]:
        """解析"当前不可信字段"清单；未显式传入时现算（廉价聚合探针）。"""
        if masked_fields is not None:
            return {str(field) for field in masked_fields}
        try:
            from app.core.quality.asset_health import masked_fields_for_database

            return set(masked_fields_for_database(self.duck))
        except Exception:  # noqa: BLE001 - 探针失败按"不遮蔽"处理，绝不 500
            logger.warning("字段遮蔽探针失败（本次不遮蔽）", exc_info=True)
            return set()

    def _reject_masked_fields(self, referenced_fields: set[str], masked_fields: Any) -> None:
        """引用了"当前不可信"字段 → 抛 :class:`MaskedFieldError`（409）。

        为什么拒绝而不是返回空集：空结果集是一个**错误的事实陈述**（"全市场
        没有一只股票满足"），而 409 能如实说"这个字段现在不可信"。
        """
        masked = self._effective_masked_fields(masked_fields)
        if not masked:
            return
        blocked = sorted(
            field
            for field in referenced_fields
            if field in masked or self._rank_base(field) in masked
        )
        if blocked:
            logger.warning("筛选引用了不可信字段，拒绝执行: %s", blocked)
            raise MaskedFieldError(blocked)

    def _is_known_field(self, field: str) -> bool:
        return _is_known_field_name(field, self._custom_fields)

    @staticmethod
    def _collect_stat_fields(
        conditions: dict,
        sort_spec: list[dict],
        columns_spec: list[str],
        rank_fields: set[str],
    ) -> set[str]:
        """收集规则/排序/列/排名中引用的历史统计字段。"""
        used: set[str] = set()

        def walk(node: dict) -> None:
            for rule in node.get("rules", []):
                if "rules" in rule:
                    walk(rule)
                    continue
                field = rule.get("field", "")
                if field in STAT_FIELDS:
                    used.add(field)
                right_field = rule.get("right_field", "")
                if right_field in STAT_FIELDS:
                    used.add(right_field)

        if isinstance(conditions, dict):
            walk(conditions)
        for sort in sort_spec:
            if sort.get("field") in STAT_FIELDS:
                used.add(sort["field"])
        for column in columns_spec:
            if column in STAT_FIELDS:
                used.add(column)
        for field in rank_fields:
            if field in STAT_FIELDS:
                used.add(field)
        return used

    def _published_statement_overrides(
        self,
        pool_where: str = "m.is_listed IS TRUE",
        pool_params: list[Any] | None = None,
    ) -> list[tuple[str, str, str, float]]:
        """仅加载本次参与股票/报告期的 published statement overrides。

        此前把 manual_overrides 全表 published 行塞进 VALUES，行数随覆写历史
        无限膨胀。现在先按基础池口径取出每只股票的最新快照期，再按
        (stock_code, report_date) 精确对查询 SQLite，并设硬上限。

        2026-11-21（`.planning/2026-screening-latency`）：正式库实测本函数
        **~500ms/次**，而 `manual_overrides` 当时是**空表** —— 这 500ms 全花在
        "取 5,222 个池内 (代码, 报告期) 对"（+85ms）和随后 **14 条**分块查询上
        （SQLite 也是 open-per-query，每开一条连接 ~27ms，再加上 400 个 OR
        条件的编译开销）。于是加两道与结果无关的短路：
        1. **没有 published 覆写 → 直接返回空**（一条 `LIMIT 1` 探针，~27ms），
           省掉整段配对查询与分块；
        2. 有覆写时分块**共用一条 SQLite 连接**（`SQLiteStore.connection()`）。
        两条都不改变"查哪些对、返回哪些行"的语义 —— 空表时原实现的两个循环
        在数学上就是恒等于 `[]`。
        """
        if self.sqlite is None:
            return []
        if not self._has_published_statement_overrides():
            return []
        statement_fields = {field for fields in STATEMENT_FIELDS.values() for field in fields}
        pairs_rows = self._read_query(
            f"""
            SELECT m.stock_code, CAST(s.report_date AS VARCHAR) AS report_date
            FROM stock_meta m
            JOIN LATERAL (
                SELECT report_date FROM indicator_snapshot
                WHERE stock_code = m.stock_code
                ORDER BY report_date DESC
                LIMIT 1
            ) s ON true
            WHERE {pool_where}
            ORDER BY m.stock_code
            """,
            list(pool_params or []),
        )
        pairs = [
            (str(row["stock_code"]), str(row["report_date"])[:10])
            for row in pairs_rows
            if row.get("report_date") is not None
        ]
        if not pairs:
            return []

        rows: list[tuple[str, str, str, float]] = []
        with contextlib.ExitStack() as stack:
            # 分块查询共用**一条** SQLite 连接（open-per-query 每开一条 ~27-47ms）；
            # store 没有 `connection()`（鸭子类型）就退回 `query()`，语义相同。
            open_conn = getattr(self.sqlite, "connection", None)
            if open_conn is None:
                def run_chunk(sql: str, params: list[Any]) -> list[dict]:
                    return self.sqlite.query(sql, params)
            else:
                sqlite_conn = stack.enter_context(open_conn())

                def run_chunk(sql: str, params: list[Any]) -> list[dict]:
                    return _rows_as_dicts(sqlite_conn.execute(sql, params))
            for offset in range(0, len(pairs), 400):
                chunk = pairs[offset:offset + 400]
                or_clauses = " OR ".join("(stock_code = ? AND report_date = ?)" for _ in chunk)
                params: list[Any] = [value for pair in chunk for value in pair]
                chunk_rows = run_chunk(
                    f"""SELECT stock_code, report_date, field_name, override_value
                        FROM manual_overrides
                        WHERE status = 'published' AND rolled_back_at IS NULL
                          AND report_date IS NOT NULL
                          AND ({or_clauses})""",
                    params,
                )
                for row in chunk_rows:
                    if row["field_name"] not in statement_fields:
                        continue
                    rows.append(
                        (
                            row["stock_code"],
                            str(row["report_date"])[:10],
                            row["field_name"],
                            row["override_value"],
                        )
                    )
                    if len(rows) > MAX_OVERRIDE_VALUES:
                        raise ValueError(
                            f"published statement overrides exceed the {MAX_OVERRIDE_VALUES}-row SQL limit; "
                            "narrow the pool or reconcile overrides"
                        )
        return rows

    @staticmethod
    def _probe_published_statement_overrides(sqlite: Any) -> bool:
        """本库有**任何**一条已发布 statement 覆写吗？（`LIMIT 1` 探针）

        用途只是"没有就整段跳过"：空表时 `_published_statement_overrides` 的任何
        分块查询都只会返回空，所以短路**不改变返回结果**（返回值恒为 `[]`）。
        有覆写时照旧走完整配对流程 —— 这条探针只是把"没有"这个常见情形的
        ~500ms 变成 ~27ms。
        """
        rows = sqlite.query(
            """SELECT 1 AS present FROM manual_overrides
               WHERE status = 'published' AND rolled_back_at IS NULL
                 AND report_date IS NOT NULL
               LIMIT 1"""
        )
        return bool(rows)

    def _has_published_statement_overrides(self) -> bool:
        """本次运行是否可能存在 statement 覆写（`run()` 已探过就直接复用）。"""
        if self._published_overrides_present is not None:
            return self._published_overrides_present
        if self.sqlite is None:
            return False
        return self._probe_published_statement_overrides(self.sqlite)

    def _raw_field_expr(
        self, table: str, field: str, alias: str, has_overrides: bool,
    ) -> str:
        """某物理列的取值表达式（已发布手工覆写优先，回退到原始列）。"""
        if has_overrides:
            return (
                f"COALESCE((SELECT override_value FROM overrides "
                f"WHERE stock_code = m.stock_code AND report_date = s.report_date "
                f"AND field_name = '{field}' LIMIT 1), {alias}.{field})"
            )
        return f"{alias}.{field}"

    def _override_aware_field_sql(
        self, table: str, field: str, alias: str, has_overrides: bool,
    ) -> str:
        expression = self._raw_field_expr(table, field, alias, has_overrides)
        return f"{expression} AS {self._field_sql_name(f'{table}.{field}')}"

    def _derived_field_select(
        self,
        key: str,
        derived: Any,
        alias: str,
        has_overrides: bool,
    ) -> str:
        """跨年口径断点派生列 → ``<派生 SQL> AS income__impairment_loss_positive``。

        - 断点判定用 ``s.report_date``（base_pool 每行被钉在快照报告期）；
        - 原始列取值走 :meth:`_raw_field_expr`，因此**手工覆写也会被归一化**，
          不会出现"覆写值按旧口径、底表值按新口径"的混算。
        """
        raw = lambda column: self._raw_field_expr(  # noqa: E731 - 单表达式取值器
            derived.table, column, alias, has_overrides,
        )
        expression = derived.sql(alias=alias, date_expr="s.report_date", raw=raw)
        return f"{expression} AS {self._field_sql_name(key)}"

    def _compile_published_indicators(
        self, locked_indicators: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        """Compile only the immutable, server-locked subset of DSL expressions."""
        if not locked_indicators:
            return {}
        if self.sqlite is None:
            raise ValueError("published DSL indicators require an injected SQLite store")
        compiled: dict[tuple[str, int, str], str] = {}
        active: set[tuple[str, int, str]] = set()

        def compile_expression(name: str, version: int, expected_hash: str | None = None) -> str:
            validate_expression_identifier(name)
            rows = self.sqlite.query(
                """SELECT id, expression_text, content_hash FROM dsl_expressions
                   WHERE name = ? AND version = ? AND status = 'published'""",
                [name, version],
            )
            if not rows:
                raise ValueError(f"published DSL indicator is unavailable: {name} v{version}")
            expression = rows[0]
            if expected_hash and expression["content_hash"] != expected_hash:
                raise ValueError(f"published DSL indicator content changed: {name} v{version}")
            key = (name, version, expression["content_hash"])
            if key in compiled:
                return compiled[key]
            if key in active:
                raise ValueError(f"published DSL dependency cycle: {name} v{version}")
            active.add(key)
            from app.core.dsl.ast_nodes import (
                BinaryOp,
                FieldRef,
                FuncCall,
                IndicatorRef,
                Literal,
                UnaryOp,
            )
            from app.core.dsl.engine import expand_shorthand
            from app.core.dsl.parser import parse

            dependencies = self.sqlite.query(
                """SELECT dependency.name, dependency.version, dependency.content_hash
                   FROM dsl_dependencies link
                   JOIN dsl_expressions dependency ON dependency.id = link.depends_on_id
                   WHERE link.expression_id = ?""",
                [expression["id"]],
            )
            dependency_versions = {row["name"]: row for row in dependencies}

            def compile_node(node: Any) -> str:
                if isinstance(node, Literal):
                    return str(node.value)
                if isinstance(node, FieldRef):
                    if node.period != "LATEST":
                        raise ValueError(
                            f"screening currently requires a materialized current value for {name}; "
                            f"unsupported period {node.period}"
                        )
                    field = f"{node.table}.{node.field}"
                    if field not in NORMALIZED_FIELDS:
                        raise ValueError(f"unknown normalized DSL field: {field}")
                    self._compiled_field_refs.add(field)
                    return self._field_sql_name(field)
                if isinstance(node, IndicatorRef):
                    if node.name in SNAPSHOT_COLUMNS:
                        return node.name
                    dependency = dependency_versions.get(node.name)
                    if dependency is None:
                        raise ValueError(f"unlocked DSL dependency: {node.name}")
                    return f"({compile_expression(node.name, dependency['version'], dependency['content_hash'])})"
                if isinstance(node, UnaryOp):
                    return f"(-{compile_node(node.operand)})"
                if isinstance(node, BinaryOp):
                    left, right = compile_node(node.left), compile_node(node.right)
                    return f"({left} / NULLIF({right}, 0))" if node.op == "/" else f"({left} {node.op} {right})"
                if isinstance(node, FuncCall):
                    if node.func_name in {"CAGR", "rolling_avg", "rolling_max", "rolling_min", "lag", "avg", "max", "min"}:
                        raise ValueError(
                            f"screening historical DSL planner is required for function: {node.func_name}"
                        )
                    if len(node.args) != 1:
                        raise ValueError(f"DSL function requires one argument: {node.func_name}")
                    argument = compile_node(node.args[0])
                    if node.func_name == "rank":
                        return f"CASE WHEN {argument} IS NULL THEN NULL ELSE RANK() OVER (ORDER BY {argument} NULLS LAST) END"
                    if node.func_name == "rank_industry":
                        return (
                            f"CASE WHEN csrc_l1 IS NULL OR {argument} IS NULL THEN NULL ELSE "
                            f"RANK() OVER (PARTITION BY csrc_l1 ORDER BY {argument} NULLS LAST) END"
                        )
                    if node.func_name == "percentile":
                        return f"CASE WHEN {argument} IS NULL THEN NULL ELSE PERCENT_RANK() OVER (ORDER BY {argument} NULLS LAST) END"
                    if node.func_name == "zscore":
                        return (
                            f"(({argument} - AVG({argument}) OVER ()) / "
                            f"NULLIF(STDDEV({argument}) OVER (), 0))"
                        )
                    if node.func_name == "normalize":
                        return (
                            f"(({argument} - MIN({argument}) OVER ()) / "
                            f"NULLIF(MAX({argument}) OVER () - MIN({argument}) OVER (), 0))"
                        )
                    raise ValueError(f"unsupported DSL function: {node.func_name}")
                raise ValueError("unsupported DSL AST node")

            compiled[key] = compile_node(parse(expand_shorthand(expression["expression_text"])))
            active.remove(key)
            return compiled[key]

        for name, lock in locked_indicators.items():
            version = lock.get("version")
            content_hash = lock.get("content_hash")
            if not isinstance(name, str) or not isinstance(version, int) or not isinstance(content_hash, str):
                raise ValueError("invalid locked DSL indicator reference")
            compile_expression(name, version, content_hash)
        return {
            name: compile_expression(name, lock["version"], lock["content_hash"])
            for name, lock in locked_indicators.items()
        }

    @staticmethod
    def _compatible_fields(left: str, right: str) -> bool:
        """Only compare numeric data fields; metadata and rank values are not interchangeable."""
        return left not in METADATA_COLUMNS and right not in METADATA_COLUMNS

    def _strict_exempt_derived_fields(self, strict_fields: set[str]) -> set[str]:
        """strict 模式下**豁免**字段级抓取血缘的派生字段（判据见 `DERIVED_AUDIT_SOURCE`）。

        fail-closed 两条铁律：
        1. `source_audit` 里**一条记录都没有**且不在注册表里的字段 → **不豁免**
           （否则"血缘缺失"就能冒充"派生字段"，strict 直接被绕过）；
        2. 有记录但混入任何非 `derived_calculator` 生产者 → **不豁免**
           （那是抓取字段，必须拿 strict 抓取血缘）。
        """
        if not strict_fields:
            return set()
        registry = registered_derived_fields()
        exempt = {field for field in strict_fields if field in registry}
        candidates = sorted(strict_fields - exempt)
        if not candidates:
            return exempt
        audit_name_of = {
            self._audit_field_name(field): field for field in candidates
        }
        names = sorted(audit_name_of)
        placeholders = ", ".join(["?"] * len(names))
        rows = self._read_query(
            "SELECT field_name, COUNT(*) AS n, "
            "COUNT(*) FILTER (WHERE source = ?) AS n_derived "
            f"FROM source_audit WHERE field_name IN ({placeholders}) "
            "GROUP BY field_name",
            [DERIVED_AUDIT_SOURCE, *names],
        )
        for row in rows:
            audit_name = row["field_name"]
            if audit_name not in audit_name_of:
                continue
            total = int(row["n"])
            if total > 0 and int(row["n_derived"]) == total:
                exempt.add(audit_name_of[audit_name])
        return exempt

    def _build_strict_clause(self, strict_fields: set[str], source_table: str) -> str:
        """Require a latest, field-level strict audit record for every enforced field.

        `strict_fields` here is the **enforced** set, i.e. source-data fields plus the
        input columns of exempted derived fields (`derived_input_fields`) — derived
        values themselves never have a capture event, so demanding one for them was a
        category error that made strict mode permanently empty for every computed
        indicator. The rule this predicate enforces is unchanged and non-negotiable:
        a captured field without a matching strict audit record is filtered out.
        """
        clauses: list[str] = []
        for field in sorted(strict_fields):
            clauses.append(
                "COALESCE((SELECT audit.confidence = 'strict' AND audit.value IS NOT DISTINCT FROM "
                f"{source_table}.{self._field_sql_name(field)} FROM source_audit audit "
                f"WHERE audit.stock_code = {source_table}.stock_code AND audit.field_name = '{self._audit_field_name(field)}' "
                f"AND audit.report_date = {source_table}.report_date "
                "ORDER BY fetch_time DESC, id DESC LIMIT 1), false)"
            )
        return " AND ".join(clauses)

    def _load_mixed_report_date_codes(self) -> list[str]:
        """Return stock codes whose snapshot period is inconsistent with the
        latest complete three-statement period.

        These stocks are excluded from the screening base pool instead of
        aborting the whole universe; other stocks remain fully usable.

        2026-11-21（`.planning/2026-screening-latency`）：这条探针的 SQL 本身只有
        ~90ms，但 open-per-query 下每次运行要多付 ~80ms 开连接，合计 ~180ms，
        而且**每次运行都重算同一份结果**。现在按**内容令牌**记住它（见
        `read_cache.serve_by_content`）。

        2026-11-25（独立审查证伪「不会假绿」）：旧令牌 = `content_epoch` + 行数 /
        最大报告期。审查构造实测：绕过 store 用裸 `duckdb.connect()` **就地改一个
        值**（`income_statement.revenue` NULL→123，把某期从"不完整"变成"完整"）
        → 行数不变、`MAX(report_date)` 不变、纪元不变 → 缓存回放旧名单 → 少剔除
        1 只、筛选**多算 1 只**，全程静默。两处都补了：

        1. `content_epoch` 之外再加一层**真正的内容摘要**（主防线，见
           `_mixed_report_content_fingerprint`）：对判定 SQL 真正读到的列做
           `SUM(hash(...))`，就地改值必变；
        2. `_guarded_duckdb_connect` 在裸**可写**连接打开时也推进纪元（同进程内的
           裸写连 epoch 都绕不过去）。

        **令牌取不到 / 算不出 / 超预算就现算、不缓存** —— 宁可慢，也绝不让
        "不知道数据变没变"变成"当作没变"。
        """
        from app.core.data_quality import snapshot_period_mismatches
        from app.core.storage import read_cache

        def compute() -> list[str]:
            # 走当前读作用域（若有）→ 与同批其它探针共用一条连接；
            # 判定 SQL 的唯一真源仍是 `data_quality.snapshot_period_mismatches`。
            reader = self._reader if self._reader is not None else self.duck
            return [str(row["stock_code"]) for row in snapshot_period_mismatches(reader)]

        token = read_cache.content_epoch_token(self.sqlite)
        if token <= 0:
            # 没有纪元 = 无法判断数据变没变 → 一律现算，不写缓存。
            return compute()
        fingerprint = self._mixed_report_content_fingerprint()
        if fingerprint is None:
            # 指纹算不出/太慢 = 无法判断内容变没变 → 同样不缓存（兜底原则）。
            return compute()
        return list(
            read_cache.serve_by_content(
                self.duck,
                ("screening", "mixed_report_date_codes"),
                (token, *fingerprint),
                compute,
            )
        )

    def _mixed_report_content_fingerprint(self) -> tuple[Any, ...] | None:
        """混合报告期判定的**内容指纹**（一条 SQL；算不出/太慢返回 `None`）。

        令牌的"内容敏感"那一半。旧版只有行数 + 最大报告期 → **看不见就地改值**
        （2026-11-25 审查构造：只 UPDATE 一个 `revenue` 值，三个廉价信号全不变，
        缓存回放旧判定，筛选静默多算 1 只）。

        现在对**判定真正读到的列**做单遍聚合 `SUM(hash(...))`（列清单逐条对齐
        `data_quality.snapshot_period_mismatches`）：

        * `stock_meta`：`stock_code, is_listed`（股票池 + `is_listed IS TRUE`）；
        * `indicator_snapshot`：`stock_code, report_date`（每只股票的快照期）；
        * `balance_sheet`：`stock_code, report_date, total_assets,
          total_liabilities, total_equity_parent, total_equity`
          （join 键 + `IS NOT NULL` 判据 + `COALESCE(...)` 两支）；
        * `income_statement`：`stock_code, report_date, revenue, parent_net_profit`；
        * `cash_flow`：`stock_code, report_date, cf_from_operating`。

        为什么哈希**值**而不只哈希"是否 NULL"：判定当前只用到非空性，但口径随时
        可能加条件；哈希整列值只会多失效（性能），绝不会漏失效（正确性）。
        `SUM` 对 `HUGEINT` 精确且与行序无关 → 同一份内容永远同一个摘要。

        行数 / 最大报告期仍一起取（zone map + 元数据，几乎免费）：它们能兜住
        "摘要碰撞"这种理论情形，也让令牌在摘要函数不可用时至少还有旧信号 ——
        但**只有摘要存在时才会缓存**（`SUM(hash())` 抛错 → `None` → 现算）。

        成本（正式库 28GB、5 表合计 ~110 万行，2026-11-25 实测，**每次一条新只读
        连接**＝生产路径的真实形状）：合并后的整条指纹 **~39ms**；改造前的旧指纹
        （只有行数+最大期）**~41ms** —— 摘要的**边际成本 ≈ 0**（同一条连接上热跑
        整条 ~28ms，单看摘要部分 ~7.6ms）。超过 `MAX_CONTENT_FINGERPRINT_MS`
        或任何异常 → 返回 `None`，调用方不缓存。
        """
        global _CONTENT_FINGERPRINT_WARMED

        started = time.perf_counter()
        try:
            row = self._read_query(
                """
                SELECT
                    (SELECT COUNT(*) FROM stock_meta) AS n_meta,
                    (SELECT COUNT(*) FROM stock_meta WHERE is_listed IS TRUE) AS n_listed,
                    (SELECT COUNT(*) FROM indicator_snapshot) AS n_snapshot,
                    CAST((SELECT MAX(report_date) FROM indicator_snapshot) AS VARCHAR) AS max_snapshot,
                    (SELECT COUNT(*) FROM balance_sheet) AS n_balance,
                    CAST((SELECT MAX(report_date) FROM balance_sheet) AS VARCHAR) AS max_balance,
                    (SELECT COUNT(*) FROM income_statement) AS n_income,
                    CAST((SELECT MAX(report_date) FROM income_statement) AS VARCHAR) AS max_income,
                    (SELECT COUNT(*) FROM cash_flow) AS n_cashflow,
                    CAST((SELECT MAX(report_date) FROM cash_flow) AS VARCHAR) AS max_cashflow,
                    CAST((SELECT SUM(hash(stock_code, is_listed))
                          FROM stock_meta) AS VARCHAR) AS digest_meta,
                    CAST((SELECT SUM(hash(stock_code, report_date))
                          FROM indicator_snapshot) AS VARCHAR) AS digest_snapshot,
                    CAST((SELECT SUM(hash(stock_code, report_date, total_assets,
                                          total_liabilities, total_equity_parent,
                                          total_equity))
                          FROM balance_sheet) AS VARCHAR) AS digest_balance,
                    CAST((SELECT SUM(hash(stock_code, report_date, revenue,
                                          parent_net_profit))
                          FROM income_statement) AS VARCHAR) AS digest_income,
                    CAST((SELECT SUM(hash(stock_code, report_date, cf_from_operating))
                          FROM cash_flow) AS VARCHAR) AS digest_cashflow
                """
            )[0]
        except Exception as error:  # noqa: BLE001 - 指纹算不出只影响缓存命中率
            logger.warning(
                "混合报告期内容指纹计算失败，本次不缓存（直接现算）: %s", error
            )
            return None
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        # 预算只对**纯查询成本**成立：`_reader is None` 时 `_read_query()` 退回
        # open-per-query，elapsed 里混着 ~30-80ms 的开连接，那不是内容摘要的账。
        # 生产路径（`run()` → `_read_scope()`）永远有 reader，所以预算对生产是真实的。
        if elapsed_ms > MAX_CONTENT_FINGERPRINT_MS and self._reader is not None:
            if _CONTENT_FINGERPRINT_WARMED:
                logger.warning(
                    "混合报告期内容指纹耗时 %.1fms > 预算 %.1fms，本次不缓存（直接现算）",
                    elapsed_ms,
                    MAX_CONTENT_FINGERPRINT_MS,
                )
                return None
            logger.info(
                "首个内容指纹耗时 %.1fms（含一次性查询规划成本，不否决本次缓存；"
                "之后的指纹按 %.1fms 预算执行）",
                elapsed_ms,
                MAX_CONTENT_FINGERPRINT_MS,
            )
        _CONTENT_FINGERPRINT_WARMED = True
        return tuple(sorted((key, str(value)) for key, value in row.items()))

    @staticmethod
    def _audit_field_name(field: str) -> str:
        return field.split(".", 1)[-1]

    def _get_base_pool_size(
        self, include_st: bool, include_suspended: bool, min_listing_years: int
    ) -> int:
        """获取基础池大小（排除快照/完整期不一致的股票）"""
        conds: list[str] = ["is_listed IS TRUE"]
        params: list[Any] = []
        if not include_st:
            conds.append("is_st = false")
        if not include_suspended:
            conds.append("is_suspended = false")
        if min_listing_years > 0:
            conds.append(
                f"listing_date <= CURRENT_DATE - INTERVAL '{min_listing_years}' YEAR"
            )
        if self._mixed_report_date_codes:
            slots = ", ".join("?" for _ in self._mixed_report_date_codes)
            conds.append(f"stock_code NOT IN ({slots})")
            params.extend(self._mixed_report_date_codes)
        # 与 _build_sql 的基础池必须同口径，否则 base_pool_size 会含黑名单股票，
        # 出现「池 5,578 只但结果里怎么也算不出」的假数字（AGENTS R1）。
        blacklist_pred, blacklist_params = exclusion_condition(
            "stock_code", self._blacklist_codes
        )
        if blacklist_pred:
            conds.append(blacklist_pred)
            params.extend(blacklist_params)
        where = " AND ".join(conds) if conds else "1=1"
        rows = self._read_query(
            f"SELECT COUNT(*) as cnt FROM stock_meta WHERE {where}", params
        )
        return rows[0]["cnt"] if rows else 0

    def _require_known_pool_status(
        self,
        include_st: bool,
        include_suspended: bool,
        min_listing_years: int,
    ) -> None:
        """Reject an unknown current-pool property instead of silently filtering it out."""
        unknown_conditions: list[str] = []
        if not include_st:
            unknown_conditions.append("is_st IS NULL")
        if not include_suspended:
            unknown_conditions.append("is_suspended IS NULL")
        if min_listing_years > 0:
            unknown_conditions.append("listing_date IS NULL")
        if not unknown_conditions:
            return
        unknown_count = self._read_query(
            "SELECT COUNT(*) AS count FROM stock_meta "
            f"WHERE is_listed IS TRUE AND ({' OR '.join(unknown_conditions)})"
        )[0]["count"]
        if unknown_count:
            raise ValueError(
                f"base pool metadata is incomplete for {unknown_count} listed stocks; "
                "refresh verified listing metadata before screening"
            )

    def _get_latest_data_date(self) -> str | None:
        """获取最新数据日期"""
        rows = self._read_query(
            "SELECT MAX(report_date) as latest FROM indicator_snapshot"
        )
        if rows and rows[0]["latest"]:
            return str(rows[0]["latest"])
        return None

    def generate_entry_explanation(
        self, stock: dict, conditions: dict
    ) -> str:
        """为入选股票生成入选解释 (PRD §12.5 SC13)

        只解释实际命中的条件；当结果行缺少条件字段值、或 OR/嵌套组无法
        逐项还原时，输出保守文本而不是断言一个错误的“实际值”。
        """
        matched, explanation = self._explain_conditions(
            stock, conditions, known_matched=True
        )
        return explanation or ("" if matched is True else "满足筛选条件")

    def _explain_conditions(
        self,
        stock: dict,
        node: dict,
        *,
        known_matched: bool = False,
    ) -> tuple[bool | None, str | None]:
        """Evaluate one condition group against a result row.

        Returns ``(matched, explanation)`` where ``matched`` can be
        ``None`` when the row lacks the fields needed to decide.
        ``known_matched=True`` means the caller already knows the group is
        part of a satisfied AND chain, so undecidable children are described
        conservatively instead of being dropped or asserted.
        """
        if not isinstance(node, dict):
            return False, None
        logic = str(node.get("logic", "AND")).upper()
        rules = node.get("rules", [])
        if not isinstance(rules, list) or not rules:
            return True, None

        child_known_matched = known_matched and logic == "AND"
        children: list[tuple[bool | None, str | None, Any]] = []
        for rule in rules:
            if isinstance(rule, dict) and "logic" in rule:
                matched, text = self._explain_conditions(
                    stock, rule, known_matched=child_known_matched
                )
            else:
                matched, text = self._explain_condition(stock, rule)
            children.append((matched, text, rule))

        flags = [child[0] for child in children]
        if logic == "AND":
            if any(flag is False for flag in flags):
                return False, None
            matched: bool | None = True if all(flag is True for flag in flags) else (
                True if known_matched else None
            )
        else:  # OR
            if any(flag is True for flag in flags):
                matched = True
            elif all(flag is False for flag in flags):
                matched = False
            else:
                matched = True if known_matched else None
        if matched is not True:
            return matched, None

        parts: list[str] = []
        for child_matched, text, rule in children:
            if text:
                parts.append(text)
                continue
            if child_matched is False:
                continue
            if isinstance(rule, dict) and "logic" in rule:
                parts.append("满足嵌套条件组（无法逐项还原实际值）")
            else:
                parts.append(self._conservative_condition_text(rule))
        if logic == "OR" and not parts:
            return True, "满足任一条件（无法逐项还原命中项）"
        if not parts:
            return True, None
        return True, f" {logic} ".join(parts)

    def _explain_condition(
        self, stock: dict, condition: Any,
    ) -> tuple[bool | None, str | None]:
        """Explain a leaf condition only when it can be verified as matched."""
        if not isinstance(condition, dict):
            return False, None
        field = condition.get("field", "")
        op = condition.get("op", "")
        right_field = condition.get("right_field")
        matched = self._condition_matches(stock, condition)

        if matched is False:
            return False, None
        if field in stock and (
            right_field is None or (right_field in stock and right_field is not None)
        ):
            actual = stock.get(field)
            if right_field is not None:
                right_actual = stock.get(right_field)
                return matched, (
                    f"{field} {op} {right_field} "
                    f"(实际: {field}={self._format_actual(actual)}, "
                    f"{right_field}={self._format_actual(right_actual)})"
                )
            if op in {"is_null", "is_not_null"}:
                state = "无值" if actual is None else self._format_actual(actual)
                return matched, f"{field} {op.upper()} (实际: {state})"
            return matched, (
                f"{field} {op} {self._format_actual(condition.get('value'))} "
                f"(实际: {self._format_actual(actual)})"
            )
        # 无法取到条件字段值：保守说明“满足”而不伪造实际值。
        return matched, self._conservative_condition_text(condition)

    @staticmethod
    def _conservative_condition_text(condition: Any) -> str:
        if not isinstance(condition, dict):
            return "满足筛选条件"
        field = condition.get("field", "")
        op = condition.get("op", "")
        right_field = condition.get("right_field")
        if right_field is not None:
            return f"比较条件 {field} {op} {right_field} 满足（未保留两侧字段值）"
        value = condition.get("value")
        if isinstance(value, list):
            value_text = ", ".join(str(item) for item in value)
        else:
            value_text = str(value)
        if op in {"is_null", "is_not_null"}:
            return f"{field} {op.upper()} 满足"
        return f"{field} {op} {value_text} 满足（未保留该字段实际值）"

    @staticmethod
    def _condition_matches(stock: dict, condition: dict) -> bool | None:
        """Evaluate one leaf condition against a result row.

        ``None`` means the row does not carry the needed fields, so the
        caller must fall back to a conservative explanation.
        """
        field = condition.get("field", "")
        op = condition.get("op", "")
        value = condition.get("value")
        right_field = condition.get("right_field")

        if op in {"is_null", "is_not_null"}:
            if field not in stock:
                return None
            actual = stock.get(field)
            return actual is None if op == "is_null" else actual is not None

        if op == "between":
            if field not in stock:
                return None
            actual = stock.get(field)
            if not isinstance(value, list) or len(value) != 2 or actual is None:
                return False
            try:
                return bool(value[0] <= actual <= value[1])
            except TypeError:
                return None
        if op == "in":
            if field not in stock:
                return None
            actual = stock.get(field)
            if not isinstance(value, list) or actual is None:
                return False
            try:
                return bool(actual in value)
            except TypeError:
                return None

        if right_field is not None:
            if field not in stock or right_field not in stock:
                return None
            left, right = stock.get(field), stock.get(right_field)
            if left is None or right is None:
                return False
            return ScreeningEngine._compare_values(left, op, right)

        if field not in stock:
            return None
        actual = stock.get(field)
        if actual is None or value is None:
            return False
        return ScreeningEngine._compare_values(actual, op, value)

    @staticmethod
    def _compare_values(left: Any, op: str, right: Any) -> bool | None:
        try:
            if op == ">":
                return bool(left > right)
            if op == "<":
                return bool(left < right)
            if op == ">=":
                return bool(left >= right)
            if op == "<=":
                return bool(left <= right)
            if op == "=":
                return bool(left == right)
            if op == "!=":
                return bool(left != right)
        except TypeError:
            return None
        return None

    @staticmethod
    def _format_actual(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)


def _rank_base_for(field: str, custom_fields: set[str]) -> str | None:
    """Module-level rank-field check shared by run-time and save-time validation."""
    for suffix in RANK_SUFFIXES:
        if field.endswith(suffix):
            base = field.removesuffix(suffix)
            return base if base in RANKABLE_INDICATORS or base in custom_fields else None
    return None


def unrankable_rank_base(field: str, custom_fields: set[str]) -> str | None:
    """`xxx_market_rank` 这类字段若基名存在**但不在可排名清单**，返回基名。

    为什么需要它：`_rank_base_for()` 只在基名可排名时才返回基名，
    所以「基名存在但不可排名」会被它判成 None，最终落到"未知字段"——
    而真实原因是"该字段不支持排名口径"。两者对用户是完全不同的信息：
    前者说自己拼错了，后者说要换个字段。

    典型例子：`revenue_cagr3_market_rank`（CAGR 不在 RANKABLE_INDICATORS 里）。
    """
    for suffix in RANK_SUFFIXES:
        if not field.endswith(suffix):
            continue
        base = field.removesuffix(suffix)
        if not base:
            continue
        if base in RANKABLE_INDICATORS or base in custom_fields:
            return None          # 可排名 → 不是本函数的事
        if base in SNAPSHOT_COLUMNS or base in NORMALIZED_FIELDS or base in custom_fields:
            return base          # 基名真实存在，只是不可排名
        return None
    return None


def _is_known_field_name(field: str, custom_fields: set[str]) -> bool:
    """字段白名单的唯一判定入口（R1 单一实现，依据 S7 §5.1 CB-09）。

    运行期 `ScreeningEngine._is_known_field` 与保存期 `validate_rule_fields`
    都调用这里。此前保存期维护的是**第二份手工白名单**，漏了
    `EXTENDED_COLUMNS`（indicator_ext 扩展指标列），导致引用扩展字段的规则
    "能跑不能存"——运行时不报错、保存时报"未知筛选字段"。

    新增字段域时只改这一个函数；`tests/regression/test_dsl_extended_fields_savable.py`
    用参数化断言守住"两份判定不分叉"。
    """
    return (
        field in SNAPSHOT_COLUMNS
        or field in NORMALIZED_FIELDS
        or field in EXTENDED_COLUMNS          # 扩展指标域（schema v25）
        or field in METADATA_COLUMNS
        or field in STAT_FIELDS
        or field in custom_fields
        or _rank_base_for(field, custom_fields) is not None
    )


def validate_rule_fields(rule: dict, custom_fields: set[str] = frozenset()) -> None:
    """Save-time field validation (依据 S2 P3-4).

    与运行期 `ScreeningEngine._is_known_field` 共用 `_is_known_field_name()`
    这一份白名单（依据 S7 §5.1 CB-09 / AGENTS R1），因此未知字段/排序/
    结果列在保存时就被拒，不会拖到运行才报错。Raises ValueError with the
    same messages the engine uses.
    """
    # 方案 C（2026-11-16）：保存期也要先展开 `rank_mode`，否则
    # `{"field": "pe_ttm", "rank_mode": "market_rank"}` 校验时看到基名、
    # 运行期却是 `pe_ttm_market_rank`，又会变成「能跑不能存」。
    rule = expand_rank_modes(dict(rule))

    def _known(field: str) -> bool:
        if not isinstance(field, str) or not field:
            return False
        # 与运行期共用同一份白名单（CB-09）：不再并列写第二份集合。
        return _is_known_field_name(field, custom_fields)

    def _walk(node: dict, level: int = 1) -> None:
        if not isinstance(node, dict):
            raise ValueError("规则节点必须是对象")
        if level > 3:
            raise ValueError("规则嵌套超过3层限制 (PRD §12.2)")
        for rule in node.get("rules", []):
            if not isinstance(rule, dict):
                raise ValueError("筛选条件必须是对象")
            if "logic" in rule:
                _walk(rule, level + 1)
                continue
            field = rule.get("field", "")
            if not _known(field):
                raise ValueError(f"未知筛选字段: {field}")
            right_field = rule.get("right_field")
            if right_field is not None and (not isinstance(right_field, str) or not _known(right_field)):
                raise ValueError(f"未知比较字段: {right_field}")

    conditions = rule.get("conditions")
    if conditions is not None:
        _walk(conditions)
    for sort in rule.get("sort", []):
        if not isinstance(sort, dict):
            raise ValueError("排序项必须是对象")
        field = sort.get("field")
        if field and not _known(field):
            raise ValueError(f"未知排序字段: {field}")
        if field is not None and not isinstance(field, str):
            raise ValueError(f"未知排序字段: {field}")
    # 结果列支持 `{field, rank_mode}` 对象（方案 C 第三块）：
    # 先归一化成字段名再校验，避免"能跑不能存"（CB-09）。
    for column in normalize_columns(rule.get("columns", [])):
        if not _known(column):
            raise ValueError(f"未知结果字段: {column}")
