---
title: 独立红队复审（2026-07-30）
status: superseded
category: reports
last-reviewed: 2026-07-30
superseded-by: reports/25_FORMAL_ACCEPTANCE_STATUS_2026-07-30.md
---

# 独立红队复审（2026-07-30）

## 裁决

**BLOCK。不得发布，也不得将当前数据用于价值研究或筛选。**

本报告不采信 `docs/23_INDEPENDENT_RED_TEAM_AUDIT_2026-07-29.md` 中任何“已闭环”表述。审查对象是当前工作树；逐路径审阅了全部 `app/**/*.py`、启动/发布/维护脚本、回归测试和前端 API/主要业务组件。结论以当前代码和隔离验证为准。

当前仍确认 **3 个 P0、13 个 P1、6 个 P2**。另有三项上线证据缺口不计入代码缺陷数：正式数据重建及全量核验、目标机真实性能签署、clean checkout 到 EXE 与已准备 profile 的四流程演练。任一 P0 已足以 BLOCK；这些证据缺口同样阻止改判 PASS。

最大的直接阻碍是：**正式研究数据可以被维护脚本静默破坏或被不完整恢复，而发行启动器又不能在空 profile 上完成首次初始化。** 因此无法同时证明“数据可信”和“交付可用”。

## 审查边界与方法

- 逐文件复核：`app` 的 CLI、Web、core（adapters、storage、DSL、screening、indicators、PDF、backup、archive、init、update、quality），以及 `scripts/`、启动/发布文件、`frontend/src` 和测试。
- 红队关注：数据毁损、来源可验证性、财务/期间语义、跨库原子性、路径与浏览器边界、发布可运行性、失败伪装、资源耗尽和 CSV 消费端攻击。
- 隔离 Python 全量回归：`scripts/s1-pytest.ps1 tests/regression -q --no-header`，结果 `272 passed`，未报告正式数据库状态变化。
- 前端：`npm run lint`、`npm run build` 与 Node 合约测试 `46 passed`。
- 打包：`python -m pip wheel --no-deps --no-build-isolation .` 成功；`uv lock --locked` 成功。
- 未直接读写正式 DuckDB/SQLite。S1 包装器的前后哈希证据只证明本次测试未改写正式库，不是正式数据正确性的证明。

最初一次 S1 启动因已有短生命周期 Python 进程被包装器正确拒绝；重试后全量回归完成。该拒绝是测试隔离控制正常工作的证据，不是测试失败。

## P0 阻断项（3）

> 2026-07-30 整改说明：以下 P0/P1 的代码整改与隔离回归已经完成；本报告的上线
> 裁决不因此改变。正式数据重建、性能签署和 clean-checkout EXE 演练仍需独立完成。

### P0-1：发行包无法从空 profile 一键启动

- 位置：`start.bat:11-13,31-57`、`vd.bat:4-25`、`scripts/build-release.ps1:39-52`。
- 证据：release builder 明确拒绝打包 DuckDB/SQLite；两个 launcher 同时要求调用方预设 `VD_ENV=formal`、`VD_FORMAL_ACK=confirmed`、两条精确绝对数据库路径，且要求目标数据对已经存在。它们没有 provision 或首次初始化路径。数据不存在时 `start.bat` 回退到系统 Python，而非发行 EXE。
- 影响：干净 Windows 接收机上双击发行物不能启动 V1；即使手工设置环境变量，空数据目录也不能由发行物自举。这直接违反 PRD §19.1、§20.4 的交付验收。
- 最小修复：交付受控 profile provisioner，或让 launcher 在显式确认后为本进程建立规范 profile、创建数据目录并由 EXE 完成首次初始化；加入无 Python 接收机的 clean-checkout EXE smoke test。

### P0-2：备份恢复在进程中断时可永久产生 DuckDB/SQLite 分裂代

