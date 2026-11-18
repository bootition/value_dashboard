---
title: 42 个大师指标 × 本库科目对照与可构建性复审（含 ROCE 同型事故复查）
status: approved
category: reports
created: 2026-11-18
last-reviewed: 2026-11-18
supplements: reports/131
---

# 42 个大师指标 × 本库科目对照与可构建性复审

> **对象**：`docs/reports/131_INVESTMENT_MASTERS_INDICATOR_SURVEY_2026-11-16.md` 的 42 个指标 / 7 个流派。
> **要回答的三个问题**：①报告里提到的**科目**和我们库里的**真实列**对得上吗？②这些指标**能不能直接构建**？
> ③我们的**代码/数据库**还有哪些问题会让"能算"变成"算出垃圾"？
> **方法**：7 个流派「科目对照 agent」+ 2 个「代码路径 / §9 阻塞复核 agent」静态审查 → 主控用 4 轮只读 SQL 探针
> 对争议判定逐条实证（含推翻我这批 agent 的结论 3 条、推翻我自己第一轮结论 1 条）。
> **边界**：全程只读（DuckDB `read_only=True`），未改 schema、未改 `app/`、未实现任何指标。
> **证据**：逐指标明细 `docs/evidence/evidence-indicator-audit-matrix-2026-11-18.md`；
> 机读 `docs/evidence/evidence-indicator-audit-2026-11-18.json`（42 裁决 + 4 轮 DB 探针）；脚本与原始 SQL 在
> `.planning/2026-11-18-indicator-feasibility-audit/`（`probe_db*.py`、`sql/<流派>_<编号>.sql`、`out/`）。

---

## 0. 一页结论

| 问题 | 答案 | 关键数字 |
|---|---|---|
| **科目对得上吗？** | **没有幻觉列，但"存在"≠"对得上"**：42 个指标引用的科目逐个对照 `information_schema`（**65 张表 / 1,306 列**），**0 个凭空编造的列**（`reports/131` §0 的这条结论仍然成立）；但有 **9 类科目（10 个列）在库里根本没有对应列**、**11 处"列存在但语义不是公式要的那个"**、**8 类外部序列缺失**（GDP/CPI/信用利差/beta/机构持仓/一致预期…） | 真缺 9 类/10 列 · 语义错配 11 处 · 外部序列 8 类 |
| **能不能直接构建？** | **42/42 数据上都有米下锅，但 0 个能"原样"直接上线**：41 个必须先改口径/去护栏/剔金融股/补注册（**合计 294 条必修项，均 7 条/指标**），1 个（#16 研发强度）与本批相比最接近可上线（仍有 5 条必修项，其中 1 条是 `field_units` 单位登记）。**真正的阻塞不是缺数据，是口径与工程注册** | buildable_now 1 · after_fix 41 · blocked 0 |
| **代码/库还有问题吗？** | **代码**：落一个指标要跨 **23 个步骤 / 16 张注册表（15 张手工）**，有 **5 条静默失败路径**（漏登记→值在库里但下拉里没有；漏单位→100 倍；漏派生域→快照重发清空…）。**数据库**：`131` §9 的 9 条 P0 已**全部**有修复提交，本轮实测 **6 条已生效**（P0-1/3/4/5/6/9）、3 条属派生层自愈（P0-2/7/8）；真正的历史性缺口是 `market_cap_at_report` 逐年覆盖率最低到 **2014 年 51.4%**（2013 年 52.7%），2025 年才 96.8%（缺数，不是 bug） | 5 条静默路径 · 市值覆盖率（"当时已上市"口径）= 2013–2019 **100%**（§11.2） |

**三句话**：

1. **字段层面干净、语义层面不干净。** K1 型「中文术语 → 列名」的直译错，本轮认定 **11 处**——`fixed_assets`(PPE)
   那条已经在 ROCE 上付过学费，但这 42 个指标里同类错误仍然存在，且**都不会报错**——
   最典型的是 #33「每股派息 ÷ 总市值」（实测该列最大 **8.73e-8**，等同恒 0）、#30 的送转单位（见 §2.4 更正）。
2. **ROCE 的八个坑没有被重犯，但被换了个花样重犯。** 没人再把「固定资产」直译成 PPE，但
   **24 个指标的必修项里出现护栏/截断类写法**（QMJ 实测 **40.6% 顶格**、PEG **16.7% 的样本 >10**）、
   **28 个指标的必修项涉及历史年数或样本量更正**（#13 写 2001–2025，实测 2001–2005 可算样本 **0**）、
   **5 处"榜首/极值"是编的**（#24 说榜首是茅台，实测是 003010，茅台第 9）。
3. **要开做，先修 6 件事**（§7）：金融股剔除进 SQL、分母 ≤0 只标 `status`、`roce_10y_min/cv` 建列、
   `market_cap_at_report` 覆盖率披露、`*_yoy_fy` 用对列、派生域注册 + 重建触发。做完这 6 件，
   `131` §6.1 的第一档 5 条（6 个指标：#39 F 分数、#37/#38 Altman、#1 Graham Number、#3 E/P−国债、#27 ROCE 持久性）
   可以按顺序落库。

---

## 1. 方法与证据链

| 环节 | 做法 | 数量 |
|---|---|---|
| 科目对照 | 7 个 agent 各审一个流派：每个指标的每个中文科目 → `formula_sql` 实际列 → 列是否存在 → 语义 `exact/approximate/wrong/missing` → 分母体检（R4 四问）→ 历史可行性（K6）→ 需要什么 SQL 实证 | 42 指标 / 294 必修项 |
| 代码路径 | 1 个 agent 从 `calculator.py`/`engine.py`/`roce.py`/`schema.py`/`update.py` 反推「落一个指标要动多少地方」 | 23 步 / 16 注册表 |
| 数据阻塞 | 1 个 agent 把 `131` §9 的 9 P0 + 27 P1 逐条对 git commit 与代码行号核状态 | 9 P0 / 27 P1 |
| **主控实证** | 4 轮只读探针（`probe_db.py` / `probe_db2.py` / round3 / round4），全部聚合口径（COUNT/分位/min/max），明细一律 `LIMIT ≤ 8` | 41 条探针 |

**为什么必须主控再跑一遍**：这批 agent 里 **3 条结论是错的**（详见 §2.4），如果直接采信就会把
"已经修好的 bug 当成待修"、"把正确的 SQL 当成单位错"。这正是 ROCE 事故 K4「看到怪数先查数据缺口」的反向用法：
**看到 agent 报的"数据问题"，先查数据**。其中 1 条是**我自己第一轮的误报**（P0-4 同比 34% 反向），也一并记在 §2.4。

**库被写锁时的处理**（`AGENTS W4`）：审查期间本机正在跑自动更新（PID 90924，15:39 起，价格步骤 5,571 只），
DuckDB 是单写者模型 → 只读连接会被 Windows 独占。所以：agent 阶段**完全不连库**、只写 SQL 到
`.planning/.../sql/`；主控用带重试的探针在**写锁释放的间隙**一次跑完（实测第 2 次尝试即成功，41 条探针总耗时 < 1s/条）。

---

## 2. 科目对照结论

### 2.1 权威清单：1,306 列，不是 23 列

`131` 的字段基线 `context.md` 给调查员的是**手写摘要**（`indicator_ext` 只列 23 列），
而实库 `information_schema` 是 **65 张表 / 1,306 列**。本轮第一版静态清单（从 `schema.py` 的
`CREATE TABLE` 抽取）又漏掉了全部 `ALTER TABLE ADD COLUMN` 迁移列（如 `indicator_ext.market_cap_at_report`、
`report_date_close`、`bps_parent` 都在 `for column in (...)` 循环里加的），
导致 4 个 agent 把它们误判成"缺列"。**结论：字段存在性只能以 `information_schema` 为准**，
静态清单只能用来说明"这列是哪次迁移加的"。清单 v2 与 DB 权威版都已归档。

### 2.2 真缺的科目（库里确实没有这一列）

