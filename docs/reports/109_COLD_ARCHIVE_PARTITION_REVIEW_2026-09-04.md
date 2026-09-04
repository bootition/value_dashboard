---
title: 冷归档分区策略复审与修正
status: approved
category: reports
created: 2026-09-04
last-reviewed: 2026-09-04
---

# 冷归档分区策略复审与修正

## 用户问题

“为什么过去数据留在旧表，新数据留新表，不按年份/GB 划分？”

## 复审结论

之前的重建做了“表级冷热分离”，但外部 Parquet 仍是全局 hash 分块：
- raw_response_archive_history 33 个 part 按 hash 每 5,000 行；
- source_audit_archive 单文件 280MB；
- 不能按年份独立删除，不符合大厂冷数据生命周期规范。

## 已修正

新增 `scripts/repartition_cold_archive.py`，从回滚旧库重新生成：

- `D:\vd-cold-archive\partitioned\raw_response_archive_history\year=YYYY\partNNNN.parquet`
  - 每年一个目录，part 按 5,000 行封顶（BLOB 表控制单文件大小/内存）；
- `D:\vd-cold-archive\partitioned\source_audit_archive\year=YYYY\partNNNN.parquet`
  - 按 report_date 年份目录，part 按 500,000 行封顶；
- `manifest.json` 记录每个 part 的年份、文件名、行数、sha256。

实测：
- source_audit_archive：81 个 part，合计 30,039,082 行；
- raw_response_archive_history：33 个 part，合计 164,651 行；
- 分区冷档总大小约 5.1GB。

修正过程中发现并修复了首版 repartition 的两个 bug：
1. history manifest 行数记录的是剩余计数而非本 part 行数；
2. source_audit_archive 的 id 窗口下界错误，导致早期 part 重复包含后续行
   （已删除错误产物并重新生成，行数从 79.5M 修正回 30.04M）。

## 尚未物理删除的保留物

- 旧主库 `data/valuedashboard.duckdb.old-20260904013322`（41GB）仍保留作回滚；
- 磁盘当前剩余约 80GB；观察一个完整自动更新周期后可考虑删除旧库。
