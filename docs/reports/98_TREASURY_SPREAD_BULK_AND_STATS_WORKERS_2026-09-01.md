---
title: 总耗时压缩——国债利差批量刷新与统计域并行扩容
status: approved
category: reports
created: 2026-09-01
last-reviewed: 2026-09-01
---

# 总耗时压缩：国债利差批量刷新与统计域并行扩容

## 结论

✅ 已完成。正式库完整增量更新周期从约 **38 分钟** 压缩到约 **12 分钟**；
其中“国债曲线变化 → 全市场指标重算”由约 16 分钟降为 **30 秒**，
历史统计域重建由约 14 分钟降为 **约 9 分钟**。

## 本轮数据更新结果

- 最近一次完整更新：**success**（随后一次 partial，原因见下）
- 财务明细缺口：**0**
- 业务概览缺口：1246 只已全部回填成功
- 历史统计域：5553 只 success、222,040 条记录
- lineage：archive_gap / batch_gap / empty_payload 均为 0
- readiness：ready=True，warning_codes=[]

## 发现并处理的问题

1. **12GB DuckDB 内存上限过紧**：服务进程冷核对曾报
   `Out of Memory: failed to pin block (11.1 GiB/11.1 GiB used)`。
   默认值调整为 **14GB**，冷核对稳定通过，峰值约 16GB，完成后回落。
2. **悬挂 job 告警**：强制重启留下的 `job_logs id=112` 处于 running，
   产生 `STALE_RUNNING_JOBS` 警告；已按死锁恢复语义标记 failed，
   重启后 warning_codes 清零。
3. **当天国债曲线 `2026-09-01` 源侧为空**：已登记
   `treasury_curve_daily_2026-09-01` missing（如实披露，后续轮次重试）。
4. **价格 partial**：301697 价格抓取失败 1 条已入 retry_list，下一轮自动重试；
   其余 2 只成功。

## 提速改动

1. **国债利差批量刷新**：新增 `IndicatorCalculator.refresh_treasury_spreads()`，
   仅批量更新 `ttm_dividend_yield` 与 9 个 `div_yield_spread_*` 列并记录
   衍生 lineage。正式库 5542 只 **30.1 秒**完成，样本值与此前全量重算一致；
   国债曲线变化不再触发全市场完整指标重算。
2. **统计域进程复用与曲线缓存**：每个 worker 进程复用同一个
   StatisticsBuilder，共享国债曲线解析缓存；默认并行数由 4 → **12**。
   5553 只重建实测约 9 分 11 秒。
3. **统计域指纹确定性修复**：`_input_fingerprint` 的内容聚合增加
   `stock_code` 作为最终排序键。此前多股共享同一报告日/除权日时，
   DuckDB 的 `string_agg` 顺序不稳定，指纹在两次读取间漂移，导致
   5553 只统计域每次启动都误重建。修复后连续 5 次读取指纹完全一致；
   后续只有真实输入变化才会重建统计域。
3. **配置**：
   - `research_statistics_parallel_workers: 12`
   - `database.duckdb_memory_limit: "14GB"`

## 验证

- 完整 S1：**677 passed / 2 failed**；2 项失败仍为已知干净基线失败
  （mixed snapshot/statement report dates 两用例），与本次改动无关。
- 定向回归：indicator / update / business / capital-history / store-path /
  data-quality 全部通过。
- 正式库 treasury 批量刷新前后抽样（600519）值一致：
  `ttm_dividend_yield=4.000033089140605`、
  `div_yield_spread_10y=2.3117330891406054`。

## 剩余风险

- DuckDB 文件高水位仍约 50GB，离线压缩/重建会进一步降低冷核对与统计
  域读取耗时，仍为下一优先项。
- 当日国债曲线若源持续缺失，将按 missing 机制在后续启动时重试；不影响
  readiness 与筛选。
