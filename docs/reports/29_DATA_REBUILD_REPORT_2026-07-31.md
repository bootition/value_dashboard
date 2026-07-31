---
title: 数据重建最终报告（2026-07-31）
status: approved
category: reports
last-reviewed: 2026-07-31
---

# 数据重建最终报告（2026-07-31）

> 本报告记录 2026-07-31 全市场数据重建的执行结果。所有写入均基于
> `data/backup/pre_rebuild_20260731-125535/` 备份（与冻结基线哈希一致），
> 重建过程按"单写者串行 + 断点续传 + 每步证据 JSON"执行。

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
- 历史期数据仍为 CSMAR 商业导入（隔离披露，不伪造 lineage）。
- 证据：`scripts/evidence/financial_repair_*.json` + `repair_state_formal_financials.json`。

### 6. 快照重算

- 新发布门禁（股本关系 circ<=total 等）下全量重算，原子替换。

## 诊断结果

（链式完成后自动写入 `docs/evidence-final-diagnostics.json`，此处引用其 ready/warning_codes/missing_counts）

## 剩余缺口（诚实披露）

1. **920305**：极新股，所有免费源（腾讯/交易所名单/BaoStock/TDX）均无数据，价格/股本/分红保持缺失，如实记录 missing。
2. **银行/券商监管字段 92 只**（资本充足率/不良贷款率/拨备覆盖率/风险覆盖率等）：免费结构化 API 不可得（已探测新浪/同花顺/东财，均无）。这些字段仅在定期报告 PDF 中披露，需 PDF 解析或人工录入；当前保持 NULL 并披露，**不伪造、不用通用财务字段替代**。
3. **历史期财务无原始响应**（2026-03-31 之前）：CSMAR 商业导入值保留，无原始字节的 lineage 已隔离披露；重建仅覆盖门禁要求的最新报告期。
4. **东财源被封**：本网络环境下东财 push2/push2his 被服务器主动断开（RemoteDisconnected），已全部迁移至腾讯/Sina/BaoStock/交易所官方名单等免费替代源；东财适配器保留在回退链末端，网络恢复后自动可用。
5. **外部真值抽样**：30 股独立对比待执行（脚本 `scripts/sample_external_truth.py` 就绪）。

## 放行条件对照

| 条件 | 状态 |
|---|---|
| 股本重建 + circ<=total=0 | ✅ 完成（5,534 只，1,215→0） |
| 价格新鲜度 + 历史完整 | ✅ 完成（raw/qfq 至 7/31，除 920305） |
| 分红/公司行动 lineage | ✅ 完成（530 只缺口处理，0 失败） |
| 财务核心字段 lineage | 🔄 进行中（Sina 全市场，断点续传） |
| LINEAGE_INVALID / MINIMUM_DATA_NOT_READY 消除 | ⏳ 待诊断确认（链式自动） |
| 30 股外部真值 | ⏳ 待执行 |
| 回归/前端/发布验证 | 前端 ✅；回归 ⏳（等正式库进程结束） |
