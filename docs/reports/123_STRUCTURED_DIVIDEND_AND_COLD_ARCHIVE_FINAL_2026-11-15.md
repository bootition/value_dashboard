---
title: 2026-11-15 结构化分红源落地与冷库 17 张收官（除权日改源 / 隔离区 8,467→4,087 逐行闭合 / A+H 口径定稿）
status: approved
category: reports
created: 2026-11-15
last-reviewed: 2026-11-15
supersedes: docs/reports/121_DIVIDEND_EX_DATE_PDF_RESOLUTION_2026-11-15.md
---

# 2026-11-15 结构化分红源落地与冷库 17 张收官

> 前置：`reports/122`（第二轮数据修复）、`reports/120`（数据资产修复）、`reports/119`（数据包价值兑现度审查）。
> **被本报告取代**：`reports/121`（分红除权日 PDF 补齐链，已标 `status: superseded`）——作废理由见 §2。
> 证据来源：正式库**只读**复核（2026-11-15 21:40–21:55）、`.planning/dividend-fetch.log` + `.planning/dividend-apply.log`（末行 `21:40:24 ALL DONE`）、`.planning/div/events.jsonl` + `.planning/div/events.coverage.jsonl`、冷库包 `D:/vd-cold-archive-20261115-r17/manifest.json` + `verified.json`。
> apply 前备份：`.planning/maintenance-backup-dividend-20261115_212720/`（`dividends.parquet` + `dividends_quarantine.parquet`，会话产物、gitignore）。
> 本轮**只改文档**：本报告 + `docs/STATUS.md` + `docs/runbooks/ops-knowledge-base.md`；代码/测试/config/data 零改动。

## 1. 结论（TL;DR）

1. **分红除权日改走结构化源，PDF 方案整体作废**：巨潮 `akshare.stock_dividend_cninfo` 与东财 `akshare.stock_fhps_detail_em` 直接返回「股权登记日 / 除权日 / 派息日 / 每 10 股派息」，可追到 1992 年（万科 1992-03-20、方正科技 1993-03-22）。`scripts/resolve_dividend_ex_dates.py`、`app/core/pdf/dividend_parser.py`、`tests/regression/test_dividend_pdf_parser.py` 已**删除**，`reports/121` 标 `superseded`（§2）。
2. **新链路是"两段式"，默认不写库**：`scripts/fetch_dividend_history.py`（只抓、串行限速 `--sleep 0.35`、**不 import duckdb**）+ `scripts/apply_dividend_history.py`（默认 dry-run，`--yes` 才写；写前 `COPY` 备份 `dividends` + `dividends_quarantine`，**单事务**）（§3.1）。
3. **正式库实测（2026-11-15 21:00–21:40）**：抓取 **6,246 只**（在市 5,579 + 隔离区独有 667 只退市/B 股）→ **57,870 个事件**，去重后 **57,460 个 `(stock_code, ex_date)`**，`failed=0`；`dividends` **57,979 → 58,600 行**（插入 **621**、更新金额 **271**、回填 NULL **28**），有分红记录的股票 **5,543 只**；`dividends_quarantine` **8,467 → 4,087**，**8,467/8,467 逐行有去向**（§3）。
4. **隔离判定本身是错的**：抽样 5 条隔离记录，**4 条占位日本来就是真除权日**（900922 / 200541 / 300339 / 600713），隔离的不是"数据缺失"，而是"从没问过结构化源"（§2.3）。
5. **冷库 17 张全部切换完成**：`csmar_risk_factors`（67,193 行）补登记并完成物理切换，`schema.py` 删除 **3 处** DDL；冷库包 `D:/vd-cold-archive-20261115-r17` = **17 张 / 4,619,914 行 / 411.4 MB**，`--verify-cold` 通过；库内 **17 个 `read_parquet` 视图 + 0 张停车表**，`csmar_fi_t1` 仍可查 302,959 行（§4）。
6. **A+H 口径按行业惯例定稿：`total_market_cap` 不改动**。腾讯实时行情核实工行 31,541.95 亿 / 平安 9,741.91 亿 / 中石油 22,676.30 亿，与库内 `total_market_cap` **完全一致**，说明行业口径就是"**A 股价格 × 总股本**"；新增 `h_share_market_cap_cny`（H 价 × H 股本 × 汇率，**196/203 只有值**）；AH 股息率改**各算各的**，恒等式降级为参考值，**任何情况下不因恒等式失败丢弃股票**（§5）。
7. **三条加固纪律（都有实测依据）**：① 删除类分支只在 `stock_meta.is_listed=TRUE` 时执行（本批 5,001/8,467 行属已退市股票，源本来就覆盖不到）；② 现金金额未知时**绝不写 0**；③ 新增"回填 NULL"通道（只填空值，不覆盖既有值）（§6）。
8. **验证**：`ttm_dividend_yield` 非空 **3,737** 行、`indicator_null_reason` **96,395** 行；`vd data doctor --fast` **P1 2 / P2 40 / P3 0（3,155ms，`run_id=doctor-20261115T134020`）**，两个 P1 是 `CHAIN_PROGRESS_STALLED`（`balance_sheet_ext` 待补 105 不降）与 `FIELD_COVERAGE_LOW`（`roic` 84.48%），`SCHEMA_DDL_DRIFT` 已随冷库切换清零；本轮**零新增依赖**（PDF 方案作废所以不需要 `pypdf`），`uv lock` 在可联网后成功解析 **80 个**依赖（§7）。
9. **仍未完成**：隔离区剩余 4,087 行是"源覆盖不到"而非未知空洞（已全部标注原因）、B 股派息口径待核、AH 恒等式四态分布存在两版数字待裁决、冷库 `DROP` 不缩文件（§8）。

