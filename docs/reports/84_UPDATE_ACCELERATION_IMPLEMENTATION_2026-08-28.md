---
title: 自动更新与指标重算提速实施报告
status: approved
category: reports
created: 2026-08-28
last-reviewed: 2026-08-28
supersedes: .planning/2026-08-28-update-acceleration/task_plan.md
---

# 自动更新与指标重算提速实施报告（2026-08-28）

## 裁决

**PASS（实施完成并在正式库验证）。** 自动更新与指标快照重算互斥、指标重算增量化与批提交、价格抓取连续流水线、腾讯 HTTP 连接复用、历史统计域多进程重建全部落地。正式库本轮更新后：`minimum_data_readiness.ready=true`、`warning_codes=[]`、`retry_count=0`、价格新鲜度 5,544/5,544（2026-08-27 目标日），快照价格一致性缺口从 5,541 收敛到 17 只既有披露项（7 天内新股/核心数据未形成）。

## 背景

- 全量指标重算（5,533 只）与自动更新价格抓取并发执行时，DuckDB 单写者竞争把价格更新拖到 5-8 只/分、内存峰值约 6.5GB。
- 瓶颈是写锁等待、HTTP 长尾请求和全表 staging 回写，不是 CPU 算力（此前已确认 CPU 未打满）。
- 用户要求：实施互斥、增量重算、批提交、动态速率控制，并文档化；不得要求用户执行终端命令。

## 实施内容

### 1. 自动更新与指标重算互斥
- `app/web/api/data_status.py`：手动指标重算复用跨进程增量更新锁
  `exclusive_update`；启动前同时检查 `auto_update_state.current_stage`
  与锁文件，冲突返回 409。自动更新已持锁时，重算线程直接让路；
  重算已持锁时，自动更新收到 `another_update_running` 并跳过，不产生双写。

### 2. 指标重算增量化 + 批提交
- `compute_snapshot_for_codes`：从“全市场复制到 staging → 整表回写”改为
  单事务 `DELETE 受影响股票 + INSERT 新值 + 校验 + lineage`。计算失败的
  股票保留上一代快照，不再被删除；任一步失败整体回滚。
- `compute_snapshot_for_all`：关闭读连接后按 batch（默认 100）分批写
  staging，最终仍单事务发布，原子性不变。
- 自动更新主流程：少量财报刷新只增量重算“价格变化 + 财报刷新 + 快照
  日期落后”的并集；只有股本/上市名单变化才全量重算。
- 细粒度进度：`compute_snapshot_for_all/codes` 支持 `progress_cb`，
  自动更新把指标计算进度接入状态页 live 进度。

### 3. 价格抓取连续流水线
- `app/core/update.py`：固定窗口（`concurrency×4` 只一批、全窗等待）改为
  在途股票任务连续流水线（默认深度 64）。慢股票只占用一个槽位，
  其他槽位继续补位，45s 长尾股票不再拖住整窗。
- 自适应并发保留：8→16、步长 4、连续 300s 无惩罚扩容；执行器 worker
  数随并发档位调整。
- 配置新增 `update.price_fetch_pipeline_depth: 64`。

### 4. 腾讯 HTTP 连接复用与动态限速
- `TencentAdapter` 使用线程本地 `requests.Session` + 连接池
  （`pool_maxsize=8`、`max_retries=0`），避免每次请求重复 TLS/建连；
  更新结束统一 `close()`。
- 既有动态请求间隔继续生效：单次响应 >30s 且窗口内 ≥2 次长尾则升档，
  稳定快响应逐步回落到源基准（腾讯 0.2s）。

### 5. 历史统计域多进程重建
- `update.research_statistics_parallel_workers`（默认 4）：输入指纹变化时
  历史统计域用进程池重建（只读分析，发布仍主进程原子完成）。串行路径
  在正式库超过 14 分钟仍未完成，多进程路径完成 5,550 只/222,000 条记录。

### 6. 状态页自动化
- `DataStatusPage`：自动更新从 running 转非 running 时，若存在数据修复
  登记的 pending 快照股票，自动 POST `/api/data-status/indicator-recompute`；
  无需按钮或终端命令。`/indicator-recompute` 响应缺失 `pending_codes` 时
  前端归一化，避免模板报错。

### 7. job_logs 如实保留 partial
- `_finish_job` 不再把 partial/skipped 一律写成 failed。状态页
  “最近一次更新执行”如实显示 partial，并仍单独保留 `last_full_success_at`。

## 正式库验证（2026-08-28）

| 指标 | 结果 |
|---|---|
| 价格抓取（新流水线，剩余 1,704 只） | 成功 1,704、失败 0；实测约 57-148 只/分，约 30 分钟完成 |
| 旧流水线同任务（并发竞争时） | 实测约 5-23 只/分，ETA 曾达约 2.5 小时 |
| 价格新鲜度 | target 2026-08-27；active 5,544，raw/qfq current 5,544/5,544 |
| 指标快照 | 5,533 只；本轮全量重算 5,533/5,533 成功（此前快照整体落后，需一次追赶） |
| 历史统计域 | 5,550 只 / 222,000 条记录，version 10，failed=[] |
| 数据质量 | `ready=true`、`warning_codes=[]`、`retry_count=0` |
| `snapshot_price_coherence` | 5,541 → 17（均为 7 天内新股/核心数据未形成披露项） |
| 最近一次更新执行 | 2026-08-28 03:49 local，`partial`（universe 子步骤按既有语义降级） |
| 互斥验证 | 更新运行中 POST `/api/data-status/indicator-recompute` 返回 409 |

## 门禁

- Ruff：`app tests/regression` 全绿。
- 前端：eslint 全绿；`npm run build` 全绿；Vitest 11 files / 56 tests 全绿。
- S1 隔离回归：`test_snapshot_atomicity` + `test_update_job_and_progress`
  31 passed；`docs/evidence/evidence-s1/<run>/hash-evidence.json`
  `delta_detected=false`（正式库前后指纹一致）。

## 剩余与风险（诚实披露）

1. **DuckDB 文件高水位**：正式库文件 16.7GB，逻辑表数据远小于此；
   主因是全量快照/统计多次“staging → DELETE 全表 → INSERT”留下的高水位，
   `source_audit` 3,641 万行及其复合索引占主要空间。`CHECKPOINT` 已执行；
   DuckDB 1.5.5 默认 `VACUUM` 未物理收缩文件。后续需离线压缩/重建评估，
   期间查询功能正常。
2. **universe 步骤 partial**：本轮 `stock_list`/`listing_info` 仍按既有
   披露语义降级，不阻断价格/财务/快照；retry=0，下一轮自动更新继续重试。
3. 全量快照重算在“旧快照整体落后于价格”时仍会触发一次（本轮已完成追赶）；
   之后财报刷新已改为增量路径。
4. 连续流水线使用 `ThreadPoolExecutor._max_workers` 运行时调整（CPython
   稳定行为）；如未来版本变化，回退为固定窗口实现。
5. 历史统计域多进程路径内存峰值约 5.8GB（4 worker × ~190MB + 主进程记录
   聚合），任务结束回落到约 0.7GB；本机 32GB 内存下安全。
