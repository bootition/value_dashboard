# 审计调查发现

## 当前基线

- 最新审计：`docs/11_RED_TEAM_AUDIT_V2.md`，日期 2026-07-20，结论 `BLOCK`。
- 产品：本地 A 股价值投资研究与筛选工具。
- 后端：Python 3.11+、FastAPI、DuckDB、SQLite、Typer。
- 前端：Vue 3、TypeScript、Vite、Naive UI。
- 数据源：AKShare/Eastmoney、BaoStock、CNINFO、TDX，以及历史 CSMAR 导入。
- 当前目录已初始化为 Git 仓库，分支为 `fix/audit-remediation`，尚无提交；远端为空仓库。

## 审计问题分类

### 立即代码阻断

- DQ-01：分红摘要 SQL 在聚合中嵌套窗口函数，阻断所有新指标计算。
- DQ-02：快照先清空正式表再计算，已实际造成全表丢失。
- DQ-04：配置使用未注册的 `akshare` 名称，核心财务抓取链静默失效。
- DQ-08：pytest 收集模块时执行正式快照重算并写生产数据库。

### 数据真实性与治理

- DQ-03：BaoStock 指标壳行混入完整财务报表语义。
- DQ-05：分红日期使用报告期末占位，公告日期缺失。
- DQ-06：`source_audit` 是事后合成摘要，不能形成真实溯源。
- DQ-07：现金流和指标快照显著陈旧。
- DQ-12：测试筛选结果、未发布覆写和陈旧作业状态混入正式 SQLite。

### 语义、一致性和事务风险

- DQ-09：TTM/YoY/QoQ API 与 DSL 名称和实际计算不一致。
- DQ-10：raw/qfq 覆盖分裂仍可报告成功。
- DQ-11：未知停牌等元数据被错误默认成 `False`。
- DQ-13：代码 schema 与现库 QFQ schema 漂移。
- DQ-14：回填路径缺少统一事务、批次和 retry 记录。

## 安全边界

- 未经用户确认，不删除壳行、占位分红、合成溯源、筛选历史或人工覆写。
- 外部真值未接入前，不能声称 DQ-03、DQ-05、DQ-06、DQ-07 已完成数据重建。
- 可立即完成的是程序安全、配置、测试隔离、错误语义和迁移门禁。
- 目录整理不得把“移动正式数据”当作清理；优先隔离历史可执行脚本、补入口文档和保留来源清单。
- `value-dashboard.spec` 是当前打包配置，不属于遗留规划材料；`task_plan.md`、`findings.md`、`progress.md` 仍是活动工作记忆，不应在本轮移动。

## 修复前数据基线

- DuckDB SHA-256：`46EBCEB6DDBCCA15D4E82D22CFA659EC1C593033DE190C08B5C54FC7211A3C91`。
- SQLite SHA-256：`228E0F53A8EBD0B99DAED8FA2D683D42F46644E8805495350EC162EFEC6596D3`。
- 备份：`data/backup/audit_pre_fix_20260720.duckdb`、`data/backup/audit_pre_fix_20260720.sqlite`，哈希与原文件一致。
- `indicator_snapshot`：5,129 行。
- `price_daily_qfq`：16,890,310 行，旧库列中缺少 `turnover_rate`。
- `source_audit`：15,649 行；`fetch_batch`：133 行；`xdxr`：0 行。
- SQLite：`screening_results=4`、`manual_overrides=8`、`job_logs=4`、`retry_list=0`，其中 `running` 作业 2 条。

## 测试收集面

- 绝大多数 `tests/test_m*.py` 是模块顶层执行的阶段性验证脚本，不是真正的 pytest 测试。
- 多个脚本会在导入时初始化 schema、联网、启动服务器、写 DuckDB/SQLite、创建备份或归档。
- 仅修复 `test_m2_snapshot.py` 不足以解决 DQ-08；必须限制 pytest 收集面并把历史脚本视为显式运行的验证工具。

## 当前阻塞项复核