## 2. 分红除权日：为什么放弃 PDF 方案

### 2.1 作废与删除清单

| 项 | 状态 | 依据 |
|---|---|---|
| `scripts/resolve_dividend_ex_dates.py`（1,023 行） | **已删除** | `git status` 显示 `D`；`scripts/` 下不再有该文件 |
| `app/core/pdf/dividend_parser.py`（398 行） | **已删除** | 同上 |
| `tests/regression/test_dividend_pdf_parser.py`（348 行） | **已删除** | 同上 |
| `reports/121` | **`status: superseded`**（`superseded-by` 指向本报告） | 该文件 front-matter |
| `pypdf` 依赖 | **不再需要**（不进 `pyproject.toml` / `uv.lock`） | 解析链已删；本轮未新增任何依赖 |

> `reports/121` 仍然有效的部分只有一条：§2 的「长循环 + DuckDB 连接 + HTTP/sleep 必崩」根因与三段式隔离结论，已登记为 knowledge-base **D47**，结构化源脚本同样遵守（`fetch_dividend_history.py` 完全不 import duckdb）。

### 2.2 结构化源直接给字段，没有理由再从正文里抠

- **巨潮 `akshare.stock_dividend_cninfo`**（主源）：实施方案公告日期 / 分红类型 / 送股比例 / 转增比例 / **派息比例** / **股权登记日** / **除权日** / **派息日** / 股份到账日 / 分红说明 / 报告时间；深历史可用（万科 **1992-03-20**、方正科技 **1993-03-22**）。
- **东财 `akshare.stock_fhps_detail_em`**（备用/交叉核对）：报告期 / 现金分红比例 / 股权登记日 / 除权除息日 / 方案进度；字段更少、**无派息日**。
- 两家单位口径**都是「每 10 股」**（派息、送股、转增同理），落库 `dividends.*_per_share` 统一 ÷10，由 `apply` 阶段唯一一处 `per_10_to_per_share` 完成（`scripts/apply_dividend_history.py`）。

### 2.3 抽样证据：5 条里 4 条日期本来就是对的

同批复核（隔离表 = apply 前的 COPY 备份 `dividends_quarantine.parquet`；源侧 = `.planning/div/events.jsonl`，巨潮结构化记录）：

