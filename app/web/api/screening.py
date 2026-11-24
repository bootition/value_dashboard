"""筛选页 API (PRD §12)

提供筛选规则运行、结果保存、导出、加入自选等功能。
"""

# 依据索引（本模块「为什么」的唯一落点；行内只写「依据 S<n>」，不再重复报告号）
# S1  reports/79 方案 A —— 更新窗口内基于快照的运行必须显式标注口径（随结果持久化标注、
#     导出时同样标注）；写锁活跃（自动更新中）时不再 409 禁用。
# S2  reports/131 §9.2 P0-5 —— 技术三列：阈值/字段集的唯一真源在 asset_health；
#     最新期覆盖率 < 95% 的字段不暴露（进 unavailable）。
# S3  reports/122 §2.3 J9 / §2.6 J5 —— 字段级遮蔽：显式传入的字段要与 /indicators 同源；
#     废弃字段 → 替代字段映射（dividend_yield）；两条过滤规则同源。
# S4  reports/68 P3 / §6 —— 国债基准与股息率利差（P3，含曲线对齐溯源）；P4 历史研究统计字段（§6）。
# S5  reports/73 —— P3-2 / P3-9 修复：利差列附加曲线对齐溯源。
# S6  reports/119 P0 —— 扩展指标域动态放行阈值：EXTENDED_SCREENING_READY 只是 2026-09-19 的
#     静态清单，不能当动态覆盖率依据；最新报告期覆盖率低于阈值的字段不暴露。
# S7  reports/124 —— 两列存储量纲不同（小数 vs 百分数），不可混用。
# S8  reports/80 簇 A/B/C —— 单位口径统一：这两列不是「算错」，也不是「废弃」，别让提示误导用户。
# S9  reports/76 P3-4 —— 保存时即校验字段名（与 engine.run 运行时校验同口径）。


from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.core import watchlist_ops as wo
from app.core.blacklist import blacklisted_code_set, filter_rows
from app.core.data_quality import (
    SCREENING_READINESS_CACHE_KEY,
    ensure_screening_readiness_refresh,
    load_screening_readiness_entry,
    screening_readiness_cache_key,
    store_screening_readiness_cache,
)

# 技术三列阈值/字段集的唯一真源在 asset_health（依据 S2 P0-5）。
# 这里只允许 import，禁止再定义第二份常量：曾有 `_TECHNICAL_MIN_COVERAGE_PCT = 95.0`
# 硬编码，运维把 config 调到 98 后体检说"不可信"而筛选仍可选中。
from app.core.quality.asset_health import (
    TECHNICAL_COVERAGE_FIELDS,
    technical_coverage_threshold,
)
from app.core.storage import read_cache, screening_snapshot
from app.core.storage.duckdb_store import DuckDBReadLockedError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screening", tags=["screening"])
MAX_RULE_JSON_BYTES = 100_000
# P1-5修复: 未保存 run 的生命周期下限; 过期清理只能删除早于该 TTL 的 run
SCREENING_RUN_TTL_HOURS = 24


def _csv_cell(value: Any) -> Any:
    # C11修复(报告41): 公式注入防护覆盖前导空白变体（如 " =cmd()"），
    # 同时保留原始首字符制表符/回车判定（"\tcmd"）——二者任一命中即加引号前缀。
    if isinstance(value, str):
        if value[:1] in {"=", "+", "-", "@", "\t", "\r"}:
            return "'" + value
        if value.lstrip()[:1] in {"=", "+", "-", "@"}:
            return "'" + value
    return value


# F4修复(第七轮红队): 网页与 CLI 导出共用同一 CSV 组装逻辑，防止截断标注漂移
_CSV_META_COLUMNS = [
    "_data_date", "_rule_id", "_rule_version", "_locked_indicators",
    "_strict_only", "_field_provenance",
]


def _csv_export_header(
    columns: list[str],
    truncated: bool,
    auto_update: bool = False,
    column_labels: dict[str, str] | None = None,
) -> list[str]:
    labels = column_labels or {}
    # 有中文标签就用中文（PRD §12.1）；缺标签时保留原始列名，绝不静默丢掉列。
    header = [labels.get(column, column) for column in columns] + _CSV_META_COLUMNS
    if truncated:
        # P1-C: 截断结果必须在导出的 CSV 中显式标注，禁止静默丢尾
        header.append("_truncated")
    if auto_update:
        # 依据 S1 方案 A：更新窗口内基于快照的运行必须显式标注口径
        header.append("_data_as_of")
        header.append("_auto_update_in_progress")
    return header


def _csv_export_row(
    columns: list[str],
    row: dict[str, Any],
    data_date: str,
    rule_id: int,
    rule_version: int,
    locked_indicators: str,
    strict_only: Any,
    provenance: dict[str, Any],
    truncated: bool,
    auto_update: bool = False,
    data_as_of: str | None = None,
) -> list[Any]:
    line = [_csv_cell(row.get(col, "")) for col in columns]
    line.append(data_date)
    line.append(rule_id)
    line.append(rule_version)
    line.append(locked_indicators)
    line.append(strict_only)
    line.append(json.dumps(provenance, ensure_ascii=False, sort_keys=True, default=str))
    if truncated:
        line.append(True)
    if auto_update:
        line.append(data_as_of or "")
        line.append(True)
    return line



def _current_indicator_trust(request: Request) -> dict:
    """Read the server-authoritative trust decision for snapshot values.

    与详情页/自选同源：`data_quality.indicator_trust(read_warning_codes(...))`。
    延迟导入保留测试通过 `app.core.data_quality.read_warning_codes` 注入
    告警码的能力（单测不跑全量质量构建）。
    """
    from app.core.data_quality import indicator_trust, read_warning_codes

    return indicator_trust(
        read_warning_codes(request.app.state.duck, request.app.state.sqlite)
    )


def _indicator_trust_or_empty(request: Request) -> dict:
    """展示路径专用：可信度读取失败时按"无遮蔽"降级，绝不让指标列表 500。"""
    try:
        return _current_indicator_trust(request)
    except Exception:  # noqa: BLE001 - 遮蔽信息拿不到就不遮，绝不阻断列表
        logger.warning("读取指标可信度失败（本次不遮蔽）", exc_info=True)
        return {
            "warning_codes": [],
            "untrusted_all": False,
            "untrusted_fields": [],
            "masked_fields": [],
        }


def _require_trusted_snapshot(request: Request) -> dict:
    """Reject screening when every snapshot-derived value is untrusted.

    判定与详情页"整页指标置 null"完全同源：两者都从 `indicator_trust()` 出发，
    再调用 `data_quality.screenability_block_reason()` 得到唯一原因码，web 层
    不再各写一遍 `trust["untrusted_all"]`。

    `masked_fields`（字段级遮蔽）不在这里阻断：它是否命中取决于规则是否引用
    该字段，由筛选引擎的 `MaskedFieldError` 负责（维持现状）。
    """
    from app.core.data_quality import screenability_block_reason

    trust = _current_indicator_trust(request)
    reason = screenability_block_reason(trust)
    if reason is None:
        return trust
    # 没有任何 indicator_snapshot 行时不存在"不可信的快照"：这是基础数据未就绪
    # （首次部署/空库/快照缺失），交给既有 readiness 门禁返回更具体的
    # minimum_data_not_ready。有快照但被阻断时才是本门禁的职责。
    try:
        has_snapshot = bool(
            request.app.state.duck.read_query(
                "SELECT 1 AS present FROM indicator_snapshot LIMIT 1"
            )
        )
    except Exception:  # noqa: BLE001 - schema 损坏时同样交给 readiness 报具体原因
        has_snapshot = False
    if not has_snapshot:
        return trust
    raise HTTPException(status_code=409, detail={
        "reason": reason,
        "reason_code": reason,
        "message": (
            "当前指标快照整体不可信（命中数据质量阻断告警），筛选已拒绝执行；"
            "请先修复数据质量告警后重试"
        ),
        "warning_codes": list(trust.get("warning_codes") or []),
    })


def _freshness_payload(entry: dict | None, *, source: str) -> dict:
    """Expose readiness-cache freshness to the caller (缺陷 B).

    以前门禁返回体只有 ``{"lock_active": False, "data_as_of": None}``：调用方
    无法区分"刚算出来的就绪"和"三天前那份过期决策"。现在随门禁/结果一起
    返回 ``readiness_source``（cache/stale_cache/computed）、``readiness_cached_at``、
    ``readiness_stale``、``readiness_age_seconds``。
    新增字段对前端是纯向后兼容的（旧客户端只读 lock_active/data_as_of）。

    2026-11-19 追加 `readiness_degraded`：指纹对超大表会退回 `count-only`（只看行数、
    不看内容），此时"就绪"判断的**可信度下降**。降级以前只写日志，调用方看不到 ——
    那本身就是"假绿"的另一种形态（看起来正常，其实守卫变弱了）。现在随响应透出。
    """
    entry = entry or {}
    return {
        "readiness_source": source,
        "readiness_cached_at": entry.get("cached_at"),
        "readiness_stale": bool(entry.get("stale", False)),
        "readiness_age_seconds": entry.get("age_seconds"),
        "readiness_degraded": entry.get("fingerprint_degraded"),
    }


