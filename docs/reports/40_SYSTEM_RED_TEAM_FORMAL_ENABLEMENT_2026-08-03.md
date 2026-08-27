---
title: 第八轮系统红队正式启用复审报告（F4 修复后独立验证，2026-08-03）
status: approved
category: reports
created: 2026-08-03
last-reviewed: 2026-08-03
supersedes: reports/39_SYSTEM_RED_TEAM_ROUND7_F4_FIX_2026-08-03.md
---

# 第八轮系统红队正式启用复审报告（F4 修复后独立验证，2026-08-03）

> 独立复核 `reports/39` / `e16c56f` 对第七轮 F4 的修复，不信任修复报告结论。
> 直接攻击此前失败的原生 CLI 导出路径，检索所有 CSV 导出入口，重跑隔离回归、
> 前端门禁、锁文件检查与完整发行构建/真实启动 smoke；正式库全程只读。
>
> **裁决：PASS（达到正式启用标准）。** 未发现未缓解 P0/P1；此前 F4 的原生 CLI
> 导出旁路已关闭，网页与 CLI 导出同源，正式安装包可实际构建、启动且不携带正式数据。

## 1. F4 独立对抗验证

### 1.1 实现与入口完整性

- `app/web/api/screening.py:30-66` 集中定义 `_csv_export_header`、`_csv_export_row`。
  两者负责 `_truncated` 的条件性 header/行标注。
- 网页 `/api/screening/export_csv` 在 332-340 行调用同一函数。
- 原生 `vd screening export_csv` 在 `app/cli/main.py:1353,1392-1401` 导入并调用同一函数，
  从已保存 `confidence_summary` 读取 `truncated`。
- 全仓 `app/` 检索确认仅有上述网页与 CLI 两个 CSV 导出入口，无遗留重复组装分支。

### 1.2 复现此前攻击

在仓库外临时 DuckDB/SQLite 隔离库，以真实 Typer 命令执行：

1. 构造 10 只匹配股票，临时设置 `MAX_RESULT_ROWS=3`。
2. 执行真实 `screening run`：返回 `truncated=true,total=10`。
3. 执行真实 `screening save_result`。
4. 执行真实 `screening export_csv`。

本轮结果：CSV 表头包含 `_truncated`。此前 F4 的同一攻击由「无标记」变为「有标记」。

新增测试也非模拟：`tests/regression/test_research_path_integrity.py:410-497` 分别覆盖
超上限（header 与每个数据行带 `_truncated=True`）及恰好上限（不出现该列）的真实
CLI run -> save -> export 流程。

## 2. 正式启用门禁（本会话独立重跑）

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression` | **408 passed**（167.29s） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| `uv lock --locked` | 通过（80 packages） |
| 前端 lint / node / vitest / build | 通过 / 52 passed / 10 passed / 成功 |
| `scripts/build-release.ps1` | **通过**：重新执行 S1/npm ci/lint/test/build/PyInstaller，真实发行 exe 启动 `/api/health` smoke 成功，发行包无 `data/` 正式库 |
| 正式库 SHA-256（前后一致） | DuckDB `741C75BE...`、SQLite `3D41498F...` |
| git | 审查写文档前干净 |

## 3. 第一性原理判断

正式启用需要同时满足：

1. 研究与导出结果不静默丢失关键事实；
2. CLI 和网页支持路径行为一致；
3. 正式库不被测试或发行验证污染；
4. 发行包可构建、可启动、可服务，且不携带用户正式数据；
5. 自动化回归能保护已发现的高风险路径。

本次 F4 修复以共用 CSV 组装函数消除了 CLI/网页分叉；独立攻击与两类边界测试证明
截断/未截断语义均正确。S1 哈希保护、全量测试、锁定依赖、前端产物和真正的 PyInstaller
启动 smoke 均通过，因而上述条件全部满足。

## 4. 裁决

**PASS（可正式启用）。**

适用于 PRD 定义的单用户、本地 Windows、localhost A 股研究工具范围。无 P0/P1
阻断项仍开放；此前所有发布级 BLOCK 项均已用回归测试和独立复核关闭。

## 5. 诚实披露的非阻断风险

- 存量保存结果的截断标记无法从恰好 5,000 行的历史数据可靠回推；不伪造。
- 免费源覆盖/时效、920305 新股、金融监管字段、早期 CSMAR lineage 等数据缺口按
  `docs/STATUS.md` 如实暴露。
- 次要 P2：计数查询额外成本、适配器静默截断时的 provenance confidence 不一致、
  备份/恢复与更新 runbook 缺口、静态个性化数据加密口径等；均已披露，未形成当前
  单用户本地研究流程的静默错误或不可恢复阻断。

## 6. 证据

- `docs/evidence/evidence-redteam-round8-formal-gates-20260803.json`
- `docs/evidence/evidence-redteam-round8-f4-attack-20260803.json`
- `.planning/2026-08-03-system-red-team-round8/`
