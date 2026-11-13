---
title: 2026-11-13 修复后复审：效率复盘与二轮 P0 修复（ready 探针/qfq 死角/统计域全量重建）
status: approved
category: reports
created: 2026-11-13
last-reviewed: 2026-11-13
---

# 2026-11-13 修复后复审：效率复盘与二轮 P0 修复

## 1. 结论（TL;DR）

对 2026-11-12 那批修复做**上线后复审**（代码审计 + 真实生产轮效率复盘），结论分三层：

1. **功能不再出错**：今天 7 轮更新**零失败**（价格 0 失败、无假 `retry_exhausted`、无 `LINEAGE_INVALID`/`SNAPSHOT_STALE`、`ready=true`），`xdxr_full_refetch` 从 293 → **10/34**，写库停顿次数 **-87%**、总时长 **-94%**。
2. **但复审抓到 2 个新 P0**：`/api/ready` 在**整站不可用时返回 200**（探针假绿换了个入口）；按股票的除权窗口存在**"永久不再重拉 qfq"**死角（前复权历史永久错配且不自愈）。
3. **效率未达标**：无缺口轮次实测 **6.7–8.3 分钟**（基线 3–5 分钟），其中 `research_statistics` 占 **65–76%**——根因是逐股指纹里混进了**全局** `verified` 计数，导致 5,580 只每轮全量重建。已修复，预期回收约 290 s/轮。

本轮共修 9 项（2×P0、4×P1、3×P2），并**更正上一轮报告里一处失真的验收数据**（见 §5）。

## 2. 今天的真实运行数据（2026-11-13）

| 轮次 | 时段（CST） | 总时长 | 价格 | 统计域 | 结果 |
|---|---|---|---|---|---|
| R1 | 01:00:07 → 01:47:53 | 47.8 min | 1,255 只 / 945.7 s / 79.67 只每分 / **0 失败** | 330.5 s | partial |
| R2 | 02:27:46 → 02:34:38 | 6.9 min | skipped（已最新） | 290.8 s | partial |
| R3 | 02:37:46 → 02:44:30 | 6.7 min | skipped | 305.4 s | partial |
| R4 | 03:19:31 → 03:26:56 | 7.4 min | skipped | 288.2 s | partial |
| R5 | 06:16:59 → 06:25:15 | 8.3 min | skipped | 284.7 s | partial |
| R6 | 09:20:18 → 09:28:03 | 7.8 min | skipped | 288.2 s | partial |
| R7 | 15:33:10 → 约 17:00 | 约 87 min | 5,572 只 / 69.4 只每分 / 0 失败 | — | 进行中 |

**无缺口轮次耗时构成**（以最快的 R3 = 403.7 s 为例）：

| 项 | 秒 | 占比 |
|---|---|---|
| `research_statistics` | 305.4 | **75.6%** |
| `announcements` | 61.5 | 15.2% |
| `capital_history` | 29.1 | 7.2% |
| 其它（check/universe/指标/业务概览/指数估值） | 7.7 | 1.9% |

`business_overview` 实测仅 0.6–0.9 s，不是瓶颈；`capital_history`（20 只/轮）29–41 s。

## 3. 本轮发现并修复的问题

### P0-A `/api/ready` 在整站不可用时返回 200（新引入，已实测）

- **现场三元组**（应用持 DuckDB 写锁的同一秒）：
  ```
  GET /api/ready                   → 200 {"ready":true,"checked_at":"...02:34:43","missing_counts":{...}}   ← 13 小时前的缓存
  GET /api/health                  → 503 "Cannot open file ... already open in PID 109704"
  GET /api/stock/600519/indicators → 503 {"reason_code":"duckdb_read_locked"}
  ```
- **性质**：2026-11-12 加 `/api/ready` 是为了消灭"`/api/health` 一直 200 而筛选全挂"，结果变成"`/api/ready` 一直 200 而筛选全挂"——**同一类缺陷换了个入口**。
- **修复**（`app/web/main.py::readiness_probe`）三道闸，任一不过即 503：
  1. **可用性**：真读 `SELECT 1`，失败 → `database_unavailable`；
  2. **实时遮蔽态**：`indicator_trust(warning_codes)["untrusted_all"]` → `snapshot_values_masked`（库能读但值全被 mask 的事故形态）；
  3. **陈旧度只披露不翻状态**：新增 `checked_at_age_seconds` / `stale`（阈值 30 min），避免应用空闲时误报。
  `/api/health` 语义不变。

