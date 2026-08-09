# 文档地图（Documentation Map）

本目录按 **docs-as-code 规范**（Diátaxis 四象限 + ADR + ISO/IEC 26511 生命周期）组织。
**阅读顺序：先读 `STATUS.md`，再按需进入各分类。** 历史文档一律不构成当前结论。

## 目录结构

| 路径 | 类型 | 内容 | 是否当前有效 |
|---|---|---|---|
| `STATUS.md` | 状态 | **当前状态唯一权威**（Verdict、剩余缺口、进行中工作） | ✅ 是 |
| `decisions/` | 决策 | PRD、技术约束、规划类（长期有效的合同与决策） | 01/02 ✅；03/04 historical |
| `reports/` | 报告 | 审计/验收/审查等事件型快照 | 当前结论以 `STATUS.md` 所列报告为准；其余仅为发现/修复事实 |
| `runbooks/` | 手册 | 运维/证据保全操作手册 | ✅ 是（首次使用指南 + O2 运维四册） |
| `contracts/` | 合同 | 正式签署的设计合同 | ✅ 是 |
| `evidence/` | 证据 | 机器生成的证据 JSON（evidence-*.json、evidence-s0/s1 事故证据） | 证据，非结论 |
| `archive/` | 归档 | 已废弃/完成的研究快照、执行计划（含 superpowers/） | ❌ 否（只读） |

## 文档生命周期（Lifecycle）

```
draft → approved → superseded → archived
```

| 状态 | 含义 | AI/读者处理方式 |
|---|---|---|
| `approved` | 当前有效结论 | 可作建议依据 |
| `superseded` | 已被更新文档取代（见 front-matter `superseded-by`） | 仅作追溯证据，**不得**作为当前结论 |
| `historical` | 已完成使命的历史决策（不再演进） | 同上 |
| `archived` | 已归档 | 不读（除非追溯历史） |

每个文档头部 YAML front-matter 示例：

```yaml
---
title: 数据重建最终报告
status: superseded        # approved | superseded | historical | archived
category: reports         # decisions | reports | runbooks | contracts | archive
created: 2026-07-31       # 可选
last-reviewed: 2026-07-31 # 必填：最后核验日期
supersedes: ...           # 可选：取代了谁
superseded-by: ...        # 可选：被谁取代（相对 docs/ 的路径）
---
```

## 当前有效的文档（Current Truth）

