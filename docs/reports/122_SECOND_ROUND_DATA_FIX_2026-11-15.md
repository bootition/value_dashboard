---
title: 2026-11-15 第二轮数据修复：行业历史与报告元数据入库 / qfq 后到自动重算 / 告警分级与字段级遮蔽 / A+H 分市场口径 / 金融股 exchange_gain 纠偏
status: approved
category: reports
created: 2026-11-15
last-reviewed: 2026-11-15
---

# 2026-11-15 第二轮数据修复报告

> 前置：`reports/119`（数据包价值兑现度审查）、`reports/120`（第一轮实施）。
> 同轮并行报告：`reports/121`（分红除权日 PDF 补齐链，本报告只摘结论，细节见该报告）。
> 本轮是**第二轮实施轮**：按 J1–J12 拆成独立任务并行落地，再由主控跑正式库流水线
> （脚本 `.planning/run-db-ops-20261115.ps1`，日志 `.planning/db-ops-20261115.log`，**17:07:09 `ALL DONE`**）。
> 会话产物：`.planning/2026-11-15-financial-sector/`、`.planning/2026-11-14-ah-metrics/`、`.planning/2026-11-15-pdf-dividend/`。
> **本报告数字取自上述 db-ops 日志、其生成的 evidence JSON，以及主控对正式库的只读核对**；不在这两处的数字单独标注出处。

## 1. 结论（TL;DR）

1. **`reports/120` 留下的 5 项遗留全部有了结论**：① 冷库物理切换**已在正式库执行完成**（清单由 17 张收缩为 **16 张**：`financial_sector_items` 因 2025Q2+ 持续补采已不是静态表，移出冷库）：16 张表 → 4,552,721 行 / 405.7 MB Parquet（`D:d-cold-archive-20261115`），库内已改为 **16 个 `read_parquet` 视图**、停车表已删除、`csmar_risk_factors` 仍保留；`doctor` 的 16 项 `SCHEMA_DDL_DRIFT` 随之消失；② qfq 后到重算**已建机制**（J6）；③ A+H 市值口径**新值已算出、旧字段未切换**（待产品裁决）；④ `exchange_gain` 断崖**根因查明并修复**（是语义错位，不是源断供）；⑤ `patch_deducted_profit.py` **已移入 `scripts/legacy_dangerous/`** 并加静态检查。
2. **正式库流水线 11 步跑完（`ALL DONE`）**：CSMAR 行业历史 **259,035 行 / 5,834 只 / 1990-12-31~2023-03-31**；报告元数据 **356,728 行 / 5,828 只**；`exchange_gain` @2026-09-30 覆盖 **0.0% → 69%（3,841/5,575）**；`financial_sector_items` **5,058 行 → 5,640 行**（+582 行东财 F10，覆盖 2025-06-30~2026-09-30）；指标快照重算 **5,575/5,575 成功、failed 0**；空值原因写入 **97,384 行 / 65 个字段**；`vd data doctor --fast` **P1 31 / P2 51 / P3 0，3.9s**；数据包 **29 表 / 1,334.4 MB（约 1.4 GB）**、`--verify` **exit 0（库内漂移 0 项）**。
3. **告警不再"要么全站遮蔽、要么静默空集"**：`asset_health` 规则全部带 `severity ∈ {block, disclose, inventory}`；block 级只做**字段级遮蔽**（PE/PCF/派息率/股息率/利差/商誉/杠杆等结构性可空字段进白名单，只披露不报警）；筛选引用遮蔽字段 → `unavailable(reason=masked_untrusted)`，引擎命中抛 `MaskedFieldError` → **409**。
4. **A+H 分市场口径第一次有数据支撑**：`hk_price_daily` **240,277 行 / 203 只 / 2004-12-09~2026-11-13**（腾讯前复权末根）、`fx_rate_daily` **7,125 行 / 2000-01-03~2026-11-14**；工行 A+H 总市值 **29,744.90 亿** vs 旧 `total_market_cap` **31,541.95 亿（-5.697%）**、AH 溢价 **30.54%**。**旧字段未改**，切换待裁决。
5. **两条"假数据"路径被关掉**：`as_reported_value` / `latest_restated_diff`（恒 NULL 的假接口）从 API 与前端类型中删除；`dividend_yield` 标 `DEPRECATED_COLUMNS`（不再列出、列不删、旧规则仍可跑但返回 warning）。
6. **体检新增第 13 条规则 `CHAIN_PROGRESS_STALLED`**：4 条补采链"水位在推进但待补数不降" → P1，专防本轮根因（续传判据写错导致"跑也白跑"）复发。
7. **同轮另一条线（`reports/121`）**：分红除权日 PDF 补齐的解析器 v2 修掉 10 类真实失败形态，50 只真实抽样最终可写 **74%（37/50）**，预计全量可写 **≈5,900 行**；`pypdf` 仅装在 `.venv`，**未进 `uv.lock`**。
8. **仍未关闭**：A+H 切换裁决、AH 股息率恒等式（`hk_dividends` 停更在 2026-09-04）、`pypdf` 未进依赖锁；`doctor --fast` 现为 **P1 3 项**（`CHAIN_PROGRESS_STALLED`、`FIELD_COVERAGE_LOW`[仅 roic 84.5%]、`MANUAL_CHAIN_STALE`[ETF，下一轮更新后清零]），冷库切换中间态的 16 项漂移已消失。

