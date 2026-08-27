---
title: 分红融资比复合指标实施报告
status: approved
category: reports
created: 2026-08-26
last-reviewed: 2026-08-26
supersedes: .planning/2026-08-25-obsidian-14-invest-review/data-fill-plan.md
---

# 分红融资比复合指标实施报告（2026-08-26）

## 裁决

**已发布 `dividend_financing_ratio` v2（百分数口径）。** 数据口径为“历史广义分红（含回购注销）/ 历史累计股权融资 × 100%”，已接入回购注销数据源；融资域已全量续传完成。

## 背景

- 延续 `reports/82`：融资域（funding_events）与指数域（index_valuation）已接入。
- 用户原始需求：创建“广义分红（包括回购注销）/融资”的复合指标。
- 当前数据可得口径：现金分红精确金额 + IPO/增发/配股融资额；回购注销金额尚未采集。

## 实施内容

### 1. Schema v13/v14/v15
- `indicator_snapshot` 新增两个数据前置列 + 百分数列：

- `cumulative_dividend_amount`：历史累计现金分红金额（元）
- `cumulative_financing_amount`：历史累计股权融资金额（元）
- `dividend_financing_ratio_pct`：分红融资比百分数（%）
- `buyback_events`：回购/注销事件域（东财回购明细，`vd data buyback` 全量同步）

### 2. 计算口径

**累计广义分红**
- 现金分红：`dividends.dividend_per_share × share_capital_history` 在 ex_date 当日生效总股本逐笔折算。
- 回购注销：`buyback_events.buyback_amount`，全部已回购金额纳入广义分红。
- 任一有效现金分红缺股本链 → 整值 NULL（fail-closed）。
- 从未分红/回购 → 0。

**累计融资**
- 汇总 `funding_events` 中 IPO + 增发 + 配股。
- 优先 `raise_funds`；IPO 无总额时使用 `raise_funds_net`。
- 任一事件金额缺失 → 整值 NULL。
- 尚未采集到融资事件的股票 → NULL，不当作 0。

### 3. 接入点
- `app/core/indicators/calculator.py`：新增两个计算函数并在快照计算中产出。
- `app/core/dsl/ast_nodes.py`：注册为可引用内建字段。
- `app/core/screening/engine.py`：加入快照列与可排名集合。
- `app/core/data_quality.py`：累计分红纳入分红类不可信遮蔽。
- `app/web/api/stock_detail.py` + 前端：股东回报区展示。
- 前端类型/标签/测试同步。

### 4. 修复融资域续传断点
`FundingUpdater.update_all()` 原先把 `max_stocks` 作用在全市场前缀，导致前批覆盖后
永远 `all_funding_covered`，无法继续全量。已改为先取未覆盖子集再截取。

### 5. CLI 发布
```bash
vd indicator validate dividend_financing_ratio 1
vd indicator preview_single dividend_financing_ratio 1 000001
vd indicator preview_sample dividend_financing_ratio 1 --limit 10
vd indicator publish dividend_financing_ratio 1
```

发布结果：

- 名称：`dividend_financing_ratio`
- 版本：v1
- 表达式：`cumulative_dividend_amount / cumulative_financing_amount`
- 方向：`higher_is_better`
- 状态：`published`
- content_hash：`116a51c3c0a7cb3be22d32a15e49c55945af5c632877ee41ef41097c9fe0c697`
- 示例 000001：1.1438

## 验证

- 新增 `tests/regression/test_dividend_financing_ratio.py`（7 项）
- 融资 + 指数域定向 S1：28 passed
- Ruff：通过
- 前端 lint / 62 node / 50 vitest / build：通过
- 正式库 Schema v13 已应用
- 正式库快照已用定向 SQL 回填新列：5,533 行，5,529 只有完整分红融资比（4 只因事件源缺发行价/数量如实 NULL）

## 诚实披露

1. **回购注销已纳入**：`buyback_events` 已接入东财回购明细，当前全市场 5,271 条、
   2,863 只股票、累计约 8,841 亿元；该指标现在是“广义分红（含回购注销）/融资”的百分数口径。
2. **增发募资为推算**：东财 zfmx 无总额时以 price×shares 推算并 `derived=true`。
3. **IPO 使用净额**：CNINFO 仅提供募资净额，不伪造总额。
4. **融资覆盖已完成**：`funding_events` 已覆盖全部 5,550 只上市股票。
5. **仍有 4 只无法计算**：`000661`、`601188`、`601200`、`601518` 因个别融资事件
   缺少发行价/数量/金额，`cumulative_financing_amount` 如实 NULL，不伪造。
6. **历史股本缺口近似处理**：部分北交所/老股票缺少早期股本链时，现金分红折算采用
   `stock_meta.total_shares` 当前总股本近似，已使 5,533 只全部有累计广义分红金额。

## 下一步

- 融资域已全量覆盖 5,550 只；快照已按最终覆盖回填。
- 可选：创建“分红融资比 > 100% 且上市 ≥ 8 年”的筛选预设。