- `STATUS.md` — 当前状态唯一权威
- `decisions/01_PRODUCT_REQUIREMENTS_V1.md` — 产品需求规格（验收合同，活文档）
- `decisions/02_TECH_CONSTRAINTS.md` — 技术约束
- `reports/29_DATA_REBUILD_REPORT_2026-07-31.md` — 最新数据重建结论
- `reports/36_SYSTEM_RED_TEAM_REAUDIT_2026-08-02.md` — 第六轮独立复审（F1/F2/F3 发现基线）
- `reports/37_REAUDIT_F1_F2_F3_FIX_2026-08-02.md` — F1/F2/F3 修复事实（整体裁决已被 38 更新）
- `reports/38_SYSTEM_RED_TEAM_ROUND7_2026-08-02.md` — 第七轮独立复审（BLOCK 发现基线；裁决已被 39 取代）
- `reports/39_SYSTEM_RED_TEAM_ROUND7_F4_FIX_2026-08-03.md` — F4 修复事实（独立裁决已被 40 更新）
- `reports/40_SYSTEM_RED_TEAM_FORMAL_ENABLEMENT_2026-08-03.md` — **当前发布裁决**：第八轮正式启用独立复审（PASS）
- `reports/41_POST_LAUNCH_TASKS_AND_UX_REVIEW_2026-08-03.md` — 正式启用后任务清单 + 用户视角可用性审查
- `reports/47_DESKTOP_SCREENING_UI_AND_STOCK_SEARCH_2026-08-04.md` — 桌面筛选界面与个股搜索入口实施报告
- `reports/48_APPROVED_FOUR_PAGE_DESKTOP_UI_INTEGRATION_2026-08-04.md` — 已确认四页桌面界面正式接入报告
- `reports/49_DESKTOP_UI_PARITY_AND_DATA_STATUS_FIX_2026-08-04.md` — 历史 UI/状态修复事实，当前结论被 reports/50 取代
- `reports/50_FINAL_SCREENING_UI_AND_AUTO_UPDATE_RECOVERY_2026-08-05.md` — 筛选 UI 与自动更新恢复历史事实，当前结论被 reports/52 取代
- `reports/51_LAUNCH_PATH_AND_LIVE_STATUS_RECOVERY_2026-08-06.md` — 连接修复与 dist 遮蔽历史事实，当前结论被 reports/52 取代
- `reports/52_VISUAL_BASELINE_AND_LAUNCH_BUILD_STRATEGY_2026-08-06.md` — 当前筛选入口、视觉基线、启动策略依据
- `reports/53_INDICATOR_CHINESE_LABELS_AND_UPDATE_PROGRESS_2026-08-07.md` — 当前指标中文展示与自动更新进度可视化依据
- `reports/54_TEST_RESIDUE_AND_PROGRESS_REPORT_2026-08-07.md` — 当前正式库卫生、断点续传实证与数据页结构依据
- `reports/55_DATA_STATUS_RESPONSIVENESS_2026-08-07.md` — 当前数据状态页响应与写锁降级依据
- `reports/56_LAUNCH_FIX_AND_STARTUP_ANALYSIS_2026-08-07.md` — 启动链路修复与历史耗时剖析（readiness 优化结论被 reports/59 取代）
- `reports/57_UPDATE_SPEED_ANALYSIS_2026-08-07.md` — 当前自动更新速率剖析与限速调优依据
- `reports/58_SOURCE_CALIBRATION_AND_CONCURRENCY_2026-08-07.md` — 官方调研与初版并发事实（当前策略被 reports/59 取代）
- `reports/59_STARTUP_READINESS_AND_PRICE_THROUGHPUT_2026-08-08.md` — readiness 与吞吐初版实现（当前结论被 reports/60 取代）
- `reports/60_THROUGHPUT_RED_TEAM_AND_FORMAL_PRACTICE_2026-08-08.md` — 当前启动/抓取红队实践与正式库 BLOCK 裁决依据
- `reports/61_SOURCE_STATUS_PROBE_AND_EASTMONEY_BAN_2026-08-08.md` — 当前数据源连通性依据（东财封禁范围、各源现状、CNINFO 分红 bug、冷却重试计划）
- `reports/62_FORMAL_AUTO_RECOVERY_COMPLETE_2026-08-08.md` — 正式库自动恢复完成与数据 PASS 依据
- `reports/63_SYSTEM_RED_TEAM_REVIEW_2026-08-09.md` — 独立红队 P2 发现基线，已被 reports/64 取代
- `reports/64_RED_TEAM_P2_REMEDIATION_2026-08-09.md` — 当前红队 P2 风险关闭与门禁依据
- `reports/65_TREASURY_BOND_REQUIREMENTS_ASSESSMENT_2026-08-09.md` — 当前记账式国债数据域范围、来源与隔离架构评估
- `reports/42_USER_ENABLEMENT_AND_UI_TIERS_2026-08-03.md` — 用户启用指南与 UI 分层审查（基础可用性 vs 高分美化）
- `reports/34_SYSTEM_RED_TEAM_REVIEW_2026-08-02.md` — 第五轮系统红队复核（BLOCK 发现基线）
- `reports/35_SYSTEM_RED_TEAM_FIX_2026-08-02.md` — 修复事实；裁决已被 36/37 更新
- `reports/30`–`reports/33` — 前四轮修复/验收事实（结论已被 34/35/36 更新）
- `runbooks/s0-evidence-preservation.md` — 证据保全手册
- `runbooks/user-first-use.md` — 首次使用与日常操作指南（G1）
- `runbooks/ops-backup-restore.md` / `ops-auto-update-retry.md` / `ops-data-rebuild.md` / `ops-build-release-s1.md` — O2 运维手册
- `contracts/path-isolation-contract.md` — 路径隔离合同

## 新文档流程（Checklist）

1. 判断类型：合同/PRD → `decisions/`；事件报告 → `reports/`；操作手册 → `runbooks/`；证据 → `evidence/`；其余 → 会话产物放 `.planning/`
2. 加 front-matter（status/category/last-reviewed，必要时 supersedes/superseded-by）
3. 若产生或取代结论 → 更新 `STATUS.md`
4. 若旧文档被取代 → 改其 `status: superseded` + `superseded-by`（永不删除）

## 编号对照（迁移说明）

2026-07-31 之前的历史文档原位于 `docs/` 根目录，编号 01–29：
01–04 → `decisions/`；05–29 → `reports/`；`evidence-*.json`、`evidence-s0/`、`evidence-s1/` → `evidence/`；`superpowers/` → `archive/superpowers/`。历史文档内部对旧路径的引用（如 `docs/11_RED_TEAM_AUDIT_V2.md`）为历史记录，未逐一改写，按 `docs/` 下新位置解释即可。