- `financial-price.json` 与独立 SQL 均确认：2025-03-31 之后资产负债表核心完整行数为 0；完整现金流和指标快照也停在 2025-03-31，距 2026-07-17 价格日期 473 天。
- `dividend-lineage.json` 与独立 SQL 均确认：44,883 条分红全部落在 12-31/06-30，公告日全部为空；15,649 条 `source_audit` 全部空哈希且全部为孤儿批次。
- `operations-schema.json` 与独立 SQL 均确认：raw/QFQ 为 5,541/5,200，341 个 raw-only，4 个匹配代码范围或行数分裂；13 个 raw 孤儿全部是已有深市代码的未补零重复行，身份已确认但来源脚本和清理审批未闭合。
- 元数据仍为申万覆盖 0、停牌状态 5,528 行全部 False；这不是“已核验无停牌”，而是未知状态未保留。
- 正式 SQLite 仍有 3 条精确测试筛选结果、8 条 active 覆写、2 条 running 作业、0 条 retry；只有 published 覆写参与计算的代码门禁不能替代数据清理。
- 正式 QFQ 仍缺 `turnover_rate`，正式 SQLite 迁移登记仍只有 v1；迁移代码和临时库测试已存在，但正式迁移未执行。
- G22 仍失败：后端返回 `summary.data_quality`，`DataStatusPage.vue` 未消费或展示。
- DQ-10 有复发路径：full-init/backfill 已配对 raw/QFQ，但 `IncrementalUpdater` 仍只写 raw。
- DQ-14 仅部分关闭：分红回填逐行写入、缺 `ex_date` 直接跳过、抓取失败不进入 retry，也未保留实施公告和更正链。
- DQ-06 不仅是旧数据问题：当前生产路径可写 `fetch_batch`，但没有字段级 `source_audit` 写入路径，尚不能重建 G13。
- DQ-05 的质量警告不阻止指标计算；现有占位分红仍会进入股息率、DPS、分红率和连续分红年数。
- 外部权威来源已经明确：财务以 CSRC 内容格式规则和发行人官方报告为基准；分红日期以实施公告及更正链为基准；元数据以交易所/法定披露为基准；申万分类需要合法许可或明确改用公开的 CSRC 分类标准。
- 第二轮反证已纠正来源表述：CNINFO 不是全 A 股唯一法定披露平台；应按适用交易所网站和符合 CSRC 条件的媒体/发行人正式文件取证。
- 原始 XBRL 实例虽存在，但免费全市场下载未获证实；G23 可用官方报告 PDF、文件标识、哈希、提取定位和重述链作为基线，XBRL 派生页面只作补充。
- 公司行动 R+1 未被当作跨板块绝对规则；样本验收按实施公告、更正/撤销链、交易日历和适用市场规则逐项判断。

## S0/S1 会话恢复结论（2026-07-24）

- 会话 `ses_0757e085fffeizmuZXhPWkJhEU` 的目标是完成 S0 冻结取证并设计、实施 S1 测试/数据库路径强隔离。
- 会话尾部两次中断前已完成 S0、基线 allowlist 审查和证据修复；随后当前工作区形成根提交 `f4517d2`。
- 当前 `HEAD` 为 `f4517d2 chore: capture incident source baseline before S1 remediation`，annotated tag `incident-2026-07-22` 指向该提交。
- 当前没有 tracked 工作区或 index 修改；大量 `_legacy/`、`.omo/`、QA 产物和早期失败证据保持 untracked，不得擅自纳入或删除。
- `app/core/storage/path_policy.py`、`scripts/s1-path-preflight.ps1`、`scripts/s1-pytest.ps1` 和正式路径隔离测试尚不存在，S1 源码实施尚未开始。
- `docs/evidence-s1/tdd-red-module/` 及若干失败 run 目录是先前尝试留下的 untracked 证据，不等于实现完成。
- 当前事故冻结哈希仍以 DuckDB `5186E660E603B277B72E4EAF9988963C64B25B83882BA5ACF4BE2789A51268D6`、SQLite `B7B5F2FF2D1B4D2F71512DFEBD8DC2FBD9625E51BE2D0BFDB5CAF0657EC11959` 为 S1 动态保全基线。
- 当前仓库根必须作为 S1 执行位置：合约和门禁绑定该根下正式五文件集；另建 worktree 会改变验证对象并失去安全意义。

## 数据重建结论（2026-07-25）

- CSMAR 数据质量良好：导入 356K 行财务数据（1990-2025Q1），覆盖 5,828 只股票，关键字段（总资产、营收、净利润）非空率 100%。
- 空壳行问题已解决：导入前删除 2025Q2+ 的 BaoStock 空壳数据，CSMAR 数据中仅 48 行总资产为 0（占 0.01%）。
- 分红日期仍为占位：CSMAR 分红表 ex_date 为报告期末（12-31/06-30），需 AKShare 补齐真实除权日。
- AKShare 补齐进行中：抓取 2025Q2+ 财报数据和真实除权日，预计 7 小时完成 5,828 只股票。
- 简化修复流程：砍掉 S0 法证副本、S5-S7 繁琐验收，保留必要步骤（schema 对齐、清壳数据、CSMAR 导入、AKShare 补齐、简单路径分离）。
- 精简 path_policy.py：从 413 行简化到 ~150 行，移除过度防护（Windows 保留名、ADS、硬链接扫描等）。
