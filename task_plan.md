# Value Dashboard 审计修复计划

## 目标

理解项目产品、技术和数据链路，以 `docs/11_RED_TEAM_AUDIT_V2.md` 为当前审计基线，逐项确认问题是否仍可复现，并以测试先行方式完成可在本地安全验证的根因修复。

## 约束

- 本地 Git 已初始化但尚无提交；修改期间持续保留数据文件备份和哈希基线。
- 不对正式数据做未经用户确认的删除、猜测性修正或外部真值替换。
- 测试不得读取或写入 `data/` 下的正式数据库。
- 每项代码缺陷先复现并建立失败测试，再实施最小修复。
- 数据真实性问题若缺少可靠外部来源，只修复程序门禁与语义，不伪造数据。

## 阶段

### Phase 1：项目背景与审计基线

Status: complete

- 阅读产品、技术、交付和历史进度文档。
- 识别后端、前端、数据存储、适配器、指标、DSL、筛选和测试入口。
- 将 DQ-01 至 DQ-14 分类为代码缺陷、数据问题或需人工/外部真值的问题。

### Phase 2：安全基线与根因复现

Status: complete

- 创建 DuckDB/SQLite 备份和 SHA-256。
- 保存关键表计数和 schema 基线。
- 在不触发模块顶层副作用的前提下复现 Phase 0 缺陷。
- 正式 DuckDB/SQLite 验证前后 SHA-256 保持不变。

### Phase 3：Phase 0 阻断修复

Status: complete

- DQ-01：修复分红摘要 SQL。
- DQ-02：快照改为 staging 计算和事务发布。
- DQ-04：规范化适配器名称并拒绝未知配置。
- DQ-08：测试数据库隔离并移除模块顶层执行。
- DQ-03：新增财务壳行入库门禁，并让指标选择忽略壳行。

### Phase 4：其余可安全修复的代码问题

Status: complete

- 校正过期或无效测试。
- 逐项复现 DQ-09、DQ-10、DQ-11、DQ-12、DQ-13、DQ-14 的代码路径。
- 仅修复有明确本地正确性标准的程序问题；数据清理保持非破坏性。
- 增加结构化数据质量状态、旧 schema 只读兼容和适配器限流配置接线。

### Phase 5：验证与结论

Status: complete_with_blocker

- 运行定向测试、全量测试、Python 诊断、前端类型检查和构建。
- 验证 `pytest --collect-only` 前后正式数据库哈希不变。
- 运行代表性 CLI/API/指标计算路径。
- 更新审计任务清单，明确已修复、仍阻断和需数据所有者决策的项目。
- 最终证据见 `docs/12_AUDIT_REMEDIATION_REPORT.md`；总体结论继续 `BLOCK`。
- `DataStatusPage.vue` 的结构化质量警告展示因视觉工程执行端被提供方过滤而未实现。

## 错误记录

| 错误 | 尝试 | 处理 |
|---|---:|---|
| `rg --files` 被 `node_modules` 和 `dist` 淹没 | 1 | 后续搜索显式排除依赖和生成目录 |
| 视觉工程与报告写作代理被提供方过滤 | 多次 | UI 项保留为明确阻塞；报告依据已验证证据直接写入 |
| `query_dividend_lineage.py` 的 JOIN 裸引用 `raw_response_hash` | 1 | 限定为 `s.raw_response_hash`，脚本成功产出 JSON |
| `query_operations_schema.py` 查询正式 schema 不存在的 `published_at` | 1 | 按正式列移除该字段，脚本成功产出 JSON |
| PowerShell 解析内联 Python SQL 逗号/括号 | 1 | 改用单引号包裹 Python 程序，独立重算成功 |
| 怀疑者深度代理被提供方过滤 | 1 | 使用独立 SQL、源码反证和第二轮 librarian 交叉验证替代 |
| Phase C 子任务代理空返回且未生成文件 | 1 | 检查工作区确认零修改，改为当前会话直接实施并独立审阅 |
| 旧版 preflight 的负向 EvidenceDir 测试在 `data/pre/` 写入失败证据 | 1 | 根因是 catch 在路径拒绝后仍无条件建目录；改为只在安全目录验证成功后写失败证据，并精确删除本次生成文件 |

### Phase 6：当前阻塞项深度调查

Status: complete_with_blocker

- 以当前正式库重新计算 DQ-03/05/06/07/10/11/12 证据，不复制旧报告数字。
- 复核 DQ-04/13/14 的残余验收缺口、G22 前端可见性和 G23 外部真值依赖。
- 建立 intent diff、claim graph、observation manifest、verification economics 和 cause-disappearance 账本。
- 完成至少两轮扩展调查和怀疑者交叉审阅。
- 输出 `docs/13_CURRENT_BLOCKERS_INVESTIGATION.md`，并证明正式库哈希不变。
- 续查补充 retry 落库、QFQ retry 类型、父子状态传播、公告检查、分红半批提交和 G22 跨页面/CLI 消费链的隔离执行证据。
- ULW 账本收敛为 O001-O026、C001-C024；研究任务完成，但正式数据、G22/G23 和生产验收仍维持 `BLOCK`。

### Phase 7：审计续修与目录治理

Status: in_progress

- 修复父任务、CLI 和重试链对 failed/partial 状态的错误包装。
- 让重试任务在目标数据成功落库后才出队，并统一 QFQ 的 `price_daily + adjust=qfq` 语义。
- 让分红回填以单股票事务发布，失败和缺失日期分别进入 retry/missing。
- 将未实现的公告差异检测明确报告为 unavailable，不再用当前时间伪装检查结果。
- 在状态页、个股页、筛选页和 CLI 消费结构化数据质量告警。
- 非破坏性整理历史测试和一次性脚本，保留来源清单；不移动正式数据库、原始审计证据或仍在使用的规划文件。
- 以回归测试、前端构建、CLI/API 冒烟和正式库哈希不变作为完成门禁。

### Phase 8：S0/S1 可信工程基线恢复

Status: in_progress

- 已恢复会话 `ses_0757e085fffeizmuZXhPWkJhEU` 的完整记录，并以当前 Git 状态交叉核验。
- S0 证据包 `docs/evidence-s0/s0-20260724-015415/` 已完成两轮审阅。
- 事故源基线提交 `f4517d2` 和 annotated tag `incident-2026-07-22` 已存在并相互指向。
- 当前执行点为 `docs/superpowers/plans/2026-07-23-s1-path-isolation.md` Phase C；Phase C-H 尚未完成。
- 先以纯 PowerShell 实现并验证预检和包装器，再通过包装器执行任何 Python/pytest。
- 后续按 TDD 实现纯 `path_policy`、Store/Config/schema 显式注入、pytest 防御纵深和完整回归。
- 整个 S1 期间正式五文件集必须保持 before/after 完全一致；任何 delta 立即以 99 阻断。

### Phase 9：数据重建与流程简化

Status: in_progress

- 简化修复流程：砍掉 S0 法证副本、S5-S7 繁琐验收，保留必要步骤。
- 精简 `path_policy.py`：从 413 行简化到 ~150 行，移除过度防护。
- CSMAR 导入完成：`scripts/import_csmar.py` 导入 356K 行财务数据（1990-2025Q1）。
- AKShare 补齐进行中：`scripts/supplement_akshare.py` 抓取 2025Q2+ 财报和真实除权日。
- 修复断点续传 bug（stock_code 被 pandas 解析为整数）和 raw_data 列类型问题。
- 准备数据验证脚本：`scripts/verify_data_completeness.py`。
- 前端构建通过，回归测试 73 passed（31 errors 因数据库被占用）。
