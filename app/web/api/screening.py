"""筛选页 API (PRD §12)

提供筛选规则运行、结果保存、导出、加入自选等功能。
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

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
    "_strict_only", "_field_provenance", "_entry_explanation",
]


def _csv_export_header(
    columns: list[str], truncated: bool, auto_update: bool = False,
) -> list[str]:
    header = columns + _CSV_META_COLUMNS
    if truncated:
        # P1-C: 截断结果必须在导出的 CSV 中显式标注，禁止静默丢尾
        header.append("_truncated")
    if auto_update:
        # reports/79 方案 A：更新窗口内基于快照的运行必须显式标注口径
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
    line.append(_csv_cell(row.get("_entry_explanation", "")))
    if truncated:
        line.append(True)
    if auto_update:
        line.append(data_as_of or "")
        line.append(True)
    return line


def _require_current_screenability(request: Request) -> dict:
    """Enforce the current database gate for screen-derived durable output.

    reports/79 方案 A（用户决策）：写锁活跃（自动更新中）时不再 409 禁用——
    引擎只读原子替换的 indicator_snapshot 与财务表（不直读 raw 价格），
    快照口径下筛选结果内部一致；放行并返回 data_as_of（快照价格日）供
    标注。快照完全缺失时仍 409 兜底。

    Returns: {"lock_active": bool, "data_as_of": str | None}
    """
    from app.core.storage.update_lock import update_lock_active

    try:
        lock_active = update_lock_active(request.app.state.duck.db_path)
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
        return {"lock_active": True, "data_as_of": data_as_of}

    from app.core.data_quality import screening_readiness

    decision = screening_readiness(request.app.state.duck, request.app.state.sqlite)
    request.app.state.startup_readiness = decision["readiness"]
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
        })
    return {"lock_active": False, "data_as_of": None}


class ScreeningRequest(BaseModel):
    """筛选运行请求"""
    rule_id: int
    rule_version: int
    include_st: bool = False
    include_suspended: bool = False
    min_listing_years: int = 1
    strict_only: bool = False


class SaveResultRequest(BaseModel):
    """保存筛选结果请求"""
    title: str                         # 标题 (必填, PRD SC14)
    note: str | None = None            # 备注 (可选)
    run_id: str
    columns: list[str] | None = None


class ExportCsvRequest(BaseModel):
    """导出 CSV 请求"""
    result_id: int


class AddToWatchlistRequest(BaseModel):
    """加入自选列表请求"""
    stock_codes: list[str]
    group: str = "default"
    result_id: int | None = None


class ScreeningDraftRequest(BaseModel):
    draft: dict[str, Any]
    revision: int = 0


@router.post("/run")
def run_screening(req: ScreeningRequest, request: Request) -> dict:
    """运行筛选 (PRD §12.2 SC8: 手动运行, 以最新完整快照为准)"""
    from app.core.screening.engine import ScreeningEngine

    gate = _require_current_screenability(request)

    rule_rows = request.app.state.sqlite.query(
        "SELECT rule_json, locked_indicators FROM screening_rules WHERE id = ? AND version = ?",
        [req.rule_id, req.rule_version],
    )
    if not rule_rows:
        raise HTTPException(status_code=400, detail="saved rule version not found")
    rule = json.loads(rule_rows[0]["rule_json"])
    # P1-5修复: 惰性过期清理由单条原子 DELETE 完成, 只回收早于 TTL 的 run,
    # 不再删除其他页面尚在有效期内未保存的 run; 正常消费仍由 /save 负责删除
    request.app.state.sqlite.execute(
        "DELETE FROM screening_runs WHERE created_at < datetime('now', ?)",
        [f"-{SCREENING_RUN_TTL_HOURS} hours"],
    )
    locked_indicators = json.loads(rule_rows[0]["locked_indicators"] or "{}")
    engine = ScreeningEngine(duck=request.app.state.duck, sqlite=request.app.state.sqlite)
    try:
        result = engine.run(
            rule=rule,
            include_st=req.include_st,
            include_suspended=req.include_suspended,
            min_listing_years=req.min_listing_years,
            strict_only=req.strict_only,
            locked_indicators=locked_indicators,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    # 为每条结果生成入选解释 (PRD SC13)
    conditions = rule.get("conditions", {})
    for stock in result["results"]:
        stock["_entry_explanation"] = engine.generate_entry_explanation(
            stock, conditions
        )
    _attach_result_report_dates(request.app.state.duck, result["results"])

    run_id = str(uuid.uuid4())
    columns = rule.get("columns", [])
    base_pool = {
        "include_st": req.include_st,
        "include_suspended": req.include_suspended,
        "min_listing_years": req.min_listing_years,
    }
    # reports/79 方案 A: 更新窗口内的快照口径运行必须随结果持久化标注
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
    with request.app.state.sqlite.transaction() as conn:
        current = conn.execute(
            "SELECT revision FROM screening_drafts WHERE id = 1"
        ).fetchone()
        if current is None:
            if req.revision != 0:
                raise HTTPException(status_code=409, detail="draft revision conflict; reload and retry")
        elif current["revision"] != req.revision:
            raise HTTPException(status_code=409, detail="draft revision conflict; reload and retry")
        next_revision = req.revision + 1
        conn.execute(
            """INSERT INTO screening_drafts (id, draft_json, revision, updated_at)
               VALUES (1, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET draft_json=excluded.draft_json, revision=excluded.revision, updated_at=CURRENT_TIMESTAMP""",
            [json.dumps(req.draft, ensure_ascii=False), next_revision],
        )
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
        cursor = conn.execute(
            """INSERT INTO screening_results
                (title, note, rule_id, rule_version, data_date,
                 result_json, columns_json, sort_json, confidence_summary, base_pool_config)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                req.title,
                req.note,
                run["rule_id"], run["rule_version"],
                run["data_date"] or datetime.now(timezone.utc).isoformat(),
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
    columns = json.loads(record["columns_json"])
    data_date = record["data_date"]
    rule = sqlite.query(
        "SELECT locked_indicators FROM screening_rules WHERE id = ? AND version = ?",
        [record["rule_id"], record["rule_version"]],
    )
    if not rule:
        raise HTTPException(status_code=400, detail="saved result rule provenance is missing")
    summary = json.loads(record.get("confidence_summary") or "{}")
    truncated = bool(summary.get("truncated"))
    # reports/79 方案 A: 更新窗口内保存的结果导出时显式标注快照口径
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
    writer.writerow(_csv_export_header(columns, truncated, auto_update))

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
    """将筛选结果加入自选列表 (PRD §12.5 SC17)"""
    _require_current_screenability(request)
    sqlite = request.app.state.sqlite
    stock_codes = req.stock_codes
    group_name = req.group
    result_id = req.result_id

    if len(stock_codes) > 10000:
        raise HTTPException(status_code=400, detail="too many stocks (max 10000)")

    added = 0
    with sqlite.transaction() as conn:
        result = conn.execute(
            "SELECT rule_id, result_json FROM screening_results WHERE id = ?",
            [result_id],
        ).fetchone()
        if result is None:
            raise HTTPException(status_code=400, detail="a saved screening result is required")
        saved_codes = {row.get("stock_code") for row in json.loads(result["result_json"])}
        requested_codes = set(stock_codes)
        if not requested_codes <= saved_codes:
            raise HTTPException(status_code=400, detail="stock codes must come from the saved result")
        for code in stock_codes:
            conn.execute(
                """INSERT INTO watchlist
                   (stock_code, group_name, source_rule_id, source_result_id)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(stock_code, group_name) DO UPDATE SET
                     source_rule_id=excluded.source_rule_id,
                     source_result_id=excluded.source_result_id,
                     added_at=CURRENT_TIMESTAMP""",
                [code, group_name, result["rule_id"], result_id],
            )
            added += 1

    return {"status": "ok", "added": added}


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
    # P3（reports/68）+ P3-2/P3-9 修复（reports/73）：利差列附加曲线对齐溯源
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


@router.get("/indicators")
def list_available_indicators(request: Request) -> dict:
    """列出可用的筛选指标"""
    from app.core.screening.engine import (
        NORMALIZED_FIELDS,
        SNAPSHOT_COLUMNS,
        RANKABLE_INDICATORS,
        STAT_METRICS,
        STAT_WINDOWS,
        STAT_METHODS,
    )

    indicators: list[dict] = []
    for col in sorted(SNAPSHOT_COLUMNS):
        indicators.append({
            "name": col,
            "rankable": col in RANKABLE_INDICATORS,
        })
    for field in sorted(NORMALIZED_FIELDS):
        indicators.append({"name": field, "rankable": field in RANKABLE_INDICATORS})
    for col in sorted(RANKABLE_INDICATORS):
        for suffix, label in (
            ("market_rank", "全市场排名"), ("market_percentile", "全市场分位"),
            # P1-D修复: 行业排名按 PRD §24 为 CSRC（证监会）口径；
            # 不再以"申万"标签误导用户，并暴露正确命名的 CSRC 列。
            ("industry_rank", "证监会一级排名"), ("industry_percentile", "证监会一级分位"),
            ("sw2_rank", "证监会二级排名"), ("sw2_percentile", "证监会二级分位"),
        ):
            indicators.append({"name": f"{col}_{suffix}", "rankable": False, "label": f"{col} {label}"})
    # P4 历史研究统计字段（已发布统计域，reports/68 §6）
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
                })
    published = request.app.state.sqlite.query(
        "SELECT name, version, content_hash FROM dsl_expressions WHERE status = 'published' ORDER BY name, version"
    )
    for expression in published:
        indicators.append({
            "name": expression["name"], "rankable": True,
            "label": f"{expression['name']} (DSL v{expression['version']})",
            "version": expression["version"], "content_hash": expression["content_hash"],
        })
    return {"indicators": indicators, "count": len(indicators)}