| # | 股票 | 隔离占位日 | 隔离金额（元/股） | 源内同日事件 | 源口径（每 10 股 → 每股） | 判定 |
|---|---|---|---:|---|---|---|
| 1 | 900922 | 2026-06-30 | 0.002639 | 有（2026-06-30） | `cash_per_10=0.02639` → 0.002639 | **占位日本身就是真除权日**，金额一致 |
| 2 | 200541 | 2026-06-30 | 0.057162456 | 有（2026-06-30） | `cash_per_10=0.57162456` → 0.057162456 | **同上，一致** |
| 3 | 300339 | 2025-12-31 | 0.07 | 有（2025-12-31，报告期 2025-09-30） | `cash_per_10=0.7` → 0.07 | **同上，一致** |
| 4 | 600713 | 2025-06-30 | 0.17 | 有（2025-06-30） | `cash_per_10=1.7` → 0.17 | **同上，一致** |
| 5 | 000001 | 1999-12-31 | 0.6 | 无（该股 1999 年真除权日是 **1999-10-18**，10 派 6 元） | `cash_per_10=6.0` → 0.6 | 金额对、**日期是假占位**（应走 `deleted_fake`） |

结论：**4/5 的"隔离行"数据本来就是对的**。所以 `reports/121` 按"解析 PDF 才能写"估算的缺口（全量 ≈5,900 行可写、≈2,600 行走不完）整体作废——真实情况是**大部分行只需要向结构化源核对一遍**。

### 2.4 顺带纠正的两处旧结论

- `reports/121` §3.2 的"最终可写 74%（37/50）"是**PDF 解析命中率**，不是数据缺口率；本轮实测 8,467 行里只有 **4,087 行（48.3%）需要保留**，且全部带原因。
- `reports/121` §7.2 的分层外推（"≤2005 只有 33.3% 可写"）建立在"PDF 正文能否取回"上；结构化源里 1990s 记录是齐全的（见 §2.3 第 5 行的 1999 年事件），**旧外推不得再引用**。

## 3. 结构化抓取与隔离区清理（逐行闭合表）

### 3.1 抓取（只抓不写库）

| 项 | 实测值 | 说明 |
|---|---:|---|
| 抓取范围 `universe` | **6,246 只** | 在市 **5,579**（`stock_meta.is_listed=TRUE`）+ 隔离区独有的 **667** 只退市/B 股 |
| `ok` | **5,531** | 有返回且至少一条可用除权日 |
| `empty` | **714** | 源确定没有该股分红记录 |
| `partial` | **1** | 有返回但无可用除权日（不可判定） |
| `failed` | **0** | 源侧零失败 |
| 事件数 `events` | **57,870** | 去重后 **57,460** 个 `(stock_code, ex_date)`：完全重复丢 1 行、同日两笔合并 42 组（84 行，金额求和） |
| 耗时 | **3,724.7s（≈62 分钟）** | 串行 `--sleep 0.35`，实测 1.68 只/秒 |

> **一处数字取舍**：任务书引用的 `ok=5,436 / empty=713 / 61 分钟` 是日志 `[6150/6246]` 的**进度行**（`elapsed=3,669.1s`）；**结束汇总**（`.planning/dividend-fetch.log` 末行）与 `events.coverage.jsonl`（apply 实际消费的覆盖度 sidecar，逐只一行，可独立复算）都是 `ok=5,531 / empty=714 / partial=1 / failed=0`、`elapsed=3,724.7s`。`universe=6,246`、`events=57,870`、`failed=0` 三处完全一致。本表按**结束汇总 / sidecar** 写。

命令（可复现，两段式且抓取段不碰库）：

```bash
# 1) 抓取：只写 JSONL + coverage sidecar，不 import duckdb
Get-Content .planning/div/codes.txt | .venv/Scripts/python.exe scripts/fetch_dividend_history.py \
  --out .planning/div/events.jsonl --sleep 0.35

# 2) 写库：默认 dry-run，--yes 才写（写前 COPY 备份两张表、单事务、写锁检查）
.venv/Scripts/python.exe scripts/apply_dividend_history.py --in .planning/div/events.jsonl
.venv/Scripts/python.exe scripts/apply_dividend_history.py --in .planning/div/events.jsonl --yes
```

### 3.2 `dividends` 写入通道（脚本输出原文，日志 21:27:18）

