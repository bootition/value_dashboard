---
title: 综合红队复审（2026-07-31）
status: superseded
category: reports
last-reviewed: 2026-07-31
superseded-by: reports/29_DATA_REBUILD_REPORT_2026-07-31.md
---

# 综合红队复审（2026-07-31）

## 裁决

**BLOCK。不得发布，也不得将当前正式数据用于价值研究、筛选、导出或投资决策支持。**

本报告补正 `docs/26_DATA_REAUDIT_SHARE_CAPITAL_2026-07-31.md` 的范围限制。审查覆盖当前工作树的正式数据、指标和筛选、维护脚本、存储/备份恢复、API/DSL、前端呈现、构建与运行门禁；`docs/26` 的全部数据结论均是本报告的一部分。

当前确认 **1 个 P0、20 个 P1、1 个 P2**。任一 P0 足以阻断发布；当前正式数据的多项 fail-closed 门禁也独立构成 BLOCK。

## 方法与证据

- 当前正式库只读证据：`docs/evidence-formal-share-capital-reaudit-20260731.json`，DuckDB SHA-256 为 `21ce1cd890428e15714d4698a51653cc9c0e98e2a3faf09bd120db15c2de4c70`。
- 股本一致性证据：`docs/evidence-data-reaudit-share-capital-20260731.json`。
- Python 隔离回归：`scripts/s1-pytest.ps1 tests/regression -q --no-header`，结果 `288 passed, 1 failed`。失败为 P2-1，详见下文；S1 包装器隔离了正式数据库。
- 前端：`npm run lint`、`npm run test`（46/46）和 `npm run build` 均通过。
- 依赖锁：`uv lock --locked` 通过。
- Python 静态检查：`uv run --locked ruff check app tests/regression` 失败，确认两个可达未定义名称，见 P1-12 和 P1-13。
- 未将测试、构建或静态检查的全绿/失败外推为正式数据正确性；当前正式数据门禁仍为 `LINEAGE_INVALID` 和 `MINIMUM_DATA_NOT_READY`。

## P0

### P0-1：SSE 股本单位混用产生 1,215 条不可能记录

- 类别：数据完整性、维护脚本、研究正确性。
- 位置：`scripts/repair_sse_share_capital.py:48-75`、`scripts/repair_sse_circ_shares.py:39-49,79-83`、`app/core/storage/schema.py:37-38`、`app/core/data_quality.py:168-169`。
- 证据：当前 2,283 只可比较 SSE 股票中，1,215 只 `circ_shares > total_shares`，最大 16,459.42 倍；`docs/evidence-data-reaudit-share-capital-20260731.json:15-40`。例如 688428 的总股本是 23,494、流通股本是 386,697,602。
- 根因：脚本将资产负债表中的 `paid_in_capital` 直接作为单位为“股”的总股本写入。样本量级证明该字段为“万股”，同时 Eastmoney `f85` 使用股单位。`minimum_data_readiness()` 仅检查大于零，未拒绝不可能的大小关系。
- 影响：总市值可缩小约 10,000 倍，流通市值可高于总市值；`app/core/indicators/calculator.py:484-494` 会把错误直接物化到指标。股本修复还未重算 SSE/SZSE 市值快照。
- 修复：回滚/隔离未经单位证实的写入；只接受按股票提供单位和 as-of 日期的权威来源；写入前要求 `total_shares > 0`、`circ_shares > 0`、`circ_shares <= total_shares`；以数据、archive、batch、field audit 同一事务发布；随后全量重算并原子发布快照。
- 复验：对全市场断言零条 `circ_shares > total_shares`，对独立供应商样本逐条比对，断言所有股本批次都有原始材料和字段级 audit，且快照时间晚于股本批次。

## P1

### P1-1：正式数据仍整体不可追溯且多项最小数据门禁不通过

- 类别：数据完整性、数据治理。
- 位置：当前正式数据；门禁实现 `app/core/data_quality.py:91-263,319-446`。
- 证据：5,534 只均缺字段级 lineage coverage，archive gap 2,535,176；价格新鲜度缺口 5,532，raw/QFQ 历史缺口 1,309/1,356，监管字段缺口 92，公司行为/分红链路缺口 538。完整数字见 `docs/evidence-formal-share-capital-reaudit-20260731.json:244-305`。
- 影响：当前虽然 fail-closed，但正式研究数据不能被认证为完整、当前或可复算。
- 修复：以权威原始材料重建或明确隔离历史数据；按字段保存来源响应、hash、映射、抓取时间和 confidence；为价格、财报、公司行为和监管字段执行全量覆盖核验。
- 复验：新的正式哈希基线中，readiness 为 true、无 `LINEAGE_INVALID`，并保存可重跑的逐项覆盖报告。

