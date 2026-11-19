---
title: 界面英文字段名与「不可用指标」清不空的根因（reports/148）
status: approved
category: reports
created: 2026-11-16
last-reviewed: 2026-11-16
---

# 两个"看起来像同一件事"的问题，其实各有各的根因

> 用户截图两次追问：
> ① 「不可用指标（7）」里折旧摊销/EBITDA 一堆「覆盖率 0.1%」，为什么还在？
> ② 条件行「观察指标」显示 `roce_10y_median_i…`（纯英文），怎么回事？

---

## 1. 「不可用指标（7）」：根因是**我的修复没合并进你看的那份代码**

### 1.1 事实

- `reports/145` 的覆盖率修复提交在分支 `fix-quarterly-coverage-fallback`；
- 用户运行的仪表盘跑的是**主检出**（分支 `fix/audit-remediation`）；
- 主检出里 `grep _EXTENDED_COVERAGE_LOOKBACK` → **0 命中**，即还是旧的"只看最新一期"逻辑。

**所以界面上那 7 个字段，是旧代码算出来的**——不是数据还在坏。

### 1.2 合并后的实测（正式库）

| | 合并前 | 合并后 |
|---|---|---|
| 不可用指标数 | **7** | **1** |
| `ebitda` | 覆盖率 0.09%（隐藏） | **99.73%**，基准 2026 半年报 |
| `depreciation_amortization` | 隐藏 | **99.73%** |
| `ebitda_to_liabilities` | 隐藏 | **99.73%** |
| `ev_ebitda` | 隐藏 | **84.72%** |
| `leverage_operating` | 隐藏 | **77.16%** |
| `leverage_total` | 隐藏 | **74.37%** |
| `dividend_yield` | 「覆盖率未知」 | 「**已改名为 股息率（TTM）**」 |

### 1.3 剩下的那 1 个不是"不可用"，是"改名"

`dividend_yield → ttm_dividend_yield` 是**同一指标换了列名**（小数 vs 百分数存储）。
把它算进「当前报告期不可用指标」导致这个列表**永远清不空**，语义也错了。

修法：前端把它拆出来单独一行提示：

```
已改名（同一指标换了列名，不影响使用）：股息率 → 股息率（TTM）
```

「当前报告期不可用指标」现在只在**真的缺数据/被遮蔽**时才出现。

---

## 2. 条件行显示英文：naive-ui 的兜底选项绕过了中文解析

### 2.1 三个 Agent 并行查证，根因是一条链

| 环节 | 证据 |
|---|---|
| ① 方案 C 之后 `/indicators` 不再下发 930 个 `*_rank` 扁平字段 | `app/web/api/screening.py`（rank 字段泄漏断言见 `test_screening_api_regressions.py`） |
| ② 库里**草稿**仍存着 `roce_10y_median_industry_rank` | `screening_drafts` id=1 revision=90，`conditions.rules[1]` |
| ③ naive-ui `n-select` 在 value 不在 options 里时**合成兜底选项** `label = String(value)` | `naive-ui/es/select/src/Select.mjs` `wrappedFallbackOptionRef`：`value => ({ [labelField]: String(value), [valueField]: value })` |
| ④ 条件行 `render-label` 只做 `NEllipsis` 省略号截断，**不调 `fieldDisplayName()`** | `RuleConditionRow.vue` `renderIndicatorLabel` → `option.label ?? ''` |
| ⑤ 于是渲染成英文原值，被截成 `roce_10y_median_i…` | vitest 挂载实测文本：`01观察指标roce_10y_median_industry_rank判断关系低于…` |

**关键结论**：`fieldDisplayName('roce_10y_median_industry_rank')` 的输出是正确的
`已动用资本回报率（ROCE·10年中位） · 证监会一级排名` —— **不是词表缺失，是渲染点没用它**。

### 2.2 为什么能存活这么久（数据链路）

- `POST /screening/draft` **不校验字段**（只有 `POST /rules/save` 校验）；
- 前端 `restoreDraft()` / `applyLoadedRule()` 直接 `Object.assign(ruleTree, …)`，**无迁移**；
- 后端 `validate_rule_fields` 对旧排名字段是**放行**的（`_rank_base_for` 能拆出基名）→ 规则能跑，不报错，所以没人发现。

### 2.3 修法（4 处）

| # | 位置 | 改法 |
|---|---|---|
| 1 | `RuleConditionRow.vue` 观察指标 / 比较指标 | 加 `:fallback-option="fallbackIndicatorOption"`，标签走 `fieldDisplayName()` |
| 2 | `ScreeningPage.vue` 结果列多选 | 加 `:fallback-option="fallbackResultColumnOption"` |
| 3 | `ScreeningPage.vue` 载入规则/草稿 | 新增 `migrateLegacyRankFields()`：`<base>_<mode>` 且 base 在清单、mode 在当前 `rank_modes` 里 → 改写成 `{field: base, rank_mode: mode}`（后端 `expand_rank_modes` 展开回同一字段名，语义等价） |
| 4 | `screening-format.ts` | 补 `sw1_rank` / `sw1_percentile` 两条后缀标签（后端 `RANK_SUFFIXES` 有 8 条，前端只有 6 条） |