- 插入 **621**（其中现金口径未知按 **NULL** 写入 **18** 行）
- 更新金额字段 **271**（金额差 > 容差 0.001；其它字段非空不覆盖）
- 金额口径未知跳过 **1,867**（源里派息比例为空，如 B 股；库里已有值一律不动）
- 回填 NULL 字段 **28**（金额已一致，只补 `announcement_date` / 送股 / 转增的空值；`--no-fill-nulls` 可关）
- 一致跳过 **56,540**
- 另有前置跳过 **367**（无 `ex_date` 0 / 无代码 0 / 派息送转全为 0 367）

> 各计数口径不同（"去重后事件数"vs"与库逐行比对"），本表只登记脚本输出、不做事后加总。
> 表规模是自洽的：**57,979 + 621（插入）= 58,600 行**，更新与回填不改行数；`COUNT(DISTINCT stock_code)` 复核 = **5,543 只**。

### 3.3 隔离区逐行闭合（8,467 → 4,087，无一行去向不明）

| 归类 | 行数 | 判定依据 | 处置 |
|---|---:|---|---|
| `matched` | **403** | 源里有同 `(stock_code, ex_date)` 事件且金额对得上（其中隔离行金额本来是空的 **5** 行） | 移出隔离 → 写 `dividends`（金额以源为准） |
| `deleted_fake` | **3,977** | 源里没有该日事件，但该股"报告期 == 占位日"有事件 → 占位期末不是除权日（报告期命中 **3,652** / 未命中 **325**） | 删除隔离行 |
| `deleted_empty` | **0** | 源里该股根本没有分红记录 | 本轮 **0**（见 §6 ①：退市股不走这一支） |
| `unavailable` | **0** | 源没回应（`failed` 才会进） | 本轮 **0**（`failed=0`） |
| `kept_coverage_gap` | **4,085** | 该股非在市 / 源覆盖不到 → 保留并改 `reason=source_coverage_gap` | 源返回空 **3,697** + 报告期已命中但占位日不是除权日 **388** |
| `kept_not_verifiable` | **2** | 源有返回但没有任何可用除权日 | 保留原样（`unverified_period_end_placeholder`） |
| **合计** | **8,467** | 403+3,977+0+0+4,085+2 = **8,467** | 移出/删除 **4,380** 行，保留 **4,087** 行 |

剩余 4,087 行的原因分布（只读复核 `GROUP BY quarantine_reason`）：**`source_coverage_gap` 4,085 + `unverified_period_end_placeholder` 2**——即隔离区里**不再有"原因未知"的行**。

### 3.4 为什么剩余 4,087 行不能靠再抓一遍解决

- **5,001/8,467 行属于已退市股票**（`stock_meta.is_listed=false`）：巨潮/东财对退市股**本来就没有覆盖**（实测 600087 `*ST 长油` 隔离区有 12 行真实派息 0.35/0.5/0.3…，巨潮返回 0 行、东财直接抛异常）。
- 3,697 行是"源确实返回空"——**空 ≠ 缺数据**，这些股票该报告期真的没有分派或源侧无历史。
- 因此剩余行的正确处置是"**保留 + 标注原因**"，而不是删除；要再降只能换源（CSMAR 派息历史）或人工逐条裁决（§8）。

## 4. 冷库 17 张全部切换

### 4.1 清单与冷库包

| 项 | 上一轮（`reports/122`） | **本轮（2026-11-15 完成）** | 依据 |
|---|---|---|---|
| 清单张数 | 16 张（`csmar_risk_factors` 留主库） | **17 张，全部 `physical_switch: switched`** | `config/cold_archive.yaml` |
| 冷库包 | `D:/vd-cold-archive-20261115`（16 张 / 4,552,721 行 / 405.7 MB） | **`D:/vd-cold-archive-20261115-r17`：17 张 / 4,619,914 行 / 411.4 MB** | 包内 `manifest.json`（`sum(rows)`、`sum(size_bytes)`）、`verified.json` |
| 校验 | `--verify` | **`--verify-cold` 通过**（manifest 与 parquet 逐表一致，`manifest_sha256=515c86d5…`） | `scripts/cold_archive_switch.py --verify-cold` |
| `schema.py` DDL | 17 处改为视图说明（中间态） | **`csmar_risk_factors` 的 3 处 DDL 全部删除**：`CREATE TABLE` + DDL 文本里的 `CREATE INDEX` + `init_duckdb_schema()` 里的 `CREATE INDEX` | `git diff app/core/storage/schema.py`（`-35/+…`，注释指向 `reports/123`） |
| `doctor` | 有 `SCHEMA_DDL_DRIFT`（`reports/122` §2.11 记 17 项，首次切换后 16 项） | **0 项**（P1 里已无 `SCHEMA_DDL_DRIFT`） | 日志 21:40:20 `P1 2 / P2 40 / P3 0` |

