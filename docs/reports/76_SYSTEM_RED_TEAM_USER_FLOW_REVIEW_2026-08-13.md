---
title: 系统红队全面审查与用户流程实测（BLOCK 发现基线）
status: superseded
category: reports
created: 2026-08-13
last-reviewed: 2026-08-13
superseded-by: 77_RED_TEAM_BLOCK_FIX_AND_ACCEPTANCE_2026-08-13.md
---

# 系统红队全面审查与用户流程实测（BLOCK 发现基线）

> 本轮为独立红队审查：不采信文档结论，全部以代码阅读、只读数据探测、
> 门禁实测与真实用户流程走查为准。审查期间对正式库的唯一写入是启动
> 自动更新的正常运行时路径（与普通用户一致），测试产生的筛选规则已在
> 服务停止后清理并核验零残留。

## 审查范围与方法

| 层面 | 方法 | 结论 |
|---|---|---|
| 代码门禁 | `ruff check`、S1 隔离回归（574 passed，正式库前后哈希一致）、前端 lint + 55 Node 测试 + 40 vitest + build | ✅ 全绿 |
| 数据完整性 | 正式库只读探测（DuckDB read_only + SQLite mode=ro）：覆盖、一致性、新鲜度、畸形值 | ✅ 与披露一致 |
| 安全控制 | 写令牌 403、跨域 403、Host 头 400、PDF 路径穿越、K线 adjust 白名单、注入拒绝 | ✅ 无 P0/P1 安全项 |
| 用户流程 | 真实启动服务（start.bat 同路径），走查数据状态/筛选/个股详情/国债/自选全部端点 | ❌ 发现 P1 |

## 裁决

**BLOCK（1 项 P1 + 6 项 P3）**。P1 为自动更新窗口内核心研究链路失效，
影响"普通用户路径"每日可用性且未被任何历史报告披露；对照本项目历史标准
（发现 P1 → BLOCK → 修复后复审，如 reports/73→74），判 BLOCK。

---

## P1：自动更新窗口内核心研究链路失效（未被披露）

### 实测现象（2026-08-13 14:12 启动正式服务后）

自动更新随每次启动自动执行（`app/web/main.py:106-111`）。本次轮次因 08-12
交易日新价格缺口触发，价格步 5,537 只、实测 79.5 只/分钟、ETA 约 3,214 秒
（约 70 分钟）。该窗口内：

| # | 现象 | 证据 |
|---|---|---|
| 1 | `/api/stock/{code}/indicators` 耗时 **43~67s**，且多次 **HTTP 500** | `duckdb_store.py:86-100` 同进程并发 connect 竞态，重试仅 5×0.5s；写事务持续数秒时必然耗尽重试并 `raise last_error` 直通 500 |
| 2 | `/api/stock/{code}/treasury-comparison` **>60s 超时** | 逐价格日 N+1 查询（每日本身 2~3 次读）+ 写锁串行化，无 stale 缓存 |
| 3 | `/api/screening/run` 交替出现 409（门禁，属设计）与 **500**（竞态 bug） | 409 为 `_require_current_screenability` 门禁；500 为 DuckDB connect 失败 |
| 4 | 全部快照指标被遮蔽（`untrusted_all`）、readiness 从 true **翻转为 false** | 价格写入使 `snapshot_price_coherence`（`data_quality.py:461-472`）对已更新股票失配；快照重建在轮次末尾（`update.py:428-443`）才恢复，窗口持续整个价格步 |
| 5 | 更新期间**任何外部进程只读无法打开 DuckDB** | 实测 `read_only=True` 也被拒：`File is already open in PID ...`（DuckDB Windows 单进程独占） |

### 影响

- 每错过一个交易日 = 下一次启动后约 1 小时窗口内：个股研究工作台指标摘要
  超时/500、国债利差图打不开、筛选被 409/500 阻断、所有快照指标显示"数据不可信"。
- 前端 15s 超时（reports/55 记载）使 43~67s 响应必然表现为页面报错。
- `reports/55` 只为数据状态页做了写锁感知 stale 缓存；个股详情/筛选/国债路径
  无同等保护。`reports/62` 的"普通用户路径恢复完成"与性能验收（10/10 <5s）
  均未覆盖更新窗口，存在文档与实况不符。
- 外部只读工具（`vd data diagnose`、备份前检查等）在服务运行期间无法打开
  DuckDB（写锁与普通服务运行期均如此，实测为 DuckDB Windows 平台特性）。

### 根因（代码定位）

1. `app/core/storage/duckdb_store.py:86-100`：重试窗口过短（5×0.5s=2.5s），
   写事务（批量 upsert，实测 4,056 行/批）持续数秒时必然失败。
