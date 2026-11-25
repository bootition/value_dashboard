---
title: DSL 维度校验按存储量纲比较（13 个指标 unit 误标）+ 周期白马筛选口径
status: approved
category: reports
created: 2026-11-25
last-reviewed: 2026-11-25
data-source: data/valuedashboard.duckdb（筛选快照 2026-11-24 建立；财报期 2026-09-30；价格日 2026-11-24）
---

# DSL 维度校验按存储量纲比较 + 周期白马筛选口径

> **起因**：用户问「蓝筹/白马怎么量化筛选」。首轮给出的两条规则在实跑时暴露
> 单位口径问题，用户追问「是硬错误吗？其他方面也会出错吗？修一修」，
> 同时提出「周期股不该全剔，周期底部也有买的价值」。
> 本报告记录：①三个疑点的定级（2 个真错 / 1 个非错）②真错的修复与回归 ③周期白马的可用口径与实测名单。

---

## 1. 结论先行

| 疑点 | 定级 | 证据 |
|---|---|---|
| `dividend_yield` 声明 `percent` 实存小数 | ❌ **硬错误（静默 100 倍）**，且**不止 1 个字段，共 13 个** | §2.1 |
| DSL 把 `ratio` 与 `pct` 判成不同维度 | ❌ **硬错误（false reject）**，把文档推荐用法拦死 | §2.2 |
| 条件 JSON 的 `right_field` 不做任何量纲校验 | ❌ **硬错误（同款静默 100 倍，另一条链路）** | §2.3 |
| 周期股混进「白马」名单 | ⚪ **不是错误**，是口径选择；已按用户意见改为**新增**周期白马规则 | §4 |
| `min_listing_years` 默认 1 年 | ⚪ **不是错误**，是默认值；UI 可改（`ScreeningPage.vue:872`） | §2.4 |

修完的验证：定向 pytest **25 passed**；相关域回归 **59 + 77 + 93 passed**；`ruff` 全绿；`check_rework_guards.py` 10 组规则通过。

---

## 2. 三个疑点的定级与证据

### 2.1 指标 `unit` 与存储口径漂移（13 个，不是 1 个）

`app/core/dsl/ast_nodes.py::INDICATOR_METADATA` 是 DSL 侧的 unit 镜像，
存储口径的真源是 `app/core/screening/field_units.py`。两条真源漂移后实测：

| 字段 | 声明 | 实存（正式库中位） | 后果 |
|---|---|---|---|
| `dividend_yield` | `percent` | 0.0129（小数） | 与 `ttm_dividend_yield`(1.29) 差 100 倍 |
| `revenue_yoy` | `percent` | 0.0371 | 与 `turnover_rate` 等百分数字段混比恒错 |
| `net_profit_yoy` / `deducted_profit_yoy` | `percent` | −0.0257 / −0.0206 | 同上 |
| `revenue_cagr3/5`、`net_profit_cagr3/5`、`deducted_profit_cagr3/5` | `percent` | 0.0309 / 0.0667 / −0.0081 / 0.0203 / −0.0038 / 0.0342 | 同上 |
| `period_return` / `annualized_volatility` / `max_drawdown` | `percent` | −0.2797 / 0.4527 / 0.4858 | 同上 |

**为什么是硬错误**：`validator._check_dimensions` 只比较 unit 字符串，
`dividend_yield > ttm_dividend_yield` 因此**通过校验**并编译成
`0.0255 > 2.55` → 恒假、筛出 0 只、无任何提示。修复前实测输出：

```
dividend_yield > ttm_dividend_yield    valid=True    err=[]
```

同一错误还会经 `/api/screening/indicators` 的 `_dsl_indicator_unit(ast)`
把 AST 根单位 `percent` 映射成前端 `percent` 格式（原值 ×1 带 %），
使以这些指标为根的**已发布复合指标**显示成 1/100。

修法：13 个字段 `percent` → `pct`（`pct` = 小数存储的百分比，前端 `×100 + %`），
并在 `ast_nodes.py` 落一段量纲说明，指向 `field_units.py` 为真源。

### 2.2 `pct` 与 `ratio` 被判成不同维度（false reject）

`field_units.py` 把"小数百分比"登记为 `pct`，而 `INDICATOR_METADATA` 里
`roe/debt_ratio/...` 写的是 `ratio`，派生字段 `balance.interest_bearing_debt_ratio`
写的是 `pct` —— 两者都是 0–1 的小数，但校验器按字符串比较 → 拒绝。修复前实测：

```
debt_ratio > balance.interest_bearing_debt_ratio     valid=False  err=['维度不匹配: ratio > pct ...']
balance.net_cash_to_assets > debt_ratio              valid=False  err=['维度不匹配: pct > ratio ...']
```