- 位置：`app/core/backup/manager.py:741-818`。
- 证据：恢复先提交公共 DuckDB 表，再在独立 SQLite 事务恢复个性化数据，最后替换 PDF。普通 `Exception` 尝试内存内补偿，但 `SystemExit`、进程崩溃、机器断电或强制终止不会执行补偿。当前没有持久恢复 journal 或启动恢复逻辑。
- 可复现条件：在 `_restore_public_tables()` 返回后、`_restore_personalized_data()` 前终止进程；DuckDB 已是备份代，SQLite 仍是当前代。此前隔离注入已验证这一状态。
- 影响：规则、保存结果、自选、人工覆写与其引用的数据代不一致，且产品没有可靠的自动检测/恢复机制。
- 最小修复：采用持久 journal 加启动恢复，或恢复到完整新文件集，完成双库/PDF 校验后用原子目录切换发布；不得把进程内补偿当作崩溃一致性。

### P0-3：正式维护 CSV 导入可错列写入并先删除有效财务数据

- 位置：`scripts/import_csv_to_db.py:44-67,149-163`。
- 证据：资产负债表和现金流导入先删除 `2025-03-31` 之后数据，再以明确目标列配合 `SELECT * FROM df` 写入，但没有按目标列重排 DataFrame。缺少 `report_type` 或源列顺序不同即造成静默错列/NULL 写入；脚本没有 staging、schema/键/覆盖率验证或可恢复备份。
- 影响：合法维护命令可破坏正式三表并删除后续数据，进而改变估值、指标和筛选结果。
- 最小修复：显式 `reindex` 到唯一、完整的目标列清单，拒绝未知/缺失关键列；导入 staging 后验证行数、键、日期和非空覆盖，再以事务化 upsert/swap 发布。删除式全量替换必须先创建可验证备份并要求显式确认。

## P1 重要缺陷（13）