### P0-B 按股票的除权窗口存在"永久不重拉 qfq"死角（新引入）

- **机制**：`_local_price_window_start()` 取该股 `min(latest_raw, latest_qfq)`，`xdxr_codes` 判据是 `event_date >= window_start`。若某除权事件 D **晚于该股价格推进才写入 `xdxr`**（上一轮 market_actions 失败、公告 general 窗口截断、或事件晚一轮），则 `event_date >= window_start` 恒假 → 该股 qfq 历史**永久保留旧复权因子**（前复权序列在 D 处跳空，MA/RSI/MACD/回撤/复权收益全错），且**任何 retry 路径都不会触发全量重拉**。旧的"全市场 min"实现天然不会漏，改成按股票后才有这个死角。
- **修复**：把触发条件从"日期比较"改成"写入事实"——`market_actions` 在 xdxr 抓取前后做事件指纹 diff，把**本次确有新写入**的股票持久化到 SQLite `data_refresh_state.qfq_refetch_pending`（JSON、去重、上限 500、裁剪退市码）；价格步骤读取该集合并**扩展目标集**（否则"价格已最新"的票根本不会被选中）+ 强制 `qfq start_date=None` 全量重拉；**只有落库成功的才移出集合，失败保留到下一轮**。xdxr 重试成功同样标记。
- 只读诊断 SQL 已写进 `_load_qfq_refetch_pending()` docstring（核对"本地 qfq 最新日 < 最新除权事件日"的股票是否清零）。

### P1-1 攒批写入在生产上退化为"一股一事务"（上一轮修复未真正生效）

- **实测**：今天 22 次落盘里 **20 次 N=1、2 次 N=2（90.9%）**，均 1.09 只/事务。原因：`should_flush()` 的时间判据是"从 buffer 开张算起 5 s"，而全历史抓取单只 28–635 s → 每次只攒到 1 只就 flush。
- **修复**：时间判据改为"距**上一次 add** 的空闲时间"且**要求 ≥ 2 只**才因超时落盘（单只由流水线排空的 `finally` 收尾）；`len(items)==1` 时日志不再写"批量"，避免验收证据失真；`steps.prices` 新增 `write_batches / write_batch_sizes / max_batch_stocks` 作为真实批大小证据。
- **注**：增量（非全历史）路径本来就是 20 只/事务（R1 的 1,245 只约 63 个事务），这条修的是全历史路径。

### P1-2 公告 general 截断让每轮 job 必 `partial`

- **实测**：R2–R6 六轮全部 `announcements: partial, truncated_categories: ["general"]` → `aggregate_job_status` 恒 partial → `last_full_success_at` 冻结在 **2026-10-30**（与事故报告里的读数相同）。
- **修复**：只有**财务 4 类 + 分红**类别的截断计入 `truncated` → `partial`；`general` 截断改为 `warnings`（如实带条数与窗口），保持"截断不丢数据"行为不变。

### P1-3 readiness 与发布门禁的股本口径相反（假红）

- `minimum_data_readiness` 把**任意 1 只**股本缺口算 blocking → `/api/ready` 503；而发布门禁允许最多 `max(50, 1%)` 只逐股隔离后正常发布。同一状态两个相反结论。
- **修复**：readiness 也用 `gate_hard_block_threshold()`；未超阈值时保留 `missing_counts`/`missing` 如实披露但不阻断，并新增 `share_capital_policy = {gap_count, threshold, blocking, quarantined}`。

### P1-4 统计域每轮全量重建（效率主因，已修）

- **实测**：`research_statistics_stock_state` 全表 5,580 行 `updated_at` **完全相同**（`2026-11-13T01:28:03.510245Z`）→ 每轮把 5,580 只全部重写，耗时 285–305 s/轮。
- **根因**：`StatisticsBuilder._stock_fingerprints()` 把**全库** `COUNT(*) FROM share_capital_history WHERE verified` 混进每只股票的指纹；`capital_history` 每轮核验 20 只就让该计数 +20 → 所有股票指纹失效。
- **修复**：逐股指纹改用**该股自己的** verified 计数（SQL 增加相关子查询），一次核验只影响被核验的那几只。预期轮次从 6.7–8.3 min 回到 **≈2.6 min**。

