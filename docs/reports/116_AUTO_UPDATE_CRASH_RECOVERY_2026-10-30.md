---
title: 自动更新子进程原生崩溃与“必须重启才能继续”恢复修复
status: approved
category: reports
created: 2026-10-30
last-reviewed: 2026-10-30
---

# 自动更新子进程原生崩溃与“必须重启才能继续”恢复修复（2026-10-30）

## 1. 结论（TL;DR）

| 观察 | 根因 | 处理 |
|---|---|---|
| 2026-10-29 13:23 自动更新日志无 traceback 突然停止 | 更新子进程 `python.exe` 在 `ucrtbase.dll` 原生 fail-fast：`0xc0000409`（BEX64），WER 记录 `AppCrash_python.exe_...`；业务日志最后一条停在 `akshare_eastmoney 成功获取 dividends: 510 行`，第 3 秒进程死亡 | 子进程 CLI 启用 `faulthandler`，下次原生崩溃可落 Python 调用栈 |
| 停止后 `auto_update_state.current_stage` / `job_logs.status` 永久为 `running`，`start.bat` 再打开只返回 `already_running` | 原生崩溃不会执行 `run_once` 的异常/finally；旧 trigger 先看持久化 stage，再看锁；只有重启时 `_reconcile_crashed_incremental_jobs()` 才会结算 | 新增 `AutoUpdateController.recover_interrupted_run()`；父进程在子进程非零退出后立即结算；trigger 在“无活跃子进程 + 无活锁”时先恢复再触发 |
| 首轮更新崩溃/挂死后，收盘后调度器也永远不启动 | `_run_startup_maintenance()` 里 `subprocess.run` 是阻塞调用，`_start_auto_update_scheduler()` 写在其后；子进程不返回，启动线程就永远走不到调度器 | 调度器先于首轮子进程启动；进程内 child guard 防止重复 spawn |
| 价格已到目标日但 retry_list 仍有到期任务，调度器不再运行 | `auto_update_is_due()` 只看 `MAX(price_daily_raw.trade_date)` | 到期 retry 任务也作为 due 条件；`retry_count < max_retries` 且 `next_retry_at <= now` |
| 页面显示“上次成功 2026-09-11” | 10 月各轮均为 `partial`；`last_success_at` 只在 `success` 时更新，这是语义正确但容易误读 | 本次不改语义；页面已有“最近结果/最近执行”，建议后续把标签改成“上次完整成功” |

## 2. 日志证据

### 2.1 正常完成/环境停机（不是程序停更）

- `data/logs/start.log`：2026-10-20 22:58:25 启动更新子进程；Windows 于 23:51:38 正常关机（System 事件 109/13），服务直到 2026-10-29 10:55:20 才再次启动。该 8 天无更新是环境停机。
- 2026-10-29 16:47:32 子进程 `exit=0`，job 193 `partial` 完成；22:05 的重启不是恢复故障，而是用户再次启动。
- 2026-10-30 00:18:45 子进程 `exit=0`，job 194 `partial` 完成；之后到 09:38 前未运行是 due 判定为空闲窗口，不是卡死。

### 2.2 真实崩溃

- `data/logs/auto-update-child.log` 最后一条：`2026-10-29 13:23:38,714 ... akshare_eastmoney 成功获取 dividends: 510 行`；此后该次运行无任何输出。
- Windows Application 事件：
  - `2026-10-29 13:23:41` Id=1000 `Application Error`：`python.exe`，faulting module `ucrtbase.dll`，exception `0xc0000409`，offset `0xa527e`。
  - `2026-10-29 13:23:51` Id=1001 `Windows Error Reporting`：`EventType=BEX64`，`P1=python.exe`，`P8=c0000409`，`P9=0000000000000007`。
- 崩溃进程创建时间 `2026-10-29 10:56:15`，正是 job 192 的更新子进程。
- `data/valuedashboard.sqlite`：job 192 滞留 `running`，直到 13:40:05 新进程启动时才被 `dead_update_lock_owner` 结算；这是“必须重启”的直接因果链。

## 3. 修复

1. `app/cli/main.py::_configure_update_logging()`：启用 `faulthandler`（`all_threads=True`，写 stderr），用于捕获下一次 `0xc0000409` 等原生致命错误。
2. `app/core/auto_update.py::recover_interrupted_run()`：将无属主但持久化为 `running` 的控制器状态恢复为 `idle`，保留 `progress.status=failed` 与失败原因。
3. `app/web/main.py`：
   - 新增进程内 child guard（`auto_update_child_is_active`）；
   - 子进程非零退出后立即调用 `recover_interrupted_auto_update()`；
   - scheduler 前移到首轮阻塞更新之前；
   - `auto_update_is_due()` 增加到期 retry 任务判定。
4. `app/web/api/data_status.py::trigger_auto_update()`：只在“进程内子进程活跃”或“锁属主仍存活”时拒绝；无活锁却残留 `running` 时先恢复再触发，避免 `start.bat` 永久 `already_running`。

## 4. 验证

- 定向隔离回归（`VD_ENV=test`，临时库，正式库不参与）：
  `test_auto_update_controller.py`、`test_auto_update_progress.py`、
  `test_auto_update_scheduler.py`、`test_cli_auto_update_logging.py`、
  `test_data_status_cache.py`、`test_update_job_and_progress.py`、
  `test_update_lock_ownership.py`、`test_web_composition_root.py`：
  **71 passed**。
- Ruff：通过。
- 官方 S1 全量未在更新子进程持锁期间执行（避免正式库指纹被真实更新污染）；待下一更新窗口结束后补跑。

## 5. 残留风险

1. 2026-10-29 原生崩溃的具体 C/C++ 调用栈仍未知；`faulthandler` 只对下一次复现生效。最后一条业务日志落在新批量分红/除权路径（reports/115 的未验证路径），下一次分红公告批次需重点观察。
2. 子进程若“活着但永久卡死”（锁仍被活进程持有），当前恢复逻辑不会杀它；仍需要人工干预或后续 heartbeat/watchdog。
3. `last_success_at` 在 partial 轮次保持旧值是刻意语义；UI 文案“上次完整成功”宜作为后续小改动。
