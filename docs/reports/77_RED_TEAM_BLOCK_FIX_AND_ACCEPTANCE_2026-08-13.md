---
title: 红队 BLOCK 修复与验收（reports/76 全部发现关闭）
status: approved
category: reports
created: 2026-08-13
last-reviewed: 2026-08-13
supersedes: 76_SYSTEM_RED_TEAM_USER_FLOW_REVIEW_2026-08-13.md
---

# 红队 BLOCK 修复与验收（reports/76 全部发现关闭）

> 本文档为 `reports/76`（BLOCK 发现基线）的修复与验收报告。修复后经
> S1 完整回归（586 passed）、ruff、前端门禁与真实服务更新窗口实测，
> **裁决更新为 PASS（可正式研究）**。

## 裁决

**PASS / 可正式研究**。reports/76 的 1 项 P1 与 6 项 P3 全部修复并验证；
过程中新发现 2 个环境/基线问题（akshare 缺失、ruff 规则集漂移）一并修复。
正式库数据完整性未受影响（S1 前后哈希一致）。

## 修复清单

### P1：自动更新窗口内核心研究链路失效（已关闭）

| # | 修复 | 实现 |
|---|---|---|
| P1-1 | DuckDB 连接竞态 500 | `duckdb_store.py read_connection`：指数退避（0.25s→3s 封顶，12 次约 28s 窗口），写锁活跃首轮额外 +2s |
| P1-2 | 更新窗口 30~60s 响应 + 全遮蔽 | `data_quality.read_warning_codes`：写锁活跃时跳过全量重建（返回 stale 缓存/空集）；TTL 过期改为 **stale-while-revalidate**（立即返回旧值 + 后台单飞重建线程），热路径永不等待全量扫描 |
| P1-3 | treasury-comparison >60s 超时 | `stock_detail.py`：分红改为单次全量读取 + 内存滑动窗口（窗口语义与原逐日 SQL 完全一致），曲线对齐改用 `align_many` 批量；实测 0.2s（原 >60s） |
| P1-4 | screening 门禁 500/74s | `screening.py`：写锁活跃时直接返回 409 `auto_update_in_progress`（跳过重型一致性扫描）；前端 api-error.ts 新增对应提示 |

### P3（全部关闭）

| # | 修复 |
|---|---|
| P3-1 | qfq 历史负值（1993-2018，42 只）→ 本报告披露；数据为 TDX 减法复权真实口径，不改 |
| P3-2 | 正式库残留 `research_statistics_staging_*`（196,178 行）→ `statistics.py` 新增 `_cleanup_staging_tables()`（重建前清扫）+ 已执行一次清理（残留 0） |
| P3-3 | 未知 `/api/*` 返回 SPA HTML → `main.py serve_spa` 对 api 前缀返回 404 JSON |
| P3-4 | 规则保存不校验字段 → `engine.validate_rule_fields()`（模块级，保存时校验 field/right_field/sort/columns，与运行时同口径）+ `screening.save_rule` 调用 |
| P3-5 | static assets 累积 852 文件 → 删除孤儿 staging 目录 + 清空重建（dist/static 各 29 文件，index.html 引用一致） |
| P3-6 | watchlist remove 无反馈 → 返回实际删除行数，0 行 404 |

### 过程中新发现（同批修复）

| # | 发现 | 修复 |
|---|---|---|
| N1 | **`.venv` 缺 akshare/baostock/easy-tdx**（`uv sync --locked` 默认不含 optional-dependencies；`start.bat`/`vd.bat` 优先用 .venv → 依赖 akshare 的数据源在用户路径不可用，`research_statistics` 步骤报 `No module named 'akshare'`） | `uv sync --locked --all-extras` 补齐（akshare 1.18.81 与 vd.bat 注释一致）；README 待补充 extras 说明 |
| N2 | **ruff 0.16.0 默认规则集扩展**导致 532 个存量违规（非本次改动引入；项目门禁基线为传统 E4/E7/E9/F） | `pyproject.toml` 显式锁定 `[tool.ruff.lint] select = ["E4","E7","E9","F"]`，门禁命令保持 AGENTS.md 原样 |

## 门禁与实测证据

- **S1 完整回归**：586 passed（新增 `test_redteam_2026_08_13_fixes.py` 12 项：连接重试、写锁感知×2、stale-while-revalidate、筛选门禁 409、treasury 批量语义、staging 清理、/api 404、字段校验×2、watchlist×2）；正式库前后/清理后哈希一致
- **ruff**：全绿（规则集显式锁定后）
- **前端**：lint、55 Node + 40 vitest、build（vue-tsc 类型检查）全绿
- **真实服务更新窗口实测**（08-13，价格步 5,537 只进行中）：
  - `indicators`：**0.1s**（修复前 43~67s），`auto_update_in_progress=true`，指标值正常显示（不再遮蔽）
  - `treasury-comparison`：**0.2s**（修复前 >60s 超时）
  - `screening/run`：**409 即时返回** `auto_update_in_progress`（修复前 500 或 74s）
  - `watchlist`：0s；health 200
- **崩溃恢复**：修复验证中两次强杀服务，锁文件均死锁可回收、数据零畸形

## 正式库现场收尾

- 08-12 价格已补 1,554 只（两次启动累积），后续轮次有界续传
- 测试规则 0 残留、无孤儿行；staging 表 0 残留
- 自动更新阶段观察：`prices/universe/financials/market_actions/treasury_curve` partial（均如实进 retry/missing），`capital_history` failed（CNINFO 冷却期，披露项）、`business_overview` success、`research_statistics` 在 akshare 补齐后成功

## 遗留观察（不阻塞）

1. `test_data_status_cache::test_dead_update_lock_does_not_mark_summary_stale` 在完整 S1 中偶发 `WinError 32`（unlink 与后台刷新线程读锁文件的 Windows 竞态），单跑与重跑均通过；属既有测试时序竞态，非本次改动引入
2. 更新中断的中间态（价格已更新、快照未重建、锁已释放）下 readiness=false 为真实状态，快照重建随下一轮自动更新完成
