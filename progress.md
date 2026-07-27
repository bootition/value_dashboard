# 审计修复进度

## 2026-07-20

- 已读取项目目录结构、`pyproject.toml`、前端 `package.json` 和最新审计全文。
- 已确认项目包含 Python/FastAPI 后端、Vue/TypeScript 前端、DuckDB/SQLite 数据层、适配器、指标、DSL、筛选、CLI/API 与大量阶段性测试脚本。
- 已定位最新审计为 `docs/11_RED_TEAM_AUDIT_V2.md`，共 14 项当前问题，4 项 Phase 0 直接阻断。
- 已确认审计记录的关键事故：测试收集触发模块顶层快照重算，正式 `indicator_snapshot` 曾从 5,129 行降为 0。
- 下一步：读取产品/技术文档和 Phase 0 相关源码，建立备份与复现基线。
- 已建立修复前 DuckDB/SQLite 备份并验证哈希一致。
- 已从备份只读核对审计表计数和 QFQ schema 漂移。
- 已审查 pytest 收集面，确认大量 `test_m*.py` 是带模块顶层副作用的输出脚本。
- 已写入批准后的安全优先设计：`docs/superpowers/specs/2026-07-20-audit-remediation-design.md`。
- 正在核实 DuckDB 原子事务和 pytest fixture/collection 官方语义，之后进入红绿修复。
- 已修复 DQ-01/02/04/08/09/11/13/14 的可证明代码缺陷，并为 DQ-03/05/06/07/10/12 增加入库或监测防护。
- 已补齐初始化 raw/QFQ 原子语义、财务壳行门禁、适配器限流配置接线和旧 schema 只读兼容。
- 已迁移 KLineCharts v10 API，修复筛选页模板索引类型、Naive UI 表格注册和根级配置 Provider。
- 最终回归为 30 passed，pytest 安全收集 30 项，前端生产构建通过。
- Playwright 已验证数据状态页和个股 raw/QFQ 切换，最终控制台 0 error / 0 warning，K 线生成 10 个 canvas。
- CLI/API 冒烟通过；QA 临时服务已关闭，端口 8765/5173 无残留监听。
- 正式 DuckDB/SQLite 最终哈希与修复前基线完全一致。
- 已生成 `docs/12_AUDIT_REMEDIATION_REPORT.md`；代码整改通过，但现有数据未重建，总体审计继续 `BLOCK`。
- 未完成：`DataStatusPage.vue` 尚未消费结构化 `summary.data_quality`，原因是视觉工程执行端持续被提供方过滤器拦截。
- 已启动当前阻塞项 ULW 深度调查，建立 `.omo/ulw-research/20260720-current-blockers/` 证据账本。
- 调查前正式库哈希再次确认与整改前基线一致；所有数据库取证限定为只读。
- 已修复两份只读取证脚本的 schema 假设：DuckDB JOIN 中限定 `s.raw_response_hash`，SQLite 查询移除不存在的 `published_at`。
- 已生成 `dividend-lineage.json`（15,513 字符）和 `operations-schema.json`（52,718 字符）；两份脚本退出码均为 0，逐文件 LSP 诊断为 0。
- 已用不同查询形状独立重算财务、分红、lineage、价格、元数据、覆写、作业和迁移状态，全部与三份证据 JSON 一致。
- 正式 DuckDB/SQLite 在取证脚本和独立反证查询后哈希仍分别为 `46EB...C91` 与 `228E...9D3`。
- 已恢复上一轮三个 librarian 的完整权威来源回报，并写入 Wave 1 研究摘要。
- 已完成源码消费链反证，发现 raw-only 增量更新、缺少生产字段级 lineage、分红指标继续消费占位数据、分红回填缺事务/retry/公告链等残余路径。
- 已更新 intent diff、claim graph、observation manifest、verification economics、cause disappearance 和 expansion log；15 个当前主张均有观察支持。
- 第二轮外部扩展已完成：CNINFO 唯一性被否定，公开 raw XBRL 仅确认商业/受限获取，公司行动使用文档链而非通用 R+1 假设。
- 第三轮本地扩展已完成：13 个价格孤儿确认为未补零重复代码；其余 lead 已转换为明确验收门禁。
- 研究达到收敛：报告所需事实均已支持、反驳或明确 abstain；剩余均为整改、授权、外部抽样或数据负责人审批。

