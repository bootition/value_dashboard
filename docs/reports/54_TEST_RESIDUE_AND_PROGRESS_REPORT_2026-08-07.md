---
title: 测试残留清理与断点续传实证及数据页结构强化报告（2026-08-07）
status: approved
category: reports
created: 2026-08-07
last-reviewed: 2026-08-07
---

# 测试残留清理与断点续传实证及数据页结构强化报告（2026-08-07）

在 `reports/53` 基础上追加三项收敛，作为当前状态补充证据。

## 1. 筛选指标残留英文「test」的根因与清理

### 1.1 误报辨析

`latest_close` 名称内部含子串 `test`（la`test`_close），被含 `test` 的检索正则命中，属于命名巧合，不是测试残留。

### 1.2 真正污染

正式库 `dsl_expressions` 是 2026-07-17~21（功能/S1 测试期）在路径隔离契约（2026-08-02）生效前直接写入 51 条 DSL 草案/发布版，其中 4 条 `published`（`test_accept`、`test_roa_s2`、`uat_roa`、`asset_turnover`）被 `/api/screening/indicators` 暴露为用户可选指标，显示为英文「test_accept (DSL v1)」等；`screening_results` 另存 5 条测试结果（name=test）。

上一轮「清理正式库测试数据」只覆盖了 watchlist/screening_rules/jobs，遗漏了 `dsl_expressions`/`screening_results`，因此未清理干净。

### 1.3 清理

- 新增受控维护脚本 `scripts/clean-formal-test-residue.py`：要求 formal profile + 路径规格；仅当目标表全部行创建于 2026-07-22 之前（测试期窗口）才清空，否则拒绝并提示人工核实；幂等。
- 执行结果：`dsl_expressions` 删除 51 条、`screening_results` 删除 5 条；两表归零。
- 验证：`/api/screening/indicators` 由 769 → 765，测试 DSL 指标消失，规则/结果表无残留引用。
- 防再入：自 2026-08-02 起测试一律在隔离 profile（`VD_TEST_RUN_ROOT`）运行，正式库不再被测试直接写入；脚本保留供审计复检。

## 2. 自动更新断点续传实证与配套核查

断点续传能力已存在且新增回归证明：

- 每只成功股票 raw/qfq 原子提交（`_persist_incremental_price_pair`），中断后下一轮按各股票 `latest_raw_date/latest_qfq_date` 缺口只补缺失部分。
- 死亡锁回收 + 悬挂 `running` 作业结算（`update_lock/exclusive_update` + `_reconcile_crashed_incremental_jobs`）；进程崩溃后 `running` 标记复位为 idle。
- 配套：CLI `enable/disable/run/pause/resume/status`、`retry_list`/`missing_list` 失败续传、job_logs 生命周期、CSRC 分块断点续传测试。
- 新增针对性回归 `test_price_update_resumes_from_committed_progress`：模拟「000001 成功提交 → 处理 600519 时源中断」，再以新实例续跑——已完成的 000001 不再被重复抓取（fetch 调用保持 1 次），600519 补齐。本次回归单独 18/18 通过（含既有更新生命周期测试）。

## 3. 数据状态页去「平坦化」

覆盖卡片、明细表与统计卡此前为大面积无边框浅灰文本，字段不突出。本轮强化（保持全站 16px 圆角轻阴影基线）：

- 覆盖概览卡：顶部 3px 强调条 + 1px 边框 + 数字 760 黑体 tabular、标签 700。
- 全部明细表（7 处 `n-descriptions`）转 `bordered`：单元格边框、标签列灰底粗体、数值列加粗 tabular-nums。
- 统计卡数值黑体 760；自动更新实时进度卡同样加顶部强调条。
- 真实浏览器计算样式验证：数字 font-weight 760、标签 700、表存在 7、`border-top: 3px` 强调条生效。

## 4. 验证与披露

- 前端：lint、55 Node、19 组件、build 全绿。
- 后端：`test_update_job_and_progress.py` 18 passed；Ruff（app+tests/regression）全绿。
- 正式服务仍为进度可视化改造前的旧进程；自动更新结束后重启一次即加载全部新能力（中文化、进度条、去平坦化）。
- 结论：测试残留已清零、断点续传有实证、数据状态页结构强化已验收。