**数据侧**：备份 SQLite 后把那条草稿迁移成 `{field: roce_10y_median, rank_mode: industry_rank}`。
`sw1_*` 已不在 `RANK_MODES` 里，无法迁移成 `rank_mode`，靠第 4 条标签兜底显示中文。

---

## 3. 全站出口审计（第三个 Agent，37 个出口 / 536 条矩阵）

修完上面 4 处之后，又按「所有会把字段名变成界面文字的地方」清单补了 5 处：

| 优先级 | 位置 | 问题 | 改法 |
|---|---|---|---|
| **P0** | CSV 导出表头（`screening.py` `_csv_export_header`） | **67/67 列全是英文原始键** | 请求体新增 `column_labels`，前端用 `fieldDisplayName` 生成映射；后端缺标签时保留原列名（绝不静默丢列） |
| **P0** | 数据状态页「缺失字段」（`DataStatusPage.vue:753`） | 直接渲染 `field_name` | 加 `render` 走 `fieldDisplayName` |
| **P0** | DSL 管理器第二行（`DslIndicatorManager.vue:95`） | 裸渲染 `row.name` | 改为 `标识 xxx` 并加等宽样式，避免被误当成名称 |
| P1 | 结果表头（`ScreeningResultsPanel.vue:193`） | `?.label \|\| key` 回退英文 | 回退改为 `fieldTitleWithUnit(key, fieldDisplayName(key, key))` |
| P1 | 数据质量告警「受影响字段」 | `untrustedFields.join(', ')` 直接吐英文 | 改为 `map(fieldDisplayName).join('、')` |
| P1 | DSL 预览「依赖指标」 | 直接渲染字段名 | 走 `displayNameOf(dep)` |
| P1 | 不可用指标 tooltip 的「替代字段」 | 直接显示 `ttm_dividend_yield` | 走 `fieldDisplayName` |
| P1 | 结果列多选 | 无 fallback | 同 §2.3 第 2 条 |

**Agent 实测确认已经正确的地方**（没有重复造轮子）：
`fieldDisplayName('pe_ttm_stat_5y_percentile')` → `市盈率（PE-TTM） · 5年 · 历史分位`；
`('balance.total_assets')` → `资产负债表 · 资产总计`；
`('dividend_financing_ratio')` → `分红融资比（A股）`；
`('revenue_cagr')` → `营收复合增速`；官方 208 个真实库字段全部中文。

---

## 4. 顺带：让 auto-update 也长"眼睛"

排查期间撞见一个 `python -m app.cli.main data auto-update run` 正在写库（PID 529492），
而它**不写租约** → 别的会话 preflight 显示 PASS、看不到有人在写。

改法：`exclusive_update()` 拿到单写者锁后**顺带写一份跨会话租约**
（`owner=auto-update`、TTL 2 小时、带 pid/hostname/command），退出时清掉。
现在 `session_lease.py status` 与 `session_preflight.py` 都能看见自动更新在跑。

---

## 5. 门禁

| 门禁 | 结果 |
|---|---|
| 前端单元测试（`node --test`） | **93 passed**（新增 `field-label-fallback.test.ts` 6 条） |
| 前端组件测试（vitest） | **70 passed**（导出断言更新为「带 result_id + 全中文标签」） |
| 前端 lint / build | 全绿 |
| `ruff check` | 全绿 |
| 正式库 API 复验 | 不可用指标 **7 → 1**（且那 1 条前端不再计入不可用） |

### 5.1 新增测试钉死的契约

- 8 种排名后缀（含曾漏掉的 `sw1_rank` / `sw1_percentile`）**全部**能解析出中文；
- `roce_10y_median_industry_rank` 的**精确输出**是
  `已动用资本回报率（ROCE·10年中位） · 证监会一级排名`；
- 未知字段仍回退原值（由调用方决定是否提示，不在词表层崩溃）；
- CSV 导出请求必须带 `column_labels`，且每个标签都含中文。

## 6. 教训

1. **修复不合进用户运行的分支 = 没修。** 用户看到的是主检出，我在工作树里改完就汇报"已修复"是错的；必须先合并再验证一遍真实 API。
2. **"看起来是同一个问题"要分开查。** 这次两条投诉各自有独立根因（未合并 / 渲染点绕过解析），合并成一个"文案问题"去修就会漏。
3. **第三方组件的兜底行为要读源码确认。** naive-ui 的 `fallbackOption` 决定了 `render-label` 拿不到真实字段名 —— 不读 `Select.mjs` 只能靠猜。
