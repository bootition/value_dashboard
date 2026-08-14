---
title: 系统红队全面审查（第二轮）发现基线 — NOT PASS
status: approved
category: reports
created: 2026-08-14
last-reviewed: 2026-08-14
supersedes: reports/77_RED_TEAM_BLOCK_FIX_AND_ACCEPTANCE_2026-08-13.md
---

# 80. 系统红队全面审查（第二轮）发现基线 — NOT PASS

- 审查日期：2026-08-14
- 审查范围：全项目（后端核心链路 / 前端 / 入口与运维 / 测试与门禁）
- 审查方式：主代理 + 4 个分区子代理并行；**全部结论落到 文件:行号 与数据实测**；关键怀疑点在正式库只读查询与临时拷贝库（审查后已删除）上复现
- 基线：`reports/77`（2026-08-13 PASS）、`reports/79`（体验实施）
- 门禁复验口径：S1 590 / Ruff / 前端 lint+test+build 在发现时均全绿 —— 本次证明"门禁全绿 ≠ 功能正确"

## 1. 总体裁决

**NOT PASS / 需修复后复核。** 发现 6 个已复现的 P1 级问题（F1-F6）+ 21 个 P2 + 20 个 P3。其中 F1 是正在发生的自动更新死循环（`auto_update_state.current_stage=failed`、`last_success_at` 停在 2026-08-08），F2 是国债曲线缺口永久化，F3 是筛选条件静默给出错误结果（单位口径双向不一致，影响数千只股票 8+ 字段），F4 是发行版双击 exe 崩溃，F5 是自选页配置过列后永久白屏，F6 是个股详情两张研究卡片跨股票串数据。全部为"功能无法使用或给出错误结果"级问题。

## 2. P1 发现（6 项，全部代码+数据双证）

### F1【阻断】share_capital_history 索引损坏 → 300479 删除确定性 FATAL → 自动更新每轮以失败告终
- 数据证据：`auto_update_state`=failed，last_error 含 DuckDB `FatalException: Failed to delete all rows from index. Only deleted 0 out of 38 rows`（2026-08-13T15:43Z）；`last_success_at`=2026-08-08；`retry_list` 中 300479 retry_count=0 永不递增；job_logs #56/#59 失败记录；`data/logs/start.log` 行 109326/109365 同错（08-13 23:42 启动轮，独立互证）。
- 复现：拷贝正式库（11.99GB）→ `DELETE FROM share_capital_history WHERE stock_code='300479'` → 100% FatalException；`DROP INDEX idx_share_capital_history_stock` 后同一 DELETE 成功（修复路径已验证）。
- 代码根因：`app/core/capital.py:418-422` 单股替换=DELETE+INSERT，命中 `app/core/storage/schema.py:464` ART 索引；`app/core/update.py:1627-1639` retry 消费对 `update_stock` 无 per-task try/except（对照 `capital.py:560-570` 有隔离）→ FATAL 逃逸 → 整轮 failed 且 retry_count 永不递增 → 永久循环。
- 修复：① 单写者窗口 `DROP INDEX` + `CREATE INDEX`（拷贝已验证）；② retry 循环 per-task 异常隔离；③ FatalException 检测→隔离队列。

### F2【阻断（渐进）】国债曲线缺失日永久不补
- 数据证据：`treasury_yield_curve` 最新 curve_date=2026-08-11（价格已到 08-13）；`missing_list` 两条 `treasury_curve_daily_2026-08-12/13`（reason=source_empty）未解决。
- 代码证据：`app/core/treasury.py` `update_daily` 仅抓今天；source_empty 记 missing 而非 retry；`_resolve_missing` 只在同日期成功时消费；`app/core/update.py:1481-1521` 的补缺只处理 listing_info → 无任何代码路径回头抓 08-12/08-13。超过 MAX_STALENESS_DAYS=5 后利差字段整体 NULL，筛选"股息率利差"失效。
- 修复：`refresh_if_due` 增加"最近 N 个交易日缺口回填"（消费 missing_list + 交易日历，每轮有界）。

