---
title: 指标标签中文化与过耦合审计（复合指标纯英文 + 61 个参数化字段）
status: approved
category: reports
created: 2026-11-16
last-reviewed: 2026-11-16
---

# 指标标签中文化与过耦合审计

> 用户 2026-11-16 反馈两件事：
> 1. 下拉里仍有纯英文指标 `dividend_financing_ratio (DSL v2)(%)`，
>    「我不是说过所有指标都应该至少有中文吗？」
> 2. 「再查一查，指标还有没有过耦合的情况。现在这个指标是铺到最浅了吗？
>    是不是可以只用这些指标加那些筛选条件，就组合成一些复合指标进行筛选？」

---

## 1. 纯英文标签：根因与修复

### 1.1 根因（两处都错）

后端 `app/web/api/screening.py` 给已发布 DSL 复合指标的标签是：

```python
"label": f"{expression['name']} (DSL v{expression['version']})",   # ← 纯英文
"display_name": _DSL_INDICATOR_LABELS.get(expression["name"], expression["name"]),
```

而前端 `indicatorLabelOf()` 的 DSL 分支只读 `label`：

```ts
const baseLabel = meta.dsl ? (meta.label ?? meta.name) : fieldDisplayName(...)
```

结果：**后端明明给了中文 `display_name`，前端却用了英文 `label`**，
且前端 `FIELD_LABELS` 里本来就有 `dividend_financing_ratio: '分红融资比（A股）'`，
也被 DSL 分支绕过。

### 1.2 修复

| 层 | 改动 |
|---|---|
| 后端 | 新增 `_dsl_indicator_label()`：有中文名 → `分红融资比（复合指标 v2）`；无中文名 → `复合指标 {name}（v{n}）`（**必须带中文标识**） |
| 前端 | DSL 分支改为：`display_name`（后端权威中文）→ `fieldDisplayName()`（FIELD_LABELS / STATEMENT 映射）→ 后端 label → 字段名兜底 |

### 1.3 顺带查出：11 个基础字段也缺中文名

用脚本把 `SNAPSHOT_COLUMNS + NORMALIZED_FIELDS` 逐个对照前端的三张映射表
（`FIELD_LABELS` / `*_FIELD_LABELS` / 统计模板），发现 **11 个字段**没有中文名，
界面上会显示「资产负债表 · accumulated_depreciation」这种半英文标签：

| 字段 | 补的中文名 |
|---|---|
| `balance.accumulated_depreciation` | 累计折旧 |
| `balance.fixed_assets_gross` | 固定资产原值 |
| `balance.investment_property` | 投资性房地产 |
| `balance.development_expenditure` | 开发支出 |
| `balance.other_current_assets` | 其他流动资产 |
| `balance.receivables_financing` | 应收款项融资 |
| `balance.long_term_payables` | 长期应付款 |
| `balance.advance_from_customers` | 预收款项及合同负债（合并口径） |
| `balance.advance_from_customers_ratio` | 客户占款率（预收+合同负债）/总资产 |
| `income.impairment_loss_positive` | 资产减值损失（损失为正） |
| `income.credit_impairment_loss_positive` | 信用减值损失（损失为正） |

**结果：基础字段中文名覆盖 156/156 = 100%。**

### 1.4 加不变量测试，防再退化

| 用例 | 钉死的契约 |
|---|---|
| `test_dsl_indicator_label_is_chinese_first` | 有中文名 → 以「分红融资比」开头；无中文名 → 必须以「复合指标」开头且含中文 |
| `test_every_indicator_label_contains_chinese` | 遍历样本（含未知 DSL 名），标签必须含 CJK 字符 |

---

## 2. 过耦合审计：还有 61 个「后缀参数化」字段

### 2.1 审计方法

对 `SNAPSHOT_COLUMNS + NORMALIZED_FIELDS + STAT_FIELDS` 全量扫描
「同一指标 + 参数后缀」模式，找出本该参数化却被铺成 N 个字段的家族。

### 2.2 结果

| 家族 | 字段数 | 例子 | 本质 |
|---|---|---|---|
| **统计窗口 × 方法** | **40** | `pe_ttm_stat_5y_percentile`、`pb_mrq_stat_99y_zscore` | 4 指标 × 5 窗口 × 2 方法 |
| **国债期限利差** | **9** | `div_yield_spread_0p25y` … `div_yield_spread_30y` | 同一公式，期限是参数 |
| **CAGR 年数** | **6** | `revenue_cagr3`、`net_profit_cagr5` | 同一公式，年数是参数 |
| **均线窗口** | **6** | `ma5`、`ma10`、`ma60`、`ma250` | 同一公式，窗口是参数 |
| **合计** | **61** | | |