2. `app/core/data_quality.py:757-775` `read_warning_codes`：每 30s TTL 全量
   重建 `build_data_quality_status`（含 3,280 万行 source_audit 扫描 + 全市场
   一致性子查询），写锁活跃时一次重建 40~60s，且无锁内跳过机制。
3. `app/web/api/stock_detail.py:759-908` `treasury-comparison`：逐日串行
   N+1（分红聚合 + 曲线对齐），无批量路径（`align_many` 已存在未复用）。
4. 遮蔽语义本身为 fail-closed 设计（reports/34 P1-4），但更新触发的全市场
   遮蔽与 409 门禁在窗口期内叠加 500/超时，形成"完全不可用"体验。

### 修复方案（reports/77 实施）

1. `read_connection`：指数退避重试（上限 ~30s），写锁活跃时放宽耐心。
2. `read_warning_codes`：写锁活跃时跳过全量重建，返回进程内最近缓存；
   无缓存时返回空集合并由端点标注 `auto_update_in_progress`（stale-while-
   revalidate，与 data-status 页既有模式一致）。
3. `treasury-comparison`：分红 TTM 改为单次全量读取 + 内存滑动窗口，
   曲线对齐改用 `align_many` 单次查询。
4. 写锁活跃时 `/api/screening/*` 门禁直接返回 409 `auto_update_in_progress`
   （跳过重型一致性查询，替代 500 与 74s 慢查询）。
5. 前端：个股详情/自选页在 `auto_update_in_progress` 时显示"自动更新中，
   数据截至 xx"横幅（替代遮罩空值）；筛选页 409 增加对应提示文案。

---

## P3 清单（修复随 reports/77）

| # | 发现 | 证据 | 处理 |
|---|---|---|---|
| P3-1 | qfq 历史收盘 ≤0 共 56,614 行（42 只高分红股，1993-2018；2019 年后 0 行；`price_daily_qfq` close<0 为 56,396、=0 为 218） | 只读探测；TDX 减法复权特性 | 文档披露；不改数据（历史真实复权口径） |
| P3-2 | 正式库残留 `research_statistics_staging_*` 表（196,178 行） | 只读探测：`research_statistics_staging_ff23625730c542a486304a53256a40c3` | 扩展统计域清理逻辑并执行一次清理 |
| P3-3 | 未知 `/api/*` 路径返回 index.html（200）而非 404 JSON | `main.py:363-374` SPA 兜底未排除 /api 前缀 | SPA 兜底前对 /api/* 返回 404 JSON |
| P3-4 | 规则保存时不校验字段名（运行时才拒，`engine.py:546`） | 实测 `nonsense_field` 规则保存成功 | 保存时前置校验（field/right_field/sort/columns） |
| P3-5 | `app/web/static/assets/` 与 `static.staging-*` 残留大量历史哈希包 | 目录实测 | `sync-static.mjs` 已有清理逻辑，残留为历史遗留：执行一次构建清理 + 删除孤儿 staging 目录 |
| P3-6 | watchlist remove 对不存在代码返回 ok（无行数反馈） | `watchlist.py:194-208` | 返回实际删除行数，0 行返回 404 |

## 通过项（实测证据）

- 门禁：S1 574 passed（含 wheel 内容、路径隔离、安全、DSL 生命周期等）；
  正式库前后/清理后哈希一致（evidence-s1/879c8ae4… 含 hash-evidence 与
  post-cleanup-hash-evidence）；ruff 0 问题；前端 lint/55+40/build 全绿。
- 数据：5,542 只上市；`circ_shares>total_shares`=0；快照重复键 0；raw 负价 0；
  价格至 2026-08-11 全市场达标（仅新股披露项）；快照 5,533 只/2026-06-30；
  国债 9 期限至 08-11；retry 2、missing 未解决 1 与 STATUS 披露一致。
- 安全：写令牌/跨域/Host/PDF 穿越/字段注入全部按预期拒绝。
- 崩溃恢复：强杀服务后 1,554 只 08-12 价格零畸形行；`stale_running_job`
  对账与死锁回收路径代码 + S1 均验证。

## 现场收尾

- 测试规则 2 条已删除（前后对比 0 残留，无孤儿 run/result/watchlist）。
- 服务已停止；`data/.value-dashboard.update.lock` 为死锁（属主 PID 已退出），
  下次启动自动回收（`update_lock.py:84-87`）。
- 08-12 价格已部分补到 1,554 只（合法部分进度），下次启动有界续传。
- `git status` 干净。