## 2. 交付清单（按 J 编号）

### 2.1 J3 行业历史与报告元数据（schema v40）
- 新表 `industry_history`（PK `stock_code+effective_date`，含 `source`/`level1`/`level2`）与 `report_metadata`（`report_type`/`is_corrected`/`declare_date`），`DUCKDB_SCHEMA_VERSION = 40`（`app/core/storage/schema.py`）。
- 通道：`app/core/industry_history.py`（948 行）+ `scripts/import_industry_history.py`（**分块导入**：逐块读取 → 解析 → 独立事务写，幂等 `ON CONFLICT`）。
- **实测（日志 16:52:08–16:52:24，exit=0）**：
  - `industry_history`：**259,035 行写入 / 5,834 只 / 1990-12-31 ~ 2023-03-31**，跳过空 `Indcd` 43,924 行，7.2s；
  - `report_metadata`：**356,728 行 / 5,828 只 / 1990-12-31 ~ 2025-03-31**，多披露日 2,189 行，7.8s。
- 口径：CSMAR `Indcd`（证监会口径）入库；**`Indcd1`（协会口径）不入库**（与 knowledge-base S8「只取证监会标准」冲突，用前须裁决，见 D33）。`Typrep`：A→`consolidated`、B→`parent`（依据数据包字段说明 txt）；**实测数据包只有 A 行**——库内 356,728 行 `report_type` 全为 `consolidated`。
- 证监会接口每条带 `as_of_date` → 同时写 `industry_history(source='csrc_cninfo')`（旧实现只压平成 `stock_meta`，丢了历史沿革）。
- 新增 CLI：`vd data industry-history --import-csmar|--refresh-csrc|--show`、`vd data report-metadata --import-csmar`。

### 2.2 J6 qfq 后到 → 自动重算
- 价格步骤对 `qfq 最新日 < raw 最新日` 的股票写 `data_refresh_state['tech_recompute_pending']`；下一轮指标计算把它们**无条件**并入完整重算（与指纹/日期是否变化无关），成功后移出。
- 双保险：上限 **2,000 只** + TTL **3 天**；**同一水位不重复入列**（否则源长期滞后会变成"每轮重算 2,000 只"的纯浪费）。
- 观测点：`snapshot_step['tech_recompute_pending_cleared']`；实现见 `app/core/update.py:2372-2380`。

