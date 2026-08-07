---
title: 指标中文化与自动更新进度可视化报告（2026-08-07）
status: approved
category: reports
created: 2026-08-07
last-reviewed: 2026-08-07
---

# 指标中文化与自动更新进度可视化报告（2026-08-07）

本报告在 `reports/52` 基础上新增两项用户可见改进，并披露正式服务轮换要求；不取代
`reports/52` 的视觉、常驻条件与按需构建结论。

## 1. 筛选指标彻底中文化

`/api/screening/indicators` 只返回稳定字段名，前端展示此前对未覆盖字段直接回退英文（如 `period_return`）。本轮在前端 `utils/screening-format.ts` 建立完整中文映射：

- 补齐全部内建指标（40+）：`区间收益率`、`年化波动率`、`最大回撤`、`换手率`、`投入资本回报率（ROIC）`、`连续分红年数`、各均线、`有息负债` 等。
- 排名字段按 `指标 · 排名` 组合：`市盈率（PE-TTM） · 全市场排名`、`净资产收益率（ROE） · 证监会一级分位` 等。
- 标准化财务表字段带表前缀：`资产负债表 · 资产总计`、`利润表 · 归母净利润`、`现金流量表 · 经营活动现金流量净额` 等（balance/income/cashflow 全字段）。

稳定字段名仍只用于 API、规则 JSON 与导出，用户界面一律中文。

## 2. 自动更新进度可视化

### 2.1 后端细粒度进度

- `IncrementalUpdater.run_incremental_update` 新增可选 `detail_cb`，透传至价格步骤：每只股票处理完回调 `{done, total, current, label}`。
- `AutoUpdateController.run_once` 将明细写入持久化 `progress.live`（瞬时快照），并按时间节流追加 `progress.log`（环形 20 条、含时间戳与中文消息）；步骤级日志使用 `STEP_LABELS` 中文名。
- 终态时清除 `live`、保留 `log`；`persisted_status` 原样暴露，前端只读展示。

### 2.2 数据状态页

- 自动更新卡片新增实时进度条（`done/total` 百分比）与当前处理股票、更新日志列表（最近 10 条）。
- 轮询从 12s 缩短到 4s（运行中），并修复轮询判据：此前用 `state==='running'` 判断，而 API 的 `state` 是生命周期位（`enabled`）、`current_stage` 才是阶段，导致运行中轮询从未真正触发。
- `auto-update` 请求从 `Promise.all` 中独立先行：`/api/data-status/summary` 在正式更新写锁竞争下可能长时间阻塞，不能让进度卡片被整体拖住（PRD §7.3 界面立即可用）。

## 3. 验证

- 前端：ESLint 通过、55 Node 测试（新增 `fieldDisplayName` 中文断言 3 组）、19 组件测试、生产 build 通过。
- 后端：完整隔离回归 447 passed、1 deselected（正式库哈希保护项未运行）；Ruff 全绿；新增 `test_auto_update_progress.py` 4 项（detail_cb 签名、live/log 持久化与清除、运行中 live 可见、步骤中文名）。
- 真实浏览器（隔离 test profile + 预置 running/live/log 状态）：数据状态页实际渲染进度条（`股票价格 1234 / 5530`）、当前股票与更新日志；运行中显示“每 4 秒自动刷新”；筛选页常驻条件保持且无 `period_return` 英文。

## 4. 披露与正式服务轮换要求

- 用户当前打开的正式服务（8765）为本次改动前的旧 Python 进程：其自动更新照常运转，但尚无 `live/log` 输出，进度条需重启后生效。
- 正式自动更新正在运行中，本轮未中断；待其自然结束后，用 `start.bat` 重启一次即加载新代码与最新前端，进度条/日志随之可用。
- 结论：指标中文化与进度可视化代码已关闭并通过验收；正式运行进程轮换是唯一待办。