---
title: ETF 预算保存后整页报错与自动更新窗口读锁降级修复
status: approved
category: reports
created: 2026-09-17
last-reviewed: 2026-09-17
---

# ETF 预算保存后整页报错与自动更新窗口读锁降级修复（2026-09-17）

## 1. 结论

用户在「指数 → ETF 策略」页保存某行业 ETF 预算后，整个 ETF 页报错、随后个股详情/搜索也报错。
根因不是预算数据本身，而是**自动更新子进程持 DuckDB 文件锁期间，缓存失效语义把唯一的降级快照删掉了**：

1. `POST /api/etf/meta` 成功写 SQLite 后调用 `_overview_cache.invalidate()`，旧概览快照被删除；
2. 前端立即 `GET /api/etf/overview` 重算 → DuckDB 被更新子进程独占（Windows 文件锁）→ `DuckDBReadLockedError`；
3. `TTLCache.get_or_compute` 无 stale 可退 → 503；前端模板 `v-if="errorText"` 优先于 `v-else-if="data"`，把内存中仍可用的数据整页替换成错误提示；
4. 同时未做缓存的个股详情/搜索/K线接口裸 500、自选 503、`/api/health` 503 → 用户体感为"整个服务报错"。

修复已落地（见 §4）：预算/持仓/流水等 SQLite 数据改为实时读、不再触发整体失效；市场快照（DuckDB 派生）保留过期回退；未捕获的读锁异常统一降级为明确 503。

## 2. 实测时间线与证据（2026-09-17）

| 时间（+08:00） | 事件 | 证据 |
|---|---|---|
| 11:42:17 | Web 服务启动（PID 5160），11:42:29 后台就绪核对 `ready=True` | `data/logs/start.log` |
| 11:42:29~11:43:12 | 启动前预热指数/ETF 只读缓存（旧代码把概览快照放进 `_overview_cache`） | `start.log` "自动更新前预热…" |
| 11:43:12 | 启动自动更新子进程（PID 28840，job `23372e95-…`），price 步骤 4732 只 | `start.log`；`auto_update_state.progress_json` |
| 11:46:17 | 用户保存 516970（基建50ETF/建筑装饰）预算 1200.0，并保存总资产 4534.25 | SQLite：`etf_meta.updated_at=2026-09-17T03:46:17.056Z, budget=1200.0`；`etf_settings.total_assets=4534.25` |
| 11:46 之后 | `GET /api/etf/overview` 503「数据正在自动更新中」；`GET /api/index/overview` 返回全 error 项；`/api/stock/*` 500；`/api/watchlist/list` 503；`/api/health` 503 | 现场 curl 复测（会话记录）；`start.log` 中 3 段 `/api/stock/*` 的 `DuckDBReadLockedError` traceback |
| 12:14 | 更新仍在 price 步骤（2080/4732，约 66 只/分） | `/api/data-status/auto-update` |

复现（隔离环境，确定性）：预热缓存 → `invalidate` → 工厂抛 `DuckDBReadLockedError` → 修复前必然抛出/503，修复后返回旧快照。

## 3. 根因链（代码级）

1. **Windows 下外部写进程独占**：DuckDB 子进程读写的同一文件，Web 进程在锁存续期间**完全无法新建任何连接**（日志：`Cannot open file ... 另一个程序正在使用此文件` + `File is already open in ... PID 28840`）。报告 104 记录的"统一连接配置"只解决同进程/配置冲突，不解决跨进程独占。
2. **`invalidate` 删除唯一降级手段**：`app/web/api/_ttl_cache.py` 旧实现 `invalidate` 直接 `pop` 条目；`app/web/api/etf_strategy.py::post_meta` 写后失效概览缓存 → stale 回退消失。
3. **前端错误态覆盖可用数据**：`EtfStrategyPanel.vue` 旧模板 `v-if="errorText"` 优先于 `data`，保存成功后的刷新失败使整页只剩错误横幅。
4. **错误文案无匹配**：`friendlyErrorMessage` 对 503 中文 detail 无规则，落到"服务器内部错误"。
5. **未缓存的只读接口无降级**：`app/web/api/stock_detail.py` 未捕获 `DuckDBReadLockedError` → 裸 500；watchlist 503 透传原始 IO 错误。

同类缺陷（同期复审发现）：
6. `/api/index/overview` 的 `stale_on` 是**死代码**：`erp_compare` 用 `except Exception` 把锁错误吞成全 error 快照（HTTP 200），随后 `_uncacheable_error_snapshot` 再 `invalidate(keep_stale=False)`，预热旧值先被覆盖、再被删除；前端 `IndexPage` 对 200 直接写 `items` 与 sessionStorage，卡片墙在更新窗口内退化为整页 `—/0 样本`。

