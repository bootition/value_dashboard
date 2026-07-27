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


class ScreeningRequest(BaseModel):
    """筛选运行请求"""
    rule: dict[str, Any]               # 规则 JSON (conditions + sort + columns)
    include_st: bool = False
    include_suspended: bool = False
    min_listing_years: int = 1


class SaveResultRequest(BaseModel):
    """保存筛选结果请求"""
    title: str                         # 标题 (必填, PRD SC14)
    note: str | None = None            # 备注 (可选)
    rule_json: dict[str, Any]
    results: list[dict[str, Any]]
    columns: list[str]
    sort: list[dict[str, Any]] | None = None
    data_date: str | None = None
    base_pool_config: dict[str, Any] | None = None


class ExportCsvRequest(BaseModel):
    """导出 CSV 请求"""
    results: list[dict[str, Any]]
    columns: list[str]
    data_date: str = ""


class AddToWatchlistRequest(BaseModel):
    """加入自选列表请求"""
    stock_codes: list[str]
    group: str = "default"
    result_id: int | None = None


@router.post("/run")
async def run_screening(req: ScreeningRequest, request: Request) -> dict:
    """运行筛选 (PRD §12.2 SC8: 手动运行, 以最新数据为准)"""
    from app.core.screening.engine import ScreeningEngine

    engine = ScreeningEngine(duck=request.app.state.duck)
    result = engine.run(
        rule=req.rule,
        include_st=req.include_st,
        include_suspended=req.include_suspended,
        min_listing_years=req.min_listing_years,
    )

    # 为每条结果生成入选解释 (PRD SC13)
    conditions = req.rule.get("conditions", {})
    for stock in result["results"]:
        stock["_entry_explanation"] = engine.generate_entry_explanation(
            stock, conditions
        )

    return result


@router.post("/save")
async def save_result(req: SaveResultRequest, request: Request) -> dict:
    """保存筛选结果 (PRD §12.5 SC14-15)"""
    sqlite = request.app.state.sqlite

    # 生成规则ID (如果规则不存在)
    rule_id = None
    rule_version = 1

    # 保存结果
    with sqlite.transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO screening_results
               (title, note, rule_id, rule_version, data_date,
                result_json, columns_json, sort_json, confidence_summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                req.title,
                req.note,
                rule_id,
                rule_version,
                req.data_date or datetime.now(timezone.utc).isoformat(),
                json.dumps(req.results, ensure_ascii=False, default=str),
                json.dumps(req.columns, ensure_ascii=False),
                json.dumps(req.sort or [], ensure_ascii=False),
                json.dumps({"total": len(req.results)}, ensure_ascii=False),
            ],
        )
        result_id = cursor.lastrowid  # P1-34修复: int类型，与INTEGER PK一致

    return {"status": "ok", "result_id": result_id}
    # P1-34修复: result_id保持int类型（与screening_results.id INTEGER PK一致）


@router.get("/results")
async def list_saved_results(request: Request, limit: int = Query(20, ge=1, le=500)) -> dict:
    """列出已保存的筛选结果"""
    sqlite = request.app.state.sqlite
    rows = sqlite.query(
        "SELECT id, title, note, data_date, created_at "
        "FROM screening_results ORDER BY created_at DESC LIMIT ?",
        [limit],
    )
    return {"results": rows, "count": len(rows)}


@router.get("/results/{result_id}")
async def get_saved_result(result_id: str, request: Request) -> dict:
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
async def export_csv(req: ExportCsvRequest) -> dict:
    """导出 CSV (PRD §12.5 SC16: 含数据日期/规则版本/指标版本/置信度/来源)"""
    results = req.results
    columns = req.columns
    data_date = req.data_date

    if not results:
        raise HTTPException(status_code=400, detail="no results to export")
    if len(results) > 10000:
        raise HTTPException(status_code=400, detail="too many results to export (max 10000)")

    output = io.StringIO()
    writer = csv.writer(output)

    # 表头: 列名 + 溯源信息
    header = columns + ["_data_date", "_entry_explanation"]
    writer.writerow(header)

    # 数据行
    for row in results:
        line = [row.get(col, "") for col in columns]
        line.append(data_date)
        line.append(row.get("_entry_explanation", ""))
        writer.writerow(line)

    csv_content = output.getvalue()
    return {"csv": csv_content, "rows": len(results)}


@router.post("/add_to_watchlist")
async def add_to_watchlist(req: AddToWatchlistRequest, request: Request) -> dict:
    """将筛选结果加入自选列表 (PRD §12.5 SC17)"""
    sqlite = request.app.state.sqlite
    stock_codes = req.stock_codes
    group_name = req.group
    result_id = req.result_id

    if len(stock_codes) > 10000:
        raise HTTPException(status_code=400, detail="too many stocks (max 10000)")

    added = 0
    with sqlite.transaction() as conn:
        for code in stock_codes:
            conn.execute(
                """INSERT OR REPLACE INTO watchlist
                   (stock_code, group_name, source_result_id)
                   VALUES (?, ?, ?)""",
                [code, group_name, result_id],
            )
            added += 1

    return {"status": "ok", "added": added}


@router.get("/indicators")
async def list_available_indicators() -> dict:
    """列出可用的筛选指标"""
    from app.core.screening.engine import SNAPSHOT_COLUMNS, RANKABLE_INDICATORS

    indicators: list[dict] = []
    for col in sorted(SNAPSHOT_COLUMNS):
        indicators.append({
            "name": col,
            "rankable": col in RANKABLE_INDICATORS,
        })
    return {"indicators": indicators, "count": len(indicators)}


class SaveRuleRequest(BaseModel):
    """保存筛选规则请求"""
    name: str
    rule_json: dict[str, Any]
    locked_indicators: dict[str, Any] | None = None
    status: str = "draft"


@router.post("/rules/save")
async def save_rule(req: SaveRuleRequest, request: Request) -> dict:
    """保存筛选规则 (PRD §12.2: 规则编辑必须版本化)"""
    sqlite = request.app.state.sqlite

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

        cursor = conn.execute(
            """INSERT INTO screening_rules (name, version, rule_json, locked_indicators, status)
               VALUES (?, ?, ?, ?, ?)""",
            [
                req.name,
                version,
                json.dumps(req.rule_json, ensure_ascii=False),
                json.dumps(req.locked_indicators or {}, ensure_ascii=False),
                req.status,
            ],
        )
        rule_id = cursor.lastrowid

    return {"status": "ok", "rule_id": rule_id, "version": version}


@router.get("/rules")
async def list_rules(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict:
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
async def get_rule(rule_id: int, request: Request) -> dict:
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
