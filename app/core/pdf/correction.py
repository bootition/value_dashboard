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
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class CorrectionField(BaseModel):
    """校正模板中的单个字段"""
    field_name: str = Field(..., description="标准化字段名, 如 total_assets")
    original_value: float | None = Field(None, description="原始值(可为空)")
    corrected_value: float = Field(..., description="校正后的值")
    unit: Literal["CNY", "ratio", "percent", "count"] = Field(
        "CNY", description="单位: CNY/ratio/percent/count"
    )


class CorrectionTemplate(BaseModel):
    """受控 JSON 校正模板 (PRD §17)

    用户或外部 AI 填写此模板, 系统负责校验和发布。
    """
    announcement_id: str = Field(..., min_length=1, description="CNINFO公告标识")
    pdf_hash: str = Field(..., pattern=r"^[0-9a-fA-F]{64}$", description="对应PDF的SHA256哈希")
    page: int = Field(..., ge=1, description="PDF页码")
    report_period: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="报告期, 如 2025-12-31")
    stock_code: str = Field(..., pattern=r"^\d{6}$", description="股票代码")
    unit: Literal["CNY", "ratio", "percent", "count"] = Field("CNY", description="默认单位")
    reason: str = Field(..., min_length=1, description="校正原因")
    fields: list[CorrectionField] = Field(..., min_length=1, description="拟写入的字段与数值")
    status: str = Field("draft", description="状态: draft/validated/previewed/published")


class CorrectionManager:
    """校正模板管理器

    生命周期 (PRD §17): 草稿→校验→影响预览→确认发布
    """

    def __init__(
        self,
        duck: DuckDBStore | None = None,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
    ) -> None:
        if paths is None and duck is None and sqlite is None:
            from app.core.storage.path_policy import resolve_and_validate_paths
            paths = resolve_and_validate_paths()
        if paths is None and (duck is None or sqlite is None):
            raise PathIsolationError("CorrectionManager requires both stores or validated paths")
        if paths is not None:
            validated = paths.validate()
            duck = duck or DuckDBStore(paths=validated)
            sqlite = sqlite or SQLiteStore(paths=validated)
            if duck.db_path != validated.duckdb_path or sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("CorrectionManager stores do not match injected paths")

        assert duck is not None and sqlite is not None
        self.duck = duck
        self.sqlite = sqlite

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

        if template.get("status_detail") != "draft":
            return {"error": f"模板必须是草稿才能校验 (当前状态: {template.get('status_detail')})"}

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
                if not math.isfinite(val):
                    errors.append(f"字段 {f['field_name']} 的校正值必须是有限数值")
            except (TypeError, ValueError):
                errors.append(f"字段 {f['field_name']} 的校正值不是数字")

        # Check ISO date semantics, not only its string shape.
        rp = template.get("report_period", "")
        try:
            datetime.strptime(rp, "%Y-%m-%d")
        except (TypeError, ValueError):
            errors.append(f"报告期格式不正确: {rp} (期望 YYYY-MM-DD)")

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
        if template.get("status_detail") != "validated":
            return {"error": f"模板必须先校验 (当前状态: {template.get('status_detail')})"}

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

        if template.get("status_detail") != "previewed":
            return {"error": f"模板必须先完成影响预览 (当前状态: {template.get('status_detail')})"}
        if not self._pdf_hash_exists(template["stock_code"], template["pdf_hash"]):
            return {"error": "本地热存储或已验证归档中不存在模板指定的 PDF 哈希"}

        # Write fields and lifecycle state atomically.  A published field with a
        # previewed template is an ambiguous correction that must never escape.
        published_count = 0
        try:
            with self.sqlite.transaction() as conn:
                for f in template["fields"]:
                    conn.execute(
                        """INSERT INTO manual_overrides
                           (stock_code, field_name, report_date, original_value,
                            override_value, reason, correction_template, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'published')""",
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
                self._update_status_in_connection(conn, override_id, "published")
        except Exception as error:
            return {"error": f"校正字段发布失败: {error}"}

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
                    override_value, reason, correction_template, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'draft')""",
                [
                    template.stock_code,
                    "_template",
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
            # The database status is authoritative; JSON is only template content.
            template["status_detail"] = row.get("status", "draft")
            return template
        except json.JSONDecodeError:
            return None

    def _update_status(self, override_id: int, status: str) -> None:
        """更新模板状态 — 直接更新 correction_template JSON 中的 status 字段"""
        rows = self.sqlite.query(
            "SELECT correction_template, status FROM manual_overrides WHERE id = ?",
            [override_id],
        )
        if not rows or not rows[0].get("correction_template"):
            return

        with self.sqlite.transaction() as conn:
            self._update_status_in_connection(conn, override_id, status)
        logger.info(f"模板 {override_id} 状态更新为: {status}")

    def _update_status_in_connection(self, conn: Any, override_id: int, status: str) -> None:
        row = conn.execute(
            "SELECT correction_template, status FROM manual_overrides WHERE id = ?", [override_id]
        ).fetchone()
        if row is None or not row["correction_template"]:
            raise ValueError(f"correction template {override_id} does not exist")
        current = row["status"] or "draft"
        allowed_from = {
            "validated": {"draft"}, "previewed": {"validated"}, "published": {"previewed"},
        }
        if current not in allowed_from.get(status, set()):
            raise ValueError(f"invalid correction lifecycle transition {current} -> {status}")
        template = json.loads(row["correction_template"])
        template["status"] = status
        conn.execute(
            "UPDATE manual_overrides SET correction_template = ?, status = ? WHERE id = ?",
            [json.dumps(template, ensure_ascii=False, default=str), status, override_id],
        )

    def _pdf_hash_exists(self, stock_code: str, pdf_hash: str) -> bool:
        """Require a local hot PDF or checksum-verified archive manifest match."""
        from app.core.pdf.manager import PDFManager

        manager = PDFManager(sqlite=self.sqlite)
        stock_dir = manager.hot_dir / stock_code
        if stock_dir.exists():
            for pdf_path in stock_dir.glob("*.pdf"):
                if self._hash_file(pdf_path) == pdf_hash:
                    return True
        rows = self.sqlite.query(
            "SELECT archive_path, checksum FROM pdf_archive_manifest WHERE stock_code = ? AND checksum = ?",
            [stock_code, pdf_hash],
        )
        return any(
            Path(row["archive_path"]).exists()
            and row["checksum"] == pdf_hash
            and self._hash_file(Path(row["archive_path"])) == pdf_hash
            for row in rows
        )

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
