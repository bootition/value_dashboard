---
status: approved
category: reports
created: 2026-11-16
last-reviewed: 2026-11-16
related: reports/125
---

# 代码级问题集中修复（2026-11-16）：信任出口、市值单源、H 股市值接口、死配置、冷却路径

用户裁决：**凡是代码层面的问题，不再上报等决策，直接修**。本报告记录这一轮的修复。

## 1. 结论（TL;DR）

| # | 问题（用户视角） | 处置 | 验证 |
|---|---|---|---|
| 1 | 同一个"数据不可信"，详情页把指标全变空、筛选页照常出排名 | 收敛成**一个判定函数**，筛选侧也 409 | 7 新用例；58 既有用例不改 |
| 2 | 总市值有 3 套算法，185 只股票差 >1%（最差 −90%） | 收敛到 `app/core/market_cap.py` 单一实现 + 补 21 行股本历史 + 重抓 176 只股本链 → **偏离降到 6 只** | 18 新用例 + 290 既有用例 |
| 3 | "H 股市值"算出来了但页面上永远看不到 | 新表 `ah_market_snapshot`（v41）+ API `/api/stock/{code}/ah-metrics` + 前端类型 | 7 新用例；正式库已写 203 行 |
| 4 | 9 个配置项改了没效果；条件数/嵌套层数根本没有上限 | 4 个接线、3 个删除、2 个改由配置驱动（嵌套上限 3、条件上限 20） | 37 新用例 |
| 5 | 补采"水位一直前进、待补数一动不动" | 区分「真的没得补」与「有得补但都在冷却」 | 16 用例（含新增 1 条） |
| 6 | 写锁守卫异常被吞 → 409 保护失效 | 只吞 `OSError`，其余向上抛 | 4 新用例 |
| 7 | `app/core` 反向依赖 `app/web`（唯一一处） | 下沉到 `app/core/missing_conditions.py` | 3 新用例（AST 扫描） |
| 8 | 代码注释引用不存在的报告编号 | 修正 40 处 | ruff 全绿 |

**全量 S1：1,553 passed / 3 skipped / 0 failed（23:27）。数据体检 P1 从 2 降到 1。**

## 2. 信任遮蔽：一个判定，两个出口行为一致

**问题**：`indicator_trust()` 给出 `untrusted_all`；详情页用它把 34 个指标置 null，筛选页**从不读它**——同一份数据两个页面两种答案。

**改法**：`data_quality.screenability_block_reason(trust)` 成为唯一判定；详情页与筛选页都调它。
- `untrusted_all=True` → 筛选返回 `409 {reason: "snapshot_values_untrusted", warning_codes: [...]}`；
- 字段级遮蔽（`masked_fields`）语义不变，仍由引擎抛 `MaskedFieldError` → 409；
- `/api/screening/indicators` 新增 `untrusted_all` 与 `warning_codes`，前端不用猜。

## 3. 总市值：单一实现 + 把数据链补上

**根因（与最初假设不同）**：不是"送转整批没写"，而是**逐股股本链停在上一次成功抓取日**（244,268 行里只有 16 行是事件行；`xdxr` 送转事件 98.1% 当日有行，缺的是链条末端）。

**处置**：
1. 新增 `app/core/market_cap.py`：`resolve_shares_asof()` / `market_cap_from()` / `latest_market_cap()` / `asof_shares_join_sql()`，`calculator`、`etf_fundamentals`、`ah_metrics`、`index_dashboard` 全部改为调用它（原来是 5 处各写一份）；
2. `scripts/backfill_share_capital_from_xdxr.py --yes`：补 **21 行**（244,268 → 244,289）；
3. `vd data capital-history --stocks <176 只>`：**偏离 >1% 的股票从 176 只降到 6 只**；
4. 剩余 6 只（`689009` CDR 单位差 10 倍、`600461`/`301297`/`300429`/`688235`/`300299` 为股权激励/转债转股未入 `xdxr`）留作数据缺口。

**兼容性**：无股本历史时结果与旧实现完全一致（逐位相等，测试钉死）；ETF 序列与新 SQL 的差异 <1e-15（同一查询重复执行的浮点抖动）。

## 4. H 股市值：从"函数返回值"变成"页面能看到的数"

- schema **v41** 新表 `ah_market_snapshot`（25 列，主键 `stock_code + as_of`），`init_duckdb_schema()` 内带 v41 迁移记录；
- `scripts/refresh_ah_market_snapshot.py`：默认 dry-run，`--yes` 写（备份 + 写锁 + 幂等 upsert）→ 正式库已写 **203 行**（196 行有 H 股市值）；
- `GET /api/stock/{code}/ah-metrics`：返回全部字段 + `units` + `disclaimer` + `total_market_cap` 参照块；404 分三种原因（`not_an_ah_stock` / `ah_snapshot_missing` / `ah_snapshot_unavailable`）；
- 前端类型已在 `frontend/src/types/stock-detail.ts`，**.vue 待接**（见 §7）。

## 5. 配置与守卫

| 配置键 | 处置 |
|---|---|
| `screening.default_exclude_st` / `default_exclude_suspended` / `default_min_listing_years` | **接线**（引擎/接口的 `None` 哨兵 → 读配置） |
| `screening.max_conditions` | **接线**（原来零校验；现超限 400） |
| `screening.max_nesting_levels` | **接线**（配置 3 vs 代码硬编码 50 的冲突消除） |
| `database.sqlite_wal_mode` | **删**（WAL 是并发前提，不允许关） |
| `screening.performance_target_seconds` / `backup.keep_full_backups` / `backup.encrypt_personal_data` | **删**（无对应分支；保留策略属破坏性操作，需单独立项） |

另：`data_status.py` 的写锁守卫只吞 `OSError`；`_actionable_missing_condition` 下沉到 core（消除 core→web 层反转）。

## 6. 补采冷却路径

`pending_codes()` 把冷却中的股票滤掉后，上层把 `no_pending_codes` 当"已追平"→ 推进水位 → 待补数不动（`balance_sheet_ext` 105 只长期不变）。
现在区分 `reason="all_pending_in_cooldown"` + `remaining_pending>0` + `status="deferred"`，**不再推进水位**。

## 7. 磁盘清理（用户授权）

| 对象 | 处理 | 释放 |
|---|---|---|
| 4 张 `raw_response_archive_2026*` staging 表（274,249 行 / 17.18 GB） | **先导出 ZSTD Parquet（3.1 GB）再删表**——核对发现这 274,249 个 hash **在 history/active/外部冷库一个都没有**，直接删会永久丢原始响应 | 17.18 GB |
| `data/valuedashboard.duckdb.old-20260904013322`（2026-09-04 快照） | 删除 | 41 GB |
| `D:/vd-cold-archive-20261115`（被 r17 取代的孤儿冷库） | 删除 | 406 MB |
| `D:/vd-reset-test`、`D:/vd-rebuild-export-20260901`、`D:/vd-rebuild-new-20260901`（9 月遗留测试目录） | 删除 | 21.2 GB |

**D 盘可用空间 130GB → 151GB；C 盘 105GB。**

## 8. 仍未完成

1. **前端三张卡片未接**：`AhMetricsCard`（H 股市值）、筛选页 409 `snapshot_values_untrusted` 的阻断提示、`/indicators` 新增的两个字段；
2. **6 只股票股本链仍有偏差**（见 §3.4）；
3. `app/resources/config/default.yaml`（打包副本）未同步 4 个已删键；
4. 11 处注释编号在禁改文件里（`schema.py`/`screening.py`/`ah_metrics.py`/`stock_detail.py`）。
