# 当前复核与修复指引（2026-07-22）

> **裁决: BLOCK / NO-GO**
>
> 本项目是个人本地 Windows 研究工具，用于 A 股基本面分析、指标计算、DSL 筛选和投资决策辅助。当前状态**不可用于**真实投资判断、公开托管、凭证签署、再分发或任何形式的正式使用。当前正式数据库已冻结，正式数据未重建，用户可见性未通过，30 股外部真值抽样未执行。

---

## 目录

1. [文档权威性与超驰规则](#1-文档权威性与超驰规则)
2. [证据快照（2026-07-22）](#2-证据快照2026-07-22)
3. [当前问题与根因](#3-当前问题与根因)
4. [状态模型](#4-状态模型)
5. [问题登记册](#5-问题登记册)
6. [修复路线图 S0-S7](#6-修复路线图-s0-s7)
7. [命令政策](#7-命令政策)
8. [门禁表 G01-G23](#8-门禁表-g01-g23)
9. [正式完成/放行标准](#9-正式完成放行标准)
10. [发布与回滚清单](#10-发布与回滚清单)
11. [证据账本模板](#11-证据账本模板)
12. [角色与批准矩阵](#12-角色与批准矩阵)
13. [立即可执行任务队列（冻结安全）](#13-立即可执行任务队列冻结安全)
14. [前人工作评估](#14-前人工作评估)
15. [假设与局限](#15-假设与局限)
16. [变更控制规则](#16-变更控制规则)

---

## 1. 文档权威性与超驰规则

### 1.1 本文件地位

本文件是整个修复周期的事实性权威指引。它汇总了截至 2026-07-22 的所有审计发现、代码整改状态、正式数据事实和运维缺口，并制定了从当前冻结状态到正式可用的完整修复路线图。所有后续任务必须参照本文件的状态表和门禁表执行。

### 1.2 超驰关系

| 被超驰的文档 | 超驰原因 | 超驰范围 |
|---|---|---|
| `docs/09_PROGRESS_REVIEW_V3.md` | 该文件声称"96% 完成，可进入打包发布"，但基于的是未经红队审计的数据状态。红队审计 V2（docs/11）和后续调查（docs/13）推翻了"数据完整"和"遗留问题 96% 修复"的说法。 | 该文件关于数据完整性、遗留问题状态、项目完成度的所有定量结论均被超驰。其中关于代码结构存在的工作量评估（如 PyInstaller 模板、前端页面结构）仍作为历史记录保留，不作为当前验收证据。 |
| `docs/06_DELIVERY_PLAN.md` | 执行计划中的命令与当前冻结状态冲突，且数据重建路径需要重写。 | 全部执行命令超驰；其阶段划分概念可参考，但不作为当前执行依据。 |
| Cycle 2 计划中的代码状态主张 | 见下方 §1.3 | 逐项标明 |

### 1.3 Cycle 2 计划中超驰的主张

Cycle 2 计划（`.opencode/plans/2026-07-21-remediation-cycle-2.md`）包含代码整改任务，其中部分主张需要根据当前证据重新分类：

| 主张 | 当前分类 | 说明 |
|---|---|---|
| T4: 父任务状态聚合已修复 | `CODE_FIXED_NOT_REVERIFIED` | 源码存在 `aggregate_status()` 和调用点，但正式后端没有独立重新验证。最近一轮验证（2026-07-22）在验证过程中改变了正式库文件，因此后端验证的独立性已受损。 |
| T5: CLI 结果映射/质量集成已修复 | `CODE_FIXED_NOT_REVERIFIED` | `make_response` 和 `_derive_status` 代码存在，但同上原因，后端重新验证被阻断。 |
| T6: CLI data status/diagnose 质量集成已修复 | `CODE_FIXED_NOT_REVERIFIED` | 同上。 |
| T7: 价格 retry 持久化规范化已修复 | `CODE_FIXED_NOT_REVERIFIED` | Retry 规范化代码存在，但后端独立验证缺失。 |
| T8: 分红交易 + retry/missing 记录已修复 | `CODE_FIXED_NOT_REVERIFIED` | 分红原子写入和 retry 记录代码存在，但后端独立验证缺失。 |
| T11: 诚实的 not_implemented 公告检查已修复 | `CODE_FIXED_NOT_REVERIFIED` | `_check_new_announcements` 返回 `not_implemented` 的代码存在，但后端独立验证缺失。 |
| T10: 前端质量警告/新鲜度/保存-导出门禁已修复 | `FRONTEND_QA_PASS_SAFE` | 前端 T10 的实现已通过 mock API 浏览器 QA（19/19 通过）。但这是 mock API 验证，不是真实后端验证。 |

**重要区分：** `CODE_FIXED_NOT_REVERIFIED` 不等于可放行。后端验证必须在测试路径与正式库强隔离之后，在新的 staging 副本上重新执行，才能提升为 `PASS`。

### 1.4 未超驰的已确认阻断

以下数据阻断被 docs/10、docs/11、docs/13 和 docs/14_PRODUCTION_OPS_AUDIT.md 一致确认，本报告不做修改：

- DQ-03: 财务壳行存在于正式库（`DATA_STILL_BLOCKED`）
- DQ-05: 分红日期为期末占位（`DATA_STILL_BLOCKED`）
- DQ-06: 溯源为合成记录（`DATA_STILL_BLOCKED`）
- DQ-07: 指标/现金流陈旧（`DATA_STILL_BLOCKED`）
- DQ-10: raw/QFQ 分裂（`DATA_AND_RECURRENCE_BLOCKED`）
- DQ-11: 元数据缺失（`DATA_STILL_BLOCKED`）
- DQ-12: 操作记录混入正式库（`DATA_AND_OPERATION_TRUTH_BLOCKED`）
- DQ-13: 正式库未迁移（`MIGRATION_BLOCKED`）

### 1.5 生产运维审计（docs/14_PRODUCTION_OPS_AUDIT.md）的继承与超驰

`docs/14_PRODUCTION_OPS_AUDIT.md`（2026-07-22）是较早的只读运维审计。本文件继承其大部分发现（零 Git 提交、备份未验证、构建后端弃用、无日志轮转、无许可证、CI 不存在），并将它们纳入第 5 节问题登记册。

本文件对 `docs/14_PRODUCTION_OPS_AUDIT.md` 作以下明确超驰：

1. **`dist/` 目录状态：** 该运维审计声称 `dist/` 不存在。鉴于现已存在的 `dist/value-dashboard/`（2026-07-18 预修复 exe），该主张已过时。当前状态以本文件 §2.5 的现场证据为准。
2. **日志计数：** 该运维审计的第 6.2 节记录了旧日志文件数。当前本文件 §2.6 记录 78 个日志文件，这一现场计数控制。
3. **整体状态：** 该运维审计的结论（BLOCK）与本文件一致，但其具体证据快照的时效以本文件替换。所有后续工作必须参照本文件的证据快照（§2）、门禁表（§8）和路线图（§6）。

---

## 2. 证据快照（2026-07-22）

### 2.1 正式数据库文件哈希

| 文件 | 当前正式文件 SHA-256 | 批准基线/备份 SHA-256 | 状态 |
|---|---|---|---|
| `data/valuedashboard.duckdb` | `5186E660E603B277B72E4EAF9988963C64B25B83882BA5ACF4BE2789A51268D6` | `46EBCEB6DDBCCA15D4E82D22CFA659EC1C593033DE190C08B5C54FC7211A3C91` | `DRIFTED_FROZEN` |
| `data/valuedashboard.sqlite` | `B7B5F2FF2D1B4D2F71512DFEBD8DC2FBD9625E51BE2D0BFDB5CAF0657EC11959` | `228E0F53A8EBD0B99DAED8FA2D683D42F46644E8805495350EC162EFEC6596D3` | `DRIFTED_FROZEN` |

备份文件（`data/backup/audit_pre_fix_20260720.duckdb`、`data/backup/audit_pre_fix_20260720.sqlite`）的 SHA-256 与批准基线一致。恢复会覆盖当前正式库，未经用户明确批准不得执行。

### 2.2 Git 状态

| 属性 | 值 |
|---|---|
| 当前分支 | `fix/audit-remediation` |
| 提交数 | **0** |
| 文件状态 | 全部 untracked |
| 远程 | `origin -> https://github.com/bootition/value_dashboard.git` |
| 远程分支 | 空仓库（零引用） |
| `.gitignore` | 存在（42 行） |

### 2.3 前端验证状态

| 门禁 | 结果 | 时间 |
|---|---|---|
| `npx vue-tsc --noEmit` | exit 0（通过） | 2026-07-22 |
| Node 合约测试（data-quality + stock-detail + screening-quality） | 46/46 通过 | 2026-07-22 |
| `npm run build` | 成功 | 2026-07-22 |
| `npm audit` | 0 漏洞 | 2026-07-22 |
| Mock 浏览器功能 QA（19 场景 x 375/768/1280） | 19/19 通过 | 2026-07-22 |
| `eslint`（scripts.lint） | **失败** — eslint 命令/包不存在 | 2026-07-22 |

### 2.4 前端构建产物哈希

| 文件 | 关键属性 |
|---|---|
| `frontend/dist/index.html` | `<html lang="zh-CN">` |
| `frontend/dist/assets/index-CjBc_Wo1.js` | SHA-256: `855ADABFEC0763BC6AB60B5B28699CD5C2BF7E48D5923C255894F1AAC94801BF` |
| `app/web/static/index.html` | `<html lang="en">`（**与 frontend/dist 不同步**） |
| `app/web/static/assets/index-ByEHj39Q.js` | SHA-256: `8BA545A9B4EE478486F2193F25DA62E704FF9AEAF4B6ADB9E938F8ABFF403822` |

**关键发现：** `app/web/static/` 是后端服务的静态资源目录。它当前的入口 JS 和 `lang="en"` 声明与 `frontend/dist/` 不一致。这表明自上次构建后，`frontend/dist/` 未同步到 `app/web/static/`，或者同步时使用了旧版本。正式发布前必须同步并验证。

### 2.5 现存 exe 产物

| 属性 | 值 |
|---|---|
| 路径 | `dist/value-dashboard/value-dashboard.exe` |
| 时间戳 | 2026-07-18 |
| 完整 dist 捆绑大小 | 783.52 MiB |
| 与文档目标对比 | 超过 `<500 MiB` 目标 |
| 与当前代码关系 | 预修复版本，不包含任何 Cycle 2 前端或后端修复 |

### 2.6 缺失基础设施

| 项 | 状态 |
|---|---|
| `data/.hashes` 锁文件 | 不存在 |
| `tests/regression/test_hash_preservation.py` | 不存在 |
| `CHANGELOG.md` | 不存在 |
| `LICENSE` | 不存在 |
| CI 配置（`.github/`） | 不存在 |
| 日志轮转配置 | 无 — 当前 `data/logs/` 有 78 个日志文件 |

### 2.7 数据行数（来自 docs/13 证据，本次未重新查询）

以下数据源自 docs/13 的只读查询证据（2026-07-20），**不是**本报告执行的新查询：

| 表 | 行数 | 股票代码数 | 日期范围或备注 |
|---|---|---|---|
| `stock_meta` | 5,528 | 5,528 | 行业一级填充 0 |
| `price_daily_raw` | 17,230,780 | 5,541 | 1990-12-19 至 2026-07-17 |
| `price_daily_qfq` | 16,890,310 | 5,200 | 1990-12-19 至 2026-07-17 |
| `balance_sheet` | 323,514 | 5,176 | 1990-12-31 至 2026-03-31 |
| `income_statement` | 323,691 | 5,176 | 1990-12-31 至 2026-03-31 |
| `cash_flow` | 309,304 | 5,129 | 1990-12-31 至 2025-03-31 |
| `dividends` | 44,883 | 4,979 | 1990-12-31 至 2024-12-31（ex_date 100% 期末占位） |
| `indicator_snapshot` | 5,129 | 5,129 | 2024-09-30 至 2025-03-31 |
| `source_audit` | 15,649 | 5,541 | 全部孤儿/空哈希 |
| `fetch_batch` | 133 | 不适用 | 全部价格批次，不被 source_audit 引用 |

**重要说明：** 由于正式库已漂移，docs/13 的行数证据可能与当前文件不完全一致。当前文件可能已发生变化。**在测试路径与正式库强隔离之前，不得运行任何查询当前正式库的命令。**

---

## 3. 当前问题与根因

### 3.1 正式数据库漂移与冻结

当前正式 DuckDB 和 SQLite 的哈希与批准基线不匹配。两次已验证的事故（2026-07-22）通过 Python 测试导入改变了正式文件。早先提出的"mmap 延迟写回"解释已撤回。当前正式库不能被视为已验收数据。

### 3.2 旧不可信数据

正式库包含以下经过审计确认的不可信数据：

- **财务壳行（DQ-03）**: 2025-Q2 至 2026-Q1 的资产负债表核心字段为 0/空，但被按"最新完整财报"排序。BaoStock 脚本将财务指标写成完整报表行。
- **占位分红日期（DQ-05）**: 44,883 条分红的 `ex_date` 100% 落在 12-31/06-30，`announcement_date` 全部为 0。指标仍然消费这些日期计算股息率、DPS、连续分红年数。
- **合成/无效溯源（DQ-06）**: 15,649 行 `source_audit` 全部为空哈希且孤儿，无法关联实际 `fetch_batch`。
- **陈旧现金流/快照（DQ-07）**: 现金流量表停在 2025-03-31，指标快照停在 2025-03-31。价格已到 2026-07-17，相差约 16 个月。
- **raw/QFQ 分裂（DQ-10）**: 5,541 raw vs 5,200 QFQ。341 raw-only（328 BSE + 13 个未补零重复代码）。4 个正常化代码仍有范围/行数分裂。
- **元数据缺失/错误（DQ-11）**: 申万覆盖 0；5,528 行 `is_suspended=FALSE`（未知被编码为 false）。
- **操作测试工件（DQ-12）**: 3 条测试筛选结果、8 条未回滚覆写、2 条陈旧的 `running` 作业。
- **正式库未迁移（DQ-13）**: QFQ 缺 `turnover_rate`；SQLite 只有 v1。

### 3.3 后端验证隔离不足

Cycle 2 验证期间，Python 测试命令改变了正式库文件。这证明当前测试路径与正式库之间没有足够的隔离。在测试路径与正式库完成强隔离之前，不得运行任何 Python/pytest/CLI/app 命令。

### 3.4 门禁 G23 未执行

30 只跨市场股票的外部真值抽样和人工签署尚未开始。没有外部基准，无法确定当前数据的准确率。

### 3.5 发布产物陈旧

当前存在的 `dist/value-dashboard/value-dashboard.exe` 是 2026-07-18 的预修复版本，大小 783.52 MiB 超过 <500 MiB 目标。它不包含任何 Cycle 2 的前端或后端修复。`app/web/static/` 的版本与 `frontend/dist/` 不同步。

### 3.6 零版本历史

当前仓库零 Git 提交、零对象。无法追溯引入、回滚或审计变更。

### 3.7 残留代码缺口

- 增量更新只写 raw，可以在全量回填通过后再次制造 raw/QFQ 分裂。
- 公告比较未实现（返回 `not_implemented`），不能发现正式财务数据已落后。
- CSV 导出缺少质量元数据（数据日期、规则版本、指标版本、置信度）。
- 静默的细节获取降级（如 `fetch(balance_sheet, 600519)` 返回 `source=local_cache`、`confidence=missing`、`hash_length=0`）可能被误解为合法空数据。
- 备份恢复缺少独立安全验证和原子性验证。

### 3.8 运维与发布卫生

- `pyproject.toml` 使用已弃用的 `setuptools.backends._legacy:_Backend` 构建后端。
- 无 CHANGELOG、无 LICENSE、无 CI。
- 日志无轮转（当前 78 个日志文件）。
- lint 脚本 (`scripts.lint = "eslint ..."`) 不可执行 — eslint 包/命令不存在。

---

## 4. 状态模型

本报告使用以下标准状态标签。每个问题/门禁/任务必须有一个且仅一个标签。

| 标签 | 含义 | 使用条件 |
|---|---|---|
| `PASS` | 已通过所有规定的验证 | 有可重复的证据（测试通过、构建成功、哈希匹配、人工签署） |
| `FAIL` | 已验证失败 | 有明确的失败证据（测试失败、哈希不匹配、构建错误） |
| `NOT_EXECUTED` | 从未执行过该验证 | 没有尝试过的证据 |
| `CODE_FIXED_NOT_REVERIFIED` | 代码已修复，但尚未在隔离后端独立重新验证 | 源码存在修复，但后端验证因冻结而无法执行 |
| `FRONTEND_QA_PASS_SAFE` | 前端代码已通过 mock API 验证（不影响后端/数据库） | vue-tsc、构建、Node 合约、mock 浏览器 QA 均通过；专门用于前端修复 |
| `DATA_STILL_BLOCKED` | 正式数据有经过审计确认的问题，修复只能由数据重建解决 | DQ-03/05/06/07/11 等经 docs/11 和 docs/13 确认 |
| `DATA_AND_RECURRENCE_BLOCKED` | 数据问题 + 增量路径再次制造同类的风险未消除 | DQ-10 raw/QFQ 分裂：全量回填后增量只写 raw 可再次制造分裂 |
| `DATA_AND_OPERATION_TRUTH_BLOCKED` | 数据问题 + 运维记录真实性受损，清理前操作不可信 | DQ-12 测试筛选/未回滚覆写/陈旧 running 作业混入正式库 |
| `MIGRATION_BLOCKED` | 正式库 schema 与代码声明不一致，迁移未执行 | DQ-13 QFQ `turnover_rate`、SQLite v2 等 |
| `DRIFTED_FROZEN` | 文件哈希与批准基线不一致，且当前禁止写入 | 当前正式 DuckDB/SQLite |
| `OWNER_APPROVAL_REQUIRED` | 需要项目所有者或数据负责人明确批准才能继续 | 导出/清理既有操作记录、批准备份恢复、签署 G23 |

---

## 5. 问题登记册

### 5.1 P0（阻断 — 必须解决才能放行）

| # | ID | 描述 | 严重度 | 证据来源 | 根因 | 影响 | 修复措施 | 所有者/批准 | 退出证据 | 当前状态 |
|---|---|---|---|---|---|---|---|---|---|---|
| P0-01 | DQ-03-DATA | 2025-Q2 至 2026-Q1 财务壳行为最新完整财报 | P0/BLOCK | docs/11 §3, docs/13 §3.1 | BaoStock 的 `.omo/run_financial_backfill.py` 将财务指标写成完整报表行 | 估值、ROE、ROA、负债率无法从最新行计算 | 隔离壳行并重建完整三表包 | 数据负责人 | staging 质量报告（三表交集 >= 门限） | `DATA_STILL_BLOCKED` |
| P0-02 | DQ-05-DATA | 44,883 条分红的 ex_date 全部为期末占位 | P0/BLOCK | docs/11 §4.1, docs/13 §3.2 | CSMAR 使用报告期作为 ex_date | 股息率、DPS、分红率、连续分红年数全部不可信 | 从交易所实施公告重建分红事件 | 数据负责人 | G12 通过 + G23 抽样 match | `DATA_STILL_BLOCKED` |
| P0-03 | DQ-06-DATA | 15,649 条 source_audit 为空哈希且孤儿 | P0/BLOCK | docs/11 §4.1, docs/13 §3.3 | `.omo/fix_p0_08_audit.py` 执行事后 INSERT COUNT(*) | 溯源页面制造虚假可信感 | 降级/隔离旧合成记录，建立真实哈希溯源 | 数据负责人 | G13 通过 | `DATA_STILL_BLOCKED` |
| P0-04 | DQ-07-DATA | 现金流/快照停 2025-03-31，价格到 2026-07-17 | P0/BLOCK | docs/11 §4.3, docs/13 §3.1 | 现金流从未通过有效适配器补抓 | 现金流指标/估值无法反映最新报告 | 补齐现金流后重建快照 | 数据负责人 | G07/G08/G09 通过 | `DATA_STILL_BLOCKED` |
| P0-05 | DB-DRIFT | 正式库哈希与批准基线不一致 | P0/BLOCK | README, docs/14_PRODUCTION_OPS_AUDIT.md §7.2 | 2026-07-22 两次 Python 测试事故 | 不能信任当前正式库的任何数据 | S0 冻结 + S1 强隔离 + S3 staging 重建 | 项目所有者 | 新批准基线记录 + staging 候选 | `DRIFTED_FROZEN` |
| P0-06 | G22-FAIL | G22 用户可见性未完全通过 | P0/BLOCK | docs/13 §4 | 前端消费者已在 T10 实现且 mock QA 通过，但真实后端验证、已部署静态资产同步、CLI/backend 路径的独立复验和 CSV 元数据闭环均未完成 | 用户风险提示在真实后端下未验证；静态资产不一致；CLI/backend 虽有源码接线但未隔离复验 | T10 前端底座 + S5 真实后端 QA + S7 静态同步 + CSV 元数据闭环 | 实施审核人 | G22 PASS + 真实后端浏览器 QA | `CODE_FIXED_NOT_REVERIFIED` |
| P0-07 | G23-NOT-DONE | G23 外部真值 30 股抽样未执行 | P0/BLOCK | docs/13 §5 | 缺少外部真值比对的独立流程 | 无法判定当前数据准确率 | S6 的 G23 执行 | 独立真值审核人 | 30 股证据包 + 签署表 | `NOT_EXECUTED` |
| P0-08 | ZERO-GIT | 零 Git 提交、零对象、远程空 | P0/BLOCK | docs/14_PRODUCTION_OPS_AUDIT.md §11 | 从未初始化版本历史 | 无法追溯引入、回滚或审计变更 | S1 初始源基线提交（含 incident 标签）+ S7 发布提交与 v1.0.0 tag | 项目所有者 | S1 基线提交记录 + S7 release tag 证据 | `NOT_EXECUTED` |
| P0-09 | FROZEN-UNSAFE-VERIFY | 后端验证隔离不足，pytest 可改变正式库 | P0/BLOCK | README, docs/14_PRODUCTION_OPS_AUDIT.md §11 | 测试与正式库没有强路径隔离 | 验证不可信；任何 Python 运行都可能改变正式库 | S1 强测试/数据路径隔离 + 哈希门禁 | 实施审核人 | S1 退出证据 | `NOT_EXECUTED` |
| P0-10 | MIGRATION-PENDING | 正式库未迁移（QFQ turnover_rate、SQLite v2） | P0/BLOCK | docs/13 §3.7, docs/11 DQ-13 | 代码已修但正式库未执行迁移 | Schema 漂移，写入可能失败 | S4 创建已迁移的 staging 发布候选（无 in-place 正式库迁移）、S7 晋级时推送该候选的精确字节 | 数据负责人 | G18 通过 | `MIGRATION_BLOCKED` |

### 5.2 P1（高 — 修复前放行不可接受）

| # | ID | 描述 | 严重度 | 证据来源 | 根因 | 影响 | 修复措施 | 所有者/批准 | 退出证据 | 当前状态 |
|---|---|---|---|---|---|---|---|---|---|---|
| P1-01 | RAW-QFQ-SPLIT | 341 raw-only + 4 代码分裂；增量只写 raw | P1/HIGH | docs/13 §3.4 | 增量更新只抓 raw，BSE 不支持 QFQ，未补零代码 | raw/QFQ 历史长度不一致；回测口径不一致 | S3 对齐 + 锁死增量路径 | 数据负责人 | G16/G17 通过 | `DATA_AND_RECURRENCE_BLOCKED` |
| P1-02 | META-MISSING | 申万覆盖 0；is_suspended 全部 false | P1/HIGH | docs/13 §3.5 | stock_list 不返回行业；未知停牌被编码为 false | 行业排名不可用；ST/停牌筛选失效 | S3 来源重建 | 数据负责人 | G19 通过 | `DATA_STILL_BLOCKED` |
| P1-03 | OPS-RECORDS | 3 条测试筛选 + 8 条未回滚覆写 + 2 条陈旧 running | P1/HIGH | docs/13 §3.6 | 验收脚本写入正式 SQLite；无作业 heartbeat | 筛选历史含假结果；作业状态不可信 | 导出确认 + 清理 + 状态治理 | 数据负责人（OWNER_APPROVAL_REQUIRED） | G20/G21 通过 | `DATA_AND_OPERATION_TRUTH_BLOCKED` |
| P1-04 | STATIC-OUT-OF-SYNC | `app/web/static/` 与 `frontend/dist/` 版本不一致 | P1/HIGH | §2.4 本报告 | 构建后未同步到后端静态目录 | 发布后用户得到旧版前端 | S7 同步静态 + 构建验证 | 项目所有者 | G01-G23 全部通过后 S7 执行 | `NOT_EXECUTED` |
| P1-05 | EXE-STALE | 现存 exe 是 2026-07-18 预修复版本，783 MiB | P1/HIGH | §2.5 本报告 | 未在代码修复后重建 PyInstaller 产物 | 使用者可能运行旧版本，错过所有修复 | S7 重建 exe + smoke test | 实施审核人 | S7 退出证据 | `NOT_EXECUTED` |
| P1-06 | NO-ROLLBACK-PLAN | 没有记录的恢复/回滚计划和演练 | P1/HIGH | docs/14_PRODUCTION_OPS_AUDIT.md §7.3 | 从未端到端验证恢复程序 | 灾难发生时无法恢复 | S0/S7 编写并演练回滚 | 项目所有者 | 回滚演练 signed off | `NOT_EXECUTED` |
| P1-07 | DATA-RIGHTS-UNCLEAR | 无许可证/使用声明 + 数据源权利未审计 | P1/HIGH | docs/14_PRODUCTION_OPS_AUDIT.md §9 | 从未添加 LICENSE，未审查各数据源（AKShare/Eastmoney、BaoStock、CNINFO、TDX、CSMAR 等）的引用/许可条款/再分发限制 | LICENSE 文件缺失；CSMAR 等商业数据许可状态不明确；无法确定当前正式库的哪些数据可被合法使用或分发 | S2 建立 LICENSE/使用声明和数据源权利登记册；S3 只允许权利已确认的数据进入候选；S7 复核签署 | 项目所有者 | LICENSE/使用声明存在 + 数据源权利登记册签署 + 候选数据中无排除项 | `NOT_EXECUTED` |

### 5.3 P2（中 — 应在发布前解决，但放行前可接受有限豁免）

| # | ID | 描述 | 严重度 | 证据来源 | 根因 | 影响 | 修复措施 | 所有者/批准 | 退出证据 | 当前状态 |
|---|---|---|---|---|---|---|---|---|---|---|
| P2-01 | LINT-FAIL | `eslint` 命令/包不存在，lint 脚本失败 | P2/MED | §2.3 本报告 | 从未安装 eslint 或配置 | 代码风格无法自动检查 | 安装 eslint 或移除脚本 | 实施审核人 | `npm run lint` 通过 | `NOT_EXECUTED` |
| P2-02 | NO-LOG-ROTATION | 78 个日志文件，无轮转/保留策略 | P2/MED | §2.6, docs/14_PRODUCTION_OPS_AUDIT.md §6.2 | 日志配置只设 level/format | 长期运行磁盘填满 | 添加 RotatingFileHandler | 实施审核人 | 日志配置包含轮转 | `NOT_EXECUTED` |
| P2-03 | NO-CHANGELOG | 无 CHANGELOG.md | P2/MED | §2.6, docs/14_PRODUCTION_OPS_AUDIT.md §8 | 从未创建 | 无法追踪版本间变更 | S7 创建 CHANGELOG | 项目所有者 | CHANGELOG.md 存在 | `NOT_EXECUTED` |
| P2-04 | BUILD-BACKEND-DEPRECATED | `pyproject.toml` 使用弃用的 `_legacy` 构建后端 | P2/MED | docs/14_PRODUCTION_OPS_AUDIT.md §2.1 | 从未更新 | `pip install -e .` 在 setuptools >= 72 下失败 | 迁移到 setuptools.setuptools 或 hatchling | 实施审核人 | `pip install -e .` 成功 | `NOT_EXECUTED` |
| P2-05 | NO-HASH-LOCK | `data/.hashes` 和 `test_hash_preservation.py` 不存在 | P2/MED | §2.6, Cycle 2 T3 | T3 被推迟且未重新申请 | 无自动检测正式库漂移的机制 | S1 创建并验证 | 实施审核人 | S1 退出证据 | `NOT_EXECUTED` |
| P2-06 | INCREMENTAL-RAW-ONLY | 增量更新仍只写 raw，全量回填后可再次制造 raw/QFQ 分裂 | P2/MED | docs/13 §3.4 | 增量路径未要求 raw/QFQ 同时更新；T7 未涉及此问题 | 全量回填后增量运行可制造新分裂 | S2 实现增量 raw/QFQ 一致维护（非仅确认 T7） | 实施审核人 | 增量路径 raw/QFQ 一致门禁 + 隔离验证 | `DATA_AND_RECURRENCE_BLOCKED` |
| P2-07 | RETRY-NO-TARGET-WRITE | 通用 retry 成功只删队列不写目标 | P2/MED | docs/13 §3.6 | retry 流程不调用 DuckDB 写入 | 重试成功但数据未修复 | T7 已修复，但需隔离验证 | 实施审核人 | 隔离验证 PASS | `CODE_FIXED_NOT_REVERIFIED` |
| P2-08 | PARENT-STATUS-MASK | 父任务/CLI 可把 partial 包装成 success/ok | P2/MED | docs/13 §3.6 | 聚合函数和 make_response 未修复 | 运维报告不真实 | T4/T5 已修复，但需隔离验证 | 实施审核人 | 隔离验证 PASS | `CODE_FIXED_NOT_REVERIFIED` |

---

## 6. 修复路线图 S0-S7

### 整体依赖

```
S0 (冻结与证据保存——法证副本)
  |
  v (需要项目所有者批准)
S1 (事故源基线 + 路径隔离 + 分层哈希账本 + incident 标签)
  |
  v
S2 (源码闭包 — raw/QFQ 增量维护实现 + 安全/发布缺口闭包)
  |
  v
S3 (staging 数据重建 + 溯源重建)
  |
  v
S4 (staging 发布候选迁移 + 运维状态治理 — 不得触碰正式库)
  |
  v
S5 (staging 发布候选冻结 + G01-G22 全体验收 + 真实后端 G22)
  |
  v
S6 (G23 30 股外部真值抽样 + 人工签署 — 对不可变候选)
  |
  v
S7 (晋级前回滚演练 → 预构建工件 → 候选签名 → 停止服务 → 预发布备份 → DB 晋级 → 冒烟 → 更新哈希 → 最终提交/v1.0.0 tag（标签最后）)
```

### S0：冻结与证据保存

**前置条件：** 无
**执行约束：** 禁止 backend/app/pytest/CLI/DB/PyInstaller 命令。禁止打开 DuckDB/SQLite 引擎。禁止查询正式库。
**状态：** `NOT_EXECUTED`

**允许的操作：**
- 使用 `Get-Process`、`Get-NetTCPConnection`（或其他端口枚举工具）记录运行中进程和端口。
- 使用 `Get-FileHash` 获取文件的 SHA-256（不打开 DB 引擎）。
- 使用 `Get-ChildItem` 记录目录树和文件大小（递归、仅元数据）。
- 引用 docs/13 等先前报告中的历史行数、schema 和样本行（这些是预期值参考，非新查询）。
- 编写 S0 证据包：运行进程清单、文件哈希表、目录大小汇总。
- 编写 S1 路径隔离合约设计文档。
- 如需在追踪的 `docs/evidence-s0/` 中保留小体积证据清单（如文件清单 JSON、哈希表），可以创建；但逐字节 DB 副本**不得**放入 Git 工作空间。
- **经项目所有者书面批准后执行以下一次性操作：** 在服务停止并确认相关 DB 进程全部退出后，对当前已漂移的正式 DB 文件集制作逐字节法证副本，存入**所有者在 Git 工作空间外指定的证据位置**（例如 `D:\Mr.Q\掌控经济\value-dashboard-incident-evidence\2026-07-22\`）。文件集包括主 DuckDB/SQLite 文件，以及当时实际存在的 DuckDB `.wal`、SQLite `-wal`、`-shm` 伴生文件；不存在也必须记录为 absent。**不得为了取证运行 checkpoint、VACUUM 或打开 DB 引擎**，因为这会改变事故现场。运行手册必须验证父目录和访问控制。复制后逐文件比较源/副本的相对名称、字节数和 `.Hash` 属性；任何文件缺失或哈希不一致立即停止。此副本仅用于法证存证，**不得进入 Git 追踪**，不得用于后续重建或 staging。法证文件集是第 2.1 节事故冻结状态的持久凭证。

**禁止事项：**
- 不得打开 DuckDB/SQLite 引擎或运行任何查询。
- 不得对正式库或备份运行 `SELECT`、`DESCRIBE`、`.tables` 等。
- 不得运行 Python/pytest/CLI/app/PyInstaller。
- 不得将法证副本用于 staging 重建（S3 必须使用备份副本）。
- **不得将法证 DB 副本放入 Git 工作空间或 `.gitignore` 试图隐藏。** 它们必须位于外部路径。

**退出证据：**
- 追踪的 `docs/evidence-s0/` 目录包含小体积证据清单（进程清单、哈希表、目录汇总）。
- 如已执行：外部法证副本目录路径记录在证据清单中 + `$src.Hash -eq $dst.Hash` 比较记录。
- S1 隔离合约草稿已完成并记录。
- 项目所有者签署 S0 完成确认。

**回滚边界：** 不适用——S0 不改变应用/数据状态。S0 证据是永久的事故边界记录，不得删除。

---

### S1：强测试/数据路径隔离与哈希门禁

**前置条件：** S0 完成 + 项目所有者明确批准
**执行约束：** 只能在 isolation 目录或 staging 副本上执行。不得操作正式库路径。
**状态：** `NOT_EXECUTED`

**必须完成：**

**0. 在任何 S1 源码/配置变更前创建初始事故源基线：**
   - 完成秘密/第三方数据审查，确认正式库、备份、原始商业数据、凭据和本地工具状态不进入 Git。
   - **不得使用 `git add .`。** 逐项 stage 非数据源文件：源码（`app/`、`config/`、`frontend/src/` 等）、当前已部署的静态文件（`app/web/static/`）、文档和配置文件。
   - 明确排除 `data/`、`dist/`、`node_modules/`、`__pycache__/` 和 `frontend/dist/`。
   - 创建初始提交并用 annotated tag `incident-2026-07-22` 永久标记。该标签必须先于后续 S1 隔离实现，才能真实表示整改前事故边界。
   - 远程推送属于独立外部副作用，只能在项目所有者另行批准后执行。

**1. 创建分层哈希清单 `data/.hashes`：**
   - **(a) 批准重建源备份哈希（rebuild-source）**：`data/backup/audit_pre_fix_20260720.duckdb`/`.sqlite` 的 SHA-256。这些是 S3 staging 重建的源。
   - **(b) 观察到的事故冻结哈希（incident）**：当前正式库文件的 SHA-256（即 §2.1 的 `DRIFTED_FROZEN` 值）。这些是 S0 法证副本的对应凭证，不代表可信数据状态。
   - **(c) 正式发布种子哈希（released-formal-seed）**：初始为 `null`。S7 候选晋级后、首次运行前记录，必须与签署候选完全一致；它是发布边界，不是对运行中可变文件的永久相等断言。
   - **(f) 运行观察哈希（runtime-observed）**：初始为空。正式运行后按时间追加文件哈希、允许的运行时写入和语义差异证据，不覆盖发布种子哈希。
   
   **`.gitignore` 处理：** 当前 `.gitignore` 中应有一行 `data/` 将整个目录排除。这行**必须被替换**为以下两条规则——注意顺序重要，且如果 `data/` 行保留，其后的 `!` 例外无效，因为 git 不重新包含已排除目录下的文件：
   ```
   data/*
   !data/.hashes
   ```
   第一条排除 `data/` 下所有内容。第二条只白名单 `data/.hashes` 使之可被追踪。`.hashes` 是 `data/` 下唯一应进入 Git 的文件。如果 `.gitignore` 中已有 `data/` 行，必须将其改为以上两条，而不是在其后追加。

**2. 创建哈希保存门禁（不对等断言）：**
   创建 `tests/regression/test_hash_preservation.py`，并在 pytest 外层包装器或 session-scoped autouse fixture/finalizer 中实现整轮门禁；普通测试函数无法在整个测试会话结束后自行取证。门禁必须：
   - 在 pytest 会话开始前**动态捕获**当前正式 DB 对的文件 SHA-256（使用 `Get-FileHash` 等效的 Python 调用，仅读取文件字节，不打开 DB 引擎）。
   - 在 pytest 会话全部 teardown 完成后**再次动态捕获**正式 DB 对的 SHA-256。
   - 断言前后哈希**不变**（`assert before == after`）。
   - **不要**断言当前漂移正式文件等于备份基线哈希。备份基线是重建源，不是当前正式库的预期值。
   - 测试代码应通过配置/环境变量读取正式库路径；不得硬编码路径。

**3. 创建路径隔离合约：**
   设计并实施路径隔离：所有 pytest/CLI/app 通过环境变量或配置开关指向隔离路径；项目正式库路径在代码层写保护。

**4. 提交 S1 隔离与哈希门禁实现：**
   - 将步骤 1-3 的 `.gitignore`、哈希清单、测试门禁和隔离合约作为新的整改提交记录在步骤 0 的事故标签之后。
   - 不得 amend、移动或删除 `incident-2026-07-22` 标签；后续修复均使用新提交保留因果链。
   - 提交前再次确认仅 `data/.hashes` 可从 `data/` 下被追踪。

**5. 建立强制门禁与自动化：**
   - `pytest --collect-only` 前后自动校验 `test_hash_preservation.py`。
   - 将 pytest 的隔离要求文档化并自动化。

**禁止事项：**
- 不得锁当前漂移哈希作为批准基线（当前哈希不代表可信状态）。
- 不得修改正式库路径上的文件。
- 不得使用 `git add .` 或追踪 `data/` 或原始商业数据。
- 提交前必须完成秘密/第三方数据审查。

**退出证据：**
- 路径隔离合约文档（定义正式库与隔离路径映射、环境变量/配置开关、pytest 默认行为）。
- `data/.hashes` 存在且包含 rebuild-source、incident、released-formal-seed=null、runtime-observed 空账本。
- `.gitignore` 包含 `!data/.hashes` 例外。
- `test_hash_preservation.py` 存在且在隔离路径上通过。
- 隔离前后正式库哈希不变的自动化测试证据（before/after 动态哈希）。
- 初始源基线提交 + `incident-2026-07-22` 标签；只有在项目所有者另行批准推送后，才要求附远程备份确认。
- 秘密/第三方数据审查记录。

**回滚边界：** `incident-2026-07-22` 标签和初始基线提交是永久性证据，**不得**删除或变更。如果 S1 引入的代码变更失败，通过修正代码或 `git revert <S1-remediation-commit>` 创建新的前向历史，同时保留完整事故链。`.hashes` 的重建源/事故哈希不得删除，只能追加或更正并留下审计说明。

---

### S2：源码闭包

**前置条件：** S1 完成
**执行约束：** 仅修改源码；不运行后端集成测试（前端测试安全）；后端测试在隔离路径执行。
**状态：** `NOT_EXECUTED`

**必须完成：**
- **预防复发（增量 raw/QFQ 维护的实现——非仅确认）：**
  - **增量 raw/QFQ 一致维护：** 审查并修复增量更新路径，使其同时维护 raw 和 QFQ 数据。当前增量更新仅写 raw（`data/scripts/incremental_update.py` 或等价位置），QFQ 行数在全量回填后不会自动增长。S2 必须修改增量路径，确保每次增量运行后 raw 与 QFQ 的股票代码和最新日期保持一致。实施后编写门禁测试，验证增量运行不制造新分裂。此问题未被 T7 修复（T7 仅涉及 retry 目标写入），因此是 `DATA_AND_RECURRENCE_BLOCKED` 的新实现。
  - 确认父任务状态聚合和 CLI 映射已正确实现（检查 T4/T5 修复的源码）——这些仍是 `CODE_FIXED_NOT_REVERIFIED` 确认。
  - 确认 retry 目标写入已正确实现（检查 T7 修复的源码）——保持 `CODE_FIXED_NOT_REVERIFIED`。
  - 确认分红原子事务 + retry/missing 已正确实现（检查 T8 修复的源码）。
  - 确认公告检查返回 `not_implemented`（检查 T11 修复的源码）。
  - 确认 TTM/YoY/QoQ 公式正确（检查 Phase 0 DQ-09 修复的源码）。
- **安全闭包：**
  - 确认 DQ-04（适配器名归一化 + 未知拒绝）已正确实现。
  - 确认 DQ-08（pytest 安全收集）已正确实现。
  - 确认 DQ-02（staging + 事务发布）已正确实现。
  - 备份恢复安全验证：确认独立安全审查（不依赖代码存在，而是审查备份恢复流程是否包含表名/列名校验）。
- **复发闭包——陈旧作业 heartbeat：**
  - 审查并实现作业 heartbeat/lease 超时/对账机制：当前运行中作业缺少 heartbeat，使得 `running` 状态可以永远卡住。S2 必须为长期运行作业（如数据回填、指标计算）添加以下一项或多项：
    * 周期性 heartbeat 写入（数据库时间戳或文件 mtime）。
    * 启动时/查询时的 lease 超时检测（如 `last_heartbeat < now - timeout` → 标记失败/可重试）。
    * 对账命令（`vd data reconcile`）扫描卡住的作业。
  - 实现后编写门禁测试验证陈旧作业可被检测和处理。注意：S2 实现 heartbeat 机制；S4 仅使用该机制对现存记录分类和清理。
- **发布缺口闭包：**
  - 确认 `data/.hashes` 锁文件和 `test_hash_preservation.py` 存在（S1）。
  - 建立 LICENSE/使用范围声明与数据源权利登记册，逐源记录引用、许可/条款、允许用途、分发限制和审定人。权利未知或不允许本项目用途的数据源必须在 S3 重建前列入排除清单。

**禁止事项：**
- 不得引入新的 Python 后端依赖（前端 devDependency 如 eslint 的安装/修复可在授权后执行，属于 S2 工作范围）。
- 不得修改正式库文件。

**退出证据：**
- 所有"确认"项有源码审查记录。
- 前端构建和 Node 合约在 S2 后仍通过。

**回滚边界：** 通过新的前向提交回滚有缺陷的源码修改（`git revert`），保留 S1 基线之后的所有历史和事故标签。不得使用 `git reset` 或任何破坏性历史重写命令。

---

### S3：Staging 数据重建与溯源重建

**前置条件：** S2 完成 + 数据负责人明确批准
**执行约束：** 只能在 staging 副本上执行。不得覆盖正式库。
**状态：** `NOT_EXECUTED`

**必须完成：**
- 从备份创建 staging 副本（**不是**从当前漂移的正式库）。使用 `Copy-Item data/backup/audit_pre_fix_20260720.duckdb data/staging/valuedashboard.duckdb`。
- 从备份创建 SQLite staging 副本。
- **壳行隔离：** 识别并隔离 BaoStock 壳行。导出隔离 Parquet，行数和哈希。从 staging 的完整财报表中移除壳行。
- **完整三表重建：** 通过修复后的适配器抓取完整三大报表。校验每表的必填字段门限。原子发布到 staging。
- **分红事件重建：** 从交易所实施公告重建。保存公告 ID/URL、PDF SHA-256、记录日、除权除息日、支付日、实施状态。未知日期保持 NULL。原子发布到 staging。
- **溯源重建：** 建立 source_entity -> extraction_activity -> mapped_field 链。旧合成摘要降级为 `synthetic_summary`，从字段级审计视图排除。
- **raw/QFQ 对齐：** 对齐 staging 中的 raw/QFQ。13 个未补零重复代码经数据负责人批准后隔离。
- **元数据重建：** 通过适配器获取行业分类。未知状态保持 NULL 而非 false。
- **指标快照重建：** 在 staging 上计算。交易发布。与旧快照生成差异报告。
- **生成 staging 质量报告：** 三表完整度、分红行数/占位比例、溯源孤儿数、raw/QFQ 对齐度、元数据完整度。
- **执行数据权利门禁：** 逐项比对 S2 权利登记册和实际 lineage/fetch_batch/source 标识。排除清单中的数据不得进入 staging 发布候选；无法确认来源的记录按未获权处理并隔离。

**禁止事项：**
- 不得直接操作正式库路径。
- 不得将 staging 数据视为已验收。
- 不得使用"按日期 > X 批量删除"代替逐行验证。

**退出证据：**
- staging 质量报告（所有指标通过）。
- staging 与备份的差异报告。
- 数据负责人签署 staging 数据质量确认。

**回滚边界：** 删除 staging/ 目录；恢复到备份副本。不影响正式库。

---

### S4：staging 发布候选迁移与运维状态治理

**前置条件：** S3 完成 + 数据负责人明确批准
**执行约束：** 仅操作 staging 发布候选路径。**不得修改当前正式 DB。** 正式库写操作推迟至 S5/S6 验收通过后、S7 正式发布时。
**状态：** `NOT_EXECUTED`

**必须完成：**
- **DuckDB staging 发布候选迁移：** 在 staging 上演练 QFQ `turnover_rate`、快照 `calculated_at/data_version`、nullable ST/停牌默认值。验证幂等、行数、主键、双向 schema diff、API 行为。
- **SQLite staging 发布候选迁移：** 在 staging 上演练 published 唯一索引。验证幂等。
- **候选文件集收敛：** 在候选冻结前关闭全部连接。允许在仍可变的 staging 候选上执行受控 checkpoint，把 SQLite WAL 完整并入主文件；随后关闭连接并确认 SQLite `-wal` 不存在或为 0 字节、`-shm` 可安全移除，DuckDB `.wal` 也不存在。记录 checkpoint 前后文件集和语义校验。只有收敛后的主 DuckDB/SQLite 文件可进入 S5 冻结；这一步不得用于 S0 法证现场或当前正式库。
- **操作记录治理（staging 发布候选）：**
  - 导出 3 条测试筛选结果 + 8 条覆写 + 2 条 running 作业记录。
  - 数据负责人逐条确认是否清理/保留/发布。
  - 执行清理（仅在 staging 发布候选上）。
- **作业 heartbeat 验证：** 确认 S2 实现的 heartbeat/lease 超时机制可在 staging 发布候选上运行。使用该机制检测并分类陈旧作业（不应遗留活着的陈旧作业，但已有的由数据负责人逐条确认）。S2 负责源码实现；S4 只使用已有机制对现存记录分类和清理。
- **当前正式 DB 不得被修改：** S4 不执行正式库写入。S7 不再对旧正式库做原地迁移，而是把已完成迁移和治理的 staging 候选精确晋级到正式路径；DB 对替换不宣称原子。

**禁止事项：**
- 不得在正式库上执行任何操作。S4 阶段正式库保持冻结。
- 不得在 staging 发布候选上执行未在 staging 演练过的迁移。
- 不得将 staging 发布候选视为已验收——验收发生在 S5/S6。

**退出证据：**
- staging 发布候选迁移报告（schema diff、行数对比、幂等性验证）。
- staging 发布候选操作治理后状态（治理后 staging SQLite）。
- 数据负责人签署 staging 发布候选迁移完成。
- 当前正式库哈希保持不变（S4 前/后确认）。

**回滚边界：** 删除 staging 发布候选目录；从备份副本重建。不影响正式库。

---

### S5：staging 发布候选上的 G01-G22 全体验收（含 G22 真实后端验证与性能/UAT）

**前置条件：** S4 完成 + 实施审核人批准
**执行约束：** 仅针对 S4 产物 staging 发布候选。正式库路径只读不写。
**状态：** `NOT_EXECUTED`

**候选冻结：** S5 启动前，对 S4 产出的 staging 发布候选执行候选清单/哈希冻结：
- 记录 DuckDB 和 SQLite 文件的 SHA-256，记入 `data/.hashes` 的 `(d) staging-candidate` 行。
- 冻结后 S5/S6 期间此候选不得修改。如检测到修改，S5/S6 必须从头重新执行。
- 所有可能写数据库的 pytest、CLI、Web/UAT 和性能流程必须使用由候选逐字节复制出的**一次性工作副本**；运行前验证副本哈希等于候选，运行后销毁副本。只读检查可以直接读取候选，但必须以只读方式打开。每组测试前后都要确认原始候选哈希未变。

**必须完成——对不可变 staging 发布候选执行 G01-G22 全体验收：**

- **G01-G04 / G10-G11 / G14-G15（代码与公式测试）：** 在隔离路径上对候选的一次性工作副本执行 pytest，确认不抛异常、公式正确、故障注入原子性、适配器行为、后备源链等。
- **G05-G06（隔离与哈希门禁）：** 确认 S1 隔离路径仍然有效，整轮 pytest 前后正式 DB 和原始 staging 候选的哈希均不变；所有写入只发生在一次性工作副本。
- **G07-G09 / G12-G13 / G16-G19（数据与 schema 验收）：**
  - G07/G08/G09：确认 staging 发布候选的三表完整度、利润表字段门限、现金流存在性。
  - G12：确认分红公告日和真实除权日可追溯（staging）。
  - G13：确认 `source_audit` 孤儿批次为零。
  - G16：确认非豁免股票 raw/QFQ 最新日期和 `(stock_code, trade_date)` 键集一致。
  - G17：确认 13 个价格孤儿代码已处理并签署。
  - G18：确认 schema 版本与代码声明一致（S4 迁移已生效）。
  - G19：确认行业字段有明确来源；未知停牌使用 NULL。
- **G20-G21（运维真实性）：** 在 staging 发布候选上确认只有 published 且未撤销的覆写参与计算；无陈旧 `running` 作业（S4 治理 + S2 heartbeat 机制验证）。
- **G22（真实后端消费者验收）：**
  - DataStatusPage 展示：记录日期、完整日期、价格日期、计算日期、数据版本、阻断警告码。
  - StockDetailPage 展示：六个 freshness 字段 + 四个分红指标"数据未验证"标签。
  - ScreeningPage 展示：质量警告横幅 + 不可信字段保存/导出门禁。
  - CLI `data status` / `data diagnose` 包含 `data_quality`。
  - CSV 导出包含质量元数据。
- **性能测试（QA-08 定量标准）：** 19 条件筛选 <5 秒（对候选的哈希一致一次性工作副本执行，记录环境和数据集元数据；原始候选前后哈希不变）。
- **浏览器 QA：** 375/768/1280 三种宽度，staging 发布候选真实后端 API。

**禁止事项：**
- 不得使用 mock API 替代真实后端进行最终前体验收。
- 不得修改 staging 发布候选。如需修改，修改后必须重新执行 S5 全部门禁。

**退出证据：**
- 候选清单/哈希冻结记录（`data/.hashes` 的 `staging-candidate` 行）。
- **G01-G22 在 staging 发布候选上的完整验收报告**（按上述分组逐一记录）。
- G22 真实后端验收报告（每个消费点的状态截图或 CLI 输出）。
- 性能测试报告（19 条件 <5 秒，记录环境和数据集元数据）。
- `npm run build` 通过。
- 浏览器 QA 报告（staging 真实后端）。

**回滚边界：** 无数据变更。前端/CLI 修复通过新前向提交回滚，保留历史。不影响正式库。

---

### S6：G23 30 股外部真值抽样与人工签署（对精确不可变 staging 发布候选）

**前置条件：** S5 完成。S5 中创建的 staging 发布候选清单/哈希冻结**必须在 S6 启动前验证未变**。如候选哈希已变，S5 和 S6 必须从头重新执行。
**执行约束：** 仅针对 S5 已冻结的 staging 发布候选。候选只能只读访问；需要工具写入时必须使用哈希一致的一次性工作副本。外部真值来源验证（交易所网站、官方公告 PDF、证监会指定披露媒体）。不得将项目内部数据作为真值。
**状态：** `NOT_EXECUTED`

**候选完整性验证：** S6 启动前必须重新验证 staging 发布候选哈希与 S5 冻结记录一致。如候选已变（DuckDB 或 SQLite 的 SHA-256 与 S5 `data/.hashes` 中 `staging-candidate` 行不匹配），S5 和 S6 均须从头重新执行。候选冻结哈希本身也记入 G23 证据包。

**必须完成：**
- 30 股样本至少覆盖：SSE、SZSE、BSE、ST/*ST、停牌/复牌、无分红、多次分红、亏损、新股、财务更正、价格异常。
- 每只股票保留：
  1. 证券代码、交易所、样本选择原因。
  2. 官方报告/公告 URL、文件标识、披露时间、SHA-256。
  3. 财务报告期、版本/更正链、审计意见。
  4. 资产、负债、权益、营收、归母净利润、经营现金流的原文定位与抽取值。
  5. 分红实施公告（含更正/取消）、记录日、除权除息日、支付日。
  6. raw/QFQ 最新日期和异常解释。
  7. 元数据来源、规则/分类版本、有效日期。
  8. 自动比对结果、人工复核人、日期、签署结论。
- 判定四种结果：`match`、`source-corrected`、`unresolved`、`not-applicable`。
- 任何核心财务字段/公司行动日期/证券状态/价格范围的 unresolved 不得用平均通过率冲抵。

**禁止事项：**
- 不得将 XBRL 派生页面作为唯一真值源。
- 不得使用跨板块通用 R+1 规则猜日期。
- 不得将"网页可访问"外推为批量再分发许可。
- 不得承认 unresolved 结果并仍标记 G23 通过。

**退出证据：**
- 30 股证据包（每个股票的完整字段）。
- 签署表（独立真值审核人 + 项目所有者）。

**回滚边界：** 证据包独立于代码和数据。直接存储。

---

### S7：正式发布（候选→正式晋级 + 发布与回滚准备）

**前置条件：** S6 完成 + 所有 G01-G23 PASS（均基于精确 staging 发布候选）+ 项目所有者 + 独立审核人签署
**执行约束：** 正式路径。所有操作按编号顺序执行，每一步确认通过后才进入下一步。**标签最后创建。**
**状态：** `NOT_EXECUTED`

**P0. 晋级前回滚演练（在 staging 副本上，第一步执行）：**
- 在独立的 staging 演练目录（非发布候选本身）中准备一对“当前状态”副本和一对“备份状态”副本，以此模拟后续 S7 预发布备份的恢复全流程；实际预发布备份尚未在此时创建。演练结束后保留命令、哈希、耗时和签署，确认回滚步骤正确。此演练不修改正式库。
- 最终回滚就绪检查：确认备份介质可读、回滚执行人有授权、回滚触发器条件已记录。

**必须完成（按严格顺序）：**

**1. 准备最终发布工件（对不可变 staging 发布候选）：**
   - **前端静态资源同步：** 先清空 `app/web/static/` 目录（如 `Remove-Item -Path "app/web/static/*" -Recurse`），再将 `frontend/dist/` 内容复制过去。为两个目录分别生成按相对路径排序的文件清单，每行包含相对路径、字节数和 SHA-256（只处理 `Get-ChildItem -Recurse -File` 的结果），并逐行比对完全一致，而不仅检查 `lang`。这是最终静态——之后不修改。
   - **exe 重建：** 对当前源码（S2-S6 修复后的版本）执行 PyInstaller。记录结果大小。确认 <500 MiB。新 exe 使用已同步的静态资源和 staging 候选后端代码，但 exe 太大无法进入 Git——其 SHA-256 记录在工件清单中。
   - **CHANGELOG.md 创建：** 反映当前版本和修复内容。
   - **LICENSE/使用声明 + 数据源权利登记册：** 复核并定稿 S2 已建立、S3 已执行的数据源权利登记册和使用范围声明。记录每个数据源（AKShare/Eastmoney、BaoStock、CNINFO、TDX、CSMAR 等）的引用/许可状态/使用范围/分发限制。若此时发现候选仍含未获权来源，立即停止 S7，回到 S3 排除并重建候选，随后重跑 S4-S6；不得在 S7 临时豁免。
   - **工件清单与哈希：** 记录所有工件（`app/web/static/` 清单/目录哈希、exe SHA-256、CHANGELOG SHA-256、LICENSE SHA-256）到 `data/.hashes` 的 `(e) release-artifacts` 行（或独立工件清单文件），从 Git 外部的 exe 路径读哈希。
   - **测试：** 使用已构建的静态和 exe 对候选的哈希一致一次性工作副本执行冒烟测试（health API、3 只股票加载、默认筛选）。记录测试结果；测试后确认原始候选哈希未变并销毁工作副本。

**2. 晋级前签署与清单/哈希验证：**
   - 项目所有者和独立审核人签署候选晋级确认书。确认：
     - `data/.hashes` 中 `(d) staging-candidate` 行的 SHA-256 与 staging 发布候选物理文件一致。
     - 工件清单中的所有哈希与物理文件一致。
     - 数据源权利登记册中无排除项存在于候选数据中。

**3. 停止服务——优雅关闭：**
   - 运行手册中记录的当前应用 PID（`$pid = Get-Content runbooks\app-pid.txt` 或通过 `Get-Process -Name python` 筛选已知工作目录/端口来定位 PID）。
   - 执行 `Stop-Process -Id $pid`（**不得使用** `-Name python, uvicorn -Force` 通配符，以免误杀无关进程）。
   - 确认目标进程已退出。**不得**执行通配符进程终止。

**4. 预发布备份——当前正式 DB 对的签名事故状态备份：**
   - 对当前正式 DB 对执行 `Get-FileHash`，将哈希记录到证据账本。
   - 记录并复制完整正式文件集：主 DuckDB/SQLite，以及实际存在的 DuckDB `.wal`、SQLite `-wal`、`-shm`；不存在须记录为 absent。服务停止后不得为事故库运行 checkpoint。
   - 验证副本与源的相对名称、字节数和逐文件 SHA-256 完全一致。
   - 此备份是首次发布发生部分替换失败时恢复文件系统到晋级前事故状态的必要来源，但它仍是已知 `DRIFTED_FROZEN` 数据，**不是可恢复正式使用的可信发布**。

**5. 候选晋级——受控 DB 对替换（单文件可使用同卷原子替换，对级替换不是原子事务）：**
   - 在**同一卷**上将 staging 发布候选 DB 复制为临时文件（如 `data/valuedashboard.duckdb.candidate` 和 `data/valuedashboard.sqlite.candidate`）。
   - 验证临时文件哈希与候选清单一致。
   - 在替换前隔离当前正式库的所有伴生 WAL/SHM 文件，避免旧事故库的 WAL 被新候选主文件误读。候选已经在 S4 收敛，晋级时不得携带未签署的 sidecar。
   - 运行手册必须采用经过演练的同卷文件替换原语（例如受控调用 `System.IO.File.Replace` 并保留单文件备份）逐个替换 DuckDB 和 SQLite。若平台/文件系统无法提供该原语，则必须明确记录单文件替换也非原子，并在服务停止状态下使用“旧文件保留 + 新文件落位 + 立即哈希验证”的降级方案。
   - **立即验证**新正式 DuckDB 的 SHA-256 与 staging 候选哈希匹配。
   - **如果 DuckDB 步骤失败**（哈希不匹配、复制错误）：立即从 `data/backup/pre-release-v1.0.0/` 恢复双 DB，停止发布过程。
   - 如果 DuckDB 步骤成功，对 SQLite 重复相同步骤。
   - **如果 SQLite 步骤失败，但 DuckDB 已替换：** 这是一个部分失败状态。必须立即从 `data/backup/pre-release-v1.0.0/` 同时恢复双 DB，不能只恢复 SQLite。恢复后验证双库哈希与预发布备份一致，并按本节回滚边界保持服务停止/BLOCK（首次发布）或切换到签名且已验收的先前发布。
   - 此过程不是真正的两阶段提交，但具有运行书记录的可验证对级回滚能力。
   - 将替换运行书记录到证据账本。
   - **候选的精确字节已保留。** 替换后正式 DB 对的哈希必须与步骤 2 签署的候选哈希完全一致。

**6. 最终冒烟测试（使用已构建的工件和已晋级 DB）：**
   - 启动前先记录晋级后的正式 DB 对哈希；二者此时必须与候选种子哈希一致，并写入 `(c) released-formal-seed`。
   - 启动服务（`python -m app.web.main` 或 start.bat）。
   - 执行 §10.1 启动健康检查清单（health API、db/status、3 只随机股票加载、默认筛选执行）。
   - 确认无新增错误日志。
   - 确认提前构建的静态和 exe 可正常消费已晋级 DB。
   - 冒烟后将主文件和实际出现的 WAL/SHM sidecar 哈希追加到 `(f) runtime-observed`，并按 §8.2 生成表级/主键级语义差异报告。若启动或只读冒烟超出 §8.2 合同，发布失败并立即触发回滚。

**7. 更新哈希 + 最终发布提交与 v1.0.0 标签（标签最后创建）：**
   - `data/.hashes` 的 `(c) released-formal-seed` 行保存首次运行前的正式 DB 对哈希（必须与 `(d) staging-candidate` 一致）；`(f) runtime-observed` 保存首次运行后的观察哈希和允许差异证据。不得用运行后哈希覆盖发布种子。
   - `(a) rebuild-source` 和 `(b) incident` 行保持不变。
   - `(d) staging-candidate` 行可保留为审计跟踪或标记为 `(promoted)`。
   - 创建最终发布提交：包含 `data/.hashes`（含 `released-formal-seed`、`runtime-observed` 和工件清单）、`app/web/static/`（已同步）、CHANGELOG.md、LICENSE、数据源权利登记册。提交信息：`"release: v1.0.0 — <short-description> | released-seed-hash-DuckDB: <hash> SQLite: <hash>"`。
   - 创建带签名的 `v1.0.0` **带注释的（annotated）** 标签（如 Git 用户已配置 GPG/Signing 则签名；否则为注释标签）：`git tag -a v1.0.0 -m "正式发布 v1.0.0"`。
   - **标签最后创建**——它代表已发布状态。远程推送（`git push --tags`）仅在项目所有者明确批准后执行。exe 不在 Git 中；其哈希记录在发布提交的工件清单/证据中。

**禁止事项：**
- 不得在 G01-G23 未全部通过时执行 S7。
- 不得在未验证候选哈希时执行晋级。
- 晋级过程中不得跳过任何验证步骤。
- 不得使用 `git reset` 作为回滚方法（见下方更新后的回滚边界）。
- 不得执行通配符进程终止（`Stop-Process -Name python -Force`）。仅终止运行手册中记录的 PID。
- 不得将 exe 放入 Git 追踪。记录其哈希。
- 不得在 DB 晋级后修改静态或重建 exe——它们必须在步骤 1 中预先构建并验证。

**退出证据：**
- 工件清单/哈希记录（`data/.hashes` 的 `(e) release-artifacts` 或独立文件），含静态目录哈希、exe SHA-256、CHANGELOG SHA-256、LICENSE SHA-256。
- 预构建工件与 staging 发布候选的冒烟测试记录。
- 候选清单/哈希签署记录（项目所有者 + 独立审核人）。
- 数据源权利登记册，确认候选数据中无排除项。
- `data/backup/pre-release-v1.0.0/` 存在且哈希已记录。
- 替换运行书（含成功/失败步骤记录、回滚触发记录）。
- `data/.hashes` 的 `(c) released-formal-seed` 与候选一致；`(f) runtime-observed` 已记录首次运行后哈希，语义差异报告确认候选事实集未变。
- 晋级后冒烟测试清单全部通过。
- 发布提交 + `v1.0.0` 带注释标签 + 远程推送确认（仅在所有者批准后）。
- 晋级前回滚演练 signed off。
- 最终回滚就绪检查 signed off。
- 发布清单所有项签署（项目所有者 + 发布批准人）。

**回滚边界：** 停止服务。若存在签名且已验收的先前发布，恢复该发布并验证其哈希后才可重新启动。首次发布不存在可信先前版本；此时从 `data/backup/pre-release-v1.0.0/` 恢复只能把文件系统恢复到晋级前的 `DRIFTED_FROZEN` 事故状态，应用必须保持停止并继续 `BLOCK`，不得恢复正式使用。**不得使用 `git reset` 或任何破坏性 Git 命令作为回滚手段。**

---

## 7. 命令政策

### 7.1 当前安全命令（冻结期内允许）

```bash
# 前端类型检查
cd frontend && npx vue-tsc --noEmit

# 前端 Node 合约测试
cd frontend && node --experimental-strip-types --test tests/data-quality.test.ts tests/stock-detail.test.ts tests/screening-quality.test.ts

# 前端生产构建
cd frontend && npm run build

# npm 审计
cd frontend && npm audit
```

```powershell
# 只读文件哈希（不打开 DB 引擎）
Get-FileHash -LiteralPath "data\valuedashboard.duckdb" -Algorithm SHA256
Get-FileHash -LiteralPath "data\valuedashboard.sqlite" -Algorithm SHA256
Get-FileHash -LiteralPath "data\backup\audit_pre_fix_20260720.duckdb" -Algorithm SHA256
Get-FileHash -LiteralPath "data\backup\audit_pre_fix_20260720.sqlite" -Algorithm SHA256
```

```bash
# Git 只读命令
git status
git log --oneline -10
git branch -a
```

### 7.2 冻结命令（禁止执行，直至 S1 完成且获得批准）

```
python                          # 所有 Python 命令
python -m pytest                # 包括 --collect-only
pytest                          # 所有 pytest 命令
python -m app.cli.main          # CLI 所有子命令
python -m app.web.main          # Web 服务器
vd                              # CLI 别名
start.bat                       # 启动脚本（会写入正式库）
duckdb                          # DuckDB CLI（任何模式）
sqlite3                         # SQLite CLI（任何模式）
pip install                     # 安装或更新依赖
pyinstaller                     # 打包
任何覆盖正式 DB 对的恢复/替换命令     # 不在冻结期文档中给出可复制执行的覆盖命令
```

### 7.3 条件命令（仅在获得明确书面批准后执行）

| 命令 | 批准条件 | 必须附带 |
|---|---|---|
| `Copy-Item data/backup/audit_pre_fix_*.duckdb data/staging/` | S0 完成 + 项目所有者批准 | 书面 runbook |
| 在隔离路径上运行 `pytest tests/regression/` | S1 完成 | 路径隔离证明 |
| 在 staging 上执行迁移 | S3 完成 + 数据负责人批准 | 迁移计划 + 回滚方案 |
| `pyinstaller value-dashboard.spec` | S7 阶段 | 构建确认 + 空间检查 |

---

## 8. 门禁表 G01-G23

门禁定义源自 `docs/11_RED_TEAM_AUDIT_V2.md` 第 9 节（第 987-1013 行）。

| Gate | 定义 | 映射阶段 | 当前状态 | 所需证据 |
|---|---|---|---|---|
| G01 | `compute_all_for_stock` 对代表样本不抛异常 | S3/S5 | `CODE_FIXED_NOT_REVERIFIED` | 隔离路径 pytest 通过（分红 SQL CTE） |
| G02 | 无分红/多次分红/跨年分红测试正确验证 `latest_dps` | S3/S5 | `CODE_FIXED_NOT_REVERIFIED` | 隔离路径 pytest 通过 |
| G03 | 快照失败注入测试中正式表逐行不变 | S3/S5 | `CODE_FIXED_NOT_REVERIFIED` | staging 故障注入测试 |
| G04 | staging 发布后双向 `EXCEPT=0` | S3/S5 | `CODE_FIXED_NOT_REVERIFIED` | staging 质量报告 |
| G05 | `pytest --collect-only` 不修改正式表或文件 | S1 | `NOT_EXECUTED` | S1 哈希门禁测试通过 |
| G06 | 全量测试使用临时库并可重复通过 | S1 | `NOT_EXECUTED` | S1 隔离测试包通过 |
| G07 | 最新资产负债表核心字段达到完整度门槛 | S3 | `DATA_STILL_BLOCKED` | staging 质量报告 |
| G08 | 最新利润表不再只有归母净利润/EPS | S3 | `DATA_STILL_BLOCKED` | staging 质量报告 |
| G09 | 同期现金流存在，或整套财报明确标记不完整 | S3 | `DATA_STILL_BLOCKED` | staging 三表交集报告 |
| G10 | 未知适配器使启动失败；合法名称真实尝试抓取 | S2 | `CODE_FIXED_NOT_REVERIFIED` | 隔离路径测试通过 |
| G11 | 主源失败后备源链执行并留下批次记录 | S2/S5 | `CODE_FIXED_NOT_REVERIFIED` | 隔离路径测试通过 |
| G12 | 分红公告日和真实除权日可追溯 | S3/S6 | `DATA_STILL_BLOCKED` | staging 重建 + G23 抽样 match |
| G13 | `source_audit` 孤儿批次为 0，哈希可重算 | S3 | `DATA_STILL_BLOCKED` | staging 溯源质量报告 |
| G14 | annual 与 TTM 在非年末样本按正确公式产生差异 | S2/S5 | `CODE_FIXED_NOT_REVERIFIED` | 隔离路径 pytest 通过 |
| G15 | YoY/QoQ/TTM 使用构造数据通过公式测试 | S2/S5 | `CODE_FIXED_NOT_REVERIFIED` | 隔离路径 pytest 通过 |
| G16 | 非豁免股票 raw/qfq 最新日期一致 | S3 | `DATA_AND_RECURRENCE_BLOCKED` | staging raw/QFQ 对齐报告 |
| G17 | 13 个价格孤儿代码已确认归属并处理 | S3 | `DATA_STILL_BLOCKED` | 数据负责人签署的隔离决定 |
| G18 | staging 发布候选的 schema 版本与代码声明一致 | S5（对 staging 发布候选验收） | `MIGRATION_BLOCKED` | 迁移报告（schema diff、行数） |
| G19 | 行业字段有明确来源；未知停牌使用 NULL | S3 | `DATA_STILL_BLOCKED` | staging 元数据质量报告 |
| G20 | 只有 published 且未撤销的唯一覆写参与计算 | S4 | `CODE_FIXED_NOT_REVERIFIED` | 隔离路径 pytest 通过 + 治理后状态 |
| G21 | 无陈旧 `running` 作业；真实失败进入 retry | S4 | `CODE_FIXED_NOT_REVERIFIED` | 隔离路径 pytest 通过 + 作业状态 |
| G22 | 状态页显示记录日期、完整日期、价格日期和计算日期 | S5（真实后端验收） | `CODE_FIXED_NOT_REVERIFIED` | 前端消费者存在且 mock QA 19/19 通过；但真实后端验收、已部署静态资产同步、CLI/后端路径和 CSV 元数据闭环均未完成 |
| G23 | 端到端样本有外部真值抽样和人工签字 | S6 | `NOT_EXECUTED` | 30 股证据包 + 签署表 |

**当前阻断计数：**
- `PASS`: 0
- `CODE_FIXED_NOT_REVERIFIED`: 11（G01, G02, G03, G04, G10, G11, G14, G15, G20, G21, G22）
- `DATA_STILL_BLOCKED`: 7（G07, G08, G09, G12, G13, G17, G19）
- `DATA_AND_RECURRENCE_BLOCKED`: 1（G16）
- `MIGRATION_BLOCKED`: 1（G18）
- `NOT_EXECUTED`: 3（G05, G06, G23）

**放行条件：全部 23 项 PASS。**

### 8.1 可量化验收标准（使完成可证伪）

以下定量条件必须全部满足，G01-G23 方可标记 PASS。任何一条不满足则 ALL_GATES_PASS 为 false。

| # | 条件 | 验证方式 | 通过门限 |
|---|---|---|---|
| QA-01 | 标记为 `complete` 的记录/包中零未记录/未经批准的壳行财务包 | staging 质量报告三表完整性检查 | 每个声明为 `complete` 的报告期的 BS 必须包含非空 `total_assets`、`total_liabilities`、`total_equity`；IS 必须包含非空 `revenue`、`parent_net_profit`；CF 必须包含非空 `operating_cash_flow`。不完整包可保留，但须**显式标记**为 `incomplete`，且不得被选为"最新完整财报"或用于衍生指标计算 |
| QA-02 | 完整金融包的 BS/IS/CF 所有必填字段与同期语义一致 | staging 质量报告字段门限检查 | 每表必填字段（BS: total_assets/total_liabilities/total_equity, IS: revenue/parent_net_profit, CF: operating_cash_flow）非空率 100%；三表的 `report_date` 必须指向相同报告期 |
| QA-03 | 零无依据的占位式分红日期 | G23 抽样验证 + STAGING 整体统计 | 无官方来源证明的占位式日期（如 `ex_date` 在 12-31/06-30 且 `announcement_date` 为 0/NULL）= 0。允许 `ex_date` 落在 12-31/06-30，但每行必须有链接的官方证据（公告 URL/PDF SHA-256）证明该日期为真实 |
| QA-04 | 100% 已验收溯源行具有可重算哈希且零孤儿 | staging 溯源质量报告 | `source_audit` 非空哈希行 = `source_audit` 总行数；每个 `source_audit` 行关联到有效 `fetch_batch` |
| QA-05 | 零非豁免 raw/QFQ 日期/范围不匹配（精确相等） | staging raw/QFQ 对齐报告 | 非豁免股票的 `(stock_code, trade_date)` 键集精确相等（raw 和 QFQ 的表级集合差为 0）。最新日期差 = 0。最小日期差 = 0。**移除**行数比宽容区间——必须精确相等。豁免仅限 BSE 等已知不支持 QFQ 的交易所，且须在报告中列出并说明 |
| QA-06 | 未知元数据保留 NULL 并附机器可读原因，而非假值 | staging 元数据质量报告 | 每个未知状态/类别必须为 NULL 并附带机器可读原因/源状态（如 `is_suspended` = NULL, `reason` = "unknown"）。无未知编码为 FALSE/空字符串。`sw_level1` 为 NULL 时必须记录从数据源获取时的实际返回值和请求参数 |
| QA-07 | G23 签署 30/30 案例，零未解决，含明确容差 | G23 证据包签署表 | 30 股全部签署为 `match`、`source-corrected` 或 `not-applicable`；`unresolved` = 0。容差：日期/状态/代码精确匹配；财务货币值在单位归一化后绝对容差 <= 1 CNY 或数据源最小发布单位（取较大者）；证券价格绝对容差不得超过官方来源的最小报价单位；比率容差 <= 1 个基点（0.01%），除非官方来源精度更低。`source-corrected` 必须在候选数据中实际修正，并在签署前重新验证——不得仅因发现差异就计为已通过 |
| QA-08 | 性能：19 条件参考查询 <5 秒（p95） | 性能测试报告 | 1 次预热 + 5 次测量运行。对 staging 发布候选，19 条件筛选在热缓存下测量。p95 计算：5 次测量按升序排序，p95 = 第 5 次（最大值）——因为 5×0.95=4.75，就近取整为第 5 位。记录：硬件（CPU/内存/存储类型）、OS、候选哈希、行数（DuckDB/SQLite）、查询/规则版本。环境/数据集变更时必须重新测量 |

**证据卫生：** 以上所有 QA 条件必须在精确的 staging 发布候选上验证，候选哈希在 S5 冻结并在 S6 重新确认。如果 QA 验证后候选发生变化，所有 QA 条件必须重新验证。

### 8.2 正式运行时写入合同

以下合同预先界定可接受变化，S7 操作员不得临场解释：

1. **启动和只读冒烟必须零逻辑变化。** `init_all_schema()` 只能幂等执行；`schema_migrations`、表/索引定义和所有表行集在启动前后必须完全一致。`run_incremental_check()` 只读检查，不得执行增量更新。允许 SQLite 因 WAL 模式产生/改变 `-wal`、`-shm` 物理文件，也允许主文件因幂等事务产生字节级重排，但 canonical schema、逐表行数、主键集合及受保护事实表的行内容哈希必须不变。
2. **S7 冒烟不调用写 API。** 不调用 `/api/screening/save`、自选股新增/编辑、人工覆写发布/回滚、计划保存、数据更新、备份恢复或任何其他写接口；若需要验证写流程，必须在晋级前对一次性工作副本完成。
3. **受保护事实集禁止由普通运行改写。** `stock_meta`、`price_daily_raw`、`price_daily_qfq`、`balance_sheet`、`income_statement`、`cash_flow`、`dividends`、`xdxr`、`indicator_snapshot`、`fetch_batch`、`source_audit` 在启动/只读冒烟中零变化。
4. **发布后的显式用户/运维写入只限对应命令。** `dsl_expressions`、`dsl_dependencies`、`screening_rules`、`screening_results`、`watchlist`、`manual_overrides`、`plans`、`job_logs`、`retry_list`、`missing_list`、`pdf_tasks`、`backup_registry`、`config` 只能由用户明确触发的对应功能写入，并保留审计字段；这类运行记录不改变发布种子，但必须进入 `runtime-observed`/操作日志。
5. **分析数据更新必须形成新数据版本。** 任何对受保护事实集的合法更新只能通过已批准的 update/backfill 流程执行，必须产生可复算 `fetch_batch`/`source_audit`、新 `data_version`、更新前备份、候选式质量报告和受影响门禁复验；不得将其伪装为普通运行时变化。任何不在本合同内的表/schema/行变化使当前运行证据失效并触发 BLOCK。

---

## 9. 正式完成/放行标准

以下条件**全部**为 true 时，BLOCK 可重新评估：

1. **G01-G23 全部 PASS** — 第 8 节门禁表每一项均有可重现的退出证据。
2. **正式候选已重建/迁移** — S3/S4 完成，staging 质量报告通过，数据负责人签署。
3. **不可变发布边界已记录** — 候选和首次运行前的正式种子哈希锁入 `data/.hashes`；运行后哈希单独追加到 runtime-observed 账本，不覆盖发布种子。
4. **核心字段/行动日期/元数据无 unresolved 差异** — G23 中任何核心财务字段、分红除权日、证券状态或价格范围的 `unresolved` 均为 0。
5. **前端真实后端 QA 通过** — S5 的 G22 验收使用真实后端 API（非 mock）在 375/768/1280 三种宽度通过。
6. **后端回归/集成测试安全通过** — S1 隔离路径上的测试包全部 PASS。整轮测试前后正式库和原始候选哈希不变，所有可能写入的验证只使用一次性工作副本。
7. **静态完整清单一致** — `app/web/static/` 与 `frontend/dist/` 均生成按相对路径排序的文件清单（相对路径 + 字节数 + SHA-256，仅文件），两份清单逐行完全一致。不仅限于 `lang` 和入口 JS。
8. **当前 exe 已重建并 smoke 测试通过** — S7 的 PyInstaller 产物 <500 MiB，smoke test 通过。exe SHA-256 记录在工件清单中（不在 Git 中）。
9. **晋级前回滚演练通过** — S7 P0 阶段在独立 staging 副本上的回滚演练完成并由实施审核人签署。
10. **数据源权利已清除** — LICENSE/使用声明存在。数据源权利登记册已签署，确认每个数据源的引用/许可状态/使用范围/分发限制，且发布候选数据中无无法证明权利的数据源。
11. **Git 基线 + 发布 tag** — 初始提交、v1.0.0 tag（最终创建）、远程备份成功（在所有者批准后）。
12. **项目所有者 + 独立审核人签署** — 双方签署正式发布清单。

**任何一项未满足则 BLOCK 继续维持。**

**额外完成约束（不可变性与证据卫生）：**

13. **晋级保留精确签名的候选字节，运行变化受约束：** S7 晋级后、首次运行前，正式 DB 对的每个字节必须与已签署候选一致，`released-formal-seed` 必须等于 `staging-candidate`。首次运行后的文件哈希可因已批准的运行状态而变化，但必须写入 `runtime-observed`，且语义差异证明候选事实集未改变。任何未经批准的事实集变化使发布失效。
14. **G23 后数据突变使发布证据无效：** 如果在 G23 完成后、S7 晋级前，staging 发布候选发生任何数据突变（DuckDB 或 SQLite 哈希改变），所有受影响的 gate（至少包括 G23 和任何引用已变数据的 gate）必须从头重新执行。候选哈希冻结在 S5 建立、S6 验证；只要哈希未变，G23 证据保持有效。

---

## 10. 发布与回滚清单

### 10.1 启动健康检查（每次启动后）

- [ ] `http://127.0.0.1:8765/api/health` 返回 200，`status: ok`。
- [ ] `http://127.0.0.1:8765/api/db/status` 返回 DuckDB + SQLite 连接正常。
- [ ] 数据状态页加载完成，无控制台错误。
- [ ] 随机抽样 3 只股票个股页可加载。
- [ ] 筛选页默认条件可执行。
- [ ] `data/logs/` 无新增错误日志。

### 10.2 关键工作流验证

- [ ] 数据状态页展示完整日期/质量警告。
- [ ] 个股页展示 freshness 字段 + 分红指标"数据未验证"标签。
- [ ] 筛选页在不可信字段门禁下禁止保存/导出。
- [ ] CLI `vd data status` 返回结构化 JSON。
- [ ] CLI `vd data diagnose` 在质量警告时返回 `healthy: false`。

### 10.3 回滚检查

**回滚恢复源：** 优先恢复签名且已验收的先前发布。首次发布前不存在可信先前版本；S7 预发布备份（`data/backup/pre-release-v1.0.0/`）只用于在部分替换失败时恢复到晋级前事故状态，恢复后必须保持服务停止和 `BLOCK`。**不得**盲目恢复 7 月 20 日重建源备份——它是 S3 重建源，不是发布后回滚目标。

| 步骤 | 时间估计 | 验证 |
|---|---|---|
| 停止 Web 服务 | <1 分钟 | 运行手册记录的应用 PID 已退出；不要求终止其他 python/uvicorn 进程 |
| 停止 start.bat（如运行中） | <1 分钟 | 任务管理器确认 |
| 选择恢复源并确认其哈希 | <2 分钟 | 优先签名且已验收的先前发布；首次发布只能选择预发布事故备份并保持停止/BLOCK |
| 恢复选定来源到正式库路径 | <5 分钟（2 GB 本地磁盘） | 有已验收先前发布则恢复它；首次发布仅可恢复事故备份并保持停止/BLOCK |
| 决定是否可重启 | <1 分钟 | 仅签名且已验收的先前发布可重启；首次发布恢复事故备份后保持停止/BLOCK |
| 如可重启则执行健康检查 | <2 分钟 | §10.1 清单 |
| 确认恢复后哈希与所选恢复源一致 | <1 分钟 | `Get-FileHash` 比对所选源的签署记录 |
| **总时间** | **<15 分钟** | 以本地磁盘和 2 GB 数据规模为目标，演练后确认 |

### 10.4 发布前检查

- [ ] S7 P0 晋级前回滚演练已完成并签署。
- [ ] S7 Step 1 工件已准备：静态已同步、exe 已重建冒烟、CHANGELOG 已编写、LICENSE/使用声明和数据源权利登记册已创建。
- [ ] S7 Step 1 工件清单/哈希已记录（`data/.hashes` (e) 或独立文件）。
- [ ] S7 Step 2 候选/工件签名验证通过。
- [ ] `data/.hashes` 存在且 `(d) staging-candidate` 哈希与候选物理文件一致。
- [ ] 数据源权利登记册确认候选数据中无排除项。
- [ ] S7 Step 3 运行手册记录了应用 PID（不得通配符停止）。
- [ ] `data/backup/pre-release-v1.0.0/` 备份空间已确认可用。
- [ ] 晋级运行书已签署（项目所有者 + 独立审核人）。
- [ ] 无 stale 的 python/uvicorn/pyinstaller 进程。

---

## 11. 证据账本模板

### 11.1 运行清单模板

```
# Run Manifest — <Phase/Step Name>
**Date:** YYYY-MM-DDTHH:MM:SS
**Operator:** <name>
**Directory:** D:\Mr.Q\掌控经济\value-dashboard

## Pre-run state
- Formal DB DuckDB SHA-256: <hash>
- Formal DB SQLite SHA-256: <hash>
- Git commit: <hash or "none">
- Running processes (relevant): <list>

## Commands executed
1. <command 1>
2. <command 2>

## Post-run state
- Formal DB DuckDB SHA-256: <hash (should equal pre-run for read-only)>
- Formal DB SQLite SHA-256: <hash>
- Exit code: <0/1/other>
- Console errors: <none/list>

## Evidence files produced
- <path>: <purpose>
```

### 11.2 哈希账本模板

```
# Hash Ledger — <Date>
| File | SHA-256 | Source |
|---|---|---|
| `data/valuedashboard.duckdb` | `<hash>` | `Get-FileHash -Algorithm SHA256` |
| `data/valuedashboard.sqlite` | `<hash>` | `Get-FileHash -Algorithm SHA256` |
| `data/backup/audit_pre_fix_20260720.duckdb` | `46EBCEB6DDBCCA15D4E82D22CFA659EC1C593033DE190C08B5C54FC7211A3C91` | Original |
| `data/backup/audit_pre_fix_20260720.sqlite` | `228E0F53A8EBD0B99DAED8FA2D683D42F46644E8805495350EC162EFEC6596D3` | Original |
| `frontend/dist/assets/index-<name>.js` | `<hash>` | `Get-FileHash -Algorithm SHA256` |
| `dist/value-dashboard/value-dashboard.exe` | `<hash>` | `Get-FileHash -Algorithm SHA256` |
```

### 11.3 迁移报告模板

```
# Migration Report — <YYYY-MM-DD>
**Staging copy source:** `data/backup/audit_pre_fix_20260720.duckdb`
**Migration list:**
1. `price_daily_qfq` ADD COLUMN `turnover_rate` DOUBLE
2. `indicator_snapshot` ADD COLUMN `calculated_at` TIMESTAMP
3. `stock_meta` ALTER `is_suspended` SET DEFAULT NULL

## Pre-migration schema diff (code schema vs staging schema)
```
<DESCRIBE output>
```

## Post-migration schema diff (code schema vs staging schema)
```
<DESCRIBE output — should be identical>
```

## Row count before/after
| Table | Before | After | Delta |
|---|---|---|---|
| price_daily_qfq | <N> | <N> | 0 |
| indicator_snapshot | <N> | <N> | 0 |

## Idempotency test
- Second run: schema unchanged (yes/no)
- Second run: row counts unchanged (yes/no)

## API behavior test
- `GET /api/stock/600519/kline` returns `turnover_rate` (yes/no)
- `GET /api/data-status/summary` returns normally (yes/no)
```

### 11.4 Staging 质量报告模板

```
# Staging Quality Report — <YYYY-MM-DD>
**Source backup hash:** DuckDB 46EB... SQLite 228E...

## Financial statement completeness
| Period | BS rows | BS complete | IS rows | IS complete | CF rows | CF complete | Full set |
|---|---|---|---|---|---|---|---|
| <date> | <N> | yes/no | <N> | yes/no | <N> | yes/no | yes/no |

## Dividend quality
- Total rows: <N>
- Announcement date present: <N>
- ex_date on 12-31/06-30 (placeholder): <N>
- ex_date other dates: <N>

## Lineage quality
- source_audit rows with non-empty hash: <N>
- source_audit rows linked to fetch_batch: <N>
- fetch_batch rows: <N>

## Raw/QFQ alignment
- raw codes: <N>
- qfq codes: <N>
- raw-only non-exempt: <N>
- codes with range/row split: <N>

## Metadata quality
- sw_level1 non-null: <N>
- is_suspended IS NULL: <N>
- is_suspended FALSE: <N>
- listing_date non-null: <N>
```

### 11.5 故障注入报告模板

```
# Failure Injection Report — <YYYY-MM-DD>
## Snapshot publication failure test
- Injection point: row 100 of 5129
- Before snapshot rows: <N>
- After failure: <N> (must equal before)
- Before snapshot SHA-256 root: <hash>
- After failure SHA-256 root: <hash> (must match)

## Dividend atomicity test
- Stock code: <code>
- Rows in dividend batch: <N>
- Injection point: row 2 of N
- Pre-failure rows for stock: <old_N>
- Post-failure rows for stock: <old_N> (must equal, no half-batch)

## Retry recording test
- Induced failure type: network/timeout/data
- Retry entry created: yes/no
- extra_json present: yes/no
```

### 11.6 G23 逐股表模板

```
## G23 Sample Stock: <code>
**Exchange:** SSE/SZSE/BSE
**Sample reason:** <ST/无分红/多次分红/亏损/新股/财务更正/价格异常>

### Official document
- Source URL: <url>
- File identifier: <name>
- Disclosure time: <timestamp>
- SHA-256: <hash>

### Financial fields
| Field | System value | Source value | Match/Source-corrected/Unresolved/N/A |
|---|---|---|---|
| total_assets | <value> | <value> | <verdict> |
| total_liabilities | <value> | <value> | <verdict> |
| total_equity | <value> | <value> | <verdict> |
| revenue | <value> | <value> | <verdict> |
| parent_net_profit | <value> | <value> | <verdict> |
| operating_cash_flow | <value> | <value> | <verdict> |

### Dividend events
| Event date | Type (record/ex-date/payment) | System value | Source value | Verdict |
|---|---|---|---|---|

### Price data
| Latest raw date | Latest qfq date | Price match (latest close) |
|---|---|---|

### Metadata
| Field | System value | Source value | Verdict |
|---|---|---|---|

**Reviewed by:** <name>
**Review date:** <YYYY-MM-DD>
**Conclusion:** match / source-corrected / unresolved / not-applicable
**Signature:**
```

### 11.7 UI/UAT 报告模板

```
# UI/UAT Report — <YYYY-MM-DD>
**Backend:** staging / formal (circle one)
**Frontend:** local dev server / built static (circle one)
**Browser:** Chromium / Firefox (circle one)
**Widths tested:** 375 / 768 / 1280 (circle)

## DataStatusPage
- [ ] Record date displayed
- [ ] Complete date displayed
- [ ] Price date displayed
- [ ] Calculated date displayed
- [ ] Data version displayed
- [ ] Warning codes displayed
- [ ] No console errors

## StockDetailPage
- [ ] Freshness card shows financial_effective_date
- [ ] Freshness card shows price_date
- [ ] Freshness card shows calculated_at
- [ ] Stale warning tag present when applicable
- [ ] Dividend_yield shows "数据未验证" when stale
- [ ] Payout_ratio shows "数据未验证" when stale
- [ ] DPS shows "数据未验证" when stale
- [ ] Consecutive_div_years shows "数据未验证" when stale

## ScreeningPage
- [ ] Quality warning banner present when warnings exist
- [ ] Save button disabled when untrusted fields used
- [ ] Export button disabled when untrusted fields used
- [ ] No POST to real backend from disabled buttons

## CLI
- [ ] `vd data status` includes data_quality
- [ ] `vd data diagnose` returns healthy=false when warnings exist

**Signed:** <name>
```

### 11.8 发布签署模板

```
# Release Sign-off — v1.0.0
**Date:** <YYYY-MM-DD>

## S7 promotion runbook (reordered — artifacts first, tag last)
- [ ] Pre-promotion rollback drill completed and signed (staging copy)
- [ ] Final rollback readiness check signed
- [ ] Step 1 — Release artifacts prepared against immutable candidate:
  - [ ] `app/web/static/` clean-replaced; manifest/dir hash matches `frontend/dist/`
  - [ ] exe rebuilt, <500 MiB, smoke tested against candidate; SHA-256 recorded in artifact manifest
  - [ ] CHANGELOG.md written
  - [ ] LICENSE/usage notice and data-source rights register created and signed
  - [ ] Artifact manifest/hashes recorded (`data/.hashes` (e) or independent file)
  - [ ] Pre-promotion smoke test passed against staging candidate using built artifacts
- [ ] Step 2 — Candidate/artifact/signature verification:
  - [ ] `data/.hashes` (d) staging-candidate hash matches physical file
  - [ ] Artifact manifest hashes match physical files
  - [ ] Data rights register confirms no excluded source in candidate data
  - [ ] Candidate promotion runbook signed (Project Owner + Independent Reviewer)
- [ ] Step 3 — Graceful service stop: only documented PID terminated (no wildcard)
- [ ] Step 4 — Pre-promotion backup saved to `data/backup/pre-release-v1.0.0/` and verified
- [ ] Step 5 — DB promotion:
  - [ ] Candidate temp files on same volume verified
  - [ ] DuckDB replaced and hash verified
  - [ ] SQLite replaced and hash verified
  - [ ] Partial-failure rollback not needed (verified)
- [ ] Step 6 — Final smoke test passed using built artifacts and promoted DBs
- [ ] Step 7 — Hashes updated + final release commit + v1.0.0 annotated tag (TAG LAST):
  - [ ] `data/.hashes` (c) released-formal-seed matches the pre-start promoted pair; (f) runtime-observed records post-smoke hashes and approved semantic differences
  - [ ] Final commit created (includes `.hashes`, `app/web/static/`, CHANGELOG, LICENSE, rights register)
  - [ ] `v1.0.0` annotated tag created (`git tag -a v1.0.0 -m "正式发布 v1.0.0"`)
  - [ ] Remote push only with explicit owner approval

## Signatures
**Project Owner:** ___________________ **Date:** ________
**Implementation Reviewer:** ___________ **Date:** ________
**Release Approver:** __________________ **Date:** ________

## Data rights register (appended to this sign-off)
| Source | Citation | License/terms | Scope | Distribution | Proven |
|---|---|---|---|---|---|
| <name> | <url> | <MIT/CC/restricted/unknown> | <local/API/redist> | <allowed/forbidden> | <by/date> |

## Rollback authority
**Authorized to roll back without further approval:** <name>
**Rollback trigger conditions:** Error rate > 2x baseline; P95 latency > <N>ms;
  Core financial field discrepancy detected in G23 spot check.
**Rollback source:** Signed and accepted prior release when available. On first release, `data/backup/pre-release-v1.0.0/` restores only the frozen incident state and must remain BLOCK/stopped.
**Rollback method:** Stop service; prefer a signed prior release. On the first release, restoring pre-release-v1.0.0 only restores the frozen incident state; verify hashes and keep the service stopped/BLOCK.
  **Not:** `git reset` or any destructive Git command.
```

---

## 12. 角色与批准矩阵

| 角色 | 职责 | S0 | S1 | S2 | S3 | S4 | S5 | S6 | S7 |
|---|---|---|---|---|---|---|---|---|---|
| **项目所有者** | 最终批准权、Git 基线、许可证、G23 与发布签署 | A* | A | I | I | I | I | A | A |
| **数据负责人** | 数据重建决策、操作记录清理审批、staging 签署 | C | C | C | A | A | C | C | C |
| **实施审核人** | 代码修复审查、测试隔离验证、路径隔离合约 | C | A | A | C | C | A | C | A |
| **独立真值审核人** | G23 外部真值比对和人工签署 | - | - | - | - | - | - | A | - |
| **发布批准人** | 正式发布签署、回滚授权 | - | - | - | - | - | - | - | A |

**A = 批准/签署（必须）; C = 咨询/审查（应参与）; I = 通知（应知晓）**

*S0 项目所有者批准是指：确认 S0 报告、批准进入 S1。

---

## 13. 立即可执行任务队列（冻结安全）

以下任务可以安全执行，因为不涉及后端/数据库操作。

### 13.1 文档任务

- [ ] 起草 S0 证据保存运行手册（`docs/runbooks/s0-evidence-preservation.md`）：
  - 包含 `Get-FileHash` 命令和预期输出模板。
  - 包含进程枚举命令和端口检查。
  - 包含目录树导出命令。
  - 包含证据包组织结构。
- [ ] 设计 S1 路径隔离合约文档（`docs/contracts/path-isolation-contract.md`）：
  - 定义正式库路径与隔离路径的映射。
  - 定义环境变量或配置文件开关。
  - 定义 pytest 默认行为（只能操作隔离路径）。
  - 定义 CI/自动化的预期行为。
- [ ] 准备 G23 抽样名册和 schema：
  - 列出 30 个候选股票的代码和选择原因。
  - 设计每只股票的证据收集表（§11.6 模板）。
  - 设计数据源查询计划（交易所网站、官方披露平台）。
- [ ] 更新 `docs/14_PRODUCTION_OPS_AUDIT.md` 中的交叉引用，指向本文件的新路径 `docs/15_CURRENT_REVERIFICATION_AND_REMEDIATION_GUIDE.md`。

### 13.2 前端任务（冻结安全——仅限准备/计划，不得执行依赖安装或静态同步）

- [ ] 准备 eslint 修复的运行手册/脚本（`frontend/package.json` 中的 `"lint": "eslint ..."`）：
  - 编写运行手册：安装 eslint 作为 devDependency 的步骤，或移除无法执行的 lint 脚本改为指向 `npx vue-tsc --noEmit`。
  - **不得在批准前执行 `npm install`。** 依赖/配置变更属于 S2 工作，须在授权后执行。
- [ ] 准备静态同步运行手册（`app/web/static/` 与 `frontend/dist/` 同步脚本）：
  - 编写脚本/运行手册：清空 `app/web/static/`，复制 `frontend/dist/*`，验证替换后清单/目录哈希。
  - **不得在 S7 之前执行同步。** 此运行手册仅在 S7 晋级时使用。
- [ ] 运行前端 Node 合约测试（现有前端测试可在冻结期内安全执行，因为它们不涉及后端/数据库）。

### 13.3 审查和交叉检查任务

- [ ] 交叉检查 `data/logs/` 中的日志文件：
  - 确认最大的日志文件和总大小。
  - 记录日志文件的日期范围。
  - 记录任何重复/明显的错误模式（仅读取文本，不交互）。
- [ ] 交叉检查 `data/backup/` 目录内容（仅 `Get-ChildItem` 和 `Get-FileHash`，不打开 DB）。

---

## 14. 前人工作评估

前人的审计、修复和QA工作为当前状态提供了基础。以下是对这些工作的评估，说明哪些是有用的，为什么。

### 14.1 红队审计（docs/10, docs/11）

**有用性：极高。** 红队审计 V1（docs/10）和 V2（docs/11）发现了项目中最关键的数据和代码问题。它们提供了：
- 结构化的 DQ-01 到 DQ-14 问题分类，使修复可以被追踪。
- 具体的 SQL PoC、配置证据和数据取证，使问题可重现。
- G01-G23 门禁定义，成为放行标准的骨架。
- 以"代码修复不等于数据修复"为核心的验收哲学。

**注意事项：** V1 的"listing_date 全部是假"的主张在 V2 中已撤销（改为 UNVERIFIED）。本报告采用 V2 的分类标准。

### 14.2 审计整改报告（docs/12）

**有用性：高。** 整改报告记录了 Phase 0 的代码修复、新增控制（staging 发布、适配器名归一化、测试隔离）和验证证据。它提供了：
- "代码整改通过；数据整改未完成"的精确边界，当前仍然正确。
- 整改前备份的基线哈希。
- 只读质量信号的设计（`data_quality` API）。

**注意事项：** 它声明了"前端质量警告展示"未完成（§5.2），这在 Cycle 2 的 T10 中已修复。但 T10 的前端实现处于 `FRONTEND_QA_PASS_SAFE` 状态。

### 14.3 阻塞项深度调查（docs/13）

**有用性：极高。** 这篇调查是当前最详细的数据状态报告。它通过只读查询和代码追踪，确认了每个 DQ 问题的当前正式数据事实。它提供了：
- 财务壳行、占位分红、溯源、raw/QFQ 分裂的具体数字。
- G22 的消费面反证（状态页、个股页、筛选、CLI 都不消费 quality）。
- G23 的采样原则和来源指引（交易所官网、证监会规则）。
- "持续 BLOCK"的严格区分（测试通过 != 数据修复，返回成功 != 运维真实）。

**注意事项：** 它的正式库数字采集于 2026-07-20，当时哈希是基线值。2026-07-22 的事故后，当前正式库已漂移，因此 docs/13 的行数证据不能被假设为与当前文件一致。

### 14.4 Cycle 2 修复（.opencode/plans/2026-07-21-remediation-cycle-2.md）

**有用性：中高。** Cycle 2 计划及执行（T1-T12）提供了重要的代码修复和目录清理：
- T1 目录治理：71 个文件移入 `_legacy/`，零删除，保持回归基线不变。
- T4/T5/T6/T7/T8/T11 后端修复：这些修复的源码存在于当前仓库中，但后端独立验证被阻断。
- T9/T10 前端修复：TypeScript 质量类型、DataStatus/StockDetail/Screening 消费端。这些通过了 mock 浏览器 QA 19/19。
- T3 未完成：`data/.hashes` 和 `test_hash_preservation.py` 不存在。

**注意事项：** 执行该计划的验证步骤（T12）导致了正式库的变化。因此该计划本身是完整的，但验证环节有意外副作用。

### 14.5 前端 QA（`frontend/test-results/final-frontend-qa/`）

**有用性：高。** 前端功能 QA 是当前最完整的消费端验证。19/19 mock 场景通过，覆盖了正常状态、警告状态、错误状态、嵌套规则、保存/导出门禁等。它还验证了 `index.html` 的 `lang="zh-CN"`、`NAlert type="error"` 等可访问性修复。

**注意事项：** 这些是 mock API 验证，不是真实后端验证。视觉交付物（57 张截图）未经 vision-capable reviewer 签署。

### 14.6 生产运维审计（docs/14_PRODUCTION_OPS_AUDIT.md）

**有用性：高。** 这篇审计发现了运维层面的 P0 问题（零 Git 提交、备份未验证、`pyproject.toml` 构建后端弃用、无日志轮转、无许可证、CI 完全不存在）。这些发现被本报告吸收为 P0-P2 问题。

### 14.7 遗留归档治理（`_legacy/README.md`）

**有用性：中。** 71 个文件从活动目录移入 `_legacy/`，清理前后的回归基线保持 48 项不变。这提供了更干净的目录结构，但不影响数据或代码门禁。

### 14.8 已超驰的早期审查（docs/07, docs/08, docs/09）

**有用性：低（就当前状态评估而言）。** 这些进度审查声称数据完整性 92%、遗留问题 96% 修复，但这些定量结论的评估标准与红队审计 V2 的标准不同。它们作为项目历史记录保留，但不构成当前验收证据。

---

## 15. 假设与局限

### 15.1 假设

1. **批准基线哈希是可信的：** 备份文件（`data/backup/audit_pre_fix_20260720.duckdb`/`.sqlite`）的 SHA-256 与 docs/12 和 README 中记录的值一致。本报告未重新计算备份文件哈希来验证此假设（因为 `Get-FileHash` 是安全的，但实际上自 docs/13 的证据以来未重新执行），**但 S0 阶段应验证此假设**。
2. **docs/13 的行数证据近似正确：** 尽管当前正式库已漂移，但 docs/13 的取证行数（2026-07-20）在数量级上应仍然有效。**S0 阶段不得查询正式库或备份来重新验证行数。重新验证必须在 S1 完成后，对外部不可变法证副本再制作分析工作副本，或直接在 staging 路径上以只读方式执行；不得直接打开和修改法证存证文件。**
3. **源码存在即已实现，除非代码审查发现不同：** 对于 `CODE_FIXED_NOT_REVERIFIED` 的项，假设源码中存在的修复在隔离路径上重新验证时会通过。但这不是放行理由。
4. **项目所有者将在 S0 后批准进入 S1：** S0 证据保存和 S1 隔离设计需要在项目所有者批准后才能执行。本报告不假设此批准已获得。

### 15.2 限制

1. **当前正式数据库的行数未经本报告重新查询。** 本报告第 2.7 节的行数源自 docs/13 的证据。在测试路径与正式库强隔离之前，不能执行安全的新查询。
2. **未获取外部真值。** G23 的 30 股证据包尚未创建。
3. **未评估数据源再分发许可。** 申万行业分类的许可状态未确定。CSMAR 商业数据的使用条款未在本报告中评估。
4. **前端视觉 QA 未经人工签署。** 57 张截图存在，但像素级 CJK 换行/裁剪/视觉层级未经 vision-capable reviewer 或人工评估。
5. **本报告中的哈希值是 2026-07-22 的证据。** 在 S0 完成前，`Get-FileHash` 可在不打开 DB 引擎的情况下安全执行以确认。

---

## 16. 变更控制规则

1. **每次代码/配置/数据变更必须在一个计划阶段中执行。** 不允许在阶段外修改正式库路径或源码（紧急安全修复除外，须立即通知项目所有者）。
2. **每次后端任务完成后必须更新本报告的以下部分：**
   - 第 5 节问题登记册（受影响问题的状态）。
   - 第 8 节门禁表（受影响门禁的状态）。
   - 第 2 节证据快照（如果正式库哈希改变）。
3. **每次前端任务完成后必须更新本报告的第 2.3 节（前端验证状态）。**
4. **任务完成不能仅从代码存在证明。** 每个任务必须提供第 11 节中指定的退出证据。
5. **本报告本身可以由项目所有者批准后通过追加修改。** 修改应保留历史记录（不要删除旧内容，添加"更新于 YYYY-MM-DD"的说明）。
6. **S7 发布前必须更新本报告的第 2 节证据快照并从第 9 节核对放行标准。**
7. **任何与本报告冲突的新发现应立即添加到第 3 节（当前问题与根因）或第 5 节（问题登记册），并标注发现日期和来源。**

---

*本报告评审日期：2026-07-22*
*评审角色：当前状态复核与修复指引编制*
*状态：BLOCK / NO-GO*
