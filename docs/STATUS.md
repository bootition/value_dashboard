# 项目当前状态（单一真相源 / Single Source of Truth）

> **本文件是项目当前状态的唯一权威来源。** 任何建议、结论、验收判断必须以此为准；
> `reports/`、`archive/` 中的历史报告只作为追溯证据，不构成当前结论。
> 修改任何代码/数据/文档后，如影响状态，必须同步更新本文件。
- **最后更新**：2026-08-31
- **更新人**：opencode 会话（2026-08-31 数据完整性审计与明细/业务概览自动续传：`reports/91`）
## 当前裁决（Verdict）
| 层面 | 状态 | 依据 |
|---|---|---|
| 整体启用 | ✅ **PASS**：2026-08-14 第二轮全项目红队审查（`reports/80`）的 6×P1（F1 自动更新死循环、F2 国债缺口永久化、F3 筛选单位口径、F4 双击 exe 崩溃、F5 自选页白屏、F6 详情卡片串数据）+ 21×P2 + 20×P3 全部关闭；S1 613 passed（正式库前后指纹一致）、Ruff、前端 lint/62 node/50 vitest/build 全绿；正式库数据修复（share_capital 索引重建 10/10 验证、dividends ex_date 恢复 278 + 去重 41,614） | `reports/81`（2026-08-14）；发现基线 `reports/80` |
| 分红融资比复合指标 | ✅ `dividend_financing_ratio` v2 已发布（百分数口径，含回购注销）；中文名“分红融资比”；Schema v15、buyback_events 回购域、快照字段、DSL/筛选/详情/前端已接入；定向 S1 32 passed、Ruff、前端门禁全绿 | `reports/83`（2026-08-26）；数据前置见 `reports/82` |
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
| 自动更新与指标重算提速 | ✅ **PASS（2026-08-28 实施）**：互斥锁、指标增量重算+批提交、价格连续流水线、腾讯连接复用、统计域多进程、状态页自动重算全部落地；正式库价格新鲜度 5,544/5,544、`ready=true`、`warning_codes=[]`、`retry_count=0`、快照一致性缺口 5,541→17；S1 31 passed + 前端 56 tests + Ruff 全绿 | `reports/84`（2026-08-28） |
| 启动 readiness | ✅ 正式实测首次 health 1.616s、后台核对约 19.55s；二次启动 health 1.682s、1.731s 即返回缓存核对结果；真实 BLOCK 状态保持 503，无假阳性 | `reports/60` §2（取代 `reports/59`） |
| 筛选就绪门禁性能 | ✅ **修复完成（2026-08-30）**：无锁筛选不再在请求线程内阻塞 54s；冷缓存全量核对降至约 19-23s，启动自动预热 `screening_readiness_cache`，修复缓存命中 gate 形状 KeyError，前端 run 超时放宽到 120s | `reports/85`（2026-08-30） |
| 证监会行业分类 | ✅ **修复完成并全量重抓（2026-08-30）**：旧适配器 end_date 截止 2022-07 且混用巨潮/申万/中证口径；已改为显式到今天、只取证监会标准。正式库 5,443 只写入证监会分类，108 只源无分类如实 NULL 并登记 missing，重抓错误 0；新增 `vd.bat data refresh_csrc [--full]` | `reports/86`（2026-08-30） |
| 筛选草稿与已保存规则隔离 | ✅ **修复完成（2026-08-30）**：`applyLoadedRule` 浅拷贝导致编辑区与 `savedRules` 共享条件数组，改条件会反向污染“已保存版本”，使 dirty 检测失效、永远按旧版本运行；已深拷贝 conditions/sort 并加回归测试，真实浏览器验证 100 亿条件会保存为 v2 并运行 | `reports/87`（2026-08-30） |
| 筛选结果列与响应速度 | ✅ **实施完成（2026-08-31）**：结果展示列可在运行前自由增删（新增上市日期/ST/停牌/总股本/流通股本等）；筛选就绪缓存过期后 stale-while-revalidate，请求不再周期性地卡 20s+ 全库核对 | `reports/88`（2026-08-31） |
| 个股详情缺失字段与来源材料 | ✅ **清理完成（2026-08-31）**：详情页不再铺满 “—” 空卡片，缺失指标/趋势列动态隐藏；底部来源材料章节已移除；已知数据缺口如实披露（财务明细与业务概览仍在补采） | `reports/89`（2026-08-31） |
| Sina 财务明细映射 | ✅ **修复完成（2026-08-31）**：Sina 三表解析从 7 个核心字段扩展为 70+ 常见明细字段，写入层不再丢弃标准字段；样本 `688139` 明细已回填，业务概览已补抓 | `reports/90`（2026-08-31） |
| 数据完整性审计与自动续传 | ✅ **审计并修复（2026-08-31）**：新增财务明细缺口检测与每轮 100 只有界回填；diagnose/status 报告财务明细与业务概览缺口；写锁期间诊断改用持久化缓存避免假阴性 | `reports/91`（2026-08-31） |
## 已知剩余缺口（诚实披露，未消除前不得宣称数据完整）
1. **代码级 P2 与运维项（`reports/41` B1/B2）已全部关闭**：C1-C16 与 O1-O6 见 `reports/46`；O7 的按日节流、增量 CSRC 与可恢复价格更新当前由 `reports/52` 承接。
2. **数据层披露缺口**：4 只上市 7 天内新股（`001232`、`301677`、`920038`、`920258`）及 `920305` 免费源核心数据未形成，暂不进入研究快照；银行/券商监管字段 90 只保持 NULL（不伪造）；2026-03-31 前历史财务为 CSMAR 导入值无原始字节 lineage；**东财行情 host（push2/push2his）被封（IP 级临时封锁，探测范围见 `reports/61`：F10 财报/股本/分红源仍可用，价格已回退腾讯/BaoStock/TDX，冷却至 2026-08-15 勿触碰 push2 系）**；无行业变更历史的新股/北交所 CSRC 分类如实 NULL。均不改变 PASS。
3. **正式库质量修复已完成**：价格最新日期 2026-08-07 全市场达标；快照一致性与 lineage 缺口已清零（仅 6 只新股披露项）；`retry_list=1`（公告 pending 合法标记）；missing_list 未解决 0。
4. **CNINFO 分红适配器 ex_date 死代码待办**（`reports/61` §3.2）：主源恒空、回退链（akshare 31 行/baostock 25 行）当前可用无数据缺口；修复评估（PDF 解析 / ex_date 降级 / 明确依赖回退链）已列入待办。**2026-08-14 由 `reports/81` 部分推进**：`dividends_quarantine` 50,359 行占位符经 xdxr 除权除息事件保守匹配完成修复——恢复真实 ex_date 278 行、判定重复件去重 41,614 行，剩余 8,467 行为真正不可核验项（无候选/歧义/冲突），继续如实隔离（证据 `docs/evidence/evidence-dividend-exdate-repair-20260814-131744.json`）。
5. **P1-P4 新域待正式库推进**：业务概览、国债曲线、历史股本链与统计域均未在正式库全量回填；启动自动更新将有界续传（业务概览 20 只/轮、股本链 20 只/轮、统计域按输入指纹原子重建）。东财交叉源偶发风控期间主链独立成立但 verified=false。
6. **P3/P4 红队已修复（`reports/74`，2026-08-12）**：reports/73 全部 P1/P2/P3 与交付缺口关闭，S1 562 全绿。**剩余约束**：① CNINFO 源风控冷却中（约 4,700 只股本链待续传，PE/PB 统计按覆盖门槛如实缺失；自动更新有界续传+retry 消费，无需干预）；② 2026-08-12 当日国债曲线未发布（合法缺失，次日自动补齐）；③ 统计域构建 partial（新股/无价格股票如实无记录，指纹变化后自动重建）。
7. **东财交叉核验已补全（2026-08-13 关闭，见 `reports/75`）**：① `cross_status`/`error` 落盘缓存表（失败含原因可见）；② 批 50 + 冷却 30-60s 安全组合（探测 150 连发无风控、全量约 5,400 次请求 0 风控）；③ 批次审查固化进 `--check-only` 审计视图；④ 收紧评估完成，**用户决策：维持主链口径 + verified 披露**。正式库结果：沪深 5,207 只全部交叉核验（链上 203,149 verified 点/5,175 只）、北交所 334 只如实无交叉源、002731 主链缺失披露。过程中修复 P1 根因：vd.bat/start.bat 改用项目 venv（系统 akshare 1.18.64 无 SECUCODE 归一化且截断 20 条）；北交所不再请求东财（防熔断殃及沪深）。
8. **红队 BLOCK 修复闭环（2026-08-13，见 `reports/77`）**：更新窗口核心研究链路失效（P1：indicators 43-67s/500、treasury >60s、screening 500/74s、全遮蔽）已修复——DuckDB 连接指数退避、warning codes stale-while-revalidate、treasury 批量查询、筛选门禁写锁 409；P3×6（qfq 负值披露、staging 表清理、/api 404、规则字段校验、assets 清理、watchlist 行数）关闭；**环境修复**：`.venv` 补齐 akshare 1.18.81/baostock/easy-tdx（此前 `uv sync --locked` 不含 extras，akshare 依赖的数据源在用户路径不可用）、ruff 0.16 默认规则集变更已显式锁定传统集（E4/E7/E9/F）。**遗留观察**：`test_dead_update_lock_does_not_mark_summary_stale` 完整 S1 中偶发 WinError 32（既有测试时序竞态，单跑/重跑均通过）；更新中断中间态（价格已更新、快照未重建）下 readiness=false 为真实状态。
9. **用户层体验评估（2026-08-13，见 `reports/78`，待用户决策）**：启动 8~12s（方案 C 可压至 3~4s）；筛选在更新窗口（错过 1 个交易日约 70 分钟）保持 409 禁用（方案 A 建议改"最新完整快照+标注"口径）；U1-U6（控制台误关、国债卡片横幅、安装门槛、exe 未复验、浏览器绑定竞态、筛选链路未真人走查）均已列入候选方案 A-F。
10. **用户层体验方案已实施（2026-08-13，见 `reports/79`）**：方案 A（筛选更新窗口快照口径+标注，PRD §12.2 已修订）、C（启动 8~12s→实测 0.9s，schema 版本一致跳过 DDL）、D（exe 重建，health 200 实测 1.2s）、E（start.bat 运行提示）、U2（国债卡片横幅）、U6（筛选完整链路真人走查）全部完成；S1 590、Ruff、前端门禁全绿。**已追加修复**：股息率等百分比字段条件输入单位换算（输入 2=2%，底层存 0.02，带 % 单位提示）；原创应用图标（K 线造型）嵌入 exe 打包。方案 F（常驻托盘）为远期。**注意**：其中"单位换算修复"经 `reports/80` 复核存在回归（ttm_dividend_yield/div_yield_spread_* 百分数存储字段被错误 ÷100），将由 `reports/81` 修复。
11. **第二轮系统红队全面审查（2026-08-14，见 `reports/80`，NOT PASS）**：发现 6 个已复现 P1——F1 自动更新死循环（`share_capital_history` ART 索引损坏：300479 删除 38 行索引失败致 DuckDB FATAL，`last_success_at` 停在 08-08，retry 永不递增）；F2 国债曲线缺失日永久不补（最新曲线 08-11，missing_list 无人消费）；F3 筛选单位口径双向不一致（簇 A 6 个小数字段漏配 PCT_FIELDS→恒假；簇 B 11 个百分数字段被 ÷100→恒真+显示 529%；簇 C turnover_rate）；F4 双击发行 exe 无 env 崩溃；F5 自选页配置过列后 TDZ 永久白屏；F6 详情页国债利差/历史统计两卡片跨股票串数据。另 21 P2 + 20 P3。
12. **第二轮红队修复闭环（2026-08-14，见 `reports/81`，PASS）**：`reports/80` 全部 6×P1 + 21×P2 + 20×P3 关闭。要点：F1 分事务索引重建（正式库 delete_verify 10/10 含 300479）+ retry per-task 隔离；F2 `backfill_missing_days` 有界缺口回填；F3 单位元数据单一来源（后端 `/indicators` 下发 unit，前端消费）；F4 frozen 默认路径；F5/F6 前端修复；S1 门禁四项加固（venv python、证据哈希排除备份目录内容、conftest 内层证据、哈希单元测试）+ 证据 JSON -AsHashtable 修复；dividends ex_date 数据修复（缺口 #4）。**遗留（不阻断）**：① DuckDB 1.5.5 同事务索引 bug 以分事务绕过，升级评估待办；② 业务概览全量回填由自动更新有界续传渐进；③ dividends_quarantine 剩余 8,467 行不可核验如实隔离；④ 08-13 单位 bug 期间保存的旧规则原始值建议用户复核另存；⑤ start.log 历史 Traceback 随 10MB 轮转归档。
13. **DuckDB 文件高水位（2026-08-28 新披露）**：正式库文件约 16.7GB，远大于逻辑表数据；主因是多次全量快照/统计 `staging → DELETE 全表 → INSERT` 留下的高水位，`source_audit` 3,641 万行及其复合索引占主要空间。已执行 `CHECKPOINT`；DuckDB 1.5.5 默认 `VACUUM` 未物理收缩文件。查询与更新功能正常，离线压缩/重建评估列入待办（`reports/84` §剩余与风险）。
## 进行中的工作
- **数据缺口补全（2026-08-25，见 `reports/82`）**：融资事件域（funding_events：IPO/增发/配股募资）与指数估值域（index_valuation：沪深300 PE 历史）代码/测试/CLI/自动更新接入完成，20 只真实数据抽样验证通过（IPO 18/20、增发配股 12/20、北交所如实 missing）。正式库 index_valuation 已全量落库；funding_events 已全量续传完成（5,550 只）。
- **分红融资比复合指标（2026-08-26，见 `reports/83`）**：`dividend_financing_ratio` v2 已发布，使用百分数口径并含回购注销；正式库 Schema v15 已应用，buyback_events 已落库（5,271 条/2,863 只），融资域已全量覆盖，快照新字段已回填。
- **第二轮全项目红队修复（2026-08-14）**：已完结——`reports/80` 全部发现关闭（`reports/81`，S1 613 全绿）；遗留长期项见缺口 #12。
- **CNINFO 股本链续传**：主链 5,541 只已全量落盘；仅 002731（*ST萃华）因 CNINFO 源最新锚点陈旧 fail-closed 保留 retry（如实披露）；CNINFO 风控冷却期续传由自动更新有界推进。
- **自动更新保持 enabled**：服务每轮启动自动执行增量更新；2026-08-28 提速后价格已补齐至 2026-08-27（5,544/5,544 达标），`retry=0`、快照与历史统计域已跟随重建；akshare 1.18.81 已补齐至项目 venv。
- **东财行情源冷却**：push2/push2his 封锁冷却期至 2026-08-15（期间勿触碰）；到期后单次探测，恢复后限速 ≤2 req/s、并发 ≤5。
- **待办（长期）**：DuckDB 1.5.5 同事务索引 bug 升级评估；CNINFO 分红 ex_date 剩余 8,467 行核验路径评估（见缺口 #4）；业务概览全量回填（自动更新有界续传中）；融资域正式库全量落库（`reports/82` §6，重启服务后）；沪深300 ERP 指标/卡片（`reports/82` §8 后续迭代）；分红融资比已发布但回购注销口径待补（`reports/83`）；**S1 既有失败 2 项待排查**（国债 `test_snapshot_ttm_dividend_yield_and_spread` 种子日期矛盾；pdf 归档测试在 WorkBuddy shim 环境下 PermissionError——均与 2026-08-25 改动无关）。
- **数据完整性审计（2026-08-31，见 `reports/91`）**：正式库财务明细缺口 5,375 只、业务概览缺口约 2,954 只（自动更新正按 100 只/轮续传）。已新增 `vd data financial-detail-backfill`、diagnose/status 缺口计数与写窗口安全诊断。
- **Sina 财务三表明细映射补全（2026-08-31，见 `reports/90`）**：解析字段从 7 个扩展为 70+ 个，`_upsert_financial_row` 放行全部标准化明细字段；`688139` 财务明细与业务概览已补抓。全市场明细回填和业务概览全量补抓仍为后续任务。
- **个股详情缺失字段清理（2026-08-31，见 `reports/89`）**：缺失指标不再显示空卡片，趋势列动态隐藏；移除来源材料章节。数据缺口如实披露：财务三表明细仅约 20 只来自备用源，业务概览覆盖 2,542/5,551。
- **筛选结果列与就绪缓存优化（2026-08-31，见 `reports/88`）**：新增“03 结果展示列”多选区，运行请求可携带 columns；就绪缓存过期后 stale-while-revalidate，消除周期性长等待。正式服务已重启加载新代码。
- **筛选草稿污染已保存规则引用修复（2026-08-30，见 `reports/87`）**：根因是 `applyLoadedRule` 浅拷贝使编辑区与 `savedRules` 共享同一条件数组；用户改 100 亿会同步改掉内存中的“已保存 30 亿”，dirty 检测因此失效。已深拷贝修复；真实 Chrome 验证修改后会保存 v2 并运行新条件。
- **证监会行业分类修复与全量重抓（2026-08-30，见 `reports/86`）**：修复 CSRC 查询日期截止 2022-07 与多行业标准混用问题，正式库 5,551 只全部重抓：5,443 只写入证监会口径，108 只 CNINFO 源无分类如实 NULL 并登记 missing；`csrc_industry_last_refresh` 已更新，自动更新增加“有未登记 NULL 缺口即增量补抓”。**注意**：当前 Web 服务进程是修复前启动的，重启 `start.bat` 后新自动更新逻辑生效。
- **筛选就绪门禁性能修复（2026-08-30，见 `reports/85`）**：根因是 `_require_current_screenability` 在请求线程内同步跑全库数据质量核对（source_audit 3,693 万行 + 6.25GB 归档重哈希），正式库约 54s，超过前端 30s 超时；服务端最终完成并落库但浏览器已中止，表现为“很慢且没结果”。已做 SQL 下推/目标计数/`integrity_verified` 冷扫描裁剪，并启动预热缓存；S1 定向 46 passed、Ruff、前端 lint/62 脚本断言 + 56 Vitest/build 全绿。
- **中报公告发现修复（2026-08-29，延续 `reports/84`）**：根因是 CNINFO 公告接口每页固定 30 条且 pageNum>100 会重复第 1 页；旧代码用请求的 50 条判断末页，只读到第一页，导致 8 月陆续披露的中报大量漏检（联网核实：8/28 单日 729 家披露半年报、累计超 4,600 家）。已修复分页/日期拆分/5xx 重试/按半年报类别查询/检查游标，并加入财务三表单事务并发刷新与实时进度。当前正式库正在进行中报 catch-up（自动运行中，不需要用户命令）。
- **自动更新与指标重算提速（2026-08-28，见 `reports/84`）**：已实施并正式库验证——互斥、增量重算/批提交、价格连续流水线（57-148 只/分）、腾讯连接复用、统计域 4 进程重建、状态页自动触发 pending 重算；本轮更新后价格 2026-08-27 全市场达标、`ready=true`、`warning_codes=[]`、`retry=0`。**剩余**：DuckDB 文件高水位离线压缩评估（见缺口 #13）。
- **已完结**：P1-P4 全部实施；P3/P4 系统红队发现全部修复（见 `reports/73/74`）；东财交叉核验补全（`reports/75`）；红队全面审查 BLOCK 修复（`reports/76/77`）；用户体验方案实施（`reports/79`）；第二轮全面审查修复闭环（`reports/81`）；数据缺口补全实施（`reports/82`）。
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
| `docs/reports/76_SYSTEM_RED_TEAM_USER_FLOW_REVIEW_2026-08-13.md` | 系统红队全面审查发现基线（BLOCK：更新窗口核心研究链路失效 + 6 项 P3） | 发现基线；裁决已被 `reports/77` 更新为 PASS（`reports/77` 又被 `reports/80` 更新为 NOT PASS） |
| `docs/reports/77_RED_TEAM_BLOCK_FIX_AND_ACCEPTANCE_2026-08-13.md` | 红队 BLOCK 修复与验收（修复事实） | 修复事实保留；**PASS 裁决已被 `reports/80` 更新为 NOT PASS** |
| `docs/reports/78_USER_EXPERIENCE_ASSESSMENT_2026-08-13.md` | **用户层体验评估**：启动耗时、更新窗口、打包形态、安装门槛实测 + 候选方案 A-F | **当前用户体验发现与方案讨论依据**（方案已实施见 reports/79） |
| `docs/reports/79_USER_EXPERIENCE_IMPLEMENTATION_2026-08-13.md` | **用户层体验方案实施报告**：方案 A/C/D/E/U2/U6 完成，启动 0.9s、exe 1.2s、更新窗口筛选可用实测 | **当前用户体验实施依据**；其中单位换算修复经 `reports/80` 发现缺陷，已由 `reports/81` 以单位元数据单一来源修复 |
| `docs/reports/80_SYSTEM_RED_TEAM_FULL_REVIEW_2026-08-14.md` | **第二轮系统红队全面审查发现基线（NOT PASS）**：6×P1 + 21×P2 + 20×P3 | 发现基线保留；**全部发现已由 `reports/81` 修复关闭（PASS）** |
| `docs/reports/81_RED_TEAM_FULL_REVIEW_FIX_CLOSURE_2026-08-14.md` | **第二轮红队修复闭环（PASS）**：6×P1 + 21×P2 + 20×P3 全部关闭 + dividends ex_date 数据修复；S1 613 全绿 | **当前整体裁决唯一依据** |
| `docs/reports/82_DATA_GAP_FILL_FUNDING_INDEX_2026-08-25.md` | **数据缺口补全实施报告（2026-08-25）**：融资域（funding_events）+ 指数估值域（index_valuation）；正式库 index_valuation 已落库，funding 续传中 | 分红融资比/沪深300 ERP 指标的数据前置 |
| `docs/reports/83_DIVIDEND_FINANCING_RATIO_IMPLEMENTATION_2026-08-26.md` | **分红融资比复合指标实施报告（2026-08-26）**：Schema v13-v15、buyback_events 回购域、快照输入字段、中文名/百分数、DSL/筛选/详情接入、CLI 发布 `dividend_financing_ratio` v2 | 当前分红融资比口径与验证依据 |
| `docs/reports/84_UPDATE_ACCELERATION_IMPLEMENTATION_2026-08-28.md` | **自动更新与指标重算提速实施报告（2026-08-28）**：互斥、增量重算+批提交、价格连续流水线、腾讯连接复用、统计域多进程、状态页自动化、job_logs partial 语义；正式库验证数据与剩余风险 | 当前更新提速与运维状态依据 |
| `docs/reports/85_SCREENING_GATE_PERFORMANCE_FIX_2026-08-30.md` | **筛选就绪门禁性能修复报告（2026-08-30）**：冷核对 54s→19-23s、启动预热缓存、归档只重算未标记行、前端 run 超时 120s；定向 46 passed + 前端门禁全绿 | 当前筛选门禁性能与缓存行为依据 |
| `docs/reports/86_CSRC_CLASSIFICATION_REPAIR_2026-08-30.md` | **证监会行业分类修复与全量重抓报告（2026-08-30）**：修复日期截止/口径混用，正式库 5,443 只写入证监会分类、108 只源无分类登记 missing、错误 0；新增 `vd.bat data refresh_csrc [--full]` | 当前 CSRC 行业分类口径与缺口依据 |
| `docs/reports/87_SCREENING_DRAFT_SAVED_RULE_ALIAS_FIX_2026-08-30.md` | **筛选草稿污染已保存规则引用修复报告（2026-08-30）**：深拷贝 conditions/sort、真实浏览器验证 100 亿自动保存 v2 运行 | 当前筛选草稿/已保存规则隔离依据 |
| `docs/reports/88_SCREENING_COLUMN_PICKER_AND_STALE_CACHE_2026-08-31.md` | **筛选结果列自由选择与缓存 stale-while-revalidate 报告（2026-08-31）**：新增结果列多选、上市日期/股本等字段、stale 缓存后台刷新；后端 23 passed + 前端 58 tests/build 全绿 | 当前筛选结果展示与响应速度依据 |
| `docs/reports/89_STOCK_DETAIL_CLEANUP_2026-08-31.md` | **个股详情缺失字段清理与来源材料移除报告（2026-08-31）**：缺失卡片动态隐藏、来源材料移除；披露财务明细/业务概览补采边界 | 当前个股详情界面与数据缺口依据 |
| `docs/reports/90_SINA_FINANCIAL_DETAIL_MAPPING_2026-08-31.md` | **Sina 财务三表明细字段映射补全报告（2026-08-31）**：70+ 明细字段映射、写入白名单、688139 样本回填与业务概览补抓 | 当前财务明细采集能力与剩余回填边界依据 |
| `docs/reports/91_DATA_INTEGRITY_AUDIT_AND_DETAIL_BACKFILL_2026-08-31.md` | **数据完整性审计与自动续传报告（2026-08-31）**：财务明细缺口检测/有界回填、业务概览缺口计数、写窗口安全诊断 | 当前数据完整性检查与回填机制依据 |
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
- `reports/76`（系统红队全面审查 BLOCK）→ **发现基线保留；BLOCK 裁决已被 `reports/77` 更新为 PASS**（2026-08-13 修复闭环）。
- `reports/77`（红队 BLOCK 修复与验收 PASS）→ **修复事实保留；整体 PASS 裁决已被 `reports/80` 更新为 NOT PASS**（2026-08-14 第二轮全面审查，F1-F6）。
- `reports/80`（第二轮系统红队全面审查 NOT PASS）→ **发现基线保留；全部发现已由 `reports/81` 修复关闭**（2026-08-14）。
- 更早编号报告（05–24、26）→ 全部 superseded，仅作追溯证据。
## 维护规则（写文档的人必须遵守）
1. **状态变化时**：更新本文件 → 将旧报告 front-matter 的 `status` 改为 `superseded` 并写 `superseded-by` → 新报告/文档必须带 front-matter 且 `status: approved`。
2. **报告类文档**（审计/验收/审查）一律放入 `docs/reports/`，命名 `NN_主题_YYYY-MM-DD.md`。
3. **新功能/修订**：先改 `decisions/01`（PRD）→ 计划放 `.planning/` → 实施 → 验收报告放 `reports/` → 回写本文件。
4. **会话产物**（findings/progress/task_plan）只放 `.planning/`，严禁写入 `docs/`。
5. **机器证据**（JSON/hash）只放 `docs/evidence/`。
6. 给 AI 的建议/结论必须附带依据文档路径与 `last-reviewed` 日期。
