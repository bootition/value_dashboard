---
status: approved
category: report
last-reviewed: 2026-11-19
---

# 151 · 指标重算只算变化的部分（增量集合 + 全量判据 + 结果等价实测）

> 用户原话：「指标重算只算增量和变化的那些量……还是每一次自动更新，所有指标都要重算吗？
> 这根本不可能啊！你只需要算变的就行了嘛。」

## 1. 旧行为与根因

`app/core/update_parts/flow.py` 旧实现是一棵二选一：

```
if share_capital_changed:                     # 股本/上市名单/ST/行业任一变化
    IndicatorCalculator(...).compute_snapshot_for_all(...)   # ← 5,567 只 × 149.7 只/分 ≈ 35 分钟
elif 财务变化 or 重试 or 待重算名单:
    compute_snapshot_for_codes / compute_price_sensitive_for_codes / refresh_funding_dividend_fields
```

"按代码集"的思路其实早就在增量路径里（`bounded_codes` / `full_codes` / `price_codes` /
`event_codes`），**全量那条是唯一的例外**：判据是"整体指纹"`_share_capital_fingerprint()`
（`md5(string_agg(...))`，只有"变没变"一个比特），拿不到"谁变了"，于是只能保守全市场重算。

## 2. 输入 → 影响列 → 何时必须重算

| 输入面 | 影响的快照列 | 判据（代码来源名） | 重算组 |
|---|---|---|---|
| 三大报表：新报告期 / 修正值 / 覆盖写 | `report_date`、PE/PB/PS/PCF、ROE/ROA/毛利/净利/ROIC、YoY/CAGR、安全性 6 项、股东回报、ROCE 六列 | `financial`（本轮刷新成功）、`financial_stale`（快照期落后完整期）、`financial_backfill`、`retry`、**`financial_fingerprint`（同期改数值也算）** | full |
| 股本 `total_shares`/`circ_shares` | `total_market_cap`、`circ_market_cap`、DPS、换手率 | **`universe_fingerprint`**（逐股 md5） | full |
| 上市状态 / ST / 停牌 / CSRC 行业 `csrc_l1` | 名单成员、排名与筛选口径 | **`universe_fingerprint`** | full |
| 行情 `price_daily_raw`/`qfq` | `latest_close`/`latest_price_date`、估值价格域、MA5–250/波动率/回撤/区间收益/成交量/换手 | `price`（价格步骤写入的代码 + 快照价格日落后）、`technical_pending`（qfq 待强制） | price |
| 分红 `dividends` / 除权 `xdxr` | `dividend_yield`、连续分红年数、DPS、分红融资比 | `dividend_event`（命中"权益分派实施"等公告的代码） | full |
| 回购 / 融资事件 | `cumulative_*_amount`、`dividend_financing_ratio_pct` | `corporate_event` | event（3 列） |
| qfq 滞后待重算名单 | 技术三列 | `tech_pending` | full |
| 国债曲线 | `ttm_dividend_yield`、`div_yield_spread_*` | `refresh_treasury_spreads`（专用批量，不进本集合） | — |
| **计算口径（公式改）** | 全部 74 列 | `INDICATOR_COMPUTATION_VERSION` bump | **全量** |

## 3. 新判据：怎么保证"该算的都算了"

### 3.1 增量集合 = 所有"输入可能变了"的信号之并集

`app/core/update_parts/indicator_scope.py::plan_indicator_scope` 是唯一入口。来源与
重算组的映射写在 `SOURCE_DOMAIN`（判不准的来源默认归 `full` = 宁可多算），组内
按代码去重，组间互斥（full 吃掉 price/event 的交集）。

**股本/上市状态/行业不再是"整体指纹"，而是逐股输入指纹**：

```sql
-- 每轮一次扫 stock_meta（实测 0.011s / 6,285 行）
SELECT stock_code, substr(md5(CAST((total_shares, circ_shares, is_listed, is_st,
       is_suspended, listing_date, csrc_l1, csrc_l2) AS VARCHAR)), 1, 16)
FROM stock_meta
```

`updated_at` / `name` / `pinyin` **刻意不进指纹**（它们每轮都会被 listing_info 刷新，
进了就等于每轮全量）。

**财务也加了逐股指纹**（"最新完整报告期"三表核心列，实测 0.41s / 6,279 只）：
`_stale_financial_snapshot_codes()` 只能发现"报告期推进"，发现不了**同期修正值/覆盖写**；
指纹差异把这一类也圈进来（回归：`test_financial_fingerprint_detects_same_period_restatement`）。

