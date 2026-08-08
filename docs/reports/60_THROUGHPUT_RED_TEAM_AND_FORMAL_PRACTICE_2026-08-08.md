---
title: 启动与价格吞吐红队复审及正式实践报告（2026-08-08）
status: approved
category: reports
created: 2026-08-08
last-reviewed: 2026-08-08
supersedes: reports/59_STARTUP_READINESS_AND_PRICE_THROUGHPUT_2026-08-08.md
---

# 启动与价格吞吐红队复审及正式实践报告

## 1. 红队裁决

初审发现并关闭以下问题：

1. 外层 `wait(timeout=45)` 不能终止源内分页线程。现将 monotonic deadline 下传腾讯、TDX、BaoStock；每页/连接读取剩余预算。
2. 超时请求仍在运行时，`AdapterManager.close()` 可能与 BaoStock socket 并发 logout。现 `close()` 与 fetch/relogin 共用 `_fetch_lock`。
3. 写锁期间 summary 缓存 TTL 到期会重新全量聚合。现有效写锁期间允许 stale 缓存持续使用；死亡 PID 锁不再标记 active；缓存按数据库路径隔离。
4. 启动维护线程原先早于 uvicorn bind。现由 FastAPI lifespan 在服务启动后触发。
5. readiness SQLite 缓存原先无年龄上限。现超过 24 小时回到 `checking` 并后台复核。
6. TDX contextmanager 在 body 抛错时二次 yield，产生 `generator didn't stop after throw()`。现只捕获建连失败，body 异常正常向上传播并关闭连接。

最终代码门禁：S1 **464 passed**；Ruff；前端 lint、55 Node + 20 组件测试、build 全通过。

## 2. 正式启动实践

实践前无存活服务；发现上一轮死亡 PID 锁与 job#29 running，已通过 `data reconcile_jobs` 双阶段计划安全结算。自动更新在启动测量时保持 disabled。

| 场景 | health | readiness 结果可见 | 说明 |
|---|---:|---:|---|
| 首次缓存 miss | 1.616s | 约 19.55s | 1.64s 起显示 `checking=true`；后台完成后发布正式 BLOCK 结果；维护 34.263s 完成 |
| 二次缓存命中 | 1.682s | 1.731s | 首次 readiness 响应即 `cached=true/checking=false` |

结论：服务秒级可用、二次启动核对结果秒开成立；正式库真实 `ready=false`，所以 `/api/readiness` 正确保持 503，没有假阳性。

## 3. 正式价格任务实践

全部使用正式 CLI、有界 `--max-stocks`，数据库写仍为主线程串行；未运行 universe/公告/财务/全量 retry。

| 批次 | 价格成功/失败 | 指标成功/失败 | 价格耗时 | 实测速率 | 并发 |
|---|---:|---:|---:|---:|---:|
| 50 | 50/0 | 50/0 | 24.297s | 123.47 股/分 | 8 |
| 100（含 API live 采样） | 100/0 | 100/0 | 45.462s | 131.98 股/分 | 8 |
| 150（lineage 修复验证） | 150/0 | 150/0 | 67.964s | 132.42 股/分 | 8 |
| 150（旧缺口恢复验证） | 150/0 | 150/0 | 70.481s | 127.69 股/分 | 8 |

100 股批次 live API 实测速率稳定在约 124–158 股/分，ETA 从约 39 秒收敛到 1 秒；首点抖动已修为至少第二个样本后显示估算。样本合计 450 股、失败 0；未达到连续 5 分钟，所以并发 16 未触发，符合保守策略。

## 4. 实践发现的阻断回归

前两批 150 股更新后，`snapshot_price_coherence` 改善 150，但 `lineage_coverage` 从 583 恶化到 733。根因是新批量价格路径只保存 batch/archive，readiness 同时要求最新 `latest_close` 字段审计。

修复：

- 每股 raw/qfq 仅增加最新收盘价一条 `source_audit`，不恢复全历史逐行审计。
- 价格选股增加最新 raw/qfq 缺少 `latest_close` 审计的判定，使旧缺口可通过正常续传恢复。
- 两个 150 股正式修复批次后，lineage 先 `733→583`，再 `583→433`；证明不再制造新缺口且可修复旧缺口。
- 全程 `invalid_hash/orphan_batch/archive_gap/hash_mismatch` 均为 0。

## 5. 当前正式数据裁决

**BLOCK，不得宣称正式库已 ready。** 最终证据显示：

- `snapshot_price_coherence=3196`
- `lineage_coverage=433`
- `price_freshness=2`、`financial_period=4`、`share_capital=2`
- `retry_list=10001`、running_jobs=0
- 唯一 warning 为 `MINIMUM_DATA_NOT_READY`

此次实践确认新流水线性能与恢复机制通过，但只改善了 450 股，未完成全市场数据修复。自动更新恢复为 disabled，服务已停止，避免会话结束后无监督写库。

## 6. 证据

- `docs/evidence/evidence-throughput-practice-pre-20260808.json`
- `docs/evidence/evidence-throughput-practice-post-20260808.json`
- `docs/evidence/evidence-throughput-practice-final-20260808.json`
- `docs/evidence/evidence-throughput-practice-final2-20260808.json`
