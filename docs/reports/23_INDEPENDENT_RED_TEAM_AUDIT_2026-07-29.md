---
title: 独立红队上线审查（2026-07-29）
status: superseded
category: reports
last-reviewed: 2026-07-30
superseded-by: reports/24_INDEPENDENT_RED_TEAM_AUDIT_2026-07-30.md
---

# 独立红队上线审查（2026-07-29）

## 裁决

**FAIL / BLOCK。不得开始作为价值研究或筛选工具使用，不得发布。**

本报告以当前磁盘工作树、当前正式库和 `docs/01_PRODUCT_REQUIREMENTS_V1.md` 为对象。历史审查报告和修复声明只作为待验证假设；本报告的结论来自独立静态审查、隔离运行验证和正式库只读核验。

隔离回归 `208 passed` 只能证明被覆盖的测试在临时数据库中通过，不能证明正式数据、计算口径、PRD 功能或发行包正确。当前存在多个独立 P0：任何一个都足以否决上线。

## 审查证据

| 检查 | 结果 | 结论 |
|---|---|---|
| 隔离 Python 回归 | `208 passed`，S1 wrapper 的正式五文件前后哈希无差异 | 测试隔离有效；不能外推为业务正确性 |
| 前端 lint | 通过 | 仅静态风格门禁 |
| 临时目录 Vite 生产构建 | 通过，18 个产物约 1.09 MB | 当前源可编译 |
| 前端 Node 合约测试 | `45 passed, 1 failed` | 前端测试门禁为红 |
| Python wheel | 失败：`BackendUnavailable: Cannot import 'setuptools.backends._legacy'` | Python 包不可发布 |
| 生产依赖审计 | `npm audit --omit=dev`: 0 vulnerabilities | 不抵消功能/数据缺陷 |
| 全量 npm 审计 | 5 high，均在 ESLint 构建工具链 | 发布卫生问题，非浏览器运行时漏洞 |
| 工作树/静态文件 | 1,567 个未提交条目；`app/web/static` 58 tracked、280 untracked | 发行对象不可重现 |
| 正式库只读核验 | 主 DuckDB/SQLite 前后 SHA-256 相同 | 审查未改写正式库；数据质量见下文 |

## P0 阻断项

### P0-1: 就绪门禁将不可研究的数据标记为 `healthy`

PRD §6.7 要求每只股票近 5 年 raw/qfq 日线或上市以来日线、当前价格和完整最小财务集。实现只检查每只股票是否存在任意一条非空 raw/qfq 收盘价，并且北交所直接豁免 qfq；没有逐股价格新鲜度、5 年覆盖或统一 as-of 日期检查：`app/core/data_quality.py:19-44`、`app/core/data_quality.py:65-70`。筛选 API 以此作为放行依据：`app/web/api/screening.py:21-30`。

正式库独立只读结果：

- 5,534 个当前上市股票只有 6 个快照的 `latest_price_date >= 2026-07-28`；上交所 2/2,310、深交所 0/2,892、北交所 4/332。
- raw 为 17,230,799 行/5,547 个代码，qfq 为 16,890,318 行/5,202 个代码；raw-only 日期键 349,901，qfq-only 9,420；332 个北交所股票没有 qfq。
- `data diagnose` 仍返回 `healthy=true`，因为它只使用聚合覆盖数和全局最大日期。

这是对“当前筛选”核心前提的错误放行，违反 PRD §§6.4、6.7、7.2、20.1、20.4。

### P0-2: 当前生产数据的来源、分红和架构完整性不合格

正式库只读核验发现：

- 53,877 条活动分红的 `announcement_date` 全部为 NULL。当前告警只识别“无公告日且日期恰为 06-30/12-31”的子集：`app/core/data_quality.py:77-89`，因此可出现分红来源全失效却不报 `DIVIDEND_DATES_UNVERIFIED` 的假绿。
- 最新 2026-06-30 三表各仅 20 行，只有 19 个上市股票具备完整三表；5,485 个快照仍是 2026-03-31，30 个更早。`indicator_snapshot.latest_price_date` 范围从 2005-08-31 到 2026-07-29。
- 100,544 条 raw 日线没有精确 `stock_meta` 父记录，均为可映射到六位代码的 13 个未补零别名。
- `schema_migrations` 声称 v1-v9，实库 `missing_list` 缺 `resolved_at`、`retry_list` 缺 `next_retry_at`/`max_retries`，与当前 schema 代码不一致。
- 133 条 `fetch_batch` 没有原始响应归档，132 条 batch ID 重复；仅有 8 条 `raw_response_archive`。

这使“当前数据”“来源可追溯”“失败可见”三个产品承诺不可成立，违反 PRD §§2.3、7.4、9.1-9.4、14、15。

### P0-3: 数值计算存在可确认的错误口径

1. ROE/ROA 将报告期前的最近一条资产负债表作为期初余额：`app/core/indicators/calculator.py:574-605`。年度 2025-12-31 常会配对 2025-09-30，而不是 2024-12-31；季度 TTM 也不能保证同比期初资产。结果可被明显高估或低估。
2. 区间收益、年化波动和最大回撤直接使用未复权 `price_daily_raw`：`app/core/indicators/calculator.py:784-858`。除权、送配和拆并股会被错误计算为价格跳空、回撤和波动，尽管项目已存 qfq 表。
3. 技术指标把 NULL 行过滤后压缩时间轴：`app/core/indicators/calculator.py:800-841`。分散于长时间缺口的 20 个观测会被当作连续交易日。
4. 股息率和分红率依赖 `CURRENT_DATE`/当前日历年，可能将最近年度 DPS 与当前 TTM EPS 混合：`app/core/indicators/calculator.py:542-555`、`app/core/indicators/calculator.py:753-776`。同一历史数据在不同重算日期会给出不同值。

这些不是展示缺陷，而是会改变选股集合和研究结论的计算错误，违反 PRD §§8.1、8.3、10、14。

### P0-4: 受控 DSL 存在 SQL 注入边界缺失，且承诺的语义不可执行

已发布 DSL 名称没有标识符校验，Web API 接受任意 `name`：`app/web/api/dsl.py:15-60`，注册表也不校验：`app/core/dsl/registry.py:49-84`。筛选器把它原样拼接为 SQL 别名：`app/core/screening/engine.py:304-308`。参数化数值无法保护 SQL 标识符；恶意名称可改变 CTE/select SQL 结构。

另有功能性失败：

- 布尔 `AND`/`OR` 与多参数函数的 parser/codegen 路径不正确，筛选编译器又直接拒绝任何多参数函数，导致 PRD 要求的 `CAGR`、rolling、lag 等 DSL 能力不可用：`app/core/dsl/parser.py:128-177`、`app/core/dsl/codegen.py:146-157`、`app/core/screening/engine.py:631-655`。
- 筛选严格模式没有把 `right_field` 加入 rank/provenance 校验：`app/core/screening/engine.py:177-190`、`530-541`、`676-690`。一条 `strict_only` 字段比较可由未严格审计的右侧字段决定。
- 快照与三表各自独立取“最新”，可把旧快照与新财务期混合：`app/core/screening/engine.py:280-300`。

违反 PRD §§9.1、11、12.2、16.4。由于 DSL 是核心写操作和筛选能力，不能通过限制 UI 来接受。

### P0-5: PRD 的必需数据能力尚未实现