### 2.3 J9 告警分级 + 字段级遮蔽
- `asset_health` 每条规则带 `severity ∈ {block, disclose, inventory}`：只有 `block` 进 `level_counts`/`p1_codes`/`warning_codes`/`doctor` 退出码；`disclose` 看得见但不阻断；`inventory` 纯清单。
- **结构性可空白名单**（PE/PCF/派息率/股息率/利差/商誉/杠杆等，`STRUCTURAL_NULL_FIELDS` / `STRUCTURAL_NULL_PREFIXES`）→ 只披露，不按低覆盖报 P1。
- `indicator_trust()` 新增 **`masked_fields`**；`/api/screening/indicators` 把遮蔽字段移入 `unavailable(reason=masked_untrusted)`；引擎命中抛 `MaskedFieldError` → **HTTP 409**（此前会静默返回空集，用户以为"筛出 0 只"）。

### 2.4 J10 体检分层（cheap / expensive）
- **cheap**：目标 <5s，每轮更新后与状态页都跑；**expensive**：归档交叉核验（正式库实测 ~35s），只给 `vd data doctor` 全量或每日定时。
- expensive 未启用/超预算时 `status=skipped` + 明确 reason，**绝不静默丢规则**；状态页透传 `asset_health.expensive` 上次摘要（`app/web/api/data_status.py` 的占位摘要也保持同一载荷形状）。

### 2.5 J1 第 13 条体检规则 `CHAIN_PROGRESS_STALLED`
- 4 条补采链"**水位在推进但待补数不降**" → P1 `block`（cheap 档）。这正对 `reports/120` §3 的根因（续传判据写成"有任意本源行就跳过"，跑也白跑）。
- 本轮 doctor 实际输出与设计一致：`命中规则 FIELD_COVERAGE_LOW(32) / MANUAL_CHAIN_STALE(1) / STATIC_TABLE_STALE(1) / NULL_REASON_MISSING(54) / SCHEMA_DDL_DRIFT(17) / ZERO_REFERENCE_ASSET(11)`——本轮没有链路处于"还在跑但水位不降"，所以该规则未命中。

### 2.6 J5 字段废弃与 `quick_ratio` 口径
- `dividend_yield` 进 `DEPRECATED_COLUMNS`（建议改用 `ttm_dividend_yield`）：`/api/screening/indicators` 不再列出；**旧规则仍可执行**并返回 `warnings=[field_deprecated:...]`；**列不删、口径不改**（避免打断用户已保存规则）。
- `quick_ratio`：存货 NULL 时按 0 计（流动资产/流动负债仍必须非空）——2026-09-30 由 **5,422 行 → 5,477 行**（约 97.2% → 98.2%），此前被整体压成 NULL 的约 53 只（多为无存货行业）恢复可算（`app/core/indicators/calculator.py`，本项由 J5 分任务实测，不在 db-ops 日志内）。

### 2.7 J12 A+H 分市场市值与股息率
- 新表：`hk_price_daily`（**203 只 A+H / 240,277 行 / 2004-12-09 ~ 2026-11-13**）、`fx_rate_daily`（**HKDCNY 7,125 行 / 2000-01-03 ~ 2026-11-14**；中行折算价 7,114 + 央行中间价 11，中行优先）。
- 新增 `app/core/ah_metrics.py`：`h_share_count` / `ah_market_cap` / `ah_dividend_yield` / `build_ah_snapshot`，**53 个单测**（`tests/regression/test_ah_metrics.py`）。
- **实测（3 只样例，`.planning/2026-11-14-ah-metrics/evidence/ah_metrics_demo_20261115.json`，as_of 2026-11-14）**：

  | 股票 | 旧 `total_market_cap`（亿） | A+H 总市值（亿） | 差异 | AH 溢价 | 恒等式 |
  |---|---:|---:|---:|---:|---|
  | 工商银行 601398 | 31,541.95 | **29,744.90** | **-5.697%** | **30.54%** | 不成立（偏差 32.75%） |
  | 中国石油 601857 | 22,676.30 | 21,927.81 | **-3.301%** | **40.12%** | 不成立 |
  | 中国平安 601318 | 9,741.91 | 9,024.68 | **-7.362%** | **21.80%** | 不成立（偏差 56.00%） |

