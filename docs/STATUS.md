# 项目当前状态（单一真相源 / Single Source of Truth）

> **本文件是项目当前状态的唯一权威来源。** 任何建议、结论、验收判断必须以此为准；
> `reports/`、`archive/` 中的历史报告只作为追溯证据，不构成当前结论。
> 修改任何代码/数据/文档后，如影响状态，必须同步更新本文件。
- **最后更新**：2026-08-07
- **更新人**：opencode 会话（指标中文化与自动更新进度可视化）
## 当前裁决（Verdict）
| 层面 | 状态 | 依据 |
|---|---|---|
| 整体启用 | ✅ **PASS / 可正式启用**（第八轮独立攻击确认 F4 已关闭：网页与原生 CLI 导出同源，原 F4 隔离复现现在含 `_truncated`；发行包实际启动 smoke、全量门禁及正式库保护均通过） | `reports/40`（取代 `reports/39` 的修复自证结论） |
| 代码层门禁 | ✅ Ruff、前端 lint/55 node + 19 组件测试/build 全通过；隔离回归 447 passed、1 deselected（正式自动更新持锁，正式库 collect-only 哈希保护项未运行）；入口契约 16 passed | `reports/53` §3；历史发布门禁见 `reports/40` §2 |
| 安全控制 | ✅ 无 P0 安全项（注入/穿越/代码执行/空值覆盖/并发写入/Web 写面均有防护与测试） | `reports/34` §3 |
| 数据层 P0-1（股本单位混用） | ✅ 已关闭（5,534 只重建，`circ_shares > total_shares` 1,215 → 0） | `reports/29` |
| 数据层整体（ready） | ✅ ready=TRUE、warning_codes=[]（2026-08-05 正式复验；5537 只上市股，4 只极新股免费源数据未形成仅披露） | `reports/52`、`reports/46`、`reports/29` |
| 30 股外部真值抽样 | ✅ 已执行（收盘 27/27、总股本 27/27；2 只流通股本为解禁时间差披露项） | `reports/29` |
| 回归/发布验证 | ✅ 前端 + S1 全绿（423 passed，2026-08-04）；正式发行包可构建并经真实 exe `/api/health` smoke；**PRD §19.1 性能验收仪式 PASS（10/10 <5s，avg 256ms）** | `reports/46` §2、`reports/40` §2、`docs/evidence/evidence-performance-20260804.json` |
| 桌面筛选界面 | ✅ 已接入正式路径：浅色侧栏四模块、筛选工作区、中文优先指标与模块内个股搜索；S1 424、Ruff、前端门禁全绿 | `reports/47` |
| 四页桌面界面 | ✅ 四页静态样稿经用户确认后已全部接入：筛选、自选规则分组、个股搜索/详情、数据状态；S1 424、Ruff、前端门禁全绿 | `reports/48` |
| 筛选界面与启动路径 | ✅ 范围（ST/停牌/上市年限）作为常驻条件并入筛选条件区，全站字体/圆角一致；`start.bat` 不再被旧 dist 遮蔽且按需构建，二次启动不再多花十几秒；筛选指标全中文（含排名与财务表字段） | `reports/52`、`reports/53` |
| 自动更新与实时状态 | ✅ Windows 死亡锁回收、逐股原子续传、价格优先、增量快照已跑通；同进程读写连接配置已统一，后台更新期间状态 API 不再因 DuckDB 配置冲突 503、SPA 入口禁止缓存；数据状态页显示逐股进度条与更新日志（4s 轮询） | `reports/52`、`reports/53` |
## 已知剩余缺口（诚实披露，未消除前不得宣称数据完整）
1. **代码级 P2 与运维项（`reports/41` B1/B2）已全部关闭**：C1-C16 与 O1-O6 见 `reports/46`；O7 的按日节流、增量 CSRC 与可恢复价格更新当前由 `reports/52` 承接。
2. **数据层披露缺口**：4 只上市 7 天内新股（`001232`、`301677`、`920038`、`920258`）及 `920305` 免费源核心数据未形成，暂不进入研究快照；银行/券商监管字段 90 只保持 NULL（不伪造）；2026-03-31 前历史财务为 CSMAR 导入值无原始字节 lineage；东财源被封（已自动回退腾讯/Sina/BaoStock）；无行业变更历史的新股/北交所 CSRC 分类如实 NULL。均不改变 PASS。
3. **价格追赶进度**：最新可得价格日为 2026-08-05，但目标日完整 raw/qfq 覆盖为 245/5530；其余非停牌股票将由后续自动更新续传，不能宣称全市场已完整更新到 08-05。
## 进行中的工作
- **价格数据续传**：自动更新控制器已恢复；job `31b22d80-fc52-4393-a395-d70470e8eb35` 正在按逐股断点追赶。上次完整复验目标日覆盖 245/5530；CSRC partial 已按 30 天节流，不再阻塞价格。
- **正式服务轮换待办**：用户当前打开的正式服务（8765）还是进度可视化之前的旧进程，自动更新照常但无 `live/log` 输出；待其自然结束后用 `start.bat` 重启一次即加载新代码与最新前端，进度条/日志生效。
## 当前有效文档（Current Truth）
| 文档 | 用途 | 注意 |
| `docs/STATUS.md` | 本文件：当前状态唯一权威 | 每次状态变化必须更新 |
| `docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md` | 产品需求规格（验收合同） | **活文档**：2026-08-05 修订筛选关系、自动更新恢复与新股披露口径 |
| `docs/decisions/02_TECH_CONSTRAINTS.md` | 技术约束清单 | 约束冲突时以 README/实施为准时需人工裁决 |
| `docs/reports/29_DATA_REBUILD_REPORT_2026-07-31.md` | 最新数据重建报告（P0-1 关闭依据） | 数据层当前结论基线 |
| `docs/reports/30_AUDIT_FIX_CLOSURE_2026-08-02.md` | 审计修复闭环报告（`reports/27` 代码级 P1/P2 全部关闭依据） | 代码层审计结论基线 |
| `docs/reports/31_RELEASE_RED_TEAM_FIX_2026-08-02.md` | 发布级红队 P0 修复报告（6 项 P0 关闭依据） | 发布级审查结论基线 |
| `docs/reports/32_RELEASE_P1_FIX_AND_FORMAL_ACCEPTANCE_2026-08-02.md` | 发布级红队 P1 修复 + 正式库只读验收报告 | 发布级 P1 关闭依据 + 正式库当前状态基线 |
| `docs/reports/33_FOURTH_ROUND_REVIEW_FIX_2026-08-02.md` | 第四轮复测修复报告（build-release 清理竞态 P1 + 2 项 P2） | 修复事实保留，结论被 `reports/35` 更新 |
| `docs/reports/34_SYSTEM_RED_TEAM_REVIEW_2026-08-02.md` | 第五轮系统红队独立复核（BLOCK + 4 项 P1 + 退出条件） | 审查发现基线；**BLOCK 裁决已被 `reports/35` 取代** |
| `docs/reports/35_SYSTEM_RED_TEAM_FIX_2026-08-02.md` | 系统红队 4 项 P1 + 3 项发布阻断 P2 修复报告 | 修复事实保留，裁决被 `reports/36`/`reports/37` 更新 |
| `docs/reports/36_SYSTEM_RED_TEAM_REAUDIT_2026-08-02.md` | 第六轮修复后独立复审（BLOCK，F1/F2/F3） | 审查发现基线；BLOCK 已被 `reports/37` 取代 |
| `docs/reports/37_REAUDIT_F1_F2_F3_FIX_2026-08-02.md` | 第六轮复审 F1/F2/F3 修复报告 | F1/F2 与网页导出修复事实保留；"可启用"结论被 `reports/38` 取代 |
| `docs/reports/38_SYSTEM_RED_TEAM_ROUND7_2026-08-02.md` | 第七轮系统红队复审（BLOCK 发现基线：F4 原生 CLI 导出静默截断） | 审查发现基线；**BLOCK 已被 `reports/39` 取代** |
| `docs/reports/39_SYSTEM_RED_TEAM_ROUND7_F4_FIX_2026-08-03.md` | 第七轮红队 F4 修复报告 | 修复事实保留；独立裁决被 `reports/40` 更新 |
| `docs/reports/40_SYSTEM_RED_TEAM_FORMAL_ENABLEMENT_2026-08-03.md` | **当前裁决依据**：第八轮正式启用独立复审（PASS） | **当前发布裁决基线** |
| `docs/reports/41_POST_LAUNCH_TASKS_AND_UX_REVIEW_2026-08-03.md` | 正式启用后任务清单 + 用户视角可用性审查（15 项 UX 增强，无 P0/P1） | 后续迭代任务索引 |
| `docs/reports/42_USER_ENABLEMENT_AND_UI_TIERS_2026-08-03.md` | 用户启用指南与 UI 分层审查（G1/L0 基础可用性、L1 专业可用性、L2 高分美化） | 分层边界；**迭代 A（G1+L0）见 `reports/43`，迭代 B（L1）见 `reports/44`** |
| `docs/reports/43_REPORT42_ITERATION_A_IMPLEMENTATION_2026-08-03.md` | 报告42 迭代 A 实施报告：G1 操作指南 + L0-1~L0-7 | G1/L0 关闭依据 |
| `docs/reports/44_REPORT42_ITERATION_B_IMPLEMENTATION_2026-08-03.md` | 报告42 迭代 B 实施报告：L1-1~L1-7 专业可用性 | L1 关闭依据 |
| `docs/reports/45_REPORT42_ITERATION_C_AND_OPS_2026-08-03.md` | **报告42 迭代 C + 并行运维项实施报告**：L2 V1-V6 美化 + O1/O2/O4 | **L2 关闭依据；O1 性能达标但 attestation 待 CSRC 落地**（已被 `reports/46` 更新） |
| `docs/reports/46_REMAINING_P2_AND_CSRC_2026-08-04.md` | **剩余 P2 与 CSRC 填充实施报告**：C3-C16 + O1 attestation **PASS** + O3/O5/O6；正式库 ready 恢复 | **B1/B2 全部关闭依据；CSRC 数据落地** |
| `docs/reports/47_DESKTOP_SCREENING_UI_AND_STOCK_SEARCH_2026-08-04.md` | **桌面筛选界面与个股搜索入口实施报告**：正式筛选页 + 四模块侧栏 + 中文指标 + 模块内股票搜索 | **本次 UI 实施与门禁依据** |
| `docs/reports/48_APPROVED_FOUR_PAGE_DESKTOP_UI_INTEGRATION_2026-08-04.md` | **已确认四页桌面界面正式接入报告**：样稿确认后完整接入四页 | **当前完整 UI 接入与门禁依据** |
| `docs/reports/50_FINAL_SCREENING_UI_AND_AUTO_UPDATE_RECOVERY_2026-08-05.md` | 最终筛选界面与自动更新恢复历史事实 | 当前结论被 `reports/51`/`reports/52` 取代 |
| `docs/reports/51_LAUNCH_PATH_AND_LIVE_STATUS_RECOVERY_2026-08-06.md` | 用户启动路径与实时状态恢复历史事实（连接修复、dist 遮蔽） | 当前结论被 `reports/52` 取代 |
| `docs/reports/52_VISUAL_BASELINE_AND_LAUNCH_BUILD_STRATEGY_2026-08-06.md` | **视觉基线回归与按需构建启动**：范围常驻条件、全站字体/圆角恢复、指纹按需构建、构建入口随 build 提交 | **当前筛选入口、视觉、启动依据；与 reports/53 合并作为当前状态** |
| `docs/reports/53_INDICATOR_CHINESE_LABELS_AND_UPDATE_PROGRESS_2026-08-07.md` | **指标中文化与自动更新进度可视化**：全量中文指标/排名/财务表字段、逐股进度条与更新日志、4s 轮询与独立先行请求 | **当前指标展示与自动更新进度依据** |
| `docs/runbooks/s0-evidence-preservation.md` | 证据保全运行手册 | |
| `docs/runbooks/user-first-use.md` | 首次使用与日常操作指南（G1，报告42 迭代 A） | 面向首次用户交付 |
| `docs/runbooks/ops-backup-restore.md` | 备份与恢复运行手册（O2） | |
| `docs/runbooks/ops-auto-update-retry.md` | 自动更新与重试运行手册（O2） | |
| `docs/runbooks/ops-data-rebuild.md` | 数据重建运行手册（O2） | |
| `docs/runbooks/ops-build-release-s1.md` | 构建、发布与 S1 门禁运行手册（O2） | |
| `docs/contracts/path-isolation-contract.md` | 路径隔离合同（签署版） | |
| `.planning/2026-07-31-automatic-data-updates/` | 当前实施会话计划 | 会话产物，不入 docs/ |
## 已被取代的结论（Superseded，禁止引用为当前结论）
- `reports/25`（7-30 正式验收 "数据层 BLOCK"）→ 数据层结论已被 `reports/29` 取代（P0-1 已修复）；代码层结论仍有效。
- `reports/27`（7-31 综合红队复审 BLOCK，1 P0 + 20 P1）→ **P0-1 已关闭**（见 `reports/29`）；**其余代码级 P1 项已全部关闭**（见 `reports/30`）。
- `reports/28`（7-31 独立红队复审，维持 BLOCK）→ 同 27，数据结论被 29 更新，代码结论被 30 更新。
- `reports/30`–`reports/33`（8-02 审计修复闭环 + 发布级 P0/P1 修复 + 正式库验收 + 第四轮复测）→ **修复事实仍有效**，其"全部关闭"结论被 `reports/34`/`reports/35` 更新。
- `reports/34`（第五轮系统红队 BLOCK，4 项 P1）→ 审查发现基线；其 BLOCK 裁决先被 35 声明修复，再被 `reports/36` 恢复（P1-A/P1-C 残余 F1/F2/F3）。
- `reports/35`（4 项 P1 + 3 项发布阻断 P2 修复，"可启用"）→ **修复事实保留**，整体裁决已被 `reports/36` 取代（P1-A/P1-C 未真正关闭）。
- `reports/36`（第六轮 BLOCK，F1/F2/F3）→ F1/F2 与网页导出 F3 已被 `reports/37` 修复；其裁决又被 `reports/38` 更新（F4 原生 CLI 导出旁路）。
- `reports/37`（F1/F2/F3 修复，"可启用"）→ **修复事实保留**，整体裁决已被 `reports/38` 取代（F4）。
- `reports/38`（第七轮 BLOCK，F4 原生 CLI 导出静默截断）→ **F4 已关闭**（见 `reports/39`），整体裁决为"可启用"。
- `reports/39`（F4 修复，"可启用"）→ **修复事实保留**，独立正式启用裁决由 `reports/40` 给出（PASS）。
- `reports/49`（桌面界面同构与数据状态修复）→ UI/状态当前结论由 `reports/50` 取代；历史修复事实保留。
- `reports/50`（最终筛选界面与自动更新恢复）→ 修复事实保留；用户启动路径和实时状态当前结论由 `reports/51` 取代，视觉与启动策略由 `reports/52` 修正。
- `reports/51`（用户启动路径与实时状态恢复）→ 连接修复与 dist 遮蔽事实保留；视觉、范围常驻与按需构建当前结论由 `reports/52` 取代。
- 更早编号报告（05–24、26）→ 全部 superseded，仅作追溯证据。
## 维护规则（写文档的人必须遵守）
1. **状态变化时**：更新本文件 → 将旧报告 front-matter 的 `status` 改为 `superseded` 并写 `superseded-by` → 新报告/文档必须带 front-matter 且 `status: approved`。
2. **报告类文档**（审计/验收/审查）一律放入 `docs/reports/`，命名 `NN_主题_YYYY-MM-DD.md`。
3. **新功能/修订**：先改 `decisions/01`（PRD）→ 计划放 `.planning/` → 实施 → 验收报告放 `reports/` → 回写本文件。
4. **会话产物**（findings/progress/task_plan）只放 `.planning/`，严禁写入 `docs/`。
5. **机器证据**（JSON/hash）只放 `docs/evidence/`。
6. 给 AI 的建议/结论必须附带依据文档路径与 `last-reviewed` 日期。
