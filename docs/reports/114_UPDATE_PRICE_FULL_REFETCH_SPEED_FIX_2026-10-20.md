---
title: 自动更新价格步骤异常变慢（全市场全历史重拉）诊断与修复
status: approved
category: reports
created: 2026-10-20
last-reviewed: 2026-10-20
---

# 自动更新价格步骤异常变慢（全市场全历史重拉）诊断与修复（2026-10-20）

## 1. 结论（TL;DR）

2026-10-20 13:21 启动的自动更新，价格步骤只有 **6.2–6.6 只/分**（页面显示 ETA 约 13.9 小时），
而历史正常轮次是 **148 只/分**。根因不是网络变慢，而是**本地价格距今天数超过
`update.incremental_window_days`（30 天）后，代码把 raw 也切成了"全历史重拉"**：

- 本次启动时正式库价格最新日期为 **2026-09-17**（33 天前），`gap_days > 30`；
- `_update_prices_incremental` 因此对**全部 5,551 只股票**设置 `raw_full_refetch=True`，
  并把 qfq 也强制全量（`qfq_full_requested = need_qfq_full or raw_full_refetch`）；
- 腾讯适配器对"全历史"请求按 640 根/页**向前翻页（最多 12 页）**，单只股票 raw+qfq
  变成约 20 个请求；持久化走 `DELETE + INSERT` 全量替换，直接打在主表
  （`price_daily_raw` / `price_daily_qfq`，约 1,700 万行/表）上；
- py-spy 连续采样显示主线程 **80% 时间停在
  `_persist_price_pair_in_connection` 的 `DELETE FROM price_daily_raw WHERE stock_code = ?`**，
  进程 I/O 约 10 MB/s 读 + 6 MB/s 写、约 1.1 核 CPU，瓶颈在**逐股全量替换写库**；
- 实测每 20 只的持久化批次约 4 分钟（≈12 s/只 × 2 张表的 DELETE）。

修复后同一台机器、同一网络下价格步骤恢复 **150.3 只/分**（剩余 5,126 只，ETA 约 33 分钟）。

## 2. 现场证据

| 项 | 值 |
|---|---|
| 作业 | `job_logs.id=186`（`incremental_update`，started 2026-10-20T05:21:12Z） |
| 正式库价格最新日期 | `2026-09-17`（`/api/data-status/summary` → `dates.price`） |
| 进度 | `done=80/5551`、`rate_per_minute=6.2`、`eta_seconds=52807`（≈14.7h） |
| 历史对照 | 2026-09-17 job 185：`raw_full_refetch=false`、`rate=148.73/min`、`conc=16` |
| 线程栈 | 主线程 `update.py:2841` → `_persist_price_pair_in_connection`（DELETE 全量替换路径） |
| 请求线程 | `tencent_adapter.py:119` → `_wait_rate_limit`（网络不是瓶颈，腾讯 0.2s/请求） |
| 进程 I/O | 60 s 内 Read +0.9 GB / Write +0.55 GB；CPU ≈ 1.1 核 |
| 数据库文件 | `data/valuedashboard.duckdb` 20.2 GB（2026-09-04 重建后为 7.8 GB） |

## 3. 根因

1. **陈旧阈值被误当成"必须整段重拉"**（`app/core/update.py`）：
   `gap_days > incremental_window_days` 即 `raw_full_refetch=True`。
   `incremental_window_days` 原本只是"多久没更新算陈旧"的判断，但触发的是
   **全市场全历史重拉**——一次 33 天的缺口本可以用从 `2026-09-17` 起的增量请求
   （单只约 17 行）补齐。
2. **全历史请求的放大效应**：腾讯 K 线单次最多 640 根，适配器按页向前翻（≤12 页），
   1 只股票 raw+qfq ≈ 20 次请求；正常增量只需 2 次请求。
3. **全量替换写放大**：`DELETE ... WHERE stock_code=?` 在 1,700 万行且有 ART 主键索引的
   表上实测约 10 s+/股（两表各一次），并且把文件高水位继续推高。
4. **可观测性缺口（次因）**：CLI 子进程没有配置 `logging`，根 logger 只在 WARNING+
   时经 `lastResort` 输出，更新过程的 INFO 日志（阶段/缺口/吞吐）全部丢弃，
   `data/logs/auto-update-child.log` 自 2026-09-17 后没有任何新增，
   导致"为什么慢"无法从日志直接看出。