| 缺的科目 | 影响指标 | 影响方式 |
|---|---|---|
| `balance_sheet.gross_ppe`（固定资产原值）/ `accumulated_depreciation`（累计折旧） | #37（Z 的 X3）、#40（AQI/DEPI）、#42（c5） | 只能用**净值**替代，与原始公式**不等价**（`131` §5 G5），必须标 `approximate` |
| `balance_sheet.other_current_assets`（其他流动资产） | #42 | Observed 用「应收+存货」近似 → 系统性偏小 |
| `balance_sheet.restricted_cash`（受限货币资金） | #2、#36 | 净现金/NNWC 偏乐观（把受限资金当可用现金） |
| `balance_sheet.receivables_financing`（应收款项融资） | #2、#40、#42 | FY2019 新金融工具准则后应收票据断崖，缺此列则 2019 前后的应收口径不可比 |
| `balance_sheet.long_term_payables`（长期应付款） | #4 | c2 的长期负债系统性偏小 |
| `balance_sheet.investment_property`（投资性房地产） | #15（林奇「隐蔽资产」） | 全库只有 `cash_flow_indirect.investment_property_depreciation`（折旧），没有资产科目 |
| `balance_sheet.development_expenditure`（开发支出/资本化研发） | #16 | 研发强度只能算费用化部分 |
| `balance_sheet.inventory_provision`（存货跌价准备） | #2、#32 | 清算价值高估存货 |
| `indicator_snapshot.roce_10y_min` / `roce_10y_cv` | #27、#30 | **不是缺数据，是缺列**：`roce_history` 有 105,168 行，但没有落地这两个聚合列（实测 `indicator_snapshot` 只有 `roce_latest/5y_median/5y_years/10y_median/10y_years/20y_median/20y_years/eligible` 共 8 列） |

**8 类外部序列缺失**（不是"缺一列"，是整张表没有）：GDP（#8）、CPI/平减指数（#34/#41）、
信用债收益率/信用利差（#3/#5/#35）、beta（#24）、机构/基金持仓（#17 的 I 项）、
一致预期/盈利预测（#13 PEG 原意）、真实变现折扣系数（#32）、全球跨市场估值（#33 的世界最低 PE 部分）。

> **注意**：其中 **#17/#18 的"指数序列缺失"是我的 agent 误判**（见 §2.4）——库里其实有。

### 2.3 语义错配 11 处（列真实存在，但不是公式要的那个东西）

这是 K1「把中文术语直接当列名」的同一类错误，也是本轮最值钱的部分：

| # | 指标 | 公式写法 | 实际列语义 | 后果 |
|---|---|---|---|---|
| 1 | #19 神奇公式 | 「已动用资本 = 净营运资本 + 净固定资产」，标 `exact` | `total_assets − total_current_liabilities` = **净营运资本 + 全部长期资产** | 与 Greenblatt 原文不等价（`131` T3 已认定），必须降级为 `approximate` 并改名「A 股适配口径」 |
| 2 | #33 邓普顿 | 股息率 = `dividend_per_share ÷ market_cap_at_report` | **每股派息 ÷ 总市值**（量纲错 1e9） | 实测该列最大 **8.73e-8** ≈ 恒 0 → 第二道闸门永久失效且不报错（`131` T1） |
| 3 | #30 Akre 腿 2 | `EXP(SUM(LN(1 + songzhuangu)))` | 实证：`songzhuangu` 是**每股送转比例**（见 §2.4） | **该写法是对的**；反而是 `extended.py:401` 的 `/10.0` 是 bug |
| 4 | #7 / #12 | 归母净利 ÷ 合并口径 D&A/capex | 分子归母、分母合并 | 少数股东权益 >25% 的 290 家（万科 50.4%、中建 40.2%）系统性扭曲 |
| 5 | #9 | 有息负债 = `short_term_loans + long_term_loans` | 银行体系里这两列**语义失效**（存款/同业不是"借款"） | 招商银行「有息负债/权益 0.499」是伪造值，恰好通过两道闸 |
| 6 | #11 | 送股/转增「不影响未分配利润」 | 转增（资本公积→股本）不影响，**送红股是从未分配利润转出** | ΔRE 被送股污染，need `xdxr` 还原 |
| 7 | #17 / #18 | 指数基准用 `price_daily_qfq` | 该表**没有任何指数行**（只有 6 位股票代码） | 语义空壳；正确路径是 `index_valuation.extra->>'index_close'`（见 §2.4） |
| 8 | #24 | 「QMJ 是纯质量因子、与估值正交」 | Altman 项含 `market_cap_at_report / total_liabilities` | 携带市值暴露，与价值因子不正交（报告自己的论断与 SQL 矛盾） |
| 9 | #24 | `indicator_ext.accruals` 分子用 `net_profit` | 同一指标 ROE 用 `parent_net_profit` | 同一 QMJ 内两种净利口径并存 |
| 10 | #18 | 文档写「52 周 = 近 250 交易日」 | SQL `hi_52w = max(close)` 覆盖 **约 325 个交易日** | 实测 1,465 只（27.1%）被多算，最大偏 26.5pp |
| 11 | #3 | 用 `pe_ttm` 反算 E/P | `pe_ttm` 对亏损股为 NULL | 静默丢掉全部亏损样本（非空仅 3,751/5,575） |

另有 3 处「列在义失」：`cash_flow_activity.dividend_interest_paid`（FY2025 后全空）、
`index_valuation.div_yield`（列存在但实质全空）、`income_statement.exchange_gain`（是公允价值变动，`131` 陷阱 4，本轮未新增）。
另 1 处旧账 `fixed_assets`=PPE 已由 `reports/129/132` 定案（ROCE 用「总资产 − 流动负债」），本轮 42 个指标里
**没有**再出现"固定资产=PPE"的直译——这是 ROCE 事故唯一被真正防住的一条。

### 2.4 主控复核：推翻 3 条 agent 结论 + 我自己 1 条误报

| 结论 | agent 说的 | 实测（SQL 见 `out/db_evidence_round*.md`） | 处置 |
|---|---|---|---|
| **#30 送转单位** | 「`songzhuangu` 是每 10 股，SQL 少除 10，是 K1 级最高优先错误」 | **反了**。两轮独立实证：①用 1,056 个「`songzhuangu ≥ 1` 且股本跳变落在 0.5–20 倍」的事件算，`growth/(1+sz)` 中位 **1.000**、`growth/(1+sz/10)` 中位 1.818；②按 `songzhuangu` 取值分 6 档（共 5,421 个事件），**每一档** `growth/(1+sz)` 都 = **1.000**（按 `/10` 解释则为 1.09–4.26）。**`songzhuangu` 存的是每股比例**。旁证：修复前 `indicator_ext.bonus_share_ratio_3y` 在 **2025 年报截面**最大只有 **0.353**（若口径对，10 送 10 三年应为 3.53；全表 9.94 来自 600602 1993–1995 的异常行 `songzhuangu=99.0`）。修复并重算后 2025 年报 max = **3.533**（§11.1） | **#30 的 SQL 正确；`app/core/indicators/extended.py:401` 的 `/10.0` 才是 bug**（独立缺陷，见 §5.3） |
| **#17/#18 指数序列** | 「库里没有指数行情，M 项/基准只能标 missing」 | `index_valuation.extra` JSON 里有 `index_close`：122,699 行有值 / 43 个指数。**但要分两类**：96% 是**申万行业指数**（SW8010xx 等 31 条，**日频** 5,039 点/条）；**`000300` 只有 265 个点（月末，密度 5.1%）** | 属**字段路径写错**，不是缺数据（行业基准可用）；但**拿 000300 当日频基准会让 μ 全空**——beta/相对强度要么用申万行业指数，要么用「全市场市值加权代理」（§11.4） |
| **#19/#20/#21 等 21 个用市值的指标** | 「先修 P0-9 才能发布」 | P0-9 **早已修好**：689009 最近 8 个报告期 `市值/(收盘×股本)` 全部 = **1.0**；全库最新报告期 5,575 个观测里 `bad_rows = 0`（5,546 个可比） | 从"阻塞项"降级为"已验证无风险"，不要再把它写进待修清单 |
| **#4 分红 streak 扇出比例** | 「108,482 行 vs 69,242 样本 = +36%」 | 按行数重算是 **+56.7%**（108,482/69,242−1）；+36% 无法复现（可能是审查员用了别的分母） | 报告统一用 **+56.7%**；矩阵里保留 agent 原文以便追溯 |
| **P0-4 同比双口径**（我自己第一轮） | 「1,900/5,536 = 34% 方向相反 ⇒ 仍未修」 | **我的探针拿错了列**：`revenue_yoy` 修好之后是 **TTM** 口径，跟年报口径比当然不同。正确的检验是 `revenue_yoy_fy`：**5,530 个非空、方向相反 0 个** | 我的假警报，记录在此；P0-4 属**已修** |

---

## 3. 42 个指标的裁决

### 3.1 总表

> `buildable_now` = 可直接落成正式指标；`after_fix` = 字段/数据够，但必须先改 X（X 见附录矩阵）；
> `blocked` = 缺数据或结构性不可算。**ROCE 同型** = 报告 §4.1 五类通病的任一（T1 量纲 / T2 分母不挡负 /
> T3 广义冒充狭义 / T4 护栏失效 / T5 样本不足）。逐条明细见 `docs/evidence/evidence-indicator-audit-matrix-2026-11-18.md`。