def _require_current_screenability(request: Request) -> dict:
    """Enforce the current database gate for screen-derived durable output.

    2026-11-14 统一口径：**快照整体不可信时任何筛选都必须拒绝执行**。
    有指标快照时该判定先于写窗口/readiness 分支——否则同一份不可信数据
    会被更早返回的 `minimum_data_not_ready` 掩盖，前端拿不到
    `snapshot_values_untrusted`，详情页全空与筛选页出排名继续分裂；
    完全没有 indicator_snapshot 行时不存在"不可信的快照"，仍由下方
    readiness 分支给出更具体的 minimum_data_not_ready。

    依据 S1 方案 A（用户决策）：写锁活跃（自动更新中）时不再 409 禁用——
    引擎只读原子替换的 indicator_snapshot 与财务表（不直读 raw 价格），
    快照口径下筛选结果内部一致；放行并返回 data_as_of（快照价格日）供
    标注。快照完全缺失时仍 409 兜底。

    Returns: {"lock_active": bool, "data_as_of": str | None,
              "readiness_source": str, "readiness_cached_at": str | None,
              "readiness_stale": bool, "readiness_age_seconds": float | None}
    """
    # 数据可信度门禁（唯一实现）：有快照且 untrusted_all=True →
    # 409 snapshot_values_untrusted。详情页用同一判定把全部指标置 null，
    # 两页结论必须一致。
    _require_trusted_snapshot(request)

    from app.core.storage.update_lock import any_write_lock_active

    try:
        # 2026-08-14 红队 P2-1：写窗口判定统一（update 锁 OR duckdb 写锁）
        lock_active = any_write_lock_active(request.app.state.duck.db_path)
    except Exception:
        lock_active = False
    if lock_active:
        rows = request.app.state.duck.read_query(
            "SELECT COUNT(*) AS c, MAX(latest_price_date) AS d FROM indicator_snapshot"
        )
        count = rows[0]["c"] if rows else 0
        data_as_of = (
            str(rows[0]["d"])[:10]
            if rows and rows[0].get("d") is not None
            else None
        )
        if count == 0:
            raise HTTPException(status_code=409, detail={
                "reason_code": "minimum_data_not_ready",
                "message": "基础数据尚未就绪（无可用指标快照）",
            })
        return {
            "lock_active": True,
            "data_as_of": data_as_of,
            "readiness_source": "lock_window",
            "readiness_cached_at": None,
            "readiness_stale": False,
            "readiness_age_seconds": None,
        }

    from app.core.data_quality import (
        SCREENING_PREWARM_WAIT_SECONDS,
        screening_readiness,
        screening_readiness_prewarm_active,
        wait_for_screening_readiness_prewarm,
    )

    cache_key = screening_readiness_cache_key(request.app.state.duck, request.app.state.sqlite)
    if cache_key:
        entry = load_screening_readiness_entry(
            request.app.state.sqlite, cache_key,
        )
        freshness = _freshness_payload(entry, source="cache")
        if entry is None:
            # 同指纹的过期决策采用 stale-while-revalidate：请求立即返回，
            # 后台单飞刷新，避免每 10 分钟让用户卡一次 20s+ 全库核对。
            # 缺陷 B（2026-11-18）：stale 有硬上限（2×TTL），超过
            # `load_screening_readiness_entry` 直接返回 None → 落到下面的
            # 同步重算；宁可这一次慢，也不对外假绿。
            entry = load_screening_readiness_entry(
                request.app.state.sqlite, cache_key, allow_stale=True,
            )
            freshness = _freshness_payload(entry, source="stale_cache")
            if entry is not None:
                ensure_screening_readiness_refresh(
                    request.app.state.duck, request.app.state.sqlite, cache_key,
                )
        if (
            entry is None
            and screening_readiness_prewarm_active()
            # 2026-11-21（用户点名的第 2 件事）：启动预热**正在算同一份**全库体检。
            # 这里再同步重算一遍 = 同一份扫描跑两次（副本实测：并发 59.5s vs 等
            # 预热 32s，且 CPU/磁盘互相拖慢）。等它落盘再读一次；预热失败/跳过
            # 时事件同样会被 set（终态），于是自然退回下面的冷算路径 —— 既不会
            # 返回过期结论，也不会无限等。
            and wait_for_screening_readiness_prewarm(SCREENING_PREWARM_WAIT_SECONDS)
        ):
            entry = load_screening_readiness_entry(
                request.app.state.sqlite, cache_key, allow_stale=True,
            )
            freshness = _freshness_payload(entry, source="cache")
        if entry is not None:
            cached = entry.get("decision") or {}
            request.app.state.startup_readiness = cached.get("readiness") or {}
            # 2026-11-20（reports/150）：`ready` 现在只由 **block 级**告警决定，
            # 所以这里的 409 与「数据有瑕疵」不再等价。disclose 级（如
            # TRADING_CALENDAR_INCOMPLETE / SNAPSHOT_STALE）走下面的 200，
            # 由 `disclosed_warnings` 如实披露（前端显示成提示而不是错误）。
            if not cached.get("ready"):
                raise HTTPException(status_code=409, detail={
                    "reason_code": (
                        "minimum_data_not_ready"
                        if not (cached.get("readiness") or {}).get("ready")
                        else "screening_data_quality_not_ready"
                    ),
                    "readiness": cached.get("readiness") or {},
                    "warning_codes": cached.get("warning_codes") or [],
                    "disclosed_warnings": cached.get("disclosed_warnings") or [],
                    **freshness,
                })
            # 缓存的是 screening_readiness 决策，不是 gate 形状；调用方
            # run_screening 需要 lock_active/data_as_of 两个键。
            return {
                "lock_active": False,
                "data_as_of": None,
                "disclosed_warnings": cached.get("disclosed_warnings") or [],
                "screening_warnings": cached.get("warnings") or [],
                **freshness,
            }
    decision = screening_readiness(request.app.state.duck, request.app.state.sqlite)
    if cache_key:
        store_screening_readiness_cache(request.app.state.sqlite, cache_key, decision)
    request.app.state.startup_readiness = decision["readiness"]
    # 2026-11-20（reports/150）：只有 block 级才 409；reason_code 映射不变
    # （`minimum_data_not_ready` 优先，其余为 `screening_data_quality_not_ready`），
    # 前端 `api-error.ts` 的既有映射继续有效。
    if not decision["ready"]:
        reason_code = (
            "minimum_data_not_ready"
            if not decision["readiness"]["ready"]
            else "screening_data_quality_not_ready"
        )
        raise HTTPException(status_code=409, detail={
            "reason_code": reason_code,
            "readiness": decision["readiness"],
            "warning_codes": decision["warning_codes"],
            "disclosed_warnings": decision.get("disclosed_warnings") or [],
            "readiness_source": "computed",
            "readiness_cached_at": None,
            "readiness_stale": False,
            "readiness_age_seconds": None,
        })
    return {
        "lock_active": False,
        "data_as_of": None,
        "disclosed_warnings": decision.get("disclosed_warnings") or [],
        "screening_warnings": decision.get("warnings") or [],
        "readiness_source": "computed",
        "readiness_cached_at": None,
        "readiness_stale": False,
        "readiness_age_seconds": None,
    }


class ScreeningRequest(BaseModel):
    """筛选运行请求"""
    rule_id: int
    rule_version: int
    include_st: bool = False
    include_suspended: bool = False
    min_listing_years: int = 1
    strict_only: bool = False
    # 结果列支持 `{field, rank_mode}` 对象（方案 C，2026-11-16）：
    # `["pe_ttm", {"field": "pe_ttm", "rank_mode": "market_rank"}]`
    columns: list[str | dict[str, Any]] | None = None


class SaveResultRequest(BaseModel):
    """保存筛选结果请求"""
    title: str                         # 标题 (必填, PRD SC14)
    note: str | None = None            # 备注 (可选)
    run_id: str
    columns: list[str] | None = None


class ExportCsvRequest(BaseModel):
    """导出 CSV 请求"""
    result_id: int
    # 2026-11-16（reports/148）：CSV 表头必须是中文（PRD §12.1）。
    # 字段名→中文名的解析在前端（`fieldDisplayName` 是唯一实现），
    # 后端不重复实现一份词表，只接受前端传上来的标签映射。
    column_labels: dict[str, str] | None = None


class AddToWatchlistRequest(BaseModel):
    """加入自选列表请求。

    `replace_group=False`（默认）保持历史语义：**累加**（同代码 upsert，其余
    旧行原样保留），既有调用方/CLI/脚本不受影响。
    `replace_group=True` 是用户显式选择的「替换」：先删除该分组里由**其它**
    筛选结果带进来的行，再写入本次结果；`source_result_id IS NULL` 的
    手动自选**永不删除**（用户手工加的股票不是筛选产物）。
    """
    stock_codes: list[str]
    group: str = "default"
    result_id: int | None = None
    replace_group: bool = False


class ScreeningDraftRequest(BaseModel):
    draft: dict[str, Any]
    revision: int = 0


# ─── 更新窗口内的筛选只读回退（2026-11-20，reports/150） ────────────────
#
# 事故：自动更新子进程独占 DuckDB 文件句柄期间，`/api/screening/run` 里的每一次
# `read_query()` 都是 `DuckDBReadLockedError` → 全局 handler 503。用户看到
# `数据正在自动更新中，请稍后刷新`，可上一次跑成功的**同一批条件**明明还在。
#
# 做法与个股详情/黑名单同源（`app/core/storage/read_cache.py`）：
# 键 = 本次运行的**全部语义输入**，值 = 上一次成功执行的完整响应体。写锁落下的
# 那一刻起，只要"同一批条件 + 同一份数据快照"跑成功过，就把那份结果原样回放，
# 并打上 `served_from="cache"` / `stale=True` / `notice`；从来没跑成功过 → 409 人话。
#
# 三条铁律：
# 1. **键必须区分条件**：规则身份 + 规则体摘要 + 基础池 + 列 + strict + 数据快照。
#    少一项就会"换了条件却回放上一次的结果"——那比 503 严重得多（用户会照着错
#    的结论下单）。等价输入必须同键：空规则集 ≡ 无 conditions、columns=None ≡ []。
# 2. **数据快照标识复用筛选就绪指纹**（`data_refresh_state` 的
#    `screening_readiness_cache.fingerprint`），**不用 `content_epoch`**：epoch 在
#    **每个写事务之后**都变，而写锁窗口恰恰是"更新还没提交、指纹不变"的那段时间，
#    用 epoch 会让每一次回放都 miss —— 把要救的场景全打穿。
# 3. **陈旧回放只读不写**：`screening_runs` 的 TTL 清理与 INSERT 都在
#    `_execute_screening` 里（只有真执行路径才走到），回放路径一行都不碰。

#: 无上次结果可回放时的稳定原因码。与 503 的 `duckdb_read_locked` 区分：
#: 这个 409 是"等更新结束再点一次"的等待态，前端 `api-error.ts` 按原因码出文案。
REASON_DATA_UPDATE_IN_PROGRESS = "data_update_in_progress"

#: 无缓存 409 的**人话**。只讲用户能做的事，不出现 `warning_codes` 这类裸字段名，
#: 也不再把用户指去「数据状态」页看一个他一辈子不会看的告警列表。
NO_CACHED_SCREENING_RESULT_MESSAGE = (
    "数据正在自动更新，这台机器上还没有可用的完整数据快照，暂时无法运行筛选，"
    "请稍后重试。更新结束时会自动生成快照，之后任何条件都能照常跑。"
)

#: 快照兜底成功时的原因码（与 `served_from="snapshot"` 配套）。
REASON_SNAPSHOT_SERVED = "screening_snapshot"

#: 随 200 结果下发的披露码：前端据此显示"这是更新前的完整快照"横幅。
SNAPSHOT_DISCLOSED_WARNING = "screening_served_from_snapshot"


def _prune_empty_containers(value: Any) -> Any:
    """递归剪掉空容器/None：`{}`、`[]`、`None` 在筛选口径里都等价于"没写"。

    为什么需要它：键是**语义**指纹而不是请求字面量。`columns: null` 与
    `columns: []` 跑出来是同一份结果，若键不同就会出现"明明有缓存却 409"。
    """
    if isinstance(value, dict):
        pruned: dict[str, Any] = {}
        for key, item in value.items():
            if item is None or item == {} or item == []:
                continue
            pruned[key] = _prune_empty_containers(item)
        return pruned
    if isinstance(value, (list, tuple)):
        return [_prune_empty_containers(item) for item in value]
    return value


def _normalized_rule_body(rule: dict[str, Any] | None) -> dict[str, Any]:
    """规则体的**语义**归一化：空规则集 ≡ 无 conditions，空容器一律剪掉。

    `{"conditions": {"logic": "AND", "rules": []}}`（/rules/save 存下来的空规则）
    与 `{}`（引擎眼里"全市场"）是同一个查询，必须落进同一个键，否则用户"什么都没
    加就点运行"与"删掉全部条件再点运行"会拿到两份不同的缓存。
    """
    body = dict(rule) if isinstance(rule, dict) else {}
    conditions = body.get("conditions")
    rules = conditions.get("rules") if isinstance(conditions, dict) else None
    if not rules:
        # rules 缺失/`[]`/`None` 都是"没有条件"；连 logic 一起丢掉，
        # 否则 `{"logic": "AND"}` 会让空规则集与无 conditions 分叉。
        body.pop("conditions", None)
    return _prune_empty_containers(body)


def _rule_body_digest(
    rule: dict[str, Any] | None,
    locked_indicators: dict[str, Any] | None = None,
) -> str:
    """规则体摘要（含 DSL 锁版本）：条件/排序/列/指标锁任一变化 → 摘要必变。

    `locked_indicators` 一起进摘要，是因为同一份 rule_json 在不同
    `dsl_expressions` 版本下可能被锁到不同指标实现上（口径不同 → 结果不同）。
    """
    payload = {
        "rule": _normalized_rule_body(rule),
        "locked_indicators": _prune_empty_containers(locked_indicators or {}),
    }
    blob = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _canonical_columns(columns: Any) -> tuple[str, ...]:
    """结果列的规范化表示：`None ≡ []`，去重顺序与 `_execute_screening` 同义。

    对象列（`{field, rank_mode}`）按规范化 JSON 串参与比较——列顺序会改变
    响应里 `columns` 的顺序，所以**保留顺序**，只去掉完全重复的项。
    """
    out: list[str] = []
    for item in columns or []:
        if isinstance(item, dict):
            token = json.dumps(
                _prune_empty_containers(item),
                sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
            )
        else:
            token = str(item)
        if token not in out:
            out.append(token)
    return tuple(out)


def _effective_columns(
    req: ScreeningRequest, rule: dict[str, Any] | None,
) -> tuple[str, ...]:
    """本次运行**实际**用的结果列：请求显式传了就用请求的，否则用保存版本的。"""
    if req.columns is not None:
        return _canonical_columns(req.columns)
    if isinstance(rule, dict):
        return _canonical_columns(rule.get("columns"))
    return ()


