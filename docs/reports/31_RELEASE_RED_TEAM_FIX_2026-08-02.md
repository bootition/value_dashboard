---
title: 发布级红队 P0 修复报告（2026-08-02）
status: approved
category: reports
created: 2026-08-02
last-reviewed: 2026-08-02
---

# 发布级红队 P0 修复报告（2026-08-02）

> 关闭 `.planning/2026-08-01-release-red-team/findings.md` 的全部 6 项 P0
> （含 08-02 复测确认的 5 项未修复项）。逐项给出修复位置与对应回归测试；
> 门禁全绿：S1 **374 passed**（较修复前 +22）、Ruff 零错误、前端 lint/52 合约/build、
> `uv lock --locked`。

## 关闭清单

| 编号 | 问题 | 修复 | 回归测试 |
|---|---|---|---|
| P0-1 | CSRC 适配器（同名 `cninfo`）覆盖 CNINFO 公告/分红适配器 | `CSRCIndustryAdapter` 更名 `cninfo_csrc`（`app/core/adapters/csrc_industry_adapter.py`）；`manager.py` KNOWN_ADAPTERS/`csrc_industry` 优先级/限流表/注册同步；`base.py` SourceName literal 增加 `cninfo_csrc` | `tests/regression/test_adapter_name_collision.py`（4 项：并存、announcements 路由、csrc 路由、优先级声明） |
| P0-2 | 增量更新遗漏股票池/上市状态/股本/CSRC 更新（PRD §7.7 第 4 项） | `run_incremental_update` 新增 `universe` 步骤：`stock_list`+`listing_info` 每次刷新，`csrc_industry` 低频节流（默认 30 天，`config/update.csrc_refresh_interval_days` 可配），刷新时间戳持久化到新 `data_refresh_state` 表（schema v14）；各子步骤独立降级不影响价格/财务 | `tests/regression/test_incremental_update_scope.py`（7 项：步骤执行、降级、节流、标记写入）；`test_announcement_check.py` 既有 4 处补 stub |
| P0-3 | 自动更新持久化状态与执行器不一致（disable 后新控制器复活） | `AutoUpdateController` 构造时 `_load_persisted_state()` 采纳 SQLite 持久化状态；`VALID_STATES` 补 `enabled`；崩溃遗留 `running` 标记重置为 idle | `test_auto_update_controller.py` +4（跨实例 disable/pause 持久、enable 复活、崩溃标记重置） |
| P0-4 | 正式库因 603435 混期全部筛选不可用 + ready=true 假阳性 | 统一"最新完整三表期"判定：`data_quality.snapshot_period_mismatches()` 为门禁与引擎共用；`minimum_data_readiness` 新增阻断项 `snapshot_period_alignment` + 披露项 `pending_financial_period`（PRD §7.7 数据源未就绪）；引擎 `_reject_mixed_report_dates` 复用同一查询 | `tests/regression/test_complete_period_policy.py`（7 项：pending 不阻断、真混期双阻断、对齐披露、计算取完整期、发布推进） |
| P0-5 | 快照发布门禁被"部分新财务+旧快照"永久阻断 | `IndicatorCalculator._get_latest_financials` 要求三表核心字段齐备（与门禁/引擎同口径），快照期=完整期；新期缺字段属披露项不阻断发布 | 同上（`test_snapshot_publish_proceeds_at_complete_period_despite_pending_rows` 等）；`test_indicator_data_quality.py`/`test_manual_overrides.py` fixture 补 cash_flow |
| P0-6 | 一键启动 `start.bat` 无法执行（`'EM' 不是内部或外部命令`） | 重写为纯 ASCII + CRLF + 无 BOM；消除块内 `%VAR%` 解析期展开（RELEASE_ROOT 提前赋值）与块内未转义括号 echo 两个潜伏解析错误；`vd.bat` 行尾统一 CRLF；`scripts/build-release.ps1` 增加真实启动 smoke（经 start.bat 拉起 EXE、120s 内 `/api/health` 200、taskkill 清理、重建禁止数据检查） | `test_release_entrypoint.py` +2（ASCII/无 BOM/CRLF 断言；`cmd /c start.bat` 实测到达打包分支且无 `'EM'` 误解析） |

## 顺带修复

- **`scripts/s1-pytest.ps1` 参数绑定 bug**：`$PytestArgs` 未声明 `Position`，首个位置参数（如 `tests/regression/test_x.py`）被误绑到 `$EvidenceDir`，导致证据目录建在 `tests/regression` 下并报 "Cannot create ... already exists"；声明 `Position = 0` 修复。历史误建的 `tests/regression/<hash>/` 证据目录已清理。

## 验证（2026-08-02）

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression -q --no-header` | **374 passed**（修复前 352） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| 前端 lint / test / build | 通过 / 52 passed / 构建成功 |
| `uv lock --locked` | 通过 |
| `start.bat` 实测（隔离目录 cmd /c） | 打包分支可达、`data\logs\start.log` 生成、无 `'EM'`/语法误解析 |

## 剩余开放项（非本次 P0 范围，来自同一审查的 P1）

跨进程更新互斥、`vd.bat` dist 目录优先、真实性能基准、前端浏览器 E2E、
CSRC 初始化性能（节流已缓解自动更新路径）、数据状态页各数据域日期、
自动更新进度模型、增量更新 job_logs、STATUS 与正式库一致性验收。