| # | 流派 | 指标 | 裁决 | ROCE同型 | 必修项数 | 最关键的一条（完整 294 条见矩阵） |
|---|---|---|---|---|---|---|
| 1 | 格雷厄姆 | 格雷厄姆数 Price/GN | after_fix | 否 | 7 | P0-9 闸门（阻断级）：在落库 SQL 里对 /market_cap_at_report/(report_date_close |
| 2 | 格雷厄姆 | NCAV / 净现金折价 | after_fix | 否 | 6 | 把行业剔除写进 SQL（阻断级）：COALESCE(m.sw_level1,'') NOT IN ('银行','非银金融')，不 |
| 3 | 格雷厄姆 | E/P − 2×10Y 国债 | after_fix | 否 | 6 | P0-9 闸门（阻断级）：对 market_cap_at_report 与 report_date_close×当期股本偏差 > |
| 4 | 格雷厄姆 | 防御型七准则 0–7 | after_fix | **是** | 8 | ★c4 改造（阻断级）：先把分红年份截断为 yy <= year(report_date)，再按股计算「截至 T 年已连续 >= |
| 5 | 格雷厄姆 | V = EPS×(8.5+2g) | after_fix | 否 | 7 | ★g 置 NULL（阻断级）：改成 CASE WHEN eps3_now > 0 AND eps3_7y > 0 THEN LE |
| 6 | 格雷厄姆 | 施洛斯四因子 | after_fix | 否 | 8 | ★f3 口径改正（阻断级）：改为「最近 10 个年报」取 min（ROW_NUMBER() OVER (PARTITION BY |
| 7 | 巴菲特 | 股东盈余 / OE 收益率 | after_fix | **是** | 5 | 加金融股剔除：用 app/core/supplement.py:507 的 FINANCIAL_SECTOR_UNIVERSE_ |
| 8 | 巴菲特 | 巴菲特指标（证券化率） | after_fix | 否 | 6 | 改名：明确写「A 股适配版 = Σ市值/Σ营业总收入，不是证券化率」，禁止被当成巴菲特指标使用（§5 G1） |
| 9 | 巴菲特 | 十年 ROE≥15% + 低负债 | after_fix | 否 | 5 | 金融股双保险剔除：FINANCIAL_SECTOR_UNIVERSE_PREDICATE（app/core/supplement |
| 10 | 巴菲特 | 毛利率十年稳定性 | after_fix | 否 | 6 | 输出'最新年报毛利率'列位（K7）：现 SQL 只有 median/min/max/range/std/trend，正是'中位数 |
| 11 | 巴菲特 | 一美元测试 | after_fix | **是** | 6 | 护栏收紧（T4）：ΔRE ≥ max(5%×期初归母权益, 2%×期末市值)，或分母改三年滚动平均 ΔRE；并对 /one_do |
| 12 | 巴菲特 | 留存资本化率 RCR | after_fix | **是** | 6 | 分母加相对 guard（ΔRE ≥ 5%×期初归母权益，与一美元测试对齐），或对 /RCR/>10 输出 status 并单列  |
| 13 | 成长 | PEG | after_fix | **是** | 5 | 先重跑 vd data compute-extended-indicators 修 689009 市值（400.4 亿 vs 表 |
| 14 | 成长 | PEGY | after_fix | **是** | 5 | 与 PEG 同步修 689009：重跑 compute-extended-indicators 或剔除；并修正 market_c |
| 15 | 成长 | 林奇六分类 | after_fix | **是** | 5 | 落地 SQL 自己写的 trap ③：WHERE sm.sw_level1 NOT IN ('银行','非银金融')（实测 42 |
| 16 | 成长 | 研发强度 + 销售效率 | **buildable_now** | 否 | 5 | 非阻塞加固：显式 WHERE sm.sw_level1 NOT IN ('银行','非银金融')，或至少输出被自然排除的金融股只 |
| 17 | 成长 | CANSLIM 盈利引擎 | after_fix | **是** | 5 | 单季同比加双边约束 25%≤g3≤300%，并要求单季归母净利绝对值 >约 2,000 万；输出 g 分位与 ≥500% 计数（ |
| 18 | 成长 | 相对强度 RS | after_fix | 否 | 6 | hi_52w 改为 max(CASE WHEN rn<=250 THEN close END) 或 high 的近 250 日  |
| 19 | 量化价值 | 神奇公式 | after_fix | **是** | 9 | ①把 CE 的 match 由 exact 改为 approximate，formula 正文改为『已动用资本 = 总资产 −  |
| 20 | 量化价值 | 聂夫总回报率 | after_fix | **是** | 8 | ①把『样本 3,251 只』改成实测 2,695，并列出漏斗（净利>0 4,345 → 有市值 3,874 → 有分红 3,42 |
| 21 | 量化价值 | 德雷曼逆向 | after_fix | **是** | 8 | ①加与 Magic Formula 同源的重述兜底：AND NOT (营收同比 > 2 OR 总资产同比 > 2)，或对 PE< |
| 22 | 量化价值 | 趋势价值 | after_fix | 否 | 8 | ①补上公式承诺的第二层 10% 切割：LIMIT 100 → LIMIT 33，或先 ntile(10) 再取第 1 档；否则指 |
| 23 | 量化价值 | FF 五因子暴露 | after_fix | 否 | 8 | ①把 RMW 覆盖率三个数换成 FY2024 实测值 5,809 / 5,734 / 5,800（分母 5,904），把现在的  |
| 24 | 量化价值 | QMJ 综合 z | after_fix | **是** | 9 | ①Altman Z 的 0.6·MVE/TL 不要用固定 3.0 截断（40.6% 顶格），改为按年截面 1%/99% wins |
| 25 | 质量 | 现金转换率 | after_fix | **是** | 6 | annual CTE 加 `JOIN stock_meta m USING (stock_code)` 并 `COALESCE( |
| 26 | 质量 | 毛利率费用率双稳态 | after_fix | 否 | 7 | revenue_cagr4 增加连续性校验：「rn=1 与 rn=5 的 report_date 相差恰为 4 年」，否则置 N |
| 27 | 质量 | ROCE 持久性 | after_fix | 否 | 8 | 新增两列：schema.py 加 v46 迁移 `ALTER TABLE indicator_snapshot ADD COLU |
| 28 | 质量 | 增量资本回报率 | after_fix | 否 | 10 | `COALESCE(income_tax, 0)` 改为「income_tax IS NULL → 0.25 中性税率」，否则缺 |
| 29 | 质量 | 再投资率 × 增量 ROIC | after_fix | 否 | 7 | 把 data_mapping 的「再投资率（已物化近似）」整行删除，或把 component 改名为「实际增速（不可替代再投资率 |
| 30 | 质量 | Akre 三腿凳 | after_fix | 否 | 9 | **【最高优先·K1】送转折算改口径**：`EXP(SUM(LN(1 + x.songzhuangu)))` → `EXP(SU |
| 31 | 风险 | Net-Net 折价 | after_fix | 否 | 5 | ① 先修 P0-9 或加黑名单：任何按 market_cap_at_report 排序的排名都要先排除 689009（`chan |
| 32 | 风险 | 折价清算价值 | after_fix | 否 | 6 | ① SQL 加 `AND b.total_liabilities IS NOT NULL`（潜在护栏，当前 0 行受影响，防数据 |
| 33 | 风险 | 扣非低 PE + 总回报 | after_fix | **是** | 7 | ①【P0】股息率改为 `100.0*coalesce(dps_12m,0)/nullif(close_end,0)`（price |
| 34 | 风险 | 个股 CAPE10 | after_fix | 否 | 7 | ① 分母加下限或显式标记：cape10>1000 标 UNSTABLE/不参与绝对阈值（**不要**用 LEAST 截断——那是 |
| 35 | 风险 | 市场温度计 | after_fix | 否 | 7 | ①【T5】聚合 CAPE10 加样本量下限（`HAVING count(*) >= 1500` 或只在 yr>=2010 输出） |
| 36 | 风险 | 资产负债表弹性 | after_fix | **是** | 8 | ①【T2/P0】两个「÷归母权益」的比率加正数护栏：`total_equity_parent > 0`（或比率处 GREATES |
| 37 | 排雷 | Altman Z | after_fix | 否 | 7 | SQL 里显式 LEFT JOIN stock_meta 并输出 industry / in_financial 标记；金融股剔 |
| 38 | 排雷 | Altman Z'' | after_fix | 否 | 8 | ★ 增加「剔除预收款项+合同负债后的经营性营运资本」辅助列，并与原 X1 版并列输出危险区名单：2024 年危险区 951 只中 |
| 39 | 排雷 | Piotroski F | after_fix | 否 | 8 | ★ 9 个 CASE 全部改成「先判 NULL 再判 0/1」，并输出 f_valid_n（可算信号数）与 f_score；现在 |
| 40 | 排雷 | Beneish M | after_fix | 否 | 8 | ★ AQI 分母加零保护：`nullif(1-(ca+coalesce(ppe,0)+sec)/ta, 0)`；同时给 dsri |
| 41 | 排雷 | Ohlson O | after_fix | 否 | 9 | ★ history_years 从「1995-2025 共 31 个年报点」改为「1998-2025 共 28 个年报点」（19 |
| 42 | 排雷 | Montier C | after_fix | 否 | 10 | ★ 6 个 CASE 全部改成「先判 NULL 再判 0/1」，并输出 c_score 与 c_valid_n（可算信号数）；c |

> ⚠️ **表格里 16 条「P0-9 闸门（阻断级）」已过期**：#1/#3/#4/#6/#13/#14/#15/#19/#21/#22/#23/#31/#33/#36/#37/#42 的首条必修项
> 是各 agent 在**静态阶段**写的（那时还不知道库里已修好）。实测 689009 八期市值比全为 1.0、最新期 `bad_rows=0`，
> 所以这些"闸门"落地时**降级为一条市值交叉体检列**即可，不必再等修数。

### 3.2 与 `reports/131` §6 分档的差异

| 131 的裁决 | 本轮裁决 | 为什么不同 |
|---|---|---|
| §6.1 第一档 5 个「改完即可用」 | **一致**（#1/#3/#27/#37/#38/#39 全为 after_fix） | 但 #27 需要**新建 2 列**（131 说"零新增数据成本"只说对了数据、漏了列） |
| §6.3「❌ 不建议做」：#4、#21 | **after_fix** | 131 的 ❌ 指"按当前交付物不可交付"；本轮逐条核到**修法明确且成本可控**（#4 修扇出+截断、#21 加重述兜底），不是永久不可算 |
| §6.3「NCAV 不建议（A 股几乎没标的）」#2、#31 | **after_fix** | 不做选股、改做横截面分位即可（§3.1 #2/#31 必修项已写） |
| §6.3「巴菲特指标只能改名」#8 | **after_fix + 硬约束** | 与 131 一致，本轮补上"必须输出 n_stocks 与锁池" |

---

## 4. 这次有没有重犯 ROCE 的八个坑？

| 坑 | 有没有重犯 | 本轮实测证据 |
|---|---|---|
| **K1** 中文术语直译成列名 | **换成了新变体，本轮认定 11 处** | §2.3 全表；最严重 #33（每股÷总市值，实测 max 8.73e-8）、#19（CE 标 exact 实为另一口径） |
| **K2** 修正项在样本外变误差制造项 | **有，但已被识别** | #12 主口径正确地**没有**剔现金（避开了 K2）；#28 的 `ex-cash` 口径并列输出，是唯一合法处理方式 |
| **K3** 用护栏掩盖看不懂的数字 | **重犯了，24 个指标的必修项里出现护栏/截断** | QMJ `LEAST(...,3)` **40.6% 顶格**；#5 `GREATEST(NULL,0)` 把 29.7% 记成零增长；#20 `LEAST(...,25)`；#13 0.5% 下限 |
| **K4** 看到怪数先改公式、不先查缺口 | **重犯了（报告层面）** | 典型：#17/#18 把"指数基准"写成 `price_daily_qfq`（表里 0 行指数），正确做法是先查表里有没有（本轮实测：`index_valuation.extra` 有 122,695 行 index_close） |
| **K5** 派生列没有重建触发 | **尚未发生，但风险最高** | 21 条必修项涉及 `derived_snapshot_domains()`；目前**只有 roce 一个派生域**先例（`calculator.py:67-80`） |
| **K6** 用上市年限冒充数据年数 | **重犯了，28 个指标涉及历史年数/样本量更正（其中 6 个被 agent 明确打上 K6）** | #13 写"2001–2025"（实测 2001–2005 可算样本 **0**，真实 2011 起）；#9/#10 `HAVING>=8` 冒充十年；#28/#29 说"2013 起第一个可算点"（实测 1994 起） |
| **K7** 只给中位数不给现况 | **重犯了，15 个指标的必修项要求补三列位** | #10 只有 median/min/max/std，无最新值；#9 只有 10 年中位——智飞生物那类"历史好现在崩"看不见 |
| **K8** 不用外部标杆校验量级 | **部分重犯** | 报告里 5 处"榜首/极值"是**编的**（#24 榜首 003010 而非茅台、#12 样本 5,071 实为 2,507、#20 3,251 实为 2,695、#21 3,274 实为 1,629、#25 "1998 年 993/1,192" 应为 1,201） |
| **R4 四问** | **分母纪律明显好于 ROCE 首版** | 42 个 SQL 里没有一个是"分母为负不挡"：多数用 `>0` 过滤 + `NULLIF`；真正的缺陷是**用过滤代替 status**（把"数学不成立"变成"这一行不出现"），以及**近零分母不标记**（55 只 cape10>1000x、882 只负 WC） |

**量级校验（K8 的正面应用）**：我按 SKILL 第 4 步对第一档做了独立量级校验，三个指标都落在合理档位：

| 指标 | 实测（2025-12-31 截面） | 外部标杆 | 判定 |
|---|---|---|---|
| #1 Graham Number 的 `price/GN` | n=3,956，中位 **2.499**，p90 7.47，过线 344 只 | `PE×PB ≤ 22.5` 的等价式；A 股常年高于红线 | 合理 |
| #37 Altman Z | 探针 A（含 `wc` 非空全部）：n=5,525，中位 **4.052**；探针 B（再要求 `tl>0` 且 `ta>0`）：n=5,347，安全区 **63.5%**、危险区 1,074 只、**p20 = 1.81** | 原始 1.81/2.99 | 中位超阈值 ⇒ **原阈值不能照搬**；但 `1.81 ≈ p20` 说明改用"后 20% 分位"仍可用 |
| #3 E/P − 2×10Y | n=3,994，中位 E/P **2.112%**，10Y **1.847%**，2× 阈值 3.695%，过线 1,055 只 | 股权风险补偿应 >0 且薄 | 合理（也印证"以国债替代 AAA 门槛偏低 50–150bp"） |

---

## 5. 代码侧问题（能不能落成"筛选里能用"）

### 5.1 落一个指标要动 23 个地方，其中 16 张注册表 15 张是手工的

关键结论（证据在 `.planning/2026-11-18-indicator-feasibility-audit/out/code_build_path.md`）：

| 环节 | 位置 | 通用? | 静默风险 |
|---|---|---|---|
| 列 DDL + 迁移版本 | `storage/schema.py` | 半自动 | 只在 fresh DB 生效；漏 `INSERT schema_migrations` 会每次启动跑全量 DDL |
| **筛选白名单** `SNAPSHOT_COLUMNS` / `EXTENDED_COLUMNS` | `screening/engine.py:48-78`（SNAPSHOT_COLUMNS）/ `engine.py:169`（EXTENDED_COLUMNS） | **手工** | **值在库里、下拉里没有**（无 DDL→白名单完整性测试） |
| **保存期校验** `validate_rule_fields()` | `screening/engine.py:1686` | **手工（第二份白名单）** | 已知缺陷 **CB-09**：不含 `EXTENDED_COLUMNS` → 扩展字段保存规则时报"未知字段" |
| DSL 元数据（unit/period_type/historical_capable） | `dsl/ast_nodes.py:150-310` | 手工 | 不补 DSL 用不了 |
| **后端单位 / 前端单位（两份）** | `screening/field_units.py` + `frontend/src/utils/screening-format.ts` | 手工 | 两处都不登记 → RG3 绿 → **少 100 倍** |
| 详情页历史曲线 | `web/api/stock_detail.py:94-264` | 手工 | 不补 → 页面仍宣称"可看历史"但接口 400（ROCE 现状） |
| **派生域注册** | `calculator.derived_snapshot_domains()` | 有机制但**只有 roce 一个先例** | 不登记 → 快照重发静默清空（K5） |
| 自动更新步骤 | `update.py` + `update_steps.py::STEP_ORDER` | 有机制 | 门控写错 → 永久 `up_to_date`，永不重建 |
| 空值原因 | `quality/null_reasons.py` + 维护脚本 | 半自动 | 只在维护脚本里跑 → 诊断永远是旧一代 |
| 行业适用性（金融股判据） | `roce.py:183` / `supplement.py:507` / `null_reason_sweeper.py:752` | **三份手写字符串且集合不一致** | 漏写 → 121 只金融股混入；用 `csrc_l1` → 同花顺被误剔 |

### 5.2 五条「不报错的失败路径」

1. 计算器 record 的 key 不在 `PRAGMA table_info` 里 → **列被静默丢弃**（`calculator.py:2604-2617` `_insert_records_in_connection`）；
2. 价格敏感字段没加进 `_PRICE_SENSITIVE_FIELDS` → 算完即丢，只有全量重算日有值（`calculator.py:919`）；
3. 字段没进 `SNAPSHOT_COLUMNS` → 筛选下拉看不见（值在库里）；
4. 单位两处只登记一处 → 展示/阈值差 100 倍（RG3 只单向校验）；
5. 派生列没登记 `derived_snapshot_domains()` → 快照重发后被写成 NULL（ROCE 六列事故）。

### 5.3 本轮新发现的代码缺陷（不在 131 范围）

| 编号 | 缺陷 | 证据 | 影响 |
|---|---|---|---|
| **X-SZ10** | `xdxr.songzhuangu` 是**每股比例**，`extended.py:401` 却 `/10.0` | 1,056 个事件实测 `股本跳变/(1+sz) = 1.000`（分 6 档全部成立） | `bonus_share_ratio_3y` 少算 10 倍（修复前 2025 年报 max 仅 0.353，修复并重算后 3.533，§11.1）；凡引用它的"送转还原"逻辑全部偏小 |
| **X-V46** | 工作区未提交的 ROCE v46（`schema.py` v45→46、`ELIGIBLE_MIN_YEARS` 8→5、engine 改 5/10/20 三窗口）与 `roce.py:216` 的 `SNAPSHOT_DERIVED_COLUMNS`（仍 6 列）、`STATUS`/`reports/132`/前端标签（仍写 ≥8）**不同步** | 审计开始时 `git status` 有 **8 个未提交文件**（全是这套在途改动）；DB 里 `roce_20y_median` 已建列但**实测 0 行有值**；`scripts/check_rework_guards.py` **RG4 当前是红的**（重建 SQL 写了 20y、登记表没写） | 上线顺序错一步就会"列建了但永远空"，或重建时引用不存在列被 guard 吞成 warning |

---

## 6. 数据库侧问题（`131` §9 的 P0 今天到底修没修）

| 编号 | 问题 | 静态状态（agent） | **本轮实测** | 结论 |
|---|---|---|---|---|
| P0-1 | 科创板成交量放大 100 倍 | 已提交修 + 修复脚本 | 科创板 `volume/circ_shares` 中位 **0.0303** vs 其他板块 **0.0489**（同量级） | ✅ 已修 |
| P0-2 | 快照重发清空 `roce_*` | 已修（派生域守卫） | `roce_latest` 5,460 / `roce_10y_median` 5,460 有值 | ✅ 已修 |
| P0-3 | FY2025 现金流两表半空壳 | FIELD_MAP 已扩 | `cash_flow_indirect` 2025：`fixed_asset_depreciation` **5,571/5,579**、`net_profit` **5,579/5,579**；`cash_flow_activity`：`capex` **5,576/5,579**（探针输出里该列表头被 UNION 对齐成 `n_dep`，实际统计的是 `capex`） | ✅ 已修 |
| P0-4 | 营收/净利同比两套口径 | 已改 TTM + 增 `*_yoy_fy` | `revenue_yoy_fy` **5,530 非空、与年报口径方向相反 0 个**；`revenue_yoy`（TTM）与年报口径仍有 1,900/5,536 方向相反，属**两套口径并存的设计**、不再是缺陷 | ✅ 已按设计分流（我第一轮的 34% 是拿 TTM 列比年报口径的误报） |
| P0-5 | 技术三指标 47% 覆盖 | 已加门禁 + 重算触发 | `period_return` **5,575/5,575**、`annualized_volatility` 5,559 | ✅ 已修 |
| P0-6 | 保险股保险合同负债混入 | 已加行业守卫 + 改道 | 2025-12-31 保险股占 `contract_liabilities` 合计 **0.0%**；但**历史 3 行仍是脏数据**（601319 中国人保 FY2011/2012/2013 = 491.6/503.1/416.4 亿，合计 **1,411.1 亿**，占这 3 年全市场 `contract_liabilities` 的 **100%**） | ⚠️ 当前期已修，**历史段未清** |
| P0-7 | 减值符号 FY2018 翻转 | 派生归一化字段 | 符号翻转实测确认（2017：正 3,317 / 负 1,213 → 2018：正 1,020 / 负 4,424） | ✅ 断点确认（跨 2018 必须用派生字段） |
| P0-8 | 预收→合同负债重分类 | 派生合并字段 | `prepayments_received` 37,379.8 亿（2019）→ **1,284.3 亿**（2020，−96.6%）；`contract_liabilities` 393 → 5,763 行 | ✅ 断点确认 |
| P0-9 | 689009 市值错 10 倍 | 指纹 + 重建触发 + 回填 | 689009 最近 8 期 `市值/(收盘×股本)` **全部 1.0**；全库**最新 1 个报告期** 5,575 行 **bad_rows=0**（可比 5,546） | ✅ 当前期已修；**历史 24 期未逐期复核**（探针只打到最新期 + 689009 的 8 期），落地时保留市值交叉体检列 |

**仍然真实存在的缺口（不是 P0，但会污染"历史中位数"类指标）**：

| 缺口 | 实测 | 影响 |
|---|---|---|
| `market_cap_at_report` 逐年覆盖率（**口径更正，见 §11.2**） | 表内比：2013 **52.7%**、2014 **51.4%**、2020 66.1%、2025 96.8%；按「**当时已上市**」口径重算：2013–2019 **100.00%**（2,248/2,248 … 3,540/3,540）、2020 99.04%（3,934/3,972），**pre-IPO/退市股 0 行有市值（这是正确行为）** | 原判断"缺失＝偏大盘偏差"**作废**：缺的那一半是当时还没上市/无行情的公司，本就不该有市值。**做历史分位只要求"当时在市"，不需要补数** |
| `indicator_ext` 季报 D&A 结构性缺失 | 2026-09-30：`ebitda`/`depreciation_amortization` 仅 **5/5,575**；2025-12-31 正常 5,533/5,625 | 季报口径的 EV/EBITDA、经营杠杆在"最新期"全空（年报表内指标不受影响） |
| `xdxr` 未来除权日 | `dividends` 有 **23 行** `ex_date > 今天`（max 2026-11-26） | 任何不按 `ex_date <= report_date` 截断的股息率都会引入未来函数 |
| `sw_level1` 为 NULL | 751 行（其中仅 52 行仍在市）；`csrc_l1` 只能兜底 11 行 | 裸 `NOT IN ('银行','非银金融')` 会静默丢 NULL 行 → 必须 `COALESCE(sw_level1,'') NOT IN (...)` |
| 保险股历史残留（P0-6 尾巴） | 601319 中国人保 FY2011/2012/2013 的 `contract_liabilities` = 491.6/503.1/416.4 亿（合计 **1,411.1 亿**），占这 3 年全市场合计的 **100%** | 2011–2013 年任何「合同负债/总资产」类排名与时间序列都是脏的（当前期已修，历史段未清） |
| `indicator_null_reason` 原因码 | 129,934 行 / 69 字段 / **仅 5 个 reason code**（`SKILL` 定义 6 类） | 新指标的"空值是数学不成立还是没抓到"无处登记（R4-4） |
| `roce_20y_median` | 列已建（v46）但 **0 行有值** | 在途改动的收尾项，需重算 ROCE 才生效 |

---

## 7. 能不能直接完成构建？—— 能，但要先修 6 件事

**答案分两层**：

- **数据层：能。** 42 个指标全部有可用字段；缺失的 9 类科目（10 个列）+ 8 类外部序列**没有一个是致命阻塞**——
  每个都有明确的替代口径（gross PPE→净值、GDP→Σ营收、信用利差→国债+标注、beta→自建回归、机构持仓→标 missing）。
- **工程层：不能"直接"。** 目前没有任何一个指标能"写个 SQL 就出现在筛选里"——要跨 23 步、15 张手工注册表；
  而且**历史中位数型**（#9/#10/#27/#34…）和**打分型**（#39/#40/#41/#42）都只有 ROCE 一个手工先例可抄。

**开做前的 6 件事（按顺序，都是小改动）**：

| # | 先修什么 | 为什么必须先做 | 落点 |
|---|---|---|---|
| 1 | **金融股剔除进 SQL**：统一成 `COALESCE(sw_level1,'') NOT IN ('银行','非银金融')` + `financial_sector_items` 兜底，并**合并三份手写判据** | 已让齐鲁银行/江苏银行霸榜、118 只 asset_play 里 60 只是金融股；三处字符串集合还不一致 | `roce.py:183` / `supplement.py:507` / `null_reason_sweeper.py:752` → 单一函数 |
| 2 | **分母 ≤0 / 近零只标 `status`，不截断不置空** | 24 个指标的必修项里出现护栏类写法，QMJ 40.6% 顶格、`GREATEST(NULL,0)` 把 29.7% 记成零增长 | `docs/skills/composite-indicator` 第 5 步 + 每个指标的 `*_status` 列 |
| 3 | **建 `roce_10y_min` / `roce_10y_cv`（schema v47）** 并登记派生域 | #27 的全部内容就是这两列；131 说"零新增成本"漏了这一点 | `schema.py` v47 + `roce.py::SNAPSHOT_DERIVED_COLUMNS` |
| 4 | **`market_cap_at_report` 逐年覆盖率做成指标卡披露项** | 52.7%–96.8% 的覆盖率决定了"10 年分位"能不能当结论 | `asset_health` 新增 disclose 级规则 + 前端指标卡 |
| 5 | **同比一律用 `*_yoy_fy`，TTM 用 `*_yoy`**，并在 DSL 元数据写死 `period_type` | 我第一轮就是拿 TTM 列比年报口径才误报 34% | `dsl/ast_nodes.py` 元数据 + 报告 |
| 6 | **每个新指标落 `derived_snapshot_domains()` + `update.py` 重建步骤 + 三列位（latest / 5y / 10y）** | K5 与 K7 是这批指标最容易复发的两条 | 抄 ROCE 三件套 |

**建议顺序（三波）**：

1. **第 1 波（风险最低、信息量最高，`131` §6.1 的第一档 5 条 = 6 个指标）**：#39 Piotroski F → #37/#38 Altman Z/Z'' → #1 Graham Number → #3 E/P−2×10Y → #27 ROCE 持久性。
   共同点：字段全真、口径争议小、都有外部标杆可校验；每人 5–9 条必修项，集中在"金融股剔除 + status 列 + 三列位"。
2. **第 2 波（历史中位数型，先解决覆盖率披露）**：#9 ROE 十年、#10 毛利率十年、#34 CAPE10、#35 市场温度计。
3. **第 3 波（要改造公式的）**：#19 神奇公式（CE 改名）、#33 邓普顿（单位修好+参数化年份）、#25 现金转换率、#36 惠特曼、#13/#14 PEG/PEGY、#21 德雷曼（重述兜底）。
   **不建议做**：#15 林奇六分类整体评分（定性框架，只保留单点指标）、#30 Akre 三腿凳（三腿窗口不同源，只保留单腿）。#4 防御型七准则可以按 §3.1 的修法做，但**只能出 0–7 分与分位、不能做二元筛选**（2025 年 7/7 全过只有 1 只）。

---

## 8. 待你决定的 3 件事

1. **第 1 波 5 个指标是否开做？** 我的建议：先做 §7 的 6 件前置（估计 1 个人日）+ 第 1 波的 #39/#37/#38（打分型，无历史中位数依赖），#1/#3 次之，#27 等 v47 迁移。
2. **`market_cap_at_report` 的 2013–2020 覆盖率（51%–66%）要不要补？** 补法只有一条：用 `price_daily_raw × 时点股本` 自建市值覆盖全历史（`share_capital_history` 有 5,551 只、244,450 行、1990 起）。不补则所有"10 年历史分位"必须带"偏大盘"标注。
3. **9 个缺失科目列要不要补采？** 优先级：`gross_ppe`/`accumulated_depreciation`（影响 3 个排雷指标的原式）> `restricted_cash`（影响净现金）= `receivables_financing`（影响 2019 前后可比性）> 其余。

---

## 9. 复现

```bash
# 只读探针（会自动等自动更新的写锁释放）
python .planning/2026-11-18-indicator-feasibility-audit/probe_db.py    # 第 1 轮：库存清单 + P0 复核 + 覆盖率
python .planning/2026-11-18-indicator-feasibility-audit/probe_db2.py   # 第 2 轮：争议判定（送转单位/同比口径/ROCE 窗口）
# 第 3、4 轮（标杆量级校验）见 out/db_evidence_round3.md / round4.md

# 逐指标明细与机读证据
docs/evidence/evidence-indicator-audit-matrix-2026-11-18.md
docs/evidence/evidence-indicator-audit-2026-11-18.json
```

| 产物 | 路径 |
|---|---|
| 42 指标 × 科目对照矩阵（人读） | `docs/evidence/evidence-indicator-audit-matrix-2026-11-18.md` |
| 机读证据（42 裁决 + 4 轮探针） | `docs/evidence/evidence-indicator-audit-2026-11-18.json` |
| 代码路径审查全文 | `.planning/2026-11-18-indicator-feasibility-audit/out/code_build_path.md` |
| §9 P0/P1 现状复核全文 | `.planning/2026-11-18-indicator-feasibility-audit/out/p0_status.md` |
| 各流派的汇总与 SQL 探针 | `.planning/2026-11-18-indicator-feasibility-audit/out/<流派>.json`、`sql/<流派>_<编号>.sql` |

---

## 11. 修复落地记录（2026-11-18 第二轮：修 + 派 agent 复审）

> 用户裁决：「能修的都修，修完派 agent 查一遍」。本轮按 `§7` 的 6 件前置逐项落地，
> 每项都有可复跑验收；**只改必要文件，指标本体仍未实现**。

### 11.1 已落地项

| # | 修了什么 | 关键改动 | 验收证据 |
|---|---|---|---|
| 1 | **RG4 恢复绿**：`roce_20y_*` 写进重建 SQL 却没登记 | `roce.py::SNAPSHOT_DERIVED_COLUMNS` 6 → 10 列（补 20y 两列 + 新增 min/cv 两列），与 `SNAPSHOT_UPDATE_SQL` 的 SET 列表**双向一致** | `scripts/check_rework_guards.py` RG1–RG5 全绿（此前 RG4 红） |
| 2 | **#27 的两列补齐**（原结论「库里根本没有」） | `schema.py` v47：`indicator_snapshot.roce_10y_min` / `roce_10y_cv`（DDL + `ALTER` 迁移 + `schema_migrations(47)` + 版本常量 46→47）；CV 只在 `MEDIAN(roce) >= 0.02` 时出值 | 正式库 `init` 到 v47；`compute-roce` 重建后 **min 4,214 / cv 3,730 / 20y 1,404 / eligible 1,406** |
| 3 | **送转单位 bug（X-SZ10）** | `extended.py` 去掉 `SUM(songzhuangu)/10.0` 的 `/10`，注释改写成实测判据 + D53；新增 `test_bonus_share_unit.py`（含反向用例：改回 `/10` 必须红） | 重算 361,393 行后：2025 年报 `bonus_share_ratio_3y` max **0.353 → 3.533** |
| 4 | **金融股判据三份不一致** | 新增 `app/core/sector.py`（单一真相：`is_financial_sql` / `is_non_financial_sql`，NULL 安全 + csrc 兜底），替换 `roce.py`、`supplement.py`、`null_reason_sweeper.py` 三处手写集合 | `test_sector_predicate.py` 6 条；旧判据「同花顺/指南针/大智慧」等 5 只不再被误判为金融股，121 只金融股一个不漏 |
| 5 | **CB-09：能跑不能存** | `engine.py` 保存期校验与运行期共用 `_is_known_field_name()`（`EXTENDED_COLUMNS` 55 列现在可保存） | `test_dsl_extended_fields_savable.py` 58 条（含 55 列参数化 + 反向用例） |
| 6 | **P0-6 历史残留**（本轮新查出来的） | 新增 `scripts/repair_insurance_contract_liabilities_history.py`（dry-run 默认 + 单写者锁 + 改前改后同谓词计数 + 备份），把 5 只险企历史 `contract_liabilities` 置 NULL 并写 `NOT_APPLICABLE` | 执行 `--yes`：**3 行 / 1,411.08 亿 → 0 行**，备份 `.planning/maintenance-backup-p06-contract-liab-history-20261118_172617`；非保险股零误伤 |
| 7 | 两处过期测试夹具 | `test_snapshot_derived_columns.py` 的 `ELIGIBLE_MIN_YEARS > 5` → `== 5`（v46 裁定）；`test_indicator_ext.py` 的送转 fixture `2.0` → `0.2`（每股口径） | 定向回归 **131 passed**；`ruff check app tests/regression` 0 报错 |

> 说明：`indicator_null_reason` 也在本轮重扫了 balance 域（写入 32,734 行原因），
> 所以「合同负债为什么空」现在有 `NOT_APPLICABLE` 可查，不再是无声 NULL。

### 11.2 「market_cap_at_report 覆盖率只有 51%」是口径错，不是数据缺（本条更正 §6）

按 `listing_date` 重算「当时已上市」口径（2013–2020，年报截面）：

| 年份 | 表内行数 | 当时已上市 | pre-IPO/无上市日 | **当时在市且有市值** | 覆盖率（在市口径） |
|---|---|---|---|---|---|
| 2013 | 4,269 | 2,248 | 2,021 | 2,248 | **100.00%** |
| 2014 | 4,599 | 2,365 | 2,234 | 2,365 | **100.00%** |
| 2015 | 4,778 | 2,579 | 2,199 | 2,579 | **100.00%** |
| 2016 | 5,085 | 2,804 | 2,281 | 2,804 | **100.00%** |
| 2017 | 5,444 | 3,235 | 2,209 | 3,235 | **100.00%** |
| 2018 | 5,740 | 3,338 | 2,402 | 3,338 | **100.00%** |
| 2019 | 5,898 | 3,540 | 2,358 | 3,540 | **100.00%** |
| 2020 | 5,953 | 3,972 | 1,981 | 3,934 | **99.04%** |

**pre-IPO / 无上市日 / 已退市的行，市值恒为 NULL——这是正确行为，不是缺数。**
所以：①不需要补历史市值（原计划的"自建市值补 2013–2020"取消，省掉一次全库重算）；
②做「10 年历史分位」时**分母只取当时已上市**，覆盖率天然 100%，不存在"偏大盘偏差"；
③真正要披露的是**样本基数**（当年在市只数），而不是"覆盖率"。

### 11.3 仍未做（数据类，属「做不了/成本过高」，需用户裁决）

| 缺口 | 现状 | 结论 |
|---|---|---|
| 9 类资产负债表科目 | **7 类已补**（schema v48 加列 + 双源抓取脚本，见 §11.4）；**2 类补不了**：`restricted_cash` 受限货币资金、`inventory_provision` 存货跌价准备 | 这两项**只存在于报表附注**：东财 319 列里 `RESTRICT/DEPREC/PROVISION` 零命中，新浪 147 列缺「存货跌价准备」，akshare 全包 grep「受限货币资金」0 文件。**不做 PDF 抠数**（D46 已裁决禁止 PDF 解析） |
| GDP / CPI | ✅ 已补（`macro_series` 844 行） | akshare 免费宏观接口 |
| 信用利差 | ✅ **AAA 与 AA 两档都已落地** | AAA：`bond_yield_curve` 106,147 行（国债/商金债AAA/中短票AAA）。**AA/AA+ 于 2026-11-18 补齐（终验推翻了「拿不到 AA」）**：`ak.bond_index_general_cbond(「市场隐含评级AA信用债指数」,「平均市值法到期收益率」,「3-5年」)` 能取到 2013-01-04 起 3,468 天日频（AA+ 与 1-3Y 段同样可取得）；已落 `bond_yield_curve` **10,404 行**（3 条指数序列）。AA(3-5Y) − 国债(3Y,5Y 均值) 利差：最新 **77.46bp**、近一年中位 **69.2bp**、全历史中位 185.68bp |
| beta | ✅ 已自建（`stock_beta` 10,799 行） | 但基准是「全市场市值加权代理」而非沪深300（000300 只有月末点），近 3 年 β=1.045/r²=0.870，详见 §11.4 |
| 一致预期 | ⚠️ **只能从 2026-11-18 起打点**（已落 9,337 行/2,360 只） | 源接口只给当期快照、无历史；已接进自动更新每天一次 |
| 机构持仓 | ✅ **已落地（schema v49）** | 实现路径：**绕过 akshare 的错误列包装**，直接读东财底层 `dataapi/zlsj/list`（全市场 × 1 报告期 = 1 次取全，11 页/6 秒），6 类持有人（基金/QFII/社保/券商/保险/信托）、**2005-06-30 起 21 年**。实测东财返回 22 字段而 akshare 按 23 列名套用 → 从第 3 列起整体左移并丢弃 9 个字段（含 `FREESHARES_RATIO`），这就是「列错位」的真因。正式库已回补 **432,343 行 / 79 个报告期 / 5,666 只**（2005-06-30 → 2026-09-30），失败 0 |
| 变现折扣 / 跨市场估值 | ❌ 无源 | 需要人工资产折价假设 / 海外市场行情，均不在免费可得范围 |

### 11.4 数据补齐（第二批，2026-11-18 当天完成）

| 数据 | 落点 | 实测 | 验收 |
|---|---|---|---|
| 7 个资产负债表科目 | `balance_sheet` 新增 7 列（schema v48） | **全市场已回补**：新浪档 5,577 只 / 0 失败 / 298,243 行（89 分钟）+ 东财补漏；覆盖 `fixed_assets_gross` 162,418 行（年报口径 86,737/105,827 = **82.0%**）、`other_current_assets` 238,191、`investment_property` 131,741、`receivables_financing` 95,892、`long_term_payables` 66,935、`development_expenditure` 54,339；幂等（两遍指纹逐位相同）、原值<净值 0 行 | ⚠️ **勾稽分布（终验实测，取代原先「残差 3.8e-06」的单例结论）**：16.07 万行里**精确闭合仅 55.0%（88,401 行）**、库内更小 44.4%（71,372 行）、库内更大 0.55%（888 行）、原值≤折旧 9 行。说明**两源口径不同**（新浪的「原值/累计折旧」覆盖范围比库内 `fixed_assets` 更宽），所以**禁止用 `原值 − 折旧` 反推 `fixed_assets`**；600519 只是精确闭合的个例 |
| GDP / CPI | `macro_series` 844 行（2006-03-31 → 2026-10-31） | GDP 83 期（亿元）+ 同比；CPI 226 期（同比/环比/指数） | 226 期恒等式 `cpi_index − 100 = cpi_yoy` 全部成立；2025Q4 GDP 140.19 万亿 |
| 中债收益率曲线 | `bond_yield_curve` 106,147 行（2007-01-04 → 2026-11-18） | 3 条曲线 × 8 期限，240 次请求 / 0 失败 / 517s | 国债 10Y 与库内 `treasury_yield_curve` 同日期 4,966 天，**\|差\| 中位 0.0bp / 最大 0.01bp**；AAA(商金债)−国债 10Y 近 1 年中位 **28.32bp** |
| 个股 beta | `stock_beta` 10,799 行（250/500 日 × 5,447/5,352 只） | **零外部请求**、6.7s 完成 | 5 只交叉验证 `max\|Δβ\|=0`。**但基准是 `CAPW_MARKET` 代理**：`000300` 的 `index_close` 只有 265 个月末点（密度 5.1%），不足以做日频回归；该代理近 36 个月对沪深300 β=1.045 / r²=0.870。β 中位 1.335（全市场池 vs 大市值基准，结构使然） |
| 机构持仓 | `institution_holdings` 432,343 行 / 79 期 / 5,666 只（schema v49） | 6 类持有人；`ratio_of_float` 为百分数 | 试点 30 只幂等（180→180 行）；全量 0 失败 |
| 分析师一致预期 | `analyst_forecast_snapshot` 9,337 行 / 2,360 只（2026-11-18 首日） | 每日快照 + 已接进自动更新（`update.py` +29 行 / `STEP_ORDER` +1 步 / 7 条回归用例） | 同日重跑幂等（`deleted=9337 / written=9337`）；源端 2,802 行里 **442 只重复**（载荷逐字相同）→ 真实覆盖 **42.3%**，不是探源时报的 50.3% |

> **三条口径更正（都写进了知识库）**：
> ① `index_valuation` 的 `index_close` 96% 是**申万行业指数日频**，`000300` 只有月末——「有 12 万行指数点位」不等于「有沪深300 日线」；
> ② CPI 的「全国-当月」是**上年同月=100 的指数**（100.7），不是百分数——直接当百分数用会差 100 倍；
> ③ 一致预期的源接口有 **442 只重复行**，按行数算覆盖率会虚高。

### 11.5 第二轮对抗性验收抓到的 3 个反证与修法（当天修完）

| # | 反证 | 实测 | 修法 |
|---|---|---|---|
| 1 | **补采脚本第一版写入了脏值** | 原值 − 净值 < −1 元的 **17 行**（最差 600036 2023-06-30 差 **−840.32 亿**：原值 536.76 亿 < 净值 1,060.71 亿，说明银行模板的「原值」与通用模板不同口径） | ①脚本加**合理性闸门**（`原值 < 净值 − 1` 整组丢弃并计数）；②新增 `scripts/repair_bs_subjects_implausible.py` 清理存量：**15 行**两列置 NULL（dry-run 15 → 修后 0），并**先把受影响行（含 7 个补采列）导出 Parquet 备份**再改（第二轮验收指出旧版备份缺列、无法回滚） |
| 2 | **「推导累计折旧」是伪值** | 银行/保险模板没有「累计折旧」列时用 `原值 − 净值` 推导；但那个差里混着**固定资产减值准备**（库内无此科目），2,515/4,403 行勾稽不闭合 | **取消推导**（宁缺不填，模板没有就 NULL + `NOT_APPLICABLE`）；`schema.py` 的列注释改为正式恒等式 `原值 − 累计折旧 − 减值准备 = 净值`。⚠️ **范围更正（第三轮复核）**：清理只应针对**银行**——券商模板有真实的「累计折旧」列（实测 667 行满足 `原值 − 折旧 − 净值 ≈ 0`，无减值时精确闭合），第一版清理把非银金融也清了（误删真数据），第二轮回填已把券商值重新写回；repair 脚本已改为 `sw_level1 = 银行` |
| 3 | **`stock_beta` 有 122 行 β 越出 0–3 且无标注** | 多为 r² 极低的估计 | 按 R4「不截断不置空」保留原值，但 **r² < 0.05 时把 `confidence` 降级为 `approximate`**，让下游知道该行不可当强证据 |

另修：`STEP_LABELS` 补 `analyst_forecast`（之前状态页会显示英文键名）；`compute_stock_beta.py:754` 的 F541 空 f-string。

> **已知来源限制（终验实测）**：北交所**老代码**（`43x`/`83x`/`87x`，245 只 / 6,634 行）新浪不发这 7 列（各种前缀都试过），这批占全表 1.8%，**属免费源限制、不是脚本 bug**；北交所**新代码 `92x`** 已覆盖（3,767 行有原值）。
>
> **验收员判「通过」的 3 项**（可直接作为结论）：schema v48 与实库逐列一致且 `init` 幂等；国债 10Y 与库内曲线 **4,966 天 |差| 中位 0.0bp / 最大 0.01bp**；beta 5 只 × 2 窗口独立重建 **max|Δβ| = 0.0**；一致预期现抓复现 2,360 只 × 12 格逐位相同；`cpi_index − 100 = cpi_yoy` 226/226 成立。

### 11.6 本轮未完成 / 正在后台跑

| 项 | 状态 | 说明 |
|---|---|---|
| 资产负债表 7 列**全市场**回填 | 🔄 后台运行中（`bs_full_sina2.log`） | 新浪档 ≈1.9h（5,580 只，1.2s/只，`--resume` 每 50 只落盘）；跑完后用东财补漏档只补「5 科目缺失」的那 ~900–1,000 只（≈50–60 分钟）。当前已落 16,448 行原值 |
| `indicator_null_reason` 对补采列的登记 | 待做 | 补采脚本本身不写原因；需在回填完成后按「模板无此列 / 准则前无此科目 / 真 0」三类扫一遍 balance 域 |
| 一致预期消费方（#13 PEG 的预期增速） | ✅ 已铺路 | 新增 `analyst_forecast.forecast_as_of(stock_code, as_of)`：只取 `snapshot_date <= as_of` 的最近一期，超过 `max_age_days` 按缺失处理（防前视）；`test_analyst_forecast_snapshot.py` 新增「不许用未来快照」的正反用例 |
| 四张新表的资产去向登记（D38） | ✅ 已登记 | `config/asset_reference.yaml` 增加 `macro_series`/`bond_yield_curve`/`stock_beta`/`analyst_forecast_snapshot` 四条零引用条目，各自写清首个消费方与「接入后删除本条目」；`vd data doctor` 的 `ZERO_REFERENCE_ASSET` 会在被读取时报「清单已过期」 |
| 全量回填的自动接力 | ✅ 已就绪 | `.planning/.../watch_and_gapfill.py`（psutil 轮询）在新浪档结束后自动跑东财补漏档，串行避免双写 |
| 机构持仓 | ❌ 判定不可用 | `stock_report_fund_hold` 列错位、`stock_institute_hold` 2025 起返回 0 行 |
| AA 信用利差 / 受限货币资金 / 存货跌价准备 / 变现折扣 / 跨市场估值 | ❌ 免费源拿不到 | 见 §11.3 |

### 11.7 前置就绪自检（把 6 件前置变成可复跑断言）

新增 `scripts/check_indicator_readiness.py`（只读、`--json` 可接状态页/CI）：把 §7 的 6 件前置
逐条变成断言，每条都输出**真实读数**而不是「OK」：

| key | 断言 | 当前实测 |
|---|---|---|
| `sector_single_source` | 3 处调用都走 `app/core/sector.py`，且**代码里**（去掉注释后）不再有手写的 `('银行','非银金融')` 集合 | 3/3 收口 |
| `denominator_status` | `indicator_null_reason` 至少 4 类原因码在用（6 类已在枚举里定义） | 130,406 行 / 70 字段 / 5 类在用 |
| `roce_persistence` | `roce_10y_min`/`roce_10y_cv`/`roce_20y_median` 存在、有值、且都进 `SNAPSHOT_DERIVED_COLUMNS` | min 4,214 / cv 3,730 / 20y 1,404，登记 10 列 |
| `yoy_split` | `*_yoy`(TTM) 与 `*_yoy_fy`(年报) 两套并存且 FY 列已回填 | 各 3 列；`revenue_yoy` 5,537 / `_fy` 5,531 |
| `derived_registration` | 重建 SQL 的 SET 列与派生域登记**双向差集为空**（RG4 的运行时版） | 10 / 10，差集为空 |
| `data_domains` | 4 张新表存在 + 资产负债表补采科目已回填 | macro 844 / bond 106,147 / beta 10,799 / forecast 9,337；补采 21,153/361,484 行（回填进行中） |

**当前整体结论不是 READY**（终验实测）：`data_domains` 因回填尚未完成（约 20%）而 **WARN**，其余 5 项 READY —— 这是**如实状态**，回填完成后会自动转 READY，无需改脚本。
>
> 运行方式（脚本走路径隔离，裸跑会直接 BLOCK，这是**有意**的 fail-closed）：
> ```bash
> export VD_ENV=formal VD_FORMAL_ACK=confirmed \n>        VD_DUCKDB_PATH="<$PWD>/data/valuedashboard.duckdb" \n>        VD_SQLITE_PATH="<$PWD>/data/valuedashboard.sqlite"
> uv run --locked python scripts/check_indicator_readiness.py            # 人读
> uv run --locked python scripts/check_indicator_readiness.py --json     # 给 CI/状态页
> ```
> 退出码：0 READY / 1 BLOCK / 2 WARN。

### 11.8 补采科目的「登记链」与一个新发现的口径坑

**登记链**（这正是 §5.1 说的「16 张注册表、15 张手工」最典型的一例）：
新加 7 个补采列后，要让它们「能被筛选/能有空值原因」，必须**同时**改三处：

1. `app/core/screening/engine.py::STATEMENT_FIELDS`（白名单，否则筛选看不见、`domain_fields()` 不认）；
2. `app/core/dsl/ast_nodes.py::FIELD_METADATA`（`unit/period_type/historical_capable`，否则有测试直接红）；
3. 空值原因规则（`null_reason_sweeper.BALANCE_SUBJECT_NOT_APPLICABLE`，否则这些 NULL 全是无声的）。

三处都已补：7 列进白名单与 DSL 元数据（`unit=CNY / point_in_time / historical_capable=True`）；
`fixed_assets_gross` 与 `accumulated_depreciation` 对**银行**登记 `NOT_APPLICABLE`（实测 42 行）；
其余 5 列在回填完成前由覆盖率规则记 `NOT_DISPATCHED`，回填后会自动转为 `SOURCE_EMPTY`/`NOT_APPLICABLE`。

**新发现的口径坑（写进 KB D58）**：`balance_sheet.fixed_assets` 并不总是「纯 PPE 净值」——
实测 15 行（全是非金融，如 000722 FY2025：原值 28.11 亿 / 折旧 13.71 亿 / 库内净值 41.38 亿）
出现 `原值 − 折旧 < 净值`，说明该股的 `fixed_assets` 含了 PPE 以外的东西（或在建工程/土地等）。
**但这 15 行的成因并不唯一**：既有「库内 `fixed_assets` 是宽口径」的迹象（000722、002050、002108 等：`原值 − 折旧` 明显小于库内净值），也有「源端两列错位」的迹象（000036 的 `原值 == 累计折旧`）。**所以处置统一为：这两列对这 15 行一律置 NULL（无法闭合就不能用），并在指标层把这类股票排除出「用原值」的样本**；不要用它们反推 `fixed_assets` 的语义。

## 10. 一句话

> 这批指标的**字段是真的、口径是脏的、工程是漏的**：没有幻觉列，但有 11 处"列名对、语义错"；
> 42 个都能算，但没有一个能直接上线；9 条 P0 都已修好、真正卡人的是**覆盖率**与**注册不全**。
> 开工前先把"金融股剔除 / 分母 status / 三列位 / 派生域注册"这四件事变成机械动作，
> 否则下一个 ROCE 只是换了个指标名。