def _screening_data_fingerprint(sqlite: Any) -> str | None:
    """数据快照标识：复用筛选就绪指纹，绝不在锁窗口里现算。

    指纹由门禁/预热器写进 `data_refresh_state['screening_readiness_cache']`；
    这里只**读**那一行（SQLite，不受 DuckDB 写锁影响）。读不到就返回 `None`
    （键里是"未知快照"，只会降命中率，绝不会误命中另一份快照）。
    """
    if sqlite is None:
        return None
    try:
        rows = sqlite.query(
            "SELECT value FROM data_refresh_state WHERE key = ?",
            [SCREENING_READINESS_CACHE_KEY],
        )
        if not rows:
            return None
        payload = json.loads(rows[0]["value"] or "{}")
    except Exception:  # noqa: BLE001 - 读不到状态行只影响命中率，不能挡住筛选
        return None
    if not isinstance(payload, dict):
        return None
    fingerprint = payload.get("fingerprint")
    return str(fingerprint) if fingerprint else None


def _screening_run_key(
    req: ScreeningRequest,
    *,
    rule: dict[str, Any] | None = None,
    locked_indicators: dict[str, Any] | None = None,
    sqlite: Any = None,
) -> tuple[Any, ...]:
    """`/run` 的结果缓存键：**同一批语义输入 ⟺ 同一个键**。

    组成（缺一项就会串结果）：

    ==================  ====================================================
    规则身份            `rule_id` + `rule_version`
    规则体摘要          conditions/sort/columns + DSL 锁版本的 sha256 前 32 位
    基础池              `include_st` / `include_suspended` / `min_listing_years`
    列                  规范化结果列（`None ≡ []`，对象列按 JSON 规范化）
    strict              `strict_only`（严格口径会改变入选集合）
    数据快照标识        筛选就绪指纹（`None` = 当前没有可信指纹）
    ==================  ====================================================

    第一条 `"screening_run"` 是端点命名空间：`read_cache` 是进程级共享表，
    不加这一项就可能与个股详情/黑名单的键撞上。
    """
    return (
        "screening_run",
        int(req.rule_id),
        int(req.rule_version),
        _rule_body_digest(rule, locked_indicators),
        bool(req.include_st),
        bool(req.include_suspended),
        int(req.min_listing_years),
        _effective_columns(req, rule),
        bool(req.strict_only),
        _screening_data_fingerprint(sqlite),
    )


def _saved_rule_row(request: Request, req: ScreeningRequest) -> dict[str, Any] | None:
    """只读 SQLite 取本次要跑的规则版本（`None` = 这个版本不存在）。

    **不算门禁**：门禁与"规则不存在 400"的先后顺序由 `_execute_screening`
    保证（门禁永远先跑，既有回归锁着这条）。这里只为了在查缓存之前拿到
    规则体——没有规则体就没法算"条件隔离"的键。
    """
    rows = request.app.state.sqlite.query(
        "SELECT rule_json, locked_indicators FROM screening_rules WHERE id = ? AND version = ?",
        [req.rule_id, req.rule_version],
    )
    return rows[0] if rows else None


@router.post("/run")
def run_screening(req: ScreeningRequest, request: Request) -> dict:
    """运行筛选 (PRD §12.2 SC8: 手动运行, 以最新完整快照为准)。

    写锁窗口内的执行顺序（2026-11-20 第二版，reports/151）：

    1. **回放**（`read_cache.serve`）：同一批语义输入曾经成功跑过 → 直接回放，
       最快，且与原行为逐字一致；
    2. **快照**（`_run_screening_from_snapshot`）：回放 miss 时**不再 409**，
       而是把引擎的数据源切到"上一次更新完成后物化的筛选快照库"（独立文件，
       不受主库写锁影响），**照常把这次的条件真跑一遍**。响应带
       `served_from="snapshot"` + `as_of` + `stale=True` + 中文 `notice`；
    3. **409**：连快照都没有/打不开 → 如实说"现在不能跑"，绝不假装成功。

    为什么必须走第 2 条而不是只做回放：回放的键含规则体，只有"完全相同的请求
    曾经成功过"才命中；用户换个条件、或第一次在更新窗口里筛 → 必然 miss
    （reports/151 §1 的日志实证）。
    """
    rule_row = _saved_rule_row(request, req)
    if rule_row is None:
        # 规则不存在：**绝不查缓存**。"空规则体"的键不能替一个不存在的规则担保
        # （删掉规则后重跑会命中旧缓存，把 400 变成 200）。直接走内层：
        # 门禁 → 400，顺序与改造前逐字一致。
        return _execute_screening(req, request, None)

    rule = json.loads(rule_row["rule_json"])
    key = _screening_run_key(
        req,
        rule=rule,
        locked_indicators=json.loads(rule_row["locked_indicators"] or "{}"),
        sqlite=request.app.state.sqlite,
    )
    try:
        payload, meta = read_cache.serve(
            request.app.state.duck, key,
            lambda: _execute_screening(req, request, rule_row),
        )
    except DuckDBReadLockedError as error:
        # 回放 miss + 主库被写锁挡住 → **快照兜底**（不再直接 409）。
        served = _run_screening_from_snapshot(req, request, rule_row, lock_error=error)
        if served is not None:
            return served
        # 连快照都没有 ⇒ 如实报"现在不能跑"，但说人话，
        # 不给用户抛 `warning_codes`/`reason_code` 这种字段名。
        logger.warning("筛选运行被写锁挡住且无快照可兜底: %s", error)
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": REASON_DATA_UPDATE_IN_PROGRESS,
                "message": NO_CACHED_SCREENING_RESULT_MESSAGE,
            },
        ) from error
    # `served_from/as_of/stale/reason_code/notice` 与搜索/详情同一套字段；
    # live 时 stale=False、notice=None，cache 时带更新提示。
    return {**payload, **meta}


def _screening_snapshot_gate(store: Any, info: dict[str, Any]) -> dict[str, Any]:
    """快照路径的门禁结果：只用**快照库**能回答的事，逐项如实标注。

    与 `_require_current_screenability()` 的区别是**不假装**做了实时判定：
    写锁窗口里主库读不到，trust/readiness 不可能现算，所以
    `readiness_source="screening_snapshot"`、`readiness_stale=True`，
    并把 `screening_served_from_snapshot` 放进 `disclosed_warnings`
    让前端显示成提示横幅（不是错误）。
    """
    data_as_of: str | None = None
    try:
        rows = store.read_query(
            "SELECT MAX(latest_price_date) AS d FROM indicator_snapshot"
        )
        if rows and rows[0].get("d") is not None:
            data_as_of = str(rows[0]["d"])[:10]
    except Exception:  # noqa: BLE001 - 快照里没有该列也不影响出结果
        logger.debug("读取快照行情日期失败", exc_info=True)
    age = screening_snapshot.snapshot_age_seconds(info)
    return {
        "lock_active": True,
        "data_as_of": data_as_of,
        "disclosed_warnings": [SNAPSHOT_DISCLOSED_WARNING],
        "screening_warnings": [],
        "readiness_source": screening_snapshot.SERVED_FROM_SNAPSHOT,
        "readiness_cached_at": info.get("built_at"),
        "readiness_stale": True,
        "readiness_age_seconds": age,
    }


def _run_screening_from_snapshot(
    req: ScreeningRequest,
    request: Request,
    rule_row: dict[str, Any],
    *,
    lock_error: Exception,
) -> dict | None:
    """回放 miss 时的兜底：把引擎数据源切到筛选快照库，**真跑**这次的条件。

    返回可直接下发的响应体（已带陈旧标注）；快照缺失/损坏/跑不通 → `None`，
    由调用方落回 409。**绝不**用"空结果集"冒充成功（那是一个错误的事实陈述）。
    """
    store = screening_snapshot.open_snapshot(request.app.state.duck)
    if store is None:
        logger.warning(
            "筛选快照不可用（未生成或已损坏），无法兜底: %s",
            screening_snapshot.snapshot_path(request.app.state.duck),
        )
        return None
    logger.info(
        "筛选主库被写锁挡住（%s），改用筛选快照 %s（as_of=%s）真跑本次条件",
        lock_error, store.db_path, store.as_of,
    )
    try:
        payload = _execute_screening(
            req, request, rule_row, store=store, snapshot_info=store.info,
        )
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 - 快照跑不通 ⇒ 409，不当成功
        logger.warning("从筛选快照执行失败，落回 409: %s", error, exc_info=True)
        return None
    finally:
        store.close()
    return {
        **payload,
        "served_from": screening_snapshot.SERVED_FROM_SNAPSHOT,
        "as_of": store.as_of,
        "stale": True,
        "reason_code": REASON_SNAPSHOT_SERVED,
        "notice": screening_snapshot.snapshot_notice(store.info),
    }