## 2026-07-21

- 因报告引用检查发现 C010 未进入正文，重新开启一轮报告质量与因果链续查。
- 使用构造对象和内存 DuckDB 证明：通用 retry 成功后只删除任务而不落库；`price_daily_qfq` 不属于 `FetchRequest.data_type`；分红同股第二行失败时第一行会保留。
- 证明 `run_full_init`、`run_full_backfill`、`run_incremental_update` 可在子步骤 failed/missing/partial 时返回顶层 success，CLI 协议还会包装为 `result.status=ok`。
- 确认 `_check_new_announcements` 只返回当前时间，不查询或比较公告/财报来源。
- G22 续查扩展到全部消费面：状态页不消费 `data_quality`，个股页不渲染 `freshness`，筛选/导出只带快照日期且可使用不可信分红，CLI status/diagnose 仅统计数量。
- 重新获取证监会第 226 号令第八条、上交所定期报告/XBRL 派生页面和中国结算指南；外部来源原则保持成立，未扩大为 raw XBRL 免费获取或统一 R+1 断言。
- 新增 `verify-runtime-orchestration.md` 和 `wave-4-runtime-and-visibility.md`；账本更新为 26 个观察、24 个 supported claims，所有主张均进入报告正文。
- 最终验证：回归测试 30 passed；前端 `vue-tsc -b && vite build` 成功；两份取证脚本重跑成功且 LSP 0 diagnostics；三份证据 JSON 可解析；本地报告链接有效。
- 正式 DuckDB/SQLite 哈希仍为 `46EBCEB6...A3C91` 与 `228E0F53...C6596D3`，与调查前基线完全一致。
- 已完成 `docs/13_CURRENT_BLOCKERS_INVESTIGATION.md`；研究与报告工作完成，总体审计仍为 `BLOCK`。
- 启动审计续修与目录治理；修订自动计划中的不安全动作：不让测试读取正式库，不移动活动规划文件，不把 `value-dashboard.spec` 误归档。
- 续修前回归基线为 `30 passed`；正式 DuckDB/SQLite SHA-256 仍分别为 `46EB...C91` 和 `228E...9D3`。

## 2026-07-22

- Cycle 2 后端代码修复已落盘；worker 曾报告 48 项回归基线，后续采集安全门禁扩展后报告 61 项，但同一验证序列改变了正式数据库，因此这些结果不构成干净的独立后端验收。DataStatus 的 `data_quality`、前端警告码/日期、stock-detail 六个 freshness 字段和四个分红指标"数据未验证"标签均已落盘；Python 路径继续冻结。
- 最终 mock 浏览器矩阵已完成：DataStatus、StockDetail、Screening 共 19 个场景，在 375/768/1280 三种宽度均通过；保存/导出/自选 payload、嵌套规则、loading/404/500、阻断状态零持久化 POST 均有运行时断言。
- 前端筛选实现已完成：类型化递归规则编辑器（ScreeningRuleEditor）；关联字段不可信时门禁保存/导出（FINANCIAL_SHELL_ROWS/SNAPSHOT_STALE/DIVIDEND_DATES_UNVERIFIED/LINEAGE_INVALID 阻断，纯操作警告 STALE_RUNNING_JOBS/UNPUBLISHED_OVERRIDES 不阻断）；"加入自选"可用。功能浏览器验收已完成，像素级视觉签署仍待具备图像能力的 reviewer 或人工完成。
- 已执行非破坏性目录治理：71 个文件（6,756,894,870 字节）移入 `_legacy/`，回归基线保持 48 不变，`data/`、`tests/regression/`、规划/规范/agent 目录受保护排除，已生成 `_legacy/README.md`。
- **更正先前记录：** Cycle 2 验证期间正式数据库发生写入，不能再声称基线哈希保留。第一次事故由显式 `python -m pytest tests/ -q --no-header` 绕过 `testpaths` 并导入带模块级副作用的遗留验收脚本触发；事故后正式 DuckDB/SQLite 已偏离批准基线。
- 第二次 DuckDB 变更发生在 worker 的 `python -m pytest tests/regression -q` 运行窗口内：DuckDB 从 `98DF496F...4CC2B1A` 变为 `5186E660...A51268D6`，`LastWriteTimeUtc=2026-07-22 01:41:26`；SQLite 在该窗口保持 `B7B5F2FF...7EC11959`。未捕获逐进程文件写入轨迹，因此具体测试/调用链仍未证明；“mmap 延迟写回且时间戳不变”的解释已撤回。
- 批准备份仍完整：DuckDB `46EBCEB6...A3C91`、SQLite `228E0F53...C6596D3`。恢复会覆盖当前正式库，未经用户明确批准未执行；Python/pytest/app/CLI/数据库命令现已冻结。
- 已生成根目录 README.md，记录架构、安装、启动、CLI、安全验证、BLOCK 状态、哈希基线和 _legacy 映射。
- 前端安全门禁新鲜结果：Node 合约测试 `46/46`，`vue-tsc --noEmit` 成功，生产构建成功；mock 浏览器矩阵 `19/19`，命令正常退出且无 Vite/6176 残留。`index.html` 已改为 `lang="zh-CN"`，DataStatus 失败状态已使用语义化 error alert。
- 57 张 QA 截图已生成，但当前独立 reviewer 不具备图像解码能力；CJK 像素级换行、裁剪和视觉层级仍需 vision-capable reviewer 或人工签署。正式数据未重建、正式迁移未执行、G23 真值抽样未执行；总体审计继续 `BLOCK`。