## 4. 修复

### 4.1 默认不再因"陈旧"整段重拉（`app/core/update.py`、`config/default.yaml`）

- 新增开关 `update.price_full_refetch_on_stale`，**默认 `false`**：
  - 默认：本地数据陈旧时仍从本地最新日期**增量补缺口**（`[增量] 本地价格截至 <date>，
    从该日期增量补缺口（不整段重拉）`）；
  - `true` 时才保留旧的"超过 `incremental_window_days` 整段重拉"语义，仅作为
    **数据修复**手段（如已知历史被污染）。
- xdxr（除权除息）触发的 qfq 全历史重拉保持不变（本次窗口仅 2 只，代价可忽略）。
- 更新了 `_update_prices_incremental` 的 docstring 与构造参数注释。

### 4.2 自动更新子进程补齐 INFO 日志（`app/cli/main.py`）

- 新增 `_configure_update_logging()`，在 `data auto-update run` 入口配置
  INFO 级日志到 stderr（Python 3.9+ 行缓冲，重定向到 `auto-update-child.log` 实时可见）。
- 其它 CLI 命令行为不变（仍只输出 JSON 结果到 stdout）。

### 4.3 测试

- `tests/regression/test_incremental_price_lineage.py`
  - 新增 `test_stale_local_price_stays_incremental_by_default`：陈旧 33 天时
    `raw_full_refetch=false`，且 raw/qfq 的 `start_date` 均为本地最新日期；
  - `test_stale_local_price_triggers_raw_full_refetch` 与
    `test_full_replace_rejects_truncated_source_and_keeps_old_data` 显式开启
    `price_full_refetch_on_stale=True`（它们验证的是数据修复路径/截断防线）。
- 新增 `tests/regression/test_cli_auto_update_logging.py`：断言更新入口安装
  INFO handler 且幂等。

## 5. 验证

| 项 | 结果 |
|---|---|
| S1 定向回归 | `test_cli_auto_update_logging` + `test_incremental_price_lineage` + `test_retry_persistence`：**26 passed**（正式库前后指纹一致） |
| S1 相关链路 | `test_auto_update_*` / `test_incremental_update_scope` / `test_update_job_and_progress` / `test_indicator_price_domain`：**61 passed** |
| Ruff | 通过 |
| 修复后运行 | 价格步骤 **150.3 只/分**、`total=5126`、ETA ≈ 33 分钟；子进程日志出现
  `[增量] 本地价格截至 2026-10-19，从该日期增量补缺口（不整段重拉）`、
  `tencent 成功获取 price_daily: 17 行` |

## 6. 处置过程

1. 13:37 采样确认瓶颈后，终止了正在跑的 14 小时全量重拉子进程（每批已提交，
   已完成的约 330 只不回滚）；
2. 修改代码与测试，先跑定向 S1；
3. 14:49 通过 `start.bat` 重启服务，自动更新按新路径重新开始（见 §5）。

## 7. 残留风险与后续

1. **全量替换 DELETE 仍然慢**（≈10 s+/股）：数据修复模式或 xdxr 批量日仍需承受；
   当前正式库 20.2 GB（重建后 7.8 GB）存在明显高水位，建议后续做一次
   `CHECKPOINT`/离线重建评估（与 reports/99-102 的数据库治理计划合并）。
2. `data/valuedashboard.duckdb.old-20260904013322`（43 GB）与
   `valuedashboard.duckdb.tmp/` 仍在 `data/`，占 ~63 GB；按 AGENTS.md 删除前需登记保留依据。
3. 若将来确实需要"整段重拉修复"，请在 `config/user.yaml` 打开
   `update.price_full_refetch_on_stale: true` 并选在收盘后执行，预期耗时数小时。
4. 更新窗口内 `/api/health` 仍会 503（D12），本次未改变该行为。

## 8. 依据

- 代码：`app/core/update.py`、`app/cli/main.py`、`config/default.yaml`
- 测试：`tests/regression/test_incremental_price_lineage.py`、`tests/regression/test_cli_auto_update_logging.py`
- 运行证据：`.planning/2026-10-20-update-speed-diagnosis/before.json`（修复前进度快照）