## 4. 修复清单

### 4.1 缓存降级语义（`app/web/api/_ttl_cache.py`）
- `invalidate(key, *, keep_stale=True)`：失效时把旧值时间戳回拨为"已过期"并保留，正常路径仍必定重算；重算遇 `stale_on` 异常时返回旧值。`keep_stale=False` 仅供"结果本身不可用"（全 error 快照）场景。
- `get_or_compute(..., reject=...)`：被判定为不可用的计算结果不写缓存；有旧值返回旧值，无旧值保持调用方原语义（如逐项 error 的 200）。

### 4.2 ETF 概览/详情分层（`app/web/api/etf_strategy.py`）
- 新增 `_compute_market_context` / `_market_context`：只把 DuckDB 派生数据（最新价、指数估值、主指标分位）放入 10s TTL 缓存并启用 stale 回退。
- `_compute_overview` / `_compute_detail`：预算、持仓、流水、设置、卖出计划等 SQLite 数据**每次实时读**；写操作后立即生效，且写后刷新不再依赖 DuckDB 快照。
- 预算/交易/资金/设置 POST 不再整体失效概览；仅 meta/reset/bootstrap 失效市场快照（保留旧值兜底），meta 变更同时失效对应基本面缓存。
- `post_meta` 未携带 `note` 时保留原备注（预算弹窗不发送该字段，避免误清空）。
- `post_setting` 校验必须是数字、有限、非负（此前 `"abc"` 也会写入 settings）。

### 4.3 统一读锁降级（`app/web/main.py`、`app/web/api/watchlist.py`）
- 全局 `DuckDBReadLockedError` 异常处理器：返回 503 `{detail: "数据正在自动更新中，请稍后刷新", reason_code: "duckdb_read_locked"}`，未单独捕获的只读接口不再裸 500。
- watchlist 单独识别读锁，返回同样的中文提示，不再透传原始 IO 错误。

### 4.4 指数对比 stale 回退（`app/core/index_dashboard.py`、`app/web/api/index_dashboard.py`）
- `erp_compare` 对 `DuckDBReadLockedError` 向上抛（其余异常保留"逐项 error 200"语义）。
- `_cached_compare` 改用 `reject=_uncacheable_error_snapshot`：全 error 快照不写缓存、有预热旧值返回旧值；无旧值才按原契约返回 200 逐项 error。
- `_all_a_series_memo` 失效键补 `db_path`（修复跨库串数据）；新增 `_all_a_market_snapshot` 30 分钟 memo，避免每次缓存未命中都对 19.7GB 库跑两条全表窗口查询。

### 4.5 锁判定收紧（`app/core/storage/duckdb_store.py`）
- `_is_external_file_lock`：泛化的 `Cannot open file` 仅在应用级更新锁文件确认存在时算锁竞争；权限/路径等永久性 IO 错误继续退避后如实抛出，不再伪装成"正在更新"。

### 4.6 前端（`EtfStrategyPanel.vue`、`api-error.ts`、`FundamentalChart.vue`）
- 概览刷新失败但有数据时：保留表格并显示顶部警告条 + 重试，不再整页替换错误态。
- 详情弹窗加请求代次 + AbortController，`@after-leave` 作废在途请求，修复"关 A 开 B 串数据/慢请求回填"。
- 预算弹窗内切换 ETF 时同步该只预算/层级/间距，修复"把上一只的预算写进新选中 ETF"。
- 买卖/资金弹窗每次打开复位，避免旧值重复提交。
- `friendlyErrorMessage` 增加 `duckdb_read_locked` / "正在自动更新"中文规则。
- `FundamentalChart`：年度聚合改为两条序列各自取年内最后一个非空值（修复最新年份利润柱丢失）；负值柱范围纳入负轴（修复亏损年份柱子不可见/1px）。

## 5. 验证

- **官方 S1 门禁**（`scripts/s1-pytest.ps1 tests/regression`）：**850 passed**，正式库前后 SHA-256 一致（`delta_detected=false`，证据 `docs/evidence/evidence-s1/663a799a1bf347338d0cc6b6e1e2fcbb/`）。
  - 期间修复一处既有"时间炸弹"测试：`test_period_semantics.py::test_current_half_year_report_is_not_stale` 硬编码 `price_date=2026-09-07`，在 9-17 运行时 `price_age_days=10>7` 必然失败（与产品行为无关）；改为基于当前时刻构造，保持"当期报告期不因财务年龄告警"的原意。
