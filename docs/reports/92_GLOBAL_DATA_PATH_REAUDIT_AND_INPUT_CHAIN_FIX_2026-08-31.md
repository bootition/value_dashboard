---
title: 全局数据路径复审计与指标输入链修复报告
status: approved
category: reports
created: 2026-08-31
last-reviewed: 2026-08-31
---

# 全局数据路径复审计与指标输入链修复报告（2026-08-31）

## 裁决

**第二轮全局数据路径复审计完成，发现并修复 8 类此前遗漏的环节。**
上一轮（`reports/91`）补齐了字段映射与缺口诊断，但自动更新的“抓取→指标快照”输入链仍存在多条过期/漏跑路径。本次从全局视角逐域核对每条输入链，修复后所有涉及 `indicator_snapshot` 的输入域都保证在同一轮内“先更新输入、再重算快照”。

## 审计发现与修复

### 1. 回购明细域从未进入自动更新
- **发现**：`BuybackUpdater` 只有 `vd data buyback` 手工入口；自动更新从未调度，`dividend_financing_ratio_pct` 的“回购注销”输入会随回购进度持续过期。
- **修复**：`IncrementalUpdater` 新增 `_refresh_buyback`（默认 7 天一次），置于指标快照重算之前；发生变化的股票代码并入 `compute_snapshot_for_codes`。
- **配套**：自动更新进度新增 `buyback` 中文标签；CLI 手工刷新成功后写入 `buyback_last_refresh` 节流标记。

### 2. 融资事件与国债曲线刷新顺序颠倒
- **发现**：`funding` 在历史统计之后、`treasury_curve` 在指标重算之后。新写入的 `cumulative_financing_amount` 与 `div_yield_spread_*` 都要等下一轮才进入快照。
- **修复**：两域均前移到指标重算之前；`_refresh_funding` 返回 `changed_codes`，`_refresh_treasury_curve` 返回 `curve_changed`，均并入本轮快照重算范围（曲线变化保守覆盖全部上市股票）。

### 3. 失败重试成果同样迟到一轮
- **发现**：`retries` 步骤位于指标重算之后。重试成功的价格/财务/分红/融资/曲线数据需要再等一整轮。
- **修复**：`retries` 前移到指标重算之前；`_retry_failed_tasks` 返回 `recompute_codes`，只收集会影响快照的重试成功类型（价格、财务三表、分红/除权、融资、国债曲线）。

### 4. 股本变化在财务 partial 时被跳过
- **发现**：旧条件为 `share_capital_changed and financials == success`；财报步骤 partial 时即使股本/上市名单已变化，市值类快照也继续用旧股本口径。
- **修复**：股本指纹变化后无条件执行全量 `compute_snapshot_for_all`，与财务步骤状态解耦。

### 5. 回购全量替换存在空响应清库风险
- **发现**：东财回购接口若返回 0 行且不带 error，`BuybackUpdater.refresh_all` 会先 `DELETE FROM buyback_events` 再空写，一次瞬时空响应即清空回购输入。
- **修复**：`result.data` 为空时保留旧事件，返回 `failed/source_empty/retained=true`，不执行全量替换。

### 6. 国债曲线变更检测只看“行数+最大日期”
- **发现**：日终 upsert 或历史回填修正同一日期的 `yield_pct` 时，行数与最大日期都不变，`div_yield_spread_*` 与历史统计 `spread_10y` 会静默陈旧。
- **修复**：`IncrementalUpdater` 新增 `_treasury_curve_fingerprint`（全表内容级 md5）；`StatisticsBuilder._input_fingerprint` 同步升级为内容级指纹，并覆盖财务、股本、分红小表（价格 1700 万级表保持 count+max 轻指纹）。

### 7. 财务明细缺口探测字段会让银行/券商永久占据回填队头
- **发现**：`_financial_detail_gap_codes` 用 `cost_of_revenue` / `cash_received_sales` 等单一字段判断旧行；银行利润表本来就没有营业成本，回填成功后仍被判为缺口。缺口队列按代码排序，头部银行会挤占每轮 100 个名额，使后面的普通股票永远排不到。
- **修复**：改为“行业通用候选字段组全部为空才算旧行”——资产负债表的 `paid_in_capital/undistributed_profit`、利润表的利息/费用/投资类字段、现金流量表的职工/税费现金字段。旧最小核心集仍能检出，特殊行业回填成功后正常出队。

### 8. 手工 CLI 写命令未全部遵守单写者契约
- **发现**：`data treasury-curve/funding/index-valuation/capital-history/research-statistics/refresh_universe/compute_indicators/replenish_missing_core_data/backfill-prices/refetch_execute` 等写命令未持有跨进程更新锁，可能与自动更新交错。
- **修复**：CLI 新增 `_with_update_lock`，上述写命令统一进入 `exclusive_update`；写锁被自动更新占用时返回 `skipped/another_update_running`。只读 `--check-only`/status 路径不受影响。

## 验证

- 定向回归：`test_update_job_and_progress.py`、`test_incremental_update_scope.py`、`test_sina_adapter.py`、`test_storage_and_ingestion.py`、`test_dividend_financing_ratio.py`、`test_capital_history_domain.py` 共 **105 passed**。
- 新增回归：回购空响应保旧值、回购/融资/国债先于指标、retry 先于指标、股本变化在财务 partial 时仍全量重算、国债同日期收益率修正触发快照与统计域指纹变化、银行等行业用通用字段完成明细缺口出队。
- Ruff：全部改动文件通过。

## 后续动作

1. 重启 Web 服务加载本轮代码。
2. 财务明细回填（`vd data financial-detail-backfill --max-stocks 1000`）与业务概览续传继续按锁队列执行。
3. 正式库跑完一轮自动更新后复核 `vd data diagnose` 的缺口计数与快照一致性。