### P1-2：损坏 lineage 不会阻断服务端筛选

- 类别：代码、数据治理。
- 位置：`app/core/data_quality.py:13-35,422-428`。
- 证据：`build_data_quality_status()` 会将 hash/archive 故障标为 `LINEAGE_INVALID`，但 `_SCREENING_BLOCKING_WARNINGS` 未包含该码。其他 readiness 条件满足时，损坏证据仍可通过筛选、保存、导出和加入自选。
- 影响：产品可持久化和导出声称有来源、实际材料已损坏的研究结果。
- 修复：将 `LINEAGE_INVALID` 加入服务端阻断集合；对 archive/hash 故障保持 fail-closed。
- 复验：构造其余条件均 ready 的隔离 fixture，篡改 payload 后断言四个持久化入口均返回 409。

### P1-3：维护脚本绕过原子来源链路

- 类别：数据治理、维护流程。
- 位置：`scripts/repair_xdxr.py:65-88`；`scripts/repair_dividend_announcement_dates.py:110-127`；股本脚本 `scripts/repair_sse_circ_shares.py:79-83`、`scripts/repair_sse_share_capital.py:67-76`。
- 证据：这些脚本直接写业务表，未同事务写 `fetch_batch`、`raw_response_archive` 和 `source_audit`。分红脚本将 archive 与字段更新拆为独立写操作；股本脚本没有字段 audit。
- 影响：修复结果即使看似覆盖完整，也无法复验来源和字段映射，失败中断还能留下半状态。
- 修复：统一走 `DataInitializer` 的 canonical upsert、`_record_batch_in_connection()` 和 `_record_field_audit_in_connection()`，在一个 DuckDB transaction 内完成。
- 复验：对每个维护脚本注入写入异常，断言业务数据、archive、batch、field audit 要么全部提交，要么全部回滚。

### P1-4：部分 CSV 维护输入可删除全部较新财务历史

- 类别：数据毁损、维护脚本。
- 位置：`scripts/import_csv_to_db.py:29-43,73-97,137-156,186-201`；`scripts/supplement_akshare.py:280-288`。
- 证据：`_prepare_frame()` 为所有缺失非键列补 NULL；通过键检查后脚本删除所有 `2025-03-31` 后的既有行，再导入任意范围的 CSV。没有预期股票/报告期覆盖、staging swap、备份或来源验证。
- 影响：一份语法正确但只有一只股票或一个报告期的 CSV 可静默删除其余当前财务数据。
- 修复：先 staging，验证完整 key 集、预期覆盖、关键字段非空率、来源和快照影响；通过后以原子 swap/upsert 发布。删除式替换必须先创建可验证备份并要求明确确认。
- 复验：两只股票的正式期 fixture 导入单股票 CSV 时应拒绝，原数据保持不变。

### P1-5：中断恢复会被遗留维护锁永久阻塞

- 类别：恢复可靠性、运行可用性。
- 位置：`app/core/storage/maintenance.py:39-61`；`app/core/backup/manager.py:45-58,619-652`；`app/web/main.py:289-296`。
- 证据：进程在 restore 中死亡会遗留 `.value-dashboard.maintenance.lock`。启动恢复通过 `exclusive_maintenance()` 重新获取同一锁，因锁已存在而失败；`run_server()` 还在恢复前执行 schema 初始化，写入同样被锁拒绝。
- 影响：可恢复的 journal 反而让 profile 永久不可启动；若 DuckDB/SQLite 已写到不同世代，无法自动回滚。
- 修复：维护锁加入 owner token/PID；仅在验证 journal rollback set 后允许恢复路径回收死亡持有者的锁；启动时先恢复再 schema 初始化。
- 复验：子进程在 public restore commit 后强杀，保留 journal/lock；新进程应自动恢复双库、删除 journal 与锁并可启动。

### P1-6：同步数据库工作运行在 async API 事件循环上

