---
title: 流通股本补全后的数据复审（2026-07-31）
status: superseded
category: reports
last-reviewed: 2026-07-31
superseded-by: reports/27_COMPREHENSIVE_RED_TEAM_REAUDIT_2026-07-31.md
---

# 流通股本补全后的数据复审（2026-07-31）

## 裁决

**BLOCK。流通股本补全未通过数据验收，且正式研究/筛选数据仍不可发布。**

本轮只复审当前正式数据及其股本链路，不重复评价前端、DSL、备份等非数据问题。结论不以“非空覆盖率”代替真实性：当前 `share_capital` 缺失从 5,202 只降至 27 只，属于显著覆盖改善；但 1,215 只上交所股票出现 `circ_shares > total_shares` 的不可能关系，最大为 16,459.42 倍。该错误足以使流通市值及任何依赖流通股本的研究结论失真。

## 审查范围与方法

- 审查对象：当前 `data/valuedashboard.duckdb` 和 `data/valuedashboard.sqlite`，以及股本补全脚本和消费代码。
- 数据库访问：通过 DuckDB `read_only=True` 与 SQLite `mode=ro` 执行只读核验；未运行 schema 初始化、回填、重算、修复或写数据库的命令。
- 证据快照：`docs/evidence-formal-share-capital-reaudit-20260731.json`。
- 股本一致性聚合：`docs/evidence-data-reaudit-share-capital-20260731.json`。
- 证据捕获时间：`2026-07-30T16:04:57.973156+00:00`。
- 正式库 SHA-256：DuckDB `21ce1cd890428e15714d4698a51653cc9c0e98e2a3faf09bd120db15c2de4c70`；SQLite `283f7a8f3190ae6e8b6438ad637c7ba7f211a3f1c8fcdc41d3559417face0797`。
- 对比基线：`docs/evidence-formal-baseline-20260730.json`。

## 已确认改善

| 项目 | 基线 | 当前 | 结论 |
|---|---:|---:|---|
| 上市股票股本缺失 | 5,202 | 27 | 覆盖率明显改善，但未完成 |
| 未验证分红公告日 | 53,503 | 0 | 已解除该项数据告警 |
| 公司行为/分红链路缺失 | 5,534 | 538 | 大幅改善，但仍不完整 |
| 未发布人工覆写 | 8 | 0 | 已清除 |
| XDXR 行数 | 0 | 183,231 | 已补入公司行为数据 |
| 原始响应 archive 行数 | 8 | 5,082 | 数量增加，不等于字段级来源闭环 |

当前质量状态仍为 `LINEAGE_INVALID` 和 `MINIMUM_DATA_NOT_READY`，证据见 `docs/evidence-formal-share-capital-reaudit-20260731.json:303-308`。

## P0：流通股本与总股本存在系统性不可能关系

### 证据

对 5,534 只上市股票进行 `circ_shares / total_shares` 核验：

| 交易所 | 上市数 | 总股本为正 | 流通股本为正 | 流通股本大于总股本 |
|---|---:|---:|---:|---:|
| BSE | 332 | 332 | 332 | 0 |
| SSE | 2,310 | 2,283 | 2,310 | **1,215** |
| SZSE | 2,892 | 2,892 | 2,892 | 0 |

上交所的 2,283 个可比较记录中，仅 1,068 个满足 `circ_shares <= total_shares`，1,215 个不满足。异常分布并非舍入误差：13 个超过 10.5 倍，49 个在 5.5 至 10.5 倍，最大比率为 16,459.42。

示例：

| 代码 | 总股本 | 流通股本 | 流通/总股本 |
|---|---:|---:|---:|
| 688428 | 23,494 | 386,697,602 | 16,459.42x |
| 689009 | 48,432 | 227,272,727 | 4,692.61x |
| 688728 | 200,434 | 683,060,109 | 3,407.91x |

### 根因判断

`scripts/repair_sse_share_capital.py:48-75` 直接把资产负债表 `paid_in_capital` 写入 `stock_meta.total_shares`。上述样本中 `paid_in_capital` 分别为 `23,494.45`、`48,431.83`、`200,434`，明显是以“万股”呈现的值；而 schema 明确要求 `total_shares` 和 `circ_shares` 的单位为“股”（`app/core/storage/schema.py:37-38`）。

同时，`scripts/repair_sse_circ_shares.py:39-49,79-83` 将 Eastmoney `f85` 直接写入流通股本。该字段与异常数量级一致为“股”。两条链路混合了“万股”和“股”，导致总股本约缩小 10,000 倍而流通股本保留为股。