- Ruff（`uv run --locked ruff check app tests/regression`）全绿。
- 前端：`npm run lint`、`node scripts/run-unit-tests.mjs`（73 passed）、`vitest run`（57 passed）、`npm run build` 全绿；静态包已同步（`frontend_build=db397cae`）。
- **线上复核**（2026-09-17 22:55 重启加载新代码；恰逢新一轮价格补抓窗口）：`/api/etf/overview` 200（516970 `budget=1200 / tranche=120 / current_price=1.106 / signal=buy`）、`/api/etf/516970/detail` 200、`/api/index/overview` 200、`/api/stock/search` 200、`/api/watchlist/list` 200；更新子进程持锁瞬间仅 `/api/health` 503（数据库探测语义不变），其余页面按 stale 快照降级，不再整页报错。

## 6. 复审发现但本报告未修的问题（按优先级）

| 级别 | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2 | `app/core/etf_fundamentals.py:market_cap_series`、`app/core/index_dashboard.py:_all_a_valuation_series` | 历史市值用「前复权收盘 × **当前** total_shares」，与仓库既有 `share_capital_history` 逐日股本口径冲突；分红除权与增发方向相反，10 年历史曲线/分位被扭曲（当前值正确） | 历史段改 `price_daily_raw` + ASOF `share_capital_history`，缺失 fail-closed；对齐 `statistics.py` 口径后再发布 |
| P2 | `frontend/src/components/IndexValuationChart.vue` | `bands` prop 声明但未参与渲染，改用当前窗口现场算 P20/P50/P80；切 1 年窗口后图例口径与页面"近 10 年分位"文案不一致，后端 p10/p90 永不显示 | 有 `bands` 时用后端 10 年分位带并标注窗口；否则删除 prop 并同步父组件与文案 |
| P2 | `app/core/etf_pool.py` + `app/core/etf_prices.py` | 伪代码 `ALL_A` 在默认池 enabled=1，行情抓取必然 error/空 → retry/missing 永久条目，更新报告长期 `partial` | 抓取侧过滤伪代码，或加 `is_pseudo` 标记 |
| P3 | `app/core/etf_fundamentals.py` | `current.keys() \| previous.keys()` 集合迭代顺序随 `PYTHONHASHSEED` 变化，影响 top15/其他行业切分；`_size_proxy_codes` 空结果不回退全A；成分匹配失败仅中文 disclaimer，无机器可读标记 | 先 `sorted` 再排序；空结果与 `_sw_codes` 对齐回退并显式标注 `is_proxy` |
| P3 | `app/core/index_dashboard.py:_all_a_index_summary` | 序列计算 `except Exception` 无日志，失败时仍 `status=ok/partial`、`samples=1`、分位为空 | 至少 warning + 可判定降级标记 |
| P3 | `frontend/src/components/IndexValuationChart.vue`、`FundamentalChart.vue`、`IndustryContributionBars.vue`、`IndexDetailPage.vue`、`IndexPage.vue` | hover 下标跨窗口不重置；x 轴 key 可能重复；贡献柱方向按 delta 而文案按 contribution_pct（总利润为负时符号相反）；上下两个 radio 共用 `fundGrowthMode`；fundamentals catch 缺 generation 检查；缺失分位显示"—%"；`FundamentalPoint` 字段名与后端不符且未使用 | 见前端复审逐条建议，建议二期统一处理 |
| P3 | `app/core/etf_reset.py:bootstrap_portfolio` | 初始化不写 `etf_cash_flows`，概览"累计净入金"显示 0 而持仓市值 >0 | 补一条资金流水，或在 UI 明确该口径只统计手工流水 |

## 7. 运维补充

- 本次更新窗口（2026-09-17 11:43 起）为**价格补抓**：4732 只、约 72 只/分，price 步骤 65 分钟；job 184（15:33）再补一次（5551 只、148 只/分）。**22:55 重启后调度器又判定 due 并启动第三次全量价格补抓（5406 只、约 150 只/分）**，说明 `price_daily_raw` 最新交易日仍落后于 `trading_dates` 目标日；说明该判定只看"最新交易日"、不看失败明细。
- `auto_update_state.last_success_at` 停留在 2026-09-11：9-16/9-17 多次增量更新均因新股指标/CNINFO 公告降级为 `partial`，不满足 `status == "success"` 的记录条件；调度是否 due 只看 `price_daily_raw` 最新交易日，故会反复触发补抓。属既有设计，本报告不改动。
- **遗留运维风险**：更新窗口内 `/api/health` 仍返回 503（数据库探测语义），而 `start.bat` 用 `/api/health` 判断"是否已有实例"；更新期间双击 start.bat 会误判未运行并尝试启动第二实例（端口占用报错）。建议后续让 launcher 改用 `/api/session` 或让 health 在"服务已起、仅 DB 暂锁"时返回 200 + `updating=true`。
