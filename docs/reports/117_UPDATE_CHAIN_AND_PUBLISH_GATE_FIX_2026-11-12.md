---
title: 2026-11-12 自动更新日志复盘与修复闭环（发布门禁/价格吞吐/适配器分类/可观测性）
status: approved
category: reports
created: 2026-11-12
last-reviewed: 2026-11-12
---

# 2026-11-12 自动更新日志复盘与修复闭环

## 1. 结论（TL;DR）

用户当日启动应用一次、触发两轮自动更新（110 min / 153 min），**两轮都以 `partial` 结束，指标快照自 2026-10-30 起再未发布**，前端指标被质量门禁整体遮蔽为 `null`、筛选 409。

根因不是单点，而是四条链同时出问题：

1. **发布门禁是"一票否决"**：26 只股本缺失的新股中 5 只于 11-03~11-10 陆续越过 90 天豁免线 → `snapshot_publish_gate` 恒 `ready=False` → 全市场 5,580 只快照发布被拒。且股本缺口**不在任何自动通道里**（不进 missing/retry、不被每日补抓选中）。
2. **状态上报假绿**：`indicators` 步骤用 `{**后一个子步骤, "full_recompute": 前一个}` 覆盖 status，`refresh_treasury_spreads`（不过门禁）的 success 抹掉了 `rejected`；该路径还把全表 `calculated_at` 刷成当天，掩盖 14 天真实陈旧。
3. **价格步骤 3.9× 回归**：除权窗口取**全市场 `min()`**，一只 qfq 陈旧股把全体拖进全历史重拉；xdxr 刷新又排在价格步骤之后，形成"第 N 轮喂第 N+1 轮"的级联（32 → 293 只）；叠加主线程串行 `DELETE+INSERT`（占增量耗时 93%）。
4. **熔断放大**：熔断桶按 adapter 名（无 data_type）、任何"有错+空数据"都计数，于是 5 个**个股级**数据问题让 3 个源同时熔断 300s，导致 601123/603435 在**零尝试**状态下被记失败（`无可用适配器 (tried: [])`），retry_count 被推到 4/5，再失败一次就会以 `retry_exhausted` 的假名义永久进 missing_list。

另有 3 个 P1（公告截断丢数据、readiness 不可观测、`missing_count` 三处口径不一）与 2 个 P2（`None` 错误日志、DuckDB 配置跨线程不生效）。

**修复后**：全量 S1 **1044 passed**（正式库指纹 `delta_detected=false`）、Ruff 全绿；正式库股本缺口 26 → **0**、`invalid_hash_rows` 676,272 → **0**（改用"按摘要算法判定"并如实披露 `legacy_hash_rows`）、`LINEAGE_INVALID` 消除；指标快照重新发布，`ready=true`。

## 2. 当日时间线（本地 UTC+8）

| 时刻 | 事件 |
|---|---|
| 12:29:51 | 启动 `python -m app.web.main`（PID 37260） |
| 12:30:02 | `就绪核对完成: ready=False`（日志**无任何原因**） |
| 12:30:55–14:21:26 | 第 1 轮（job 199）：价格 60.8 min / 91.65 只每分 / `xdxr_full_refetch=32`；财报 20.7 min；公告截断 `150/3719`；14:12:58 三源同时熔断 |
| 14:14:10 | `指标快照发布被质量门禁拒绝: share_capital_integrity, ['001232','301707','301717','603468','688828']` |
| 16:15:57–18:49:20 | 第 2 轮（job 200）：价格 **143.5 min** / **38.83 只每分** / `xdxr_full_refetch=293`；`indicators` 对外 `success` 实为 `rejected` |
| 18:49:18 | `增量更新完成: partial`；`last_full_success_at` 停在 2026-10-30 |

## 3. 暴露的问题与修复

### P0-1 门禁一票否决全市场（发布被永久拒绝）

