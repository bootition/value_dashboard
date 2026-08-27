---
title: 正式库普通用户路径自动恢复完成报告（2026-08-08）
status: approved
category: reports
created: 2026-08-08
last-reviewed: 2026-08-08
supersedes: reports/60_THROUGHPUT_RED_TEAM_AND_FORMAL_PRACTICE_2026-08-08.md
---

# 正式库普通用户路径自动恢复完成报告

## 1. 目标与方法

以普通用户使用方式（`start.bat` 启动服务 + 自动更新 enabled）持续运行自动恢复，期间发现的问题立即暂停、修复、全门禁验证后续跑，最终完成条件：正式数据库 PASS、无待修复项目、无 retry 遗留、无待补项目、数据库标记可研究。

全程不手动改库；修复通过代码提交并由正式库后续轮次自然验证。

## 2. 恢复过程与修复（7 轮代码修复）

| 轮 | 发现 | 修复 | 提交 |
|---|---|---|---|
| 1 | 服务端速率 23-35 股/分，远低于 CLI 123+；请求线程空闲、主线程卡 `_persist_price_batch` | DuckDB `ON CONFLICT DO UPDATE` 对 1730 万行表线性退化（基准：2000 行冲突 upsert 4.5s/百万行）。大响应（>200 行）改事务内 DELETE+INSERT，带行数截断防护（远程 < 本地 50% 拒绝并保留旧值） | `1372b51` |
| 2 | 自适应限速计时包含 `_wait_rate_limit` 排队，误判长尾永久抬档 | 计时移到适配器真实请求处（腾讯/TDX/BaoStock）；窗口 ≥2 次 >30s 才升档；快响应不重复升档 | `5a842bc` |
| 3 | 北交所 raw 6 行 vs 本地 700+ 被截断防护误拒（腾讯北交所能力限制，非截断） | raw/qfq 独立判定 full_replace：北交所 raw 走 upsert，qfq 走替换 | `c050a3a` |
| 4 | retry 重抓不带增量起点（每股 3000+ 行全历史，100 条重试 80 分钟） | 重试带 `start_date=本地最新` 增量；冗余清理：价格已达标且 lineage 完整的条目直接删除 | `c050a3a` |
| 5 | retry_list 10302 条中 9893 条是"无可用适配器"历史垃圾（listing_info/stock_list 无逐股重试路径的死循环） | `_cleanup_unretryable_tasks` 清理无维护路径类型；保留 announcements pending 语义（PRD §7.4） | `3cab20d` |
| 6 | 391 只快照日期落后（价格已最新但指标未重算——无价格缺口则不被选中，形成盲区） | indicators 步骤额外纳入"快照日期落后"的股票重算 | `2213faf` |
| 7 | missing_list 2892 条 listing_info 是源失败产生的假缺口（本地字段 100% 完整） | 源失败只记录本地确实缺失的股票；每轮把字段完整的 missing 记录标记 resolved；统计口径仅计未解决项 | `9d1314c`、`294572a` |

## 3. 最终正式库状态（2026-08-08，证据 `evidence-formal-auto-recovery-pass-20260808.json`）

- `minimum_data_readiness.ready = true`
- `warning_codes = []`
- `verdict = PASS`
- `running_jobs = 0`
- `retry_list = 1`（唯一 1 条为 announcements pending 标记，PRD §7.4 要求保留）
- `missing_list` 未解决 = 0（2892 条 listing_info 历史假缺口已 resolve）
- `lineage_coverage = 0`；`snapshot_price_coherence = 6`（仅新股/极新股披露项）
- 披露项（合法市场状态，不阻断）：新股财务/股本 6/4、无分红/无除权 102、银行/券商监管字段 90（保持 NULL 不伪造）、待完整财报期 1

## 4. 性能实测（正式任务）

- 单轮自动更新：价格缺口 3937 只约 40 分钟（约 100 股/分）；无缺口轮次 3-5 分钟完成。
- 快照修复：全量指标重算 5533 只约 9 分钟。
- retry 清理：冗余清理 + 增量重试后，单轮数分钟内完成。

## 5. 已知待办（不在本次完成范围内）

- CNINFO 分红适配器 `ex_date` 永不填充的死代码问题（`reports/61` §3.2）：主源恒空、靠回退链填充，当前无数据缺口；修复评估（PDF 解析 / ex_date 降级 / 明确依赖回退链）已列入待办。
- 东财行情 host 封禁期间，`listing_info` 主源 akshare 不可用（universe partial），数据由本地完整存量支撑；源恢复后自动刷新。