class SaveRuleRequest(BaseModel):
    """保存筛选规则请求"""
    name: str
    rule_json: dict[str, Any]
    locked_indicators: dict[str, Any] | None = None
    status: str = "draft"


@router.post("/rules/save")
def save_rule(req: SaveRuleRequest, request: Request) -> dict:
    """保存筛选规则 (PRD §12.2: 规则编辑必须版本化)"""
    sqlite = request.app.state.sqlite
    if len(json.dumps(req.rule_json, ensure_ascii=False).encode("utf-8")) > MAX_RULE_JSON_BYTES:
        raise HTTPException(status_code=400, detail="rule JSON is too large")

    # reports/76 P3-4: 保存时即校验字段名（与 engine.run 运行时校验同口径），
    # 未知字段/排序/结果列在保存时拒绝，不再拖到运行筛选才报错。
    from app.core.screening.engine import validate_rule_fields

    try:
        published_names = {
            row["name"]
            for row in sqlite.query(
                "SELECT name FROM dsl_expressions WHERE status = 'published'"
            )
        }
        validate_rule_fields(req.rule_json, published_names)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    try:
        locks = resolve_rule_indicator_locks(sqlite, req.rule_json, req.locked_indicators or {})
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    with sqlite.transaction() as conn:
        # Check if rule with same name exists
        existing = conn.execute(
            "SELECT MAX(version) as max_version FROM screening_rules WHERE name = ?",
            [req.name],
        ).fetchone()

        if existing and existing[0] is not None:
            version = existing[0] + 1
        else:
            version = 1

        try:
            cursor = conn.execute(
                """INSERT INTO screening_rules (name, version, rule_json, locked_indicators, status)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    req.name,
                    version,
                    json.dumps(req.rule_json, ensure_ascii=False),
                    json.dumps(locks, ensure_ascii=False),
                    # C6修复(报告41): 服务端固定状态机，忽略客户端提交的 status
                    "saved",
                ],
            )
        except Exception:
            raise HTTPException(status_code=409, detail="concurrent rule save conflict; retry")
        rule_id = cursor.lastrowid

    return {"status": "ok", "rule_id": rule_id, "version": version}


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
            "SELECT name, version, content_hash FROM dsl_expressions WHERE status = 'published'"
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