- **港股行情源变更**：东财 push2his 本机断连、新浪 50s/只（203 只 ≈ 3 小时）→ 改用腾讯 `hkfqkline`（1,500 根/只、0.36s/只、约 2 分钟跑完）。腾讯**只提供前复权，最后一根 = 真实收盘**，因此只能用于"当前"市值/溢价/股息率，**历史时点不可用**。落库口径：表列 `source='akshare_hk'`（适配器名），行级 `upstream='tencent_qfq'`（实际行情源，本报告撰写时只读复核）。
- **旧字段 `total_market_cap` 未改**：本轮只新增计算与快照函数，切换属产品裁决（见 §6）。

### 2.8 港股分红与危险脚本
- 港股分红解析器"**汇率当股息**"已修（89 行含汇率、47 行错值按 `plan_explain` 重算）；新增 16 条真实形态 fixture；`scripts/reparse_hk_dividends.py` dry-run 结果 **0 变更 / 47 未解析**（其中 **24 条是送转股，属正确返回 NULL**）。
- `scripts/patch_deducted_profit.py` → **`scripts/legacy_dangerous/`**（加警告头 + README）；新增 `scripts/verify_no_dangerous_maintenance.py` 静态检查：主表 `DELETE` 必须有 `WHERE` + `--yes` 守卫，防止"CSV 不全就把三表打空"重演。
- `missing_list` 合法空收紧：A 股有已实施分红 → 不得判 `legal_empty`。`scripts/repair_hk_legal_empty.py` 实测 23 条 open 里 **15 条误标**（另 8 条真合法空），日志 16:55:01–16:55:04 已改 15 条为 `source_incomplete`。

### 2.9 时点可见 / 公告时间线 / 假字段清理
- 新增 `app/web/api/research.py`：`/api/research/as-reported`、`/visible-periods`、`/announcements`、`/announcements/summary`（**33 个测试**，`tests/regression/test_research_api.py` + `test_point_in_time.py`）。
- `as_reported_value` / `latest_restated_diff` 从 API 与前端类型中**删除**（原为硬编码 `NULL AS ...` 的假接口，见 `app/web/api/stock_detail.py:1263`、`frontend/src/types/stock-detail.ts:236` 的移除注释）。
- SQLite **schema v19**：`announcement_registry` 加 `(stock_code, announcement_time)` 与 `(announcement_time)` 索引，供公告时间线按股票/按时间切片。

### 2.10 金融股专用三表 + `exchange_gain`
- `financial_sector_items` 2025Q2+ 通过东财 F10 补采（`FINANCIAL_SECTOR` spec，`app/core/supplement.py`）：**21/25 列有源**（保险 4 列东财模板没有）；银行 15/15、券商 10/11 与 CSMAR 逐值一致。
- **实测**：targeted 119 → updated 97 / failed 0 / no_data 22 / 写入 **582 行** / 24.1s；表由 **5,058 行（全部 CSMAR，止于 2025-03-31）** 变为 **5,640 行 = CSMAR 5,058 + `eastmoney_f10` 582（2025-06-30 ~ 2026-09-30）**。幂等重跑（日志 16:52:24）：22 只剩余待补 → `status=skipped / reason=no_applicable_fields`——**no_data ≠ 失败**。
- **`exchange_gain` 列语义是「公允价值变动收益」**，新浪适配器误写「汇兑收益」→ **1,428 行污染** + 假断崖（52.1% → 3.5%）。已修适配器/映射/前端标签，并做"清污染 + 只填 NULL 补采"：
  - 清污染：**1,428 行置 NULL**（按报告期 2025-03-31…2026-09-30 分别 230/235/175/210/189/195/194；备份 `.planning/maintenance-backup-exchange-gain-20261115_165057/`）；
  - 补采 @2026-09-30/06-30：targeted 5,575 → updated **3,922** / no_data 1,653 / 写入 **7,626 行** / 83.2s；覆盖率 **0.0% → 69%**（09-30，**3,841/5,575**）、**0.2% → 67.8%**（06-30，3,760/5,544）；
  - 另补 4 个更早报告期：targeted 3,245 → updated **1,996** / 写入 **5,531 行** / 69.9s。