### P1-5 空适配器链被当成"源不支持"（会造成永久假 missing）

- `manager` 在 `tried_adapters` 为空（未注册/依赖缺失/全在熔断冷却）时返回 `source_unsupported`，而 retry 结算把它当"该票没数据"直接写 `missing` 并删 retry → 环境问题伪装成数据缺失、永久不重试。
- **修复**：该分支一律 `source_unavailable`（可重试），错误串写明 `adapter_not_registered:`（含已注册列表）或 `adapter_cooldown:`（含剩余冷却），绝不进 `_NON_RETRYABLE_ERROR_KINDS`。

### P1-6 B 股 900948 走向假名义 `retry_exhausted`

- `retry_list` 当时唯一一行 `900948/xdxr/retry_count=4/max=5`；公告侧已有 `_is_b_share_stock` 跳过通道，`_MARKET_ACTION_DATA_TYPES` 没有。
- **修复**：market_actions 与重试路径统一走 B 股守卫 → 直接 `missing(source_unsupported)`、删除该股 retry、不抓取/不计失败。

### P2 两项

- **baostock 空结果未标注**：`_classified_source_fields()` 在"无 skip、无错误、0 条记录"时返回 `none` → manager 打误导性的 `WARNING baostock 返回错误（none）`（今天 9 次）。改为 `legal_empty` + reason，manager 打 INFO。
- **`data_status.summary.price_age_days` 时区回归**：仍用 `datetime.now(UTC)`，与 `stock_detail` 的 CN_TZ 口径不一致（北京 00:00–08:00 少算 1 天），已统一。

## 4. 效率复盘：修复前 vs 修复后

| 维度 | 2026-11-12（事故轮） | 2026-11-13（修复后） | 变化 |
|---|---|---|---|
| 价格成功率 | 5,567/5,573（6 失败） | **1,255/1,255、5,572/5,572（0 失败）** | 失败面清零 |
| `xdxr_full_refetch` | **293** | **10 / 34** | −97% / −88% |
| 价格吞吐 | 38.83 只每分 | 79.67（R1）→ 69.4（R7） | **+79%~+105%** |
| 相对 150 只每分基线 | 0.26× | 0.46–0.53× | 仍未达标 |
| ≥10 s 写库停顿 | 144 处 / 5,790 s / 最长 159 s（占价格步 67%） | R1 19 处 / 347.8 s / 41.7 s；R7 32 处 / 647.9 s / 50.9 s | 次数 **−87%**、时长 **−94%** |
| 无缺口轮次总时长 | — | **6.7–8.3 min**（基线 3–5 min） | 未达标（主因统计域，已修） |
| 轮次状态 | partial | 仍全 partial（general 截断，已修） | — |

**仍未解决的效率项**（列入残留）：
1. **写放大**：实测每推进 1 只价格要付出 **≈237 MB 读 + 303 MB 写 + ≈1.0 s CPU**（17M 行主表上的 `ON CONFLICT` 冲突检查/行组重写），这是吞吐卡在 0.5× 基线的主因；建议对大表增量写入做 `EXPLAIN ANALYZE` 后改为"按 stock_code 范围 DELETE + append"或更粗粒度事务。
2. **自适应并发无效抖动**：今天 6 次档位切换（0.11–0.13 次/分，是修复前的 1.9–2.3 倍），且实测扩档不带来吞吐提升（瓶颈在写 I/O），属"无效抖动"。
3. **公告 general 窗口**：每轮固定 ~61 s，5 页只能覆盖 150/~1,200 条（≈88% 未读取）；建议按游标增量收敛或提高 `max_pages`。

## 5. 更正上一轮报告的一处失真

`docs/STATUS.md` 与本项目 `reports/117` §5.1 曾写"价格 **1,255 只全历史重拉** 15.7 min"。经日志核对（`job_logs#201.steps.prices.xdxr_full_refetch=10`、`raw_full_refetch=false`、`[增量] 本地价格截至 2026-11-12，从该日期增量补缺口（不整段重拉）`）：
**真正全历史重拉的只有 10 只，另 1,245 只是增量窗口（20 只/事务 upsert）**。结论方向不变（0 失败、吞吐翻倍），但归因改写：**增量路径的 20 只/事务批量写入**才是那 15.7 分钟的主要效率来源；全历史批量当时并未生效（见 P1-1）。