def _execute_screening(
    req: ScreeningRequest,
    request: Request,
    rule_row: dict[str, Any] | None,
    *,
    store: Any = None,
    snapshot_info: dict[str, Any] | None = None,
) -> dict:
    """门禁 → 规则 → 引擎 → 落库。返回可直接回放的响应体（不含陈旧标记）。

    ⚠️ 顺序是被测试锁死的：**门禁先跑**（409 `minimum_data_not_ready` 等），
    之后才是"规则不存在 400"。把 400 提到门禁前面会让"更新中且没数据"变成
    "规则不存在"，用户被指去改规则——错的方向。

    Args:
        store: 引擎的数据源。`None` = 主库（默认）。写锁窗口里传
            `SnapshotStore` —— **只换连接，SQL 一个字不改**。
        snapshot_info: 快照 meta（`built_at`/`row_counts`）。非 `None` 表示
            本次是快照口径：门禁改用 `_screening_snapshot_gate()`，
            字段遮蔽改由引擎在快照库上现算（同一条实现，不抄第二份）。
    """
    from app.core.screening.engine import ScreeningEngine

    data_store = store if store is not None else request.app.state.duck
    gate = (
        _screening_snapshot_gate(data_store, snapshot_info)
        if snapshot_info is not None
        else _require_current_screenability(request)
    )

    if not rule_row:
        raise HTTPException(status_code=400, detail="saved rule version not found")
    rule = json.loads(rule_row["rule_json"])
    # 2026-08-31：结果列可在运行前自由选择；本次运行覆盖保存版本的 columns，
    # 并随 screening_runs.columns_json 持久化，保证屏幕/保存/导出同口径。
    if req.columns is not None:
        rule = dict(rule)
        rule["columns"] = list(dict.fromkeys(req.columns))
    locked_indicators = json.loads(rule_row["locked_indicators"] or "{}")
    from app.core.screening.engine import MaskedFieldError

    engine = ScreeningEngine(duck=data_store, sqlite=request.app.state.sqlite)
    try:
        result = engine.run(
            rule=rule,
            include_st=req.include_st,
            include_suspended=req.include_suspended,
            min_listing_years=req.min_listing_years,
            strict_only=req.strict_only,
            locked_indicators=locked_indicators,
            # 字段级遮蔽（依据 S3 §2.3 J9）：显式传入与 /indicators 同源的
            # 不可信字段清单，命中即 409 —— 绝不静默返回空结果集。
            # 快照口径下传 None：让引擎在**快照库**上用同一条探针
            # （`masked_fields_for_database`）现算，不拿主库的清单套快照的数据。
            masked_fields=(
                None if snapshot_info is not None else _masked_indicator_fields(request)
            ),
        )
    except MaskedFieldError as error:
        raise HTTPException(status_code=409, detail=error.as_detail()) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    _attach_result_report_dates(data_store, result["results"])

    # P1-5修复: 惰性过期清理由单条原子 DELETE 完成, 只回收早于 TTL 的 run,
    # 不再删除其他页面尚在有效期内未保存的 run; 正常消费仍由 /save 负责删除。
    # 2026-11-20（reports/150）：清理**必须挪到引擎成功之后**。它原来在"开跑之前"，
    # 于是写锁窗口里的**陈旧回放也会顺手删掉过期的 screening_runs 行** ——
    # 回放是纯读语义，对库必须零副作用（连"顺便清理"都不行：那条旧 run 可能
    # 正是用户马上要在前端点"保存结果"的那一份）。真执行路径才走到这里，
    # 回放路径在引擎读库那一步就被 `read_cache` 接走了。
    request.app.state.sqlite.execute(
        "DELETE FROM screening_runs WHERE created_at < datetime('now', ?)",
        [f"-{SCREENING_RUN_TTL_HOURS} hours"],
    )

    run_id = str(uuid.uuid4())
    columns = rule.get("columns", [])
    base_pool = {
        "include_st": req.include_st,
        "include_suspended": req.include_suspended,
        "min_listing_years": req.min_listing_years,
    }
    # 依据 S1 方案 A: 更新窗口内的快照口径运行必须随结果持久化标注
    auto_update = bool(gate["lock_active"])
    data_as_of = gate.get("data_as_of")
    confidence_summary = {
        "total": result["total"], "strict_only": req.strict_only,
        "locked_indicators": locked_indicators,
        # P1-C: 截断状态随结果持久化，导出 CSV 时标注
        "truncated": bool(result.get("truncated")),
        "auto_update_in_progress": auto_update,
        "data_as_of": data_as_of,
    }
    request.app.state.sqlite.execute(
        """INSERT INTO screening_runs
           (run_id, rule_id, rule_version, result_json, columns_json, sort_json, data_date,
            base_pool_config, strict_only, confidence_summary)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            run_id, req.rule_id, req.rule_version,
            json.dumps(result["results"], ensure_ascii=False, default=str),
            json.dumps(columns, ensure_ascii=False), json.dumps(rule.get("sort", []), ensure_ascii=False),
            result["data_date"], json.dumps(base_pool, ensure_ascii=False), req.strict_only,
            json.dumps(confidence_summary, ensure_ascii=False),
        ],
    )
    result["run_id"] = run_id
    result["auto_update_in_progress"] = auto_update
    result["data_as_of"] = data_as_of
    # 缺陷 B：把门禁用的就绪决策新鲜度一并返回（新增字段，向后兼容）。
    # 2026-11-20（reports/150）：disclose 级告警随 200 结果一起返回，
    # 前端显示成提示（不是错误），用户能自己判断"这条结论有多新/多脏"。
    result["disclosed_warnings"] = gate.get("disclosed_warnings") or []
    result["screening_warnings"] = gate.get("screening_warnings") or []
    result["readiness_source"] = gate.get("readiness_source")
    result["readiness_cached_at"] = gate.get("readiness_cached_at")
    result["readiness_stale"] = bool(gate.get("readiness_stale", False))
    result["readiness_age_seconds"] = gate.get("readiness_age_seconds")
    return {
        **result,
        "results": [
            {key: value for key, value in row.items() if key != "_report_date"}
            for row in result["results"]
        ],
    }


@router.get("/draft")
def get_draft(request: Request) -> dict[str, Any]:
    rows = request.app.state.sqlite.query(
        "SELECT draft_json, revision, updated_at FROM screening_drafts WHERE id = 1"
    )
    if not rows:
        return {"draft": None}
    return {"draft": json.loads(rows[0]["draft_json"]), "revision": rows[0]["revision"], "updated_at": rows[0]["updated_at"]}


@router.put("/draft")
def save_draft(req: ScreeningDraftRequest, request: Request) -> dict[str, int | str]:
    # C7修复(报告41): 草稿 PUT 大小上限，防止超限负载（413）
    draft_bytes = len(json.dumps(req.draft, ensure_ascii=False).encode("utf-8"))
    if draft_bytes > MAX_RULE_JSON_BYTES:
        raise HTTPException(status_code=413, detail="draft payload is too large")
    next_revision = req.revision + 1
    draft_json = json.dumps(req.draft, ensure_ascii=False)
    with request.app.state.sqlite.transaction() as conn:
        if req.revision == 0:
            # 首次创建：INSERT 成功即获得 revision=1；若与并发请求竞争，
            # rowcount=0 会落入下方条件 UPDATE 并因 revision 已变化而 409。
            inserted = conn.execute(
                """INSERT INTO screening_drafts (id, draft_json, revision, updated_at)
                   VALUES (1, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(id) DO NOTHING""",
                [draft_json, next_revision],
            ).rowcount
            if inserted == 1:
                return {"status": "ok", "revision": next_revision}
        # 乐观锁原子条件更新：读取比较与写入合并为同一条 UPDATE，
        # 并发 PUT 携带同一 revision 时 SQLite 串行化后只有一个 rowcount=1。
        cursor = conn.execute(
            """UPDATE screening_drafts
               SET draft_json = ?, revision = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = 1 AND revision = ?""",
            [draft_json, next_revision, req.revision],
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=409, detail="draft revision conflict; reload and retry")
    return {"status": "ok", "revision": next_revision}


@router.post("/save")
def save_result(req: SaveResultRequest, request: Request) -> dict:
    """保存筛选结果 (PRD §12.5 SC14-15)"""
    _require_current_screenability(request)
    sqlite = request.app.state.sqlite

    if not req.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    with sqlite.transaction() as conn:
        run = conn.execute(
            "SELECT * FROM screening_runs WHERE run_id = ?", [req.run_id]
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=400, detail="server screening run not found")
        # 2026-11-25：规则可以被删除，而运行日志行会**保留**（悬空 rule_id）。
        # 拿这种"规则已被删"的运行记录保存结果会撞 screening_results.rule_id 的
        # 外键 → 500。这里提前拦住并让用户重新跑一次，别再抛 IntegrityError。
        live_rule = conn.execute(
            "SELECT 1 FROM screening_rules WHERE id = ?", [run["rule_id"]]
        ).fetchone()
        if live_rule is None:
            raise HTTPException(status_code=400, detail="run rule was deleted")
        cursor = conn.execute(
            """INSERT INTO screening_results
                (title, note, rule_id, rule_version, data_date,
                 result_json, columns_json, sort_json, confidence_summary, base_pool_config)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                req.title,
                req.note,
                run["rule_id"], run["rule_version"],
                run["data_date"] or datetime.now(UTC).isoformat(),
                run["result_json"],
                json.dumps(req.columns, ensure_ascii=False) if req.columns is not None else run["columns_json"],
                run["sort_json"], run["confidence_summary"], run["base_pool_config"],
            ],
        )
        result_id = cursor.lastrowid  # P1-34修复: int类型，与INTEGER PK一致
        conn.execute("DELETE FROM screening_runs WHERE run_id = ?", [req.run_id])

    return {"status": "ok", "result_id": result_id}
    # P1-34修复: result_id保持int类型（与screening_results.id INTEGER PK一致）


@router.get("/results")
def list_saved_results(request: Request, limit: int = Query(20, ge=1, le=500)) -> dict:
    """列出已保存的筛选结果"""
    sqlite = request.app.state.sqlite
    rows = sqlite.query(
        "SELECT id, title, note, data_date, created_at "
        "FROM screening_results ORDER BY created_at DESC LIMIT ?",
        [limit],
    )
    return {"results": rows, "count": len(rows)}


@router.get("/results/{result_id}")
def get_saved_result(result_id: str, request: Request) -> dict:
    """获取已保存的筛选结果详情"""
    sqlite = request.app.state.sqlite
    rows = sqlite.query(
        "SELECT * FROM screening_results WHERE id = ?",
        [result_id],
    )
    if not rows:
        raise HTTPException(status_code=404, detail="saved result not found")
    row = rows[0]
    # 解析 JSON 字段
    row["result_json"] = json.loads(row["result_json"]) if row.get("result_json") else []
    # 2026-11-22：已保存结果是历史 run 的冻结快照（result_json 是一整段 JSON），
    # 无法在 SQL 层过滤，只能读出来后在内存里剔掉黑名单股票。否则「加入黑名单后
    # 不再出现」会被历史结果绕过（依据 app/core/blacklist.py 模块 docstring）。
    row["result_json"] = filter_rows(
        row["result_json"], blacklisted_code_set(sqlite)
    )
    row["columns_json"] = json.loads(row["columns_json"]) if row.get("columns_json") else []
    row["sort_json"] = json.loads(row["sort_json"]) if row.get("sort_json") else []
    return row


@router.post("/export_csv")
def export_csv(req: ExportCsvRequest, request: Request) -> dict:
    """导出 CSV (PRD §12.5 SC16: 含数据日期/规则版本/指标版本/置信度/来源)"""
    _require_current_screenability(request)
    sqlite = request.app.state.sqlite
    saved = sqlite.query("SELECT * FROM screening_results WHERE id = ?", [req.result_id])
    if not saved:
        raise HTTPException(status_code=404, detail="saved result not found")
    record = saved[0]
    results = json.loads(record["result_json"])
    # 与 get_saved_result 同口径：导出也不能把黑名单股票漏出去。
    results = filter_rows(results, blacklisted_code_set(sqlite))
    columns = json.loads(record["columns_json"])
    data_date = record["data_date"]
    rule = sqlite.query(
        "SELECT locked_indicators FROM screening_rules WHERE id = ? AND version = ?",
        [record["rule_id"], record["rule_version"]],
    )
    if not rule:
        # 2026-11-25：`PUT /rules/{id}` 是**原地升版本**，于是历史结果里记的
        # `rule_version` 不会再对应规则行上的版本号（同一 id 仍在，规则改名/换
        # 条件了）。这不是"溯源缺失"，不能拿 400 挡住用户导出自己的历史结果：
        # CSV 表头照旧打印结果自己的 run-time 版本号，只有指标锁降级用当前行。
        rule = sqlite.query(
            "SELECT locked_indicators FROM screening_rules WHERE id = ?",
            [record["rule_id"]],
        )
    if not rule:
        raise HTTPException(status_code=400, detail="saved result rule provenance is missing")
    summary = json.loads(record.get("confidence_summary") or "{}")
    truncated = bool(summary.get("truncated"))
    # 依据 S1 方案 A: 更新窗口内保存的结果导出时显式标注快照口径
    auto_update = bool(summary.get("auto_update_in_progress"))
    data_as_of = summary.get("data_as_of")

    if not results:
        raise HTTPException(status_code=400, detail="no results to export")
    if len(results) > 10000:
        raise HTTPException(status_code=400, detail="too many results to export (max 10000)")

    output = io.StringIO()
    writer = csv.writer(output)

    # 表头: 列名 + 可重现的规则/指标版本和严格模式信息
    try:
        provenance = _field_provenance(request.app.state.duck, request.app.state.sqlite, results, columns)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    writer.writerow(_csv_export_header(columns, truncated, auto_update, req.column_labels))

    # 数据行
    for index, row in enumerate(results):
        writer.writerow(_csv_export_row(
            columns, row, data_date, record["rule_id"], record["rule_version"],
            rule[0]["locked_indicators"], summary.get("strict_only", False),
            provenance[index], truncated, auto_update, data_as_of,
        ))

    csv_content = output.getvalue()
    return {"csv": csv_content, "rows": len(results)}


