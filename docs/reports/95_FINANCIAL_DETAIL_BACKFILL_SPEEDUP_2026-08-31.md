---
title: 财务明细回填提速与自动完成机制
status: approved
category: reports
created: 2026-08-31
last-reviewed: 2026-08-31
---

# 财务明细回填提速与自动完成机制（2026-08-31）

## 背景

旧 `_refresh_financial_detail_backfill` 逐股串行 `refetch_one`，正式库实测约 2 只/分钟，且存在两个隐蔽瓶颈：
1. `_financial_detail_gap_codes` 对每个缺口股票逐只调用 `_is_b_share_stock`，每次打开一个 DuckDB 连接；4,900 个缺口仅判断 B 股就要约 98 秒。
2. 每股三次独立 DuckDB 事务，commit 在大库上约 7 秒；源缺口股还会逐次落到 40 秒以上的 TDX 慢回退。

## 修复内容

- `_financial_detail_gap_codes`：B 股过滤下推到 SQL；缺失股在 `missing_list` 登记后 7 天内出队。
- `_refresh_financial_detail_backfill` 重写为：
  - 并发抓取（默认 16 线程，只走 `sina -> akshare_eastmoney` 快速链）；
  - 每 50 只合并为一个 DuckDB 事务；
  - TDX 仅对每轮最多 10 只快速源缺口做预算兜底；
  - 快速源确认无数据 → 登记 missing，不再阻塞队头。
- `DataInitializer`：
  - 财务行写入的 information_schema / 主键约束查询改为实例缓存；
  - 字段级溯源支持白名单；明细回填只审计 readiness/screening 使用的核心口径字段。
- 配置默认值：`max_stocks_per_run 500`、`concurrency 16`、`persist_batch_size 50`。
- CLI `data financial-detail-backfill` 默认 500 只，并在写锁内自动重算成功股票快照。
- `AdapterManager.fetch_with_sources`：允许批量任务显式选择快速源链，避免默认慢回退。

## 实测

- 正式库 100 只明细回填：**290 秒（20.7 只/分钟）**，旧实现约 2 只/分钟，提升约 10 倍。
- 回填后诊断缺口按新口径统计，缺失股 7 天窗口后自动重试。
- 回归：update/storage/business/scope/sina/funding 等 105 passed。

## 自动完成安排

- 服务重启后，自动更新每轮可处理 500 只（约 25 分钟）。
- 一个独立完成器会循环执行 `data financial-detail-backfill --max-stocks 1000`，直到 `financial_detail_gap_count` 归零或达到时间上限；每个批次结束自动重算快照。
- 完成后无需人工监控。