## 2026-07-24

- 导出并恢复 `ses_06cf5f194ffeHcfGg7JJKIlOQN` 及其引用的 `ses_0757e085fffeizmuZXhPWkJhEU`，核对完整会话尾段、Todo、S0/S1 计划和当前 Git 状态。
- 确认先前“零提交、停在 B-6 前”的汇报已过时：当前已有根提交 `f4517d2`，annotated tag `incident-2026-07-22` 已指向该提交。
- 确认工作树无 tracked/index 修改；未追踪的历史脚本、商业原始数据、QA 产物和失败证据保持原样。
- 确认 S1 Phase C-H 尚未实现：两个 PowerShell 门禁脚本、纯路径策略模块和隔离测试均不存在。
- 重新同步活动计划，下一步从 Phase C 纯 PowerShell 预检与包装器开始；在包装器可用前继续禁止裸 Python/pytest/app/DB 命令。
- Phase C 两个 PowerShell 脚本已实现并通过 AST 解析、仓库外正向 Before、仓库内路径负向、After-only、formal ACK 和危险 pytest 参数拒绝验证；期间未启动 Python。
- 两轮静态审阅发现并推动关闭 pytest 环境/插件注入、证据目录写入 data、After 覆盖、退出码优先级、UNC/device/ADS/reparse 与 cleanup 身份验证缺口。
- 旧版负向验证曾在 `data/pre/before-failure.json` 写入单个失败证据；确认目录只有该本次生成文件后已精确删除，未触碰其他 data 内容。

## 2026-07-25

- 简化修复流程：砍掉 S0 法证副本、S5-S7 繁琐验收，保留必要步骤（schema 对齐、清壳数据、CSMAR 导入、AKShare 补齐、简单路径分离）。
- 精简 `path_policy.py`：从 413 行简化到 ~150 行，移除过度防护（Windows 保留名、ADS、硬链接扫描等），保留核心功能（环境区分、路径注入、基本验证）。
- CSMAR 导入完成：编写 `scripts/import_csmar.py`，从 .dta 文件导入 356K 行财务数据（资产负债表 356,728 行、利润表 356,907 行、现金流量表 339,165 行、分红 49,862 条），覆盖 5,828 只股票，日期范围 1990-12-31 ~ 2025-03-31。
- AKShare 补齐进行中：编写 `scripts/supplement_akshare.py`，抓取 2025Q2+ 财报数据和真实除权日。修复断点续传 bug（stock_code 被 pandas 解析为整数导致重复抓取）。后台运行中，预计 7 小时完成 5,828 只股票。
- 修复 `raw_data` 列类型问题：`_build_raw_data_json` 返回 NULL 导致 DuckDB 推断为 VARCHAR，改为返回 `'{}'`。
- 前端构建通过：`vue-tsc -b && vite build` 成功。
- 回归测试：73 passed，1 failed（数据库被后台脚本占用导致 PermissionError），31 errors（同上）。等数据收集完成后重新运行。
- 准备数据验证脚本：`scripts/verify_data_completeness.py`，检查表行数、日期范围、空壳行比例、分红日期修正情况。