对比：排名家族原本 **930** 个字段，改成条件级 `rank_mode` 后变成 **0** 个字段 + 6 个口径。
同样的问题还剩 61 个。

### 2.3 但它们的性质与排名不同，不能一刀切

| 家族 | 能否像 `rank_mode` 一样收进下拉 | 理由 |
|---|---|---|
| **国债期限利差（9）** | ✅ 建议收 | 期限是纯参数，`div_yield_spread(tenor=10y)` 语义清晰；9 个字段在 DSL 里也无法表达「期限」这个概念 |
| **CAGR 年数（6）** | ✅ 建议收 | 同上，`cagr(3)` / `cagr(5)` 是函数，不该是字段名 |
| **均线窗口（6）** | ⚠️ 可收但要保留字段 | MA 是**价格技术指标**，日常用法就是「MA20 上穿 MA60」这类跨窗口比较，收成参数后就无法在同一条件里比较两条均线 |
| **统计窗口×方法（40）** | ❌ 不建议收 | 它对应 PRD §10.7 的**独立研究统计域**，每条记录带序列、股息率口径、国债期限、窗口、方法、阈值等元数据（PRD §12.2 明确要求「每条条件必须显式保存序列、口径、期限、窗口、统计方法和阈值」）。强行收成下拉会丢掉这些可审计元数据 |

**结论：61 个里建议收 15 个（期限 9 + CAGR 6），保留 46 个（统计域 40 + 均线 6）。**

---

## 3. 回答「现在铺到最浅了吗？」

**没有最浅。分成三层看：**

| 层 | 现在是否最浅 | 说明 |
|---|---|---|
| 排名/分位 | ✅ 已最浅 | 930 字段 → 6 个条件级口径 |
| 期限 / CAGR | ❌ 还有 15 个可收 | 见 §2.3 |
| 统计域 / 均线 | ⭕ 已到位 | 前者有元数据审计要求，后者需要跨窗口比较 |

而且**基础字段本身（156 个）已经到底了**——它们是数据库真实列，
再"浅"就是直接写 SQL 了。`balance.total_assets` 不可能再合并。

---

## 4. 回答「能不能只用这些指标 + 筛选条件组合成复合指标？」

**能，而且这正是当前的三层架构：**

```
① 基础字段（156 个，数据库真实列）
        ↓  DSL 复合指标（app/core/dsl/，可发布、带版本、哈希锁定）
② 复合指标（如 rank(pe_ttm)、roce_5y_median / pb_mrq）
        ↓  筛选条件（AND/OR 嵌套 ≤3 层、≤20 条）+ 排名口径 + 排序 + 结果列
③ 筛选规则 → 结果集
```

**实测已验证**（`test_screening_published_dsl.py`）：

```python
# 发布一个复合指标
INSERT INTO dsl_expressions (name, version, expression_text, status, ...)
VALUES ('pe_rank', 1, 'rank(pe_ttm)', 'published', ...)

# 筛选条件直接引用它
{"conditions": {"logic": "AND",
                "rules": [{"field": "pe_rank", "op": "=", "value": 1}]}}
→ 返回 PE 排名第 1 的那只股票 ✅
```

DSL 支持的横截面函数：`rank()` / `rank_industry()` / `percentile()` /
`zscore()` / `normalize()`；以及 `TTM` / `YoY` / `QoQ` / `CAGR` /
rolling / lag 等时序运算（PRD §11.2）。

所以：**不需要为每个想法新增字段**。想筛「ROCE 高但 PB 低」，
发布一个复合指标 `roce_5y_median / pb_mrq` 即可，不必让后端加列。

---

## 5. 变更清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `app/web/api/screening.py` | 新增 `_dsl_indicator_label()` + `_DSL_FALLBACK_PREFIX`；DSL 标签改为中文优先 |
| 2 | `frontend/src/views/ScreeningPage.vue` | `indicatorLabelOf()` DSL 分支改为 `display_name` → `fieldDisplayName` → label → 字段名 |
| 3 | `frontend/src/utils/screening-format.ts` | 补 11 个基础字段中文名 |
| 4 | `tests/regression/test_screening_rank_mode.py` | 新增 2 条中文不变量用例（共 16 条） |

