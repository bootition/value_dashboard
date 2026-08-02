---
title: 发布级红队 P1 修复与正式库验收报告（2026-08-02）
status: superseded
category: reports
created: 2026-08-02
last-reviewed: 2026-08-02
superseded-by: reports/34_SYSTEM_RED_TEAM_REVIEW_2026-08-02.md
---

# 发布级红队 P1 修复与正式库验收报告（2026-08-02）

> 关闭 `.planning/2026-08-01-release-red-team/findings.md` 列出的全部 P1
> （含三轮回测新发现的"股本刷新未触发快照重算"），并对正式库执行当前
> 可复现的只读验收，消除 STATUS 与正式库的矛盾。
> 门禁：S1 **389 passed**、Ruff 零错误、前端 lint + 52 node 合约 + **9 项
> 组件流程测试**、build、`uv lock --locked`。

## P1 关闭清单

| 问题 | 修复 | 回归测试 |
|---|---|---|
| 增量更新不写 job_logs（状态页"最近更新"失真） | `run_incremental_update` 记录 job_logs running→success/failed（job_id/started_at/details_json），`_finish_job` 非 success 一律 failed | `test_update_job_and_progress.py`：job 生命周期、失败态 |
| 跨进程更新互斥未实现 | 新增 `app/core/storage/update_lock.py`：锁文件 + PID 属主，活进程持锁即 skipped（`another_update_running`），死属主自动回收；CLI 与 Web 共用 | 同上：活锁拒绝、死锁回收、同进程重入 |
| 自动更新状态非可操作进度模型 | `run_incremental_update` 增加 `progress_cb`（每步回调）；`AutoUpdateController.run_once` 每步持久化 phase/job_id/started_at/steps；前端自动更新卡片展示阶段进度与作业 ID | `test_auto_update_controller.py`：逐步持久化断言 |
| （三轮回测新发现）股本刷新未触发快照重算 | `_share_capital_fingerprint()`（stock_code+total_shares+circ_shares+is_listed 的 md5）前后比对，`share_capital_changed` 纳入快照重算条件 | 同上：变化触发重算、未变化跳过 |
| CSRC 首启性能（2.3h 理论下限） | `_fetch_csrc_industry` 改为只补抓 `csrc_l1 IS NULL` 的股票 + 分块（50/块）独立事务 + `data_refresh_state` 进度断点续传；已有分类保留（可用旧分类策略） | 同上：只抓缺失、全齐跳过、进度记录 |
| vd.bat dist 目录优先遮蔽开发 CLI | vd.bat 重写为开发优先：仅当 `%~dp0value-dashboard.exe` 存在（发行布局）才走打包模式；仓库根目录始终 `python -m app.cli.main`；保持 ASCII+CRLF | `test_release_entrypoint.py`：开发优先断言 + 既有数据目录断言 |
| 数据状态页缺各数据域最新日期（PRD §6.4/§15） | API 增加 xdxr 日期范围、股本/上市名单 `updated_at`、`data_refresh_state` 刷新时间戳（stock_list/listing_info/CSRC）；前端新增"各数据域最新日期"卡片 | `frontend/tests/component/data-status-flow.test.ts`：新卡片渲染断言 |
| 前端无组件/浏览器 E2E | 新增 vitest + @vue/test-utils + jsdom（0 漏洞）；`src/http.ts` 抽出写令牌拦截器；9 项组件测试覆盖：筛选运行（SC8）、保存/导出/加入自选（SC14/16/17）、数据状态、自选信任遮蔽、个股不可信告警、写令牌拦截器；`npm test` 门禁串联 vitest | `frontend/tests/component/*.test.ts`（6 文件 9 用例） |
| 无真实全市场性能基准（PRD §19.1） | 新增合成 5,000 股隔离夹具（20 条件 + 复合指标 + 行业排名），预热后连续 10 次，断言 9/10 ≤ 5s；正式主机验收仍走 `scripts/create_performance_fixture.py` + `screening_performance_acceptance.py` | `test_screening_performance.py` |
| STATUS 与正式库冲突 | 正式库只读验收（见下）+ 本报告 + STATUS 更新 | 证据：`docs/evidence/evidence-formal-status-20260802.json`、`evidence-formal-screening-20260802.json` |

## 正式库只读验收（2026-08-02，当前工作树）

| 检查项 | 结果 |
|---|---|
| `vd data status`（read-only） | ready=**true**、5,533 只、四域全覆盖、retry 11、missing 0（`evidence-formal-status-20260802.json`） |
| `screening_readiness` 门禁 | ready=true、warning_codes=[]（假阳性消除：此前 ready=true 但筛选全灭） |
| `snapshot_period_mismatches` | **0**（603435 混期按完整期策略不再阻断） |
| 正式库筛选实测 | 451ms、total=3,878、base_pool=5,314、data_date=2026-06-30（`evidence-formal-screening-20260802.json`） |

## 验证门禁

| 门禁 | 结果 |
|---|---|
| `scripts/s1-pytest.ps1 tests/regression -q --no-header` | **389 passed**（含新增 15 项 P1 回归） |
| `uv run --locked ruff check app tests/regression` | All checks passed |
| 前端 lint / node 合约 / vitest 组件 / build | 通过 / 52 passed / 9 passed / 成功 |
| `uv lock --locked` | 通过 |
| 正式库只读 | ready=true、筛选可用（上表） |

## 剩余披露项（非阻断）

- 92 只银行/券商监管字段 NULL（免费源不可得，PRD 允许）、8 只停牌股、
  96 只无分红事件、2 只解禁时间差、920305 已退市——均见 `reports/29`。
- 性能验收的"目标主机 + host-spec 正式夹具"部分仍属发布仪式性步骤
  （脚本与隔离基准已就绪；`prd_acceptance=PASS` 需在目标主机执行）。