- 初始化和历史回填没有抓取/落库 `xdxr` 公司行为；只抓价格和分红：`app/core/init.py:96-142`、`app/core/backfill.py:101-110`。schema 存在不等于能力存在。违反 PRD §6.4。
- 银行资本充足率/不良贷款率/拨备覆盖率与券商风险覆盖率没有 storage、DSL field catalog、ingestion 或测试实现。违反 PRD §§6.6、10、11.1。
- 没有 5,000+ 股票、20 条条件、一个复合指标、行业排名、预热后连续 10 次中 9 次小于 5 秒的验收夹具或结果。违反 PRD §§12.6、19.1、20.1。

### P0-6: 发行与一键启动不可复现、不可交付

- `pyproject.toml:41-43` 指向不存在的 `setuptools.backends._legacy:_Backend`。实际 `python -m pip wheel --no-deps --no-build-isolation .` 已失败。
- `value-dashboard.spec:11-29` 打包静态资源、配置和 grammar，但不打包正式数据对或 `start.bat`/`vd.bat`。`start.bat:23-51` 又要求发行目录有完整 DuckDB+SQLite；因此 PyInstaller 产物不能一键使用。
- 当前 `app/web/static/index.html` 引用的 `index-CDSDZpIm.js` 未被 Git 跟踪；`app/web/static` 仅 58 个 tracked、280 个 untracked 文件。临时干净 Vite build 只输出 18 个文件，当前服务目录 337 个文件约 20.19 MB。Git clean checkout 无法复现当前 Web UI。

违反 PRD §19.1 和 §20.4 的一键启动验收。

### P0-7: 本地边界和危险操作控制不足

- frozen EXE 优先信任当前工作目录的 `config/default.yaml`：`app/core/config.py:15-32`。该配置可将 host 改为 `0.0.0.0`，而 Web 的 DSL、筛选、自选写 API 没有认证，违背“仅 localhost”硬约束（PRD §3.1）。
- 危险 plan 在 confirm 时即标记 `executed`，执行只读取状态，没有原子 consume；同一确认可并发重复执行：`app/cli/protocol.py:147-231`。违反 PRD §16.3 的安全语义。
- CNINFO 公告和 PDF 使用明文 HTTP，却将结果作为严格来源：`app/core/adapters/cninfo_adapter.py:41-49`、`app/core/pdf/manager.py:24-28`。中间人可篡改财务公告和 PDF。
- PDF archive 将用户 `target_dir` 直接与 data root 拼接：`app/core/pdf/manager.py:186-234`。绝对路径或 `..` 可逃逸受控目录，随后原 PDF 被删除。

## P1 重要缺陷

- 备份恢复只校验与 registry 路径精确匹配的备份；复制后的备份不验证完整性：`app/core/backup/manager.py:489-498`。且没有服务停机/跨进程排他锁，恢复可覆盖同时发生的 Web 写入：`app/core/backup/manager.py:608-666`。
- 启动在 Uvicorn bind 前执行全股票公告检查，CNINFO 1.5 秒限流使 5,000 股至少约 2 小时后才可能可用：`app/web/main.py:215-244`、`app/core/update.py:204-245`、`config/default.yaml:14-29`。
- 健康检查永远 200，数据库异常也返回成功载荷：`app/web/main.py:80-128`。
- CLI 创建的复合指标锁为空，随后筛选无法识别该字段；部分 CLI 输出不遵循统一 JSON schema，CSV 也缺必要溯源。违反 PRD §§12.5、16、20.3。
- 冷归档/verify/clean 未建立包含 qfq、分红、xdxr、raw response、PDF 的可校验归档清单，也未要求成功 verify 记录才允许清理。违反 PRD §18。
- 客户端质量门禁只在页面挂载时获取，保存/导出可使用陈旧状态，直接 API 可绕过：`frontend/src/views/ScreeningPage.vue:249-277`、`app/web/api/screening.py:67-276`。
- 筛选结果没有跳转个股详情入口：`frontend/src/components/ScreeningResultsPanel.vue:309-317`，故 PRD §20.2 第 1 步失败。
- 个股“时间维度”控件只发 event，父组件未消费，趋势数据从不改变：`frontend/src/components/IndicatorTabs.vue:23-43`、`frontend/src/views/StockDetailPage.vue:299-307`。
- 已有的 API 溯源字段（生效日、版本、公式、重述差异）被前端类型和 UI 丢弃；数据状态页不显示归档/公司行动摘要。违反 PRD §§14、15。

## 前端与可访问性

功能代码可编译，但设计和可访问性未完成：

- 前端测试自身失败：`frontend/tests/data-quality.test.ts:20-33` 断言 6 个告警，源代码定义 7 个：`frontend/src/types/data-quality.ts:22-53`。
- 根组件没有 skip link、`main` 或页面 `h1`：`frontend/src/App.vue:29-43`。
- 多个 form control 无可关联 label，筛选结果以最多 5,000 行完整数组交给 data table 而未虚拟化：`frontend/src/components/ScreeningResultsPanel.vue:295-317`。
- 详情页自定义趋势只有八个硬编码字段，未调用后端 custom-trend API；固定 4 列和固定宽度趋势图存在窄屏溢出风险。虽然 PRD 不要求移动端，桌面缩放和无障碍仍未通过验证。

## PRD 四条流程裁决

| 流程 | 裁决 | 直接原因 |
|---|---|---|
| 20.1 当前筛选 | **FAIL** | 最小数据门禁假绿；技术/财务计算错误；DSL 注入及多参数/布尔表达式失效；无 5 秒验收 |
| 20.2 单股研究 | **FAIL** | 不能从筛选结果进入详情；时间维度控件无效；溯源字段缺失；价格与指标日期不一致 |
| 20.3 CLI 与 OpenCode | **FAIL** | CLI composite lock/protocol 不完整；plan 可重放；DSL 无安全标识符边界 |
| 20.4 初始化、更新、修复、归档、备份、恢复 | **FAIL** | 缺 xdxr；启动阻塞；归档/恢复完整性与排他性不足；发行包缺运行数据；正式库 schema/data 不一致 |

## 最低整改门槛

重新申请 PASS 前，至少需要全部完成并提供可重复证据：

1. 修复并测试所有 P0 数值、DSL identifier、严格溯源和同一 as-of 期选择问题。
2. 重写 readiness/diagnose 为逐股检查：5 年 raw/qfq（未满 5 年则上市以来）、每股最新价、新鲜度阈值、三表同报告期、qfq/company-action、分红公告来源和 schema 兼容性；任一硬项失败必须阻断筛选。
3. 重建或隔离正式数据：规范代码、补齐/明确缺失 qfq、真实公司行为、分红实施公告、原始材料与唯一 batch lineage；以抽样和全量 SQL 证明。
4. 完成 PRD §6.4、§6.6、§11.2、§18、§19.1 的未实现能力，并为负向路径添加回归测试。
5. 限制配置来源和监听地址，HTTPS 获取权威材料，修复 archive path policy，令 plan 原子一次性消费，恢复期间建立跨进程维护锁。
6. 修复 Python build backend、把发行数据与启动器纳入可追溯发行工件、清理并跟踪当前静态产物，建立从 clean checkout 到 EXE smoke 的 CI 门禁。
7. 修复失败的前端测试，完成 PRD 四流程的真实浏览器/API/CLI 验收与性能夹具；不是 mock-only 测试。

## 担保结论

无法诚实地对任何复杂软件给出数学意义的“绝对正确”担保。但本次 **FAIL** 不依赖推测：其基础是当前源代码中的直接路径、当前正式库的只读 SQL 结果、实际失败的前端测试和实际失败的 wheel 构建。若以“林纳斯式”标准下注，我会担保的结论是：**当前项目不通过审查，开始使用会把错误、过期或不可溯源的数据伪装为可研究结果。**