**门禁**：`test_screening_rank_mode.py` **16 passed**、Ruff 全绿、前端 lint + build 全绿。

---

## 6. 待你决定的下一步

是否把 **15 个可收字段**（国债期限利差 9 + CAGR 6）也改成条件级参数？

| 方案 | 效果 | 代价 |
|---|---|---|
| 做 | 下拉再减 15 条（196 → 181），且期限/CAGR 可自由取值（如 8 年 CAGR） | 需新增两种条件参数（`tenor` / `years`），前后端各改一次，约半天 |
| 不做 | 保持现状，功能不受影响 | 9 个期限固定死，想要 8 年期国债利差就得开发加字段 |

我倾向**做**：`div_yield_spread` 的 9 个期限里，实际常用的只有 10 年期，
剩下 8 个基本是噪音；`cagr8` 这类需求反而真实存在。


---

## 7. 落地：把 15 个参数化字段收成条件参数（用户裁定「查出来就直接修复」）

§2.3 建议收的 15 个字段已落地。机制与 `rank_mode` 一致：**库里仍是真实列，
但下拉里只暴露基名 + 参数下拉**。

| 家族 | 收起前 | 收起后 |
|---|---|---|
| 国债期限利差 | 9 个字段（`div_yield_spread_0p25y` … `_30y`） | **1 个入口** `div_yield_spread` + 期限下拉（9 档） |
| CAGR 年数 | 6 个字段（`{revenue,net_profit,deducted_profit}_cagr{3,5}`） | **3 个入口** + 年数下拉（3 / 5） |
| 合计 | **15** | **4** |

写法：

```json
{"field": "revenue_cagr", "cagr_years": 3, "op": ">=", "value": 0.15}
{"field": "div_yield_spread", "tenor": "10y", "op": ">=", "value": 0}
```

`expand_rank_modes()` 在入口按 **参数 → 排名** 的顺序展开：
`div_yield_spread` + `10y` + `market_rank` → `div_yield_spread_10y_market_rank`。
旧的带后缀字段名仍然兼容（已保存规则继续可跑），只是不再出现在下拉里。

### 7.1 四类错误都给可执行提示（不是「未知字段」）

| 输入 | 提示 |
|---|---|
| `cagr_years: 8` | `cagr_years 取值必须是 3, 5，收到 8` |
| 只给 `revenue_cagr` 不给参数 | `revenue_cagr 需要指定 cagr_years（可选：3, 5）` |
| `roe` + `cagr_years` | `roe 不支持 cagr_years；只有 revenue_cagr, net_profit_cagr, deducted_profit_cagr 支持` |
| CAGR + 排名（CAGR 不在可排名清单） | `该字段不支持排名口径（不在可排名清单里）: revenue_cagr3_market_rank（基名 revenue_cagr3）` |

最后一条要特别说明：`_rank_base_for()` 只在基名**可排名**时才返回基名，
所以「基名存在但不可排名」会被它判成 `None`，最终落到含糊的「未知字段」。
新增 `unrankable_rank_base()` 专门区分这两种情况 —— 用户拼错名字 vs 换字段。

### 7.2 遮蔽要向上传播（施工中发现的坑）

`deducted_profit_cagr3/5` 会因为 `DEDUCTED_PROFIT_GAP` 被判 `masked_untrusted`。
收起后下拉里只剩基名 `deducted_profit_cagr`，若不传播遮蔽，
用户能选中一个**实际会被 409 拒绝**的入口。已加 `_masked_param_columns()`
把遮蔽传播到基名，并在提示里列出被遮蔽的具体列。

### 7.3 三个入口都支持参数

| 位置 | 交互 |
|---|---|
| 筛选条件 | 指标后面出现「年数」/「国债期限」下拉 |
| 排序规则 | 同上，字段旁出现参数下拉 |
| 结果展示列 | 每个参数取值一个选项（`营收3年复合增速 · 年数 3`） |

结果列的合成值编码扩展为 `字段@参数=值::rank::口径`（`decodeResultColumn` / `encodeResultColumn`）。

### 7.4 测试

`test_screening_rank_mode.py` 从 16 条扩到 **24 条**，新增 8 条：