| ID | 位置 | 发现与影响 | 最小修复 |
|---|---|---|---|
| P1-1 | `app/core/data_quality.py:299-325` | lineage 只检查 hash 长度、关联和非空 payload，不重算 SHA-256。内容被篡改但保留 64 位 key 仍可通过 `LINEAGE_INVALID` 检查。 | 对每个被引用 archive payload 重算 SHA-256，并与 `raw_response_hash` 比较。 |
| P1-2 | `app/core/storage/duckdb_store.py:102-154` | 写锁超过一小时会在不检查 PID 是否仍存活时被删除；旧持有者退出后还会无条件删掉新锁。长写入可失去序列化。 | 永不因年龄删除存活锁；引入随机 owner token 并仅由持有者删除自己的锁。 |
| P1-3 | `app/core/adapters/cninfo_adapter.py:473-495` | 用公告日冒充除权日。实施公告与实际 ex-date 不同时会创建另一主键记录，股息率、分红率和连续分红年数可重复计算。 | 缺真实 ex-date 时保持缺失并阻止该事实入库，或从权威结构化/PDF 证据解析真实日期。 |
| P1-4 | `app/core/adapters/baostock_adapter.py:253-264`、`app/core/adapters/tdx_adapter.py:317-333,586-606` | 这些路径归档的是“某股票 N 行”之类摘要而非返回值，却被标为原始来源材料。数值无法独立复算。 | 归档供应商原响应或可逆、逐行 canonical serialization；摘要只能作诊断材料。 |
| P1-5 | `app/core/pdf/manager.py:116-125,212-226`、`app/web/api/stock_detail.py:714-725` | PDF download/list 路径未统一校验六位股票代码，`..`/反斜杠可逃出 hot root，写入或枚举其他目录的 PDF。 | API 与 manager 的所有入口统一验证六码代码，并在每次 resolve 后强制 `relative_to(hot_dir)`。 |
| P1-6 | `app/core/pdf/correction.py:145-151,263-288,356-384` | 校正校验拒绝 NaN 但接受 `inf`；发布字段和模板状态分两个事务，状态更新还吞异常。可发布无限值或形成“已发布字段、previewed 模板”的半状态。 | 用 `math.isfinite` 和字段/单位范围校验；在同一 SQLite 事务写全部 override 并更新模板状态，失败必须回滚并显式报错。 |
| P1-7 | `app/core/screening/engine.py:417-501`、`app/web/api/screening.py:426-461` | rule JSON、节点数和 `in` 数组没有总量上限。10 万项 `in` 会生成 10 万 bind 参数和约 300 KB SQL，可耗尽 DuckDB/API 资源。 | 保存前限制 JSON 字节数、树深度、叶子数和每个 `in` 的有限数值项数。 |
| P1-8 | `app/web/api/screening.py:260-284`、`app/cli/main.py:1285-1298` | Web 和 CLI CSV 未转义前导 `= + - @ Tab CR` 的字符串。上游股票名称或文本字段可在 Excel/WPS 中成为公式。 | 统一 CSV serializer，对危险前缀加单引号并做 Web/CLI 共用回归。 |
| P1-9 | `app/web/api/stock_detail.py:111-142` | `stale_warning` 仅取价格与快照年龄，完全忽略 `financial_age_days`。两年前财报配合今天快照仍显示“无滞后警告”。 | 把财报年龄纳入阈值，或返回并在 UI 强制显示独立 `financial_stale_warning`。 |
| P1-10 | `app/web/api/stock_detail.py:82,411-431,621-637` | 多处以 `a or b` 选择归母/总额，并以真值判断比率。合法零值被视作缺失或回退到另一口径，零利润/零负债公司的趋势和 ROE/利润率失真。 | 统一用 `is not None` 做空值回退，允许 0 参与有定义的计算。 |
| P1-11 | `frontend/src/components/FinancialTrendCard.vue:46-55,67-74,121-124`、`app/web/api/stock_detail.py:415-433` | 图表先过滤空值再按压缩索引画点，X 轴却用未过滤报告期；后续数据会落在错误日期。后端也未在 financial trend 返回 `parent_net_profit`，前端“归母净利”选择会得到空列。 | 用同一过滤后的 `{date,value}` 序列绘制轴和点；返回并消费 `parent_net_profit`。 |
| P1-12 | `app/web/api/data_status.py:27-224`、`frontend/src/views/StockDetailPage.vue:150-158` | 数据库/质量请求异常被伪造成 `0`、`null` 或空 warning。损坏、权限或网络失败会呈现为“没有数据/没有风险”。 | 对核心依赖返回 503；其余字段提供 `available/error`，前端显示未知/不可用，不能把失败转换为空告警。 |
| P1-13 | `app/core/data_quality.py:63-126`、`app/core/indicators/calculator.py:1020-1080` | 就绪度只要求交易日历存在一行，并用约 67% 的日历天计数评估五年价格历史；日历自身不完整或内部缺口可让技术序列视为连续。 | 按权威交易日历验证逐日覆盖/末端连续窗口，并将日历完整性、缺口和价格覆盖同时 fail-closed。 |

## 整改复核（代码层，2026-07-30）

- P0-1：launcher 为自身 canonical formal profile 创建数据目录并使用发行 EXE 完成初始化；不再依赖调用方预设 `VD_ENV`。
- P0-2：恢复前持久化 rollback snapshot/journal；CLI 和 Web 启动时自动恢复中断的恢复操作。已注入 `SystemExit` 验证 DuckDB/SQLite 回到同一恢复前世代。
- P0-3：CSV 在任何删除前验证关键键，并显式重排列至 SQL 目标列；错误 CSV 回归验证不会执行删除。
- P1-1 至 P1-12：来源 SHA-256、锁 owner token、真实分红日期、可逆原始归档、PDF 路径、有限数值/原子发布、规则和 CSV 防护、财报新鲜度、零值语义、趋势字段/日期与 API 失败语义均已有代码修复与隔离回归。
- P1-13：筛选门禁现在逐只已上市股票双向校验 QFQ 序列与持久化交易日历；任一日历缺口、价格缺口或 QFQ 日历外日期均 fail-closed。
- 复核命令：`scripts/s1-pytest.ps1 tests/regression -q --no-header`，286 passed；前端 lint/build/Node 合约测试，46 passed；`uv lock --locked`、`python -m pip wheel --no-deps --no-build-isolation .` 通过。

