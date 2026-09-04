---
title: 港股分红数据域（A+H 双地上市）
status: approved
category: reports
created: 2026-09-04
last-reviewed: 2026-09-04
---

# 港股分红数据域实施与导入（2026-09-04）

## 背景与目标

用户裁决（2026-09-04）：分红融资比最终应使用**总股本/总市场视角**（A+H 合计融资与
合计广义分红），而不是单独 A 股流通股本。前置数据缺口是**港股分红历史**。
本轮交付独立低频域 `hk_dividends`，并按"模仿 A 股导入流程"的要求完成
调查 → 导入 → 重算链路（重算侧明确**不**修改现有指标公式，见下）。

## 数据源与限速（S10）

- 每股分红历史：`akshare.stock_hk_dividend_payout_em(symbol)`，底层为
  东财 **datacenter.eastmoney.com**（非 push2/push2his，不触碰已封 host）；
  单股接口、返回全历史事件。
- 适配器 `eastmoney_hk_dividend` 硬限速 **0.5s/请求（≤2 req/s）**，
  逐股串行、独立限速实例，不与 A 股/业务概览适配器共享间隔；
  调用前临时摘除 HTTP(S)_PROXY（国内源直连）。
- A+H 映射快照源：`ak.stock_zh_ah_spot()`（新浪，单次请求）。

## A→HK 映射纪律（D18）

- 映射持久化在 `app/core/ah_hk_mapping.py`：
  - `AH_SPOT_NAME_SNAPSHOT`：2026-09-04 快照 203 只；
  - 名称 NFKC/去空白归一后与 stock_meta（当前上市池）**精确匹配**；
  - 匹配不到的走 `MANUAL_OVERRIDES` 人工覆写（官方简称/更名关系）；
  - **禁止后缀剥离模糊猜映射**（招商银行≠招商证券）；
  - 名称归一后重名的 A 股自动跳过并告警。
- 实测映射：203/203，其中精确名匹配 153 + 人工覆写 50；
  **600941（中国移动）→ 00941 在列**。

## 域边界（与 A 股就绪语义解耦）

- 仅新增 `hk_dividends` 表（schema v20）：
  `PRIMARY KEY (stock_code, ex_date, plan_explain)` + stock_code 索引；
  列含 `dividend_per_share_hkd/cny`（从"每股派息2.51元(相当于港币2.9003元)"
  等方案文本解析，**无法识别时如实 NULL，绝不猜数**）。
- 写路径唯一：`vd data hk-dividends`（`--check-only/--stocks/--max-stocks/--batch-size`），
  经 `_with_update_lock` 单写者锁；**单股事务原子替换**（DELETE→INSERT）。
- 网络失败 → 整股失败、保留旧值、写 retry_list；合法空 → 保留旧值、写 missing_list。
- **不写** stock_meta / indicator_snapshot / source_audit / readiness，
  不触发 A 股指标重算；A 股口径分红融资比（流通股本，600941=34.7%）保持不变。

## 导入结果（正式库）

- 命令：`vd data hk-dividends`（全量 203 只，0.5s/股限速）。
- 覆盖：【待填：成功/失败/合法空只数】
- 数据量：【待填：hk_dividends 总行数、ex_date 范围】
- 抽查 600941：【待填：行数、首末派息、解析样例】
- retry/missing：【待填：条数与原因】

## 总市场口径：为什么仍 BLOCK

- 分红侧本轮已就绪（HKD/CNY 每股 + 可与港股股本相乘）；
- 但**港股融资事件（IPO/配股/供股）无免费源**接入，
  总市场"分红融资比"分母不完整，发布即错误数据（600941 曾出现 825.9% 教训）。
- 维持 D16 裁决：A股口径正常发布，总市场口径待港股融资源接入后再评估。

## 测试与门禁

- 新增 `tests/regression/test_hk_dividend_domain.py`（30 用例：
  映射/解析/适配器/单股原子替换/失败保旧值/限速注册/域隔离）；
  Ruff 全绿；S1 定向子集通过。
- 重建脚本 `scripts/rebuild_duckdb.py` verify 指纹显式覆盖 hk_dividends
  （行数/日期范围/HKD-CNY 合计/去重股票数）。

## 已知限制

- 映射快照为 2026-09-04 时点；新两地上市公司需 `refresh_mapping=True`
  刷新或更新快照（CLI 全量路径默认带刷新，失败自动回退快照）。
- 东财港股份红方案文本为准；特殊币种表述解析不了的行保持 NULL
  （confidence=approximate，plan_explain 原文可追溯）。