- **根因**：`data_quality.py::share_capital_violations()` 是 fail-closed 单条 SQL；唯一豁免是"上市 ≤90 天且两列皆 NULL"，用 `CURRENT_DATE` 滚动。5 只 8 月初上市的新股（001232/603468/301707/301717/688828）在 **11-03~11-10** 越过豁免线即变成硬阻断；三个发布调用点（`calculator.py`）随即整批拒绝。
- **为什么没有自愈**：`update.py::_listing_info_gap_codes()` 只查 `listing_date/is_st/is_suspended`（docstring 却写 "needed by the publish gate"）；`init.py::_fetch_listing_info()` 只对三列缺失登记 missing；`replenish_missing_core_data` 不含股本 → 这 26 只既不在 missing_list 也不在 retry_list，无人管。
- **第二性原因（实测）**：`akshare_adapter._fetch_listing_info` 传入的上交所股本列 `总股本/流通股本` **在 `ak.stock_info_sh_name_code()` 返回里根本不存在**（只有代码/简称/上市日期），所以**沪市股票永远拿不到股本**，只能靠人工跑 `scripts/rebuild_share_capital.py`。
- **修复**：
  - 门禁分级：`snapshot_publish_gate()` 返回 `hard_block / quarantined_codes`；`violation_count > max(50, 1% 上市数)` 才整批拒绝（怀疑系统性单位错乱），否则**逐股隔离**、其余正常发布（`status=partial, reason=publish_gate_quarantined`），被隔离股票**旧行保留**。
  - 单一真相：`listed_share_capital_gap_codes()` 同时供门禁、readiness、每日补抓使用，彻底删掉第二份 SQL（此前"门禁报 5 只、readiness 报 26 只"）。
  - 自动通道：`_listing_info_gap_codes()` 并入股本缺口；`_fetch_listing_info()` 对仍缺失的股票写 `missing_list(share_capital/source_incomplete)`。
  - 源侧补齐：`akshare_adapter._fetch_listing_info` 对交易所清单缺股本的代码，用腾讯实时行情 `qt.gtimg.cn` 字段 72/73 回填（≤50 只/批、名称一致性校验、`circ<=total` 校验、失败保持 NULL 不猜值），并标注 `shares_source`。
  - 数据修复：`scripts/rebuild_share_capital.py --only-codes <26 只>` 双源核验，`accepted=26 skipped=0 mismatches=0`，证据 `docs/evidence/evidence-share-capital-26-20261112.json`。
  - **同类缺陷一并收口**：`compute_snapshot_for_all` 的 `minimum_data_not_ready` 前置判断原本也是"任一非新股缺核心三表/价格 → 整批不发布"，与门禁是同一类"单点拖死全站"。现改为同一阈值政策：缺口 ≤ `max(50, 1%)` 逐股排除、其余发布（`partial + minimum_data_not_ready + missing_input_codes`），超过阈值才整批拒绝；被排除股票与隔离股的**上一代快照行通过 `preserve_codes` 原样保留**，避免"逐股排除"退化成"删掉旧行、详情页彻底消失"。

### P0-2 指标步骤状态假绿

- **根因**：`update.py` 三处 `snapshot_step = {**后一个子步骤, "full_recompute": 前一个}` 让后者的 status 覆盖前者；`refresh_treasury_spreads()` 不过门禁、直接 `UPDATE` 全表，其 success 掩盖了价格域/财务域的 `rejected`，同时把 `calculated_at` 刷成当天 → `snapshot_age_days=0` 掩盖 14 天陈旧。
- **修复**：新增 `job_status.merge_step_status(*steps)`（严重度 `rejected > failed/error > partial > success > skipped`，`rejected` 永不被覆盖）；`update.py::_merge_indicator_steps()` 统一走它并汇总计数；`data_status.py` 增加 `data_age_days`（价格日与快照 `latest_price_date` 之差），不再只靠 `calculated_at` 判断陈旧。

### P0-3 价格步骤 143.5 min（38.83 只每分，基线 150，3.9× 回归）

- **根因（三条叠加）**：
  1. 除权窗口 `min(target_dates)` 取全市场最旧日期 → 一只 qfq 陈旧股把全体拖进全历史重拉；
  2. xdxr 刷新排在价格步骤之后 → 第 1 轮写入 3,590 行新 xdxr 后，第 2 轮窗口变大（32 → 293）；
  3. 全历史响应逐股独立事务写 17M 行主表：144 处 ≥10s 全静默、共 96.5 min（增量的 93%）。
- **修复**：
  - `_local_price_window_start()` + `_get_xdxr_codes_since(stock_windows=...)`：窗口**按股票**计算（单条 JOIN SQL），删除全市场 `min()`；
  - `_PriceReplaceBuffer`（8 只 / 20 万行 / 5s 三重硬上限）+ `_flush_price_replace_buffer()`：整批一个事务 `DELETE ... IN (...)` + 分组 INSERT，失败整批回滚后**同一批 items 逐股回退**，`try/finally` 收尾 flush；
  - `tencent_adapter`：deadline 检查移到限速之后、剩余预算 <0.5s 不发请求、HTTP 超时下限 1s（此前的 `max(0.1, remaining)` 制造了"必然失败"的假超时）。

### P0-4 熔断放大 + 假失败归因