### F3【阻断】百分比单位口径双向不一致 → 筛选条件静默错误
- 数据证据（正式库 `indicator_snapshot`）：`ttm_dividend_yield`=5.2977（百分数，`calculator.py:724` ×100）vs `dividend_yield`=0.0529（小数）；`div_yield_spread_10y`=3.5816（百分数）；`net_profit_cagr3`=-0.0216、`period_return`/`annualized_volatility`/`max_drawdown`（小数）；`turnover_rate`=0.36（百分数，raw 表交叉验证）。non-null：period_return 5,532 / annualized_volatility 5,234 / net_profit_cagr3 3,421 / ttm+利差 3,596。
- 簇 A（小数存储，前端不换算 → 恒假）：`frontend/src/utils/screening-format.ts:246-256` PCT_FIELDS 缺 `net_profit_cagr3`、`deducted_profit_cagr3/5`、`period_return`、`annualized_volatility`、`max_drawdown` → 输入 20（20%）→ SQL `> 20` vs 0.2 → 恒假。
- 簇 B（百分数存储，前端 ÷100 → 恒真 + 展示 ×100）：`ttm_dividend_yield` + 10 个 `div_yield_spread_*` 在 PCT_FIELDS 中 → 输入 2（2%）→ SQL `> 0.02` vs 5.29 → 几乎全命中；`fmtPct(5.29)` 显示 **529%**。git `77f7a3b`（08-13"单位换算修复"）对这批字段构成回归。
- 簇 C：`turnover_rate` 百分数存储、前端不换算 → 恒假 + 无 % 显示。
- 修复：指标单位元数据单一来源（后端 `/api/screening/indicators` 下发 unit，前端消费），+ 契约测试钉死口径。

### F4【阻断】发行版直接双击 value-dashboard.exe 崩溃
- 实测复现（无 env）：`dist\value-dashboard\value-dashboard.exe` → `PathIsolationError: Missing environment variables: VD_ENV, VD_DUCKDB_PATH, VD_SQLITE_PATH`（`app/core/storage/path_policy.py:93`）；`docs/runbooks/user-first-use.md:18` 明文推荐双击 exe。
- 修复：frozen 运行时无 env 自动推导 exe 同级 `data/`（formal），并同步 runbook 表述。

### F5【阻断】自选页配置过列后永久白屏（TDZ）
- 代码证据：`frontend/src/views/WatchlistPage.vue:88` 在 `:109` 声明前引用 `allColumnOptions`；`:91-97` 配置列时写入 localStorage → 之后每次进入自选页 filter 回调抛 `ReferenceError: Cannot access 'allColumnOptions' before initialization` → 整页白屏（node 同构代码已复现；vue-tsc 不拦 TS2448 类）。
- 修复：声明前移 + 组件测试（预置 localStorage 挂载不抛错）。

### F6【阻断】个股详情两张研究卡片跨股票串数据
- 代码证据：`TreasuryComparisonCard.vue:151-152` 仅 `watch(tenor)`+`onMounted`；`ResearchStatisticsCard.vue:173-174` 仅 `watch(metric)`+`onMounted`；`App.vue:99` `<router-view />` 无 `:key` → `/stock/A`→`/stock/B` 复用组件，两卡片仍显示 A 的数据且无股票代码标注。
- 修复：两卡片补 `watch(() => props.stockCode)` 清空并重拉。

## 3. P2 发现（21 项，节选表格；完整证据见分区报告）