**为什么是硬错误**：`app/core/dsl/derived_fields.py` 的 doc 明确建议用户把这两个
条件组合成「高负债率 + 低有息负债率」的排雷条件 —— **文档推荐的用法写不出来**。

修法：`validator.py` 新增 `DIMENSION_CLASS` / `dimension_class()`，维度校验只认
**存储量纲**：`pct` 与 `ratio` 同为「无量纲小数」等价；`percent`（1 = 1%）独立；
`CNY` 与 `price` 同属金额。报错信息同时给出量纲类名，便于用户定位。

### 2.3 条件 JSON 的 `right_field` 无任何量纲校验

DSL 之外还有一条筛选链路：条件 JSON 的字段对字段比较（`right_field`）。
`ScreeningEngine._compatible_fields` 只排除元数据列，**不校验量纲**，于是
`dividend_yield > ttm_dividend_yield` 在这一侧同样能存能跑、结果恒假。

修法：新增 `_magnitude_conflict()`，只拦**已确证的 100 倍对**
（`pct` ↔ `percent`），不扩大到金额/股数，避免误伤「每股股息 vs 每股价格」这类
合法比较；报错文案直接说清两侧量纲。

### 2.4 不是错误的两个疑点

- **周期股混入**：规则阈值选择问题，不是代码错。按用户意见改为新增独立规则（§4）。
- **`min_listing_years` 默认 1**：是产品默认值，UI 有输入框（`frontend/src/views/ScreeningPage.vue:872`），
  蓝筹/白马自查时把它设成 3–5 年即可。

### 2.5 顺带澄清：两个"百分位"字段的量纲不同（**未改动，仅登记**）

- `research_statistics` 的分位（条件字段 `*_stat_{N}y_percentile`）存 **0–100**（中位 19.275）；
- 引擎 `rank_mode` 生成的 `*_percentile` 列是 `PERCENT_RANK()` → **0–1**。

前端 `fieldInputUnit()` 对两者都给 `label='分位', scale=1`（输入原值），
且 `rank_mode` 下拉已从 UI 移除（历史规则兼容用），因此**当前不构成用户可见错误**。
登记进 `ops-knowledge-base` §23，避免以后有人按 0–1 去填 `*_stat_*_percentile`。

---

## 3. 修复清单

| 文件 | 改动 |
|---|---|
| `app/core/dsl/ast_nodes.py` | 13 个指标 `unit: percent → pct`；`INDICATOR_METADATA` 上方新增量纲说明（pct/ratio/percent/CNY/count 的语义与真源指针） |
| `app/core/dsl/validator.py` | 新增 `DIMENSION_CLASS` + `dimension_class()`；比较/加减法改按等价类比较，报错带量纲类名 |
| `app/core/screening/engine.py` | 新增 `_magnitude_conflict()` + `_magnitude_label()`；`right_field` 比较前先拦跨量纲 |
| `tests/regression/test_dsl_unit_dimension_classes.py` | 新增 25 例：跨量纲必拦（DSL + 条件 JSON）、同量纲必放行、等价类契约、以及「unit 声明 ↔ `field_units.py`」双向对拍棘轮 |
| `docs/runbooks/ops-knowledge-base.md` | §23 登记量纲事实（含 §2.5 的两个分位尺度） |

棘轮设计（防止再次漂移）：`test_every_pct_decimal_field_is_known_to_the_dsl` 用
`field_units.PCT_DECIMAL_FIELDS` 反查 DSL 侧 unit；例外只允许两类且**由结构推导**
（`EXTENDED_SCREENING_READY` 扩展域列、`*_yoy_fy` 年报口径），不写死名单。
`test_decimal_pct_fields_are_not_declared_percent` 直接锁住本次缺陷形态。

---

## 4. 周期白马：口径与实测名单

用户判断：**周期股不必全剔**——周期底部有买的价值，且大公司不容易倒闭。
因此不把周期股排除，而是给它们一套**不同的判据**：

| # | 条件 | 阈值 | 为什么这样定 |
|---|---|---|---|
| 1 | `total_market_cap` | ≥ 300 亿 | 规模即存续能力：大到不容易倒 |
| 2 | `roce_10y_median` | ≥ 10% | **穿越周期**的资本回报，而不是当期景气度 |
| 3 | `pb_mrq_stat_10y_percentile` | ≤ 30 | 价格处在**自身 10 年低位**（0–100 尺度，见 §2.5） |
| 4 | `debt_ratio` | ≤ 60% | 周期低谷要能扛过利息 |
| 5 | `balance.interest_bearing_debt_ratio` | ≤ 35% | 段永平口径：真正要还本付息的部分 |
| 6 | `consecutive_div_years` | ≥ 5 | 熊市里仍在分红 = 现金流是真的 |
| 7 | `cf_to_net_profit` | ≥ 0.5 | 周期底部利润难看，放宽但仍要有现金 |

