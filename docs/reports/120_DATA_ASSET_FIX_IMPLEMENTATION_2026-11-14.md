---
title: 2026-11-14 数据资产修复实施：P0 扩展指标复活 / 空值可解释 / 手工链接入自动更新 / 资产体检
status: approved
category: reports
created: 2026-11-14
last-reviewed: 2026-11-14
---

# 2026-11-14 数据资产修复实施报告

> 前置发现：`reports/119`（数据包价值兑现度审查）。
> 本轮是**实施轮**：把审查出的问题按"一个问题交给一个 agent"并行落地，再由主控集成、跑数据、验证。
> 详细会话产物：`.planning/2026-11-14-datapackage-review/`。

## 1. 结论（TL;DR）

1. **P0 已关闭**：`indicator_ext` 从 2026-06-30 重建到 **2026-09-30**，55 项扩展筛选指标从"全体失效"恢复到 **99%+ 覆盖**（与快照按报告期 join 的匹配数 3 → **5,576**；条件 `free_cash_flow > 10 亿` 从 **0 只** → **421 只**）。
2. **空值可解释了**：新增 `indicator_null_reason` 稀疏表 + 清扫器，首次全量写入 **102,818 行 / 66 个字段**，6 类原因（不适用 / 该算没算 / 源没给 / 没去抓 / 分母无效 / 被遮蔽）；前端 `—` 悬停可见原因。
3. **8 条手工链已接入自动更新**（PDF 域除外），全部带水位/到期/上限/失败隔离。
4. **12 条资产体检规则上线**：`vd data doctor` 一条命令跑完（`--fast` 3.5s），P1 进 `warning_codes`（**只披露、不遮蔽**）。
5. **多个真实数据缺陷被修**：扣非净利润 0 → 5,573 行、2026Q3 固定资产 102 → **5,575 行**、`roic` 656 → **4,710** 行、波动率 994 → **5,556** 行、港股分红 47 行解析错误。
6. **审查报告里一条根因判断被推翻**：折旧摊销/报表补充科目"停更"不是"没人跑"，而是**续传判据写错**（有任意本源行就跳过）→ 手动跑也补不到新报告期。已修 + 回归钉死。

## 2. 交付清单（按问题）

### 2.1 冷热分库（reports/119 §7 建议 A：Parquet 冷库）
- 新增 `scripts/cold_archive_split.py` + `config/cold_archive.yaml`：`--plan / --export / --verify / --views / --drop`，带写锁检查、原子改名、manifest（行数/sha256/日期范围/消费者）。
- **实测**：17 张静态归档表 / **4,557,779 行** / 570 列 → Parquet(ZSTD) **406 MB**。
- 生成 17 表 / 52 处 DDL 删除清单（含 34 条索引与 1 条 `ALTER TABLE`——打在视图上会抛错）。
- **本轮只做到"冷库已备好 + 可验证"**：物理切换（建视图 + 删 DDL + drop 表）留给下一轮，原因见 §6。

### 2.2 空值原因体系
- 新增 `app/core/quality/null_reasons.py`（`NullReason` 6 值枚举 + 中文文案 + 幂等 upsert + 分布查询）、`null_reason_sweeper.py`（规则表 + 证据 SQL，跨 5 个域）、`scripts/classify_null_reasons.py`（默认 dry-run）。
- 新增只读端点 `GET /api/data-status/null-reasons`；筛选结果空单元格带 `title` 原因。
- **实测写入 102,818 行 / 66 字段**：不适用 62,926 / 源未提供 30,657 / 没去抓 5,403 / 该算没算 3,486 / 分母无效 346。
- 关键规则：`fixed_assets` 断崖（99.9%→1.8%）与 `deducted_net_profit` 近零覆盖（80.2%→0%）被正确判为 `NOT_DISPATCHED`；三季报不披露补充资料导致的折旧摊销缺失判为 `SOURCE_EMPTY` 而不是"没去抓"。

### 2.3 手工链接入自动更新
`_run_incremental_update_flow` 新增 8 个步骤：`cashflow_supplement` / `balance_sheet_ext` / `fixed_assets` / `deducted_profit` / `regulatory_metrics` / `indicator_ext` / `etf_prices` / `hk_dividends`。
每个步骤：`data_refresh_state` 水位（`*_last_refresh` + `*_last_success_at` 双写）+ `_refresh_due` 到期 + 每轮上限（股票数/耗时）+ 失败隔离（只让自己 failed/partial）。
- `indicator_ext` 门控：`max(report_date) < 最新完整三表报告期` → 必重建；否则输入指纹变化才重建；另有 7 天兜底。
- 新增 `app/core/supplement.py`（声明式补采：cashflow / balance_sheet_ext / fixed_assets / deducted_profit + 监管指标）。
- **修掉续传判据 bug**：旧逻辑「本表有任意本源行就跳过」→ 改为「本源最新报告期 < 目标最新报告期」，`fetch_cashflow_supplement.py` 与 `fetch_balance_sheet_ext.py` 都有该 bug，已修 + 回归。

