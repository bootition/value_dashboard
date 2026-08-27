---
title: 技术规划审查报告 V1
status: historical
category: decisions
last-reviewed: 2026-07-26
---

# 技术规划审查报告 V1

> 审查对象: TECH_PLAN_V1.md (对照 PRODUCT_REQUIREMENTS_V1.md、tech_constraints.md、findings.md、phase4_feasibility.md、phase5_coverage_matrix.md)
> 审查日期: 2026-07-17
> 审查结论: **90 / 100，暂不过关**。修订本文档列出的 8 条问题 (重点是前 3 条) 后可达 95+，即可开始实施。
> 使用方式: 本文档自包含，供独立会话按"修订清单"逐条修改 TECH_PLAN_V1.md。架构主体方向正确，**不需要推翻选型**，修订工作量约半天。

---

## 审查发现的问题

### 问题 1【高】DuckDB 多进程写冲突没有设计协调机制

- **位置**: TECH_PLAN_V1.md §2.2「三入口共享核心库」中 "CLI和Web共享同一个核心库和同一个数据库文件。CLI是独立进程"
- **问题**: DuckDB 的进程模型是——要么单进程读写，要么多进程只读，二者互斥。Web 服务常驻进程持有 DuckDB 句柄期间，CLI 的 `vd data update` / `vd data retry` / `vd data refetch` / `vd backup restore` / 指标快照重算全部无法获得写权限。Windows 文件锁下这会直接报错而不是排队等待。M1 或 M7 一联调就会撞上。
- **影响范围**: 所有向 DuckDB 写入的 CLI 命令 (data init/update/retry/refetch、快照重算、backup restore) 与 Web 常驻读 (K线、个股详情、筛选) 之间的并发。
- **注意**: SQLite 侧无此问题 (WAL 模式天然支持跨进程多读单写)，问题仅限 DuckDB 分析库。
- **修法** (任选其一或组合，写进 §2.2 与 §2.3.4，不改选型):
  1. Web 进程对 DuckDB 采用 open-per-query (嵌入式打开开销极小) 或只读模式 + 监听数据变更信号后重开句柄；
  2. 应用级锁文件 (如 `data/.write.lock`): CLI 写操作先抢锁，抢不到则明确提示"请先关闭 Web 服务或等待当前写操作完成"；
  3. `backup restore` 明确为独占操作，前置条件是 Web 进程退出 (PRD AR5 规定恢复只能走 CLI，正好吻合)。

### 问题 2【中】申万行业备用源写成 CSRC，属于分类法污染

- **位置**: TECH_PLAN_V1.md §2.3.1 适配器优先级矩阵，行 `sw_industry | 本地缓存 (SWS手动下载) | CNINFO (CSRC)`
- **问题**: 证监会行业分类 (CSRC) ≠ 申万行业分类，是两套不同 taxonomy。把 CSRC 值写入 `sw_level1/sw_level2` 列，行业排名会在错误的分类法上**静默**计算。这违反:
  - PRD §6.3: 行业排名明确是**申万**一级与二级；
  - PRD §9.2: 不允许把没有业务意义的值伪装成有效值；
  - PRD §12.4: 缺申万归属时行业排名返回 `null`，全市场排名仍可用——这才是规定的正确行为。
- **附带矛盾**: findings.md §5.1.5 将 `stock_industry_category_cninfo` 对申万 L1 标 ✅，而 TECH_PLAN 标注该源为 CSRC。两处结论不一致，需实证确认该 API 的分类标准选项 (是否支持申万标准)。
- **修法**:
  1. 优先级矩阵中 `sw_industry` 的"备用适配器"列改为「无 (缺失按 PRD §12.4 置 null + 原因码)」；
  2. 在 M1 任务清单中增加一条验证任务: 实证 `stock_industry_category_cninfo` 的分类标准选项。若其真支持申万标准，可恢复为合法 fallback；若只是 CSRC，维持"无备用 + missing 处理"。

### 问题 3【中】筛选示例 SQL 用全局 `MAX(report_date)`，会静默剔除未刷新股票

