"""构建 / 核对 VD 数据包。

2026-09-19 首版按数据形态分两层：**A 类 = 可由外来数据包覆盖、本项目提供更新/扩展版；
B 类 = 外来数据包不覆盖、本项目独有的数据**。

2026-11-14 审查（docs/reports/119 §3）后的三项改造
--------------------------------------------------
1. **文档不再硬编码**：`README.md` / `SOURCES.md` / `KNOWN_ISSUES` 全部改为按**本次实际
   导出结果**生成。首版把"indicator_ext 51 列""年报公布日仅年度频率""三表至 06-30"
   写成常量，重导出从不纠正，文档与内容 5 处不一致全是这么来的。现在每个数字都来自
   导出实测，重导出即自动更新。
2. **补齐缺表**：`announcement_registry`（公告台账）、`trading_dates`（交易日历）进包
   （`B_supplements/`，来源性质「自采」）。前者 SOURCES 一直声称有、包里却没有；后者
   缺失导致别人拿到包无法复算连续性/滞后天数。
3. **新增 `--verify --pkg <dir>` 只读对账**：文件存在性 + 行数 + sha256（致命），外加
   「库内漂移」检查（manifest 的 as_of 与当前库最新日期比较，逐表输出落后天数）。首版
   包落后正式库 57 天，只能靠人工发现——对账模式就是为这件事兜底。

包的形态
--------
    vd-datapackage-<版本>-<导出日>/
    ├── manifest.json           装箱单：as_of 分域/逐表行数·列数·最新日期·覆盖率/sha256
    ├── README.md               人读说明（自动生成）
    ├── SOURCES.md              数据来源说明（自动生成）
    ├── load.sql                DuckDB 一键装载
    ├── A_updates/              基础财务与自算指标（含更新/扩展）
    ├── B_supplements/          本项目独有数据（行情/事件/参考/统计）
    └── lineage/                溯源（--with-lineage）

安全
----
- 纯只读导出：不改任何现有数据；写入只发生在目标目录。
- 导出前检查写锁（自动更新进行中时导出会读到半成品快照）。
- 逐表 COPY 到临时文件后原子改名，失败不留半成品。
- **不含个人数据**（自选/筛选规则/ETF 流水/预算等一律不导出）：导出面是显式白名单，
  `EXCLUDED_PERSONAL_TABLES` 里的表一张都进不来（导出前还会再断言一次）。

用法
----
    python scripts/export_data_package.py --plan                     # 只列计划
    python scripts/export_data_package.py --out D:/vd-package        # 导出
    python scripts/export_data_package.py --with-lineage             # 含溯源层
    python scripts/export_data_package.py --verify --pkg <包目录>     # 只读对账（不写文件）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    DatabasePathSet,
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402

logger = logging.getLogger(__name__)

PACKAGE_VERSION = "2026.11"

# manifest 结构版本：2 起每表带 columns/latest_date/date_column/field_coverage，
# as_of 改为分域结构。（1 是 2026-09-19 首版扁平 as_of，--verify 仍兼容。）
MANIFEST_FORMAT_VERSION = 2

# 日期列探测优先级：业务日期优先于抓取时间，同一张表命中第一个即用。
# 前六项是审查清单指定的候选列；后四项是库内真实存在、但清单没列的列——
# 不补进来，xdxr / announcement_registry 这类表的最新日期永远是 None。
DATE_COLUMN_PRIORITY: tuple[str, ...] = (
    "trade_date",
    "report_date",
    "ex_date",
    "fetch_time",
    "announce_date",
    "effective_date",
    "event_date",
    "announcement_time",
    "fetched_at",
    "calculated_at",
)

# 表滞后判定阈值（交易日）。滞后超过它才写进 KNOWN_ISSUES。
LAG_TRADING_DAYS = 5
# 覆盖率「塌陷」阈值：某字段在最新期间有值但不足两成时提示（对应 119 §2.3）。
COVERAGE_COLLAPSE = 0.2
# 单条问题里最多列几个字段名，超出的用「等 N 个字段」收口，避免文档变天书。
MAX_ISSUE_COLUMNS = 6
# 覆盖率只在样本量足够时才下结论：当期只有 1-3 行时"字段无数据"纯属噪声。
MIN_COVERAGE_ROWS = 5
# 外部历史数据包的截止报告期（固定事实，见 docs/reports/119 §5）。
HISTORY_ARCHIVE_CUTOFF = "2025-03-31"

# freshness = 这张表「应该」跟到什么新鲜度：
#   daily     每天跟着主数据走 → 与价格域最新交易日比
#   quarterly 每期跟着财报走   → 与三表最新报告期比
#   event     事件驱动、天然稀疏（分红/回购/股本…）→ 不做滞后判定
#   frozen    只含外部历史归档段，永久冻结 → 由「历史段说明」条款覆盖
SOURCE_CLASSES: dict[str, str] = {
    "自采": "本项目自行采集（行情/事件/公告等）",
    "自算": "本项目基于原始数据自行计算的指标",
    "公开接口": "第三方公开接口的加工品",
    "历史归档": "外部数据包的历史段，截止 2025-03-31",
    "混合": "历史段与本项目续写合并",
}
CONTENTS: dict[str, str] = {
    "A_updates": "基础财务与自算指标（含更新与扩展）",
    "B_supplements": "本项目独有数据（行情/事件/参考/统计）",
    "lineage": "溯源与质量披露（可选层）",
}
NOT_INCLUDED: list[str] = [
    "个人数据（自选/筛选规则/ETF 流水/预算）",
    "原始源文件",
    "原始响应字节",
]

# 白名单纪律的兜底名单：这些表无论谁加进白名单都不许导出。
# missing_list / retry_list 不在其中：它们是自动生成的数据质量台账（股票代码 + 字段 +
# 原因码），没有任何用户输入内容，备份侧加密只因为它们属于「本机生成物」。
EXCLUDED_PERSONAL_TABLES: frozenset[str] = frozenset({
    "watchlist",
    "screening_rules",
    "screening_runs",
    "screening_results",
    "screening_drafts",
    "manual_overrides",
    "plans",
    "dsl_expressions",
    "dsl_dependencies",
    "etf_meta",
    "etf_trades",
    "etf_cash_flows",
    "etf_sell_plans",
    "etf_settings",
    "config",
    "job_logs",
    "backup_registry",
    "pdf_tasks",
    "pdf_archive_manifest",
})

DOMAIN_LABELS: dict[str, str] = {
    "price": "价格",
    "financial": "财务三表",
    "indicator": "自算指标",
    "event": "事件",
}
# as_of 分域成员（包内输出名）。域最新日期 = 成员探测到的最新日期取最大。
DOMAIN_MEMBERS: dict[str, tuple[str, ...]] = {
    "price": ("price_daily_raw", "price_daily_qfq", "etf_daily"),
    "financial": ("balance_sheet", "income_statement", "cash_flow"),
    "indicator": ("indicator_snapshot", "indicator_ext"),
    "event": (
        "announcement_registry",
        "dividends",
        "xdxr",
        "buyback_events",
        "funding_events",
    ),
}
# 首版 manifest 的扁平 as_of 键 → 域名（--verify 读旧包时用）。
LEGACY_AS_OF_KEYS: dict[str, str] = {
    "price_latest": "price",
    "financial_latest": "financial",
    "indicator_latest": "indicator",
    "event_latest": "event",
}


# 不用 slots=True：@dataclass(slots=True) 在 3.12 会重建类并要求模块已登记进
# sys.modules；tests/regression 用 importlib 直接从路径加载本脚本（不登记）会崩，
# 因此这里保持普通 frozen dataclass（测试侧也补了 sys.modules 登记，见测试文件）。
@dataclass(frozen=True)
class TableSpec:
    """一张导出表的登记项——白名单的原子单位。

    库内表名与输出名分开：内部标识保持不变，包内用中性命名。
    `backend` 区分 DuckDB / SQLite 源（公告台账与交易日历只在 SQLite 里）。
    `freshness` 是「这张表应该跟到什么新鲜度」，滞后判定只对 daily / quarterly 生效，
    避免给分红、回购这类天然稀疏的事件表制造假告警。
    """

    layer: str
    table: str
    output: str
    source_class: str
    note: str
    backend: str = "duckdb"
    freshness: str = "event"


# 注意：note 里**不许写列数/行数/截止日期**——那些数字一旦写死就会和实际导出脱节
# （首版把 indicator_ext 记成 51 列、实际 60 列就是这么来的）。数字统一来自实测。
A_TABLES: list[TableSpec] = [
    TableSpec("A_updates", "balance_sheet", "balance_sheet", "混合",
              "资产负债表（历史段 + 本项目续写）", freshness="quarterly"),
    TableSpec("A_updates", "income_statement", "income_statement", "混合",
              "利润表（历史段 + 本项目续写）", freshness="quarterly"),
    TableSpec("A_updates", "cash_flow", "cash_flow", "混合",
              "现金流量表（历史段 + 本项目续写）", freshness="quarterly"),
    TableSpec("A_updates", "indicator_snapshot", "indicator_snapshot", "自算",
              "★ 自算主链快照（列数见本表 columns）", freshness="quarterly"),
    TableSpec("A_updates", "indicator_ext", "indicator_ext", "自算",
              "★ 自算扩展指标（列数见本表 columns）", freshness="quarterly"),
    # 2026-11-14 审查：本表已不是纯外部归档段——实测最新日期 2026-06-30 越过
    # 归档截止期 2025-03-31（2026-09-20 用东财 NOTICE_DATE 续写到季度精度），
    # 故来源标「混合」；否则 KNOWN_ISSUES 会永久误报“归档表越过截止期”。
    TableSpec("A_updates", "financial_report_dates", "financial_report_dates", "混合",
              "报告公布日期（外部归档段 + 本项目季度续写）", freshness="quarterly"),
    # 实测它的 report_date 里混着快照抓取日（同一"报告期"只有 2 行），不是真报告期，
    # 拿它跟三表报告期比会写出假告警，因此按事件表处理（最新日期仍会写进 README）。
    TableSpec("A_updates", "company_employee_history", "company_employee_history", "混合",
              "员工人数历史 + 当前快照"),
    # indicator_ext 的直接输入（缺了它们，包内指标无法复现/核验）
    TableSpec("A_updates", "cash_flow_indirect", "cash_flow_indirect", "混合",
              "间接法经营现金流（折旧摊销等，indicator_ext 输入）", freshness="quarterly"),
    TableSpec("A_updates", "cash_flow_activity", "cash_flow_activity", "混合",
              "现金流补充科目（资本支出等，indicator_ext 输入）", freshness="quarterly"),
    TableSpec("A_updates", "balance_sheet_ext", "balance_sheet_ext", "公开接口",
              "资产负债表补充科目（indicator_ext 输入）", freshness="quarterly"),
    TableSpec("A_updates", "csmar_disclosure_metrics", "disclosure_metrics", "历史归档",
              "官方披露指标（含加权 ROE）", freshness="frozen"),
    TableSpec("A_updates", "csmar_risk_factors", "risk_factors", "历史归档",
              "风险治理因子", freshness="frozen"),
]
B_TABLES: list[TableSpec] = [
    TableSpec("B_supplements", "price_daily_raw", "price_daily_raw", "自采",
              "★ 日线行情（原始价）", freshness="daily"),
    TableSpec("B_supplements", "price_daily_qfq", "price_daily_qfq", "自采",
              "★ 日线行情（前复权）", freshness="daily"),
    TableSpec("B_supplements", "xdxr", "xdxr", "自采", "除权除息（真实除权日）"),
    TableSpec("B_supplements", "dividends", "dividends", "自采", "分红事件"),
    TableSpec("B_supplements", "hk_dividends", "hk_dividends", "自采", "港股分红（A+H）"),
    TableSpec("B_supplements", "share_capital_history", "share_capital_history", "自采",
              "★ 股本变动明细"),
    TableSpec("B_supplements", "funding_events", "funding_events", "公开接口",
              "融资事件（东财 F10 为主）"),
    TableSpec("B_supplements", "buyback_events", "buyback_events", "公开接口", "回购注销"),
    TableSpec("B_supplements", "company_profile", "company_profile", "公开接口",
              "公司资料（含员工数）"),
    TableSpec("B_supplements", "business_breakdown", "business_breakdown", "公开接口",
              "主营构成", freshness="quarterly"),
    TableSpec("B_supplements", "treasury_yield_curve", "treasury_yield_curve", "自采",
              "国债收益率曲线（财政部）", freshness="daily"),
    TableSpec("B_supplements", "index_valuation", "index_valuation", "公开接口",
              "指数估值（乐咕/申万/中证）", freshness="daily"),
    TableSpec("B_supplements", "etf_daily", "etf_daily", "公开接口",
              "ETF 行情（同花顺）", freshness="daily"),
    TableSpec("B_supplements", "research_statistics", "research_statistics", "自算",
              "★ 历史统计域", freshness="quarterly"),
    TableSpec("B_supplements", "stock_meta", "stock_meta", "公开接口", "股票主数据",
              freshness="daily"),
    # 2026-11-14 补齐：SOURCES 一直声称"自采含公告台账"，包里却没有；交易日历缺失
    # 则别人无法复算连续性指标（审计 §3 ④⑤）。
    TableSpec("B_supplements", "announcement_registry", "announcement_registry", "自采",
              "★ 公告台账（CNINFO 巨潮，逐条公告登记）", backend="sqlite", freshness="daily"),
    TableSpec("B_supplements", "trading_dates", "trading_dates", "自采",
              "★ 交易日历（连续性/滞后天数复算必需）", backend="sqlite", freshness="daily"),
]
LINEAGE_TABLES: list[TableSpec] = [
    TableSpec("lineage", "fetch_batch", "fetch_batch", "自采",
              "抓取批次台账（每批一行的汇总溯源）", freshness="daily"),
    TableSpec("lineage", "source_audit", "source_audit", "自采",
              "★ 逐行溯源：每行数据来自哪个源/批次/时刻", freshness="daily"),
    TableSpec("lineage", "missing_list", "missing_list", "自采",
              "缺失清单（质量披露台账）", backend="sqlite"),
    TableSpec("lineage", "retry_list", "retry_list", "自采",
              "重试清单（质量披露台账）", backend="sqlite"),
]


class ExportRefusedError(RuntimeError):
    """导出被拒绝（写锁生效 / 个人数据混进白名单）。"""


class ExportLockedError(ExportRefusedError):
    """自动更新写锁生效——此刻导出会拿到半成品快照。"""


class PersonalDataLeakError(ExportRefusedError):
    """个人数据混进了导出白名单——白名单写错时必须当场炸掉，绝不静默导出。"""


def all_specs(with_lineage: bool = False) -> list[TableSpec]:
    """返回本次导出的白名单（--with-lineage 才追加溯源层）。"""
    specs = [*A_TABLES, *B_TABLES]
    if with_lineage:
        specs.extend(LINEAGE_TABLES)
    return specs


def assert_no_personal_tables(specs: list[TableSpec]) -> None:
    """导出前再断言一次个人数据纪律（白名单写错也能拦住）。"""
    leaked = sorted({spec.table for spec in specs} & EXCLUDED_PERSONAL_TABLES)
    if leaked:
        raise PersonalDataLeakError(f"个人数据出现在导出白名单里，拒绝导出：{leaked}")


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DATE_RE = re.compile(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _quoted(name: str) -> str:
    """把表/列名安全地包成双引号（内部双引号加倍）。"""
    return '"' + name.replace('"', '""') + '"'


def normalize_date(value: Any) -> str | None:
    """把 DATE / TIMESTAMP / 8 位数字日期统一成 'YYYY-MM-DD'；解析不了返回 None。

    为什么要统一：`announcement_registry.announcement_time` 是文本时间戳，
    `trade_date` 是 DATE，`trading_dates.trade_date` 又是文本——不归一化就
    没法放在同一张 as_of 表里比较。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    match = _DATE_RE.match(str(value).strip())
    if match is None:
        return None
    year, month, day = match.groups()
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def pick_date_column(columns: list[str]) -> str | None:
    """按优先级挑出这张表的「最新日期」来源列。"""
    lowered = {column.lower(): column for column in columns}
    for candidate in DATE_COLUMN_PRIORITY:
        if candidate in lowered:
            return lowered[candidate]
    return None