## P0 整改复验（2026-07-29）

本节记录本报告后实际实施的整改，替代上文关于“代码尚未修复”的历史状态；它**不**替代上文正式数据与 PRD 结论。

### 已完成的代码闭环

| 原 P0 | 当前代码状态 | 关键整改 |
|---|---|---|
| P0-1/P0-2 | 已闭环，正式数据仍 BLOCK | 就绪度改为逐股 raw/qfq 历史、成交量、价格新鲜度、同报告期三表、快照价格一致性检查；状态页检查活动分红公告日、code alias、lineage/raw archive 和 live schema。服务端持久化筛选重新检查门禁。 |
| P0-3 | 已闭环 | ROE/ROA 只使用同比同报告期初值；收益、波动、回撤使用 QFQ；QFQ 缺行、NULL 行或交易日历断档均 fail-closed；分红计算显式使用快照 as-of 日期。 |
| P0-4 | 已闭环 | DSL 名称使用受限标识符并安全引用；布尔/多参数解析修复；历史 planner 未实现的函数在校验/发布前拒绝；严格模式涵盖 `right_field` 且按报告期验证；筛选拒绝混合 snapshot/statement 时点。 |
| P0-5 | 部分闭环 | `xdxr` 已进入初始化、回填、重抓、失败记录及原始材料归档；银行/券商强制字段已有 schema、映射、DSL 与筛选执行支持；新增 PRD 19.1 测量脚本。目标主机热数据夹具性能签署仍未完成。 |
| P0-6 | 已闭环，待提交后 clean-checkout 签署 | 修复 PEP 517 backend；wheel 包含 DSL grammar、包内默认配置与静态资源；静态同步改为替换并哈希验证；clean release builder 生成 EXE、静态资源、配置和启动器，明确不包含正式数据库。 |
| P0-7 | 已闭环 | frozen 配置不再信任 CWD 且强制 loopback；危险 plan 原子一次性消费；CNINFO/PDF 改 HTTPS；PDF 与通用归档/清理均限于配置根、校验 manifest/verify 记录。 |

### 本轮验证

- 全量 Python 隔离回归：`247 passed`，`571 warnings`；S1 前后及 cleanup 后正式文件集无 delta。
- 前端：`npm run lint`、`npm run build` 通过；Node 合约 `46 passed, 0 failed`。
- 打包：PEP 517 wheel 通过并由回归检查包含 `grammar.lark`、`app/resources/config/default.yaml`、静态入口和 assets；临时 release build 通过，含 EXE/静态资源/config/两个 launcher，不含正式 DuckDB/SQLite。
- 正式只读诊断：返回 `healthy: false`，证明新门禁不再假绿。审计后 DuckDB SHA-256 为 `BB7A18C94BE4D5FB8E984CAF6E01407F61A615B7050CFC55550478BD58062B42`，SQLite 为 `E06CB9AB295E676A4B2E1E34D5944E658E5562BE0E279E19F6588E926AE6DB1A`，均未改变。

### 仍然阻断上线的正式数据与验收项

当前正式诊断列出：raw 历史不足 182 只、qfq 历史不足 112 只、价格新鲜度不足 5,528 只、有效成交量不足 206 只、快照价格不一致 7 只；53,503 条活动分红缺公告日；133 个 batch 缺原始归档。`MINIMUM_DATA_NOT_READY`、`DIVIDEND_DATES_UNVERIFIED` 和 `LINEAGE_INVALID` 因此仍是正确的阻断状态。

此外，PRD §19.1 仍需在用户目标主机、真实 5,000+ 热数据夹具、20 条条件/复合指标/行业排名下完成预热后 10 次测量并达到至少 9 次小于 5 秒。未完成这些正式数据维护和性能签署前，总体裁决继续为 **FAIL / BLOCK**。

## 整改后独立复审（2026-07-29）

### 裁决

**FAIL / BLOCK 不变。** 上一节的“代码闭环”被重新审查后只能改述为“部分修复已生效”。全量测试继续通过不能推翻以下实现缺陷，因为现有测试没有覆盖这些攻击路径。

### 新确认 P0

1. **CLI 绕过最小数据门禁。** `app/cli/main.py:480-528` 的 `screening run` 直接运行并把结果写入 `screening_runs`，没有调用 `minimum_data_readiness()`；随后 `screening save_result` 可持久化。Web 入口会检查而 CLI 不会，违反 PRD §§6.7、12、16、20.3。
2. **非 frozen 的标准 Python Web 入口可被配置为 LAN 写服务。** `app/core/config.py:84-87` 允许 `config/user.yaml` 覆盖 host，`app/web/main.py:29-31` 的 loopback 强制仅适用于 frozen。`app/web/api/dsl.py:48-106`、`app/web/api/watchlist.py:113-153` 等无认证写 API 因而可在 `0.0.0.0` 上暴露，违反 PRD §3.1 的 localhost 硬约束。
3. **就绪度仍未覆盖 PRD 最小核心集和必需公司行为。** `app/core/data_quality.py:45-96` 只要求 6 个通用三表字段；不检查 `xdxr`、分红、全部内建指标输入、股本、适用银行/券商字段，或缺失值的原因码/置信度。`app/web/api/screening.py:21-30` 仅依赖该不完整门禁。一个缺失全部公司行为和行业强制字段的银行可被标记为可筛选，违反 PRD §§6.4、6.6、6.7、9。
4. **财务重述可不重算快照而继续通过门禁。** `app/core/update.py:144-174` 只在价格步骤成功且有价格更新时重建指标；若价格已最新，`_update_prices_incremental()` 在 `:329-334` 返回 skipped，即使公告驱动的财务刷新成功，旧快照仍被筛选。`app/core/data_quality.py:83-96` 不验证快照的输入来源版本/抓取时间，违反 PRD §§7.4、8.1、8.3。
5. **空 AKShare 原始响应仍被伪装成有效溯源。** `app/core/adapters/akshare_adapter.py:433-437,500-504` 不传 raw response；`app/core/adapters/base.py:157-197` 对空内容计算固定 `<empty>` hash；`app/core/init.py:985-1018` 会归档空 payload；质量检查只验证 hash 长度和 archive 行存在性（`app/core/data_quality.py:166-187`）。这不能满足 PRD §§9.4、14 的原始来源保留/可核验要求。

### 新确认 P1

