---
title: 数据与代码双重复检查报告
status: approved
category: reports
created: 2026-08-31
last-reviewed: 2026-08-31
---

# 数据与代码双重复检查报告（2026-08-31）

## 裁决

**数据侧与代码侧已完成双重复查。核心链路数据完整可筛；剩余缺口均为已在诊断中披露的有界续传项或市场/源真实状态，不影响筛选可信度。**

## 数据侧结果

- 上市股票 5,551；价格覆盖 5,551（最新交易日期 2026-08-28，4 只停牌股自然停在停牌前日期）。
- 三表核心覆盖 5,542；最新完整报告期 2026-06-30 覆盖 5,540。
- 指标快照 5,542；`snapshot_price_coherence` 仅 9 只上市不足 90 天新股为披露项。
- 融资事件覆盖 5,551/5,551；回购事件 5,286 条/2,868 只；历史股本链覆盖 5,551/5,551；历史统计覆盖 5,551/5,551。
- lineage：hash mismatch 0、orphan batch 0、archive/batch/audit gap 0。
- retry：0；running job：0（复查时自动更新已完成）。
- **仍在有界续传的缺口**：
  - 财务明细字段缺口 4,975 只（已排队 1000 只加速回填，正在运行）。
  - 业务概览缺口 2,554 只（自动更新每轮 100 只）。
  - CSRC 源无分类 108 只（源如实 NULL）。
  - 银行/券商监管字段 NULL（免费源不可得，不伪造）。
  - 2026-08-31 国债曲线源尚未发布（周一，后续自动补）。
- **个位数数据缺口**：
  - 4 只股票 `dividend_financing_ratio_pct` 为空：`000661/601188/601200/601518`，根因是 CNINFO IPO 记录缺 `issue_price` 且无募资净额，fail-closed 不估算。
  - 603365（水星家纺）最新完整报告期为 2026Q1，2026H1 尚未形成三表（源未发布/未抓取）。
  - 9 只上市不足 90 天新股核心财务未形成，属披露豁免项。
  - 002731 为停牌 *ST，最新完整期停在 2025Q3。

## 代码侧新发现与修复

1. **`BusinessOverviewUpdater.update_all(max_stocks>0)` 重复刷前缀**
   - CLI `vd data business-overview --max-stocks N` 之前直接取全市场前 N 只，已覆盖股票会被反复刷新，加速命令无法推进。
   - 修复：`max_stocks>0` 改为从 `_due_stock_codes` 的缺失/陈旧子集续传；`max_stocks=0` 保留“显式全量”语义。
2. **`PriceBackfiller._record_missing` 裸 INSERT**
   - 已与其他域统一为 `ON CONFLICT ... DO UPDATE`，不再因重复 missing 条目产生 UNIQUE 告警。

## 已在本轮正式库验证的前序修复

- 历史股本链主链抓取结果缓存已落库：最近一次自动更新成功处理前 20 只并写入 `main_status=ok`，下一轮游标将从 000514 之后推进，不再重复队头。
- 财务明细缺口探测已使用跨行业候选字段，回填队头已从银行段推进到 000820 之后。
- CNINFO 全局公告 retry 已清零。
- 国债周末 missing 已结清，仅剩 2026-08-31 真实待补。

## 验证

- `test_business_overview_domain.py`：29 passed。
- 前序回归：treasury/update/capital/storage 104 passed、announcement 17 passed。
- Ruff：通过。

## 后续动作

1. 等待 1000 只财务明细加速回填完成，下一轮自动更新会重算其快照。
2. 下一轮自动更新验证历史股本链游标推进。
3. 持续观察 603365 半年报与 2026-08-31 国债曲线是否由源发布后自动补齐。
