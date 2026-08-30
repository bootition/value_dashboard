---
title: 数据完整性审计与财务明细/业务概览缺口自动续传报告
status: approved
category: reports
created: 2026-08-31
last-reviewed: 2026-08-31
---

# 数据完整性审计与财务明细/业务概览缺口自动续传报告（2026-08-31）

## 裁决

**审计完成并已落地自动续传机制。**
- 之前缺少完整性检验的原因已定位：readiness/diagnose 只检查“核心最小字段”，把财务明细和业务概览视为非阻断披露域，导致“页面有大量空值但诊断仍显示健康”。
- 新增财务明细缺口检测与有界自动回填；`vd data diagnose` / `vd data status` 现在会如实报告两个此前被忽略的缺口：财务明细缺口和业务概览缺口。

## 审计发现

1. **财务明细缺口被静默忽略**
   - `_financial_row_is_complete` 只要求 total_assets/revenue/cf_from_operating 等核心字段。
   - Sina 源实际返回 70+ 明细字段，但旧解析器只保留 7 个，且写入层二次过滤，导致详情页大量 “—”。
   - 自动更新只为“有新财报公告”的股票刷新财务；旧报告期的明细缺口永远不会自动补齐。

2. **业务概览缺口可自动续传但诊断不可见**
   - `BusinessOverviewUpdater.refresh_if_due` 按股票 missing/stale 选择队列，每轮有界处理，设计正确。
   - 但 `vd data diagnose` 只统计 retry/missing 数量；未排到的 3,000+ 缺口不进入 issues，容易误判“健康”。

3. **诊断在自动更新写窗口内可能失真**
   - `vd data diagnose` / `vd data status` 之前无条件执行 `build_data_quality_status`；写锁活跃时可能读到半成品股票池/快照，出现假阴性“ready=false”。

## 修复内容

- `app/core/update.py`
  - 新增 `_financial_detail_gap_codes`：按最新报告期检测三大表明细缺失股票。
  - 新增 `_refresh_financial_detail_backfill`：每轮有界重抓明细缺口（默认 100 只），并把成功代码纳入指标快照增量重算。
  - 自动更新流程新增 `financial_detail_backfill` 步骤。
- `app/resources/config/default.yaml`
  - 新增 `financial_detail_backfill_max_stocks_per_run: 100`。
- `app/cli/main.py`
  - 新增 `vd data financial-detail-backfill --max-stocks N`（单写者串行）。
  - `vd data diagnose` / `vd data status` 增加 `financial_detail_gap_count`、`business_overview_gap_count`。
  - 写锁活跃时诊断改用持久化 readiness/就绪缓存，并标注 `auto_update_in_progress=true`。
- `app/core/adapters/sina_adapter.py` / `app/core/init.py`（报告 90）
  - 三表明细字段映射与写入已修复。

## 当前正式库实测

- 财务明细缺口：5,375 只（几乎全部股票，因为旧解析器只写了核心字段）。
- 业务概览缺口：约 2,954 只且正在由当前自动更新按 100 只/轮续传。
- 海尔生物 688139：财务明细已回填，业务概览已补抓。

## 后续建议

1. 自动更新每轮默认回填 100 只财务明细；如希望更快，可执行：
   `vd data financial-detail-backfill --max-stocks 1000`
2. 业务概览每轮自动 100 只；如希望更快，可执行：
   `vd data business-overview --max-stocks 1000`
3. 全部回填完成后，`vd data diagnose` 的 issues 会相应减少并恢复 healthy。

## 验证

- 后端回归：`test_update_job_and_progress.py`、`test_incremental_update_scope.py`、`test_sina_adapter.py`、`test_storage_and_ingestion.py` 等通过；新增明细缺口检测测试通过。
- Ruff 全部通过。
