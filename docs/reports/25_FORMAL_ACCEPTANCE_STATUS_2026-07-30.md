---
title: 正式验收状态（2026-07-30 终版）
status: superseded
category: reports
last-reviewed: 2026-07-30
superseded-by: reports/29_DATA_REBUILD_REPORT_2026-07-31.md
---

# 正式验收状态（2026-07-30 终版）

## 当天完成的自动修复

| 修复项 | 前 | 后 | 方式 |
|--------|----|----|------|
| 分红公告日期（53,877 条） | announcement_date 全 NULL | 全部填充 | CNINFO API 批量 re-fetch，列名修正为 `实施方案公告日期` |
| SZSE 股本（2,892 只） | total_shares/circ_shares 均为 0 | 2,205 只已写入 | AKShare SZSE API 批量 fetch + DuckDB 事务批量 UPDATE |
| XDXR 除权除息 | 0 行 | 183,232 行，覆盖 5,534 只 | TDX 适配器按 50 只/批全量抓取 |
| 只读 CLI 边界 | data diagnose/backup list 触发 schema 写入 | 只读不写 | `initialize=False` 参数拆分 |
| 发行包 AKShare 资源 | 空 profile 启动无股票列表 | calendar.json 等已打入 | PyInstaller `collect_data_files('akshare')` |
| 启动后台化 | 空 profile 在端口 bind 前同步阻塞初始化 | 先 bind 再后台 | 启动维护移入 daemon 线程 |

## 仅剩的 3 个告警

### 1. LINEAGE_INVALID
- **原因**: 2,535,176 条 source_audit 记录缺少原始响应 payload（旧 CSMAR 导入将 `raw_data` 写为 `{}`）
- **能否自动修复**: 不能。需要原始供应商响应文件重建，或授权隔离。
- **建议**: 用户提供原始 CSMAR/Wind 源文件后，在 staging 重导并生成 SHA-256 + archive + field audit。

### 2. UNPUBLISHED_OVERRIDES
- **原因**: 8 条 600519 的 manual_test 历史覆写
- **能否自动修复**: 不能。需数据所有者逐条决定 publish/rollback/keep_draft
- **已生成**: `C:\Users\qhdjxgm\Desktop\override-decisions.csv`

### 3. MINIMUM_DATA_NOT_READY（多子项）
| 子项 | 数量 | 原因 | 可自动 |
|------|------|------|--------|
| raw_history | 1,308 | 无 5 年历史价格 | 可（价格回填） |
| qfq_history | 1,356 | 无 QFQ 历史 | 可（价格回填） |
| price_freshness | 5,532 | 大多数价格停在 2026-07-17 | 可（增量更新） |
| share_capital | 2,310 | 全部 SSE，API 不返回 | **不可**（需授权源） |
| corporate_action | 538 | 无分红记录 | 真实状态 |
| sector_financials | 92 | 银行/券商监管字段 | **不可**（CSMAR 不含） |
| lineage_coverage | 5,534 | 缺字段级来源材料 | 同上 LINEAGE_INVALID |

## 已完成的验收证据

- S1 隔离回归: `289 passed`（`scripts/s1-pytest.ps1`）
- 前端 lint + build + 46 项合约: 全部通过
- `uv lock --locked`: 通过
- wheel 构建: 通过
- 生产依赖漏洞: 0
- PRD §19.1 性能: `10/10 < 5s, avg 165.5ms`（`docs/evidence-performance-20260730.json`）
- EXE UAT: 无 Python PATH 下 CLI schema + Web health/SPA/favicon 均 200（`docs/evidence-release-uat-20260730.json`）
- 正式基线: `docs/evidence-formal-baseline-20260730.json`

## 裁决

**代码层**: 可自动化项已完成。性能、构建、发行入口均通过。

**数据层**: 仍 BLOCK。3 条告警中，8 条覆写可快速审批消除；价格历史/新鲜度可批量更新消除；lineage 和金融监管字段需要授权源文件或接受长期不可用。
