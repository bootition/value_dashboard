---
title: P3/P4 红队发现修复与正式库数据补全报告（2026-08-12）
status: approved
category: reports
created: 2026-08-12
last-reviewed: 2026-08-12
supersedes: reports/73_SYSTEM_RED_TEAM_REVIEW_P3_P4_2026-08-11.md
---

# P3/P4 红队发现修复与正式库数据补全报告

## 1. 裁决

**PASS（修复完成）。** `reports/73` 系统红队发现的全部 P1×3、P2×9、P3×15 与交付缺口
D-1~D-4 均已修复并通过门禁；正式库完成国债曲线全量回填、指标快照重算（含利差列）、
统计域构建与价格补齐。剩余约束：CNINFO 源风控冷却（股本链约 4,700 只待续传）、
2026-08-12 当日国债曲线未发布（合法缺失，明日自动更新补齐）。

## 2. 修复清单（reports/73 逐项关闭）

### P4（历史股本链与统计）

| 项 | 修复 | 位置 | 回归 |
|---|---|---|---|
| P4-1 | CNINFO 显式传 `start_date=19900101`/`end_date=今日`（亲验 akshare 1.18.64 默认 20091227~20241021 截断），最新锚点超 365 天 fail-closed → retry | `capital_history_adapter.py:128-163` | `test_cninfo_adapter_forwards_explicit_dates_and_guards_staleness` |
| P4-2 | `update_all` 改 due 游标：无记录或最新锚点早于该股最新价格日才入选（参照 business 模式） | `capital.py:278-319` | `test_update_all_uses_due_cursor` |
| P4-3/P4-6 | 覆盖按窗口计算且只认 verified 延续；`coverage_report` 真实化（不再有记录即 100%） | `statistics.py:_stats_for_stock/_capital_coverage`、`capital.py:348-394` | `test_coverage_per_window_uses_verified_chain` |
| P4-4 | 发布列集合固定 `_PUBLISH_COLUMNS`，reason 首行不再丢失成功行 min_date/max_date | `statistics.py:_PUBLISH_COLUMNS/_write_records` | `test_publish_columns_fixed_when_first_record_is_reason` |
| P4-5 | `share_capital_history` retry 入 `_retry_failed_tasks` 白名单（走 `update_stock`） | `update.py:1602-1623` | 既有 retry 测试 + 正式库观察 |
| P4-7 | 双源单位亲验：CNINFO 万股×10000=股（茅台 94380 万✓）、东财=股（1250081601✓），换算正确并固化注释；交叉核验改 ±10 天近邻匹配，无近邻区间 fail-closed | `capital_history_adapter.py:34`、`capital.py:196-243` | `test_cross_check_*` |
| P4-8 | PB 增加 `equity>0` 守卫（负净资产不入统计） | `statistics.py:169-173` | 既有序列测试 |
| P4-9 | 输入指纹加入 dividends（仅分红变化也触发统计重建） | `statistics.py:_input_fingerprint` | `test_input_fingerprint_includes_dividends` |
| P4-10 | 统计域仅全部成功才持久化指纹（partial 下轮自动重试失败股） | `update.py:536-544` | 既有重建测试 |
| P4-11 | `update_many` per-stock 异常隔离 | `capital.py:245-262` | 既有失败路径测试 |
| P4-12 | 统计卡序列按窗口过滤 + x 轴日期线性映射 | `ResearchStatisticsCard.vue` | 前端 build/组件测试 |
| P4-13 | `/research-statistics` 响应标注 `computed=realtime` 并注明与发布域时差；卡片脚注同步 | `stock_detail.py`、`ResearchStatisticsCard.vue` | API 验证 |

### P3（国债曲线与利差）

