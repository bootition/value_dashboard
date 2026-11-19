---
title: 扩展指标「覆盖率 0.1%」根因：门禁拿错基准期（按字段回溯到最近可算期）
status: approved
category: reports
created: 2026-11-16
last-reviewed: 2026-11-16
---

# 扩展指标覆盖率门禁：从「最新期」改为「每个字段自最近的达标期」

> 用户截图：筛选页「当前报告期不可用指标（7）」里，折旧摊销、息税折旧摊销前利润
> （EBITDA）、EBITDA 对负债、企业价值倍数（EV/EBITDA）、经营杠杆、综合杠杆
> 全部标注「覆盖率 0.1%」/「覆盖率 0.0%」，并追问「是不是你给它改坏了？」

---

## 1. 结论先行：不是本次改动造成的，是门禁设计缺陷

**覆盖率函数最后修改是 `06b19e8`（reports/120），不是本会话的提交。**

根因是**基准期选错了**：

| 报表期 | 平均有几家披露折旧摊销 |
|---|---|
| 03-31（一季报） | **174 只** |
| 06-30（半年报） | 2,017 只 |
| 09-30（三季报） | **211 只** |
| 12-31（年报） | 2,140 只 |

**A 股一季报 / 三季报不强制披露现金流量表补充资料**，所以折旧摊销族在 Q1/Q3
结构性地只剩个位数有值。而 `_extended_field_coverage()` 只统计
`MAX(report_date)` 这一期：

- 最新期 = 2026-09-30（三季报）→ `ebitda` 只有 **5 / 5,575 = 0.09%**
- 门禁 `_EXTENDED_MIN_COVERAGE_PCT = 50` → 整族隐藏
- 而同一批字段在 **2026-06-30 半年报** 覆盖率是 **74%–99.7%**

也就是说：**数据没坏，字段可用，只是门禁在错误的期上量了覆盖率。**

## 2. 改法

`_extended_field_coverage()` 改为按字段回溯：

```
对每个字段，从最新期往前最多看 8 期（≈两年）：
  找到第一个覆盖率 >= 50% 的期  → 该期即「基准期」，字段放行，带 basis_report_date
  8 期内都达不到 50%            → 字段进 unavailable（这才是真没数据）
```

返回结构从 `{字段: 覆盖率}` 变成：

```json
{"ebitda": {
   "coverage_pct": 99.73,
   "basis_report_date": "2026-06-30",
   "latest_report_date": "2026-09-30",
   "basis_label": "2026 半年报"
}}
```

门禁侧：

- `basis_report_date is None` → `unavailable`（reason 不变，`message` 说明「最近 8 期均不足 50%」）
- `basis_report_date != latest` → 字段放行，并下发 `basis_report_date` / `basis_label`
- 基准期等于最新期 → **不下发**这两个字段（前端不会给所有指标都加提示）

## 3. 正式库实测（改造后）

| 字段 | 覆盖率 | 基准期 |
|---|---|---|
| `ebitda` | **99.73%** | 2026 半年报 |
| `depreciation_amortization` | **99.73%** | 2026 半年报 |
| `ebitda_to_liabilities` | **99.73%** | 2026 半年报 |
| `ev_ebitda` | **84.72%** | 2026 半年报 |
| `leverage_operating` | **77.16%** | 2026 半年报 |
| `leverage_total` | **74.37%** | 2026 半年报 |

改造前这 6 个全部被隐藏（覆盖率显示 0.09%）。

## 4. 前端：把基准期写在明处

下拉标签会变成「**息税折旧摊销前利润（EBITDA）（2026 半年报口径）**」——
用户一眼知道这个数不是最新季报的，而来自最近半年报。

类型侧新增（`ScreeningIndicator`）：

```ts
readonly basis_report_date?: string
readonly basis_label?: string
readonly latest_report_date?: string
```

## 5. 测试

新增 `tests/regression/test_extended_coverage_fallback.py`（4 条）：

| 用例 | 钉死的契约 |
|---|---|
| `test_field_falls_back_to_last_period_with_data` | 最新期空、上一期满 → **放行** + `basis_report_date=2026-06-30` + `basis_label=2026 半年报` |
| `test_field_hidden_when_no_period_has_data` | 两期都空 → **仍隐藏**（放宽不放过真没数据的字段） |
| `test_no_basis_note_when_latest_period_has_data` | 最新期有数据 → **不下发** `basis_*`（避免多余提示） |
| `test_partial_coverage_uses_threshold` | 阈值仍是 50%，改阈值要同步用例 |

**门禁**：新增 4 条 + 相关 5 个文件 = **75 passed**；`ruff` 全绿；前端 lint/build 全绿。

## 6. 顺带回答「为什么以前没有这个问题」

因为门禁只在**最新期恰好落在半年报/年报**时才正常。ext 的最新期随更新推进：
2026-06-30 时一切正常，推进到 2026-09-30（三季报）就整族翻红。
这是**周期性复发**的缺陷——每个 Q1/Q3 都会出现，之前只是没被截图撞上。

## 7. 遗留（未在本次改动）

- `indicator_ext` 里 Q1/Q3 的 `ebitda` 仍是 NULL（不做跨期填充）。本次只解决
  **门禁判定**，不改数据含义：字段在季报期确实没有值，用基准期如实标注。
- 若要真正在季报期也有 EBITDA，需要补采东财 F10 的现金流量表补充资料
  （对应 `reports/131` §9.2 P0-3「FY2025 明细两表半空壳」的同一根因）。