### 3.2 水位持久化 → 崩溃/外部改写都不会丢信号

指纹写在 `data_refresh_state`（`indicator_universe_fingerprint:v1` /
`indicator_financial_fingerprint:v1` / `indicator_computation_version`）。
每轮做**当前值 vs 上轮水位**的集合差异，所以：

* 变化不是本流程造成的（外部脚本改库、人工覆写）→ 照样进集合；
* 上一轮算到一半崩了 → 水位没推进，本轮重新进集合；
* 只有 `step.status == "success"` 才 `commit_indicator_scope()` 推进水位，partial/failed/
  rejected 一律保留旧水位（下一轮多算，不漏算）。

### 3.3 incremental vs full：只有五个可解释原因走全量

| reason | 触发条件 |
|---|---|
| `first_run` | `indicator_snapshot` 为空 |
| `input_fingerprint_bootstrap` | 首次部署本机制，`data_refresh_state` 里没有上一轮指纹（一次） |
| `computation_version` | 存的水位版本 != `INDICATOR_COMPUTATION_VERSION` |
| `incremental_over_threshold` | **完整重算组** > 全市场 60%（且 > 5 只）：一次全扫更省 |
| `forced_full` | `owner.force_full_indicator_recompute` 显式开关 |
| `scope_planning_failed` | 指纹查询本身失败（拿不准 → 退回全量，绝不"猜着增量"） |

阈值 60% 的理由：全量是"一次 staging 批量发布"，增量是 1–3 次 `DELETE+INSERT` +
派生域保全/ROCE 重建/lineage，有**每次调用的固定开销**（实测 ~6s/次）；股票越接近
全市场，逐股维护的固定开销与 SQL 次数劣势越大。
**阈值只数"完整重算组"**：价格域组天然可能每天等于全市场（"今天全市场都成交了"），
拿它判全量等于天天退回 35 分钟（实测全市场价格域增量 0.09s/只，见 §5）。

### 3.4 可观测

`report_step("indicators", ...)` 现在固定带：
`mode`（incremental/full）、`reason`、`scope_reason`、`computation_version`、
`recomputed_count`、`skipped_count`、`changed_sources`（每个来源各贡献几只）、
`planned_groups`、`planned_count`、`recomputed_codes`（前 200）。
一只都没算也会入册 `status=skipped, reason=no_changed_stocks`。
口径版本同时写进 `indicator_snapshot.data_version = "audit-safe-v1;calc=<version>"`
与 `data_refresh_state[indicator_computation_version]`。

## 4. 结果等价（最重要）：增量 vs 全量逐行相等

`tests/regression/test_indicator_incremental_scope.py::test_incremental_snapshot_equals_full_recompute_row_by_row`

样本 **50 只**，一次跑完三类混合改动：

* 财报：4 只推进到 2026-03-31 新完整期；3 只**同期覆盖写**（revenue 999 等）；
* 股本：4 只改 total/circ + 1 只 `is_st` 翻转；
* 价格：6 只最新收盘价 ×1.05（走价格域路径）。

增量跑完后 `indicator_snapshot` 与"同一份数据 `compute_snapshot_for_all()` 重算一代"
**逐行逐列比对**（74 列中除 `calculated_at` 的 73 列，`ORDER BY stock_code, report_date`）：

```
50 行 × 73 列，mismatches = 0
（断言失败信息会打印首例行号与两侧整行内容）
```

同文件另有 10 个用例覆盖双向：不变→0 只、单股→只算它、3 只股本变→只算 3 只、
价格域→只算价格域、首轮空快照→full、版本 bump→full、超阈值→full、
`updated_at`/`name` 变更**不**触发重算、同期修正值**必须**触发重算。

## 5. 实测：35 分钟 → 多少（正式库副本，不启服务、不写正式库）

副本：`D:/vd-incr-test2`（`valuedashboard.duckdb` 28 GB + sqlite 从 `data/` 复制）。

### 5.1 改前（全量）

| 项 | 数值 |
|---|---|
| 真跑 200 只 `compute_snapshot_for_all()` | **27.3 s**（0.136 s/只、439.6 只/分） |
| 外推 5,575 只 | **760.9 s ≈ 12.7 min** |

（历史报告口径是 149.7 只/分 ≈ 35 min；副本磁盘更快、缓存更热，所以本机测得
0.136 s/只。两个数字同量级结论一致：全量是**分钟级到半小时级**，且随股票池线性增长。）

### 5.2 改后（增量，同一副本，稳态）

