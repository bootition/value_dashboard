"""跨年口径断点派生字段（依据 S1 §9.2 P0-7 / P0-8，2026-11-16）。

为什么需要这一层
----------------
这两个科目的**原始值没错，跨断点直接相加/相比才是错的**：

1. **P0-7 资产减值损失符号在 FY2018 翻转。** 财会〔2018〕15 号起，
   「资产减值损失 / 信用减值损失」按**损失以负号列示**；2017 及以前按
   **损失为正**。库内两段直接透传、未做符号归一，2018 年还是**混合年**
   （同年不同公司混用两种约定），逐年汇总时正负相消，得到"假低值"
   （实测影响 2015–2025 约 6.4 万行）。
   → 派生口径 ``*_positive``：``CASE WHEN report_date >= DATE '2018-12-31'
     THEN ABS(x) ELSE x END`` —— 三年都按"损失为正"读。

2. **P0-8 FY2020 预收款项 → 合同负债重分类。** 新收入准则 CAS14
   （境内上市 2020-01-01 起施行）把与客户合同相关的预收款重分类为合同负债：
   ``prepayments_received`` 单列一夜掉 **96.6%**（37,379.8 亿 → 1,284.3 亿，
   非空率 94.9% → 28.2%），残留只剩租金等非合同类，**语义已漂移**；
   而 ``contract_liabilities`` 在 2019 及以前基本不存在（393 只，为个别提前
   执行准则的公司）。两者合计（61,677.7 亿 → 68,348.5 亿，+10.8%）才是连续口径。
   → 派生口径 ``advance_from_customers`` = 预收款项 + 合同负债（合并口径）。

设计纪律（与 依据 S1 的硬约束一致）
--------------------------------------
- **原始列一行不改**：归一化只发生在派生层。原始列仍可查询，但引用时一律带
  ``field_semantics_break:*`` 警告（**不标废弃** —— 它们在各自区间内仍然正确，
  整列标废弃会让 2018 年以前/2020 年以后的历史查询全部失效）。
- **SQL 只有一个真源**：本模块的 ``build_sql`` 同时被
  ① DSL 代码生成（``app/core/dsl/codegen.py`` —— 详情页自定义趋势/预览）
  ② 筛选引擎（``app/core/screening/engine.py`` —— base_pool 派生列）
  使用，杜绝"同一概念两处各写一套"（依据 S1 P0-4 的同型教训）。
- **不制造假 0**：``advance_from_customers`` 只在两列**都**缺失时返回 NULL；
  任一列有值则缺失侧按 0 计（会计上"没有该科目"就是 0）。若一律
  ``COALESCE(a,0)+COALESCE(b,0)``，2015 年以前两列都没有的股票会被写成
  "预收款项 = 0"，那是又一个假零值。
- **默认派生口径**：裸字段名（``asset_impairment_loss`` /
  ``credit_impairment_loss`` / ``prepayments_received``）在 DSL 简写展开时
  落到派生字段（见 ``BARE_NAME_DEFAULTS``）；要取原始值必须写全路径
  （``income.asset_impairment_loss@LATEST``），并会收到口径断点警告。
"""

# 依据索引（本模块「为什么」的唯一落点；行内只写「依据 S<n>」，不再重复报告号）
# S1  reports/131 §9.2 P0-7 / P0-8 / P0-4 —— 跨年口径断点派生：资产减值损失符号 FY2018 翻转
#     （P0-7）；预收款项 FY2020 语义变更，必须用「预收 + 合同负债」合并口径，
#     「合同负债/总资产」类排名不分年断开就会误导（P0-8）；同一概念只许一处 SQL 真源
#     （P0-4 的同型教训）。


from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

# ─── 断点日期（口径断点年表的代码侧唯一真源）─────────────────────────────
#: 财会〔2018〕15 号（2018 年度起施行）—— 减值损失改按负号列示，2018 为混合年。
IMPAIRMENT_SIGN_CUTOFF = "2018-12-31"
#: 新收入准则 CAS14（境内上市 2020-01-01 起施行）—— 预收款项重分类为合同负债。
ADVANCE_RECLASS_CUTOFF = "2020-01-01"

#: 把「物理列名 → 该行上的 SQL 表达式」交给模板的回调。
#: 筛选引擎用它注入"手工覆写优先"的表达式，DSL 代码生成用默认的 ``alias.column``。
RawResolver = Callable[[str], str]


