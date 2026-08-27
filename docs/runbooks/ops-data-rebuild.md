---
title: 数据重建运行手册
status: approved
category: runbooks
created: 2026-08-03
last-reviewed: 2026-08-03
---

# 数据重建运行手册

> O2（`reports/41` B1）。2026-07-31 全市场数据重建（`docs/reports/29`）的
> 复现路径与操作说明。**危险操作：正式库写操作必须单写者串行、先备份。**

## 1. 前置

- 先 `vd backup create`（重建前基线）；
- 确认无其他进程占用正式库（S1 纪律：单写者串行）；
- 网络可用（BaoStock/腾讯/Sina/CNINFO/交易所名单）。

## 2. 重建步骤（对应 `reports/29` 顺序）

```bash
# 1) 初始化工库 schema（幂等）
vd data init

# 2) 刷新股票池（上市名单 + 校验元数据；部分响应有退市门禁保护）
vd data refresh_universe

# 3) 全量更新价格/财报/分红/公司行动（PRD §7.3）
vd data update

# 4) 历史价格回填（可选，用于趋势/因子）
vd data backfill-prices

# 5) 股本/价格等基础数据补全与一致性
vd data replenish_missing_core_data

# 6) 收尾：交易日历→分红缺口→quarantine→指标快照→诊断证据
python scripts/finalize_rebuild.py --evidence-dir docs
```

## 3. 校验

```bash
# 就绪与警告码
vd data status
# 只读哈希（重建前后对比，证据留档）
Get-FileHash data\valuedashboard.duckdb, data\valuedashboard.sqlite -Algorithm SHA256
```

重建完成验收：`ready=TRUE`、`warning_codes=[]`、
`snapshot_period_mismatches=0`、30 股外部真值抽样（见 `reports/29`）。

## 4. 注意事项

- 银行/券商监管字段 90 只（STATUS 缺口#4）与 `920305` 等免费源缺口保持 NULL（不伪造）；
- 2026-03-31 之前历史期财务为 CSMAR 商业导入值（无原始字节 lineage），
  quarantine 表保留不删除；
- CSRC 行业分类（`csrc_l1/l2`）已落地（2026-08-04，4923/5533）：无行业变更历史
  的新上市/北交所（301xxx/920xxx）如实 NULL；行业排名对 NULL 行业返回 NULL
  （PRD §12.4 设计行为，不造假）。填充命令：
  `python scripts/populate_csrc_industry.py`（正式 profile，幂等/断点续传）。

## 5. 参考

- `docs/reports/29_DATA_REBUILD_REPORT_2026-07-31.md`
- `docs/reports/43/44`（用户手册与 UI 分层不涉及数据重建）