| 用例 | 契约 |
|---|---|
| `test_param_families_hide_suffixed_fields` | 15 个后缀字段被隐藏，4 个基名保留且基名不是真实列 |
| `test_expand_param_cagr_and_tenor` | 参数展开成真实列名并移除参数键 |
| `test_param_value_must_be_in_options` | 8 年 / 12 年期显式报错 |
| `test_param_base_without_value_is_rejected` | 基名不带参数 → 提示需要指定参数 |
| `test_param_on_wrong_field_is_rejected` | `roe` + `cagr_years` → 报不支持 |
| `test_param_and_rank_mode_compose_when_rankable` | 参数先于排名展开 |
| `test_unrankable_base_gives_actionable_error` | 可排名/不可排名/不存在三种分类正确 |
| `test_param_works_in_conditions_sort_and_columns` | 三个入口联用 |

**端到端实测（正式库）**：条件 `revenue_cagr3 >= 15%` + `div_yield_spread_10y >= 0`
按 CAGR 降序 → 命中 251 只，结果列 `div_yield_spread_10y` 正确带出。

门禁：24 passed、10 个回归文件 **145 passed**、Ruff 与前端 lint/build/70 vitest 全绿。

---

## 8. UI 截断全项目审计（2 个 Agent）与修复

用户反馈「排名口径下拉根本看不全，只能看到『原…』『全…』『证…』」，
要求全项目排查同类问题。两个 Agent 分别做了「下拉/文本截断」与「排序能力」审计。

### 8.1 真根因不是缺 tooltip，是 **grid 轨道动态错位**

`RuleConditionRow` 用 CSS Grid，6 条轨道靠**自动放置**排布。加入参数下拉后：

| 字段有参数时 | 子元素 | 落到的轨道 |
|---|---|---|
| 无参数（如 `pe_ttm`） | 序号、指标、**排名口径**、关系、值、操作 | 排名口径掉进第 3 轨（参数轨，88px） |
| 有参数 | 序号、指标、参数、排名口径、关系、值、**操作** | 第 7 个元素掉进第 1 轨（序号轨，40px） |

排名口径落在 88px 轨道 → 文本区约 27px → **只能装 1 个字 + 省略号**。
更糟的是 naive-ui `consistentMenuWidth` 默认 `true`，
**弹层宽度被锁成触发器宽度**，所以「点开也看不到全名」。

**修法**：轨道改为 7 条并给每个控件**显式 `grid-column`**（v-if 缺省时空出该轨，
不后移）；4 个 `n-select` 全部加 `:consistent-menu-width="false"`；保留 `NEllipsis` tooltip。

### 8.2 其他修复

| 位置 | 问题 | 修法 |
|---|---|---|
| `BusinessOverviewSection.vue:140` | 所属行业 CSRC 名最长 30 字（≈360px），格子 174px + nowrap 省略号，无 title | 加 `:title` |
| `AnnouncementTimelineCard.vue:172` | 公告标题最长 101 字，无 title | 加 `:title` |
| `ScreeningPage.vue:898` | 已保存规则下拉，本页唯一既无 `render-label` 也无 `consistent-menu-width` | 挂 `renderOptionLabel` |
| `ScreeningResultsPanel.vue:379` | 结果列多选无 `render-label`（同页另一处有，不一致） | 新增 `renderColumnLabel` |

### 8.3 Agent 排除的假阳性（记下来防下一轮返工）

- `width: 90px` 的升序/降序、130px 公告时间窗、96px 窗口选择 —— **宽度窄但内容更短，不要一起改**
- `IndustryContributionBars.vue` 150px + ellipsis 无 tooltip —— 但数据源 `sw_level1` 实测 31 个行业名**全是 4 字**，完全富裕
- 数据状态页/黑名单/ETF/指数页的窄列装长英文 —— naive-ui 表格 `word-break: break-word`，**换行不截断**
- 全项目 `ellipsis: true`（不带 tooltip）**0 处** —— 4 处 ellipsis 全是 `{ tooltip: true }`

---

## 9. 排序架构审计：PRD 与实现漂移（待决策）

Agent 查清了用户质疑「为什么在筛选的时候就让它排序」：

### 9.1 现状：排序确实是**规则版本的一部分**，而且有副作用

| 事实 | 证据 |
|---|---|
| 排序存进 `rule_json` | `ScreeningPage.vue` 保存时 `rule_json: { conditions, sort, columns }` |
| 改排序会**新建规则版本** | `/rules/save` 是追加式（`version = existing + 1`），而运行前若脏会自动保存 |
| 对比：`columns` 可随 `/run` 覆盖 | `ScreeningRequest` 有 `columns`，**没有** `sort` |
| 服务端 `ORDER BY` 后 `LIMIT 5000` | 排序不同 → **被保留的 5000 只股票不同** → 冻结进结果快照与 CSV |
| 严格模式下排序字段进 strict 断言 | 仅改排序字段**就可能过滤掉行** |

