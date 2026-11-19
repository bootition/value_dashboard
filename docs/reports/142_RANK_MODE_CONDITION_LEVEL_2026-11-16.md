---
title: 排名口径改为条件级下拉（方案 C）——筛选项 1126 → 196
status: approved
category: reports
created: 2026-11-16
last-reviewed: 2026-11-16
---

# 排名口径改造（方案 C）

> 用户裁决 2026-11-16：「C」（彻底不铺字段，改成条件行上的下拉）。
>
> 起因：用户在下拉里搜 `ROCE` 命中 **21 条**，主指标被埋住无法选择；
> 且质疑「我有说要做全市场排名、证监会那些东西吗？是不是没用的功能？」

---

## 1. 先回答"这是不是我擅自加的"

**不是无中生有，也不是我引入的。**

| 问题 | 答案 | 证据 |
|---|---|---|
| PRD 要求了吗？ | **要求了** | §11.2「DSL 必须支持…当前基础股票池内的全市场排名、CSRC 一级与二级排名、百分位、Z-score、归一化」；§12.2「条件中可混用…全市场排名与 CSRC 一级或二级排名」；§12.3「切换后市场排名与行业排名的分母也必须同步变化」；§12.4「缺少 CSRC 归属时行业排名返回 null」 |
| 是我擅自引入的吗？ | **不是** | `git log -S "market_rank"` 显示该机制在 **2026-08-28 基线快照 `40ba9ff`** 里就已存在 |
| 是没用的功能吗？ | **功能有效，但实现冗余** | 实测 `pe_ttm_sw2_rank <= 3` 能正确返回每个证监会二级行业 PE 最低的股票（269 只）；`is_null` 也符合 §12.4 |

## 2. 但用户的架构批评成立：它是**重复实现**

PRD 把排名定位成 **DSL 运算**，而 DSL 早已实现：

```python
# app/core/dsl/parser.py:154  +  codegen.py:44
"rank_industry": "RANK() OVER (PARTITION BY csrc_l1 ORDER BY {arg})"
"percentile":    "PERCENT_RANK() OVER (ORDER BY {arg})"
```

`rank()` / `rank_industry()` / `percentile()` / `zscore()` / `normalize()` 五个函数都在。

再为**每个可排名指标**额外铺 6 条扁平字段，就是同一功能的第二套实现，并且造成组合爆炸：

| 类别 | 改造前 |
|---|---|
| 主指标 | 156 |
| **排名/分位派生** | **930**（155 个可排名指标 × 6） |
| 时间序列统计 | 40 |
| **合计** | **1126 条** |

ROCE 一个指标就占 21 条（3 窗口 × 6 + 3 主指标）——用户搜 "roce" 命中 21 条，
**主指标被埋在下拉中间根本选不到**。

## 3. 顺带查出的真 bug：分位单位错

`*_market_percentile` 存的是 **0–1 小数**（0.8692 = 86.9 分位），但单位声明为 `plain`：

| 用户输入 | 结果 |
|---|---|
| `>= 0.9` | 命中 399 只 ✅ |
| `>= 90` | **命中 0 只** ❌ 静默为空，不报错 |

按常识输入 90（想找前 10%）得到 0 只。改造后该字段不再暴露，
由 `rank_mode=market_percentile` 表达，单位随口径固定为 ratio。

## 4. 改造方案

### 4.1 条件级 `rank_mode`

条件 JSON 新增可选字段：

```json
{"field": "pe_ttm", "op": "<=", "value": 3, "rank_mode": "sw2_rank"}
```

后端在**入口**展开成 `{"field": "pe_ttm_sw2_rank", ...}`，
之后全流程沿用既有排名 SQL —— **不写第二套排名实现**，
`NULLS LAST`、`csrc_l1` 为 NULL 时返回 null（PRD §12.4）、
排名分母只由基础池决定（PRD §12.3）这些语义一条不动。

六个口径（`RANK_MODES`）：

| 值 | 中文（PRD §12.1 要求全中文） |
|---|---|
| `market_rank` | 全市场排名 |
| `market_percentile` | 全市场分位 |
| `industry_rank` | 证监会一级排名 |
| `industry_percentile` | 证监会一级分位 |
| `sw2_rank` | 证监会二级排名 |
| `sw2_percentile` | 证监会二级分位 |

**向后兼容**：仍接受旧的字段名写法（`pe_ttm_market_rank`），已保存的规则继续可跑。

### 4.2 效果