- **根因**：熔断桶 key 只有 adapter 名（无 data_type）、无时间窗、任何"有错且无数据"都 +1；`akshare` 上游在 CNINFO 分红 `records=[]` 时抛 `KeyError: '实施方案公告日期'`（5 只无实施公告的新股），我方当源故障；`cninfo` 分红恒失败却仍在 `config/default.yaml` 的 dividends 链上；`baostock` 把 `9` 开头一律判北交所（900948 实为沪市 B 股），skip 被拼进 `error` 字符串。
- **修复**：`SourceMetadata` 引入 `error_kind`（`legal_empty / per_symbol_data_error / source_unsupported / source_unavailable / deadline / truncated_ok`）+ `reason / skipped_codes / missing_columns / truncated`；**熔断只计 `source_unavailable`**（未迁移适配器的"error 非空+空数据"兜底计入），桶 key 改 `adapter:data_type` 加 10 分钟窗口，入口 deadline 直接返回不计数，`close()` 清空；akshare 空表 KeyError → 合法空、缺列 → `missing_columns`、缺 ex_date → `per_symbol_data_error` 不写假行；cninfo 分红移出默认链并改 `source_unsupported`；baostock 正确分类 B 股/北交所并把 skip 移出 error；`retry_list` 增 `error_kind`（schema v18），`legal_empty/source_unsupported/skipped_codes` **直接写 missing、不占重试次数**，并对历史 `tried: []` 零尝试失败做一次性重查。

### P1 其余

| 问题 | 修复 |
|---|---|
| CNINFO 公告截断 `raise` → 已抓 150 条整批丢弃 + 计熔断 | 截断改为返回数据 + `truncated=True/error_kind=truncated_ok`；general 窗口跨度 >1 天时复用日期二分；递归补 `progress_cb`；`report_step("announcements", ...)` 无条件入册（截断=partial 而非 failed） |
| `ready=False` 不可观测 | 启动日志带 `warning_codes` + `missing_counts`（含每类前 5 个代码）；新增 `GET /api/ready`（ready=False → 503），`/api/health` 保持探活语义 |
| `missing_count` 三处口径不一（11,823 vs 162） | 统一 `actionable_missing_count()`（`resolved_at IS NULL` 且可行动），供 update/CLI/Web 共用 |

### P2 其余

| 问题 | 修复 |
|---|---|
| `所有适配器均失败 (['eastmoney_f10']): None` | `_missing_result()` 写 `reason` + `legal_empty`；manager 日志三分支（合法空 INFO / 真失败 ERROR / partial），**不再出现字面 None**，并带 data_type 与 codes |
| DuckDB `different configuration` | `_MEMORY_LIMIT_OVERRIDE` 由 `ContextVar` 改为**进程级全局 + 锁**（更新子进程内所有线程一致） |
| 遗留隔离 CLI 无法执行（本次实测发现） | `quarantine_legacy_records()` 的 `INSERT ... SELECT x.*` 位置绑定在隔离表列序漂移时把 BOOLEAN 写进 TIMESTAMP → 全部改 `INSERT ... BY NAME` |

### 关于 `LINEAGE_INVALID`（与股本门禁并列的第二层阻断）

- **实测**：676,272 行 `LENGTH(raw_response_hash)!=64`，全部是 **32 位 MD5**，来自 2026-09-19~20 的 CSMAR/东财历史回填批次（`cf_from_operating_indirect` 433,724 + `deducted_net_profit` 242,548）。它们不是坏数据，而是"没有原始字节"的历史事实（STATUS 已披露），却因一刀切长度判定永久 `LINEAGE_INVALID` → 命中 `_SNAPSHOT_BLOCKING_WARNINGS` → 详情页数值全遮蔽、筛选 409。
- **修复（按摘要算法判定，不删除审计行）**：
  - `invalid_hash_rows` = 长度 ≠ 64 **且不是合法 MD5 形态**；
  - `legacy_hash_rows` / `legacy_archive_gap_batches` 如实披露被豁免的批次，**不进** `LINEAGE_INVALID`；
  - 现代 64 位内容寻址批次缺归档 = 真 lineage 断裂 = 仍然阻断。
- **为什么不隔离/删除**：审计行是溯源资产；隔离 67.6 万行会削弱可追溯性，且 `lineage_coverage` 判定与之无交集。

## 4. 关键设计决策（第一性原理）