| 项 | 修复 | 位置 | 回归 |
|---|---|---|---|
| P3-1 | 双线视图两条独立路径（绿=TTM、蓝=国债，不再互相覆盖） | `TreasuryComparisonCard.vue` | 前端 build/组件测试 |
| P3-2 | 导出溯源对齐 `latest_price_date`（快照实际计算日）而非财务报告期 | `screening.py:_field_provenance` | `test_export_provenance_aligns_on_latest_price_date` |
| P3-3 | 国债比较 API 接入 `indicator_trust` 遮蔽（DIVIDEND_DATES_UNVERIFIED 时股息率/利差置空并注明；曲线基准不受影响）；`DIVIDEND_INDICATOR_FIELDS` 补 10 字段与前端对齐 | `stock_detail.py:759-895`、`data_quality.py:701-716` | `test_treasury_comparison_masks_dividend_when_unverified` |
| P3-4 | `refresh_if_due` 日门控（当日已刷新直接 skip，不再每轮请求财政部） | `treasury.py:253-320` | `test_refresh_if_due_gates_on_marker` |
| P3-5 | 国债 retry 消费：按 extra_json 恢复 history（按期限回填）/daily（按日期日终） | `update.py:1624-1654` | 正式库观察 |
| P3-6 | 时间轴正序 + 日期线性映射（节假日/停牌自然压缩） | `TreasuryComparisonCard.vue` | 前端 build |
| P3-7 | `status_report` missing 按国债域过滤（与 retry_open 口径一致） | `treasury.py:373-380` | 既有 check-only 测试 |
| P3-8 | `--tenors` 全部非法时显式 failed（不再静默空转） | `treasury.py:87-95` | `test_backfill_invalid_tenors_fails_explicitly` |
| P3-9 | `align_many` 单 SQL 批量对齐；导出溯源批量（消除行×期限 N+1） | `treasury.py:345-410`、`screening.py:433-495` | 既有导出测试 |
| P3-10 | 收益率 `_to_float` 拒绝 NaN/±Inf（有限正数校验） | `czb_mof_adapter.py:69-76` | `test_adapter_rejects_non_finite_yields` |

### 交付缺口

| 项 | 修复 |
|---|---|
| D-1 | `user-first-use.md` §6 增补业务概览/历史统计/国债比较口径（5 日陈旧、期限切换、缺失语义） |
| D-2 | `ops-auto-update-retry.md` 新增 §4.1 新数据域 CLI（treasury-curve/capital-history/research-statistics）+ 异常处理表 |
| D-3 | `README.md` 5 分钟上手增补研究功能、适配器表加财政部源、状态段更新至 2026-08-11 |
| D-4 | `/api/data-status/retry-list?data_type=`、`/missing-list?field_prefix=` 支持按域过滤 |

### 测试缺口补测（reports/73 §6）

- P4-1 日期参数与新鲜度断言、P4-2 due 游标、P4-3 按窗口覆盖、P4-4 列集合、
  P4-9 指纹、P3-4 日门控、P3-8 非法期限、P3-10 NaN 拒绝均新增回归测试；
- `test_capital_history_domain.py` fixture 1250 次逐行写入改批量（274s → 15s）。

## 3. 门禁验证

| 门禁 | 结果 |
|---|---|
| 完整 S1 隔离回归 | **562 passed**（修复后全量） |
| Ruff | PASS |
| 前端 lint / Node / 组件 / build | PASS / 55 / 40 / PASS |
| 正式库端到端 | 国债比较（10Y 1.7114%、利差 2.26%）、研究统计（10y 样本 2286）、指标 trust=[]、状态页过滤均验证通过 |

## 4. 正式库数据补全（打开即用）

| 数据域 | 状态 |
|---|---|
| 国债曲线 | ✅ 9 期限全历史回填完成：2006-03-01 ~ 2026-08-11，每期限 5,114 点；当日（8-12）曲线未发布属合法缺失（source_empty → missing，明日自动补齐） |
| 指标快照（含 TTM 股息率 + 9 利差列） | ✅ `compute_indicators` 全量重算 5,533 只，0 失败 |
| 历史研究统计域 | ✅ version 1 构建完成（15.2 万条记录）；PE/PB 覆盖率依赖股本链，如实展示 reason（coverage_below_threshold） |
| 价格 | ✅ 自动更新补齐至 2026-08-11（最后交易日） |
| 历史股本链 | ⚠️ 822 只完成；CNINFO 源触发风控（约 1,000 只后返回非 JSON），剩余约 4,700 只已入 retry，冷却后由自动更新续传（P4-2 due 游标 + P4-5 retry 消费） |
| 自动更新 | enabled；统计域 partial 不落指纹，股本链/统计随输入变化自动重建 |

## 5. 诚实披露与剩余约束

1. **CNINFO 风控冷却中**（2026-08-12 02:00 前后触发，`Expecting value` 非 JSON 响应）：股本链约 4,700 只待冷却后续传；期间 PE/PB 历史统计按覆盖门槛如实缺失（reason 注明），不影响其他功能。冷却时长未知（参考东财 push2 曾达数天），自动更新每轮有界续传 + retry 消费，无需人工干预；如需加速可冷却后用 `vd data capital-history --max-stocks 1000` 分批回填。
2. 统计域构建为 partial（约 1,700 只无价格/新股无记录 + 少量失败），失败股在指纹变化后自动重试。
3. 自动更新进程曾被工具超时终止，状态页 `current_stage=running` 为残留标记，下次启动自动重置（`_load_persisted_state`）。
4. 本次修复未改变 `reports/71/72` 的实施事实；其 PASS 裁决仍以本报告与 `reports/73` 为当前依据。