- **财务时序 DSL 仍会算错。** `app/core/dsl/codegen.py:186-200` 将累计报表字段的 `MRQ` 当作原累计值，`YoY` 也对累计值同比；Q2/Q3 流量字段会得出错误季度指标。
- **TTM 与增长可跨错年/缺年计算。** `app/core/indicators/calculator.py:395-415,663-708,931-939` 未强制上一年度年报或连续年度序列。
- **严格筛选不比对审计值。** `app/core/screening/engine.py:703-706` 仅匹配股票、字段、报告期和 confidence；同期间不同数值仍可被旧 strict 审计记录认证。
- **导出溯源不绑定保存结果报告期。** `app/web/api/screening.py:317-325` 始终导出字段最新审计行，而不是所保存结果的报告期。
- **已发布 DSL 依赖缓存只以名称为键。** `app/core/screening/engine.py:590-606` 可在同次筛选中把不同锁定版本错误编译成先遇到的版本。
- **更新与重试链不完整。** 增量更新不更新 dividends/xdxr（`app/core/update.py:144-165`），除 price 外 retry 永久标为 unsupported（`:474-481`），且 `retry_list` 没有逻辑唯一约束却使用 `INSERT OR REPLACE`（`:714-732`）。
- **旧 SQLite migration 漂移未修复。** `app/core/storage/schema.py:653-711` 对旧 v1 库不补 `retry_list.max_retries`、`next_retry_at` 与 `missing_list.resolved_at`，但 schema version 仍可继续登记。
- **备份/恢复/归档仍不完整。** 全量备份不包含冷归档 PDF 与 `pdf_archive_manifest`（`app/core/backup/manager.py:238-243,400-410`）；复制的备份不做完整性验证（`:489-498`）；恢复缺跨进程维护锁（`:591-666`）；archive verify 后新增数据仍可能在 cleanup 时被删除（`app/core/archive.py:70-109`）。
- **前端可绕过质量策略。** `frontend/src/helpers/screening-quality.ts:70-91` 仅客户端阻断 warning，后端 save/export/watchlist 只检查最小就绪度（`app/web/api/screening.py:155-276`）。
- **PRD 详情溯源和冷归档体验不符合。** `frontend/src/types/stock-detail.ts:144-157` 丢弃 API 的生效日、版本、公式和重述差异；冷归档 PDF 只打开 JSON 指引而不展示位置/校验和（`app/web/api/stock_detail.py:675-683`、`frontend/src/components/DataTraceability.vue:111-119`）。

### 复验证据

- 隔离回归：`247 passed`，`571 warnings`；S1 wrapper 未报告正式文件 delta。
- 前端：`npm run lint` 通过，Node 合约 `46 passed, 0 failed`。
- 正式只读 `data diagnose`：仍为 `healthy:false`，报告 raw 历史不足 182、qfq 历史不足 112、价格新鲜度不足 5,528、有效成交量不足 206、快照价格不一致 7、活动分红无公告日 53,503、batch archive 缺口 133。
- 本次复审后正式 DuckDB/SQLite SHA-256 仍为 `BB7A18C94BE4D5FB8E984CAF6E01407F61A615B7050CFC55550478BD58062B42` 与 `E06CB9AB295E676A4B2E1E34D5944E658E5562BE0E279E19F6588E926AE6DB1A`。

### 结论修正

“P0-1/2、P0-3/4、P0-6/7 已闭环”这一历史表述不成立，应改为“部分攻击路径已修复，但仍存在上述 P0/P1 绕过或错误语义”。项目不得以当前 247 项测试全绿、前端构建成功或诊断已返回 false 作为上线理由。

## 再次独立红队复审（2026-07-29）

### 裁决

**FAIL / BLOCK 不变。P0 和 P1 均未全部修复。**

本次复审不采信此前“代码闭环”结论，也不以既有回归全绿代替攻击路径验证。审查对象为当前未提交工作树。执行了独立静态复核、隔离回归、前端构建与 wheel 构建；未改写正式 DuckDB/SQLite。

### 本轮证据

| 检查 | 结果 | 结论 |
|---|---|---|
| 隔离 P0/P1 定向回归 | `54 passed, 1 failed` | `test_xdxr_lifecycle_persists_corporate_actions` 因测试 fixture 的 payload hash 与字节不匹配而失败；此处证明来源 hash 校验已生效，但不是完整能力签署。 |
| 已有扩展定向回归 | `73 passed`、`79 passed` | 覆盖部分已修复路径，未覆盖本节列出的 CLI、BSE、恢复并发和前端流程攻击。 |
| 前端 lint/build | 通过 | 仅证明可编译，不证明筛选到详情、时间控件或服务端质量策略成立。 |
| PEP 517 wheel | 通过 | wheel 可构建；不抵消发行工件、启动数据和 clean-checkout 问题。 |
| 工作树 | 大量已修改及未跟踪文件，静态目录包含多代 assets | 当前树不能作为可复现发行证据。 |

### P0 再判定

| 项目 | 判定 | 当前证据 |
|---|---|---|
| P0-1 最小数据门禁 | 部分修复 | `app/core/data_quality.py:67-95` 已逐股检查 raw、成交量、三表、股本、分红与公司行为，但 `:73-82` 对 BSE 无条件豁免 QFQ 历史及新鲜度；不含任何 QFQ 的 BSE 股票可越过这两个硬项。 |
| P0-2 来源、分红、架构完整性 | 部分修复 | 活动分红公告日、archive gap、空 archive payload、别名及 live schema 已被诊断，见 `data_quality.py:186-275`。但 `akshare_adapter.py:512-551` 的 dividends 与 trading_dates 成功响应未提供原始 payload；`backfill.py` 仍可将空材料写为归档行后由质量检查事后发现。正式数据的既有阻断结论也没有新的全量修复证据。 |
| P0-3 数值口径 | 部分修复 | 同比期初资产、QFQ 收益/波动/回撤及 as-of 分红路径已修复并有回归。`calculator.py:1013-1053` 在交易日历不存在时返回 `None` 并跳过日期连续性检查，分散日期仍可被压缩成连续序列计算技术指标。 |
| P0-4 DSL 注入与筛选语义 | 已修复 | 名称和 SQL identifier 验证、布尔/多参数解析、严格审计值比对、锁定版本缓存与同一报告期选择均在当前源存在；`test_p0_3_p0_4_regressions.py` 相关 8 项通过。 |
| P0-5 必需能力与性能 | 部分修复 | XDXR、银行/券商字段、DSL catalog 和重抓路径已实现。`scripts/screening_performance_acceptance.py` 仍为未签署测量工具，缺少目标主机上真实 5,000+ 热数据、20 条条件、复合指标及行业排名的 10 次验收记录。 |
| P0-6 发行与一键启动 | 部分修复 | wheel、静态同步和 release builder 已改善；但 builder 明确不携带 DuckDB/SQLite，而 `start.bat` 需要预先存在的正式 profile、确认变量和完整数据库。加之静态 assets 未被完整跟踪，尚无从 clean checkout 到可用 EXE 的可重现签署。 |
| P0-7 本地边界与危险操作 | 已修复 | `config.py:56-60` 和 `web/main.py:29-36` 均限制 loopback；危险 plan 原子消费、HTTPS 和 archive path policy 的定向安全回归通过。 |
| 新 P0-1 CLI 绕过最小门禁 | **未修复** | `app/cli/main.py:488-538` 的 `screening run` 未调用 `minimum_data_readiness()` 或同等质量门禁，直接执行并写入 `screening_runs`。`screening save_result` 虽在 `:550-559` 复核，却无法阻止不完整数据被 CLI 运行、展示或后续在数据变化后持久化。 |
| 新 P0-2 非 frozen LAN 暴露 | 已修复 | `Config` 对所有运行形态拒绝非 loopback host，Web 启动前再次校验。 |
| 新 P0-3 最小核心集和公司行为 | 部分修复 | 股本、XDXR、公告日分红、适用银行/券商字段和快照输入水位已加入门禁，见 `data_quality.py:87-119`；但通用财务核心仍只验证六项字段，不是全部内建指标输入，且 BSE QFQ 豁免仍存在。 |
| 新 P0-4 财务重述旧快照 | 已修复 | `update.py:144-177` 在财务刷新成功且价格 skipped 时仍触发指标重算；`data_quality.py:104-119` 拒绝晚于 snapshot 的核心字段审计记录。 |
| 新 P0-5 空 AKShare 来源材料 | 部分修复 | 价格和三表现在提供 payload，`DataInitializer._record_batch()` 会拒绝有数据而无材料或 hash 不匹配的写入。AKShare dividends/trading_dates 仍没有 payload，且 backfill 写边界未 fail-closed。 |

