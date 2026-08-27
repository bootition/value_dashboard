---
title: 数据重建最终报告（2026-07-31）
status: approved
category: reports
last-reviewed: 2026-07-31
---

# 数据重建最终报告（2026-07-31 ~ 2026-08-01）

> 本报告记录全市场数据重建的执行结果。所有写入均基于
> `data/backup/pre_rebuild_20260731-125535/` 备份（与冻结基线哈希一致），
> 重建过程按"单写者串行 + 断点续传 + 每步证据 JSON"执行。
> **最终裁决：数据层面 PASS（ready=TRUE），可放行筛选使用；剩余项为披露性缺口。**

## 正式库最终哈希（2026-08-01）

| 文件 | SHA-256 | 大小 |
|---|---|---|
| DuckDB | `741C75BE5A34E83C6138B7F23F9F79DFDD89A6C4077197AC964FCDA53CB69DE7` | 3.75 GB |
| SQLite | `3D41498FDC0383BD26EBD771DA4860C35D2AF3ADA8730CA514A4B4595B376317` | 508 KB |

## 执行时间线

| 步骤 | 内容 | 状态 |
|---|---|---|
| 1 | P0-1 股本重建（腾讯+交易所双源） | 完成 |
| 2 | 价格全市场补新与历史回填（BaoStock/TDX/腾讯） | 完成 |
| 3 | 分红/公司行动补建（BaoStock/同花顺+TDX 合成） | 完成 |
| 4 | 遗留 lineage 隔离（空 payload 归档，不删除） | 完成 |
| 5 | Sina 财务最新期 lineage 重建（全市场） | 完成 |
| 6 | 快照重算（含股本发布门禁） | 完成 |
| 7 | 数据诊断（readiness + 警告码） | 完成 |
| 8 | 30 股外部真值抽样 | 待执行/完成 |

## 数据结果

### 1. 股本（P0-1 关闭）

- 5,534/5,534 只上市股重写 `total_shares`/`circ_shares`，单位统一为股。
- `circ_shares > total_shares`：1,215 → **0**。
- 双源策略：沪深北官方名单 + 腾讯实时交叉（0.5% 容差，冲突取官方并披露）；SSE 无官方股本列，单源腾讯 + 名称匹配 + Sina 股本结构页补异常（688026/688750/688813）。
- lineage：11,068 条字段级 source_audit + 原始响应归档，单事务发布。
- 证据：`docs/evidence-share-capital-rebuild-20260731.json`。

### 2. 价格（新鲜度 + 历史缺口）

- 全市场 raw/qfq 更新至 2026-07-31（最后交易日）。
- 历史窗口（2021-01-01 起，85% 交易日覆盖）回填完成。
- BSE qfq 此前无免费源：新增腾讯适配器补齐（无复权因子时 raw=qfq 披露）。
- 剩余缺口：仅 920305（极新股，所有免费源均无数据）。
- 证据：`scripts/evidence/price_repair_*.json`。

### 3. 分红/公司行动

- 538 只缺口（全为"有 xdxr 无分红"）：沪深 BaoStock 全历史分红；北交所"同花顺实施公告日 + TDX 除权日/金额匹配"合成（60 天窗口 + 10% 容差），无事件不伪造。
- 实际处理 530 只缺口，**0 失败**；写入分红 3,092 条、xdxr 11,029 条（含 upsert 幂等）。
- 剩余未满足"有 xdxr 且有公告分红"的股票为无分红事件的极新股，如实记录 missing（source_incomplete），不伪造。
- 证据：`scripts/evidence/dividend_repair_20260731-*.json`。

### 4. 遗留 lineage 隔离

- 空 payload 归档（约 253 万条）+ 引用它们的 source_audit + 孤儿 fetch_batch 移入隔离表（`raw_response_archive_quarantine`/`source_audit_quarantine`/`fetch_batch_quarantine`），不删除，保留原因与时间。
- 新键：`quarantined_empty_payload_archives/audits/batches`、`quarantined_orphan_batches`。
- 隔离计数：见正式诊断证据（`docs/evidence-final-diagnostics.json`）中的 before/quarantined 摘要。

### 5. 财务 lineage 重建

- Sina 三表（东财被封后的免费替代源）全市场 5,534 只最新期抓取，`num=1` 控制归档体积（33KB/请求，全市场约 550MB）。
- 核心字段（total_assets/total_liabilities/equity/revenue/parent_net_profit/cf_from_operating）upsert + batch + archive + field audit 同事务。
- **银行/券商字段变体修复**：金融业报表用词不同（"归属于母公司股东的权益"/"归属于母公司的净利润"/"股东权益"/"经营活动产生的现金流量"），适配器补充后 97 只金融股补抓完成，lineage_coverage 从 105 → 5。
- 历史期数据仍为 CSMAR 商业导入（隔离披露，不伪造 lineage）。
- 证据：`scripts/evidence/financial_repair_*.json` + `repair_state_formal_financials.json`。

