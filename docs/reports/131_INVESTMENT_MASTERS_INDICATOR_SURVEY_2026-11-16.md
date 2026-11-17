---
title: 投资大师复合指标调研报告（42 个指标 × 数据可行性 × 独立审查）
status: approved
category: reports
created: 2026-11-16
last-reviewed: 2026-11-16
---

# 投资大师复合指标调研报告

> **目标**：把投资史上宗师级人物留下的、能**超脱市盈率/市净率**的可计算复合指标系统盘一遍，逐个判定能否严丝合缝落到本库字段上。
>
> **流程**：7 个流派调查 Agent（并行）→ 7 个独立审查 Agent（并行、逐条证伪）→ 本报告。
>
> **边界**：全程只读（DuckDB `read_only=True`），未写库、未改 schema、**未实现任何指标**。做哪几个由用户批准。
>
> **可复现**：调查与审查的原始 JSON 在 `.planning/2026-11-16-masters-survey/`（`schools/` 7 份、`reviews/` 7 份、`dbq.py` 只读查库工具）。

---

## 0. 一页结论

| 项 | 结果 |
|---|---|
| 覆盖流派 / 大师 | **7 个流派、30+ 位**：格雷厄姆、Dodd、施洛斯、Kahn、Tweedy Browne、Brandes、巴菲特、芒格、Simpson、Ruane、费雪、T. Rowe Price、林奇、欧奈尔、格林布拉特、聂夫、德雷曼、欧香纳西、Fama & French、Asness、Terry Smith、Akre、Sleep、Rochon、Gayner、克拉曼、邓普顿、Eveillard、马克斯、惠特曼、Altman、Piotroski、Beneish、Ohlson、Montier |
| 筛出的可计算指标 | **42 个**（标注 high 优先级 31 个） |
| 审查裁决 | ✅ 直接通过 **3**　🟡 需修正后可用 **36**　❌ 不通过 **3** |
| 存在「ROCE 同型」风险 | **13 / 42**（分母可趋零或为负、单位量纲错配、把广义口径标成 exact） |

**三句话结论**：

1. **字段层面是干净的**：7 个调查员共引用 250+ 个 `table.column`，审查员逐个对照 `information_schema`，**没有发现任何凭空编造的字段**。大师指标在数据上基本都「有米下锅」。
2. **口径层面不干净**：13 个指标犯了和 ROCE 同一类的错误——把「清算价值」除以「总市值」、用 `nullif(x,0)` 挡零不挡负、把 `总资产−流动负债` 说成格林布拉特的「净营运资本+净固定资产」、把 2001 年才有的序列宣称成 2001–2025。这些错误**都不会报错，只会静默产出垃圾数值**。
3. **真正能原样复刻的只有带明确打分表或绝对阈值公式的那一类**（Piotroski F、Altman Z、Graham Number、E/P−国债），而「软结构类」（护城河、管理层、内生增长）必须降级为代理变量并明确标注。

---

## 1. 调研与审查方法

### 1.1 两轮制

| 轮次 | 参与者 | 任务 | 产出 |
|---|---|---|---|
| 第一轮 | 7 个「投资指标考古员」 | 各负责一个流派，先读数据库字段基线 `context.md`（含覆盖率实测与已知陷阱），再逐人过一遍大师，挑 3–6 个确实能算的 | `.planning/.../schools/<流派>.json` |
| 第二轮 | 7 个「对抗性审查员」 | 逐条证伪：字段是否真存在、`formula_sql` 能否跑通、`match` 分级是否诚实、样本量是否编造、陷阱是否属实、有无 ROCE 同型风险 | `.planning/.../reviews/<流派>.json` |

第一轮就明确告知了数据库的 **8 条已知陷阱**（`fixed_assets` 是 PPE 不是长期资产大区、`financial_expenses` 是净额、`interest_expense` 2016 年只有 12.6% 覆盖、`exchange_gain` 其实是公允价值变动、`cash_flow_activity.dividend_interest_paid` 2025 年全空、金融股无流动/非流动划分且无财务费用行、大量 NULL 属正常、`csrc_l1` 行业标签脏）。

第二轮给审查员的开工提示里，直接把我们做 ROCE 的事故写成反面教材：

> 「我们做 ROCE 时把『固定资产净值』直译成 `balance_sheet.fixed_assets`（其实那是 PPE），结果 219 只股票分母为负、区间 −8311%~+21285%。教训：加总类公式必须先验证会计恒等式；比率出现荒谬量级时先怀疑分母口径，而不是加护栏。」

### 1.2 裁决定义

| 裁决 | 含义 |
|---|---|
| ✅ 通过 | 字段全真、SQL 可跑、`match` 分级诚实、无 ROCE 同型风险 |
| 🟡 需修正 | 方向成立，但有 P1 级问题（口径 / 护栏 / 样本 / 剔除规则），改完可用 |
| ❌ 不通过 | 存在结构性缺陷，按现状无法交付 |

---

## 2. 总表

> 优先级 = 调查员对该指标「资本效率信息量」的判断；审查 = 独立审查员的最终裁决。

| # | 流派 | 指标 | 提出者 | 优先级 | 可算 | 审查 | ROCE同型 |
|---|---|---|---|---|---|---|---|
| 1 | 格雷厄姆学派 | 格雷厄姆数（合理价格上限） | Benjamin Graham | high | yes | 🟡 需修正 | 低 |
| 2 | 格雷厄姆学派 | 净流动资产价值（净净股）NCAV 与净现金折价 | Benjamin Graham | high | yes | 🟡 需修正 | 低 |
| 3 | 格雷厄姆学派 | 盈利收益率与 10 年期国债利差（E/P − 无风险利率） | Benjamin Graham | high | yes | ✅ 通过 | 低 |
| 4 | 格雷厄姆学派 | 防御型投资者七准则合规分（0-7） | Benjamin Graham | high | yes | ❌ 不通过 | **高** |
| 5 | 格雷厄姆学派 | 格雷厄姆成长股价值公式 V = EPS × (8.5 + 2g)（含利率重标定版） | Benjamin Graham | medium | yes | 🟡 需修正 | 低 |
| 6 | 格雷厄姆学派 | 施洛斯式深度价值四因子（低 PB + 低杠杆 + 长期盈利 + 多年低位） | Walter Schloss（与 Tweedy Browne《What Has Worked in Investing》同源） | high | yes | 🟡 需修正 | 低 |
| 7 | 巴菲特-芒格学派 | 股东盈余与股东盈余收益率 | Warren Buffett | high | yes | 🟡 需修正 | **高** |
| 8 | 巴菲特-芒格学派 | 巴菲特指标（证券化率）与 A 股可执行适配版 | Warren Buffett | medium | partial | 🟡 需修正 | 低 |
| 9 | 巴菲特-芒格学派 | 十年中位数 ROE ≥ 15% 且真正低负债 | Warren Buffett, Charlie Munger | high | yes | 🟡 需修正 | 低 |
| 10 | 巴菲特-芒格学派 | 毛利率十年稳定性（定价权代理） | Charlie Munger | high | yes | 🟡 需修正 | 低 |
| 11 | 巴菲特-芒格学派 | 一美元测试 | Warren Buffett | high | yes | 🟡 需修正 | **高** |
| 12 | 巴菲特-芒格学派 | 留存资本化率（经营口径增量资本回报） | Charlie Munger, Lou Simpson, Bill Ruane | high | yes | 🟡 需修正 | **高** |
| 13 | 成长股学派 | PEG（市盈率 / 盈利增速比） | Peter Lynch | high | yes | 🟡 需修正 | **高** |
| 14 | 成长股学派 | PEGY（股息调整后的 PEG） | Peter Lynch | medium | yes | 🟡 需修正 | **高** |
| 15 | 成长股学派 | 林奇六类股票分类器（含周期股标记） | Peter Lynch | medium | partial | 🟡 需修正 | **高** |
| 16 | 成长股学派 | 研发强度与销售组织效率（费雪十五要点中可算的部分） | Philip Fisher | medium | partial | ✅ 通过 | 低 |
| 17 | 成长股学派 | CANSLIM 盈利引擎（C 季度加速 + A 年度复利） | William O'Neil | high | yes | 🟡 需修正 | **高** |
| 18 | 成长股学派 | 相对强度 RS 评级（含 52 周高点距离） | William O'Neil | high | yes | 🟡 需修正 | 低 |
| 19 | 量化价值 / 神奇公式流派 | 神奇公式（资本回报率 × 盈利收益率 双排名合成） | Joel Greenblatt | high | yes | 🟡 需修正 | 低 |
| 20 | 量化价值 / 神奇公式流派 | 聂夫总回报率（低 PE + 高股息 + 盈利增长） | John Neff | high | yes | 🟡 需修正 | 低 |
| 21 | 量化价值 / 神奇公式流派 | 德雷曼逆向价值复合（低 PE + 低 PB + 低 P/CF，叠加行业折价） | David Dreman | medium | yes | ❌ 不通过 | **高** |
| 22 | 量化价值 / 神奇公式流派 | 趋势价值（价值前 10% ∩ 6 个月动量最强） | James O'Shaughnessy | high | yes | 🟡 需修正 | 低 |
| 23 | 量化价值 / 神奇公式流派 | Fama-French 五因子单股暴露输入（价值 / 规模 / 盈利 / 投资） | Eugene Fama & Kenneth French | medium | yes | 🟡 需修正 | 低 |
| 24 | 量化价值 / 神奇公式流派 | 质量减垃圾（QMJ）综合 z 分 | Cliff Asness | high | yes | 🟡 需修正 | **高** |
| 25 | 质量 / 复利机器流派 | 现金转换率（自由现金流 / 净利润） | Terry Smith（Fundsmith） | high | yes | 🟡 需修正 | **高** |
| 26 | 质量 / 复利机器流派 | 毛利率与费用率双稳态（含「规模效应共享」检验） | Terry Smith（Fundsmith）+ Nick Sleep（Nomad Capital） | high | yes | 🟡 需修正 | 低 |
| 27 | 质量 / 复利机器流派 | ROCE 持久性（10 年中位数 + 最低值 + 变异系数） | Terry Smith（Fundsmith）+ François Rochon（Giverny Capital） | high | yes | 🟡 需修正 | 低 |
| 28 | 质量 / 复利机器流派 | 增量资本回报率（5 年，双口径） | Chuck Akre + Tom Gayner（Markel） | high | yes | 🟡 需修正 | 低 |
| 29 | 质量 / 复利机器流派 | 再投资率 × 增量 ROIC = 内生增长率 | Chuck Akre + Nick Sleep | medium | yes | 🟡 需修正 | 低 |
| 30 | 质量 / 复利机器流派 | Akre 三腿凳质量复利评分 | Chuck Akre（三腿凳）+ Tom Gayner（股本不稀释腿） | medium | partial | 🟡 需修正 | 低 |
| 31 | 风险 / 周期 / 安全边际流派 | 净流动资产折价率（Net-Net 折价） | Seth Klarman | high | yes | ✅ 通过 | 低 |
| 32 | 风险 / 周期 / 安全边际流派 | 折价清算价值/市值（安全边际折价率） | Seth Klarman | medium | partial | 🟡 需修正 | 低 |
| 33 | 风险 / 周期 / 安全边际流派 | 扣非低 PE 分位 + 预期总回报率（双闸门） | John Templeton | high | yes | ❌ 不通过 | **高** |
| 34 | 风险 / 周期 / 安全边际流派 | 个股周期调整市盈率（10 年正常化盈利） | Jean-Marie Eveillard | high | yes | 🟡 需修正 | 低 |
| 35 | 风险 / 周期 / 安全边际流派 | 市场温度计（全 A 横截面聚合，市场级而非个股） | Howard Marks | high | partial | 🟡 需修正 | 低 |
| 36 | 风险 / 周期 / 安全边际流派 | 资产负债表弹性（净营运资本弹性 + 净现金占比） | Marty Whitman | high | yes | 🟡 需修正 | **高** |
| 37 | 财务排雷 / 多因子评分 | Altman Z 值（制造业经典 5 变量破产预警） | Edward Altman | high | yes | 🟡 需修正 | 低 |
| 38 | 财务排雷 / 多因子评分 | Altman Z'' 值（非制造业/新兴市场版，账面权益替代市值） | Edward Altman | high | yes | 🟡 需修正 | 低 |
| 39 | 财务排雷 / 多因子评分 | Piotroski F 分数（9 项 0/1 基本面改善评分） | Joseph Piotroski | high | yes | 🟡 需修正 | 低 |
| 40 | 财务排雷 / 多因子评分 | Beneish M 分数（8 变量财务舞弊识别） | Messod Beneish | high | partial | 🟡 需修正 | 低 |
| 41 | 财务排雷 / 多因子评分 | Ohlson O 分数（9 变量破产概率 Logit 模型） | James Ohlson | high | yes | 🟡 需修正 | 低 |
| 42 | 财务排雷 / 多因子评分 | Montier C 分数（6 项造假信号计数） | James Montier | medium | partial | 🟡 需修正 | 低 |

---
## 3. 分流派详解

每个指标给出：提出者 / 公式 / 含义 / 为什么超出 PE·PB / 字段映射 / 历史可用 / 主要陷阱 / **审查发现**。

### 3.1 格雷厄姆学派（防御型价值 / 深度价值）

**覆盖大师**：Benjamin Graham、David Dodd、Walter Schloss、Irving Kahn、Christopher Browne / Tweedy Browne、Charles Brandes

**本流派审查总评**：对 schools/graham.json 的 6 个指标逐条做了字段存在性、match 诚实性、SQL 可执行性、样本量、traps、ROCE 式口径风险与历史可用性复核。总结论 pass_with_fixes：零伪造字段（51 处限定引用全部命中 information_schema），6 段 formula_sql 全部可执行且返回非空、量级合理，没有重蹈 ROCE 的负分母覆辙（NCAV 的 total_current_assets+total_non_current_assets=total_assets 恒等式 2025 年 5520/5520 通过，guarded 后的 price/NCAV 最小值 +0.238）。最值得肯定的是「实测」数字命中率极高：格雷厄姆数的 2000=797/2003=947/2010=1721/2025=3956 与过关只数 2007=3/2022=594/2025=344、E/P 利差的 2007=17/2015=93/2020=592/2025=1163 及四个分母 1354/2579/3934/5447、NCAV 的 2003=1055/2010=1808/2025=5347 与 net-net 2025=3 只/中位 7.59、成长公式的 1999=54/2007=939/2016=1996/2025=4567、施洛斯的 5206…

#### 1. 格雷厄姆数（合理价格上限）（Graham Number）

- **提出者**：Benjamin Graham
- **公式**：`GrahamNumber = SQRT(22.5 × EPS × BVPS)，等价于 P/E × P/B ≤ 22.5。EPS 取年报归母基本每股收益（缺失时用归母净利润/期末总股本派生），BVPS 取期末归母每股净资产；买入阈值 price ≤ GrahamNumber。`
- **含义**：把「低市盈率」和「低市净率」两条互斥的尺子合并成一个绝对价格上限：只有在 P/E 和 P/B 的乘积不超过 22.5 时，股价才低于这个数。它给的不是便宜度评分，而是一个可以直接和股价比较的「贵了别买」红线，隐含 15 倍 PE 上限乘以 1.5 倍 PB 上限的格雷厄姆原始双约束。
- **为什么超出 PE/PB**：PE 和 PB 单看都会被行业属性骗：高 ROE 公司 PB 天然高、周期股低谷时 PE 天然高。Graham Number 把两者相乘，只有在「盈利便宜且资产也便宜」时才低，能同时排除高 ROE 高溢价股和周期顶部低 PE 股。且输出的是元/股的价格阈值，可直接和现价做比值，形成连续的深度折价排序。
- **字段映射**：exact 4 / approximate 1 / missing 0
  - ✔ `income_statement.basic_eps` ← EPS（基本每股收益）：精确语义，但只有 2007 年起稳定：2025 年 5625/5625、2020 年 5878/5953（98.7%）、2006 年 1521/1961（77.6%）、2005 年仅 166/1794（9.3%）、2003 年仅 7/1621（0.4%）。2007 年前不可用。
  - △ `income_statement.parent_net_profit ÷ share_capital_history.total_shares` ← EPS 兜底（归母净利润/期末股本）：实测 2025 年报两者中位绝对差 0.0025 元（EPS 中位 0.39 元），偏差来自「加权平均股本 vs 期末股本」。股本取 effective_date ≤ report_date 的最近一条。这是 2007 年前唯一的 EPS 口径。
  - ✔ `indicator_ext.bps_parent` ← BVPS（每股净资产）：实测 = 归母权益/总股本；2025 年报 5512/5625 非空。注意同表还有 indicator_ext.bps（全部权益口径，含少数股东权益），格雷厄姆必须用 parent 口径，否则银行/地产会被高估。
  - ✔ `indicator_ext.report_date_close` ← 价格：未复权收盘价，取 report_date 当日或之前最近交易日（实测 2024-12-31 与 price_daily_raw 当日收盘 5334/5334 完全一致；2016-12-31 为周六时正确回退到 12-30）。但非空数远小于财报行数（2015 年 2577/4778），缺的是当时尚未上…
  - ✔ `indicator_ext.market_cap_at_report / indicator_ext.book_to_market` ← 总市值 / PB 辅助：book_to_market 实测 = total_equity_parent / market_cap_at_report（000651 逐 4 位小数对齐），故 PB = 1/book_to_market，均为归母口径，与 Graham Number 的 BVPS 口径自洽。
- **历史可用**：可算点 2000-2025 共 26 个年报截面：2000 年 797 只 → 2003 年 947 只 → 2010 年 1721 只 → 2025 年 3956 只（2025 年 5447 只有价格的样本里 3956 只 EPS/BVPS 同为正）。因 basic_eps 覆盖率在 2007 年才跳到 93%，真正可比的历史起点应定在 2007（19 年），并可用低一级的派生 EPS 口径把 2000-2006 补成 approxi…
- **主要陷阱**：① BVPS 必须用 bps_parent，用 indicator_ext.bps（含少数股东权益）会让少数股东占比高的公司 BVPS 虚高、GrahamNumber 虚高，凭空多出一批「便宜股」；② 亏损股 EPS<0 时 SQRT 无定义，不能用 abs() 强行开方——实测某版本加了 abs() 后，巨亏公司反而得到巨大的 GrahamNumber，出现「越亏越便宜」的倒挂，必须 CASE WHEN eps>0 AND bvps>0 置 NULL；③ report_date_close 的 NULL 不是随机缺失而是「当时还没上市」（财报被回溯补录：2015 年报 income_stat…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：①把 data_mapping 里 bps_parent 的非空数由 5512/5625 改为实测 5475/5625（5512 是 bps 的数）；②EPS 中位数由 0.39 改为 0.23；③把「中位 P/GN 常年 2.0-2.5」改为「2000-2025 逐年中位区间 1.45-4.68、中位约 2.3，仅 8/26 年落在 2.0-2.5」；④formula_sql 已正确，建议按 traps ⑤ 补一列单独输出 P/E<=15 与 P/B<=1.5 的交叉校验（现只在文字里提出、未在 SQL 实现）。

#### 2. 净流动资产价值（净净股）NCAV 与净现金折价（Net Current Asset Value (NCAV / Net-Net) & Net Cash Discount）

- **提出者**：Benjamin Graham / David Dodd / Irving Kahn
- **公式**：`NCAV = 流动资产合计 − 负债合计（A 股无优先股；归母口径再减少数股东权益）；NCAVPS = NCAV ÷ 期末总股本；买入阈值 price ≤ 2/3 × NCAVPS。变体一 NNWC = 货币资金 + 交易性金融资产 + 0.75×(应收票据+应收账款) + 0.5×存货 − 负债合计（清算折扣加权）；变体二 净现金 = 货币资金 + 交易性金融资产 − 负债合计（Kahn 式，市值低于净现金）。`
- **含义**：不看盈利、只看清算视角的资产：把全部流动资产变现、还清全部负债后，剩下的钱除以股本，就是每股能分到的「白送资产」。股价低于其中三分之二时买入，本质是让市场替投资者付运营费和商誉。它是格雷厄姆-多德《证券分析》里最硬的资产价值法，也是 Kahn 兄弟净现金折价的同源版本。
- **为什么超出 PE/PB**：PE/PB 都以「持续经营」为假设，NCAV 以「清算」为假设，两者打分体系不重合：一家亏损但有大量现金和存货的公司 PE 无效、PB 也可能高（若有商誉），却可能是极深的 NCAV 折价股。它天然排除了商誉、无形资产、长期股权投资这些最容易虚高的科目，只看能变现的流动资产。
- **字段映射**：exact 7 / approximate 2 / missing 0
  - ✔ `balance_sheet.total_current_assets` ← 流动资产合计：年报覆盖率约 98%（2025 年 5525/5625、2020 年 5854/5953）；2025 年恰好 100 只为空，即金融股（无流动/非流动划分）。
  - ✔ `balance_sheet.total_liabilities` ← 负债合计：2025 年报 5625/5625 全非空，可直接与流动资产相减。
  - ✔ `share_capital_history.total_shares` ← 总股本：以 effective_date ≤ report_date 的最近一条还原当期股本（实测 000001 于 2025-12-31 为 19,405,918,198 股，与总市值/收盘价反推一致）。覆盖 5551/6284 只，2003 年只有 1101/1621（68%），2007 年后 >96%…
  - ✔ `balance_sheet.monetary_funds` ← 货币资金：2025 年报 5578/5625。注意含受限资金（保证金、冻结存款），严格清算口径应扣减，本库无受限资金字段 → 净现金口径系统性偏乐观。
  - △ `balance_sheet.trading_financial_assets` ← 交易性金融资产：2019 年新金融工具准则后才有：2020 年 3124/5953、2016 年 1000/5085；2019 年前对应科目（以公允价值计量的金融资产）基本全空，故净现金/NNWC 变体只能从 2019 年起算。
  - △ `balance_sheet.notes_receivable + balance_sheet.accounts_receivable` ← 应收票据 + 应收账款：2019 年新准则期间票据与账款曾合并披露「应收票据及应收账款」，拆分后 2025 年 notes_receivable 非空 4375/5625。NULL 语义多为「本就没有票据」，可 COALESCE 0，但 2019 前后口径有断点。
  - ✔ `balance_sheet.inventory` ← 存货：2025 年报 5470/5625。清算折扣取 50% 属格雷厄姆惯例，不是数据问题而是口径约定，需在结果里标注。
  - ✔ `balance_sheet.minority_interest` ← 少数股东权益：2025 年报 4513/5625 非空，NULL 即无少数股东，COALESCE 0 安全。仅归母口径 NCAV 需要用到。
  - ✔ `indicator_ext.report_date_close` ← 价格：见格雷厄姆数条目；同样必须 INNER JOIN 有价格的行以剔除回溯补录的未上市样本。
- **历史可用**：2003-2025 每年都能算（2003 年 1055 只 → 2010 年 1808 只 → 2025 年 5347 只），但受股本链覆盖限制，2003-2006 年只有 68% 的样本有期末股本，真正干净的是 2008-2025（18 年），可做 10 年历史分位（2016-2025 每年 2752-5347 只）。NNWC 与净现金两个变体受 trading_financial_assets 限制，只能 2019-2025（7 年…
- **主要陷阱**：① 金融股必须靠 total_current_assets IS NOT NULL 自然剔除（2025 年报 100 只为空），千万不要 COALESCE(…,0)，否则银行会算出 NCAVPS≈−全部负债/股本 的荒谬值；② NCAVPS ≤ 0 时 price/NCAVPS 会变成巨大的负数并顶到「最便宜」榜首——实测 301565 得到 −28629、920981 得到 −13747，必须先 WHERE ncav_ps > 0 或 CASE WHEN ncav_ps>0 再取比值；③ 净净股在 A 股是濒危物种：实测 price < 2/3×NCAVPS 每年只有 0-5 只（2025…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ match 分级：9 个 db_field 全部真实存在，多数打分诚实：total_current_assets / total_liabilities / inventory / monetary_funds / share_capital_history / report_date_close 标 exact 正确；trading_financial_assets 与「应收票据+应收账款」标 approximate 并写明 2019 新准则口径断点，正确。唯一 overstated 项：balance_sheet.minorit…
  - 🛠 修法：①把 minority_interest 从 exact 降为 approximate，note 改为「NULL 多数为无少数股东，但实测 2025 年有 32 家 minority_interest 为空而少数股东损益 >0，COALESCE 0 会使 ncav_parent_ps 偏高」，或改用 COALESCE(minority_interest, CASE WHEN i.minority_shareholder_profit > 0 THEN NULL END) 并对该子集置空；②2003 年股本覆盖分母改…

#### 3. 盈利收益率与 10 年期国债利差（E/P − 无风险利率）（Earnings Yield minus 10Y Government Bond Yield）

- **提出者**：Benjamin Graham / Charles Brandes
- **公式**：`E/P = 归母净利润 ÷ 总市值 × 100%；利差 = E/P − 10 年期国债到期收益率(%)；格雷厄姆防御准则：E/P ≥ 2 × 债券收益率（本库无 AA/AAA 公司债，用 10Y 国债代替 → approximate）。Brandes 的相对价值版：把同一 E/P 与国债的差额做历史分位，判断资产类别间的相对吸引力。`
- **含义**：股票和债券是同一笔钱的两个去处，格雷厄姆要求「买股票拿到的盈利收益率至少是买债券的 2 倍」才值得承担股权风险。这个指标把股票收益率与无风险利率放在同一把尺子上，是唯一能回答「现在该买股还是买债」的可计算指标。
- **为什么超出 PE/PB**：PE 是绝对倍数，脱离利率环境就没有意义：4% 利率下 PE 15 是合理，1.6% 利率下 PE 25 可能还便宜。E/P 与国债的差额把「贵不贵」从倍数翻译成「超出无风险收益的补偿百分比」，可跨时代、跨国比较，也能算出市场整体的水位（过关股票数占比）。
- **字段映射**：exact 4 / approximate 1 / missing 1
  - ✔ `income_statement.parent_net_profit` ← 归母净利润（E）：2025 年报 5625/5625 全非空；保留负数样本是刻意设计（负 E/P 代表亏损），不要过滤。
  - ✔ `indicator_ext.market_cap_at_report` ← 总市值（P）：= 报告日收盘价 × 当期总股本（实测 000001 于 2025-12-31 为 2214 亿元，与 11.41 元 × 194 亿股一致）。与 report_date_close 同源，同样存在「当时未上市即为空」的样本缺失。
  - ✔ `treasury_yield_curve.yield_pct（tenor_years = 10.0）` ← 10 年期国债收益率：日频 2006-03-01 至 2026-11-16 共 5,179 个交易日；年均值实测 2007 年 3.99%、2014 年 4.16%、2025 年 1.74%。
  - ✘ `无对应表` ← AA/AAA 公司债收益率：格雷厄姆原式要求「AA 级公司债收益率 2 倍」，本库只有国债曲线（treasury_yield_curve），无信用债收益率 → 用 10Y 国债替代会低估阈值（信用利差通常 50-150bp），必须标 approximate 并在阈值上留出利差假设。
  - △ `indicator_snapshot.pe_ttm` ← PE_TTM（现成字段，慎用）：只有 3751/5575 非空——亏损股为 NULL 而非负值，用它反算 E/P 会静默丢掉全部亏损样本。
  - ✔ `indicator_snapshot.div_yield_spread_10y` ← 股息率利差（已有字段，勿混用）：项目已实现「股息率 − 10Y 国债」，非空 3734/5575。它衡量的是分红回报而非盈利回报，与 E/P 利差是两个指标，不可互相替代。
- **历史可用**：国债曲线自 2006-03-01 起日频（0.25/0.5/1/2/3/5/7/10/30 年九个 tenor 全历史），与年报截面匹配可得 2006-2025 共 20 个点，做 10 年历史中位数（2016-2025）毫无压力，是目前本库时间深度最好的复合指标之一。实测「E/P ≥ 2×国债」的股票数：2007 年 17/1354 只（大牛市顶）→ 2015 年 93/2579 → 2020 年 592/3934 → 2025 年…
- **主要陷阱**：① 绝对不要用 1/pe_ttm 代替 E/P：pe_ttm 亏损时为 NULL（仅 3751/5575 非空），反算会把全部亏损股静默剔除，横截面中位数被系统性抬高，也丢失了「负盈利收益率」这一最强信号；直接 parent_net_profit/market_cap 才能保留负样本；② treasury_yield_curve 的 tenor_years 是浮点，111 个 tenor 里有 102 个只有 2026-08-12 起的 47 天数据，必须精确 tenor_years = 10.0 或显式 IN 九个全历史 tenor，用 BETWEEN 会匹配到一堆单日 tenor 把均值污…
- **审查裁决**：**✅ 通过**　ROCE 同型风险：低
  - 🛠 修法：无需返工。可选增强：①对 |E/P|>100 的 75 行加市值下限（如 >=1 亿元）或在输出中标记；②把 2×国债 阈值做成可配置参数，并在结果表标注「以国债替代公司债、缺 50-150bp 信用利差」。

#### 4. 防御型投资者七准则合规分（0-7）（Defensive Investor Criteria Score）

- **提出者**：Benjamin Graham
- **公式**：`score = c1+…+c7（0-7 分）。c1 规模：营业总收入 ≥ 50 亿元（格雷厄姆 1972 年用「年销售额 1 亿美元」，阈值须重标定）；c2 财务稳健：流动比率 ≥ 2 且 长期负债 ≤ 净流动资产（营运资本）；c3 盈利稳定：过去 10 个年报归母净利润全部 > 0；c4 股息记录：连续 ≥ 20 年现金分红；c5 盈利成长：近 3 年平均 EPS ≥ 1.33 × 7-9 年前 3 年平均 EPS（十年增长三分之一）；c6 适度 P/E：总市值 ÷ 最近年报归母净利润 ≤ 15 且净利润 > 0；c7 适度 P/B：P/B ≤ 1.5 或 P/E×P/B ≤ 22.5。`
- **含义**：格雷厄姆在《聪明的投资者》第 14 章把「防御型投资者」的选股条件写成了七条可逐项核对的硬性检查，涵盖规模、财务稳健、盈利稳定、分红历史、成长、估值上限。单条都是常识，但组合起来是一套完整的「不想亏钱」过滤器：任何一条崩掉都意味着某类特定风险（小盘脆性、流动性危机、业绩不稳、不分红、无成长、估值透支）。
- **为什么超出 PE/PB**：PE/PB 只是结果，七准则检查的是「这个盈利和账面值能不能信」：连续 10 年盈利排除了周期顶部和一次性利润，流动比率排除短债长投，20 年分红史排除财务操纵，最后才用 PE/PB 卡价格。它是唯一把「质量」和「价格」写在同一张评分卡上的格雷厄姆指标。
- **字段映射**：exact 5 / approximate 2 / missing 0
  - ✔ `income_statement.total_operating_revenue` ← 营业总收入（规模）：年报口径完整。格雷厄姆原阈值是 1972 年美元营收，A 股用 50 亿元重标定属人为选择，应作为可配置参数。
  - ✔ `balance_sheet.total_current_assets ÷ balance_sheet.total_current_liabilities` ← 流动比率：金融股两者皆 NULL，必须剔除，否则除零或整条失效。
  - △ `balance_sheet.long_term_loans + bonds_payable + lease_liabilities` ← 长期负债：三项都大量 NULL 且语义为「没有该项负债」，必须 COALESCE 0；bonds_payable 2020 年仅 906/5953 只有值、2025 年 4633 只有值；严格口径还应含长期应付款，本库无该列。
  - ✔ `income_statement.parent_net_profit` ← 归母净利润（10 年稳定性）：用窗口函数数满 10 个年报点并要求 min>0；不能用 income_statement.net_profit（含少数股东损益）。
  - ✔ `dividends.ex_date + dividends.dividend_per_share` ← 现金分红记录（20 年）：分红数据可回溯至 1991-05（58,605 条、5,543 只），按 year(ex_date) 去重后用「年份 − 行号」分组求连续年数。要求 dividend_per_share > 0 以剔除纯送转。
  - △ `income_statement.basic_eps 或 parent_net_profit ÷ share_capital_history.total_shares` ← EPS（成长率）：basic_eps 2007 年前几乎全空，派生口径与 basic_eps 中位差 0.0025 元，可接受。
  - ✔ `indicator_ext.market_cap_at_report ÷ income_statement.parent_net_profit；indicator_ext.book_to_market` ← P/E 与 P/B：book_to_market 为归母口径（实测 = total_equity_parent/market_cap_at_report），故 P/B = 1/book_to_market 与格雷厄姆的账面值口径一致。
- **历史可用**：整套评分 1996-2025 共 30 个年报截面都可计算（1998 年起每年样本 ≥ 2000 只，2025 年 5348 只），但两条时间敏感条件的可用起点不同：c3（10 年盈利）与 c5（10 年 EPS）需要 10 个连续年报点，对老股在 1996 年即成立、对次新股永远不成立；c4（20 连红）受 1991 年分红数据起点限制，2011 年前恒为 0，会系统性低估早期分数。因此做「历史中位数」建议窗口取 2011-2025（…
- **主要陷阱**：① 绝不能做成二元筛选：实测 2025-12-31 全部 7 条同时通过的只有 1 只（600511），6 条 35 只、5 条 107 只、0 条 1236 只；2020 年同样是 7/7 仅 1 只。必须输出 0-7 分并用历史分位，把「全过才买」改成「分数分位前 10%」；② c6 的 P/E ≤ 15 若不先要求净利润 > 0，负 PE 天然 ≤ 15 会把亏损股全部误判成低估值——我第一版漏了该守卫，低 PE 命中数从 249 只虚增到 1668 只，这是最容易静默发生的错误；③ 判定年报不能用 report_type（同时存在 'annual'/'quarterly'/'semi_…
- **审查裁决**：**❌ 不通过**　ROCE 同型风险：**高**
  - ⚠️ match 分级：字段本身全真（7 个 db_field 均存在），但 c4「连续 20 年现金分红」的实现与其 exact 定级不符。streak CTE 按 (stock_code, grp) 输出「每一段分红连续区间」而不是「每股一行」，随后 LEFT JOIN 用 last_y >= year(report_date) 连接——这个条件既允许「区间起点晚于报告年」（纯未来信息），又会一对多扇出。实测：000548 在 1993-12-31 有 18 个分红年份、10 段区间、无任何 n>=20 的区间，却被展开成 10 行输…
  - ⚠️ 样本量：覆核不通过（三处）：①报告称「1998 年起每年样本 >=2000 只，2025 年 5348 只」——2025 年的 5348 属实且无重复，但早期年份是被 JOIN 扇出膨胀的「行数」：实测 1998 年 2,131 行却只有 667 只股票、2003 年 2,619 行只有 1,055 只，全 36 个年报截面中只有 15 个（2011-2025）真正 >=2,000 只，故「1998 年起」不成立，应为「2011 年起」。②报告…
  - ⚠️ ROCE 同型问题：本条目不属于分母为负型事故，但存在同等级的结构性口径缺陷，且只在历史回测路径暴露（正是 history_years 宣称的主用途）：①一对多 JOIN 扇出，108,482 行 vs 69,242 个真实样本（+36%），早期年份膨胀达 3.7 倍（1993 年 501 行 / 137 只），任何「样本数」「过关只数占比」「平均分」统计都被系统性放大；②695 个 (stock, date) 的评分不唯一，同一股票同一年份既可能 5 分也可能 4 分，分位排名会随机化；③c4 前视偏差使 1991-2011 全部 c4 命中为伪，跨年口径断点极陡（2011 年合法 0 只 vs 输出 197…
  - 🛠 修法：必须返工后重测：①把 streak 改为每股一行以消除扇出（例如先按 T 截断分红年份再算滚动连续年数，或对 streak 只保留 max(n) 并用 stock_code 唯一化），并在输出层对 (stock_code, report_date) 做 GROUP BY/DISTINCT 兜底，补一条断言 n_rows = n_distinct；②c4 判定改为「截至 T 年该股已连续分红年数 >=20」，即先把分红年份限制为 yy <= year(report_date) 再数连续区间，禁止使用 last_y >…

#### 5. 格雷厄姆成长股价值公式 V = EPS × (8.5 + 2g)（含利率重标定版）（Graham Growth Formula / Graham Intrinsic Value）

