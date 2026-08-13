# 项目当前状态（单一真相源 / Single Source of Truth）

> **本文件是项目当前状态的唯一权威来源。** 任何建议、结论、验收判断必须以此为准；
> `reports/`、`archive/` 中的历史报告只作为追溯证据，不构成当前结论。
> 修改任何代码/数据/文档后，如影响状态，必须同步更新本文件。
- **最后更新**：2026-08-13
- **更新人**：opencode 会话（东财交叉核验补全，STATUS 缺口 #7 关闭）
## 当前裁决（Verdict）
| 层面 | 状态 | 依据 |
|---|---|---|
| 整体启用 | ✅ **PASS / 可正式研究**：正式库 readiness ready=true、warning_codes=[]；retry 与 missing 未解决项清零；服务保持自动更新 enabled；2026-08-09 红队 P2 风险全部关闭 | `reports/64`（数据状态见 `reports/62`） |
| 国债基准与历史统计 | ✅ 需求与可行性门禁通过：财政部曲线独立域、默认10年期、TTM股息率利差、统计型筛选；不做逐券债券投资、提示或结论 | `reports/68`（2026-08-10） |
| 个股研究工作台 P1 | ✅ 驾驶舱、四组摘要、纵向章节、粘性目录、日/周/月研究型 K 线和全局偏好已实施；S1 490、Ruff、前端门禁全绿 | `reports/69`（2026-08-10） |
| 业务概览 P2 | ✅ 独立低频域、东财 F10 适配器、保旧值/retry/missing、详情接入已实施；20股探测沪深18/18可用，北交所2只如实 missing；S1 516、前端门禁全绿 | `reports/70`（2026-08-10） |
| 国债曲线与利差 P3 | ✅ **PASS（修复完成）**：reports/73 全部 P2/P3 修复（双线图、导出对齐、信任遮蔽、日门控、retry 消费、批量对齐、NaN 拒绝等）并经 S1 562 全绿；正式库 9 期限全历史回填完成（2006-03-01~2026-08-11） | `reports/74`（2026-08-12）；发现基线 `reports/73` |
| 历史股本链与统计 P4 | ✅ **PASS（修复完成，续传中）**：P4-1 日期截断等 3 项 P1 + 4 项 P2 + 6 项 P3 全部修复；统计域 version 1 构建完成；股本链 822 只完成、CNINFO 风控冷却后自动续传（约 4,700 只） | `reports/74`（2026-08-12）；发现基线 `reports/73` |
| 个股详情信息架构 | ✅ PRD 已定义：研究驾驶舱、业务概览、纵向研究章节、粘性目录和研究型K线；按 P1-P4 分阶段开发 | `reports/68`；`.planning/2026-08-09-stock-detail-and-treasury-discovery/task_plan.md` |
| 代码层门禁 | ✅ 红队 P2 修复后 Ruff、前端 lint/55 node + 20 组件测试/build 全通过；隔离回归 481 passed | `reports/64` §2 |
| 安全控制 | ✅ 无 P0 安全项（注入/穿越/代码执行/空值覆盖/并发写入/Web 写面均有防护与测试） | `reports/34` §3 |
| 数据层 P0-1（股本单位混用） | ✅ 已关闭（5,534 只重建，`circ_shares > total_shares` 1,215 → 0） | `reports/29` |
| 数据层整体（ready） | ✅ **ready=TRUE、warning_codes=[]**：快照一致性 3196→6（仅新股披露项）、lineage 433→0、retry 10302→1（公告 pending 合法标记）、missing 未解决 0；审计 archive/hash/orphan 全 0 | `reports/62`（取代 `reports/60` BLOCK） |
| 30 股外部真值抽样 | ✅ 已执行（收盘 27/27、总股本 27/27；2 只流通股本为解禁时间差披露项） | `reports/29` |
| 回归/发布验证 | ✅ 前端 + S1 全绿（423 passed，2026-08-04）；正式发行包可构建并经真实 exe `/api/health` smoke；**PRD §19.1 性能验收仪式 PASS（10/10 <5s，avg 256ms）** | `reports/46` §2、`reports/40` §2、`docs/evidence/evidence-performance-20260804.json` |
| 桌面筛选界面 | ✅ 已接入正式路径：浅色侧栏四模块、筛选工作区、中文优先指标与模块内个股搜索；S1 424、Ruff、前端门禁全绿 | `reports/47` |
| 四页桌面界面 | ✅ 四页静态样稿经用户确认后已全部接入：筛选、自选规则分组、个股搜索/详情、数据状态；S1 424、Ruff、前端门禁全绿 | `reports/48` |
| 筛选界面与启动路径 | ✅ 范围（ST/停牌/上市年限）作为常驻条件并入筛选条件区，全站字体/圆角一致；`start.bat` 不再被旧 dist 遮蔽且按需构建，二次启动不再多花十几秒；筛选指标全中文（含排名与财务表字段） | `reports/52`、`reports/53`、`reports/56` |
| 自动更新与实时状态 | ✅ 正式普通用户路径完成恢复：3937 只价格缺口约 40 分钟（约 100 股/分）；无缺口轮次 3-5 分钟；retry 增量重试与冗余清理、快照盲区修复均有正式轮次验证 | `reports/62`（取代 `reports/60`） |
| 启动 readiness | ✅ 正式实测首次 health 1.616s、后台核对约 19.55s；二次启动 health 1.682s、1.731s 即返回缓存核对结果；真实 BLOCK 状态保持 503，无假阳性 | `reports/60` §2（取代 `reports/59`） |
## 已知剩余缺口（诚实披露，未消除前不得宣称数据完整）
1. **代码级 P2 与运维项（`reports/41` B1/B2）已全部关闭**：C1-C16 与 O1-O6 见 `reports/46`；O7 的按日节流、增量 CSRC 与可恢复价格更新当前由 `reports/52` 承接。
2. **数据层披露缺口**：4 只上市 7 天内新股（`001232`、`301677`、`920038`、`920258`）及 `920305` 免费源核心数据未形成，暂不进入研究快照；银行/券商监管字段 90 只保持 NULL（不伪造）；2026-03-31 前历史财务为 CSMAR 导入值无原始字节 lineage；**东财行情 host（push2/push2his）被封（IP 级临时封锁，探测范围见 `reports/61`：F10 财报/股本/分红源仍可用，价格已回退腾讯/BaoStock/TDX，冷却至 2026-08-15 勿触碰 push2 系）**；无行业变更历史的新股/北交所 CSRC 分类如实 NULL。均不改变 PASS。
3. **正式库质量修复已完成**：价格最新日期 2026-08-07 全市场达标；快照一致性与 lineage 缺口已清零（仅 6 只新股披露项）；`retry_list=1`（公告 pending 合法标记）；missing_list 未解决 0。
4. **CNINFO 分红适配器 ex_date 死代码待办**（`reports/61` §3.2）：主源恒空、回退链（akshare 31 行/baostock 25 行）当前可用无数据缺口；修复评估（PDF 解析 / ex_date 降级 / 明确依赖回退链）已列入待办。
5. **P1-P4 新域待正式库推进**：业务概览、国债曲线、历史股本链与统计域均未在正式库全量回填；启动自动更新将有界续传（业务概览 20 只/轮、股本链 20 只/轮、统计域按输入指纹原子重建）。东财交叉源偶发风控期间主链独立成立但 verified=false。
6. **P3/P4 红队已修复（`reports/74`，2026-08-12）**：reports/73 全部 P1/P2/P3 与交付缺口关闭，S1 562 全绿。**剩余约束**：① CNINFO 源风控冷却中（约 4,700 只股本链待续传，PE/PB 统计按覆盖门槛如实缺失；自动更新有界续传+retry 消费，无需干预）；② 2026-08-12 当日国债曲线未发布（合法缺失，次日自动补齐）；③ 统计域构建 partial（新股/无价格股票如实无记录，指纹变化后自动重建）。
7. **东财交叉核验已补全（2026-08-13 关闭，见 `reports/75`）**：① `cross_status`/`error` 落盘缓存表（失败含原因可见）；② 批 50 + 冷却 30-60s 安全组合（探测 150 连发无风控、全量约 5,400 次请求 0 风控）；③ 批次审查固化进 `--check-only` 审计视图；④ 收紧评估完成，**用户决策：维持主链口径 + verified 披露**。正式库结果：沪深 5,207 只全部交叉核验（链上 203,149 verified 点/5,175 只）、北交所 334 只如实无交叉源、002731 主链缺失披露。过程中修复 P1 根因：vd.bat/start.bat 改用项目 venv（系统 akshare 1.18.64 无 SECUCODE 归一化且截断 20 条）；北交所不再请求东财（防熔断殃及沪深）。
## 进行中的工作
- **东财交叉核验已全量完成（2026-08-13）**：沪深 5,207 只全部核验、北交所 334 只无交叉源如实记录、error 行 0；见 `reports/75`。
- **CNINFO 股本链续传**：主链 5,541 只已全量落盘；仅 002731（*ST萃华）因 CNINFO 源最新锚点陈旧 fail-closed 保留 retry（如实披露）。
- **自动更新保持 enabled**：服务每轮启动自动执行增量更新；价格已补齐至 2026-08-11。
- **东财行情源冷却**：push2/push2his 封锁冷却期至 2026-08-15（期间勿触碰）；到期后单次探测，恢复后限速 ≤2 req/s、并发 ≤5。
- **待办**：CNINFO 分红 ex_date 修复评估（见已知缺口 #4）。
- **已完结**：P1-P4 全部实施；P3/P4 系统红队发现全部修复（见 `reports/73/74`）；东财交叉核验补全（`reports/75`）。
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
| `docs/reports/61_SOURCE_STATUS_PROBE_AND_EASTMONEY_BAN_2026-08-08.md` | **数据源状态探测与东财封禁范围调查**：东财仅 push2/push2his 被封、F10/股本/分红源可用；各源现状；CNINFO 分红适配器 bug 发现；冷却重试计划 | **当前数据源连通性依据** |
| `docs/reports/62_FORMAL_AUTO_RECOVERY_COMPLETE_2026-08-08.md` | **正式库普通用户路径恢复完成**：7 轮修复、最终 PASS、性能实测、剩余披露项清单 | **当前数据与恢复状态依据** |
| `docs/reports/63_SYSTEM_RED_TEAM_REVIEW_2026-08-09.md` | 独立系统红队复审：P2-1 至 P2-4 发现基线 | **已被 `reports/64` 关闭** |
| `docs/reports/64_RED_TEAM_P2_REMEDIATION_2026-08-09.md` | **红队 P2 风险修复**：启动、静态资源、状态轮询、价格重抓原子性全部关闭 | **当前红队修复与门禁依据** |
| `docs/reports/65_TREASURY_BOND_REQUIREMENTS_ASSESSMENT_2026-08-09.md` | 记账式国债来源与架构调研事实 | **需求未确认，已被 `reports/66` 取代** |
| `docs/reports/66_TREASURY_BOND_DISCOVERY_RESET_2026-08-09.md` | 国债需求探索重置：撤回未充分讨论的早期结论 | **已被 `reports/68` 取代** |
| `docs/reports/67_BUSINESS_OVERVIEW_DATA_FEASIBILITY_2026-08-09.md` | **个股业务概览数据可行性**：东财 F10 主源、字段和风险边界 | **当前业务概览范围评估依据** |
| `docs/reports/68_STOCK_DETAIL_AND_TREASURY_FEASIBILITY_2026-08-10.md` | **个股研究工作台与国债基准可行性**：P1-P4 门槛、来源和隔离架构 | **当前需求与实施前提依据** |
| `docs/reports/69_STOCK_RESEARCH_WORKBENCH_P1_2026-08-10.md` | **个股研究工作台 P1**：驾驶舱、纵向章节、粘性目录与日/周/月 K 线 | **当前详情页信息架构与 K 线依据** |
| `docs/reports/70_BUSINESS_OVERVIEW_P2_2026-08-10.md` | **业务概览 P2**：独立低频域、来源门禁、更新隔离和详情展示 | **当前业务概览实施依据** |
| `docs/reports/71_TREASURY_CURVE_P3_2026-08-10.md` | 国债曲线与利差 P3 实施事实（门禁通过） | **PASS 裁决已被 `reports/73` 更新（NOT PASS）** |
| `docs/reports/72_CAPITAL_HISTORY_AND_STATISTICS_P4_2026-08-10.md` | 历史股本链与统计 P4 实施事实（门禁通过） | **PASS 裁决已被 `reports/73` 更新（NOT PASS）** |
| `docs/reports/73_SYSTEM_RED_TEAM_REVIEW_P3_P4_2026-08-11.md` | P3/P4 系统红队审查（发现基线：P1×3、P2×9、P3×15 + 交付缺口） | **发现基线；修复与裁决见 `reports/74`** |
| `docs/reports/74_P3_P4_RED_TEAM_FIX_AND_DATA_COMPLETION_2026-08-12.md` | **P3/P4 修复与正式库补全（当前裁决）**：全部发现关闭、S1 562 全绿、国债/快照/统计/价格补全、CNINFO 续传约束 | **当前 P3/P4 状态唯一依据** |
| `docs/reports/75_CROSS_VERIFICATION_COMPLETION_2026-08-13.md` | **东财交叉核验补全（STATUS 缺口 #7 关闭）**：4 项待办全部落地、沪深 5,207 只全量核验、vd.bat venv 根因修复、统计域口径用户决策 | **当前交叉核验与核验披露依据** |
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
- `reports/60`（红队与正式实践 BLOCK 裁决）→ 修复事实保留；数据现状由 `reports/62` 更新为 PASS。
- `reports/63`（独立系统红队复审）→ 其 P2-1 至 P2-4 已由 `reports/64` 关闭。
- `reports/66`（国债需求探索重置）→ 个股详情与国债基准需求已完成可行性门禁，当前结论见 `reports/68`。
- `reports/71`（国债曲线与利差 P3 实施 PASS）→ **实施事实保留；PASS 裁决被 `reports/73` 更新为 NOT PASS**（2026-08-11 系统红队）。
- `reports/72`（历史股本链与统计 P4 实施 PASS）→ **实施事实保留；PASS 裁决被 `reports/73` 更新为 NOT PASS**（2026-08-11 系统红队）。
- `reports/73`（P3/P4 系统红队 NOT PASS）→ **发现基线保留；全部发现已由 `reports/74` 修复关闭**（2026-08-12）。
- 更早编号报告（05–24、26）→ 全部 superseded，仅作追溯证据。
## 维护规则（写文档的人必须遵守）
1. **状态变化时**：更新本文件 → 将旧报告 front-matter 的 `status` 改为 `superseded` 并写 `superseded-by` → 新报告/文档必须带 front-matter 且 `status: approved`。
2. **报告类文档**（审计/验收/审查）一律放入 `docs/reports/`，命名 `NN_主题_YYYY-MM-DD.md`。
3. **新功能/修订**：先改 `decisions/01`（PRD）→ 计划放 `.planning/` → 实施 → 验收报告放 `reports/` → 回写本文件。
4. **会话产物**（findings/progress/task_plan）只放 `.planning/`，严禁写入 `docs/`。
5. **机器证据**（JSON/hash）只放 `docs/evidence/`。
6. 给 AI 的建议/结论必须附带依据文档路径与 `last-reviewed` 日期。