### 2.4 资产体检（12 条规则）
- 新增 `app/core/quality/asset_health.py`（1,561 行 / 12 条规则）+ `vd data doctor [--json] [--write-history] [--fast]`。
- 新 code **不进** `_SNAPSHOT_BLOCKING_WARNINGS`（断言测试钉死），只做披露；`min_domain_rows=100` 防小库假 P1。
- 首次运行写出 111 行覆盖率基线，后续 `FIELD_COVERAGE_DROP` 才有环比可比。

### 2.5 数据包（交付物）
- `scripts/export_data_package.py` 重写：README/SOURCES/KNOWN_ISSUES **全部从本次导出实测渲染**（已删除 `KNOWN_ISSUES` 常量，有测试断言其不存在）；补 `announcement_registry`（22,179 条）与 `trading_dates`（8,797 行）；新增 `--verify --pkg` 对账（文件完整性 + 逐表行数漂移 + as_of 分域漂移，退出码 0/2/1）。
- 旧包 `--verify` 实测返回 **2**：34 文件 sha256 全对，但 26 项漂移（2 缺表 + 21 表行数 + 3 个 as_of 域，价格落后 57 天）——审查里的数字被机器自动复现。

### 2.6 数据修复（实测前后）
| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| `indicator_ext` 最新报告期 | 2026-06-30 | **2026-09-30** |
| ext↔snapshot 按报告期 join | 3 只 | **5,576 只** |
| `free_cash_flow > 10 亿` | 0 只 | **421 只** |
| `income_statement.deducted_net_profit` @2026-09-30 | 0 / 5,575 | **5,573 / 5,575** |
| `balance_sheet.fixed_assets` @2026-09-30 | 102 / 5,575（1.8%） | **5,575 / 5,575** |
| `roic` 覆盖 | 656（11.8%） | **4,710（84.5%）** |
| `annualized_volatility` 覆盖 | 994（17.8%） | **5,556（99.6%）** |
| `deducted_profit_yoy` 覆盖 | 4,421（79.3%） | **5,116（91.8%）** |
| `fixed_asset_turnover` / `fixed_asset_ratio` @Q3 | 1.8% | **100%** |
| `hk_dividends` 汇率被当股息 | 47 行错值 | 已按 `plan_explain` 重算修复（备份 Parquet） |

补采实绩：折旧摊销/资本支出 **5,565 只**、资产负债表补充科目 **5,476 只**、扣非 **5,575 只**（27,850 行）、固定资产 **5,474 只**（27,287 行）、金融监管指标 **70 只**。

## 3. 根因更正与意外发现

| # | 事实 | 影响 |
|---|---|---|
| 1 | **续传判据 bug**（不是调度缺失）：`have = {stock_code ...}` 判"有任意本源行"，导致跑也白跑 | 已修，两个脚本 + 回归 |
| 2 | **三季报不披露现金流量表补充资料**：2026-09-30 的 `cash_flow_indirect` 有 5,574 行但折旧摊销列只有 5 行非空 | 折旧摊销/EBITDA/EV-EBITDA/经营杠杆/综合杠杆在 Q3 **天然不可算**，判 `SOURCE_EMPTY` 并由动态覆盖门隐藏 |
| 3 | **`income.exchange_gain` 断崖**：52.1%（2026-03-31）→ 3.5%，判 `NOT_DISPATCHED` 5,381 | 新增发现，下一次财务刷新需查源 |
| 4 | **波动率/区间收益/回撤是"补齐后没重算"**：重算一次就从 17.8% → 99.6% | 说明数据本身没问题，缺的是"qfq 后到 → 标记重算"的触发（仍未做，见 §6） |
| 5 | **`roic` 塌陷是口径缺陷**：`bonds_payable` 缺失就整列 NULL（多数公司本来没发债） | 已修（缺项按 0），覆盖 11.8% → 84.5% |
| 6 | **港股分红解析把汇率当股息**（89 行含"汇率"，47 行错值） | 已修 + 数据修复 |