- **提出者**：Benjamin Graham
- **公式**：`V = EPS × (8.5 + 2g)，g 为预期年增长率（%）。实践中 g 用历史代理：g = (近 3 年平均 EPS ÷ 7-9 年前 3 年平均 EPS)^(1/7) − 1，并截断到 [0, 15]。安全边际 = V ÷ 价格 − 1。1974 年修订版（利率重标定）：V = EPS × (8.5 + 2g) × 4.4 ÷ Y，其中 Y 为当期优质债券收益率（本库用 10Y 国债替代）。`
- **含义**：格雷厄姆晚年的成长股估值捷径：把「无成长时值 8.5 倍市盈率」和「每 1% 预期增长加 2 倍市盈率」写成一个线性式，得到可以和安全边际直接相乘的内在价值。它的价值不在精确度，而在给出一个「成长被定价到什么程度」的坐标，让 PEG 式的直觉变成可比的绝对数字。
- **为什么超出 PE/PB**：PE 完全不含成长信息，PB 完全不含盈利能力信息；这条公式把盈利水平与增长率放进同一个乘数，且乘数可用无风险利率重标定，因此能在利率环境不同的年份之间做纵向比较（这是 PE/PB 都做不到的）。同时它直接输出「V/价格」这个带安全边际的比率。
- **字段映射**：exact 2 / approximate 3 / missing 0
  - ✔ `income_statement.basic_eps` ← EPS（起点盈利）：2007 年起覆盖 93%+；2006 年 77.6%；2005 年及以前基本全空，必须走派生兜底。
  - △ `income_statement.parent_net_profit ÷ share_capital_history.total_shares` ← EPS 派生兜底：实测 2025 年与 basic_eps 中位绝对差 0.0025 元；差异源于加权平均股本 vs 期末股本，在高送转/增发年份可达 1-3%。
  - △ `同一 EPS 序列的窗口聚合（avg OVER 2 PRECEDING …、avg OVER 9~7 PRECEDING）` ← 增长率 g（3 年平均口径）：格雷厄姆原文要求「未来 7-10 年预期增长率」，本库只能用已实现的历史增长作代理，属定义性近似（且含幸存者偏差），不可消除。
  - △ `treasury_yield_curve.yield_pct（tenor_years = 10.0，按年报年份取年均值）` ← 利率重标定 Y：格雷厄姆修订版用 AA/AAA 公司债收益率；本库只有国债曲线，无信用债 → 用 10Y 国债会系统性抬高重标定后的 V（少 50-150bp 利差），需标注。
  - ✔ `indicator_ext.report_date_close` ← 价格：与格雷厄姆数同一字段，同样必须 INNER JOIN 有价格的行。
- **历史可用**：3 年平均首尾 + 需 10 个连续年报点，实测 1999-2025 共 27 个年度截面可算（1999 年 54 只、2007 年 939 只、2016 年 1996 只、2025 年 4567 只）；2007 年前可算的都是「有 10 年连续报表的老股」，存在明显幸存者/长上市期偏差，真正可比的起点仍是 2007（19 年），10 年历史中位数（2016-2025 每年 1996-4567 只）完全可行。若放宽为两端单年 7 年 C…
- **主要陷阱**：① 单年两端点算 g 会被极小基数放大——实测 002379 用单年口径得 g=189%、V=605 元而股价 23.92；改 3 年平均口径后仍得 g=84.5%（截断 15%）、V=394.66 元，说明「截断 g」远不够，必须再加基期 EPS 绝对下限（如 ≥0.1 元）或要求基期盈利连续为正且波动可控，否则榜单前几名全是周期反转股和 ST 复活股；② 8.5 这个常数是 1962 年 4.4% AAA 债券利率下校准的，A 股 2025 年 10Y 国债只有 1.74%，直接用会系统性低估；必须用 1974 修订版 ×4.4/Y 重标定，否则同一 g 在 2014 年（国债 4.16%…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：①把 g 真正置 NULL：改用 CASE WHEN eps_avg3_now > 0 AND eps_avg3_7y_ago > 0 THEN LEAST(GREATEST(g_pct_raw,0),15) END（当前写法 GREATEST(NULL,0) 在 DuckDB 返回 0，导致 13,920 行被静默记为零增长），并对该子集不输出 graham_value；②删除或更正 trap ① 中「g=84.5%、V=394.66」的实测数字，改为实测 g_raw=68.11%、v1962=32.72、v_a…

#### 6. 施洛斯式深度价值四因子（低 PB + 低杠杆 + 长期盈利 + 多年低位）（Schloss Deep-Value Screen (low P/B, low leverage, persistent earnings, multi-year price low)）

- **提出者**：Walter Schloss（与 Tweedy Browne《What Has Worked in Investing》同源）
- **公式**：`四项布尔条件求和 deep_value_score = f1+f2+f3+f4（0-4 分）。f1 低 PB：pb_mrq ≤ 1.0（或全市场 P/B 分位 ≤ 20%）；f2 低杠杆：total_liabilities ÷ total_equity ≤ 0.5；f3 长期盈利：过去 ≥ 10 个年报 parent_net_profit 全部 > 0；f4 多年低位：最新前复权收盘 ÷ 近 5 年前复权最高收盘 ≤ 0.6。`
- **含义**：施洛斯一辈子只看一件事：用低于重置成本的价格买「不会死」的公司，然后分散持有等均值回归。四个因子正好覆盖他的四道防线——便宜（PB）、不会死（负债低）、一直赚钱（10 年盈利）、没人要（股价在多年低位）。Tweedy Browne 的实证研究把「低 PB + 价格大幅下跌」列为历史上最有效的两类因子，是同一套逻辑的量化版。
- **为什么超出 PE/PB**：单看低 PB 会买到「资产是商誉、负债是刚兑」的价值陷阱，单看低 PE 会买到周期顶部。四个因子叠加后：低杠杆排除财务脆弱、10 年盈利排除一次性利润、股价多年低位保证「便宜还没被发现」，这是纯 PE/PB 排序无法表达的「便宜且活着且长期赚钱」的联合条件。
- **字段映射**：exact 5 / approximate 1 / missing 0
  - ✔ `indicator_snapshot.pb_mrq` ← P/B（当期快照）：5,512/5,575 非空（亏损不影响 PB）。注意它是「最新一期」快照，做历史回测必须改用报告期口径。
  - ✔ `indicator_ext.book_to_market（PB = 1/book_to_market）` ← P/B（报告期口径）：实测 = total_equity_parent / market_cap_at_report（000651 逐 4 位小数对齐），归母口径，可逐年回溯。
  - ✔ `balance_sheet.total_liabilities ÷ balance_sheet.total_equity` ← 负债 / 权益：两者 2025 年报均 5625/5625 非空。分母选 total_equity（含少数股东）比 total_equity_parent 更宽松，会把高少数股东占比的地产/基建杠杆低估，必须显式声明口径。
  - ✔ `income_statement.parent_net_profit（按 stock_code 聚合 count 与 min）` ← 10 年连续盈利：需 10 个年报点，用 min(...) > 0 且 count = 10；新上市股会被自动排除，这是设计而非缺陷。
  - ✔ `price_daily_qfq.close（arg_max + max/min over 5 年窗口）` ← 近 5 年最高/最低价：日频 1,756 万行、1990 起完整；实测全市场 5,585 只可算，中位 close/5年高点 = 0.47。
  - △ `stock_meta.is_st / stock_meta.is_listed / stock_meta.sw_level1` ← ST / 退市 / 行业标签：is_st 197 只、is_listed 5579/6284；sw_level1 有 750 只 NULL，金融股标签为 '银行'(42) 与 '非银金融'(79)，用 csrc_l1='金融业' 会被同花顺/指南针等软件股污染。
- **历史可用**：价格史 1990 起完整（price_daily_raw / price_daily_qfq 各约 1,756 万行，10 年以上日线的股票 2,651 只、5-10 年 1,697 只），10 年年报盈利条件把四因子齐全的最早截面推到 2007 年报（老股可更早）；实测每年样本 4,000-5,400 只，可做 10 年以上历史回测。2025 年 12 月截面漏斗：全市场合格 5,223 只 → PB≤1: 446 → 负债/权益≤0…
- **主要陷阱**：① price_daily_qfq 是前复权（以最新日为基准重算历史价）：用今天的数据算「过去 5 年最高价」再回测 2018 年截面，等于把未来的复权因子带回过去（每次新增分红送转都会改写整条历史），回测必须用截面日当时的前复权基准或 price_daily_raw × 当期因子，否则 f4 因子会带前视偏差；② ST/退市必须显式过滤：A 股 2019 年后常态化退市，不过滤会把即将面值退市的仙股全部捞进来——这是施洛斯式低 PB 策略在 A 股最致命的死法（低 PB 里 ST 占比极高）；③ 金融股负债率天然 90%+，靠 f2 就能自动排除，但一旦把阈值放宽或改成「负债/归母权益」，银…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ match 分级：6 个 db_field 全部存在；pb_mrq（5512/5575 非空）、book_to_market（实测严格等于 total_equity_parent / market_cap_at_report，归母口径）、total_liabilities/total_equity、price_daily_qfq.close、stock_meta.is_st / is_listed / sw_level1（实测 is_st=197、is_listed=5579/6284、sw_level1 空 750、银行 42、非…
  - 🛠 修法：①f3 按自己的 trap ⑤ 落地：改成对「最近 10 个年报」取 min（例如先 ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY report_date DESC) <= 10 再聚合，且要求 count=10），同时把该 db_field 的 match 由 exact 降为 approximate 或改写 note 说明「口径＝上市以来全部年报」；②统一 funnel 口径，把 5,223 改为实测 5,206、f4 由 4,167 改为 4,154；…


### 3.2 巴菲特-芒格学派（优质企业 + 护城河 + 资本配置）

**覆盖大师**：Warren Buffett、Charlie Munger、Lou Simpson、Bill Ruane

**本流派审查总评**：6 个指标全部无 P0：db_field 逐条对照 information_schema.columns 后 0 个假字段（含 SQL 里 alias 引用的列，靠逐条执行证实确实存在），6 条 formula_sql（共 7 个 statement）全部跑通且都不是全 NULL/全 0（OE 68,011 行、巴菲特池 29 年、ROE 337 只、毛利率 418 只、一美元 2,491 行、RCR 2,507 行）。样本量诚实度是本文件最大的优点：7 个一美元测试样例（8.00/8.32/3.25/2.40/0.98/0.92/−187.79）逐位复现，600519 自建市值在 indicator_ext 有值的 25 个年报点（含 2016-2025 全部十行）与后者逐年完全相等（diff=0.0000%），2004/2012/2025 毛利率覆盖（1,674/1,739、3,653/3,747、5,530/5,625）完全一致，1990 年 5 只股票/市值营收比 183.7978、601398 市值 28,263 亿、A+H 股本三项、raw/qfq 2.3% 差、ROE 深度 6,142/5,839/5,109/3,311（报告 6,106/5,804/5,071/3,274）、茅台 ROE 33.65%/34.46%、EBIT=总利润+财务费用 等全部对得上，未发现编…

#### 7. 股东盈余与股东盈余收益率（Owner Earnings & Owner Earnings Yield）

- **提出者**：Warren Buffett
- **公式**：`OE = 归母净利润 + 折旧摊销 + 非现金减值摊销 − 资本支出（作为维持性资本支出的上界代理）; OE_Yield = OE ÷ 期末总市值`
- **含义**：巴菲特 1986 年致股东信把「股东盈余」定义为：净利润 + 折旧、摊销及其他非现金费用 − 维持竞争地位与现有产能所必需的资本支出。它衡量的是股东真正能从企业拿走的现金，而不是会计利润。再除以总市值得到的股东盈余收益率，可以直接和国债收益率比较，回答「这家企业给我的现金回报比无风险利率高多少」。
- **为什么超出 PE/PB**：PE 用的是会计净利润，会被折旧年限、商誉减值、资本开支节奏污染；OE 把资本开支扣回来，能一眼分出「轻资产真现金牛」与「账面高利润但年年重资产再投入」的公司。PB 完全无法区分资产轻重与资本开支强度，而这正是 A 股大量「利润表好看、自由现金流常年为负」公司的识别关键。
- **字段映射**：exact 7 / approximate 4 / missing 0
  - ✔ `income_statement.parent_net_profit` ← 归母净利润：必须用利润表；不可用 cash_flow_indirect.net_profit，该列 2025 年报 5,579 行全空。
  - ✔ `cash_flow_indirect.fixed_asset_depreciation` ← 固定资产折旧：2025 年报 5,532/5,625 有值；NULL 属未披露，COALESCE 0 但需统计样本量。
  - ✔ `cash_flow_indirect.intangible_asset_amortization` ← 无形资产摊销：2025 年报 5,490/5,625 有值。
  - △ `cash_flow_indirect.right_of_use_asset_depreciation` ← 使用权资产折旧：2021 年租赁新准则后才有，2020 年及以前恒为 0/空（2020 年报 0 行）；2025 年报仅 4,744 家。跨 2021 年做长序列会在口径上出现台阶。
  - △ `cash_flow_indirect.investment_property_depreciation` ← 投资性房地产折旧：2025 年报仅 174 家有值，2020 年及以前 1 行；覆盖面过窄，只能 COALESCE 0 处理。
  - ✔ `cash_flow_indirect.long_term_prepaid_amortization` ← 长期待摊费用摊销：与折旧摊销同属非现金费用，应计入。
  - ✔ `cash_flow_indirect.asset_impairment_provision` ← 资产减值准备：间接法补充资料口径，是可加回的非现金项。
  - ✔ `cash_flow_indirect.credit_impairment_loss` ← 信用减值损失：应收账款/合同资产减值，同属非现金项。
  - △ `cash_flow_activity.capex` ← 维持性资本支出：库里没有「维持性资本支出」字段（巴菲特原文的 maintenance capex 从未被披露），只能用资本支出总额代理：它包含扩张性 capex，会系统性低估股东盈余，高扩张期公司甚至算出负 OE。差值就在于「扩张性 vs 维持性」这一块无法拆分。
  - △ `indicator_ext.market_cap_at_report` ← 期末总市值（分母）：= A 股收盘价 × 总股本；A+H/B 股公司（如 601398 总股本 3,564 亿股含 H 股）会高估市值；且逐年覆盖率从 2014 年 51% 升到 2025 年 97%，做时序须谨慎。可改用 price_daily_raw.close × share_capital_history.to…
  - ✔ `indicator_ext.free_cash_flow` ← 可比对照：自由现金流：= cf_from_operating − capex，已实测对齐；OE 与 FCF 并列报告可暴露「折旧/减值口径差异」与「经营性应收应付占用」。