- 类别：运行可用性、API。
- 位置：`app/web/api/screening.py:81-143`、`app/web/api/data_status.py:15-247`、`app/web/api/stock_detail.py:176-647`；阻塞存储 `app/core/storage/duckdb_store.py:71-90`、`app/core/storage/sqlite_store.py:33-86`。
- 证据：路由定义为 `async def`，却在协程内直接执行同步 DuckDB、SQLite 和筛选。单默认 Uvicorn worker 下慢筛选、质量 hash 或大导出会阻塞 health 与所有 UI 请求。
- 影响：本地服务表现为冻结，长操作还能触发客户端超时和前端错配。
- 修复：将纯阻塞路由改为同步函数以进入 FastAPI threadpool，或以 `run_in_threadpool` 包裹完整数据库工作单元。
- 复验：注入延迟筛选/质量查询，同时请求 `/api/health`，必须在设定 SLA 内响应。

### P1-7：详情页面可能显示前一只股票的数据

- 类别：前端正确性、研究呈现。
- 位置：`frontend/src/views/StockDetailPage.vue:66-80,83-147,253`。
- 证据：路由变更启动六个请求，只为 K 线设置 `AbortController`。信息、指标、财务趋势、溯源和质量告警无取消或 generation 校验。
- 影响：快速从 A 跳至 B 时，A 的慢响应可以覆盖 B URL 下的研究数据。
- 修复：为整个 route load 使用单一 generation token 或取消所有股票作用域请求；仅当前 code/generation 可提交状态。
- 复验：延迟 A 响应、先跳转 B，断言所有区块仅显示 B。

### P1-8：当前已知不可信指标在个股页没有统一警示

- 类别：前端呈现、数据真实性。
- 位置：`frontend/src/components/IndicatorTabs.vue:115-158`；规则在 `frontend/src/types/data-quality.ts:102-158`。
- 证据：`LINEAGE_INVALID` 和 `MINIMUM_DATA_NOT_READY` 会使 snapshot 指标不可信，但页面仅对部分分红字段渲染“数据未验证”；PE/PB/ROE/成长/安全指标仍以普通数值展示。当前正式数据正具有两个告警。
- 影响：用户会从视觉上把明确不可验证的数据当成正常研究结论。
- 修复：持续展示全局 fail-closed alert，并标记或隐藏每个受影响的 snapshot 指标。
- 复验：浏览器测试注入 `LINEAGE_INVALID`，断言每个 snapshot 指标均有不可用状态而非正常数值。

### P1-9：筛选草稿存在乱序覆盖

- 类别：前端并发、数据丢失。
- 位置：`frontend/src/views/ScreeningPage.vue:148-164`；`app/web/api/screening.py:164-173`。
- 证据：多次 debounce 写请求独立发送；服务器无版本比较直接 upsert id=1。较早草稿 A 若在较新 B 完成后返回，会覆盖 B。
- 影响：用户看到 B、持久草稿却回退为 A。
- 修复：前端串行化写入并携带单调 revision；服务端拒绝过期 revision。
- 复验：延迟 A、让 B 先完成，读取持久草稿必须为 B。

### P1-10：PDF 错误响应被伪装为 HTTP 200

- 类别：API 语义、错误真实性。
- 位置：`app/web/api/stock_detail.py:658-718`；现有错误测试 `tests/regression/test_stock_detail_periods.py:38-50`。
- 证据：非法路径、文件不存在、路径穿越和冷归档均返回 HTTP 200 的 JSON `{error: ...}`，而不是相应 400/404/409。
- 影响：浏览器、监控、缓存与调用方把失败记录为成功；`window.open()` 只显示 JSON 空页。
- 修复：使用 `HTTPException` 和 400/404/409；成功响应必须为 `application/pdf`。
- 复验：覆盖每个失败路径的状态码和 content type。

### P1-11：watchlist 吞掉 DuckDB 读取失败并返回成功空数据

- 类别：API 真实性、前端呈现。
- 位置：`app/web/api/watchlist.py:63-79`；`frontend/src/views/WatchlistPage.vue:101-113`。
- 证据：宽泛 `except Exception` 将 `info_map` 设为空，端点仍返回 200，前端将空名称、交易所和指标视作成功加载。
- 影响：数据库损坏/锁冲突被伪装为“没有数据”，用户无法区分系统故障。
- 修复：核心读取失败返回 503，或返回显式 partial/error 状态并在 UI 明示不可用。
- 复验：mock `read_query()` 抛错，断言非 2xx 或 UI 不可用告警。

### P1-12：DSL 指标校验含可达未定义名称