- **位置**: TECH_PLAN_V1.md §2.3.3 阶段 1 SQL: `WHERE s.report_date = (SELECT MAX(report_date) FROM indicator_snapshot)`
- **问题**: 增量更新部分失败时 (PRD L4 场景，必然发生)，重试列表中股票的快照日期落后于全局最大值，该 WHERE 条件会把这些股票**从基础股票池中静默删除**。直接违反:
  - PRD §7.4 (L4): 更新失败时保留旧值；
  - PRD §12.3 (SC9): 股票池成员资格只由 ST/停牌/上市年限预设决定。
- **正确语义**: 股票池成员资格由 `stock_meta` 决定；指标值取**每只股票各自**的最新快照，并携带各自的 `data_date` 供结果页与 CSV 溯源展示 (PRD §12.5)。
- **修法** (改 §2.3.3 示例 SQL 与文字说明):
  - 基础池改为从 `stock_meta` 出发，按 ST/停牌/上市年限开关过滤；
  - 指标快照按 per-stock 最新取数，例如 `QUALIFY row_number() OVER (PARTITION BY stock_code ORDER BY report_date DESC) = 1`，或 `stock_meta LEFT JOIN` per-stock 最新快照；
  - 排名窗口函数仍作用于过滤后的基础池 (该语义方案已正确，保持不变)。

### 问题 4【低】依赖清单三处遗漏

- **位置**: TECH_PLAN_V1.md §1.5 完整依赖清单
- **遗漏项**:
  1. **`pywin32` (或 `keyring`)**: PRD AR12 要求 Windows 凭据保护机制，§2.3.5 也承诺 DPAPI，但 Python 依赖清单没有对应库；
  2. **`pypinyin`**: PRD SD1 要求显示股票名称拼音，数据模型有 `pinyin` 列，但无免费源提供拼音，需本地生成；
  3. **前端通用图表库 (建议 ECharts)**: KLineCharts 只画 K 线，`FinancialTrend.vue` (财务趋势) 与数据状态页图表需要通用图表库，前端依赖清单缺失；
  4. 另建议显式 pin `pandas` (akshare 的传递依赖，版本漂移会破坏兼容性)。

### 问题 5【低】`source_audit` 粒度与体量未界定

- **位置**: TECH_PLAN_V1.md §3.2「每个存储的值都关联一条 source_audit 记录」
- **问题**: 逐值审计在 500 字段 × ~30 万报告期行下可达亿级行；若价格行逐行审计更不现实 (1875 万 × 2)。DuckDB 扛得住，但写入开销与维护复杂度不必要。PRD §14 只要求**关键字段**溯源。
- **修法**: 在 §3.2 补充两级界定:
  1. **批次级 lineage**: 每次抓取一条记录 (源/时间/hash/行数/adapter版本)，覆盖全量数据；
  2. **关键字段级逐值溯源**: 仅对 PRD §14 要求的溯源字段 (估值、盈利、成长、安全、股东回报摘要涉及的字段) 做逐值记录。

### 问题 6【低】PF1 主机规格记录未进入任何里程碑

- **位置**: TECH_PLAN_V1.md 第 5 章 M0–M10
- **问题**: PRD §19.1 (PF1) 要求「技术规划开始时记录目标主机的 CPU、内存与磁盘类型」，作为 5 秒筛选性能验收环境的组成部分。当前所有里程碑任务清单均未提及。
- **修法**: 在 M0 (或 M1) 任务清单加一条: 记录目标 Windows 主机 CPU/内存/磁盘类型，存入 `config/` 或验收文档。

### 问题 7【低】PyInstaller onefile 形态与「一键启动」体验冲突

- **位置**: TECH_PLAN_V1.md §1.4 打包与启动
- **问题**: DuckDB + pyarrow + pandas 打包体积约 300–500MB，onefile 模式每次启动需解压到临时目录，冷启动可达 10 秒级，反复拖慢日常使用。
- **修法**: 改 `--onedir` 分发形态 (安装时解压一次，之后秒启)。PRD E6 只要求一键启动，不要求单文件。§1.4 与 M10 相应调整。