- **历史可用**：折旧摊销（cash_flow_indirect）与 capex（cash_flow_activity）最早可回溯 1998 年，但按年覆盖率是断崖式的：2012 年 2,586/3,745（69%）、2016 年 3,434/5,085（68%）、2020 年 4,578/5,953（77%）、2025 年 5,532/5,625（98%）。因此 OE 的「10 年历史中位数」实际只能从 2016 年起做，合格样本约 3,400-4,6…
- **主要陷阱**：① 库里没有 maintenance capex 字段，total capex 是上界代理，OE 会系统性偏低；高扩张期公司（2018-2022 的宁德时代类）OE 常年为负，这不是企业变差而是口径使然，必须与 FCF 并列看。② cash_flow_indirect.net_profit 在 2025 年报 5,579 行中 0 非空（整列空），取净利润只能走 income_statement.parent_net_profit。③ 使用权资产折旧与投资性房地产折旧在 2021 年前根本不存在于表中（2020 年报分别为 0 行、1 行），2021 年前后的 OE 不可严格比较。④ 折旧摊销…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：**高**
  - ⚠️ ROCE 同型问题：① 2025 年 credit_impairment_loss 整列 NULL：2024 年有 2,546 家有值（中位 = 归母净利的 3.1%、p90 = 45.8%），2025 年 0 家，OE 在 2024→2025 少加回一项，形成结构性下台阶，会被误读成「OE 变差」。报告在 traps 里点了 net_profit 的 2025 断点，却漏了同一张表的这一列。② 2021 年新租赁准则让使用权资产折旧从 0 跳到 4,778 家（已披露）。③ 归母净利 + 合并 D&A/capex 混口径（290 家 MI>25%）。④ oe_yield 尾部 ±493%（|yield|>100…
  - 🛠 修法：① 2025 年改用 income_statement.credit_impairment_loss，或在结果里标注 2025 断点、禁止跨 2024-2025 直接比较 OE 增速；② 分子改净利口径（net_profit + 合并 D&A − capex，或 parent_net_profit + minority_shareholder_profit）；③ 输出 n_da_effective（D&A 非空只数），落实 trap ④ 自己的要求；④ 加 stock_meta.sw_level1 NOT IN (…

#### 8. 巴菲特指标（证券化率）与 A 股可执行适配版（Buffett Indicator (Market Cap / GDP) and A-share Market Cap / Revenue Adaptation）

- **提出者**：Warren Buffett
- **公式**：`证券化率 = 全A股总市值 ÷ 名义 GDP（年度）; A 股可执行适配版 = Σ市值 ÷ Σ营业总收入（锁定「市值与营收都非空」的同一股票池）`
- **含义**：巴菲特 2001 年提出用「股市总市值 ÷ GDP」衡量整体市场贵还是便宜，并称之为衡量任何时点估值水平的最佳单一指标。分子是市场愿意付出的价格总量，分母是一国经济真正产出的总量，比值越高说明同样的经济产出被标了越高的价。库内完全没有 GDP，所以此处给出逻辑同构、且百分之百可执行的适配版：全A市值 ÷ 全A营业总收入（分子=价格，分母=经济产出），一样能给出跨年份的绝对水位。
- **为什么超出 PE/PB**：个股 PE/PB 只能做「同一时点不同公司」的横截面排序，无法回答「现在整个市场比五年前贵还是便宜」。巴菲特指标把分母换成宏观产出，直接产出时序上的绝对水位（实测全A 市值/营收：2018 年 1.07 → 2021 年 1.52 → 2025 年 1.69），这是任何个股估值倍数拼不出来的市场级信号。
- **字段映射**：exact 2 / approximate 3 / missing 1
  - △ `indicator_ext.market_cap_at_report` ← 分子：Σ全A总市值：报告日 A 股收盘价 × 总股本。两点偏差：(a) A+H/B 股公司总股本含非 A 股本，市值高估；(b) 逐年覆盖率从 2014 年 51%（3,338/5,955）升到 2025 年 97%（5,447/5,625），对全样本直接 SUM 会造出伪上升趋势。
  - ✔ `price_daily_raw.close × share_capital_history.total_shares` ← 分子（自建，推荐）：全A市值序列：ASOF 连接取报告日收盘价与当期股本。已实测与 indicator_ext.market_cap_at_report 逐股逐年完全一致（600519 2016-2025 十行全等）；2025 全A合计自建 122.93 万亿 vs indicator_ext 122.88 万亿，差 0.04%。价…
  - ✘ `（库内不存在）` ← 分母：名义 GDP（年度/季度）：对全库 65 张表做过 information_schema 检索，'%gdp%' / '%m2%' / '%gnp%' / '%macro%' / '%national%' 全部零命中。index_valuation 只有指数 PE/PB/股息率，treasury_yield_curve 只有国债…
  - ✔ `income_statement.revenue` ← 分母替代：Σ全A营业总收入：作为 GDP 的库内替代。必须与分子锁定同一股票池（市值与营收都非空的交集），否则两侧覆盖率差异会伪造趋势。
  - △ `indicator_snapshot.total_market_cap` ← 对照：全A总市值快照：只有当期一期（5,575 行，latest_price_date 集中在 2026-11-16，report_date 混杂 2026-06-30/09-30），是 latest_close × total_shares 的快照而非时序，绝不能用来构造历史序列。
  - △ `income_statement.total_operating_revenue` ← 分子交叉验证：全A营业总收入合计：与 revenue 口径不同（含利息收入等），可作稳健性检查的第二分母，但不可与 revenue 混用。
- **历史可用**：分两条路：(a) 依赖 indicator_ext.market_cap_at_report —— 2014 年 51%、2016 年 55%、2018 年 57%、2020 年 66%、2025 年 97%，实际只有 2020 年后可靠，做 10 年中位数会静默丢掉四成样本。(b) 自建市值（price_daily_raw 1990 年代起 + share_capital_history 覆盖 5,551/6,284 只、最早 199…
- **主要陷阱**：① 分母 GDP 在库内根本不存在（65 张表零命中），这是硬缺口：任何声称「已算出巴菲特指标」的实现都必须是外部注入了 GDP，否则是编数据。② GDP 是流量、总市值是存量，严格做法是用 10 年 GDP 均值做分母，单年 GDP 会让比值随经济周期剧烈摆动。③ 中国证券化率长期在 50%-80% 区间（2025 年约 88%），与美国 150%-200% 的常态不可比，绝不能拿「>100% 即泡沫」这条美国经验当 A 股阈值，否则过去二十年会一直报警。④ 覆盖率漂移陷阱（最容易踩）：indicator_ext.market_cap_at_report 逐年覆盖率差异极大，直接对全样本 S…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：① 名称与结论分离：明确写「A 股适配版 = 市值/营收，不是证券化率」，避免被当成巴菲特指标使用；② GDP 分母写成显式外部注入接口（含 10 年 GDP 均值口径）；③ 自建市值 SQL 目前只算 2025 单点，应改成按年窗口/循环出全序列，否则算不出 20 年曲线；④ 修正「2014 年 51%（3,338/5,955）」这对错配的绝对数。

#### 9. 十年中位数 ROE ≥ 15% 且真正低负债（Durable High ROE (10-Year Median >= 15%) with Genuinely Low Leverage）

- **提出者**：Warren Buffett, Charlie Munger
- **公式**：`ROE_t = parent_net_profit_t ÷ [(total_equity_parent_t + total_equity_parent_{t−1}) ÷ 2]; 门槛：MEDIAN(ROE, 近10年) ≥ 15% 且 MIN(ROE) ≥ 8% 且 MEDIAN(有息负债 ÷ 归母权益) ≤ 50%，其中有息负债 = short_term_loans + long_term_loans + bonds_payable`
- **含义**：巴菲特与芒格反复强调要买「长期能维持高股东权益报酬率、且几乎不靠债务」的企业，理由是杠杆推出来的高 ROE 在市场收缩时会连环爆掉。用十年中位数而非单年值，是为了滤掉卖资产、资产重估、周期顶部这类一次性高利润，只留下结构性赚钱能力；再用最小年份值兜底，防止「九年平庸 + 一年暴利」被中位数掩盖。
- **为什么超出 PE/PB**：低 PE 可能只是烂生意卖得便宜，低 PB 往往是资产一直在毁值——它们衡量的是价格，不是生意。ROE 与有息负债的组合衡量的是生意本身的质量，与价格无关，是横向筛掉价值陷阱的第一道网；而中位数口径进一步回答「这个高 ROE 是十年都这样，还是去年才这样」，这是任何单点估值倍数都给不出的时间维度。
- **字段映射**：exact 7 / approximate 5 / missing 0
  - ✔ `income_statement.parent_net_profit` ← 归母净利润（分子）：必须与 total_equity_parent 配对；不可与 net_profit 混。
  - △ `income_statement.deducted_net_profit` ← 扣非归母净利润（交叉验证）：覆盖不全：2025 年 5,622/5,625 很好，但 2016 年仅 3,222/5,085（63%）、2020 年 4,586/5,953（77%），只能做近 5 年的辅助口径，无法支撑十年序列。
  - ✔ `balance_sheet.total_equity_parent` ← 归母权益（分母，期初+期末均值）：与 ROCE 采用同一 (期初+期末)/2 口径以保证体系内一致。
  - ✔ `balance_sheet.total_equity` ← 全部所有者权益（全口径对照）：若改配 net_profit 则必须用这一列；两组不可交叉搭配。
  - ✔ `balance_sheet.short_term_loans` ← 有息负债：短期借款：有息负债三项之一。
  - ✔ `balance_sheet.long_term_loans` ← 有息负债：长期借款：有息负债三项之一。
  - △ `balance_sheet.bonds_payable` ← 有息负债：应付债券：大量 NULL 属正常（无债券的公司本就没有，2020 年报仅 906 只有值），必须 COALESCE 0，不能当缺失剔除。
  - △ `balance_sheet.lease_liabilities` ← 租赁负债（可选计入）：2021 年新租赁准则后才有；是否计入会显著改变航空、零售、餐饮类公司的杠杆判定，建议单列一版。
  - ✔ `balance_sheet.total_assets` ← 资产总计（异常过滤）：用于剔除 total_assets<=0 的异常行。
  - △ `balance_sheet.total_liabilities` ← 负债合计（禁止用于判低负债）：语义本身清楚，但不适合做「低负债」判据：它含合同负债/应付账款这类无息经营性占款，会把占款型好生意误杀。详见 traps。
  - △ `stock_meta.sw_level1` ← 行业分类（剔除金融股）：取值 '银行'(42 只) / '非银金融'(79 只) 可精确匹配；但该字段有 750 只 NULL，单靠它剔除会漏，必须叠加 cost_of_revenue IS NULL 兜底。
  - ✔ `stock_meta.listing_date` ← 上市日期（防借壳前后不可比）：用于截断重组前会计主体。
- **历史可用**：本流派历史最深的指标。三表可回溯 1990 年前后，2007 年后每年 5,000+ 只。实测（以 2025 年报为终点的年报序列）：6,106 只股票有 ≥5 个 ROE 点、5,804 只 ≥8 个、5,071 只 ≥10 个、3,274 只 ≥15 个。因此「十年中位数 ROE」可覆盖全市场约 5,000 只，「十五年稳定性」可覆盖 3,200 只——完全可以像 ROCE 那样做历史中位数，且样本量比 ROCE 更大（ROCE 依…
- **主要陷阱**：① 分母口径必须与已有 ROCE 统一为期初期末均值：indicator_snapshot.roe 是单期口径，茅台 2025 期末口径 33.7% vs 均值口径 34.5%，两套 ROE 并存会让同一家公司出现两个数字，必须显式标注来源。② 判「低负债」绝不能用 total_liabilities/total_assets（即 indicator_snapshot.debt_ratio）：茅台、伊利、海天这类占款型好生意的负债主体是合同负债与应付账款（零成本、越多越好），debt_ratio 常在 30-40% 却完全没有有息负债；反过来全靠银行借款的公司 debt_ratio 45% 反…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：① SQL 补上 stock_meta.sw_level1 NOT IN ('银行','非银金融') AND i.cost_of_revenue IS NOT NULL 双保险（现在一条都没有）；② LAG 改在「未过滤负权益」的完整年报序列上取前值，前值 ≤0 时置 NULL，避免跨年平均；③ HAVING COUNT(*)>=8 与指标名「十年中位数」不一致，改为 >=10 或更名为「8~10 年中位数」。

#### 10. 毛利率十年稳定性（定价权代理）（10-Year Gross Margin Durability as a Pricing-Power Proxy）

- **提出者**：Charlie Munger
- **公式**：`GM_t = (revenue_t − cost_of_revenue_t) ÷ revenue_t（仅年报，n ≥ 8）; 稳定性三件套：STDDEV_SAMP(GM) ≤ 3pp、MAX(GM) − MIN(GM) ≤ 8pp、MIN(GM) ≥ 20%; 趋势项：REGR_SLOPE(GM, fy) ≥ 0`
- **含义**：芒格把「能否在不流失客户的前提下持续提价」视为护城河最可观测的证据，而毛利率的长期水平与波动是它最便宜的量化代理。真有定价权的公司毛利率既高又稳——茅台十年毛利率 89.8%-92.0%、标准差仅 0.63pp；没有定价权的公司毛利率会随原材料价格和价格战来回跳。稳定性比绝对水平更关键：高但不稳的毛利率说明提价能力是周期性的，不是结构性的。
- **为什么超出 PE/PB**：PE/PB 完全不含「这条护城河还能维持多久」的信息。毛利率方差是唯一能用公开财报零成本测出的「护城河持久性」代理，能提前识别「今年利润好是因为提价一次性兑现、明年会回吐」的公司——这是估值倍数天生看不见的维度。
- **字段映射**：exact 2 / approximate 5 / missing 0
  - ✔ `income_statement.revenue` ← 营业收入（分母）：必须用 revenue，不可用 total_operating_revenue。
  - ✔ `income_statement.cost_of_revenue` ← 营业成本（分子减项）：必须是营业成本本体，不可用 total_operating_cost。
  - △ `income_statement.total_operating_revenue` ← 营业总收入（禁止配对）：含利息收入等其他业务收入，与 cost_of_revenue 不同口径，配对会算出假毛利率。
  - △ `income_statement.total_operating_cost` ← 营业总成本（禁止配对）：含销售/管理/研发/财务费用与税金及附加，用它做减项得到的是营业利润率，不是毛利率。
  - △ `indicator_snapshot.gross_margin` ← 现成毛利率：只有当期一期单点值（5,575 行），无历史序列，算不了方差与极差，只能用于当期快筛。
  - △ `stock_meta.sw_level1` ← 行业分类（剔除金融股）：金融股 cost_of_revenue 天然 NULL，用 NULL 条件剔除更可靠；sw_level1 有 750 只 NULL 只能作辅助。
  - △ `dividends.dividend_per_share` ← 股息/分红（交叉验证现金流质量）：辅助项：毛利率稳且长期高分红，比单看毛利率更能确认利润是真的。
- **历史可用**：很好，仅次于 ROE。revenue / cost_of_revenue 覆盖早且密：2004 年报 1,674/1,739（96%）、2012 年 3,653/3,747、2025 年 5,530/5,625（98%）。十 年窗口对所有上市满十年的公司都可算（约 5,000 只），二十年窗口对 2005 年前上市的公司可算。因此「十年毛利率标准差」完全可以做成历史中位数式的长期打分。
- **主要陷阱**：① 金融股必须剔除：银行/保险/券商 cost_of_revenue 全为 NULL（2025 年报 95 只），强行计入只会得到 NULL 或 0 毛利率，把方差算成 0 的假稳定。② 2020 年新收入准则把运输费从销售费用重分类进营业成本，2019→2020 存在 1-3pp 的结构性断点（快消、零售、家电类尤甚）；跨 2020 年直接算 σ，会把会计政策变更误读成「定价权恶化」，建议分段（2016-2019 / 2020-2025）或做断点校正。③ revenue ≤ 0 必须剔除（2025 年报有 1 只），否则除零；cost_of_revenue = 0 要区分「真零成本」（软件、…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：① 让 regr_slope 的注释与窗口一致（要么真取 fy>=2021 的近 5 年，要么把注释改成「2016 起全窗口斜率」）；② 把「revenue≤0 有 1 只」更正为 2 只；③ 建议默认输出 2016-2019 / 2020-2025 两段 σ，或对 2020 年做断点校正，避免把准则变更读成定价权恶化。

#### 11. 一美元测试（One-Dollar Test (Retained-Earnings Market-Value Creation)）

- **提出者**：Warren Buffett
- **公式**：`OneDollar = [市值_t − 市值_{t−N}] ÷ [留存收益_t − 留存收益_{t−N}], 其中 留存收益 = undistributed_profit + surplus_reserve; 判定 ≥ 1.0 通过（优选 ≥ 1.5）; 有效样本门槛: Δ留存收益 ≥ 5% × 期初归母权益`
- **含义**：巴菲特的标准是：公司每留存 1 美元盈余，至少应当创造出 1 美元的市值；留得越多、创造越少的公司，说明管理层把股东的钱投进了坏项目。它把「资本配置能力」变成了一道有明确分子分母的算术题，是巴菲特衡量管理层最重要的一条硬标准，也是他反对「为增长而增长」的量化依据。
- **为什么超出 PE/PB**：PE/PB 只描述「当下价格相对利润/净资产贵不贵」，回答不了「过去十年管理层把赚来的钱再投出去之后，公司整体变值钱了没有」。一美元测试同时纳入时间维度与资本配置效率，正好能把「便宜是因为被错杀」和「便宜是因为管理层十年一直在毁值」两类低估值公司区分开。
- **字段映射**：exact 5 / approximate 2 / missing 1
  - ✔ `price_daily_raw.close × share_capital_history.total_shares` ← 期末市值（自建，推荐）：ASOF 取当年 12-31 前最后一个交易日的未复权收盘价 × 当期总股本。已实测与 indicator_ext.market_cap_at_report 逐股逐年完全一致（600519 2016-2025 十行全等），且不受后者覆盖率限制。
  - △ `indicator_ext.market_cap_at_report` ← 期末市值（快捷口径）：可直接用，但逐年覆盖率 2014 年 51% → 2025 年 97%，做十年序列会静默丢样本且丢得不随机。
  - ✔ `balance_sheet.undistributed_profit` ← 留存收益：未分配利润：利润表结转与分红都发生在这个科目，是留存收益的准确载体。
  - ✔ `balance_sheet.surplus_reserve` ← 留存收益：盈余公积：必须与未分配利润相加：法定 10% 提取只是在两个科目之间搬运，不加总会把「提取盈余公积的年份」误算成留存减少。
  - ✔ `balance_sheet.total_equity_parent` ← 期初归母权益（样本门槛分母）：用于 Δ留存收益 ≥ 5%×期初权益 的门槛，剔除分母趋零导致的爆炸值。
  - △ `dividends.dividend_per_share × share_capital_history.total_shares` ← 分红总额（校核路径）：dividends 表只有 ex_date、没有「所属报告期」，茅台 2022 年后一年两次除权（中期+年度），按 ex_date 年份聚合会错配。实测该路径与 Δ留存收益 路径在 2022-2024 年差异 <0.1%，可做校核但不能做主口径。
  - ✘ `cash_flow_activity.dividend_interest_paid` ← 现金流量表分红支出：2025 年报该字段 100% 全空，且语义是「分配股利、利润或偿付利息支付的现金」，混了利息支出，不可用。
  - ✔ `balance_sheet.total_equity_parent + short_term_loans + long_term_loans + bonds_payable` ← 对照口径：Δ投入资本：用于剔除增发/转债转股带来的外部融资，得到「纯内生」版一美元测试。
- **历史可用**：价格数据自 1990 年代起、share_capital_history 覆盖 5,551/6,284 只股票（最早 1990-12-19），因此 2015→2025 十年窗口对绝大多数公司可算；2005→2025 二十年窗口对 2005 年前上市公司可用，但 1990s 的股本链不完整。若退化为依赖 indicator_ext.market_cap_at_report，则只有 2020 年后样本可靠。实测样例（2015→2025）：6…
- **主要陷阱**：① 市值必须用 price_daily_raw（未复权收盘价）配当期 total_shares；用 price_daily_qfq（前复权）乘原始股本是错的——600519 在 2025-12-31 raw=1377.18、qfq=1345.34，单点差 2.3%，十年复利后是灾难性偏差。② 分母趋零/为负会爆炸：万科A 2015→2025 的 Δ留存收益仅 11.4 亿元（十年利润被后续亏损吃光），一美元测试算得 −187.79，完全无意义；必须加 guard：Δ留存收益 ≥ 5% × 期初归母权益，否则置 NULL。③ 分母只含内部留存，分子却混入外部融资：靠增发、可转债转股堆出来的市值增…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：**高**
  - ⚠️ ROCE 同型问题：分母 Δ留存收益 对成熟派息公司天然趋零，现有 guard（Δ留存 ≥ 5%×期初归母权益）太松：过 guard 的 1,667 家里仍有 118 家 >10 倍、38 家 >20 倍、157 家 <−10、20 家 <−50，极值 +219.2 / −212.0——与 ROCE 事故完全同型的「分母趋零 → 量级荒谬」。另有两处已披露：分子含增发/可转债转股（外部融资被记成留存创造价值）、A+H 股本高估市值。
  - 🛠 修法：① guard 收紧为 Δ留存收益 ≥ max(5%×期初权益, 2%×期末市值)，或分母改用三年滚动平均 Δ留存；② 输出时对 |one_dollar|>10 置 NULL 并单列 n_dropped（现在已有 824 行因 guard 为 NULL，说明机制存在，只是阈值不够）；③ 把 dividend_interest_paid 的 match 从 missing 改为 approximate(2025 全空不可用)。

#### 12. 留存资本化率（经营口径增量资本回报）（Retained-Capital Capitalization Rate (Operating Incremental ROIC)）

- **提出者**：Charlie Munger, Lou Simpson, Bill Ruane
- **公式**：`RCR = [NOPAT_t − NOPAT_{t−N}] ÷ Σ留存收益; NOPAT = EBIT × (1 − 实际税率)，实际税率 = CLIP(income_tax ÷ total_profit, 0, 40%); Σ留存收益 = Δ(undistributed_profit + surplus_reserve); 对照口径 ROIIC = ΔNOPAT ÷ Δ投入资本，投入资本 = 归母权益 + 有息负债`
- **含义**：这是「一美元测试」的经营版：完全不看市场出价，只问每留存 1 元究竟换来多少增量税后经营利润。芒格、辛普森、鲁安挑公司的共同动作，就是先确认「这家公司把赚到的钱再投回去还能继续赚到高回报」，然后让它自己去复利——RCR 就是这个复利引擎的实测转速。RCR ≥ 1 表示留存收益能自我增值，公司具备内生的复利能力。
- **为什么超出 PE/PB**：PE/PB 无法回答「这家公司把利润留下来再投资，回报率高不高」，而这恰恰决定了十年后的 EPS 是多少。RCR 是判定「成长股值不值得为增长付溢价」的唯一硬证据：同样 20% 的利润增速，RCR = 15% 的公司是在真复利，RCR = 3% 的公司是在烧钱买增长。
- **字段映射**：exact 7 / approximate 1 / missing 1
  - △ `indicator_ext.ebit` ← EBIT（分子起点）：已实测等于 利润总额 + 财务费用（茅台 2025：1,147.55 + (−8.15) = 1,139.40）。差在财务费用是净额：净现金公司的负财务费用会把 EBIT 系统性压低（茅台 2025 低估 8.15 亿元，约 0.7%），净现金制造业可达 3-5%。
  - ✔ `income_statement.total_profit` ← 利润总额：用于计算实际税率，也用于剔除亏损年份。
  - ✔ `income_statement.income_tax` ← 所得税费用：实际税率 = income_tax / total_profit，必须 CLIP 到 [0, 40%]。
  - ✔ `balance_sheet.total_equity_parent` ← 投入资本：归母权益：ROIIC 口径的权益部分。
  - ✔ `balance_sheet.short_term_loans + balance_sheet.long_term_loans + balance_sheet.bonds_payable` ← 投入资本：有息负债：bonds_payable 大量 NULL 属正常（无债券），必须 COALESCE 0。该组合与 indicator_snapshot.interest_bearing_debt 定义一致。
  - ✔ `balance_sheet.undistributed_profit + balance_sheet.surplus_reserve` ← 留存收益（主口径分母）：两个科目相加才是完整留存收益；单取未分配利润会把提取盈余公积的年份误算成留存减少。
  - ✔ `balance_sheet.monetary_funds` ← 货币资金（净投入资本可选减项）：覆盖 99%+（2025 年报 5,578/5,625）。但用它做减项会让净现金公司的投入资本趋零甚至为负（茅台 2025：毛投入资本 2,446 亿，扣货币资金后 1,930 亿），ROIIC 分母爆炸——这是本项目已踩过的坑，因此主口径不减现金。
  - ✘ `income_statement.interest_expense` ← 利息费用（想精确算 EBIT 时）：语义不稳定（存在负值），且 2016 年报覆盖率仅 643/5,085 = 12.6%；无法用于跨年序列，只能放弃精确 EBIT，接受财务费用净额的近似。
  - ✔ `indicator_ext.free_cash_flow` ← 自由现金流（对照口径）：= cf_from_operating − capex，已实测对齐（茅台 2025：615.22 − 31.28 = 583.95）。但 capex 端只有 2015 年后可靠。
- **历史可用**：两个组成部分的历史深度不同：ΔNOPAT 端依赖 indicator_ext.ebit（三表主表可回溯 1990 年代、2007 年后每年 5,000+ 只）；分母 Δ留存收益 端只依赖资产负债表主表，同样自 1990 年代起可用、完全不依赖现金流表。因此十年窗口（5/10/15 年均可）对 5,071 只有 ≥10 个年报点的公司完全可做，是本流派里不亚于 ROE 的一个长期指标。若改用现金流表口径（FCF / capex）则受 ca…
- **主要陷阱**：① EBIT = 利润总额 + 财务费用，而财务费用是净额（利息费用 − 利息收入 + 汇兑 + 手续费），净现金公司的 EBIT 会被系统性低估：茅台 2025 财务费用 −8.15 亿元，EBIT 1,139.40 亿元 vs 利润总额 1,147.55 亿元。想精确必须换成 利润总额 + 利息费用，但 interest_expense 2016 年覆盖率仅 12.6% 且含负值，根本不可用，只能接受近似并标注。② 实际税率必须 CLIP 到 [0, 40%]：亏损年份 total_profit ≤ 0 时税率无意义，递延所得税冲回会造出负税率或 >100% 的税率，不 CLIP 会算出荒…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：**高**
  - ⚠️ 样本量：报告称「十年窗口对 5,071 只有 ≥10 个年报点的公司完全可做」。实测：2015 与 2025 两端都有年报的只有 4,363 只（两端 EBIT 齐全同样是 4,363 只）；再叠加 total_profit>0（两端都盈利 2,963 只）与 ic_end>ic_begin、re_end>re_begin 后，shipped SQL 实际只出 2,507 行。「数据可得」勉强成立，但把 5,071 当成实际样本量是高估（实际约…
  - ⚠️ ROCE 同型问题：① 分母只有 re_end>re_begin 的符号 guard，没有相对阈值：2,507 行里 11 家 RCR>2、2 家 >5，最大 +108.5、最小 −41.2，量级同型荒谬（一美元测试至少用了 5% 相对 guard，这里没有）。② 分子 NOPAT 是合并口径（EBIT 来自合并利润表 + 财务费用），分母 Δ留存收益 是归母口径，少数股东权益>25% 总权益的 290 家（万科 50.4%、保利发展 41.6%、中国建筑 40.2%）RCR 会被系统性抬高。③ WHERE total_profit>0 在端点年份静默剔除亏损公司（两端都盈利仅 2,963 只），并非随机缺失。
  - 🛠 修法：① 分母加相对 guard（Δ留存收益 ≥ 5%×期初归母权益，与一美元测试对齐），或对 |RCR|>10 置 NULL；② 分子改归母口径（parent_net_profit×(1−税率)）或并列输出「剔除少数股东」版本；③ 把 interest_expense 的 match 从 missing 改为 approximate(覆盖率 12.6%，不可用于跨年)；④ 删除「与 indicator_snapshot.interest_bearing_debt 定义一致」这句错述；⑤ 历史样本量从 5,071 更正为…


### 3.3 成长股学派（GARP / 成长质量）

**覆盖大师**：Philip Fisher、T. Rowe Price、Peter Lynch、William O'Neil

**本流派审查总评**：成长股学派是这批报告里『数值诚实度最高、陷阱设置却最自相矛盾』的一份。逐条证伪结果：① 字段层面零伪造——65 张表、6 段 formula_sql 实跑全部通过，无一处不存在的表名/列名（连 context.md 都没列的 xdxr、share_capital_history、stock_meta、csmar_risk_factors、csmar_disclosure_metrics 也真实存在）；② 六段 SQL 全部可执行且行数与宣称完全一致（PEG 1,738 / PEGY 1,738 / 分类器 5,419 / 费雪 4,413 / CANSLIM 4,142 / RS 5,403），无全 NULL、无全 0；③ 样本量宣称几乎逐位复现：PEG 中位 2.72、19.0%<1，PEGY 中位 2.27、23.9%<1，七类分布 1906/1443/684/589/416/263/118，费雪 5.08%/3.65%，CANSLIM 1,094/182/157，RS 中位 -7.37%——伪造样本量的嫌疑基本排除；④ 大量细节经得起查证：market_cap_at_report 与 close×总股本比值恒为 1.0000，basic_eps 与归母净利/期末股本比值中位 1.0007，xdxr.songzhuangu 对 300750 确为 0.8，dividends.d…

#### 13. PEG（市盈率 / 盈利增速比）（PEG Ratio (PE-to-Earnings-Growth)）

- **提出者**：Peter Lynch
- **公式**：`PEG = PE_FY ÷ (EPS 五年复合增速 × 100)；PE_FY = 报告日总市值 ÷ 该年归母净利润；EPS 复合增速必须先做送转复权：EPS_base_adj = EPS_base ÷ Π(1 + 每股送转比例)，CAGR = (EPS_now ÷ EPS_base_adj)^(1/5) − 1。过滤带：0.5% ≤ 增速 ≤ 100%，且基期/当期 EPS 与当期归母净利均 > 0`
- **含义**：林奇把「贵不贵」翻译成「为每一单位成长付了多少钱」：PEG≈1 表示市盈率与盈利增速相称，低于 0.75 才算便宜，高于 2 基本不碰。它是 T. Rowe Price 开创的成长股纪律（只买成长可被测量、且价格与成长匹配的公司）的可执行版本。分母刻意用 5 年复合增速而非最近一年，以过滤单年高增长的噪声。
- **为什么超出 PE/PB**：PE/PB 只回答「现在贵不贵」，PEG 回答「为这个增速付的溢价是否合理」——同样是 40 倍 PE，35% 增速与 8% 增速是两门完全不同的生意。
- **字段映射**：exact 3 / approximate 2 / missing 1
  - ✔ `indicator_ext.market_cap_at_report` ← PE 分子：报告日总市值：实测 1996-2025 每年对已上市公司覆盖率 100%；已内含当期股本，天然免疫送转与增发，比 close × shares 稳
  - ✔ `income_statement.parent_net_profit` ← PE 分母：年度归母净利润：必须用 month(report_date)=12 AND day(report_date)=31 过滤年报，report_type 是脏字段（None 占 114,812 行）
  - △ `income_statement.basic_eps` ← EPS 现期与基期：语义是每股收益，但库内按各期期末股本计算、未按后续送转重述；且 2006 年前几乎全空（2005 年只有 170 只）
  - ✔ `xdxr.songzhuangu` ← 送转复权因子：每股送转比例（300750 在 2023-04-26 为 0.8，即 10 转 8）；区间内取 product(1+songzhuangu)
  - ✘ `（库内无盈利预测/一致预期表）` ← 林奇原意：未来 3-5 年预期增速：这是 PEG 在 A 股最大的语义折扣：只能用已实现的历史增速替代预期增速
  - △ `income_statement.deducted_net_profit` ← 扣非口径并行版本：2025 年报 5,622/5,625 有值，但新披露期会不全（2025 中报仅 4,514/5,627）
- **历史可用**：PE 分子 1996-2025 共 30 个年报点且 100% 覆盖已上市；PEG 因需 5 年前基数，实际序列 2001-2025，2010 年后样本干净 → 完全可做「10 年历史中位数」。2025 年报实测：5,447 只已上市中有 1,738 只通过正负与增速带过滤，PEG 中位 2.72，约 19% 低于 1。
- **主要陷阱**：① 头号坑：basic_eps 不做送转重述。比亚迪(002594) 2020→2025 原始 EPS 复合增速仅 19.5%，按 xdxr 复权后 48.8%、净利润复合增速 50.4%；宁德时代(300750) 原始 45.3% vs 复权 63.4% vs 净利 66.9%。不复权会把 PEG 系统性高估 2-3 倍，而且专坑做了送转的成长股（正是本流派的目标池）。② DuckDB 里 power(负数, 小数) 返回 NaN 而不是 NULL，且 NaN >= 20 判定为 True——所有『增速 ≥ X』分支必须先显式写 eps_base>0 AND eps_now>0 AND pn…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：**高**
  - ⚠️ ROCE 同型问题：三条与 ROCE 事故同构的风险：① 分母趋零——PEG 分母是『增速百分点』，交付 SQL 允许低至 0.5%，此时 PEG ≈ 2×PE，任何 PE>5 的低增速股都自动 PEG>10；这正是『比率出现荒谬量级时应先怀疑分母口径、而不是加护栏』的翻版，而本报告恰恰选择了加护栏（0.5% 下限 + 100% 上限）。② PE 分母被一次性收益污染——000656 靠 658.96 亿重组投资收益做出 PE 0.52 倍，SQL 无扣非/非经常性损益约束。③ 分子用市值/归母净利、增速却用 EPS，只有送转被复权，增发/配股/回购造成的股本变动未修正——1,738 只里 563 只（32.4%…
  - 🛠 修法：① 增速带下限从 0.5% 提到 ≥5%（或对 PEG 做稳健分位截尾/winsorize）——实测 0.5% 下限下仍有 16.7% 样本 PEG>10；② 并行输出扣非 PEG（deducted_net_profit 2025 年报覆盖 5,622/5,625），并剔除 PE<2 的样本（实测仅 1 只：000656 PE=0.52）；③ 用 funding_events.issue_shares 补增发股本变动，消除 32.4% 样本的 EPS/净利背离；④ 删掉被证伪的『中芯国际 3.1% → PEG 63…

#### 14. PEGY（股息调整后的 PEG）（PEGY (PEG + Dividend Yield)）

- **提出者**：Peter Lynch
- **公式**：`PEGY = PE_FY ÷ (EPS 五年复合增速% + 滚动 12 个月股息率%)；股息率 = Σ_{ex_date ∈ (T−12M, T]} (dividend_per_share × 除权日总股本) ÷ 报告日总市值`
- **含义**：现金分红本身就是股东已经拿到手的增长，所以林奇把股息率直接加进 PEG 的分母。这是给「增长慢但现金回报高」的公司准备的尺子：公用事业、高速公路、银行、家电龙头。PEGY<1 意味着「增速+股息」的合计回报率已经超过市场付出的市盈率。
- **为什么超出 PE/PB**：PB 低 + 分红高不等于划算，也分不清「低增长且慷慨分红」和「低增长且一毛不拔」；PEGY 把股东实际收到的现金并入增长，把这两类公司分开。
- **字段映射**：exact 3 / approximate 1 / missing 1
  - ✔ `dividends.dividend_per_share` ← 每股现金分红：已折算为每股；可用 ex_date 做滚动 12 个月窗口（1991-2026，5,543 只）
  - ✔ `share_capital_history.total_shares` ← 除权日股本：按 effective_date<=ex_date 取最后一条，避免送转后股本错配
  - ✔ `indicator_ext.market_cap_at_report` ← 总市值分母：与 PEG 同一口径，保证两个指标可比
  - △ `indicator_snapshot.ttm_dividend_yield` ← 现成股息率字段：只有最新一期快照（5,573 行、单一报告期），无法回溯历史，只能做交叉校验
  - ✘ `cash_flow_activity.dividend_interest_paid` ← 分红现金流口径：2025 年报该字段全空，不能用来反推分红总额
- **历史可用**：分红数据 1991-2026，但 2012 年前每年只有约 1,600 只有分红记录、早年送转频繁，滚动 12 个月口径 2010 年后才稳定 → 可做 10-15 年历史中位数。2025 年报实测：1,738 只有效样本，PEGY 中位 2.27、23.9% 低于 1（比 PEG 的 19.0% 多判出约 5 个百分点的「便宜」，正是高股息慢增长股被 PEG 错杀的部分）。
- **主要陷阱**：① A 股年报分红除权发生在次年 5-7 月：按会计年度归属会把股息挂到错误年份，必须用 ex_date 落在过去 12 个月的滚动口径，不能用 report_date 归属。② dividend_per_share 是除权日当时的每股口径，配股本要用 share_capital_history 里 effective_date<=ex_date 的最后一条，否则送转公司股息率会错 1-2 倍。③ indicator_snapshot.ttm_dividend_yield 只有最新一期，做历史分位必须自建序列。④ 特别分红会一次性抬高滚动股息率（例如 600519 在 2025-12-19 派…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：**高**
  - ⚠️ ROCE 同型问题：与 PEG 同源的分母趋零风险：PEGY 分母 = g_pct + dy_pct，两者都可接近 0（增速下限仍是 0.5%）。SQL 虽加了 (g+DY)>0 保护，但 (g+DY) 只要 0.6% 就能让 PE 20 倍的公司算出 PEGY 33 倍，属『加护栏而非修分母』。股息率侧口径本身健康（分子=每股派息×除权日股本，分母=报告日总市值，同为元/元）。另需注意 dividends 表含未来除权日（max ex_date=2026-11-26，晚于当前 2026-11-16），做历史截面时若时间窗写错会引入未来函数，现 SQL 窗口封在 2025-12-31，未踩坑。
  - 🛠 修法：① 与 PEG 同步把增速下限提到 ≥5%，或对 (g+DY)<2% 的样本直接不排名；② dy 增加『剔除一次性特别分红』处理（用滚动股息率 3 年中位数替代当期值），降低 600519 这类单次派息造成的假性便宜；③ 修正 note 两处事实：indicator_snapshot 实为 5,575 行 / 2 个报告期 / ttm_dividend_yield 非空 3,734 行；④ 历史可用性下调：受 EPS 基期约束实际只有 11 个年报点（2015-2025），做不到 10-15 年。

#### 15. 林奇六类股票分类器（含周期股标记）（Lynch Stock Categorization (Six Buckets)）

- **提出者**：Peter Lynch
- **公式**：`按优先级阶梯分类：turnaround（基期亏损、当期转盈）→ distressed_loss（当期仍亏）→ asset_play（净现金/市值 ≥ 0.5）→ fast_grower（4 年净利 CAGR ≥ 20%）→ stalwart（10%~20%）→ slow_grower（0~10%）→ declining（<0）；另配并列标记 cyclical_flag = 1 当 5 年净利变异系数 ≥ 1.0 或亏损年数 ≥ 2`
- **含义**：林奇的方法论核心不是一条选股公式，而是先把股票装进不同抽屉：快增长股、稳健股(stalwart)、慢增长股、周期股、困境反转股、资产股。同一个 PE 在不同抽屉里的含义完全相反，分类本身就是让估值指标知道自己该不该被相信。这个 SQL 把抽屉做成可算的规则阶梯，并额外输出周期股标记供后续叠加。
- **为什么超出 PE/PB**：PE/PB 对所有公司用同一把尺，而周期股在 PE 最低时最危险（盈利高点）、资产股在 PE 最高时最安全；先分类再估值，才不会被同一张市盈率排行榜误导。
- **字段映射**：exact 3 / approximate 2 / missing 1
  - ✔ `income_statement.parent_net_profit` ← 4 年净利润复合增速：取 2021 与 2025 两个年报点，power 前必须判基期>0，否则 NaN 冒充通过
  - ✔ `indicator_ext.market_cap_at_report` ← 市值（区分快增长股/大型稳健股）：林奇原意用市值分档，阈值需按 A 股实际分布重设（本 SQL 用 500 亿/300 亿）
  - ✔ `dividends.dividend_per_share × share_capital_history.total_shares` ← 股息率：同 PEGY，滚动 12 个月口径
  - △ `balance_sheet.monetary_funds + trading_financial_assets − short_term_loans − long_term_loans − bonds_payable` ← 净现金（资产股）：库内无投资性房地产/开发支出，资产股只能抓「净现金型」，抓不到「隐蔽资产型」
  - △ `csmar_risk_factors.profits_volatility_3y` ← 周期股判定：净利波动：仅 2000-2024、量级很小（中位 0.022、p90 0.08），只能用分位数设阈值；本 SQL 默认改用库内自算 5 年净利变异系数
  - ✘ `（无订单/产能/产品价格数据）` ← 困境反转的判定依据（经营是否真改善）：林奇要求确认基本面真的转向，库内只能看到财务结果，看不到驱动因素
- **历史可用**：分类所需的 4-5 年利润史 1996 年起可算，股息率 2010 年后稳定，净现金项 1996 年起可得 → 2010-2025 共 16 个年报点可稳定回溯，可做 10 年历史分布；csmar_risk_factors 只到 2024，用库内自算的 5 年净利变异系数即可全覆盖。2025 年报实测分布：declining 1,906 / distressed_loss 1,443 / slow_grower 684 / fast_g…
- **主要陷阱**：① power(负数, 1/4) = NaN，而 NaN 在 DuckDB 里比较恒为 True：不做 pnp>0 保护，亏损股会被误判进 fast_grower（本 SQL 已用 CASE 显式判空，改阈值时务必保留）。② 净现金口径残缺：库内没有投资性房地产、开发支出、资源储量，林奇意义的「资产股」在 A 股只能退化为「类净-net 现金股」，0.5 这个阈值不是林奇原意。③ 金融股没有流动/非流动划分，净现金公式对 99 只金融股必然为空，分类前应剔除 sw_level1 ∈ {银行, 非银金融}（上下文已记录金融股陷阱）。④ csmar_risk_factors.profits_vol…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：**高**
  - ⚠️ ROCE 同型问题：与 ROCE 同构的『分母口径错、再用阈值兜底』：asset_play 的判定分母是市值（可信），但分子净现金对金融股与零负债公司系统性虚高，且 coalesce(NULL,0) 使『没借过钱』与『数据缺失』不可区分；结果是 asset_play 抽屉一半装的是银行/券商（券商客户资金存款被算进 monetary_funds），这个抽屉在语义上已经失效。g_pct 用 4 年净利 CAGR（分子分母同为归母净利，量纲一致，无单位错配），跨年口径亦无断点。净现金只减长短期借款与应付债券，未减租赁负债 lease_liabilities（2021 年后体量可观），属次要低估。
  - 🛠 修法：① 按自己写的 trap ③ 落地：WHERE sw_level1 NOT IN ('银行','非银金融')（实测这两个行业共 121 只）；② 净现金改为 CASE WHEN short_term_loans IS NULL AND long_term_loans IS NULL AND bonds_payable IS NULL THEN NULL ELSE coalesce(...) END，别把 824 只零负债/缺数股票当成『净现金充裕』；③ 补减 lease_liabilities；④ 修正 p90 为…

#### 16. 研发强度与销售组织效率（费雪十五要点中可算的部分）（R&D Intensity & Sales-Organization Efficiency）

- **提出者**：Philip Fisher
- **公式**：`研发强度 = rd_expenses ÷ revenue（取 2021-2025 五年中位数，并用 2023-2025 中位数 − 2018-2020 中位数看趋势）；销售组织效率 = (selling_expenses + administrative_expenses) ÷ revenue（库内即 indicator_ext.selling_expense_ratio + admin_expense_ratio，均为小数）；佐证项 = 毛利率 (revenue − cost_of_revenue)/revenue 的五年中位数与趋势`
- **含义**：费雪第 1、2、3 要点分别是「产品市场潜力」「管理层是否决心开发新工艺」「研发相对于公司规模有多大」，落到财报上就是研发费用率与它的趋势；他反复强调「销售组织效率」决定了研发成果能否变成收入，对应销售+管理费用率与毛利率的组合。本指标要求研发强度上升的同时毛利率不塌——这才是费雪说的「研发转化为利润」。
- **为什么超出 PE/PB**：PE/PB 看不见「公司是否在为三年后的产品付钱」，研发强度趋势 + 毛利率趋势把成长股的「投入侧」和「变现侧」同时放进视野，能筛掉靠砍研发做短期利润的伪成长。
- **字段映射**：exact 5 / approximate 0 / missing 2
  - ✔ `income_statement.rd_expenses` ← 研发费用：2015 年后才逐步有数（2015 年 86 只、2016 年 627 只、2017 年 3,932 只、2018 年后 5,100+）
  - ✔ `indicator_ext.rd_expense_ratio` ← 研发费用率：实测与 rd_expenses/revenue 完全一致（差值 0），但单位是小数不是百分数
  - ✔ `indicator_ext.selling_expense_ratio` ← 销售费用率：实测等于 selling_expenses/revenue，同为小数
  - ✔ `indicator_ext.admin_expense_ratio` ← 管理费用率：2017 年前包含研发费用，构成结构断点
  - ✔ `income_statement.revenue − cost_of_revenue（自算）` ← 毛利率：indicator_ext 无 gross_margin 列，indicator_snapshot.gross_margin 只有最新一期
  - ✘ `（balance_sheet 无开发支出字段）` ← 资本化研发（开发支出）：rd_expenses 只含费用化部分，创新药/军工等资本化研发公司强度被低估
  - ✘ `（无对应结构化数据）` ← 费雪的定性要点（管理层深度、销售队伍质量、劳资关系）：company_profile 只有文本简介，无法量化
- **历史可用**：研发费用科目 2015 年后才逐步有数，且 2017-2018 是「从管理费用剥离」的准则切换 → 研发强度序列只有 2018-2025 共 8 个年报点，仅能做 5 年中位数（2022 年起）与 3 年趋势，不可做 10 年历史中位数。销售费用率与毛利率可回溯至 1996 年，可做 10 年。2025 年报实测：4,413 只满足「至少 4 年有研发数据且 5 年研发强度中位 >2%」，研发强度中位 5.08%、销售费用率中位 3.6…
- **主要陷阱**：① 2017-2018 准则切换：管理费用率中位数 2016 年 9.43% → 2017 年 6.70% → 2018 年 6.21%，同期研发费用填报数从 2016 年 627 只跳到 2018 年 5,128 只。任何跨越 2017 年的「研发强度趋势」都是假的。② 2020 新收入准则把运输费/装卸费从销售费用移入营业成本：销售费用率中位数 2019 年 4.82% → 2020 年 3.41%，跨越 2020 年的「销售组织效率」比较同样失效（这是本项目尚未记录的新坑）。③ 研发费用只含费用化部分，资本化开发支出库内无字段，重研发资本化的公司（创新药、军工、部分半导体）强度被低估。④…
- **审查裁决**：**✅ 通过**　ROCE 同型风险：低
  - 🛠 修法：维持现状即可，仅两处加固：① 毛利率 CTE 补 cost_of_revenue>0 的显式判断（当前只有 IS NOT NULL），避免成本为 0 的极端样本把毛利率顶到 100%；② rd_trend_pp 用 2023-2025 中位减 2018-2020 中位，中间空掉 2021-2022，建议改为 Theil-Sen 斜率或 2021-2025 线性斜率，口径更干净。

#### 17. CANSLIM 盈利引擎（C 季度加速 + A 年度复利）（CANSLIM: Current Quarterly Earnings + Annual Earnings Growth）

- **提出者**：William O'Neil
- **公式**：`单季归母净利 = 本期累计 − 上年同期累计（利润表是 YTD 累计制）；C：最近单季净利同比增速 ≥ 25% 且单季净利 > 0；盈利加速：g(Q3) > g(Q2) > g(Q1) > 0（连续三季增速抬升）；A：3 年每股收益复合增速 ≥ 25%（经 xdxr 送转复权）且 ROE ≥ 17%（归母净利 ÷ 平均归母权益）`
- **含义**：欧奈尔把盈利拆成两问：最近一个季度是否在加速（C），以及过去三年是不是一台稳定复利机器（A）。只过 C 是事件驱动，只过 A 是白马股，两条都过才是他大牛股样本里最稳定的共同特征。盈利加速（g 逐季抬升）是 C 的加强版，用于捕捉「拐点刚出现、市场还没定价」的样本。
- **为什么超出 PE/PB**：PE/PB 都是年度静态量，看不见「最近两个季度增速连续抬升」这种边际变化——而这恰恰是大牛股启动时唯一领先于股价的东西。
- **字段映射**：exact 3 / approximate 4 / missing 1
  - ✔ `income_statement.parent_net_profit` ← 累计归母净利润（差分出单季）：按 (stock_code, 年度) 做 lag 差分；2026 年已有 Q1/H1/Q3 三期
  - △ `income_statement.deducted_net_profit` ← 扣非净利润（更干净的 C 口径）：2025 中报仅 4,514/5,627 只填报，最新一季用它会系统性缺样本
  - △ `income_statement.basic_eps` ← 每股收益（年度 A 档）：未做送转重述，必须乘 xdxr 复权因子，否则 A 档全错
  - ✔ `xdxr.songzhuangu` ← 送转复权因子：取 event_date ∈ (FY_base, FY_now] 的 product(1+songzhuangu)
  - ✔ `income_statement.parent_net_profit ÷ ((balance_sheet.total_equity_parent 本期 + 上期)/2)` ← ROE 分子分母：自算平均归母权益；比 snapshot.roe（仅最新一期）可回溯得多
  - △ `csmar_disclosure_metrics.roe_weighted` ← 加权 ROE 交叉校验：更专业但只到 2024（2025 年仅有 2025-03-31 一期）；roe_weighted_deducted 覆盖更差（2016 年 6,281/12,211）
  - ✘ `（库内无基金持仓/机构持股表）` ← 机构持股变动（CANSLIM 的 I）：CANSLIM 的 I 完全不可算
  - △ `index_valuation.pe_ttm / price_daily_qfq 指数序列` ← 市场方向（CANSLIM 的 M）：只能用指数价格与均线自建，库内无官方 follow-through day 标记
- **历史可用**：利润表 2000 年起每年 4 个报告期（1998-1999 只有 3 期），单季同比需 t−4 期 → 2001 年起可算；basic_eps 2006 年前几乎全空（2005 年 170 只）→ 年度 A 档 2006-2025 共 20 年，季度口径 2001-2026Q3。完全可做 10 年历史序列。2026Q3 实测：4,142 只有同比、1,094 只 C 过关、182 只连续三季加速、157 只 A 过关。
- **主要陷阱**：① 利润表是累计制（YTD），必须先按 (stock_code, 年度) 差分才是单季：300750 2025 年累计 139.6/304.9/490.3/722.0 亿 → 单季 139.6/165.2/185.5/231.7 亿。直接拿累计值算同比毫无意义。② Q4 单季 = 全年 − 三季报，含年度审计调整与减值计提，波动最大；做加速判断建议只用 Q1-Q3。③ 送转让 basic_eps 年度间不可比：300750 2023 年 10 转 8 后，2024Q1 EPS 2.39 vs 2023Q1 4.03 = −41%，而同期净利实际 +7%。A 档必须走 xdxr 复权（复权后 3…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：**高**
  - ⚠️ match 分级：8 个 db_field 里 7 个真实、1 个是语义空壳。真实的：income_statement.parent_net_profit / deducted_net_profit / basic_eps、xdxr.songzhuangu、balance_sheet.total_equity_parent、csmar_disclosure_metrics.roe_weighted（该表 13 列中确有；『2025 年只有 2025-03-31 一期』实测为真：243,202 行、73 个报告期、max=2025-0…
  - ⚠️ ROCE 同型问题：典型的『分母趋零未设上限』：C 档用单季归母净利同比 g3>=25 判定，g_yoy 无任何上限。实测 2026Q3 全市场 5,575 个单季同比 p95=332.7%、max=50,526.4%；1,094 只 c_pass 里 139 只（12.7%）同比 ≥500%，是基期利润趋零造成的假加速，与 ROCE 事故同一病理（先怀疑分母，别加护栏）。A 档反而干净：power 前有 eps_base>0 AND eps_now>0 显式保护；ROE 分母用 (本期+上期)/2 归母权益且 eq_prev>0；单位统一为小数（0.17 阈值与 roe_avg_eq 的 0.0569 同量纲）…
  - 🛠 修法：① 给单季同比加双边约束（如 25% <= g3 <= 300%）并要求单季归母净利绝对值 > 约 2,000 万，可一次性清掉 139 只（12.7%）基期趋零的假加速；② 增加『Q1..Q3 三个累计期齐全』的完整性校验，修掉 131 只（2.3%）因缺季导致的单季差分污染；③ 把 M 项 db_field 改为真实路径 index_valuation.extra->>'index_close'（43 个申万指数 + 000300，2006/2010→2026），或老实退回 missing——现在的 'pric…

#### 18. 相对强度 RS 评级（含 52 周高点距离）（Relative Strength Rating (IBD-style)）

- **提出者**：William O'Neil
- **公式**：`RS_raw = 0.4×(P0/P_3M − 1) + 0.2×(P0/P_6M − 1) + 0.2×(P0/P_9M − 1) + 0.2×(P0/P_12M − 1)；RS 评级 = ntile(99) 全市场分位（1-99，越接近 99 越强）；52 周高点距离 = P0 ÷ max(近 250 个交易日收盘) − 1`
- **含义**：欧奈尔认为股价的相对强弱本身就是基本面（盈利加速 + 行业景气）被市场投票的结果，按最近一季度 40%、前三个季度各 20% 加权，能同时抓「最近加速」与「一年维度领涨」。52 周高点距离是它的补充：真正的领涨股往往贴着历史新高运行，而不是深跌后反弹。
- **为什么超出 PE/PB**：PE/PB 是估值与净资产的历史值，完全不看价格自身的动量结构；RS 是唯一把「市场已经在为什么付钱」变成数字的成长股指标，常与 CANSLIM 的 C/A 交叉验证。
- **字段映射**：exact 3 / approximate 2 / missing 0
  - ✔ `price_daily_qfq.close / trade_date` ← 复权收盘价序列：1990-12-19→2026-11-16，5,585 只，17,543,770 行；必须用 qfq 不能用 raw
  - △ `price_daily_qfq.close 的近 250 交易日最大值` ← 52 周高点：用收盘价近似最高价；严格应用 high，但 high 会受单日异常影响
  - ✔ `stock_meta.listing_date / is_st / is_suspended` ← 全市场横截面（分位分母）：分位必须在同一交易日的完整横截面上算，并剔除 ST 与退市整理期
  - △ `index_valuation（仅指数估值）/ price_daily_qfq 指数序列` ← 相对强度基准（行业/指数）：库内无现成的申万行业指数价格，行业相对强度需自建
  - ✔ `price_daily_raw.volume / turnover_rate` ← 成交量放大（O'Neil 的突破确认）：可另做 50 日均量比的辅助条件
- **历史可用**：价格数据 1990-12-19 起，但 250 交易日窗口 + 全市场分位需要足够的市场宽度 → 1996 年后才有意义；2005 年起每年 1,200+ 只、2015 年起 2,500+、2025 年 5,450 只 → 可做 15-20 年历史序列，完全支持「历史分位中位数」。2026-09-30 实测 5,403 只可算，RS_raw 中位 −7.4%（熊市），评级 1-99 全覆盖。
- **主要陷阱**：① 必须用 price_daily_qfq；用 price_daily_raw 会把每次分红除权当成下跌。同一天 qfq 与原始价可以差很多（600519 在 2019-12-31：report_date_close 1183.00 vs qfq 1001.27，差 18%）。② 12 月 31 日常常不是交易日（2018-12-31 休市、2022-12-31 与 2023-12-31 是周末），实测 2016-2025 这 10 个年报点里有 5 个（2016/2017/2018/2022/2023）不是交易日，`join price_daily_qfq on trade_date = r…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ match 分级：5 个 db_field 中 4 个真实、1 个空壳、1 个描述与实现不符。真实的：price_daily_qfq.close/trade_date 标 exact 属实且覆盖率描述精确到个位——实测 17,543,770 行、5,585 只、1990-12-19→2026-11-16，与文中『17,543,770 行、5,585 只』完全一致；price_daily_raw.volume/turnover_rate 存在；stock_meta.listing_date/is_st/is_suspended 存在（…
  - 🛠 修法：① hi_52w 改为 max(CASE WHEN rn<=250 THEN close END)，或直接用 high 列取近 250 日 max——实测 27.1% 的股票会受影响、最大偏差 26.5pp；② db_field 的『price_daily_qfq 指数序列』换成 index_valuation.extra->>'index_close'（43 个申万指数、2006/2010→2026），或标 missing，并修正『库内无现成的申万行业指数价格』这句与事实相反的判断；③ 落地 note 自己的要求…


### 3.4 量化价值 / 神奇公式流派

**覆盖大师**：Joel Greenblatt、John Neff、David Dreman、James O'Shaughnessy、Eugene Fama & Kenneth French、Cliff Asness

**本流派审查总评**：逐条证伪结果：6/6 指标的字段全部真实存在（57 个引用零伪造），6/6 的 formula_sql 全部可执行且返回行级结果（3,790 / 2,695 / 1,629 / 334 / 5,190 / 5,193），没有一条全 NULL 或全 0。所以本文件不是『编字段骗人』那类，ROCE 事故里最恶劣的 P0 形态没有重演，分母为负的 219 只式灾难也没有出现——加总类公式的会计恒等式这次基本是对的（roce_history.ce ≡ TA−CL 实测 105,168/105,168 全等，indicator_ext.accruals ≡ (net_profit−OCF)/TA 实测 349,369/349,369 全等）。真正的问题在三处诚实性与一处口径：（1）神奇公式把 TA−CL 标成 exact 去冒充 Greenblatt 的『净营运资本+净固定资产』，实测中位 PPE/CE 只有 0.258，是 ROCE 同款『广义当狭义』的标注模式，只是方向保守不爆表；（2）三处关键样本量对不上——聂夫 3,251 实为 2,695、德雷曼 3,274 实为 1,629、FF5 的 RMW 覆盖率整段是 FY2025 的数据；（3）历史可用性被高估——market_cap_at_report 在 FY2010–FY2019 只有 52%~66% 覆盖且系统性剔除小盘（FY20…

#### 19. 神奇公式（资本回报率 × 盈利收益率 双排名合成）（Magic Formula (ROC & Earnings Yield dual-rank composite)）

- **提出者**：Joel Greenblatt
- **公式**：`ROC = EBIT ÷ (净营运资本 + 净固定资产) = EBIT ÷ (总资产 − 流动负债)；EY = EBIT ÷ EV，其中 EV = 股权市值 + 有息负债 + 少数股东权益 − 货币资金；MagicRank = rank(ROC, 降序) + rank(EY, 降序)，数值越小越好。`
- **含义**：Greenblatt 认为高资本回报的好生意和便宜的价钱极少同时出现，所以把两个绝对量指标的截面排名相加，用排名抵消行业之间估值中枢的差异，只买双高分的一篮子股票，持有约一年后换仓。它刻意不用 ROE，因为 ROE 奖励高杠杆，而 ROC 用 EBIT 除以全部已动用资本，把负债与税都还原掉。
- **为什么超出 PE/PB**：PE 只看股东盈余、PB 只看账面净资产，都不回答「这门生意用多少资本赚回多少钱」。ROC 直接度量资本效率，把重资产低回报公司和高周转公司分开；EY 用 EV 而非市值给企业整体定价，不会因为资本结构不同而误判贵贱。
- **字段映射**：exact 5 / approximate 3 / missing 0
  - △ `income_statement.total_profit + income_statement.financial_expenses（已落成 indicator_ext.ebit，实测 62,286/62,347 完全一致）` ← EBIT（两腿共同分子）：只能把「财务费用净额」整体加回，而它是利息费用−利息收入+汇兑+手续费。净现金公司该项为负，EBIT 会被系统性低估（context 陷阱 2）。严格 EBIT 只该加回利息费用，但 interest_expense 有负值且 2016 年覆盖仅 12.6%，不可用。
  - ✔ `balance_sheet.total_assets - balance_sheet.total_current_liabilities` ← 已动用资本 CE = 净营运资本 + 净固定资产（ROC 分母）：会计恒等式：(流动资产−流动负债)+非流动资产 ≡ 总资产−流动负债。即本项目 ROCE 域 2026-11-16 裁决的口径 C，依据见 app/core/indicators/roce.py：直接取 fixed_assets(PPE) 会漏掉油气资产/在建工程/无形资产，中国海油因此算出 119…
  - ✔ `indicator_ext.market_cap_at_report` ← 股权市值（EY 的股票部分）：= 报告日原始收盘价 × share_capital_history 中 effective_date<=报告日 的时点股本。绝不可用 stock_meta.total_shares（当前股本），否则历史期市值被后期增发污染。
  - △ `balance_sheet.short_term_loans + balance_sheet.long_term_loans + balance_sheet.bonds_payable` ← 有息负债：缺三项：①一年内到期的非流动负债 balance_sheet_ext.non_current_liab_due_1y 实测只有 2025 年报有值（5,165/5,477），历史全空；②租赁负债 balance_sheet.lease_liabilities 未计入；③应付票据未计入。方向一致：系统…
  - △ `balance_sheet.monetary_funds` ← 货币资金（EY 的现金扣减项）：未扣 trading_financial_assets（交易性金融资产，A 股常是大额银行理财，2025 年报 3,124 只有值）。少扣这块会让 EV 偏大、EY 偏小，与上一条方向相反，两者部分对冲。
  - ✔ `balance_sheet.minority_interest` ← 少数股东权益：COALESCE 0；NULL 表示公司本来没有，不是缺数据。
  - ✔ `stock_meta.sw_level1 NOT IN ('银行','非银金融','公用事业')` ← 行业剔除（金融、公用事业）：实测 FY2024 剔除后 total_current_liabilities 非空率 5,644/5,644 = 100%，CE 定义对全样本成立。必须用 sw_level1；csrc_l1='金融业' 混进了同花顺、指南针、大智慧等软件公司（context 陷阱 8）。
  - ✔ `roce_history.roce + indicator_snapshot.roce_5y_median / roce_10y_median` ← 历史 ROC 中位数（历史可达性复用）：roce_history.ce 就是 total_assets−total_current_liabilities，与 Magic Formula 分母完全同源，只是用 (ce+ce_prev)/2 的平均资本，比 Greenblatt 的期末资本略保守（茅台 FY2024：平均法 50.67% v…
- **历史可用**：ROC 腿：roce_history 1991–2025 连续 36 年，2011 年后每年 2,600–5,800 只有效，**可直接做 5 年 / 10 年历史中位数**，且 indicator_snapshot 已有 roce_5y_median / roce_5y_years / roce_10y_median / roce_10y_years，无需重算。EY 腿：受 market_cap_at_report 约束，1998 年…
- **主要陷阱**：①EV→0 爆炸（最致命）：货币资金接近或超过市值时，靠有息负债撑起的 EV 会塌到 1 亿量级。实测 FY2024 中钢国际市值 91.4 亿、货币资金 100.5 亿、有息负债 7.0 亿、少数股东权益 3.2 亿 → EV = 1.14 亿 → EY = 1063%，直接登顶排行榜。必须加 |EY|≤1.0 闸门。②同一控制下企业合并的追溯重述：上年报表被重述成收购后规模，但 market_cap_at_report 仍是重述前的股本×价格。002379 宏桥控股 FY2024 营收 26.9 亿→1,503 亿（55.9 倍）、总资产 36.2 倍，市值却只有 102 亿 → EY =…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ match 分级：唯一硬伤：CE = balance_sheet.total_assets − total_current_liabilities 被标为 exact，但文件自己的 formula 写的是 Greenblatt 的「净营运资本 + 净固定资产」。TA−CL ≡ 净营运资本 + 全部长期资产（含商誉、无形、长期股权投资、在建工程、使用权资产、递延税），实测 FY2024 剔金融公用 5,644 只中位 NCA/CE=0.588、PPE/CE 仅 0.258，其中 3,208 只（57%）PPE/CE<30%——「净固定…
  - 🛠 修法：①把 CE 的 match 由 exact 改为 approximate，并把 formula 文本改成「已动用资本 = 总资产 − 流动负债（本项目 ROCE 口径 C，≠ Greenblatt 原文的净营运资本+净固定资产）」；②删掉/重测「461 只」（实测 99 只 / 含腰斩 241 只）；③把 EBIT 一致性表述改为实测 356,194/361,301（98.6%），并说明差额来自 1995–1997 年报单位取整；④加营收同比兜底以剔除 002379 类重述股；⑤EY 闸门从 1.0 收到 0.4，…

#### 20. 聂夫总回报率（低 PE + 高股息 + 盈利增长）（Neff Total Return Ratio (growth + dividend yield over P/E)）

- **提出者**：John Neff
- **公式**：`TTR = (盈利年化增长率 % + 近 12 个月股息率 %) ÷ PE；配套纪律：PE ≤ 行业 PE 中位数的 0.7 倍、股息率 ≥ 市场平均、连续分红 ≥ 5 年。TTR ≥ 2 视为合格。`
- **含义**：聂夫把「便宜」拆成可兑现的两条腿：估值便宜（低 PE）和持有期间真金白银到手（股息），再加企业自身长大（增长）。三者相除得到一个「单位估值能换回多少年化收益」的比率，本质是自建一个粗粒度的预期收益率，用来跨行业横向比较。这是他执掌温莎基金 31 年跑赢标普的核心公式。
- **为什么超出 PE/PB**：单看 PE 会把「便宜但零回报、也不增长」的价值陷阱买进来。TTR 要求低 PE 必须同时有股息或增长做背书，等于给低估值加了一道「为什么便宜还能赚钱」的检验；PB 完全不参与，避免把轻资产的优秀公司误判为贵。
- **字段映射**：exact 5 / approximate 1 / missing 0
  - ✔ `indicator_ext.market_cap_at_report / income_statement.parent_net_profit` ← 估值（PE）：年报口径 PE。实测 FY2024 剔金融后 3,250 只可算。
  - ✔ `SUM(dividends.dividend_per_share)（ex_date 落在评估日前 12 个月）/ indicator_ext.report_date_close` ← 股息率：窗口按 ex_date 划，不按 announcement_date——2024 年报的分红常在 2025 年 6 月才除权，按公告日划窗会把它算进 2024。
  - △ `income_statement.parent_net_profit 四年年化复合增速` ← 盈利增长率：聂夫用的是分析师对未来 5–7 年的预期增长率，本地库没有一致预期数据，只能用已实现的历史 CAGR 替代，天然滞后一到两年，且会被周期股谷底反弹严重放大。
  - ✔ `indicator_ext.report_date_close（名义收盘价）/ price_daily_raw.close` ← 价格（股息率分母）：必须与 dividends.dividend_per_share 的名义口径配对。dividends 的每股派息是「已折算为每股」的名义值，未经复权。
  - ✔ `indicator_snapshot.consecutive_div_years` ← 连续分红年数：快照口径，只有最新一期；做历史回溯需用 dividends 逐年重算。
  - ✔ `median(...) OVER (PARTITION BY stock_meta.sw_level1)` ← 行业 PE 中位数：用 sw_level1，不要用 csrc_l1。
- **历史可用**：分红可回溯到 1992 年（2024 年 4,063 只股票发生分红事件、2025 年 3,861 只），利润表 1990 年起，价格 1990-12-19 起；market_cap_at_report 2010 年后才稳定（FY2024 非金融 5,075/5,644 = 89.9% 有值）。FY2024 满足「归母净利>0 + 当年有分红 + 有市值」的样本 3,251 只。5 年中位数需要 4 年增长历史，实际起点约 1996 年…
- **主要陷阱**：①历史 CAGR 会把周期谷底反弹当成成长：云天化 2020→2024 归母净利 CAGR 高达 103.8%，TTR 算出 14.13（聂夫合格线才 2），整套排序被周期股霸榜。必须把 g 截断到 [0,25]%（SQL 已用 LEAST/GREATEST），并只取连续盈利年份。②股息率的价格口径：dividends.dividend_per_share 是名义每股分红，若拿 price_daily_qfq 复权价当分母，高送转股票（10 转 10 会让复权价只剩一半）股息率会翻倍虚高。必须用 price_daily_raw 或 indicator_ext.report_date_close…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ 样本量：核心样本数对不上：报告称「FY2024 满足『归母净利>0 + 当年有分红 + 有市值』的样本 3,251 只」，但脚本实际只产出 2,695 只（base 内 INNER JOIN 2020 年报 + 外层 WHERE pe>0 AND pe<80 AND dy_pct>0 之后）。按报告措辞反推各口径：归母净利>0 = 4,345；再加有市值 = 3,874；再加窗口内有分红 = 3,427；三者同时满足 = 3,355——没有任何…
  - 🛠 修法：①把「样本 3,251 只」改成实测 2,695，并列出漏斗（净利>0 4,345 → 有市值 3,874 → 有分红 3,427 → 两期齐全且有分红 3,355 → 最终输出 2,695）；②外层加市值交叉校验 `AND abs(market_cap_at_report/(close*shares)-1)<0.05`，或把 689009 列入脏数据黑名单，否则 TTR 永远由脏数据霸榜；③公式写明「PE≤0.7×行业中位数、股息率≥市场平均」是配套纪律，但 SQL 只算 pe_vs_ind 并不过滤，补 `AN…

#### 21. 德雷曼逆向价值复合（低 PE + 低 PB + 低 P/CF，叠加行业折价）（Dreman Contrarian Value Composite (low PE / PB / PCF with industry discount)）

- **提出者**：David Dreman
- **公式**：`DremanScore = [1−pct_rank(PE) + 1−pct_rank(PB) + 1−pct_rank(P·CF)] ÷ 3（越高越便宜）；再叠加条件：PE < 0.8 × 行业 PE 中位数、市值处于全市场前 1,500–2,000 名、行业整体处于低估值区。`
- **含义**：德雷曼用大量实证说明「买入最不受欢迎的股票」长期跑赢，而衡量不受欢迎的代理变量就是低 PE、低 PB、低 P/CF、低 P/S、低 P/D 这一组估值比率。他坚持这些指标同时便宜才入场，并用行业层面的低估值做二次确认，持有 3–5 年等均值回复，靠的正是「分析师和机构对坏消息反应过度」。
- **为什么超出 PE/PB**：PE 会被非经常性损益和周期利润骗，PB 会被商誉和资产质量骗，单用任何一个都能买到便宜的烂公司。三个比率同时低，才说明市场在所有维度上都给了它不受欢迎的定价，这才是真正的逆向；再叠加行业折价，可排除「整个行业被重估」造成的假便宜。
- **字段映射**：exact 5 / approximate 1 / missing 0
  - ✔ `indicator_ext.market_cap_at_report / income_statement.parent_net_profit` ← PE：要求 parent_net_profit > 0；负 PE 不是便宜。
  - ✔ `indicator_ext.market_cap_at_report / balance_sheet.total_equity_parent（或取 1/indicator_ext.book_to_market）` ← PB：indicator_ext.book_to_market 内部有 COALESCE(total_equity_parent, total_equity) 的回退，回退发生时会把少数股东权益算进分母，口径混入。
  - ✔ `indicator_ext.market_cap_at_report / cash_flow.cf_from_operating` ← P/CF：经营现金流净额覆盖极好：2000 年后每年 100%（FY2024 5,904/5,904）。
  - ✔ `stock_meta.sw_level1 + median(PE) OVER (PARTITION BY sw_level1)` ← 行业相对折价：750 只股票 sw_level1 为 NULL，这批不进行业比较。
  - ✔ `indicator_ext.market_cap_at_report` ← 市值规模门槛：德雷曼原文限定在市值最大的一批公司里选，避免流动性陷阱和壳股；建议门槛 ≥ 20 亿。
  - △ `indicator_ext.market_cap_at_report 逐年截面自建 PE / PB 序列` ← 历史分位（可选增强）：库内没有历史 pe_ttm 序列（indicator_snapshot 只有最新一期快照），历史 PE/PB 分位必须按下式用 market_cap_at_report 逐年自建。
- **历史可用**：PE / PB / P/CF 全部依赖 market_cap_at_report 与三张主表，1998 年起有零散数据，2010 年后才完整（FY2024 非金融合格样本 3,274 只）。**10 年历史中位数可行（2010 起）**，5 年更稳；2000 年前样本不足以做分位。注意 indicator_snapshot 的 pe_ttm / pb_mrq 只是最新快照（5,575 行，每股一行），**不能用来做历史分位**，必须用…
- **主要陷阱**：①负 PE 会被排序逻辑当成最便宜：亏损股 PE 为负，若直接按 PE 升序取最低分位，排在前面的恰恰是亏得最狠的，必须强制 parent_net_profit > 0。②低 PE 的周期顶陷阱：煤/钢/化工在景气顶点 PE 极低（中远海控 FY2024 EY 达 43.6%），买进去往往正好接在盈利下坡起点。德雷曼本人要求叠加「行业整体处于低估值区」并持有 3–5 年，单一年度截面一定会被骗——实测 FY2024 选出的是中国中铁、中国电建、华能国际、中国铝业这类，方向正确，但必须配合多年持有期回测才成立。③PB 分母陷阱：total_equity_parent ≤ 0（资不抵债）时 PB…
- **审查裁决**：**❌ 不通过**　ROCE 同型风险：**高**
  - ⚠️ 样本量：报告称「FY2024 非金融合格样本 3,274 只」，三种口径都对不上：不加 20 亿市值门槛的 base = 3,792；加 20 亿门槛后（percent_rank 的真实分母）= 3,117；脚本最终输出（外层 pe<40 AND pb<4）= 1,629。也就是说读者以为有 3,274 只可比，实际排名只在 1,629 只上产出，高估约 2 倍。这条会误导后续「截面分位是否稳定」的判断，属于必须更正的量化声明。其余可复现项：行…
  - ⚠️ ROCE 同型问题：这是本文件里唯一一个产出被已知垃圾数据霸榜且没有任何护栏的指标：第 1 名 002379 的 dreman_score=1.0000（三项都拿满），源头是同一控制下企业合并追溯重述——FY2023 营收 26.87 亿 → FY2024 1,503.36 亿（55.9 倍）、总资产 29.82 → 1,080.26 亿（36.2 倍），而 market_cap_at_report 仍是重述前的 101.93 亿。Magic Formula 至少还有 |ROC|≤1 / |EY|≤1 两道闸门（虽然拦不住 002379），Dreman 这边零闸门、零重述过滤、零营收同比校验，可以直接把 1.00…
  - 🛠 修法：①必须加与 Magic Formula 同源的兜底：`AND NOT (营收同比>2 OR 总资产同比>2)`，或对 PE<1 / PB<0.3 的样本强制打标复核，否则榜首永远是重述股；②把「样本 3,274 只」改为实测 1,629，并注明 percent_rank 分母 3,117 与输出 1,629 的差异（或把 pe<40/pb<4 过滤前移到 base，使分位与名单同源）；③把 `mcap>=2e9` 换成按年截面的市值分位（如前 X%），避免绝对金额门槛的跨年漂移；④把 sw_level1 为 NUL…

#### 22. 趋势价值（价值前 10% ∩ 6 个月动量最强）（Trending Value (value decile intersected with 6-month momentum)）

- **提出者**：James O'Shaughnessy
- **公式**：`ValueScore = [1−pct_rank(PE) + 1−pct_rank(P/S) + 1−pct_rank(P·CF) + pct_rank(股息率)] ÷ 4；先取 ValueScore 前 10%，再在这 10% 内按 6 个月复权动量降序取前 10%。`
- **含义**：欧香纳西在《What Works on Wall Street》中发现，单纯低估值组合会买进大量「便宜但还在跌」的股票，单纯高动量组合会买进已经炒贵的股票；先用价值筛池、再用动量在池内排序（两个因子串联而非并联），长期年化显著高于任一单因子。价值负责便宜，动量负责「市场已经开始认错」，本质是规避价值陷阱。
- **为什么超出 PE/PB**：PE/PB 是纯静态估值，完全不含市场对未来的判断。动量项把「这只便宜股是否已被资金重新定价」加进来，等于在便宜资产里挑正在被修正的那批，避免在下跌趋势里接飞刀；P/S 和 P/CF 则补上净利润受非经常损益干扰的短板。
- **字段映射**：exact 6 / approximate 0 / missing 0
  - ✔ `indicator_ext.market_cap_at_report / income_statement.parent_net_profit` ← PE：需 >0 且盈利。
  - ✔ `indicator_ext.market_cap_at_report / income_statement.total_operating_revenue` ← P/S：用营业总收入而非营业收入，口径更全。
  - ✔ `indicator_ext.market_cap_at_report / cash_flow.cf_from_operating` ← P/CF：2000 年后覆盖率 100%。
  - ✔ `SUM(dividends.dividend_per_share) 近 12 个月 / indicator_ext.report_date_close` ← 股息率：同聂夫口径：ex_date 划窗、名义价配名义 DPS。
  - ✔ `price_daily_qfq.close` ← 6 个月动量：必须用前复权价；原始价 price_daily_raw 遇除权日会凭空出现 −50% 假跌幅。窗口两端都要取「<= 目标日的最后一个交易日」（2024-12-31 恰是交易日，但多数月末不是，等值匹配会返回空）。
  - ✔ `financial_report_dates.announce_date` ← 可交易时点（防前视）：实测覆盖 100%（FY2024：6,096/6,096；FY2010：2,452/2,452）。回测中必须用 announce_date + 1 个交易日建仓，而不是 12-31。
- **历史可用**：价格 1990-12-19 起、分红 1992 年起、三张主表 1990 年起、market_cap_at_report 2010 年后稳定。6 个月动量需半年价格，动量部分最早约 1991 年可用；价值部分需 PE/PS/PCF 全正，FY2024 合格样本 3,385 只。**10 年历史中位数可行**（2010 起）；做逐年回测建议用 2010–2025 共 16 个调仓截面。
- **主要陷阱**：①前视偏差是本指标头号杀手：年报 12-31 的数据通常次年 3–4 月才披露，用 12-31 收盘价选股等于用当时根本没公开的财报作弊。本库 financial_report_dates.announce_date 覆盖 100%，必须用它错开建仓时点。更隐蔽的坑是 report_metadata 里名字更像的 declare_date，但它覆盖率极低（FY2024 仅 98/5,482），误用会直接丢掉 98% 的样本。②动量必须用复权价：price_daily_qfq 前复权，除权日不出现假暴跌；用 price_daily_raw 会让高分红高送转股票在动量排序里被随机打到底部。③窗口日…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：①补上公式承诺的第二层 10% 切割（LIMIT 100 改成 LIMIT 33，或先 ntile(10) 再取第 1 档），否则名字叫「趋势价值」实际跑的是「价值十分位按动量排序」；②美盈森这类异常股息率加护栏（`AND dy < 3*median(dy) OVER (PARTITION BY sw_level1)`，或对 dy 做 1%/99% winsorize 后再算分位）；③把「FY2010：2,452/2,452」改成实测的 2,895/2,895；④把「动量最早约 1991 年可用」改成实测覆盖率（2…

#### 23. Fama-French 五因子单股暴露输入（价值 / 规模 / 盈利 / 投资）（Fama-French 5-Factor style inputs (HML / SMB / RMW / CMA)）

- **提出者**：Eugene Fama & Kenneth French
- **公式**：`HML：BE/ME = 归母权益 ÷ 股权市值；SMB：规模 = ln(股权市值)（取负向暴露）；RMW：(营业收入 − 营业成本 − 销售费用 − 管理费用) ÷ 归母权益；CMA：资产增速 = 总资产(t) ÷ 总资产(t−1) − 1（取负向暴露，即保守投资为正）。`
- **含义**：Fama-French 用这四组特征把个股收益分解到市场之外的系统性风格上：便宜（高 BE/ME）、小盘、高盈利、低资产扩张，长期各自带有溢价。对个股而言这四个数值就是它在四条风格轴上的坐标，用来解释「涨跌到底是 alpha 还是风格」，也是构建风格中性组合与业绩归因的基座。
- **为什么超出 PE/PB**：PB 只有一个估值维度；FF5 把「便宜」拆成价值、规模、盈利、投资四条近似正交的轴，能解释 PE/PB 组合为什么在某些年份集体失效（往往是规模或投资风格在反向）。RMW 用经营盈利能力而非净利润，剔除非经常损益；CMA 用资产扩张速度而非资产质量，抓住「管理层乱花钱」这一独立风险维度。
- **字段映射**：exact 5 / approximate 1 / missing 1
  - ✔ `balance_sheet.total_equity_parent` ← HML 分子（账面权益）：FF 原文 book equity = 股东权益合计 − 优先股 + 递延税，A 股无优先股，用归母权益最接近；用 total_equity 会把少数股东权益算进来。
  - ✔ `indicator_ext.market_cap_at_report` ← HML 分母 / SMB（股权市值）：报告日收盘价×时点股本；与 indicator_ext.book_to_market 互为倒数关系（后者内部有 COALESCE 回退，个股层面可能差一个回退口径）。
  - △ `income_statement.total_operating_revenue - income_statement.cost_of_revenue - income_statement.selling_expenses - income_statement.administrative_expenses` ← RMW 分子（经营利润）：FF 原始定义还要再减利息费用 INT，但本库 interest_expense 语义不稳定（有负值）且 2016 年覆盖仅 12.6%（context 陷阱 3），完全不可用，只能做「息前」版本。代价：高杠杆公司的 RMW 被系统性高估。备选 operating_profit 虽覆盖更好，但含投资…
  - ✘ `income_statement.interest_expense（虽存在但不可用）` ← RMW 分子中的利息费用 INT（FF 原式必需）：字段确实在表里，但语义不稳定（存在负值，方向不可依赖）且 2016 年报覆盖只有 643/5,085 = 12.6%，跨年计算必然断裂。对本指标而言等同「库里没有可用字段」，这是 RMW 只能退化成息前版本的根本原因。另：financial_expenses 是净额，也不能替代 INT。
  - ✔ `balance_sheet.total_equity_parent` ← RMW 分母：FF 用账面权益作分母，与 HML 同源。
  - ✔ `balance_sheet.total_assets 连续两个年报期` ← CMA（资产增速）：需自连接 t 与 t−1 两个 12-31 期，缺上年报的会被过滤。
  - ✔ `stock_meta.sw_level1 NOT IN ('银行','非银金融')` ← 行业剔除：金融企业没有 COGS/存货，RMW 与 CMA 都无经济含义，FF 原文也剔除金融。
- **历史可用**：cost_of_revenue / selling_expenses / administrative_expenses 2007 年后覆盖 95% 以上，1998–2006 约 90%；total_assets 与 total_equity_parent 1990 年起；market_cap_at_report 2010 年后完整。**FY2024 实测四个因子齐全样本 5,190 只（占非金融 5,644 只的 92%）**。**1…
- **主要陷阱**：①RMW 的口径让步是硬伤：因 interest_expense 不可用，只能算「息前经营利润率」，等于给高杠杆公司送分——而 FF 的 RMW 溢价恰恰想在控制杠杆后度量经营效率。做因子归因时必须声明，不能拿去和文献数值直接对比。②这张表给的是「暴露度」不是「收益」：HML=0.8 只说明偏价值，不能当买卖信号；要真正还原 FF 因子收益，还得按 2×3 分组构建六个组合并算多空收益，本地库没有现成因子收益序列。③fixed_assets 是 PPE（厂房设备净值），不是会计意义上的「固定资产」大区，任何拿它做分母的变体都会重演 ROCE 域 119% 的假值事故（context 陷阱 1）…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ 样本量：主样本数精确：「FY2024 四个因子齐全 5,190 只，占非金融 5,644 只的 92%」✓ 逐位吻合（实测 5,190 / 5,644 = 91.96%）。但 RMW 分母三项的覆盖率整段写成了 FY2025 的数：报告称「FY2024 cost_of_revenue 5,530/5,625、selling 5,456/5,625、administrative 5,521/5,625」，实测 FY2024 是 5,809 / 5…
  - 🛠 修法：①把 RMW 覆盖率三个数换成 FY2024 实测值 5,809/5,734/5,800（分母 5,904），把现在的 5,530/5,456/5,521（FY2025，分母 5,625）单独标注年份；②按报告自己在陷阱⑦里的建议，在 SQL 里真的加 LEAST(GREATEST(cma,-2),2) 或 1%/99% winsorize（代价仅 11 只）；③HML 也做同样处理，并把 002379 类重述样本单列；④size_ln 明确写成「Ln(总市值/1e8)（单位：亿元）」或改回 ln(元)，以免与 F…

#### 24. 质量减垃圾（QMJ）综合 z 分（Quality Minus Junk (QMJ) composite z-score）

- **提出者**：Cliff Asness
- **公式**：`QMJ = 平均[ 盈利能力 z 组（毛利/总资产、ROE、经营现金流/总资产） + 安全 z 组（Altman Z、−资产负债率、−应计项） ]；每个原始比率先在截面做 1%/99% winsorize，再 z-score 标准化后等权合成。`
- **含义**：Asness 等人把「质量」定义成可度量的维度：盈利能力强、盈利在增长、经营安全（低杠杆低波动）、股东友好（回购分红而非圈钱），再把每只股票在各维度上的原始比率做截面标准化后加总。他们发现做多高质量、做空低质量（垃圾）的组合能在多国市场取得显著超额收益，且质量与价值相关性低，叠加后能显著改善组合。
- **为什么超出 PE/PB**：PE/PB 完全是价格侧信息，对「这家公司生意好不好、会不会爆雷」零判断。QMJ 全部由基本面构成：高毛利意味着定价权，低杠杆 + 高 Altman Z 意味着抗风险，低应计意味着利润不是纸面数字。把它当估值筛选的过滤器，正好避开低 PE + 低 PB 里最脏的那一批（价值陷阱识别器）。
- **字段映射**：exact 6 / approximate 3 / missing 0
  - ✔ `(income_statement.total_operating_revenue - income_statement.cost_of_revenue) / balance_sheet.total_assets` ← 毛利/总资产（GP/A，Novy-Marx 质量核心）：必须要求 cost_of_revenue IS NOT NULL，否则 COGS 缺失的约 1.7% 公司 GP 被当成 100% 收入，直接登顶质量榜。
  - ✔ `income_statement.parent_net_profit / balance_sheet.total_equity_parent` ← ROE：归母口径。
  - ✔ `cash_flow.cf_from_operating / balance_sheet.total_assets` ← 经营现金流/总资产（CFO/A）：覆盖极好：2000 年后每年 100%。
  - ✔ `indicator_ext.accruals = (net_profit - cf_from_operating) / total_assets` ← 应计项（越低越安全）：现成字段，2025 年报覆盖 5,618/5,625。Sloan 应计异象的标准定义。
  - ✔ `balance_sheet.total_liabilities / balance_sheet.total_assets` ← 杠杆（越低越安全）：取负号进入安全组。
  - △ `balance_sheet.total_current_assets - total_current_liabilities、undistributed_profit、indicator_ext.ebit、indicator_ext.market_cap_at_report、total_liabilities、income_statement.total_operating_revenue` ← Altman Z 安全项：Z = 1.2·WC/TA + 1.4·RE/TA + 3.3·EBIT/TA + 0.6·MVE/TL + 1.0·Sales/TA。0.6·MVE/TL 在负债极低的公司会发散（实测未截断时出现 297.58、196.85、192.11），必须 LEAST(...,3.0) 截断或 winsor…
  - △ `indicator_ext.dilution_3y` ← 摊薄（越低越友好）：依赖 funding_events 已发行记录，覆盖 5,512/5,625；未记录不等于没增发，故摊薄分偏乐观。indicator_ext.buyback_ratio_3y 覆盖率仅约 51.8%（回购为自愿披露），不宜单独作分子。
  - ✔ `indicator_snapshot.payout_ratio / dividends 逐笔加总` ← 分红：payout_ratio 只有最新快照，历史回溯需用 dividends 自建。
  - △ `（本版未纳入）需用 price_daily_qfq 日收益自建市场收益序列后回归` ← 安全组的 beta（Asness 原文要求）：库里没有现成的市场指数日收益序列，但可用 price_daily_qfq 等权或市值加权自建，或用 index_valuation / etf_daily 作代理。本 SQL 未含该项，故 QMJ 为不含 beta 的近似版；若要补齐，需额外一条按日回归的流水线。
- **历史可用**：全部输入来自三张主表 + indicator_ext，**2000 年后每年覆盖率接近 100%**（唯一强约束是 cash_flow.cf_from_operating，FY2024 5,904/5,904 全有值）。FY2024 剔金融后 5,193 只全部可算。**5 年 / 10 年历史中位数完全可行**，且建议直接做成历史中位数版本——质量是慢变量，单年截面噪音大（商誉减值、一次性投资收益会大幅扰动 ROE 与 Altman…
- **主要陷阱**：①Altman Z 的 0.6×市值/总负债 项在低负债公司会爆炸：实测 FY2024 未截断时该值最高 297.58（300561）、196.85（300333）、192.11（688582），会把 ROE −3.5% 的亏损股推到 QMJ 第一名、把 ROE −234% 的 002310 推进前 15。必须对该项截断到 3 或整体 1%/99% winsorize（SQL 已用 LEAST(...,3.0)）。②不做截面 winsorize 就做 z-score，单个极值会主导整个因子——这也是 Asness 原文明确要求 1%/99% 缩尾的原因。③Altman Z 只对制造业成立，服务…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：**高**
  - ⚠️ match 分级：字段级映射基本诚实（accruals 标 exact 且定义完全对得上：app/core/indicators/extended.py:345-348 就是 (net_profit − cf_from_operating)/total_assets，实测 349,369/349,369 全等；GP/A 要求 cogs 非空的说明也正确），但有两处实质性夸大：①意义层面声称「QMJ 是纯质量因子，与估值正交」（why_beyond_pe_pb 与陷阱⑥都写了），而它自己的 Altman Z 里就含 0.6·marke…
  - ⚠️ ROCE 同型问题：这是六个指标里 ROCE 式风险最高的一条，且失败模式与 ROCE 事故完全同构——分母趋 0 → 比率发散 → 选择加护栏而非改口径：0.6·market_cap_at_report / total_liabilities 在 near-zero-debt 公司身上发散到 297，报告的处置是 LEAST(..., 3.0)。但实测 2,108 / 5,193（40.6%）的样本被顶到同一个 3.0，这不是护栏而是把这一维结构性压平：低负债公司之间比不出安全度，只能靠其余四项拉开差距；同时这项又随市值水涨船高（市值/总负债），等于给 QMJ 混入了一条估值腿——报告自己在 why_beyon…
  - 🛠 修法：①Altman Z 的 0.6·MVE/TL 不要用固定 3.0 截断（40.6% 顶格），改成按年截面 1%/99% winsorize，或把该项从安全组移出（Asness 原文 QMJ 安全组是低 beta / 低杠杆 / 低破产风险，不含市值项），以恢复与价值因子的正交性；②删掉或实现 data_mapping 里的 dilution_3y / payout_ratio / beta 三条；③把「纯质量因子与估值正交」改成「本版因 Altman Z 含市值项而带市场/估值暴露，归因时需剥离」；④修正陷阱⑥的「…


### 3.5 质量 / 复利机器流派

**覆盖大师**：Terry Smith、Chuck Akre、Nick Sleep、François Rochon、Tom Gayner

**本流派审查总评**：结论：pass_with_fixes（无 P0）。6 条指标全部通过字段存在性、SQL 可执行性与跨表连接安全检查——这次没有重演 ROCE 那次「固定资产净值直译」的伪造字段问题：data_mapping 里出现的每一个 db_field（含 context.md 未登记、我优先怀疑是编造的 roce_history、xdxr.songzhuangu/event_date、share_capital_history.change_reason、funding_events、business_breakdown）全部在 information_schema 中真实存在；6 条 formula_sql 原样包一层 SELECT 全部跑通，无全 NULL / 全 0 列；income_statement / balance_sheet / cash_flow / cash_flow_activity / roce_history 在年报口径下均无 (stock_code, report_date) 重复键，窗口函数不会因重复行静默翻倍。会计恒等式也逐条验过：roce_history.roce ≡ ebit/((ce+ce_prev)/2)（97,650 行 0 不符）、ce ≡ total_assets − total_current_liabilities（62,729 行 0 不符…

#### 25. 现金转换率（自由现金流 / 净利润）（Cash Conversion Ratio (FCF / Net Profit)）

- **提出者**：Terry Smith（Fundsmith）
- **公式**：`CC = 自由现金流 ÷ 净利润，其中 FCF = 经营活动现金流净额 − 资本支出。聚合用 5 年累计口径：CC_5y = Σ(近5个年报 FCF) ÷ Σ(同期净利润)，分母只累计净利润 > 0 的年份。判定线：Fundsmith 要求「利润几乎全部转成现金」，落地阈值 CC_5y ≥ 0.90，且近 5 年中至少 3 年单年 FCF/净利润 ≥ 1.0。`
- **含义**：衡量账面利润里到底有多大比例真正变成了可自由支配的现金。它用最短的路径把「利润是真的」和「利润只是应收账款和存货」区分开。Fundsmith 用它把复利机器和报表漂亮但现金枯竭的公司划到两个池子里。
- **为什么超出 PE/PB**：PE/PB 只看利润和净资产的存量，不看利润的含金量。两家 PE 完全相同的公司，CC=0.3 与 CC=1.1 的差别是「赚到的钱能不能拿去买下一块资产」，复利能力天差地别。
- **字段映射**：exact 5 / approximate 1 / missing 0
  - ✔ `cash_flow.cf_from_operating` ← 经营活动现金流净额：直接法现金流量表汇总行，年报覆盖 100%（2025 年报 5,624/5,624 全非空）
  - ✔ `cash_flow_activity.capex` ← 资本支出（毛额）：字段注释与来源已核实为「购建固定资产、无形资产和其他长期资产支付的现金」；年报覆盖率 1998 年 77%、2013 年 61.8%（历史谷底）、2020 年 76.9%、2025 年 98.4%
  - ✔ `income_statement.net_profit` ← 净利润（合并口径，含少数股东）：必须用合并净利润而非 parent_net_profit，理由见 traps ①
  - ✔ `indicator_ext.free_cash_flow` ← 自由现金流（已物化派生列）：= operating_cash_flow − capex，逐报告期物化；capex 为 NULL 时该列为 NULL（fail-closed，正确行为），可直接用于本公式
  - ✔ `indicator_ext.operating_cash_flow` ← 经营现金流（已物化派生列）：= cash_flow.cf_from_operating 的副本，可作交叉校验
  - △ `indicator_snapshot.cf_to_net_profit` ← 现成比率（同名但不同义）：该列 = cf_from_operating / net_profit（TTM），**没有扣资本支出**，是「经营现金流覆盖率」而非 Fundsmith 的现金转换率。两者名字接近但不可互相替代
- **历史可用**：年报口径：cf_from_operating 最早可用 1998 年（993/1,192 家），2000 年起每年 1,275+ 家且覆盖率 > 93%。但分母 capex 只存在于 cash_flow_activity，1998 年起覆盖率在 61.8%–98.4% 之间波动。实测 indicator_ext.free_cash_flow：有 ≥5 个有效年报的 5,166 只（5 年累计可做），有 ≥10 个的仅 3,400 只（占…
- **主要陷阱**：① 分母必须用 net_profit（合并，含少数股东）而不是 parent_net_profit。现金流量表是合并口径、包含少数股东对应部分，用归母净利润当分母会让控股平台型公司（少数股东权益大的：地产、集团控股、部分基建）现金转换率系统性虚高 20–60%。② 净利润 ≤ 0 的年份必须剔除或置 NULL，不能直接 FCF/净亏损——负分母会让「亏损年现金其实很好」显示成负比率。所以只能用「ΣFCF / Σ净利润」并只累计净利润>0 的年（与本项目 cf_to_net_profit 的既有裁定一致）。③ 绝对不能用 COALESCE(capex, 0) 兜底：capex 缺失年份会被当成「…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：**高**
  - ⚠️ SQL：无。6 条 formula_sql 全部包 SELECT ... LIMIT 5 跑通；本条返回 6,145 行，无全 NULL / 全 0 列。另独立复核 indicator_ext.free_cash_flow ≡ cf_from_operating − cash_flow_activity.capex（132,196 行 0 处不符）、indicator_ext.capex ≡ cash_f…
  - ⚠️ 样本量：一半精确一半错。精确：indicator_ext.free_cash_flow 有 ≥5 年报 5,166 只、≥10 年报 3,400 只（占 6,242 的 54.5%）——与报告一字不差；capex 年报覆盖率 1998=76.4%（称 77%）、2013=61.8%、2020=76.9%、2025=98.4%——四个数全对；茅台 2015-2025 单年 CC 实测 0.480~2.032（称 0.45~2.03，基本准确）。错…
  - ⚠️ ROCE 同型问题：ROCE 事故同型风险，且已实际发生：把「分母在该行业无经济含义」的板块放进排序前端——金融股 OCF 含存贷款与同业拆借变动、capex 极小，CC 恒为 2-7 倍，任何 CC>=0.90 的筛选都会把银行券商排满第一屏。第二处同型风险是窗口长度：formula_sql 不要求 cc_years_5y=5，实测 6,145 只里 1,375 只不足 5 年、674 只只有 <=2 年，却仍输出名叫 cash_conversion_5y 的比率（报告在 history_years 已预告要落「有效年数」并确实输出了 cc_years_5y 列，属披露到位但未设门）。分母本身已用 net_pr…
  - 🛠 修法：① 在 formula_sql 的 annual CTE 补 JOIN stock_meta 并按 sw_level1 NOT IN ('银行','非银金融') 排除，或至少单列 is_financial 标记列而不参与排序；② 外层加 WHERE cc_years_5y >= 4（或要求 =5），不足年数只保留 cc_years_5y 不输出比率；③ trap ④ 的「CC_5y=1.82/2.31」改写为「10 年累计 CC=1.82/2.31，5 年窗口为 2.62/2.87」；④ 1998 分母改 1,20…

#### 26. 毛利率与费用率双稳态（含「规模效应共享」检验）（Gross Margin / Opex Ratio — Level, Stability and Scale-Economies-Shared Test）

- **提出者**：Terry Smith（Fundsmith）+ Nick Sleep（Nomad Capital）
- **公式**：`① 毛利率 gm(t) = (营业收入 − 营业成本) ÷ 营业收入；费用率 opex(t) = (销售费用 + 管理费用) ÷ 营业收入。② 取近 5 个年报算：gm_5y_median、gm_5y_std = STDDEV_SAMP(gm)、opex_5y_std、gm_drift_5y = 最近两年均值 − 最早两年均值（opex_drift_5y 同理），以及 revenue_cagr4。③ Fundsmith 形态：gm_5y_median ≥ 0.40 且 gm_5y_std ≤ 0.03 且 gm_drift_5y ≥ −0.02。④ Sleep「规模效应共享」形态：revenu…`
- **含义**：毛利率高说明产品有定价权，波动小说明这份定价权没有被竞争侵蚀；费用率的稳定或下行说明规模扩张时成本没有同步膨胀。Nick Sleep 加的检验是：真正持久的复利机器会把规模效应让给客户（价格不涨、毛利率不扩张），用短期利润率换取长期市场份额与攻不破的成本壁垒。
- **为什么超出 PE/PB**：PE/PB 是当期价格除以当期利润/净资产，对「明年毛利率会不会掉 10 个点」完全无感。毛利率的水平 + 波动率 + 费用率漂移，是「这条护城河是否正在被填平」最便宜的可计算代理。
- **字段映射**：exact 6 / approximate 2 / missing 0
  - ✔ `income_statement.revenue` ← 营业收入：年报覆盖 97–99%（2025 年报 5,625/5,625）。注意不是 total_operating_revenue，两者对制造业同值，对金融股 revenue 才是真正的营收
  - ✔ `income_statement.cost_of_revenue` ← 营业成本：年报非空率 96–98%。**不是 total_operating_cost**（那个含三费与税金附加），见 traps ①
  - ✔ `income_statement.selling_expenses` ← 销售费用：年报非空率 97–99%（2025 年报 5,456/5,625）
  - ✔ `income_statement.administrative_expenses` ← 管理费用：年报非空率 97–99%；2017 年起研发费用被拆出，跨 2017 不可比，见 traps ③
  - △ `income_statement.rd_expenses` ← 研发费用：2015 年 86 条、2016 年 627 条、2017 年 3,932 条、2018 年 5,128 条——2017 年前基本没有，不能进跨 2017 的任何费用率序列
  - ✔ `indicator_ext.selling_expense_ratio` ← 销售费用率（已物化）：= selling_expenses / revenue，逐报告期物化并带 |ratio| 护栏
  - ✔ `indicator_ext.admin_expense_ratio` ← 管理费用率（已物化）：= administrative_expenses / revenue，同上
  - △ `indicator_snapshot.gross_margin` ← 毛利率（快照单点）：= (revenue − cost_of_revenue)/revenue 但为 **TTM 单点**且带 [−1,1] 护栏；只能当最新截面参考，不能替代 5 年稳定性
- **历史可用**：年报「营业收入 + 营业成本」双非空：2003 年 1,560/1,621（96%），2007 年后每年 5,000+ 家。实测 5,023 只股票有 ≥10 个年报毛利率、6,054 只有 ≥5 个，平均序列长度 17.0 年——10 年标准差在数据量上完全可做。但因为 2020 年新收入准则断点（traps ②），**推荐的稳定性主窗口是最近 5 个年报（2021–2025，全部在准则之后）**，10 年窗口只作参考并必须标注断点。…
- **主要陷阱**：① 分子分母必须用 revenue 与 cost_of_revenue；**绝不能用 total_operating_cost**。A 股「营业总成本」= 营业成本 + 税金及附加 + 销售/管理/研发/财务费用 + 减值损失，用它算出来的是「营业利润率的补数」，不是毛利率。这是本项目最容易踩的一处。② **2020 年新收入准则（CAS14）断点**：运输费从销售费用重分类进营业成本。我用同一批 4,188 只（2015–2025 每年都在）固定面板实测中位数：毛利率 2019=28.66% → 2020=27.38%（−1.28pct）；销售费用率 2019=4.77% → 2020=3.…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ SQL：无。返回 6,130 行；gm_5y_std / opex_5y_std 空值率仅 0.07%，revenue_cagr4 空值率 1.96%（不足 5 个年报所致，合理），无全 NULL / 全 0 列。
  - 🛠 修法：① 管理费用率中位数修正为 9.43%（2016）→ 6.70%（2017）；②「2024 年 2,983 条年报行 / 847 非空」改为「121 只金融股全历史年报行 2,983 条、cost_of_revenue 仅 847 条非空（2024 单年 484 行 / 104 非空）」；③ revenue_cagr4 增加「rn=1 与 rn=5 的 report_date 相差恰为 4 年」的连续性校验，否则置 NULL；④ gm_5y_std 增加 n5>=4 才输出的门。

#### 27. ROCE 持久性（10 年中位数 + 最低值 + 变异系数）（ROCE Persistence (10y median / floor / coefficient of variation)）

- **提出者**：Terry Smith（Fundsmith）+ François Rochon（Giverny Capital）
- **公式**：`复用 roce_history 的年度 ROCE（EBIT ÷ 平均已动用资本），在最近 10 个有效年报上再算三个统计量：roce_10y_median（水平）、roce_10y_min（地板）、roce_10y_cv = STDDEV_SAMP(roce)/MEDIAN(roce)（稳定性），外加 roce_years_lt10pct 与 roce_years_negative 两个计数。判定：roce_10y_years ≥ 8 且 median ≥ 0.15–0.20 且 min ≥ 0.10 且 cv ≤ 0.35。`
- **含义**：中位数回答「平均能赚多少」，最低值回答「最差的一年还赚不赚」，变异系数回答「是不是靠某一年暴利拉高的」。Fundsmith 与 Rochon 都不接受「10 年里 3 年 40%、7 年 3%」这种形态——复利机器必须是每一年都不难看，而不只是平均好看。
- **为什么超出 PE/PB**：PB 低可能只是资产烂，ROE 高可能只是杠杆高。ROCE 分母含负债、把杠杆效应排除掉，再看 10 年分布，才能把「真高回报」和「借钱堆出来的高 ROE」分开。
- **字段映射**：exact 8 / approximate 0 / missing 0
  - ✔ `roce_history.roce` ← 年度 ROCE（已物化）：= ebit / ((ce + ce_prev)/2)，105,168 行、1989–2025，同表带 status 状态码
  - ✔ `roce_history.ce` ← 已动用资本 CE：= balance_sheet.total_assets − total_current_liabilities，含超额现金（项目既有裁决）
  - ✔ `balance_sheet.total_assets` ← 总资产：年报覆盖 100%
  - ✔ `balance_sheet.total_current_liabilities` ← 流动负债合计：**121 只金融股为 NULL**（无流动/非流动划分），这些股票根本没有 ROCE
  - ✔ `income_statement.total_profit` ← 利润总额：年报覆盖 100%
  - ✔ `income_statement.financial_expenses` ← 财务费用：净额（可为负）；金融股为 NULL，直接导致 roce_history 的 EBIT 缺失
  - ✔ `indicator_snapshot.roce_10y_median` ← ROCE 10 年中位数（已物化）：已覆盖 5,460/5,575 只，可直接复用；本指标只需再补 min / cv 两列
  - ✔ `indicator_snapshot.roce_10y_years` ← ROCE 有效年数（已物化）：必须与 median 一起展示，否则使用者不知道这是几年的中位数
- **历史可用**：roce_history 全表 105,168 行，覆盖 1989-12-31 ~ 2025-12-31；实测 4,610 只股票有 ≥10 个有效年报、6,006 只有 ≥5 个，平均序列长度 15.8 年；indicator_snapshot 已物化 roce_10y_median 覆盖 5,460/5,575。结论：10 年中位数 / 最低值 / 变异系数**完全可做**，是这一批指标里历史最扎实的一个，计算成本也最低（roce_…
- **主要陷阱**：① roce_history.ebit 要求 income_statement.financial_expenses 非空；121 只金融股没有财务费用行，status 直接是 MISSING_EBIT。而 indicator_ext.ebit 对金融股有 COALESCE(financial_expenses,0) 回退（≈利润总额）。**两处 EBIT 口径不一致**，混用会算出两个不同的 ROCE，交叉引用时务必指定取哪一处。② CE = total_assets − total_current_liabilities **含超额现金**（2026-11-16 既有裁决，见 app/co…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ match 分级：字段全部真实且恒等式复核到位：roce_history.roce ≡ ebit/((ce+ce_prev)/2)（97,650 行 0 处不符）、roce_history.ce ≡ total_assets − total_current_liabilities（62,729 行 0 处不符）、ebit ≡ total_profit + COALESCE(financial_expenses,0)（62,611 行 0 处不符），indicator_snapshot.roce_10y_median 覆盖 5,460…
  - ⚠️ SQL：无。返回 6,161 行；roce_10y_cv 空值率 7.55%（MEDIAN(roce)<=0 时按 CASE 置 NULL，符合设计），无全 NULL / 全 0 列。
  - 🛠 修法：① financial_expenses 的 match 由 exact 改为 approximate，与 IDX3 对齐并注明「EBIT=利润总额+财务费用为项目既定近似，净现金公司会被低估」；② 三处「121 只金融股 TCL 为 NULL / 根本没有 ROCE」改为「74 只金融股全历史 TCL 全空、被 NULL 过滤自动排除；另 47 只（券商为主）TCL 非空并已算出 783 个有效 ROCE 年」；③ 删掉「金融股导致 EBIT 缺失」的因果，改为「financial_expenses 为空即 MI…

#### 28. 增量资本回报率（5 年，双口径）（Incremental ROIC (5-year, CE caliber plus ex-cash sensitivity)）

- **提出者**：Chuck Akre + Tom Gayner（Markel）
- **公式**：`NOPAT(t) = EBIT(t) × (1 − t_eff)；EBIT = 利润总额 + COALESCE(财务费用, 0)；t_eff = CLAMP(所得税费用 / 利润总额, 0, 0.35)，利润总额 ≤ 0 时取 25% 中性税率。已动用资本 IC(t) = 总资产 − 流动负债（与 roce_history.ce 同口径）。增量资本回报率 = ΔNOPAT ÷ ΔIC，窗口 5 年（LAG(…, 5)，即 t 与 t−5 两个年报）。敏感性列 incr_roic_5y_excash 用 IC_excash = 所有者权益合计 + 短期借款 + 长期借款 + 应付债券 − 货币资…`
- **含义**：它回答的是 Akre 那句话的算术版本：公司今年留在账上的每一块钱利润，再投出去能赚回多少。水平 ROIC 只说明「过去攒下的资产回报高」，增量 ROIC 才说明「今天新投的钱回报还高不高」——这是判断一台复利机器是在变强还是在变钝的核心。
- **为什么超出 PE/PB**：PE/PB 对「新增投资回报是否在递减」完全无感。一家 ROIC 30% 但新增投资只赚 5% 的公司，PE 再便宜也是在缓慢摊薄股东价值；反过来 ROIC 15% 且增量仍在 15% 以上的公司，时间就是它最好的朋友。
- **字段映射**：exact 8 / approximate 2 / missing 1
  - ✔ `income_statement.total_profit` ← 利润总额：年报覆盖 100%，EBIT 的基准项
  - △ `income_statement.financial_expenses` ← 财务费用：净额且净现金公司为负；金融股为 NULL，COALESCE 为 0 时 EBIT ≈ 利润总额（与 indicator_ext.ebit 回退式一致）
  - ✔ `income_statement.income_tax` ← 所得税费用：必须 CLAMP(tax/total_profit, 0, 0.35)，亏损年会变负或爆表
  - ✔ `balance_sheet.total_assets` ← 总资产：CE 的第一项
  - ✔ `balance_sheet.total_current_liabilities` ← 流动负债合计：CE 的第二项；金融股为 NULL，这些股票必须排除
  - ✔ `balance_sheet.total_equity` ← 所有者权益合计：ex-cash 口径的起点（用合计而非归母，与资产端口径对齐）
  - ✔ `balance_sheet.short_term_loans / long_term_loans / bonds_payable` ← 有息负债三项：与 indicator_snapshot.interest_bearing_debt 同源；三项全空的公司多是真没有借款，但无法与「没采到」区分
  - ✔ `balance_sheet.monetary_funds` ← 货币资金：ex-cash 口径扣除项
  - △ `balance_sheet.trading_financial_assets` ← 交易性金融资产：2020 年报仅 3,124 条非空（多数公司本来没有），COALESCE 0 合理，但要意识到「没有」和「没采到」不可区分
  - ✔ `indicator_ext.ebit` ← EBIT（已物化）：= total_profit + COALESCE(financial_expenses,0)，可直接替代本式的 EBIT 表达式
  - ✘ `（库中无此列）` ← NOPAT：全库没有任何 NOPAT 字段，必须由 EBIT × (1 − 有效税率) 现场派生；差异就在「有效税率怎么取」
- **历史可用**：依赖 income_statement × balance_sheet 主表，2007 年后覆盖完整（每年 5,000+ 家）。算 5 年增量需要每股至少 6 个年报 → 2013 年起才有第一个可算点，2013–2025 共 13 个可算年度；可以对这 13 个点再取中位数来做抗噪筛选。10 年窗口需要 11 个年报 → 2018 年起才可算，覆盖会掉一半以上。ex-cash 口径还额外要求货币资金/有息负债齐全，覆盖率略低于主口径。
- **主要陷阱**：① **超额现金污染（本指标最致命，且与水平 ROCE 的偏差方向相反）**：IC 含货币资金。现金堆积型公司的 ΔIC 几乎全是存款，ΔNOPAT 却不动，增量 ROIC 被压到个位数。实测海天味业 2020→2025：IC 从 204.5 亿涨到 423.3 亿（Δ218.8 亿），ΔNOPAT 仅 +8.9 亿，增量 ROCE = 2.7%，而同期水平 ROCE 中位数是 34%。这不是「海天变差了」，是口径把存款当成了再投资。所以**必须两列一起读**：incr_roic_5y（主口径，与 ROCE 可比）与 incr_roic_5y_excash（剔现金，经济含义优先）。两者背离越大…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ SQL：无。返回 105,089 行；incr_roic_5y 空值率 41.3%、excash 口径 45.3%（护栏 + LAG(5) 未满所致，属设计内），无全 NULL / 全 0 列。
  - ⚠️ 样本量：三条声明两假一真。真：海天味业数字精确——IC 2020=204.5 亿 → 2025=423.3 亿（Δ218.8 亿）、incr_roic_5y=2.7%，与报告完全一致。假：「ic_excash<=0 的年报行占 2.6%（1,600/62,347）」不可复现——实测全历史 2,040/105,089=1.94%、2013 年后 1,320/70,017=1.89%，得不到 62,347 这个分母，也得不到 1,600。假：「算…
  - 🛠 修法：①「1,600/62,347=2.6%」改为实测「ic_excash<=0 占 1.9%（2,040/105,089；2013 年后 1,320/70,017）」；② history_years 改为「1994 年起即有可算点，2007 年后覆盖变厚，共 32 个可算年度」，删除「2013 年起才有第一个可算点」；③ 海天 ΔNOPAT 由 +8.9 亿改为 +5.9 亿；④ 第六处「121 只金融股」同样改为 74 只；⑤ COALESCE(income_tax,0) 改为 income_tax IS NULL…

#### 29. 再投资率 × 增量 ROIC = 内生增长率（Reinvestment Rate x Incremental ROIC = Organic Growth (value-driver decomposition)）

- **提出者**：Chuck Akre + Nick Sleep
- **公式**：`RR_5y（再投资率）= ΔIC ÷ Σ(近 5 年 NOPAT)；incr_roic_5y = ΔNOPAT ÷ ΔIC（同指标 4）；g_implied = RR_5y × incr_roic_5y。判定：RR_5y ∈ [0.30, 1.50] 且 incr_roic_5y ≥ 0.15 且 g_implied ≥ 0.10。RR < 0 不判为「停止投资」，而是标记 rr_flag='BUYBACK_OR_SHRINK' 单独成组。`
- **含义**：把增长率拆成两个正交维度：投得多不多（再投资率）与投得值不值（增量 ROIC）。只有「投得多 + 投得值」同时成立才是 Akre 要的复利机器；投得多但投得差是价值毁灭，投得值但投不出去是现金牛而不是复利机器。
- **为什么超出 PE/PB**：PE 里的 g 是市场预期的黑箱；这个式子把 g 拆成两个会计可验证的乘数，让「高增长」必须自证：钱从哪来、投出去赚了多少。
- **字段映射**：exact 2 / approximate 1 / missing 1
  - ✘ `（库中无此列，须由 income_statement.total_profit + financial_expenses 与 income_tax 派生）` ← NOPAT：派生式 = (total_profit + COALESCE(financial_expenses,0)) × (1 − CLAMP(income_tax/total_profit,0,0.35))
  - ✔ `balance_sheet.total_assets − balance_sheet.total_current_liabilities` ← 已动用资本 IC：与 roce_history.ce 完全同口径
  - △ `indicator_snapshot.revenue_cagr5 / net_profit_cagr5` ← 再投资率（已物化近似）：这两个是**实际实现增速**（可用于交叉校验 g_implied），不是再投资率本身；库里没有任何再投资率字段，必须按 ΔIC/ΣNOPAT 现算
  - ✔ `indicator_snapshot.revenue_cagr5` ← 实际增速校验：用来对照 g_implied 与实际增速的缺口（缺口部分通常来自并购或利润率变化）
- **历史可用**：窗口条件比指标 4 更严：需要 5 个连续年报的 NOPAT 累计（n_win5 = 5）外加 t−5 的 NOPAT/IC，即每股至少 6 个年报 → 2013 年起可算，2013–2025 共 13 个可算年度。可以做「近 3 个重叠 5 年窗口的中位数」来抗噪。不建议做 10 年窗口：需要 11 个年报，覆盖掉一半以上，且会跨越 2020 疫情与准则变更。
- **主要陷阱**：① **数学上 g_implied = RR × incr_roic ≡ ΔNOPAT / ΣNOPAT，是恒等变形而不是独立证据。**它唯一的价值是把 g 拆成「投得多」和「投得值」两个可分别筛选的轴。绝不能把它当未来增速的外推器，也不能拿它与实际营收增速的差值当「护城河证据」——那个差值主要反映并购、利润率变化和税率变化。② 再投资率的分母必须用窗口累计 ΣNOPAT，不是期末单年 NOPAT；用期末值会让高增长公司的 RR 系统性低估约一半。③ 回购会让 IC 下降 → RR 变负 → g_implied 变负，而 EPS 增速可能为正。必须把 RR < 0 单独标记为「回购型/收缩型」…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ SQL：无。返回 105,089 行；reinvest_rate_5y 空值率 37.3%，rr_flag 空值率 95.5%（只在 ΔIC<0 时赋值，符合设计），无全 NULL / 全 0 列。
  - 🛠 修法：① 把「再投资率（已物化近似）」这行从 data_mapping 删除，或把 component 改名为「实际增速（不可替代再投资率）」；② business_breakdown 行数改为 1,012,409；③ 在 formula_sql 输出层加 reininvest_rate 上下限列或 rr_flag='NOPAT_TOO_SMALL' 标记，使 RR>1.5 在单指标使用时也被识别；④ 与 IDX3 同步修正 income_tax 空值处理。

#### 30. Akre 三腿凳质量复利评分（Akre Three-Legged Stool Quality Compounder Score）

- **提出者**：Chuck Akre（三腿凳）+ Tom Gayner（股本不稀释腿）
- **公式**：`三腿全过才算合格（AND 逻辑，不是加权平均）：腿 1「生意质量」= roce_10y_years ≥ 8 且 roce_10y_median ≥ 0.15 且 roce_10y_min ≥ 0.10 且 roce_10y_cv ≤ 0.35 且 gm_5y_median ≥ 0.35 且 gm_5y_std ≤ 0.03；腿 2「管理层再投资能力 + 股本不稀释」= incr_roic_5y ≥ 0.15 且 reinvest_rate_5y ∈ [0.30, 1.50] 且 net_dilution_5y ≤ 0.02；腿 3「跑道（代理）」= cash_conversion_5y ≥ 0…`
- **含义**：Akre 的三腿凳 = 好生意、会再投资的管理层、足够长的跑道，缺一条凳子就倒。这里把前两条完整落地，第三条只能是代理：库里没有 TAM、没有市场份额空间、没有定价权的直接观测，只能用「收入还在增长 + 再投资没有停滞 + 现金转换健康」来证明跑道尚未耗尽。
- **为什么超出 PE/PB**：PE/PB 是单点估值，三腿凳是「这台机器还能转多久」的结构性判断。它解释的是：同样的 PE，为什么有的公司能复利 20 年、有的 3 年就到顶。
- **字段映射**：exact 8 / approximate 0 / missing 2
  - ✔ `roce_history.roce / indicator_snapshot.roce_10y_median` ← 腿 1 的 ROCE 部分：同指标 3，已物化，可直接复用
  - ✔ `income_statement.revenue / income_statement.cost_of_revenue` ← 腿 1 的毛利率部分：同指标 2
  - ✔ `income_statement.total_profit + financial_expenses / income_tax；balance_sheet.total_assets − total_current_liabilities` ← 腿 2 的 NOPAT 与 IC：同指标 4
  - ✔ `share_capital_history.total_shares（按 effective_date 取 <= 报告期的最近一笔）` ← 腿 2 的时点股本：5,551 只股票、1990 起、244,691 行；**绝不能用 stock_meta.total_shares（当前股本）**
  - ✔ `xdxr.songzhuangu` ← 腿 2 的送转倍数（用于剥离送转）：184,486 行里 58,634 行非空；Π(1+songzhuangu) 即窗口内送转累积倍数
  - ✔ `cash_flow.cf_from_operating − cash_flow_activity.capex` ← 腿 3 的现金转换：同指标 1
  - ✔ `income_statement.revenue（4 年 CAGR）` ← 腿 3 的收入增速：也可用 indicator_snapshot.revenue_cagr5 做快照校验
  - ✔ `stock_meta.is_listed` ← 去重用的上市状态：北交所旧代码（8xxxxx）与现用代码（92xxxx）是同一家公司，旧代码 is_listed=False
  - ✘ `（库中无对应字段）` ← 管理层诚信 / 资本配置意愿：Akre 原意里的「integrity + owner-operator」无法从财报数据观测；只能用品行更好的代理（股本不稀释）承担
  - ✘ `（库中无对应字段）` ← 再投资跑道长度 / TAM：跑道本质不可计算，只能用「收入仍在增长 + 再投资未停滞」代理，必须如实标注
- **历史可用**：受最短板约束：ROCE 腿要 ≥8 个有效年报，增量 ROIC 腿要 6 个年报（2013 年起），股本稀释腿取决于 share_capital_history 是否覆盖该时点（5,551 只，1990 起）。实测可完整评分 6,161 只（含已退市、未去重），按 stock_meta.is_listed 去重并剔除 B 股后剩 5,460 只。做「三腿凳评分的历史中位数」不现实（窗口太长、股本链会断），只建议做最新截面 + 逐年回看（…
- **主要陷阱**：① 第三腿「再投资跑道」本质不可计算：TAM、市场份额空间、定价权、管理层能力库里都没有对应字段。任何打分都只是代理。必须如实标 missing，不能让 stool_score 看起来像有跑道数据支撑。② 三腿必须用 AND（乘积 / min）而不是加权平均。Akre 原意是「缺一即废」；加权平均会让「质量极高 + 跑道为 0」的公司拿到 0.67 分冒充合格。③ **股本稀释口径**：share_capital_history.change_reason 有 99.96% 是 NULL（244,584/244,691），无法区分送转和定增。本式改用「时点股本增长 ÷ 送转累积倍数 − 1」（…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ SQL：无。为 6 条中最重的一条（含 correlated subquery 逐股回溯股本），仍完整跑通，返回 5,460 行；leg1/leg2/leg3 均非全 0（64/300/291），无全 NULL 列。
  - 🛠 修法：① 腿 2 稀释判据改为 COALESCE(d.net_dilution_5y,0) <= 0.02 且 d.net_dilution_5y IS NOT NULL（缺失应判「不可评估」而非「通过」），或单独输出 dilution_data_missing 标记列；②「business_breakdown（203 万行）」改为 1,012,409 行；③ leg_cc 改为「近 5 个日历年」，亏损年置 NULL，并输出窗口实际跨度年数；④ 建议把三腿拆成三个独立可筛选字段（报告自己已建议），并把各腿实际窗口年数一…


### 3.6 风险 / 周期 / 安全边际流派

**覆盖大师**：Howard Marks、Seth Klarman、John Templeton、Jean-Marie Eveillard、Marty Whitman

**本流派审查总评**：逐条对抗性复核结论：未发现任何伪造字段——6 个指标共 42 处 db_field 引用（覆盖 balance_sheet / income_statement / indicator_ext / indicator_snapshot / stock_meta / dividends / index_valuation / treasury_yield_curve / ah_market_snapshot / price_daily_raw / price_daily_qfq / share_capital_history / funding_events 等 13 张表）全部命中 information_schema，全库 65 张表与报告所述一致，无 fixed_assets 式语义偷换、无 exchange_gain 式张冠李戴。6 条 formula_sql 全部可执行（分别返回 69,312 / 69,312 / 3,749 / 38,878 / 33 / 69,312 行），无语法错误、无 P0 跑不通。样本量与覆盖率是本次最扎实的部分：抽验 30+ 项数字，绝大多数精确到小数点后 2-4 位——NCAV 2025 分位 -0.3318/0.0794/0.2941、NCAV>市值六只数 16/16/13/18/11/7、pre-IPO 1,701/1,811/610、r…

#### 31. 净流动资产折价率（Net-Net 折价）（Net Current Asset Value (NCAV) Discount）

- **提出者**：Seth Klarman
- **公式**：`NCAV = 流动资产合计 − 负债合计；折价率 = NCAV / 总市值。Klarman 的原始净网阈值是 NCAV/市值 ≥ 1（更严格版本要求 ≥ 1.5，即用不超过 2/3 的 NCAV 价格买入）。`
- **含义**：用清算视角估值：把全部长期资产（厂房、商誉、在建工程）当作免费赠送，只看流动资产能否覆盖全部负债，剩余部分相对市值有多大。数值越大，说明市场给出的价格相对可快速变现资产越便宜。Klarman 认为只有在这里才存在不依赖“未来会变好”的安全边际。
- **为什么超出 PE/PB**：PE/PB 都建立在“持续经营 + 账面口径”上，会被利润造假、固定资产/商誉虚高抵消掉。NCAV 直接检验“今天关掉公司能收回多少”，对这类账面美化免疫。
- **字段映射**：exact 3 / approximate 2 / missing 0
  - ✔ `balance_sheet.total_current_assets` ← 流动资产合计：金融股该字段为 NULL，天然可用来剔除银行/券商/保险
  - ✔ `balance_sheet.total_liabilities` ← 负债合计（全部负债，含非流动）：Klarman 的 NCAV 减去的是全部负债，不只是流动负债
  - △ `indicator_ext.market_cap_at_report` ← 总市值（报告期时点）：= 报告日前后最后交易日收盘价 × 当期末总股本；对 A+H 公司 total_shares 含 H 股而只用 A 股价，会高估市值→折价率被低估。indicator_snapshot.total_market_cap 只有最新一期快照，不可回溯。已复核：该字段与自建（收盘价 × as-of 股本）…
  - ✔ `stock_meta.listing_date` ← 上市状态过滤：必须 listing_date <= report_date，否则 pre-IPO 财报会污染横截面
  - △ `无单一字段，需按第 2 条公式自行折算` ← Klarman 原意的“清算折扣后 NCAV”（应收打折、存货剔除）：本式用账面全额，会系统性高估净网公司的清算价值
- **历史可用**：1993-2025 共 33 个年报点（69,312 个 股票×年 观测）可做横截面分位；但 1993-2009 每年只有 137-1,492 只（受 market_cap_at_report 覆盖限制，等于只剩老股），2010 年后为 1,808-5,347 只。→ 做“历史中位数/历史分位”建议只用 2010 年及以后（16 点），2007-2009 作为参考。实测 2025 年 NCAV/市值：p10 −0.332、p50 0.0…
- **主要陷阱**：① 金融股必须显式剔除：2025 年 total_current_assets 为 NULL 的 100 只里 98 只属 financial_sector_items，若不写 IS NOT NULL 而用 coalesce 补 0，银行会被算成“NCAV = −负债合计”直接砸到分布底部。② 不能做二值筛选：NCAV > 市值 的股票 2020/2021/2022/2023/2024/2025 分别只有 16/16/13/18/11/7 只（占全样本 0.13%-0.42%）——只能当连续分位指标用。③ 选出来的净网股几乎全是地产开发商：2025 年 7 只里 600325 华发（NCAV/…
- **审查裁决**：**✅ 通过**　ROCE 同型风险：低
  - 🛠 修法：把 history_years 里的『1993-2025 共 33 个年报点』改为『1990-2025 共 36 个年报点，其中 1990-1992 仅 54 只（建议弃用），可用区间 1993-2025』；trap③ 的『几乎全是地产开发商』改为『7 只中 3 只属房地产（600048/600325/000965），其余为建筑装饰、贸易、医药商业、轻工制造——共同点是存货/应收占流动资产比重极高』。SQL 本身无需改动。

#### 32. 折价清算价值/市值（安全边际折价率）（Haircut Liquidation Value to Market Cap）

- **提出者**：Seth Klarman
- **公式**：`清算价值 LV = 货币资金 + 交易性金融资产 + 0.85×(应收账款 + 应收票据 + 合同资产) + 0.50×存货 − 负债合计；折价率 = LV / 总市值。系数即 Klarman 的“可变现折扣”，0.85/0.50 为可配置参数。`
- **含义**：对 NCAV 做一次“按变现能力打折”的修正：现金按 100%，应收按 85%（坏账折扣），存货按 50%（清仓折价），然后减全部负债。它回答的是“公司主动清盘、把资产贱卖之后，股东还能剩多少”，比账面 NCAV 更接近 Klarman 所说的安全边际。
- **为什么超出 PE/PB**：PB 用股东权益做分母，而股东权益里商誉、无形资产、在建工程可能一文不值；LV 只承认现金和打折后的营运资产，能直接看出“账面净资产里有多少是水”。
- **字段映射**：exact 5 / approximate 3 / missing 1
  - ✔ `balance_sheet.monetary_funds` ← 货币资金：含受限资金（如保证金/票据保证金），本式未扣减受限部分
  - △ `balance_sheet.trading_financial_assets` ← 交易性金融资产：覆盖率剧烈波动：2016-2018 年只有 1,000/915/1,147 行有值（准则切换期），2020 年 3,124，2025 年接近全覆盖。coalesce 当 0 会在这几年系统性低估 LV
  - ✔ `balance_sheet.accounts_receivable` ← 应收账款：0.85 折扣
  - ✔ `balance_sheet.notes_receivable` ← 应收票据：0.85 折扣；银行承兑票据实际变现率接近 100%
  - △ `balance_sheet.contract_assets` ← 合同资产：2020 年新收入准则才有该科目，2016 年全库仅 1 行 → 2020 年前恒为 0，跨年不可比
  - ✔ `balance_sheet.inventory` ← 存货：0.50 折扣；地产/工程类存货（土地、在建楼盘）实际折价率远低于 50%
  - ✔ `balance_sheet.total_liabilities` ← 负债合计：全额扣减，含预计负债/递延所得税负债
  - △ `indicator_ext.market_cap_at_report` ← 总市值：同第 1 条：A+H 公司高估市值
  - ✘ `无` ← 折扣系数本身（真实变现率）：库里没有资产处置/减值回款数据，系数只能外生设定
- **历史可用**：与 NCAV 同步：1993-2025 共 33 个年报点 / 69,312 个观测（本式显式要求 total_current_assets IS NOT NULL，故实际剩余 2000 年后的全部非金融股）。2025 年实测：n=5,347，p10 −0.565、p50 0.004、p90 0.179；LV>0 的 2,763 只。
- **主要陷阱**：① 几乎不产生正折价：2025 年 LV ≥ 市值 的股票 0 只，LV ≥ 2/3 市值 的只有 2 只 → 这个指标在 A 股只能当“分位/排序得分”，绝不能用 Klarman 原书的 66% 阈值做二值筛选，否则永远空集。② 折扣系数是外生的：0.85/0.50 对制造业合理，对地产（存货=地皮）、工程建设（合同资产）、贸易（应收）都过于乐观，必须按 sw_level1 分行业换系数。③ 三个 coalesce 全是雷：trading_financial_assets（2016-2018 覆盖骤降）、contract_assets（2020 前不存在）、notes_receivable（…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：①SQL 加 AND b.total_liabilities IS NOT NULL（护栏，当前 0 行受影响但防未来）；②把『2025 年接近全覆盖』改为『2025 年 61.6%（3,464/5,625）有值』；③把 trap⑤ 的『相关系数接近 1』改为『2025 年实测 0.77』；④history_years 的『33 个年报点』口径同第 1 条修正。

#### 33. 扣非低 PE 分位 + 预期总回报率（双闸门）（Deducted-Earnings PE Rank with Total-Return Gate）

- **提出者**：John Templeton
- **公式**：`闸门一：PE_ded = 总市值 / 扣非净利润，取当年全市场 PE_ded 分位 ≤ 20%（Templeton 原表述为“低于市场平均的一半”）；闸门二：预期年化总回报 = 扣非净利润 5 年 CAGR + 股息率 ≥ 15%（原表述为“买入时就要能算出未来 5 年每年 15% 的回报，而不是指望别人接盘”）。两闸门 AND。`
- **含义**：Templeton 在全球范围内找“最便宜 + 增长还在 + 有现金分红”的标的，核心是把“便宜”换算成可算的算术回报。分母用扣非净利润而不是净利润，是为了剥离债务重组、处置资产这类一次性收益对分母的污染。
- **为什么超出 PE/PB**：库里的 indicator_snapshot.pe_ttm 是净利润口径且只有最新一期快照（无法回溯历史分位）；换成扣非净利润后 PE 才反映主营盈利，再叠加 CAGR+股息率，就把“估值低”变成了“每年能拿回多少”。
- **字段映射**：exact 3 / approximate 2 / missing 1
  - ✔ `income_statement.deducted_net_profit` ← 扣非净利润：2007 年才入库；2025 年 5,622/5,625 只非空
  - △ `indicator_ext.market_cap_at_report` ← 总市值：A+H 公司高估
  - ✔ `income_statement.deducted_net_profit（2020 与 2025 两个年报日两端计算）` ← 5 年扣非 CAGR：indicator_snapshot.deducted_profit_cagr5 只有最新快照，不能回溯，必须自建
  - ✔ `dividends.dividend_per_share` ← 股息（近 12 个月每股派息）：1991 年起，已折算为每股；需按 ex_date 归集
  - △ `dividends.dividend_per_share / indicator_ext.market_cap_at_report × 总股本 或 直接 dps / 年末收盘价` ← 股息率：未做送转股还原，含大比例送转的公司会被系统性高估
  - ✘ `无（库里只有 A 股）` ← 全球跨市场相对估值（Templeton 的“全球最低 PE”）：Templeton 原方法是跨国比较；本项目只能在 A 股内部做横截面分位
- **历史可用**：受扣非净利润入库起点限制：PE_ded 的历史分位只能做 2007-2025 共 19 个年报点（扣非覆盖率 2007 年 1,661/2,349=71%、2016 年 3,222/5,085=63%、2025 年 5,622/5,625=99.9%）；5 年 CAGR 闸门需两端扣非同时 > 0，2020→2025 实测可用 2,423 只。净利润口径可回溯到 1991 年，但含一次性收益，不建议用于历史中位数。
- **主要陷阱**：① 最大的坑是一次性收益幻觉：000656 2025 年报 net_profit +247.7 亿、归母 +293.2 亿，但 deducted_net_profit −355.8 亿、investment_income 658.96 亿 —— 用净利润算出来 PE 只有 0.62x，是纯幻觉。2025 年全市场 3,749 只扣非>0 的样本里，有 259 只是“净利润>0 但扣非<=0”，另有 1,696 只扣非<=0，必须先用 ded>0 出局。② A 股扣非 PE < 10x 的股票 2025 年只有 79 只（<8x 只有 49 只）→ 直接套用 Templeton 的“PE<10”…
- **审查裁决**：**❌ 不通过**　ROCE 同型风险：**高**
  - ⚠️ SQL：SQL 可执行（返回 3,749 行），但 div_yield_pct 一列恒为 0，属单位错配：代码为 100.0 * coalesce(d.dps_12m,0) / b.mc，其中 dps_12m 是每股派息（2025 中位数 0.20 元/股），b.mc 是总市值（中位数 75.8 亿元），漏乘总股本或未改为除以收盘价。实测该列 min=max≈0、99.9 分位 5.83e-08、最大值仅…
  - ⚠️ ROCE 同型问题：正是 ROCE 事故的同型复发：分子每股派息 ÷ 分母总市值，量级差 1e9 倍，结果列全 0 却不报错，静默污染了『预期总回报』这一整道闸门——与当年『固定资产净值直译 PPE 导致 219 只分母为负』属同一类错误（先信字段名/公式形态，未做量级核验）。另有一处同性质隐患：pe_ded 最大值 20,751x（未做下限约束，扣非极小值把 PE 拉到 4 位数），虽被 percent_rank 吸收，但任何绝对阈值都会失效。单位维度上 mc/ded 两者同为人民币元、正确；错的是 dividend 那一项。
  - 🛠 修法：①把 div_yield_pct 改为 100.0*coalesce(d.dps_12m,0)/nullif(px.close_end,0)（年末收盘价，取 price_daily_raw 中 trade_date<=报告日的最后一条），或等价地 100.0*dps_12m*m.total_shares/mc——务必与 data_mapping 里的 note 保持一致；②total_return_no_rerating_pct 同步修正，并加自检断言 div_yield_pct 中位数应落在 0.1%~5%；③给…

#### 34. 个股周期调整市盈率（10 年正常化盈利）（Stock-level CAPE10 (Cyclically Adjusted PE)）

- **提出者**：Jean-Marie Eveillard
- **公式**：`CAPE10 = 总市值 / (过去 10 个完整年报的归母净利润之和 ÷ 10)；正常化盈利收益率 = 1 / CAPE10。Eveillard 的用法是：只在“按周期平均盈利算也很便宜”时买入，并用周期平均而不是单年盈利判断贵贱。`
- **含义**：把分母从“最近一年利润”换成“十年平均利润”，抹平周期波动和一次性损益。数值越高说明当前市值相对公司的中周期赚钱能力越贵。Eveillard 长期持有 First Eagle 的全球价值组合，核心就是用正常化盈利对抗“周期顶点的低 PE 陷阱”。
- **为什么超出 PE/PB**：单年 PE 在周期顶会显示“很便宜”（利润虚高），在周期底会显示“很贵”（利润被压缩），方向完全是反的。CAPE10 把分母拉平，才能区分“真便宜”和“周期高点的假便宜”。
- **字段映射**：exact 1 / approximate 2 / missing 1
  - ✔ `income_statement.parent_net_profit` ← 归母净利润（10 年逐年年报）：年报口径 1990 年前后就有，2007 年后完整；income_statement 是累计(YTD)制，只能取年报日
  - △ `indicator_ext.market_cap_at_report` ← 总市值：A+H 公司高估；2010 年前覆盖不足（2000 年 864 只、2010 年 1,840 只）
  - ✘ `无（全库无 CPI/通胀表）` ← 通胀调整（Shiller 原版用实际价格/实际 EPS）：只能做名义口径，A 股 10 年累计通胀约 20-25%，会使早期利润被高估、CAPE 偏低
  - △ `index_valuation.pe_ttm（31 个申万一级行业指数，2006-01-04 起日频）+ stock_meta.sw_level1` ← 行业相对估值（跨行业不可比时的归一化基础）：用行业 PE 分位做归一化，可替代缺失的全球跨市场比较
- **历史可用**：个股层面：≥10 年年报盈利史的股票 5,109 只（≥7 年 6,028 / ≥5 年 6,142），全部 6,242 只有年报盈利记录 → 个股 CAPE10 可算 2000-2025 年（需 10 年窗口），实测 2025 年有 3,597 只能算出完整 10 年。市场级聚合：agg_cape10 2010-2025 共 16 个年报点（更早年份因市值覆盖不足<1,500 只而不可靠）。
- **主要陷阱**：① 成长股被系统性惩罚：2025 年单股 CAPE10 中位数 50.6，而同年全 A 聚合 PE 仅 18.65、单年 PE 中位 47.3 —— 因为 10 年均值只有当期利润的 0.4-0.6 倍。所以 CAPE10 的绝对值绝不能拿去和“PE<15 才便宜”的门槛比，只能做同年横截面分位。② 方向可能与初衷相反：若公司刚经历周期底（分母 10 年均值 高于当期利润），CAPE10 看起来贵；若刚经历周期顶，CAPE10 看起来便宜 —— 必须同时要求“过去 10 年至少 8 年扣非为正”，否则均值被亏损年拉低成假便宜。③ 无 CPI 表，Shiller 原版的“实际值调整”做不了 →…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：①给分母加下限（如 w.avg_np10 > 0.01×总资产，或直接按当年分位截尾），或在输出里显式标记 cape10>1000 为不可用，避免 43,766x 这类值直接进 UI；②trap⑤ 注明该序列取自 total_operating_revenue；③复核『单年 PE 中位 47.3』的口径（重算 45.61），若口径不同请写明（是否含亏损股、是否用扣非）；④可补一句：2016-2025 连续 10 年窗口完整的个股为 4,571 只，CAPE10 可算 3,597 只，缺口来自『10 年均值≤0 或当…

#### 35. 市场温度计（全 A 横截面聚合，市场级而非个股）（Market Thermometer (cross-sectional aggregate, market-level)）

- **提出者**：Howard Marks
- **公式**：`温度 = 各刻度的历史分位加权：刻度A 全 A 聚合 PE = Σ总市值 / Σ(净利>0 的净利润)；刻度B 全 A 聚合 CAPE10 = Σ总市值 / Σ(10 年均净利)；刻度C ERP = 100 / 沪深300 PE_TTM − 10 年期国债收益率（反向计分）；刻度D 破净占比 = PB<1 的股票数占比（反向计分）；刻度E 日换手率中位数；刻度F 年度翻倍股占比。刻度G 信用利差 = 无数据（missing）。`
- **含义**：Marks 的“市场温度”不是预测涨跌，而是回答“现在这个价格买入的人，未来还能不能赚到钱”。温度高（估值分位高 + 风险偏好高）意味着预期回报低、下行风险大；温度低则相反。它是市场级（横截面聚合）指标，不能套到单只股票上。
- **为什么超出 PE/PB**：个股 PE/PB 回答不了“整个市场贵不贵”。把全 A 横截面聚合成 1993 年至今 33 个年报点的历史序列后，才能说“现在是历史第几档”；再叠加 ERP（相对无风险利率）与风险偏好刻度，才能区分“便宜但无人敢买”和“贵但还在抢”。
- **字段映射**：exact 3 / approximate 3 / missing 2
  - △ `indicator_ext.market_cap_at_report + indicator_ext.book_to_market + income_statement.net_profit` ← 全 A 聚合 PE 与破净占比的原子字段：聚合口径自建；book_to_market = 净资产/市值 = 1/PB（已复核：600519 2025 = 0.1419 对应 PB 7.05），破净判定是 book_to_market > 1
  - △ `index_valuation.pe_ttm（index_code='000300'）` ← 沪深 300 PE_TTM：2005-04-08 起 5,265 个交易日，source=legulegu，confidence=approximate：覆盖到 2026-11-13，但 pb 仅 264 行（月频）、div_yield 仅 55 行（只有 2026 年）
  - ✔ `treasury_yield_curve.yield_pct（tenor_years=10）` ← 10 年期国债收益率：2006-03-01 起日频 5,179 行，source=czb_mof，confidence=strict；另有 0.25/0.5/1/2/3/5/7/30 年 8 个期限同样覆盖
  - ✔ `price_daily_raw.turnover_rate` ← 日换手率：1,756 万行，至 2026-11-16
  - ✔ `price_daily_qfq.close（年内首/末交易日）` ← 年度翻倍股占比（风险偏好）：前复权价；注意用前复权而非原始价
  - ✘ `无（全库 65 张表均无企业债/信用债收益率）` ← 信用利差 / 高收益债利差：已按 %bond%/%credit%/%spread% 全表检索，只命中 balance_sheet.bonds_payable、cash_flow_activity.bond_issued 等会计科目和 indicator_snapshot.div_yield_spread_*（仅最新快照）。Ma…
  - △ `ah_market_snapshot.ah_premium_pct` ← A/H 溢价（跨境风险偏好）：仅 203 行，不足以构成历史序列
  - ✘ `index_valuation.div_yield` ← 沪深 300 历史股息率：5,265 行中只有 55 行有值且全在 2026 年 → 无法做历史分位
- **历史可用**：聚合 PE：1993-2025 共 33 个年报点（2025 = 18.65x，历史 59 分位；极值校验合理：2000 年 55.36x、2007 年 38.38x、2013 年 10.65x、2018 年 11.70x）；聚合 CAPE10：2010-2025 共 16 点（2025 = 23.25，2018 低点 17.48、2010 高点 39.33）；ERP：2006-03-01→2026-11-13 共 5,051 个日频点…
- **主要陷阱**：① 横截面聚合必须按 stock_meta.listing_date <= report_date 清洗：balance_sheet 年报 2015 年 4,778 行里 1,701 行是未上市（pre-IPO）财报、2019 年 5,898 行里 1,811 行、2022 年 5,996 行里 610 行 —— 不清洗会让 2015-2019 的聚合值有 31%-36% 的样本是幽灵公司。② 聚合 PE 的分子分母口径必须锁死：Σ全部市值 / Σ(仅盈利股的净利) 会系统性低估 PE（亏损股计入分子但不进分母），实测 2025 年该值为 18.65x，而同年单年 PE 中位数是 47.34x…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：①给 cape/cape_agg 加样本量下限（如 HAVING count(*) >= 1500，或仅在 yr>=2010 输出 agg_cape10），避免 1998-2009 的 67x~106x 垃圾值混进温度分位；②把 temp_* 分位的窗口显式限制在各自可用年份区间（agg_pe 1993-2025、agg_cape10 2010-2025、ERP 2006-2026、换手 1994-2025）；③修正三处峰值/谷值表述：破净峰值是 2005 年 13.38%（不是 2018 年 11.78%）、换手…

#### 36. 资产负债表弹性（净营运资本弹性 + 净现金占比）（Balance-Sheet Elasticity）

- **提出者**：Marty Whitman
- **公式**：`弹性得分 = 三个刻度的年内分位加权：① CA/TL = 流动资产合计 / 流动负债合计（越高越好）；② 净现金/市值 = (货币资金 + 交易性金融资产 − 有息负债) / 总市值，其中有息负债 = 短期借款 + 长期借款 + 应付债券 + 租赁负债；③ 有息负债/归母权益（反向）。可选第四刻度：商誉/归母权益（反向）。`
- **含义**：Whitman 认为财务弹性本身就是安全边际：一家流动性充裕、净现金充裕、几乎不欠银行钱的公司，即使业务暂时不顺，也永远不会被迫在最低点卖资产或增发。这三个刻度的组合就是“这家公司能扛多久”的量化版本。
- **为什么超出 PE/PB**：PE/PB 完全不看负债结构：同样 10 倍 PE，一家净现金占市值 30% 和一家有息负债是权益 2 倍的公司，风险天差地别。弹性指标把“活下去的能力”变成可比较的分位。
- **字段映射**：exact 5 / approximate 3 / missing 0
  - ✔ `balance_sheet.total_current_assets` ← 流动资产合计：金融股为 NULL，本身即金融股识别器（2025 年 100 只 NULL 中 98 只属 financial_sector_items）；sw_level1 有 750 只 NULL，csrc_l1 标签脏（同花顺/指南针在“金融业”里）
  - ✔ `balance_sheet.total_current_liabilities` ← 流动负债合计：同上，金融股 NULL
  - ✔ `balance_sheet.monetary_funds` ← 货币资金：含受限资金，未扣减
  - △ `balance_sheet.trading_financial_assets` ← 交易性金融资产：2016-2018 年覆盖骤降（1,000/915/1,147 行），coalesce 当 0 会低估净现金
  - △ `balance_sheet.short_term_loans / long_term_loans / bonds_payable / lease_liabilities` ← 有息负债四件套：lease_liabilities 有准则断点：2013-2020 每年只有 0-210 行有值，2021 起约 5,000 行（新租赁准则）→ 跨年不可比；indicator_snapshot.interest_bearing_debt 只含短借+长借+应付债券，不含租赁负债
  - ✔ `balance_sheet.total_equity_parent` ← 归母权益：不要用 total_equity（含少数股东权益），否则高少数股东权益的公司杠杆被低估
  - ✔ `balance_sheet.goodwill` ← 商誉（可选反向刻度）：大量 NULL 是正常的（公司本来就无商誉），但做“商誉为 0”判定时不能盲目 coalesce——需区分“无商誉”与“未披露”
  - △ `indicator_ext.market_cap_at_report` ← 总市值：A+H 公司高估市值→净现金占比被低估
- **历史可用**：1993-2025 共 33 个年报点 / 69,312 个观测（含市值字段）；CA/TL 本身可回溯到 1994 年（覆盖 99%），即使没有市值数据也能单独做历史中位数。实测年度中位 CA/TL：1993 年 2.01 → 2005 年谷底 1.12 → 2025 年 1.78；净现金/市值中位：2010 年前长期为负（2005 年 −0.144）→ 2010 年后转正（2025 年 +0.034）。
- **主要陷阱**：① 金融股必须剔除：银行/券商/保险没有流动与非流动划分（total_current_assets 与 total_current_liabilities 全 NULL），CA/TL 对它们无定义。② lease_liabilities 跨年断点：2013-2020 每年只有 0-210 行有值，2021 年起约 5,000 行（新租赁准则实施）→ 含租赁负债的有息负债口径在 2020/2021 之间有一次性跳升，做历史分位必须分段或用不含租赁的口径。③ 地产/工程类的 CA/TL 会虚高（存货计入流动资产），2025 年实测 CA/TL 中位 1.78、p90 5.35、CA > 2×TL…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：**高**
  - ⚠️ ROCE 同型问题：这是本流派最接近 ROCE 事故的一处，且报告没写进 traps：ibd_to_equity = 有息负债 / nullif(total_equity_parent,0)、goodwill_to_equity 同理，nullif 只挡 0 不挡负数。实测 2025 年 total_equity_parent ≤ 0 的股票有 36 只（5,525 只中），直接导致 ibd_to_equity 出现 33 个负值，全区间 -36.77 ~ +90.99，1% 分位 0、99% 分位 6.28——即极端分组里全是负权益公司，杠杆 91 倍（600185）、51 倍（002069）、42 倍（300…
  - 🛠 修法：①对两个『÷归母权益』的比率加分母正数护栏：WHERE b.total_equity_parent > 0（或比率处 nullif + GREATEST 组合），并把被排除的 36 只单独标注为『资不抵债、弹性分不可计算』，而不是让它产生 -36.77x / +90.99x；②在 traps 里补第 7 条：『归母权益≤0 的公司（2025 年 36 只）会让 ibd_to_equity 与 goodwill_to_equity 符号翻转，必须先剔除』；③把『2025 年接近全覆盖』改为『61.6%』。SQL 其余…


### 3.7 财务排雷 / 多因子评分

**覆盖大师**：Edward Altman、Joseph Piotroski、Messod Beneish、James Ohlson、James Montier

**本流派审查总评**：结论：pass_with_fixes（无 P0）。6 个指标、191 处字段引用（Z 27 + Z'' 26 + F 36 + M 44 + O 18 + C 40，其中 181 处可直接解析到真实表列、其余为 CTE 内部列引用）逐列比对 information_schema 的 65 张表 1,299 列，全部命中，0 个伪造字段或伪造表名；此前最可疑的 funding_events、balance_sheet_ext、share_capital_history、xdxr、csmar_per_share_history 均真实存在。6/6 formula_sql 包一层 SELECT 均可跑通（Z 69,306 行 / Z'' 105,037 / F 94,921 / M 95,826 / O 105,022 / C 72,775），无一条 P0 语法错误。正面抽验（均为精确命中，说明大部分数字是真跑出来的）：contract_liabilities 2016 年恰好 1 行（600382，值 0）；indicator_ext.ebit = total_profit + coalesce(financial_expenses,0)，2024 年 5,903/5,904；indicator_ext.accruals = (net_profit - cf_from_operatin…

#### 37. Altman Z 值（制造业经典 5 变量破产预警）（Altman Z-Score (1968, manufacturing, 5-variable)）

- **提出者**：Edward Altman
- **公式**：`Z = 1.20·X1 + 1.40·X2 + 3.30·X3 + 0.60·X4 + 1.00·X5，其中 X1 =(流动资产合计−流动负债合计)/资产总计；X2 = 留存收益/资产总计；X3 = EBIT/资产总计；X4 = 普通股市值/负债合计；X5 = 营业收入/资产总计。判读（Altman 原始样本阈值）：Z>2.99 安全区，1.81≤Z≤2.99 灰色区，Z<1.81 危险区。`
- **含义**：用 5 个互相独立的维度合成一个破产概率序数：短期偿债能力（X1）、累积盈利能力（X2）、当期真实赚钱能力（X3）、市场对权益的定价相对负债的缓冲（X4）、资产周转效率（X5）。它衡量的是「这家公司离资不抵债还有多远」，而不是「贵不贵」。
- **为什么超出 PE/PB**：PB 只看一个静态比值，且分母（净资产）恰恰是最容易被动过手脚、也最容易被商誉撑起来的科目；Z 值把负债结构、留存收益累积、EBIT 与市值缓冲同时纳入，能区分「PB 低是因为便宜」还是「PB 低是因为净资产正在被亏损吃掉」。
- **字段映射**：exact 4 / approximate 4 / missing 1
  - ✔ `balance_sheet.total_current_assets − balance_sheet.total_current_liabilities` ← X1 分子：营运资本（流动资产−流动负债）：两列语义完全对应。金融股这两列为 NULL（见 traps），不要 coalesce 成 0。
  - ✘ `（库内不存在独立科目）` ← X2 的原始科目：留存收益（独立列）：对全库 65 张表做过列名检索：'%retained%' 只命中 indicator_ext.retained_earnings_per_share 与 csmar_per_share_history.retained_earnings_per_share（每股口径，需再乘股本反推，且股本又受送转影…
  - △ `balance_sheet.undistributed_profit + balance_sheet.surplus_reserve` ← X2：留存收益（retained earnings）：库里没有「留存收益」单列。A 股口径 = 盈余公积 + 未分配利润（均为归母）；不含资本公积（正确，资本公积不是留存收益）。2025 年报两列覆盖 5,620 / 5,534 行（99.9% / 98.4%）。
  - △ `indicator_ext.ebit` ← X3：EBIT：实测该列 = income_statement.total_profit + financial_expenses（2024 年报 5,903/5,904 行逐元吻合）。financial_expenses 是净额（利息支出−利息收入+汇兑+手续费），净现金公司 EBIT 被压低；2018 年后可改…
  - △ `indicator_ext.market_cap_at_report` ← X4 分子：普通股市值：实测 = 报告日收盘价 × 当期总股本（002043：11.88 × 832,058,923 = 9,884,860,005.24，逐分吻合）。含限售/非流通股；Altman 原式用的是流通普通股市值。覆盖率随年份上升：2015 年 54% → 2020 年 66% → 2024 年 90% → 2…
  - ✔ `balance_sheet.total_liabilities` ← X4 分母 / 各项分母：负债合计：含全部流动+非流动负债，语义与公式一致。
  - ✔ `income_statement.revenue（回落 income_statement.total_operating_revenue）` ← X5：营业收入（net sales）：非金融公司两者基本相同（营业总收入=营业收入+利息/保费/手续费收入，非金融部分为 0）；银行只用 revenue。2024 年 revenue 覆盖 5,899/5,904。
  - ✔ `balance_sheet.total_assets` ← 各项分母：资产总计：2025 年报 5,625 行 100% 非空。
  - △ `stock_meta.sw_level1` ← 行业适用范围过滤（制造业 / 剔除金融）：Z 原式只适用于制造业。必须排除 sw_level1 ∈('银行','非银金融')：这两类的 total_current_assets/liabilities 全为 NULL，且没有流动/非流动划分。不要用 csrc_l1='金融业'（同花顺、指南针、大智慧等软件公司也在里面）。
- **历史可用**：1994-2025 共 32 个年报点可算，但受 market_cap_at_report 覆盖约束：1994 年 224 只(32%) / 2007 年 1,329 只(59%) / 2015 年 2,555 只(54%) / 2020 年 3,847 只(66%) / 2023 年后 5,135-5,347 只(87%-97%)。→「10 年历史中位数」技术可行（2016-2025 每年 2.5k-5.3k 只），但 2015-20…
- **主要陷阱**：① 金融股必须显式剔除：银行 42 只、非银金融 79 只的 total_current_assets/total_current_liabilities 为 NULL（其余行业除 6 行未映射行业外无缺失），若 coalesce 成 0，X1 会变成「−流动负债/资产」把银行直接砸进危险区。② X4 用总市值不是流通市值，A 股次新股/大股东绝对控股会系统性抬高 Z。③ EBIT 是 total_profit+financial_expenses 的近似，financial_expenses 为净额且含汇兑与手续费，净现金公司 EBIT 被低估（本项目已踩过的陷阱 #2）。④ 阈值不能照搬：…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：1) formula_sql 里显式加 sw_level1 过滤（或 LEFT JOIN stock_meta 输出 industry NULL 标记），别依赖 NULL 巧合剔除；2)「非银金融 79 只 NULL」改为 57，「6 行未映射行业」改为「2024 年 370 行 / 全库 750 只无行业标签」，并注明 NOT IN 在 NULL 上会静默丢行；3) traps 补两条：X4 近零负债反向爆表、预收/合同负债驱动的负营运资本假阳性；4) X4 若要贴合 Altman 原意应改流通市值口径，否则写明…

#### 38. Altman Z'' 值（非制造业/新兴市场版，账面权益替代市值）（Altman Z''-Score (non-manufacturer, book-equity version) / Z''-EM）

- **提出者**：Edward Altman
- **公式**：`Z'' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4，X1=(流动资产−流动负债)/总资产，X2=留存收益/总资产，X3=EBIT/总资产，X4=权益账面价值/负债合计（注意：分子换成账面权益，不含 X5 周转项）。判读：Z''>2.60 安全，1.10–2.60 灰色，<1.10 危险。新兴市场版 Z''-EM = 3.25 + 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4。私营企业版 Z' = 0.717·X1 + 0.847·X2 + 3.107·X3 + 0.420·X4 + 0.998·X5（判读 1.23 / 2.90…`
- **含义**：同一套破产判别逻辑去掉「市场定价」这一项，改用账面上的权益缓冲，因此可以覆盖非制造业、非上市可比的场景，也能回溯到市值数据还不存在的年份。它是本库里历史最深、覆盖最稳的一个破产预警指标。
- **为什么超出 PE/PB**：PB 只告诉你「1 元净资产卖多少钱」，Z'' 告诉你「这些净资产能承受多少亏损才会资不抵债」（X4=权益/负债 就是杠杆倒数），并与盈利质量（X2/X3）联立，能在 PB 看起来便宜时识别出「净资产正在被亏损侵蚀」的陷阱股。
- **字段映射**：exact 3 / approximate 3 / missing 0
  - ✔ `balance_sheet.total_current_assets − balance_sheet.total_current_liabilities，除以 balance_sheet.total_assets` ← X1：营运资本/总资产：金融股为 NULL，WHERE 中自动剔除。
  - △ `balance_sheet.undistributed_profit + balance_sheet.surplus_reserve` ← X2：留存收益/总资产：库里无「留存收益」单列，用盈余公积+未分配利润替代（归母口径）。
  - △ `indicator_ext.ebit` ← X3：EBIT/总资产：= total_profit + financial_expenses（实测逐元吻合），净现金公司被低估；可选用 total_profit+interest_expense（仅 2018 年后可用）。
  - ✔ `balance_sheet.total_equity` ← X4 分子：权益账面价值：所有者权益合计（含少数股东权益）——与分母 total_liabilities（含全部负债）口径一致。若要归母口径改用 total_equity_parent，但会造成分子分母口径错配，少数股东权益大的地产/建筑/基建会被系统性低估。
  - ✔ `balance_sheet.total_liabilities` ← X4 分母：负债合计：总负债>总资产时 X4 为负，Z'' 崩到极端负值，分位计算前需截尾。
  - △ `balance_sheet.total_equity − balance_sheet.goodwill − balance_sheet.intangible_assets` ← （可选）有形权益修正版：商誉/无形资产会撑高 X4；商誉 2024 年只有 4,602/5,904 有值，其余是「本来没有商誉」而非缺失，可安全 coalesce 0。
- **历史可用**：1994-2025 共 32 个年报点，覆盖极高：1994 年 682/692 只(99%)、2007 年 2,262/2,263、2024 年 5,805 只、2025 年 5,525 只。→ 是本流派里唯一能直接做「20-30 年历史中位数/历史分位」的指标，可完整复用 ROCE 那套历史中位数框架（roce_history 的做法）。实测 2024 年报：中位 Z''=4.28、p10≈0.00、p90=11.36，危险区(<1.…
- **主要陷阱**：① 账面权益会被商誉/无形资产虚高 → 必须同时提供有形权益版（扣 goodwill + intangible_assets），否则「商誉型公司」在 Z'' 里永远安全。② 分子 total_equity 含少数股东权益、分母 total_liabilities 含全部负债，两者口径配套；擅自把分子换成 total_equity_parent 会造成系统性低估。③ 阈值 1.10/2.60 是 Altman 用 1990s 美国非制造业样本定的，A 股中位 4.28 远超 2.60 → 原阈值下 84% 的样本落「安全区」，必须改横截面分位或行业内标准化后再用。④ X3 继承 EBIT 的口径…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：1) 商誉覆盖数 4,602 改实测 2,766/5,904，并据此评估「可安全 coalesce 0」的强度；2)「1990s 脏行」改为「全库年报仅 1 行 ta<=0，护栏属防御性写法」；3) 危险区 951 只 / 16.38%；4) 强烈建议增加「剔除预收+合同负债后的经营性营运资本」辅助列并对比两版危险区名单，否则 26% 的「危险」是负营运资本假阳性；5) 明确 X4 用 total_equity（含少数股东）而非归母。

