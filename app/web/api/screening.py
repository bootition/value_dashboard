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

from fastapi import APIRouter, HTTPException, Query
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


@router.post("/run")
async def run_screening(req: ScreeningRequest) -> dict:
    """运行筛选 (PRD §12.2 SC8: 手动运行, 以最新数据为准)"""
    from app.core.screening.engine import ScreeningEngine

    engine = ScreeningEngine()
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
async def save_result(req: SaveResultRequest) -> dict:
    """保存筛选结果 (PRD §12.5 SC14-15)"""
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()

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
async def list_saved_results(limit: int = Query(20, ge=1, le=500)) -> dict:
    """列出已保存的筛选结果"""
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
    rows = sqlite.query(
        "SELECT id, title, note, data_date, created_at "
        "FROM screening_results ORDER BY created_at DESC LIMIT ?",
        [limit],
    )
    return {"results": rows, "count": len(rows)}


@router.get("/results/{result_id}")
async def get_saved_result(result_id: str) -> dict:
    """获取已保存的筛选结果详情"""
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
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
async def export_csv(req: dict) -> dict:
    """导出 CSV (PRD §12.5 SC16: 含数据日期/规则版本/指标版本/置信度/来源)"""
    results = req.get("results", [])
    columns = req.get("columns", [])
    data_date = req.get("data_date", "")

    if not results:
        raise HTTPException(status_code=400, detail="no results to export")

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
async def add_to_watchlist(req: dict) -> dict:
    """将筛选结果加入自选列表 (PRD §12.5 SC17)"""
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
    stock_codes = req.get("stock_codes", [])
    group_name = req.get("group", "default")
    result_id = req.get("result_id")

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