@dataclass(frozen=True)
class DerivedField:
    """一个跨年口径断点派生字段。

    ``build_sql(alias, date_expr, raw)`` 负责生成该行上的 SQL 表达式：
    - ``alias``     —— 物理表别名（``bs`` / ``ic`` / ``cf`` …）
    - ``date_expr`` —— **该行**的报告期表达式（``s.report_date`` / ``prior.report_date`` …），
      断点判定必须逐行做，否则 TTM/YoY 的历史项会用错约定。
    - ``raw``       —— 物理列取值器（见 :data:`RawResolver`）
    """

    key: str
    table: str
    unit: str
    period_type: str
    historical_capable: bool
    raw_columns: tuple[str, ...]
    build_sql: Callable[[str, str, RawResolver], str]
    doc: str

    def sql(
        self,
        *,
        alias: str,
        date_expr: str,
        raw: RawResolver | None = None,
    ) -> str:
        resolver: RawResolver = raw or (lambda column: f"{alias}.{column}")
        return self.build_sql(alias, date_expr, resolver)

    def metadata(self) -> dict[str, Any]:
        """DSL ``FIELD_METADATA`` 条目（多出的键由 validator/codegen 忽略）。"""
        return {
            "unit": self.unit,
            "period_type": self.period_type,
            "historical_capable": self.historical_capable,
            "derived": True,
            "derived_from": list(self.raw_columns),
            "doc": self.doc,
        }


def _positive_loss_builder(column: str) -> Callable[[str, str, RawResolver], str]:
    """生成「损失恒为正」的归一化表达式（P0-7）。

    2018-12-31 及以后取 ABS（库里按负号列示损失），之前原样（库里损失为正）。
    ``x`` 为 NULL 时两分支都是 NULL —— 如实缺失，不回退 0。
    """

    def build(alias: str, date_expr: str, raw: RawResolver) -> str:
        value = raw(column)
        return (
            f"CASE WHEN {date_expr} >= DATE '{IMPAIRMENT_SIGN_CUTOFF}' "
            f"THEN ABS({value}) ELSE {value} END"
        )

    return build


def _advance_from_customers_builder(alias: str, date_expr: str, raw: RawResolver) -> str:
    """预收款项 + 合同负债合并口径（P0-8）。

    跨 2020 连续：2020 起合同负债承接了原先记在预收款项里的"与客户合同相关的
    预收款"。两列都缺失 → NULL（别把"没这个科目"写成 0 再参与均值）。
    """
    prepayments = raw("prepayments_received")
    contracts = raw("contract_liabilities")
    return (
        f"CASE WHEN {prepayments} IS NULL AND {contracts} IS NULL THEN NULL "
        f"ELSE COALESCE({prepayments}, 0) + COALESCE({contracts}, 0) END"
    )


def _advance_ratio_builder(alias: str, date_expr: str, raw: RawResolver) -> str:
    """客户占款率 = (预收款项 + 合同负债) / 总资产（占款类指标的合并口径消费者）。

    依据 S1 P0-8：「合同负债/总资产」类排名必须用合并口径，否则 2019→2020
    会出现 −96.6% 的假断崖。分母 ≤ 0 或缺失 → NULL（比率无业务含义）。
    """
    advance = _advance_from_customers_builder(alias, date_expr, raw)
    assets = raw("total_assets")
    return f"CASE WHEN {assets} > 0 THEN ({advance}) / {assets} END"


DERIVED_FIELDS: dict[str, DerivedField] = {
    field.key: field
    for field in (
        DerivedField(
            key="income.impairment_loss_positive",
            table="income",
            unit="CNY",
            period_type="cumulative",
            historical_capable=True,
            raw_columns=("asset_impairment_loss",),
            build_sql=_positive_loss_builder("asset_impairment_loss"),
            doc=(
                "资产减值损失（损失为正）。2018-12-31 起库内按负号列示，此处取 ABS；"
                "2017 及以前库内按正数列示，原样保留。跨 2018 的减值率/EBIT 修正/"
                "扣非还原必须用本字段。"
            ),
        ),
        DerivedField(
            key="income.credit_impairment_loss_positive",
            table="income",
            unit="CNY",
            period_type="cumulative",
            historical_capable=True,
            raw_columns=("credit_impairment_loss",),
            build_sql=_positive_loss_builder("credit_impairment_loss"),
            doc=(
                "信用减值损失（损失为正），断点规则同资产减值损失。该科目 2019 年"
                "（新金融工具准则）才成建制出现，2018 及以前覆盖率极低（5.5%），"
                "跨 2019 的序列另需注意覆盖断崖。"
            ),
        ),
        DerivedField(
            key="balance.advance_from_customers",
            table="balance",
            unit="CNY",
            period_type="point_in_time",
            historical_capable=True,
            raw_columns=("prepayments_received", "contract_liabilities"),
            build_sql=_advance_from_customers_builder,
            doc=(
                "预收款项 + 合同负债（合并口径，跨 2020 连续）。2020-01-01 起 CAS14 "
                "把与客户合同相关的预收款重分类为合同负债，单列任取其一都会出现假断崖。"
            ),
        ),
        DerivedField(
            key="balance.advance_from_customers_ratio",
            table="balance",
            unit="ratio",
            period_type="point_in_time",
            historical_capable=True,
            raw_columns=("prepayments_received", "contract_liabilities", "total_assets"),
            build_sql=_advance_ratio_builder,
            doc="客户占款率 = (预收款项 + 合同负债) / 总资产（占款类指标的合并口径）。",
        ),
    )
}