## 4. 验证

- **全量 S1：1,273 passed / 0 failed（23 分 48 秒）**，正式库指纹 `delta_detected=false`。
  - 首轮 1,271 passed / 2 failed：两个失败是 `test_redteam_2026_08_13_fixes` 的 schema 版本契约用例——
    `DUCKDB_SCHEMA_VERSION` 已升到 39 但 `init_duckdb_schema()` 末尾漏了 v39 的 `schema_migrations` 记录，
    快速启动路径会永久判"未到最新"。已补 v39 迁移记录并对正式库应用（`max(version)=39`），两个用例单测复验通过后重跑全量。
- 后端定向回归：新增 `test_null_reasons.py`(26) / `test_asset_health.py`(51) / `test_manual_chain_scheduling.py`(15) / `test_cold_archive_split.py`(27) / `test_data_package_export.py`(20)，全部通过；相关既有回归（screening/indicator/update/quality）多轮通过。
- Ruff：`ruff check app tests/regression` 全绿（`scripts/` 下 71 条为历史遗留脚本，不属门禁范围）。
- 前端：`npm run lint` 通过、`npm run test` **57 passed**、`npm run build` 通过。
- `vd data doctor --fast`（正式库只读）：P1 从 37 项降到 **32 项**（`EXT_PERIOD_MISMATCH`、`DEDUCTED_PROFIT_GAP`、`SCHEMA_DDL_DRIFT` 已消失），剩余主要是结构性可空字段与两条停摆链。
- 数据包：新包 `.planning/vd-datapackage-2026.11-20261114` 已导出——**29 表 / 1,333.5 MB**（含 `announcement_registry` 22,179 条、`trading_dates` 8,797 行、2026Q3 三表），KNOWN_ISSUES 21 条全部规则生成；**`--verify` 返回 0**（34+ 文件 sha256 全对、逐表行数 0 漂移、as_of 四域均落后 0 天）。对照：旧包 `2026.09` 用同一命令返回 **2**（26 项漂移）。

## 5. 遗留与下一步

1. **冷热分库物理切换**（本轮的下一步）：建视图 + 删 52 处 DDL + drop 17 表 + 6 个导入脚本改走冷库 + 测试改造。必须先有 `--views` 验证，且注意 `DELETE/INSERT` 写入的导入脚本会写不进视图。**DROP 不会缩小现有 28GB 文件**，收益要等下一次离线重建。
2. **QFQ 后到的重算触发**：本轮靠手动重算把波动率修回 99.6%，但机制仍缺——建议在价格步骤里对 `qfq_lag_count>0` 的股票写 `data_refresh_state` 待重算标记。
3. **`income.exchange_gain` 断崖**：待查源。
4. **动态覆盖门**：已按 50% 阈值过滤扩展字段；`EXTENDED_SCREENING_READY` 从"准绳"降级为"候选集"。
5. **A+H 市值口径缺陷（`product-gaps` 发现）**：`total_market_cap = 最新收盘价 × 总股本`，而 A+H 公司的总股本含 H 股 → 203 只 A+H 的市值/PE/PB/PS 目前按 A 价给 H 股定价（工行实测完全吻合）。这是"已在用的指标就是错的"，需要产品裁决（是否改用 H 股价或披露口径），**本轮未动**。
6. **`scripts/patch_deducted_profit.py` 应废弃**：它会 `DELETE FROM income_statement WHERE report_date > '2025-03-31'` 再从 CSV 回灌，CSV 不全会打空三表；新的 `fetch_deducted_profit.py` 只填 NULL。
7. **服务需重启**才能加载新流水线（当前 Web 进程仍是旧代码）。

## 6. 为什么冷库"只备好不切换"

- 17 张表的 DDL 分散在 `schema.py` 的 52 处（表 17 + 索引 34 + ALTER 1），**索引与 ALTER 打在视图上会直接抛错**，而 `init_duckdb_schema()` 每次启动都会执行；
- 6 个 `scripts/import_csmar_*.py` 仍然 `INSERT` 这些表，转视图后写不进去；
- `tests/regression` 里多个用例依赖这些表存在于 schema 初始化结果中；
- 而 **DROP 表并不会缩小 28GB 文件**（DuckDB 高水位），真实收益要等离线重建时才兑现。

结论：本轮先把"冷库产物 + 校验工具 + DDL 删除清单"做扎实，物理切换单独一轮执行（预计 0.5 天），避免在主功能修复的同一轮里冒 schema 风险。