17 张明细（行数取 `manifest.json`，合计 4,619,914）：

| 表 | 行数 | 是否零引用（`config/asset_reference.yaml`，只算 `app/**` 运行时引用） |
|---|---:|---|
| `csmar_fi_t1` | 302,959 | —（消费者 `archive_crosscheck`） |
| `csmar_fi_t3` | 302,959 | —（消费者 `archive_crosscheck`） |
| `csmar_fi_t4` | 304,505 | —（消费者 `archive_crosscheck`） |
| `csmar_fi_t5` | 305,273 | —（消费者 `archive_crosscheck`） |
| `csmar_fi_t6` | 301,294 | **是**（口径不同，未登记为可核验概念） |
| `csmar_fi_t8` | 302,716 | **是** |
| `csmar_fi_t9` | 303,065 | —（消费者 `archive_crosscheck`） |
| `csmar_fi_t10` | 264,772 | **是** |
| `csmar_fi_t11` | 260,092 | **是** |
| `csmar_far_finidx` | 76,262 | **是** |
| `csmar_balance_items` | 356,709 | **是** |
| `csmar_income_items` | 354,877 | **是** |
| `csmar_cashflow_items` | 339,035 | **是** |
| `csmar_financial_items` | 232,421 | **是** |
| `csmar_per_share_history` | 302,580 | —（消费者 `archive_crosscheck`） |
| `csmar_disclosure_metrics` | 243,202 | **是**（只有 `scripts/export_data_package.py` 读取，不计 app 运行时引用） |
| `csmar_risk_factors` | 67,193 | **是**（同上；本轮补登记并完成切换） |
| **合计** | **4,619,914** | **11 张零引用** + 6 张有 app 运行时消费者（`app/core/quality/archive_crosscheck.py`） |

### 4.2 库内现状（只读复核）

- `information_schema.tables`：`csmar*` 视图 **17 个**；`%__cold_parked` **0 张**——即 **17 个 `read_parquet` 视图 + 0 张停车表**。
- 视图仍可查：`csmar_fi_t1` **302,959 行**、`csmar_risk_factors` **67,193 行**（与 manifest 一致）。
- 冷存对用户可见能力零影响：这批表**不进筛选界面**（`config/cold_archive.yaml` 域纪律）。

### 4.3 11 张零引用表：退役 ≠ 删除

`config/asset_reference.yaml` 的 11 条零引用记录**每条都加 `cold_archive_state`**（含恢复方式），文件头明确写清"**退役 = 迁冷库（Parquet + `read_parquet` 视图），不是 `DROP TABLE`**"，并给出两种恢复方式：

```sql
-- 方式 A：脚本逆向（DROP VIEW + 把 <表名>__cold_parked 改名回来）
python scripts/cold_archive_switch.py            # 见 --help
-- 方式 B：已删停车表时，从 Parquet 直接重建
CREATE TABLE csmar_risk_factors AS
  SELECT * FROM read_parquet('D:/vd-cold-archive-20261115-r17/csmar_risk_factors.parquet');
```

### 4.4 两个必知坑（本轮再次验证）

1. **带索引的表不能 `ALTER TABLE ... RENAME`**（DuckDB 1.5.5 `Dependency Error`）——必须先 `DROP INDEX`，脚本已内置 `_indexes_on`。
2. **`CREATE INDEX` / `ALTER TABLE` 打在视图上会抛错**，而 `init_duckdb_schema()` 每次启动都跑，所以 DDL 删除清单必须与**切换后的清单**严格同步；漏一处，`--create-views` 会因残留 DDL **整体拒绝**（`EXIT_REFUSED`，有意的安全门）。

