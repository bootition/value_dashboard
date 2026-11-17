---
status: approved
category: reports
created: 2026-11-17
last-reviewed: 2026-11-17
related: reports/131
---

# reports/131 §9.2 的 8 个 P0 修复 + 血缘告警复发处置（2026-11-17）

用户指令：读 `reports/131` §9，把里面提到的 P0 全部修完，派 agent 修、另派独立 agent 审查。

## 1. 结论（TL;DR）

| 编号 | 问题 | 修复前 | 修复后 | 状态 |
|---|---|---|---|---|
| P0-1 | 科创板成交量放大 100 倍 | 科创板换手中位 **2.0629**（194%） | **0.0211**（2.1%，与其他板块 0.0167 同量级） | ✅ |
| P0-2 | 快照重发清空 `roce_*` | — | 另一个会话已修（`_publish_snapshot` 内追加 `SNAPSHOT_UPDATE_SQL`） | ✅ 前一会话 |
| P0-3 | FY2025 明细两表半空壳 | `cash_flow_indirect` 19/29 列全空、`cash_flow_activity` 20/21 列全空 | 各 **1 列**全空（且都是已登记的结构性缺失） | ✅ |
| P0-4 | 营收/净利同比两套口径 | 详情页与快照 27.8% 方向相反 | `*_yoy` 统一 TTM（与详情页同源）、`*_yoy_fy` 另存年报口径；TTM 覆盖 5,535 / FY 覆盖 5,529 | ✅ |
| P0-5 | 技术三指标 47% 覆盖 | `period_return` 2,644/5,575（47.4%） | **5,573/5,573（100%）**，波动率 5,555 | ✅ |
| P0-6 | 保险股保险合同负债错映射 | 5 只险企占全市场合同负债 **53.6%** | **0.075%** | ✅ |
| P0-7 | 减值损失符号 FY2018 翻转 | 2018 年原始合计 490.3 亿（假低值）、2019 年 −2,828.9 亿（符号翻转） | 派生口径 2018=**9,982.5 亿**、2019=**6,726.8 亿**（三段连续）；原始数据未改 | ✅ |
| P0-8 | FY2020 预收→合同负债重分类 | 单列 2020 年 −96.6% 断崖 | 合并口径 `advance_from_customers` 2020 年 +10.8%，无台阶 | ✅ |
| P0-9 | 689009 市值/每股族错 10 倍 | `market_cap_at_report` 比值 **0.1** | 比值 **1.0**（2024Q4/2025Q4/2026Q3 逐期核对） | ✅ |

**数据体检：P1 从 2 条降到 1 条**（只剩 `roic` 覆盖率 84.5%，其中 815 家无有息负债，结构性不可算）。

## 2. 顺带修掉的两个「静默假红」

这两个不是 §9 的 P0，是在执行修复时被 `/api/ready` 挡下来查出来的，都属于**代码层面的误判**，一并修了。

### 2.1 血缘告警换了个形式复发（reports/124 的后遗症）

`reports/124` 修 P0（补采批次摘要格式）时，给 13 个历史批次各写了一条**描述型**归档记录
（payload 是本轮请求描述，不是原始响应）。这解决了 `batch_archive_gap_rows`，
但违反了内容寻址不变式 → 现在触发 `hash_mismatch_rows = 13` → **同一个 `LINEAGE_INVALID` 复发**，
整站 34 个指标又被打成 null。

**真相**：那 13 个批次（2026-11-14 12:42 ~ 11-15 10:21）的原始响应**从来没有被保留**
（抓取发生在归档改造之前）。描述型 payload 不是原始响应，留在归档表里等于**伪装成有原始字节**。

**处置**：
1. 删除 13 条描述型归档记录（`scripts/repair_lineage_backfill_rows.py`，备份到 `.planning/`）；
2. `fetch_batch` 记录**保留**（它们是真实发生过的抓取事实）；
3. `data_quality.py` 的 legacy 判据放宽：`摘要形态 = endpoint名:十六进制串` 与 32 位 MD5 同类，
   归入 `legacy_archive_gap_batches`（**已披露的历史缺口，不算今天的数据损坏**）。
   实测：`legacy_archive_gap_batches` 35 → **48**，`batch_archive_gap_rows` 13 → **0**，
   `hash_mismatch_rows` 13 → **0**。

### 2.2 新股上市日期为空 → `/api/ready` 永久 503

`920309 N菊乐` 的 `stock_meta.listing_date` 是 NULL（新股尚未被元数据管线回填），
而 readiness 的"上市 90 天豁免"判据要求 `listing_date IS NOT NULL` →
豁免失效 → 一只新股把整站打成"未就绪"。