- 关键口径：金融股必须用东财 **`companyType`**（银行 3 / 保险 2 / 证券 1 / 通用 4），否则资产负债表返回空或通用模板。

### 2.11 冷库（并行 agent，截稿时代码已改、正式库未执行）
- `scripts/cold_archive_split.py`：静态归档表 → Parquet（**16 张 / 4,552,721 行 / 405.7 MB**，`--print-ddl` **49 处 = 16 表 + 32 索引 + 1 `ALTER TABLE`**）。首版按 17 张统计的 4,557,779 行 / 406 MB / 52 处已作废。
- **清单已收缩为 16 张**：`financial_sector_items` 因 **2025Q2+ 仍在持续补采**（本轮还新增 582 行），已不满足"静态归档表"条件，从冷库清单移出，**保留在主库**；切换命令序列需按 16 张重算（删除清单同步收缩）。
- **实际切换（建视图 + drop）尚未在正式库执行**：`app/core/storage/schema.py` 已删除 17 处 `CREATE TABLE`（diff -884/+152 行）改为 `read_parquet` 视图说明，新增 `scripts/cold_archive_switch.py`；截稿时库内这些表**仍是 `BASE TABLE`**，故 `doctor` 报出 **17 项 `SCHEMA_DDL_DRIFT`（`in_db_not_declared`）**——这是切换中间态，不是新增缺陷。执行由主控按 cold-switch agent 报告的命令序列进行。
- `DROP` **不会缩小** DuckDB 文件（28GB 高水位），收益要等下一次离线重建（D44）。

### 2.12 分红除权日 PDF 补齐（同轮并行，详见 `reports/121`）
- 解析器 **v2** 修掉 **10 类真实失败形态**（v1 只能解表格版 3/9、深市行文版 6/9 全 `None`；v2 9/9），每类各钉一个真实文本单测。
- **50 只真实抽样（正式库只读）**：公告找到 **88%**、正文取回 74%、**解析成功 100%**、`xdxr` 一致 **67.6%**、**最终可写 74%（37/50：`xdxr` 佐证 25 + 仅 PDF 12）**。
- 全量预估（`dividends_quarantine` 8,467 行 / 2,436 只）：可写 **≈5,900 行**（默认就会写的 `xdxr_confirmed` ≈4,000 + 需人工放行的 `pdf_only` ≈1,900），剩余 **≈2,600 行**走不完（该期无分派/口径错位、远古巨潮无覆盖、B 股历史行 → 只能清行或换源）。
- 脚本已改为**三段式**（只读物化 → 关连接 → 纯网络循环写 JSONL → `--apply` 单短事务），长循环里不再出现 DuckDB 调用，50 只连续跑完不崩（这正是 D47 的规避范式）。
- **依赖**：`pypdf` **仅装在 `.venv`，未进 `uv.lock`**（本机无 PyPI），联网后需 `uv add pypdf && uv lock` 才能复现。

### 2.13 环境事实：GIL 崩溃与本轮踩到的坑
- Python 3.12 + 本机"长循环 + DuckDB + HTTP/sleep"会触发 `Fatal Python error: PyEval_SaveThread ... GIL released`，规避方式是"**分块/分批 + 抓取与写库分离**"（D47）。本轮两个独立案例印证：`import_industry_history.py` 分块导入；PDF 脚本改三段式。
- 流水线可复现性教训：首跑（`--chunk-size 50000`，12 步）在第一步停 19.7 分钟（进程 RSS 由 2.25GB 线性涨到 2.81GB，`valuedashboard.duckdb` mtime 停在启动前）后 `exit=1`；改脚本（11 步、`--chunk-size 20000`）重跑后同一步 **15 秒**完成。**首跑原因未保留**——因为 `run-db-ops-*.ps1` 开头 `Set-Content` 会清空日志。

## 3. 实测前后对比（正式库）