| 项 | 改造前 | 改造后 |
|---|---|---|
| 下拉条目 | 1126 | **196** |
| 搜 "roce" | 21 条 | **3 条** |
| 排名能力 | 930 个字段 | 6 个口径（条件行下拉） |

### 4.3 关于"为什么不直接在筛选里限制行业"

用户问得有道理，但两者不同：

| | 含义 | 现有能力 |
|---|---|---|
| **行业筛选** | 只看某行业 | ✅ 已有 `csrc_l1` / `csrc_l2` / `sw_level1` / `sw_level2` 元数据字段 |
| **行业排名** | 在**本行业内**排第几 | 需要同行业其他公司做分母 |

大多数场景「行业筛选 + 指标阈值」确实够用（"食品饮料里 ROE > 20%"比"ROE 行业内前 10%"更直观）。
行业排名只在「每个行业各挑最好的几只」这类场景有独特价值，因此保留但收进下拉。

## 5. 变更清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | `app/core/screening/engine.py` | 新增 `RANK_MODES` / `RANK_MODE_LABELS` / `expand_rank_modes()`；`run()` 与 `validate_rule_fields()` 入口均归一化 |
| 2 | `app/web/api/screening.py` | 删除 930 条排名派生字段生成块；响应体新增 `rank_modes` |
| 3 | `frontend/src/types/screening.ts` | 新增 `RankMode` 类型；条件加 `rank_mode?` |
| 4 | `frontend/src/components/RuleConditionRow.vue` | 条件行新增「排名口径」下拉（仅 rankable 字段显示），首项「原始值」 |
| 5 | `frontend/src/components/ScreeningRuleEditor.vue` | 透传 `rankModes` / `rankableOf`；换字段时清理失效的 `rank_mode` |
| 6 | `frontend/src/views/ScreeningPage.vue` | 接入后端 `rank_modes`；移除上一轮的「显示派生字段」折叠开关（930 条已不存在） |
| 7 | `tests/regression/test_screening_rank_mode.py`（新） | **10 用例** |
| 8 | `tests/regression/test_screening_api_regressions.py` | 旧断言（必须存在 `_sw2_rank` 字段）改为：**不得下发任何排名字段** + 六个口径必须在 `rank_modes` 里 |

### 5.1 测试覆盖

| 用例 | 钉死的契约 |
|---|---|
| `test_expand_rank_mode_rewrites_field` | 展开成字段后缀且移除 `rank_mode`（防重复展开） |
| `test_expand_keeps_conditions_without_rank_mode` | 无条件原样保留 |
| `test_expand_handles_nested_groups` | 嵌套组也展开 |
| `test_every_rank_mode_expands_to_a_known_suffix` | 6 口径 ↔ `RANK_SUFFIXES` 一一对应，且都有中文名 |
| `test_unknown_rank_mode_is_rejected` | 非法口径报错，不静默降级 |
| `test_double_suffix_is_rejected` | 已带后缀又指定 `rank_mode` → 报错 |
| `test_validate_rule_fields_accepts_rank_mode` | 保存期接受（防"能跑不能存"） |
| `test_validate_rule_fields_rejects_unknown_rank_mode` | 保存期同样拒绝 |
| **`test_rank_mode_matches_legacy_suffix_condition`** | **新旧写法逐只一致**（方案 C 可安全替代的前提） |
| `test_indicators_endpoint_stops_emitting_rank_fields` | 端点不再铺排名字段，且下发 `rank_modes` |

## 6. 施工中发现并修掉的自己的 bug

`expand_rank_modes()` 最初只递归 `rules` 键，导致 `validate_rule_fields()`
（入参是 `{"conditions": {...}}`）里的 `rank_mode` **逃过展开与校验**：
非法口径不报错、合法口径只是碰巧通过。已改为递归**所有** dict/list 值。

这个 bug 若不修，会重现本项目的老问题——「能跑不能存 / 能存不能跑」（CB-09）。

## 7. 门禁

- `test_screening_rank_mode.py` **10 passed**
- 定向回归 8 个文件 **93 passed**
- 前端 lint + build 全绿
- Ruff 全绿

⚠️ 一个与本改动**无关**的失败：`test_stock_detail_indicators_expose_annual_yoy_caliber`
返回 404。经核实是**并行会话正在重构** `app/web/api/stock_detail.py`
（`router` → `secure_router`，16 处），非本次改动引入，未触碰该文件。

## 8. 遗留

- `*_stat_*`（时间序列统计域，40 条）仍以扁平字段形式存在。它对应 PRD §10.7
  的独立研究统计域，有版本与样本元数据要求，与横截面排名不是一回事，本次未动。
