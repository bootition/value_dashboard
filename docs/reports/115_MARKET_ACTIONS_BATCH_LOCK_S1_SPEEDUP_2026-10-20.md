---
title: 分红除权批量提速、更新锁误判修复与 S1 门禁提速
status: approved
category: reports
created: 2026-10-20
last-reviewed: 2026-10-20
---

# 分红除权批量提速、更新锁误判修复与 S1 门禁提速（2026-10-20）

## 1. 结论（TL;DR）

| 问题 | 根因 | 修复后 |
|---|---|---|
| 分红除权 535 只耗时约 76 分钟（7 只/分） | 逐股 `refetch_one` → 每次重建 TDX 连接（`from_best_host` 实测 **5.02s**）＋每股一次事务/审计 | 按 50 只/批在同一会话内抓取、每批一次事务与一次批量审计，预计 **5–8 分钟** |
| 分红失败被静默丢弃 | `_refresh_market_actions` 只回填 `failed_codes`，不写 retry_list | 失败按数据域写 retry_list，下一轮由 `_retry_failed_tasks` 消费 |
| 子进程读连接偶发失败（15:59–16:00 共 62 条） | 本进程持更新锁时，只读连接回退为读写连接后撞上 Web 只读连接，被误判为"外部写进程"立即失败 → `_get_latest_financial_report_date` 返回 None | 新增 `update_lock_owned_by_current_process`，本进程属主时退避重试 |
| S1 门禁每轮固定开销 8.5 分钟以上 | 5 次捕获 × 每次 2 遍全量 SHA-256（19GB 正式库）＋ data/ 全树含 41GB 回滚快照与日志 | 每轮 **2 次**全量哈希＋元数据核对；97 项用例轮次实测 **5.4 分钟**（小集合 1m29s） |

## 2. 分红/除权批量化（app/core/update.py、app/core/init.py、config/default.yaml）

### 2.1 证据

- `_refresh_market_actions` 原先逐股调用 `refetch_one(code, "dividends"/"xdxr")`；
  xdxr 走 `_fetch_xdxr` → `_tdx_session()` → **每次新建** `TdxClient.from_best_host()`。
- 实测（本机 2026-10-20）：`from_best_host` **5.02s**；同一会话内第一次 xdxr 查询 0.16s、
  第二次 **0.02s**；akshare 分红 491 次平均 **0.23s**。
- 当日 `market_actions` 535 只、7.0 只/分 ≈ 8.6s/只（5.2s 建连 + 0.2s 限速 + 0.2s 查询
  + 约 3s 落库/溯源），整步约 76 分钟。

### 2.2 修复

- 新增 `update.market_actions_batch_size`（默认 50）。
- `_refresh_market_actions` → 按批 `_refresh_market_actions_chunk`：
  - `_fetch_market_action_batch`：一个数据域一次 `FetchRequest(stock_codes=[...50])`，
    TDX 会话复用、行按 `stock_code` 分组；
  - `_persist_market_action_batch`：**一个事务**内完成多股 upsert + 单条 fetch_batch +
    一次批量审计写入（`DataInitializer._record_field_audit_bulk_in_connection`）；
  - 批内缺席/行不可用/批量落库失败的股票回退 `refetch_one` 逐股，保留精确失败归因。

## 3. 分红失败不再静默（app/core/update.py）

- 失败按 `data_type` 调 `_record_failure` 写入 `retry_list`，`_retry_failed_tasks`
  已有 dividends/xdxr 分支可直接消费。
- 背景：2026-10-20 当日 `301688`（akshare KeyError → baostock 登录失败 → cninfo 不支持）
  与 1 只 TDX `xdxr_info 非法 market 值: 208` 失败后无人重试。

## 4. 更新锁误判修复（app/core/storage/update_lock.py、duckdb_store.py）

- 现象：财务刷新并发时出现 62 条
  `IO Error: Cannot open file ...: 另一个程序正在使用此文件`。
- 机制：`read_connection` 遇到 "different configuration" 后回退读写连接；该连接与
  Web 进程的只读连接争用文件锁，Windows 报同一句占用文案，`_is_external_file_lock`
  因本进程持有更新锁（`lock_active=True`）判定为外部写进程 → 立即抛
  `DuckDBReadLockedError`。调用方 `_get_latest_financial_report_date` 吞掉异常返回 None，
  最坏把"有本地报告期"误判成"无本地数据"而全量重拉财务。