#### 39. Piotroski F 分数（9 项 0/1 基本面改善评分）（Piotroski F-Score (2000, 9 binary signals)）

- **提出者**：Joseph Piotroski
- **公式**：`F = f1+…+f9，每项 ∈{0,1}。盈利性：f1 = ROA>0；f2 = 经营现金流>0；f3 = ΔROA>0；f4 = 经营现金流>净利润（应计质量）。杠杆/流动性/融资：f5 = 长期负债率下降；f6 = 流动比率上升；f7 = 本财年未增发股权。经营效率：f8 = 毛利率上升；f9 = 总资产周转率上升。判读：F≥8 强，F≤1 弱（Piotroski 原样本中对高 BM 组，F 高/低组合年化差约 7.5%）。`
- **含义**：9 个信号全部是「同比改善」的方向判断而非绝对水平，因此天然免疫行业差异与规模差异：同一行业里，ROA 由负转正、现金流开始追上利润、杠杆下降、周转加快的公司被加分。分数越高代表基本面在自我修复，而不是「现在很便宜」。
- **为什么超出 PE/PB**：PE/PB 是时点静态值，无法区分「便宜且正在变好」与「便宜但还在恶化」——这正是价值陷阱的两种形态。F 分数把「改善方向」离散化成 0/1，把 PB 低 + F 低（陷阱）和 PB 低 + F 高（机会）分开。
- **字段映射**：exact 6 / approximate 4 / missing 1
  - ✔ `income_statement.net_profit / balance_sheet.total_assets` ← f1 ROA>0：合并净利润（含少数股东）配总资产，口径自洽；若要归母口径，须整套换成 parent_net_profit + total_equity_parent，不可半换。
  - ✔ `cash_flow.cf_from_operating` ← f2 经营现金流>0：2025 年报 5,624/5,625 有值。
  - △ `income_statement.net_profit、balance_sheet.total_assets（本年 vs 紧邻上年）` ← f3 ΔROA>0：本式用「期末总资产」做分母；Piotroski 原式用期初/平均总资产。总资产剧烈变动的年份（借壳、大额并购、大额再融资）两种口径可能给出相反的 0/1。
  - ✔ `cash_flow.cf_from_operating vs income_statement.net_profit` ← f4 经营现金流>净利润（应计质量）：等价于 indicator_ext.accruals<0（该列实测=(net_profit−cf_from_operating)/total_assets）。
  - △ `(balance_sheet.long_term_loans + balance_sheet.bonds_payable) / balance_sheet.total_assets` ← f5 长期负债率下降：Piotroski 原式用 LTD/平均总资产。本式刻意不含 lease_liabilities（2021 新租赁准则跳变）与一年内到期非流动负债；后者在 balance_sheet_ext.non_current_liab_due_1y 里有，但该表目前只有 2025 年一期（5,477 行），不…
  - ✔ `balance_sheet.total_current_assets / balance_sheet.total_current_liabilities` ← f6 流动比率上升：金融股为 NULL → 金融股天然无法计分。
  - △ `funding_events.event_type + funding_events.list_date` ← f7 未增发股权：event_type ∈('a_placement' 定增,'rights' 配股,'ipo') 且 list_date 落在该财年 → 视为增发。该表 1993 年起有数据（2024 年仅 128 家定增/配股，符合当年的再融资规模）。
  - ✘ `balance_sheet_ext.non_current_liab_due_1y` ← f5 更精确所需的「一年内到期非流动负债」（历史序列）：字段存在但只有 2025 年一期数据（该表 5,477 行，全部 report_date=2025-12-31），无法用于任何历史对比，故 f5 只能用「长期借款+应付债券」的简化口径。
  - △ `share_capital_history.total_shares（按 effective_date ≤ 报告日取最近一条）` ← f7 备用方案（不推荐单独使用）：会被送股/转增污染，见 traps ①。该表覆盖：2012 年 2,249/3,745(60%)、2024 年 5,434/5,904(92%)。
  - ✔ `(income_statement.revenue − income_statement.cost_of_revenue) / revenue` ← f8 毛利率上升：金融股 cost_of_revenue 全为 NULL（银行 42 只实测 0 条有值）→ 自然缺失。
  - ✔ `income_statement.revenue / balance_sheet.total_assets` ← f9 总资产周转率上升：与 indicator_ext.total_asset_turnover 同源，可直接取该列（覆盖 2024 年 5,899/5,904）。