### 问题 8【微】M3 验收标注 PRD §20.1，但完整 §20.1 依赖 M5 的 DSL

- **位置**: TECH_PLAN_V1.md §5.2 M3 验收行
- **问题**: PRD §20.1 步骤 3–4 要求创建复合指标 (DSL 引擎，M5 才交付)。M3 实际只能验收 §20.1 中基于内建指标的子集。M10 有全量验收兜底，无实际影响，仅文档口径不一致。
- **修法**: M3 验收行改为「PRD §20.1 (内建指标子集)，完整 §20.1 于 M10 全量验收」。

---

## 方案中做得对、修订时不得改动的决策

1. **排名分母语义**: 先 base_pool 计算排名、后应用用户条件，精确符合 PRD §12.3 (SC11)——最容易做错的点已做对；
2. **指标快照预计算**物化到 indicator_snapshot 表: 5 秒性能目标的正确关键决策，5000 行 × 50 列估算现实；
3. **DSL 设计**: current_only 自动推导 (DL4–DL6)、维度校验 (DL7)、空值传播 + 稳定原因码 (DL8–DL9)、lark 解析不 eval (DL16)；
4. **数据源优先级**: CNINFO 真值层 → AKShare/Eastmoney 主适配 → easy_tdx/TDX 备用 → BaoStock 价格补充 → SWS 手动下载，与 findings/phase5 证据链一致，适配器可替换 + 溯源保留满足 PRD 附录 A.1；
5. **CLI 协议**: 两段式确认 (plan_id, 15 分钟)、schema_version 主版本兼容、非交互 JSON，逐条对齐 PRD §16；
6. **备份/加密**: AES-256-GCM、离线恢复密钥、冷热分层、凭据不入备份，对齐 PRD §18 (仅缺 pywin32 依赖，见问题 4)；
7. **里程碑顺序**: 符合 PRD §7.2 分阶段可用性，关键路径 M0→M1→M2→M3 正确。

---

## 评分明细

| 维度 | 得分 | 说明 |
|---|---|---|
| 约束覆盖度 | 19/20 | 120+ 约束基本逐条落地，PF1 主机记录遗漏 (问题 6) |
| 架构正确性 | 16/25 | DuckDB 多进程写冲突未设计 (问题 1)，唯一架构级漏洞 |
| 数据语义正确性 | 17/20 | 申万 fallback 分类法污染 (问题 2)、全局 MAX(report_date) 静默缩池 (问题 3) |
| 可实现性与证据链 | 19/20 | 源覆盖全部有实证，性能估算可信 |
| 完整性/细节 | 14/15 | 依赖清单 3 处遗漏 (问题 4)、审计粒度未界定 (问题 5)、onefile 启动慢 (问题 7) |
| **合计** | **90/100** | 修订问题 1–8 后预计 96+，可进入实施 |

---

## 修订清单 (供实施会话逐条执行)

- [x] 问题 1【高】: §2.2 + §2.3.4 补充 DuckDB 并发模型设计 (open-per-query 或只读+重开 / 应用级写锁 / restore 独占前置条件)
- [x] 问题 2【中】: §2.3.1 优先级矩阵 sw_industry 备用列改为「无 (缺失按 PRD §12.4 置 null + 原因码)」；M1 增加 CNINFO 行业 API 分类标准实证任务
- [x] 问题 3【中】: §2.3.3 示例 SQL 基础池改为 stock_meta 出发 + per-stock 最新快照 (LEFT JOIN LATERAL)，结果保留每股 data_date
- [x] 问题 4【低】: §1.5 增加 pywin32 (或 keyring)、pypinyin、pandas 显式 pin；前端依赖增加 echarts
- [x] 问题 5【低】: §3.2 补充批次级 lineage + 关键字段级逐值溯源的两级界定
- [x] 问题 6【低】: M0 增加记录目标主机 CPU/内存/磁盘类型的任务
- [x] 问题 7【低】: §1.4 + M10 打包形态由 onefile 改为 onedir
- [x] 问题 8【微】: M3 验收行改为「PRD §20.1 (内建指标子集)」
