# 项目当前状态（单一真相源 / Single Source of Truth）

> **本文件是项目当前状态的唯一权威来源。** 任何建议、结论、验收判断必须以此为准；
> `reports/`、`archive/` 中的历史报告只作为追溯证据，不构成当前结论。
> 修改任何代码/数据/文档后，如影响状态，必须同步更新本文件。
- **最后更新**：2026-08-08
- **更新人**：opencode 会话（启动与价格吞吐红队及正式实践）
## 当前裁决（Verdict）
| 层面 | 状态 | 依据 |
|---|---|---|
| 整体启用 | ⛔ **BLOCK / 软件可启动但研究功能继续 fail-closed**：发布代码历史裁决仍 PASS，但 2026-08-08 正式库复验 `MINIMUM_DATA_NOT_READY`，在数据修复前不得宣称可正式研究 | `reports/60`（数据现状取代 `reports/40` 的历史整体启用结论） |
| 代码层门禁 | ✅ 红队修复后 Ruff、前端 lint/55 node + 20 组件测试/build 全通过；隔离回归 464 passed | `reports/60` §1；历史发布门禁见 `reports/40` §2 |
| 安全控制 | ✅ 无 P0 安全项（注入/穿越/代码执行/空值覆盖/并发写入/Web 写面均有防护与测试） | `reports/34` §3 |
| 数据层 P0-1（股本单位混用） | ✅ 已关闭（5,534 只重建，`circ_shares > total_shares` 1,215 → 0） | `reports/29` |
| 数据层整体（ready） | ⛔ **BLOCK / 当前正式库不可宣称 ready**：2026-08-08 正式实践复验 `MINIMUM_DATA_NOT_READY`；最终 `snapshot_price_coherence=3196`、`lineage_coverage=433`，另有少量新股价格/财务/股本缺口；审计 archive/hash/orphan 均为 0 | `reports/60` §4–§6（取代旧 ready 结论） |
| 30 股外部真值抽样 | ✅ 已执行（收盘 27/27、总股本 27/27；2 只流通股本为解禁时间差披露项） | `reports/29` |
| 回归/发布验证 | ✅ 前端 + S1 全绿（423 passed，2026-08-04）；正式发行包可构建并经真实 exe `/api/health` smoke；**PRD §19.1 性能验收仪式 PASS（10/10 <5s，avg 256ms）** | `reports/46` §2、`reports/40` §2、`docs/evidence/evidence-performance-20260804.json` |
| 桌面筛选界面 | ✅ 已接入正式路径：浅色侧栏四模块、筛选工作区、中文优先指标与模块内个股搜索；S1 424、Ruff、前端门禁全绿 | `reports/47` |
| 四页桌面界面 | ✅ 四页静态样稿经用户确认后已全部接入：筛选、自选规则分组、个股搜索/详情、数据状态；S1 424、Ruff、前端门禁全绿 | `reports/48` |
| 筛选界面与启动路径 | ✅ 范围（ST/停牌/上市年限）作为常驻条件并入筛选条件区，全站字体/圆角一致；`start.bat` 不再被旧 dist 遮蔽且按需构建，二次启动不再多花十几秒；筛选指标全中文（含排名与财务表字段） | `reports/52`、`reports/53`、`reports/56` |
| 自动更新与实时状态 | ✅ 正式受控实践 450 股零失败，四批价格速率 123.47/131.98/132.42/127.69 股/分；源内硬 deadline、BaoStock 生命周期锁、最新收盘价最小审计与旧 lineage 缺口续传均经红队/正式任务验证 | `reports/60`（取代 `reports/59`） |
| 启动 readiness | ✅ 正式实测首次 health 1.616s、后台核对约 19.55s；二次启动 health 1.682s、1.731s 即返回缓存核对结果；真实 BLOCK 状态保持 503，无假阳性 | `reports/60` §2（取代 `reports/59`） |
## 已知剩余缺口（诚实披露，未消除前不得宣称数据完整）
1. **代码级 P2 与运维项（`reports/41` B1/B2）已全部关闭**：C1-C16 与 O1-O6 见 `reports/46`；O7 的按日节流、增量 CSRC 与可恢复价格更新当前由 `reports/52` 承接。
2. **数据层披露缺口**：4 只上市 7 天内新股（`001232`、`301677`、`920038`、`920258`）及 `920305` 免费源核心数据未形成，暂不进入研究快照；银行/券商监管字段 90 只保持 NULL（不伪造）；2026-03-31 前历史财务为 CSMAR 导入值无原始字节 lineage；东财源被封（已自动回退腾讯/Sina/BaoStock）；无行业变更历史的新股/北交所 CSRC 分类如实 NULL。均不改变 PASS。
3. **正式库质量修复未完成**：价格最新日期已达 2026-08-07，但 3,196 只快照价格一致性与 433 只最新价格 lineage 仍待受控续传修复；`retry_list=10001` 是历史遗留，不能宣称全市场研究快照 ready。
## 进行中的工作
- **质量缺口续传**：本轮已用 450 股正式任务验证性能和 lineage 恢复；自动更新当前保持 disabled、服务已停止。后续应继续分批修复 3,196/433 缺口并清理 10,001 条历史 retry，每批后复验质量门禁。
## 当前有效文档（Current Truth）
| 文档 | 用途 | 注意 |
| `docs/STATUS.md` | 本文件：当前状态唯一权威 | 每次状态变化必须更新 |
| `docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md` | 产品需求规格（验收合同） | **活文档**：2026-08-08 修订 readiness 后台核对、价格并发/超时/自适应、优先续传与 ETA |
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
| `docs/reports/54_TEST_RESIDUE_AND_PROGRESS_REPORT_2026-08-07.md` | **测试残留清理与断点续传实证及数据页结构强化**：测试 DSL/结果清除、价格续传回归、明细表边框黑体 | **当前正式库卫生与续传/UI 依据** |
| `docs/reports/55_DATA_STATUS_RESPONSIVENESS_2026-08-07.md` | **数据状态页实时响应与写锁降级**：summary 写锁缓存+stale、4s 独立轮询、15s 超时、刷新解耦 | **当前状态页响应与轮询依据** |
| `docs/reports/56_LAUNCH_FIX_AND_STARTUP_ANALYSIS_2026-08-07.md` | 启动修复与历史耗时剖析 | readiness 优化结论被 `reports/59` 取代 |
| `docs/reports/57_UPDATE_SPEED_ANALYSIS_2026-08-07.md` | **自动更新速率剖析与限速调优**：90s 隐式节流根因与 baostock 降频 | **当前更新速率依据** |
| `docs/reports/58_SOURCE_CALIBRATION_AND_CONCURRENCY_2026-08-07.md` | 官方调研、限速校准与初版并发事实 | 当前策略被 `reports/59` 取代 |
| `docs/reports/59_STARTUP_READINESS_AND_PRICE_THROUGHPUT_2026-08-08.md` | readiness 后台缓存与价格吞吐初版实现 | 红队与正式实践结论被 `reports/60` 取代 |
| `docs/reports/60_THROUGHPUT_RED_TEAM_AND_FORMAL_PRACTICE_2026-08-08.md` | **红队修复与正式实践**：启动实测、450 股任务、lineage 回归发现/修复、正式库 BLOCK 裁决 | **当前启动/抓取策略与正式数据状态依据** |
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
- `reports/56`（启动耗时剖析）→ 根因与历史实测保留；readiness 后台缓存实施结论由 `reports/59` 更新。
- `reports/58`（腾讯主源与初版并发）→ 官方调研与校准事实保留；当前并发、超时、动态限速和优先续传策略由 `reports/59` 更新。
- `reports/59`（readiness 缓存与吞吐初版）→ 实现事实保留；红队修复、正式实测速率和正式库 BLOCK 裁决由 `reports/60` 更新。
- 更早编号报告（05–24、26）→ 全部 superseded，仅作追溯证据。
## 维护规则（写文档的人必须遵守）
1. **状态变化时**：更新本文件 → 将旧报告 front-matter 的 `status` 改为 `superseded` 并写 `superseded-by` → 新报告/文档必须带 front-matter 且 `status: approved`。
2. **报告类文档**（审计/验收/审查）一律放入 `docs/reports/`，命名 `NN_主题_YYYY-MM-DD.md`。
3. **新功能/修订**：先改 `decisions/01`（PRD）→ 计划放 `.planning/` → 实施 → 验收报告放 `reports/` → 回写本文件。
4. **会话产物**（findings/progress/task_plan）只放 `.planning/`，严禁写入 `docs/`。
5. **机器证据**（JSON/hash）只放 `docs/evidence/`。
6. 给 AI 的建议/结论必须附带依据文档路径与 `last-reviewed` 日期。
