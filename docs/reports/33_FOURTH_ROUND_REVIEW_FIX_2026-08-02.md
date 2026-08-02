---
title: 发布级红队第四轮复测修复报告（2026-08-02）
status: approved
category: reports
created: 2026-08-02
last-reviewed: 2026-08-02
---

# 发布级红队第四轮复测修复报告（2026-08-02）

> 关闭 `.planning/2026-08-01-release-red-team/findings.md` 第四轮系统红队
> 复审（提交 030932f + f2535d8 之后）新发现的 **1 项 P1 + 2 项 P2**。
> 结论：P0（6 项）与 P1（12 项）全部关闭，复跑 `build-release.ps1` 退出码 0。

## 关闭清单

| 编号 | 问题 | 修复 | 验证 |
|---|---|---|---|
| P1 | **build-release.ps1 清理竞态**：smoke 后 `taskkill /T /F` 返回但 EXE 句柄未释放，立即 `Remove-Item data\` 抛错（start.log 被占用）→ 脚本非零退出且"Release must not package formal data"检查被跳过，发行目录残留空库 | `scripts/build-release.ps1` finally 块：taskkill 后轮询 `Get-Process value-dashboard` 直到进程树退出（60×500ms，超时 throw）；删除改为最多 20 次重试（500ms 间隔），仍失败则 throw | 模拟锁定文件竞态：锁定中删除失败重试、释放后删除成功；完整 build-release **退出码 0**，`dist/value-dashboard/` 无 `data/` 残留、无残留进程 |
| P2 | **首次 `data init` CSRC 全量约 2.3h**：断点续传只解决中断恢复，首次全缺失耗时不变 | `run_full_init(skip_csrc=...)` + CLI `vd data init --skip-csrc`：跳过 CSRC 并披露 `skipped_by_flag`，先行建立最小可用，后续由自动更新低频补齐（PRD §24） | `test_incremental_update_scope.py::test_full_init_skip_csrc_discloses_skipped`（跳过时适配器零调用） |
| P2 | **universe 步骤每轮 ~104s 网络开销**（stock_list 51s + listing_info 53s） | 新增 `update.universe_refresh_interval_days`（默认 1 天）按日节流；`_csrc_refresh_due` 泛化为 `_refresh_due(key, interval)` 复用同一机制；节流时披露 `refreshed_within_interval` | `test_incremental_update_scope.py` 新增 2 项（标记新鲜跳过、标记缺失/过期执行） |

## 验证门禁（2026-08-02）

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression -q --no-header` | **392 passed**（+3 新增） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| `scripts/build-release.ps1`（完整：S1 + 前端 ci/lint/test/build + PyInstaller + start.bat 真实启动 smoke + 清理 + 禁数据检查） | **退出码 0**；发行目录无 `data/` 残留 |
| 正式库只读（上轮） | ready=true、mismatches=0、筛选 451ms/3,878 只（`docs/evidence/evidence-formal-*20260802.json`） |

## 状态

- 发布级红队 P0（6）与 P1（12：10 项上轮 + job_logs/STATUS 复述项已含 + 本轮 1 项）全部关闭；
  2 项 P2 已按建议修复（`--skip-csrc` 最小可用 + universe 按日节流）。
- 剩余披露项不变（银行/券商监管字段、停牌、无分红、解禁时间差、退市股等，见 `reports/29`）；
  性能验收的"目标主机 host-spec 仪式步骤"仍属发布前人工执行项（脚本与隔离基准就绪）。