def days_between(earlier: str | None, later: str | None) -> int:
    """两个 'YYYY-MM-DD' 之间的自然日差（later - earlier），解析失败按 0 处理。"""
    try:
        if not earlier or not later:
            return 0
        return (date.fromisoformat(later) - date.fromisoformat(earlier)).days
    except ValueError:
        return 0


def trading_days_between(
    earlier: str | None, later: str | None, calendar: list[str] | None
) -> int:
    """(earlier, later] 区间内的交易日数；无日历时退化为工作日近似。"""
    if not earlier or not later or earlier >= later:
        return 0
    if calendar:
        return sum(1 for day in calendar if earlier < day <= later)
    start = date.fromisoformat(earlier)
    end = date.fromisoformat(later)
    return sum(
        1
        for offset in range(1, (end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
    )


def _fmt_bytes(size: int | None) -> str:
    if not size:
        return "0 MB"
    return f"{size / 1024 / 1024:.1f} MB"


def _index_by_output(entries: dict[str, dict]) -> dict[str, dict]:
    """包内文件键 '层/名' → 以「输出名」为键，方便按表名查。"""
    return {key.rsplit("/", 1)[-1]: value for key, value in entries.items()}


# --------------------------------------------------------------------------- #
# 数据源读取：DuckDB / SQLite 共用一套探测口径
# --------------------------------------------------------------------------- #
class _Reader:
    """表结构 / 最新日期 / 覆盖率 / 落盘的统一入口。

    为什么收敛成一个类：README、SOURCES、manifest 里的每个数字都由这里产出，
    两个后端各写一份探测逻辑，迟早出现「SQLite 表的最新日期算法和 DuckDB 不同」，
    文档随即失真。这里只读：DuckDB 连接由调用方以 read_only 打开，SQLite 用 mode=ro。
    """

    def __init__(
        self, *, backend: str, duck: Any = None, sqlite_path: Path | None = None
    ) -> None:
        if backend not in {"duckdb", "sqlite"}:
            raise ValueError(f"未知导出后端: {backend}")
        self.backend = backend
        self._duck = duck
        self._sqlite_path = sqlite_path
        self._sqlite: sqlite3.Connection | None = None

    def close(self) -> None:
        if self._sqlite is not None:
            self._sqlite.close()
            self._sqlite = None

    @property
    def sqlite(self) -> sqlite3.Connection:
        """按需打开只读 SQLite 连接（mode=ro，连 WAL 都不会触发写入）。"""
        if self._sqlite is None:
            if self._sqlite_path is None:
                raise RuntimeError("SQLite 后端缺少 sqlite_path")
            self._sqlite = sqlite3.connect(f"file:{self._sqlite_path}?mode=ro", uri=True)
        return self._sqlite

    def _cursor(self, sql: str) -> Any:
        return self.sqlite.execute(sql) if self.backend == "sqlite" else self._duck.execute(sql)

    def columns(self, table: str) -> list[str]:
        if self.backend == "sqlite":
            pragma = f"PRAGMA table_info({_quoted(table)})"
            return [str(row[1]) for row in self._cursor(pragma).fetchall()]
        description = self._duck.execute(f"SELECT * FROM {_quoted(table)} LIMIT 0").description
        return [str(column[0]) for column in description]

    def row(self, sql: str) -> tuple:
        fetched = self._cursor(sql).fetchone()
        return (None,) if fetched is None else tuple(fetched)

    def scalar(self, sql: str) -> Any:
        return self.row(sql)[0]

    def rows(self, sql: str) -> list[tuple]:
        return [tuple(item) for item in self._cursor(sql).fetchall()]

    def export(self, table: str, temporary: Path) -> None:
        """把一张表整表写到 temporary（parquet/zstd），由调用方原子改名。"""
        if self.backend == "sqlite":
            import pandas as pd

            frame = pd.read_sql_query(f"SELECT * FROM {_quoted(table)}", self.sqlite)
            frame.to_parquet(temporary, compression="zstd", index=False)
            return
        quoted = str(temporary).replace("'", "''")
        self._duck.execute(
            f"COPY (SELECT * FROM {_quoted(table)}) TO '{quoted}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )

    def latest_date(self, table: str, column: str) -> str | None:
        raw = self.scalar(f"SELECT CAST(MAX({_quoted(column)}) AS VARCHAR) FROM {_quoted(table)}")
        return normalize_date(raw)

    def period_coverage(
        self, table: str, columns: list[str], date_column: str, latest: str | None
    ) -> dict[str, Any] | None:
        """最新日期那一期（当天）的逐列非空覆盖率。

        为什么只看最新一期：审查里"字段 X 在当前报告期无数据"就是这么定义的
        （119 §2.3 的 fixed_assets 1.8% / deducted_net_profit 0%）。只看当期
        还能把开销压在几千行上——全表逐列 COUNT 会把 17M 行行情再扫一遍。
        """
        if latest is None:
            return None
        try:
            window_hi = (date.fromisoformat(latest) + timedelta(days=1)).isoformat()
        except ValueError:
            return None
        safe_columns = [column for column in columns if _IDENTIFIER_RE.match(column)]
        if not safe_columns:
            return None
        selects = ", ".join(["COUNT(*)"] + [f"COUNT({_quoted(c)})" for c in safe_columns])
        sql = (
            f"SELECT {selects} FROM {_quoted(table)} WHERE {_quoted(date_column)} >= '{latest}' "
            f"AND {_quoted(date_column)} < '{window_hi}'"
        )
        row = self.row(sql)
        total = int(row[0] or 0)
        # 期间行为 0 说明日期列不是可比较的 ISO 文本（或最新值落在畸形数据上）：
        # 此时宁可不给覆盖率，也不能凭空写"字段无数据"。
        if total == 0:
            return {"period_rows": 0, "coverage": {}}
        coverage = {
            column: round(int(count or 0) / total, 4)
            for column, count in zip(safe_columns, row[1:], strict=False)
        }
        return {"period_rows": total, "coverage": coverage}


def _export_one(reader: _Reader, spec: TableSpec, root: Path) -> dict:
    """导出一张表并采集它的可用性元信息（行数/列数/最新日期/覆盖率/哈希）。"""
    import pyarrow.parquet as pq

    layer_dir = root / spec.layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    target = layer_dir / f"{spec.output}.parquet"
    temporary = layer_dir / f".{spec.output}.{uuid.uuid4().hex}.tmp"
    try:
        reader.export(spec.table, temporary)
        temporary.replace(target)
    finally:
        # replace 成功后这里是 no-op；失败则清掉半成品，绝不把残片留在包里。
        temporary.unlink(missing_ok=True)

    metadata = pq.ParquetFile(target).metadata
    columns = reader.columns(spec.table)
    date_column = pick_date_column(columns)
    latest = reader.latest_date(spec.table, date_column) if date_column else None
    return {
        "file": f"{spec.layer}/{spec.output}.parquet",
        "table": spec.table,
        "backend": spec.backend,
        "rows": int(metadata.num_rows),
        "columns": int(metadata.num_columns),
        "size_bytes": target.stat().st_size,
        "sha256": _sha256(target),
        "date_column": date_column,
        "latest_date": latest,
        "freshness": spec.freshness,
        "field_coverage": reader.period_coverage(
            spec.table, columns, date_column, latest
        )
        if date_column
        else None,
        "source_class": spec.source_class,
        "note": spec.note,
    }


def _load_trading_calendar(reader: _Reader) -> list[str]:
    """读交易日历（用于把"落后多少天"换算成交易日）。读不到就返回空表。"""
    try:
        rows = reader.rows('SELECT "trade_date" FROM "trading_dates" ORDER BY "trade_date"')
    except sqlite3.Error:
        return []
    return [day for day in (normalize_date(row[0]) for row in rows) if day]


def _probe_history_segment(reader: _Reader, entries: dict[str, dict]) -> dict[str, dict]:
    """探测三表「外部归档段 → 本项目续写段」的分界。

    分界日 2025-03-31 是外部数据包的固定截止期（已知事实），能被实测验证的
    是「归档段实际最晚报告期」「续写段最早/最新报告期」——这三项才写进文档。
    """
    by_output = _index_by_output(entries)
    segments: dict[str, dict] = {}
    for spec in A_TABLES:
        if spec.output not in {"balance_sheet", "income_statement", "cash_flow"}:
            continue
        if spec.output not in by_output or spec.backend != "duckdb":
            continue
        table = _quoted(spec.table)
        segments[spec.output] = {
            "archive_latest": normalize_date(
                reader.scalar(
                    f"SELECT CAST(MAX(\"report_date\") AS VARCHAR) FROM {table} "
                    f"WHERE \"report_date\" <= '{HISTORY_ARCHIVE_CUTOFF}'"
                )
            ),
            "live_earliest": normalize_date(
                reader.scalar(
                    f"SELECT CAST(MIN(\"report_date\") AS VARCHAR) FROM {table} "
                    f"WHERE \"report_date\" > '{HISTORY_ARCHIVE_CUTOFF}'"
                )
            ),
            "live_latest": normalize_date(
                reader.scalar(f"SELECT CAST(MAX(\"report_date\") AS VARCHAR) FROM {table}")
            ),
        }
    return segments


# --------------------------------------------------------------------------- #
# 包级 as_of 与 KNOWN_ISSUES（规则生成）
# --------------------------------------------------------------------------- #
def build_as_of(entries: dict[str, dict]) -> dict[str, dict]:
    """按域汇总包级 as_of：价格 / 三表 / 指标 / 事件。

    域最新日期 = 成员实测最新日期取最大；每张成员的最新日期一起带上，
    这样"域看着很新、里面有一张表很旧"一眼可见（etf_daily 落后个股行情就是这样）。
    """
    by_output = _index_by_output(entries)
    as_of: dict[str, dict] = {}
    for domain, members in DOMAIN_MEMBERS.items():
        present = {
            name: by_output[name]["latest_date"] for name in members if name in by_output
        }
        known = [value for value in present.values() if value]
        as_of[domain] = {"latest_date": max(known) if known else None, "tables": present}
    return as_of


def build_known_issues(
    *,
    entries: dict[str, dict],
    as_of: dict[str, dict],
    calendar: list[str],
    skipped: list[str],
    history: dict[str, dict],
) -> list[str]:
    """规则生成 KNOWN_ISSUES —— 出现的每个数字都来自本次导出实测。

    首版这里是手写常量，重导出从不纠正，于是出现「年报公布日仅年度频率」
    （实际已是季度精度）、「indicator_ext 51 列」（实际 60 列）这类失真。
    """
    issues: list[str] = []
    by_output = _index_by_output(entries)
    price_anchor = as_of.get("price", {}).get("latest_date")
    financial_anchor = as_of.get("financial", {}).get("latest_date")

    # R1 外部历史归档段：只有真的探测到归档段（≤ 截止期有行）才写这条，
    # 否则会出现「归档段实测最晚报告期：无」这种自相矛盾的句子。
    if history:
        sample = history.get("income_statement") or next(iter(history.values()))
        archive_latest = sample.get("archive_latest")
        live_earliest = sample.get("live_earliest")
        live_latest = sample.get("live_latest")
        if archive_latest and live_earliest:
            issues.append(
                f"财务三表历史段（≤ {HISTORY_ARCHIVE_CUTOFF}）来自外部数据包："
                f"归档段实测最晚报告期 {archive_latest}，"
                f"本项目续写段最早 {live_earliest}、最新 {live_latest}；"
                "续写段质量取决于当期源"
            )
        elif archive_latest:
            issues.append(
                f"财务三表仅含外部归档段（≤ {HISTORY_ARCHIVE_CUTOFF}），"
                f"实测最晚报告期 {archive_latest}：本项目续写段尚无数据"
            )
    for name, entry in sorted(by_output.items()):
        if entry.get("source_class") != "历史归档":
            continue
        latest = entry.get("latest_date")
        scope = f"{entry.get('rows'):,} 行 / {entry.get('columns')} 列"
        if latest and latest > HISTORY_ARCHIVE_CUTOFF:
            # 标了"历史归档"却越过截止期 —— 标签与实测不符，必须报出来（审查 §3 ①
            # 的"公布日仅年度频率"其实就是这么来的：实际已含本项目续写的季度数据）。
            issues.append(
                f"{name} 标注来源为「历史归档」，但实测最新日期 {latest} 已超过归档截止期 "
                f"{HISTORY_ARCHIVE_CUTOFF}（{scope}）：该表实际含本项目续写，来源标注应改为「混合」"
            )
        else:
            issues.append(
                f"{name} 只含外部数据包归档段（≤ {HISTORY_ARCHIVE_CUTOFF}），"
                f"实测最新日期 {latest or '未探测到'}（{scope}），不含本项目续写"
            )

    # R2 滞后判定：daily 比价格域、quarterly 比三表报告期；事件表与冻结表不比。
    for name, entry in sorted(by_output.items()):
        latest = entry.get("latest_date")
        freshness = entry.get("freshness")
        if not latest:
            continue
        if freshness == "daily" and price_anchor:
            lag = trading_days_between(latest, price_anchor, calendar)
            if lag > LAG_TRADING_DAYS:
                issues.append(
                    f"{name} 滞后：最新日期 {latest} 落后主数据（价格域 {price_anchor}）"
                    f"{lag} 个交易日"
                )
        elif freshness == "quarterly" and financial_anchor and latest < financial_anchor:
            lag = trading_days_between(latest, financial_anchor, calendar)
            issues.append(
                f"{name} 滞后：最新报告期 {latest} 落后三表最新报告期 {financial_anchor}"
                f"（{lag} 个交易日）；按报告期 join 的筛选在该期无值"
            )

    # R3 字段覆盖率：0% → 当期无数据；不足阈值 → 塌陷告警。
    for name, entry in sorted(by_output.items()):
        coverage_block = entry.get("field_coverage") or {}
        coverage = coverage_block.get("coverage") or {}
        if not coverage:
            continue
        period = coverage_block.get("period_rows") or 0
        if period < MIN_COVERAGE_ROWS:
            continue
        zeros = sorted(column for column, ratio in coverage.items() if ratio == 0)
        if zeros:
            listed = "、".join(zeros[:MAX_ISSUE_COLUMNS])
            more = len(zeros) - len(zeros[:MAX_ISSUE_COLUMNS])
            suffix = f" 等 {len(zeros)} 个字段" if more > 0 else ""
            issues.append(
                f"{name} 在最新日期 {entry['latest_date']} 这一期（{period} 行）"
                f"字段 {listed}{suffix} 覆盖率为 0%：该期无数据"
            )
        weak = sorted(
            (column, ratio)
            for column, ratio in coverage.items()
            if 0 < ratio < COVERAGE_COLLAPSE
        )
        if weak:
            listed = "、".join(
                f"{column} {ratio:.1%}" for column, ratio in weak[:MAX_ISSUE_COLUMNS]
            )
            more = len(weak) - len(weak[:MAX_ISSUE_COLUMNS])
            suffix = f" 等 {len(weak)} 个字段" if more > 0 else ""
            issues.append(
                f"{name} 在最新日期 {entry['latest_date']} 这一期覆盖率不足 "
                f"{COVERAGE_COLLAPSE:.0%}：{listed}{suffix}（数据源缺项，非口径问题）"
            )

    # R4 本次导出跳过的表（导出报错不能悄悄吞掉）。
    if skipped:
        issues.append(f"本次导出跳过 {len(skipped)} 张表（导出报错，详见导出日志）：{'、'.join(skipped)}")

    # R5 固定纪律条款（不含任何随数据变化的数字）。
    issues.append(
        "本包不含个人数据（自选/筛选规则/ETF 流水/预算等），导出面为显式白名单；"
        "missing_list / retry_list 是数据质量台账，不含用户输入"
    )
    return issues


# --------------------------------------------------------------------------- #
# 文档生成（README / SOURCES / load.sql）
# --------------------------------------------------------------------------- #
def render_load_sql(entries: dict[str, dict]) -> str:
    """生成 load.sql —— 必须按 entry 的 file 字段（含扩展名）判定 parquet。

    2026-09-19 首版用 dict 的**键**（无扩展名）做 endswith('.parquet') 判断，
    结果 load.sql 全空，这里保留 value.file 判定并加回归测试。
    """
    parquet_files = sorted(
        str(v["file"]) for v in entries.values() if str(v.get("file", "")).endswith(".parquet")
    )
    lines = ["-- VD 数据包一键装载（DuckDB）", "-- 用法: duckdb vd.duckdb < load.sql", ""]
    for relative in parquet_files:
        view = relative.replace("/", "_").removesuffix(".parquet")
        lines.append(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{relative}');")
    return "\n".join(lines) + "\n"


def _entry_latest(entry: dict) -> str:
    latest = entry.get("latest_date") or "未探测到"
    column = entry.get("date_column")
    return f"{latest}（{column}）" if column and entry.get("latest_date") else latest


def render_readme(manifest: dict) -> str:
    """README 完全由 manifest 生成：行数/列数/日期没有一个手写常量。"""
    entries = {k: v for k, v in manifest["files"].items() if str(v.get("file", "")).endswith(".parquet")}
    as_of = manifest["as_of"]
    total_bytes = sum(int(v.get("size_bytes") or 0) for v in entries.values())
    lines = [
        f"# VD 数据包 {manifest['package_version']}",
        "",
        f"由 value-dashboard 导出于 {manifest['exported_at'][:19]}（UTC）。",
        "",
        "本文件在导出结束时由 `scripts/export_data_package.py` **自动生成**：其中的行数、"
        "列数、最新日期全部取自本次实际导出，重导出即自动更新（首版这些数字是手写常量，"
        "重导出不会纠正）。",
        "",
        "## 包级 as_of（分域）",
        "",
        "| 域 | 最新日期 | 覆盖表（各自最新日期） |",
        "|---|---|---|",
    ]
    for domain, block in as_of.items():
        members = "、".join(
            f"`{name}`={value or '—'}" for name, value in block.get("tables", {}).items()
        )
        lines.append(
            f"| {DOMAIN_LABELS.get(domain, domain)} | {block.get('latest_date') or '未探测到'} | "
            f"{members or '—'} |"
        )
    lines += [
        "",
        "## 表清单",
        "",
        "| 层 | 表 | 行数 | 列数 | 最新日期（日期列） | 更新期望 | 来源性质 |",
        "|---|---|---:|---:|---|---|---|",
    ]
    for key, entry in sorted(entries.items()):
        name = key.rsplit("/", 1)[-1]
        lines.append(
            f"| {entry['file'].split('/', 1)[0]} | `{name}` | {entry['rows']:,} | "
            f"{entry['columns']} | {_entry_latest(entry)} | {entry.get('freshness') or '—'} | "
            f"{entry.get('source_class') or '—'} |"
        )
    by_layer: dict[str, int] = {}
    for entry in entries.values():
        layer = entry["file"].split("/", 1)[0]
        by_layer[layer] = by_layer.get(layer, 0) + 1
    by_class: dict[str, int] = {}
    for entry in entries.values():
        by_class[entry.get("source_class") or "—"] = (
            by_class.get(entry.get("source_class") or "—", 0) + 1
        )
    lines += [
        "",
        "## 规模",
        "",
        f"- 表数：{len(entries)}",
        f"- 合计大小：{_fmt_bytes(total_bytes)}",
        f"- 分层：{'、'.join(f'{k} {v} 张' for k, v in sorted(by_layer.items()))}",
        f"- 来源构成：{'、'.join(f'{k} {v} 张' for k, v in sorted(by_class.items()))}",
        "",
        "## 快速使用",
        "",
        "```bash",
        "duckdb vd.duckdb < load.sql     # 建好全部视图，直接用 SQL 查",
        "```",
        "",
        "## 已知问题（规则生成，见 manifest.json 的 known_issues）",
        "",
    ]
    lines += [f"- {item}" for item in manifest["known_issues"]]
    lines += ["", "## 未包含", ""]
    lines += [f"- {item}" for item in manifest["not_included"]]
    lines.append("")
    return "\n".join(lines)


def render_sources(manifest: dict) -> str:
    """SOURCES 只写本次真正进包的表 —— 不再出现"声称有、包里没有"的来源说明。"""
    entries = {k: v for k, v in manifest["files"].items() if str(v.get("file", "")).endswith(".parquet")}
    lines = [
        "# 数据来源说明",
        "",
        "本文件由导出脚本按**实际导出结果**生成：只列本次真正进包的表，"
        "行数与最新日期取自导出实测。来源性质是数据描述，不构成任何使用限制。",
        "",
    ]
    for source_class, description in SOURCE_CLASSES.items():
        members = {
            name: entry
            for name, entry in entries.items()
            if entry.get("source_class") == source_class
        }
        lines += [f"## {source_class}", "", description, ""]
        if not members:
            lines += ["（本次导出无此来源的表）", ""]
            continue
        lines += ["| 表 | 行数 | 最新日期（日期列） | 说明 |", "|---|---:|---|---|"]
        for key, entry in sorted(members.items()):
            name = key.rsplit("/", 1)[-1]
            lines.append(
                f"| `{name}` | {entry['rows']:,} | {_entry_latest(entry)} | "
                f"{entry.get('note') or '—'} |"
            )
        lines.append("")
    segments = manifest.get("history_segments") or {}
    if segments:
        lines += ["## 历史归档段与续写段（实测分界）", ""]
        lines.append(
            f"外部数据包截止报告期为 {HISTORY_ARCHIVE_CUTOFF}；下表是导出实测的分界，"
            "归档段与续写段在库内拼接，续写段由本项目爬虫增量补齐。"
        )
        lines += ["", "| 表 | 归档段最晚报告期 | 续写段最早 | 续写段最新 |", "|---|---|---|---|"]
        for name, block in sorted(segments.items()):
            lines.append(
                f"| `{name}` | {block.get('archive_latest') or '—'} | "
                f"{block.get('live_earliest') or '—'} | {block.get('live_latest') or '—'} |"
            )
        lines.append("")
    lines += ["## 未包含", ""]
    lines += [f"- {item}" for item in manifest["not_included"]]
    lines.append("")
    return "\n".join(lines)


def _write_text(path: Path, content: str) -> dict:
    path.write_text(content, encoding="utf-8")
    return {"file": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)}


# --------------------------------------------------------------------------- #
# 导出主流程
# --------------------------------------------------------------------------- #
def build_package(
    paths: DatabasePathSet,
    root: Path,
    *,
    todo: list[TableSpec] | None = None,
    with_lineage: bool = False,
    now: datetime | None = None,
) -> dict:
    """导出数据包到 root，返回写出的 manifest（文档全部按实测生成）。"""
    specs = list(todo) if todo is not None else all_specs(with_lineage)
    assert_no_personal_tables(specs)
    if any_write_lock_active(paths.duckdb_path):
        raise ExportLockedError(
            "检测到自动更新写锁生效——此时导出的会是半成品快照。请等更新结束后重试。"
        )

    exported_at = (now or datetime.now(UTC)).isoformat()
    root.mkdir(parents=True, exist_ok=True)

    entries: dict[str, dict] = {}
    skipped: list[str] = []
    sqlite_reader = _Reader(backend="sqlite", sqlite_path=paths.sqlite_path)
    duck = DuckDBStore(paths=paths)
    try:
        with duck.read_connection() as connection:
            duck_reader = _Reader(backend="duckdb", duck=connection)
            for spec in specs:
                reader = sqlite_reader if spec.backend == "sqlite" else duck_reader
                try:
                    entries[f"{spec.layer}/{spec.output}"] = _export_one(reader, spec, root)
                    logger.info("  已导出 %-24s → %s", spec.output, spec.layer)
                except Exception as error:  # noqa: BLE001 - 单表失败不能拖垮整包
                    skipped.append(f"{spec.output}（{error}）")
                    logger.warning("  跳过 %s：%s", spec.table, error)
            as_of = build_as_of(entries)
            calendar = _load_trading_calendar(sqlite_reader)
            history = _probe_history_segment(duck_reader, entries)
    finally:
        sqlite_reader.close()

    known_issues = build_known_issues(
        entries=entries,
        as_of=as_of,
        calendar=calendar,
        skipped=skipped,
        history=history,
    )
    files: dict[str, dict] = dict(entries)
    manifest: dict[str, Any] = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "package_version": PACKAGE_VERSION,
        "exported_at": exported_at,
        "source_project": "value-dashboard",
        "as_of": as_of,
        "source_classes": SOURCE_CLASSES,
        "contents": CONTENTS,
        "history_segments": history,
        "trading_calendar_rows": len(calendar),
        # 本次"打算"导出的表（含失败跳过的）：--verify 用它核对完整性，
        # 首版包没有这个字段时按默认 A+B 白名单核对。
        "expected_tables": [f"{spec.layer}/{spec.output}" for spec in specs],
        "files": files,
        "known_issues": known_issues,
        "not_included": NOT_INCLUDED,
    }
    # 文档在 manifest 骨架之上生成：它们读的是同一份实测结果，不会再各自抄一遍数字。
    files["README.md"] = _write_text(root / "README.md", render_readme(manifest))
    files["SOURCES.md"] = _write_text(root / "SOURCES.md", render_sources(manifest))
    files["load.sql"] = _write_text(root / "load.sql", render_load_sql(entries))
    # manifest.json 无法内嵌自身地址：`self` 标记让 --verify 只校验存在性。
    files["manifest.json"] = {
        "file": "manifest.json",
        "self": True,
        "note": "自引用：manifest 无法记录自身的 sha256",
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    parquet_count = sum(1 for v in entries.values() if str(v.get("file", "")).endswith(".parquet"))
    logger.info("数据包构建完成：%s", root)
    logger.info("  表 %d 张 / 合计 %s", parquet_count, _fmt_bytes(
        sum(int(v.get("size_bytes") or 0) for v in entries.values())
    ))
    logger.info("  已知问题 %d 条（规则生成）", len(known_issues))
    return manifest


# --------------------------------------------------------------------------- #
# 对账模式（--verify）：纯只读
# --------------------------------------------------------------------------- #
def _spec_registry(with_lineage: bool = True) -> dict[str, TableSpec]:
    """包内输出名 → 登记项，供 --verify 回头找库内表名与后端。"""
    return {spec.output: spec for spec in all_specs(with_lineage)}


def _current_latest_date(reader: _Reader, table: str) -> tuple[str | None, str | None, int]:
    """当前库里这张表的最新日期 / 日期列 / 行数（探测口径与导出完全一致）。"""
    columns = reader.columns(table)
    date_column = pick_date_column(columns)
    latest = reader.latest_date(table, date_column) if date_column else None
    rows = int(reader.scalar(f"SELECT COUNT(*) FROM {_quoted(table)}") or 0)
    return latest, date_column, rows


def verify_package(package_dir: Path, *, paths: DatabasePathSet | None = None) -> int:
    """只读对账。返回码：0 全部通过；2 有库内漂移（不致命）；1 有文件不一致（致命）。

    分两级是有意的：文件改坏/缺失是「这个包不可信」，必须拦住；库里又长出新数据
    只是「这个包该重导了」，不该让下游脚本红着退出。
    """
    import pyarrow.parquet as pq

    root = Path(package_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        logger.error("对账失败：%s 下没有 manifest.json", root)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files") or {}

    fatal: list[str] = []
    verified_files = 0
    for key, entry in sorted(files.items()):
        relative = str(entry.get("file") or key)
        path = root / relative
        if not path.is_file():
            fatal.append(f"{key}: 文件缺失（{relative}）")
            continue
        # manifest.json 无法记录自身 sha256（写进内容就变了），因此这个条目一律
        # 只校验存在性——首版 manifest 也把 manifest.json 的 sha 写成了"落盘前的
        # 草稿"，对账时不该把结构性自引用报成文件损坏。
        if entry.get("self") or relative.endswith("manifest.json"):
            continue
        verified_files += 1
        size = path.stat().st_size
        if entry.get("size_bytes") is not None and size != int(entry["size_bytes"]):
            fatal.append(
                f"{key}: 大小不一致（manifest {entry['size_bytes']} / 实际 {size}）"
            )
        if relative.endswith(".parquet"):
            metadata = pq.ParquetFile(path).metadata
            if entry.get("rows") is not None and int(metadata.num_rows) != int(entry["rows"]):
                fatal.append(
                    f"{key}: 行数不一致（manifest {entry['rows']} / 实际 {metadata.num_rows}）"
                )
            if entry.get("columns") is not None and int(metadata.num_columns) != int(
                entry["columns"]
            ):
                fatal.append(
                    f"{key}: 列数不一致（manifest {entry['columns']} / 实际 "
                    f"{metadata.num_columns}）"
                )
        if entry.get("sha256"):
            actual = _sha256(path)
            if actual != entry["sha256"]:
                fatal.append(
                    f"{key}: sha256 不一致（manifest {entry['sha256'][:12]}… / "
                    f"实际 {actual[:12]}…）"
                )

    logger.info("对账：%s", root)
    logger.info("  文件完整性：校验 %d 个文件，%d 项不一致", verified_files, len(fatal))
    for problem in fatal:
        logger.error("  ✗ %s", problem)

    drift: list[str] = []
    # 完整性：manifest 声明的表必须真的在包里。旧包没有 expected_tables 字段时按
    # 当前 A+B 白名单核对——审查 §3 ④⑤（公告台账/交易日历"说有却没有"）就是
    # 靠这条自动抓出来的。
    expected = manifest.get("expected_tables")
    if not expected:
        expected = [f"{spec.layer}/{spec.output}" for spec in all_specs()]
        logger.info("  包未声明 expected_tables（首版格式），按默认 A+B 白名单核对完整性")
    missing = [key for key in expected if key not in files]
    logger.info("  完整性：应含 %d 张表，缺 %d 张", len(expected), len(missing))
    for key in missing:
        # 明细统一在下面的「库内漂移」清单里打印，避免同一件事报两遍
        drift.append(f"缺表：{key}（白名单里有，包内没有）")
    registry = _spec_registry(with_lineage=True)
    if paths is None:
        logger.warning("  未提供库路径，跳过「库内漂移」检查")
    else:
        drift.extend(_collect_drift(manifest, paths, registry))
        logger.info("  库内漂移：%d 项（逐表滞后 / 缺表 / as_of 合计）", len(drift))
        for line in drift:
            logger.warning("  ~ %s", line)

    if fatal:
        return 1
    return 2 if drift else 0


def _collect_drift(
    manifest: dict, paths: DatabasePathSet, registry: dict[str, TableSpec]
) -> list[str]:
    """逐表比较「manifest 记录的日期/行数」与「当前库实测」，输出落后天数。"""
    drift: list[str] = []
    files = manifest.get("files") or {}
    sqlite_reader = _Reader(backend="sqlite", sqlite_path=paths.sqlite_path)
    duck = DuckDBStore(paths=paths)
    try:
        with duck.read_connection() as connection:
            duck_reader = _Reader(backend="duckdb", duck=connection)
            for key, entry in sorted(files.items()):
                if not str(entry.get("file") or "").endswith(".parquet"):
                    continue
                output = key.rsplit("/", 1)[-1]
                spec = registry.get(output)
                if spec is None:
                    logger.info("  ? %s 不在导出白名单里，跳过漂移检查", key)
                    continue
                reader = sqlite_reader if spec.backend == "sqlite" else duck_reader
                try:
                    latest, _column, rows = _current_latest_date(reader, spec.table)
                except Exception as error:  # noqa: BLE001 - 库内表结构变化也属于"查不了"
                    logger.warning("  ? %s 无法读取（%s），跳过漂移检查", key, error)
                    continue
                packaged_latest = entry.get("latest_date")
                packaged_rows = entry.get("rows")
                lag_days = days_between(packaged_latest, latest) if packaged_latest else 0
                row_delta = rows - int(packaged_rows) if packaged_rows is not None else 0
                logger.info(
                    "    %-26s 包 %-10s → 库 %-10s %s / 行数 %+d",
                    output,
                    packaged_latest or "—",
                    latest or "—",
                    f"落后 {lag_days:3d} 天" if packaged_latest else "未记录日期",
                    row_delta,
                )
                reasons: list[str] = []
                if packaged_latest and lag_days > 0:
                    reasons.append(
                        f"落后 {lag_days} 天（包 {packaged_latest} → 库 {latest}）"
                    )
                if row_delta != 0:
                    reasons.append(f"行数 {packaged_rows} → {rows}（{row_delta:+d}）")
                if reasons:
                    drift.append(f"{output}: " + "，".join(reasons))
            for domain, block in _manifest_domains(manifest).items():
                packaged = block.get("latest_date")
                current = _domain_latest(duck_reader, domain)
                lag = days_between(packaged, current) if packaged else 0
                logger.info(
                    "    as_of[%s] 包 %-10s → 库 %-10s 落后 %3d 天",
                    domain,
                    packaged or "—",
                    current or "—",
                    lag,
                )
                if lag > 0:
                    drift.append(
                        f"as_of[{domain}]: 落后 {lag} 天（包 {packaged} → 库 {current}）"
                    )
    finally:
        sqlite_reader.close()
    return drift


def _manifest_domains(manifest: dict) -> dict[str, dict]:
    """把 manifest 的 as_of 归一成 {'price': {'latest_date': ...}, ...}。

    首版 manifest 是扁平字符串（price_latest 等），--verify 必须能读旧包。
    """
    domains: dict[str, dict] = {}
    for key, value in (manifest.get("as_of") or {}).items():
        domain = LEGACY_AS_OF_KEYS.get(key, key)
        if isinstance(value, dict):
            domains[domain] = {"latest_date": value.get("latest_date")}
        elif isinstance(value, str):
            domains[domain] = {"latest_date": normalize_date(value)}
    return domains


def _domain_latest(reader: _Reader, domain: str) -> str | None:
    """当前库里某个域的最新日期（成员取最大）。"""
    registry = _spec_registry(with_lineage=True)
    dates: list[str] = []
    for name in DOMAIN_MEMBERS.get(domain, ()):
        spec = registry.get(name)
        if spec is None or spec.backend != "duckdb":
            continue
        try:
            latest, _column, _rows = _current_latest_date(reader, spec.table)
        except Exception:  # noqa: BLE001 - 缺表就当它没有数据
            continue
        if latest:
            dates.append(latest)
    return max(dates) if dates else None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="构建 / 核对 VD 数据包")
    ap.add_argument("--out", type=Path, default=None, help="输出目录")
    ap.add_argument("--plan", action="store_true", help="只列计划，不导出")
    ap.add_argument("--with-lineage", action="store_true", help="含溯源层（fetch_batch + 台账）")
    ap.add_argument("--verify", action="store_true", help="对账模式：核对已有包，不写任何文件")
    ap.add_argument("--pkg", type=Path, default=None, help="与 --verify 搭配的包目录")
    args = ap.parse_args(argv)

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    if args.verify:
        if args.pkg is None:
            ap.error("--verify 需要同时给 --pkg <包目录>")
        return verify_package(args.pkg, paths=paths)

    todo = all_specs(args.with_lineage)
    if args.plan:
        logger.info("数据包计划：%d 张表", len(todo))
        for spec in todo:
            logger.info(
                "  [%s] %-22s %-14s %-9s %s",
                spec.source_class,
                spec.table,
                spec.layer,
                spec.backend,
                spec.note,
            )
        logger.info(
            "输出目录默认：<项目>/.planning/vd-datapackage-%s-<导出日>", PACKAGE_VERSION
        )
        return 0

    stamp = datetime.now().strftime("%Y%m%d")
    root = args.out or (PROJECT_ROOT / ".planning" / f"vd-datapackage-{PACKAGE_VERSION}-{stamp}")
    try:
        build_package(paths, root, todo=todo)
    except ExportRefusedError as error:
        logger.error("%s", error)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