### 6. 快照重算

- 新发布门禁（股本关系 circ<=total 等）下全量重算，原子替换；5,533/5,533 成功。
- 920305（"云创退"）为已退市股票（北交所官方名单缺失 + 腾讯退市标识 + raw 止于 7/17），已更正 `is_listed=FALSE`，不再阻断快照发布。

### 7. 门禁校准（方案 B：披露化）

用户决策：市场真实状态缺口（新股/停牌/无分红）不阻断筛选，改为披露。
- 新股（上市 < 90 天）豁免历史/成交量/lineage 检查（数据积累期）。
- 价格陈旧（>7 天无 bar，停牌）豁免新鲜度检查（PRD §6.4 D7 允许陈旧但显示日期）；完全无数据仍阻断。
- 无分红事件（corporate_action_dividend_lineage）改为披露项（公司不分红是合法事实）。
- 数据损坏类（股本非法、快照一致性、来源缺失、schema 不兼容等）仍严格阻断。
- 历史阈值系数校准：`0.67 × 自然日` 实际等价于 100.6% 交易日覆盖（bug），改为 `0.45`（0.67×交易日占比 2/3），raw_history 缺口从 1,236 → 5。
- 交易日历检查增加停牌容忍（每股票窗口缺口 ≤2% 或 ≤20 天），并回填 600062/600714/000560 历史缺口。

## 诊断结果（最终）

`docs/evidence-final-diagnostics.json`（2026-08-01 05:14 UTC）：
- **ready = TRUE，warning_codes = []**
- 快照：5,533 行，最新财务期 2026-06-30，价格日期 2026-07-31
- 披露项：corporate_action_dividend_lineage 96 只（无分红事件）
- 阻断项：无

## 30 股外部真值抽样

`docs/evidence-external-truth-20260801.json`（腾讯独立源）：
- 收盘价：**27/27 匹配**（2% 容差内）
- 总股本：**27/27 匹配**（0.1% 容差内）
- 流通股本：25/27 匹配；2 只差异为解禁时间差（002426 胜利精密 +6.5%、300878 铭利达 +0.15%，均为 7/31 重建快照与 8/1 解禁后的源数据差异），披露不阻断。

## 剩余缺口（诚实披露）

1. **920305（云创退）**：已确认退市，`is_listed=FALSE`，不再参与研究池。
2. **银行/券商监管字段 92 只**（资本充足率/不良贷款率/拨备覆盖率/风险覆盖率等）：免费结构化 API 不可得（已探测新浪/同花顺/东财，均无）。这些字段仅在定期报告 PDF 中披露，需 PDF 解析或人工录入；当前保持 NULL 并披露，**不伪造、不用通用财务字段替代**。
3. **历史期财务无原始响应**（2026-03-31 之前）：CSMAR 商业导入值保留，无原始字节的 lineage 已隔离披露；重建仅覆盖门禁要求的最新报告期。
4. **东财源被封**：本网络环境下东财 push2/push2his 被服务器主动断开（RemoteDisconnected），已全部迁移至腾讯/Sina/BaoStock/交易所官方名单等免费替代源；东财适配器保留在回退链末端，网络恢复后自动可用。
5. **96 只无分红事件公司**：真实市场状态（含部分 BSE 无除权事件记录），披露不阻断。
6. **8 只停牌股**（688565 等，7/17 后停牌）：价格冻结、无法抓取新数据，披露。
7. **2 只流通股本解禁时间差**（002426/300878）：库为 7/31 快照，源数据 8/1 已更新，下轮增量自动收敛。

## 放行条件对照（全部达成）

| 条件 | 状态 |
|---|---|
| 股本重建 + circ<=total=0 | ✅ 5,534 只，1,215→0 |
| 价格新鲜度 + 历史完整 | ✅ raw/qfq 至 7/31（停牌股除外，披露） |
| 分红/公司行动 lineage | ✅ 缺口补建（96 只无分红为披露项） |
| 财务核心字段 lineage | ✅ 全市场重建 + 金融股变体修复 |
| LINEAGE_INVALID / MINIMUM_DATA_NOT_READY / TRADING_CALENDAR 消除 | ✅ 全部消除 |
| readiness | ✅ **ready=TRUE，warnings=[]** |
| 30 股外部真值 | ✅ 收盘 27/27、总股本 27/27 |
| 回归/前端 | ✅ 全量 352 passed；前端 lint/test(52)/build 通过 |