- **历史可用**：1998-2025 共 28 个年报点（1997 年仅 3 只，可视为不可用），每年可算数量：1998 年 965 → 2007 年 1,862 → 2015 年 4,468 → 2024 年 5,800（占当年年报 98%），因为 9 项里只有 f7 依赖外部表。→ 完全可做「10 年 F 中位数」与「F 同比改善」序列。实测 2024 年报：均值 5.20、中位 5、p10=3 / p90=7，F≥8 占 8.6%（497 只）、F…
- **主要陷阱**：① f7 绝不能用「股本没变多」判断：A 股每年 400-700 家送股转增（2024 年 398 家），2024 年有 647 只(12%)股本涨≥5%，而真实定增/配股只有 128 只 → 直接看股本会把 12% 的公司误判为「增发」而丢 1 分。必须查 funding_events（或用 xdxr.songzhuangu 把送转从股本增量里剔掉）。② f5 不能把 lease_liabilities 算进长期负债，也不能用 total_liabilities/total_assets 替代：2021 年新租赁准则会让有租赁的公司长期负债跳升，造成全市场性口径断裂，误判成「杠杆恶化」。③…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - 🛠 修法：1) balance_sheet_ext 描述改为实测事实（6 期 27,303 行，最早 2025-06-30），否则复核时会被当成编造；2)「152 行 lag 为空」改 1,523 行并注明统计窗口；3) 2024 年 n 改 5,796；4) 输出层带 in_financial 标记（现在金融股被 WHERE 静默剔除，样本减少不可见）。

