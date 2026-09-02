---
title: 个股详情/自选页连接冲突修复与 CLI 状态查询修复
status: approved
category: reports
created: 2026-09-03
last-reviewed: 2026-09-03
---

# 个股详情/自选页连接冲突修复与 CLI 状态查询修复

## 背景

2026-09-02 体检与用户反馈确认两类问题：

1. 打开中国移动（600941）个股详情页很慢且接近空白；打开已保存自选分组要等待约 28 秒。
2. `vd data auto-update status` 在正式库上触发全量 schema 初始化并抛 `OutOfMemoryException`。

## 根因

1. **DuckDB 同进程混合连接配置（主因）**。后台 `data-status/summary` 全量刷新
   使用 `memory_limit("4GB", threads=2, preserve_insertion_order=False)`，
   而普通 K 线/财务趋势/自选查询使用默认 `2GB` 且不带 threads/preserve 配置。
   DuckDB 不允许同一进程对同一文件同时存在不同配置的连接；普通请求因此重试约
   28 秒后返回 500，前端表现为卡片空白或长等待。
   复现实测：触发 summary 刷新后并发请求 `/api/stock/600941/kline`、
   `/api/stock/600941/financial-trend`、`/api/watchlist/list` 均
   500/503，耗时 27.8s。
2. **CLI 只读命令误走写初始化路径**。`vd data auto-update status` 通过
   `AutoUpdateController` 构造路径调用 `_database_context(initialize=True)`，
   执行 `init_all_schema()`（未带 `skip_if_current`）。schema v17 的幂等
   初始化会扫描 `raw_response_archive_all`（约 43GB BLOB 视图），在默认
   2GB 预算下 OOM。正式库主文件 mtime 未变化，未观察到持久化写入。

## 修复

1. **统一 DuckDB 连接配置**：
   - `config/default.yaml`：`database.duckdb_memory_limit` 2GB→8GB，
     新增 `duckdb_threads: 2`、`duckdb_preserve_insertion_order: false`；
   - `DuckDBStore._connection_config()` 固定输出 memory/threads/preserve
     三项；`memory_limit()` 文档明确禁止在 Web 服务进程内使用；
   - 删除 `app/web/main.py` 与 `app/web/api/data_status.py` 中后台任务的
     `memory_limit()` 覆盖，后台扫描与普通查询共用同一套配置。
2. **CLI 状态查询只读化**：
   - 新增 `read_persisted_auto_update_status()`（仅读 SQLite，不打开 DuckDB）；
   - `vd data auto-update status` 改为 `_sqlite_store(initialize=False)`；
   - 其它 CLI 写命令的 `init_all_schema()` 增加 `skip_if_current=True`
     （与 Web 启动一致），schema 已最新时跳过全量幂等 DDL。
3. **降低空闲期重量摘要轮询频率**：
   - `_SUMMARY_TTL_SECONDS` 60→300；前端空闲态重量轮询 60s→300s；
   - 自动更新从 running→finished 时前端立即主动刷新一次重量摘要，
     避免 stale 提示延迟消失。

## 验证

- `vd data auto-update status` 正常返回只读状态，不再打开 DuckDB、不再 OOM。
- 重启服务并加载新代码后，在 startup maintenance 运行期间与 summary 后台刷新
  期间实测：
  - `/api/stock/600941/kline` 200，0.05–0.12s（修复前 500/27.8s）；
  - `/api/stock/600941/financial-trend` 200，0.04–0.07s（修复前 500/27.8s）；
  - `/api/watchlist/list` 200，0.09–0.10s（修复前 503/27.8s）；
  - `/api/stock/600941/business-overview` 200，0.03–0.10s（修复前 23–25s）。
- 后端 Ruff 通过；前端 lint 通过、Node 69 + Vitest 57 = 126 测试通过、生产构建通过。
- 重启后 startup maintenance 的 readiness 在 8GB 统一配置下约 12s 完成（4GB 初版在
  与普通请求并发时出现 3.7GiB OOM，故最终采用 8GB）。
- 完整 S1 回归未在本轮运行（服务在线时 S1 门禁按设计拒绝执行），待服务停窗后补跑。

## 剩余观察

- 重量摘要全量构建实测约 80s（此前记录 19-23s 为筛选就绪核对口径），但已为后台
  stale-while-revalidate，TTL 已放宽至 300s，且不再阻塞/拒绝普通查询。
- 自动更新子进程在 research_statistics 全量重建的发布阶段会持续持有 DuckDB 写连接
  （本轮约 4-6 分钟），期间 Web 查询会等待/超时。该窗口与本次连接配置冲突无关，
  属 DuckDB 单写者模型的固有约束，后续可优化发布策略（分批可见/快照读）。
- `source_audit` 4,302 万行仍是 DuckDB 文件偏大的主因；冷热分离/归档为其长期治理项，
  与本次响应慢无直接因果。
