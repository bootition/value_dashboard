---
title: 数据治理优化、分红口径修复与详情图表化实施
status: approved
category: reports
created: 2026-09-03
last-reviewed: 2026-09-03
---

# 数据治理优化、分红口径修复与详情图表化实施

## 1. research_statistics 发布写锁分批化

- `_write_records` 按 2,000 行分批写入 staging；
- `_publish_statistics_merge` 按 200 只股票分批替换，每批独立 DuckDB
  事务；全量发布路径同样分批清空旧表。
- 效果：页面在统计域发布期间不再被数分钟连续写锁冻结；每只股票的新旧
  行仍在同一事务内替换，不出现半只股票混旧新。

## 2. source_audit 冷热分离

- schema v19 新增 `source_audit_archive` 表、索引与 `source_audit_all` 视图；
- 新维护命令 `vd data source-audit-archive --before YYYY-MM-DD [--execute]`
  （check-only 默认在命令参数中显式控制），按 report_date 分页迁移；
- 正式库已执行：归档 30,039,082 行（report_date < 2025-01-01），
  热表剩 12,988,150 行，归档状态写入 SQLite；
- 个股溯源接口已改查 `source_audit_all`，历史排查可继续看到冷数据；
- 日常 readiness/lineage 只扫描热表；全量核对不再碰 3,000 万行旧审计。

## 3. 数据状态全量核对增量缓存

- `build_data_quality_status_cached()` 以轻量输入指纹（snapshot/价格/财务/
  source_audit/archive 计数）判断是否复用上次全量结果；
- `/api/data-status/summary` 与 warning-code 扫描、筛选就绪计算均复用缓存；
- 空闲期不再周期性重复 20-80s 全量扫描，更新完成后指纹变化才重算。

## 4. 分红融资比 A/H 口径修复

- `cumulative_dividend_amount` 改为 **A股流通股本（circ_shares）优先**，
  H 股总股本不再混入；港股分红未采集即不计入，绝不伪造；
- 中文名与快照注释改为“A股累计现金分红/分红融资比（A股）”，
  回购注销仍计入广义分红；
- 正式库全市场快照分块重算；中国移动 600941 已从 825.9% 修正为
  **34.7%**，与同花顺“不到 40”一致；
- 受限股导致流通股本口径略低估的情况已在注释中如实披露，待
  share_capital_history 增加 A股总股本后再升级为更精确口径。

## 5. 详情页数字卡 → 原位图表

- 新增 `/api/stock/{code}/metric-history`（TTM latest_restated、上市以来），
  支持 ROE/ROA/毛利率/净利率/CF净利润/营收YoY/净利YoY/扣非YoY/
  负债率/流动比率/速动比率；
- 新增 `MetricHistoryChart.vue`：一块区域一张图 + 下拉切换指标，
  含 P10/P50/P80/均值辅助线；
- 估值与市场：一张历史研究统计图（PE/PB/TTM股息率/利差）；
- 经营与成长：一张指标历史图（默认 ROE）；
- 财务安全：一张指标历史图（默认负债率）；
- 股东回报：一张历史研究统计图（默认 TTM 股息率）；
- 移除了四组固定数字卡、财务趋势表与自定义趋势表；当前无历史意义的
  MA/换手率/最新收盘价/总市值不再单独陈列。

## 验证与遗留

- Ruff 通过；前端 lint、69 Node + 57 Vitest、生产构建通过；
- 600941 metric-history 从 2022-06-30 起返回，K 线全历史正常；
- 全量指标重算一次 OOM（8GB/14GB 单事务 lineage 路径），改为 500 只分块
  重算完成，但部分批次为 partial，当前自动更新正在补算/刷新价格与快照；
  readiness 在自动更新完成前可能显示 false，属真实中间态。
- source_audit 逻辑热表已 12.99M 行；物理文件瘦身仍待离线 rebuild
  （DuckDB 删除后不自动释放文件高水位）。
- S1 完整回归待自动更新结束、服务停窗后补跑。
