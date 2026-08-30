---
title: 筛选结果列自由选择与就绪缓存 stale-while-revalidate 报告
status: approved
category: reports
created: 2026-08-31
last-reviewed: 2026-08-31
---

# 筛选结果列自由选择与就绪缓存 stale-while-revalidate 报告（2026-08-31）

## 裁决

**实施完成。**
- 筛选结果展示列可在运行前自由增删，新增上市日期、ST/停牌状态、总股本、流通股本等基础信息；本次运行通过 `/api/screening/run` 的 `columns` 参数生效，并持久化到草稿与 `screening_runs.columns_json`。
- 筛选就绪缓存过期后采用 stale-while-revalidate：同数据指纹下请求立即返回，后台单飞刷新，不再每 10 分钟让用户卡一次 20s+ 全库核对。

## 背景

用户反馈：
1. 筛选结果界面只能删减列，无法增加上市时间等信息。
2. 筛选运行出现长时间无响应；数据库记录显示请求最终完成，但用户侧等待过久。

## 实施内容

### 1. 结果列自由选择

- `app/core/screening/engine.py`
  - `METADATA_COLUMNS` 增加 `listing_date`、`is_st`、`is_suspended`、`total_shares`、`circ_shares`。
  - 基础池 SQL 选中 `total_shares`、`circ_shares`。
- `app/web/api/screening.py`
  - `ScreeningRequest` 增加可选 `columns`；本次运行列覆盖保存版本的 `columns`，并写入 `screening_runs.columns_json`。
- `frontend/src/views/ScreeningPage.vue`
  - 新增“03 结果展示列”多选区：基础信息 + 全部筛选指标可自由增删。
  - 选择持久化到草稿 `result_columns`；加载规则/草稿时恢复。
  - 修改展示列不产生新规则版本（保存规则时才会写入规则 JSON）。
- `frontend/tests` 与后端回归同步更新。

推荐基础列：`stock_code`、`name`、`exchange`、`listing_date`、`is_st`、`is_suspended`、`total_shares`、`circ_shares`、`csrc_l1`、`csrc_l2`、`latest_close`、`total_market_cap`；再按策略加 `pe_ttm`、`pb_mrq`、`roe`、`dividend_yield`、`dividend_financing_ratio_pct` 等。

### 2. 就绪缓存 stale-while-revalidate

- `app/core/data_quality.py`
  - `load_screening_readiness_cache(..., allow_stale=True)` 允许读取同指纹的过期决策。
  - 新增单飞后台刷新 `ensure_screening_readiness_refresh`；写锁活跃时跳过。
- `app/web/api/screening.py`
  - 缓存 miss 时先读 stale；命中 stale 立即返回并触发后台刷新；无任何缓存时才同步全量核对。

## 验证

- 后端定向回归：`test_screening_server_runs.py`、`test_screening_gate.py`、`test_screening_rule_validation.py` 共 23 passed。
- 新增测试：
  - 运行请求可携带 `columns`，响应与 `screening_runs.columns_json` 均包含 `listing_date/total_shares/circ_shares`。
  - stale cache 读取语义：fresh 返回 None，allow_stale 返回过期决策。
- 前端：lint 通过；Vitest 12 文件 58 tests 通过；`npm run build` 成功。
- 正式服务已重启加载新代码；实际 `/api/screening/run` 携带新增列返回 200，耗时 0.49s（更新窗口快照口径）。

## 已知边界

- 结果列选择影响本次运行返回的数据；历史已保存结果仍按保存时的 `columns_json` 展示/导出。
- 展示列不是筛选条件，修改展示列不会创建新规则版本（符合预期）。
