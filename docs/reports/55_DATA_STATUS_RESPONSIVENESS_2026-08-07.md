---
title: 数据状态页实时响应与写锁降级报告（2026-08-07）
status: approved
category: reports
created: 2026-08-07
last-reviewed: 2026-08-07
---

# 数据状态页实时响应与写锁降级报告（2026-08-07）

## 1. 用户反馈的五个表现与根因

1. **“每 4 秒自动刷新”不刷新**：旧轮询在 `fetchData` 末尾设置 `setInterval`，而 `fetchData` 会先 `await Promise.all`（含 summary）。summary 被自动更新写锁阻塞 60s+，导致第二次循环从未启动。
2. **日志不随进度推进**：同上——进度/日志数据只有轮询拿到，轮询被卡死；且 detail 阶段的 `phase` 停留在 `starting`，界面阶段文案不准确。
3. **打开数据页很慢**：整页 `n-spin :show="loading"` 依赖 summary 返回；summary 一个请求 60s 超时，页面整屏转圈。
4. **大量警告/重试/缺失**：这些是本产品故意保留的“如实披露”机制——免费源（如东财源被限制、BaoStock 不稳）失败会进 `retry_list`/`missing_list`，数据质量门禁如实列 `warning_codes`；自动更新按失败重试。属已知运行状态，非修复遗漏。
5. **刷新按钮永久转圈**：刷新复用同一 `fetchData`，同样被 summary 卡死且无超时/防抖。

## 2. 修复

### 2.1 后端 summary 写锁降级缓存

- `data_status.get_summary`：检测更新写锁（`data/.value-dashboard.update.lock`）存在时，直接返回最近一次成功摘要（模块级缓存，TTL 60s）并标注 `stale=true / stale_reason=auto_update_active`；完全避开 DuckDB 写锁。
- 无锁时：TTL 内直接复用缓存，避免每次页面/轮询重复触发重量聚合；过期后重算。
- 实证：写锁存在时 summary 冷启动 718ms、热命中 4ms（修复前 60s+）。

### 2.2 前端轮询与加载解耦（DataStatusPage.vue）

- 拆分为两条独立链路：
  - `fetchAutoOverview()`：只请求 auto-update，8s 超时；`setTimeout` 递归 + in-flight guard（上一请求未完成则跳过），running 状态下每 4s 轮询——彻底摆脱 summary 阻塞。
  - `fetchHeavy()`：summary/retry/missing 15s 超时；失败仅更新错误提示，不阻塞 auto 卡；刷新按钮使用独立 `refreshing` 状态，不整页转圈。
- `loading && !summary` 才整页 spin；auto 卡在 summary 未到时也立即渲染；`stale` 时提示“当前为自动更新期间的状态快照”。
- `detail_cb` 同步把 `phase` 更新为 `step:prices`，阶段文案与进度条一致。

## 3. 验证

- 后端全量隔离回归 451 passed、1 deselected；Ruff 全绿；新增 `test_data_status_cache.py`（无锁构建、写锁命中 stale、TTL 复用）3 项。
- 前端 lint/build 通过；真实浏览器（demo 库 + 写锁）实测：summary 60s+ → 718ms/4ms，页面秒开，自动更新卡、进度条、日志、每 4 秒提示均即时渲染，无转圈。

## 4. 结论

代码层：数据状态页实时响应、轮询可靠性、写锁期间可用性均已修复并验收。
运行时：正式服务需在下一次自动更新结束后重启一次，使以上修复与中文/进度/去平坦化一并生效。