#### 40. Beneish M 分数（8 变量财务舞弊识别）（Beneish M-Score (1999, 8-variable earnings manipulation model)）

- **提出者**：Messod Beneish
- **公式**：`M = −4.840 + 0.920·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI + 0.115·DEPI − 0.172·SGAI + 4.679·TATA − 0.327·LVGI。其中 DSRI = (应收_t/收入_t)/(应收_{t−1}/收入_{t−1})；GMI = 毛利率_{t−1}/毛利率_t；AQI = [1−(流动资产+PPE+有价证券)/总资产]_t ÷ 同式_{t−1}；SGI = 收入_t/收入_{t−1}；DEPI = [折旧/(折旧+PPE)]_{t−1} ÷ 同式_t；SGAI = (销售+管理费用)/收入 的同比；LVG…`
- **含义**：用 8 个「结构性异常」的相对变化（应收增速超过收入、毛利率塌陷、资产质量下降、销售异常扩张、折旧率下降、费用率下降、杠杆上升、应计利润占比高）合成一个舞弊概率打分。它不是判断公司好坏，而是判断「报表数字本身有多可信」。
- **为什么超出 PE/PB**：PE/PB 全部建立在「利润和净资产是真的」这个前提上；M 分数恰恰检验这个前提。同样是 PE 10 倍，M=−2.8 的公司利润大概率是真的，M=−1.2 的公司利润可能是提前确认出来的，两者的 PE 完全不可比。
- **字段映射**：exact 3 / approximate 6 / missing 1
  - △ `balance_sheet.accounts_receivable / income_statement.revenue` ← DSRI：应收/收入指数：不含应收票据与应收款项融资（notes_receivable 2019 年只有 3,673/5,898=62% 有值）；2018-2019 年新准则把「应收票据及应收账款」合并列报，这两年口径会跳变。
  - ✔ `income_statement.revenue − income_statement.cost_of_revenue` ← GMI：毛利率指数：cost_of_revenue 2024 年覆盖 5,734/5,904(97%)。
  - △ `balance_sheet.total_current_assets + balance_sheet.fixed_assets + (balance_sheet.trading_financial_assets + balance_sheet.long_term_equity_investment)` ← AQI：资产质量指数（流动资产+PPE+有价证券）：Beneish 原式的 securities 是短期有价证券，A 股用交易性金融资产替代（2016 年只有 1,000/5,085 非空、2020 年 3,124/5,953）；fixed_assets 是 PPE 净值，符合 AQI 中 PPE 的语义（不要换成 total_non_current…
  - ✔ `income_statement.revenue` ← SGI：销售增长指数：收入<=0 的行必须剔除或单独处理。
  - △ `cash_flow_indirect.fixed_asset_depreciation（+ investment_property_depreciation）` ← DEPI：折旧率指数（分子：折旧费用）：A 股折旧费用只出现在现金流量表补充资料。备选 indicator_ext.depreciation_amortization（其实测 = ebitda−ebit，覆盖更好但含无形摊销+使用权资产折旧）。两者不能混在同一条时间序列里。
  - △ `balance_sheet.fixed_assets` ← DEPI 分母：PPE：Beneish 原式用 gross PPE，A 股只有净值（context 陷阱 #1：fixed_assets 是 PPE 净值，不是「固定资产」大区）。
  - ✘ `（库内不存在）` ← DEPI 分母的原始口径：PPE 原值（gross block）/ 累计折旧：对全库 65 张表做过列名检索：'%gross%' 只命中 indicator_snapshot.gross_margin，'%accum%' 零命中，无 PPE 原值与累计折旧科目（A 股财报附注里才有）。只能用 fixed_assets（PPE 净值）代入，DEPI 因此系统性偏 1（折旧率随资…
  - △ `(income_statement.selling_expenses + income_statement.administrative_expenses) / revenue` ← SGAI：销售+管理费用率指数：研发费用 2018 年从管理费用里拆出单列（rd_expenses：2015 年 86 条 / 2017 年 3,932 条 / 2018 年后约 94%）→ 2017→2018 年 SGAI 有全市场性下移，跨 2018 年不可直接比。
  - △ `(balance_sheet.total_current_liabilities + balance_sheet.long_term_loans + balance_sheet.bonds_payable) / balance_sheet.total_assets` ← LVGI：杠杆指数：简式（流动负债+长期借款+应付债券）/总资产；刻意不含 lease_liabilities，避免 2021 年租赁准则断裂。
  - ✔ `indicator_ext.accruals` ← TATA：总应计/总资产：实测 = (income_statement.net_profit − cash_flow.cf_from_operating)/balance_sheet.total_assets（2024 年报 5,901/5,901 完全一致，容差 1e-5）。可直接用这一列，覆盖 2024 年 5,901/…