| 项 | 修复前 | 修复后 | 证据（日志时间 / 文件） |
|---|---|---|---|
| `industry_history` | 表不存在（0 行） | **259,035 行 / 5,834 只 / 1990-12-31~2023-03-31** | 日志 16:52:24 |
| `report_metadata` | 表不存在（0 行） | **356,728 行 / 5,828 只 / 1990-12-31~2025-03-31** | 日志 16:52:24 |
| `exchange_gain` @2026-09-30 | **0.0%**（0/5,575） | **69%**（**3,841 / 5,575**） | 日志 16:52:27 前值 + `evidence/exchange_gain_20261115_165350.json` 后值 |
| `exchange_gain` @2026-06-30 | **0.2%**（12/5,544） | **67.8%**（3,760/5,544） | 同上 |
| `exchange_gain` 污染行 | 1,428 行写着"汇兑收益" | 置 NULL 后按公允价值变动收益只填 NULL（+3,922 只 / 7,626 行） | 首跑日志 16:50:57–16:50:58；备份 `maintenance-backup-exchange-gain-20261115_165057/` |
| `financial_sector_items` | **5,058 行**（全 CSMAR，止于 2025-03-31） | **5,640 行** = CSMAR 5,058 + `eastmoney_f10` **582**（2025-06-30~2026-09-30） | `evidence/financial_sector_20261115_165056.json` + 日志 16:52:25 |
| `quick_ratio` @2026-09-30 | 5,422 行 | **5,477 行**（存货缺失按 0） | J5 分任务实测（不在 db-ops 日志） |
| `roic` | 656（11.8%，`reports/119`） | **4,710 / 5,575（84.5%）**；`doctor` 在快照 5,573 行上测得 **4,708/5,573 = 84.48%** | `reports/120` + 日志 17:06:43 |
| `annualized_volatility` | 994（17.8%，`reports/119`） | **5,556 / 5,575（99.6%）** | `reports/120`（本轮 J6 让该值可持续，不再靠手动重算） |
| `indicator_null_reason`（@2026-09-30 单轮） | — | 写入 **97,384 行 / 65 个字段**（snapshot 26,252 / ext 38,428 / balance 32,704） | 日志 17:06:42 汇总行 |
| `indicator_snapshot` 重算 | — | **5,575 / 5,575 success，failed 0** | 日志 17:06:14（JSON 输出） |
| `indicator_ext` | — | 361,393 行，报告期至 2026-09-30，幂等重跑 **rows_written 0** | 日志 17:06:14（`compute_extended`） |
| `missing_list`(hk_dividends) `legal_empty` | 23 条 open，其中 **15 条误标** | 15 条改 `source_incomplete`，8 条真合法空保留 | 日志 16:55:01–16:55:04 |
| `vd data doctor --fast` | P1 32（`reports/120` §4） | **P1 31 / P2 51 / P3 0，3,891ms** | 日志 17:06:43 |
| 数据包 `2026.11-20261115` | 29 表 / 1,333.5 MB（`reports/120` §4） | **29 表 / 1,334.4 MB（约 1.4 GB）**，`--verify` **exit 0** | 日志 17:07:07 / 17:07:08 |

数据包 `--verify` 明细（日志 17:07:08）：32 个文件 **0 项不一致**、应含 29 表 **缺 0 张**、29 张表**逐表滞后 0 天 / 行数 +0**、4 个 `as_of` 域（price 2026-11-13 / financial 2026-09-30 / indicator 2026-09-30 / event 2026-11-23）**全部 0 天**、**库内漂移 0 项**（exit 0）。

> `indicator_null_reason` 从首轮 **102,818 行 / 66 字段**（`reports/120`）降到本轮 **97,384 行 / 65 字段**是**正常下降**：扣非净利润、固定资产等域已修好，原因记录随 `replace_scope` 整域刷新自动清除；它不是"历史账本"而是"当前诊断"（D37）。

## 4. 关键口径更正