## 5. A+H 口径按行业惯例定稿

### 5.1 行业口径核实：`total_market_cap` 就是行业算法，不切换

| 股票 | 腾讯实时总市值（亿） | 库内 `total_market_cap`（亿，2026-09-30） | 差异 |
|---|---:|---:|---:|
| 工商银行 601398 | 31,541.95 | **31,541.95** | **0** |
| 中国平安 601318 | 9,741.91 | **9,741.91** | **0** |
| 中国石油 601857 | 22,676.30 | **22,676.30** | **0** |

结论：A 股页面「总市值」= **A 股价格 × 总股本**（不按 A/H 分开算），与库内字段完全一致 → **`total_market_cap` 不改、不切换**，`app/core/indicators/calculator.py` **零改动**。`reports/122` §2.7 的"AH 分市场总市值比旧值低 5.7%/3.3%/7.4%"是**另一套口径**（A 部分 + H 部分），**不是旧字段算错**。

### 5.2 新增 `h_share_market_cap_cny`：H 股单列（行业软件也单列"港股市值"）

`h_share_market_cap_cny` = **H 股价格 × H 股股本 × HKD→CNY 汇率**，单位**人民币元**，是独立曝光字段、不是 `total_market_cap` 的拆分，也不回写快照：

| 股票 | `h_share_market_cap_cny`（亿人民币） | `total_market_cap_ah`（亿，参考量） | AH 溢价（%） |
|---|---:|---:|---:|
| 工商银行 601398 | **5,884.22** | 29,744.90 | 30.54 |
| 中国石油 601857 | **1,865.67** | 21,927.81 | 40.12 |
| 中国平安 601318 | **3,289.56** | 9,024.68 | 21.80 |

覆盖：**196/203 只**有值（其余 7 只缺 A 价/H 价/汇率/H 股本之一，如实为 `None` 并带 `reason`）。

### 5.3 AH 股息率"各算各的"，恒等式降级为参考值

- A 用 A 的分红与 A 的股价、H 用 H 的分红与 H 的股价，**各自独立**；任一侧缺数据只让**该侧**为 `None`。
- `identity_status` 四态：`ok` / `not_comparable`（两端分红笔数不一致）/ `mismatch`（笔数一致但偏离 > 5%）/ `insufficient_data`。
- **硬纪律：任何情况下不因恒等式失败丢弃股票**——203 只里恒等式不成立是**正常现象**（A 窗口 1 次、H 2 次这类），不是数据错误。
- 口径纪律写在 `app/core/ah_metrics.py` 顶部 docstring §1–§3（该处行情核实自标"2026-11-16"，与本表相差一天，数值与结论一致）。

> **四态分布存在两版数字（需裁决）**：任务书给出 `ok **164** / not_comparable **32** / insufficient_data **7** / mismatch **0**（当前口径下没有"两边笔数一致但金额对不上"的股票）`；本报告撰写时（21:50）用**当前工作区** `app/core/ah_metrics.py` 对同一份库数据重跑 `build_ah_snapshot`（只读，`as_of=2026-11-14` 与默认今日结果相同）得到 `ok 164 / not_comparable 32 / insufficient_data 7`，且 601788 落在 `not_comparable`。差异只可能来自"`ok` 与 `not_comparable` 的边界（两端笔数是否一致）"的实现细节，**不影响本节结论**（恒等式只是参考值、绝不据此丢股）；**以哪一版为发布口径需主控裁决**（§8）。

## 6. 三条加固纪律

### ① 删除前必须查 `is_listed`：退市股源覆盖不到，不能当"源说没有"

`deleted_empty` / `deleted_fake`（报告期未命中那一支）都建立在"源是完整的"这个假设上，而**退市股在巨潮/东财都查不到历史**。因此只有 `stock_meta.is_listed = TRUE` 的股票才允许走这两个删除分支；非在市/未知一律进 `kept_coverage_gap`；`stock_meta` 读不到时按"全部未知"处理（**fail-closed，宁可不删**）。