- **历史可用**：8 变量全齐受 DEPI 约束（需要折旧）：1999 年 879 只 → 2007 年 1,451 只(80%) → 2013 年 2,489 → 2015 年 2,685 只(56%) → 2020 年 3,909 只(72%) → 2024 年 5,126 只(87%) → 2025 年 4,892 只(91%)。即 1999-2025 共 27 个年报点，2013 年后每年 ≥2.5k 只。→ 可做 10 年历史中位数，但 201…
- **主要陷阱**：① DEPI 是唯一真正受限的变量，缺失时禁止补 0：补 0 会让 DEPI→0、M 值虚降约 0.11，把可疑公司判成干净。② AQI 的 securities 项在库里只能近似（交易性金融资产 + 长期股权投资），且 trading_financial_assets 大量 NULL 是「本来没有」可 coalesce 0，但 2016 年前覆盖率仅 20%-35%，早年 AQI 的噪声很大。③ 应收口径在 2018-2019 年因新准则合并列报而跳变，DSRI 会出现「两年口径性突增」，不能当作舞弊信号；同一时期 notes_receivable 覆盖率只有 62%-90%，无法靠加总修复…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ 样本量：大部分精确、一处计数错：2024 年报 n=5,653（✓ 一字不差）、DEPI 子集 5,140（✓ 一字不差）、p10=-3.2466(声称 -3.25 ✓)、中位 -2.4869(-2.49 ✓)、p90=-1.5738(-1.57 ✓)。但「M > -1.78 占 11.8%（607 只）」自相矛盾：实测 2024 年 M>-1.78 = 669 只，占 5,653 的 11.83%（百分数对、只数错约 10%；607 既不等于…
  - 🛠 修法：1) 「607 只」改 669 只（11.83%）；2) SQL 末尾加 WHERE isfinite(m_score)，或输出 m_status∈{ok, missing_dep, nonfinite}，把 118 NaN + 124 Inf 显式隔离；3) aqi 分母改 nullif(...) 防 0/0；4)「2024 年约 250 只毛利率<=0」改实测 135 只；5) 历史口径里同时输出每年「可算只数/总只数」，避免覆盖率漂移影响分位。

#### 41. Ohlson O 分数（9 变量破产概率 Logit 模型）（Ohlson O-Score (1980, 9-factor logit bankruptcy model)）

