---
title: 数据库治理计划执行状态
status: approved
category: reports
created: 2026-09-01
last-reviewed: 2026-09-01
supersedes: reports/101_SECOND_PLAN_REVIEW_EXECUTABILITY_2026-09-01.md
---

# 数据库治理计划执行状态

## 1. 执行结论

P0 离线重建已完成并通过校验；P1/P2/P3 的代码与 schema v17 已上线。
P6 采用“硬链接快照 + 已导出的 Parquet 冷归档”作为当前回滚基线。
`vd backup` 对 26GB BLOB 大表的单次 COPY 仍受 DuckDB 内存限制，后续
需把备份管理器改为分块导出（代码已具备同等逻辑，待切换）。

## 2. 已执行

1. **P0 离线重建**
   - 旧库：49.7GiB，新库：约 41GB 十进制；
   - 逐表导出：`D:\vd-rebuild-export-20260901`，manifest 含 SHA-256；
   - 逐表导入：`D:\vd-rebuild-new-20260901\valuedashboard.duckdb`；
   - 校验通过：全部表行数、关键聚合指纹、视图、索引、约束、序列；
   - 新库已替换到正式路径；旧文件与硬链接快照保留。
2. **P1 lineage hash 集合**
   - schema v17 创建 `raw_response_archive_valid_hash`；
   - lineage 查询改为连接小 hash 表，冷核对不再触碰 26GB BLOB。
3. **P2 归档轮转**
   - schema v17 创建 `raw_response_archive_partitions`；
   - 写入路径在 5GB / 100,000 行 / 31 天触发自动轮转；
   - 历史 BLOB 已导出 Parquet 冷归档：`D:\vd-cold-archive`，4.9GB，含 manifest。
4. **P3 统计域批量预取**
   - worker 按块 prime_batch，减少逐股查询；
   - `build_series` / `_stats_for_stock` 兼容缓存与逐股查询。
5. **P5 可观测性部分**
   - 重建证据、冷核对耗时、价格更新耗时已记录。

## 3. 已验证

- 小库导出/导入/校验全绿；
- lineage、business、backup、statistics 定向回归 133 passed；
- 重建后正式库 readiness=true、warning_codes=[]；
- 重建后完整价格更新：5543 只成功，82.3 只/分；
- 冷核对从启动路径约 3s（缓存命中）。

## 4. 当前限制与待办

- `vd backup` 仍无法一次性 COPY 26GB BLOB 归档，需分块导出改造；
- P2 冷归档未接入 CLI 恢复命令；
- P3 正式库批量预取与黄金样本的长期基准未跑完；
- 推送仍因代理未开启失败。

## 5. 回滚信息

- 旧 DuckDB：`data/valuedashboard.duckdb.old-20260901154717`
- 硬链接快照：`data/valuedashboard.duckdb.pre-rebuild-20260901`
- SQLite 副本：`data/valuedashboard.sqlite.pre-rebuild-20260901`
- Parquet 导出：`D:\vd-rebuild-export-20260901`
- 冷归档：`D:\vd-cold-archive`