### 未消除的 P0 阻断

1. CLI `screening run` 可绕过最小就绪门禁并创建运行记录。
2. BSE 可以在缺少 QFQ 历史及 QFQ 新鲜度时获得 screenable 状态。
3. 无持久化交易日历时，技术指标仍可能跨真实交易缺口压缩时间轴。
4. 分红和交易日历的 AKShare 成功响应仍可产生不可验证的空原始材料归档。
5. PRD §19.1 性能签署、正式数据重建/全量核验、clean-checkout 一键交付均未完成。

### P1 再判定

| 项目 | 判定 | 当前证据 |
|---|---|---|
| 备份复制完整性、冷归档和恢复排他 | 未修复/部分修复 | `backup/manager.py:489-498` 只对 registry 同路径备份校验 zip checksum；复制后的 archive 可绕过。恢复 `:591-672` 无跨进程维护锁。冷归档 PDF 和 `pdf_archive_manifest` 未形成完整 restore 闭环。 |
| 启动公告扫描 | 已修复 | `web/main.py:246` 使用 `include_announcements=False`，避免 bind 前逐股远程扫描。 |
| health 假绿 | 未修复 | `/api/health` 在 `web/main.py:90-96` 固定 200；`/api/db/status` 已能 503，但 health 不能作为数据库 ready 信号。 |
| CLI composite/CSV 协议 | 部分修复 | CLI create 已解析 published DSL locks；CLI run 仍缺门禁。CLI CSV 路径没有与 Web 等价的字段溯源、锁版本和质量门禁闭环。 |
| 通用归档 verify/clean | 部分修复 | `archive.py:18-155` 已覆盖 qfq、dividends、xdxr、raw response 并阻止 verify 后热表变化清理；不覆盖冷 PDF 及其 SQLite manifest，且 create/verify 未使用跨存储一致快照。 |
| 服务端质量策略 | 部分修复 | Web save/export/watchlist 实时阻断 minimum readiness、分红公告日和 lineage；`screening.py:21-36` 未按结果字段处理 `SNAPSHOT_STALE`、`FINANCIAL_SHELL_ROWS` 等其他质量警告，直接 API 仍可绕过前端的字段级限制。 |
| 筛选结果跳转详情 | 未修复 | `ScreeningResultsPanel.vue:135-173,309-317` 没有 `/stock/:code` 路由入口或行操作。 |
| 个股时间维度控件 | 未修复 | `IndicatorTabs.vue:23-41` 仅 emit；`StockDetailPage.vue:300` 未监听 `update:timeDimension`，趋势请求不变。 |
| 详情溯源与冷归档体验 | 部分修复 | 生效日、版本、公式及冷归档 checksum/path 已进入类型或 UI；`stock_detail.py:454-463` 仍固定返回 `as_reported_value` 和 `latest_restated_diff` 为 NULL，且冷归档 UI 只提示 toast，未展示可执行恢复指令。 |
| 财务时序 DSL | 已修复 | 累计流量 MRQ/YoY/QoQ 改为单季差分，相关回归通过。 |
| TTM 与增长跨错年 | 已修复 | TTM 要求精确上一年年报，CAGR 要求连续年报。 |
| strict 审计值比对 | 已修复 | `screening/engine.py:696-712` 比较最新 strict audit value 与筛选源值。 |
| 保存结果报告期导出溯源 | **未修复** | `screening.py:318-341` 仍只以 `(stock_code, field_name)` 取最新 source_audit，没有将保存结果绑定到 report_date；保存后新增审计行会污染历史 CSV 的来源。 |
| published DSL 缓存 | 已修复 | 缓存 key 为 `(name, version, content_hash)`。 |
| update/retry 链与 SQLite migration | 已修复 | 公告更新调用 market actions，非价格 retry 使用 `refetch_one`，迁移修复 retry/missing 列与唯一索引；仍需并发及退避上限实测。 |

### 本轮结论

本轮不是“全部修复完成”的复验。P0 仍有直接绕过和 fail-open 数据路径，P1 仍有备份恢复完整性、溯源重现、质量策略和核心用户流程缺陷。即使后续补齐单元测试，也必须完成正式数据的重新维护、性能签署、干净发行构建和真实浏览器/API/CLI 验收，才可重新申请上线审查。

## Phase 16 代码复验（2026-07-29）

本节仅替代上文已由当前工作树修复的代码级证据；不改变正式数据、真实性能、clean-checkout 和发布签署仍为 `FAIL / BLOCK` 的结论。

| 项目 | 当前状态 | 可重复证据 |
|---|---|---|
| CLI/Web 筛选门禁与保存报告期 | 已修复 | CLI `screening run`、保存、CSV、加入自选与 Web 共用 `screening_readiness()`；运行记录保存 `_report_date`，CSV 按 `(stock_code, report_date, field_name)` 取审计记录。 |
| BSE QFQ、交易日历与来源材料 | 已修复 | BSE 不再豁免 QFQ；无持久交易日历拒绝 QFQ 技术统计；有结果行而缺 payload 或 SHA-256 不匹配会在 init/backfill 写入前失败。 |
| 备份、恢复与冷 PDF | 已修复 | ZIP 内逐文件 SHA-256 + 数据密钥 HMAC 清单认证，复制件亦验证；恢复维护锁跨进程阻断 DuckDB/SQLite 写入；备份/恢复包含冷 PDF 及 `pdf_archive_manifest`。 |
| 通用归档跨存储一致性 | 已修复 | v2 归档同时记录 DuckDB 公共表、SQLite 冷 PDF 清单与逐文件 PDF 哈希；PDF 或 SQLite 清单变化会使 verify/clean 失效。 |
| health、详情与状态 UI | 已修复 | `/api/health` 双库探针失败返回 503；筛选代码可进入 `/stock/:code`；时间维度触发趋势请求；状态页展示分红/XDXR，冷 PDF 显示恢复命令。 |

### 本轮验证

- 隔离全量回归：`258 passed`；S1 wrapper 完成。
- 前端：`npm run lint` 与 `npm run build` 通过。
- 打包：`python -m pip wheel --no-deps --no-build-isolation .` 通过。

### 仍然阻断上线

1. 正式数据仍需依据权威来源重建并全量核验；不能用隔离夹具替代。
2. PRD §19.1 仍需在目标主机、真实 5,000+ 热数据和完整条件集下取得 10 次性能签署。
3. 仍需 clean checkout 到 EXE/启动器/已准备正式 profile 的可重复发布演练及真实浏览器/API/CLI 四流程验收。

## 全项目独立红队复审（2026-07-29，Phase 16 后）

### 裁决

**FAIL / BLOCK。发现 4 项 P0 与 8 项 P1。**

本轮以当前工作树为对象，不采信 Phase 16 的“代码整改完成”作为结论。测试和构建通过只证明已有夹具覆盖的路径；以下问题来自对成功写入、进程中断、跨进程竞争、真实 SQLite 外键关系、浏览器来源边界和发行输入的独立复核。

### 运行证据

| 检查 | 结果 | 结论 |
|---|---|---|
| S1 隔离全量 Python 回归 | `258 passed`，`662 warnings` | 现有回归通过；未覆盖本节中断/竞争/真实外键图攻击路径。 |
| 前端 lint/build | 通过 | 前端能编译且静态同步成功；不是浏览器安全或产品流程签署。 |
| PEP 517 wheel | 通过 | 当前环境可构建 wheel；不证明干净环境可构建 EXE。 |
| 静态复核 | 完成 | 确认以下 P0/P1 代码路径。 |

