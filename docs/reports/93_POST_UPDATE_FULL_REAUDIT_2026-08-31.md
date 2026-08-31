---
title: 更新完成后全量复审计报告
status: approved
category: reports
created: 2026-08-31
last-reviewed: 2026-08-31
---

# 更新完成后全量复审计报告（2026-08-31）

## 裁决

**本轮更新完成后再次全量复审计：核心数据链已按新顺序执行完毕，`minimum_data_readiness.ready=true`、`warning_codes=[]`。** 复审计又发现 5 个此前遗漏的队列/清理问题，已全部修复并回归。

## 本轮更新实测结果

- 最新 `incremental_update` 作业：`status=success`，耗时约 55 分钟。
- 步骤顺序已生效：`financial_detail_backfill → buyback → funding → treasury_curve → retries → indicators → business_overview → capital_history → research_statistics → index_valuation`。
- 财务明细回填：100 只全部成功；业务概览：100 只全部成功；回购首轮 5,286 条全量刷新并触发全量快照；指标快照 5,542 只成功。
- 数据诊断：`ready=true`、`warning_codes=[]`、lineage/hash/orphan 全 0。
- 剩余披露缺口：财务明细 5,065 只、业务概览 2,654 只、9 只新股核心财务未形成、108 只 CSRC 源无分类、1 条 CNINFO 公告源故障 retry（源恢复后按本次修复自动清理）。

## 新发现与修复

### 1. 历史股本链 20 只/轮永远重复队头
- **发现**：`CapitalHistoryUpdater._due_stock_codes` 只用 `latest_cap < latest_price` 判断陈旧；但 CNINFO 主链锚点天然早于最新价格，成功回填后下一轮仍会排在队头。正式库连续 3 个作业都重复处理 `000001…000513` 前 20 只，后面的 4,000+ 只永远排不到。
- **修复**：`capital_cross_cache` 新增 `main_fetched_at/main_status` 主链抓取结果缓存；主链成功 7 天内、失败 30 分钟内不再复刷。成功股票出队，游标真正推进。

### 2. 国债曲线周末 missing 永久重试
- **发现**：`backfill_missing_days` 会把 missing_list 里的非交易日（如 2026-08-29/30 周末）当缺口，每天重试并重新登记 `source_empty`。
- **修复**：候选日先经交易日历校验；非交易日的一次性解决为已结清。正式库已执行，现仅剩 2026-08-31（周一，源未发布，属真实待补）。

### 3. CNINFO 公告源级 retry 恢复后不清理
- **发现**：源故障以 `stock_code=''` 记录全局 retry；CNINFO 恢复后没有清理路径，`retry_count=1` 会永久残留在诊断里。
- **修复**：公告检查 `available` 且无 errors 时清理 `stock_code='' AND data_type='announcements'`，不影响逐股 pending 标记。

### 4. missing_list 重复写入日志告警
- **发现**：`DataInitializer._record_missing` 用裸 INSERT，已存在未解决条目时触发 UNIQUE 冲突警告（本次更新日志两次出现）。
- **修复**：改为 `ON CONFLICT ... DO UPDATE`，原因码可更新且不再报错。

### 5. 既有 S1 失败测试的种子日期矛盾
- **发现**：`test_snapshot_ttm_dividend_yield_and_spread` 把固定曲线日期写在动态 `CURRENT_DATE` 价格史之前，导致利差恒为 NULL。
- **修复**：曲线种子动态对齐 `date.today()`，陈旧样本用 `today - 18 天`。

## 验证

- `test_treasury_curve_domain.py`、`test_update_job_and_progress.py`、`test_capital_history_domain.py`、`test_storage_and_ingestion.py`：**104 passed**。
- `test_announcement_check.py`：**17 passed**。
- Ruff 与 compileall 全部通过。

## 后续动作

1. 重启服务加载本轮修复。
2. 下一轮自动更新验证 `capital_history` 游标不再重复前 20 只。
3. 1000 只财务明细加速回填已重试成功（第一次 watcher 因 DuckDB 读连接瞬态失败退出，代码/锁机制本身正常）。