@router.post("/add_to_watchlist")
def add_to_watchlist(req: AddToWatchlistRequest, request: Request) -> dict:
    """筛选结果入自选（批量，先校验可筛选性）。

    与 `watchlist.add_to_watchlist`（`POST /api/watchlist/add`，用户手动加单只）
    **不是重复实现**：本端点先过 `_require_current_screenability`，且只接受
    `screening_results` 里已保存的代码（PRD §12.5 SC17）。
    """
    _require_current_screenability(request)
    sqlite = request.app.state.sqlite
    stock_codes = req.stock_codes
    group_name = req.group
    result_id = req.result_id

    if len(stock_codes) > 10000:
        raise HTTPException(status_code=400, detail="too many stocks (max 10000)")

    added = 0
    removed = 0
    inserted = 0
    updated = 0
    manual_rows_preserved = 0
    with sqlite.transaction() as conn:
        result = conn.execute(
            "SELECT rule_id, result_json FROM screening_results WHERE id = ?",
            [result_id],
        ).fetchone()
        if result is None:
            raise HTTPException(status_code=400, detail="a saved screening result is required")
        saved_codes = {row.get("stock_code") for row in json.loads(result["result_json"])}
        # 黑名单股票不许进自选（前端已看不到了，这里是绕过前端的兜底）
        blocked_codes = blacklisted_code_set(sqlite)
        # 先去重：同一请求里重复代码只 upsert 一次，added 按唯一代码数计。
        unique_codes = list(dict.fromkeys(stock_codes))
        requested_codes = set(unique_codes)
        if not requested_codes <= saved_codes:
            raise HTTPException(status_code=400, detail="stock codes must come from the saved result")
        if requested_codes & blocked_codes:
            raise HTTPException(status_code=400, detail="blacklisted stock codes cannot be added")
        # 2026-11-22：规则现在可以被删除（只保留运行历史），`screening_results.rule_id`
        # 因此可能出现悬空值。`watchlist.source_rule_id` 带外键，照抄悬空 id 会
        # `IntegrityError` → 500。这里先查一次：规则还在就照抄，不在就写 NULL
        # （`source_result_id` 仍能溯源到结果本身），并如实告诉调用方溯源降级了。
        rule_still_exists = conn.execute(
            "SELECT 1 FROM screening_rules WHERE id = ?", [result["rule_id"]],
        ).fetchone() is not None
        source_rule_id = result["rule_id"] if rule_still_exists else None
        if req.replace_group:
            # 用户显式选「替换」：删掉该分组里**由历史筛选结果带进来**的行。
            # 只按 `source_result_id` 判定，绝不按 `source_rule_id` 一刀切：
            # `source_result_id IS NULL` = 用户手动加的自选，任何模式下都保留
            # （本次结果恰好也命中的手动行会走下面的 upsert 补上溯源，但不会被删）。
            removed = conn.execute(
                "DELETE FROM watchlist WHERE group_name = ?"
                " AND source_result_id IS NOT NULL AND source_result_id != ?",
                [group_name, result_id],
            ).rowcount
        existing_codes: set[str] = set()
        if unique_codes:
            placeholders = ", ".join("?" for _ in unique_codes)
            existing_codes = {
                row["stock_code"]
                for row in conn.execute(
                    f"SELECT stock_code FROM watchlist WHERE group_name = ?"
                    f" AND stock_code IN ({placeholders})",
                    [group_name, *unique_codes],
                ).fetchall()
            }
        conn.executemany(
            """INSERT INTO watchlist
               (stock_code, group_name, source_rule_id, source_result_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(stock_code, group_name) DO UPDATE SET
                 source_rule_id=excluded.source_rule_id,
                 source_result_id=excluded.source_result_id,
                 added_at=CURRENT_TIMESTAMP""",
            [
                (code, group_name, source_rule_id, result_id)
                for code in unique_codes
            ],
        )
        added = len(unique_codes)
        inserted = len(requested_codes - existing_codes)
        updated = len(requested_codes & existing_codes)
        manual_rows_preserved = conn.execute(
            "SELECT COUNT(*) AS c FROM watchlist"
            " WHERE group_name = ? AND source_result_id IS NULL",
            [group_name],
        ).fetchone()["c"]
        # 审计（v22）：这是自选行数**最大的**来源（用户现场那个 591 行的分组就是这条
        # 路径累加出来的），漏了它，「这 591 行哪来的」就仍然只能靠猜。
        # 与数据写同事务：上面任何一条 400 抛出去都会连这条日志一起回滚。
        wo.record(
            sqlite,
            op=wo.OP_SCREENING_SAVE,
            stock_codes=unique_codes,
            to_group=group_name,
            detail={
                "result_id": result_id,
                "source_rule_id": source_rule_id,
                "added": added,
                "removed": removed,
                "inserted": inserted,
                "updated": updated,
                "manual_rows_preserved": manual_rows_preserved,
                "replace_group": bool(req.replace_group),
                "rule_provenance": "rule_deleted" if source_rule_id is None else "ok",
            },
            actor=wo.actor_from_headers(request.headers),
            conn=conn,
        )

    return {
        "status": "ok",
        "added": added,
        # 替换模式下删掉的「历史筛选结果行」条数；默认累加模式恒为 0。
        "removed": removed,
        # 本次真正新插入 / 覆盖更新的行数（added = inserted + updated）。
        "inserted": inserted,
        "updated": updated,
        # 该分组里手动自选（source_result_id IS NULL）的现有条数：
        # 前端据此如实告诉用户「替换不会动这些行」。
        "manual_rows_preserved": manual_rows_preserved,
        "replaced": req.replace_group,
        # 规则已被删除时溯源只能落到结果本身（rule_provenance=rule_deleted）
        "rule_provenance": "rule_deleted" if source_rule_id is None else "ok",
    }


