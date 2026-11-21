"""Read-only data quality signals for the status API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.financial_period import cn_today
from app.core.missing_conditions import actionable_missing_condition
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

DEFAULT_MINIMUM_HISTORY_OBSERVATIONS = 1_300
DEFAULT_MINIMUM_VOLUME_OBSERVATIONS = 1_300
# 价格/快照价格日的「陈旧上限」（PRD §6.4 D7：允许陈旧但必须显示日期）。
# 2026-11-21 前这个 7 天在 `has_fresh_prices` 的 SQL 字面量、`SNAPSHOT_STALE`
# 的 `_is_stale(..., days=7)`、以及快照价格日的一致性判据里各写一份（或根本
# 没有容差）。现在收敛成唯一常量：**改口径只改这里**。
PRICE_FRESHNESS_MAX_AGE_DAYS = 7
READINESS_CACHE_KEY = "minimum_data_readiness"
READINESS_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
# ─── 筛选门禁的告警三级（2026-11-20 裁定，依据 reports/122 §2.3 J9 + S2 §2.3）───
#
# 三级语义与 `app/core/quality/asset_health.py` 的 severity 完全同源：
#   block     = 该告警意味着**筛选结果不可信**（读不到列 / 无法判定窗口），
#               必须整库拦截；
#   disclose  = 数据有瑕疵但结论仍可用：**不拦**，但必须在响应里如实披露
#               （`disclosed_warnings`），让用户自己判断；
#   inventory = 纯盘点，既不拦也不进告警展示之外的地方。
#
# 判据（2026-11-20 正式库实测，见 reports/150）：
#   * 旧表把 6 条统统当硬阻断，于是"数据有点瑕疵"= "整个筛选不能用"，
#     用户点「运行筛选」只得到「数据质量不满足筛选要求 / 去数据状态页看」。
#   * 其中 4 条快照域告警（FINANCIAL_SHELL_ROWS / SNAPSHOT_STALE /
#     CODE_IDENTITY_ALIAS / LINEAGE_INVALID）**已经有唯一的硬阻断实现**
#     —— `_SNAPSHOT_BLOCKING_WARNINGS` + `indicator_trust()` +
#     `app/web/api/screening.py::_require_trusted_snapshot()`（409
#     `snapshot_values_untrusted`，并同步遮蔽详情页指标）。本表把它们降为
#     disclose **不是撤掉阻断，而是删掉重复的第二道 409**：真正让数字不可
#     信时，快照信任门禁仍然先于本表拦截，且给出的原因更具体、详情页结论
#     与筛选页保持一致。把同一条告警在两个地方各判一次，只会让用户看到
#     更含糊的那条消息。
#   * LIVE_SCHEMA_INCOMPATIBLE / TRADING_CALENDAR_UNAVAILABLE 没有其它实现，
#     且真的会让结果错，**保持 block**。
SCREENING_SEVERITY_BLOCK = "block"
SCREENING_SEVERITY_DISCLOSE = "disclose"
SCREENING_SEVERITY_INVENTORY = "inventory"

_SCREENING_WARNING_SEVERITY: dict[str, str] = {
    # 实时 schema 不兼容：列都读不到，"筛选结果"无从谈起 —— 硬阻断。
    "LIVE_SCHEMA_INCOMPATIBLE": SCREENING_SEVERITY_BLOCK,
    # 交易日历整体不可用（表空/查询失败/系统性缺日期）→ 无法判定窗口 —— 硬阻断。
    # 注意：日历"存在但个别标的覆盖不全"走的是另一个码
    # `TRADING_CALENDAR_INCOMPLETE`（disclose），见 `_trading_calendar_coverage`。
    "TRADING_CALENDAR_UNAVAILABLE": SCREENING_SEVERITY_BLOCK,
    # ↓ 以下 4 条的快照域硬阻断由 `_SNAPSHOT_BLOCKING_WARNINGS` 唯一负责。
    "FINANCIAL_SHELL_ROWS": SCREENING_SEVERITY_DISCLOSE,
    "SNAPSHOT_STALE": SCREENING_SEVERITY_DISCLOSE,
    "CODE_IDENTITY_ALIAS": SCREENING_SEVERITY_DISCLOSE,
    "LINEAGE_INVALID": SCREENING_SEVERITY_DISCLOSE,
    # 日历存在但少量标的的日历覆盖不匹配：只有这几只的窗口计数可能偏移，
    # 不影响其余标的的排名 —— 披露，不拦（正式库实测 197/5,580 = 3.5%）。
    "TRADING_CALENDAR_INCOMPLETE": SCREENING_SEVERITY_DISCLOSE,
}

# 只有 block 级才 ready=False。**从表派生，禁止另处手写清单**：
# 新增 block 必须改 `_SCREENING_WARNING_SEVERITY`（单点），回归护栏
# `tests/regression/test_screening_readiness_severity.py` 会盯住这层派生关系。
_SCREENING_BLOCKING_WARNINGS = frozenset(
    code for code, severity in _SCREENING_WARNING_SEVERITY.items()
    if severity == SCREENING_SEVERITY_BLOCK
)
# 2026-11-12 契约 §3：发布门禁的硬阻断阈值。违规票 ≤ 阈值时逐股隔离发布，
# 超过阈值才认为"系统性单位错乱"并整批拒绝。
# 正式库 ~5,580 只上市股票 → 阈值 max(50, 55) = 55 只。
GATE_HARD_BLOCK_MIN = 50
GATE_HARD_BLOCK_RATIO = 0.01
# 2026-11-13 P1：绝对阈值在小库/退化库上形同虚设——上市股 <101 只时阈值恒为 50，
# 于是 30 只库里 30 只全违规也只会被逐股隔离、ready=True、发布集合空（等价于
# "永不出新快照"）。补一条与库大小无关的占比判据：**≥1/3 的上市股同时违规**
# 一律视为系统性错乱。分母 3 用整数比较实现，避免浮点边界问题。
GATE_HARD_BLOCK_SHARE_DIVISOR = 3


def checking_data_readiness() -> dict:
    """Return a conservative placeholder while the background check runs."""
    return {
        "ready": False,
        "checking": True,
        "cached": False,
        "stock_count": 0,
        "missing": {},
        "missing_counts": {},
        "schema_compatibility": {"compatible": True, "missing": []},
    }


def read_cached_data_readiness(sqlite: SQLiteStore) -> dict | None:
    """Load the last completed readiness check from the profile SQLite store."""
    try:
        rows = sqlite.query(
            "SELECT value, updated_at FROM data_refresh_state WHERE key = ?",
            [READINESS_CACHE_KEY],
        )
        if not rows:
            return None
        checked_at = datetime.fromisoformat(rows[0]["updated_at"])
        if checked_at.tzinfo is None:
            checked_at = checked_at.astimezone()
        if (datetime.now().astimezone() - checked_at).total_seconds() > READINESS_CACHE_MAX_AGE_SECONDS:
            return None
        value = json.loads(rows[0]["value"])
        if not isinstance(value, dict) or "ready" not in value:
            return None
        value["checking"] = False
        value["cached"] = True
        value["checked_at"] = rows[0]["updated_at"]
        return value
    except Exception:
        return None


def store_cached_data_readiness(sqlite: SQLiteStore, readiness: dict) -> dict:
    """Persist one completed check and return its display-ready representation."""
    checked_at = datetime.now().astimezone().isoformat()
    value = dict(readiness)
    value.pop("checking", None)
    value.pop("cached", None)
    value.pop("checked_at", None)
    sqlite.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        [READINESS_CACHE_KEY, json.dumps(value, ensure_ascii=False, default=str), checked_at],
    )
    value["checking"] = False
    value["cached"] = False
    value["checked_at"] = checked_at
    return value


def _screening_warning_detail(
    code: str, quality: dict, calendar: dict,
) -> dict:
    """Per-code evidence payload for a screening warning (never raises)."""
    try:
        if code == "TRADING_CALENDAR_UNAVAILABLE":
            return {
                "calendar_days": calendar.get("calendar_days"),
                "affected_stocks": calendar.get("affected_stocks"),
                "listed_stocks": calendar.get("listed_stocks"),
            }
        if code == "TRADING_CALENDAR_INCOMPLETE":
            return {
                "calendar_days": calendar.get("calendar_days"),
                "affected_stocks": calendar.get("affected_stocks"),
                "listed_stocks": calendar.get("listed_stocks"),
                "raw_gap_excess_stocks": calendar.get("raw_gap_excess_stocks"),
                "qfq_gap_excess_stocks": calendar.get("qfq_gap_excess_stocks"),
                "raw_stocks_outside_calendar": calendar.get("raw_stocks_outside_calendar"),
                "qfq_stocks_outside_calendar": calendar.get("qfq_stocks_outside_calendar"),
            }
        if code == "SNAPSHOT_STALE":
            dates = quality.get("dates") or {}
            snapshot = dates.get("indicator_snapshot") or {}
            return {
                "price_date": dates.get("price"),
                "snapshot_price_date": snapshot.get("latest_price_date"),
            }
        if code == "FINANCIAL_SHELL_ROWS":
            dates = quality.get("dates") or {}
            return {
                "balance_sheet": dates.get("balance_sheet"),
                "income_statement": dates.get("income_statement"),
            }
        if code == "LINEAGE_INVALID":
            lineage = quality.get("lineage") or {}
            return {
                key: lineage.get(key)
                for key in (
                    "invalid_hash_rows", "orphan_batch_rows",
                    "archive_gap_rows", "hash_mismatch_rows",
                )
                if key in lineage
            }
        if code == "CODE_IDENTITY_ALIAS":
            return dict(quality.get("code_identity") or {})
    except Exception:  # noqa: BLE001 - 披露细节坏掉不能反过来阻断筛选
        logger.warning("筛选告警披露细节构建失败(非致命): %s", code, exc_info=True)
    return {}


def screening_readiness(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    """Return one fail-closed policy decision for every screening entry point.

    2026-11-20（reports/150）**告警三级**：此前这里把 6 条告警写成硬阻断集合，
    于是任何一条命中 = 整个筛选功能不可用（用户实测：「运行筛选」只弹出
    「数据质量不满足筛选要求」）。现在按 `_SCREENING_WARNING_SEVERITY` 分级：

      * ``ready`` 只由 **block 级**决定（``_SCREENING_BLOCKING_WARNINGS``）；
      * disclose/inventory 级 **不拦**，但必须进 ``disclosed_warnings``，
        并带 ``warnings``（code + severity + detail）供前端如实展示；
      * ``warning_codes`` **语义不变**：仍是"硬阻断码"的排序列表（老客户端
        只读这个键也不会误判成"放行"）。
    """
    quality = build_data_quality_status_cached(duck, sqlite)
    calendar = _trading_calendar_coverage(duck, sqlite)
    codes = list(quality["warning_codes"])
    verdict = calendar.get("verdict")
    if verdict == "unavailable":
        codes.append("TRADING_CALENDAR_UNAVAILABLE")
    elif verdict == "partial":
        codes.append("TRADING_CALENDAR_INCOMPLETE")
    # 字典序去重：同一码只出现一次，且顺序稳定（缓存/指纹比对要看稳定性）。
    warnings = []
    seen: set[str] = set()
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        warnings.append({
            "code": code,
            # 未登记的码按 disclose 处理 = 维持旧行为（旧表里它们都不阻断）。
            "severity": _SCREENING_WARNING_SEVERITY.get(code, SCREENING_SEVERITY_DISCLOSE),
            "detail": _screening_warning_detail(code, quality, calendar),
        })
    buckets: dict[str, list[str]] = {
        SCREENING_SEVERITY_BLOCK: [],
        SCREENING_SEVERITY_DISCLOSE: [],
        SCREENING_SEVERITY_INVENTORY: [],
    }
    for warning in warnings:
        buckets[warning["severity"]].append(warning["code"])
    blocking_warnings = sorted(buckets[SCREENING_SEVERITY_BLOCK])
    return {
        # 2026-08-27：筛选/导出不再被少数股票的数据缺口全局阻断。
        # 2026-11-20：连"数据有点瑕疵"也不再阻断 —— 只有 block 级才不 ready。
        "ready": not blocking_warnings,
        "readiness": quality["minimum_data_readiness"],
        # 向后兼容：语义仍是"硬阻断码"。
        "warning_codes": blocking_warnings,
        "blocking_warnings": blocking_warnings,
        "disclosed_warnings": sorted(buckets[SCREENING_SEVERITY_DISCLOSE]),
        "inventory_warnings": sorted(buckets[SCREENING_SEVERITY_INVENTORY]),
        "warnings": warnings,
    }

SCREENING_READINESS_CACHE_KEY = "screening_readiness_cache"
SCREENING_READINESS_CACHE_TTL_SECONDS = 600
# 2026-11-18 缺陷 B：allow_stale 不再是"过期就无限期返回"。
# TTL(600s) 只是"该刷新了"的触发点；决策的**正确性边界**是硬上限 2×TTL。
# 超过硬上限仍返回旧决策 = 对外宣称就绪的门禁可以永远基于三天前的判定
# （后台 worker 遇写锁直接 return、异常只 logger.warning，都不会落盘，
# 于是过期决策可以无限期存活）。2×TTL 的依据：一次完整后台刷新在正式库
# 上约 20-60s，一个自动更新窗口约几分钟；给一个完整 TTL 的重试余量足以
# 覆盖"刷新正在进行"，再久就说明刷新确实坏了，必须同步重算而不是继续骗。
SCREENING_READINESS_STALE_MAX_AGE_SECONDS = 2 * SCREENING_READINESS_CACHE_TTL_SECONDS
# 2026-11-20 性能修复（用户实测"筛选要等 20-30 秒"）：上面的 2×TTL 硬上限只在
# **指纹不可信**时才必要。指纹一旦是"内容敏感"的（未降级 + 有 content_epoch），
# 它就等价于输入内容的哈希：**同指纹 = 同输入**，那条决策仍然是当前输入的正确
# 函数值，墙钟年龄不提供任何额外的正确性保证。此时把硬上限放到 24h，只作为
# "世界变了但指纹看不见"（外部 CLI 直写库等）的最终兜底；TTL(600s) 仍然是
# **刷新触发点**，每次命中都会 single-flight 后台重算，所以正常情况决策几分钟
# 内就是新的。实测：正式库空闲态每次"缓存超过 20 分钟"都要在请求线程里同步
# 重建一次全量质量（25-37s），这是用户看到的主要卡顿。
SCREENING_READINESS_CONFIDENT_STALE_MAX_AGE_SECONDS = 24 * 60 * 60
SCREENING_READINESS_REFRESH_STATE_KEY = "screening_readiness_refresh_state"
FULL_DATA_QUALITY_CACHE_KEY = "data_quality_status_cache_v1"


def load_full_data_quality_cache(sqlite: SQLiteStore, fingerprint: str) -> dict | None:
    """Load a cached full data-quality build for an exact input fingerprint."""
    try:
        rows = sqlite.query(
            "SELECT value, updated_at FROM data_refresh_state WHERE key = ?",
            [FULL_DATA_QUALITY_CACHE_KEY],
        )
        if not rows or not rows[0].get("value"):
            return None
        value = json.loads(rows[0]["value"])
        if not isinstance(value, dict) or value.get("fingerprint") != fingerprint:
            return None
        value["cached_full_build"] = True
        value["cached_at"] = rows[0]["updated_at"]
        return value
    except Exception:
        return None


def store_full_data_quality_cache(
    sqlite: SQLiteStore, fingerprint: str, quality: dict,
) -> None:
    value = dict(quality)
    value["fingerprint"] = fingerprint
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO data_refresh_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            [
                FULL_DATA_QUALITY_CACHE_KEY,
                json.dumps(value, ensure_ascii=False, default=str),
                datetime.now(UTC).isoformat(),
            ],
        )


def build_data_quality_status_cached(
    duck: DuckDBStore, sqlite: SQLiteStore,
) -> dict:
    """Incremental full-quality build: reuse the persisted result while inputs unchanged.

    2026-09-03 优化：此前数据状态页每个刷新周期都执行一次约 20-80s 的
    4300 万行 source_audit 全量扫描。现在用与筛选就绪缓存同源的轻量指纹
    判断输入是否变化；更新完成后指纹变化才重算一次，空闲期直接复用缓存。
    """
    fingerprint = screening_readiness_cache_key(duck, sqlite)
    if fingerprint:
        cached = load_full_data_quality_cache(sqlite, fingerprint)
        if cached is not None:
            return cached
    quality = build_data_quality_status(duck, sqlite)
    if fingerprint:
        try:
            store_full_data_quality_cache(sqlite, fingerprint, quality)
        except Exception:
            logger.warning("全量数据质量缓存写入失败(非致命)")
    return quality


# ---------------------------------------------------------------------------
# 缺陷 A 修复（2026-11-18）：让筛选就绪指纹"能看见内容变化"
#
# 旧指纹对 12 张表只取 COUNT(*) + MIN/MAX(...)，对**就地 UPDATE / 内部替换**
# 完全透明：交易日历把 03-04、03-05 两个工作日换成 03-07、03-08 两个周末后，
# (COUNT, MIN, MAX) 三元组一模一样 → 指纹不变 → 缓存命中 → 返回
# ready=True，而 `_has_complete_trading_calendar()` 按代码语义应为 False
# （对外"假绿"）。
#
# "加 MAX(updated_at)" 方案**不成立**——schema 已逐表实测确认
# (app/core/storage/schema.py)：
#   trading_dates                            → 只有 trade_date TEXT PRIMARY KEY
#   balance_sheet/income_statement/cash_flow → 完全没有 created_at/updated_at
#   xdxr                                     → 没有时间戳列
#   manual_overrides                         → 有 created_at，但改 payload 不刷新
#   source_audit / fetch_batch               → 有 created_at / fetch_time
#   indicator_snapshot                       → 有 calculated_at
#
# 也**不能**一键全量 checksum：source_audit 在正式库约 4300 万行，全表内容
# 摘要会把 20s 缓存的价值抹平。故分层：
#   1) 行数有界的小表（交易日历、人工覆盖）→ **精确内容摘要**（sha256，
#      group_concat 单次传输，亚毫秒级）；
#   2) 中等表（财务三表/分红/除权）→ DuckDB 行级 bit_xor(hash(row))，
#      一次列扫描，实测 ≈4.4ms/百万行，且带行数预算上限兜底；
#   3) 超大表（source_audit / *_archive / 价格表）→ 保持 COUNT + MAX(时间戳)，
#      残余盲区（就地翻 reason_code 之类）在报告里显式登记；根治需要 schema
#      层加 updated_at 列或统一的 content epoch，均在本文件租约之外。
_CONTENT_CHECKSUM_ROW_BUDGET = 2_000_000
# 表 → 需要从行内容摘要中排除的重列。raw_data 存 500+ 原始字段（单行可达
# 数 KB），readiness/筛选只读解析后的列；计入内容摘要会把指纹从毫秒级抬到
# GB 级扫描。dividends/xdxr 按 schema 没有这类列（schema 已实测确认）。
_CONTENT_CHECKSUM_TABLES: dict[str, str] = {
    "balance_sheet": " EXCLUDE (raw_data)",
    "income_statement": " EXCLUDE (raw_data)",
    "cash_flow": " EXCLUDE (raw_data)",
    "dividends": "",
    "xdxr": "",
}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _trading_calendar_fingerprint(sqlite: SQLiteStore) -> dict:
    """Exact content digest of the persisted trading calendar.

    COUNT+MIN+MAX 对"内部替换"透明（03-04/03-05 → 03-07/03-08 三值全等），
    而 `_has_complete_trading_calendar()` 的判定**真的依赖完整日期集合**，
    且对"价格表有、日历里没有"的日期零容忍；所以这张表必须对内容敏感。
    正式库约 5k 行（20 年 × ~250 天），group_concat 单次传输 + sha256 实测
    在毫秒以下，不影响 20s 缓存的收益。
    """
    try:
        rows = sqlite.query(
            """SELECT COUNT(*) AS n, MIN(trade_date) AS lo, MAX(trade_date) AS hi,
                      group_concat(trade_date, ',' ORDER BY trade_date) AS joined
               FROM trading_dates"""
        )
    except Exception:
        # SQLite < 3.44 不支持聚合内 ORDER BY：退化为逐行取回（仍远小于一次
        # 全量 readiness 构建），绝不让整个指纹失效。
        joined = ",".join(
            str(row["trade_date"])
            for row in sqlite.query(
                "SELECT trade_date FROM trading_dates ORDER BY trade_date"
            )
        )
        rows = [{"n": None, "lo": None, "hi": None, "joined": joined}]
    row = rows[0] if rows else {}
    return {
        "n": row.get("n"),
        "lo": row.get("lo"),
        "hi": row.get("hi"),
        "digest": _sha256_text(row.get("joined") or ""),
    }


def _manual_overrides_fingerprint(sqlite: SQLiteStore) -> dict:
    """Exact content digest of the published manual overrides.

    `manual_overrides` 只有 created_at（无 updated_at）：发布后改 payload、
    改状态都不刷新 created_at，COUNT + MAX(created_at) 完全看不见内容变化。
    该表行数是个位到百位量级，直接做精确内容摘要。
    """
    rows = sqlite.query(
        """SELECT COUNT(*) AS n, MAX(created_at) AS created_max,
                  group_concat(
                      id || '|' || stock_code || '|' || field_name || '|' ||
                      COALESCE(CAST(report_date AS TEXT), '') || '|' ||
                      COALESCE(CAST(override_value AS TEXT), '') || '|' ||
                      COALESCE(reason, '') || '|' ||
                      COALESCE(correction_template, '') || '|' ||
                      COALESCE(status, ''),
                      ';' ORDER BY stock_code, field_name, report_date, id
                  ) AS joined
           FROM manual_overrides
           WHERE status = 'published' AND rolled_back_at IS NULL"""
    )
    row = rows[0] if rows else {}
    return {
        "n": row.get("n"),
        "created_max": row.get("created_max"),
        "digest": _sha256_text(row.get("joined") or ""),
    }


# ---------------------------------------------------------------------------
# 盲区 2/3/4 修复（2026-11-19 复审续修）
#
# 上一轮修好了日历 / 人工覆盖 / 财务表，但审查实测点名仍留 4 个盲区：
#   ① `source_audit`（正式库实测 22,421,205 行）**就地翻标记**：旧信号只有
#      COUNT(*) + MAX(fetch_time)/MAX(created_at)。改某行的 confidence（标记列，
#      也是 LINEAGE_INVALID 一类阻断码的判定输入）行数与时间戳全不变 →
#      指纹不变 → 缓存命中 → 对外 `ready=True`（假绿）。
#   ② 归档系（`source_audit_archive` / `fetch_batch`）同理。
#   ③ 价格两表**从未进过外层指纹**（只在内层门禁用 COUNT/MIN/MAX）：
#      外层一命中缓存，内层根本没机会跑 → 价格数据怎么变外层都不动。
#   ④ 超过 `_CONTENT_CHECKSUM_ROW_BUDGET` 时**静默**退回 count-only。
#
# 为什么**不**做全表行级 checksum（正式库只读实测，5 次取中位数，单条 SQL 的
# 增量成本；"锚点"= 一条 `SELECT 1` 的 92ms 连接开销已扣除）：
#   bit_xor(hash(struct_pack(*COLUMNS(*)))) on source_audit   ≈ +854ms
#   SUM(hash(confidence))                      22.4M 行      ≈ +300ms
#   bit_xor(hash(confidence))                                 ≈  +2ms,但**不可用**
#   GROUP BY confidence 的分布拼接（本方案）                  ≈  +69ms
# `bit_xor(hash(col))` 虽然最便宜，却是**奇偶敏感**的：偶数列的同值会完全抵消
# 成 0 —— 实测 `price_daily_qfq` 整列 close 相同、行数为偶数时摘要恒为 0，
# 整列改动也看不出来（等于给了一个假安全感）。所以改用**分布级**信号：
# 按字典编码列分组 + 按 key 排序拼接，翻标记必然改变分布、重复值不抵消、
# 且不需要把每行的值物化后逐个 hash。
#
# 语义边界（不夸大，报告里同样登记）：
#   * 只覆盖列出的列。改未列出的列（value / source / api_version /
#     raw_response_hash / reason_code）不改变外层指纹 —— raw_response_hash 是
#     64 字符高基数列，`SUM(hash())` 实测 +300ms、`bit_xor` 又会抵消，都不可用；
#     reason_code 同为自由文本。
#   * 分布只看 (key, 行数) 这一对面：把两行的 key **互换**不改变分布。
_DISTRIBUTION_SIGNALS: dict[str, str] = {
    # 表 → 「分布级」标量子查询 SQL。**分组聚合**（几十个组）而不是行级摘要：
    #   ① 翻标记（confidence 由 high→low）一定改变分布 → 指纹变化；
    #   ② 代价是"按字典编码列分组"，比逐行 hash 便宜（正式库实测 +35ms）；
    #   ③ 结果按 key 排序拼接 ⇒ 与行序无关、重复值不抵消。
    # ⚠️ 不要用 `SUM(hash(col))` / `bit_xor(hash(col))` 代替：
    #   - SUM(hash()) 在 22.4M 行上实测 +300ms 以上（要把每行的值都物化再算哈希）；
    #   - bit_xor 更是**奇偶敏感**的：偶数列的同值会完全抵消成 0
    #     （实测 price_daily_qfq 整列 close 相同、行数为偶数时
    #     `bit_xor(hash(close))` 恒为 0，整列改动也看不出来）。
    # 拼接只发生在**分组结果**上（组数个位到几十个），所以 `||` 不产生行级开销。
    "source_audit": """(SELECT string_agg(k || '=' || n, '|' ORDER BY k) FROM
        (SELECT CAST(confidence AS VARCHAR) AS k, COUNT(*) AS n
           FROM source_audit GROUP BY confidence))""",
    "source_audit_archive": """(SELECT string_agg(k || '=' || n, '|' ORDER BY k) FROM
        (SELECT CAST(confidence AS VARCHAR) AS k, COUNT(*) AS n
           FROM source_audit_archive GROUP BY confidence))""",
    "fetch_batch": """(SELECT string_agg(k || '=' || n, '|' ORDER BY k) FROM
        (SELECT CAST(data_type AS VARCHAR) AS k, COUNT(*) AS n
           FROM fetch_batch GROUP BY data_type))""",
}
# 价格表：外层指纹此前**一张都没有**（只在内层门禁用 COUNT/MIN/MAX，而外层
# 一旦命中缓存，内层根本没机会跑）。现在补 COUNT(*)。
#
# 被性能红线砍掉的候选信号（正式库实测，read_only，5 次中位数，单条 SQL 的
# 增量成本）：`MAX(trade_date)` +180ms（primary key 是 (stock_code, trade_date)，
# trade_date 不在排序前缀上，zone map 帮不上忙）；按年分桶 COUNT +204ms；
# 按年 COUNT + `SUM(round(close*100)::BIGINT)`（能看见"就地重算"的内容信号）
# +275ms。三者任一都会把就绪判断的耗时翻倍（见报告的成绩对比表），故全部不进
# 常态路径 —— **残余盲区：qfq 就地重算（不新增行）外层仍然看不见**，已登记。
_PRICE_FINGERPRINT_TABLES: tuple[str, ...] = ("price_daily_raw", "price_daily_qfq")

# 降级可见性（盲区 4）：指纹 → 本次就绪判定是否降级。
_FINGERPRINT_DEGRADATION_LIMIT = 8
_fingerprint_degradation: dict[str, dict] = {}
_fingerprint_degradation_lock = threading.Lock()
_last_degraded_logged: tuple | None = None


def _remember_fingerprint_degradation(fingerprint: str, degraded: dict) -> None:
    """Record + log the degradation of one fingerprint (never raises)."""
    global _last_degraded_logged
    with _fingerprint_degradation_lock:
        _fingerprint_degradation[fingerprint] = degraded
        while len(_fingerprint_degradation) > _FINGERPRINT_DEGRADATION_LIMIT:
            _fingerprint_degradation.pop(next(iter(_fingerprint_degradation)))
        signature = (fingerprint, tuple(degraded.get("count_only_tables") or ()))
        already_logged = _last_degraded_logged == signature
        _last_degraded_logged = signature
    if not already_logged:
        # 静默降级 = 假绿的另一种形态。每次降级**首次**出现时留一条 WARNING，
        # 让运维在日志里能看出"这次就绪判断是降级的"。
        logger.warning(
            "筛选就绪指纹降级为 count-only（这些表的就地改动不再可见）: "
            "tables=%s reasons=%s fingerprint=%s",
            degraded.get("count_only_tables"),
            degraded.get("reasons"),
            fingerprint[:12],
        )


def fingerprint_degradation(fingerprint: str | None) -> dict | None:
    """Return how the given readiness fingerprint degraded, or ``None``.

    盲区 4 的对外出口：调用方（web 门禁 / CLI）拿到的指纹若降级，这里能查到
    ``{"active": True, "count_only_tables": [...], "reasons": {...}}``。
    进程重启后历史指纹的降级记录会丢——降级信息本身**每次重算都会重新产生**，
    不需要持久化。
    """
    if not fingerprint:
        return None
    with _fingerprint_degradation_lock:
        recorded = _fingerprint_degradation.get(fingerprint)
        return dict(recorded) if recorded else None


def _duck_content_checksums(
    duck: DuckDBStore, row_counts: dict[str, int],
) -> dict[str, dict]:
    """Order-insensitive row-content checksums, **one** DuckDB round trip.

    `bit_xor(hash(<整行 struct>))` 对行序不敏感、对任意列内容变化敏感
    （就地 UPDATE 也改变），且只需一次列扫描。

    关键成本事实（本机实测，正式库同量级）：`DuckDBStore.read_query` 每次
    都要新开连接，单次约 24ms（28GB 正式库上实测 60-140ms），**连接数才是
    指纹的主要成本**。所以这里把所有被保护的表并成一条 `UNION ALL` 查询，
    按行数预算决定哪些表走内容摘要、哪些表退回纯 COUNT。

    盲区 4 修复：退回 count-only 的表会带 `degraded_reason`
    （``row_budget_exceeded`` / ``checksum_error``），供上层汇总成 payload 里的
    ``degraded`` 字段并落一条 WARNING —— 不再静默降级。
    """
    payload: dict[str, dict] = {}
    targets: list[str] = []
    for table in _CONTENT_CHECKSUM_TABLES:
        count = int(row_counts.get(table) or 0)
        payload[table] = {"c": count}
        if count > _CONTENT_CHECKSUM_ROW_BUDGET:
            payload[table]["mode"] = "count-only"
            payload[table]["degraded_reason"] = "row_budget_exceeded"
        else:
            targets.append(table)
    if not targets:
        return payload
    statements = []
    for table in targets:
        exclude = _CONTENT_CHECKSUM_TABLES[table]
        statements.append(
            f"""SELECT '{table}' AS tbl,
                       bit_xor(hash(struct_pack(*COLUMNS(*{exclude})))) AS h
                FROM {_quote_ident(table)}"""
        )
    try:
        rows = duck.read_query(" UNION ALL ".join(statements))
    except Exception:
        # 任何 schema 意外都退回 COUNT（指纹仍然可用，只是退化到旧粒度）。
        # 但**必须**标出来：这正是盲区 4 说的"静默降级"。
        logger.warning("行内容摘要查询失败，退回 count-only", exc_info=True)
        for table in targets:
            payload[table]["mode"] = "count-only"
            payload[table]["degraded_reason"] = "checksum_error"
        return payload
    for row in rows:
        payload.setdefault(str(row["tbl"]), {})["mode"] = "content"
        payload[str(row["tbl"])]["xor"] = str(row["h"])
    return payload


def _degradation_summary(content: dict[str, dict]) -> dict:
    """Summarise which tables fell back to count-only (盲区 4 的 payload 字段)."""
    count_only = sorted(
        table for table, info in content.items() if info.get("mode") != "content"
    )
    return {
        "active": bool(count_only),
        "count_only_tables": count_only,
        "reasons": {
            table: content[table].get("degraded_reason") or "row_budget_exceeded"
            for table in count_only
        },
    }


def screening_readiness_cache_key(duck: DuckDBStore, sqlite: SQLiteStore) -> str | None:
    """Cheap fingerprint for the global inputs behind screening readiness.

    COUNT(*) is metadata-only in DuckDB and the snapshot aggregate is served
    by zone maps on the production database; this must stay far cheaper than
    the full readiness build it guards.

    缺陷 A 修复要点：只有"行数有界"的表才付内容摘要的代价，大表继续走
    COUNT + MAX(时间戳)。这保证命中路径的开销仍在毫秒量级，同时让"日历内部
    替换 / 财务就地重述 / 人工覆盖改 payload"这几类真实修复动作一定会改变
    指纹（详见本文件顶部缺陷 A 说明）。

    盲区 2/3/4 续修：大表（source_audit / 归档 / price_daily_qfq）追加**逐列
    hash 的 XOR**（对就地翻标记敏感，代价 +2~63ms 而不是整行摘要的 +854ms）；
    价格两表进入外层指纹；超出预算的降级写进 payload 的 ``degraded`` 并落
    WARNING，也可用 :func:`fingerprint_degradation` 查。
    """
    fingerprint, _payload = screening_readiness_fingerprint(duck, sqlite)
    return fingerprint


def screening_readiness_fingerprint(
    duck: DuckDBStore, sqlite: SQLiteStore,
) -> tuple[str | None, dict]:
    """Fingerprint **plus** the payload it was computed from (for diagnostics).

    返回 ``(fingerprint, payload)``；``payload["degraded"]`` 标明本次就绪判定
    有没有退化成 count-only（盲区 4）。指纹把 ``degraded`` 也一起吃进哈希，
    所以"降级态"与"非降级态"永远不会命中彼此的缓存行。
    """
    try:
        distribution_columns = ", ".join(
            f"{sql} AS dist_{table}"
            for table, sql in _DISTRIBUTION_SIGNALS.items()
        )
        price_columns = ", ".join(
            f"(SELECT COUNT(*) FROM {_quote_ident(table)}) AS {table}_c"
            for table in _PRICE_FINGERPRINT_TABLES
        )
        # 全部聚合塞进**一条** SQL：DuckDB 单次 read_query 的连接开销（正式库
        # 60-140ms）才是指纹的主要成本，比单个聚合本身贵一个量级。快照聚合
        # 本轮并进这条查询，省掉一次连接，抵掉新增信号的一部分开销。
        counts = duck.read_query(
            f"""SELECT
                 (SELECT MAX(calculated_at) FROM indicator_snapshot) AS snap_c_at,
                 (SELECT MAX(latest_price_date) FROM indicator_snapshot) AS snap_p,
                 (SELECT MAX(report_date) FROM indicator_snapshot) AS snap_r,
                 (SELECT COUNT(*) FROM source_audit) AS source_audit_c,
                 (SELECT COUNT(*) FROM source_audit_archive) AS source_audit_archive_c,
                 (SELECT COUNT(*) FROM fetch_batch) AS fetch_batch_c,
                 (SELECT COUNT(*) FROM raw_response_archive_all) AS archive_c,
                 (SELECT COUNT(*) FROM indicator_snapshot) AS snapshot_c,
                 (SELECT COUNT(*) FROM balance_sheet) AS balance_sheet,
                 (SELECT COUNT(*) FROM income_statement) AS income_statement,
                 (SELECT COUNT(*) FROM cash_flow) AS cash_flow,
                 (SELECT COUNT(*) FROM dividends) AS dividends,
                 (SELECT COUNT(*) FROM xdxr) AS xdxr,
                 (SELECT MAX(fetch_time) FROM source_audit) AS source_audit_fetch_max,
                 (SELECT MAX(created_at) FROM source_audit) AS source_audit_created_max,
                 {price_columns},
                 {distribution_columns}"""
        )[0]
        # 财务三表/分红/除权除息会改变 readiness 与筛选结果，且它们的
        # "就地重述"不改变 COUNT/MAX(date)：行数有界时改做行内容摘要。
        content = _duck_content_checksums(
            duck, {table: int(counts.get(table) or 0) for table in _CONTENT_CHECKSUM_TABLES},
        )
        overrides = _manual_overrides_fingerprint(sqlite)
        calendar_fp = _trading_calendar_fingerprint(sqlite)
        degraded = _degradation_summary(content)
        # 读不到（表未建 / 文件缺失 / 任何错误）时返回 0，指纹退化成与旧行为等价。
        from app.core.storage import content_epoch as _content_epoch

        content_epoch = _content_epoch.current(sqlite.paths.sqlite_path)
        payload = {
            "snap": {
                "c": counts.get("snap_c_at"),
                "p": counts.get("snap_p"),
                "r": counts.get("snap_r"),
                "n": counts.get("snapshot_c"),
            },
            "counts": {k: v for k, v in counts.items() if k.endswith("_c")},
            "audit": {
                "fetch_max": counts.get("source_audit_fetch_max"),
                "created_max": counts.get("source_audit_created_max"),
            },
            "price": {
                table: {"c": counts.get(f"{table}_c")}
                for table in _PRICE_FINGERPRINT_TABLES
            },
            "distribution": {
                table: str(counts.get(f"dist_{table}"))
                for table in _DISTRIBUTION_SIGNALS
            },
            "content": content,
            # 内容纪元：由 DuckDBStore 在每个**成功的写事务**后 +1（O(1)，见
            # app/core/storage/content_epoch.py）。价格表 1750 万行做行级摘要要 +275ms
            # （指纹基线才 430–510ms），所以"就地重算"改用"写入自己记账"来暴露：
            # 只要有人改过库，纪元就不同 → 缓存不命中 → 不会拿旧结论假装已就绪。
            "content_epoch": content_epoch,
            "degraded": degraded,
            "published_overrides": overrides,
            "trading_calendar": calendar_fp,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        if degraded["active"]:
            _remember_fingerprint_degradation(fingerprint, degraded)
        return fingerprint, payload
    except Exception:
        # 绝不让指纹异常变成"永久命中旧决策"：返回 None，调用方同步重算。
        # 但**留痕**，避免又一次静默退化（盲区 4 的精神）。
        logger.warning("筛选就绪指纹计算失败，将退回同步重算", exc_info=True)
        return None, {}


def readiness_fingerprint_confident(fingerprint: str, sqlite: SQLiteStore) -> bool:
    """指纹是否**内容敏感**（决定旧决策还能不能按指纹复用）。

    只有同时满足两条，"同指纹 ⇒ 同输入 ⇒ 决策仍是当前正确值"才成立：
      ① 指纹没有降级（没有表退回 count-only，见 :func:`fingerprint_degradation`）；
      ② `content_epoch` 可用（>0）——它由每个成功的 DuckDB 写事务 +1，
         正是"就地 UPDATE 也改变指纹"的那一半信号（见 content_epoch 模块）。
    任一不成立都返回 ``False``，调用方退回 2×TTL 的 fail-closed 硬上限。
    """
    if not fingerprint:
        return False
    try:
        if fingerprint_degradation(fingerprint) is not None:
            return False
        from app.core.storage import content_epoch as _content_epoch

        return int(_content_epoch.current(sqlite.paths.sqlite_path) or 0) > 0
    except Exception:
        return False


def readiness_stale_limit_seconds(payload: dict | None) -> int:
    """A same-fingerprint decision may be how old before we refuse to serve it.

    2026-11-20 性能修复：门槛从"墙上时钟"改成"指纹可信度"。
    指纹内容敏感时（缓存行带 ``fingerprint_confident: true``），同指纹即同输入，
    旧决策仍是当前输入的正确函数值 —— 年龄不提供任何额外正确性保证，因此给
    24h 兜底上限（真正的刷新由 TTL + 后台 single-flight 完成）。
    指纹降级 / 无纪元 / **旧格式缓存行没有该标记**时，一律保持 2×TTL 的
    fail-closed 硬上限（宁可同步重算一次 25s，也不对外假绿）。
    """
    if isinstance(payload, dict) and payload.get("fingerprint_confident") is True:
        return SCREENING_READINESS_CONFIDENT_STALE_MAX_AGE_SECONDS
    return SCREENING_READINESS_STALE_MAX_AGE_SECONDS


def load_screening_readiness_entry(
    sqlite: SQLiteStore, fingerprint: str, *, allow_stale: bool = False,
) -> dict | None:
    """Return a cached screening decision **with its freshness metadata**.

    Returns ``None`` on miss / fingerprint change / too old. The returned dict:
        {"decision": dict, "cached_at": str, "age_seconds": float, "stale": bool}

    缺陷 B 修复（2026-11-18）：``allow_stale=True`` 以前只要求"同指纹"，
    过期多久都返回，于是 TTL 沦为"刷新触发器"而不是正确性边界；叠加
    worker 遇写锁直接 return、异常只 logger.warning，过期决策可以被无限期
    当作"已就绪"返回。现在 stale 读取同样有硬上限
    ``SCREENING_READINESS_STALE_MAX_AGE_SECONDS``（2×TTL）：超过即返回 None，
    由调用方同步重算（宁可慢一次，也不对外假绿）。
    """
    try:
        rows = sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?",
            [SCREENING_READINESS_CACHE_KEY],
        )
        if not rows:
            return None
        payload = json.loads(rows[0]["value"] or "{}")
        if not isinstance(payload, dict):
            return None
        if payload.get("fingerprint") != fingerprint:
            return None
        updated = payload.get("updated_at")
        # ⚠️ fail-closed（2026-11-19 审查修）：缓存行**缺 updated_at 或解析失败**时，
        # 年龄无法判定 ⇒ 绝不能当作"新鲜"放行。原实现把年龄闸门整块写在
        # `if age_seconds is not None:` 里，于是 `age_seconds=None` 会**同时跳过
        # TTL 与 1200s 硬上限**，直接返回旧决策 —— 等于给"没有时间戳的行"开了
        # 一个永久豁免（缺陷 B 只对写了时间戳的行成立）。宁可重算一次 20s。
        if not updated:
            return None
        try:
            updated_dt = datetime.fromisoformat(updated)
        except (TypeError, ValueError):
            return None
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - updated_dt).total_seconds()
        max_age = (
            readiness_stale_limit_seconds(payload)
            if allow_stale
            else SCREENING_READINESS_CACHE_TTL_SECONDS
        )
        if age_seconds > max_age:
            return None
        return {
            "decision": payload.get("decision"),
            "cached_at": updated,
            "age_seconds": age_seconds,
            "stale": bool(
                age_seconds is not None
                and age_seconds > SCREENING_READINESS_CACHE_TTL_SECONDS
            ),
            # 盲区 4：这条决策当初算的时候指纹是否降级。旧缓存行没有该字段
            # → None（不是"未降级"的断言，只是"当时没记录"）。
            "fingerprint_degraded": payload.get("fingerprint_degraded"),
            # 2026-11-20：本条决策的指纹是否内容敏感（决定它还能按指纹复用多久）
            "fingerprint_confident": payload.get("fingerprint_confident") is True,
            "stale_limit_seconds": max_age,
        }
    except Exception:
        return None


def load_screening_readiness_cache(
    sqlite: SQLiteStore, fingerprint: str, *, allow_stale: bool = False,
) -> dict | None:
    """Return a cached screening decision, or None on miss/fingerprint change.

    Fresh calls reject entries older than ``SCREENING_READINESS_CACHE_TTL_SECONDS``.
    ``allow_stale=True`` still requires the same input fingerprint but permits an
    expired decision so the request path can use stale-while-revalidate instead
    of blocking on another 20s+ full-data-quality scan. The staleness window is
    **bounded** by ``SCREENING_READINESS_STALE_MAX_AGE_SECONDS`` (缺陷 B);
    callers that need ``cached_at``/``stale`` should use
    :func:`load_screening_readiness_entry`.
    """
    entry = load_screening_readiness_entry(sqlite, fingerprint, allow_stale=allow_stale)
    return entry["decision"] if entry else None


def record_screening_readiness_refresh_outcome(
    sqlite: SQLiteStore,
    fingerprint: str,
    *,
    ok: bool,
    error: str | None = None,
    skipped_reason: str | None = None,
) -> None:
    """Persist the last background-refresh outcome so failures stay visible.

    缺陷 B 修复（2026-11-18）：后台 worker 以前对"写锁活跃"直接 return、对
    异常只 ``logger.warning``，两者都不落盘，运维与调用方都看不到"上次刷新
    失败"。现在每次都写 ``data_refresh_state['screening_readiness_refresh_state']``
    （best-effort，绝不抛出）：``last_success_at`` / ``last_failure_at`` /
    ``consecutive_failures`` / ``last_error`` / ``last_skip_reason``。
    """
    now = datetime.now(UTC).isoformat()
    try:
        rows = sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?",
            [SCREENING_READINESS_REFRESH_STATE_KEY],
        )
        state: dict = {}
        if rows and rows[0].get("value"):
            try:
                loaded = json.loads(rows[0]["value"])
                if isinstance(loaded, dict):
                    state = loaded
            except Exception:
                state = {}
        state["fingerprint"] = fingerprint
        if ok:
            state["last_success_at"] = now
            state["consecutive_failures"] = 0
            state["last_error"] = None
            state["last_skip_reason"] = None
        else:
            state["last_failure_at"] = now
            state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
            state["last_error"] = (error or "")[:500] or None
            state["last_skip_reason"] = skipped_reason
        state["updated_at"] = now
        sqlite.execute(
            """INSERT INTO data_refresh_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                             updated_at=excluded.updated_at""",
            [
                SCREENING_READINESS_REFRESH_STATE_KEY,
                json.dumps(state, ensure_ascii=False, default=str),
                now,
            ],
        )
    except Exception:
        logger.warning("screening readiness 刷新结果落盘失败(非致命)", exc_info=True)


_screening_readiness_refresh_lock = threading.Lock()
_screening_readiness_refreshing: set[str] = set()


def ensure_screening_readiness_refresh(
    duck: DuckDBStore, sqlite: SQLiteStore, fingerprint: str,
) -> None:
    """Refresh an expired screening decision in a single-flight background thread."""
    key = f"{duck.db_path}|{sqlite.db_path}|{fingerprint}"
    with _screening_readiness_refresh_lock:
        if key in _screening_readiness_refreshing:
            return
        _screening_readiness_refreshing.add(key)
    threading.Thread(
        target=_screening_readiness_refresh_worker,
        args=(duck, sqlite, fingerprint, key),
        name="vd-screening-readiness-refresh",
        daemon=True,
    ).start()


def _screening_readiness_refresh_worker(
    duck: DuckDBStore, sqlite: SQLiteStore, fingerprint: str, key: str,
) -> None:
    try:
        from app.core.storage.update_lock import any_write_lock_active

        # 缺陷 B：跳过也要留痕。以前遇写锁直接 return，调用方与运维都无法
        # 区分"正在刷新"和"刷新一直在被写锁挡掉"。
        if any_write_lock_active(duck.db_path):
            logger.warning(
                "后台 screening readiness 刷新被写窗口跳过（缓存不更新）: %s", key,
            )
            record_screening_readiness_refresh_outcome(
                sqlite, fingerprint, ok=False, skipped_reason="write_lock_active",
            )
            return
        decision = screening_readiness(duck, sqlite)
        store_screening_readiness_cache(sqlite, fingerprint, decision)
        record_screening_readiness_refresh_outcome(sqlite, fingerprint, ok=True)
    except Exception as error:
        logger.warning("后台 screening readiness 刷新失败: %s", error)
        record_screening_readiness_refresh_outcome(
            sqlite, fingerprint, ok=False, error=f"{type(error).__name__}: {error}",
        )
    finally:
        with _screening_readiness_refresh_lock:
            _screening_readiness_refreshing.discard(key)


def store_screening_readiness_cache(
    sqlite: SQLiteStore, fingerprint: str, decision: dict,
) -> None:
    """Persist a completed screening decision atomically (best-effort).

    盲区 4：把**本次判定用的指纹的降级情况**一起落盘
    （``fingerprint_degraded``），这样读过这条缓存的人（web 门禁 / CLI）
    通过 :func:`load_screening_readiness_entry` 就能看出"这次就绪判断是
    降级的"，而不是只有日志里才有。
    """
    try:
        sqlite.execute(
            """INSERT INTO data_refresh_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                             updated_at=excluded.updated_at""",
            [
                SCREENING_READINESS_CACHE_KEY,
                json.dumps({
                    "fingerprint": fingerprint,
                    "decision": decision,
                    "fingerprint_degraded": fingerprint_degradation(fingerprint),
                    # 2026-11-20：把"指纹是否内容敏感"随决策一起落盘，读取侧据此
                    # 决定旧决策的可复用上限（见 readiness_stale_limit_seconds）。
                    # ⚠️ 只有**成功算完**的决策才会走到这里，失败/未算出绝不落盘。
                    "fingerprint_confident": readiness_fingerprint_confident(
                        fingerprint, sqlite
                    ),
                    "updated_at": datetime.now(UTC).isoformat(),
                }, default=str),
                datetime.now(UTC).isoformat(),
            ],
        )
    except Exception:
        logger.warning("screening readiness cache write failed", exc_info=True)


def warm_screening_readiness_cache(
    duck: DuckDBStore, sqlite: SQLiteStore,
) -> dict | None:
    """Compute and persist the screening decision when no fresh cache exists.

    Startup maintenance calls this off the web request path so the first
    user-triggered screening run normally hits the persistent cache instead
    of paying the full data-quality scan inline.
    """
    fingerprint = screening_readiness_cache_key(duck, sqlite)
    if not fingerprint:
        return None
    cached = load_screening_readiness_cache(sqlite, fingerprint)
    if cached is not None:
        return cached
    decision = screening_readiness(duck, sqlite)
    store_screening_readiness_cache(sqlite, fingerprint, decision)
    return decision




# Fraction of a stock's observed window that may lack bars before the
# calendar check fails. Suspensions legitimately produce zero bars on
# calendar days; 2% of a multi-year window covers ordinary suspensions while
# still failing real source gaps (e.g. 000560 qfq 2017-2020 ~12%).
_CALENDAR_GAP_TOLERANCE = 0.02
# Suspensions routinely span several days to a few weeks; any stock whose
# missing-bar count is within this absolute floor passes regardless of window
# size, so small recent windows (new listings, resume-from-suspension) are not
# failed on a handful of quiet days.
_CALENDAR_GAP_ABSOLUTE_FLOOR = 20


# 「系统性」判据（2026-11-20）：受影响上市股 ≥ 1/3 视为日历整体不可用。
# 与本文件 `GATE_HARD_BLOCK_SHARE_DIVISOR` 同一套"占比而非绝对阈值"的理由：
# 绝对阈值在小库上形同虚设，占比判据与库大小无关。正式库实测受影响
# 197/5,580 = 3.5% → 远低于 1/3 → 披露而非阻断（见 reports/150）。
_CALENDAR_SYSTEMIC_SHARE_DIVISOR = 3


def _calendar_status(verdict: str, **counts) -> dict:
    return {"verdict": verdict, **counts}


def _trading_calendar_coverage(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    """Classify how usable the persisted trading calendar is for screening.

    返回 ``{"verdict": "ok" | "partial" | "unavailable", ...counts}``：

      * ``ok``          —— 每个上市股的 raw/qfq 序列都与日历对得上；
      * ``partial``     —— 日历存在，但**个别标的**的覆盖不匹配（缺 bar 或
        有日历里没有的成交日）。只有这些标的的窗口计数可能偏移，其余标的
        的排名不受影响 → 筛选门禁按 disclose 处理（不拦，如实披露）；
      * ``unavailable`` —— 日历表为空 / 查询失败 / 受影响上市股 ≥ 1/3（系统性）
        → 无法判定窗口，硬阻断。

    2026-11-20 缺陷（本次修复）：旧实现把这些情况一律返回 False → 门禁
    `TRADING_CALENDAR_UNAVAILABLE` → **整个筛选永久 409**，而正式库的真实
    规模只有 197/5,580 只标的（3.5%），用户却完全无法筛选。同时旧实现把
    门禁缓存写死成 ``{"result": True}``（无论本次判定是真是假），于是
    "第一次报错、立刻重试又通过、下次更新后再报错"——失败被缓存成了通过
    （fail-open）。现在缓存存**真实 verdict + counts**，判定与缓存不再互相矛盾。

    Require every listed stock's raw and QFQ series to match the persisted calendar.

    A global first/last-date check permits an incomplete calendar or an internal
    per-stock gap to pass. Screening indicators read latest_close from raw but
    require adjusted series for technicals, so validate both directions for both
    tables: every calendar date in a stock's observed window has a close, and
    every close date is represented by the calendar.

    Suspensions produce legal zero-bar days, so a per-stock gap fraction up to
    _CALENDAR_GAP_TOLERANCE of its window is accepted. Dates that exist in the
    price tables but not in the calendar remain fail-closed at zero tolerance.
    """
    try:
        calendar_dates = sorted({
            date.fromisoformat(str(row["trade_date"])[:10]).isoformat()
            for row in sqlite.query("SELECT trade_date FROM trading_dates")
        })
        if not calendar_dates:
            # 日历表空 = 无法判定任何窗口 → 硬阻断（安全底线）。
            return _calendar_status("unavailable", calendar_days=0)
        # 2026-08-30 提速：该检查在正式库上约需 60 秒，且筛选每次运行都会
        # 调用。交易日历与价格覆盖不变时结果不会变，因此用输入指纹做持久
        # 缓存；价格更新后仅首次筛选会重算一次。
        try:
            # 缺陷 A（嵌套双盲）：这一层内层缓存的日历指纹此前同样是
            # COUNT+MIN+MAX，等同于把外层刚修掉的盲区又复制了一遍——外层
            # 修好后内层仍会对"日历内部替换"命中旧结论。改用与外层同源的
            # 精确内容摘要（日历只有 ~5k 行，成本可忽略）。
            cal_fp = _trading_calendar_fingerprint(sqlite)
            price_fp = duck.read_query(
                """SELECT
                     (SELECT COUNT(*) FROM price_daily_raw WHERE close IS NOT NULL) AS raw_c,
                     (SELECT MIN(trade_date) FROM price_daily_raw WHERE close IS NOT NULL) AS raw_lo,
                     (SELECT MAX(trade_date) FROM price_daily_raw WHERE close IS NOT NULL) AS raw_hi,
                     (SELECT COUNT(*) FROM price_daily_qfq WHERE close IS NOT NULL) AS qfq_c,
                     (SELECT MIN(trade_date) FROM price_daily_qfq WHERE close IS NOT NULL) AS qfq_lo,
                     (SELECT MAX(trade_date) FROM price_daily_qfq WHERE close IS NOT NULL) AS qfq_hi"""
            )[0]
            import hashlib as _hashlib
            fingerprint = _hashlib.sha256(
                json.dumps({
                    "calendar": cal_fp,
                    "price": price_fp,
                }, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            cached = sqlite.query(
                "SELECT value FROM data_refresh_state WHERE key = 'trading_calendar_gate_cache'"
            )
            if cached:
                payload = json.loads(cached[0]["value"] or "{}")
                status = payload.get("status")
                if (
                    payload.get("fingerprint") == fingerprint
                    and isinstance(status, dict)
                    and status.get("verdict") in {"ok", "partial", "unavailable"}
                ):
                    return status
                # 旧格式（{"result": bool}）不再采信：它把失败写成了 True，
                # 继续读它就是继续 fail-open。直接落到下面的重算。
        except Exception:
            fingerprint = None
        values = ", ".join("(CAST(? AS DATE))" for _ in calendar_dates)
        missing = duck.read_query(
            f"""
            WITH calendar(trade_date) AS (VALUES {values}),
            raw_windows AS (
                SELECT m.stock_code, MIN(r.trade_date) AS first_date, MAX(r.trade_date) AS last_date
                FROM stock_meta m
                JOIN price_daily_raw r ON r.stock_code = m.stock_code AND r.close IS NOT NULL
                WHERE m.is_listed IS TRUE
                GROUP BY m.stock_code
            ),
            qfq_windows AS (
                SELECT m.stock_code, MIN(q.trade_date) AS first_date, MAX(q.trade_date) AS last_date
                FROM stock_meta m
                JOIN price_daily_qfq q ON q.stock_code = m.stock_code AND q.close IS NOT NULL
                WHERE m.is_listed IS TRUE
                GROUP BY m.stock_code
            ),
            raw_stats AS (
                SELECT w.stock_code, w.first_date, w.last_date,
                       (SELECT COUNT(*) FROM calendar c
                        WHERE c.trade_date BETWEEN w.first_date AND w.last_date) AS window_days,
                       COUNT(r.trade_date) AS have_days
                FROM raw_windows w
                LEFT JOIN calendar c ON c.trade_date BETWEEN w.first_date AND w.last_date
                LEFT JOIN price_daily_raw r
                  ON r.stock_code = w.stock_code AND r.trade_date = c.trade_date AND r.close IS NOT NULL
                GROUP BY w.stock_code, w.first_date, w.last_date
            ),
            qfq_stats AS (
                SELECT w.stock_code, w.first_date, w.last_date,
                       (SELECT COUNT(*) FROM calendar c
                        WHERE c.trade_date BETWEEN w.first_date AND w.last_date) AS window_days,
                       COUNT(q.trade_date) AS have_days
                FROM qfq_windows w
                LEFT JOIN calendar c ON c.trade_date BETWEEN w.first_date AND w.last_date
                LEFT JOIN price_daily_qfq q
                  ON q.stock_code = w.stock_code AND q.trade_date = c.trade_date AND q.close IS NOT NULL
                GROUP BY w.stock_code, w.first_date, w.last_date
            ),
            raw_gap_excess AS (
                SELECT stock_code FROM raw_stats
                WHERE window_days > 0
                  AND have_days < window_days * (1 - {_CALENDAR_GAP_TOLERANCE})
                  AND window_days - have_days > {_CALENDAR_GAP_ABSOLUTE_FLOOR}
            ),
            qfq_gap_excess AS (
                SELECT stock_code FROM qfq_stats
                WHERE window_days > 0
                  AND have_days < window_days * (1 - {_CALENDAR_GAP_TOLERANCE})
                  AND window_days - have_days > {_CALENDAR_GAP_ABSOLUTE_FLOOR}
            ),
            raw_missing_calendar AS (
                SELECT DISTINCT r.stock_code
                FROM price_daily_raw r
                JOIN stock_meta m ON m.stock_code = r.stock_code AND m.is_listed IS TRUE
                LEFT JOIN calendar c ON c.trade_date = r.trade_date
                WHERE r.close IS NOT NULL AND c.trade_date IS NULL
            ),
            qfq_missing_calendar AS (
                SELECT DISTINCT q.stock_code
                FROM price_daily_qfq q
                JOIN stock_meta m ON m.stock_code = q.stock_code AND m.is_listed IS TRUE
                LEFT JOIN calendar c ON c.trade_date = q.trade_date
                WHERE q.close IS NOT NULL AND c.trade_date IS NULL
            ),
            affected AS (
                SELECT stock_code FROM raw_gap_excess
                UNION SELECT stock_code FROM qfq_gap_excess
                UNION SELECT stock_code FROM raw_missing_calendar
                UNION SELECT stock_code FROM qfq_missing_calendar
            )
            SELECT
                (SELECT COUNT(*) FROM raw_gap_excess) AS raw_gap_excess_stocks,
                (SELECT COUNT(*) FROM qfq_gap_excess) AS qfq_gap_excess_stocks,
                (SELECT COUNT(*) FROM raw_missing_calendar) AS raw_stocks_outside_calendar,
                (SELECT COUNT(*) FROM qfq_missing_calendar) AS qfq_stocks_outside_calendar,
                -- 逐个 stock_code 去重：同一个标的同时命中 raw/qfq 只算 1 只，
                -- 否则"受影响标的数"会被重复计数、把披露级误升成系统性阻断。
                (SELECT COUNT(*) FROM affected) AS affected_stocks,
                (SELECT COUNT(*) FROM stock_meta WHERE is_listed IS TRUE) AS listed_stocks
            """,
            calendar_dates,
        )
        counts = dict(missing[0]) if missing else {}
        affected = int(counts.get("affected_stocks") or 0)
        listed = int((missing[0] if missing else {}).get("listed_stocks") or 0)
        counts["calendar_days"] = len(calendar_dates)
        if affected == 0:
            status = _calendar_status("ok", **counts)
        elif listed > 0 and affected * _CALENDAR_SYSTEMIC_SHARE_DIVISOR >= listed:
            # ≥1/3 的上市股同时覆盖不上 → 视同日历整体不可用（系统性）。
            status = _calendar_status("unavailable", **counts)
        else:
            status = _calendar_status("partial", **counts)
        try:
            if fingerprint is not None:
                sqlite.execute(
                    """INSERT INTO data_refresh_state (key, value, updated_at)
                       VALUES ('trading_calendar_gate_cache', ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                     updated_at=excluded.updated_at""",
                    [
                        json.dumps(
                            {"fingerprint": fingerprint, "status": status},
                            ensure_ascii=False,
                        ),
                        datetime.now(UTC).isoformat(),
                    ],
                )
        except Exception:
            pass
        return status
    except Exception:
        # 查询本身失败 = 无法判定 → 保持 fail-closed。
        return _calendar_status("unavailable", error="calendar_query_failed")


def _has_complete_trading_calendar(duck: DuckDBStore, sqlite: SQLiteStore) -> bool:
    """True only when the calendar is fully consistent (verdict == "ok").

    保留这个布尔入口：既有调用方/测试读的是"完整"语义；分级由
    :func:`_trading_calendar_coverage` 负责。
    """
    return _trading_calendar_coverage(duck, sqlite).get("verdict") == "ok"


# 2026-11-12 契约 §3：股本完整性地板的唯一 SQL。
# 违规 = NOT(total>0 AND circ>0 AND circ<=total)，且不满足
# 「上市 ≤90 天且 total/circ 皆为 NULL」的新股豁免。
# RG1 时区单源（2026-11-21）：'今天'由 `cn_today()` 在 Python 侧算出后以
# `$today` 绑定进来，SQL 里**不许**写 `CURRENT_DATE`（= 进程本地时区，
# UTC 机器上会差 1 天，且与 readiness 的"今天"不同源）。
_SHARE_CAPITAL_FLOOR_SQL = """
    SELECT stock_code
    FROM stock_meta
    WHERE is_listed IS TRUE
      AND NOT (
          listing_date IS NOT NULL
          AND listing_date >= $today::DATE - INTERVAL '90 days'
          AND total_shares IS NULL AND circ_shares IS NULL
       )
      AND NOT (
          total_shares IS NOT NULL AND total_shares > 0
          AND circ_shares IS NOT NULL AND circ_shares > 0
          AND circ_shares <= total_shares
      )
    ORDER BY stock_code
"""


def _strict_share_capital_gap_codes(duck: DuckDBStore) -> list[str]:
    """Raw share-capital floor query; raises so callers can fail closed."""
    rows = duck.read_query(
        _SHARE_CAPITAL_FLOOR_SQL,
        _bind_named_params(
            _SHARE_CAPITAL_FLOOR_SQL,
            {"today": cn_today().isoformat()},
            context="share_capital_floor",
        ),
    )
    return [str(row["stock_code"]) for row in rows]


def listed_share_capital_gap_codes(duck: DuckDBStore) -> list[str]:
    """List every listed stock that breaks the share-capital integrity floor.

    **单一口径（契约 §3）**：发布门禁、minimum_data_readiness 的
    ``has_share_capital``、以及 update.py 的每日定向补抓全部复用本函数，
    禁止在别处复制这份 SQL。

    永不抛异常：查询失败时记录 warning 并返回 ``[]``，避免一次瞬时读错误
    把每日补抓流程整体打断。需要 fail-closed 的调用方（发布门禁）请使用
    :func:`_strict_share_capital_gap_codes`，它会把异常抛给上层。
    """
    try:
        return _strict_share_capital_gap_codes(duck)
    except Exception as error:
        logger.warning("股本完整性检查失败，缺口列表按空处理: %s", error)
        return []


def _listed_stock_count(duck: DuckDBStore) -> int:
    rows = duck.read_query("SELECT COUNT(*) AS cnt FROM stock_meta WHERE is_listed IS TRUE")
    return int(rows[0]["cnt"]) if rows else 0


def gate_hard_block_threshold(listed_count: int) -> int:
    """``max(50, int(listed_count * 0.01))`` — see contract §3."""
    return max(GATE_HARD_BLOCK_MIN, int(listed_count * GATE_HARD_BLOCK_RATIO))


def gate_hard_blocks_by_share(violation_count: int, listed_count: int) -> bool:
    """「≥1/3 上市股同时违规」→ 系统性错乱（与库大小无关，小库也能触发）。

    用整数比较 ``violation_count * 3 >= listed_count`` 实现"≥1/3"，
    而不是 ``violation_count >= int(listed_count / 3)``：后者在 20 只库里
    会把 6 只（30%）误判成系统性（``int(20/3) == 6``），与"≥1/3"不符。
    空库（``listed_count <= 0``）或零违规一律不触发。
    """
    if listed_count <= 0 or violation_count <= 0:
        return False
    return violation_count * GATE_HARD_BLOCK_SHARE_DIVISOR >= listed_count


def share_capital_hard_block(violation_count: int, listed_count: int) -> bool:
    """发布门禁与 readiness 共用的硬阻断判定（契约 §3 唯一实现）。

    命中任一即整批拒绝（hard_block）：
    * ``violation_count > gate_hard_block_threshold(listed_count)``
      —— 系统性规模（正式库 5,580 只 → 55 只）；
    * ``violation_count >= listed_count / 3``
      —— 占比兜底，小库/退化库上同样能识别"整库都被隔离"。
    ``snapshot_publish_gate`` 与 ``minimum_data_readiness`` 都必须调用本函数，
    禁止各写一份判定（否则同一状态会得出两个相反结论）。
    """
    return (
        violation_count > gate_hard_block_threshold(listed_count)
        or gate_hard_blocks_by_share(violation_count, listed_count)
    )


def snapshot_publish_gate(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    """Quality gate consulted before an indicator snapshot is published.

    2026-11-12 P0（契约 §3/§4）：此前本门禁是 fail-closed 的"一票否决"——
    任一只上市股票股本不满足 ``total>0 AND circ>0 AND circ<=total`` 就
    ``ready=False``，三个发布调用点随即整批拒绝，于是 26 只股本缺失票
    （其中 5 只新股在 11-03~11-10 陆续越过 90 天豁免线）让全市场 5,580 只
    快照自 10-30 起再没发布过。

    新语义（分级）：
    - ``violation_count > threshold``：怀疑系统性单位错乱 → ``hard_block``，
      整批拒绝（保留上一代快照）；
    - 否则 ``ready=True, hard_block=False``，违规票进 ``quarantined_codes``
      逐股隔离，其余正常发布；
    - 检查本身抛异常 → ``ready=False, hard_block=True``（fail-closed）。

    唯一口径：违规集合来自 :func:`listed_share_capital_gap_codes`（含 90 天
    新股豁免），readiness 的 ``has_share_capital`` 复用同一函数；本函数使用
    其严格版本，以便把"查询失败"与"没有违规"区分开。
    """
    try:
        violations = _strict_share_capital_gap_codes(duck)
        listed_count = _listed_stock_count(duck)
    except Exception as error:
        # 门禁自身不可用必须 fail-closed：宁可保留上一代快照，也不发布
        # 未经校验收割的快照。
        logger.error("股本完整性门禁不可用，整批拒绝发布(fail-closed): %s", error)
        return {
            "ready": False,
            "reason": "share_capital_check_unavailable",
            "error": str(error),
            "hard_block": True,
            "quarantined_codes": [],
            "violations": [],
            "violation_count": 0,
            "listed_count": 0,
            "threshold": max(GATE_HARD_BLOCK_MIN, 0),
            "hard_block_rule": "check_unavailable",
        }
    threshold = gate_hard_block_threshold(listed_count)
    violation_count = len(violations)
    # 2026-11-13 P1：绝对阈值 OR 占比判据（≥1/3），唯一实现见
    # share_capital_hard_block。占比判据让 30 只的小库也能识别"整库被隔离"。
    over_threshold = violation_count > threshold
    over_share = gate_hard_blocks_by_share(violation_count, listed_count)
    hard_block = over_threshold or over_share
    hard_block_rule = (
        "threshold" if over_threshold else "share" if over_share else None
    )
    if hard_block:
        logger.error(
            "股本完整性违规 %s 只（上市 %s 只，绝对阈值 %s 只，命中=%s），"
            "疑似系统性单位错乱，整批拒绝发布",
            violation_count,
            listed_count,
            threshold,
            hard_block_rule,
        )
    elif violations:
        logger.warning(
            "股本完整性违规 %s 只（上市 %s 只，阈值 %s 只）→ 逐股隔离发布: %s",
            len(violations),
            listed_count,
            threshold,
            violations[:20],
        )
    return {
        "ready": not hard_block,
        "reason": "share_capital_integrity" if hard_block else None,
        "hard_block": hard_block,
        # 命中哪条规则：threshold（绝对阈值）/ share（≥1/3 占比）/ None。
        "hard_block_rule": hard_block_rule,
        "quarantined_codes": list(violations),
        "violations": violations[:20],
        "violation_count": violation_count,
        "listed_count": listed_count,
        "threshold": threshold,
    }


def actionable_missing_count(sqlite: SQLiteStore) -> int:
    """Single source of truth for the actionable ``missing_list`` count (§3).

    口径 = ``resolved_at IS NULL`` 且"可行动"（复用
    ``app.core.missing_conditions.actionable_missing_condition()``）。2026-11-12 P1：
    此前 CLI 用 ``SELECT COUNT(*) FROM missing_list``（含已解决行）报 11,823，而
    状态页用可行动口径报 162，同一时刻两个数字。

    异常向上抛出（不返回 0）：把"查询坏了"伪装成"没有缺口"是假绿。
    """
    condition, disabled = actionable_missing_condition()
    rows = sqlite.query(
        f"SELECT COUNT(*) AS cnt FROM missing_list "
        f"WHERE resolved_at IS NULL AND {condition}",
        disabled,
    )
    return int(rows[0]["cnt"]) if rows else 0



def snapshot_period_mismatches(duck: DuckDBStore) -> list[dict]:
    """Return listed stocks whose published snapshot period is not the latest
    complete three-statement period.

    The latest complete period is the newest report_date where balance_sheet,
    income_statement, and cash_flow all carry their core non-null fields.
    Newer statement rows with missing core fields are a data-source pending
    state (PRD §7.7: "数据源尚未就绪时保留旧值"), not a screening period.

    This is the single shared judgment for both the readiness gate
    (minimum_data_readiness → snapshot_period_alignment) and the screening
    engine (_reject_mixed_report_dates), so a partial source update can never
    pass one and silently block the other.
    """
    return duck.read_query(
        """
        SELECT m.stock_code,
               s.report_date AS snapshot_date,
               cp.complete_date
        FROM stock_meta m
        LEFT JOIN LATERAL (
            SELECT report_date FROM indicator_snapshot
            WHERE stock_code = m.stock_code
            ORDER BY report_date DESC LIMIT 1
        ) s ON true
        LEFT JOIN LATERAL (
            SELECT MAX(bs.report_date) AS complete_date
            FROM balance_sheet bs
            JOIN income_statement ic
              ON ic.stock_code = bs.stock_code AND ic.report_date = bs.report_date
            JOIN cash_flow cf
              ON cf.stock_code = bs.stock_code AND cf.report_date = bs.report_date
            WHERE bs.stock_code = m.stock_code
              AND bs.total_assets IS NOT NULL
              AND bs.total_liabilities IS NOT NULL
              AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
              AND ic.revenue IS NOT NULL
              AND ic.parent_net_profit IS NOT NULL
              AND cf.cf_from_operating IS NOT NULL
        ) cp ON true
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_date FROM balance_sheet WHERE stock_code = m.stock_code
        ) lb ON true
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_date FROM income_statement WHERE stock_code = m.stock_code
        ) li ON true
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_date FROM cash_flow WHERE stock_code = m.stock_code
        ) lc ON true
        WHERE m.is_listed IS TRUE
          AND s.report_date IS NOT NULL
          AND (
              -- 完整期存在但与快照期不一致（任一方向）
              (cp.complete_date IS NOT NULL AND s.report_date <> cp.complete_date)
              -- 报表有比快照更新的行，但从未构成完整期（无法回算/校验口径）
              OR (cp.complete_date IS NULL
                  AND (
                      lb.max_date IS NOT NULL AND lb.max_date > s.report_date
                      OR li.max_date IS NOT NULL AND li.max_date > s.report_date
                      OR lc.max_date IS NOT NULL AND lc.max_date > s.report_date
                  ))
          )
        ORDER BY m.stock_code
        """
    )


#: SQL 里的具名参数引用（DuckDB 的 `$name` 语法）。
_NAMED_PARAM_REF = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _bind_named_params(
    sql: str,
    params: Mapping[str, Any],
    *,
    context: str,
) -> dict[str, Any]:
    """把 `$name` 具名参数绑到 SQL 上，并**强制**引用集合与键集合逐字相等。

    为什么需要这个函数（2026-11-21 事故，`reports/152` §B）：`minimum_data_readiness`
    的长 SQL 用 6 个位置参数 `?`，参数表里多插了一个
    `PRICE_FRESHNESS_MAX_AGE_DAYS`，于是**从第 3 个 `?` 起全部错位**——
    快照新鲜度窗口从 7 天变成 1300 天（偏偏能跑、能算、还"看起来合理"），
    而 `has_raw_history` 的观测下限从 1300 掉到 7。位置参数下"第 k 个 `?`
    绑谁"只存在于作者脑子里，改 SQL 里的任何一个 `?` 都会静默移位。

    具名参数把绑定关系写进 SQL 文本本身，顺序不再有语义；本函数再补两道
    结构性断言（错就抛，不静默错算）：
      ① 不许混入 `?`：一旦有人把某个 `$name` 改回 `?`，DuckDB 会把 `?`
         当成"第 n 个位置参数"而调用方传的是 dict —— 那只会得到一条宿主
         语言的 TypeError。这里直接给出可读的失败原因。
      ② SQL 引用的 `$name` 集合必须与传入的键集合**完全相等**（双向）：
         漏传会 RuntimeError，多传说明有人删了判据却留着参数（口径漂移）。
    """
    if "?" in sql:
        raise ValueError(
            f"{context}: SQL 混入了位置参数 `?`；本函数只支持 `$name` 具名参数"
            "（位置参数错位曾把 7 天新鲜度窗口变成 1300 天，见 reports/152 §B）"
        )
    referenced = set(_NAMED_PARAM_REF.findall(sql))
    bound = set(params)
    if referenced != bound:
        raise ValueError(
            f"{context}: 具名参数与 SQL 引用不一致；"
            f"SQL 引用但未传值={sorted(referenced - bound)}，"
            f"传了值但 SQL 未引用={sorted(bound - referenced)}"
        )
    return dict(params)


def minimum_data_readiness(
    duck: DuckDBStore,
    sqlite: SQLiteStore | None = None,
    *,
    minimum_history_observations: int = DEFAULT_MINIMUM_HISTORY_OBSERVATIONS,
    minimum_volume_observations: int = DEFAULT_MINIMUM_VOLUME_OBSERVATIONS,
) -> dict:
    """Evaluate the current, per-stock screening floor without mutating data."""
    today = cn_today()
    if minimum_history_observations < 1 or minimum_volume_observations < 1:
        raise ValueError("readiness observation thresholds must be positive")
    compatibility = _live_schema_compatibility(duck)
    if not compatibility["compatible"]:
        missing = {"schema_compatibility": compatibility["missing"][:20]}
        return {
            "ready": False,
            "stock_count": 0,
            "missing": missing,
            "missing_counts": {"schema_compatibility": len(compatibility["missing"])},
            "schema_compatibility": compatibility,
        }

    # SQL 与参数分开两处：参数一律**具名**（`$name`），见末尾 `_bind_named_params`。
    readiness_sql = """
        WITH clock AS (
            -- RG1 时区单源：'今天'只来自 `cn_today()`（Python 绑定），
            -- 不用 DuckDB CURRENT_DATE（= 进程本地时区，UTC 机器上会差 1 天）。
            SELECT $today::DATE AS today, $price_stale_before::DATE AS price_stale_before
        ), raw AS (
            SELECT stock_code, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,
                   COUNT(*) AS observation_count,
                   COUNT(*) FILTER (WHERE volume IS NOT NULL AND volume > 0) AS volume_count
            FROM price_daily_raw WHERE close IS NOT NULL GROUP BY stock_code
        ), qfq AS (
            SELECT stock_code, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,
                   COUNT(*) AS observation_count
            FROM price_daily_qfq WHERE close IS NOT NULL GROUP BY stock_code
        ), complete_financials AS (
            SELECT bs.stock_code, MAX(bs.report_date) AS report_date,
                   arg_max(bs.capital_adequacy_ratio, bs.report_date) AS capital_adequacy_ratio,
                   arg_max(bs.non_performing_loan_ratio, bs.report_date) AS non_performing_loan_ratio,
                   arg_max(bs.provision_coverage_ratio, bs.report_date) AS provision_coverage_ratio,
                   arg_max(bs.risk_coverage_ratio, bs.report_date) AS risk_coverage_ratio
            FROM balance_sheet bs
            JOIN income_statement ic
              ON ic.stock_code = bs.stock_code AND ic.report_date = bs.report_date
            JOIN cash_flow cf
              ON cf.stock_code = bs.stock_code AND cf.report_date = bs.report_date
            WHERE bs.total_assets IS NOT NULL
              AND bs.total_liabilities IS NOT NULL
              AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
              AND ic.revenue IS NOT NULL
              AND ic.parent_net_profit IS NOT NULL
               AND cf.cf_from_operating IS NOT NULL
            GROUP BY bs.stock_code
        )
        SELECT m.stock_code,
            m.listing_date,
            m.total_shares,
            m.circ_shares,
            -- 2026-11-17（reports/135）：listing_date IS NULL = 上市日期未知（新股尚未被
            -- 元数据管线回填）。此时无法证明它是老股，按近期上市豁免深度检查；
            -- 否则一只新股就会让 /api/ready 永久 503（实测 920309 N菊乐）。
            (m.listing_date IS NULL
             OR m.listing_date >= clock.today - INTERVAL '90 days') AS is_recent_listing,
            raw.last_date AS raw_last_date,
            qfq.last_date AS qfq_last_date,
            complete_financials.report_date AS financial_report_date,
            m.listing_date IS NOT NULL AND m.is_st IS NOT NULL AND m.is_suspended IS NOT NULL
                AS has_pool_metadata,
            -- 新股（上市不足 90 天）仍在积累历史与成交量，属于市场真实状态，
            -- 不阻断筛选（披露项），因此对这些检查豁免。
            (m.listing_date IS NULL OR m.listing_date >= clock.today - INTERVAL '90 days'
             OR (raw.first_date IS NOT NULL
                 AND raw.first_date <= GREATEST(m.listing_date, clock.today - INTERVAL '5 years') + INTERVAL '30 days'
                 AND raw.observation_count >= LEAST($minimum_history_observations, GREATEST(1, CAST(CEIL(
                     date_diff('day', GREATEST(m.listing_date, clock.today - INTERVAL '5 years'), clock.today) * 0.45
                 ) AS INTEGER)))))
                AS has_raw_history,
            (m.listing_date IS NULL OR m.listing_date >= clock.today - INTERVAL '90 days'
             OR (qfq.first_date IS NOT NULL
                 AND qfq.first_date <= GREATEST(m.listing_date, clock.today - INTERVAL '5 years') + INTERVAL '30 days'
                 AND qfq.observation_count >= LEAST($minimum_history_observations, GREATEST(1, CAST(CEIL(
                     date_diff('day', GREATEST(m.listing_date, clock.today - INTERVAL '5 years'), clock.today) * 0.45
                 ) AS INTEGER)))))
                AS has_qfq_history,
            -- 价格允许陈旧但必须显示日期（PRD §6.4 D7）：已有数据但超过
            -- `PRICE_FRESHNESS_MAX_AGE_DAYS` 天无新 bar 的股票视为停牌，
            -- 新鲜度缺口属披露项，不阻断。完全没有数据的股票仍为缺口（数据损坏类）。
            (raw.last_date IS NOT NULL
             AND (raw.last_date < clock.price_stale_before
                  OR raw.last_date >= clock.price_stale_before
                     AND qfq.last_date >= clock.price_stale_before))
                AS has_fresh_prices,
            (m.listing_date IS NULL OR m.listing_date >= clock.today - INTERVAL '90 days'
             OR raw.volume_count >= LEAST($minimum_volume_observations, GREATEST(1, CAST(CEIL(
                 date_diff('day', GREATEST(m.listing_date, clock.today - INTERVAL '5 years'), clock.today) * 0.45
             ) AS INTEGER)))) AS has_meaningful_volume,
            complete_financials.report_date >= clock.today - INTERVAL '18 months'
                AS has_complete_financial_period,
            EXISTS (SELECT 1 FROM xdxr action WHERE action.stock_code = m.stock_code)
              AND EXISTS (
                SELECT 1 FROM dividends dividend
                WHERE dividend.stock_code = m.stock_code
                  AND dividend.announcement_date IS NOT NULL
              ) AS has_corporate_action_or_dividend_lineage,
            CASE
                WHEN lower(coalesce(m.csrc_l1, '') || ' ' || coalesce(m.csrc_l2, '')) LIKE '%银行%'
                  OR lower(coalesce(m.csrc_l1, '') || ' ' || coalesce(m.csrc_l2, '')) LIKE '%证券%'
                THEN complete_financials.report_date IS NOT NULL
                ELSE TRUE
            END AS has_sector_financials,
            -- 银行/券商监管字段（资本充足率/不良贷款率/拨备覆盖率/风险覆盖率）：
            -- 免费结构化 API 不可得，所有者口径为"保持 NULL，不伪造"（STATUS 缺口#4），
            -- 因此该缺口属披露项而非阻断项（disclosure_keys 处理）。
            CASE
                WHEN lower(coalesce(m.csrc_l1, '') || ' ' || coalesce(m.csrc_l2, '')) LIKE '%银行%'
                THEN complete_financials.capital_adequacy_ratio IS NOT NULL
                  AND complete_financials.non_performing_loan_ratio IS NOT NULL
                  AND complete_financials.provision_coverage_ratio IS NOT NULL
                WHEN lower(coalesce(m.csrc_l1, '') || ' ' || coalesce(m.csrc_l2, '')) LIKE '%证券%'
                THEN complete_financials.risk_coverage_ratio IS NOT NULL
                ELSE TRUE
            END AS has_regulatory_fields,
            EXISTS (
                SELECT 1 FROM indicator_snapshot snapshot
                WHERE snapshot.stock_code = m.stock_code
                  AND snapshot.report_date = complete_financials.report_date
                  AND snapshot.calculated_at IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM source_audit audit
                    WHERE audit.stock_code = m.stock_code
                      AND audit.report_date = complete_financials.report_date
                      AND audit.field_name IN (
                          'total_assets', 'total_liabilities', 'total_equity', 'total_equity_parent',
                          'revenue', 'parent_net_profit', 'cf_from_operating'
                      )
                       AND audit.fetch_time > snapshot.calculated_at
                   )
            ) AS has_current_snapshot_inputs,
            -- 2026-11-21（reports/152 §B）：快照价格日的一致性判据。
            -- 旧判据要求 `snapshot.latest_price_date` **逐字等于**该股最新 bar 日，
            -- 于是"今天收盘的股价刚入库、快照还是昨天算的"这个窗口（几分钟到
            -- 几小时；自动更新正在重算快照时则是整轮）会把全市场判成不达标
            -- （实测该窗口内 5,567/5,567 只全部命中，见 reports/152 §A2）。
            -- 新判据拆成两件事，两件都必须成立：
            --   ① 证据（不许放宽）：快照声称的价格日必须在库里真有那一根 bar，
            --      且 `ABS(快照收盘 - 那根 bar 的收盘) <= max(|close|,1)*1e-6`
            --      —— 这是"快照用的确实是那根 bar"的凭据，缺 bar 或对不上仍阻断；
            --   ② 新鲜度：该价格日距**该股自己最新 bar**不超过
            --      `PRICE_FRESHNESS_MAX_AGE_DAYS` 天（与 `has_fresh_prices` /
            --      `SNAPSHOT_STALE` 同一常量，不另造口径）。超过就是真过期，
            --      仍然阻断（防止用户拿一年前的陈旧快照做决策）。
            -- 停牌股不受影响：它们的 `raw.last_date` 自己就是旧的，① ② 都按
            -- 它自己的最新 bar 判，与 `has_fresh_prices` 的停牌豁免同向。
            EXISTS (
                SELECT 1 FROM indicator_snapshot snapshot
                JOIN price_daily_raw snapshot_bar
                  ON snapshot_bar.stock_code = snapshot.stock_code
                 AND snapshot_bar.trade_date = snapshot.latest_price_date
                WHERE snapshot.stock_code = m.stock_code
                  AND snapshot.report_date = complete_financials.report_date
                  AND snapshot.latest_close IS NOT NULL
                  AND snapshot_bar.close IS NOT NULL
                  AND ABS(snapshot.latest_close - snapshot_bar.close)
                      <= GREATEST(ABS(snapshot_bar.close), 1.0) * 0.000001
                  AND snapshot.latest_price_date >= raw.last_date - $price_freshness_max_age_days
            ) AS has_coherent_snapshot,
            -- P0-4/5: 快照期必须等于最新完整三表期（与筛选引擎共用判定）。
            -- 若任一张报表存在晚于完整期的新行，属"数据源未就绪"的披露项
            -- （PRD §7.7），不阻断；快照期与完整期不一致才是阻断项。
            (complete_financials.report_date IS NOT NULL AND (
                lb.max_balance IS NOT NULL AND lb.max_balance > complete_financials.report_date
                OR li.max_income IS NOT NULL AND li.max_income > complete_financials.report_date
                OR lc.max_cashflow IS NOT NULL AND lc.max_cashflow > complete_financials.report_date
            )) AS has_pending_incomplete_period
        FROM stock_meta m
        CROSS JOIN clock
        LEFT JOIN raw ON raw.stock_code = m.stock_code
        LEFT JOIN qfq ON qfq.stock_code = m.stock_code
        LEFT JOIN complete_financials ON complete_financials.stock_code = m.stock_code
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_balance FROM balance_sheet WHERE stock_code = m.stock_code
        ) lb ON true
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_income FROM income_statement WHERE stock_code = m.stock_code
        ) li ON true
        LEFT JOIN LATERAL (
            SELECT MAX(report_date) AS max_cashflow FROM cash_flow WHERE stock_code = m.stock_code
        ) lc ON true
        WHERE m.is_listed IS TRUE
        ORDER BY m.stock_code
        """
    rows = duck.read_query(
        readiness_sql,
        # ─── 命名参数（2026-11-21 修复 `reports/152` 的位置参数错位）─────────
        # 事故：本查询原来用 6 个 `?`，参数表却是
        #   [today, today-7, PRICE_FRESHNESS_MAX_AGE_DAYS, history, history, volume]
        # 于是第 6 个 `?`（快照价格日的新鲜度窗口）绑到了
        # `minimum_volume_observations`(1300)，新鲜度判据实际边界 = **1300 天**；
        # 同一个错位又把 `has_raw_history` 的观测下限从 1300 压到 7。
        # 只要还是位置参数，"谁绑谁"就只存在于人脑里。改用 DuckDB 的 `$name`
        # 具名参数后，绑定关系写在 SQL 里与参数名上，顺序不再有语义；
        # `_bind_named_params` 还会强制「SQL 里的 `$name` 集合 == 这里的键集合」
        # 且禁止混入 `?`，错位/漏绑直接抛错而不是静默错算。
        _bind_named_params(
            readiness_sql,
            {
                # RG1 单源：'今天' 与 '价格陈旧截止日' 都从 cn_today() 派生。
                "today": today.isoformat(),
                "price_stale_before": (
                    today - timedelta(days=PRICE_FRESHNESS_MAX_AGE_DAYS)
                ).isoformat(),
                # 快照价格日相对该股最新 bar 允许落后的天数（同一常量，>7 天即拦）。
                "price_freshness_max_age_days": PRICE_FRESHNESS_MAX_AGE_DAYS,
                # 历史/成交量观测下限：两者默认都是 1300，但**语义不同**，
                # 用同名参数就会再次把两个口径悄悄合并，故分开两个名字。
                "minimum_history_observations": minimum_history_observations,
                "minimum_volume_observations": minimum_volume_observations,
            },
            context="minimum_data_readiness",
        ),
    )
    issues: dict[str, list[str]] = {
        "pool_metadata": [], "raw_history": [], "qfq_history": [],
        "price_freshness": [], "meaningful_volume": [], "financial_period": [],
        "snapshot_price_coherence": [], "share_capital": [],
        "corporate_action_dividend_lineage": [],
        "sector_financials": [], "regulatory_fields": [], "snapshot_input_freshness": [],
        "lineage_coverage": [], "snapshot_period_alignment": [],
        "pending_financial_period": [],
    }
    for row in rows:
        for key, column in (
            ("pool_metadata", "has_pool_metadata"),
            ("raw_history", "has_raw_history"),
            ("qfq_history", "has_qfq_history"),
            ("price_freshness", "has_fresh_prices"),
            ("meaningful_volume", "has_meaningful_volume"),
            ("financial_period", "has_complete_financial_period"),
            ("corporate_action_dividend_lineage", "has_corporate_action_or_dividend_lineage"),
            ("sector_financials", "has_sector_financials"),
            ("regulatory_fields", "has_regulatory_fields"),
            ("snapshot_input_freshness", "has_current_snapshot_inputs"),
            ("snapshot_price_coherence", "has_coherent_snapshot"),
        ):
            if not row[column]:
                issues[key].append(row["stock_code"])
        # has_pending_incomplete_period 为真才是不利状态（列语义与其余相反）
        if row.get("has_pending_incomplete_period"):
            issues["pending_financial_period"].append(row["stock_code"])
    issues["lineage_coverage"].extend(_missing_lineage_coverage(duck, rows))
    # 股本完整性走唯一真相函数（契约 §3）：发布门禁与 readiness 同源，
    # 90 天新股豁免已在该函数内实现，此处不得再写第二份判定。
    issues["share_capital"] = listed_share_capital_gap_codes(duck)
    # P0-4/5: 快照期与最新完整三表期不一致是阻断项（与筛选引擎共用判定），
    # 消除 "ready=true 但筛选全部失败" 的假阳性。
    issues["snapshot_period_alignment"] = [
        row["stock_code"] for row in snapshot_period_mismatches(duck)
    ]
    if sqlite is not None:
        stale_override_codes = _published_override_stale_snapshot_codes(duck, sqlite)
        issues["snapshot_input_freshness"].extend(stale_override_codes)
    for key, codes in issues.items():
        issues[key] = list(dict.fromkeys(codes))
    # 披露性缺口（市场真实状态，非数据损坏）：公司从未分红/无除权事件是
    # 合法事实，筛选指标对其正确返回空值；报表存在晚于完整期的新行是
    # "数据源尚未就绪"（PRD §7.7），快照按完整期计算；银行/券商监管字段
    # 免费源不可得（所有者口径：保持 NULL，不伪造）——均不阻断筛选，
    # 但保留计数供 UI 披露。
    disclosure_keys = {
        "corporate_action_dividend_lineage",
        "pending_financial_period",
        "regulatory_fields",
    }
    recent_listing_disclosure_keys = {
        "price_freshness",
        "financial_period",
        "snapshot_input_freshness",
        "snapshot_price_coherence",
        # 2026-11-17（reports/135）：新股上市首日必然没有行业/指数归属（pool_metadata）
        # 与除权/分红历史（lineage_coverage）。这两项对老股是真缺口，对新股是**必然状态**，
        # 属于披露而非阻断——否则每只新股上市第一天都会让 /api/ready 503。
        "pool_metadata",
        "lineage_coverage",
    }
    recent_codes = {
        row["stock_code"] for row in rows
        # 2026-11-17：不再要求 listing_date 非空——它为空时 is_recent_listing 已为 True
        # （见上面的 SQL），这里若还要求非空，新股会绕过豁免、把 readiness 打成 False。
        if row.get("is_recent_listing")
    }
    missing = {key: codes[:20] for key, codes in issues.items() if codes}
    blocking: dict[str, list[str]] = {}
    for key, codes in issues.items():
        if key in disclosure_keys:
            continue
        # share_capital 走「阈值政策」而非「一票否决」，见下方统一判定。
        if key == "share_capital":
            continue
        blocking_codes = []
        for code in codes:
            # 新品 90 天豁免已在唯一真相函数 / 各列 SQL 内判定，此处不再复制口径。
            if key in recent_listing_disclosure_keys and code in recent_codes:
                continue
            blocking_codes.append(code)
        if blocking_codes:
            blocking[key] = blocking_codes
    # 2026-11-13 P1：readiness 与发布门禁必须同一政策（阈值唯一实现在
    # gate_hard_block_threshold）。
    # 为什么不能"任一缺口即 ready=False"：少量股本缺口在发布侧是被逐股隔离、
    # 其余股票照常发布的；若 readiness 对同一状态判 ready=False，/api/ready
    # 就会 503 假红（同一状态两个相反结论）。只有缺口数超过硬阻断阈值
    # （=怀疑系统性单位错乱、发布侧整批拒绝）才在这里阻断。
    # 未超阈值时缺失计数仍在 missing_counts["share_capital"] 里如实披露。
    share_capital_gaps = issues.get("share_capital") or []
    share_capital_threshold = gate_hard_block_threshold(len(rows))
    # 与 snapshot_publish_gate 同一判定：绝对阈值 OR ≥1/3 占比。两边必须
    # 同源，否则同一状态会出现"发布侧整批拒绝 / readiness=ready"或反之。
    share_capital_blocking = share_capital_hard_block(
        len(share_capital_gaps), len(rows),
    )
    if share_capital_blocking:
        blocking["share_capital"] = share_capital_gaps
    return {
        "ready": bool(rows) and not blocking,
        "stock_count": len(rows),
        "missing": missing,
        "missing_counts": {key: len(codes) for key, codes in issues.items() if codes},
        "disclosure_missing_counts": {
            key: len(issues[key]) for key in disclosure_keys if issues.get(key)
        },
        # 股本缺口的阈值政策如实披露：多少个缺口、阈值多少、是否据此阻断。
        "share_capital_policy": {
            "gap_count": len(share_capital_gaps),
            "threshold": share_capital_threshold,
            "blocking": share_capital_blocking,
            "quarantined": not share_capital_blocking and bool(share_capital_gaps),
        },
        "schema_compatibility": compatibility,
    }


def _missing_lineage_coverage(duck: DuckDBStore, readiness_rows: list[dict]) -> list[str]:
    """Fail closed if materialized inputs or snapshots lost their source graph.

    2026-08-30 提速：原实现把约 957 万条 evidence 行全部拉回 Python 再逐股
    判空，正式库单次约 25 秒。改为在 DuckDB 内先按每只股票所需的
    raw/qfq 最新交易日、完整财务报告期裁剪 evidence，再聚合出缺失股票。
    语义与原实现一致（新股/停牌股仍豁免），结果只返回缺失代码。
    """
    if not readiness_rows:
        return []
    sql = """
        WITH valid_hashes AS (
            SELECT raw_response_hash FROM raw_response_archive_valid_hash
            UNION ALL
            -- 兼容旧测试/维护脚本直接 INSERT raw_response_archive 的路径；
            -- 只回退扫描小型 active 热表，绝不回退到 26GB 的 history 大表。
            SELECT a.raw_response_hash FROM raw_response_archive a
            WHERE a.payload IS NOT NULL
              AND OCTET_LENGTH(a.payload) > 0
              AND NOT EXISTS (
                  SELECT 1 FROM raw_response_archive_valid_hash v
                  WHERE v.raw_response_hash = a.raw_response_hash
              )
        ), raw AS (
            SELECT stock_code, MAX(trade_date) AS last_date
            FROM price_daily_raw WHERE close IS NOT NULL GROUP BY stock_code
        ), qfq AS (
            SELECT stock_code, MAX(trade_date) AS last_date
            FROM price_daily_qfq WHERE close IS NOT NULL GROUP BY stock_code
        ), complete_financials AS (
            SELECT bs.stock_code, MAX(bs.report_date) AS report_date
            FROM balance_sheet bs
            JOIN income_statement ic
              ON ic.stock_code = bs.stock_code AND ic.report_date = bs.report_date
            JOIN cash_flow cf
              ON cf.stock_code = bs.stock_code AND cf.report_date = bs.report_date
            WHERE bs.total_assets IS NOT NULL
              AND bs.total_liabilities IS NOT NULL
              AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
              AND ic.revenue IS NOT NULL
              AND ic.parent_net_profit IS NOT NULL
              AND cf.cf_from_operating IS NOT NULL
            GROUP BY bs.stock_code
        ), base AS (
            SELECT m.stock_code,
                   m.listing_date,
                   raw.last_date AS raw_last_date,
                   qfq.last_date AS qfq_last_date,
                   complete_financials.report_date AS financial_report_date
            FROM stock_meta m
            LEFT JOIN raw ON raw.stock_code = m.stock_code
            LEFT JOIN qfq ON qfq.stock_code = m.stock_code
            LEFT JOIN complete_financials ON complete_financials.stock_code = m.stock_code
            WHERE m.is_listed IS TRUE
        ), evidence AS (
            SELECT base.stock_code,
                   audit.report_date,
                   audit.field_name,
                   batch.data_type
            FROM base
            JOIN source_audit audit ON audit.stock_code = base.stock_code
            JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
            JOIN valid_hashes raw
              ON raw.raw_response_hash = audit.raw_response_hash
              AND (
                  (audit.field_name = 'latest_close'
                   AND audit.report_date IN (base.raw_last_date, base.qfq_last_date))
                  OR (
                      audit.field_name IN (
                          'total_assets', 'total_liabilities', 'total_equity',
                          'total_equity_parent', 'revenue', 'parent_net_profit',
                          'cf_from_operating'
                      )
                      AND audit.report_date = base.financial_report_date
                      AND batch.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                  )
                  OR (
                      batch.data_type = 'indicator_snapshot'
                      AND audit.report_date = base.financial_report_date
                  )
              )
        ), flags AS (
            SELECT base.stock_code,
                   base.listing_date,
                   base.raw_last_date,
                   base.qfq_last_date,
                   base.financial_report_date,
                   BOOL_OR(
                       evidence.data_type = 'price_daily_raw'
                       AND evidence.field_name = 'latest_close'
                       AND evidence.report_date = base.raw_last_date
                   ) AS raw_price_ok,
                   BOOL_OR(
                       evidence.data_type = 'price_daily_qfq'
                       AND evidence.field_name = 'latest_close'
                       AND evidence.report_date = base.qfq_last_date
                   ) AS qfq_price_ok,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name = 'total_assets'
                   ) AS has_total_assets,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name = 'total_liabilities'
                   ) AS has_total_liabilities,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name = 'revenue'
                   ) AS has_revenue,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name = 'parent_net_profit'
                   ) AS has_parent_net_profit,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name = 'cf_from_operating'
                   ) AS has_cf_from_operating,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type IN ('balance_sheet', 'income_statement', 'cash_flow')
                       AND evidence.field_name IN ('total_equity', 'total_equity_parent')
                   ) AS has_equity,
                   BOOL_OR(
                       evidence.report_date = base.financial_report_date
                       AND evidence.data_type = 'indicator_snapshot'
                   ) AS snapshot_ok
            FROM base
            LEFT JOIN evidence ON evidence.stock_code = base.stock_code
            GROUP BY base.stock_code, base.listing_date, base.raw_last_date,
                     base.qfq_last_date, base.financial_report_date
        )
        SELECT flags.stock_code
        FROM flags
        WHERE NOT (
            COALESCE(flags.raw_price_ok, FALSE)
            AND COALESCE(flags.qfq_price_ok, FALSE)
            AND COALESCE(flags.has_total_assets, FALSE)
            AND COALESCE(flags.has_total_liabilities, FALSE)
            AND COALESCE(flags.has_revenue, FALSE)
            AND COALESCE(flags.has_parent_net_profit, FALSE)
            AND COALESCE(flags.has_cf_from_operating, FALSE)
            AND COALESCE(flags.has_equity, FALSE)
            AND COALESCE(flags.snapshot_ok, FALSE)
        )
          -- 新股（上市不足 90 天）与停牌股（最近 bar 超过 7 天前，无法抓取
          -- 新数据）的原始响应仍在积累/冻结中，属披露项，不阻断筛选。
          -- RG1 时区单源（2026-11-21）：'今天'用 `$today`（cn_today() 绑定），
          -- 停牌窗口用 `$price_stale_before`（= 今天 - PRICE_FRESHNESS_MAX_AGE_DAYS，
          -- 与 has_fresh_prices / has_coherent_snapshot 同一常量），不写 CURRENT_DATE。
          AND (flags.listing_date IS NULL
               OR flags.listing_date < $today::DATE - INTERVAL '90 days')
          AND (flags.raw_last_date IS NULL
               OR flags.raw_last_date >= $price_stale_before::DATE)
        ORDER BY flags.stock_code
        """
    today = cn_today()
    rows = duck.read_query(
        sql,
        _bind_named_params(
            sql,
            {
                "today": today.isoformat(),
                "price_stale_before": (
                    today - timedelta(days=PRICE_FRESHNESS_MAX_AGE_DAYS)
                ).isoformat(),
            },
            context="lineage_coverage",
        ),
    )
    return [row["stock_code"] for row in rows]


_ARCHIVE_HASH_CACHE_TTL_SECONDS = 60.0
_archive_hash_mismatch_cache: dict[str, tuple[float, int]] = {}


def _archive_hash_mismatch_rows(
    duck: DuckDBStore, *, force_refresh: bool = False
) -> tuple[int, bool]:
    """Count archived payloads failing SHA-256 verification, cached briefly.

    Returns (mismatch_rows, cache_hit). Rows written by the adapters are
    hashed before insert and carry integrity_verified=TRUE (schema v7), so a
    cold scan only re-checks the rows that have not been marked verified yet.
    This keeps the fail-closed check while avoiding a ~40s read of all 6GB+
    of archived payloads on every process start. Failures are never cached.
    """
    cache_key = str(duck.db_path)
    now = time.monotonic()
    if not force_refresh:
        cached = _archive_hash_mismatch_cache.get(cache_key)
        if cached is not None and now - cached[0] < _ARCHIVE_HASH_CACHE_TTL_SECONDS:
            return cached[1], True
    try:
        try:
            rows = duck.read_query(
                "SELECT raw_response_hash, payload FROM raw_response_archive_all "
                "WHERE payload IS NOT NULL AND COALESCE(integrity_verified, FALSE) = FALSE"
            )
        except Exception:
            # 兼容尚无 integrity_verified 列的旧测试库/历史 schema。
            rows = duck.read_query(
                "SELECT raw_response_hash, payload FROM raw_response_archive_all WHERE payload IS NOT NULL"
            )
        mismatches = sum(
            1
            for archive in rows
            if not isinstance(archive["payload"], bytes)
            or hashlib.sha256(archive["payload"]).hexdigest() != archive["raw_response_hash"]
        )
    except Exception:
        return 1, False
    _archive_hash_mismatch_cache[cache_key] = (now, mismatches)
    return mismatches, False


# Warnings that invalidate every value read from indicator_snapshot.
_SNAPSHOT_BLOCKING_WARNINGS = frozenset({
    "FINANCIAL_SHELL_ROWS",
    "SNAPSHOT_STALE",
    "LINEAGE_INVALID",
    "CODE_IDENTITY_ALIAS",
    "LIVE_SCHEMA_INCOMPATIBLE",
})

# 详情页"全空"与筛选页 409 共用的机器可读原因码（detail.reason）。
SNAPSHOT_UNTRUSTED_REASON = "snapshot_values_untrusted"

# Warnings that invalidate only dividend-derived indicator fields.
_DIVIDEND_BLOCKING_WARNINGS = frozenset({"DIVIDEND_DATES_UNVERIFIED"})

# P3-3 修复（reports/73）：国债/股息率域字段与前端 data-quality.ts 对齐，
# DIVIDEND_DATES_UNVERIFIED 时一并遮蔽。
DIVIDEND_INDICATOR_FIELDS = (
    "consecutive_div_years",
    "cumulative_dividend_amount",
    "dividend_financing_ratio_pct",
    "dividend_yield",
    "dps",
    "payout_ratio",
    "ttm_dividend_yield",
    "div_yield_spread_0p25y",
    "div_yield_spread_0p5y",
    "div_yield_spread_1y",
    "div_yield_spread_2y",
    "div_yield_spread_3y",
    "div_yield_spread_5y",
    "div_yield_spread_7y",
    "div_yield_spread_10y",
    "div_yield_spread_30y",
)

# ─── 字段级遮蔽策略（reports/122 §2.3 J9）──────────────────────────────────────
# 背景：`vd data doctor` 一次报 32 条 FIELD_COVERAGE_LOW + 54 条
# NULL_REASON_MISSING，真问题（扩展指标报告期错位、扣非整期空）被噪音淹没；
# 而这两条真问题本该**字段级遮蔽**，不是全站阻断。此处的策略表是"哪个
# warning code 让哪些字段不可信"的唯一真相，筛选接口与引擎都从这里取。
#
# 维护约定：新增一条要有"为什么只坏这几个字段"的判据；宁可少遮（可解释）
# 也不要多遮（整页 NULL 等于把信息全丢掉）。
DEDUCTED_PROFIT_INDICATOR_FIELDS: tuple[str, ...] = (
    "deducted_profit_yoy",
    "deducted_profit_cagr3",
    "deducted_profit_cagr5",
    "income.deducted_net_profit",
)

# 扩展域字段清单从筛引擎常量派生（单一真相），延迟导入避免循环依赖。
def _extended_indicator_fields() -> tuple[str, ...]:
    """扩展指标域字段清单（来自 `screening.engine.EXTENDED_COLUMNS`）。"""
    try:
        from app.core.screening.engine import EXTENDED_COLUMNS
    except Exception:  # pragma: no cover - 只在极端导入失败时降级
        logger.warning("读取 EXTENDED_COLUMNS 失败，扩展域遮蔽退化为空集", exc_info=True)
        return ()
    return tuple(sorted(EXTENDED_COLUMNS))


def masked_fields_for_codes(warning_codes: list[str] | tuple[str, ...]) -> list[str]:
    """把 warning codes 翻译成"不可信字段"清单（字段级遮蔽的唯一公开入口）。

    2026-11-17（reports/136 §6.2）：这里**只保留一个名字**。曾有一个指向同一
    实现的别名函数，两处都能跑 → grep 失效、改一处漏一处；已删除，新增调用
    一律走本函数（回归护栏：`tests/regression/test_trust_gate_single_entry.py`）。
    """
    codes = set(warning_codes)
    masked: set[str] = set()
    if "EXT_PERIOD_MISMATCH" in codes:
        # ext 与快照报告期错位 → 按报告期严格 join 的扩展字段全空/错期。
        # 快照域（PE/PB/ROE/技术指标）不受影响，绝不能被一起遮掉。
        masked.update(_extended_indicator_fields())
    if "DEDUCTED_PROFIT_GAP" in codes:
        masked.update(DEDUCTED_PROFIT_INDICATOR_FIELDS)
    return sorted(masked)


def indicator_trust(warning_codes: list[str]) -> dict:
    """Server-authoritative trust decision for snapshot-derived indicator values.

    Mirrors the frontend isIndicatorUntrusted policy: snapshot-dependent
    warnings invalidate every indicator field, dividend warnings invalidate
    only dividend fields, and operational warnings invalidate no numeric
    field. Read-only endpoints mask the affected values instead of serving
    them as normal research data.

    2026-11-14（reports/122 §2.3 J9）**字段级遮蔽**：还有一个中间档 ——
    `masked_fields`。它回答的是"这次坏的是**哪几个字段**"，而不是
    "整站数字能不能信"：

      EXT_PERIOD_MISMATCH → 只遮扩展域（ext 与快照报告期错位的 55 列）；
      DEDUCTED_PROFIT_GAP → 只遮扣非四兄弟（yoy/cagr3/cagr5/明细列）。

    为什么不能塞进 `untrusted_all`：那会把 PE/PB/ROE 这些完全正常的价格域/
    财务域指标一起打成 NULL（整页"—"），用户无法区分"这一个字段坏了"和
    "全站坏了"。字段级遮蔽才是这两条规则的正确表达。
    """
    codes = set(warning_codes)
    return {
        "warning_codes": list(warning_codes),
        "untrusted_all": bool(codes & _SNAPSHOT_BLOCKING_WARNINGS),
        "untrusted_fields": (
            sorted(DIVIDEND_INDICATOR_FIELDS)
            if codes & _DIVIDEND_BLOCKING_WARNINGS
            else []
        ),
        "masked_fields": masked_fields_for_codes(warning_codes),
    }


def screenability_block_reason(trust: dict) -> str | None:
    """Return why snapshot-derived numbers cannot be used, else ``None``.

    This is the single translation from a trust decision to a block reason.
    Both read and screening paths must call it instead of re-checking
    ``trust["untrusted_all"]`` themselves:

      - 详情页/自选：`mask_untrusted_values` 据此把整页指标置 null；
      - 筛选页：`_require_current_screenability` 据此返回
        ``409 {"reason": "snapshot_values_untrusted"}``。

    字段级遮蔽（`masked_fields`）不在这里判：它是否阻断取决于规则是否
    引用该字段，由筛选引擎的 `MaskedFieldError` 负责。
    """
    if trust.get("untrusted_all"):
        return SNAPSHOT_UNTRUSTED_REASON
    return None


def mask_untrusted_values(values: dict, trust: dict) -> dict:
    """Null the snapshot-derived values the server cannot vouch for (fail closed)."""
    if screenability_block_reason(trust) is not None:
        return {key: None for key in values}
    untrusted = set(trust["untrusted_fields"]) | set(trust.get("masked_fields") or ())
    return {
        key: (None if key in untrusted else value)
        for key, value in values.items()
    }


_WARNING_CODES_CACHE_TTL_SECONDS = 30.0
_WARNING_CODES_STATE_KEY = "warning_codes_cache"
_warning_codes_cache: dict[str, tuple[float, list[str]]] = {}
# reports/76 P1-2 增强：TTL 过期后不阻塞请求——返回 stale 结果并后台单飞重建
_warning_codes_refresh_lock = threading.Lock()
_warning_codes_refreshing: set[str] = set()


def _build_warning_codes_low_memory(duck: DuckDBStore, sqlite: SQLiteStore) -> list[str]:
    """Full quality build under the process-uniform DuckDB budget.

    The full lineage/archive scan is the only operation in this service that
    approaches the multi-GiB DuckDB limit. Connection settings are fixed by
    database.* (8GB / 2 threads / preserve_insertion_order=false on the
    formal profile). This function must NOT call memory_limit(): a different
    configuration in the same web process would make ordinary K-line/detail
    queries fail with different-configuration errors. Hot read paths never run
    this synchronously in formal mode.
    """
    return list(build_data_quality_status_cached(duck, sqlite)["warning_codes"])


def _persisted_warning_codes(sqlite: SQLiteStore) -> list[str]:
    try:
        rows = sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?",
            [_WARNING_CODES_STATE_KEY],
        )
        if rows and rows[0].get("value"):
            value = json.loads(rows[0]["value"])
            if isinstance(value, list):
                return [str(item) for item in value]
    except Exception:
        pass
    return []


def _persist_warning_codes(sqlite: SQLiteStore, codes: list[str]) -> None:
    try:
        with sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO data_refresh_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                [_WARNING_CODES_STATE_KEY, json.dumps(codes, ensure_ascii=False),
                 datetime.now(UTC).isoformat()],
            )
    except Exception:
        logger.warning("持久化 warning_codes 缓存失败", exc_info=True)


def read_warning_codes(duck: DuckDBStore, sqlite: SQLiteStore) -> list[str]:
    """Return quality warning codes for read-only display, cached briefly.

    The full status build re-verifies readiness across the whole pool; hot
    read paths (stock indicators, watchlist) reuse the process-local result
    instead of rebuilding it per request. Fail closed: a build failure
    reports LINEAGE_INVALID, and failures are never cached.

    Write-lock awareness (reports/76 P1-2): while the auto-update writer
    holds the DuckDB file, the full build (3,280 万行 source_audit 扫描 +
    全市场一致性子查询) would take 40~60s or fail with file-open races.
    During that window we serve the last idle-time result when present and
    otherwise skip the rebuild entirely (return []); callers detect the
    window via update_lock_active and label responses with
    auto_update_in_progress instead of blocking. Integrity is preserved:
    screening is separately gated, and the next idle build restores exact
    codes within one TTL.

    Stale-while-revalidate: an expired cache returns the previous result
    immediately and rebuilds in a single-flight background thread, so hot
    paths never wait for a 20~60s full-universe scan.
    """
    cache_key = f"{duck.db_path}|{sqlite.db_path}"
    now = time.monotonic()
    cached = _warning_codes_cache.get(cache_key)
    if cached is not None and now - cached[0] < _WARNING_CODES_CACHE_TTL_SECONDS:
        return list(cached[1])
    try:
        from app.core.storage.update_lock import update_lock_active

        if update_lock_active(duck.db_path):
            return list(cached[1]) if cached is not None else _persisted_warning_codes(sqlite)
    except Exception:
        pass
    if cached is not None:
        _ensure_warning_codes_refresh(duck, sqlite, cache_key)
        return list(cached[1])
    # S1 测试进程需要确定性同步结果；正式服务第一次请求绝不执行 40-80s
    # 的全库扫描——用持久化缓存兜底并触发后台单飞刷新。
    if os.environ.get("VD_ENV") == "test":
        try:
            warning_codes = _build_warning_codes_low_memory(duck, sqlite)
        except Exception:
            return ["LINEAGE_INVALID"]
        _warning_codes_cache[cache_key] = (now, warning_codes)
        return list(warning_codes)
    persisted = _persisted_warning_codes(sqlite)
    _ensure_warning_codes_refresh(duck, sqlite, cache_key)
    return persisted


def _ensure_warning_codes_refresh(duck: DuckDBStore, sqlite: SQLiteStore, key: str) -> None:
    """Start a single-flight background rebuild; never blocks the request."""
    with _warning_codes_refresh_lock:
        if key in _warning_codes_refreshing:
            return
        _warning_codes_refreshing.add(key)
    threading.Thread(
        target=_warning_codes_refresh_worker,
        args=(duck, sqlite, key),
        name="vd-warning-codes-refresh",
        daemon=True,
    ).start()


def _warning_codes_refresh_worker(duck: DuckDBStore, sqlite: SQLiteStore, key: str) -> None:
    try:
        from app.core.storage.update_lock import update_lock_active

        if update_lock_active(duck.db_path):
            return
        warning_codes = _build_warning_codes_low_memory(duck, sqlite)
        _warning_codes_cache[key] = (time.monotonic(), warning_codes)
        _persist_warning_codes(sqlite, warning_codes)
    except Exception as error:
        logger.warning("后台 warning codes 刷新失败: %s", error)
    finally:
        with _warning_codes_refresh_lock:
            _warning_codes_refreshing.discard(key)


def build_data_quality_status(
    duck: DuckDBStore, sqlite: SQLiteStore, *, force_archive_hash_recheck: bool = False
) -> dict:
    """Return machine-readable freshness, completeness, lineage, and operation warnings."""
    readiness = minimum_data_readiness(duck, sqlite)
    if not readiness["schema_compatibility"]["compatible"]:
        return _incompatible_schema_status(readiness, sqlite)

    dates = _data_dates(duck)
    dividends = duck.read_query(
        """
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (
                WHERE announcement_date IS NULL
                  AND ex_date >= DATE '2005-01-01'
                  AND (
                    COALESCE(dividend_per_share, 0) != 0
                    OR COALESCE(stock_dividend, 0) != 0
                    OR COALESCE(transfer_share, 0) != 0
                    OR COALESCE(rights_issue, 0) != 0
                  )
            ) AS active_missing_announcement_rows,
            COUNT(*) FILTER (
                WHERE announcement_date IS NULL
                  AND ex_date < DATE '2005-01-01'
                  AND (
                    COALESCE(dividend_per_share, 0) != 0
                    OR COALESCE(stock_dividend, 0) != 0
                    OR COALESCE(transfer_share, 0) != 0
                    OR COALESCE(rights_issue, 0) != 0
                  )
            ) AS legacy_missing_announcement_rows,
            COUNT(*) FILTER (
                WHERE announcement_date IS NULL
                  AND (
                      (EXTRACT(MONTH FROM ex_date) = 12 AND EXTRACT(DAY FROM ex_date) = 31)
                      OR (EXTRACT(MONTH FROM ex_date) = 6 AND EXTRACT(DAY FROM ex_date) = 30)
                  )
            ) AS unverified_period_end_rows
        FROM dividends
        """
    )[0]
    try:
        from app.core.source_audit_archive import read_archive_state
        source_audit_archive_state = read_archive_state(sqlite) or {
            "archived_rows": 0, "note": "not_archived_yet",
        }
    except Exception:
        source_audit_archive_state = {"archived_rows": 0, "note": "unavailable"}
    lineage = duck.read_query(
        """
        WITH valid_hashes AS (
            SELECT raw_response_hash FROM raw_response_archive_valid_hash
            UNION ALL
            -- 兼容旧测试/维护脚本直接 INSERT raw_response_archive 的路径；
            -- 只回退扫描小型 active 热表，绝不回退到 26GB 的 history 大表。
            SELECT a.raw_response_hash FROM raw_response_archive a
            WHERE a.payload IS NOT NULL
              AND OCTET_LENGTH(a.payload) > 0
              AND NOT EXISTS (
                  SELECT 1 FROM raw_response_archive_valid_hash v
                  WHERE v.raw_response_hash = a.raw_response_hash
              )
        ), batch_hash AS (
            -- 2026-11-12 契约 §3 扩展：归档缺口必须区分「真缺口」与「历史事实」。
            -- 判据只有一条：**摘要形态**决定它是不是 legacy。
            --   现行溯源约定是 64 位内容寻址 SHA256；**32 位十六进制字节串本身
            --   就是 MD5 时代摘要的形态，与哪个适配器写的无关**。
            --   现代批次（64 位 SHA256）：摘要即内容寻址，缺原始字节归档 =
            --   真 lineage 断裂 → 计入 batch_archive_gap_rows → LINEAGE_INVALID 阻断。
            --   legacy 批次（32 位 MD5 形态，2026-09-19~20 那批历史回填）：导入时
            --   从未落原始字节，这是已由 STATUS 披露的历史事实，不是今天的数据损坏
            --   → 只计入 legacy_archive_gap_batches 如实披露，不得触发 LINEAGE_INVALID。
            -- 2026-11-12 二次裁决：曾用 `adapter_version LIKE 'csmar-%'` 白名单保守兜底，
            -- 被正式库证伪——同一战役里还有 6 个东财批次（eastmoney-f10-xjllb/1、
            -- eastmoney-f10-zcfzb/1、em-datacenter-mainfinadata），32 位 MD5 且无归档。
            -- 白名单会把它们误判成 lineage 断裂，故收敛为纯形态判定（唯一表达式，
            -- source_audit 侧与 fetch_batch 侧共用同一口径）。
            SELECT b.batch_id,
                   b.raw_response_hash,
                   COALESCE(
                       LENGTH(b.raw_response_hash) = 32
                       AND regexp_matches(lower(b.raw_response_hash), '^[0-9a-f]{32}$'),
                       FALSE
                   )
                   -- 2026-11-17（reports/135）：还有一类**同性质的已披露历史缺口**——
                   -- 补采管线在 2026-11-14~15 曾把摘要写成
                   -- `f"{endpoint}:{batch_id}"[:32]`（形如 `xjllbAjaxNew:451ef0…`），
                   -- 既非内容寻址、原始响应也从未归档。生成侧已在 reports/124 改为
                   -- `sha256(请求描述)` + 先归档后登记；这 13 个历史批次不可回溯，
                   -- 与 32 位 MD5 时代那批同类：**如实计入 legacy 披露，不算今天的数据损坏**。
                   -- 判据只看形态（endpoint 名 + 冒号 + 十六进制串），与来源无关。
                   OR COALESCE(
                       regexp_matches(b.raw_response_hash, '^[A-Za-z_]+:[0-9a-f]{16,32}$'),
                       FALSE
                   ) AS is_legacy_digest,
                   NOT EXISTS (
                       SELECT 1 FROM valid_hashes archive
                       WHERE archive.raw_response_hash = b.raw_response_hash
                   ) AS archive_gap
            FROM fetch_batch b
        ), audit_hash AS (
            -- legacy 形态只看该行自身的摘要字节串，与它属于哪个批次无关。
            SELECT s.raw_response_hash,
                   LENGTH(s.raw_response_hash) AS hash_len,
                   COALESCE(
                       LENGTH(s.raw_response_hash) = 32
                       AND regexp_matches(lower(s.raw_response_hash), '^[0-9a-f]{32}$'),
                       FALSE
                   ) AS is_legacy_digest
            FROM source_audit s
        )
        SELECT
            (SELECT COUNT(*) FROM audit_hash
             WHERE hash_len != 64 AND NOT is_legacy_digest) AS invalid_hash_rows,
            (SELECT COUNT(*) FROM audit_hash WHERE is_legacy_digest) AS legacy_hash_rows,
            (SELECT COUNT(*) FROM source_audit s
             WHERE s.fetch_batch_id IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM fetch_batch f WHERE f.batch_id = s.fetch_batch_id
                )) AS orphan_batch_rows,
            (SELECT COUNT(*) FROM source_audit s
             WHERE LENGTH(s.raw_response_hash) = 64
               AND NOT EXISTS (
                   SELECT 1 FROM valid_hashes archive
                   WHERE archive.raw_response_hash = s.raw_response_hash
               )) AS audit_archive_gap_rows,
            (SELECT COUNT(*) FROM source_audit s
             WHERE EXISTS (
                 -- 2026-10-30：active 热表直接检查空 payload。不能用
                 -- raw_response_archive_all 的 storage 列：轮转/重建后的
                 -- 合并视图不保证带 storage，曾导致 summary 每 5 分钟 503。
                 SELECT 1 FROM raw_response_archive archive
                 WHERE archive.raw_response_hash = s.raw_response_hash
                   AND (archive.payload IS NULL OR OCTET_LENGTH(archive.payload) = 0)
             )) AS empty_archive_payload_rows,
            (SELECT COUNT(*) FROM batch_hash
             WHERE archive_gap AND NOT is_legacy_digest) AS batch_archive_gap_rows,
            (SELECT COUNT(*) FROM batch_hash
             WHERE archive_gap AND is_legacy_digest) AS legacy_archive_gap_batches
        """
    )[0]
    lineage["archive_gap_rows"] = (
        lineage["audit_archive_gap_rows"]
        + lineage["batch_archive_gap_rows"]
        + lineage["empty_archive_payload_rows"]
    )
    # 注：legacy_archive_gap_batches（32 位 MD5 摘要的历史回填批次从未落原始字节，
    # 与适配器无关）故意不计入 archive_gap_rows —— 它是已披露的历史事实，不是
    # 今天的数据损坏。真缺口（64 位内容寻址批次缺归档）仍走 batch_archive_gap_rows
    # → LINEAGE_INVALID 阻断。
    lineage["source_audit_archive"] = source_audit_archive_state
    # integrity_verified 标记由写路径在插入前校验；冷启动只重算未标记行，
    # 避免把 6GB+ 归档 BLOB 全部读回。未标记行仍逐条验证，验证失败继续
    # 报 LINEAGE_INVALID（fail closed）。
    lineage["hash_mismatch_rows"], hash_check_cached = _archive_hash_mismatch_rows(
        duck, force_refresh=force_archive_hash_recheck
    )
    lineage["archive_hash_check"] = {
        "cached": hash_check_cached,
        "ttl_seconds": int(_ARCHIVE_HASH_CACHE_TTL_SECONDS),
    }
    code_identity = duck.read_query(
        """
        WITH listed_codes AS (
            SELECT stock_code,
                   regexp_replace(upper(trim(stock_code)), '^(SH|SZ|BJ)|\\.(SH|SS|SZ|BJ)$', '', 'g') AS normalized_code
            FROM stock_meta WHERE is_listed IS TRUE
        )
        SELECT COUNT(DISTINCT raw.stock_code) AS raw_alias_rows
        FROM price_daily_raw raw
        JOIN listed_codes meta
          ON regexp_replace(upper(trim(raw.stock_code)), '^(SH|SZ|BJ)|\\.(SH|SS|SZ|BJ)$', '', 'g') = meta.normalized_code
         AND raw.stock_code != meta.stock_code
        """
    )[0]
    operations = {
        "unpublished_overrides": sqlite.query(
            """
            SELECT COUNT(*) AS count FROM manual_overrides
            WHERE status != 'published' AND rolled_back_at IS NULL
            """
        )[0]["count"],
        # 仅当 running job 悬挂（超过 2 小时未结束）才算 stale：
        # 正常的自动更新运行时 job 处于 running 是预期状态，
        # 若一律计数会误报 STALE_RUNNING_JOBS 警告。
        "running_jobs": sqlite.query(
            """SELECT COUNT(*) AS count FROM job_logs
               WHERE status = 'running' AND started_at < ?""",
            [(datetime.now(UTC) - timedelta(hours=2)).isoformat()],
        )[0]["count"],
    }

    warning_codes: list[str] = []
    balance_dates = dates["balance_sheet"]
    income_dates = dates["income_statement"]
    if (
        balance_dates["latest_record"] != balance_dates["latest_complete"]
        or income_dates["latest_record"] != income_dates["latest_complete"]
    ):
        warning_codes.append("FINANCIAL_SHELL_ROWS")
    if _is_stale(
        dates["price"], dates["indicator_snapshot"]["latest_price_date"],
        days=PRICE_FRESHNESS_MAX_AGE_DAYS,
    ):
        warning_codes.append("SNAPSHOT_STALE")
    if dividends["active_missing_announcement_rows"]:
        warning_codes.append("DIVIDEND_DATES_UNVERIFIED")
    if (
        lineage["invalid_hash_rows"]
        or lineage["orphan_batch_rows"]
        or lineage["archive_gap_rows"]
        or lineage["hash_mismatch_rows"]
    ):
        warning_codes.append("LINEAGE_INVALID")
    if code_identity["raw_alias_rows"]:
        warning_codes.append("CODE_IDENTITY_ALIAS")
    if operations["unpublished_overrides"]:
        warning_codes.append("UNPUBLISHED_OVERRIDES")
    if operations["running_jobs"]:
        warning_codes.append("STALE_RUNNING_JOBS")
    if not readiness["ready"]:
        warning_codes.append("MINIMUM_DATA_NOT_READY")

    # 2026-11-14（reports/119）：把人工审查结论固化成的 14 条资产健康规则（见 asset_health.RULE_CODES）挂进
    # 同一个状态载荷。这里只跑**廉价规则**（正式库实测 <2s），贵重规则（归档
    # 交叉核验 ~31s）留给 `vd data doctor`；预算耗尽/未启用的规则在 asset_health
    # 里如实标 skipped，不会被静默丢弃。
    #
    # 纪律：本模块的 code **不进** `_SNAPSHOT_BLOCKING_WARNINGS` /
    # `_SCREENING_BLOCKING_WARNINGS` —— 它们只披露，不遮蔽指标、不让筛选 409。
    # 升级为阻断型必须另开裁决。
    asset_health = _asset_health_summary(duck, sqlite)
    for code in asset_health.get("p1_codes", []):
        if code not in warning_codes:
            warning_codes.append(code)

    return {
        "minimum_data_readiness": readiness,
        "dates": dates,
        "dividends": dividends,
        "lineage": lineage,
        "code_identity": code_identity,
        "operations": operations,
        "warning_codes": warning_codes,
        "asset_health": asset_health,
    }


def _asset_health_summary(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    """Run the cheap asset-health rules for the status payload (never raises).

    体检本身坏掉时返回一个显式不可用的摘要，而不是让整个数据状态页 503：
    状态页的主要价值（日期/lineage/warning_codes）不应被新加的披露项拖垮。

    2026-11-14（reports/122 §2.4 J10）**分层调度**：状态页/每轮只跑 cheap 档
    （目标 <5s）；expensive 档（归档交叉核验 ~35s）只由 `vd data doctor`
    全量或每日定时跑。这里不跑它，但必须把它的"上次运行时间 + 结果摘要"
    从缓存读出来附在 `asset_health.expensive` 里 —— 否则运维只能靠人记得
    "上次核验是什么时候"，等于没有分层。
    """
    try:
        from app.core.quality.asset_health import (
            load_expensive_summary,
            run_asset_health,
            summarize_asset_health,
        )

        payload = run_asset_health(duck, sqlite, allow_expensive=False)
        summary = summarize_asset_health(payload)
        summary["expensive"] = load_expensive_summary(sqlite)
        return summary
    except Exception:
        logger.warning("资产健康检查失败（数据状态降级，不阻断）", exc_info=True)
        return {
            "generated_at": None,
            "run_id": None,
            "level_counts": {},
            "severity_counts": {},
            "code_counts": {},
            "p1_codes": [],
            "finding_count": 0,
            "findings": [],
            "truncated": 0,
            "rules": [],
            "skipped": [],
            "errors": [{"code": "ASSET_HEALTH_UNAVAILABLE", "error": "status build failed"}],
            "elapsed_ms": None,
            "expensive": {"status": "not_run", "reason": "asset_health_unavailable"},
        }


def _live_schema_compatibility(duck: DuckDBStore) -> dict:
    """Report whether the live analytical schema can support the quality contract."""
    required = {
        "stock_meta": {"stock_code", "exchange", "listing_date", "is_listed", "is_st", "is_suspended"},
        "price_daily_raw": {"stock_code", "trade_date", "close"},
        "price_daily_qfq": {"stock_code", "trade_date", "close"},
        "balance_sheet": {"stock_code", "report_date", "total_assets", "total_liabilities", "total_equity", "total_equity_parent"},
        "income_statement": {"stock_code", "report_date", "revenue", "parent_net_profit"},
        "cash_flow": {"stock_code", "report_date", "cf_from_operating"},
        "indicator_snapshot": {"stock_code", "report_date", "latest_close", "latest_price_date", "calculated_at"},
        "dividends": {"stock_code", "announcement_date", "dividend_per_share", "stock_dividend", "transfer_share", "rights_issue"},
        "source_audit": {"stock_code", "fetch_batch_id", "raw_response_hash"},
        "fetch_batch": {"batch_id"},
        "raw_response_archive": {"raw_response_hash"},
    }
    rows = duck.read_query(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'main'"
    )
    available: dict[str, set[str]] = {}
    for row in rows:
        available.setdefault(row["table_name"], set()).add(row["column_name"])
    missing = [
        f"{table}.{column}"
        for table, columns in required.items()
        for column in sorted(columns - available.get(table, set()))
    ]
    return {"compatible": not missing, "missing": missing}


def _published_override_stale_snapshot_codes(
    duck: DuckDBStore, sqlite: SQLiteStore,
) -> list[str]:
    """Return stocks whose materialized snapshot predates a published correction."""
    overrides = sqlite.query(
        """SELECT DISTINCT stock_code FROM manual_overrides
           WHERE status = 'published' AND rolled_back_at IS NULL"""
    )
    if not overrides:
        return []
    codes = [row["stock_code"] for row in overrides]
    slots = ", ".join("?" for _ in codes)
    snapshots = duck.read_query(
        f"""SELECT stock_code, MAX(calculated_at) AS calculated_at
             FROM indicator_snapshot WHERE stock_code IN ({slots}) GROUP BY stock_code""",
        codes,
    )
    snapshot_by_code = {row["stock_code"]: row["calculated_at"] for row in snapshots}
    stale: list[str] = []
    for override in sqlite.query(
        """SELECT stock_code, MAX(created_at) AS published_at FROM manual_overrides
           WHERE status = 'published' AND rolled_back_at IS NULL GROUP BY stock_code"""
    ):
        calculated_at = snapshot_by_code.get(override["stock_code"])
        if calculated_at is None or _as_datetime(override["published_at"]) > _as_datetime(calculated_at):
            stale.append(override["stock_code"])
    return stale


def _as_datetime(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _incompatible_schema_status(readiness: dict, sqlite: SQLiteStore) -> dict:
    """Keep the status API truthful instead of querying a known-incompatible database."""
    operations = {
        "unpublished_overrides": sqlite.query(
            "SELECT COUNT(*) AS count FROM manual_overrides WHERE status != 'published' AND rolled_back_at IS NULL"
        )[0]["count"],
        "running_jobs": sqlite.query(
            "SELECT COUNT(*) AS count FROM job_logs "
            "WHERE status = 'running' AND started_at < ?",
            [(datetime.now(UTC) - timedelta(hours=2)).isoformat()],
        )[0]["count"],
    }
    return {
        "minimum_data_readiness": readiness,
        "dates": {
            "price": None,
            "balance_sheet": {"latest_record": None, "latest_complete": None},
            "income_statement": {"latest_record": None, "latest_complete": None},
            "cash_flow": {"latest_record": None, "latest_complete": None},
            "indicator_snapshot": {"latest_complete": None, "calculated_at": None, "latest_price_date": None},
        },
        "dividends": {"total_rows": 0, "active_missing_announcement_rows": 0, "unverified_period_end_rows": 0},
        "lineage": {
            "invalid_hash_rows": 0, "orphan_batch_rows": 0,
            "audit_archive_gap_rows": 0, "batch_archive_gap_rows": 0, "archive_gap_rows": 0,
            # 2026-11-12：legacy 摘要披露项，schema 不兼容时同样给 0，保持载荷形状一致。
            "legacy_hash_rows": 0, "legacy_archive_gap_batches": 0,
        },
        "code_identity": {"raw_alias_rows": 0},
        "operations": operations,
        "warning_codes": ["LIVE_SCHEMA_INCOMPATIBLE", "MINIMUM_DATA_NOT_READY"],
        # 载荷形状与正常构建保持一致：live schema 不兼容时体检无法进行，
        # 如实披露 unavailable，而不是留一个键让前端判断 undefined。
        "asset_health": {
            "generated_at": None,
            "run_id": None,
            "level_counts": {},
            "severity_counts": {},
            "code_counts": {},
            "p1_codes": [],
            "finding_count": 0,
            "findings": [],
            "truncated": 0,
            "rules": [],
            "skipped": [{"code": "ALL", "reason": "live_schema_incompatible"}],
            "errors": [],
            "elapsed_ms": None,
            "expensive": {"status": "not_run", "reason": "live_schema_incompatible"},
        },
    }


def _data_dates(duck: DuckDBStore) -> dict:
    price = duck.read_query("SELECT MAX(trade_date) AS value FROM price_daily_raw")[0]["value"]
    balance = duck.read_query(
        """
        SELECT
            MAX(report_date) AS latest_record,
            MAX(report_date) FILTER (
                WHERE total_assets IS NOT NULL
                  AND total_liabilities IS NOT NULL
                  AND COALESCE(total_equity_parent, total_equity) IS NOT NULL
            ) AS latest_complete
        FROM balance_sheet
        """
    )[0]
    income = duck.read_query(
        """
        SELECT
            MAX(report_date) AS latest_record,
            MAX(report_date) FILTER (
                WHERE revenue IS NOT NULL AND parent_net_profit IS NOT NULL
            ) AS latest_complete
        FROM income_statement
        """
    )[0]
    cash_flow = duck.read_query(
        """
        SELECT
            MAX(report_date) AS latest_record,
            MAX(report_date) FILTER (WHERE cf_from_operating IS NOT NULL) AS latest_complete
        FROM cash_flow
        """
    )[0]
    snapshot_columns = duck.read_query(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'indicator_snapshot'
        """
    )
    if "calculated_at" in {row["column_name"] for row in snapshot_columns}:
        snapshot = duck.read_query(
            """
            SELECT
                MAX(report_date) AS latest_complete,
                MAX(calculated_at) AS calculated_at,
                MAX(latest_price_date) AS latest_price_date
            FROM indicator_snapshot
            """
        )[0]
    else:
        snapshot = {
            "latest_complete": duck.read_query(
                "SELECT MAX(report_date) AS value FROM indicator_snapshot"
            )[0]["value"],
            "calculated_at": None,
            "latest_price_date": None,
        }
    return {
        "price": _date_string(price),
        "balance_sheet": _stringify_dates(balance),
        "income_statement": _stringify_dates(income),
        "cash_flow": _stringify_dates(cash_flow),
        "indicator_snapshot": _stringify_dates(snapshot),
    }


def _stringify_dates(values: dict) -> dict:
    return {key: _date_string(value) for key, value in values.items()}


def _date_string(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _is_stale(latest: str | None, earlier: str | None, days: int = 365) -> bool:
    if latest is None or earlier is None:
        return True
    latest_date = date.fromisoformat(latest[:10])
    earlier_date = date.fromisoformat(earlier[:10])
    return (latest_date - earlier_date).days > days
