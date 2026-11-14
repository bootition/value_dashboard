"""补采域核心逻辑（2026-11-14，reports/119 D29「手工链」自动接入）

本模块把三个一次性脚本的核心逻辑抽成**可调用函数**，让自动更新主流程
（`app/core/update.py::_run_incremental_update_flow`）与 CLI 共用同一份实现：

1. `cash_flow_supplement` —— 折旧摊销族（`cash_flow_indirect`）+ 资本支出
   （`cash_flow_activity`）；来源 `scripts/fetch_cashflow_supplement.py`
2. `balance_sheet_ext`   —— 其他应付款 / 一年内到期非流动负债 / 其他应收款合计；
   来源 `scripts/fetch_balance_sheet_ext.py`
3. `deducted_profit`     —— 扣非净利润（只 UPDATE `income_statement` 既有行）；
   来源 `scripts/patch_deducted_profit.py`

为什么必须抽出来：不是"调度缺失"，是**续传判据错了**
----------------------------------------------------
旧脚本的断点续传判据是「该股在本表里有**任意** `source='eastmoney_f10'` 行就跳过」。
第一次跑完（当时源只到 2026-06-30）之后，全市场股票都被永久判为"已有本源"，
**即使手动再跑也不会补 2026-09-30** —— 即"跑了也白跑"。2026-11-14 实测
`fetch_cashflow_supplement.py` dry-run 仍报"待补 40 只 / 已有本源 5540"，
这 40 只是从未取到数的，5540 只里的 Q3 缺口永远不会被补。

现在统一改成「**本源最新报告期 < 目标最新报告期 → 进 pending**」，
并对"抓了仍然没有该期数据"的股票做**失败冷却**（默认 7 天），
避免它们在队首吃掉每轮上限、把其余股票饿死。

域纪律
------
- 只写 `source='eastmoney_f10'` 的行；`cash_flow_indirect`/`cash_flow_activity`/
  `balance_sheet_ext` 按 (stock_code, report_date) 先删同源再插（幂等）；
- `deducted_profit` **只 UPDATE 既有行的 NULL 值，绝不 INSERT / DELETE**，
  因此字段名若与源不一致时结果是"没更新"，不会写坏主链三表；
- 不写 `indicator_snapshot` / `readiness`；失败保留旧值并如实计数；
- 每轮都有 `max_stocks` 与 `deadline_seconds` 双上限，保证更新周期不被拖长。
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import requests

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = [
    "BALANCE_SHEET_EXT",
    "CASHFLOW_SUPPLEMENT",
    "CSMAR_CUTOFF",
    "DEDUCTED_PROFIT",
    "REGULATORY_METRICS",
    "SPECS",
    "RegulatoryMetricsUpdater",
    "SupplementFetcher",
    "SupplementSpec",
    "em_symbol",
    "recent_quarter_ends",
]

SOURCE = "eastmoney_f10"
CONFIDENCE = "strict"
BASE = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis"
DATACENTER_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
# CSMAR C17 的截止报告期：只补这之后，避免与 CSMAR 行重复覆盖。
CSMAR_CUTOFF = date(2025, 3, 31)
# 东财 F10 `*AjaxNew` 端点单次最多接受 5 个报告期（传更多仍只回 5 期）。
PERIODS_PER_REQUEST = 5
MAX_RETRY = 3
# 失败冷却：抓过但仍无该期数据的股票在 N 天内不再进队（防止饿死队尾）。
ATTEMPT_STATE_PREFIX = "supplement_attempted:"
ATTEMPT_STATE_KEEP_DAYS = 30

# ─── 数据集标识（update.py / CLI 共用）───────────────────────────────────
CASHFLOW_SUPPLEMENT = "cash_flow_supplement"
BALANCE_SHEET_EXT = "balance_sheet_ext"
DEDUCTED_PROFIT = "deducted_profit"
FIXED_ASSET_SUPPLEMENT = "fixed_assets_supplement"
REGULATORY_METRICS = "regulatory_metrics"

# 折旧摊销族 → cash_flow_indirect 列名（实测确认，见旧脚本 docstring）
INDIRECT_FIELD_MAP: dict[str, str] = {
    "FA_IR_DEPR": "fixed_asset_depreciation",
    "IR_DEPR": "investment_property_depreciation",
    "USERIGHT_ASSET_AMORTIZE": "right_of_use_asset_depreciation",
    "IA_AMORTIZE": "intangible_asset_amortization",
    "LPE_AMORTIZE": "long_term_prepaid_amortization",
    "ASSET_IMPAIRMENT": "asset_impairment_provision",
    "FAIRVALUE_CHANGE_LOSS": "fair_value_change_loss",
    "INVEST_LOSS": "investment_loss",
    "DISPOSAL_LONGASSET_LOSS": "disposal_long_term_asset_loss",
    "NETCASH_OPERATE": "cf_from_operating_indirect",
}
# 资本支出 → cash_flow_activity 列名
ACTIVITY_FIELD_MAP: dict[str, str] = {
    "CONSTRUCT_LONG_ASSET": "capex",
}
# 资产负债表补充科目 → balance_sheet_ext 列名
BALANCE_EXT_FIELD_MAP: dict[str, str] = {
    "TOTAL_OTHER_PAYABLE": "other_payables",
    "NONCURRENT_LIAB_1YEAR": "non_current_liab_due_1y",
    "TOTAL_OTHER_REC": "total_other_receivable",
}
# 扣非净利润 → income_statement.deducted_net_profit（字段名由旧脚本实测确认：
# akshare `stock_profit_sheet_by_report_em` 返回的原始列名）
DEDUCTED_FIELD_MAP: dict[str, str] = {
    "DEDUCT_PARENT_NETPROFIT": "deducted_net_profit",
}

INDIRECT_COLUMNS: tuple[str, ...] = (
    "stock_code", "report_date", "report_type", "fixed_asset_depreciation",
    "investment_property_depreciation", "right_of_use_asset_depreciation",
    "intangible_asset_amortization", "long_term_prepaid_amortization",
    "asset_impairment_provision", "fair_value_change_loss", "investment_loss",
    "disposal_long_term_asset_loss", "cf_from_operating_indirect",
    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id", "raw_data",
)
ACTIVITY_COLUMNS: tuple[str, ...] = (
    "stock_code", "report_date", "report_type", "capex",
    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id",
)
BALANCE_EXT_COLUMNS: tuple[str, ...] = (
    "stock_code", "report_date", "report_type",
    *BALANCE_EXT_FIELD_MAP.values(),
    "source", "fetch_time", "raw_response_hash", "confidence", "batch_id",
)
DEDUCTED_COLUMNS: tuple[str, ...] = ("stock_code", "report_date", "deducted_net_profit")
# 固定资产净额 → balance_sheet.fixed_assets。
# 2026-11-14 审查（reports/119 P1-3）：2026Q3 主链 `balance_sheet.fixed_assets`
# 只剩 102/5,575（1.8%），而上期 99.9% —— 新浪 Q3 返回的资产负债表没有这个键
# （抽 000408 raw_data：Q2 有、Q3 无）。东财 F10 `zcfzbAjaxNew` 有该字段
# （同一接口已在补 other_payables），复用它**只回填 NULL**。
FIXED_ASSET_FIELD_MAP: dict[str, str] = {
    "FIXED_ASSET": "fixed_assets",
}
FIXED_ASSET_COLUMNS: tuple[str, ...] = ("stock_code", "report_date", "fixed_assets")


@dataclass(frozen=True)
class SupplementSpec:
    """一个补采数据集的抓取/解析/落库描述（声明式，避免三份近似代码）。"""

    key: str
    data_type: str
    endpoint: str
    api_version: str
    field_map: Mapping[str, str]
    columns: Mapping[str, tuple[str, ...]]
    #: replace_source_rows = 按股票删同源再插；update_existing = 只填 NULL 列
    write_mode: str
    #: latest_period = 本源最新报告期落后目标期即待补；missing_field = 列仍有 NULL
    pending_mode: str
    #: 判据表（latest_period 模式用它判断"该股本源最新报告期"）
    judge_table: str
    #: 待补报告期的下界（只补 CSMAR 未覆盖期）
    period_floor: date | None = None
    fills_null_only: bool = False


SPECS: dict[str, SupplementSpec] = {
    CASHFLOW_SUPPLEMENT: SupplementSpec(
        key=CASHFLOW_SUPPLEMENT,
        data_type="cash_flow_supplement",
        endpoint="xjllbAjaxNew",
        api_version="eastmoney-f10-xjllb/1",
        field_map={**INDIRECT_FIELD_MAP, **ACTIVITY_FIELD_MAP},
        columns={
            "cash_flow_indirect": INDIRECT_COLUMNS,
            "cash_flow_activity": ACTIVITY_COLUMNS,
        },
        write_mode="replace_source_rows",
        pending_mode="latest_period",
        judge_table="cash_flow_activity",
    ),
    BALANCE_SHEET_EXT: SupplementSpec(
        key=BALANCE_SHEET_EXT,
        data_type="balance_sheet_ext",
        endpoint="zcfzbAjaxNew",
        api_version="eastmoney-f10-zcfzb/1",
        field_map=dict(BALANCE_EXT_FIELD_MAP),
        columns={"balance_sheet_ext": BALANCE_EXT_COLUMNS},
        write_mode="replace_source_rows",
        pending_mode="latest_period",
        judge_table="balance_sheet_ext",
    ),
    FIXED_ASSET_SUPPLEMENT: SupplementSpec(
        key=FIXED_ASSET_SUPPLEMENT,
        data_type="fixed_assets_supplement",
        endpoint="zcfzbAjaxNew",
        api_version="eastmoney-f10-zcfzb-fixed/1",
        field_map=dict(FIXED_ASSET_FIELD_MAP),
        columns={"balance_sheet": FIXED_ASSET_COLUMNS},
        write_mode="update_existing",
        pending_mode="missing_field",
        judge_table="balance_sheet",
        period_floor=CSMAR_CUTOFF,
        fills_null_only=True,
    ),
    DEDUCTED_PROFIT: SupplementSpec(
        key=DEDUCTED_PROFIT,
        data_type="deducted_profit",
        endpoint="lrbAjaxNew",
        api_version="eastmoney-f10-lrb/1",
        field_map=dict(DEDUCTED_FIELD_MAP),
        columns={"income_statement": DEDUCTED_COLUMNS},
        write_mode="update_existing",
        pending_mode="missing_field",
        judge_table="income_statement",
        period_floor=CSMAR_CUTOFF,
        fills_null_only=True,
    ),
}


def em_symbol(code: str, exchange: str | None = None) -> str:
    """证券代码 → 东方财富 symbol。

    优先用 `stock_meta.exchange`；**不要用代码前缀猜** —— 北交所新代码
    920xxx 以 9 开头，按"9→沪市"的旧规则会被错判为上交所（2026-09-19
    实测 336 只北交所股票因此取数失败）。
    """
    code = str(code).strip().zfill(6)
    prefix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get((exchange or "").upper())
    if prefix is None:  # exchange 缺失时的兜底：北交所优先
        if code.startswith(("920", "43", "83", "87")):
            prefix = "BJ"
        elif code.startswith(("60", "68", "90", "9")):
            prefix = "SH"
        else:
            prefix = "SZ"
    return f"{prefix}{code}"


def recent_quarter_ends(today: date | None = None, n: int = PERIODS_PER_REQUEST) -> list[str]:
    """最近 n 个已过去的季末报告期（含当季，若已过季末）。"""
    today = today or date.today()
    ends: list[date] = []
    for year in (today.year, today.year - 1):
        for month, day in ((12, 31), (9, 30), (6, 30), (3, 31)):
            ends.append(date(year, month, day))
    ordered = sorted({end for end in ends if end <= today}, reverse=True)
    return [end.isoformat() for end in ordered[:n]]


def report_type_of(report_date: date) -> str:
    """报告期 → report_type 文案（与主链三表口径一致）。"""
    return {12: "annual", 6: "semi_annual"}.get(report_date.month, "quarterly")


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(result) else result


_LOCAL = threading.local()


def _session() -> requests.Session:
    """每线程一个 Session：requests.Session 不是线程安全的。"""
    session = getattr(_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        _LOCAL.session = session
    return session


class SupplementFetcher:
    """补采域执行器：有界、幂等、失败冷却。

    调用方（自动更新 / CLI）只需 `refresh(key, ...)`；网络层在 `_fetch_rows`，
    解析在 `_parse_rows`，落库在 `_persist_rows`，三段都可单独测试/替换。
    """

    def __init__(
        self,
        duck: DuckDBStore,
        sqlite: SQLiteStore | None = None,
    ) -> None:
        self.duck = duck
        self.sqlite = sqlite

    # ─── 目标报告期 ────────────────────────────────────────────────

    @staticmethod
    def resolve_periods(
        key: str,
        *,
        today: date | None = None,
        periods: Sequence[str] | None = None,
    ) -> list[str]:
        """目标报告期：显式传入优先，否则取最近 5 个季末（过滤 CSMAR 覆盖期）。"""
        spec = SPECS[key]
        raw = list(periods) if periods else recent_quarter_ends(today=today)
        if spec.period_floor is not None:
            raw = [
                item for item in raw
                if date.fromisoformat(str(item)[:10]) > spec.period_floor
            ]
        return raw

    # ─── 待补股票（修掉「有任意本源行就跳过」的续传判据）─────────────

    def pending_codes(
        self,
        key: str,
        periods: Sequence[str],
        *,
        max_stocks: int = 0,
        retry_days: int = 0,
    ) -> list[tuple[str, str]]:
        """返回本轮应补的股票 `[(code, exchange), ...]`。

        判据（两种模式）：
        - `latest_period`：本源最新报告期 < 目标最新报告期；
          旧实现是"本表有任意本源行就跳过"，这会永久跳过所有股票
          （2026-09-19 那次只到 06-30，之后再也补不到 09-30）。
        - `missing_field`：目标期内仍有该列为 NULL 的既有行（扣非）。
        """
        spec = SPECS[key]
        if not periods:
            return []
        latest = str(periods[0])[:10]
        rows = self.duck.read_query(
            self._pending_sql(spec),
            self._pending_params(spec, periods),
        )
        pending: list[tuple[str, str]] = []
        for row in rows:
            if spec.pending_mode == "latest_period":
                # 判据核心：本源**最新报告期**是否已经追平目标最新期。
                # 只要落后（或从来没有本源行）就必须再抓 —— 旧脚本在这里
                # 用的是"有没有任意本源行"，于是永远补不到新报告期。
                have = row.get("max_report_date")
                if have is not None and str(have)[:10] >= latest:
                    continue
            pending.append((str(row["stock_code"]), str(row.get("exchange") or "")))
        pending = self._filter_cooldown(key, pending, retry_days)
        if max_stocks > 0:
            pending = pending[:max_stocks]
        return pending

    @staticmethod
    def _pending_sql(spec: SupplementSpec) -> str:
        if spec.pending_mode == "missing_field":
            # 只关心近期窗口内仍为空的行；更早的报告期源侧不再返回，抓了也补不上。
            # 2026-11-14：原实现硬编码 income_statement.deducted_net_profit，
            # 新增数据集（balance_sheet.fixed_assets）会扫错表 —— 改为按 spec 生成：
            # 判据表取 `judge_table`，待填列取该表 columns 里除主键外的所有列。
            table = spec.judge_table
            fill_columns = [
                column for column in spec.columns[table]
                if column not in ("stock_code", "report_date")
            ]
            null_clause = " OR ".join(f"i.{column} IS NULL" for column in fill_columns)
            return f"""
                SELECT m.stock_code AS stock_code,
                       COALESCE(m.exchange, '') AS exchange,
                       MIN(i.report_date) AS first_gap
                FROM stock_meta m
                JOIN {table} i ON i.stock_code = m.stock_code
                WHERE m.is_listed IS TRUE
                  AND i.report_date >= CAST(? AS DATE)
                  AND i.report_date <= CAST(? AS DATE)
                  AND ({null_clause})
                GROUP BY m.stock_code, m.exchange
                ORDER BY first_gap DESC, m.stock_code
            """
        # latest_period：LEFT JOIN 判据表，MAX(report_date) 落后即待补。
        return f"""
            WITH universe AS (
                SELECT DISTINCT m.stock_code AS stock_code,
                       COALESCE(m.exchange, '') AS exchange
                FROM stock_meta m
                JOIN income_statement i ON i.stock_code = m.stock_code
                WHERE m.is_listed IS TRUE
            )
            SELECT u.stock_code AS stock_code,
                   u.exchange AS exchange,
                   MAX(t.report_date) AS max_report_date
            FROM universe u
            LEFT JOIN {spec.judge_table} t
                   ON t.stock_code = u.stock_code AND t.source = ?
            GROUP BY u.stock_code, u.exchange
            ORDER BY (MAX(t.report_date) IS NULL) DESC,
                     MAX(t.report_date) ASC,
                     u.stock_code ASC
        """

    @staticmethod
    def _pending_params(spec: SupplementSpec, periods: Sequence[str]) -> list[Any]:
        if spec.pending_mode == "missing_field":
            return [str(periods[-1])[:10], str(periods[0])[:10]]
        return [SOURCE]

    # ─── 失败冷却（SQLite 持久化，跨轮次生效）────────────────────────

    def _attempt_state_key(self, key: str) -> str:
        return f"{ATTEMPT_STATE_PREFIX}{key}"

    def _load_attempts(self, key: str) -> dict[str, str]:
        if self.sqlite is None:
            return {}
        try:
            rows = self.sqlite.query(
                "SELECT value FROM data_refresh_state WHERE key = ?",
                [self._attempt_state_key(key)],
            )
            payload = json.loads(rows[0]["value"]) if rows else {}
            return payload if isinstance(payload, dict) else {}
        except Exception as error:  # noqa: BLE001 - 冷却态损坏不得阻断补采
            logger.warning("读取补采失败冷却态失败(%s): %s", key, error)
            return {}

    def _record_attempts(self, key: str, codes: Sequence[str]) -> None:
        if self.sqlite is None or not codes:
            return
        now = datetime.now(UTC)
        attempts = self._load_attempts(key)
        stamp = now.isoformat()
        for code in codes:
            attempts[str(code)] = stamp
        cutoff = now - timedelta(days=ATTEMPT_STATE_KEEP_DAYS)
        pruned = {
            code: value
            for code, value in attempts.items()
            if (moment := _parse_iso(value)) is not None and moment >= cutoff.replace(tzinfo=None)
        }
        try:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO data_refresh_state (key, value, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value = excluded.value, updated_at = excluded.updated_at""",
                    [self._attempt_state_key(key), json.dumps(pruned), stamp],
                )
        except Exception as error:  # noqa: BLE001
            logger.warning("写入补采失败冷却态失败(%s): %s", key, error)

    def _filter_cooldown(self, key: str, pending: list[tuple[str, str]], retry_days: int) -> list[tuple[str, str]]:
        if retry_days <= 0:
            return pending
        attempts = self._load_attempts(key)
        if not attempts:
            return pending
        cutoff = datetime.now(UTC) - timedelta(days=retry_days)
        blocked = {
            code for code, value in attempts.items()
            if (moment := _parse_iso(value)) is not None
            and moment >= cutoff.replace(tzinfo=None)
        }
        return [item for item in pending if item[0] not in blocked]

    # ─── 网络层（测试通过 monkeypatch 本方法离线化）───────────────────

    def _company_type(self, symbol: str, timeout: float) -> str | None:
        """金融股 `companyType` 与通用企业不同，从页面 hidctype 取真实值。"""
        try:
            page = _session().get(
                f"{BASE}/Index",
                params={"type": "web", "code": symbol.lower()},
                timeout=timeout,
            )
            match = re.search(r'id="hidctype"[^>]*value="([^"]+)"', page.text)
            return match.group(1) if match else None
        except Exception as error:  # noqa: BLE001
            logger.debug("取 %s companyType 失败: %s", symbol, error)
            return None

    def _fetch_rows(
        self,
        spec: SupplementSpec,
        code: str,
        exchange: str,
        dates: Sequence[str],
        *,
        timeout: float = 25.0,
    ) -> list[dict[str, Any]]:
        """拉取单只股票的目标报告期数据；失败返回空列表（逐股失败不中断整轮）。"""
        symbol = em_symbol(code, exchange)
        params = {
            "companyType": "4",       # 通用企业；金融股首次空结果时回退 hidctype
            "reportDateType": "0",
            "reportType": "1",        # 1=按报告期（累计）
            "dates": ",".join(dates),
            "code": symbol,
        }
        for attempt in range(MAX_RETRY):
            try:
                response = _session().get(
                    f"{BASE}/{spec.endpoint}", params=params, timeout=timeout,
                )
                response.raise_for_status()
                rows = response.json().get("data") or []
                if rows:
                    return rows
                if attempt == 0:
                    company_type = self._company_type(symbol, timeout)
                    if company_type and company_type != "4":
                        params["companyType"] = company_type
                        continue
                return []
            except Exception as error:  # noqa: BLE001
                if attempt == MAX_RETRY - 1:
                    logger.debug("补采取数失败 %s(%s): %s", code, spec.key, error)
                    return []
                time.sleep(1.5 * (attempt + 1) + random.random())
        return []

    # ─── 解析 ─────────────────────────────────────────────────────

    def _parse_rows(
        self,
        spec: SupplementSpec,
        code: str,
        rows: Sequence[dict[str, Any]],
        *,
        batch_id: str,
        fetched_at: datetime,
        src_hash: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """把源返回的原始行解析成按表分组的落库行（列名显式，不依赖列序）。"""
        parsed: dict[str, list[dict[str, Any]]] = {table: [] for table in spec.columns}
        for row in rows:
            raw_date = str(row.get("REPORT_DATE") or "")[:10]
            try:
                report_date = date.fromisoformat(raw_date)
            except ValueError:
                continue
            if report_date <= CSMAR_CUTOFF:
                continue  # 只补 CSMAR 未覆盖的报告期
            values = {
                column: _to_float(row.get(em_field))
                for em_field, column in spec.field_map.items()
            }
            if not any(value is not None for value in values.values()):
                continue
            base = {
                "stock_code": code,
                "report_date": report_date,
                "report_type": report_type_of(report_date),
                "fetch_time": fetched_at,
                "raw_response_hash": src_hash,
                "confidence": CONFIDENCE,
                "batch_id": batch_id,
            }
            if spec.key == CASHFLOW_SUPPLEMENT:
                indirect = {
                    **base,
                    **{
                        column: values.get(column)
                        for column in INDIRECT_FIELD_MAP.values()
                    },
                    "source": SOURCE,
                    "raw_data": "{}",
                }
                if any(
                    indirect[column] is not None
                    for column in INDIRECT_FIELD_MAP.values()
                ):
                    parsed["cash_flow_indirect"].append(indirect)
                if values.get("capex") is not None:
                    parsed["cash_flow_activity"].append({
                        **base, "capex": values["capex"], "source": SOURCE,
                    })
            elif spec.key == BALANCE_SHEET_EXT:
                parsed["balance_sheet_ext"].append({
                    **base, **values, "source": SOURCE,
                })
            else:
                # update_existing 模式（扣非净利润 / 固定资产净额…）：
                # 只产出「主键 + 目标列」，由 _persist_rows 做 "只填 NULL" 的 UPDATE。
                # 2026-11-14：原实现硬编码 income_statement/deducted_net_profit，
                # 导致新增数据集（fixed_assets）直接 KeyError，这里改为按 spec 驱动的通用形态。
                target_table = next(iter(spec.columns))
                target_columns = spec.columns[target_table]
                row_values = {
                    column: values.get(column)
                    for column in target_columns
                    if column not in ("stock_code", "report_date")
                }
                parsed.setdefault(target_table, []).append({
                    "stock_code": code,
                    "report_date": report_date,
                    **row_values,
                })
        return parsed

    # ─── 落库 ─────────────────────────────────────────────────────

    def _persist_rows(
        self,
        spec: SupplementSpec,
        rows_by_table: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        batch_id: str,
        fetched_at: datetime,
        src_hash: str,
        periods: Sequence[str],
    ) -> dict[str, int]:
        """按数据集写入模式落库，返回 {table: 写入/更新行数}。"""
        written: dict[str, int] = {}
        with self.duck.transaction() as conn:
            for table, columns in spec.columns.items():
                rows = list(rows_by_table.get(table) or [])
                if not rows:
                    written[table] = 0
                    continue
                frame = pd.DataFrame(rows, columns=list(columns))
                if "report_date" in frame.columns:
                    frame["report_date"] = pd.to_datetime(frame["report_date"])
                conn.register("_supp_rows", frame)
                try:
                    if spec.write_mode == "replace_source_rows":
                        conn.execute(
                            f"DELETE FROM {table} WHERE source = ? AND stock_code IN "
                            "(SELECT DISTINCT stock_code FROM _supp_rows)",
                            [SOURCE],
                        )
                        conn.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _supp_rows")
                    else:
                        # 只填 NULL：既有的 CSMAR / 其他源值保持不动（主链只读纪律）。
                        for column in columns:
                            if column in {"stock_code", "report_date"}:
                                continue
                            null_guard = (
                                f" AND t.{column} IS NULL"
                                if spec.fills_null_only else ""
                            )
                            conn.execute(
                                f"UPDATE {table} AS t SET {column} = r.{column} "
                                "FROM _supp_rows AS r "
                                "WHERE t.stock_code = r.stock_code "
                                "AND t.report_date = r.report_date"
                                f" AND r.{column} IS NOT NULL{null_guard}",
                            )
                finally:
                    conn.unregister("_supp_rows")
                written[table] = len(frame)
            conn.execute(
                """INSERT INTO fetch_batch
                   (batch_id, data_type, source, adapter_version, fetch_time,
                    raw_response_hash, row_count, report_date_range, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    batch_id, spec.data_type, SOURCE, spec.api_version, fetched_at,
                    src_hash, sum(written.values()),
                    f"{str(periods[-1])[:10]}~{str(periods[0])[:10]}", CONFIDENCE,
                ],
            )
        return written

    # ─── 主入口 ───────────────────────────────────────────────────

    def refresh(
        self,
        key: str,
        *,
        periods: Sequence[str] | None = None,
        max_stocks: int = 0,
        concurrency: int = 4,
        timeout: float = 25.0,
        deadline_seconds: float = 0.0,
        retry_days: int = 0,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """有界补采一轮：到期由调用方判断，本方法只管"本轮抓哪些、抓多少"。

        Args:
            max_stocks: 本轮最多处理的股票数（0=不设上限，仅测试/手动用）。
            deadline_seconds: 本轮最大耗时（秒）；到点后剩余股票转 deferred，
                下一轮继续（按 max_report_date 升序，进度只前进不回头）。
            retry_days: 失败冷却天数，抓过但仍无该期数据的股票在冷却期内跳过。

        Returns:
            {status, reason, periods, targeted, updated, skipped, failed,
             deferred, rows_written, failed_codes, elapsed_seconds}
        """
        started = time.time()
        spec = SPECS[key]
        resolved_periods = self.resolve_periods(key, periods=periods)
        report: dict[str, Any] = {
            "dataset": key,
            "status": "skipped",
            "reason": None,
            "periods": resolved_periods,
            "targeted": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "deferred": 0,
            "rows_written": {},
            "failed_codes": [],
            "remaining_pending": 0,
            "elapsed_seconds": 0.0,
        }
        if not resolved_periods:
            report["reason"] = "no_target_periods"
            report["elapsed_seconds"] = round(time.time() - started, 2)
            return report
        try:
            # 先取"未截断"的完整待补集合：remaining_pending 是调用方判断
            # "这一域是否已追平"的唯一依据（追平才推进水位，否则下一轮继续）。
            all_pending = self.pending_codes(
                key, resolved_periods, retry_days=retry_days,
            )
            pending = all_pending[:max_stocks] if max_stocks > 0 else all_pending
        except Exception as error:  # noqa: BLE001 - 判据查询失败只影响本数据集
            logger.warning("补采待补查询失败(%s): %s", key, error)
            report["status"] = "failed"
            report["reason"] = f"pending_query_failed: {type(error).__name__}: {error}"
            report["elapsed_seconds"] = round(time.time() - started, 2)
            return report

        # `pending_codes` 已经做过失败冷却过滤与每轮上限截断，这里不再重复。
        if not pending:
            report["reason"] = "no_pending_codes"
            report["elapsed_seconds"] = round(time.time() - started, 2)
            return report

        batch_id = uuid.uuid4().hex
        fetched_at = datetime.now(UTC).replace(tzinfo=None)
        src_hash = f"{spec.endpoint}:{batch_id}"[:32]
        rows_by_table: dict[str, list[dict[str, Any]]] = {table: [] for table in spec.columns}
        fetched_codes: list[str] = []
        failed_codes: list[str] = []
        deferred: list[str] = []

        pool = ThreadPoolExecutor(max_workers=max(1, int(concurrency)))
        futures = {
            pool.submit(
                self._fetch_rows, spec, code, exchange, resolved_periods,
                timeout=timeout,
            ): code
            for code, exchange in pending
        }
        try:
            remaining = set(futures)
            while remaining:
                done, _ = wait(remaining, timeout=1.0, return_when=FIRST_COMPLETED)
                for future in done:
                    remaining.discard(future)
                    code = futures[future]
                    try:
                        rows = future.result()
                    except Exception as error:  # noqa: BLE001 - 逐股失败如实计数
                        logger.debug("补采取数异常 %s: %s", code, error)
                        rows = []
                    if not rows:
                        failed_codes.append(code)
                        continue
                    parsed = self._parse_rows(
                        spec, code, rows,
                        batch_id=batch_id, fetched_at=fetched_at, src_hash=src_hash,
                    )
                    if any(parsed.values()):
                        fetched_codes.append(code)
                    else:
                        failed_codes.append(code)
                    for table, items in parsed.items():
                        rows_by_table[table].extend(items)
                    if progress_cb is not None:
                        progress_cb(code, {
                            "done": len(fetched_codes) + len(failed_codes),
                            "total": len(pending),
                        })
                if deadline_seconds and (time.time() - started) >= deadline_seconds:
                    deferred = [futures[f] for f in remaining]
                    for future in remaining:
                        future.cancel()
                    remaining.clear()
                    break
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        try:
            written = self._persist_rows(
                spec, rows_by_table,
                batch_id=batch_id, fetched_at=fetched_at, src_hash=src_hash,
                periods=resolved_periods,
            ) if fetched_codes else {}
        except Exception as error:  # noqa: BLE001 - 落库失败整批回滚，旧值保留
            logger.warning("补采落库失败(%s): %s", key, error)
            report.update({
                "status": "failed",
                "reason": f"persist_failed: {type(error).__name__}: {error}",
                "targeted": len(pending),
                "failed": len(failed_codes),
                "failed_codes": failed_codes[:20],
                "elapsed_seconds": round(time.time() - started, 2),
            })
            return report

        # 只把**真正尝试过**的股票记入失败冷却：deferred 是"截止时间到了、
        # 还没来得及抓"，若也记进去会被冷却窗口白挡 7 天。
        self._record_attempts(key, list(fetched_codes) + failed_codes)

        updated = len(fetched_codes)
        if updated == 0:
            status = "failed"
            reason = "no_rows_fetched"
        elif failed_codes or deferred:
            status = "partial"
            reason = "partial_source_failures"
        else:
            status = "success"
            reason = None
        report.update({
            "status": status,
            "reason": reason,
            "targeted": len(pending),
            "updated": updated,
            "failed": len(failed_codes),
            "deferred": len(deferred),
            "skipped": max(0, len(pending) - updated - len(failed_codes) - len(deferred)),
            "rows_written": written,
            "failed_codes": failed_codes[:20],
            # 本轮之后还剩多少只待补（含本轮 deferred）。调用方据此决定
            # 是否推进水位：还有残留就下一轮继续，否则会被间隔白挡 7 天。
            "remaining_pending": max(0, len(all_pending) - updated),
            "elapsed_seconds": round(time.time() - started, 2),
        })
        logger.info(
            "补采 %s: targeted=%d updated=%d failed=%d deferred=%d remaining=%d "
            "rows=%s elapsed=%.1fs",
            key, report["targeted"], report["updated"], report["failed"],
            report["deferred"], report["remaining_pending"], written,
            report["elapsed_seconds"],
        )
        return report


def _parse_iso(value: Any) -> datetime | None:
    """解析 data_refresh_state 里的 ISO 时间戳；无法解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


# ─── 金融监管指标（6 列）──────────────────────────────────────────────
#
# 与上面三个数据集不同：该域**每次到期全量重抓**（每只金融股 1 次请求，
# 只取最新报告期 1 行），没有"有没有本源行"的续传判据，因此旧脚本不存在
# 上面那个 bug；接入调度时只需补上到期判断 + 每轮上限。

REGULATORY_REPORT_NAME = "RPT_F10_FINANCE_MAINFINADATA"
# ⚠️ 同名不同义：`CQBL` 对**银行**是核心一级资本充足率，对**券商**是另一个
# 概念（东北证券 5.197）。因此按行业分别映射，跨行业一律不写。
REGULATORY_FIELD_MAP_BANK: dict[str, str] = {
    "NEWCAPITALADER": "capital_adequacy_ratio",
    "FIRST_ADEQUACY_RATIO": "tier1_capital_adequacy_ratio",
    "CQBL": "core_tier1_capital_adequacy_ratio",
    "NONPERLOAN": "non_performing_loan_ratio",
    "BLDKBBL": "provision_coverage_ratio",
}
REGULATORY_FIELD_MAP_BROKER: dict[str, str] = {
    "RISK_COVERAGE": "risk_coverage_ratio",
}
REGULATORY_COLUMNS: tuple[str, ...] = (
    "stock_code", "report_date",
    *dict.fromkeys([
        *REGULATORY_FIELD_MAP_BANK.values(),
        *REGULATORY_FIELD_MAP_BROKER.values(),
    ]),
)
# 与配置一致的行业口径（按名称识别金融股）
FINANCIAL_LIKE = ("银行", "证券", "保险", "信托", "金融", "金控")


def regulatory_field_map(name: str) -> dict[str, str]:
    """按公司名称判定行业 → 返回适用的字段映射（跨行业不写）。"""
    if "银行" in name:
        return REGULATORY_FIELD_MAP_BANK
    if "证券" in name:
        return REGULATORY_FIELD_MAP_BROKER
    return {}


def datacenter_secucode(code: str, exchange: str | None) -> str:
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get((exchange or "").upper())
    if suffix is None:
        suffix = em_symbol(code, exchange)[:2]
    return f"{code}.{suffix}"


class RegulatoryMetricsUpdater:
    """6 个金融监管指标补采（东财 datacenter 主要指标，1 请求/股票）。

    只对银行/券商抓取（保险/信托/金融/金控该指标本不适用，保持 NULL）；
    缺失如实为空，不估算。每轮全量重抓，靠 `max_stocks` + 截止时间有界。
    """

    def __init__(self, duck: DuckDBStore) -> None:
        self.duck = duck

    def universe(self) -> list[tuple[str, str, str]]:
        """金融类股票 (code, exchange, name) —— 按名称识别，与旧脚本一致。"""
        like = " OR ".join(f"name LIKE '%{keyword}%'" for keyword in FINANCIAL_LIKE)
        rows = self.duck.read_query(
            f"""SELECT stock_code, COALESCE(exchange, '') AS exchange, name
                FROM stock_meta
                WHERE is_listed IS TRUE AND ({like})
                ORDER BY stock_code"""
        )
        return [
            (str(row["stock_code"]), str(row["exchange"]), str(row["name"]))
            for row in rows
            if regulatory_field_map(str(row["name"]))
        ]

    def coverage_gap_codes(self) -> set[str]:
        """最新可得报告期里 6 列仍为空的股票（用于把缺口股排在队首）。"""
        try:
            rows = self.duck.read_query(
                """SELECT stock_code FROM balance_sheet
                   WHERE report_date = (
                       SELECT MAX(report_date) FROM balance_sheet
                   ) AND capital_adequacy_ratio IS NULL
                     AND risk_coverage_ratio IS NULL"""
            )
            return {str(row["stock_code"]) for row in rows}
        except Exception as error:  # noqa: BLE001
            logger.debug("读取监管指标缺口失败: %s", error)
            return set()

    def _fetch_row(
        self, secucode: str, *, timeout: float = 25.0,
    ) -> dict[str, Any] | None:
        """取单只股票最新一期的监管指标原始行（测试 monkeypatch 本方法）。"""
        params = {
            "reportName": REGULATORY_REPORT_NAME, "columns": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "pageNumber": "1", "pageSize": "1",
            "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        }
        session = _session()
        session.headers.setdefault("Referer", "https://emweb.securities.eastmoney.com/")
        for attempt in range(MAX_RETRY):
            try:
                response = session.get(DATACENTER_URL, params=params, timeout=timeout)
                response.raise_for_status()
                rows = (response.json().get("result") or {}).get("data") or []
                return rows[0] if rows else None
            except Exception as error:  # noqa: BLE001
                if attempt == MAX_RETRY - 1:
                    logger.debug("监管指标取数失败 %s: %s", secucode, error)
                    return None
                time.sleep(1.5 * (attempt + 1))
        return None

    def refresh(
        self,
        *,
        max_stocks: int = 200,
        deadline_seconds: float = 0.0,
        timeout: float = 25.0,
        pause_seconds: float = 0.35,
        progress_cb: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """全量重抓一轮（有界）；只 UPDATE `balance_sheet` 既有行，不 INSERT。"""
        started = time.time()
        report: dict[str, Any] = {
            "status": "skipped",
            "reason": None,
            "targeted": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "deferred": 0,
            "rows_updated": 0,
            "coverage_rows": 0,
            "failed_codes": [],
            "elapsed_seconds": 0.0,
        }
        try:
            universe = self.universe()
        except Exception as error:  # noqa: BLE001
            report["status"] = "failed"
            report["reason"] = f"universe_query_failed: {type(error).__name__}: {error}"
            report["elapsed_seconds"] = round(time.time() - started, 2)
            return report
        if not universe:
            report["reason"] = "no_financial_stocks"
            report["elapsed_seconds"] = round(time.time() - started, 2)
            return report
        gap_codes = self.coverage_gap_codes()
        # 缺口股优先，其余按代码序（保证有界续传只前进不回退）。
        ordered = sorted(universe, key=lambda item: (item[0] not in gap_codes, item[0]))
        if max_stocks > 0:
            ordered = ordered[:max_stocks]

        records: list[dict[str, Any]] = []
        failed_codes: list[str] = []
        deferred = 0
        for index, (code, exchange, name) in enumerate(ordered):
            if deadline_seconds and (time.time() - started) >= deadline_seconds:
                deferred = len(ordered) - index
                break
            field_map = regulatory_field_map(name)
            row = self._fetch_row(datacenter_secucode(code, exchange), timeout=timeout)
            if row is None:
                failed_codes.append(code)
                continue
            raw_date = str(row.get("REPORT_DATE") or "")[:10]
            try:
                report_date = date.fromisoformat(raw_date)
            except ValueError:
                failed_codes.append(code)
                continue
            values = {
                column: _to_float(row.get(em_field))
                for em_field, column in field_map.items()
            }
            if not any(value is not None for value in values.values()):
                # 该接口对本股无监管数据（保险/信托常见）：如实跳过并计数，
                # 不写 NULL 覆盖既有值。
                continue
            records.append({
                "stock_code": code, "report_date": report_date, **values,
            })
            if progress_cb is not None:
                progress_cb(code, {"done": index + 1, "total": len(ordered)})
            if pause_seconds > 0:
                time.sleep(pause_seconds)

        written: dict[str, int] = {"rows_updated": 0, "coverage_rows": 0}
        if records:
            try:
                written = self._persist(records)
            except Exception as error:  # noqa: BLE001
                report.update({
                    "status": "failed",
                    "reason": f"persist_failed: {type(error).__name__}: {error}",
                    "targeted": len(ordered),
                    "failed": len(failed_codes),
                    "failed_codes": failed_codes[:20],
                    "elapsed_seconds": round(time.time() - started, 2),
                })
                return report

        missing = len(ordered) - len(records) - len(failed_codes) - deferred
        if not records:
            status = "failed" if failed_codes else "skipped"
        elif failed_codes or deferred:
            status = "partial"
        else:
            status = "success"
        report.update({
            "status": status,
            "reason": None if status in {"success", "skipped"} else "partial_source_failures",
            "targeted": len(ordered),
            "updated": len(records),
            "failed": len(failed_codes),
            "deferred": deferred,
            "skipped": max(0, missing),
            "rows_updated": written["rows_updated"],
            "coverage_rows": written["coverage_rows"],
            "failed_codes": failed_codes[:20],
            "elapsed_seconds": round(time.time() - started, 2),
        })
        if status == "skipped":
            report["reason"] = "no_regulatory_rows"
        logger.info(
            "监管指标补采: targeted=%d updated=%d failed=%d deferred=%d rows=%d elapsed=%.1fs",
            report["targeted"], report["updated"], report["failed"],
            report["deferred"], report["rows_updated"], report["elapsed_seconds"],
        )
        return report

    def _persist(self, records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """按 (stock_code, report_date) UPDATE 既有三表行；不 INSERT、不 DELETE。

        Returns:
            {"rows_updated": 本轮写入的候选行数, "coverage_rows": 更新的非空覆盖行数}
            —— 前者是本轮动作量，后者是"资本充足率非空"的全库余额，两者含义
            不同，旧脚本只打印后者容易被读成"本轮更新了 8 千行"。
        """
        frame = pd.DataFrame(list(records), columns=list(REGULATORY_COLUMNS))
        frame["report_date"] = pd.to_datetime(frame["report_date"])
        value_columns = [column for column in REGULATORY_COLUMNS
                         if column not in {"stock_code", "report_date"}]
        with self.duck.transaction() as conn:
            conn.register("_reg_rows", frame)
            try:
                for column in value_columns:
                    conn.execute(
                        f"UPDATE balance_sheet AS t SET {column} = r.{column} "
                        "FROM _reg_rows AS r "
                        "WHERE t.stock_code = r.stock_code "
                        "AND t.report_date = r.report_date "
                        f"AND r.{column} IS NOT NULL",
                    )
                updated = int(conn.execute(
                    "SELECT COUNT(*) FROM balance_sheet "
                    "WHERE capital_adequacy_ratio IS NOT NULL"
                ).fetchone()[0])
                batch_id = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO fetch_batch
                       (batch_id, data_type, source, adapter_version, fetch_time,
                        raw_response_hash, row_count, report_date_range, confidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        batch_id, REGULATORY_METRICS, SOURCE,
                        "em-datacenter-mainfinadata",
                        datetime.now(UTC).replace(tzinfo=None), batch_id[:32],
                        len(frame),
                        f"{frame['report_date'].min().date()}~"
                        f"{frame['report_date'].max().date()}",
                        CONFIDENCE,
                    ],
                )
            finally:
                conn.unregister("_reg_rows")
        return {"rows_updated": len(frame), "coverage_rows": updated}