| 决策 | 理由 |
|---|---|
| 门禁从"全市场 fail-closed"改为"逐股隔离 + 阈值硬阻断" | 门禁的职责是**阻止不可信输入进入已发布快照**，不是"阻断整站"。单只股票缺数据只需隔离该股票；只有"大面积单位错乱"才说明系统性问题，才值得整批拒绝 |
| 阈值 `max(50, 1%)` | 50 只以内的零星缺口是数据源现实；超过 1% 上市股票同时违反股本关系，几乎只可能来自单位/口径系统性错误 |
| 错误分类（`error_kind`）而非"有错/没错" | 熔断的语义是"源不可用"。把"个股数据格式问题""源不支持""合法空"混进熔断，等于让个别数据问题冻结整个源 |
| 熔断桶 `(adapter, data_type)` | 分红失败不应封杀公告；价格超时不应封杀分红 |
| 单一真相函数（股本缺口/missing 口径/状态合并） | 同一事实多处实现必然漂移（本次即是 5 vs 26、11,823 vs 162、success vs rejected） |
| 按摘要算法判定 lineage，而不是按批次白名单 | 复用原白名单（`adapter_version LIKE 'csmar-%'`）会在同一战役的东财批次上失效——判据应描述**数据形态**（MD5 vs SHA256），而不是描述**谁写的** |
| 进程级 DuckDB 覆盖值是安全的 | 更新是独立子进程；`ContextVar` 不跨线程是 Python 语义，不是并发保护手段 |

## 5. 数据与验证

| 项目 | 修复前 | 修复后 | 证据 |
|---|---|---|---|
| 股本缺口（上市股票） | 26 只（其中 5 只阻断发布） | **0** | `docs/evidence/evidence-share-capital-26-20261112.json`（accepted=26, skipped=0, mismatches=0, post_verification circ>total=0） |
| `invalid_hash_rows` | 676,272 | **0** | `vd data status`（`legacy_hash_rows=676272` 如实披露） |
| `batch_archive_gap_rows` | 33 | **0**（`legacy_archive_gap_batches` 披露） | 同上 |
| `missing_count` 口径 | 日志 11,823 / 网页 162 | 统一 **162**（可行动） | `vd data status` + `/api/data-status/summary` |
| 指标快照 `latest_price_date` | 2026-10-30（陈旧 9 个交易日） | 见 §6 验证 | `/api/data-status/summary` |
| 全量回归 S1 | — | **1055 passed**，`delta_detected=false` | `docs/evidence/evidence-s1/<run>` |
| 静态检查 | — | `ruff check app tests/regression` All checks passed | — |

### 5.1 生产验证（2026-11-13 01:00–01:48 真实自动更新轮）

修复上线后由应用自身触发的真实更新轮，**逐条印证了修复有效**：

| 观测点 | 修复前（2026-11-12） | 修复后（2026-11-13） | 说明 |
|---|---|---|---|
| `xdxr_full_refetch` | 32 → **293**（第 2 轮） | **10** | 除权窗口按股票计算，级联消失 |
| 价格步骤 | 5,573 只 / 143.5 min / 38.83 只每分 / 失败 6 | **1,255 只 / 15.7 min / 约 80 只每分 / 失败 0** | 该轮是"修复 1,255 只 qfq 陈旧"的修复轮，全历史重拉占满 |
| 写库形态 | 逐股独立事务，144 处 ≥10s 静默 | 日志出现 `价格全历史批量写入 N 只 / M 行（单事务）` | 攒批事务生效 |
| 公告截断 | `raise` → ERROR + 计熔断 + 丢弃 150 条 | `CNINFO 公告窗口被截断（返回已抓取部分）` WARNING + `error_kind=truncated_ok` + **返回 600 行** | 数据不再丢，也不计熔断 |
| 新股无分红 | ERROR + 计熔断 + 进 retry | `stock_dividend_cninfo(601123) 无已实施分红（上游空 records）` INFO | 合法空不再当故障 |
| 指标快照 | `rejected`（门禁）/ 对外 `success`（假绿） | `指标快照计算完成: 成功 5576, 失败 0, 隔离 0`，job step `indicators=success` 且确实发布 | 状态与事实一致 |
| 就绪 | `ready=False`、`warning_codes=[SNAPSHOT_STALE, LINEAGE_INVALID, MINIMUM_DATA_NOT_READY]` | **`ready=True`、`warning_codes=[]`** | 三处阻断全部消除 |
| 详情页 | `pe_ttm=null`、`untrusted=true` | `pe_ttm=21.457`、`total_market_cap=1.76e12`、`untrusted=false`、`latest_price_date=2026-11-12` | 数值恢复可见 |