def _field_provenance(duck: Any, sqlite: Any, results: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    """Return source rows pinned to the report date saved with each result row."""
    stock_codes = [row.get("stock_code") for row in results if row.get("stock_code")]
    fields = [field for field in columns if field not in {"stock_code", "name", "exchange", "sw_level1", "sw_level2", "csrc_l1", "csrc_l2"}]
    if not stock_codes or not fields:
        return [{} for _ in results]
    if any(not row.get("_report_date") for row in results):
        raise ValueError("saved result lacks report-date provenance; rerun the screening")
    code_slots = ", ".join("?" for _ in stock_codes)
    audit_names = {field.split(".", 1)[-1] for field in fields}
    field_slots = ", ".join("?" for _ in audit_names)
    rows = duck.read_query(
        f"""SELECT stock_code, report_date, field_name, source, fetch_batch_id, fetch_time,
                    raw_response_hash, confidence, reason_code
             FROM source_audit
             WHERE stock_code IN ({code_slots}) AND field_name IN ({field_slots})
             QUALIFY ROW_NUMBER() OVER (
                 PARTITION BY stock_code, report_date, field_name ORDER BY fetch_time DESC, id DESC
             ) = 1""",
        [*stock_codes, *audit_names],
    )
    by_stock: dict[tuple[str, str], dict[str, Any]] = {}
    price_date_by_stock: dict[str, str] = {}
    for row in rows:
        by_stock.setdefault((row["stock_code"], str(row["report_date"])), {})[row["field_name"]] = {
            key: value for key, value in row.items() if key not in {"stock_code", "report_date", "field_name"}
        }
    for override in sqlite.query(
        f"""SELECT stock_code, report_date, field_name, override_value, reason, created_at
             FROM manual_overrides WHERE status = 'published' AND rolled_back_at IS NULL
               AND stock_code IN ({code_slots}) AND field_name IN ({field_slots})""",
        [*stock_codes, *audit_names],
    ):
        by_stock.setdefault((override["stock_code"], str(override["report_date"])), {})[
            override["field_name"]
        ] = {
            "source": "published_override", "value": override["override_value"],
            "reason": override["reason"], "published_at": override["created_at"],
        }
    # P3（依据 S4）+ P3-2/P3-9 修复（依据 S5）：利差列附加曲线对齐溯源
    # （期限/曲线日/日期差）。对齐锚点用快照实际计算用的最新价格日
    # （latest_price_date，缺省回退 _report_date），且批量一次查询。
    spread_fields = [field for field in audit_names if field.startswith("div_yield_spread_")]
    if spread_fields:
        from app.core.adapters.czb_mof_adapter import CZB_CURVE_YIELD_TENOR_LABELS
        from app.core.treasury import TreasuryCurveUpdater

        tenor_by_column = {column: tenor for tenor, column in CZB_CURVE_YIELD_TENOR_LABELS.items()}
        aligner = TreasuryCurveUpdater(duck=duck, sqlite=sqlite)
        for row in results:
            stock = row.get("stock_code", "")
            if not stock or stock in price_date_by_stock:
                continue
            price_date = str(row.get("latest_price_date") or row.get("_report_date") or "")[:10]
            if price_date:
                price_date_by_stock[stock] = price_date
        alignments = aligner.align_many(
            list(price_date_by_stock.values()),
            [tenor_by_column[field] for field in spread_fields],
        )
        for row in results:
            stock = row.get("stock_code", "")
            price_date = price_date_by_stock.get(stock, "")
            if not price_date:
                continue
            key = (stock, str(price_date))
            entry = dict(by_stock.get(key, {}))
            for field in spread_fields:
                tenor = tenor_by_column.get(field)
                if tenor is None:
                    continue
                aligned = alignments.get((price_date, tenor)) or {
                    "curve_date": None, "staleness_days": None,
                }
                base = dict(entry.get(field) or {})
                base.update({
                    "tenor_years": tenor,
                    "curve_date": aligned.get("curve_date"),
                    "staleness_days": aligned.get("staleness_days"),
                })
                entry[field] = base
            by_stock[key] = entry
    # P3-2 修复：返回按实际对齐锚点（latest_price_date 优先）匹配，
    # 与写入 by_stock 的 key 保持一致。
    return [
        by_stock.get(
            (
                row.get("stock_code", ""),
                price_date_by_stock.get(
                    row.get("stock_code", ""), str(row.get("_report_date", ""))
                ),
            ),
            {},
        )
        for row in results
    ]


def _attach_result_report_dates(duck: Any, results: list[dict[str, Any]]) -> None:
    """Capture the selected snapshot period before a result is persisted."""
    stock_codes = [row.get("stock_code") for row in results if row.get("stock_code")]
    if not stock_codes:
        return
    code_slots = ", ".join("?" for _ in stock_codes)
    snapshots = duck.read_query(
        f"""SELECT stock_code, report_date FROM indicator_snapshot
            WHERE stock_code IN ({code_slots})
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY stock_code ORDER BY report_date DESC
            ) = 1""",
        stock_codes,
    )
    report_dates = {row["stock_code"]: str(row["report_date"]) for row in snapshots}
    for row in results:
        row["_report_date"] = report_dates.get(row.get("stock_code"))


_DSL_INDICATOR_LABELS = {
    "dividend_financing_ratio": "分红融资比",
}

#: 未登记中文名的复合指标在界面上必须至少带一个中文标识（PRD §12.1
#: 「用户可见指标名称必须全中文」）。只显示 `dividend_financing_ratio (DSL v2)`
#: 这类纯英文标签是不可接受的，所以缺中文名时统一加「复合指标」前缀。
_DSL_FALLBACK_PREFIX = "复合指标"
# 历史白名单：发布/校验时代 AST 之前创建的指标只能靠硬编码兜底。
#
# ⚠️ 这是单位事实的**第三份副本**（前两份：`app/core/screening/field_units.py` 与
# `frontend/src/utils/screening-format.ts`，由门禁 RG3 + `frontend/tests/screening-format.test.ts`
# 双向对拍）。此处**不能直接删**：`dividend_financing_ratio` 是**发布型 DSL 指标名**，
# 不是快照列名（真实列名是 `dividend_financing_ratio_pct`，见 `schema.py`、`ast_nodes.py:287`），
# 删了会让旧草稿里的该指标拿不到 percent 元数据、退回默认口径 → 100 倍误差。
# 新增指标**禁止**往这里加；AST 时代的指标单位一律走 `field_units.py`。
_DSL_INDICATOR_UNITS = {
    "dividend_financing_ratio": "percent",
}
# DSL AST 根节点单位 → 筛选前端单位口径（field_units.py 的五类值）。
_DSL_AST_UNIT_MAP = {
    "percent": "percent",
    "ratio": "ratio",
    "pct": "pct",
    "cny": "plain",
    "count": "plain",
    "mixed": "plain",
    "unknown": "plain",
}


def _dsl_indicator_unit(expression: dict[str, Any]) -> str:
    """Derive a published DSL indicator's unit from its validated AST.

    只识别 AST 能可靠给出的 percent/ratio；金额、股数、混合单位等一律
    归为 plain（原始数值，不做百分比换算）。
    """
    hardcoded = _DSL_INDICATOR_UNITS.get(expression.get("name", ""))
    if hardcoded:
        return hardcoded
    try:
        ast_json = json.loads(expression.get("ast_json") or "{}")
        if not isinstance(ast_json, dict):
            return "plain"
        return _DSL_AST_UNIT_MAP.get(str(ast_json.get("unit", "unknown")).lower(), "plain")
    except (TypeError, ValueError):
        return "plain"


# 扩展指标域动态放行阈值（依据 S6 P0）：最新报告期覆盖率低于此值的字段不暴露，
# 避免「能选中但筛不出结果」。50% 的取值理由：结构性可空的字段（亏损公司杠杆、
# 无分红公司股息率）实测仍在 73%~85%，真正的"整族缺失"是 0.1%~2% 这种量级。
_EXTENDED_MIN_COVERAGE_PCT = 50.0

# 技术三列门禁（2026-11-16，依据 S2 §9.2 P0-5）：阈值与字段集直接复用
# asset_health 的 `technical_coverage_threshold()` / `TECHNICAL_COVERAGE_FIELDS`。
# 体检说不可信，指标列表就必须以 unavailable(reason=not_available_for_latest_period)
# 隐藏，不能一边报警一边让用户选；也不能再保留本地副本（R1 单一真源）。


def _technical_field_coverage(request: Request) -> dict[str, float]:
    """按"最新快照报告期覆盖率%"实测技术三列。

    复用 `_EXTENDED_MIN_COVERAGE_PCT` 同一条门禁语义：覆盖率不足的字段进
    `unavailable`，而不是让用户选中一个"永远筛出 0 只"的死条件。
    任何异常返回空字典（调用方按"无数据不拦"处理）——体检失败不能把
    指标列表整个打挂。
    """
    counts = ", ".join(
        f'COUNT("{column}") AS "{column}"' for column in TECHNICAL_COVERAGE_FIELDS
    )
    try:
        rows = request.app.state.duck.read_query(
            f"""
            WITH latest AS (
                SELECT MAX(report_date) AS report_date FROM indicator_snapshot
            )
            SELECT COUNT(*) AS total, {counts}
            FROM indicator_snapshot s, latest
            WHERE s.report_date = latest.report_date
            """
        )
    except Exception:  # noqa: BLE001 - 覆盖率探针失败不影响指标列表
        return {}
    if not rows:
        return {}
    row = rows[0]
    total = int(row.get("total") or 0)
    if total <= 0:
        return {}
    return {
        column: float(row.get(column) or 0) / total * 100.0
        for column in TECHNICAL_COVERAGE_FIELDS
    }


#: 覆盖率门禁往前回溯的期数（reports/145）。
#: 8 期 ≈ 两年，足够跨过「季报无折旧摊销」的坑；再往前就太旧、失去参考意义。
_EXTENDED_COVERAGE_LOOKBACK = 8


def _period_type_label(report_date: object) -> str:
    """报告期 → 中文口径名（年报 / 半年报 / 一季报 / 三季报）。"""
    text = str(report_date or "")
    year = text[:4] if len(text) >= 4 else text
    suffix = text[5:10]
    mapping = {
        "03-31": "一季报",
        "06-30": "半年报",
        "09-30": "三季报",
        "12-31": "年报",
    }
    return f"{year} {mapping.get(suffix, '报告期')}"


def _extended_field_coverage(request: Request) -> dict[str, dict[str, object]]:
    """按字段统计覆盖率，并给出**各自可算的基准期**（reports/145）。

    为什么不是只看最新期：`reports/145` §1 —— 一季报/三季报天生不披露现金流量表
    补充资料，折旧摊销族在 Q1/Q3 只剩个位数。只看最新期会让 EBITDA 全家在季报
    窗口被整族隐藏（界面上显示成「覆盖率 0.1%」），而它们在最近一个半年报/年报
    里覆盖率 >98%，完全可用。

    返回：``{字段: {"coverage_pct": float, "basis_report_date": str|None,
    "latest_report_date": str, "basis_label": str|None}}``

    - ``basis_report_date`` 为 None 表示最近 ``_EXTENDED_COVERAGE_LOOKBACK`` 期
      都没有达标覆盖 → 调用方据此隐藏该字段；
    - ``basis_report_date`` 早于 ``latest_report_date`` 表示"用的是上一期口径"，
      前端会提示「按 XXXX 年报/半年报口径」，让用户知道数的时点。
    """
    from app.core.screening.engine import EXTENDED_SCREENING_READY

    columns = sorted(EXTENDED_SCREENING_READY)
    if not columns:
        return {}
    counts = ", ".join(f'COUNT("{column}") AS "{column}"' for column in columns)
    try:
        rows = request.app.state.duck.read_query(
            f"""
            WITH recent AS (
                SELECT DISTINCT report_date
                FROM indicator_ext
                ORDER BY report_date DESC
                LIMIT {int(_EXTENDED_COVERAGE_LOOKBACK)}
            )
            SELECT e.report_date AS report_date, COUNT(*) AS total, {counts}
            FROM indicator_ext e
            JOIN recent r ON r.report_date = e.report_date
            GROUP BY e.report_date
            ORDER BY e.report_date DESC
            """
        )
    except Exception:  # noqa: BLE001 - 体检失败不能影响指标列表
        return {}
    if not rows:
        return {}

    latest_report_date = str(rows[0].get("report_date") or "")
    result: dict[str, dict[str, object]] = {}
    for column in columns:
        basis_date: str | None = None
        basis_coverage = 0.0
        newest_coverage = 0.0
        for idx, row in enumerate(rows):
            total = int(row.get("total") or 0)
            if total <= 0:
                continue
            coverage = float(row.get(column) or 0) / total * 100.0
            if idx == 0:
                newest_coverage = coverage
            if coverage >= _EXTENDED_MIN_COVERAGE_PCT:
                basis_date = str(row.get("report_date") or "")
                basis_coverage = coverage
                break
        result[column] = {
            # 没找到达标期时，仍报出最新期的真实覆盖率（用于 unavailable 提示）
            "coverage_pct": basis_coverage if basis_date else newest_coverage,
            "basis_report_date": basis_date,
            "latest_report_date": latest_report_date,
            "basis_label": _period_type_label(basis_date) if basis_date else None,
        }
    return result


def _masked_indicator_fields(request: Request) -> list[str]:
    """当前"不可信"字段清单（字段级遮蔽，依据 S3 §2.3 J9）。

    来源是 `data_quality.indicator_trust()["masked_fields"]` —— 与只读端点
    （个股详情/自选）同一份判定，避免"筛选说能用、详情说不可信"的分裂。
    任何异常都退化为空清单（宁可少遮一个字段，也不让指标列表整个 500）。
    """
    return list(_indicator_trust_or_empty(request)["masked_fields"])


def _masked_param_columns(base: str, masked: set[str]) -> list[str]:
    """参数化基名下**被遮蔽的具体列**（如 `deducted_profit_cagr3`）。

    基名本身不是真实列，不会被数据质量规则直接判遮蔽；
    必须把它展开成具体列后再看哪些落在 masked 里。
    """
    from app.core.screening.engine import PARAM_FAMILIES

    out: list[str] = []
    for spec in PARAM_FAMILIES.values():
        if base not in spec["bases"]:
            continue
        for value in spec["options"]:
            concrete = str(spec["format"]).format(base=base, value=value)
            if concrete in masked:
                out.append(concrete)
    return out


def _dsl_indicator_label(expression: dict) -> str:
    """复合指标的用户可见标签：中文名优先，无中文名时加「复合指标」前缀。

    PRD §12.1 要求「用户可见指标名称必须全中文（内建指标、排名组合、
    标准化财务表字段），稳定字段名仅用于 API/规则/导出」。
    之前这里直接拼 `{name} (DSL v{n})`，界面上就出现纯英文标签
    （用户 2026-11-16 反馈：`dividend_financing_ratio (DSL v2)`）。
    """
    name = str(expression.get("name") or "")
    version = expression.get("version")
    chinese = _DSL_INDICATOR_LABELS.get(name)
    if chinese:
        return f"{chinese}（复合指标 v{version}）"
    return f"{_DSL_FALLBACK_PREFIX} {name}（v{version}）"


#: 废弃字段替代者的中文名（只用于提示文案，避免让用户看到纯英文列名）。
DEPRECATED_LABELS: dict[str, str] = {
    "ttm_dividend_yield": "股息率（TTM）",
}


def _deprecated_indicator_names() -> dict[str, str]:
    """废弃字段 → 替代字段（依据 S3 §2.6 J5，dividend_yield）。"""
    from app.core.screening.engine import DEPRECATED_COLUMNS

    return dict(DEPRECATED_COLUMNS)


@router.get("/indicators")
def list_available_indicators(request: Request) -> dict:
    """列出可用的筛选指标

    2026-11-14（依据 S3 §2.6 J5 / §2.3 J9）两条过滤规则：
    1. **废弃字段不再列出**（`dividend_yield` → `ttm_dividend_yield`）：
       旧列分母用 `now()`，同一天不同时刻结果不同，不可复现；
    2. **当前不可信字段移到 `unavailable`**（reason=`masked_untrusted`）：
       扩展指标报告期错位时，55 个扩展字段"能选中但筛不出"，必须明示而不是
       让用户以为"全市场都不满足"。
    `unavailable` 过去只构造不返回（前端拿不到原因），这次一并返回。
    """
    from app.core.screening.engine import (  # noqa: F401
        EXTENDED_SCREENING_READY,
        NORMALIZED_FIELDS,
        PARAM_FAMILIES,
        RANKABLE_INDICATORS,
        SNAPSHOT_COLUMNS,
        STAT_METHODS,
        STAT_METRICS,
        STAT_WINDOWS,
        param_field_names,
        param_spec_for,
    )
    from app.core.screening.field_units import field_unit

    indicators: list[dict] = []
    unavailable: list[dict] = []
    # 依据 S2 §9.2 P0-5：技术三列最新期覆盖率 < 95% → 进 unavailable。
    # 只影响这三个字段，绝不触碰下面的信任门禁（masked_untrusted/deprecated）。
    technical_coverage = _technical_field_coverage(request)
    technical_limit = technical_coverage_threshold()
    # 参数化字段（reports/143 §2）：`div_yield_spread_10y`、`revenue_cagr5` 这类
    # 「同一指标 + 参数后缀」不再逐个下发，改为只下发基名 + 参数规格，
    # 由条件/排序/结果列上的参数下拉选择（9 + 6 = 15 个字段 → 4 个入口）。
    _param_columns = param_field_names()

    for col in sorted(SNAPSHOT_COLUMNS):
        if col in _param_columns:
            continue
        coverage = technical_coverage.get(col)
        if (
            col in TECHNICAL_COVERAGE_FIELDS
            and coverage is not None
            and coverage < technical_limit
        ):
            unavailable.append({
                "name": col,
                "domain": "snapshot",
                "coverage_pct": round(coverage, 2),
                "reason": "not_available_for_latest_period",
                "message": (
                    "该技术指标在最新报告期覆盖率 "
                    f"{coverage:.2f}% < {technical_limit:.0f}%，"
                    "qfq 有待重算；为避免筛出 0 只的假结论，暂不暴露"
                ),
            })
            continue
        _spec = param_spec_for(col)
        indicators.append({
            "name": col,
            "rankable": col in RANKABLE_INDICATORS,
            # 2026-08-14 红队 F3：单位元数据单一来源，前端据此做输入换算与展示
            "unit": field_unit(col),
            **({"coverage_pct": round(coverage, 2)} if coverage is not None else {}),
            # 参数化字段（如 revenue_cagr 需要选 3/5 年）→ 前端渲染参数下拉
            **({"param_spec": _spec} if _spec else {}),
        })
    for field in sorted(NORMALIZED_FIELDS):
        if field in _param_columns:
            continue
        _spec = param_spec_for(field)
        indicators.append({
            "name": field,
            "rankable": field in RANKABLE_INDICATORS,
            "unit": field_unit(field),
            **({"param_spec": _spec} if _spec else {}),
        })
    # 参数化字段的**基名入口**（reports/143 §2）：库里只有 `revenue_cagr3/5` 这类
    # 带后缀的真实列，但下拉里只暴露基名 + 参数下拉。
    # 单位沿用同族真实列（CAGR 存小数比例用 pct，国债利差存百分数用 percent）。
    _unit_samples = {
        "cagr": "revenue_cagr3",
        "tenor": "div_yield_spread_10y",
    }
    for kind, spec in PARAM_FAMILIES.items():
        sample_unit = field_unit(_unit_samples[kind])
        for base in spec["bases"]:
            indicators.append({
                "name": base,
                "rankable": any(
                    str(spec["format"]).format(base=base, value=v) in RANKABLE_INDICATORS
                    for v in spec["options"]
                ),
                "unit": sample_unit,
                "param_spec": param_spec_for(base),
            })

    # 扩展指标域（schema v25）：按**当前报告期实测覆盖率**动态放行。
    #
    # 2026-11-14（依据 S6 P0）：`EXTENDED_SCREENING_READY` 是 2026-09-19 的静态
    # 结论，此后 ext 冻结在 2026-06-30、报告期与快照错位一个月 → 55 项全部变成
    # 「能选中但永远筛不出结果」的死条件。静态白名单挡不住这种腐化，改为运行时判定：
    # 覆盖率 < 阈值（默认 50%）的字段**不暴露**，并用 `unavailable` 说明原因。
    # 三季报不披露现金流量表补充资料，折旧摊销族天然缺失 —— 这正是需要隐藏而非
    # 让用户筛出 0 结果的场景。
    # 2026-11-16（reports/145）：门禁基准期从「最新一期」改为「每个字段各自最近
    # 一个达标期」。原因：一季报/三季报天生不披露现金流量表补充资料，折旧摊销族
    # 在季报窗口只剩个位数有值（实测 09-30 仅 5/5575），旧逻辑会把 EBITDA 全家
    # 整族隐藏成「覆盖率 0.09%」，而它们在最近半年报/年报里覆盖率 >98%。
    extended_coverage = _extended_field_coverage(request)
    for col in sorted(EXTENDED_SCREENING_READY):
        meta = extended_coverage.get(col) or {}
        coverage = meta.get("coverage_pct")
        basis = meta.get("basis_report_date")
        latest_period = meta.get("latest_report_date")
        if coverage is not None and basis is None:
            # 最近 N 期都达不到阈值 —— 这才是真的"没有可用数据"
            unavailable.append({
                "name": col,
                "domain": "extended",
                "coverage_pct": round(float(coverage), 2),
                "reason": "not_available_for_latest_period",
                "message": (
                    f"最近 {_EXTENDED_COVERAGE_LOOKBACK} 个报告期内覆盖率均不足 "
                    f"{_EXTENDED_MIN_COVERAGE_PCT:.0f}%；最新期覆盖率 "
                    f"{float(coverage):.2f}%"
                ),
            })
            continue
        entry = {
            "name": col,
            "rankable": col in RANKABLE_INDICATORS,
            "unit": field_unit(col),
            "domain": "extended",
            "coverage_pct": None if coverage is None else round(float(coverage), 2),
        }
        # 基准期早于最新期 → 明确告诉前端"这是上一期口径"，
        # 否则用户会以为筛选用的是最新季报的数。
        if basis and latest_period and str(basis) != str(latest_period):
            entry["basis_report_date"] = str(basis)
            entry["basis_label"] = meta.get("basis_label")
            entry["latest_report_date"] = str(latest_period)
        indicators.append(entry)
    # 2026-11-16：派生排名/分位字段必须带 `derived_from` 标记。
    # 每个可排名指标派生 6 条（全市场排名/分位 + 证监会一级排名/分位 + 二级排名/分位），
    # ROCE 三个窗口因此膨胀成 3 + 3×6 = **21 条**，用户在下拉里根本找不到主指标。
    # 标记后前端默认折叠，只在打开「显示派生字段」时列出。
    # 排名口径改为**条件级下拉**（2026-11-16 方案 C）。
    #
    # 过去这里为 RANKABLE_INDICATORS 里每个指标铺 6 条扁平字段
    # （`pe_ttm_market_rank` / `pe_ttm_industry_percentile` …）。
    # 155 个可排名指标 × 6 = **930 条**，ROCE 一个指标就占 21 条，主指标被埋在
    # 下拉中间根本选不到；而且它与 DSL 已有的
    # `rank()/rank_industry()/percentile()/zscore()/normalize()` 是同一功能的
    # 两套实现（PRD §11.2 把排名定位为 DSL 运算）。
    #
    # 现在只下发「可用的排名口径」清单（见响应体 rank_modes），前端在条件行上
    # 渲染一个下拉；后端 `expand_rank_modes()` 把它展开回原字段名，
    # 排名 SQL（NULLS LAST、csrc_l1 为 NULL 时行业排名返回 NULL 等）完全不变。
    # 只有 `rankable: true` 的指标才会在该条件下显示这个下拉。
    # P4 历史研究统计字段（已发布统计域，依据 S4 §6）
    for metric in STAT_METRICS:
        for window in STAT_WINDOWS:
            for method in STAT_METHODS:
                field = f"{metric}_stat_{window}y_{method}"
                indicators.append({
                    "name": field,
                    "rankable": False,
                    "stat": True,
                    "metric": metric,
                    "window_years": window,
                    "method": method,
                    "derived_from": metric,
                    "derived_kind": "stat",
                })
    # 已发布 DSL：只暴露每个名称的最新版本，并支持中文名/单位元数据。
    # 单位用于筛选输入/展示；percent 表示值本身是百分数（如 150.69 → 150.69%，
    # 按用户口径我们发布前乘 100，所以 150.69 应显示为 15069%）。
    published = request.app.state.sqlite.query(
        """SELECT name, version, content_hash, ast_json
           FROM dsl_expressions
           WHERE status = 'published' AND version = (
               SELECT MAX(version) FROM dsl_expressions e2
               WHERE e2.name = dsl_expressions.name AND e2.status = 'published'
           )
           ORDER BY name"""
    )
    for expression in published:
        indicators.append({
            "name": expression["name"], "rankable": True,
            # 2026-09-02：筛选条件保留英文表达式名（规则 JSON 稳定口径）；
            # display_name 供复合指标管理/详情等非筛选界面显示中文名。
            "label": _dsl_indicator_label(expression),
            "display_name": _DSL_INDICATOR_LABELS.get(expression["name"], expression["name"]),
            "unit": _dsl_indicator_unit(expression),
            "version": expression["version"], "content_hash": expression["content_hash"],
            "dsl": True,
        })
    # ── 两条过滤：废弃字段 & 当前不可信字段（字段级遮蔽）─────────────────
    deprecated = _deprecated_indicator_names()
    # 整体可信度必须显式返回：前端不能只看 unavailable 猜"是不是整页都坏了"。
    # 与 /run 的 409 判定同源（indicator_trust → screenability_block_reason）。
    trust = _indicator_trust_or_empty(request)
    masked = set(trust["masked_fields"])
    from app.core.screening.engine import DEPRECATED_SCALE

    kept: list[dict] = []
    for entry in indicators:
        name = entry.get("name")
        if name in deprecated:
            scale = DEPRECATED_SCALE.get(name)
            unavailable.append({
                "name": name,
                "domain": entry.get("domain") or "snapshot",
                "reason": "deprecated",
                "replaced_by": deprecated[name],
                # 2026-11-15（依据 S7）：两列存储量纲不同（小数 vs 百分数，实测恰
                # 100 倍）。不给倍数，用户把 >0.03 换成 >0.03 会命中几乎全市场。
                "scale_factor": scale,
                # 2026-11-16：这里原本写「字段已废弃（口径不可复现）」，措辞是错的——
                # `dividend_yield` 与 `ttm_dividend_yield` 是**同一个指标**（实测 3,735 行
                # 中位绝对差 0.0000），差别只是存储单位（小数 0.0121 vs 百分数 1.2118）。
                # 说成"废弃"会让用户以为数算错了；真相是单位口径统一（依据 S8 簇 A/B/C）。
                "message": (
                    f"同一指标的单位已统一，请改用「{DEPRECATED_LABELS.get(deprecated[name], deprecated[name])}」"
                    + (f"（字段 {deprecated[name]}）：旧列存小数（0.0121 = 1.21%），"
                       f"新列存百分数（1.21），量纲差 {scale:g} 倍，"
                       f"写成条件时旧列的 >0.03 要改成新列的 >3"
                       if scale else f"（字段 {deprecated[name]}）")
                ),
            })
            continue
        # 参数化字段的遮蔽要**向上传播**（reports/143 §2）：
        # 库里只有 `deducted_profit_cagr3/5` 这些真实列，下拉里只暴露基名
        # `deducted_profit_cagr`。若具体列被判为不可信，基名也必须进 unavailable，
        # 否则用户能选中一个实际会被 409 拒绝的入口。
        masked_concrete = _masked_param_columns(name, masked)
        if name in masked or masked_concrete:
            unavailable.append({
                "name": name,
                "domain": entry.get("domain") or "snapshot",
                "reason": "masked_untrusted",
                "message": (
                    "字段当前不可信（数据质量规则判定为遮蔽状态），"
                    "已从可选指标中移除；引用它的规则运行会被拒绝（409）"
                    + (
                        f"（遮蔽的具体列：{', '.join(masked_concrete)}）"
                        if masked_concrete else ""
                    )
                ),
            })
            continue
        kept.append(entry)
    indicators = kept

    from app.core.screening.engine import RANK_MODE_LABELS, RANK_MODES

    return {
        "indicators": indicators,
        "count": len(indicators),
        # 条件级排名口径（方案 C）：前端据此渲染条件行上的「排名口径」下拉。
        # 空/缺省 = 用原始值；否则后端展开成 `{field}_{mode}`。
        "rank_modes": [
            {"value": mode, "label": RANK_MODE_LABELS[mode]} for mode in RANK_MODES
        ],
        # 不暴露的字段必须**带原因**返回：只消失不给理由，用户会以为功能没了。
        # reason ∈ {deprecated, masked_untrusted, not_available_for_latest_period}
        "unavailable": unavailable,
        "unavailable_count": len(unavailable),
        # 整体可信度（与详情页 /indicators 的 trust 同一判定）：
        # untrusted_all=True 时 /run 必然 409 snapshot_values_untrusted，
        # warning_codes 供前端解释"为什么不能筛"。
        "untrusted_all": bool(trust["untrusted_all"]),
        "warning_codes": list(trust["warning_codes"]),
        "masked_fields": sorted(masked),
        "deprecated_fields": sorted(deprecated),
        # 单位口径说明（与 app/core/screening/field_units.py 对齐）：
        # plain 明确表示“原始数值，不进行百分比换算”（金额/股数/倍率等），
        # 供前端展示与输入换算时判断。
        "unit_semantics": {
            "plain": "plain 表示原始数值（金额/股数/倍率等），不进行百分比换算",
            "pct": "pct 表示小数比例存储，展示时乘以 100 并加 %",
            "percent": "percent 表示百分数原值存储，直接加 %",
            "price": "price 表示价格",
            "ratio": "ratio 表示倍率",
        },
    }


class SaveRuleRequest(BaseModel):
    """保存筛选规则请求"""
    name: str
    rule_json: dict[str, Any]
    locked_indicators: dict[str, Any] | None = None
    status: str = "draft"


class UpdateRuleRequest(BaseModel):
    """原地更新一套已保存规则请求（`PUT /rules/{rule_id}`）。

    2026-11-25 UX（用户投诉「改了点数字就自动存成 V1/V2/V3，我以后怎么分辨」）：
    同一套规则的编辑**不再追加一行同名规则**，而是原地改写 `rule_json` /
    `locked_indicators` 并把 `version` +1；`id` 与 `name` 都不变。
    """
    rule_json: dict[str, Any]
    locked_indicators: dict[str, Any] | None = None
    # 乐观并发：客户端声明"我编辑的是哪一版"。版本已被别处推进 → 409。
    expected_version: int | None = None


def _validated_rule_payload(
    sqlite: Any,
    rule_json: dict[str, Any],
    locked_indicators: dict[str, Any] | None,
) -> tuple[str, str]:
    """规则写库前的统一校验，返回 `(rule_json_text, locked_indicators_text)`。

    `POST /rules/save` 与 `PUT /rules/{id}` **共用这一份**（R1：一个事实只能有
    一个实现），否则两条写入路径迟早分叉出"保存校验了、更新没校验"的漏洞。

    依据 S9 P3-4: 保存时即校验字段名（与 engine.run 运行时校验同口径），
    未知字段/排序/结果列在保存时拒绝，不再拖到运行筛选才报错。
    指标锁同样在这里解析/校验（过期锁 → 400）。
    """
    if len(json.dumps(rule_json, ensure_ascii=False).encode("utf-8")) > MAX_RULE_JSON_BYTES:
        raise HTTPException(status_code=400, detail="rule JSON is too large")

    from app.core.screening.engine import validate_rule_fields

    try:
        published_names = {
            row["name"]
            for row in sqlite.query(
                "SELECT name FROM dsl_expressions WHERE status = 'published'"
            )
        }
        validate_rule_fields(rule_json, published_names)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    try:
        locks = resolve_rule_indicator_locks(sqlite, rule_json, locked_indicators or {})
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return (
        json.dumps(rule_json, ensure_ascii=False),
        json.dumps(locks, ensure_ascii=False),
    )


@router.post("/rules/save")
def save_rule(req: SaveRuleRequest, request: Request) -> dict:
    """保存筛选规则 (PRD §12.2: 规则编辑必须版本化)

    2026-11-25 用户裁定（投诉「一堆 V1、V2、V3 分不清」）：同名规则**不再**追加
    `v2/v3` 新行 —— 那种历史行为让下拉里出现两条看起来不同的规则，实际是同一套
    被改过两次。现在同名已存在就直接 400，引导用户去「编辑那一条」（PUT）。
    """
    sqlite = request.app.state.sqlite
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="rule name is required")
    rule_json_text, locks_text = _validated_rule_payload(
        sqlite, req.rule_json, req.locked_indicators
    )

    with sqlite.transaction() as conn:
        # 同名只允许"编辑那一条"（PUT），不允许再插一行新版本。
        existing = conn.execute(
            "SELECT id FROM screening_rules WHERE name = ? LIMIT 1", [name]
        ).fetchone()
        if existing is not None:
            raise HTTPException(
                status_code=400,
                detail="已存在同名规则，请换个名字或直接编辑那一条",
            )

        try:
            cursor = conn.execute(
                """INSERT INTO screening_rules (name, version, rule_json, locked_indicators, status)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    name,
                    1,
                    rule_json_text,
                    locks_text,
                    # C6修复(报告41): 服务端固定状态机，忽略客户端提交的 status
                    "saved",
                ],
            )
        except Exception as error:
            # 唯一索引兜底（并发下同名两行同时插入）：与 save 既有风格一致的 409。
            raise HTTPException(
                status_code=409, detail="concurrent rule save conflict; retry"
            ) from error
        rule_id = cursor.lastrowid

    return {"status": "ok", "rule_id": rule_id, "version": 1}


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, req: UpdateRuleRequest, request: Request) -> dict:
    """原地更新一套已保存规则（同一 `id` / 同一 `name`，`version` +1）。

    契约与 `POST /rules/save` 对齐（前端同一条分支即可处理）：

    - 校验复用 `_validated_rule_payload`（字段名 / 指标锁 / 体积，与 save 同口径）
    - 规则不存在 → 404 `rule not found`
    - 并发冲突（`expected_version` 落后，或 UPDATE 落空）→ 409
      `concurrent_rule_save_conflict`（前端已有中文映射，刷新后重试）
    - 成功 → `{status, rule_id, version}`，`version` 为 +1 后的新版本

    为什么必须原地改而不是再插一行：`screening_runs.rule_version` 是运行日志的
    历史读数，用户视角的"一套规则"只能对应 `screening_rules` 的一行；多行同名
    规则在下拉里就是 V1/V2/V3 那堆分不清的东西（用户原话）。
    """
    sqlite = request.app.state.sqlite
    rule_json_text, locks_text = _validated_rule_payload(
        sqlite, req.rule_json, req.locked_indicators
    )

    with sqlite.transaction() as conn:
        current = conn.execute(
            "SELECT id, name, version FROM screening_rules WHERE id = ?", [rule_id]
        ).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail="rule not found")

        current_version = int(current["version"])
        if req.expected_version is not None and int(req.expected_version) != current_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason_code": "concurrent_rule_save_conflict",
                    "message": (
                        f"规则「{current['name']}」已被其他操作改成 v{current_version}，"
                        f"你编辑的是 v{int(req.expected_version)}；请刷新后重试"
                    ),
                },
            )

        next_version = current_version + 1
        # 历史遗留的同名多行（如正式库 49=v1 / 50=v2，都是修复前的产物）：
        # `UNIQUE(name, version)` 会让"49 从 v1 升到 v2"撞上 50 那一行。取
        # 「该名字下现有最大版本 + 1」即可原地升版本且永不撞车（干净数据里
        # max == current_version，等价于 +1）。不要求用户先清理历史数据。
        highest = conn.execute(
            "SELECT MAX(version) AS max_version FROM screening_rules WHERE name = ?",
            [current["name"]],
        ).fetchone()
        if highest is not None and highest["max_version"] is not None:
            next_version = max(next_version, int(highest["max_version"]) + 1)
        try:
            cursor = conn.execute(
                """UPDATE screening_rules
                      SET rule_json = ?, locked_indicators = ?, version = ?, status = 'saved'
                    WHERE id = ? AND version = ?""",
                [rule_json_text, locks_text, next_version, rule_id, current_version],
            )
            if cursor.rowcount != 1:
                raise HTTPException(
                    status_code=409, detail="concurrent rule update conflict; retry"
                )
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                status_code=409, detail="concurrent rule update conflict; retry"
            ) from error

    return {"status": "ok", "rule_id": rule_id, "version": next_version}


def _collect_rule_fields(node: dict[str, Any]) -> set[str]:
    fields: set[str] = set()
    for rule in node.get("rules", []):
        if "logic" in rule:
            fields.update(_collect_rule_fields(rule))
        elif rule.get("field"):
            fields.add(rule["field"])
            if rule.get("right_field"):
                fields.add(rule["right_field"])
    return fields


def resolve_rule_indicator_locks(
    sqlite: Any,
    rule: dict[str, Any],
    requested_locks: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Resolve only current published DSL versions referenced by one rule."""
    referenced = _collect_rule_fields(rule.get("conditions", {}))
    referenced.update(item.get("field", "") for item in rule.get("sort", []))
    referenced.update(rule.get("columns", []))
    published = {
        row["name"]: row
        for row in sqlite.query(
            """SELECT name, version, content_hash
               FROM dsl_expressions
               WHERE status = 'published'
                 AND version = (
                     SELECT MAX(version) FROM dsl_expressions e2
                     WHERE e2.name = dsl_expressions.name AND e2.status = 'published'
                 )
               ORDER BY name"""
        )
    }
    locks: dict[str, dict[str, Any]] = {}
    for name in referenced:
        if name not in published:
            continue
        supplied = requested_locks.get(name, {})
        version = supplied.get("version", published[name]["version"])
        content_hash = supplied.get("content_hash", published[name]["content_hash"])
        if version != published[name]["version"] or content_hash != published[name]["content_hash"]:
            raise ValueError(f"published indicator lock is stale: {name}")
        locks[name] = {"version": version, "content_hash": content_hash}
    unknown_requested = set(requested_locks) - set(locks)
    if unknown_requested:
        raise ValueError(f"unknown or unreferenced indicator locks: {sorted(unknown_requested)}")
    return locks


@router.get("/rules")
def list_rules(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict:
    """列出已保存的筛选规则"""
    sqlite = request.app.state.sqlite

    rows = sqlite.query(
        """SELECT id, name, version, rule_json, locked_indicators, status, created_at
           FROM screening_rules
           ORDER BY created_at DESC
           LIMIT ?""",
        [limit],
    )

    rules = []
    for row in rows:
        rule = dict(row)
        rule["rule_json"] = json.loads(rule["rule_json"]) if rule.get("rule_json") else {}
        rule["locked_indicators"] = json.loads(rule["locked_indicators"]) if rule.get("locked_indicators") else {}
        rules.append(rule)

    return {"rules": rules, "count": len(rules)}


@router.get("/rules/{rule_id}")
def get_rule(rule_id: int, request: Request) -> dict:
    """获取单个筛选规则"""
    sqlite = request.app.state.sqlite

    rows = sqlite.query(
        """SELECT id, name, version, rule_json, locked_indicators, status, created_at
           FROM screening_rules
           WHERE id = ?""",
        [rule_id],
    )

    if not rows:
        raise HTTPException(status_code=404, detail="rule not found")

    rule = dict(rows[0])
    rule["rule_json"] = json.loads(rule["rule_json"]) if rule.get("rule_json") else {}
    rule["locked_indicators"] = json.loads(rule["locked_indicators"]) if rule.get("locked_indicators") else {}

    return rule


def _rule_delete_blocked_message(results_count: int, watchlist_count: int) -> str:
    """按**真实条数**拼"删不掉"的文案（哪种引用、几条，动态拼）。

    2026-11-25 用户投诉现场：错误文案写「已被运行结果或自选列表引用」，用户去
    自选列表翻了一圈 —— 那里当然没有这个分组，因为挡住删除的是运行日志
    `screening_runs`。所以这里只允许点名**真正的溯源锚点**：
    「已保存的筛选结果」与「自选记录」，并逐项带条数；"运行结果"这种指向不明
    的词永远不再出现。
    """
    if results_count and watchlist_count:
        return (
            f"该规则有 {results_count} 条已保存的筛选结果、{watchlist_count} 条自选记录"
            "引用，不能删除；请先删除这些筛选结果或自选记录"
        )
    if results_count:
        return (
            f"该规则有 {results_count} 条已保存的筛选结果引用，不能删除；"
            "请先删除这些筛选结果"
        )
    return (
        f"该规则有 {watchlist_count} 条自选记录引用，不能删除；请先删除这些自选记录"
    )


def delete_rule_with_guards(sqlite: Any, rule_id: int) -> dict:
    """删除没有被「已保存结果 / 自选记录」引用的筛选规则。

    这是规则删除的**唯一守卫实现**：web `DELETE /api/screening/rules/{id}` 与
    `DELETE /api/watchlist/groups/{group}` 的 `delete_rule=True` 都只准调它，
    不允许任何调用方复制一份"只查部分引用"的守卫（那样删分组就会绕过规则保护）。

    2026-11-25 修 UX（投诉「自选列表里明明没有这个分组，为什么它还删不掉」）：
    旧守卫把 `screening_runs` 也算引用 —— 那是"每跑一次加一行"的**运行日志**，
    用户不会去删它，于是**规则只要跑过一次就永远删不掉**。现在的口径：

    - 真正拦删除的只有两个溯源锚点：`screening_results.rule_id`（用户保存的
      筛选结果）与 `watchlist.source_rule_id`（自选记录来源）；
    - `screening_runs` 行**保留**（审计日志），删除规则后它们的 `rule_id` 成为
      悬空历史值。本仓库没有"按 run 反查规则"的读取方（`save_result` 只用 run
      行上的 rule_id/rule_version 副本，规则已被删时按 400 提示重新运行），
      所以悬空 id 不会让任何读取路径崩。

    返回 `{"status": "ok", "deleted_rule_id": id}`；规则不存在抛 404，
    被引用时抛 409，detail 为 `{reason_code, message, refs}`（调用方必须原样
    尊重这两个出口，不能把 409 吞成"看起来删掉了"）。
    """
    rows = sqlite.query("SELECT id FROM screening_rules WHERE id = ?", [rule_id])
    if not rows:
        raise HTTPException(status_code=404, detail="rule not found")

    refs = sqlite.query(
        """SELECT
               (SELECT COUNT(*) FROM screening_results WHERE rule_id = ?) AS results,
               (SELECT COUNT(*) FROM watchlist WHERE source_rule_id = ?) AS watchlist,
               (SELECT COUNT(*) FROM screening_runs WHERE rule_id = ?) AS runs""",
        [rule_id, rule_id, rule_id],
    )[0]
    results_count = int(refs["results"] or 0)
    watchlist_count = int(refs["watchlist"] or 0)
    if results_count or watchlist_count:
        raise HTTPException(status_code=409, detail={
            "reason_code": "rule_has_provenance_refs",
            "message": _rule_delete_blocked_message(results_count, watchlist_count),
            # refs 同时带回运行日志条数（只做展示/排障，不参与拦截判定）。
            "refs": refs,
        })

    with sqlite.transaction() as conn:
        # `screening_runs.rule_id` 在 schema v7 带 FOREIGN KEY，而连接固定
        # `PRAGMA foreign_keys=ON` —— 不关掉的话，只要跑过一次就删不掉规则。
        # 这里只在本连接（事务刚开、尚无 DML）关掉外键检查：delete 规则行，
        # 运行日志行原样保留为审计记录（悬空 rule_id 是**有意**的历史读数）。
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM screening_rules WHERE id = ?", [rule_id])
    return {"status": "ok", "deleted_rule_id": rule_id}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, request: Request) -> dict:
    """删除筛选规则（守卫见 `delete_rule_with_guards`，本路由只是薄外壳）。"""
    return delete_rule_with_guards(request.app.state.sqlite, rule_id)
