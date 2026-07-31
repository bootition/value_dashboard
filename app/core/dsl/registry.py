"""DSL 版本化注册表 — 在 SQLite 中管理已发布表达式

PRD §11.5:
- 生命周期: 草稿→校验→单股预览→小样本预览→发布
- 已发布版本不可变更, 修改必须生成新版本
- 依赖必须版本锁定, 拒绝循环依赖
- 每个已发布表达式必须带中文描述与方向定义
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from app.core.storage.sqlite_store import SQLiteStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.dsl.ast_nodes import INDICATOR_METADATA

logger = logging.getLogger(__name__)

# 表达式状态
STATUS_DRAFT = "draft"
STATUS_VALIDATED = "validated"
STATUS_SINGLE_PREVIEWED = "single_previewed"
STATUS_PREVIEWED = "previewed"
STATUS_PUBLISHED = "published"
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def validate_expression_identifier(name: str) -> str:
    """Allow only identifiers that are safe as persisted SQL aliases."""
    if not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name):
        raise ValueError("expression name must match [a-z][a-z0-9_]{0,63}")
    if name in INDICATOR_METADATA:
        raise ValueError("expression name conflicts with a built-in indicator")
    return name


class ExpressionRegistry:
    """版本化表达式注册表"""

    def __init__(
        self,
        sqlite: SQLiteStore | None = None,
        *,
        paths: DatabasePathSet | None = None,
    ) -> None:
        if sqlite is None and paths is None:
            raise PathIsolationError("ExpressionRegistry requires a SQLite store or validated paths")
        if paths is not None:
            validated = paths.validate()
            sqlite = sqlite or SQLiteStore(paths=validated)
            if sqlite.db_path != validated.sqlite_path:
                raise PathIsolationError("ExpressionRegistry store does not match injected paths")

        assert sqlite is not None
        self.sqlite = sqlite

    def create(
        self,
        name: str,
        expression: str,
        description: str = "",
        direction: str = "none",
    ) -> dict[str, Any]:
        """创建新的表达式草稿 (PRD §11.5 DL12: 生命周期起点)

        Args:
            name: 表达式名称 (英文标识符)
            expression: DSL 表达式文本
            description: 中文描述 (PRD §11.5 DL12)
            direction: 方向定义 higher_is_better / lower_is_better / none

        Returns:
            创建结果, 包含 id 和版本号
        """
        validate_expression_identifier(name)
        # 确定版本号
        existing = self.sqlite.query(
            "SELECT MAX(version) as max_ver FROM dsl_expressions WHERE name = ?",
            [name],
        )
        next_version = 1
        if existing and existing[0]["max_ver"]:
            next_version = existing[0]["max_ver"] + 1

        with self.sqlite.transaction() as conn:
            conn.execute(
                """INSERT INTO dsl_expressions
                   (name, version, expression_text, status, description, direction,
                    historical_capable)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [name, next_version, expression, STATUS_DRAFT,
                 description, direction, False],
            )

        logger.info(f"创建表达式草稿: {name} v{next_version}")
        return {"name": name, "version": next_version, "status": STATUS_DRAFT}

    def validate(self, name: str, version: int, ast_json: str,
                 historical_capable: bool) -> dict[str, Any]:
        """更新表达式状态为已校验"""
        self._update_status(name, version, STATUS_VALIDATED,
                            {"ast_json": ast_json, "historical_capable": historical_capable},
                            allowed_from={STATUS_DRAFT})
        return {"name": name, "version": version, "status": STATUS_VALIDATED}

    def preview_single(self, name: str, version: int) -> dict[str, Any]:
        """记录已完成单股预览。"""
        self._update_status(
            name, version, STATUS_SINGLE_PREVIEWED, allowed_from={STATUS_VALIDATED}
        )
        return {"name": name, "version": version, "status": STATUS_SINGLE_PREVIEWED}

    def preview(self, name: str, version: int) -> dict[str, Any]:
        """记录已完成小样本预览。"""
        self._update_status(
            name, version, STATUS_PREVIEWED, allowed_from={STATUS_SINGLE_PREVIEWED}
        )
        return {"name": name, "version": version, "status": STATUS_PREVIEWED}

    def publish(self, name: str, version: int) -> dict[str, Any]:
        """发布表达式 (PRD §11.5 DL14: 已发布版本不可变更)

        发布后表达式不可修改, 修改必须创建新版本。
        """
        # 生成内容哈希用于版本锁定
        row = self.sqlite.query(
            "SELECT expression_text, ast_json FROM dsl_expressions WHERE name=? AND version=?",
            [name, version],
        )
        if not row:
            return {"error": "expression not found"}

        content = f"{name}:{version}:{row[0]['expression_text']}:{row[0].get('ast_json', '')}"
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        self._update_status(name, version, STATUS_PUBLISHED,
                            {"content_hash": content_hash}, allowed_from={STATUS_PREVIEWED})

        logger.info(f"发布表达式: {name} v{version} (hash={content_hash[:8]})")
        return {"name": name, "version": version, "status": STATUS_PUBLISHED,
                "content_hash": content_hash}

    def get(self, name: str, version: int | None = None) -> dict[str, Any] | None:
        """获取表达式"""
        if version:
            rows = self.sqlite.query(
                "SELECT * FROM dsl_expressions WHERE name=? AND version=?",
                [name, version],
            )
        else:
            rows = self.sqlite.query(
                "SELECT * FROM dsl_expressions WHERE name=? ORDER BY version DESC LIMIT 1",
                [name],
            )
        return rows[0] if rows else None

    def list_expressions(self, status: str | None = None) -> list[dict[str, Any]]:
        """列出表达式"""
        if status:
            return self.sqlite.query(
                "SELECT * FROM dsl_expressions WHERE status=? ORDER BY name, version",
                [status],
            )
        return self.sqlite.query(
            "SELECT * FROM dsl_expressions ORDER BY name, version"
        )

    def list_published(self) -> list[dict[str, Any]]:
        """列出已发布的表达式 (最新版本)"""
        return self.sqlite.query(
            """SELECT * FROM dsl_expressions WHERE status = ?
               AND version = (SELECT MAX(version) FROM dsl_expressions e2
                              WHERE e2.name = dsl_expressions.name AND e2.status = ?)
               ORDER BY name""",
            [STATUS_PUBLISHED, STATUS_PUBLISHED],
        )

    def add_dependency(
        self, name: str, version: int,
        dep_name: str, dep_version: int,
    ) -> None:
        """添加依赖关系 (PRD §11.5 DL15: 版本锁定)"""
        with self.sqlite.transaction() as conn:
            expr_row = conn.execute(
                "SELECT id, status FROM dsl_expressions WHERE name=? AND version=?",
                [name, version],
            ).fetchone()
            if not expr_row:
                return
            if expr_row["status"] != STATUS_VALIDATED:
                raise ValueError("dependencies can only be recorded for validated expressions")

            dep_row = conn.execute(
                "SELECT id FROM dsl_expressions WHERE name=? AND version=?",
                [dep_name, dep_version],
            ).fetchone()
            if not dep_row:
                return
            conn.execute(
                "INSERT OR REPLACE INTO dsl_dependencies (expression_id, depends_on_id, depends_on_version) VALUES (?, ?, ?)",
                [expr_row["id"], dep_row["id"], dep_version],
            )

    def check_circular(self, name: str, version: int) -> bool:
        """检查是否存在循环依赖"""
        # 简化实现: 检查直接依赖链
        return self._dfs_cycle(name, version, set())

    def _dfs_cycle(self, name: str, version: int, active_path: set[int]) -> bool:
        rows = self.sqlite.query(
            "SELECT id FROM dsl_expressions WHERE name=? AND version=?",
            [name, version],
        )
        if not rows:
            return False
        expr_id = rows[0]["id"]

        if expr_id in active_path:
            return True  # 循环
        active_path.add(expr_id)

        deps = self.sqlite.query(
            "SELECT e2.name, d.depends_on_version FROM dsl_dependencies d "
            "JOIN dsl_expressions e2 ON d.depends_on_id = e2.id "
            "WHERE d.expression_id = ?",
            [expr_id],
        )
        for dep in deps:
            if self._dfs_cycle(dep["name"], dep["depends_on_version"], active_path):
                return True
        active_path.remove(expr_id)
        return False

    def _update_status(
        self, name: str, version: int, status: str,
        extra: dict[str, Any] | None = None,
        *,
        allowed_from: set[str],
    ) -> None:
        """Advance an expression only from the lifecycle stage that precedes it."""
        sets = ["status = ?"]
        params: list[Any] = [status]

        if extra:
            for k, v in extra.items():
                # P1-23修复: content_hash存入独立列，不再追加到ast_json
                if k == "content_hash":
                    sets.append("content_hash = ?")
                    params.append(v)
                else:
                    sets.append(f"{k} = ?")
                    params.append(v)

        placeholders = ", ".join("?" for _ in allowed_from)
        params.extend([name, version, *sorted(allowed_from)])

        with self.sqlite.transaction() as conn:
            cursor = conn.execute(
                f"""UPDATE dsl_expressions SET {', '.join(sets)}
                    WHERE name=? AND version=? AND status IN ({placeholders})""",
                params,
            )
            if cursor.rowcount != 1:
                raise ValueError(f"invalid DSL lifecycle transition to {status}")
