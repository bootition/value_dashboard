---
title: 第七轮红队 F4 修复报告（原生 CLI 导出截断标注，2026-08-03）
status: approved
category: reports
created: 2026-08-03
last-reviewed: 2026-08-03
supersedes: reports/38_SYSTEM_RED_TEAM_ROUND7_2026-08-02.md
---

# 第七轮红队 F4 修复报告（原生 CLI 导出截断标注，2026-08-03）

> 针对 `reports/38`（第七轮系统红队）BLOCK 依据 F4 的修复：
> 原生 `vd screening export_csv` 静默丢弃已持久化的 `truncated` 标记。
> 修复后重跑全量门禁（S1 408 passed、ruff、前端 lint/test/build），
> 正式数据库全程只读且 SHA-256 前后一致。

## 1. F4 修复内容

### 1.1 CLI 导出对齐网页导出（`app/cli/main.py::screening_export_csv`）

- 读取 `confidence_summary["truncated"]`，为表头与每条数据行追加 `_truncated` 列
  （值为 `True`），不再静默丢尾。
- 与网页导出共用同一 CSV 组装逻辑（防再次漂移）：

| 位置 | 内容 |
|---|---|
| `app/web/api/screening.py::_csv_export_header` | 列名 + 元数据列 + 可选 `_truncated` |
| `app/web/api/screening.py::_csv_export_row` | 单元格防护 + 元数据 + 可选 `_truncated=True` |
| `app/cli/main.py::screening_export_csv` | 改为调用上述公共函数，删去重复组装代码 |
| `app/web/api/screening.py::export_csv` | 同步重构为公共函数（行为不变，既有测试覆盖） |

### 1.2 新增真实 CLI 回归测试（`tests/regression/test_research_path_integrity.py`）

| 测试 | 场景 | 断言 |
|---|---|---|
| `test_cli_run_to_cli_export_preserves_truncated_marker` | 10 只匹配、`MAX_RESULT_ROWS=3`：真实 `vd screening run` → `save_result` → `export_csv` | header 含 `_truncated`；数据行全部以 `,True` 结尾；行数 = 上限 3 |
| `test_cli_export_at_limit_has_no_truncation_marker` | 恰好 3 只命中上限（不截断） | header 不含 `_truncated`；行数 = 3（完整输出） |

## 2. 门禁（2026-08-03 独立重跑）

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression` | **408 passed**（184.36s；较第七轮 406 多出 2 个新 F4 测试） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| 前端 lint / node / vitest / build | 通过 / 52 passed / 10 passed / 成功 |
| 正式库 SHA-256（前后一致，S1 preflight 逐字节比对） | DuckDB `741C75BE...`、SQLite `3D41498F...` |

## 3. 裁决

**第七轮 BLOCK 依据 F4 已关闭。** 原生 CLI 导出现可复现截断标记，
与网页导出共用公共组装逻辑，且补齐了全仓此前缺失的 `screening export_csv` 测试。
项目达到「所有核心写操作与运维操作可由 CLI 完成」及 CSV 导出完整溯源的
PRD §12.5/§16.1 要求（就此问题而言）。

## 4. 证据

- `docs/evidence/evidence-redteam-round8-f4fix-gates-20260803.json`
- `docs/evidence/evidence-s1/<最新 run>/hash-evidence.json`（正式库逐字节比对）

## 5. 后续仍开放事项（非本次范围）

`reports/34` §5、`reports/36` §5、`reports/38` §4 所列次要 P2 与运行类缺口
（存量结果截断标记迁移、计数查询成本、P1-B 溯源 strict 不一致、SQLite 明文数据
等）不构成本轮 BLOCK 依据，按 STATUS.md「已知剩余缺口」如实披露。
