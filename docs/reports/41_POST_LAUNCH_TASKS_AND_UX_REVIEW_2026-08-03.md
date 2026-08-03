---
title: 正式启用后任务清单与用户视角可用性审查报告（2026-08-03）
status: approved
category: reports
created: 2026-08-03
last-reviewed: 2026-08-03
---

# 正式启用后任务清单与用户视角可用性审查报告（2026-08-03）

> 承接 `reports/40` 的 **PASS（可正式启用）** 裁决：本报告回答「下一步还要
> 做什么」，并以**首次使用者视角**对四个页面与整体框架做可用性/易用性审查。
> 结论：**无 P0/P1 阻断项，PASS 维持不变**；以下均为增强任务（P2/P3），
> 按优先级与工作量排入后续迭代。

---

## A. 结论摘要

1. 正式启用裁决（`reports/40`）**维持不变**：本报告不改变 PASS。
2. 用户视角审查共 15 项发现，全部为可用性增强（最高价值两项：首次上手引导、
   单位/口径一致性），不构成使用阻断。
3. 后续事项合并为三组：**运维与发布**、**已知 P2 代码项**、**用户视角 UX 改进**，
   每项给出优先级、位置/依据、建议动作。

---

## B. 后续任务清单

### B1 运维与发布（建议先做）

| # | 事项 | 优先级 | 位置/依据 | 建议动作 |
|---|---|---|---|---|
| O1 | PRD §19.1 目标主机性能验收仪式 | P1 | `docs/decisions/01` §19.1；`scripts/screening_performance_acceptance.py` | 在用户正式主机上按统一夹具执行预热+10 次，记录 CPU/内存/磁盘与 ≥9/10 <5s 结果 |
| O2 | 补齐运行手册 runbook | P1 | `docs/runbooks/` 仅有 s0 | 新增：备份/恢复、自动更新与重试、数据重建、build-release/S1 使用说明 |
| O3 | 存量结果截断标记迁移决策 | P2 | `reports/36` §5、`reports/38` §4 | 决定是否/如何对既有 `screening_results` 回填 `truncated`；无法可靠回推的如实披露 |
| O4 | SQLite 个性化数据加密口径决定 | P2 | PRD §18.3；`reports/34` §5 | 所有者明示静态个性化数据（规则/自选/保存结果）是否必须加密；落实或文档化 |
| O5 | Node engines 声明与文档化 | P2 | `frontend/package.json`（无 engines）；Vite 8/`--experimental-strip-types` 要求 Node ≥20.19/22.6 | 增加 `engines` 并写入 README/AGENTS |
| O6 | `chain-finalize.ps1` 证据目录治理 | P2 | `scripts/chain-finalize.ps1:11-12` | 硬编码正式路径改参数化；诊断写入 `docs/evidence/` 治理目录 |
| O7 | 自动数据更新 + CSRC 行业分类实施 | 进行中 | `.planning/2026-07-31-automatic-data-updates/`（Phase A–G） | 按既有实施计划继续 |

### B2 已知 P2 代码项（多数已有出处，可合并到迭代）

| # | 事项 | 优先级 | 位置/依据 | 建议动作 |
|---|---|---|---|---|
| C1 | 草稿 409 后自动保存永久停用 | P2 | `ScreeningPage.vue:180-193`；`reports/34` §5 | 409 后提示刷新，或改为恢复最新服务端草稿并继续自动保存 |
| C2 | strict-only 显示/持久化一致 | P2 | `ScreeningResultsPanel.vue:82-95`；`reports/34` §5 | 切换 strict 时重跑或持久化前按当前开关重算，保证保存/导出与 UI 一致 |
| C3 | 打包版恢复指引命令错误 | P2 | `DataTraceability.vue:84-86`；`reports/34` §5 | 打包环境显示 `vd.bat`/exe 命令而非 `python -m app.cli.main` |
| C4 | 写令牌重启后需刷新 | P2 | `frontend/src/http.ts:12-16`；`reports/34` §5 | 401 时自动重新拉取令牌并重放请求 |
| C5 | watchlist 股票代码校验 | P2 | `WatchlistPage.vue:147-158`；后端 `watchlist.py`；`reports/34` §5 | 前后端校验 `^\d{6}$` + 池内存在性，给出明确错误 |
| C6 | `save_rule` 接受客户端 `status` | P2 | `screening.py:444-492`；`reports/34` §5 | 服务端固定状态机，忽略客户端 status |
| C7 | 草稿 PUT 大小上限 | P2 | `screening.py:211-229` | 增加字节上限并 413/400 |
| C8 | 日志/plan/缺失表 GC | P2 | `reports/34` §5 | 增加 job_logs/plans/missing 清理策略 |
| C9 | 更新锁被拒时状态误记 failed | P2 | `auto_update.py:261-277` | 改为 `skipped` |
| C10 | `max_stocks` 无 ORDER BY | P2 | `update.py:739-741` | 加稳定排序 |
| C11 | `_csv_cell` 公式注入防护增强 | P2 | `screening.py:24-27`；`reports/34` §5 | 防护 ` =cmd()` 等前导空白变体 |
| C12 | DSL 草稿被依赖时删除 500 | P2 | `dsl.py:106`；`reports/34` §5 | 捕获 IntegrityError 返回 409 与依赖信息 |
| C13 | 绝对路径泄露 | P2 | `stock_detail.py:735-739`、`main.py:232-243`；`reports/34` §5 | 返回相对/脱敏路径 |
| C14 | 计数查询成本×2 | P2 | `engine.py:220`；`reports/36` §5 | 复用窗口查询或物化计数，保持 SLA |
| C15 | P1-B 溯源 strict 不一致 | P2 | `init.py:284`；`reports/36` §5 | 部分/截断批次按 approximate 记录 provenance |
| C16 | 过期 plan 从不清理 | P2 | `reports/34` §5 | 定期清理过期 plan |

