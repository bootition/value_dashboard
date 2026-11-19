"""指标空值原因（null reason）：枚举单一真相 + 稀疏写入 + 查询。

为什么需要（docs/reports/119 §5）
--------------------------------
`indicator_snapshot`（61 列）与 `indicator_ext`（60 列）里的 NULL 至少有 6 种
**完全不同的含义**，但前端统一渲染成「—」：

    源没提供（SOURCE_EMPTY）/ 本就不适用（NOT_APPLICABLE）/
    分母无效（INVALID_DENOMINATOR）/ 我们压根没去抓（NOT_DISPATCHED）/
    有值但被门禁遮蔽（MASKED_UNTRUSTED）/ 该算没算（NOT_COMPUTED）

用户因此无法区分「没有」和「坏了」，我们自己也失去了自检信号
（2026-09-17 扣非空洞、2026Q3 fixed_assets 覆盖率从 99.9% 掉到 1.8%，
都是长时间无人察觉）。本模块把空值变成**可解释、可统计**的：

- 稀疏表 `indicator_null_reason` 只写「NULL 且原因已知」的单元格，
  有值的行一行不写 —— 表大小与「问题规模」成正比，而不是与数据规模成正比。
- 原因码只在本模块定义（单一真相），清扫器、API、前端都从这里取文案。

设计约束
--------
1. **判不出就不写**：没有确定证据的 NULL 一律不落库（宁缺勿猜）。
   `MASKED_UNTRUSTED` 也刻意不由清扫器判定 —— 遮蔽的真值在
   `app/core/data_quality.py` 的 `indicator_trust` 里，两处各判一次必然打架。
2. **幂等 + 可刷新**：同一 (stock, report_date, field) 重复写只更新不重复插入；
   整域刷新时用 `replace_scope` 先清后写，避免「字段修好了但原因还挂着」。
3. **只读优先**：所有查询函数都走 `read_query`/`read_connection`（不加写锁）。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import pandas as pd

from app.core.financial_period import CN_TZ
from app.core.storage.duckdb_store import DuckDBStore

logger = logging.getLogger(__name__)

# 指标域：由 `app/core/screening/engine.py` 的字段集合反推，不硬编码字段名。
DOMAIN_SNAPSHOT = "snapshot"
DOMAIN_EXT = "ext"
# 源表域（2026-11-14 裁决 2）：字段名带前缀，与 `missing_list` / `source_audit`
# 的既有命名一致（`income.deducted_net_profit`、`balance.fixed_assets`、…）。
# 为什么必须扩到这里：审计抓到的"有源、没调度"重灾区（扣非、固定资产、6 个银行
# 监管指标）都发生在源表，只清扫指标表的话它们永远归因不到。
SOURCE_DOMAIN_TABLES: dict[str, str] = {
    "income": "income_statement",
    "balance": "balance_sheet",
    "cashflow": "cash_flow",
    # 2026-11-17（reports/135）：现金流量表**补充资料**两张表单独成域。
    # 为什么不能用 `cashflow` 域：`cash_flow`（直接法主表，16 列）与
    # `cash_flow_indirect`（补充资料间接法，29 列）/ `cash_flow_activity`
    # （投资筹资活动，21 列）是**不同的表、不同的字段集**，
    # 混在一个前缀下会让 `domain_fields` 回查到不存在的列。
    # 字段名前缀 `cash_flow_indirect.unconfirmed_investment_loss` 与源表列同名。
    "cash_flow_indirect": "cash_flow_indirect",
    "cash_flow_activity": "cash_flow_activity",
}
SOURCE_DOMAINS: tuple[str, ...] = tuple(SOURCE_DOMAIN_TABLES)
DOMAINS: tuple[str, ...] = (DOMAIN_SNAPSHOT, DOMAIN_EXT, *SOURCE_DOMAINS)

# 表名（写入/查询共用）
NULL_REASON_TABLE = "indicator_null_reason"


class NullReasonTableMissing(RuntimeError):
    """稀疏表不存在：正式库 schema 还没升到建表的那一版。

    刻意**不**自动建表 —— schema 迁移是发布动作（`app/core/storage/schema.py`
    + 单写者串行），清扫器偷偷 DDL 会让版本号与迁移记录对不上。
    """


class NullReason(StrEnum):
    """NULL 的 6 种含义。

    与 `indicator_null_reason.reason_code` 一一对应；新增取值必须同时补文案，
    否则前端 tooltip 只能显示裸码。
    """

    SOURCE_EMPTY = "SOURCE_EMPTY"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INVALID_DENOMINATOR = "INVALID_DENOMINATOR"
    NOT_DISPATCHED = "NOT_DISPATCHED"
    MASKED_UNTRUSTED = "MASKED_UNTRUSTED"
    NOT_COMPUTED = "NOT_COMPUTED"


# 短文案（表格 tooltip / 状态页标签）。刻意保持 4~6 字：tooltip 与列宽都紧张。
NULL_REASON_LABELS: dict[NullReason, str] = {
    NullReason.SOURCE_EMPTY: "数据源未提供",
    NullReason.NOT_APPLICABLE: "不适用",
    NullReason.INVALID_DENOMINATOR: "分母无效",
    NullReason.NOT_DISPATCHED: "未调度抓取",
    NullReason.MASKED_UNTRUSTED: "被门禁遮蔽",
    NullReason.NOT_COMPUTED: "未计算",
}

# 长文案（说明「为什么会这样」+「谁该去修」）。前端 tooltip 直接展示短文案 +
# 这里的解释，避免运维/用户把「不适用」误读成「数据坏了」。
NULL_REASON_DESCRIPTIONS: dict[NullReason, str] = {
    NullReason.SOURCE_EMPTY: "数据源本身未提供该科目，我们没有别的渠道可补。",
    NullReason.NOT_APPLICABLE: "该指标对这类公司本就没有业务含义（如无分红公司谈股息率）。",
    NullReason.INVALID_DENOMINATOR: "分母为 0 或负（如股东权益为负、营业收入为 0），比率无意义。",
    NullReason.NOT_DISPATCHED: "上一报告期有数据、本期整体为空：说明本期没有调度抓取/计算。",
    NullReason.MASKED_UNTRUSTED: "数据已入库但未通过可信门禁，被遮蔽（真值见数据质量页）。",
    NullReason.NOT_COMPUTED: "该算却没算：输入列为空、报告期未对齐或依赖项缺失。",
}

# 展示顺序：越「需要人动手」的越靠前，方便状态页一眼看到问题。
REASON_ORDER: tuple[NullReason, ...] = (
    NullReason.NOT_DISPATCHED,
    NullReason.NOT_COMPUTED,
    NullReason.SOURCE_EMPTY,
    NullReason.MASKED_UNTRUSTED,
    NullReason.INVALID_DENOMINATOR,
    NullReason.NOT_APPLICABLE,
)


def reason_labels() -> dict[str, str]:
    """`{reason_code: 短文案}`，供 API 直接下发给前端（避免文案两处维护）。"""
    return {code.value: label for code, label in NULL_REASON_LABELS.items()}


def reason_descriptions() -> dict[str, str]:
    return {code.value: text for code, text in NULL_REASON_DESCRIPTIONS.items()}


def coerce_reason(value: str | NullReason) -> NullReason:
    """字符串 → 枚举；非法值抛 ValueError（写入前必须校验，别让脏码进库）。"""
    if isinstance(value, NullReason):
        return value
    return NullReason(value)


@functools.lru_cache(maxsize=1)
def _domain_fields() -> dict[str, frozenset[str]]:
    """域 → 字段集合（`indicator_*` 用裸列名，源表域用 `表前缀.列名`）。

    延迟导入 `screening.engine`：那里牵了 dsl/stores，Web 进程只是查原因分布时
    不该为了一张稀疏表把筛选引擎整条链拉起来。
    字段清单三个域都取自 engine（`STATEMENT_FIELDS` 就是 DSL/missing_list 用的
    那份注册表），因此新增字段不需要改本模块。
    """
    from app.core.screening.engine import (
        EXTENDED_COLUMNS,
        SNAPSHOT_COLUMNS,
        STATEMENT_FIELDS,
    )

    # 现金流量补充域的两张表不在 `STATEMENT_FIELDS`（那张注册表只覆盖主链三表），
    # 数值列清单由 `supplement.py` 的 spec 列声明提供 —— 单一真相，不另抄一份。
    from app.core.supplement import (
        ACTIVITY_VALUE_COLUMNS,
        INDIRECT_VALUE_COLUMNS,
    )

    fields: dict[str, frozenset[str]] = {
        DOMAIN_SNAPSHOT: frozenset(SNAPSHOT_COLUMNS),
        DOMAIN_EXT: frozenset(EXTENDED_COLUMNS),
    }
    for prefix in SOURCE_DOMAINS:
        if prefix == "cash_flow_indirect":
            columns = INDIRECT_VALUE_COLUMNS
        elif prefix == "cash_flow_activity":
            columns = ACTIVITY_VALUE_COLUMNS
        else:
            columns = STATEMENT_FIELDS.get(prefix, set())
        fields[prefix] = frozenset(f"{prefix}.{column}" for column in columns)
    return fields


def domain_fields(domain: str) -> frozenset[str]:
    """返回某指标域的全部可筛选字段（未知域抛 ValueError）。"""
    fields = _domain_fields()
    if domain not in fields:
        raise ValueError(f"unknown indicator domain: {domain!r} (expect one of {DOMAINS})")
    return fields[domain]


@dataclass(frozen=True)
class NullReasonRow:
    """一条空值原因记录 = 一个 (股票, 报告期, 字段) 单元格。"""

    stock_code: str
    report_date: date
    field_name: str
    reason_code: NullReason
    detail: str | None = None

    def as_tuple(self, computed_at: datetime) -> tuple[Any, ...]:
        return (
            self.stock_code,
            self.report_date,
            self.field_name,
            self.reason_code.value,
            self.detail,
            computed_at,
        )


def null_reason_table_exists(store: DuckDBStore) -> bool:
    """稀疏表是否已存在（正式库可能还停在建表前的 schema 版本）。"""
    rows = store.read_query(
        "SELECT COUNT(*) AS c FROM information_schema.tables WHERE table_name = ?",
        [NULL_REASON_TABLE],
    )
    return bool(rows and rows[0]["c"])


def _iter_chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def upsert_reasons(
    store: DuckDBStore,
    rows: Iterable[NullReasonRow],
    *,
    computed_at: datetime | None = None,
    replace_scope: tuple[date, str] | None = None,
) -> int:
    """写入空值原因（稀疏：调用方只传 NULL 单元格）。

    - `replace_scope=(report_date, domain)`：同一事务内先删掉该域该报告期的旧记录
      再写新记录。为什么需要：字段修好之后旧原因必须消失，否则表会变成
      「历史错误永久挂在字段上」的反向噪音。
    - 主键去重：同一批次内重复的 (stock, report_date, field) 以后者为准，
      避免 `executemany` 撞 PK 直接抛错。
    - 时间戳用北京时间墙钟（不带 tz），与库内其它 `TIMESTAMP` 列口径一致；
      写 tz-aware 会被 DuckDB 按会话时区二次换算，出现 8 小时漂移。
    """
    stamp = computed_at or datetime.now(CN_TZ).replace(tzinfo=None)
    deduped: dict[tuple[str, date, str], NullReasonRow] = {}
    for row in rows:
        # 非法原因码在写库前就炸出来，不留脏数据
        reason = coerce_reason(row.reason_code)
        if reason is not row.reason_code:
            row = NullReasonRow(
                stock_code=row.stock_code,
                report_date=row.report_date,
                field_name=row.field_name,
                reason_code=reason,
                detail=row.detail,
            )
        if not row.stock_code or not row.field_name:
            raise ValueError("null reason row requires stock_code and field_name")
        deduped[(row.stock_code, row.report_date, row.field_name)] = row

    payload = [row.as_tuple(stamp) for row in deduped.values()]
    purge_fields: tuple[str, ...] = ()
    scope_date: date | None = None
    scope_domain: str | None = None
    if replace_scope is not None:
        scope_date, scope_domain = replace_scope
        purge_fields = tuple(sorted(domain_fields(scope_domain)))

    if not payload and not purge_fields:
        return 0
    if not null_reason_table_exists(store):
        raise NullReasonTableMissing(
            f"{NULL_REASON_TABLE} 不存在：正式库 schema 尚未迁移到包含该表的版本。"
            "请先执行 schema 迁移（迁移是发布动作，清扫器不擅自 DDL）。"
        )

    with store.transaction() as connection:
        if purge_fields and scope_date is not None:
            placeholders = ", ".join("?" for _ in purge_fields)
            connection.execute(
                f"DELETE FROM {NULL_REASON_TABLE} "
                f"WHERE report_date = ? AND field_name IN ({placeholders})",
                [scope_date, *purge_fields],
            )
            logger.info("null reasons purge scope %s/%s", scope_date, scope_domain)
        # 2026-11-14：原实现用 `executemany`（2000 行/批）在正式库上触发
        # `PyEval_SaveThread ... GIL released` 致命错误（DuckDB 1.5.5 + Python 3.12
        # 对 executemany 内 DATE 参数的老问题，见 ops-knowledge-base D19）。
        # 改为「pandas register + 单条 INSERT SELECT」——与 lineage/价格批量写入同一范式。
        frame = pd.DataFrame(
            payload,
            columns=[
                "stock_code", "report_date", "field_name",
                "reason_code", "detail", "computed_at",
            ],
        )
        frame["report_date"] = pd.to_datetime(frame["report_date"]).dt.date
        frame["computed_at"] = pd.to_datetime(frame["computed_at"])
        connection.register("_null_reason_rows", frame)
        try:
            connection.execute(
                f"""INSERT INTO {NULL_REASON_TABLE}
                        (stock_code, report_date, field_name, reason_code, detail, computed_at)
                    SELECT stock_code, report_date, field_name, reason_code, detail, computed_at
                    FROM _null_reason_rows
                    ON CONFLICT (stock_code, report_date, field_name) DO UPDATE SET
                        reason_code = excluded.reason_code,
                        detail      = excluded.detail,
                        computed_at = excluded.computed_at"""
            )
        finally:
            connection.unregister("_null_reason_rows")
    logger.info("null reasons written: %s rows (report_date filter: %s)", len(payload), replace_scope)
    return len(payload)


def reason_distribution(
    store: DuckDBStore,
    *,
    report_date: date | str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """聚合每个字段的 NULL 原因分布：`{field: {reason: count}}`。

    `report_date` 缺省取稀疏表里最新报告期（= 最近一次清扫的对象），
    这样状态页不必先知道「最近一次清扫了哪一期」。
    """
    if domain is not None and domain not in DOMAINS:
        raise ValueError(f"unknown indicator domain: {domain!r} (expect one of {DOMAINS})")
    if not null_reason_table_exists(store):
        # 正式库还没迁移到该表时，状态页应当显示「尚未生成」而不是 500/503
        return {
            "report_date": None,
            "domain": domain,
            "fields": {},
            "totals": {},
            "total_cells": 0,
            "table_ready": False,
            "labels": reason_labels(),
            "descriptions": reason_descriptions(),
        }
    resolved_date = report_date
    if resolved_date is None:
        latest = store.read_query(f"SELECT MAX(report_date) AS d FROM {NULL_REASON_TABLE}")
        resolved_date = latest[0]["d"] if latest else None
    if resolved_date is None:
        return {
            "report_date": None,
            "domain": domain,
            "fields": {},
            "totals": {},
            "total_cells": 0,
            "table_ready": True,
            "labels": reason_labels(),
            "descriptions": reason_descriptions(),
        }

    rows = store.read_query(
        f"""SELECT field_name, reason_code, COUNT(*) AS cells
            FROM {NULL_REASON_TABLE}
            WHERE report_date = ?
            GROUP BY field_name, reason_code""",
        [resolved_date],
    )
    wanted = domain_fields(domain) if domain is not None else None
    distribution: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    total_cells = 0
    order = {code.value: index for index, code in enumerate(REASON_ORDER)}
    for row in rows:
        field_name = str(row["field_name"])
        if wanted is not None and field_name not in wanted:
            continue
        reason_code = str(row["reason_code"])
        cells = int(row["cells"])
        distribution.setdefault(field_name, {})[reason_code] = cells
        totals[reason_code] = totals.get(reason_code, 0) + cells
        total_cells += cells
    # 稳定顺序：字段按名字、原因按 REASON_ORDER（前端表格无需二次排序）
    ordered_fields = {
        field: dict(sorted(buckets.items(), key=lambda kv: order.get(kv[0], len(order))))
        for field, buckets in sorted(distribution.items())
    }
    ordered_totals = dict(sorted(totals.items(), key=lambda kv: order.get(kv[0], len(order))))
    return {
        "report_date": str(resolved_date),
        "domain": domain,
        "fields": ordered_fields,
        "totals": ordered_totals,
        "total_cells": total_cells,
        "table_ready": True,
        "labels": reason_labels(),
        "descriptions": reason_descriptions(),
    }