- 建议后续把「排名口径」也支持到**排序**与**结果列**（目前仅条件支持），
  否则用户仍无法按排名排序。

---

## 9. 补齐排序与结果列（2026-11-16 追加）

用户看到 §8「遗留」后要求「全部都改了」。实测后发现**后端三项其实都支持**，
缺的只是前端入口——因为上一轮把 930 个排名字段从下拉里删掉后，
**排序**和**结果列**这两个下拉复用的是同一个指标列表，于是也失去了排名选项。

| 能力 | 后端（改造前） | 前端入口（改造前） | 改造后 |
|---|---|---|---|
| ① 条件用排名 | ✅ | ✅ | ✅ |
| ② 排序用排名 | ✅（`expand_rank_modes` 递归到了 `sort`） | ❌ 选不出 | ✅ 排序行新增「排名口径」下拉 |
| ③ 结果列显示排名 | ✅（旧字段名可作列） | ❌ 选不出 | ✅ 结果列支持 `{field, rank_mode}` |

### 9.1 后端

```python
def normalize_columns(columns_spec) -> list[str]:
    """["stock_code", {"field": "pe_ttm", "rank_mode": "market_rank"}]
       → ["stock_code", "pe_ttm_market_rank"]"""
```

- `run()` 与 `validate_rule_fields()` 都走它（防「能跑不能存」）
- `ScreeningRequest.columns` 类型放宽为 `list[str | dict[str, Any]]`
- 校验顺序：**先查 `field` 再展开**，否则 `{"rank_mode": ...}` 会先报
  「rank_mode 只能用在带 field 的条件上」，对结果列来说语义不清

### 9.2 前端

- **排序行**：字段旁边新增「排名口径」下拉（仅 `rankable` 字段显示，首项「原始值」）
- **结果列**：为每个可排名指标追加 6 个「`基础指标` · `口径`」选项；
  用合成取值 `字段::rank::口径` 编码（多选下拉里同名字段无法区分），
  提交时 `decodeResultColumn()` 还原成 `{field, rank_mode}`，
  载入草稿/已保存规则时 `encodeResultColumn()` 反向编码

### 9.3 端到端验收（正式库实测）

```json
{"conditions": {"logic": "AND", "rules": [
   {"field": "pe_ttm", "op": ">", "value": 0, "rank_mode": "market_rank"},
   {"field": "pe_ttm", "op": "<=", "value": 500}]},
 "sort": [{"field": "pe_ttm", "rank_mode": "market_rank", "direction": "asc"}],
 "columns": ["stock_code", "pe_ttm",
             {"field": "pe_ttm", "rank_mode": "market_rank"},
             {"field": "pe_ttm", "rank_mode": "market_percentile"}]}
```

```
命中 3565
000656  PE=0.25  全市场排名=1  分位=0.0000
600841  PE=2.00  全市场排名=2  分位=0.0002
002582  PE=2.12  全市场排名=3  分位=0.0004
603726  PE=3.33  全市场排名=4  分位=0.0006
```

三项联用正确：条件筛出有 PE 的、按排名升序、结果里同时显示排名和分位。

### 9.4 测试

`test_screening_rank_mode.py` 从 10 条扩到 **14 条**，新增：

| 用例 | 钉死的契约 |
|---|---|
| `test_normalize_columns_accepts_plain_and_rank_objects` | 两种列写法都能归一化 |
| `test_normalize_columns_rejects_bad_shapes` | 非数组/非字段名/缺 field 都拒绝 |
| `test_validate_rule_fields_accepts_rank_sort_and_columns` | 排序+结果列的排名口径可保存 |
| **`test_rank_mode_works_in_conditions_sort_and_columns`** | **三项联用**：排名字段名、排序顺序、排名值 1/2/3 全部正确 |

另外更新 `test_research_path_integrity.py` 里「指标列表必须暴露 `pe_ttm_industry_rank`」
的旧断言 → 改为「不得暴露排名字段 + `rank_modes` 里必须有证监会一/二级排名且标签不含"申万"」，
保留了 P1-D 的原始意图。

### 9.5 门禁

- `test_screening_rank_mode.py` **14 passed**（新增 4 条）
- 11 个相关回归文件 **192 passed**
- 前端 lint + build 全绿、screening 组件测试 7 passed
- 唯一失败 `test_stock_detail_indicators_expose_annual_yoy_caliber`（404）来自
  并行会话对 `stock_detail.py` 的 `router → secure_router` 重构，与本改动无关