### 9.2 结果表其实已经能点表头排序

`ScreeningResultsPanel` 每列都有 `sorter: 'default'`，纯客户端本地排序、不落库。
**但它与导出是两套顺序**：CSV 走 `/export_csv` 按 `result_json` 顺序写，界面无任何提示。

### 9.3 PRD 怎么说

- §12.5：「明确多字段排序」与「明确列配置」并列在**结果展示**
- `:576`：保存结果必须保留排序
- `:764`：**「结果页可配置列、多字段排序」**

→ **PRD 从未要求排序属于规则版本**，现状是实现漂移。

### 9.4 建议（待用户批准，涉及 API 语义变更）

把 `sort` 像 `columns` 一样改成**运行参数**（去版本化）：

1. `/run` 请求体新增 `sort`，`rule_json` 不再强制含 sort；
2. 服务端 `ORDER BY` 保留（保住「截断可复现」与「导出同序」）；
3. 结果页表头 sorter 保留，但补提示「仅影响当前视图，保存/导出按规则排序」并显示当前服务端排序字段；
4. 截断提示改为「按 <排序字段> 取前 5000 条，调整排序会改变被保留的股票」。

**不推荐纯前端排序**：那会让「截断保留谁」退回不可控的 `stock_code` 兜底序。

---

## 10. 排序改造落地：筛选页删除排序，结果页点表头（2026-11-16 用户裁定）

### 10.1 用户的决定

> 「筛选的时候把排序按钮删掉，然后在结果上面留一下就行了呗。
> 比如它排序的时候显示 PE、市值这些东西，然后就让它点一下，就能升序排列或者降序排列。」

采纳。§9 那套「sort 改成运行参数」的复杂方案**不做**，直接按用户说的删。

### 10.2 改了什么

| 位置 | 改动 |
|---|---|
| 筛选页 | **删除整个「结果排序」区块**（原 02 区块），「结果展示列」顺延为 02 |
| 筛选页状态 | 删除 `sortRules` / `SortRule` / `addSortRule` / `removeSortRule` / 排序参数三级联动函数 |
| 规则保存 | `rule_json` **不再写入 `sort`**；草稿不再保存/恢复 `sort`；脏检查不再比较 `sort` |
| 编辑器快照 | 只含 `conditions`（否则残留的 sort 会让「改排序也算未保存修改」） |
| 结果页 | 保留原有的**表头点击排序**（每列 `sorter: 'default'`，纯前端）；新增提示「点列头可升序/降序排列」 |
| 结果面板 `sort` prop | 改为可选并标注废弃（面板内本来就没用到，是死 prop） |

**旧规则兼容**：库里已有规则若 `rule_json` 带 `sort`，引擎仍会照它排序（不影响可复现性，
排序随规则版本一起冻结）。新规则不带 `sort` → 服务端回退 `ORDER BY stock_code`（确定性）。

### 10.3 为什么这么改是对的（用大白话说）

之前排序写在「筛选规则」里，会带来三个看不见的副作用：

1. **改一次排序 = 存成一个新规则版本**（版本号一路涨，但公司名单没变）
2. **结果最多只留 5000 家**，而排序发生在截断**之前** → 换个排序，
   **被留下的 5000 家也会变**（想看第 5001 名就得改排序，逻辑上说不通）
3. **严格模式下排序字段会被当成过滤条件** → 仅改排序就可能少掉几家公司

放到结果页点表头就没这些事：**筛选只负责"选哪些公司"，排序只负责"怎么显示"**，
两者彻底分开。这也符合 PRD §12.5 / `:764`「结果页可配置列、多字段排序」。

### 10.4 门禁

- 前端 lint + build + **70 vitest** 全绿
- 后端 `test_screening_draft` / `test_screening_api_regressions` /
  `test_screening_rank_mode` / `test_screening_engine_regressions` **58 passed**
- 全项目已无 `sortRules` / 「结果排序」残留（grep 确认）

---

## 11. 二次中断：我又漏了 4 个基名，用户第三次看到纯英文

用户截图（第二张）显示下拉里仍有：

```
revenue_cagr(%)
net_profit_cagr(%)
deducted_profit_cagr(%)
div_yield_spread(%)
```