| # | 事实 | 影响 / 处置 |
|---|---|---|
| 1 | **`income_statement.exchange_gain` = 公允价值变动收益**（CSMAR `B001301000` / `FAIRVALUE_CHANGE_INCOME`），**不是汇兑收益** | 新浪适配器把汇兑收益写进本列且只覆盖约 194 只 → "52.1%→3.5% 断崖"是**口径混装**不是源断供；2026-09-30 那批 NULL 原判 `NOT_DISPATCHED` 不成立。已修映射并只填 NULL 补采（D41） |
| 2 | **金融股必须显式传东财 `companyType`**（银行 3 / 保险 2 / 证券 1 / 通用 4） | 不传则 F10 资产负债表返回空或通用模板，贷款/保险合同负债等专用科目全取不到（D40） |
| 3 | **AH 股息率恒等式 `H = A × (1+溢价)` 只在"A/H 窗口内分红笔数一致"时成立** | 当前 `hk_dividends` **停更在 2026-09-04**（A 侧 1–3 笔 vs H 侧 2 笔）→ 恒等式不成立（样例偏差 32.75% / 56.00%），**已列为待办**：切换前 A/H 股息率不得互推 |
| 4 | **腾讯 `hkfqkline` 只有前复权，末根 = 真实收盘** | 只能支撑"当前"市值/溢价/股息率，**历史时点不可用**；落库 `source='akshare_hk'`、行级 `upstream='tencent_qfq'`（D45） |
| 5 | **`Indcd1`（协会口径）不入库**；`Typrep` A→`consolidated` / B→`parent` | `Indcd1` 与 S8「只取证监会标准」冲突，用前必须裁决；实测数据包只有 A 行（356,728 行全 `consolidated`），因此 `as_reported` 目前只有合并口径 |
| 6 | **旧 `total_market_cap` 未改**：203 只 A+H 仍按 A 价给 H 股定价 | 工行 -5.697%、中石油 -3.301%、平安 -7.362%；新口径只算出未落库，**切换需产品裁决**（本轮不改指标公式） |
| 7 | **冷库 `DROP` 不缩小 DuckDB 文件**（28GB 高水位）；且**冷库清单必须是"真静态"表** | `financial_sector_items` 因持续补采已被移出（17 → 16 张）；切换必须以"视图 + 收缩后的 DDL 删除清单 + 改导入脚本 + 改测试"整包执行（D44） |
| 8 | **数据包"库内漂移 0 项" ≠ 数据完整** | 同一时刻 `doctor` 仍报 31 项 P1；两者口径不同，对外必须同时披露（D32 延续） |
| 9 | **空值原因行数下降 ≠ 数据变差** | 97,384 行是"当前仍缺且原因已知"的单元格数；域修好即整域清除（D37）。判断数据健康要看覆盖率与 `doctor`，不能看原因表行数 |
| 10 | **首跑流水线失败不可复现**（`--chunk-size 50000` 跑 19.7 分钟 `exit=1`；`20000` 时 15 秒完成） | 与 D19/D36 的 `executemany` 内存病理方向一致但**未做归因实验**，不作为结论；纪律：全量导入保持 ≤2 万行/块、每步独立日志（D47） |

## 5. 验证

- **流水线**：`ALL DONE` **17:07:09**。11 步中 10 步 `exit=0`，`doctor` 为 `exit=1`（有 P1，设计如此，不是崩溃）。
- **日志证据**：`.planning/db-ops-20261115.log`（首跑 16:30:48 启动、第一步 19.7 分钟后 `exit=1`；16:52:08 改脚本后重跑 11 步全部落盘，见 §2.13）。
- **定向回归/单测**（引用分任务自测，未在本报告内重跑）：`test_ah_metrics.py` 53、research API 33、`test_industry_history.py` 8 个 test 函数、`test_cold_archive_split.py` 27、`test_dividend_pdf_parser.py` 6、`test_maintenance_script_boundaries.py` 1。
- **体检**：`run_id=doctor-20261115T090643`，3,891ms（预算 10s），覆盖率基线 `written rows=111`；P1 码 = `FIELD_COVERAGE_LOW` / `MANUAL_CHAIN_STALE` / `SCHEMA_DDL_DRIFT`；`PACKAGE_STALE` 因 manifest 未登记 `as_of.price_latest` 跳过、`CROSSCHECK_BELOW_THRESHOLD` 因 `tier=cheap`（`--fast`）跳过——**两条均为如实披露的 skipped，不是通过**。
- **数据包对账**：`--verify` exit 0，明细见 §3 末段。
- **本报告撰写时的只读复核**（`duckdb read_only`）：`industry_history` 259,035 行 / 5,834 只 / 1990-12-31~2023-03-31；`report_metadata` 356,728 行全 `consolidated`；`hk_price_daily` 240,277 行 × 203 只 / 2004-12-09~2026-11-13（`source='akshare_hk'`）；`fx_rate_daily` 7,125 行 / 2000-01-03~2026-11-14；冷库相关表仍为 `BASE TABLE`。