`scripts/repair_sse_circ_from_tencent.py:57-59` 的市值反推路径同样会写入股单位；它不能修正已经被错误单位写入的总股本。`scripts/repair_sse_share_capital.py:9-11,72-76` 中以非上交所平均流通比例派生流通股本的做法更不应作为正式数据来源，因为个股流通比例不是常数。

### 影响

- `app/core/indicators/calculator.py:484-494` 用价格直接乘总/流通股本计算总市值和流通市值。异常记录会让总市值严重低估，流通市值可大于总市值。
- `app/core/data_quality.py:168-169` 只要求两个字段为正数，不校验 `circ_shares <= total_shares`，因此 1,215 条不可能记录被误计为“股本已就绪”。
- 依赖市值、流通市值或流通股本的筛选与展示没有可靠性保证。

## P1：补全后的股本未被来源链路审计，也没有触发快照重算

### 来源不可追溯

- 当前 `source_audit` 中没有 `field_name IN ('total_shares', 'circ_shares')` 的记录。
- `fetch_batch` 中只有 1 个 `listing_info` 批次，共声明 5,534 行；没有专门对应本次 SSE/Eastmoney/Tencent 修复的批次。
- 基线与当前 `fetch_batch` 数均为 1,465，`source_audit` 数均为 2,813,753，说明本次股本字段写入没有建立同代 batch/field audit。
- 当前仍有 2,535,176 条 archive gap，其中 2,535,043 条 archive payload 为空，导致 5,534 只上市股票全部缺字段级 lineage coverage。

因此，不能对任一补入的股本值给出供应商响应、抓取时间、字段映射和 hash 可复验的证据。

### 快照未重算

股本更改后没有重新发布 `indicator_snapshot`：

- 2,283 只 SSE、2,892 只 SZSE 已有正的总/流通股本，但对应快照的 `total_market_cap` 与 `circ_market_cap` 均为 NULL。
- 前一份正式基线在 `2026-07-30T08:28:32+00:00` 仍有 5,202 个股本缺口，而当前快照的 `calculated_at` 全部在 `2026-07-29`；因此这些快照必然早于本轮股本写入，不能代表修复后的股本输入。
- BSE 332 只快照中市值字段已存在且与存量股本一致，但该结果不能外推到 SSE/SZSE。

这意味着，即使股本数据本身正确，当前物化指标也仍不是股本修复后的版本。

## 剩余正式数据阻断项

股本之外，当前正式数据仍有独立 BLOCK 条件：

| 门禁项 | 当前受影响数 |
|---|---:|
| 字段级 lineage coverage | 5,534 |
| lineage archive gap | 2,535,176 |
| 价格新鲜度 | 5,532 |
| raw 历史不足 | 1,309 |
| QFQ 历史不足 | 1,356 |
| 有效成交量不足 | 1,313 |
| 公司行为/分红链路不足 | 538 |
| 银行/券商监管字段不足 | 92 |
| 股本仍缺失 | 27 |
| 快照价格/输入不一致 | 7 / 7 |

上述计数来自同一哈希绑定的证据快照。即使修正全部 1,215 条异常股本记录，`LINEAGE_INVALID`、价格与行业字段缺口仍会独立阻断正式研究和筛选。

## 复验通过条件

1. 回滚或隔离所有未能证明为“股”单位的 SSE `total_shares`/`circ_shares` 写入，禁止以平均流通比例填充正式字段。
2. 使用单一权威来源按股票记录总股本、流通股本、单位、as-of 日期和原始响应 hash；在同一 DuckDB 事务中写入数据、`fetch_batch`、`raw_response_archive` 和 `source_audit`。
3. 在写入前强制 `total_shares > 0`、`circ_shares > 0`、`circ_shares <= total_shares`，并对异常单位进行拒绝而非自动换算猜测。
4. 为股本引入生效日期或 as-of 日期。当前 `stock_meta` 只有单个静态值，无法表达解禁、增发、回购后的历史流通股本变化；若指标声明“当前口径”，需明确该限制并记录抓取时点。
5. 修复后全量重算并原子发布 `indicator_snapshot`，验证总/流通市值等于 `latest_close * shares`，且快照时间晚于股本批次。
6. 将 `circ_shares > total_shares` 纳入 `minimum_data_readiness()` 的 fail-closed 条件并添加回归测试。
7. 单独完成 lineage、价格历史/新鲜度、公司行为和金融行业字段的正式数据重建与核验。

在这些条件满足且生成新的正式哈希证据前，本轮结论保持 **BLOCK**。
