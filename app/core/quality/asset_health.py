"""资产健康检查（asset health）—— 把一次性人工审查固化成每轮可复跑的规则引擎。

为什么需要（背景：`docs/reports/119` 与 `docs/runbooks/ops-knowledge-base.md` D28–D33）
----------------------------------------------------------------------------------
2026-11-14 的数据包价值兑现度审查找出的每一个问题（扩展指标冻结在旧报告期、
扣非净利润归零、字段覆盖率塌陷、手工链不跑、数据包过期、schema 漂移、
零引用资产……），当时**全部只能靠人去库上手工查**。人工审完就散会，问题
下个季度原样回潮。本模块把这些判定写成 12 条可复跑的规则（编号即报告编号），
每轮更新后由 `vd data doctor` 与数据状态页自动执行。

规则清单（code → 判定 → 阈值）
------------------------------
 1 EXT_PERIOD_MISMATCH        P1  max(indicator_ext.report_date) ≠ max(indicator_snapshot.report_date)
 2 FIELD_COVERAGE_LOW         P1  上界面字段最新报告期覆盖率 < 90%
 3 FIELD_COVERAGE_DROP        P1  同字段本期 vs 上一期基线跌幅 > 10pp 或 > 50%
 4 MANUAL_CHAIN_STALE         P1  手工派生链最后成功时间超过该链 stale_after_days
 5 SNAPSHOT_PERIOD_GAP        P1  复用 snapshot_period_mismatches()，有缺口即告警
 6 STATIC_TABLE_STALE         P2  company_employee_history / financial_report_dates 落后应有报告期
 7 PACKAGE_STALE              P2  数据包 manifest.as_of.price_latest 落后行情 > 5 交易日
 8 NULL_REASON_MISSING        P2  上界面字段有 NULL 但 indicator_null_reason 登记率 < 80%
 9 CROSSCHECK_BELOW_THRESHOLD P2  run_crosscheck() 一致率 < 85%
10 SCHEMA_DDL_DRIFT           P1  schema.py DDL 表名 vs 库内实际表，双向差集非空
11 ZERO_REFERENCE_ASSET      P3  零引用资产清单（config/asset_reference.yaml）如实披露
12 DEDUCTED_PROFIT_GAP        P1  income_statement 最新报告期 deducted_net_profit 覆盖率 < 50%

设计约定（为什么这样写）
------------------------
1. **只读**：引擎只发 SELECT。唯一写路径 `write_field_coverage_history()` 是
   opt-in（`vd data doctor --write-history`），且写前自检
   `update_lock.any_write_lock_active()` —— 自动更新进行中绝不写基线，
   否则会把"更新到一半的覆盖率"当成正常基线，下一轮必然假报警。
2. **聚合 SQL**：每条规则只用少量聚合查询（正式库单条 < 1s）。贵重规则
   （规则 9 归档交叉核验，正式库实测 ~31s）由全局时间预算控制：超预算时
   **跳过并如实披露**（status=skipped + reason），绝不静默丢规则。
3. **披露型告警**：本模块的 code **一律不进入**
   `data_quality._SNAPSHOT_BLOCKING_WARNINGS` / `_SCREENING_BLOCKING_WARNINGS`。
   P1 只进 `warning_codes`（状态页可见），不会遮蔽指标、也不会让筛选 409。
   升级为阻断型必须另开裁决（reports/119 §5 的建议清单）。
4. **规则失败 ≠ 资产健康**：单条规则抛错时该规则记 status=error 并出现在
   输出里，其余规则照跑；引擎整体永不因单条规则失败而抛异常。
5. **口径复用**：规则 5 直接调用 `data_quality.snapshot_period_mismatches()`，
   规则 9 直接调用 `archive_crosscheck.run_crosscheck()`，规则 2/8 共用同一次
   覆盖率测量 —— 判定只有一处真相，避免"体检结论"与"页面结论"漂移。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

P1 = "P1"
P2 = "P2"
P3 = "P3"
_LEVEL_ORDER = {P1: 0, P2: 1, P3: 2}

SNAPSHOT_DOMAIN = "indicator_snapshot"
EXTENDED_DOMAIN = "indicator_ext"

# 阈值默认值。全部可被 config/default.yaml 的 `asset_health:` 覆盖 ——
# 正式库与测试库规模差三个数量级，阈值写死在代码里迟早两边都不对。
DEFAULT_COVERAGE_LOW_PCT = 90.0
DEFAULT_COVERAGE_DROP_PP = 10.0
DEFAULT_COVERAGE_DROP_RATIO = 0.5
DEFAULT_NULL_REASON_RATIO_PCT = 80.0
DEFAULT_CROSSCHECK_PCT = 85.0
DEFAULT_DEDUCTED_COVERAGE_PCT = 50.0
DEFAULT_PACKAGE_LAG_TRADING_DAYS = 5
DEFAULT_STATIC_LAG_PERIODS = 1
# 覆盖率类判定的最小样本量。为什么必须有：1~2 行的验收库/临时库上覆盖率只能是
# 0% 或 100%，拿它判 P1 就是假红灯（2026-11-14 实测：S1 夹具库因 1 行快照
# 直接报出 55 条 FIELD_COVERAGE_LOW）。正式库 5,573 行远高于本阈值。
DEFAULT_MIN_DOMAIN_ROWS = 100
DEFAULT_MAX_SECONDS = 10.0
DEFAULT_EXPENSIVE_MAX_SECONDS = 180.0

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DDL_TABLE_PATTERN = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", re.IGNORECASE)
# schema.py 之外动态建表的合法来源（轮转/隔离/一次性合并脚本），不算漂移。
# 判据：表名由运行期生成（时间戳后缀）或由维护脚本按隔离语义复制。
DYNAMIC_TABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^raw_response_archive_\d{8}_\d{6}$"),
    re.compile(r"^raw_response_archive_merged_\d{8}$"),
    re.compile(r"^raw_response_archive_quarantine$"),
    re.compile(r"^fetch_batch_quarantine$"),
)

_MODULE_PATH = Path(__file__).resolve()
PROJECT_ROOT = _MODULE_PATH.parents[3]


# ─────────────────────────────────────────────────────────────────────────────
# 结构化输出
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """一条规则命中的结构化结论（字段固定，供 CLI / 状态页 / 测试共用）。"""

    code: str
    level: str
    domain: str
    field: str
    metric: Any
    threshold: Any
    detail: str
    suggestion: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "level": self.level,
            "domain": self.domain,
            "field": self.field,
            "metric": self.metric,
            "threshold": self.threshold,
            "detail": self.detail,
            "suggestion": self.suggestion,
        }

    def sort_key(self) -> tuple[int, str, str, str]:
        return (_LEVEL_ORDER.get(self.level, 9), self.code, self.domain, self.field)


@dataclass
class RuleOutcome:
    """一条规则的执行结果。status 让"没命中"和"没跑了"在输出里可区分。"""

    code: str
    level: str
    title: str
    status: str = "ok"  # ok | warn | skipped | error
    note: str = ""
    elapsed_ms: int = 0
    findings: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "level": self.level,
            "title": self.title,
            "status": self.status,
            "findings": len(self.findings),
            "elapsed_ms": self.elapsed_ms,
            "note": self.note,
        }


@dataclass(frozen=True)
class ManualChain:
    """手工派生链登记项（reports/119 D29 的 ①–⑦）。

    成功时间来源按优先级：① `data_refresh_state` 记账 → ② 数据侧探针。
    为什么要有 ②：D29 的绝大多数链**根本没有记账**（脚本直接写表就退出）。
    只认记账会让规则永远只能报"未登记"，且重建后也不会转绿；数据侧探针
    让告警**自愈**——链一跑，时间就动。
    """

    chain: str
    label: str
    marker_keys: tuple[str, ...]
    probe_sql: str | None
    probe_kind: str  # timestamp | period
    default_stale_after_days: int
    rebuild: str
    default_enabled: bool = True
    disabled_reason: str = ""
    rebuild_note: str = ""

    def suggestion(self) -> str:
        """人话建议：命令用反引号包起来，补充说明跟在后面（别塞进命令里）。"""
        text = f"人工重跑该链：`{self.rebuild}`"
        return f"{text}（{self.rebuild_note}）" if self.rebuild_note else text


MANUAL_CHAINS: tuple[ManualChain, ...] = (
    ManualChain(
        chain="indicator_ext",
        label="扩展指标域 indicator_ext（D29 ①）",
        marker_keys=("indicator_ext_last_success_at",),
        probe_sql="SELECT MAX(calculated_at) AS ts FROM indicator_ext",
        probe_kind="timestamp",
        default_stale_after_days=150,
        rebuild="vd data compute-extended-indicators",
    ),
    ManualChain(
        chain="cashflow_supplement",
        label="现金流补充域 cash_flow_indirect/activity/balance_sheet_ext（D29 ②）",
        marker_keys=("cashflow_supplement_last_success_at",),
        probe_sql="SELECT MAX(fetch_time) AS ts FROM cash_flow_activity",
        probe_kind="timestamp",
        default_stale_after_days=150,
        rebuild="python scripts/fetch_cashflow_supplement.py",
    ),
    ManualChain(
        chain="deducted_profit",
        label="扣非净利润补丁（D29 ③）",
        marker_keys=("deducted_profit_last_success_at",),
        # income_statement 没有 fetch_time 列 → 探针只能用"最新有值报告期"。
        # 语义是"该链的数据停在哪一期"，阈值按报告期跨度设，不是抓取时延。
        probe_sql=(
            "SELECT MAX(report_date) AS ts FROM income_statement "
            "WHERE deducted_net_profit IS NOT NULL"
        ),
        probe_kind="period",
        default_stale_after_days=150,
        rebuild="python scripts/patch_deducted_profit.py",
    ),
    ManualChain(
        chain="regulatory_metrics",
        label="金融监管指标域 financial_sector_items（D29 ④）",
        marker_keys=("regulatory_metrics_last_success_at",),
        probe_sql="SELECT MAX(report_date) AS ts FROM financial_sector_items",
        probe_kind="period",
        default_stale_after_days=200,
        rebuild="python scripts/fetch_regulatory_metrics.py",
    ),
    ManualChain(
        chain="hk_dividends",
        label="港股分红域 hk_dividends（D29 ⑤）",
        marker_keys=("hk_dividends_last_success_at", "hk_dividends_last_refresh"),
        probe_sql="SELECT MAX(fetch_time) AS ts FROM hk_dividends",
        probe_kind="timestamp",
        default_stale_after_days=120,
        rebuild="vd data hk-dividends",
    ),
    ManualChain(
        chain="etf_prices",
        label="ETF 行情 etf_daily（D29 ⑥：只在 retry 分支，无主流程步骤）",
        marker_keys=("etf_prices_last_success_at", "etf_daily_last_success_at"),
        probe_sql="SELECT MAX(trade_date) AS ts FROM etf_daily",
        probe_kind="period",
        default_stale_after_days=10,
        rebuild="vd data update",
        rebuild_note="ETF 行情当前只在 retry 分支被拉，跑完需确认 etf_daily 是否推进",
    ),
    ManualChain(
        chain="pdf_domain",
        label="PDF 域 pdf_tasks（D29 ⑦：恒 0，未启用）",
        marker_keys=("pdf_domain_last_success_at",),
        probe_sql=None,
        probe_kind="timestamp",
        default_stale_after_days=365,
        rebuild="vd data download_pdf",
        default_enabled=False,
        disabled_reason="PDF 域未启用（reports/119 D29 ⑦），不计入超期判定",
        rebuild_note="PDF 域尚未启用，启用前该链不参与超期判定",
    ),
)

# 结构性可空的字段（代码库里已经裁决过的口径，不是"数据没取到"）。
# 出处：`app/core/screening/engine.py::EXTENDED_SCREENING_READY` 的 (b) 类例外
# ——亏损公司的杠杆/EBITDA 本就没有业务含义，NULL 是正确表达。
# 这里**不降级别、不抑制告警**，只在 detail 里加一句人话，避免下一个人
# 以为是回归而去"修"一个本来就该是 NULL 的字段。
STRUCTURAL_NULL_NOTES: dict[str, str] = {
    "leverage_financial": "结构性可空：亏损公司（EBIT/利润总额 ≤ 0）的财务杠杆无业务含义",
    "leverage_operating": "结构性可空：亏损公司经营杠杆无业务含义",
    "leverage_total": "结构性可空：亏损公司综合杠杆无业务含义",
    "ev_ebitda": "结构性可空：需 EBITDA 可用（依赖折旧摊销），留存样本仍 >4,500 只",
}

# 静态低频域：最新报告期落后"应有报告期"即告警（reports/119 D29 ②/③ 的衍生域）。
STATIC_TABLE_SPECS: tuple[tuple[str, str], ...] = (
    (
        "company_employee_history",
        "补采员工数：人效族指标（employee_count/revenue_per_employee/profit_per_employee）"
        "的分母来源，落后一期即整列停在旧报告期",
    ),
    (
        "financial_report_dates",
        "补采年报公布日：`当时可见`（point-in-time）口径的唯一时间来源，"
        "落后一期会让 as-of 查询看不到最新年报",
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# 上下文与工具
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class HealthContext:
    """一次体检的运行上下文。

    `coverage` 缓存规则 2 的测量结果给规则 3/8 复用——三条规则讲的是同一批
    字段，各算一遍在正式库上是三次 0.5s 的全表聚合，且可能读到不同时点的值。
    """

    duck: DuckDBStore
    sqlite: SQLiteStore | None
    now: datetime
    repo_root: Path
    run_id: str
    allow_expensive: bool
    deadline: float
    settings: dict[str, Any]
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    expected_period: date | None = None
    min_domain_rows: int = DEFAULT_MIN_DOMAIN_ROWS

    def sample_is_judgeable(self, total: int) -> bool:
        """样本量够不够下判定（小库上的覆盖率没有判定意义）。"""
        return total >= self.min_domain_rows

    def budget_left(self) -> float:
        return self.deadline - time.monotonic()


def _setting_number(settings: dict[str, Any], key: str, default: float) -> float:
    value = settings.get(key)
    try:
        return float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def load_settings() -> dict[str, Any]:
    """读取 `asset_health.*` 配置；配置不可用时回落到代码默认值。"""
    try:
        from app.core.config import Config

        raw = Config.current().get_value("asset_health", {})
    except Exception:  # 配置未加载/测试环境 → 用默认阈值，绝不因配置问题让体检失败
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _chain_config(chain: ManualChain) -> dict[str, Any]:
    try:
        from app.core.config import Config

        update_cfg = Config.current().get_value("update", {})
    except Exception:
        return {}
    if not isinstance(update_cfg, dict):
        return {}
    entry = update_cfg.get(chain.chain)
    return entry if isinstance(entry, dict) else {}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _as_datetime(value: Any) -> datetime | None:
    """解析日期/时间字符串；无时区的按 UTC 处理（SQLite 记账统一写 UTC ISO）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        as_date = _as_date(text)
        return (
            datetime(as_date.year, as_date.month, as_date.day, tzinfo=UTC)
            if as_date
            else None
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _quarter_end(value: date) -> date:
    month = ((value.month - 1) // 3 + 1) * 3
    last_day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
    return date(value.year, month, last_day)


def _periods_between(earlier: date, later: date) -> int:
    """later 比 earlier 晚几个报告期（earlier 所在季度不算落后）。

    例：earlier=2026-06-30 / later=2026-09-30 → 1 期；
        earlier=2026-09-30 / later=2026-09-30 → 0 期；
        earlier=2026-09-17（落在 Q3 内的非季末日期）/ later=2026-09-30 → 0 期
        —— 同一季度的数据视为"当期"，避免把季度内的日期误判成落后一期。
    """
    count = 0
    cursor = _quarter_end(earlier)
    while True:
        cursor = _quarter_end(date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1))
        if cursor > later:
            break
        count += 1
        if count > 80:  # 防呆：历史域最多 20 年
            break
    return count


def _table_columns(duck: DuckDBStore, table: str) -> set[str]:
    try:
        rows = duck.read_query(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = ?""",
            [table],
        )
    except Exception:
        return set()
    return {str(row["column_name"]).lower() for row in rows}


def _table_exists(duck: DuckDBStore, table: str) -> bool:
    try:
        rows = duck.read_query(
            """SELECT COUNT(*) AS cnt FROM information_schema.tables
               WHERE table_schema = 'main' AND lower(table_name) = lower(?)""",
            [table],
        )
    except Exception:
        return False
    return bool(rows and rows[0]["cnt"])


def _sqlite_table_exists(sqlite: SQLiteStore | None, table: str) -> bool:
    if sqlite is None:
        return False
    try:
        rows = sqlite.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", [table]
        )
    except Exception:
        return False
    return bool(rows)


def _quote(identifier: str) -> str:
    """只允许安全标识符进入 SQL（列名来自代码常量，这里是纵深防御）。"""
    if not IDENTIFIER_PATTERN.match(identifier):
        raise ValueError(f"unsafe identifier: {identifier!r}")
    return f'"{identifier}"'


def measure_coverage(
    duck: DuckDBStore,
    table: str,
    columns: list[str] | set[str] | tuple[str, ...],
    *,
    report_date: date | None = None,
) -> dict[str, Any] | None:
    """测一次"某表最新报告期各列非空率"。

    返回 {"table", "report_date", "total", "fields": {列: {non_null, coverage_pct}}}；
    表不存在/无数据时返回 None（调用方按"跳过"处理，不告警）。
    一条聚合查询算完所有列：正式库 56 列 / 5,573 行实测 0.5s。
    """
    existing = _table_columns(duck, table)
    if not existing or "report_date" not in existing:
        return None
    usable = sorted({col for col in columns if col.lower() in existing and IDENTIFIER_PATTERN.match(col)})
    if not usable:
        return None
    if report_date is None:
        rows = duck.read_query(f"SELECT MAX(report_date) AS latest FROM {table}")
        report_date = _as_date(rows[0]["latest"]) if rows else None
    if report_date is None:
        return None
    selects = ", ".join(f"COUNT({_quote(col)}) AS {_quote(col)}" for col in usable)
    rows = duck.read_query(
        f"SELECT COUNT(*) AS __total, {selects} FROM {table} WHERE report_date = ?",
        [report_date],
    )
    if not rows:
        return None
    row = rows[0]
    total = int(row.get("__total") or 0)
    fields: dict[str, dict[str, Any]] = {}
    for col in usable:
        non_null = int(row.get(col) or 0)
        fields[col] = {
            "non_null": non_null,
            "coverage_pct": round(100.0 * non_null / total, 2) if total else 0.0,
        }
    return {"table": table, "report_date": report_date, "total": total, "fields": fields}


def _ui_domains() -> tuple[tuple[str, set[str]], ...]:
    """上界面字段域（与筛选引擎同一份常量，避免两处口径）。"""
    from app.core.screening.engine import EXTENDED_COLUMNS, SNAPSHOT_COLUMNS

    return (
        (SNAPSHOT_DOMAIN, set(SNAPSHOT_COLUMNS)),
        (EXTENDED_DOMAIN, set(EXTENDED_COLUMNS)),
    )


def _domain_coverage(ctx: HealthContext, domain: str) -> dict[str, Any] | None:
    if domain in ctx.coverage:
        return ctx.coverage[domain]
    columns: set[str] = set()
    for known_domain, known_columns in _ui_domains():
        if known_domain == domain:
            columns = known_columns
            break
    if not columns:
        return None
    measurement = measure_coverage(ctx.duck, domain, columns)
    ctx.coverage[domain] = measurement  # type: ignore[assignment]
    return measurement


# ─────────────────────────────────────────────────────────────────────────────
# 规则 1–12
# ─────────────────────────────────────────────────────────────────────────────


def _rule_ext_period_mismatch(ctx: HealthContext) -> list[Finding]:
    """扩展指标域报告期必须与界面快照同源对齐（reports/119 P0 根因）。

    筛选 SQL 按 `ex.report_date = s.report_date` 严格对齐，一旦错位，
    55 项扩展字段全部变成"能选中但筛不出结果"的死条件。
    """
    rows = ctx.duck.read_query(
        """SELECT (SELECT MAX(report_date) FROM indicator_ext) AS ext_max,
                  (SELECT MAX(report_date) FROM indicator_snapshot) AS snap_max"""
    )
    if not rows:
        return []
    ext_max = _as_date(rows[0]["ext_max"])
    snap_max = _as_date(rows[0]["snap_max"])
    if ext_max is None or snap_max is None:
        return []
    if ext_max == snap_max:
        return []
    return [
        Finding(
            code="EXT_PERIOD_MISMATCH",
            level=P1,
            domain=EXTENDED_DOMAIN,
            field="report_date",
            metric=ext_max.isoformat(),
            threshold=snap_max.isoformat(),
            detail=(
                f"扩展指标最新报告期 {ext_max.isoformat()} ≠ 界面快照最新报告期 "
                f"{snap_max.isoformat()}；按报告期严格对齐的扩展筛选字段当前全部为空"
            ),
            suggestion="重建扩展指标：`vd data compute-extended-indicators`",
        )
    ]


def _rule_field_coverage_low(ctx: HealthContext) -> list[Finding]:
    """上界面字段最新报告期覆盖率过低 = 该字段是"能选中但筛不出"的死条件。

    每个域按**自己的**最新报告期算：跨域错位由规则 1 负责（否则这里会给
    整域 60 个字段各报一条 0%，噪声淹没根因）。
    """
    threshold = _setting_number(ctx.settings, "coverage_low_pct", DEFAULT_COVERAGE_LOW_PCT)
    findings: list[Finding] = []
    for domain, _columns in _ui_domains():
        measurement = _domain_coverage(ctx, domain)
        if not measurement or not measurement["total"]:
            continue
        # 小库不判覆盖率：1~2 行的样本只能是 0%/100%，那不是数据健康问题。
        if not ctx.sample_is_judgeable(measurement["total"]):
            continue
        report_date = measurement["report_date"].isoformat()
        for name, stats in sorted(measurement["fields"].items()):
            pct = float(stats["coverage_pct"])
            if pct >= threshold:
                continue
            findings.append(
                Finding(
                    code="FIELD_COVERAGE_LOW",
                    level=P1,
                    domain=domain,
                    field=name,
                    metric=pct,
                    threshold=threshold,
                    detail=(
                        f"{report_date} 期 {stats['non_null']}/{measurement['total']} 行非空"
                        f"（{pct:.2f}%）"
                        + (
                            f"；{STRUCTURAL_NULL_NOTES[name]}"
                            if name in STRUCTURAL_NULL_NOTES
                            else ""
                        )
                    ),
                    suggestion=(
                        "重建扩展指标：`vd data compute-extended-indicators`"
                        if domain == EXTENDED_DOMAIN
                        else "重算指标快照：`vd data compute_indicators`；"
                        "若字段依赖上游补充域，先补采上游（ops-knowledge-base D30/D31）"
                    ),
                )
            )
    return findings


def _rule_field_coverage_drop(ctx: HealthContext) -> list[Finding]:
    """同字段环比跌幅看门狗（reports/119 §5 C2）。

    基线取 `field_coverage_history` 中**最近一次不同 run_id** 的记录：
    同一次体检写的行不能给自己当基线。没有基线 → 跳过（不告警），
    等下一轮写入后才具备环比能力。
    """
    if not _table_exists(ctx.duck, "field_coverage_history"):
        raise _RuleSkipped("field_coverage_history 表不存在（schema < v39），无历史基线")
    baseline_rows = ctx.duck.read_query(
        """SELECT run_id, MAX(recorded_at) AS recorded_at
           FROM field_coverage_history
           WHERE run_id IS NOT NULL AND run_id <> ?
           GROUP BY run_id
           ORDER BY recorded_at DESC
           LIMIT 1""",
        [ctx.run_id],
    )
    if not baseline_rows:
        raise _RuleSkipped("field_coverage_history 无历史 run_id，暂无环比基线")
    baseline_run = str(baseline_rows[0]["run_id"])
    recorded_at = _as_datetime(baseline_rows[0]["recorded_at"])
    rows = ctx.duck.read_query(
        """SELECT domain, field_name, report_date, coverage_pct
           FROM field_coverage_history WHERE run_id = ?""",
        [baseline_run],
    )
    if not rows:
        raise _RuleSkipped(f"基线 run_id={baseline_run} 无记录")
    baseline = {(str(r["domain"]), str(r["field_name"])): r for r in rows}
    drop_pp_limit = _setting_number(ctx.settings, "coverage_drop_pp", DEFAULT_COVERAGE_DROP_PP)
    drop_ratio_limit = _setting_number(
        ctx.settings, "coverage_drop_ratio", DEFAULT_COVERAGE_DROP_RATIO
    )
    findings: list[Finding] = []
    for domain, _columns in _ui_domains():
        measurement = _domain_coverage(ctx, domain)
        if not measurement or not ctx.sample_is_judgeable(int(measurement["total"] or 0)):
            continue
        for name, stats in sorted(measurement["fields"].items()):
            previous = baseline.get((domain, name))
            if previous is None or previous.get("coverage_pct") is None:
                continue
            before = float(previous["coverage_pct"])
            now_pct = float(stats["coverage_pct"])
            drop_pp = before - now_pct
            drop_ratio = (drop_pp / before) if before > 0 else 0.0
            if drop_pp <= drop_pp_limit and drop_ratio <= drop_ratio_limit:
                continue
            findings.append(
                Finding(
                    code="FIELD_COVERAGE_DROP",
                    level=P1,
                    domain=domain,
                    field=name,
                    metric=round(drop_pp, 2),
                    threshold=(
                        f"跌幅 <= {drop_pp_limit:g}pp 且 <= {drop_ratio_limit:.0%}"
                    ),
                    detail=(
                        f"覆盖率 {before:.2f}% → {now_pct:.2f}%（-{drop_pp:.2f}pp / "
                        f"-{drop_ratio:.1%}）；基线 run_id={baseline_run}"
                        f"（{_iso(recorded_at)}，{_iso(previous.get('report_date'))} 期）"
                    ),
                    suggestion=(
                        "先确认是上游断供还是重算漏项；确认后重建对应域"
                        "（`vd data compute-extended-indicators` / `vd data compute_indicators`）"
                    ),
                )
            )
    return findings


def _chain_last_success(ctx: HealthContext, chain: ManualChain) -> tuple[datetime | None, str]:
    """返回 (最后成功时间, 证据来源说明)。两处都没有 → (None, 原因)。"""
    if ctx.sqlite is not None:
        for key in chain.marker_keys:
            try:
                rows = ctx.sqlite.query(
                    "SELECT value FROM data_refresh_state WHERE key = ?", [key]
                )
            except Exception:
                rows = []
            if not rows:
                continue
            moment = _as_datetime(rows[0].get("value"))
            if moment is not None:
                return moment, f"data_refresh_state 记账 {key}"
    if chain.probe_sql:
        try:
            rows = ctx.duck.read_query(chain.probe_sql)
        except Exception:
            rows = []
        raw = rows[0].get("ts") if rows else None
        if chain.probe_kind == "period":
            period = _as_date(raw)
            if period is not None:
                return datetime(period.year, period.month, period.day, tzinfo=UTC), "数据侧探针（最新报告期）"
        else:
            moment = _as_datetime(raw)
            if moment is not None:
                return moment, "数据侧探针（最新写入时间）"
        return None, "探针为空（该域尚无数据）"
    return None, "既无成功记账也无数据侧探针"


def _rule_manual_chain_stale(ctx: HealthContext) -> list[Finding]:
    """手工链必须有人跑（reports/119 D29）：超期即 P1。

    正式库实测（2026-11-14）：ETF 行情停在 2026-09-18（`etf_daily` 只在 retry
    分支被拉），而主流程每轮都在推进价格 —— 正是这条规则要抓的情形。
    """
    findings: list[Finding] = []
    for chain in MANUAL_CHAINS:
        config = _chain_config(chain)
        enabled = bool(config.get("enabled", chain.default_enabled))
        if not enabled:
            continue
        stale_days = config.get("stale_after_days")
        try:
            stale_days = int(stale_days) if stale_days is not None else chain.default_stale_after_days
        except (TypeError, ValueError):
            stale_days = chain.default_stale_after_days
        moment, source = _chain_last_success(ctx, chain)
        if moment is None:
            # 无任何时间证据（新库/空域）不判超期：那属于"还没开始"，
            # 不是"开始后停摆"。后果由规则 2/12 的空字段覆盖。
            continue
        age_days = (ctx.now - moment).days
        if age_days <= stale_days:
            continue
        findings.append(
            Finding(
                code="MANUAL_CHAIN_STALE",
                level=P1,
                domain=chain.chain,
                field="last_success_at",
                metric=age_days,
                threshold=stale_days,
                detail=(
                    f"{chain.label} 最后成功时间 {moment.date().isoformat()}"
                    f"（{age_days} 天前 > {stale_days} 天；来源：{source}）"
                ),
                suggestion=chain.suggestion(),
            )
        )
    return findings


def _rule_snapshot_period_gap(ctx: HealthContext) -> list[Finding]:
    """复用现成的缺口判定，避免"体检"和"门禁"两套口径。"""
    from app.core.data_quality import snapshot_period_mismatches

    rows = snapshot_period_mismatches(ctx.duck)
    if not rows:
        return []
    sample = ", ".join(str(r.get("stock_code")) for r in rows[:8])
    return [
        Finding(
            code="SNAPSHOT_PERIOD_GAP",
            level=P1,
            domain=SNAPSHOT_DOMAIN,
            field="report_date",
            metric=len(rows),
            threshold=0,
            detail=f"{len(rows)} 只已上市股票的快照报告期 ≠ 最新完整三表期（例：{sample}）",
            suggestion=(
                "重算指标快照：`vd data compute_indicators`；"
                "若三表缺核心字段，先补财务数据（`vd data update`）"
            ),
        )
    ]


def _expected_report_period(ctx: HealthContext) -> date | None:
    """应有报告期 = 最新"完整"资产负债表期（与 data_quality._data_dates 同口径）。"""
    if ctx.expected_period is not None:
        return ctx.expected_period
    try:
        rows = ctx.duck.read_query(
            """SELECT MAX(report_date) AS latest FROM balance_sheet
               WHERE total_assets IS NOT NULL
                 AND total_liabilities IS NOT NULL
                 AND COALESCE(total_equity_parent, total_equity) IS NOT NULL"""
        )
    except Exception:
        return None
    ctx.expected_period = _as_date(rows[0]["latest"]) if rows else None
    return ctx.expected_period


def _rule_static_table_stale(ctx: HealthContext) -> list[Finding]:
    """静态低频域落后应有报告期（P2）：它们不会自己更新，只能靠人补采。"""
    expected = _expected_report_period(ctx)
    if expected is None:
        raise _RuleSkipped("无可比应有报告期（资产负债表无完整期）")
    tolerance = int(_setting_number(ctx.settings, "static_lag_periods", DEFAULT_STATIC_LAG_PERIODS))
    findings: list[Finding] = []
    for table, suggestion in STATIC_TABLE_SPECS:
        if not _table_exists(ctx.duck, table):
            continue
        try:
            rows = ctx.duck.read_query(f"SELECT MAX(report_date) AS latest FROM {table}")
        except Exception:
            continue
        latest = _as_date(rows[0]["latest"]) if rows else None
        if latest is None:
            continue
        lag = _periods_between(latest, expected)
        if lag < max(tolerance, 1):
            continue
        findings.append(
            Finding(
                code="STATIC_TABLE_STALE",
                level=P2,
                domain=table,
                field="report_date",
                metric=lag,
                threshold=tolerance - 1 if tolerance > 1 else 0,
                detail=(
                    f"最新期 {latest.isoformat()}，应有报告期 {expected.isoformat()}，"
                    f"落后 {lag} 期"
                ),
                suggestion=suggestion,
            )
        )
    return findings


def _latest_package_manifest(repo_root: Path) -> tuple[Path | None, dict[str, Any]]:
    candidates = sorted(
        repo_root.glob(".planning/vd-datapackage-*/manifest.json"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return path, payload
    return None, {}


def _trading_days_between(ctx: HealthContext, earlier: date, later: date) -> int:
    """区间内交易日数：优先用 SQLite 交易日历（唯一真相），缺失时退化为工作日。"""
    if _sqlite_table_exists(ctx.sqlite, "trading_dates") and ctx.sqlite is not None:
        try:
            rows = ctx.sqlite.query(
                """SELECT COUNT(*) AS cnt FROM trading_dates
                   WHERE trade_date > ? AND trade_date <= ?""",
                [earlier.isoformat(), later.isoformat()],
            )
            if rows:
                return int(rows[0]["cnt"] or 0)
        except Exception:
            pass
    span = 0
    cursor = earlier
    while cursor < later:
        cursor = date.fromordinal(cursor.toordinal() + 1)
        if cursor.weekday() < 5:
            span += 1
    return span


def _rule_package_stale(ctx: HealthContext) -> list[Finding]:
    """数据包 manifest 的行情日期 vs 库内行情（reports/119 §2 的"6 张表停更"）。"""
    path, payload = _latest_package_manifest(ctx.repo_root)
    if path is None:
        raise _RuleSkipped("未找到 .planning/vd-datapackage-*/manifest.json")
    as_of = payload.get("as_of") if isinstance(payload.get("as_of"), dict) else {}
    packaged = _as_date(as_of.get("price_latest"))
    if packaged is None:
        raise _RuleSkipped(f"{path.name} 未登记 as_of.price_latest")
    try:
        rows = ctx.duck.read_query("SELECT MAX(trade_date) AS latest FROM price_daily_raw")
    except Exception:
        raise _RuleSkipped("price_daily_raw 不可读") from None
    latest = _as_date(rows[0]["latest"]) if rows else None
    if latest is None:
        raise _RuleSkipped("price_daily_raw 无数据")
    lag_trading_days = _trading_days_between(ctx, packaged, latest) if latest > packaged else 0
    limit = int(
        _setting_number(ctx.settings, "package_lag_trading_days", DEFAULT_PACKAGE_LAG_TRADING_DAYS)
    )
    if lag_trading_days <= limit:
        return []
    return [
        Finding(
            code="PACKAGE_STALE",
            level=P2,
            domain="data_package",
            field="as_of.price_latest",
            metric=lag_trading_days,
            threshold=limit,
            detail=(
                f"{path.parent.name} 的行情截止 {packaged.isoformat()}，"
                f"库内行情已到 {latest.isoformat()}，落后 {lag_trading_days} 个交易日"
            ),
            suggestion=(
                "重导出数据包：`python scripts/export_data_package.py`（文档已从实测自动生成，"
                "无需手改常量）；导出后跑 `--verify --pkg <新包>` 必须返回 0"
            ),
        )
    ]


def _rule_null_reason_missing(ctx: HealthContext) -> list[Finding]:
    """NULL 有值却说不清原因的字段（reports/119 §5 P2-1）。

    `indicator_null_reason` 是稀疏表，只登记"NULL 且原因已知"的单元格。
    空表 = 这条登记链还没上线，按约定**跳过不告警**（否则一上线全站 P2）。
    """
    if not _table_exists(ctx.duck, "indicator_null_reason"):
        raise _RuleSkipped("indicator_null_reason 表不存在（schema < v39）")
    try:
        count_rows = ctx.duck.read_query("SELECT COUNT(*) AS cnt FROM indicator_null_reason")
    except Exception:
        raise _RuleSkipped("indicator_null_reason 不可读") from None
    if not count_rows or not count_rows[0]["cnt"]:
        raise _RuleSkipped("indicator_null_reason 为空（登记链尚未产生数据），按约定跳过")
    limit = _setting_number(
        ctx.settings, "null_reason_ratio_pct", DEFAULT_NULL_REASON_RATIO_PCT
    )
    findings: list[Finding] = []
    for domain, _columns in _ui_domains():
        measurement = _domain_coverage(ctx, domain)
        if not measurement or not measurement["total"]:
            continue
        if not ctx.sample_is_judgeable(measurement["total"]):
            continue
        report_date = measurement["report_date"]
        null_fields = {
            name: measurement["total"] - int(stats["non_null"])
            for name, stats in measurement["fields"].items()
            if measurement["total"] - int(stats["non_null"]) > 0
        }
        if not null_fields:
            continue
        placeholders = ", ".join("?" for _ in null_fields)
        try:
            registered_rows = ctx.duck.read_query(
                f"""SELECT field_name, COUNT(*) AS cnt FROM indicator_null_reason
                    WHERE report_date = ? AND field_name IN ({placeholders})
                    GROUP BY field_name""",
                [report_date, *null_fields.keys()],
            )
        except Exception:
            continue
        registered = {str(r["field_name"]): int(r["cnt"] or 0) for r in registered_rows}
        for name, null_count in sorted(null_fields.items()):
            ratio = 100.0 * min(registered.get(name, 0), null_count) / null_count
            if ratio >= limit:
                continue
            findings.append(
                Finding(
                    code="NULL_REASON_MISSING",
                    level=P2,
                    domain=domain,
                    field=name,
                    metric=round(ratio, 2),
                    threshold=limit,
                    detail=(
                        f"{report_date.isoformat()} 期 {null_count} 个 NULL 单元格中仅 "
                        f"{registered.get(name, 0)} 个登记了原因（{ratio:.2f}%）"
                    ),
                    suggestion=(
                        "补登记空值原因：按 `app/core/quality/null_reasons.py` 的 reason_code "
                        "枚举写入 indicator_null_reason（前端才能区分「没有」和「坏了」）"
                    ),
                )
            )
    return findings


def _rule_crosscheck_below_threshold(ctx: HealthContext) -> list[Finding]:
    """归档交叉核验一致率（reports/119：自算实现正确性的唯一独立证据）。

    贵重规则：正式库实测 ~31s（pandas 全表 merge）。只在调用方允许昂贵规则
    且仍有预算时执行，否则如实跳过。
    """
    if not ctx.allow_expensive:
        raise _RuleSkipped("昂贵规则未启用（`vd data doctor` 去掉 --fast 才会执行）")
    if ctx.budget_left() <= 0:
        raise _RuleSkipped(f"时间预算耗尽（{_iso(ctx.now)}）")
    from app.core.quality.archive_crosscheck import DEFAULT_PAIRS, REVIEW_THRESHOLD, run_crosscheck

    report = run_crosscheck(ctx.duck)
    pair_index = {pair.concept: pair for pair in DEFAULT_PAIRS}
    findings: list[Finding] = []
    compared_total = 0
    for result in report.results:
        if result.compared <= 0:
            continue
        compared_total += result.compared
        if result.within_tolerance >= REVIEW_THRESHOLD:
            continue
        pair = pair_index.get(result.concept)
        findings.append(
            Finding(
                code="CROSSCHECK_BELOW_THRESHOLD",
                level=P2,
                domain=pair.our_table if pair else "indicator_ext",
                field=pair.our_column if pair else result.concept,
                metric=round(result.within_tolerance * 100, 2),
                threshold=round(REVIEW_THRESHOLD * 100, 2),
                detail=(
                    f"概念「{result.concept}」比对 {result.compared} 行，一致率 "
                    f"{result.within_tolerance * 100:.2f}%（中位相对差 "
                    f"{result.median_rel_diff * 100:.4f}%）"
                ),
                suggestion=(
                    "复核自算口径与归档域（`vd data cross-check-archive` 可单独重跑）；"
                    "口径本就不同的概念应改登记表，不要放宽阈值"
                ),
            )
        )
    if not compared_total:
        raise _RuleSkipped("归档域无可比对行（归档未导入或与本库无重叠期）")
    return findings


def _schema_ddl_tables(schema_path: Path) -> set[str]:
    """从 schema.py 的 DDL 正则提取 **DuckDB 侧** 表名。

    为什么要切片段：schema.py 同时定义了 SQLite 表（data_refresh_state 等），
    直接全文件正则会把 26 张 SQLite 表当成"DuckDB 缺表"，产生整屏假报警。
    切片规则：丢掉 `SQLITE_SCHEMA_V1` 字符串段与 `init_sqlite_schema` 之后的段。
    """
    try:
        text = schema_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    start_sqlite = text.find("SQLITE_SCHEMA_V1")
    start_duck_fn = text.find("def init_duckdb_schema")
    start_sqlite_fn = text.find("def init_sqlite_schema")
    if start_sqlite > 0 and start_duck_fn > 0 and start_sqlite_fn > start_duck_fn:
        text = text[:start_sqlite] + text[start_duck_fn:start_sqlite_fn]
    return {m.group(1).lower() for m in DDL_TABLE_PATTERN.finditer(text)}


def _rule_schema_ddl_drift(ctx: HealthContext) -> list[Finding]:
    """schema.py 声明的表 vs 库里实际的表（reports/119：schema 已升版但库没迁移）。"""
    schema_path = ctx.repo_root / "app" / "core" / "storage" / "schema.py"
    declared = _schema_ddl_tables(schema_path)
    if not declared:
        raise _RuleSkipped(f"未能从 {schema_path} 提取 DDL 表名")
    try:
        rows = ctx.duck.read_query(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"""
        )
    except Exception:
        raise _RuleSkipped("information_schema.tables 不可读") from None
    actual = {str(row["table_name"]).lower() for row in rows}
    dynamic = {name for name in actual if any(p.match(name) for p in DYNAMIC_TABLE_PATTERNS)}
    missing = sorted(declared - actual)
    orphan = sorted(actual - declared - dynamic)
    findings: list[Finding] = []
    for table in missing:
        findings.append(
            Finding(
                code="SCHEMA_DDL_DRIFT",
                level=P1,
                domain="schema",
                field=table,
                metric="declared_not_in_db",
                threshold="双向差集为空",
                detail=f"schema.py 声明了 {table}，但正式库里没有这张表（版本未迁移）",
                suggestion="执行 schema 初始化/迁移：`vd data init`（或等下一轮自动更新）",
            )
        )
    for table in orphan:
        findings.append(
            Finding(
                code="SCHEMA_DDL_DRIFT",
                level=P1,
                domain="schema",
                field=table,
                metric="in_db_not_declared",
                threshold="双向差集为空",
                detail=f"库里有 {table}，但 schema.py 未声明（来源不明的野表）",
                suggestion=(
                    "在 config/asset_reference.yaml 登记该表来源与用途，或按退役流程删除"
                ),
            )
        )
    return findings


def _runtime_reference_counts(repo_root: Path, tables: list[str]) -> dict[str, int]:
    """统计 `app/` 下**运行时代码**对各表名的引用数（不含 schema.py）。

    写库脚本（scripts/）不算引用：只进不出的表正是 reports/119 说的
    "囤积而非利用"。schema.py 是定义本身，也必须排除，否则恒不为零。
    一次遍历算全部表名：单表一次扫描要 47ms，12 张表逐次扫就是半秒。
    """
    patterns = {table: re.compile(rf"\b{re.escape(table)}\b") for table in tables}
    counts = dict.fromkeys(patterns, 0)
    for path in sorted((repo_root / "app").rglob("*.py")):
        if path.name == "schema.py" and path.parent.name == "storage":
            continue
        # 本模块自己：链路探针 SQL 与文案里必然出现这些表名，算进去会让
        # 清单里每一张表都立刻"已过期"。体检器不算资产使用者。
        if path.name == "asset_health.py" and path.parent.name == "quality":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for table, pattern in patterns.items():
            counts[table] += len(pattern.findall(text))
    return counts


def _count_runtime_references(repo_root: Path, table: str) -> int:
    """单表引用数（供测试与临时排查使用）。"""
    return _runtime_reference_counts(repo_root, [table])[table]


def _load_asset_reference(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "config" / "asset_reference.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _rule_zero_reference_asset(ctx: HealthContext) -> list[Finding]:
    """零引用资产清单（P3，仅报告）：导入进库却没人读的表是负债不是资产。

    清单本身也会过期（表被别人拿去用了），所以每次体检都复核一遍引用数，
    并如实披露"清单已过期"，让文档跟着代码走。
    """
    payload = _load_asset_reference(ctx.repo_root)
    entries = payload.get("zero_reference_tables")
    if not isinstance(entries, list) or not entries:
        raise _RuleSkipped("config/asset_reference.yaml 未登记零引用资产")
    declared = [
        str(entry.get("table") or "").strip()
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("table") or "").strip()
    ]
    reference_counts = _runtime_reference_counts(ctx.repo_root, sorted(set(declared)))
    findings: list[Finding] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        table = str(entry.get("table") or "").strip()
        if not table:
            continue
        rows_count: int | None = None
        if _table_exists(ctx.duck, table):
            try:
                counted = ctx.duck.read_query(f"SELECT COUNT(*) AS cnt FROM {table}")
                rows_count = int(counted[0]["cnt"] or 0) if counted else None
            except Exception:
                rows_count = None
        references = reference_counts.get(table, 0)
        reason = str(entry.get("reason") or "").strip()
        fulfillment = str(entry.get("fulfillment") or "").strip()
        retirement_candidate = bool(entry.get("retirement_candidate"))
        reviewed_rows = entry.get("rows_at_review")
        detail_parts = [
            f"运行时引用 {references} 处",
            f"库内 {'表不存在' if rows_count is None else f'{rows_count} 行'}",
        ]
        if isinstance(reviewed_rows, int) and rows_count is not None and reviewed_rows != rows_count:
            detail_parts.append(f"清单登记 {reviewed_rows} 行（行数已变化，记得复核）")
        if reason:
            detail_parts.append(f"理由：{reason}")
        if references > 0:
            detail_parts.append("清单已过期：该表已被运行时代码引用，请更新 asset_reference.yaml")
        findings.append(
            Finding(
                code="ZERO_REFERENCE_ASSET",
                level=P3,
                domain=str(entry.get("domain") or "archive_domain"),
                field=table,
                metric=references,
                threshold=0,
                detail="；".join(detail_parts),
                suggestion=(
                    "更新 config/asset_reference.yaml（该表已不再零引用）"
                    if references > 0
                    else (
                        "退役候选：替代路径已就绪，可走退役流程（退役前在 "
                        "ops-knowledge-base 登记依据）"
                        if retirement_candidate
                        else f"先兑现：{fulfillment}" if fulfillment else "先完善登记理由"
                    )
                ),
            )
        )
    return findings


def _rule_deducted_profit_gap(ctx: HealthContext) -> list[Finding]:
    """扣非净利润是价值判断的核心字段，最新报告期塌陷即为 P1。

    正式库 2026-11-14 实测：2026-09-30 期 0/5,575（Q2 也只有 20 行）——
    抓取侧自 2026Q2 起归零（reports/119 §1.4）。
    """
    columns = _table_columns(ctx.duck, "income_statement")
    if "deducted_net_profit" not in columns or "report_date" not in columns:
        raise _RuleSkipped("income_statement 缺 deducted_net_profit/report_date 列")
    rows = ctx.duck.read_query(
        """SELECT report_date,
                  COUNT(*) AS total,
                  COUNT(deducted_net_profit) AS non_null
           FROM income_statement
           WHERE report_date = (SELECT MAX(report_date) FROM income_statement)
           GROUP BY report_date"""
    )
    if not rows:
        raise _RuleSkipped("income_statement 无数据")
    row = rows[0]
    total = int(row["total"] or 0)
    non_null = int(row["non_null"] or 0)
    if not total:
        raise _RuleSkipped("income_statement 最新报告期无行")
    if not ctx.sample_is_judgeable(total):
        raise _RuleSkipped(f"最新报告期仅 {total} 行（< {ctx.min_domain_rows}），覆盖率不具判定意义")
    pct = 100.0 * non_null / total
    limit = _setting_number(
        ctx.settings, "deducted_profit_coverage_pct", DEFAULT_DEDUCTED_COVERAGE_PCT
    )
    if pct >= limit:
        return []
    return [
        Finding(
            code="DEDUCTED_PROFIT_GAP",
            level=P1,
            domain="income_statement",
            field="deducted_net_profit",
            metric=round(pct, 2),
            threshold=limit,
            detail=(
                f"{_iso(_as_date(row['report_date']))} 期 {non_null}/{total} 行非空"
                f"（{pct:.2f}%）"
            ),
            suggestion=(
                "补采扣非净利润：`python scripts/patch_deducted_profit.py`"
                "（也可 `python scripts/import_csmar_deducted_profit.py` 走 CSMAR 归档）"
            ),
        )
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 规则注册与引擎
# ─────────────────────────────────────────────────────────────────────────────


class _RuleSkipped(Exception):
    """规则本次不适用（表缺失/无基线/预算不足）—— 记 skipped，不算失败。"""


_RULES: tuple[tuple[str, str, str, bool, Any], ...] = (
    ("EXT_PERIOD_MISMATCH", P1, "扩展指标域报告期与界面快照错位", False, _rule_ext_period_mismatch),
    ("FIELD_COVERAGE_LOW", P1, "上界面字段最新报告期覆盖率过低", False, _rule_field_coverage_low),
    ("FIELD_COVERAGE_DROP", P1, "上界面字段覆盖率环比断崖", False, _rule_field_coverage_drop),
    ("MANUAL_CHAIN_STALE", P1, "手工派生链超期未跑", False, _rule_manual_chain_stale),
    ("SNAPSHOT_PERIOD_GAP", P1, "快照报告期与完整三表期存在缺口", False, _rule_snapshot_period_gap),
    ("STATIC_TABLE_STALE", P2, "静态低频域落后应有报告期", False, _rule_static_table_stale),
    ("PACKAGE_STALE", P2, "数据包行情截止日落后于库内行情", False, _rule_package_stale),
    ("NULL_REASON_MISSING", P2, "上界面字段有 NULL 但未登记原因", False, _rule_null_reason_missing),
    (
        "CROSSCHECK_BELOW_THRESHOLD",
        P2,
        "归档交叉核验一致率低于阈值",
        True,
        _rule_crosscheck_below_threshold,
    ),
    ("SCHEMA_DDL_DRIFT", P1, "schema.py DDL 与库内实际表漂移", False, _rule_schema_ddl_drift),
    ("ZERO_REFERENCE_ASSET", P3, "零引用资产清单（仅披露）", False, _rule_zero_reference_asset),
    ("DEDUCTED_PROFIT_GAP", P1, "扣非净利润最新报告期覆盖率塌陷", False, _rule_deducted_profit_gap),
)

RULE_CODES: tuple[str, ...] = tuple(spec[0] for spec in _RULES)
P1_CODES: frozenset[str] = frozenset(spec[0] for spec in _RULES if spec[1] == P1)


def run_asset_health(
    duck: DuckDBStore,
    sqlite: SQLiteStore | None = None,
    *,
    now: datetime | None = None,
    repo_root: Path | None = None,
    run_id: str | None = None,
    allow_expensive: bool = True,
    max_seconds: float | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """跑完 12 条规则，返回结构化结果（永不抛异常给调用方）。

    返回体字段：
      generated_at / run_id / elapsed_ms / budget / level_counts / code_counts /
      p1_codes / findings（扁平列表，按 级别→code→domain→field 排序）/
      rules（每条规则的 status/note/耗时）/ skipped（被跳过的 code 与原因）
    """
    started = time.monotonic()
    moment = now or datetime.now(UTC)
    health_settings = dict(settings) if settings is not None else load_settings()
    budget = (
        float(max_seconds)
        if max_seconds is not None
        else _setting_number(health_settings, "max_seconds", DEFAULT_MAX_SECONDS)
    )
    if allow_expensive:
        budget = max(
            budget,
            _setting_number(health_settings, "expensive_max_seconds", DEFAULT_EXPENSIVE_MAX_SECONDS),
        )
    ctx = HealthContext(
        duck=duck,
        sqlite=sqlite,
        now=moment,
        repo_root=Path(repo_root) if repo_root is not None else PROJECT_ROOT,
        run_id=run_id or f"doctor-{moment.astimezone(UTC).strftime('%Y%m%dT%H%M%S')}",
        allow_expensive=allow_expensive,
        deadline=started + budget,
        settings=health_settings,
        min_domain_rows=int(
            _setting_number(health_settings, "min_domain_rows", DEFAULT_MIN_DOMAIN_ROWS)
        ),
    )
    disabled = health_settings.get("disabled_rules")
    disabled_codes = {str(code) for code in disabled} if isinstance(disabled, list) else set()

    outcomes: list[RuleOutcome] = []
    findings: list[Finding] = []
    for code, level, title, expensive, check in _RULES:
        outcome = RuleOutcome(code=code, level=level, title=title)
        rule_started = time.monotonic()
        if code in disabled_codes:
            outcome.status = "skipped"
            outcome.note = "配置 asset_health.disabled_rules 显式关闭"
        elif expensive and not allow_expensive:
            outcome.status = "skipped"
            outcome.note = "昂贵规则未启用（`--fast`）"
        elif expensive and ctx.budget_left() <= 0:
            outcome.status = "skipped"
            outcome.note = f"时间预算 {budget:.0f}s 已耗尽"
        else:
            try:
                outcome.findings = list(check(ctx))
                outcome.status = "warn" if outcome.findings else "ok"
            except _RuleSkipped as skipped:
                outcome.status = "skipped"
                outcome.note = str(skipped)
            except Exception as error:  # noqa: BLE001 - 单条规则失败不得拖垮体检
                outcome.status = "error"
                outcome.note = f"{type(error).__name__}: {error}"
                logger.warning("资产健康规则 %s 执行失败", code, exc_info=True)
        outcome.elapsed_ms = int((time.monotonic() - rule_started) * 1000)
        outcomes.append(outcome)
        findings.extend(outcome.findings)

    findings.sort(key=Finding.sort_key)
    level_counts = {P1: 0, P2: 0, P3: 0}
    code_counts: dict[str, int] = {}
    for finding in findings:
        level_counts[finding.level] = level_counts.get(finding.level, 0) + 1
        code_counts[finding.code] = code_counts.get(finding.code, 0) + 1
    p1_codes = sorted({f.code for f in findings if f.level == P1})
    return {
        "generated_at": moment.astimezone(UTC).isoformat(),
        "run_id": ctx.run_id,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "budget": {
            "max_seconds": budget,
            "expensive_allowed": allow_expensive,
        },
        # healthy 供 CLI 协议层判定 result.status（与 `vd data diagnose` 同约定）。
        # 体检"不健康"= 有 P1；P2/P3 是待办清单，不影响 healthy。
        "healthy": not p1_codes,
        "level_counts": level_counts,
        "code_counts": dict(sorted(code_counts.items())),
        "p1_codes": p1_codes,
        "findings": [finding.as_dict() for finding in findings],
        "rules": [outcome.as_dict() for outcome in outcomes],
        "skipped": [
            {"code": outcome.code, "reason": outcome.note}
            for outcome in outcomes
            if outcome.status == "skipped"
        ],
        "errors": [
            {"code": outcome.code, "error": outcome.note}
            for outcome in outcomes
            if outcome.status == "error"
        ],
    }


def summarize_asset_health(payload: dict[str, Any], *, max_findings: int = 20) -> dict[str, Any]:
    """把完整结果压成状态页可用的摘要（完整 findings 留在 CLI/JSON 输出里）。

    为什么要压：正式库首轮实测 FIELD_COVERAGE_LOW 一个 code 就有 33 条，
    整包塞进 `/api/data-status/summary` 会把响应撑大十几倍而信息量不变。
    """
    findings = payload.get("findings") or []
    return {
        "generated_at": payload.get("generated_at"),
        "run_id": payload.get("run_id"),
        "level_counts": payload.get("level_counts", {}),
        "code_counts": payload.get("code_counts", {}),
        "p1_codes": payload.get("p1_codes", []),
        "finding_count": len(findings),
        "findings": findings[:max_findings],
        "truncated": max(len(findings) - max_findings, 0),
        "rules": payload.get("rules", []),
        "skipped": payload.get("skipped", []),
        "errors": payload.get("errors", []),
        "elapsed_ms": payload.get("elapsed_ms"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 覆盖率基线（唯一写路径）
# ─────────────────────────────────────────────────────────────────────────────


def write_field_coverage_history(
    duck: DuckDBStore,
    sqlite: SQLiteStore | None = None,
    *,
    now: datetime | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """把本轮字段覆盖率写成下一轮的环比基线（`--write-history` 才会调用）。

    写前自检 `any_write_lock_active()`：自动更新持锁时库里的覆盖率是半成品，
    拿它当基线会把"暂时为空"固化成"正常"，下一轮反而漏报。
    """
    moment = now or datetime.now(UTC)
    run = run_id or f"doctor-{moment.astimezone(UTC).strftime('%Y%m%dT%H%M%S')}"
    try:
        from app.core.storage.update_lock import any_write_lock_active

        if any_write_lock_active(duck.db_path):
            return {"status": "skipped", "reason": "update_lock_active", "run_id": run, "rows": 0}
    except Exception:
        pass
    if not _table_exists(duck, "field_coverage_history"):
        return {
            "status": "skipped",
            "reason": "field_coverage_history_missing（schema < v39，先跑 `vd data init`）",
            "run_id": run,
            "rows": 0,
        }
    rows: list[tuple[Any, ...]] = []
    for domain, columns in _ui_domains():
        measurement = measure_coverage(duck, domain, columns)
        if not measurement or not measurement["total"]:
            continue
        for name, stats in sorted(measurement["fields"].items()):
            rows.append(
                (
                    moment.astimezone(UTC),
                    run,
                    domain,
                    name,
                    measurement["report_date"],
                    int(stats["non_null"]),
                    int(measurement["total"]),
                    float(stats["coverage_pct"]),
                )
            )
    if not rows:
        return {"status": "skipped", "reason": "no_ui_domain_rows", "run_id": run, "rows": 0}
    try:
        with duck.transaction() as connection:
            connection.executemany(
                """INSERT INTO field_coverage_history
                       (recorded_at, run_id, domain, field_name, report_date,
                        non_null_count, total_count, coverage_pct)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (run_id, domain, field_name) DO UPDATE SET
                       recorded_at = excluded.recorded_at,
                       report_date = excluded.report_date,
                       non_null_count = excluded.non_null_count,
                       total_count = excluded.total_count,
                       coverage_pct = excluded.coverage_pct""",
                rows,
            )
    except Exception as error:  # noqa: BLE001 - 写基线失败只披露，不让体检失败
        return {
            "status": "error",
            "reason": f"{type(error).__name__}: {error}",
            "run_id": run,
            "rows": 0,
        }
    return {"status": "written", "run_id": run, "rows": len(rows), "recorded_at": _iso(moment)}


def run_doctor(
    duck: DuckDBStore,
    sqlite: SQLiteStore | None = None,
    *,
    write_history: bool = False,
    allow_expensive: bool = True,
    max_seconds: float | None = None,
    now: datetime | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """`vd data doctor` 的完整实现：体检 +（可选）写基线。"""
    payload = run_asset_health(
        duck,
        sqlite,
        now=now,
        repo_root=repo_root,
        allow_expensive=allow_expensive,
        max_seconds=max_seconds,
    )
    if write_history:
        payload["history"] = write_field_coverage_history(
            duck, sqlite, now=now, run_id=payload["run_id"]
        )
    else:
        payload["history"] = {
            "status": "disabled",
            "reason": "未传 --write-history（本轮不写环比基线）",
            "run_id": payload["run_id"],
            "rows": 0,
        }
    return payload