### B3 用户视角 UX 改进（本报告新增）

| # | 事项 | 优先级 | 位置/依据 | 建议动作 |
|---|---|---|---|---|
| U1 | 首次上手引导 | **P1** | `ScreeningPage.vue:109-113,224-251,432` | 增加「新建规则」入口与首次提示（输入名称→保存→运行）；或允许从当前条件直接运行并自动建规则；空态给出步骤说明 |
| U2 | 单位/口径一致性 | **P1** | `ScreeningResultsPanel.vue:162-168` vs `WatchlistPage.vue:110-114` | 筛选表按字段语义显示（百分比×100、市值 亿/万、PE 两位小数），与自选/详情一致；数字列用 tabular-nums |
| U3 | 清理 Vite 模板样式并接线主题 | P2 | `style.css:159-169,18-20`；`App.vue:30` | 删除 `.hero/#center/#next-steps` 等死代码；`#app` 改为流式布局；`NConfigProvider` 绑定深色主题并统一 `color-scheme` |
| U4 | 响应式网格与固定宽度控件 | P2 | `ScreeningResultsPanel.vue:326`；各页 `n-grid :cols="4"` | 窄窗口降列；固定宽度控件改为自适应/最小宽度 |
| U5 | 趋势图响应式与 X 轴防重叠 | P2 | `FinancialTrendCard.vue:167,116-127` | SVG 改响应式宽度；X 轴按可用宽度抽样标签 |
| U6 | 无效股票代码空态 | P2 | `StockDetailPage.vue:285-311` | 信息加载失败时显示「股票不存在/数据不可得」结果态而非全横线 |
| U7 | 数据状态页自动刷新 | P2 | `DataStatusPage.vue:83-102,128` | 自动更新运行期间轮询（如 10s）展示进度 |
| U8 | 错误提示人性化 | P2 | `ScreeningPage.vue:135-137` 等 | reason code → 中文可读 + 下一步建议 |
| U9 | 自选移除确认 | P3 | `WatchlistPage.vue:160-169` | 移除弹确认或提供撤销 |
| U10 | 导出文件名含规则名 | P3 | `ScreeningResultsPanel.vue:233` | `规则名_日期.csv` |
| U11 | DSL 状态中文化 + 预览友好化 | P3 | `DslIndicatorManager.vue:61,241` | 状态映射中文；预览用字段表格而非裸 JSON |
| U12 | 可访问性基础项 | P3 | 全局 | 加 skip link；页面使用 `<h1>`；筛选表股票链接改 `<router-link>` 支持中键/键盘 |

---

## C. 用户视角审查方法与结论

- 方法：以「首次使用者」走查四个页面（筛选/自选/个股详情/数据状态）+ 共享框架
  （App 导航、样式、路由、令牌拦截），并按 Web Interface Guidelines 规则比对。
- 结论：**易用性整体可用**，无阻断；主要摩擦在**首次上手**（必须先生成并保存规则
  才能运行）与**跨页单位口径不一致**。数据状态页只读设计、K 线 raw/qfq 切换、
  可信度遮蔽与截断警示均符合 PRD 且体验良好。
- 全部 15 项发现详见 `.planning/2026-08-03-postlaunch-ux-review/findings.md` 与上表 B3。

## D. 关联文档

- `docs/reports/40_SYSTEM_RED_TEAM_FORMAL_ENABLEMENT_2026-08-03.md`（PASS 裁决）
- `docs/STATUS.md`（当前状态唯一权威）
- `.planning/2026-08-03-postlaunch-ux-review/`（本会话产物）
