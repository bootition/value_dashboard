---
title: 更新内存上限与吞吐优化——F10 并发抓取与财务明细批量合并
status: approved
category: reports
created: 2026-09-01
last-reviewed: 2026-09-01
---

# 更新内存上限与吞吐优化

## 结论

✅ 已完成。DuckDB 冷核对/大事务峰值由默认约 25GB 限制到约 **13.5GB**；
业务概览回填速率由 **49.7 只/分** 提升到 **约 60.5 只/分**（+22%），
且源侧仍保持 reports/67 约定的 **≤2 req/s**。财务明细 Sina 写入路径
由逐行 DELETE+INSERT 改为 TEMP TABLE 合并，10 只批量写入实测
**0.17s/只**（不含网络抓取）。

## 改动

1. **DuckDB 有界内存**：`database.duckdb_memory_limit` 默认 `"12GB"`，
   DuckDBStore 的读/写连接统一携带该配置，可通过 `config/user.yaml` 覆盖。
   正式库冷核对实测 98s 完成、峰值 WS 13.5GB；服务启动后约 20s 回落到
   约 1.5GB，不再出现 20~28GB 的长时间占用。

2. **业务概览抓取流水线**：`update_many` 按
   `business_overview_concurrency`（默认 4）并发抓取，内存有界
   （in-flight 上限 = concurrency × 4）；每只股票的两个 F10 数据面都
   完成后在主线程串行提交，避免同进程 DuckDB 多写者竞争。东财 F10
   适配器的全局限速锁继续保证 2 req/s 上限。

3. **源缺失股票出队**：profile 与 breakdown 都已登记为
   `source_empty` 的股票在 `business_overview_missing_retry_days`
   （默认 7 天）内不再进入 due 队列，避免北交所等无 F10 覆盖股票每轮
   反复占用限速额度。

4. **财务明细批量合并**：Sina 标准化行改为每表一条
   TEMP TABLE → UPDATE（源非 NULL 字段覆盖）+ INSERT（新报告期），
   语义与 legacy merge 等价；非 Sina 兜底仍走通用逐行 upsert。

## 验证

- 正式库业务概览：更新前 49.7 只/分 → 更新后稳定 60.5 只/分；
  进程 WS 约 0.3~1.1GB；
- 正式库财务明细写入：10 只批量事务 1.7s（0.17s/只），峰值 WS 0.41GB；
- 定向回归：业务概览 31 passed；storage/update/sina/lineage 等
  95 passed；store 路径注入 12 passed；
- 完整 S1：675 passed / 3 failed。其中 1 项为本次静态导入约束
  （`test_store_modules_do_not_import_config`）已修复并单独复跑通过；
  其余 2 项与 `reports/96` 记录的干净基线失败相同，非本次引入。

## 剩余风险

- `12GB` 是在 32GB 主机上的保守默认；超大分析查询若出现频繁落盘变慢，
  可在 `user.yaml` 按机器内存上调（不建议超过 16GB）。
- DuckDB 文件约 50GB 高水位的离线压缩/重建仍待办。
