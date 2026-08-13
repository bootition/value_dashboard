---
title: 用户层体验方案实施报告（A/C/D/E/U2/U6 完成）
status: approved
category: reports
created: 2026-08-13
last-reviewed: 2026-08-13
supersedes: null
---

# 用户层体验方案实施报告（reports/79）

> 承接 `reports/78`（用户层体验评估与候选方案 A-F）。用户决策：除方案 F
> （常驻托盘，远期）外全部实施。本报告记录实施内容、门禁证据与实测数据。

## 一、实施清单

### 方案 A：筛选更新窗口快照口径（用户决策，PRD §12.2 已修订）

- 写锁活跃（自动更新中）时 `/api/screening/*` 门禁不再 409 禁用：
  - 引擎只读原子替换的 `indicator_snapshot` 与财务表（不直读 raw 价格），
    快照口径下全市场排名/分位内部一致；
  - 快照完全缺失时仍 409 `minimum_data_not_ready` 兜底。
- 标注链路：运行响应 `auto_update_in_progress` + `data_as_of`（快照价格日）；
  `confidence_summary` 持久化两项；网页与 CLI 导出 CSV 在快照口径运行时
  追加 `_data_as_of`、`_auto_update_in_progress` 元列（F4 同源一致）。
- 前端：运行按钮在更新窗口内保持可用（`auto-update` 状态联动），结果区
  显示"本次结果基于最近完整快照（数据截至 xx）"横幅，状态标签显示
  "更新中（快照口径）"。
- PRD §12.2 SC8 与 02 技术约束 SC8 同步修订；PRD 修订记录 2026-08-13。

### 方案 C：启动提速（实测 8~12s → 0.9s）

- 根因定位：`init_all_schema` 在 10GB DuckDB 上执行幂等 DDL 实测 **5.01s**
  （其余启动环节合计 0.1s）。
- 修复：`init_all_schema(skip_if_current=True)`——SQLite 与 DuckDB 的
  `schema_migrations` 均为最新版本（`SQLITE_SCHEMA_VERSION=15` /
  `DUCKDB_SCHEMA_VERSION=10`）时跳过全部 DDL；版本不符或探测失败回落
  完整初始化。**纪律要求**：迁移新增必须递增版本常量。
- U5：浏览器在 uvicorn 绑定后打开（2s 定时器），消除页面先于服务就绪竞态。

### 方案 D：打包 exe 重建与实测

- `build-release.ps1` 完整执行（内嵌 S1 门禁 **590 passed** + 前端门禁 +
  PyInstaller）→ `dist/value-dashboard/`（exe 19.4MB + `_internal/` +
  start.bat/vd.bat）。
- 真实 exe smoke（空库、与 start.bat 相同环境变量）：**health 200 仅 1.2s**。
- smoke 产生的空 data 目录已清理。

### 方案 E：start.bat 体验兜底

- 源模式与打包模式均提示："服务运行中，关闭本窗口将停止软件；停止方式：
  关窗或 Ctrl+C"。注意 CMD 块内 echo 不得含括号（既有测试约束）。

### U2：国债卡片横幅

- `TreasuryComparisonCard` 在 `auto_update_in_progress` 时显示 info 横幅
  （与个股指标、自选页一致）；类型 `TreasuryComparisonResponse` 同步。

### U6：筛选完整链路真人走查（更新窗口内实测）

- 实测链路：保存规则 → 运行（**350 条，8.1s**，`auto_update_in_progress=true`、
  `data_as_of=2026-08-11`）→ 保存结果 → 导出 CSV（表头含 `_data_as_of`、
  `_auto_update_in_progress`）→ 加入自选 3 只 → 自选分组可见。
- 坏字段规则保存被 400 拒绝（P3-4 前置校验生效）。
- 测试数据已清理（规则/结果/自选/run 全部 0 残留）。

## 二、门禁与实测证据

| 项 | 结果 |
|---|---|
| S1 完整回归（build-release 内嵌） | **590 passed**（新增快照口径 2 项 + schema 快速路径 3 项；5 处既有测试契约随新功能更新：start.bat echo 括号、init_all_schema 签名、门禁 lambda 返回 dict） |
| ruff | 全绿 |
| 前端 | lint 全绿；55 node + 41 vitest（新增快照口径组件测试 1 项）；build 通过 |
| 源模式启动 | **health 200 仅 0.9s**（修复前 8~12s） |
| exe 启动 | **health 200 仅 1.2s**（空库 smoke） |
| 更新窗口内筛选 | 运行 8.1s 成功 + 快照标注（修复前 409 禁用约 70 分钟） |
| U6 走查后清理 | rules/results/watchlist/orphan_runs 全部 0 |

## 三、新发现（本轮，待用户议）

1. **股息率条件值域口径**：`dividend_yield` 在快照中为小数（如 0.0529 =
   5.29%）。筛选条件输入 `dividend_yield > 2`（用户直觉 2%）实际筛的是
   200%，结果为 0 条。前端结果显示用 `fmtPct` 乘 100 显示，但**条件输入
   无单位换算或提示**。建议：条件编辑对百分比字段显示单位后缀/换算提示，
   或输入时按百分数换算。属 P3 级 UX 口径问题，暂不改（需产品确认）。
2. exe 发布形态建议：正式分发时把 `data/` 与 exe 同目录放置即可复用现库。

## 四、正式库收尾

- 价格 08-12 续传在多次服务运行中持续推进（有界、断点续传，无畸形行）。
- 测试数据零残留；`dist/value-dashboard/data` 已清理。