## 6. 验证

- 全量 S1（本轮修复后重跑）与 Ruff 结果见 `docs/STATUS.md` 对应行。
- 新增回归测试 5 个文件：
  - `test_fix20261113_probe_ready.py`（12 例：三道闸、阈值对齐、CN 时区）
  - `test_fix20261113_pipeline_price_batch.py`（9 例：空闲 30 s 的两次 add 仍合并为一个事务、N=1 不写"批量"、原子性/回退/整段替换）
  - `test_fix20261113_pipeline_qfq_pending.py`（10 例：事件晚于价格推进仍强制全量重拉、失败保留、B 股通道）
  - `test_fix20261113_pipeline_source_contract.py`（12 例：general 截断不算 partial、空链可重试、baostock legal_empty）
  - `test_fix20261113_lead_statistics_fingerprint.py`（2 例：逐股指纹不受全局 verified 计数影响）
- 同步更新既有用例 2 处：`test_fix20261112_update_price_batch.py::test_buffer_is_bounded_by_stocks_rows_and_seconds`（新时限语义）、`test_data_quality_status.py::test_recent_listing_data_gaps_are_disclosed_but_share_capital_still_blocks`（阈值政策）。

## 7. 残余风险（未在本轮处理）

1. **`legal_empty` 在源降级时可能污染 missing**：若上游被风控后返回空表而非抛错（东财/akshare 常见降级形态），整批股票会被写进 `missing(source_empty)` 且永不重试。建议：批量请求**全部**返回空时升级为 `source_unavailable`（保守），只有单票请求才允许 `legal_empty`。
2. **熔断计数可被 `legal_empty`/`per_symbol_data_error` 清零**：源持续返回空表时该 `(adapter,data_type)` 桶永不熔断，每轮继续打同一死源。建议这两类改为"中性"（不增不减），只有明确成功才清零。
3. **自建 `AdapterManager()`**（buyback/index_valuation/treasury/funding/capital/business/hk_dividends/pdf）每次调用都是全新熔断状态，熔断隔离在这些域上失效。建议在 `update.py` 构造处注入共享 `adapter_mgr`。
4. **`capital_history` 的 due 判据**（`latest_cap < latest_price`）使几乎全部上市股票长期"due"，每轮 20 只、永远续不完；配合"每轮写 20 只 → 统计域 20 只重算"，成本可控但吞吐永远追不上。建议改为"覆盖到最新价格日即完成"或引入覆盖水位。
5. **`price_daily_qfq` 在收盘后首轮会大面积滞后，而价格步骤仍报 `success`（本轮新发现，P1）**：
   实测 job 207（15:33–17:29，5,572 只、59.73 只每分、0 失败）结束后，   `price_freshness.qfq_current_count = 959 / 5,572`——即 **4,613 只的 qfq 仍停在 11-12**，而 raw 已到 11-13。   下一轮（18:02）因此重新选中 4,613 只（`needs_update=False` 但目标集非空），形成「每轮都在补 qfq」的稳态。
   机制推断（证据：18:02 轮的响应分布为 1 行 × 1,213 / 2 行 × 1,200）：收盘后不久腾讯 `qfqday` 对多数股票尚未发布当日复权数据，   而 `fetch_one` 只以 **raw** 成功与否判定 `err`，qfq 返回空不算失败 → 该股 raw 前进、qfq 不动，步骤仍计 success。
   后果：15:33 那一轮发布的指标快照用「当日 raw + 前一日 qfq」混算技术指标（MA/RSI/MACD），约 2.5 小时后下一轮才纠正。
   建议（未实施）：① 价格步骤统计「raw 前进但 qfq 未前进」的只数并落进 step，超过阈值（如 5%）→ `partial` + `reason=qfq_lagged`；   ② 收盘后首轮延后到源稳定（16:30+），或对 qfq 滞后集合做一次定向重试。
6. **小库阈值**：`gate_hard_block_threshold = max(50, 1%)` 在上市股 <101 只的库里可把 100% 股票隔离光且只报 `partial`。建议加"隔离占比 ≥ X% 即 hard_block"。