- 修复：`update_lock_owned_by_current_process()` 解析锁文件 pid；本进程即属主时
  文件锁冲突按退避重试处理（最多 12 次、单次 ≤3s），并取消首轮额外 2s 延迟。

## 5. S1 门禁提速（scripts/s1-path-preflight.ps1、scripts/s1-pytest.ps1、conftest.py）

### 5.1 原有冗余（实测）

一轮 S1 的正式状态捕获：

1. wrapper `Before`（19.3GB 具名文件 + data/ 全树，含 **41GB** `.old-*` 回滚快照与日志）；
2. conftest 内层 `pre`（再哈希 19.3GB）；
3. conftest 内层 `post`（再哈希 19.3GB）；
4. wrapper `After`（19.3GB + 全树）；
5. wrapper `after-cleanup`（19.3GB + 全树）。

且 `Get-FormalState` 每次捕获都做 **两遍**全量哈希做稳定性核对 → **10 次全量读取**，
约 190–600GB/轮，固定开销 8.5 分钟以上（更早一轮实测 13 分钟）。

### 5.2 修复（保证不变或仅按设计弱化）

| 改动 | 说明 |
|---|---|
| data/ 树内容哈希排除 | `.old-*` 回滚快照、`logs/`、`tmp/`、`pdf/`、`valuedashboard.duckdb.tmp/` 只记录存在性/长度/修改时间（回滚快照不是正式状态） |
| 稳定性第二遍改元数据 | 第二遍只核对 exists/length（树另加 mtime），不再重复 SHA-256 |
| 内层证据复用 | conftest `pre` 直接复用 wrapper `before` 证据（`VD_S1_BASELINE_FINGERPRINT`）；`post` 由 wrapper 在 `after` 捕获后写出（`VD_S1_WRAPPER_OWNS_POST=<formal data root>`）；合成哨兵测试仍走真实哈希 |
| cleanup 复核对 | `after-cleanup` 捕获与比对使用 `-MetadataOnly`（清理只删除测试运行树） |

结果：全量哈希 **10 次 → 2 次**（before/after 各一次 19.3GB）。

### 5.3 实测

| 轮次 | 用例 | pytest | 整轮墙钟 |
|---|---|---|---|
| 改前 | 90 | 210.5s | 约 8.5 分钟 |
| 改后 | 97 | 218.9s | **约 5.4 分钟** |
| 改后（小集合） | 11 | 2.8s | **1m29s** |

证据目录 `docs/evidence/evidence-s1/44d19dbf...`：`delta_detected=false`、
post-cleanup delta=false、内层 pre/post 来源分别为 wrapper before/after 证据。

## 6. 验证与门禁

- S1：97 passed（`test_market_actions_batch`、`test_update_lock_ownership`、
  `test_announcement_check`、`test_field_audit_ingestion`、`test_incremental_price_lineage`、
  `test_retry_persistence`、`test_cli_auto_update_logging`、`test_incremental_update_scope`、
  `test_update_job_and_progress`、`test_hash_preservation`）。
- Ruff 全绿；正式库前后指纹一致（`delta_detected=false`）。
- 服务重启后 `/api/health` 200，自动更新继续推进。

## 7. 残留与建议

1. 每轮 S1 仍需 2 次 19.3GB 全量哈希（约 90s）；这是"正式库未被测试改变"的最低成本，
   再压缩只能抽样/纯元数据化，不建议。
2. 批量分红/除权路径尚未在真实分红公告批次上跑过（下一个分红公告日验证）；
   单元测试覆盖"批内命中 / 批内缺席回退 / 失败写 retry"。
3. 盘后价格步骤约 69 只/分（下午 148 只/分），已排除网络因素（腾讯实测 0.17–0.25s），
   指向落库/溯源写路径，待单独分析。
4. `data/valuedashboard.duckdb.old-20260904013322`（41GB）仍占用磁盘；删除前需按
   AGENTS.md 登记保留依据。