### P0 阻断项

1. **QFQ 与分红成功回填可无可验证 lineage 地写入。** `app/core/init.py:462-522` 将 raw 与 QFQ 一起提交后，只为 raw 调用 `_record_batch()` / `_record_field_audit()`；`app/core/backfill.py:238-297` 也只记录 raw。`app/core/backfill.py:375-398` 成功写入 dividends 后完全不记录 batch、raw archive 或字段审计。`data_quality.py:228-255` 仅检查已有审计行是否坏，不要求已写价格/分红行有审计覆盖。因而来源不可恢复的 QFQ、分红和由其导出的指标可进入研究和筛选，违反 PRD §§6.4、9、14。现有回归只覆盖 raw 或失败路径。

2. **数据/快照与 lineage 仍分开提交，进程中断后可出现“健康但无来源”的研究数据。** `app/core/init.py:462-522`、`:614-622`、`:643-650`、`:671-678` 均先提交标准化数据，后另开事务写 batch/audit；`app/core/indicators/calculator.py:223-235` 先发布 `indicator_snapshot`，后再写派生 lineage。若在二者之间终止进程，数据永久存在而 lineage 缺失。`minimum_data_readiness()` 仅拒绝“晚于快照的审计行”，不拒绝审计行完全缺失（`data_quality.py:124-139`）。这会把不可证明来源的输入或快照放行给筛选。

3. **无持久交易日历时，技术指标仍可跨缺口压缩时间轴。** `calculator.py:1016-1034` 在 `trading_dates` 缺失或为空时返回 `None`；`:1036-1056` 随即跳过连续性判断。`minimum_data_readiness()` 不要求交易日历，价格覆盖仅凭最早/最新日期和 65% 行数（`data_quality.py:89-106`）。稀疏但满足计数门槛的 QFQ 数据可被当作连续行情计算收益、波动、回撤和均线，重复原审计 P0-3 的核心数值错误。

4. **仅 loopback 监听不能阻止浏览器 DNS rebinding 对无认证写 API 的访问。** `app/web/main.py:29-87` 只校验 bind host；未设置受信 Host、中间件 Origin 校验或每次启动的浏览器写入令牌。攻击者控制的页面可在 rebinding 后以同源请求调用 DSL、草稿和自选写接口，例如 `app/web/api/dsl.py:48-106`、`watchlist.py:113-153`。这违反 PRD §3.1 的仅本地个人研究边界；“不暴露 LAN”不是浏览器来源认证。

### P1 重要缺陷

1. **就绪度接受任意陈旧的完整财务期。** `data_quality.py:67-84,107` 仅要求存在任意一个三表共同完整期，不限制该期相对当前日期、最新可得披露期或快照报告期的年龄。旧财务配合新价格和快照即可通过筛选，违反 PRD §§6.7、7.2、10。

2. **已发布人工更正不会使快照失效或触发重算。** `calculator.py:916-938` 只在计算时叠加 `manual_overrides`；`data_quality.py:124-139,270-303` 不比较 override 创建/发布时点与 `indicator_snapshot.calculated_at`，且只将未发布 override 作为运维提示。发布更正后筛选仍使用旧快照，直到额外全量重算。

3. **真实个性化关系无法可靠恢复。** `backup/manager.py:831-843` 按 `PERSONALIZED_TABLES` 的顺序删除和插入；该顺序先删 `dsl_expressions` 再删 `dsl_dependencies`，先删 `screening_results` 再删 `watchlist`，与 `schema.py:395-443` 的外键关系相反。SQLite foreign keys 已启用，包含实际 DSL 依赖或筛选结果来源的备份会恢复失败；补偿也复用相同顺序，可能在 DuckDB 已恢复后再失败。

4. **维护锁会在有效长恢复期间被其他写进程主动删除。** `storage/maintenance.py:18-39` 将超过四小时的锁无条件 unlink，既不检查持有者是否活着，也没有续租。大备份/PDF 恢复超过阈值时，新的写操作即可删除锁并与恢复交错，破坏跨库恢复排他性。

5. **归档验证与删除之间存在 TOCTOU 数据丢失窗口。** `archive.py:153-181` 在独立连接中验证后释放锁；`cli/main.py:897-904` 随后在另一个事务中删除整张热表。任一更新/回填在二者之间提交的数据未在已验证 Parquet 中，仍会被 `DELETE FROM` 永久删除。

6. **独立 watchlist API 可伪造筛选来源。** `web/api/watchlist.py:16-21,113-124` 接受任意 `source_rule_id` / `source_result_id`，不验证股票属于结果或规则。虽然 `/api/screening/add_to_watchlist` 有成员校验，调用 `/api/watchlist/add` 可把任意股票显示为某个筛选结果的产物，破坏 PRD §13 的来源记录。

7. **`data switch_source` 是临时内存变更却返回持久成功，且无效请求协议状态为 ok。** `cli/main.py:1128-1152` 仅修改当前 CLI 进程的 `ADAPTER_PRIORITY`；下次命令会重新加载默认值。无效输入把 `error` 放在 data 中，`protocol.py:74-95` 因没有 `status/error_code` 而仍返回 `result.status=ok`。这会误导 OpenCode 或自动化维护流程。

8. **发行不能从声明的 Python 输入可靠复现。** `pyproject.toml:6-36` 未锁定依赖，也未把 PyInstaller 和必需数据适配器纳入默认发布依赖；`build-release.ps1:19-34` 直接使用当前环境的 `python -m PyInstaller`，而 `value-dashboard.spec:36-54` 强制导入 `akshare`、`easy_tdx`、`baostock`。空环境按默认项目依赖安装后不能保证构建或运行相同 EXE，违反 PRD §§19、20.4。

### 已复核但本轮未发现新的同级绕过

- 筛选 SQL 的字段白名单、参数绑定、已发布 DSL 锁定及 strict 审计值比较未发现新的注入或版本混用路径。
- 恢复 ZIP 路径穿越、尺寸/压缩比限制、认证 manifest 及复制件内容校验存在且 fail-closed。
- PDF 文件名和归档根路径检查未发现新的目录穿越路径。
- `/api/health` 的双库探针在数据库读取失败时正确返回 503。

### 上线前最低整改门槛

1. 将每一种成功写入及派生快照与 raw archive、batch、字段 lineage 放入同一个可回滚提交协议，并让 readiness 对关键输入/快照缺少 lineage fail-closed。
2. 交易日历缺失时阻止技术指标和筛选放行；加入逐段历史覆盖与最新财务报告期新鲜度检查。
3. 为浏览器写 API 实施 localhost Host/Origin 防护和启动会话令牌，或移除 Web 写操作并仅保留 CLI 写路径。
4. 修复恢复的外键拓扑顺序、可续租维护锁和归档 verify-delete 原子窗口；为真实依赖/来源图、锁过期和并发写入加入隔离回归。
5. 修复独立自选来源校验、CLI source switch 协议真实性、快照对 published override 的失效机制，并从干净锁定环境完成 release/EXE 演练。

在以上 P0/P1、正式数据核验、PRD §19.1 目标机性能签署和真实四流程验收全部关闭前，项目继续 **FAIL / BLOCK**。

## P0/P1 代码复验闭环（2026-07-29）

本节记录 Phase 16 后红队列出的 4 项 P0 与 8 项 P1 的代码级修复和隔离回归证据。它只关闭可由当前工作树和隔离测试证明的工程缺陷，不授权使用正式数据或发布。

