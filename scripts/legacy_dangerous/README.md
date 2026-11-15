# legacy_dangerous — 已隔离的危险维护脚本

本目录只存放**禁止在正式库执行**、仅作历史审计留档的脚本。
`scripts/verify_no_dangerous_maintenance.py` 会递归扫描 `scripts/**/*.py`
并**排除本目录**；目录内的脚本不参与日常维护门禁，但也不得被任何模块 import。

## patch_deducted_profit.py

### 它原来做什么

1. 读 `data/income_statement_akshare_temp.csv`（历史中间产物）；
2. 逐只股票调用 AKShare 东财利润表接口，把 `DEDUCT_PARENT_NETPROFIT`
   写回 CSV；
3. 在 DuckDB 事务里执行：

   ```sql
   DELETE FROM income_statement WHERE report_date > '2025-03-31';
   ```

   然后把整份 CSV `INSERT` 回 `income_statement`。

### 为什么危险

- CSV 是**全量回灌**的唯一来源。只要 CSV 缺股票、缺报告期或列不齐，
  `DELETE` 之后的 `INSERT` 就会把 2025-03-31 之后的利润表行**永久打空**；
  若 CSV 还包含其它表字段/口径不一致，连带影响三表口径。
- 删除条件按报告期区间，不按股票、不按批次；没有行级白名单。
- `--yes` + 备份只能降低概率，不能消除“CSV 不全 → 数据缺失”的根因。
- 该脚本最早是补 `deducted_net_profit` 的一次性补丁，后来被误当成常规维护通道。

### 替代通道

`scripts/fetch_deducted_profit.py`（2026-11-14 起）：

- 数据源同样是东财 F10 `lrbAjaxNew`（字段 `DEDUCT_PARENT_NETPROFIT`）；
- **只 UPDATE 既有行里 `deducted_net_profit IS NULL` 的列**；
- 绝不 INSERT / DELETE / 覆盖既有值；
- 默认 dry-run，`--yes` 才写库；
- 写前 Parquet 备份 + 单写者锁检查；
- 已接入增量更新链（`deducted_profit` 手工链自动调度）。

### 使用纪律

- ❌ 禁止在 `data/valuedashboard.duckdb`（正式库）上执行本目录脚本；
- ❌ 禁止 `import` 本目录任何模块（包括测试）；
- ✅ 需要补扣非净利润时，用 `scripts/fetch_deducted_profit.py`；
- ✅ 需要审计历史行为时，只读本文件与 `docs/reports/120` §5.6。

### 保留原因

按“不物理删除任何东西”的维护纪律，危险脚本只做**移动隔离 + 警示**，
保留原始实现以便追溯 2026 年扣非补丁的口径与事故根因。