**刻意不用 PE**：周期底部盈利被压到极低时 PE 反而很高（甚至为负），
用 PE 会把底部筛掉、把顶部筛进来。周期股的位置判断必须用 PB 分位。

实测漏斗（**引擎实测**，非离线拼 SQL；池 5,220 = 上市≥1年 + 非 ST + 非停牌，
数据截至 2026-11-24，财报期 2026-09-30）：

| 累加（周期分支） | 剩余 |
|---|---|
| PB ≤ 自身 10 年 30% 分位 | 2,500 |
| + 市值 ≥ 300 亿 | 198 |
| + ROCE 10 年中位 ≥ 10% | 109 |
| + 资产负债率 ≤ 60% | 84 |
| + 有息负债率 ≤ 35% | 74 |
| + 连续分红 ≥ 5 年 | 70 |
| + 经营现金流/净利润 ≥ 0.5 | **59** |

对照的成长分支（`total_market_cap≥100亿 + roe≥15% + gross_margin≥30% + net_profit_cagr5≥15%
+ cf_to_net_profit≥0.8 + debt_ratio≤50% + ttm_dividend_yield≥1 + 0<pe_ttm≤40`）**37 只**。
两者合并（OR）**91 只**，交集 5 只 —— 即「周期白马」与「成长白马」是**互补但不互斥**的两批公司。

> ⚠️ **本轮自我更正**：会话中先给出过「周期白马 13 只 / 与成长白马重叠 0 只」，
> 那是**离线手写 SQL 的产物**：用「每股最新一行资产负债表」近似派生字段
> `balance.interest_bearing_debt_ratio`，与引擎的 fail-closed 口径（借款科目全空 → NULL、
> 快照期对齐）不一致，把 74 只压成了 19 只。**以上 59 / 37 / 91 才是引擎实测值**，
> 教训与 KB 里 I-NAIVE SQL 型事故同源：**派生字段的数值只能由产出它的那条链路给**，
> 自拼 SQL 复算等于换口径。

### 本节的交付物（写入用户真实库）

| 前端位置 | 产物 | 事实 |
|---|---|---|
| 筛选页 | 规则「蓝筹股」`rule_id=55` v1 | 7 个 AND 条件，跑出 **51 只** |
| 筛选页 | 规则「白马股」`rule_id=56` v1 | OR(成长 9 条, 周期 7 条)，跑出 **91 只** |
| 保存结果页 | `screening_results` #14「蓝筹股 2026-11-25」 | 规则来源 + 列定义已冻结 |
| 保存结果页 | `screening_results` #15「白马股 2026-11-25」 | 同上 |
| 自选页 | 分组「蓝筹股」`kind=rule` `rule_ids=[55]` | 51 只，`source_rule_id/source_result_id` 齐全（可一键刷新） |
| 自选页 | 分组「白马股」`kind=rule` `rule_ids=[56]` | 91 只，同上 |

写入方式：起正式 profile 的 web 服务，**逐条打前端真实使用的 HTTP 接口**
（`POST /api/watchlist/groups` → `/api/screening/rules/save` → `/run` → `/save`
→ `/add_to_watchlist`，带 `/api/session` 的 `write_token`），与人工在页面上点
完全同一条代码路径；未直接改库。写前已整库备份
`data/backup_accept/valuedashboard.PRE-AGENT-20261125-194129.sqlite`。

---

## 5. 未做（留待用户决定）

- **`roce_latest` 未进条件白名单**：它登记在 DSL 指标里，但不在
  `SNAPSHOT_COLUMNS`（`_is_known_field` 判 False），因此「当期 ROCE 低于自身 10 年中位」
  这条**周期位置确认**条件目前在条件 JSON 里写不出来（DSL 侧可以）。
  若要更严谨的周期定位，需开放该字段（1 行白名单 + 指标列表 + 回归）。
- **规则没有做成「预设模板」**：两条规则已作为普通已保存规则落在库里（#55/#56），
  不是内置模板；新建规则时不会自动出现在下拉里的「模板」分区。
- **`白马股` 91 只偏宽**：若需要更精选，可把周期分支的
  `consecutive_div_years` 提到 ≥8（跨过完整一轮周期）或把 PB 分位收到 ≤20；
  实测收紧后数量与名单需重跑确认，本轮未改（避免为凑数量调参）。