| 审计项 | 代码级状态 | 当前控制 |
|---|---|---|
| P0: QFQ/分红 lineage 覆盖 | 已修复 | raw、QFQ、分红、XDXR、财务与派生快照的成功写入同事务记录 `fetch_batch`、非空 raw archive 和字段 audit；没有可校验来源材料或 hash 不匹配即回滚。 |
| P0: 数据/快照与 lineage 原子性 | 已修复 | 初始化、回填、增量重抓和 snapshot publish 在单个 DuckDB transaction 中提交数据与 lineage；就绪度对 raw/QFQ、核心财务和 snapshot 缺 source graph fail-closed。 |
| P0: 交易日历/BSE QFQ | 已修复 | `trading_dates` 成为 SQLite schema 的持久表；筛选没有日历即阻断；BSE 不再豁免 QFQ 抓取、回填、重抓、补数或快照发布。 |
| P0: DNS rebinding 写 API | 已修复 | Trusted Host 限制、精确 Origin/Host 匹配和每次进程启动的写 token 同时覆盖所有 `/api/*` mutation；前端 interceptor 只在本地 session 获取 token。 |
| P1: 陈旧财务/override 快照 | 已修复 | 完整财务期限定 18 个月；published override 或核心 source audit 晚于 snapshot 时使就绪度失败。 |
| P1: SQLite FK 恢复 | 已修复 | 个性化表按 child-first 删除、parent-first 插入；真实 DSL dependency、rule/result/watchlist 外键图恢复回归通过。 |
| P1: 长恢复维护锁 | 已修复 | 锁不再按固定时长删除；只有持锁上下文可写，其他 profile 写入 fail-closed。 |
| P1: 归档 verify/delete TOCTOU | 已修复 | 归档复验与热表删除在同一 DuckDB transaction 中进行，并复核 PDF SQLite manifest。 |
| P1: watchlist 溯源伪造 | 已修复 | 独立接口验证 `source_result_id`、结果成员及 `source_rule_id` 一致，不能伪造筛选来源。 |
| P1: `switch_source` 真实性 | 已修复 | 切换写入 `config/user.yaml` 的 adapter priority；无效请求返回稳定 `E001`/`error` 协议状态。 |
| P1: 可重现发行输入 | 已修复 | 新增 `uv.lock` 和 `release` extra；`scripts/build-release.ps1` 要求 Python/Node 两个锁文件并通过 `uv run --locked --extra release` 调用 PyInstaller。 |

### 本轮可重复证据

- 全量隔离 Python 回归：`264 passed`，S1 wrapper 完成；新增回归覆盖坏 raw hash 回滚、缺 source graph、无交易日历、BSE 缺 QFQ、Host/Origin/token、published override、真实 SQLite FK 恢复、归档和维护锁。
- 前端：`npm run lint`、`npm run build` 通过。
- Python 发布工件：`python -m pip wheel --no-deps --no-build-isolation .` 通过；`uv lock --locked` 通过。
- 未观察或修改正式 DuckDB/SQLite；所有 Python 验证均经 S1 隔离 profile 运行。

### 仍然阻断上线

1. 正式数据必须根据权威来源重建并全量核验，尤其是价格新鲜度、QFQ、交易日历、分红公告日、XDXR 和 lineage 材料。
2. PRD §19.1 必须在目标主机和真实 5,000+ 热数据上完成 10 次性能测量并获得签署。
3. 必须从 clean checkout 使用锁文件完成 EXE/启动器构建，配合已准备正式 profile，完成浏览器、API、CLI 四流程真实验收。

在这些外部证据完成前，总体裁决仍为 **FAIL / BLOCK**。

## 独立红队复审（2026-07-30）

### 裁决

**FAIL / BLOCK 不变。未确认新的 P0；确认 11 项 P1，其中多项证明上一节“代码级闭环”是范围过窄的假修复。**

本轮审查以当前未提交工作树为唯一对象，未采信上一节的整改表或测试名称。方法包括数据写入、派生计算、筛选/导出、PDF、备份恢复、CLI/冻结发行路径的独立静态追踪，并复核已有隔离回归。未读取或写入正式 DuckDB/SQLite。

### 已复核的真实修复

- raw/QFQ/分红的常规初始化、回填及增量路径现在会在同一 DuckDB transaction 写入 batch、非空材料和字段 audit；snapshot publish 也已同事务写派生 audit。
- BSE 的 QFQ 豁免已从主要价格更新路径删除；无持久交易日历时 `screening_readiness()` 会阻断。
- Host、精确 Origin 和进程级写 token 已覆盖 Web `/api/*` mutation；独立 watchlist 来源校验和 SQLite FK 恢复顺序也已存在。
- `uv.lock`、release extra 及 `uv run --locked` 已消除“依赖完全依赖当前 Python 环境”的原始问题。

这些结论不能外推到下列未覆盖的同类路径。

### P1 重要缺陷

1. **分红回填仍会以 NULL 覆盖已验证字段。** `app/core/backfill.py:386-405` 使用 `INSERT OR REPLACE`，而初始化路径使用保留旧非空值的 `COALESCE` upsert。若同一 `ex_date` 的回填结果缺 `announcement_date`、现金分红或送转字段，会擦除已验证值；随后股息率/分红率和就绪度使用被降级的数据。此项使“成功回填安全保留既有值”的整改不成立。

2. **XDXR 回填仍无字段级 lineage，且就绪度不要求它。** `app/core/backfill.py:465-470` 只写 XDXR 与 `fetch_batch`/raw archive，没有调用 `_record_field_audit_in_connection()`；`app/core/data_quality.py:215-265` 的 source graph 只要求价格、核心财务和 snapshot。终止或伪造后仍可留下被就绪度认可的公司行为记录，无法逐值追溯。上一节“XDXR 已有字段 audit”的表述不实。

3. **指标快照可混合不同报告期的财务数据。** `app/core/indicators/calculator.py:72-90` 以三表共同完整期确定 snapshot `report_date`，但 `_get_ttm_data()` 在 `:336-365` 独立选择最新 income/cash-flow 行，未限制到该共同期。比如 Q1 已到而 Q1 balance/cash-flow 未到时，会把 Q1 TTM 收入/利润与 Q4 资产/权益写成 Q4 snapshot。筛选的同 report-date join 无法发现已写入 snapshot 内部的错配。

4. **历史完整性和 raw 技术指标仍可跨缺口压缩。** `app/core/data_quality.py:65-116` 只检查首末日期和最多 1,000 条的计数阈值；5 年约 1,250 个交易日的成熟股票可以缺失大量内部日期仍通过。`app/core/indicators/calculator.py:826,837-856,900-904` 只对 QFQ return/volatility/drawdown 传入交易日历；raw MA、平均成交量和换手率会过滤 NULL 后压缩时间轴。持久日历存在但不完整也只要一行即可放行。

5. **已发布更正未在筛选的标准化字段和财务趋势中生效。** calculator 会叠加 `manual_overrides`，但 `app/core/screening/engine.py:297-308` 和 `app/web/api/stock_detail.py:349-390` 直接读取 DuckDB 三表。发布 `balance.total_assets` 更正并重算 snapshot 后，派生指标可能采用新值，而 `balance.total_assets` 筛选和趋势仍显示旧值，形成同一研究结果自相矛盾。

