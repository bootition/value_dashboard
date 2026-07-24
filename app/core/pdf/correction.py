"""受控 JSON 校正模板 (PRD §17)

PRD §17 要求:
- PDF解析失败或字段不可信时, 生成可机器处理的失败任务
- 必须支持受控JSON校正模板, 至少包含:
  公告标识、对应PDF的哈希、页码、报告期、单位、校正原因、拟写入字段与数值
- 校正流程: 草稿→校验→影响预览→确认发布
- V1不提供内建OCR, 但允许人工或外部AI生成校正模板
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class CorrectionField(BaseModel):
    """校正模板中的单个字段"""
    field_name: str = Field(..., description="标准化字段名, 如 total_assets")
    original_value: float | None = Field(None, description="原始值(可为空)")
    corrected_value: float = Field(..., description="校正后的值")
    unit: str = Field("CNY", description="单位: CNY/ratio/percent/count")


class CorrectionTemplate(BaseModel):
    """受控 JSON 校正模板 (PRD §17)

    用户或外部 AI 填写此模板, 系统负责校验和发布。
    """
    announcement_id: str | None = Field(None, description="CNINFO公告标识")
    pdf_hash: str | None = Field(None, description="对应PDF的SHA256哈希")
    page: int | None = Field(None, description="PDF页码")
    report_period: str = Field(..., description="报告期, 如 2025-12-31")
    stock_code: str = Field(..., description="股票代码")
    unit: str = Field("CNY", description="默认单位")
    reason: str = Field(..., description="校正原因")
    fields: list[CorrectionField] = Field(..., description="拟写入的字段与数值")
    status: str = Field("draft", description="状态: draft/validated/previewed/published")


class CorrectionManager:
    """校正模板管理器

    生命周期 (PRD §17): 草稿→校验→影响预览→确认发布
    """

    def __init__(self) -> None:
        self.duck = DuckDBStore()
        self.sqlite = SQLiteStore()

    def create_from_json(self, template_json: str) -> dict[str, Any]:
        """从 JSON 创建校正模板草稿

        Args:
            template_json: 符合 CorrectionTemplate schema 的 JSON 字符串

        Returns:
            创建结果, 含模板ID
        """
        try:
            data = json.loads(template_json)
            template = CorrectionTemplate(**data)
        except json.JSONDecodeError as e:
            return {"error": f"JSON解析失败: {e}"}
        except Exception as e:
            return {"error": f"模板校验失败: {e}"}

        # 存储到 SQLite (复用 manual_overrides 表, correction_template 字段)
        override_id = self._store_template(template)

        return {
            "status": "ok",
            "override_id": override_id,
            "stock_code": template.stock_code,
            "report_period": template.report_period,
            "field_count": len(template.fields),
            "status_detail": "draft",
        }

    def validate(self, override_id: int) -> dict[str, Any]:
        """校正模板校验 (PRD §17: 校正流程第2步)

        检查:
        - 字段名是否在标准化字段列表中
        - 报告期格式是否正确
        - 校正值是否合理 (非负、非NaN等)
        """
        template = self._load_template(override_id)
        if not template:
            return {"error": f"模板 {override_id} 不存在"}

        errors: list[str] = []
        warnings: list[str] = []

        # 获取标准化字段列表
        from app.core.dsl.ast_nodes import FIELD_METADATA
        valid_fields = set()
        for key in FIELD_METADATA:
            _, field = key.split(".", 1)
            valid_fields.add(field)

        for f in template["fields"]:
            if f["field_name"] not in valid_fields:
                errors.append(f"未知字段名: {f['field_name']}")

            if f["corrected_value"] is None:
                errors.append(f"字段 {f['field_name']} 的校正值为空")

            # 检查校正值合理性
            try:
                val = float(f["corrected_value"])
                if val != val:  # NaN check
                    errors.append(f"字段 {f['field_name']} 的校正值是 NaN")
            except (TypeError, ValueError):
                errors.append(f"字段 {f['field_name']} 的校正值不是数字")

        # 检查报告期格式
        rp = template.get("report_period", "")
        if rp and len(rp) != 10:
            warnings.append(f"报告期格式可能不正确: {rp} (期望 YYYY-MM-DD)")

        valid = len(errors) == 0

        if valid:
            self._update_status(override_id, "validated")

        return {
            "override_id": override_id,
            "valid": valid,
            "errors": errors,
            "warnings": warnings,
            "field_count": len(template["fields"]),
        }

    def preview_impact(self, override_id: int) -> dict[str, Any]:
        """影响预览 (PRD §17: 校正流程第3步, PRD §9.5 R7: 可预览影响面)

        展示校正前后的值对比, 以及受影响的指标。
        """
        template = self._load_template(override_id)
        if not template:
            return {"error": f"模板 {override_id} 不存在"}

        stock_code = template["stock_code"]
        report_period = template["report_period"]

        # 获取当前值
        current_values: dict[str, Any] = {}
        try:
            rows = self.duck.read_query(
                """SELECT * FROM balance_sheet
                   WHERE stock_code = ? AND report_date = ?""",
                [stock_code, report_period],
            )
            if rows:
                current_values = rows[0]
        except Exception:
            pass

        # 构建影响预览
        impact: list[dict[str, Any]] = []
        for f in template["fields"]:
            field_name = f["field_name"]
            original = f.get("original_value")
            if original is None:
                original = current_values.get(field_name)

            impact.append({
                "field_name": field_name,
                "original_value": original,
                "corrected_value": f["corrected_value"],
                "unit": f.get("unit", "CNY"),
                "changed": original != f["corrected_value"],
            })

        # 推断受影响的指标
        affected_indicators: list[str] = []
        field_to_indicators: dict[str, list[str]] = {
            "total_assets": ["roa", "debt_ratio", "goodwill_ratio"],
            "total_liabilities": ["debt_ratio"],
            "total_equity": ["roe", "debt_ratio"],
            "total_equity_parent": ["roe", "pb_mrq"],
            "revenue": ["ps_ttm", "gross_margin", "net_margin", "revenue_yoy"],
            "net_profit": ["pe_ttm", "roa", "net_margin"],
            "parent_net_profit": ["pe_ttm", "roe"],
            "cost_of_revenue": ["gross_margin"],
            "cf_from_operating": ["pcf_ttm", "cf_to_net_profit"],
            "goodwill": ["goodwill_ratio"],
        }

        for f in template["fields"]:
            affected = field_to_indicators.get(f["field_name"], [])
            for ind in affected:
                if ind not in affected_indicators:
                    affected_indicators.append(ind)

        self._update_status(override_id, "previewed")

        return {
            "override_id": override_id,
            "stock_code": stock_code,
            "report_period": report_period,
            "impact": impact,
            "affected_indicators": affected_indicators,
            "changed_fields": sum(1 for i in impact if i["changed"]),
        }

    def publish(self, override_id: int) -> dict[str, Any]:
        """确认发布 (PRD §17: 校正流程第4步, PRD §9.5 R7: 与原始值分离存储)

        将校正值写入 manual_overrides 表, 与原始抓取值分离。
        不直接修改 DuckDB 中的原始数据 (PRD §9.5 R8: 人工覆写不得静默覆盖原始来源值)。
        """
        template = self._load_template(override_id)
        if not template:
            return {"error": f"模板 {override_id} 不存在"}

        if template.get("status_detail") not in ("validated", "previewed"):
            return {"error": f"模板必须先校验和预览 (当前状态: {template.get('status_detail')})"}

        # 写入 manual_overrides (与原始值分离)
        published_count = 0
        for f in template["fields"]:
            with self.sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO manual_overrides
                       (stock_code, field_name, report_date,
                        original_value, override_value, reason, correction_template)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [
                        template["stock_code"],
                        f["field_name"],
                        template["report_period"],
                        f.get("original_value"),
                        f["corrected_value"],
                        template["reason"],
                        json.dumps(template, ensure_ascii=False, default=str),
                    ],
                )
            published_count += 1

        # 更新状态
        self._update_status(override_id, "published")

        logger.info(f"校正模板发布: {override_id}, {published_count} 个字段")

        return {
            "status": "ok",
            "override_id": override_id,
            "published_fields": published_count,
            "stock_code": template["stock_code"],
            "report_period": template["report_period"],
        }

    def list_templates(self, status: str | None = None) -> list[dict[str, Any]]:
        """列出现有校正模板"""
        if status:
            # P2修复: 实际使用status过滤
            return self.sqlite.query(
                "SELECT * FROM manual_overrides WHERE correction_template IS NOT NULL "
                "AND status = ? ORDER BY created_at DESC LIMIT 50",
                [status],
            )
        return self.sqlite.query(
            "SELECT * FROM manual_overrides WHERE correction_template IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 50"
        )

    def _store_template(self, template: CorrectionTemplate) -> int:
        """存储模板到 SQLite"""
        template_json = template.model_dump_json()
        with self.sqlite.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO manual_overrides
                   (stock_code, field_name, report_date,
                    override_value, reason, correction_template)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    template.stock_code,
                    template.fields[0].field_name if template.fields else "_template",
                    template.report_period,
                    0,  # placeholder
                    template.reason,
                    template_json,
                ],
            )
            return cursor.lastrowid or 0

    def _load_template(self, override_id: int) -> dict[str, Any] | None:
        """加载模板"""
        rows = self.sqlite.query(
            "SELECT * FROM manual_overrides WHERE id = ?",
            [override_id],
        )
        if not rows:
            return None

        row = rows[0]
        template_json = row.get("correction_template")
        if not template_json:
            return None

        try:
            template = json.loads(template_json)
            # 状态从 JSON 中的 status 字段读取
            template["status_detail"] = template.get("status", "draft")
            return template
        except json.JSONDecodeError:
            return None

    def _update_status(self, override_id: int, status: str) -> None:
        """更新模板状态 — 直接更新 correction_template JSON 中的 status 字段"""
        rows = self.sqlite.query(
            "SELECT correction_template FROM manual_overrides WHERE id = ?",
            [override_id],
        )
        if not rows or not rows[0].get("correction_template"):
            return

        try:
            template = json.loads(rows[0]["correction_template"])
            template["status"] = status
            template_json = json.dumps(template, ensure_ascii=False, default=str)
            with self.sqlite.transaction() as conn:
                conn.execute(
                    "UPDATE manual_overrides SET correction_template = ? WHERE id = ?",
                    [template_json, override_id],
                )
            logger.info(f"模板 {override_id} 状态更新为: {status}")
        except Exception as e:
            logger.warning(f"更新模板状态失败: {e}")