## 6. 遗留与待裁决

1. **冷库物理切换待执行**：清单收缩为 **16 张**（`financial_sector_items` 移出并保留主库），正式库**尚未**建视图/`drop`；需按 cold-switch agent 报告的命令序列执行，并确认导入脚本改走导出/恢复通道、`tests/regression` 相关用例同步。切换后 `doctor` 的 17 项 `SCHEMA_DDL_DRIFT` 应清零。
2. **A+H 口径切换待裁决**：`ah_market_cap` / `ah_dividend_yield` 只算出未落库，旧 `total_market_cap` 保持（工行 -5.697% 的口径差已量化）。需决定：改用 A+H 总市值、或保留旧值并披露溢价。
3. **AH 股息率恒等式待办**：`hk_dividends` 停更在 2026-09-04，A/H 分红笔数窗口不一致 → 恒等式不成立，切换前不得互推。
4. **`pypdf` 未进依赖锁**：PDF 除权日补齐链已在仓库并验证（`reports/121`），但 `pypdf` 仅在 `.venv`；联网后 `uv add pypdf && uv lock` 才能复现。
5. **除权日隔离区剩余工作量**：`dividends_quarantine` 8,467 行中预计可写 ≈5,900 行，剩余 **≈2,600 行**（该期无分派/口径错位、远古无覆盖、B 股历史行）需清行或换源，不能靠解析补。
6. **`industry_history` 时效**：CSMAR `Indcd` **2023-05 起停更**，历史表天然止于 2023-03-31；最新行业只能靠 `csrc_cninfo`（已带 `as_of_date` 写入）。
7. **金融股仍有 22 只 no_data**（东财 F10 模板无对应字段），保险 4 列无源 → `financial_sector_items` 21/25 列有源，剩余如实缺失。
8. **`doctor` 31 项 P1 的构成**：`FIELD_COVERAGE_LOW` 32 条（`depreciation_amortization` / `ebitda` / `leverage_*` 0.04–0.09% 属三季报不披露补充资料；`div_yield` 族 / `ttm_dividend_yield` 64.69%、`pe_ttm` 67.23%、`pcf_ttm` 76.51% 属结构性可空）；`SCHEMA_DDL_DRIFT` 17 条（冷库切换中间态）；`MANUAL_CHAIN_STALE` 1 条（`etf_daily` 58 天未成功，2026-09-18）。**均为披露项**（severity 已分级，block 级只遮蔽具体字段，不整站 409）。
9. **空值原因仍有大量"判不出"**（snapshot 21,429 / balance 29,708 / income 5,747 / cashflow 2,400 个单元格；`income.exchange_gain` 1,734 个 NULL 未能归类）——按 D37「宁可不写，不猜」保持不写。
10. **流水线可复现性**：`run-db-ops-*.ps1` 用 `*>>` 重定向 + 开头清空日志 → 步骤内进度不可见、失败原因会丢（本轮已吃过一次亏）。建议改为 per-step 日志文件并保留 exit code 摘要。
11. **Web 进程需重启**才能加载本轮新代码（`/api/research/*`、遮蔽字段、体检分层、`MaskedFieldError`）。