| 一轮的形态 | 规划 | 计算 | 合计 | 对比 12.7 min |
|---|---|---|---|---|
| 什么都没变 | 0.73 s | 0 | **0.73 s** | ×1,057 |
| 全市场价格都推进一天（200 只进价格域） | 0.66 s | 17.98 s | **18.64 s** | ×41 |
| 1 只股本变了 | 0.60 s | 6.22 s | **6.81 s** | ×112 |
| 40 只股本变了 | 0.60 s | 9.77 s | **10.38 s** | ×72 |

规划（= 每轮固定成本）拆解：逐股行业指纹 0.011 s + 财务指纹 0.41 s + 集合差异/裁决 ≈ 0.2 s。
**每轮的固定成本从"35 分钟"降到"0.7 秒"**；真正变化的股票才付计算成本。
单只增量的 6.2 s 是 `_publish_snapshot_delta` 的固定开销（DELETE+INSERT、派生域保全/
ROCE 重建、lineage），与股票数几乎无关——这也是 60% 阈值存在的理由。

### 5.3 正式库真实状态（顺带发现）

副本上 `indicator_snapshot.latest_price_date` = 2026-11-19（5,567 只），
而 `price_daily_raw` 已到 2026-11-20 → **上一轮价格更新完成后，指标步被 35 分钟全量
拖死/中断**。所以升级后第一轮会因"完整重算组 + 价格域组"被判 `incremental_over_threshold`
跑一次全量（可解释），之后每天回到上表的增量形态。

## 6. 变异验证

把 `flow.py` 里改回旧行为（`if share_capital_changed: scope_plan.mode = "full"`，
即股本一变就 `compute_snapshot_for_all`）：

```
SHA256(before)   = 557475756543bda2b1b6f7e0a16fb8ad168622c238b5f4283bd59b01e1d4750c
变异后跑 -k share_capital → 1 failed, 1 passed
  FAILED test_share_capital_change_never_calls_full_recompute
  AssertionError: 股本/上市名单变化触发了 compute_snapshot_for_all —— 又回到每轮全量 5575 只
还原后 SHA256  = 557475756543bda2b1b6f7e0a16fb8ad168622c238b5f4283bd59b01e1d4750c  ✅ 一致
```

## 7. 改动文件

| 文件 | 改动 |
|---|---|
| `app/core/update_parts/indicator_scope.py` | **新增**（471 行）：输入指纹、集合差异、`plan_indicator_scope`、`commit_indicator_scope`、`bootstrap_indicator_scope` |
| `app/core/update_parts/flow.py` | 删掉 `if share_capital_changed: compute_snapshot_for_all` 分支；全量/增量统一走 `plan_indicator_scope`；`indicators` 步骤带 mode/reason/counts |
| `app/core/indicators/calculator.py` | 新增 `INDICATOR_COMPUTATION_VERSION` + `indicator_data_version()`（每次现算，bump 必须落到 `data_version`） |
| `tests/regression/test_indicator_incremental_scope.py` | **新增** 11 个用例（双向 + 逐行等价 + 变异守卫） |
| `tests/regression/test_update_job_and_progress.py` | 3 个旧用例改成新口径（"股本变化=全量"→"只算变化代码集"；补 `_seed_scope_baseline` 基线） |

`app/core/update.py` **未改**（仍 5,179 行 ≤ 棘轮 5,226），逻辑全在 `update_parts/`。

## 8. 门禁

```
ruff check app tests/regression   → All checks passed!
python scripts/check_rework_guards.py → 10 项门禁全部通过
update.py 行数 5,179 ≤ 5,226（棘轮未动）
靶向回归：
  test_indicator_incremental_scope.py          11 passed
  test_update_job_and_progress.py              29 passed
  test_qfq_recompute_trigger.py / test_yoy_caliber_and_technical_coverage.py
  / test_update_orchestration.py               64 passed
  test_indicator_* / test_update_* / test_announcement_check.py  135 passed
```

## 9. 已知边界（没做、要下一轮做）

1. **退市股票的旧快照行不会删**：`compute_snapshot_for_all` 整表发布时它们会消失，
   增量路径只 DELETE 重算过的代码。今天增量路径本来就有这个行为（不是我引入的），
   但 `first_run`/全量与增量在这里不再等价——需要有独立的"退市清行"步骤。
2. **`refresh_treasury_spreads` 仍单独发布**：它与指标步是两次 `report_step("indicators")`
   合并，`mode/reason` 只描述指标集合本身。
3. **价格域固定开销 6 s** 值得下一轮优化（`_derived_columns_guard` 的 ROCE 重建按 scope 全量跑）。