- 本批实测：**5,001/8,467 行**属已退市股票；`deleted_empty` 因此为 **0**（本该被删的退市股全被拦下）。
- 反例证据：600087（`*ST 长油`）隔离区 12 行真实派息，巨潮 0 行、东财抛异常——按旧逻辑会被整批删掉。
- 依据：`scripts/apply_dividend_history.py` 头部「删除必须"有正面证据"（2026-11-15 实测教训）」。

### ② 现金金额未知时绝不写 0

巨潮对 B 股常把「派息比例」留空。实测 **200541 2007-06-08**：源 `派息比例 = NaN`，分红说明却写着「10转增3股派5元」，库里 `dividend_per_share = 0.5` 是**真的**。所以：

- 金额口径未知 → **只允许回填其它空字段**，金额字段一律不写；
- 插入时按 **NULL** 写（本轮 **18 行**），**绝不写 0**（0 会被下游当成"真的不分红"）。
- 依据：同上脚本头部；回归 `tests/regression/test_dividend_history_structured.py`（含 200541 实况用例）。

### ③ 新增"回填 NULL"通道（只填空值，不覆盖已有值）

金额已一致、但 `announcement_date` / `stock_dividend` / `transfer_share` 在库里是 NULL、而源里有正面事实时，**只把 NULL 改成正数**。实测例：600519 2006-05-19 巨潮两行（10 派 3 元 + 10 转增 10 股），库里派息 0.3 正确但 `transfer_share` 为空 → 回填 1.0。

- 本轮回填 **28 行**；`--no-fill-nulls` 可关。
- 与 ② 的关系：回填通道**不碰金额**，金额通道**不碰其它字段**，两条互不越界。

## 7. 验证与数字

### 7.1 前后对比（正式库）

| 项 | 本轮前 | **本轮后** | 证据 |
|---|---|---|---|
| `dividends` 行数 | 57,979 | **58,600**（+621） | apply 日志 21:27:18 + 只读复核 |
| `dividends` 有分红记录股票 | — | **5,543 只** | 只读复核 `COUNT(DISTINCT stock_code)` |
| `dividends_quarantine` 行数 | 8,467 | **4,087** | 同上 |
| 隔离区**可解释性** | 8,467 行共用同一个 `unverified_period_end_placeholder`（其中大量行的日期本来就是对的），无法区分处置 | **全部可解释**：`source_coverage_gap` 4,085 + `unverified_period_end_placeholder` 2 | 只读复核 `GROUP BY quarantine_reason` |
| 冷库清单 | 16 张（第 17 张 `pending`） | **17 张全部 `switched`** | `config/cold_archive.yaml` |
| 冷库包 | 16 张 / 4,552,721 行 / 405.7 MB | **17 张 / 4,619,914 行 / 411.4 MB** | `manifest.json` + `--verify-cold` |
| 库内冷库对象 | 16 视图 + 1 张物理表 | **17 视图 + 0 停车表** | `information_schema.tables` |
| `ttm_dividend_yield` 非空 | — | **3,737 行** | 只读复核 |
| `indicator_null_reason` | 97,384 行 / 65 字段（`reports/122`） | **96,395 行** | 只读复核（下降属正常，见下） |
| `vd data doctor --fast` | P1 31 / P2 51 / P3 0（3,891ms，`reports/122` §3） | **P1 2 / P2 40 / P3 0，3,155ms** | 日志 21:40:20，`run_id=doctor-20261115T134020` |

> `indicator_null_reason` 从 97,384 降到 **96,395** 是**正常下降**：分红相关字段被本轮补齐后，对应"当前仍缺且原因已知"的单元格随之消失；它不是历史账本，而是"当前诊断"（D37）。

### 7.2 体检（`doctor --fast`）明细

- **P1 2 项**：
  - `CHAIN_PROGRESS_STALLED` / `balance_sheet_ext.backlog` **metric=105, threshold=105**（上一轮 105，差值 **+0**）——链路在跑但待补数不降，处置建议：`python scripts/fetch_balance_sheet_ext.py`，先核对续传判据（`reports/120` §3 事实 1）。
  - `FIELD_COVERAGE_LOW` / `indicator_snapshot.roic` **metric=84.48, threshold=90.0**。