- **提出者**：James Ohlson
- **公式**：`O = −1.32 − 0.407·SIZE + 6.03·TLTA − 1.43·WCTA + 0.0757·CLCA − 1.72·OENEG − 2.37·NITA − 1.83·FUTL + 0.285·INTWO − 0.521·CHIN。SIZE=ln(总资产/GNP 平减指数)；TLTA=总负债/总资产；WCTA=(流动资产−流动负债)/总资产；CLCA=流动负债/流动资产；OENEG=1 若总负债>总资产；NITA=净利润/总资产；FUTL=经营活动资金/总负债；INTWO=1 若近两年净利润均为负；CHIN=(NI_t−NI_{t−1})/(|NI_t|+|NI_{t−1}|…`
- **含义**：Altman 的线性打分被 Ohlson 改成 Logit 概率模型，输出的是「未来一年破产的条件概率」而不是分区。它同时把规模（SIZE）、杠杆、流动性、近期亏损趋势（INTWO/CHIN）纳入，因此对「连续两年亏损 + 净资产为负」这类 A 股退市风险股特别敏感。
- **为什么超出 PE/PB**：PE/PB 对亏损股直接失效（负 PE 无意义、PB 为负），而 O 分数对亏损、资不抵债的极端公司依然给出可比的连续值，是少数能覆盖 ST/*ST 股的估值无关风险指标。
- **字段映射**：exact 7 / approximate 2 / missing 1
  - △ `ln(balance_sheet.total_assets / 1e6)` ← SIZE = ln(总资产/价格指数)：库里没有 GNP/CPI 平减指数（treasury_yield_curve、fx_rate_daily、index_valuation 都不是），用「百万元」为单位近似原量纲；换成 ln(元) 会使整列平移约 5.6 个点并让阈值 0.5 失效。
  - ✘ `（库内不存在）` ← SIZE 的平减项：GNP / CPI 价格指数：对全库 65 张表做过列名检索：'%cpi%' / '%gnp%' / '%deflator%' / '%inflation%' / '%price_index%' / '%macro%' / '%gdp%' 全部零命中（treasury_yield_curve 只有国债收益率、fx_rate_da…
  - ✔ `balance_sheet.total_liabilities / balance_sheet.total_assets` ← TLTA = 总负债/总资产：含全部负债。
  - ✔ `(balance_sheet.total_current_assets − balance_sheet.total_current_liabilities) / total_assets` ← WCTA = 营运资本/总资产：金融股为 NULL，WHERE ca>0 自动剔除。
  - ✔ `balance_sheet.total_current_liabilities / balance_sheet.total_current_assets` ← CLCA = 流动负债/流动资产：同上，需要一个 >0 的流动资产做分母。
  - ✔ `balance_sheet.total_liabilities > balance_sheet.total_assets` ← OENEG（资不抵债哑变量）：A 股净资产为负的公司每年约 30-80 只，主要落在 ST 板块。
  - ✔ `income_statement.net_profit / balance_sheet.total_assets` ← NITA = 净利润/总资产：用合并净利润（含少数股东），与总资产口径一致。
  - △ `cash_flow.cf_from_operating / balance_sheet.total_liabilities` ← FUTL = 经营活动资金/总负债：原式是 funds from operations（净利润+折旧摊销）/总负债；折旧只有 2015 年后才较全，改用经营现金流净额替代（更严格，会让 FUTL 偏小、O 偏大）。
  - ✔ `income_statement.net_profit 当年 <0 且 lag(net_profit) <0` ← INTWO（连续两年亏损）：需保证 lag 是紧邻上年年报，否则停牌公司会拿多年前的亏损来判定。
  - ✔ `(income_statement.net_profit − lag(net_profit)) / (abs(ni) + abs(lag(ni)))` ← CHIN（净利润变化率）：分母需 >0 保护，否则双零/双微值会爆炸。
- **历史可用**：1995-2025 共 31 个年报点，覆盖极高（只需 balance_sheet + income_statement + cash_flow 三张主表）：2024 年 5,805/5,904(98%)、2025 年 5,525/5,625(98%)。→ 是本流派里历史最深、覆盖率最高的连续型指标，可直接做 10 年/20 年历史中位数与横截面分位（ROCE 那套框架可直接复用）。实测 2024 年报：中位 O=−2.86、p10=−…
- **主要陷阱**：① 缺少 GNP 平减指数：SIZE 只能用 ln(总资产/1e6) 近似，单位一旦写错，整列会平移几十个点（ln(元) 与 ln(百万元) 相差 13.8，乘 −0.407 后是 5.6 个点），阈值 0.5 立刻失效 → 单位必须写死并写进文档，跨年比较请改用分位秩。② O 的绝对值只在「同一 SIZE 定义 + 同一年」内可比：通胀会让全市场 O 逐年漂移，做时间序列必须用分位而不是原始值。③ FUTL 是替代变量（cf_from_operating 代 FFO），经营现金流已扣营运资本变动，比 FFO 波动大，制造/地产的 FUTL 会剧烈摆动，进而让 O 抖动。④ 必须先过滤金融股：…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ 样本量：2024 年报数字全对：n=5,805（与 5,904 分母一致 ✓）、中位 -2.8612(声称 -2.86 ✓)、p10=-5.2637(-5.26 ✓)、p90=-0.4429(-0.44 ✓)、O>0.5 =281 只 4.84%（声称 281 只/4.8% ✓ 精确）。但历史可用性声明是假的：「1995-2025 共 31 个年报点，覆盖极高」——实测 1990-1995 年 o_score 100% 为 NULL（1995…
  - 🛠 修法：1) history_years 改为「1998-2025 共 28 个年报点」（1995 年 0 个有效值，不能算年报点）；2) SQL 末尾加 WHERE o_score IS NOT NULL，避免输出 4,928 行全 NULL；3) 若坚持 1995 起，需先补 cash_flow（cf_from_operating 最早 1996-12-31 且早年覆盖极低）；4) 同时输出 o_pd 的分位秩，避免原始值跨年比较。

#### 42. Montier C 分数（6 项造假信号计数）（Montier C-Score (6 accounting red flags, 0-6)）

- **提出者**：James Montier
- **公式**：`C = c1+c2+c3+c4+c5+c6，每项 ∈{0,1}：c1 = 净利润 > 经营现金流（应计背离）；c2 = 应收账款周转天数 DSO 上升（应收/收入 同比变差）；c3 = 存货周转天数 DSI 上升（存货/营业成本 同比变差）；c4 = 其他流动资产/收入 上升；c5 = 折旧/（折旧+净 PPE）下降；c6 = 总资产增长 > 10%。判读：0 = 无明显痕迹，5-6 = 高度可疑（Montier 实测 C=5 且 PS>2 的组合年化跑输约 4%-8%）。`
- **含义**：不比比率高低，只数「异常迹象」有几条成立：利润跑在现金流前面、应收和存货越堆越多、费用被塞进资产科目、折旧被压低、资产靠买买买膨胀——每出现一条记 1 分。分数越高，报表越可能是「做出来的」。
- **为什么超出 PE/PB**：PE/PB 对这套信号完全失明：一家靠提前确认收入做出 PE 20 的公司和一家真实赚到 PE 20 的公司，估值指标一模一样，只有 C 分数能把两者分开。它是「估值之前的过滤器」，用来避免把假利润乘上一个倍数。
- **字段映射**：exact 4 / approximate 3 / missing 2
  - ✔ `income_statement.net_profit vs cash_flow.cf_from_operating` ← c1 净利润 > 经营现金流：等价于 indicator_ext.accruals>0；Montier 原意是「背离在扩大」，也可改用 accruals 的同比增量做变体（结果会差很多，见 traps ③）。
  - △ `balance_sheet.accounts_receivable / income_statement.revenue` ← c2 DSO 上升：原式乘 365 不影响单调性；未含应收票据/应收款项融资，2018-2019 年新准则合并列报年份口径会跳。
  - ✔ `balance_sheet.inventory / income_statement.cost_of_revenue` ← c3 DSI 上升：存货 2024 年覆盖 5,743/5,904；cost_of_revenue 为 0 的贸易/控股公司需 nullif 保护。
  - △ `(balance_sheet.prepayments + balance_sheet.other_receivables) / income_statement.revenue` ← c4 其他流动资产/收入 上升：A 股没有独立「其他流动资产」科目可用，用预付款项+其他应收款近似；遗漏 contract_assets（2024 年 2,521 条有值）与其他流动资产科目——这两处恰是费用资本化的高发地。
  - ✘ `（库内不存在）` ← c5 的原始口径：PPE 原值（gross PPE）：全库 65 张表无 gross PPE / 累计折旧列（'%gross%' 仅命中 gross_margin，'%accum%' 零命中），只能用净 PPE 代入 → 折旧年限长的老资产公司 c5 系统性不触发。
  - ✘ `（库内不存在独立科目）` ← c4 的原始科目：其他流动资产（独立列）：全库检索 '%other_current%' / '%other_assets%' 零命中；balance_sheet 只有 other_receivables、prepayments、contract_assets、deferred_tax_assets 等明细项，没有汇总的「其他流动资产」，因此…
  - △ `cash_flow_indirect.fixed_asset_depreciation（备选 indicator_ext.depreciation_amortization）除以同式加 balance_sheet.fixed_assets` ← c5 折旧/（折旧+PPE）下降：Montier 原式用 gross PPE，A 股只有净 PPE（fixed_assets）；净额做分母会随折旧年限拉长而上升，令 c5 系统性偏「不触发」。
  - ✔ `balance_sheet.total_assets 同比` ← c6 总资产增长 > 10%：阈值 10% 是 Montier 的绝对阈值；A 股触发率随年份从 23% 到 50% 大幅波动（见 traps ⑤）。
  - ✔ `indicator_snapshot.ps_ttm` ← （可选）价格配合条件 PS > 2：Montier 的组合条件是 C 高 + PS 高；indicator_snapshot 只有最新一期（5,575 行），历史 PS 需用 indicator_ext.market_cap_at_report/revenue 自算。
- **历史可用**：6 个信号里只有 c5 依赖折旧（cash_flow_indirect），其余 5 个从三张主表即可算 → 整体从 1999 年起可用（1998 年折旧列为空）。可算数量与 Beneish 同源：2015 年 2,737 只(57%)、2020 年 3,934 只(66%)、2024 年 5,380 只(91%)。→ 可做 10 年历史中位数，但 2013-2020 的覆盖率在 57%-77% 波动。实测 2024 年报：均值 2.54…
- **主要陷阱**：① c5 只能用净 PPE（fixed_assets），原式要 gross PPE：A 股财报不披露原值 → 折旧年限长、资产老的公司 c5 天然不触发，等于对钢铁/水泥/电力这类重资产老化行业放水。② c4 用「预付款项+其他应收款」近似「其他流动资产」，会漏掉 contract_assets 与「其他流动资产」科目本身——造假的常见藏身处恰好在这里，因此 c4 的召回率偏低，不要把它当完整信号。③ c1 若改成「应计背离扩大」的变体（accruals_t > accruals_{t−1}），2024 年触发率比按符号判定低约 15 个百分点，两种定义不能混用在同一张评分卡上。④ 分母为 0…
- **审查裁决**：**🟡 需修正**　ROCE 同型风险：低
  - ⚠️ 样本量：2024 年三个关键数字两个精确：n=5,380（✓ 一字不差）、C>=5 =420 只占 7.81%（声称约 420 只/7.8% ✓ 精确）；但均值实测 2.512、声称 2.54（差 1.1%）。历史覆盖数对不上：「2015 年 2,737 只」实测 2,792、「2020 年 3,934 只」实测 4,109（差 175 只/4.5%）；「与 Beneish 同源」只对 2024 年成立（Beneish 2020 年 n_val…
  - 🛠 修法：1) 六个 CASE 全部改成「先判 NULL 再判 0/1」，并输出 c_score 与 c_valid_n（可算信号数）；2) 修正历史覆盖数（2015=2,792、2020=4,109）；3) 均值改 2.512；4) trap ④ 要么改文案（承认现在会把不可算记成 0），要么按上条改 SQL——两边不能不一致。


---
## 4. 审查抓到的通病（这是本报告最有价值的部分）

42 个指标里 **13 个**存在与 ROCE 同型的风险。它们分五类，每一类我们都已经踩过一次：

### 4.1 五类通病

| # | 通病 | 本次实例（审查员实测） | 与 ROCE 事故的关系 |
|---|---|---|---|
| T1 | **单位/量纲错配** | 邓普顿「扣非低 PE + 总回报率」把**每股派息**除以**总市值**，实测该列最大 1.27e-7（等同全 0）；而正确口径下中位股息率是 0.97%。后果：第二道闸门的股息项恒为 0、永不可能补足，SQL 不报错、静默失效 | 与 ROCE「拿 PPE 当长期资产」同类：**分子分母不同量纲，但不报错** |
| T2 | **分母不挡负** | 惠特曼「资产负债表弹性」用 `nullif(x,0)` 只挡零不挡负；2025 年归母权益 ≤0 的 54 只股票里，52 只产生负比率，区间 **−111.1x ~ +337.0x**（本人已复现） | 与 ROCE 首版「219 只分母为负」完全同型 |
| T3 | **广义口径冒充狭义** | 神奇公式的 ROC 分母标成 `exact`，实际用的是 `总资产−流动负债`，而格林布拉特原文是「净营运资本 + 净固定资产」。实测中位 PPE/CE 仅 0.258、57% 的股票 PPE 占比 <30% | **与 ROCE 第一版一模一样**：把「固定资产」理解成 PPE 还是长期资产大区 |
| T4 | **护栏拍脑袋 / 失效** | PEG 用 0.5% 增速下限做护栏，实测仍有 **16.7% 样本 PEG>10、最大 418.8**；QMJ 用 `LEAST(x, 3.0)` 硬截断，实测 **40.6%（2,108/5,193）顶格**，指标退化成 0/1 | 与我第一版给 ROCE 设「300% 上限」同型：**用截断掩盖分母问题** |
| T5 | **样本下限缺失** | 马克斯「市场温度计」早期年份样本只有 2/54/70 只，却照样输出 67x~106x 的 CAPE 值 | 与我第一版「只剔 |ROCE|>300% 就发不出去」同型：**没检查分母的样本充分性** |

### 4.2 另外四类高频问题（P1，不致命但必须改）

| # | 问题 | 实例 |
|---|---|---|
| P1-a | **金融股剔除只写在陷阱里，没写进 SQL** | 巴菲特学派「一美元测试」混入 121 只金融股；质量学派「增量 ROIC」让齐鲁银行（CC=6.79）、江苏银行（5.90）占据榜首 |
| P1-b | **跨年口径断点未标注** | 2020 新收入准则（运费入营业成本，同面板毛利率 −1.28pct、销售费用率 −1.32pct）；2017 研发费用单列；`credit_impairment_loss` 2025 年整列空导致 OE 口径断点 |
| P1-c | **样本量夸大** | 聂夫称 3,251 实为 2,695；德雷曼称 3,274 实为 1,629；PEG 称历史 2001–2025，实测 2001–2005 可算样本为 **0**，真实序列 2011–2025 |
| P1-d | **声称与交付不符** | 邓普顿的 `formula_sql` 把年份硬编码成 2025，只能出单年快照，却宣称「可做 19 年历史分位」；审查员把它参数化后实测**历史完全可行**，只是交付物没做 |

### 4.3 顺手挖出的一个真实数据 bug（我们自己库的问题）

审查量化价值流派时发现：**689009 九号公司（CDR）的 `indicator_ext.market_cap_at_report` 恰好小 10 倍**。我独立复核并全库扫描确认：

| 项 | 实测 |
|---|---|
| 689009 快照总股本 | 735,848,500 股 |
| 历史股本链（2025-12-31 当前值） | 720,335,580 股 |
| 报告日收盘价 | 55.59 元（与 `price_daily_raw` 当日收盘逐位一致） |
| 应有市值 | 55.59 × 720,335,580 = **400.4 亿元** |
| `market_cap_at_report` 表内值 | **40.04 亿元**（恰好 1/10，且 2024-12-31 起 8 个报告期全是 0.1 倍） |
| 影响范围（2024-12-31 起 43,478 个观测全扫描） | **仅 689009 一只**；其余 43,470 个观测全部落在 1% 内 |

**根因：不是 CDR 汇率/单位本身，而是派生表陈旧。** `share_capital_history` 里 689009 的每一条记录都带
`change_reason = 'unit_rescale:10'`——股本链后续被 ×10 校正过（`scripts/align_share_capital_tail.py`），
但 `indicator_ext` 是在校正**之前**构建的，`market_cap_at_report = 收盘价 × 当时股本` 仍停留在旧的 1/10 口径。
这正是 `reports/119` D28「派生域没有重建触发就会冻结」的又一次复现。

**影响**：`market_cap_at_report`、`book_to_market`、`tobin_q`、`ev_ebitda` 对这只股票全部错 10 倍；
本次调研中它因此被聂夫总回报率排到**全市场第 1 名**、德雷曼排到第 2 名——排名污染的证据确凿。

**修法**：重跑 `vd data compute-extended-indicators` 即可（股本链已是正确值，重算后应为 400.4 亿）。
**但更该修的是机制**：股本链变更后必须触发 `indicator_ext` 重建，否则任何依赖"报告期股本"的派生指标都会静默停旧。
做新指标（尤其用到 `market_cap_at_report` 的）之前，应先把这条重建触发补上。

---

## 5. 数据库端的 5 个结构性缺口

以下不是某个指标的问题，而是**任何流派都会撞到的数据墙**：

| # | 缺口 | 影响哪些指标 | 现状 |
|---|---|---|---|
| G1 | **没有 GDP / 宏观序列** | 巴菲特指标（证券化率） | 审查员已全库扫 `information_schema`，65 张表零命中。只能用「全 A 市值 ÷ 全 A 营业收入」做替代版，且必须改名不能叫巴菲特指标 |
| G2 | **没有「维持性资本支出」字段** | 股东盈余 Owner Earnings、自由现金流类 | 只有 `cash_flow_activity.capex`（总额）。用总额会系统性惩罚高成长公司（京东方 2024：净利 53 亿 / 折旧 384 亿 / capex 340 亿，两口径差近一倍） |
| G3 | **没有信用债收益率 / 信用利差** | 格雷厄姆 E/P 双倍 AAA 门槛、马克斯市场温度计 | 只有国债曲线（`treasury_yield_curve`）。格雷厄姆原式是「2×AAA 企业债」，用国债替代等于放宽标准，**必须改名并写明** |
| G4 | **没有 beta / WACC / 永续增长率** | 任何 DCF 类 | 全库零命中。DCF 无法像 ROCE 那样预计算成「历史中位数」指标，只能做成用户输入 r 和 g 的参数化工具 |
| G5 | **没有 gross PPE（固定资产原值）** | Altman Z 的 X3（EBIT/固定资产原值）、Ohlson O 的部分变量 | 只有净值 `fixed_assets`。Z 值只能用净值替代，**与原始公式不等价**，需标注 |

---

## 6. 分档建议

综合「信息量 × 可算性 × 审查裁决」，我建议分三档：

### 6.1 第一档：建议首批实现（改完即可用，风险可控）

| 指标 | 提出者 | 为什么值得做 | 施工前必做的修正 |
|---|---|---|---|
| **Piotroski F 分数** | Piotroski | 9 项 0/1 打分，公式完全公开、无自由参数，是 42 个里**口径争议最小**的一个 | 修正审查指出的 `balance_sheet_ext` 行数引用错误；确认 9 项各自的历史覆盖率 |
| **Altman Z / Z'' 分数** | Altman | 破产预警，与现有 ROE/ROCE 完全正交（一个是回报、一个是存活概率） | X3 用固定资产净值替代 gross PPE，需标注；Z'' 危险区要处理「负营运资本 + 当年盈利」的语义反转 |
| **Graham Number + 安全边际** | 格雷厄姆 | 公式封闭、只有 EPS/BVPS/价格三个输入，且 `PE×PB ≤ 22.5` 与它数学等价 | 股本口径统一（历史用 `share_capital_history`，缺时回退 `paid_in_capital`）；金融股剔除 |
| **盈利收益率 E/P − 2×10Y 国债** | 格雷厄姆 / Kahn | **3 个 pass 之一**，审查零问题。实测通过率 2007 年 1.2% → 2018 年 18.4% → 2024 年 30.2%，对市场泡沫/底部区分度最好 | 改名（AAA→国债）；亏损股 E/P 需分档 |
| **ROCE 持久性（10 年中位数 + 最低值 + 变异系数）** | Terry Smith / Rochon | 直接复用我们已有的 `roce_history`（105,168 行），**零新增数据成本** | 只需在现有 ROCE 域上加两个聚合列 |

### 6.2 第二档：建议改造后实现（价值高但必须先修口径）

| 指标 | 提出者 | 必须先解决的 |
|---|---|---|
| 一美元测试 / 留存资本化率 | 巴菲特 / 芒格 / Ruane | 分母护栏（负权益、留存为负）；`funding_events` 扣减增发；送转还原 |
| 股东盈余 Owner Earnings | 巴菲特 | G2 缺口：必须双口径并列（总额 capex / 折旧代理），且标注 2025 年信用减值空列造成的断点 |
| 神奇公式 | 格林布拉特 | T3：分母必须改名成「A 股适配口径」，不能叫 exact 复刻；另需先修 689009 的 10 倍市值 bug |
| 聂夫总回报率 / 德雷曼逆向 | 聂夫 / 德雷曼 | 样本量声明修正；689009 bug 修复；德雷曼的零护栏重述股（002379）需要剔除规则 |
| PEG / PEGY | 林奇 | T4：现有护栏无效（16.7% > 10），需改为「增速截尾 + 亏损股单列」而非硬截断 |
| 现金转换率 | Terry Smith | T4 同型：净利润为负/接近零时的护栏；金融股剔除 |
| 扣非低 PE + 总回报（邓普顿双闸门） | 邓普顿 | **T1 单位错配必须先修**（股息率现在恒为 0） |
| 资产负债表弹性 | 惠特曼 | **T2 分母不挡负**（−111x~+337x） |
| 市场温度计 | 马克斯 | T5 样本下限；G3 信用利差缺失需如实标注「不可冒充」 |

### 6.3 第三档：不建议做（或明确不做）

| 指标 | 原因 |
|---|---|
| **防御型投资者七准则（0–7 分）** | ❌ 审查不通过：行扇出（row fan-out）+36%、股息连续年数存在前视偏差；且 A 股 2013–2025 每年只有 0–2 只能全过，做出来也是死条件 |
| **德雷曼逆向价值复合** | ❌ 审查不通过：零护栏下被重述股霸榜（002379 score=1.000） |
| **NCAV / 净净股系列（Graham、Klarman 两条）** | ⚠️ 可算但**在 A 股几乎不存在标的**：2025 年末净净股仅 3–7 只且全是地产/大宗贸易（存货按成本挂账，清算价值远低于账面）。建议降级为「全市场 P/NCAV 中位数分位」的估值温度计，不做选股 |
| **巴菲特指标（证券化率）** | ⚠️ G1 缺口：库内无 GDP。可做「全 A 市值 ÷ 全 A 营收」替代版，但**不能叫巴菲特指标** |
| **DCF 系列** | ⚠️ G4 缺口：无 beta/WACC/g，无法预计算 |
| **费雪十五要点 / 林奇六分类 / Akre 三腿凳** | ⚠️ 定性框架，只能取其中可量化的代理变量，且审查已指出多处样本与剔除问题；建议只保留其中单点指标（研发强度、盈利加速），不做整体评分 |

---

## 7. 复现与归档

```bash
# 只读查库（带自动更新锁重试）
python .planning/2026-11-16-masters-survey/dbq.py "SELECT ..."

# 原始产出
.planning/2026-11-16-masters-survey/
  context.md                  数据库字段/覆盖率/已知陷阱基线（7 个调查员共用）
  schools/<流派>.json         7 份调查报告，42 个指标（含 formula_sql）
  reviews/<流派>.json         7 份审查报告，逐指标裁决与证据
  dbq.py                      只读查询工具
```

---

## 8. 待你批准的决策

1. **第一档 5 个**是否全部做成正式复合指标（我的建议：先做这 5 个，风险最低、信息量最高）？
2. **第二档 9 个**要不要按上述顺序逐个改造后加入？
3. **689009 的 10 倍市值 bug** 是否本轮就修（它会污染任何用到 `market_cap_at_report` 的新指标）？
4. **G1–G5 五个数据缺口**：是否需要另立项目补数据（GDP / 维持性 capex / 信用利差 / beta / gross PPE），还是接受「用到时如实标注缺失」？
## 9. 数据库数据问题专项调查报告（补充调查）

> 本节是应要求追加的**独立补充调查**：在做大师指标可行性映射的过程中反复撞到数据本身的问题。
> 由 5 个专项审计 Agent 分别从「派生表陈旧 / 单位量纲 / 跨年口径断点 / NULL 语义 / 勾稽完整性」五个角度全库扫描，
> 共 **60 条问题，其中 P0 9 条**。每条都带可复现 SQL 与实测数字，原始 JSON 在 `.planning/2026-11-16-masters-survey/dbquality/`。

### 9.1 总览

| 专项 | 条数 | P0 | 一句话总评 |
|---|---|---|---|
| 派生表陈旧 | 11 | 2 | 派生表的「报告期陈旧」这个老病已经治好（indicator_ext 追到 2026-09-30、fixed_assets/扣非等补采已补齐），但「输入变化不触发重建」这个更隐蔽的同型病仍在：68900 |
| 单位量纲 | 6 | 1 | 五类量纲（市值元/亿、股本单位、每股份额、分红每股价、报表金额）的主路径已核验自洽，但科创板成交量量纲在 2026-07-31 整体断裂 100 倍（P0，612 只 / 45,067 行 × 2 表 |
| 跨年口径断点 | 16 | 4 | 扫描 5 张财务主表 × 100 个字段 × 2005-2025，用固定面板同股同比 + 覆盖断崖 + 恒等式残差三法定位到 16 个口径台阶/语义漂移（4 个 P0），最严重的是 FY2025 明细 |
| NULL 语义 | 14 | 1 | 查出 1 个 P0（技术三指标 47% 覆盖率 + 无门禁 → 动量类筛选静默丢 52.6% 股票）与 8 个 P1；三类 NULL 已可稳定区分：金融股结构性不适用（银行 42 只 8 年全 0%） |
| 勾稽完整性 | 13 | 1 | 主键与三大会计恒等式本身是干净的（声明 PK 的表零重复、资产/权益拆分违反 0.07%、权益=归母+少数违反 0.06%），真正的风险全在派生层：indicator_snapshot 的同比字段用了 |

**最该先修的 9 个 P0（按危害排序）**：

| # | 问题 | 影响面 | 状态 |
|---|---|---|---|
| P0-1 | **科创板成交量放大 100 倍** | 612 只 × 45,067 行 × 2 张价格表；已污染 533 只股票的快照 `avg_volume` | 待修 |
| P0-2 | **`indicator_snapshot` 每次重发把 `roce_*` 五列清成 NULL** | 全市场 5,575 行 × 5 列；任意一次快照发布即触发 | ✅ **本会话已修复** |
| P0-3 | **FY2025 年报明细两表「半空壳」** | `cash_flow_activity` 19/21 列、`cash_flow_indirect` 18/35 列全空，约 10.7 万个空单元格 | 待修 |
| P0-4 | **营收/净利同比两套口径** | `indicator_snapshot` 全表；27.8% 的股票在详情页与快照里方向相反 | 待修 |
| P0-5 | **技术三指标只有 47% 覆盖** | 2,931 只股票的动量/波动筛选静默丢股，且无覆盖率门禁 | 待修 |
| P0-6 | **保险股「保险合同负债」写进 `contract_liabilities`** | 5 只保险股占全市场合同负债合计的 **42%–68%** | 待修 |
| P0-7 | **`asset_impairment_loss` 符号在 FY2018 翻转** | 2015–2025 约 6.4 万行；跨年减值率序列符号相反 | 待修 |
| P0-8 | **FY2020 预收款项→合同负债重分类** | `prepayments_received` 单列一夜 −96.6% | 待修 |
| P0-9 | **689009 九号公司市值/每股族错 10 倍** | 1 只 × 24 期 × 14 个派生列；全库仅此 1 只 | 待修 |

### 9.2 P0 逐条：证据、根因、修法

#### P0-1　科创板（688/689）成交量被腾讯适配器放大 100 倍

**证据**（本人独立复现）：
- 688111 金山办公 2026-07-30 成交量 12,675,928 股（换手 2.7%）；**2026-07-31 变成 2,873,692,500 股**（换手 **619%**），单日跳约 227 倍。
- 2026-11-13 全市场横向：科创板 `volume/circ_shares` 中位 **1.9396**（194%），其他板块 **0.0169**（1.7%），相差约 115 倍。

**影响面**：`price_daily_raw` / `price_daily_qfq` 各 45,067 行、612 只；下游 `indicator_snapshot.avg_volume` 533 只偏大 100 倍。所有以成交量为输入的使用方（流动性、换手、量价筛选）全部失真。

**根因**：`app/core/adapters/tencent_adapter.py:214-216` 无条件 `float(bar[5]) * 100`（注释假定腾讯 K 线为「手」）。主板/创业板确实返回手数，**但腾讯对科创板返回的已是股数**，再乘 100 即放大一百倍。断点落在 2026-07-31，因为当天价格主链整体切到腾讯适配器。

**修法**：
1. 适配器加守卫：科创板不乘 100，并把注释改成「科创板腾讯 day/qfqday 的 bar[5] 已是股数，主板/创业板为手数」。
2. 回归用例：同批返回 688xxx 与 600xxx 各一根 bar，断言 688 不乘 100、600 乘 100。
3. 存量修复：`scripts/repair_star_volume_x100.py` 把 2026-07-31 起 688/689 的 `volume` 除以 100（两张表各 45,067 行），再重算受影响 533 只的 `avg_volume`。
4. 验收：修复后科创板 `volume/circ_shares` 中位应回到 1%–10%。

#### P0-2　`indicator_snapshot` 每次重发把 `roce_*` 五列清成 NULL

**证据**：`calculator._publish_snapshot()` 走 `DELETE FROM indicator_snapshot` + `INSERT ... BY NAME SELECT * FROM staging`；staging 表列齐但只写计算器的已知键，`roce_*` 五列不在其中 → 被写回 NULL。而全流程只有 `vd data compute-roce` 会写这五列，主更新流里没有这一步。

**这是我在实现 ROCE 时引入的缺陷**（`reports/130`），属于 D28「派生域没有重建触发」的同型事故。

**状态：✅ 本会话已修复**
1. `calculator._publish_snapshot()` 在 INSERT 之后、同事务内追加执行 `roce.SNAPSHOT_UPDATE_SQL`（毫秒级；失败只告警、不阻断主链发布）；
2. `update.py` 新增 `_refresh_roce()` 步骤，门控 = `max(roce_history.report_date) < 最新年报期` 或首次构建；
3. 新增回归用例 `test_snapshot_republish_preserves_roce_columns`：模拟 republish 清空后再聚合，断言五列被恢复。

#### P0-3　FY2025 年报明细两表「半空壳」

**证据**：`cash_flow_activity` 2025 年报 5,576 行中 19/21 个数值列全空；`cash_flow_indirect` 5,579 行中 18/35 列全空。行存在、`source`/`capex` 有值 ⇒ 不是「没采到公司」，而是**只采了几列**。

**根因**：`app/core/supplement.py` 的 `CSMAR_CUTOFF = date(2025,3,31)`——此后的报告期全部改走东财 F10 补充管线，而该管线只按 `INDIRECT_FIELD_MAP`（9 字段）与 `ACTIVITY_FIELD_MAP`（1 字段 capex）写列，其余一律 NULL。

**影响**：任何依赖「折旧摊销 + 营运资本变动」的指标（EBITDA、EV/EBITDA、经营杠杆、自由现金流质量检验，以及本次调研里的 Owner Earnings）在 2025 年全部失去加项。

**修法**：扩两张 FIELD_MAP，补齐东财 `cfAjaxNew` 的其余同名列；补不上的在 `indicator_null_reason` 登记 `SOURCE_EMPTY`，不要留成「无声的 NULL」。

#### P0-4　营收/净利同比存在「年报口径」与「TTM 口径」两套算法

**证据**：`indicator_snapshot.revenue_yoy` 由 `calculator._calc_growth()` 用**最新两个年报**算；而 `stock_detail._metric_history_rows()` 对同一字段用 `base_period='ttm'` 重算。**27.8%（1,527/5,485）的股票两处方向相反**。

**根因**：同一业务概念在两个模块各写一套公式；且快照行 `report_date` 是 2026-09-30（季报期），却挂着一个「年报口径」的增速。

**修法**：`_calc_growth()` 改为 TTM 口径写入 `revenue_yoy`；年报口径另存 `revenue_yoy_fy` 等新列并登记单位；DSL `period_type` 由 `current_only` 改 `ttm`。

#### P0-5　技术三指标只有 47% 覆盖，且无覆盖率门禁

**证据**：`period_return / annualized_volatility / max_drawdown` 在 5,575 行中 2,931 行（52.6%）为空，而 `price_daily_qfq` 对 5,585 只是完整的。

**根因**：三列由 `_trailing_contiguous_closes()` 从 qfq 收盘序列算，而 qfq 是**事后补齐**的——这些股票的快照行在 qfq 落库前就算过一次并写死 NULL，补齐后没有机制触发重算（`reports/119` D30 已裁决、至今未落地）。且 `_EXTENDED_MIN_COVERAGE_PCT` 门禁只对 `indicator_ext` 生效，不管快照列。

**修法**：qfq 写入成功后打 `technical_pending` 标记 → 下一轮强制重算；一次性补偿脚本重算 `period_return IS NULL` 且有 ≥2 条 qfq 收盘的股票；`asset_health` 新增 `TECHNICAL_COVERAGE_GAP`（覆盖率 <95% 即 P1）。

#### P0-6　保险股「保险合同负债」写进 `contract_liabilities`

**证据**：601318 中国平安 / 601319 中国人保 / 601336 新华保险 / 601601 中国太保 / 601628 中国人寿，FY2022–FY2025 共 19 行有值，却占**全市场合同负债合计的 42%–68%**。

**根因**：上游金融专用科目「保险合同负债」被按序号/名称匹配到了通用科目 `contract_liabilities`。出现年份恰是新保险合同准则 CAS25 的实施年（A+H 险企 2022、其余 2023）——只有实施新准则的公司才有这一行，可反证。

**修法**：映射层加守卫（`sw_level2='保险Ⅱ'` 时拒绝写入），保险合同负债落到 `financial_sector_items` 新列；一次性把 5 只保险股该列置 NULL；「合同负债/总资产」类排名加保险股剔除。

#### P0-7　`asset_impairment_loss` 符号在 FY2018 翻转

**证据**：财会〔2018〕15 号起，「资产减值损失/信用减值损失」按**损失以负号列示**，2017 及以前按损失为正。库内两段直接透传、未做符号归一；2018 年为混合年（同一年度内不同公司混用两种约定），逐年汇总时正负相消成「假低值」。影响 2015–2025 约 6.4 万行。

**影响**：`income.asset_impairment_loss` / `income.credit_impairment_loss` 都在 DSL 与 screening 白名单里，任何跨 2018 的减值率、EBIT 修正、扣非还原都会算错。

**修法**：不擅改原始值，在派生层提供 `impairment_loss_positive = CASE WHEN report_date >= '2018-12-31' THEN ABS(x) ELSE x END` 并让 DSL 使用；schema 列注释写明分段；禁止跨 2018 的 raw SUM/AVG。

#### P0-8　FY2020 预收款项→合同负债重分类

**证据**：新收入准则 CAS14（境内上市 2020-01-01 起施行）把与客户合同相关的预收款重分类为合同负债，`prepayments_received` 单列一夜掉 **96.6%**，残留的 1,284 亿只剩非合同类（租金等），语义已漂移。

**影响**：任何「预收账款」单列时间序列，以及依赖它的净营运资本/占款类指标，在跨 2020 时断裂。

**修法**：建合并口径视图 `v_advance_from_customers = COALESCE(prepayments_received,0)+COALESCE(contract_liabilities,0)`，营运资本/占款类指标改用它；DSL 给 `balance.prepayments_received` 加 `deprecated_after='2020-01-01'` 并在报错文案提示。

#### P0-9　689009 九号公司市值/每股族错 10 倍

**证据**（本人独立复现）：`indicator_ext.market_cap_at_report` 在 2024-12-31 起 8 个报告期全部是应有值的 0.1 倍（2025-12-31：应有 400.4 亿，表内 40.04 亿）；全库 43,478 个观测中**仅此 1 只**。连带 `bps / bps_parent / revenue_per_share / ocf_per_share / ...` 等 14 个派生列错 10 倍。

**根因**：`share_capital_history` 该股全链带 `change_reason='unit_rescale:10'`（股本链后续被 ×10 校正），但 `indicator_ext` 是校正**之前**构建的——`_INPUT_TABLES` 输入指纹不含 `share_capital_history`，`_refresh_indicator_ext()` 也完全不看 `share_capital_changed`。

**修法**：① `extended.py` 输入指纹增补 `share_capital_history`（尾段 400 天的 `stock_code:total_shares:effective_date` 摘要）；② `_refresh_indicator_ext()` 增加 `force_codes`，股本变化时按变更股重建；③ 立即重跑 `vd data compute-extended-indicators` 消除存量。

### 9.3 P1 问题汇总（按专项）

| 专项 | P1 问题 |
|---|---|
| 派生表陈旧 | 1. roce_history 没有任何自动重建触发，且 roce_latest 不披露取自哪一年：43 行展示的是 2025 年以前的 ROCE<br>2. 折旧摊销族在三季报/一季报结构性缺失：indicator_ext 的 ebitda/杠杆/EV-EBITDA 在最新期只剩 0.09%，而门禁只判报告期是否推进<br>3. 金融股专用三表链条没进自动更新：121 只金融股里 24 只最新期无任何专用科目（其中 21 只从来没有一行）<br>4. etf_daily 有 7 只 ETF 冻结 58 天：更新器只遍历 etf_meta，而这 7 只在 etf_daily 里却不在 etf_meta<br>5. price_daily_qfq 与 price_daily_raw 行集不一致（313 只缺 35,077 行 / 289 只多 11,599 行），技术指标在残缺序列上静默出 |
| 单位量纲 | 1. 689009（九号公司 CDR）indicator_ext 全部 24 期市值小 10 倍、每股份额大 10 倍（股本链 ×10 校正后未重建派生表）<br>2. 量纲声明与库内实际存储不符：13 个指标声明 unit='percent' 实存小数，basic_eps 声明 'ratio'、diluted_eps 声明 'CNY'（二者都 |
| 跨年口径断点 | 1. report_type 标签在 FY2025 断崖：'annual' 从 4,807 行归零，5,226 行变 NULL，WHERE report_type='annual' <br>2. 全库 0/NULL 约定在 FY2016 切换：同一个稀疏科目 2015 年写「字面 0」、2016 年起写 NULL，覆盖率先假高后断崖<br>3. 营业总成本口径在 FY2018 变更：资产减值损失/信用减值损失移出 total_operating_cost，EBIT=营收−营业总成本 的序列出现台阶<br>4. FY2017 研发费用单列：管理费用/营收 中位一年掉 3.10pp（−33%）且永不回补，2016 及以前的 rd_expenses 只有 12.3% 覆盖<br>5. FY2020 新收入准则把运费搬进营业成本：销售费用率中位 −1.41pp、毛利率中位 −1.03pp，同年 Δ销售费用率与 Δ毛利率 1:1 对应<br>6. FY2019 新金融工具准则：27.9% 的股票应收票据归零，而库内根本没有「应收款项融资」列，应收票据合计一年掉 48%<br>7. 使用权资产折旧 FY2021-2023 采集缺失、FY2024 才补全：间接法该科目合计从 13.2亿 跳到 3,705.3亿，把 depreciation_amortizat |
| NULL 语义 | 1. cash_flow_activity 自 2025-06-30 起 21 个数值列只剩 capex 有值，其余 20 列全市场恒 NULL（行存在、值不存在）<br>2. cash_flow_indirect 自 2025-09-30 起 29 个数值列中 19 列恒 NULL：net_profit、营运资本三项、financial_expens<br>3. 「0 与 NULL」在 2016 年报整体切换写法：goodwill/bonds_payable/trading_financial_assets/notes_receivab<br>4. cash_flow_indirect / cash_flow_activity 的表级覆盖率在 2010-2019 只有 60-70%，且缺失系统性偏向小盘股（FY2018 中<br>5. 2025 中报（2025-06-30）现金流量表补充域整期空洞：cash_flow_indirect 与 cash_flow_activity 各只有 5 行（正常应约 5,4<br>6. FY2025 有 247 只股票（LEFT JOIN 口径 248 只）的 interest_expense + cash_paid_taxes 等 6 个字段整批为空，自有历<br>7. 派生列 indicator_ext.shareholder_occupation 只从 2025-09-30 起有值，2024-06-30～2025-06-30 全空：新增派生<br>8. 库里没有『应收款项融资 / 应收票据及应收账款』类科目：notes_receivable 2019 年从 79.8% 断崖到 62.3%，000651 进 2019 年后恒为  |
| 勾稽完整性 | 1. 2006 年前的「净利润=利润总额-所得税」在 5,049 行不成立，导致 EBIT 两套公式在 1,801 行分歧 >5%<br>2. indicator_ext 极端值穿透：资产周转率最大 5,311、权益周转率 18,053、流动资产占比 4,985%、EV/EBITDA 996<br>3. 财报公布日缺口：最新一期 5,575 行财务数据 0 条公布日，历史各季缺 3.5%-4.2%，24.0% 记录公布日滞后 >365 天<br>4. funding_events 无唯一键，334 组同一增发事件重复入库且股数互不相同，dilution_3y 在 4,016 行上多计<br>5. 现金流三处勾稽失败：期末-期初≠净增加额 4,923 行、直接法≠间接法 7,617 行、利润表净利润≠间接法净利润 16,446 行 |

### 9.4 P2 问题清单

| 专项 | P2 问题 |
|---|---|
| 派生表陈旧 | 1. index_valuation 结构性落后 1 个交易日：_refreshed_today 按日历日判重，盘前那轮把当天额度用掉了<br>2. 派生表 calculated_at 时区口径不一致（带 tz 的 UTC 落成本地 +8，裸 UTC 落成 UTC），陈旧度比较系统性差 8 小时<br>3. indicator_null_reason 落后快照一代且没有任何重建触发（诊断表与事实脱节）<br>4. 三条静态/CSMAR 域停在旧期（financial_report_dates 2026-06-30、report_metadata 2025-03-31、industry_h |
| 单位量纲 | 1. 金融股流动/非流动小计被零填充（0 而非 NULL），导致 indicator_ext.current_asset_ratio 静默为 0<br>2. payout_ratio 无上界：77 只 >5 倍、最大 1,938.7 倍（TTM 每股收益趋零导致分母塌缩）<br>3. 1993-2002 年少数股东损益/少数股东权益被填成合计值（净利润与归母净利差达 3,000 倍） |
| 跨年口径断点 | 1. interest_expense / interest_income 的 FY2017 覆盖台阶：2016 年 interest_expense 仅 12.64% 有值，负值只<br>2. income_statement.exchange_gain 是「公允价值变动收益」而非汇兑损益（与间接法 fair_value_change_loss 严格反号相等 92.6<br>3. FY2019 交易性金融资产口径扩大（覆盖 +28.3pp）：新金融工具准则把理财并进来，跨 2019 的「持有金融资产」口径不可比<br>4. indicator_ext.shareholder_occupation 只有 FY2025 有值（5,261/5,625），因为它的原料表 balance_sheet_ext<br>5. FY2007 新会计准则断点：每股收益/扣非/商誉/资产减值/公允价值变动等科目 2007 才成建制出现，2006 及以前不可与之混算 |
| NULL 语义 | 1. 结构性断点年表缺失登记：rd_expenses / interest_expense / credit_impairment_loss / lease_liabilities <br>2. report_type 元数据大面积为空：全库 114,812 行 NULL（31.8%），FY2025 年报 92.9% 为空，且同一列混着英文枚举与中文标签；任何 repo<br>3. balance_sheet 的 6 个银行监管比率列在年报口径**全历史 0 行**，只有 2026-06-30 与 2026-09-30 两个季报期有值（38 只 / 34-<br>4. minority_interest 为 NULL 但少数股东损益≠0：FY2025 有 78 只（损益>0 的 32 只），『NULL=无少数股东』不成立，COALESCE 0<br>5. indicator_null_reason 未覆盖 2.1% 的 NULL 单元（840/39,268），集中在 bonus_share_ratio_3y 与 sharehol |
| 勾稽完整性 | 1. EBITDA 族在季报期整列不可用（已登记 SOURCE_EMPTY，非静默错误），但快照期恰为季报，EV/EBITDA 等 4 个筛选字段在「当前期」全空<br>2. report_type 大面积 NULL 且中英两套词表，任何按报告期类型过滤的 SQL 都会静默丢行<br>3. 伪零值：total_liabilities=0 而总资产>0（98 行）、revenue=0 而营业总收入>0（14 行）、revenue 为 NULL 而营业总收入>0（44<br>4. 27 只金融股的流动/非流动资产存 0 而非 NULL（361 行），与 context.md「金融股为 NULL」的记载不符，导致流动资产占比被算成 0%<br>5. report_date_close 取「报告期当日或之前最近交易日」的不复权价：116 行滞后 >30 天，最长 2,169 天<br>6. buyback_events.raw_hash 全部 5,484 行同一个值，血缘哈希完全失效<br>7. 累计口径微违规：年报营收 < 前三季营收 438 行，Q1→H1、H1→9M 反向 188 行 |

### 9.5 修复优先级建议

| 批次 | 内容 | 理由 |
|---|---|---|
| **第 0 批（立即）** | P0-1 科创板成交量、P0-9 九号公司市值、P0-2 ROCE 清空（已修） | 都是**静默产出错误数值**，且已经污染对外展示的排名 |
| **第 1 批（做新指标前必须完成）** | P0-4 同比双口径、P0-5 技术指标覆盖、P0-7 减值符号、P0-8 预收/合同负债合并 | 大师指标里有一半要用同比、减值、营运资本——不先修就会把老 bug 复制进新指标 |
| **第 2 批（数据完整性）** | P0-3 FY2025 明细半空壳、P0-6 保险合同负债错映射 | 影响面大但可标 NULL 降级，不阻塞新指标 |
| **第 3 批（口径登记）** | 所有 P1 / P2 | 主要是补元数据、加门禁、写注释；工作量小，但能防住下一个 ROCE 事故 |

### 9.6 已核验确认无问题的项

为避免只报坏消息造成误判，各专项也明确核验了以下项是**干净的**：

| 专项 | 已核验无问题 |
|---|---|
| 派生表陈旧 | indicator_ext 的报告期门禁已生效：361,393 行、MAX(report_date)=2026-09-30 恰等于 latest_complete_trio_perio<br>indicator_ext 除 689009 外全部可由源表重算：281,214 个 market_cap_at_report 观测里 281,190 个与 report_date_c<br>indicator_snapshot.report_date 与各股三表最新期一致：5,573 只为 2026-09-30；仅 688613、920533 停在 2026-06-30，<br>indicator_snapshot.total_market_cap 可由源表重算：5,575 只中 5,574 只等于 latest_close × stock_meta.tota<br>新报告期字段覆盖率已修复：balance_sheet.fixed_assets 在 2026-09-30 为 5,575/5,575（reports/119 记 1.8%），incom<br>price_daily_qfq 的「最新交易日」维度无陈旧：5,585 只股票的 MAX(trade_date) 全部等于 price_daily_raw（0 只滞后、0 只缺失），r<br>company_employee_history 整体 MAX(report_date) 只到 2026-09-17，但 indicator_ext 为 2026-09-30 选中的员<br>同步型行情域都在最新交易日：treasury_yield_curve MAX(curve_date)=2026-11-16、etf_meta 的 34 只 ETF 全部到 2026-1<br>indicator_ext.free_cash_flow 在最新期覆盖 5,564/5,575，FCF 族不受「三季报无间接法」影响（capex 来自直接法现金流量表，季度可得），所以<br>roce_history 只含年报期（COUNT(DISTINCT report_date)=37，1989-12-31 至 2025-12-31），与「年报口径 + 5/10 年中位 |
| 单位量纲 | C1【股本单位：股 vs 千股 vs CDR 份】stock_meta.total_shares 与 share_capital_history 最新一笔逐股比对，5,551/5,55<br>C2【快照市值量纲：元 vs 亿元】indicator_snapshot.total_market_cap 与「最新收盘价 × stock_meta.total_shares」逐股比对<br>C3【每股份额族内部一致】indicator_ext 的 bps（287,514 行）、bps_parent（285,955）、revenue_per_share（287,125）、o<br>C4【分红 per share：每 10 股陷阱】dividends 53,398 条「除权日单次股息率 = dividend_per_share ÷ 除权日收盘价」中位 0.96%，<br>C5【累计分红金额：元 vs 亿元】cumulative_dividend_amount 与「历史 Σdividend_per_share × 当前股本」交叉验证，5,429 只中位比<br>C6【资产负债表金额量纲自洽】total_assets = total_liabilities + total_equity：360,962 行中仅 387 行相对残差 >1%（集中于<br>C7【利润表恒等式】net_profit = parent_net_profit + minority_shareholder_profit（少数股东损益非空子集）286,865 行中<br>C8【实收资本 vs 股本（面值）】paid_in_capital / total_shares 在 5,474/5,520 只上落在 [0.5, 2]（面值 1 元）；偏离者为 0.<br>C9【EPS 量纲：元/股】basic_eps 与「归母净利 ÷ 时点股本」逐期比对，422,600 行中只有 689009 呈系统性 10.05 倍（CDR 1 份 = 0.1 股，<br>C10【快照百分数字段对照】turnover_rate（中位 2.03）、ttm_dividend_yield（1.19）、div_yield_spread_10y（−0.44）、di |
| 跨年口径断点 | FY2025 数据源从 csmar 切到 eastmoney_f10 后，**数值量纲与语义未坏**：固定面板 4,156 只 2024→2025 的营业总收入同比中位 1.04、P5<br>利润总额恒等式 `total_profit = operating_profit + non_operating_income − non_operating_expenses` 在 <br>`revenue` 与 `total_operating_revenue` 基本恒等：2010-2025 每年只有 130-227 只（≤3.9%）不相等，平均相对差 ≤0.7%（最大<br>三表主键无重复：`(stock_code, report_date)` 在 income_statement / balance_sheet / cash_flow 各 0 条重复，不<br>**线索「credit_impairment_loss 2025 年整列为空」不成立**：income_statement.credit_impairment_loss 在 2025-<br>**线索「interest_expense 2016 存在负值」不成立**：2013-2016 年负值样本数均为 0；负值只在 FY2017 之后出现且占比 0.1%-0.4%。201<br>cash_flow 主表未受 FY2025 源切换影响：2025-12-31 共 5,624 行，cf_from_operating 5,624 有值、cash_received_sa<br>`cash_flow_indirect.asset_impairment_provision`（计提额）符号在 FY2013-2025 始终「计提为正」（正值占比 91%-95%），可<br>直接法与间接法经营现金流交叉一致：`cash_flow.cf_from_operating` 与 `cash_flow_indirect.cf_from_operating_indir |
| NULL 语义 | 主键唯一性（已核验无问题）：balance_sheet 361,484 行 / income_statement 371,252 / cash_flow 358,862 / cash_<br>折旧摊销的 COALESCE 0 是安全的（已核验无问题）：FY2025 年报 fixed_assets>0 却无 fixed_asset_depreciation 的 92 只中，8<br>EBITDA / EV-EBITDA 的空值都有解释（已核验无问题）：2026-09-30 期 depreciation_amortization / ebitda / ebitda_<br>total_current_assets 的 NULL ≈ 金融股（已核验无问题）：2018-2025 年报每年 97-100 个 NULL 中 95-100 个属 sw_level1<br>商誉 NULL ≈ 无商誉（已核验无问题）：FY2024 有商誉（>0）的 2,567 只中，FY2025 转为 NULL 的仅 107 只（4.2%），其中 100 只当年有资产减值<br>合并权益恒等式（已核验无问题）：total_equity ≡ total_equity_parent + COALESCE(minority_interest,0)，FY2023-20<br>银行监管比率缺口的处置（已核验无问题）：6 列在年报口径全历史 0 行（仅 2026-06-30 的 38 只与 2026-09-30 的 34-37 只季报有值），但 app/cor<br>有息负债口径（已核验无问题）：_calc_interest_bearing_debt 已按 D31 改为『缺项按 0、三项全空才返回 None』，不再要求三项全非空（旧口径曾把 roi<br>interest_expense 的『语义不稳定』影响面比传闻小（已核验）：2018 年后负值只有 12-24 只/年（0.2-0.5%），且 interest_coverage 已用<br>价格域完整性（已核验无问题）：5,585 只股票的 price_daily_qfq 末根收盘价 NULL/≤0 记录数 = 0；raw 与 qfq 末交易日期错配 = 0；qfq 有效 |
| 勾稽完整性 | 主键重复：duckdb_constraints() 中声明 PRIMARY KEY 的 33 张表，按声明键 GROUP BY ... HAVING count(*)>1 全部为 0 <br>无主键表的真重复：buyback_events 按 (stock_code, announce_date, buyback_shares) 0 组（37 组同股同日是不同回购方案，正常<br>恒等式①「总资产 = 流动资产 + 非流动资产」：扣除金融股 0/0 的 361 行后，353,292 行可评估中仅 34 行（0.010%）偏差 >5%，其余 352,692 行完全<br>恒等式②「总资产 = 负债合计 + 所有者权益合计」：360,950 行可评估中 261 行（0.072%）偏差 >5%，其中 241 行在 2006 年前，2021 年后仅 4 行（<br>恒等式③「所有者权益合计 = 归母权益 + 少数股东权益」：290,380 行可评估中 180 行（0.062%）超标。另有 13,038 行出现「归母权益 > 所有者权益合计」，逐一<br>恒等式④「净利润 = 归母净利润 + 少数股东损益」：286,438 行可评估中 285,871 行成立（99.80%）；恒等式⑤「负债合计 = 流动负债 + 非流动负债」：351,0<br>负资产与零资产：total_assets < 0 共 0 行、total_assets = 0 仅 12 行；total_current_assets < 0 共 0 行、total_<br>indicator_snapshot 与源表期次对齐：5,575 行中 report_date 与 balance_sheet、income_statement 各自的 max(rep<br>indicator_snapshot 价格对齐：5,575 行 latest_price_date 与 price_daily_qfq 的 max(trade_date) 完全一致（0<br>indicator_snapshot 估值勾稽：pb_mrq 与 total_market_cap / total_equity_parent 在 5,456 行可评估中 100% 一 |

---

## 10. 复盘：ROCE 这一路踩的 8 个坑（下次构建复合指标前的必读）

> ROCE 从第一次实现到定稿，**返工了四次**（`reports/128` → `129` → `130` → `132`）。
> 这 8 个坑每一个都实际发生过、每一个都产出了错误数字但不报错。
> 后面那一节的检查清单，就是从这 8 个坑反推出来的。

### 10.1 八个坑

| # | 坑 | 实际发生了什么 | 后果 |
|---|---|---|---|
| **K1** | **把公式里的中文术语直接当成数据库列名** | 「净固定资产」→ 直译成 `balance_sheet.fixed_assets`（PPE）。但书里 *net fixed assets* 指的是经典资产负债表的「Fixed Assets」整段（对 Current Assets），含在建工程、无形资产、长期股权投资、油气资产 | 中国海油非流动资产 8,003 亿、`fixed_assets` 仅 85 亿 → 算出 **119% 压过茅台**；219 只分母为负；区间 **−8,311% ~ +21,285%** |
| **K2** | **「修正项」在样本外变成「误差制造项」** | 书里说「剔除超额现金」，这是对印度样本的向上修正。A 股现金占资产 80–90%，机械执行把分母削到零 | 同花顺剔完只剩 **2 亿**资本、EBIT 35 亿 → **444%**；重庆啤酒分母变负；海天 90.5%、公牛 80.6%。而书中标杆是蒂芙尼 16%／好市多 22%／组合中位 42% |
| **K3** | **用护栏掩盖自己看不懂的数字** | ROE 域写着 `roe if abs(roe) <= 1.0 else None` | **48 只真实 ROE > 100% 的公司被静默写成空值**。用户原话：「错的当然以修复为主；如果是对的，为什么要剔除呢？它不应该有剔除这个东西」 |
| **K4** | **看到怪数先改公式，不先查数据缺口** | 茅台 46% 偏低，我改了三次口径。真因是 `balance_sheet` **缺「拆出资金」列**——茅台财务公司放贷 1,394 亿混在流动资产里 | 影响 **123 只（9.7%）**。改公式只会把错误搬来搬去 |
| **K5** | **派生列没有重建触发** | `indicator_snapshot` 的发布是 `DELETE` + `INSERT BY NAME`，staging 表不带 `roce_*` 五列 | 每次快照重发都会把全市场 5,575 行的 ROCE **静默清成 NULL**（D28 同型） |
| **K6** | **用「上市年限」冒充「数据年数」** | 筛选条件写成「上市满 5 年」 | 算单年 ROCE 需要**上一年的资本**，上市满 5 年实际只够算出 **4 个**值。用户后来自评：「这 5 年是我拍脑袋想出来的」 |
| **K7** | **只给一个中位数，不给现况** | 快照只展示 `roce_10y_median` | 智飞生物 10 年 **36.9%**、最新年报 **−66.4%**；卓胜微 44.1% / −2.6%；五粮液 31.1% / 7.3%。**中位数越长，越会掩盖正在崩塌的公司** |
| **K8** | **没有先用外部标杆校验量级** | 前两版结果飞到 ±8,000%、444%、1,632% | 书里明明白白给了标杆（16%/22%/42%/52%），超出这个量级一个数量级时就该立刻怀疑分母，而不是继续调口径 |

### 10.2 单个坑的放大效应

K1 与 K2 都是"分母被改小"这一类的不同变体：**比率类指标一旦分母被系统性改小，结果会成倍放大且不报错。**

```
真实分母 2,542 亿 → ROCE 45%     （正常）
分母改成 1,737 亿 → ROCE  66%     （剔现金，K2 轻症）
分母改成   217 亿 → ROCE 525%     （只取 PPE，K1 重症）
分母改成     2 亿 → ROCE 444%     （现金占 89% 再剔，K2 重症）
分母改成   −0.4 亿 → 符号翻转     （K2 极重症）
```

**判据只有一个：把结果拿去和外部标杆比。** 对不上就说明分母错了，不是公司特殊。

### 10.3 下次构建复合指标的检查清单

> 按顺序做，每一步都要留下可复现的证据（SQL + 实测数字），不要凭印象跳过。

| 步骤 | 动作 | 通过标准 |
|---|---|---|
| **1** | **先写清「这个指标回答什么问题、不回答什么问题」** | ROCE 回答「生意每元资本赚多少」，**不**回答「股票贵不贵」「能不能持续」。写不出这句话就先别动手 |
| **2** | **公式每一项 → 精确字段路径 → 打印 5 个样本人工核对** | 例如「净固定资产 → `total_assets - total_current_liabilities - 净营运资本`」，并手算 1 家与公式对得上 |
| **3** | **如果公式含加总或相减，先验证会计恒等式** | `流动资产 + 非流动资产 = 总资产`、`资产 = 负债 + 权益` 在对得上（本项目 0.01%–0.07% 违反率可接受） |
| **4** | **用外部标杆校验量级** | 有书/论文标杆的（ROCE 16%–52%、PEG<1、F-Score 0–9、Z-Score 1.8/3.0）直接对；没有的就用「行业常识 + 头部公司手工核算」 |
| **5** | **检查分母是否会 ≤ 0，以及 ≤ 0 时怎么标注** | 必须写 `status`，**不做截断、不设上限、不置空**。用户裁决：「极值要么是对的要么是错的，对的就该显示，错的就去修」 |
| **6** | **检查字段的历史覆盖率与跨年口径断点** | 例：`interest_expense` 2016 年只有 12.6% 覆盖；2020 新收入准则让销售费用率跳 −1.41pp；`contract_liabilities` 2020 才有 |
| **7** | **检查特殊行业是否适用** | 金融股无流动/非流动划分（42 只银行 40 只缺 `total_current_assets`）、无「财务费用」行（101 只）。判据用 `sw_level1`，**不要用 `csrc_l1`**（同花顺/指南针都在里面） |
| **8** | **派生域必须有重建触发，且快照重发要保全** | 两条都要：`update.py` 里有重建步骤 + `calculator._publish_snapshot` 里重新聚合（K5 的修法） |
| **9** | **筛选口径用「有效数据年数」，不用「上市年限」** | 算比率需要上一期数据，N 年历史只能算出 N−1 个值 |
| **10** | **至少输出三个列位：历史中位 / 近期中位 / 最新值** | 三者背离就是信号：10年高+最新崩 = 恶化；5年>10年 = 最近才好起来（天孚通信 18.5%→25.8%→48.4%，AI 周期） |

### 10.4 一句话总结

> **比率类指标的风险不在分子，全在分母。**
> 分母错了不会报错，只会把结果放大十倍百倍，而且看起来"很有说服力"。
> 所以每做一次除法，都要问三句：分母是什么？它会不会趋零或变负？拿出去和别人的数对得上吗？

---

## 11. 金融股为什么被排除（不是因为它不好）

| 项 | 数字 |
|---|---|
| 金融股只数 | **119 只**（占全市场 5,575 只的 **2.1%**） |
| 金融股市值 | **24.2 万亿**（占全市场 115 万亿的 **21.1%**） |

**只数只占 2%，市值却占 21%**——把金融股排除掉，等于放弃五分之一的市值。所以这个决定必须讲清楚理由。

### 11.1 三个硬事实

| 类型 | 只数 | 有 ROCE 值 | ROCE 中位 | ROE 中位 | 资产负债率中位 |
|---|---|---|---|---|---|
| 证券Ⅱ | 48 | 21 | 4.2% | 6.4% | 78.9% |
| 多元金融 | 24 | 19 | 8.4% | 6.3% | 66.8% |
| 城商行Ⅱ | 17 | **1** | 1.4% | 9.3% | **93.1%** |
| 农商行Ⅱ | 10 | **0** | — | 9.8% | **91.6%** |
| 股份制银行Ⅱ | 9 | 5 | 1.6% | 6.2% | **91.3%** |
| 国有大型银行Ⅱ | 6 | **0** | — | 8.5% | **92.2%** |
| 保险Ⅱ | 5 | **0** | — | 11.6% | **89.8%** |

1. **资产负债率 90% 上下**。银行的存款、保险的保费，会计上是负债但本质是"原材料"——负债不是财务杠杆，是商业模式本身。
2. **没有流动/非流动划分**。42 只银行里 **40 只缺 `total_current_assets`**、**41 只缺 `financial_expenses`**。`总资产 − 流动负债` 这个分母根本算不出来。
3. **好不容易算出来的也是垃圾**。招商银行 ROE 11.5%，但用 `总资产 − 流动负债` 算出的 ROCE 只有 **1.7%**——因为"流动负债"里装着 80% 的存款，减完存款剩下的数字没有经济含义。

### 11.2 工商银行、中国平安当然是好公司

**排除的是"指标不适用"，不是"公司不好"。** 这两家在我们的库里数据齐全：

| 公司 | 市值 | ROE | PB | PE | 股息率 |
|---|---|---|---|---|---|
| 工商银行 | 32,255 亿 | 9.0% | **0.74** | 8.4 | 5.10% |
| 建设银行 | 32,072 亿 | 9.4% | 0.81 | 9.0 | 3.17% |
| 招商银行 | 10,945 亿 | **11.5%** | 0.79 | 7.2 | 4.65% |
| 中国平安 | 9,896 亿 | 6.9% | **0.67** | 10.0 | 5.00% |
| 中国人寿 | 9,895 亿 | 11.6% | 1.49 | 13.1 | 2.79% |

**量银行要用银行自己的尺子**：ROE（核心）、PB（估值）、净息差、不良率、拨备覆盖率、核心一级资本充足率；保险看新业务价值、内含价值、综合成本率。本库 `financial_sector_items` 表已经采了 33 个金融专用科目（存款、贷款、同业、保险合同负债等），只是 ROCE 用不上它们。

**书里也是这么做的**——原文明写「因此，**非金融企业**的 ROCE 可定义为：息税前利润 ÷（净营运资本 + 净固定资产）」。

### 11.3 对"大师指标调研"的启示

第 2–8 节里的 42 个指标，凡是分母用到「营运资本」「流动比率」「总资产周转」的，**对金融股一律失效**，必须在 SQL 里显式剔除（用 `sw_level1`，不是 `csrc_l1`）。审查环节已发现巴菲特「一美元测试」混入 121 只金融股、质量学派「增量 ROIC」让齐鲁银行和江苏银行霸榜——都是同一个原因（见 §4.2 P1-a）。

---