**这是我 §7 自己造成的新遗漏**：把 15 个参数化字段收成基名后，
前端 `FIELD_LABELS` 里只有带后缀的 `revenue_cagr3/5`，**基名没有中文名**。
而 §1 写进知识库的 `I-LABELCN` 讲的正是这件事 —— 自己违反了。

已补：`营收复合增速` / `净利润复合增速` / `扣非净利复合增速` / `股息率-国债利差`。

## 12. 派 Agent 独立审查我的修复（用户要求）

用户明确要求：「你修复问题的时候，记得派一个agent去审查一下你到底搞得怎么样？」
审查员独立枚举（**不复用我的守卫测试逻辑**，避免继承同样的盲区）后给出 `pass_with_fixes`：

### 12.1 修好的部分（审查员实测确认）

| 项 | 结论 |
|---|---|
| 4 个基名中文名 | ✅ 修好（`f6d043d` 前后对照） |
| 独立枚举 242 个指标名 → 纯英文 | **0 个** |
| 排序残留 `sortRules`/`SortRule`/「结果排序」/「排序方式」 | **0 处** |
| `rule_json` 是否仍含 sort | 否，只剩 `{conditions, columns}` |
| 引擎无 sort 时行为 | `ORDER BY stock_code`（确定性） |
| 表头排序是否真可用 | 实测点两次：降序→[30,20,5]，升序→[5,20,30] ✅ |
| 后端是否仍认旧后缀字段 | ✅（`revenue_cagr3` 等继续可用） |
| 遮蔽向上传播是否生效 | ✅（把 `_masked_param_columns` 改成 `return []` 后对应测试立刻失败） |

### 12.2 审查员抓到的真问题（已全部修复）

| # | 严重度 | 问题 | 修法 |
|---|---|---|---|
| 1 | **P0（守卫盲区）** | 守卫只枚举 185 个，**漏了 `EXTENDED_SCREENING_READY` 55 个**；实测删掉 `tobin_q` 的中文名，测试照样通过 | 枚举补到 6 个 append 站点（含扩展域 + `METADATA_COLUMNS`） |
| 2 | **P0（守卫盲区）** | `STAT_FIELDS` 整段被 `continue` 跳过，三张 stat 模板表从未校验；实测删掉 `STAT_METRIC_LABELS.spread_10y` 测试仍通过 | 改为校验 `STAT_METRIC_LABELS` 必须含中文，不再跳过 |
| 3 | **P1（真 bug）** | 结果表头显示 `listing_date` 等纯英文（默认结果列里就有） | 补 7 个元数据列中文名：上市日期/是否 ST/是否停牌/总股本/流通股本/申万一二三级行业 |
| 4 | **P1（真 bug）** | `STAT_METRIC_LABELS` 的 `pe_ttm`/`pb_mrq` 值是 `PE-TTM`/`PB-MRQ` —— **纯英文**，统计域字段显示成「PE-TTM · 5年 · 历史分位」 | 改为 `市盈率（PE-TTM）`/`市净率（PB-MRQ）`（这条是**守卫补强后立刻抓出来的**） |
| 5 | P2 | 导出提示「导出按当前顺序」不准 —— 导出实际读服务端 `result_json` 顺序 | 改为「导出按服务器保存的顺序」 |
| 6 | P2 | 参数值未本地化：国债期限下拉显示 `0p25y` | 显示 `0.25 年`；CAGR 显示 `3 年`（条件行 + 结果列两处） |
| 7 | P2 | 死代码：结果面板 `sort?:` prop、`screening-quality.sortField` | 删除 |
| 8 | P2 | `DslIndicatorManager` 主标题对未登记中文名的复合指标仍是纯英文 | 加「复合指标」前缀兜底 |

### 12.3 变异测试（证明守卫现在真有牙齿）

| 变异 | 旧守卫 | 新守卫 |
|---|---|---|
| 删 `pe_ttm` 中文名 | ❌ 1 failed（有牙齿） | ✅ 1 failed |
| 删 `tobin_q`（扩展域） | ✅ **5 passed 盲区** | ❌ 1 failed |
| 删 `STAT_METRIC_LABELS.spread_10y` | ✅ **5 passed 盲区** | ❌ 1 failed |

每次变异后均已 `git checkout` 还原，工作区干净。

### 12.4 门禁

- `test_indicator_label_coverage.py` **5 passed**（补强后仍全绿）
- 后端 4 个回归文件 **66 passed**
- 前端 lint + build + **70 vitest** 全绿
- 全量枚举（6 个 append 站点）缺中文名 **0 个**