6. **发布 DSL 的 preview 与 screening 有不同的期间语义。** preview codegen 对累计流量 `@MRQ` 做单季差分（`app/core/dsl/codegen.py:188-211`）；screening compiler 在 `app/core/screening/engine.py:624-645` 将 `MRQ` 直接作为累计字段，且在运行时拒绝 TTM/YoY/QoQ。Q2 累计收入为 300、Q1 为 100 时，同一 DSL `income.revenue@MRQ` 可 preview 为 200、筛选为 300；可发布 shorthand 在筛选时还会失败。

7. **DSL 排名函数将缺失值当作可筛选排名。** `app/core/screening/engine.py:654-671` 的 `rank`、`rank_industry`、`percentile` 未对 argument 为 NULL 返回 NULL。DuckDB 的 `PERCENT_RANK(... NULLS LAST)` 会给 NULL 行末端百分位；`percentile(pe_ttm) > 0.9` 因而可选择没有 PE 的股票。内建 rank 字段有 NULL guard，DSL 路径没有。

8. **CSV 溯源不能关联标准化字段或 published override。** `_field_provenance()` 以 `balance.total_assets` 等导出列直接匹配 audit field（`app/web/api/screening.py:326-351`），而 ingestion audit 储存 `total_assets`（`app/core/init.py:1117-1132`）。更正发布只落 SQLite，不产生 override audit。保存并导出带标准化字段或更正的结果时，CSV 可显示空 provenance 或原始值来源，违反保存结果可复现实证的承诺。

9. **备份创建不是跨存储一致快照，也会在同秒并发冲突。** `app/core/backup/manager.py:325-328` 的 ID 只有秒级并以 `exist_ok=True` 创建目录；两个进程可同时写一个 ZIP。DuckDB 在 `:350-365` 的锁内导出，SQLite 个性化表在锁外逐表读于 `:379-383`，PDF 又在 `:421-443` 复制。并发发布 override、下载/归档 PDF 或第二个 backup 可生成从未共同存在过的恢复点或破坏备份文件。

10. **恢复和 PDF 归档未排除直接文件写入，存在源材料丢失竞争。** `PDFManager.download_pdf()` 在 `app/core/pdf/manager.py:123-139` 直接覆写 hot PDF，不检查维护锁；restore 随后在 `app/core/backup/manager.py:767-776` 删除并替换整个目录。`archive_pdfs()` 在 `pdf/manager.py:273-292` copy/hash 后直接 unlink 原文件，也没有文件版本检查。并发下载同名 PDF 可在 archive 校验后被 unlink；恢复中下载的 PDF 可被静默删除。

11. **冻结发行中的 `data switch_source` 仍是假持久化，CLI discovery 也不是实际协议。** frozen `Config.load()` 只读取 bundled `_internal/config`，并忽略 release 根的 `user.yaml`（`app/core/config.py:17-25,73-91`）；`data switch_source` 却写 `<exe-dir>/config/user.yaml`（`app/cli/main.py:1144-1154`）。打包产物中该目录通常不存在，即使手动创建，下一进程也不会加载它。另 `get_schema()` 只声明 `ok|error`，实际会输出 `partial`/`missing`，`get_capabilities()` 漏报多个 `*_execute` 命令（`app/cli/protocol.py:243-279`）。自动化客户端无法可靠校验或完成两段式维护。

### 对“发行闭环”的复核

`uv.lock` 确实锁定了 Python 解析输入，但还不能证明一键交付。`scripts/build-release.ps1` 和 `value-dashboard.spec` 明确不含正式数据库；`start.bat:31-57` 在不存在该数据对时退回 `python -m app.web.main`。这种“发行包需预先配置正式 profile”的设计可以成立，但目前没有 clean checkout、无 Python 接收机、已准备 profile 的实际 EXE 演练。因此不把它重复计为新的 P1，而继续作为发布签署阻断项。

### 本轮证据与限制

- 最近 S1 隔离全量回归为 `264 passed, 756 warnings`；它没有覆盖本节的并发 backup/PDF、回填 NULL 覆盖、TTM 跨期、override 标准化筛选或 DSL NULL rank 路径。
- 先前前端 lint/build、wheel 与 `uv lock --locked` 通过，均不能证明上述业务语义或发行运行时路径。
- 当前工作树包含大量未提交及未跟踪内容，无法作为 clean-checkout 可重现发行证据。

### 最低整改门槛

1. 统一所有 ingestion/backfill upsert 的非空保留语义，并对 XDXR、分红、metadata 和公司行为实施可验证字段 lineage；将相关 source graph 纳入 readiness。
2. 将 TTM/增长/技术指标严格绑定 snapshot as-of 与完整交易日历；对内部缺口 fail-closed，而非只检查计数。
3. 建立单一 override-resolved read model，供 calculator、screening、trend 和 CSV provenance 共用；将 override 版本/来源写入审计。
4. 使用同一语义编译/执行 DSL preview 和 screening，并对所有 rank/percentile 参数 NULL fail-closed。
5. 为 backup、PDF download/archive/restore 使用跨进程快照和文件级排他/原子替换；为 CLI frozen config 提供真实可加载的持久配置，或拒绝该命令。
6. 用隔离并发/中断测试和 clean-checkout EXE 演练证明修复，再重新申请审查。

在上述 P1、正式数据核验、目标机性能签署和真实四流程验收完成前，整体裁决保持 **FAIL / BLOCK**。

## P1 整改复验（2026-07-30）

本节只记录上一节 11 项 P1 的当前工作树代码整改；正式数据、性能与 clean-checkout EXE 验收仍不是本节的替代品。

| P1 | 当前代码整改 |
|---|---|
| 分红 NULL 覆盖 | `PriceBackfiller` 改为与初始化一致的 `ON CONFLICT ... COALESCE` upsert；稀疏响应不能擦除已验证公告日或分红数值。 |
| XDXR field lineage | XDXR backfill 在同一 transaction 写入 batch/raw archive 和按 `event_date` 的字段 audit。 |
| TTM 跨期 | `_get_ttm_data()` 接受 snapshot report-date 上界，`compute_all_for_stock()` 显式传入共同完整三表期。 |
| 技术指标跨缺口 | raw MA、平均成交量、换手率与 QFQ 风险指标都只使用交易日历连续的尾部窗口；历史/成交量就绪阈值提高到约五年工作日覆盖。 |
| Override 读模型 | 筛选 normalized statement 字段以 published override 覆盖原值；财务趋势也覆盖；CSV 将 `balance.*` 等字段映射至 audit 名，并将 override 标记为 `published_override`。 |
| DSL 期间/rank | 筛选不再接受 MRQ/TTM/YoY/QoQ 等没有同义 historical planner 的已发布表达式；rank、industry rank、percentile 对 NULL 返回 NULL。 |
| backup/PDF 竞争 | 全量备份在维护锁内创建，使用 UUID generation ID；PDF 下载、归档、恢复同一维护锁协调，下载使用临时文件原子替换，归档删除前重验源文件。 |
| frozen switch/discovery | frozen release 明确拒绝 `switch_source` 持久化；CLI init 输出协议 JSON，schema 枚举 `partial/missing`，capability 列出 executor 命令。 |

### 隔离验证

- 新增/扩展负向回归：分红稀疏响应、TTM as-of、XDXR field audit、raw 日历缺口、override 筛选/CSV、DSL 历史发布拒绝、backup generation 与 CLI 协议。
- S1 全量 Python：`267 passed`。
- 前端：`npm run lint`、`npm run build` 通过。

整体裁决继续为 **FAIL / BLOCK**，直至正式数据核验、目标主机性能、clean-checkout EXE 及真实四流程验收完成。