- **P2 40 / P3 0**；命中规则清单：`FIELD_COVERAGE_LOW(32)` / `STATIC_TABLE_STALE(1)` / `NULL_REASON_MISSING(53)` / `ZERO_REFERENCE_ASSET(11)` / `CHAIN_PROGRESS_STALLED(1)`。
- **两条 skipped 是如实披露、不是通过**：`PACKAGE_STALE`（manifest 未登记 `as_of.price_latest`）、`CROSSCHECK_BELOW_THRESHOLD`（`tier=cheap`，expensive 档未启用）。
- `SCHEMA_DDL_DRIFT` 已从 P1 中消失（冷库 17 张切换完成）。

### 7.3 环境与依赖

- **PyPI 本轮已可访问**：`uv lock` 成功，**80 个**依赖解析通过。
- **本轮没有新增任何依赖**：PDF 方案作废 → **不需要 `pypdf`**（`reports/121` 遗留的"`uv add pypdf && uv lock`"待办随之关闭）。
- 运行期 Python 3.12 的 GIL 崩溃纪律继续适用（D47）：抓取脚本**不 import duckdb**，写库是独立脚本的单短事务。

### 7.4 门禁与迁移

- 结构化源回归：`tests/regression/test_dividend_history_structured.py`（新增，覆盖单位 ÷10、同日两笔合并、金额未知不写 0、退市股 fail-closed 保留）。
- 冷库回归：`tests/regression/test_cold_archive_split.py`、`tests/regression/test_cold_archive_switch.py`（`physical_switch` 与 `schema.py` DDL 双向钉住：`pending` 必须有 DDL、`switched` 必须没有）。
- `scripts/` 目录仍有 **70 条历史 ruff 告警**，属存量、**与文档无关，不是本轮新问题**（本报告不改代码）。

## 8. 仍未完成

1. **隔离区 4,087 行是"永久保留 + 已标原因"，不是待办队列**：`source_coverage_gap` 4,085（源返回空 3,697 + 报告期口径不符 388）+ `unverified_period_end_placeholder` 2。要再降只有两条路：换源（CSMAR 派息历史）或人工逐条裁决；**不能再期望"重抓一遍就好"**（§3.4）。
2. **B 股派息口径未核**：900922 2026-06-30 源内 `cash_per_10=0.02639`，而说明文本写「10派0.18元」，疑似外币折算口径差异；本轮**以结构化数值为准**、未做汇率折算核验，B 股现金分红口径需单独确认。
3. **AH 恒等式四态分布需裁决**：`ok/not_comparable/mismatch` 存在 141/54/1 与 164/32/0 两版（§5.3 脚注）。**结论不受影响**，但对外发布的口径要统一到一版。
4. **`h_share_count` 仍是推导值**：用 `total_shares − circ_shares` 近似，中国银行偏高 33%（含优先股）、建设银行偏高 4.8%，只能标 `confidence='derived'`；`h_share_market_cap_cny` 的 7 只缺口也源于此。
5. **冷库 `DROP` 不缩小 DuckDB 文件**（28GB 高水位），磁盘收益要等下一次离线重建（D44）；**视图冻结绝对路径**，搬冷库目录或离线重建后必须重跑 `--create-views`。
6. **`config/asset_reference.yaml` 文件头仍未同步**：其中一段写着"10 张已物理切换、`csmar_risk_factors` `physical_switch: pending`"，与"17 张全部 switched"不一致（11 条 `cold_archive_state` 本身是对的）。**本轮按硬约束不改 config**，留待下一次 config 修订统一。
7. **`doctor` 两个 P1 未关闭**：`CHAIN_PROGRESS_STALLED`（`balance_sheet_ext` 待补 105 不降）与 `FIELD_COVERAGE_LOW`（`roic` 84.48%）。两者都是如实披露项，不改变本轮结论。
8. **`reports/122` 遗留的三项待裁决（冷库物理切换、A+H 口径、AH 股息率恒等式）中，前两项已由本报告关闭**，剩 **AH 股息率恒等式**随 §8.3 继续跟进。