- 类别：代码正确性、DSL。
- 位置：`app/core/dsl/validator.py:14-18,186-188`。
- 证据：代码引用未导入的 `INDICATOR_METADATA`；Ruff 报 `F821`。校验 `pe_ttm` 等内建指标会走该分支并抛 `NameError`，上层变为一般校验错误而非可靠 DSL 生命周期。
- 影响：核心 DSL 功能对合法内建指标不稳定，错误诊断不真实。
- 修复：显式导入 `INDICATOR_METADATA` 并增加内建指标正/负向生命周期测试。
- 复验：`ruff check` 零 F821，Web/CLI 均能校验并发布合法内建指标表达式。

### P1-13：CLI 筛选辅助函数在门禁失败时引用未定义名称

- 类别：代码正确性、CLI。
- 位置：`app/cli/main.py:59-71`。
- 证据：`_screening_engine()` 的未就绪分支调用未导入的 `make_response`；Ruff 报 `F821`。该 helper 当前未被主要命令调用，但作为公开模块内辅助路径不可运行。
- 影响：后续复用或直接调用时，数据门禁错误被 `NameError` 覆盖。
- 修复：在函数作用域导入 `make_response` 或删除未使用 helper；以 CLI/单元测试覆盖门禁失败输出。
- 复验：静态检查通过，未就绪 profile 返回稳定 JSON 协议而非 traceback。

### P1-14：规则并发保存可产生 500

- 类别：API 并发、数据完整性。
- 位置：`app/web/api/screening.py:449-472`；唯一约束 `app/core/storage/schema.py:406-415`。
- 证据：事务内读取 `MAX(version)` 后插入；两个并发请求可取得相同 next version，其中一个触发 SQLite unique/lock 异常且未转为可预期响应。
- 影响：多标签页协作或重试时丢失规则保存且 API 返回 500。
- 修复：原子版本分配，或针对唯一冲突重试并返回明确 409。
- 复验：并发 POST 同名规则，断言产生两个不同版本或一个明确冲突，不能出现 500。

### P1-15：raw 行情未纳入交易日历完整性验证

- 类别：数据正确性、指标计算。
- 位置：`app/core/data_quality.py:38-88`；`app/core/indicators/calculator.py:304-312,807-909`。
- 证据：readiness 的日历双向校验只查询 `price_daily_qfq`；但最新价、均线、成交量和换手率读取 `price_daily_raw`。raw 表可包含周末、未来日期或内部断档，仍进入技术指标。
- 影响：单个 raw 异常收盘价或成交量可改变 MA、平均量、换手率、最新价和筛选结果，而 QFQ 门禁不会阻止它。
- 修复：对 raw 与 QFQ 同时验证日期属于交易日历、完整覆盖声明窗口且没有内部缺口；违反时禁止发布快照和筛选。
- 复验：插入 raw-only 日历外日期或内部缺口，断言 readiness/snapshot fail-closed，技术指标不发布。

### P1-16：归母权益为零时被错误替换为总权益

- 类别：计算正确性。
- 位置：`app/core/indicators/calculator.py:507,594,600`；`app/web/api/stock_detail.py:418,630`。
- 证据：`a or b` 将合法数值 0 当作缺失。`total_equity_parent=0`、总权益为正时，PB、ROE、ROIC 和详情趋势改用了总权益。
- 影响：零归母权益公司展示出貌似合理但口径错误的估值和盈利指标。
- 修复：仅在 `is None` 时回退；零分母返回 NULL/未定义，不能换成另一口径。
- 复验：构造归母权益为零、总权益为正的 fixture，断言指标为 NULL 且详情不回退。

### P1-17：DSL 输入没有资源复杂度上限

- 类别：安全、可用性。
- 位置：`app/web/api/dsl.py:15-19,48-61`；`app/core/dsl/parser.py:25-30,202-217`。
- 证据：表达式没有最大字节数、token 数、嵌套深度或函数参数数量限制，解析器使用 Earley。恶意或异常超长表达式可在保存/校验时造成不成比例的 CPU 与内存占用。
- 影响：本地恶意页面或损坏客户端输入可使单进程服务无法响应。
- 修复：在持久化前限制 UTF-8 字节、token、AST 深度、节点数与函数 arity；在预算外直接拒绝。
- 复验：提交超长、深度嵌套和高歧义表达式，断言快速 400 且健康检查持续可用。