#: 裸字段名默认展开到派生口径（"默认用派生口径，原始列保留可查"的落地点）。
#:
#: 只对**没有被全路径限定的裸名**生效：``asset_impairment_loss`` 落派生字段；
#: 要原始值必须写 ``income.asset_impairment_loss@LATEST``（并收到口径断点警告）。
BARE_NAME_DEFAULTS: dict[str, str] = {
    "asset_impairment_loss": "income.impairment_loss_positive@TTM",
    "credit_impairment_loss": "income.credit_impairment_loss_positive@TTM",
    "prepayments_received": "balance.advance_from_customers@LATEST",
}


@dataclass(frozen=True)
class SeriesBreak:
    """一个"原始列在某个日期之后语义已变"的口径断点登记。

    刻意**不**复用 ``DEPRECATED_COLUMNS``：那些列是"整列口径不可复现"（如
    ``dividend_yield`` 用 ``now()`` 做分母），而这里的列在各自区间内仍然正确，
    只是**跨断点不可直接比较**。混在一起会让用户以为历史数据全废。
    """

    field: str
    from_date: str
    prefer: str
    reason: str

    def warning(self) -> str:
        """筛选结果/DSL 校验用的中文警告（格式稳定，便于断言与展示）。"""
        return (
            f"field_semantics_break:{self.field}, {self.from_date} 起口径已变，"
            f"跨断点不可直接比较（{self.reason}）；请改用 {self.prefer}"
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "from_date": self.from_date,
            "prefer": self.prefer,
            "reason": self.reason,
        }


def _break(field: str, from_date: str, prefer: str, reason: str) -> SeriesBreak:
    return SeriesBreak(field=field, from_date=from_date, prefer=prefer, reason=reason)


SERIES_BREAKS: dict[str, SeriesBreak] = {
    brk.field: brk
    for brk in (
        _break(
            "income.asset_impairment_loss",
            IMPAIRMENT_SIGN_CUTOFF,
            "income.impairment_loss_positive",
            "财会〔2018〕15 号起损失以负号列示，2017 及以前损失为正，2018 为混合年",
        ),
        _break(
            "income.credit_impairment_loss",
            IMPAIRMENT_SIGN_CUTOFF,
            "income.credit_impairment_loss_positive",
            "财会〔2018〕15 号起损失以负号列示，2017 及以前损失为正，2018 为混合年",
        ),
        _break(
            "balance.prepayments_received",
            ADVANCE_RECLASS_CUTOFF,
            "balance.advance_from_customers",
            "CAS14 新收入准则把与客户合同相关的预收款重分类为合同负债，"
            "单列一夜 −96.6%，残留只剩租金等非合同类",
        ),
        _break(
            "balance.contract_liabilities",
            ADVANCE_RECLASS_CUTOFF,
            "balance.advance_from_customers",
            "CAS14 2020-01-01 起施行，2019 及以前单列基本不存在（仅个别公司提前执行）",
        ),
    )
}


def field_metadata() -> dict[str, dict[str, Any]]:
    """所有派生字段的 DSL 元数据（供 ``ast_nodes.FIELD_METADATA`` 注入）。"""
    return {key: derived.metadata() for key, derived in DERIVED_FIELDS.items()}


def series_break_metadata() -> dict[str, dict[str, Any]]:
    """原始列的断点元数据（供 ``ast_nodes.FIELD_METADATA`` 注入）。"""
    return {key: brk.metadata() for key, brk in SERIES_BREAKS.items()}


def series_break_for(full_key: str) -> SeriesBreak | None:
    """取某原始列的口径断点登记（派生字段本身没有断点）。"""
    return SERIES_BREAKS.get(full_key)


def series_break_warnings(fields: Iterable[Any] | None) -> list[str]:
    """把引用到的"断点原始列"翻译成警告（顺序稳定，便于断言与展示）。

    与 ``screening.engine.deprecated_field_warnings`` 同形状，但前缀是
    ``field_semantics_break:`` —— 调用方可以据此区分"整列废了"与"跨断点不可比"。
    """
    warnings: list[str] = []
    for field in sorted({str(item) for item in (fields or ())}):
        brk = SERIES_BREAKS.get(field)
        if brk is not None:
            warnings.append(brk.warning())
    return warnings


def bare_name_defaults() -> dict[str, str]:
    """DSL 简写展开的"默认派生口径"覆盖表（返回副本，调用方可直接 update）。"""
    return dict(BARE_NAME_DEFAULTS)
