---
title: 更新链 OOM 与 raw_response_archive 失控轮转修复
status: approved
category: reports
created: 2026-09-04
last-reviewed: 2026-09-04
---

# 更新链 OOM 与失控轮转修复（2026-09-04，自动更新验证轮发现）

## 发现过程

用户要求"运行自动更新作为一次检验"。离线重建（`reports/107`）后的首次
完整自动更新在指标发布阶段 OOM 崩溃；修复后第二轮完整通过；第三轮（重试
消费轮）在 raw_response_archive 轮转处 Catalog Error 崩溃。两个都是重建后
首次全量走到的深水区路径。

## 缺陷一：lineage executemany OOM（P1）

**现象**：`_record_derived_lineage_in_connection` 的 24.5 万行
`executemany` 插入 source_audit 时 `Out of Memory Error
(7.4 GiB/7.4 GiB used)`，更新整体 failed。

**根因（库副本上复现定位）**：DuckDB 1.5.5 Python 驱动在 executemany
绑定 `date`/`datetime` 参数时，事务本地内存按 **~450KB/行** 堆积
（IN_MEMORY_TABLE 标签；1 万行实测峰值 4.4GB，纯字符串参数仅 ~3KB/行）。
指标全量发布 lineage = 5,542 股 × ~44 数值字段 ≈ 24.5 万行 → 必然 OOM。
昨日旧库成功是因该路径在 8GB 预算下勉强未越界，重建后行布局变化压垮。

**修复**：`app/core/indicators/calculator.py` 改为 pandas DataFrame
`connection.register()` + 单条 `INSERT ... SELECT`（DATE 列显式 CAST，
fetch_time 统一 naive UTC 与旧语义一致）。

**实测**：24.5 万行 0.3s、峰值 <100MB（原路径分钟级、>7.4GB）；
仍在同一发布事务内，原子性不变。第二轮验证轮 indicators=success 实锤。

## 缺陷二：raw_response_archive 失控轮转（P1）

**现象**：`Catalog Error: Could not rename "raw_response_archive" to
"raw_response_archive_20260904_104836": another entry with this name
already exists!`，第三轮 failed。

**根因**：`_rotate_raw_archive_if_needed` 的 registry 记账错误——
轮转时 UPDATE 打在**尚不存在**的 TS 名上（0 行生效），新活跃行插入用
`ON CONFLICT DO NOTHING`，导致活跃 registry 行的
row_count/estimated_bytes/created_at **原样保留**。当日累计写入使
estimated_bytes 首次越过 5GB 阈值后，**每次归档写入都再轮转**：
1 小时内生成 416 个单行 TS 表、视图 UNION 膨胀到 400+ 分支，最终
同秒两次轮转目标同名 → 崩溃。

**修复**（`app/core/storage/duckdb_store.py`）：
1. 先把活跃 registry 行改名为 TS 名并落终值（row_count/bytes）；
2. 再插入计数器**归零**、created_at=now 的新活跃行（去掉
   ON CONFLICT——状态异常时让事务显式失败，好过静默保留脏计数器）；
3. 表名冲突时自动加序号，消除同秒重名这一崩溃类。

**存量整理**（`.planning/.../merge_runaway_partitions.py`，幂等）：
416 个 TS 表合并为 `raw_response_archive_merged_20260904`
（52,173 行 / 5.39GB，payload 原样保留），registry 收敛为 1 行，
视图总数 216,825 = 164,651(history) + 52,173(merged) + 1(active) 核验一致。

## 回归与验证

- 新增 `test_raw_archive_rotation_resets_counters_and_survives_same_second_double_rotate`
  （阈值触发后计数器归零、同秒双轮转不崩溃、视图完整性）；S1 定向
  5 passed。
- 快照原子性/完整周期/HK 域定向回归通过（18+30 passed）。
- 自动更新验证轮：第二轮全阶段 success（含 indicators 发布）；
  第四轮（修复轮转+合并存量后）验证重试消费与再次全量发布。

## 运维提示

- 正式库 data/ 目前还有旧库回滚件（43GB）；观察期后按 D15 处理。
- 轮转阈值为 5GB/10万行/31天（KB D4/D20）；merged 表计入
  raw_response_archive_all 视图，冷归档工具未来可整体外置。
