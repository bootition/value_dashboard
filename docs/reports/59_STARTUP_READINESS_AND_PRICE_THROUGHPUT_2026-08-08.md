---
title: 启动 readiness 与价格吞吐自适应实施报告（2026-08-08）
status: superseded
category: reports
created: 2026-08-08
last-reviewed: 2026-08-08
superseded-by: reports/60_THROUGHPUT_RED_TEAM_AND_FORMAL_PRACTICE_2026-08-08.md
supersedes: reports/56_LAUNCH_FIX_AND_STARTUP_ANALYSIS_2026-08-07.md, reports/58_SOURCE_CALIBRATION_AND_CONCURRENCY_2026-08-07.md
---

# 启动 readiness 与价格吞吐自适应实施报告

## 1. 实施范围

1. 启动不再同步执行 `minimum_data_readiness`。无缓存时以 `checking=true/ready=false` 保守占位，后台核对后写入 SQLite `data_refresh_state`；后续启动直接复用缓存并后台复核。
2. 每股 raw/qfq 在独立请求池中并行，数据库仍由主线程批量串行提交，保持单股双口径原子性。
3. 单股双请求 45 秒未完成即记入 `retry_list`；BaoStock 标记为下次请求前强制重登。因 AkShare 价格调用无可控网络 timeout，已从价格 fallback 链移除，保留腾讯、BaoStock、TDX 三个有界源。
4. 适配器记录近 10 次响应耗时；出现 >30 秒长尾时将有效间隔逐级拉到 0.5/1.0 秒，连续恢复后每轮回落 0.1 秒至配置基线。
5. 价格外层从并发 8 起步，连续 300 秒无失败/长尾则 +4，最多 16；出现失败或长尾则回落到基线 8。
6. 自选列表全部股票作为研究优先名单，在 `--max-stocks` 截断前稳定前置；无需增加新的个性化字段。
7. 数据状态页显示实时速率和 ETA；完整 job 的价格耗时/速率写入 `job_logs.details_json` 与 `data_refresh_state.price_update_last_rate`。
8. `scripts/calibrate_source_rates.py --parallel N` 可按源并发校准，同一源的多个 interval 仍串行，避免污染实验。
9. 修复发行包默认配置漂移：与开发配置统一为腾讯主源、0.2/0.8 秒源间隔及 8→16 自适应参数。

## 2. 验证

- Ruff：`uv run --locked ruff check app tests/regression scripts/calibrate_source_rates.py` 通过。
- 后端 S1 隔离回归：458 passed（审查修复后复跑）。
- 前端：lint 通过；55 个 Node 测试 + 20 个组件测试通过；生产 build 通过。
- 新增回归覆盖：readiness 占位/缓存、raw/qfq 同时在途、45 秒 deadline 进入 retry、自选优先、ETA 字段。

## 3. 诚实披露

- 代码门禁与隔离行为已验证；未对正式 `data/` 执行写入或全市场长跑。
- “约 25-30 股/分”和“二次启动秒开”仍是待正式运行复验的预期，不在本报告中宣称为实测结果。
- 并发 16 只有连续稳定 5 分钟后才启用；正式 job 完成后应读取 `price_update_last_rate` 并运行并发校准，再决定是否调整默认参数。