## P2 改进项（6）

| ID | 位置 | 问题 |
|---|---|---|
| P2-1 | `app/core/storage/schema.py:434-443`、`app/web/api/watchlist.py:115-142` | 表缺 `(stock_code, group_name)` 唯一约束，`INSERT OR REPLACE` 不会替换；重试/双击能产生重复自选。 |
| P2-2 | `app/web/api/data_status.py:229-256` | `limit` 无上下界；SQLite 负 LIMIT 表示无限制，`limit=-1` 可返回整张 retry/missing 表。 |
| P2-3 | `app/core/data_quality.py:111-117`、`app/core/update.py:350-377`、`app/core/backfill.py:156-177` | 新鲜度使用 `CURRENT_DATE`，更新/回填使用主机本地时间；长假与时区边界会造成错误 stale 或请求结束日期。 |
| P2-4 | `pyproject.toml:55-58`、`app/web/static/` | wheel 会带入多代 hash 静态 bundle，当前工作树也有大量未跟踪资产。发布体积、审计面和 clean checkout 可重复性变差。 |
| P2-5 | `scripts/build-release.ps1:25-37`、`frontend/package.json:6-11` | release 流程只做 build，不执行现有 Node 合约测试或 S1 Python 回归；绿色测试不是发布门禁。 |
| P2-6 | `scripts/import_csmar.py:242-455` | 默认 CSMAR 导入删除五张核心表，写入 `raw_data='{}'`，会丢失更新后数据、元数据和可验证来源；虽然是维护脚本，仍缺 staging/确认/备份。 |

## 已验证控制与限制

以下控制在当前源中存在，未在本轮发现同级绕过：

- Web 对 `/api/*` 写操作实施 Host、同源 Origin 和进程级 write token 检查；非 loopback 配置被拒绝。
- 核心筛选字段、排序与 DSL 标识符使用白名单/参数绑定；未确认可达 SQL 注入或 shell 执行路径。
- 通常初始化、回填和单股票重抓路径会在单个 DuckDB transaction 中写数据、batch、非空 source payload 和字段审计。
- 归档清理会在 DuckDB transaction 内重新验证公共热表；ZIP 还原检查路径穿越、大小、压缩比和 manifest/HMAC。
- 当前 Python/Node 构建和隔离回归均可通过。

这些控制不能抵消本报告的 P0/P1，也不能证明正式数据正确。尤其是，测试夹具用模拟数据和短路径，不能替代当前正式数据库的权威来源重建、全量覆盖核验和性能验收。

## PASS 前门槛

1. 关闭全部 3 个 P0，并为崩溃恢复、错误列 CSV、空 profile EXE 启动分别添加独立负向测试。
2. 修复 13 个 P1，优先处理来源 hash、交易日历、分红日期、数值零值/财报新鲜度和所有 fail-open 状态展示。
3. 在隔离并发/中断测试中证明恢复、锁、PDF 和导入不会产生分裂代或毁损。
4. 基于权威材料重建正式数据并全量核验价格、QFQ、交易日历、财报、分红、公司行为和逐字段 lineage；在本报告前不把历史审查中的数据结论自动视作已修复。
5. 在目标主机、真实 5,000+ 股票热数据、20 条条件、复合指标和行业排名下完成 PRD §12.6/§19.1 的 10 次性能记录。
6. 从 clean checkout、锁定依赖、无 Python 的接收机和已准备 profile 完成 EXE/launcher、浏览器、API、CLI、维护恢复四流程演练。

在以上条件完成前，结论维持 **BLOCK**。