> 该轮 job 最终状态仍是 `partial`：构成是 `announcements=partial`（general 窗口截断，设计取舍）+ `treasury_curve=partial`（当日财政部曲线尚未发布）+ `retries=failed`（4 个历史分红重试在**旧重试代码**下仍计失败，本轮进程启动于修复提交之前；下一次运行将按 `error_kind` 直接结算为 `missing(source_empty/source_unsupported)`）。这正是 §7 残留项 6 要解决的"partial 疲劳"。

## 6. 上线后验证清单（维护者执行）

1. `vd.bat data status` → `data_quality.warning_codes` 不含 `LINEAGE_INVALID` / `MINIMUM_DATA_NOT_READY`。
2. `/api/data-status/summary` → `dates.indicator_snapshot.latest_price_date` = 最近交易日，`minimum_data_readiness.ready=true`。
3. `/api/stock/600519/indicators` → `untrusted=false`、`pe_ttm` 有值。
4. 下一轮自动更新日志应出现：`xdxr_full_refetch` 接近当日真实除权数（0–5，而非 293）、`价格全历史批量写入 N 只 / M 行（单事务）`、无 `无可用适配器 (tried: [])`、无 `所有适配器均失败 (...): None`。
5. `retry_list` 中不应再出现 `dividends` 的 BSE/无分红假失败。

## 7. 残余风险与未做事项

1. **未迁移的适配器**（tdx/sina/ths/cninfo_funding/capital_history/index_valuation）仍返回"未分类空 error"，本轮靠 manager 兜底（error 非空+空数据 → 计入熔断）；后续应逐个迁移到 `error_kind`。
2. **价格批量写仍在主线程**：本轮把"逐股事务"改为"整批事务"，把提交次数降了一个量级，但没有把写库搬出主循环；若单批耗时仍偏长，下一步应改为独立写线程 + 队列。
3. **`TRADING_CALENDAR_UNAVAILABLE`** 仅在单次采样中出现，未连续验证是瞬时还是持续。
4. **`share_capital_history` 与 `stock_meta` 仍是两条链**：本轮把缺口纳入每日补抓，但 history 域不写 `stock_meta` 的设计未变。
5. **`refresh_treasury_spreads` 仍会刷新全表 `calculated_at`**：语义上"值变了就该更新计算时间"成立，但页面陈旧度必须依赖 `data_age_days`（本轮已补）。
6. **`partial` 疲劳（未修，需产品裁决）**：`aggregate_job_status` 把"公告 general 窗口截断""当日国债曲线尚未发布"这类**合法降级**与真失败一起聚合成 `partial`，导致 `last_success_at` 长期不动、状态页告警疲劳。建议引入 `degraded`（合法降级）与 `partial`（真失败）两级，并让 `last_success_at` 在 `degraded` 时也推进；因涉及前端状态展示与既有门禁语义，本轮未动，单独立项更安全。
7. **UTC/CN 边界类缺陷的另一处已修**：日节流标记（`_refresh_due`）与详情页年龄（`build_freshness_metadata`）此前都用 UTC "今天" 与 CN 日期相减，北京时间 00:00–08:00 会差一天（前者导致凌晨重跑约 104s 的 universe 全量刷新，后者让刚发布的快照显示 `snapshot_age_days=-1`）。两处均已按 `CN_TZ` 归一。

## 8. 变更清单（代码）

- `app/core/data_quality.py`：`listed_share_capital_gap_codes` / `snapshot_publish_gate`（分级）/ `actionable_missing_count` / lineage 摘要形态判定
- `app/core/job_status.py`：`merge_step_status`
- `app/core/indicators/calculator.py`：三处发布点按"硬阻断 vs 逐股隔离"分流，`_publish_snapshot(preserve_codes=)`
- `app/core/update.py`：按股票除权窗口、价格攒批写、指标状态合并、股本缺口入通道、retry 分类与 zero-attempt 重查、价格 `failed_codes`、公告入册、`missing_count` 口径
- `app/core/init.py`：`_fetch_listing_info` 登记 share_capital missing / `skipped_codes`
- `app/core/adapters/*`：`error_kind` 体系、熔断 `(adapter,data_type)`、akshare 合法空与 SSE 股本回填、cninfo 截断与 unsupported、baostock B 股/北交所分类、tencent deadline/超时
- `app/core/storage/schema.py`：`retry_list.error_kind`（v18）
- `app/core/storage/duckdb_store.py`：覆盖改进程级全局
- `app/web/main.py`：启动就绪日志 + `/api/ready`
- `app/web/api/data_status.py` / `app/cli/main.py`：missing 口径统一、`data_age_days`
- `app/core/data_maintenance.py`：隔离表 `INSERT ... BY NAME`
- `config/default.yaml`：dividends 链移除 cninfo