**处置**（`data_quality.py` 三处）：
1. SQL 里 `listing_date IS NULL` 视为近期上市（无法证明是老股，按新股豁免深度检查）；
2. `recent_codes` 不再要求 `listing_date` 非空；
3. `pool_metadata` / `lineage_coverage` 加入新股豁免清单（新股必然没有行业归属与除权历史）。

实测：`ready: False → True`，`warning_codes: [MINIMUM_DATA_NOT_READY] → [FIELD_COVERAGE_LOW]`。

## 3. 交付物

### 代码
| 文件 | 内容 |
|---|---|
| `app/core/adapters/tencent_adapter.py` | 科创板不乘 100 + 换手率量级自检兜底（>500% 按未乘处理并 warning） |
| `app/core/supplement.py` | 东财 `xjllbAjaxNew`/`zcfzbAjaxNew`/`lrbAjaxNew` 全字段映射（间接法 28/29、活动表 20/21）；保险股「保险合同负债」两层守卫 |
| `app/core/indicators/calculator.py` | 同比双口径（`_calc_ttm_yoy`）；技术 `technical_pending` 消费 |
| `app/core/indicators/extended.py` | 股本链进输入指纹（400 天窗口摘要）+ `force_codes` 按变更股重建 |
| `app/core/dsl/derived_fields.py`（新） | 跨年断点派生字段唯一真源（减值符号、预收+合同负债合并） |
| `app/core/dsl/{ast_nodes,codegen,engine,validator}.py` | 派生字段登记 / 逐行归一化 / 裸名默认 / 断点 warning |
| `app/core/screening/engine.py` | 派生列纳入白名单、断点 warning 冒泡 |
| `app/core/quality/asset_health.py` | 第 14 条规则 `TECHNICAL_COVERAGE_GAP` |
| `app/core/update.py` | `technical_pending` 写入/消费；`force_codes` 接线 |
| `app/core/storage/schema.py` | v43（`*_yoy_fy` 三列）、v44（`insurance_contract_liability`）、列注释写明断点 |
| `app/core/data_quality.py` | legacy 判据放宽；新股豁免三处修正 |
| `app/web/api/screening.py` | 技术字段覆盖率门禁（<95% 进 `unavailable`） |

### 脚本（默认 dry-run，`--yes` 才写，写前备份 + 写锁）
`repair_star_volume_x100.py` / `fetch_cashflow_supplement.py --stale-null` /
`repair_insurance_contract_liability.py` / `backfill_indicator_ext.py` /
`backfill_yoy_calibers.py` / `backfill_technical_indicators.py` / `repair_lineage_backfill_rows.py`

## 4. 正式库已执行的动作

| 动作 | 结果 |
|---|---|
| `repair_star_volume_x100.py --yes` | `price_daily_raw` 43,481 行 / 616 只；`price_daily_qfq` 43,411 行 / 615 只；614 只 `avg_volume` 同步修正 |
| `repair_insurance_contract_liability.py --yes` | 74 行置 NULL（年报 19 行），误伤 0 |
| `fetch_cashflow_supplement.py --yes --stale-null` | **5,570 只 / 55,667 行**（间接法 27,842 + 活动表 27,825） |
| `backfill_indicator_ext.py --yes` | 361,393 行重建（含 689009 修复） |
| `backfill_yoy_calibers.py --yes` | 同比六列回填 |
| `backfill_technical_indicators.py --yes` | 技术三列从 47.4% → 100% |
| `repair_lineage_backfill_rows.py --yes` | 删除 13 条描述型归档，备份留存 |
| `vd init` | schema 40 → **44**（v41 AH 表 / v42 ROCE / v43 同比 / v44 保险列） |
| 指标重算 | 5,575/5,575 成功，failed 0 |

## 5. 验证

- `doctor --fast`：**P1 1 / P2 46 / P3 0**（4.5s）；
- `/api/ready`：`ready: True`，`warning_codes: [FIELD_COVERAGE_LOW]`；
- 8 个 P0 全部有可复现 SQL 的前后对比（见 §1 表）；
- 独立审查：4 个审计 agent 各自重新取证（结果见 `reports/135` 附录与各自报告）。

## 6. 仍未完成

1. **前端三块界面未接**（用户明确说先不急）：H 股市值卡片、筛选 409 提示、指标列表新字段；
2. `NULL_REASON_MISSING` 46 条 P2（披露级）：扩展域/资产负债域有 NULL 但原因未登记；
3. `roic` 84.5% 覆盖率（815 家无有息负债，结构性）；
4. `price_daily_qfq` 在 2026-07-31 **之前**仍有约 14% 科创板股票的量被腾讯 qfq 污染（在本次 `--since` 范围外，可用 `--since <更早日期>` 复跑同一判据）。