### P1-18：每次筛选门禁全量读取并哈希所有 archive BLOB

- 类别：性能、可用性、数据治理。
- 位置：`app/core/data_quality.py:319-383`；`app/core/storage/duckdb_store.py:85-90`。
- 证据：`build_data_quality_status()` 在请求链路执行 `SELECT raw_response_hash, payload FROM raw_response_archive`，`read_query()` 再把全部结果物化到 Python list 并逐个 SHA-256。筛选运行、保存、导出和加自选均调用该门禁。
- 影响：archive 增长时，任意用户操作会线性占用内存和 CPU，最终可造成卡顿、超时或进程终止。
- 修复：在导入时校验并持久化完整性状态；后台/增量重验，门禁只读取已计算状态和变更代际。
- 复验：以生产级 archive 数量和字节数 benchmark，确认请求路径不读取全 BLOB 且在 SLA 内完成。

### P1-19：恢复缺失 PDF 树时保留当前世代 PDF

- 类别：备份恢复、证据一致性。
- 位置：`app/core/backup/manager.py:654-660,793-800,842-859`。
- 证据：`_restore_pdf_tree()` 对不存在 source 直接返回；restore 仅在备份含 PDF 目录时才替换 target。备份没有 PDF、当前 profile 有 PDF 时，数据库回到旧世代但较新的 PDF 继续存在。
- 影响：恢复结果不是单一备份世代，可能向用户提供与恢复 manifest 不一致的证据文件。
- 修复：将缺失的备份 PDF tree 解释为空 tree，恢复时明确删除 live target；同样纳入 crash rollback journal。
- 复验：从不含 PDF 的备份恢复到含新 PDF 的 profile，断言 hot/cold 文件树和 manifest 都与备份完全一致。

### P1-20：静态资源同步会在替代文件确认前破坏线上目录

- 类别：发布可靠性、前端可用性。
- 位置：`frontend/scripts/sync-static.mjs:42-54`；`app/web/main.py:257-265`。
- 证据：脚本验证 staging 后直接删除 `app/web/static`，再复制新 bundle。复制失败或进程中断时没有恢复旧 tree；服务会返回 200 的“前端尚未构建”页。
- 影响：构建机器的磁盘、杀毒或文件锁故障可把最后一个可用 UI 变成空/部分目录，并被 HTTP 200 掩盖。
- 修复：保留旧目录直至新目录复制、校验和原子切换成功；失败时回滚旧 manifest/tree。
- 复验：在删除后复制前注入失败，断言此前 served manifest 仍完整可访问。

## P2

### P2-1：快照新鲜度在 UTC/本地日期边界偏移一天

- 类别：时间语义、数据呈现。
- 位置：`app/web/api/stock_detail.py:122-145`；失败测试 `tests/regression/test_stock_freshness.py:8-20`。
- 证据：使用本地 `date.today()` 与 UTC `calculated_at.date()` 比较；本轮隔离回归实际得到 15 天而测试期望 14 天。
- 影响：接近七天阈值时可能错误显示 stale/non-stale。
- 修复：使用单一 UTC 时钟和 timezone-aware timestamp，或显式定义中国业务日并统一转换。
- 复验：冻结 UTC 与本地跨日时刻，年龄和 warning 稳定一致。

## 已验证但不构成 PASS 的控制

- Web 绑定 loopback，写 API 需同源与每进程 token。
- 核心筛选字段、排序和 SQL 标识符存在白名单/参数化路径；本轮未确认新的 SQL 注入路径。
- 前端可编译、构建和既有 46 项 Node 合约测试通过。
- 当前服务端 readiness 会阻断多数当前数据缺口，但 P1-2 证明 lineage 状态仍可绕过服务端筛选阻断。

## PASS 前置条件

1. 关闭 P0-1，重建并独立抽样验收所有 SSE 股本及快照市值。
2. 关闭 P1-1 至 P1-5 的正式数据、来源链路、CSV 与恢复可靠性问题。
3. 关闭 P1-6 至 P1-20 的事件循环、并发、错误真实性、UI 失真、计算、资源与发布可靠性问题。
4. 为每个 P0/P1 补充对应负向或中断回归；全量 S1 必须零失败，Ruff 必须无 F821。
5. 以新的正式数据 hash、全量 readiness、来源覆盖、样本外部真值和真实主机性能证据重新申请验收。

在以上条件均满足前，结论保持 **BLOCK**。
