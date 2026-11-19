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