| # | 标题 | 位置 | 证据 |
|---|---|---|---|
| P2-1 | 更新窗口判定查错锁（只查 `.value-dashboard.update.lock`，实为 `.duckdb.write.lock`） | `app/web/api/data_status.py:32-40`、`app/web/api/screening.py:95-101` | 代码对照 |
| P2-2 | 读连接未 read_only | `app/core/storage/duckdb_store.py:96` | 代码 |
| P2-3 | 写路径无重试（读侧有 12 次退避） | `duckdb_store.py:190,201` | 代码 |
| P2-4 | strict_only 恒空（lineage 全记 approximate） | `app/core/screening/engine.py:853-869`、`calculator.py:1394` | 代码推演 |
| P2-5 | S1 证据只覆盖 5 个 DB 文件，data/ 其余写删无检测 | `scripts/s1-path-preflight.ps1:19-25` | 代码+实测 |
| P2-6 | S1 在任意 python 进程存活时拒跑（实测 exit 97） | `s1-pytest.ps1:296-299` | 实测复现 |
| P2-7 | S1 在 PowerShell 5.1 必崩（IsPathFullyQualified 为 PS7 API） | `s1-pytest.ps1:23,189` | 实测复现 |
| P2-8 | 方案 A 快照口径门禁 + CSV 标注零直接测试 | `app/web/api/screening.py:85-133` | 代码对照 |
| P2-9 | `test_collection_safety.py` 嵌套 pytest 收集 + 依赖正式库存在，违反合约 §8.4 | `tests/regression/test_collection_safety.py:55-75` | 代码 |
| P2-10 | 前端 node --test 硬编码 4 文件、engines 版本声明不符 | `frontend/package.json:14`、`vite.config.ts:25` | 代码 |
| P2-11 | start.bat 源码分支失败黑窗瞬闭无 pause | `start.bat:80-81` | 代码 |
| P2-12 | start.bat `where python` 门禁先于 venv 选择 | `start.bat:65-76` | 代码 |
| P2-13 | flaky 根因：TestClient 后台线程泄漏 + `_unlink_with_retry` 2s 上限后裸 unlink | `tests/regression/test_data_status_cache.py:28-68,152-167` | 代码 |
| P2-14 | CLI 读型命令默认对正式库执行完整 DDL（~5s + 写锁） | `app/cli/main.py:27-48`、`schema.py:1122-1164` | 代码 |
| P2-15 | dist\value-dashboard\data 冒烟残留半成品库（2.6MB） | `build-release.ps1:121-137` | 实测 |
| P2-16 | import_csmar/patch_deducted_profit 全表/区间删除无自动备份 | `import_csmar.py:242-455`、`patch_deducted_profit.py:146-151` | 代码 |
| P2-17 | pip 安装态 `vd` 无 env 直接 traceback | `path_policy.py:76-117` | 实测 |
| P2-18 | vd.bat/start.bat 无 setlocal → formal 门禁 env 泄漏到父会话 | `vd.bat:16-19`、`start.bat:27-30` | 代码 |
| P2-19 | 空分组 → groups:[] 侧栏清空 | `app/web/api/watchlist.py:58-71` | 代码 |
| P2-20 | 运行筛选用已保存版本而非屏幕草稿，无提示 | `ScreeningPage.vue:141-178` | 代码 |
| P2-21 | 前端写请求无超时（axios timeout=0） | `frontend/src/http.ts` | 代码 |

## 4. P3 发现（20 项，概要）

后端：国债 N+1 查询（`calculator.py:728-740`）；业务概览覆盖 60/5,543 + `business.py:346` 死代码；dividends_quarantine 50,359 行占 47%（ex_date 占位符，STATUS 缺口 #4 待办）；`transaction()` conn.begin() 在 try 外（`duckdb_store.py:203`）；update_lock 空文件竞态 + PID 复用；DSL validator 循环检测死代码；`research_statistics` ART 索引同型风险；`_build_select` AS-alias 分支死代码；pyproject markers 未注册；ruff 规则集过窄（仅 E4/E7/E9/F）。

入口运维：start.log 10MB 无轮转 + 中文乱码（未设 PYTHONIOENCODING）+ 1,086 个 Traceback；文档命令失配（`vd backup` 实为 `vd backup create`）；README Verdict 滞后（08-03）；`config/sw_industry_raw.xls` 等原始数据被 git 跟踪；`scripts/evidence/` 15 JSON 被 git 跟踪；chain-finalize.ps1 硬编码 PID；check_*.py 依赖 CWD；data/ 测试残留（test_writeback.csv 等）。

前端：搜索页无错误处理 + 响应乱序竞态；DataStatusPage `isPolling` 恒真；K线整图重建 + 卸载不 abort；加载规则覆盖草稿；toFixed(4) 精度漂移；between 切回单值残留数组；运行按钮 auto-update 状态只在挂载读一次；PDF URL 未 encodeURIComponent。

S1/门禁：用 PATH python（3.14）非 venv；证据哈希 ~67GB/轮读；conftest 内层证据未实现（合约 §8.2）；hash 逻辑无单元测试（合约 §10.1）。

## 5. 修复顺序（承接 reports/81）

1. 立即：F1（索引重建 + retry 隔离）、F3（单位元数据）、F5、F6。
2. 本周：F2 补抓、P2-1 写锁统一、start.bat 两处、F4 冻结默认路径。
3. 门禁轮：zone4 P1/P2 全套（S1 证据全树指纹、PS7 守卫、freeze 收窄等）。
4. 长期：DuckDB 升级评估、ex_date 待办、业务概览提速。

## 6. 证据位置

- 分区原始报告（会话产物）：`.planning/2026-08-14-red-team-full-review/zone1-backend-sub-storage.md`、`zone2-frontend.md`、`zone3-entry-ops.md`、`zone4-tests-gates.md`
- 主报告（会话产物）：`.planning/2026-08-14-red-team-full-review/red-team-report.md`
- 全部 P1 有：代码行号 + 正式库只读实测值 + 拷贝库复现记录；审查全程只读，未改动产品代码与 data/。
