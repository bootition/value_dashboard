---
title: 运行期硬约束知识库（Ops Knowledge Base）
status: approved
category: runbooks
created: 2026-09-01
last-reviewed: 2026-11-26
supersedes: null
---

# 运行期硬约束知识库（Ops Knowledge Base）

> **本文件是什么**：把散落在 100+ 份事件报告（reports/）与历史会话里的**运行期硬约束**集中显性化。
> **为什么存在**：这些约束此前只存在于"当时那份报告"里，新会话/新维护者必须翻几十份报告才能拼全；很多知识在会话结束后就变成隐性知识（例如"12GB 的实验库为什么不敢删"）。
> **如何使用**：遇到任何运维/数据/环境问题，先查本文件；本文件是这些约束的**唯一常驻入口**。
> **更新规则**：对话中确认任何新的硬约束/环境事实/保留期限，必须在本文件登记（见 AGENTS.md「隐性知识回写规则」）。
> 关联：`docs/decisions/02_TECH_CONSTRAINTS.md`（PRD 产品/技术规格约束，本文件不重复）；`docs/STATUS.md`（当前状态唯一权威）。

---

## 1. 环境与启动

| # | 约束 | 依据 |
|---|---|---|
| E1 | 仅 Windows 中文个人桌面 + Chrome/Edge，localhost 使用，无公网、无多用户 | PRD §3.1 |
| E2 | 本机：Win11 家庭版 / 32GB RAM / RTX 5070 Laptop 8GB / Ryzen 9 7845HX / C 盘空间紧张（模型装 D 盘） | 用户环境 |
| E3 | **数据路径必须用项目 venv**：`vd.bat`/`start.bat` 优先 `.venv\Scripts\python.exe`；`uv sync --locked` 默认**不含 extras**，必须 `uv sync --locked --all-extras`（akshare 1.18.81 / baostock / easy-tdx），否则数据源模块缺失 | reports/77 N1 |
| E4 | 系统 Python 的 akshare 1.18.64 有缺陷（SECUCODE 无归一化、pageSize=20 截断历史），**禁止**用于数据路径 | reports/75 |
| E5 | ruff 规则集**必须显式锁定**传统集（pyproject.toml `[tool.ruff.lint] select`）；ruff 0.16 默认扩展集会爆 500+ 存量违规 | reports/77 N2 |
| E6 | Git push 默认走代理 `127.0.0.1:10808`（git 全局 URL 级 http proxy）；代理不可用时可用 `git -c http.https://github.com.proxy= -c https.https://github.com.proxy= push origin <branch>` 临时直连（2026-09-01 验证成功）。网络变更后需重新确认 | AGENTS.md；2026-09-01 实测 |
| E7 | **DuckDB 连接配置统一且禁止同进程混配**：`config/default.yaml` 现在统一为 `database.duckdb_memory_limit=8GB`、`duckdb_threads=2`、`duckdb_preserve_insertion_order=false`；Web 服务进程内**禁止**再调用 `DuckDBStore.memory_limit()` 制造差异配置（DuckDB 会报 different-configuration，普通请求重试约 28s 后 500/503）。`memory_limit()` 只允许专用子进程/CLI 维护任务使用。`VD_DUCKDB_MEMORY_LIMIT` 环境变量仍是运维逃生口 | reports/104；config；duckdb_store.py |

## 2. 数据源约束与限速

| # | 约束 | 依据 |
|---|---|---|
| S1 | 东财 push2/push2his 曾于 2026-08 被封；2026-09-02 单请求探测已恢复（HTTP 200）。价格主链当前仍为腾讯/BaoStock/TDX；若要切回东财，先限速 ≤2 req/s、并发 ≤5 观察 | reports/61；reports/103 |
| S2 | **北交所（920xxx）不请求东财**：无交叉源且异常会触发 cninfo_capital 熔断殃及沪深；如实记 empty（`no_cross_source:bse`），不入 retry | reports/75 |
| S3 | 东财交叉核验安全组合：**批 50 + 批间冷却 30-60s**；`update_many` 连续 8 次交叉错误 / 16 次空响应中止保护 | reports/75 |
| S4 | CNINFO 风控冷却：股本链约 4,700 只由自动更新有界续传，勿手动批量触发 | reports/74 |
| S5 | CNINFO 公告接口**每页固定 30 条**，pageNum>100 重复第 1 页——分页判断必须用实际 30 条 | reports/84/中报修复 |
| S6 | CNINFO 分红适配器 **ex_date 主源恒空**（公告接口不提供该字段，死代码保留至 M8 PDF 解析阶段）；回退链 akshare/baostock 提供 ex_date 可用 | reports/61 §3.2；manager.py |
| S7 | 源级限速（秒/请求）：cninfo 1.5、akshare_eastmoney 0.5、tdx 0.2、baostock 0.8、tencent 0.2、sina 0.35、eastmoney_f10 0.5 | config/default.yaml |
| S8 | CSRC 行业分类：**只取证监会标准**，显式 `start_date=19900101`、`end_date=今天`；源无分类如实 NULL + `missing_list(csrc_industry)`，禁止混用巨潮/申万/中证 | reports/86 |
| S9 | 价格主链：腾讯（连接复用）→ baostock → tdx；连续流水线 `price_fetch_pipeline_depth=64` | config；update.py |
| S10 | **港股分红源**：`ak.stock_hk_dividend_payout_em` 底层为 `datacenter.eastmoney.com`（非 push2）；适配器 `eastmoney_hk_dividend` 硬限速 **0.5s/请求（≤2 req/s）**，逐股串行，禁止并发；`stock_zh_ah_spot()` 为映射快照源（单次请求，不属于 datacenter 限速面） | app/core/adapters/hk_dividend_adapter.py；2026-09-04 实测 |
| S18 | **价格更新默认禁止因"陈旧"整段重拉**：`update.incremental_window_days`（默认 30 天）只是陈旧阈值，整段重拉需显式 `update.price_full_refetch_on_stale=true`（数据修复用）。默认路径始终从本地最新日期增量补缺口。反例（2026-10-20）：停用 33 天后触发全市场 5,551 只 raw+qfq 全历史重拉，腾讯每只约 20 次分页请求 + 逐股 DELETE+INSERT，吞吐 148→6.2 只/分、ETA 14.7 小时 | reports/114；app/core/update.py |
| S19 | **TDX 逐股建连是分红/除权步骤的瓶颈**：`TdxClient.from_best_host` 实测 **5.02s/次**，同一会话内 xdxr 查询仅 0.02–0.16s。任何逐股调用 TDX 的批处理都必须改成"同批共用一次会话"（`update.market_actions_batch_size`，默认 50）：535 只可从约 76 分钟降到 5–8 分钟；失败要写 retry_list，否则会静默丢失 | reports/115；app/core/update.py |

## 3. DuckDB / 存储约束

| # | 约束 | 依据 |
|---|---|---|
| D1 | **单写者锁**：跨进程互斥（`app/core/storage/update_lock.py`）；所有 CLI 写命令必须经 `exclusive_update`；库层第二道 `_write_lock` | reports/84、92 |
| D2 | **DuckDB 1.5.5 同事务 `DROP INDEX`+`CREATE INDEX` bug**（`BoundIndex::CreateDeltaIndex` FATAL）：索引重建必须分事务（DROP 提交后再 CREATE）。**升级评估已作废（2026-11-22）**：v50 起库里已无二级索引（`DROPPED_SECONDARY_INDEXES` 全删且 `init_duckdb_schema()` 不再建），仓库里已无同事务 DROP+CREATE 的调用点 —— `drop_useless_secondary_indexes()` 只 DROP、一条一个事务；`reindex_table()` 逐条自动提交。**另需记住记账口径**：`drop_useless_secondary_indexes()` 是**条件记账**（返回值 `complete` 要求「目录读得到 + 无 failed + 无 remaining」，**没清干净就不写 `schema_migrations.version=50`**，`MAX(version)` 停在 49 → 下次启动必跑完整 init → **自动重试**），所以"v50 记账存在"本身=索引已清干净的证书，不能顺手无条件写 | reports/81 F1；v50 的依据是 2026-11-21 逐索引 A/B 实测＋同日对"机制解释"的纠正，**常驻条文只有** `app/core/storage/schema.py` v50 段注释与本节（`reports/151` 正文是"指标增量重算"，不含索引内容，勘误见其 §10 附录） |
| D3 | `raw_response_archive` 冷热分层（schema v16）：`history`（冷）+ `active`（小）+ 视图 `raw_response_archive_all`；冷核对走 hash 集合，不触碰 BLOB | reports/96 |
| D4 | lineage hash 集合（schema v17）：`raw_response_archive_valid_hash` + `raw_response_archive_partitions`（5GB / 10万行 / 31天自动轮转，计数器维护避免每次 SUM 全扫） | reports/102 |
| D5 | 当前 schema 版本：`DUCKDB_SCHEMA_VERSION = 48`、`SQLITE_SCHEMA_VERSION = 19`（app/core/storage/schema.py） | 代码 |
| D6 | `vd backup` 对 26GB BLOB 表须**分块导出**（raw_response_archive_history 5000 行分块），单次 COPY 会受内存限制 | reports/102 |
| D10 | `vd data auto-update status` 是**只读**命令（只查 SQLite，不打开 DuckDB）；其它 CLI 写命令在 schema 已最新时经 `skip_if_current=True` 跳过全量幂等 DDL，避免扫描 43GB BLOB 视图 OOM | reports/104 |
| D11 | 数据状态重量摘要：后台 stale-while-revalidate，TTL 300s；空闲期前端每 300s 拉一次，更新 running→finished 时前端主动立即刷新一次。全量构建仍约 19-23s，但不得阻塞或拒绝普通查询 | reports/104；data_status.py |
| D12 | **自动更新写连接窗口会阻塞 Web 查询**：DuckDB 单写者模型下，research_statistics 全量重建的发布阶段会持续持有写连接（2026-09-03 实测约 4-6 分钟），期间普通 K 线/详情/自选请求会等待或超时。这不是连接配置冲突；优化方向是分批可见发布或快照读，而不是调大超时硬扛。**2026-09-17 补充**：自动更新是独立子进程，其持锁期间 Web 进程**完全无法新建任何连接**（Windows 文件独占：`Cannot open file ... 另一个程序正在使用此文件` + `File is already open in <子进程 PID>`）；本次价格补抓 4732 只、约 66-98 只/分，仅 price 步骤约 70 分钟（整体 1.5-2h），期间只读能力完全依赖更新前预热缓存 | reports/104、reports/113；实测 |
| D13 | `source_audit` 冷热分离：日常 readiness/lineage 只扫热表 `source_audit`；历史排查查 `source_audit_all`。归档命令 `vd data source-audit-archive --before YYYY-MM-DD`，按 id keyset 分页，每批独立事务；正式库已归档 30,039,082 行（cutoff 2025-01-01） | reports/106；app/core/source_audit_archive.py |
| D24 | **价格表全量替换（`DELETE ... WHERE stock_code=?`）在正式库约 10s+/股**（两表各一次，1,700 万行 + ART 主键），是"整段重拉"模式下真正的瓶颈；正式库 20.2GB（2026-09-04 重建后 7.8GB）高水位继续被推高。数据修复模式请选收盘后执行并预留数小时；常规更新不得走该路径 | reports/114；py-spy 采样 |
| D25 | **读连接"外部锁"判定必须区分锁属主**：autoupdate 子进程持有 `.value-dashboard.update.lock` 时，其只读连接回退为读写连接会与 Web 进程的只读连接争用文件锁，Windows 报 "Cannot open file ... 另一个程序正在使用此文件"；旧逻辑因 `lock_active=true` 立即抛 `DuckDBReadLockedError`，导致 `_get_latest_financial_report_date` 返回 None（可能触发财务全量重拉）。现由 `update_lock_owned_by_current_process()` 判定属主并退避重试 | reports/115；duckdb_store.py |
| D14 | 分红融资比为 **A股流通股本口径**：`cumulative_dividend_amount` 用 `circ_shares` 优先，total_shares 中的 H 股不得混入；港股分红未采集即不计入、缺数据返回 NULL。600941 已修正为 34.7% | reports/106；calculator.py |
| D15 | 2026-09-04 离线重建后主库 7.8GB：`source_audit_archive`（30,039,082 行）与 `raw_response_archive_history` payload 均只存外部 Parquet（`D:\vd-cold-archive`），主库仅空表/元数据；旧库 `valuedashboard.duckdb.old-20260904013322` 保留回滚 | reports/107 |
| D16 | **总股本分红融资比暂不发布**：当前 `dividends`/`funding_events` 只有 A 股数据，用 total_shares（A+H）会出现 600941 825.9% 类错误；待港股分红与港股融资数据源接入后再增加全市场口径字段 | reports/107 |
| D17 | 外部冷 Parquet 分区规范：`D:\vd-cold-archive\partitioned` 下按 `year=YYYY` 目录，raw_response_archive_history 每 part ≤5,000 行，source_audit_archive 每 part ≤500,000 行；manifest 必须逐 part 记录 rows/sha256，迁移窗口下界必须携带上一游标，避免重复包含 | reports/109；scripts/repartition_cold_archive.py |
| D20 | **raw_response_archive 轮转记账修复（2026-09-04）**：旧 `_rotate_raw_archive_if_needed` 轮转后活跃 registry 行的 row_count/estimated_bytes/created_at 全部原样保留（UPDATE 打在尚不存在的 TS 名上 + 新行 ON CONFLICT DO NOTHING），5GB/10万行/31天 阈值一旦触发即**每次写入都轮转**；当日实锤 416 个单行 TS 表、同秒两次轮转同名表 Catalog Error 崩溃。修复：活跃行先改名为 TS 名并落终值→插入计数器归零的新活跃行（无 ON CONFLICT，异常让事务显式失败）；表名冲突时加序号。存量 416 表已合并为 `raw_response_archive_merged_20260904`（52,173 行/5.39GB） | reports/110；app/core/storage/duckdb_store.py；.planning/merge_runaway_partitions.py |
| D19 | **DuckDB 1.5.5 executemany 绑定 date/datetime 参数按 ~450KB/行 堆积事务内存**（1 万行实测峰值 4.4GB；全量 lineage 发布 24.5 万行直接 OOM 7.4GB）。修复范式：大批量写入带日期列的持久表一律走 pandas DataFrame `connection.register()` + 单条 `INSERT ... SELECT`（24.5 万行 0.3s/峰值 <100MB，2026-09-04 库副本复现验证）；小批量（≤数百行/批）仍可用 executemany。fetch_time 时区语义：`pd.to_datetime(..., utc=True).dt.tz_localize(None)` 落 naive UTC，与旧路径一致 | reports/110；app/core/indicators/calculator.py 2026-09-04 修复 |
| D18 | **港股分红域 `hk_dividends`（schema v20，2026-09-04）**：仅覆盖 `stock_zh_ah_spot()` 可映射的 A+H 公司（快照 203 条；按当前上市池实测映射 202 只：152 精确名 + 50 人工覆写，药明康德 H 02359 如实 unmatched）；A→HK 映射持久化在 `app/core/ah_hk_mapping.py`，禁止后缀剥离模糊猜映射（招商银行≠招商证券）。写路径只有 `vd data hk-dividends` + `_with_update_lock`，单股 DELETE→INSERT 原子替换；不触碰 stock_meta/indicator_snapshot/readiness，指标公式暂不修改。港股 IPO/配股/供股融资仍缺失，总市场分红融资比继续 BLOCK | reports/108 |
| D7 | **正式库 data/ 只读**：所有写操作必须经 CLI/维护脚本 + 单写者锁；S1 回归强制 `VD_ENV=test` + 正式库 SHA-256 指纹前后对比 | AGENTS.md；conftest.py |
| D8 | ✅ 回滚快照已按窗口删除（2026-09-02）：9-01 22:52 完整成功周期（job 124）通过观察；两硬链接 + sqlite pre-rebuild 已删除，释放约 50GB | reports/101、102；job_logs 124 |
| D9 | 重建/导出相关外部路径：新库构建 `D:\vd-rebuild-new-20260901`、Parquet 导出 `D:\vd-rebuild-export-20260901`、冷归档 `D:\vd-cold-archive` | reports/102 |
| D21 | **读锁降级缓存铁律（2026-09-17 事故后）**：① 写操作后禁止用裸 `TTLCache.invalidate()` 删除旧快照——默认 `keep_stale=True`，旧值置为过期回退，重算遇 `DuckDBReadLockedError` 时返回旧值；只有"结果本身不可用"（如全 error 快照）才 `keep_stale=False` 或用 `reject`。② 只读接口必须区分 SQLite 用户数据（预算/持仓/流水/设置，实时读、不缓存）与 DuckDB 派生数据（价格/估值/分位，可缓存 + stale 回退），否则一次写后刷新就会在更新窗口内 503。③ 未单独捕获 `DuckDBReadLockedError` 的接口由 `app/web/main.py` 全局处理器统一降级 503（`reason_code=duckdb_read_locked`） | reports/113；app/web/api/_ttl_cache.py、etf_strategy.py、index_dashboard.py |
| D22 | `_is_external_file_lock` 不能只看 `Cannot open file`：该文案对"拒绝访问/路径不可用"等永久性 IO 错误同样出现。只有 `另一个程序正在使用此文件` / `File is already open in`，或应用级更新锁文件确认存在时，才抛 `DuckDBReadLockedError` 立即降级；其余继续退避后如实报错 | reports/113；app/core/storage/duckdb_store.py |
| D23 | **已知未修口径缺陷（P2，2026-09-17 登记）**：`etf_fundamentals.market_cap_series` 与 `index_dashboard._all_a_valuation_series` 的历史市值 = 前复权收盘 × `stock_meta.total_shares`（当前股本），与既有 `share_capital_history` 逐日股本口径冲突（分红低估、增发高估历史市值）；**当前值正确、仅历史段待修**。修复前上述 10 年历史市值/分位曲线不得当结论使用 | reports/113 §6；schema.py:456-459、statistics.py |

## 4. 数据口径与隔离表设计意图

| # | 说明 | 依据 |
|---|---|---|
| Q1 | **quarantine 隔离表是设计内机制，不是垃圾**：`dividends_quarantine`（8,467 行不可核验 ex_date 如实隔离）、`source_audit_quarantine`（255 万行 `unsupported_legacy_lineage`，2026-07-28 批量隔离）、`fetch_batch_quarantine`、`raw_response_archive_quarantine` —— 清理前必须确认无恢复价值 | STATUS 缺口 #4；审计发现 |
| Q2 | 2026-03-31 前历史财务为 CSMAR 导入值，**无原始字节 lineage**（如实披露，不伪造） | STATUS 缺口 #2 |
| Q3 | 4 只新股（001232/301677/920038/920258）及 920305 免费源核心数据未形成，不进研究快照；银行/券商监管字段 90 只保持 NULL（不伪造） | STATUS 缺口 #2 |
| Q4 | 统计域 partial 构建为设计：新股/无价格股票如实无记录；输入指纹变化后原子重建 | reports/74 |
| Q5 | 单位元数据**单一来源**：后端 `/indicators` 下发 `unit`，前端消费换算（百分数字段不要自行 ÷100） | reports/81 F3 |
| Q6 | 筛选更新窗口口径：以最近原子完整快照为准并标注数据截至日期（方案 A 已实施） | reports/79；PRD SC8 |

## 5. 代码与门禁约束

| # | 约束 | 依据 |
|---|---|---|
| G1 | S1 隔离回归：`scripts/s1-pytest.ps1 tests/regression`；conftest 拒绝 `VD_FORMAL_ACK`、强制 `VD_ENV=test`；正式 data/ 树前后全量对比，delta 退出码 99 | AGENTS.md |
| G2 | 静态检查：`uv run --locked ruff check app tests/regression`；前端 `npm run lint / test / build` | AGENTS.md |
| G3 | 测试证据只放 `docs/evidence/evidence-s1/`（hash 目录）；`scripts/evidence/` 为运行期产物目录，**只允许刻意入库** | .gitignore；红队 P3 |
| G4 | 锁文件：`uv lock --locked` | AGENTS.md |
| G5 | 写操作面：Web 页面只读（DS3）；危险操作两段式确认（plan_id，15 分钟有效） | PRD CL10/CL11；02 |

| C-LOG | **长任务 CLI 必须自带 logging 配置**：`data auto-update run` 入口调用 `_configure_update_logging()` （INFO→stderr，行缓冲）。未配置时根 logger 只在 WARNING+ 经 lastResort 输出，更新过程的 INFO 日志全部丢失，`data/logs/auto-update-child.log` 会一直为空 | reports/114；app/cli/main.py |

| C-S1 | **S1 门禁固定开销 = 2 次 19GB 全量哈希**（before/after）。一轮 S1 只应跑一次并合并目标测试：每次捕获两遍全量哈希、wrapper/conftest 重复捕获、`.old-*` 41GB 回滚快照与 logs 全树哈希都会成倍放大开销（改前约 190–600GB/轮、8.5–13 分钟；现 2 次约 90s）。合成哨兵测试（test_hash_preservation）仍走真实哈希路径 | reports/115；scripts/s1-*.ps1、conftest.py |

## 6. Git 纪律（AGENTS.md 摘要）

- 会话结束前必须提交；里程碑完成立即提交；禁止跨主题打包；按主题拆分（docs/ feat/ fix/ chore/）
- **永不提交**：`data/`、`*.whl`、`app/web/static/assets/`、`tests/regression/<hash>/`、`docs/evidence/evidence-s0|s1/`、`_legacy/`、`.omo/`、`.opencode/`、`.planning/`、`frontend/test-results/`
- 每次提交后必须 `git push`（代理 127.0.0.1:10808）；push 前 `git fetch` 检查冲突；**push 失败必须如实告知**（不得谎报"已推送"）
- 重要基线打 tag 推送（如 `incident-2026-07-22`、`s1-path-isolation-archive-156dded`）

## 7. 已知技术债 / 待办（截至 2026-09-01）

| # | 事项 | 出处 |
|---|---|---|
| T1 | ✅ 已评估（2026-09-02）：本地/镜像最新可用版本仍为 1.5.5，无升级空间；维持分事务重建 workaround，待上游发布 >1.5.5 后复评 | reports/81；2026-09-02 核验 |
| T2 | ✅ 已关闭（2026-09-02）：`vd archive restore` + `restore_execute` 已实现（先验证 manifest/verified 记录再恢复全部热表），回归 5 passed | reports/102；代码核验 |
| T3 | ✅ 已关闭（2026-09-01）：`config/user.yaml` 骨架已创建，覆盖通道激活；只写需覆盖的键 | 体检修复 2026-09-01 |
| T4 | ✅ 核验路径已接入（2026-09-02）：`vd data quarantine-dividends-audit` 只读分类剩余隔离行（可恢复/重复/无候选/歧义/冲突）；实际修复仍走 `scripts/repair_dividend_ex_dates.py --yes` | STATUS 缺口 #4；代码核验 |
| T5 | ✅ 已探测（2026-09-02）：`vd data probe-eastmoney-push2` 实测 HTTP 200（600519 f43=129750），**push2 已解封**；如重新启用仍须 ≤2 req/s、并发 ≤5 | reports/103；实测 |
| T6 | ✅ 审计命令已接入（2026-09-02）：`vd screening audit-legacy-unit-rules` 列出受影响规则与字段；复核另存仍需用户操作 | reports/81；代码核验 |
| T7 | ✅ 已核验（2026-09-02）：`_unlink_with_retry` 重试 + 后台线程等待已就位；定向与完整收集通过 | reports/77；2026-09-02 测试 |
| T8 | ✅ 已核验（2026-09-02）：国债测试已改为动态对齐最新价格日；PDF 归档路径隔离测试定向通过 | STATUS；2026-09-02 测试 |
| T9 | ✅ 正常（2026-09-02 复核）：`vd backup` 对 `raw_response_archive_history` 默认 5000 行分块导出/恢复（`_BLOB_CHUNK_TABLES`） | STATUS；代码核验 |

## 8. 隐性知识回写规则（本文件的自我约束）

1. **对话中确认的任何新硬约束**（环境事实、数据源行为、保留期限、口径裁决）→ 当日登记到本文件对应章节，并更新 `last-reviewed`
2. **决策必须附着在决策物上**：代码/schema 级特殊设计（如 quarantine 表）在代码注释或本文件登记设计意图
3. **会话收尾清单**（每次会话结束前执行）：git commit → git push → 本文件/STATUS.md 知识提炼 → 删除实验产物（数据库副本、临时 CSV）
4. 引用本文件结论时标注：`docs/runbooks/ops-knowledge-base.md` + `last-reviewed: 2026-10-20`

## 9. 多指数 ERP 与 ETF 工作台新增约束（2026-09-05）

| # | 约束 | 出处 |
|---|---|---|
| S11 | 乐咕 `stock_index_pe_lg/pb_lg` 上游已改为**月末序列**（约 144~261 点，2005 至今）；沪深300 日度历史已留存正式库不回退；UI 标注 cadence=monthly | reports/111 探测 |
| S12 | 乐咕对连发请求敏感：约 4 个指数（8 次请求）后 csrf 页面解析失败（AttributeError 'NoneType'）；适配器限速 2s/请求，批量回填按逐指数 + 30s 冷却；自动更新宽基组需分日轮转，勿一次连打 12 个 | 2026-09-05 正式库回填实测 |
| S13 | 申万 `index_analysis_report` 直连：`page_size` 可给到 **50000**（3 页/11.8 万行）；单页 26~55s、3 并发墙钟约 56s；证书链不完整必须 `verify=False`；akshare 50/页路径禁用（2372 页×15s≈10h） | 2026-09-05 实测 |
| S14 | 同花顺 Financial-API Key 只存环境变量 `HITHINK_FINANCE_API_KEY`（用户级已 setx）；适配器必须用 **httpx**（akshare 会 monkeypatch requests.Session.request，`trust_env` 报 TypeError）；QDII（513130/159605）`track_index_pe_ttm_five_year_percentile` 上游恒 null → 如实 unavailable | reports/111 实测 |
| S15 | DuckDB 1.5.5 executemany 含 date/datetime 参数的病理内存问题同样适用于 `index_valuation` 批量写入（11.8 万行卡死）——>1 万行批量一律 pandas register + 单条 INSERT SELECT（D19 泛化） | reports/111 |
| S16 | S1 包装器 preflight 会冻结**本机全部 python 进程状态**：无关 python 服务（如 streamlit）运行/变化会导致"Python process state changed"或 tmp 清理 PermissionError 误报；跑 S1 前需停掉无关 python 应用 | reports/82 §5.4、2026-09-05 实测 |
| S17 | ETF 池三层（用户 2026-09-05 定稿）：industry=申万一级有工具的 26 个、strategy=红利/红利低波/央企红利/A500、market=沪深300/中证500/中证1000/**恒生科技**；无合适工具的申万行业不加入观察；`vd etf seed-pool --apply` 只插入缺失代码、不覆盖用户预算 | `app/core/etf_pool.py` |

## 10. 自动更新子进程崩溃与调度恢复（2026-10-30）

| # | 硬约束/事实 | 出处 |
|---|---|---|
| R1 | 更新子进程可能原生 fail-fast（2026-10-29 实测 `python.exe` + `ucrtbase.dll`，`0xc0000409`/BEX64），此时 Python logger 没有 traceback；排查必须同时看 Windows Application 事件 1000/1001 与 `auto-update-child.log` 最后时间戳。CLI 已启用 `faulthandler`，下次复现应落线程栈 | reports/116；WER AppCrash_python.exe_20261029 |
| R2 | 原生崩溃/控制台被关闭不会执行 `AutoUpdateController.run_once` 的 finally，`auto_update_state.current_stage` 与 `job_logs.status` 会永久停在 `running`。**锁文件是唯一活性权威**：无活锁 + 无进程内 child = 陈旧标记，必须先 `recover_interrupted_auto_update()` 结算，不能仅凭 DB stage 返回 `already_running` | reports/116；app/web/main.py、app/web/api/data_status.py |
| R3 | 启动维护必须**先启动收盘后 scheduler，再执行阻塞的首轮更新子进程**；否则首轮崩溃/挂死会让 scheduler 永远不启动，只能人工重启。`trigger_auto_update` 也必须用“child guard + 活锁”判重，而非只读持久化 stage | reports/116；app/web/main.py |
| R4 | `auto_update_is_due()` 除价格目标日外，还必须把 `retry_list` 中 `retry_count < max_retries` 且 `next_retry_at <= now` 的任务视为 due，否则价格一旦达标，失败任务只能等人工重启才会重试 | reports/116；app/web/main.py |
| R5 | 当前仍未覆盖“子进程活着但永久卡死”（活锁一直存在）；此类情形需要后续 heartbeat/watchdog 或人工处置 | reports/116 残留风险 |
| R6 | `snapshot_publish_gate` 的 90 天新股股本豁免按 `CURRENT_DATE` **滚动**：到期后若 `stock_meta.total_shares/circ_shares` 仍 NULL 即成为违规股。**2026-11-12 起门禁不再一票否决全市场**：`violation_count > max(50, 1% 上市数)` 才 `hard_block` 整批拒绝（怀疑系统性单位错乱），否则违规票进 `quarantined_codes` **逐股隔离**、其余照常发布（`partial + publish_gate_quarantined`，被隔离股旧行保留）。每日自动通道已接上：`_listing_info_gap_codes()` 含股本缺口，`_fetch_listing_info()` 对仍缺失者写 `missing_list(share_capital/source_incomplete)`。**仍然禁止绕过门禁或猜测股本**，补数据必须双源核验（`scripts/rebuild_share_capital.py`） | reports/117、reports/116；`docs/evidence/evidence-share-capital-26-20261112.json` |
| R7 | 价格管道 raw/qfq 成对失败时，旧 retry 在 `extra_json={}` 默认只补 raw，成功后删任务导致 qfq 永久停在旧日期。现价格失败登记 `{"adjust":"both"}`，无显式 adjust 的旧任务也按本地 max 成对增量刷新；只补单边的维护必须显式写 adjust | reports/116；`app/core/update.py::_retry_failed_tasks` |
| R8 | `raw_response_archive_all` 在不同轮转/重建路径下不一定有 `storage` 列；`data_quality` 不得依赖该列。空 payload 检查直接查 active 热表 `raw_response_archive`，否则 summary 每 5 分钟 503 而 readiness 实际为 true | reports/116；`app/core/data_quality.py` |
| R9 | 排查“数据未就绪”必须区分三件事：`minimum_data_readiness.ready`、`indicator_snapshot` 发布门禁、`/api/data-status/summary` 是否 200。2026-10-30 三者分别被 301677 股本、qfq 陈旧、archive 视图 schema 影响；只看页面或只看 auto-update stage 会误判 | reports/116 二次复查 |
| R10 | CNINFO 公告超过 100 页会递归按日期窗口补齐，但递归子查询未传 `progress_cb`；UI 可停在 `100/108` 而子进程仍每约 1.5s 请求。判定“自动更新停止”前必须核对 `auto-update-child.log` mtime 与 httpx 行，不能只看 UI 进度 | reports/116 残留风险 5 |
| R11 | 历史股本有界续传的进度必须由 `update_many()` 回传 done/total；`update_stock()` outcome 本身不含这两个字段，旧实现会让状态页恒显 0/0 | reports/116；app/core/capital.py |
| R12 | 侧边栏“数据就绪”= `minimum_data_readiness.ready` 质量门禁通过，不等于后台没有任务。设计上允许“数据就绪 + 自动更新中”并存；UI 必须同时展示 running 状态，避免把正常更新误判为异常 | reports/116；frontend/src/App.vue |

## 11. 2026-11-12 更新链与发布门禁修复新增约束

| # | 硬约束/事实 | 出处 |
|---|---|---|
| R13 | **lineage 判据按摘要形态，不按适配器白名单**：现行溯源约定是 64 位 SHA256；32 位小写十六进制即 MD5 时代形态，一律计入 `legacy_hash_rows`（如实披露、**不阻断**）；现代 64 位批次缺归档才是真断裂（`batch_archive_gap_rows` → `LINEAGE_INVALID`）。2026-11-12 实测 676,272 行 32 位 MD5 来自 CSMAR/东财历史回填批次，曾用 `adapter_version LIKE 'csmar-%'` 白名单，被 6 个东财批次证伪后收敛为形态判定 | reports/117；`app/core/data_quality.py` |
| R14 | `LINEAGE_INVALID` / `SNAPSHOT_STALE` 等命中 `_SNAPSHOT_BLOCKING_WARNINGS` 时，详情页/自选页**所有快照派生数值被置 null**、筛选 409。排查"页面一片空"必须先看 `warning_codes`，不要先怀疑前端 | reports/117；`app/core/data_quality.py::mask_untrusted_values` |
| R15 | `missing_count` 唯一口径是 `actionable_missing_count()`（`resolved_at IS NULL` 且可行动）。历史上 CLI/更新日志用裸 `COUNT(*)` 报 11,823、网页报 162，差两个数量级；新增统计入口必须复用该函数 | reports/117 |
| R16 | 指标发布门禁 `hard_block` 才会整批拒绝；`ready=True + quarantined_codes` 是**正常态**（逐股隔离）。同一政策也适用于 `minimum_data_not_ready`（输入缺口）：缺口 ≤ `max(50, 1%)` 时逐股排除并发布其余，超过阈值才整批拒绝。新增发布路径必须调用 `snapshot_publish_gate`、尊重 `quarantined_codes`/`missing_input_codes`，并通过 `preserve_codes` **保留被排除股票的上一代快照行** | reports/117；`app/core/indicators/calculator.py` |
| S20 | **适配器错误分类**：`SourceMetadata.error_kind ∈ {none, legal_empty, per_symbol_data_error, source_unsupported, source_unavailable, deadline, truncated_ok}`。只有 `source_unavailable` 计入熔断（未迁移适配器的"error 非空+空数据"兜底计入）；熔断桶 key 是 `adapter:data_type`，跨域互不影响；"源不支持/无数据"**不得写进 `error` 字符串**，改用 `skipped_codes`/`reason` | reports/117；`app/core/adapters/base.py`、`manager.py` |
| S21 | **沪市股本只能靠腾讯回填**：`ak.stock_info_sh_name_code()` 返回**没有**总股本/流通股本列（深交所清单有）。`_fetch_listing_info` 现对缺股本代码用 `qt.gtimg.cn` 字段 `[72]=流通`、`[73]=总股本` 回填（≤50/批，名称一致性 + `circ<=total` 双校验，失败保持 NULL）。沪市新股若长期 NULL，90 天豁免到期即触发门禁 | reports/117；`app/core/adapters/akshare_adapter.py` |
| S22 | **CNINFO 公告截断不再抛异常**：`announcement_general_max_pages` 截断时返回已抓数据 + `truncated=True/error_kind=truncated_ok`，窗口跨度 >1 天走日期二分；截断在 job 里是 `partial` 而非 failed。**分级口径**：财务/分红类别的截断 → `partial`；**general 全文列表的截断只进 `warnings`（带条数与窗口），不把整轮打成 partial**（`update.py` 的 `general_warnings` 段，否则每个公告量大日都 partial、`last_full_success_at` 永久冻结）。页上限 2026-11-22 起为 20 页/窗口（5→20，覆盖 vs 时间折中，边界=只影响非财务公告登记）。遗留事实：非财务公告域只覆盖"最近 3 天前若干页"，游标推进后不回补 | reports/117；`app/core/adapters/cninfo_adapter.py`、`app/core/update.py` |
| C-RETRY | `retry_list.error_kind`（schema v18）：`legal_empty → missing(source_empty)`、`source_unsupported → missing(source_unsupported)`、`skipped_codes` 同理，**一律不占重试次数**；耗尽时 `reason_code=retry_exhausted:<data_type>`。历史上 BSE/无分红新股会以"重试耗尽"的假名义永久进 missing_list，排查时先看 `error_kind` | reports/117；`app/core/update.py`、`app/core/storage/schema.py` |
| P-PRICE | 价格全历史响应改为**攒批单事务写**（8 只 / 20 万行 / 5s 三重上限，失败整批回滚后同一批 items 逐股回退，`try/finally` 收尾 flush）。除权窗口必须**按股票**计算（`xdxr.event_date >= 该股自身最新 raw/qfq 日期`），禁止全市场 `min()`——那会让一只陈旧股把全体拖进全历史重拉（2026-11-12：32→293 只、价格步骤 143 分钟） | reports/117；`app/core/update.py` |
| D26 | DuckDB `memory_limit/threads` 覆盖值必须是**进程级全局**，不能用 `ContextVar`：`ThreadPoolExecutor` 工作线程不继承 ContextVar，会回落 `database.*` 配置并触发 `Can't open a connection to same database file with a different configuration`。Web 进程仍禁止调用 `memory_limit()` | reports/117；`app/core/storage/duckdb_store.py` |
| O-READY | 启动就绪日志必须带 `warning_codes` + `missing_counts`；`GET /api/ready` 未就绪返回 **503**，`/api/health` 只表示进程活着。判断"数据能不能用"看 `/api/ready` 或 `/api/data-status/summary`，不要看 `/api/health` | reports/117；`app/web/main.py` |
| D27 | 隔离表 `INSERT ... SELECT x.*` 会因列序漂移（`ALTER TABLE ... ADD COLUMN storage`）把 BOOLEAN 写进 TIMESTAMP 而整条维护命令失败。所有 quarantine 表写入必须用 `INSERT ... BY NAME` | reports/117；`app/core/data_maintenance.py` |
| C-TZ | **UTC/CN 边界是复发型缺陷**：`data_refresh_state` 标记由 `_mark_refreshed()` 以 UTC ISO 写入、`calculated_at` 从 DuckDB 读回是**无时区**的本地（CN）时间戳。任何"今天/年龄/是否到期"的比较必须先把两边归一到 `CN_TZ`（`_marker_cn_date()`、`build_freshness_metadata()`）。旧实现直接用 `datetime.now(UTC).date()` 相减，北京 00:00–08:00 会差一天：凌晨重跑约 104s 的 universe 全量刷新、刚发布的快照显示 `snapshot_age_days=-1` | reports/117；`app/core/update.py`、`app/web/api/stock_detail.py` |
| O-READY2 | **探针必须同时验证“现在能读吗”与“值可信吗”**：`/api/ready` 只读 readiness 缓存会在更新持锁时假绿（2026-11-13 实测：ready=200 而 health/detail 503）。现为三道闸——真读 `SELECT 1`（`database_unavailable`）、`indicator_trust` 遮蔽（`snapshot_values_masked`）、陈旧度仅披露（`checked_at_age_seconds`/`stale`）。新增任何健康探针都必须自问：缓存会不会压过当前的不可用状态 | reports/118；`app/web/main.py::readiness_probe` |
| P-QFQ | **除权窗口不能用“日期比较”决定是否全量重拉 qfq**：事件晚于价格推进才写入 `xdxr` 时，`event_date >= 该股本地最新日` 恒假 → 前复权历史永久错配且不自愈。必须按“写入事实”触发：market_actions 做 xdxr 事件指纹 diff → 写入 `data_refresh_state.qfq_refetch_pending` → 下轮强制 `qfq start_date=None`，落库成功才清除 | reports/118；`app/core/update.py` |
| P-BATCH | 价格全历史攒批的时限判据必须是“距**上一次 add** 的空闲时间且 ≥2 只”，否则慢抓取（单只 28–635 s）下每次只攒到 1 只 → 退化成逐股事务（2026-11-13 实测 90.9% 落盘为 N=1）。`steps.prices.write_batch_sizes` 是真实批大小的唯一可信证据，不要只看“出现批量写入日志”就判定修复生效 | reports/118 |
| P-STATS | 统计域**逐股指纹不得包含全库聚合量**（如 `COUNT(*) ... WHERE verified`）：`capital_history` 每轮核验 20 只就会让全库计数变化 → 5,580 只指纹全部失效 → 每轮全量重建（实测 285–305 s/轮，占无缺口轮次 65–76%）。逐股输入用逐股聚合 | reports/118；`app/core/statistics.py::_stock_fingerprints` |
| C-STATUS2 | `partial` 的构成必须只含**真降级**：公告 general 窗口截断（设计取舍）不得计入 `truncated`，否则每轮 job 恒 partial、`last_full_success_at` 永久冻结。财务/分红类别截断仍应 partial | reports/118；`app/core/update.py::_announcement_step_result` |
| C-READY-T | readiness 与发布门禁必须共用同一阈值政策：门禁允许 `max(50, 1%)` 逐股隔离时，readiness 不得因 1 只缺口判死（假红）。`share_capital_policy` 字段如实披露 `gap_count/threshold/blocking/quarantined` | reports/118 |
| C-RETRY2 | 重试路径也必须看 `error_kind`：重新抓取若返回 `legal_empty`/`source_unsupported`，必须按真实原因写 `missing(source_empty/source_unsupported)` 并移出队列，**不得**继续递增 `retry_count` 直到`retry_exhausted`。新增数据域的重试分支同样要接 `_settle_non_retryable_error()` | reports/117；`app/core/update.py::_retry_failed_tasks` |

## 12. 2026-11-14 数据包价值兑现审查新增约束

| # | 硬约束/事实 | 出处 |
|---|---|---|
| D28 | **派生指标表不进自动更新 = 会在一个季度内变成死条件**：`indicator_ext` 由 `ExtendedIndicatorBuilder` 仅在 `vd data compute-extended-indicators` 中构建，`_run_incremental_update_flow` 无此步。筛选 SQL 按 `ex.report_date = s.report_date` **严格对齐**，所以 ext 一旦停在旧报告期，55 项扩展字段全部 NULL（2026-11-14 实测：join 只剩 3 只，`free_cash_flow>10亿` 返回 0 只）。新增任何"派生域"必须在同一轮里回答：谁触发重建、多久一次、覆盖率门禁怎么判 | reports/119；`app/core/indicators/extended.py`、`app/core/screening/engine.py` |
| D29 | **手工链清单 —— 已于 2026-11-14 全部接入 `_run_incremental_update_flow`（除 PDF）**：`cashflow_supplement` / `balance_sheet_ext` / `fixed_assets` / `deducted_profit` / `regulatory_metrics` / `indicator_ext` / `etf_prices` / `hk_dividends` 八个步骤，全部带 `data_refresh_state` 水位 + `_refresh_due` 到期判断 + 每轮上限（max_stocks/deadline）+ 失败隔离（只让自己 failed/partial，绝不影响 readiness）。**根因更正**：折旧摊销与报表补充科目的断更像是「续传判据写错」（有任意本源行就跳过，导致跑也白跑），不是单纯没人跑；判据已改为"本源最新报告期 < 目标最新报告期"并有回归测试。PDF 域仍无调度（`pdf_tasks` 恒 0） | reports/120 |
| D30 | **技术三指标依赖 qfq 尾部连续**：`period_return`/`annualized_volatility`/`max_drawdown` 用 `_trailing_contiguous_closes(qfq_close)`，收盘后 qfq 未跟上即整列 NULL，且**补齐后不会自动重算**。2026-11-14 实测覆盖率 17.8%（数据侧 5,578/5,585 只 qfq 完整）。修法：qfq 补成功后标记该股技术输入指纹失效 → 下一轮重算 | reports/119；`app/core/indicators/calculator.py` |
| D31 | **有息负债的"三项全非空"口径是缺陷**：`_calc_interest_bearing_debt` 要求 `short_term_loans`/`long_term_loans`/`bonds_payable` 全非空，而 `bonds_payable` 全市场仅 815 只非空（多数公司本就没有应付债券）→ `interest_bearing_debt` 与 `roic` 覆盖率 11.8%。正确口径：缺 `bonds_payable` 视为 0，只要短期/长期借款至少一项非空即可计算 | reports/119；`app/core/indicators/calculator.py::_calc_interest_bearing_debt` |
| D32 | ~~数据包文档硬编码~~ **已于 2026-11-14 修复（不再成立）**：`scripts/export_data_package.py` 的 README/SOURCES/KNOWN_ISSUES 改为**从本次导出实测结果渲染**（每表行数/列数/最新日期/覆盖率、滞后按交易日历换算、归档段越界自动报），`KNOWN_ISSUES` 常量已删除并有测试断言其不存在；新增 `--verify --pkg` 对账模式（文件完整性 + 逐表行数漂移 + as_of 分域漂移，退出码 0/2/1）。**当前规则：包内每个数字必须来自实测，不许手写常量；每次重导出后跑一次 `--verify` 必须返回 0** | reports/119/120；`scripts/export_data_package.py`；`tests/regression/test_data_package_export.py` |
| D33 | **CSMAR 原始 `.dta` 的元数据列没有导入**：`Indcd/Indnme`（按报告期的行业分类）、`IfCorrect`/`DeclareDate`（更正标记/更正披露日）、`Typrep`（合并/母公司报表类型）都只在原始文件里。这就是 STATUS 里"无行业变更历史"和"`as_reported` 未上线"两个缺口的原料——**不需要再抓数，重跑导入即可**；`Indcd1`（上市公司协会口径）与 S8「只取证监会口径」冲突，用前必须裁决 | reports/119；`.planning/2026-09-17-datapackage-research/csmar_schema.json` |

| D34 | **人工审查结论必须固化成可复跑的 asset health 规则**：`app/core/quality/asset_health.py` 12 条规则（`EXT_PERIOD_MISMATCH`/`FIELD_COVERAGE_LOW`/`FIELD_COVERAGE_DROP`/`MANUAL_CHAIN_STALE`/`SNAPSHOT_PERIOD_GAP`/`STATIC_TABLE_STALE`/`PACKAGE_STALE`/`NULL_REASON_MISSING`/`CROSSCHECK_BELOW_THRESHOLD`/`SCHEMA_DDL_DRIFT`/`ZERO_REFERENCE_ASSET`/`DEDUCTED_PROFIT_GAP`），`vd data doctor` 一条命令跑完（`--fast` 3.5s / 全量 39s，其中跨域一致率核验占 35s）。**新 code 一律先做"披露型"警告，不得直接进 `_SNAPSHOT_BLOCKING_WARNINGS`**（否则一上线就全站遮蔽/筛选 409）；`min_domain_rows=100` 防止小库假 P1 | reports/120 |
| D35 | **筛选指标按"当前报告期实测覆盖率"动态放行**：`EXTENDED_SCREENING_READY` 是静态集合，挡不住数据腐化（2026-11 的 P0 就是 ext 冻结一个月后 55 项全空）。`GET /api/screening/indicators` 现按 `indicator_ext` 最新期覆盖率过滤（阈值 `_EXTENDED_MIN_COVERAGE_PCT=50`），未达标进 `unavailable` 并在前端不展示。**新增任何上界面字段都必须走这条动态门**，静态白名单只作为"候选集" | reports/120；`app/web/api/screening.py` |
| D36 | **DuckDB 大批量写入禁用 `executemany`**（D19 的复发）：`indicator_null_reason` 首版用 2000 行/批 `executemany` + DATE 参数，在正式库上直接 `Fatal Python error: PyEval_SaveThread ... GIL released` 崩溃。已改为 `pandas.DataFrame → conn.register → 单条 INSERT ... SELECT ... ON CONFLICT`。**新增写路径一律用向量化范式**，单批 ≤ 10 万行为宜 | reports/120；`app/core/quality/null_reasons.py` |
| D37 | **空值原因表是稀疏的、按域整域刷新**：`indicator_null_reason` 只写"NULL 且原因已知"的单元格（2026-11-14 首次全量 102,818 行 / 66 个字段），6 类原因 `NOT_APPLICABLE / NOT_COMPUTED / SOURCE_EMPTY / NOT_DISPATCHED / INVALID_DENOMINATOR / MASKED_UNTRUSTED`。**数据修好后原因会随 `replace_scope` 整域清除**（扣非与固定资产两族在修复后自动归零），所以它不是"历史账本"而是"当前诊断"；判定不出时**宁可不写**，绝不猜 | reports/120；`app/core/quality/null_reasons.py`、`null_reason_sweeper.py` |

| D38 | **数据资产必须有"去向"，不是为了用而用**（reports/119 J2 裁决）：每张表/每个域三选一 —— ① 接上（有指标/界面读它）② 进冷库（`cold_archive_split.py` 导出 Parquet + 可回读视图）③ 退役（删）。零引用表登记在 `config/asset_reference.yaml`，**每季度 15 分钟过一遍**，看引用数是否变化、是否需要改去向。注意：不以"用户现在用不用"作为唯一判据，因为"不用"仍然在占主库、进数据包、写进说明书 | reports/119/120；`config/asset_reference.yaml` |
| D39 | **季报不可用的指标要明说，不能留空让人猜**（reports/120 J7）：三季报通常**不披露现金流量表补充资料**，东财只给经营现金流净额、不给折旧摊销明细 → `depreciation_amortization / ebitda / ev_ebitda / leverage_operating / leverage_total / ebitda_to_liabilities` 在 2026-09-30 覆盖率只有 0.1%（上期 99.9%）。处理：标 `SOURCE_EMPTY`（原因"该期补充资料未披露折旧摊销"）并由筛选覆盖率门（<50% 隐藏）拦截；**不要**用半年报折旧做年化去填季报（口径不同）。同类季节性字段还包括 `exchange_gain`（需单独确认源） | reports/120/121；`app/core/quality/null_reason_sweeper.py`、`app/web/api/screening.py` |

## 13. 2026-11-15 第二轮数据修复新增约束

| # | 硬约束/事实 | 出处 |
|---|---|---|
| D40 | **金融股三表必须显式传东财 `companyType`**（银行 3 / 保险 2 / 证券 1 / 通用 4）。不传或按通用模板取数时，东财 F10 对金融股返回空资产负债表或通用模板列，贷款/保险合同负债等专用科目全部取不到——2026-11-15 实测金融股池 119 只（银行 42 + 非银金融 77），补采前 `financial_sector_items` 只有 78 只 / 5,058 行且止于 2025-03-31。**为什么**：这不是限速或网络问题，是接口参数语义问题，重试多少次都拿不到数据 | reports/122 §2.10；`app/core/supplement.py`（`FINANCIAL_SECTOR` spec）、`scripts/fetch_financial_sector.py` |
| D41 | **`income_statement.exchange_gain` 是「公允价值变动收益」，不是「汇兑收益」**（CSMAR `B001301000` / 补充域 `FAIRVALUE_CHANGE_INCOME`）。新浪适配器曾把汇兑收益写进本列且只覆盖约 194 只，造成 2026-09-30 覆盖率假断崖 52.1%→3.5%（5,381 行被误判 `NOT_DISPATCHED`）。**为什么**：口径错会让"公允价值变动收益为零/异常"这类结论整条失真；按本列名直译成汇兑损益的读法一律作废 | reports/122 §4；`config/field_mapping/akshare.yaml:68`、`app/core/adapters/sina_adapter.py:135`、`scripts/fetch_exchange_gain.py` |
| D42 | **qfq 落后于 raw 必须触发强制重算**：价格步骤发现某股 `qfq 最新日 < raw 最新日` 时，必须写 `data_refresh_state['tech_recompute_pending']`，下一轮指标计算**无条件**把该股并入完整重算集合，重算成功后才移出；名单上限 **2,000 只** + TTL **3 天**，同一水位不重复入列。**为什么**：指标重算集合原本只认"本轮真正写入价格的股票"，qfq 补齐那天 raw 已是最新 → 该股永远进不了重算集合，技术三指标永久停在旧值（2026-11-14 实测 `annualized_volatility` 17.8%，手动重算一次即 99.6%） | reports/122 §2.2、reports/120 §5；`app/core/update.py:2372-2380` |
| D43 | **告警必须分级，坏字段必须"字段级遮蔽"而不是整站遮蔽或静默空集**：`asset_health` 每条规则带 `severity ∈ {block, disclose, inventory}`——只有 `block` 进 `level_counts`/`p1_codes`/`warning_codes`/`doctor` 退出码，`disclose` 只披露，`inventory` 是纯清单；结构性可空字段（PE/PCF/派息率/股息率/利差/商誉/杠杆）在白名单里只披露不报警；`block` 级中"某几个字段不可信"走 `indicator_trust()['masked_fields']` → `/api/screening/indicators` 把字段移入 `unavailable(reason=masked_untrusted)` → 引擎命中抛 `MaskedFieldError` = HTTP **409**。**为什么**：旧行为要么一上线就全站 409，要么静默返回空集让用户以为"筛出 0 只"；两者都比"明确告知这几个字段不可信"更坏 | reports/122 §2.3；`app/core/quality/asset_health.py:26-50,90-92`、`app/core/data_quality.py:1327-1374`、`app/core/screening/engine.py:90,976`、`app/web/api/screening.py:862` |
| D44 | **冷库 Parquet 的读写纪律（2026-11-15 已在正式库执行切换）**：**⚠️ 状态更新（2026-11-15 晚）：清单已从 16 张扩到 17 张且全部 `physical_switch: switched`，以 D50 为准；下面“16 张 / `csmar_risk_factors` pending”是同日中途状态，只作过程记录**。冷库 = `scripts/cold_archive_split.py` 导出的静态表 Parquet（**16 张 / 4,552,721 行 / 405.7 MB** ZSTD，`D:d-cold-archive-20261115`，manifest 记行数/sha256/日期范围/消费者）。`financial_sector_items` 因 2025Q2+ 仍在持续补采（2026-11-15 补 582 行）**移出冷库、保留主库物理表**。切换动作：`cold_archive_switch.py --create-views`（16 张表 `ALTER TABLE ... RENAME TO ...__cold_parked` + `CREATE VIEW ... read_parquet`）→ 验证 → `--drop-tables --yes`；现库内是 **16 个 VIEW + 0 张停车表**。**2026-11-15 晚些补登记：清单已从 16 张增至 17 张** —— `csmar_risk_factors`（67,193 行，域 `csmar_disclosure`，消费者 `scripts/export_data_package.py`）已写进 `config/cold_archive.yaml`，带 **`physical_switch: pending`**：已登记、照常导出，但主库仍是物理表（schema.py 里 DDL 还在，`--create-views` 会因残留 DDL 整体拒绝，这是有意的安全门）；导出 + 建视图 + 删表由主控在正式库按顺序执行，该表行数/体积**待导出后实测**。清单状态字段 `physical_switch` 由 `cold_archive_split.load_config()` 校验、由回归用例双向钉住（pending 必须有 DDL、switched 必须没有）。**两个必知坑**：① DuckDB 1.5.5 里**带索引的表不能 ALTER TABLE RENAME**（`Dependency Error: Cannot alter entry ... because there are entries that depend on it`）——必须先 `DROP INDEX`（脚本已内置 `_indexes_on`）；② `CREATE INDEX` / `ALTER TABLE` 打在视图上会抛错，而 `init_duckdb_schema()` 每次启动都跑，所以 DDL 删除清单必须按**收缩后的 16 张**重算（首版 52 处 = 17 表 + 34 索引 + 1 `ALTER`；现为 **49 处 = 16 表 + 32 索引 + 1 `ALTER`**）。其余纪律不变：`DELETE`/`INSERT` 型导入脚本写不进视图（已加 `cold_archive_guard` 退出码 6）；`DROP` **不会缩小** DuckDB 文件（收益等离线重建）；`--verify` 回读校验是回滚依据，parquet 保留即可用 `CREATE TABLE AS SELECT * FROM read_parquet(...)` 恢复。 | reports/122 §2.11；`scripts/cold_archive_switch.py`、`config/cold_archive.yaml` |
| D45 | **A+H 分市场口径只能用腾讯 qfq 的「最后一根」算"当前"值**：`hk_price_daily` 的末根（表列 `source='akshare_hk'`、行级 `upstream='tencent_qfq'`）= 真实收盘，可算当前市值/溢价/股息率；**禁止**用于任何历史时点口径（前复权序列随每次除权整体重算，历史点位不可比）。**为什么**：行情源只剩腾讯可用（东财 push2his 本机断连；新浪 50 s/只 → 203 只要 3 小时；腾讯 1,500 根/只、0.36 s/只、约 2 分钟跑完），而腾讯只提供前复权——把它当历史收盘用会得到错误的历史市值序列 | reports/122 §2.7；`app/core/adapters/hk_price_adapter.py`、`app/core/ah_metrics.py` |
| D46 | **分红除权日必须走结构化源，禁止再走 PDF 解析**（2026-11-15 裁决，取代 `reports/121` 的 PDF 方案）：巨潮 `akshare.stock_dividend_cninfo` 与东财 `akshare.stock_fhps_detail_em` 都**直接返回结构化日期**——巨潮给「股权登记日 / 除权日 / 派息日 / 每 10 股派息」（深历史到 1992-03-20），东财给「股权登记日 / 除权除息日 / 每股派息口径的现金分红比例」但字段更少、无派息日（用于交叉核对或主源不可用时兜底）。因此 `app/core/pdf/dividend_parser.py` + `scripts/resolve_dividend_ex_dates.py` 已**整体删除**，**不得再以任何形式下载公告 PDF 从正文抠日期**；`pypdf` 也不再需要（`pyproject.toml`/`uv.lock` 都不应加它）。**单位硬规则：两家返回的派息比例都是「每 10 股」，落库 `dividends.dividend_per_share` 必须 ÷10**（送股/转增比例同理 ÷10 换算成每股股数）。季度不可用字段见 D39。**为什么**：结构化源给的是官方口径字段，可追到 1992 年；PDF 正文解析要长期维护十几种真实行文形态、命中率仍是概率性的（`reports/121` 实测仅 74%），而单位忘记 ÷10 是 10 倍级数据错误且不会报错 | `reports/123`（结构化源）、`reports/121`（已 superseded）、`reports/122` §2.12；`scripts/fetch_dividend_history.py`、`akshare.stock_dividend_cninfo` / `stock_fhps_detail_em`、D39 |
| D47 | **本机 GIL 崩溃是环境事实，不是代码 bug**：Python 3.12 + 正式库上"长循环 + DuckDB 写入 + HTTP/sleep 混合"会触发 `Fatal Python error: PyEval_SaveThread ... GIL released` 直接杀进程（无 traceback、`try/except` 抓不到）。规避方式固定两条：① **分块/分批 + 抓取与写库分离**——逐块读取→解析→独立事务写（`scripts/import_industry_history.py --chunk-size 50000` 实测 259,035 行 7.2 s、356,728 行 7.8 s）；② 大批量写入一律 pandas `register` + 单条 `INSERT ... SELECT`（D19/D36），长抓取循环单独进程分批提交。**为什么**：一次全量导入若不分块，崩了就得从头再跑；且日志默认被流水线脚本清空，首轮失败原因可能直接丢失 | reports/120 §2.2/§5、reports/122 §2.13；`scripts/import_industry_history.py`、`app/core/industry_history.py:674`（`iter_dta_chunks`）、D19/D36 |

---

## 14. 2026-11-15 结构化分红源与冷库收官新增约束（reports/123）

| # | 硬约束/事实 | 出处 |
|---|---|---|
| D48 | **分红除权日只能走结构化源；两家派息比例都是「每 10 股」；退市股票源覆盖不到，删除前必须先查 `is_listed`**（源与单位的总则已在 D46 裁决，本篇补足“删除前必须查 `is_listed`”的判据与实测证据）。① **源**：巨潮 `akshare.stock_dividend_cninfo`（主源）直接给「股权登记日 / 除权日 / 派息日 / 每 10 股派息 / 送股 / 转增」，深历史可追到 **1992-03-20**（万科）、1993-03-22（方正科技）；东财 `akshare.stock_fhps_detail_em`（备用/交叉核对）字段更少、**无派息日**。`app/core/pdf/dividend_parser.py`、`scripts/resolve_dividend_ex_dates.py`、`tests/regression/test_dividend_pdf_parser.py` 已**整体删除**，**不得再以任何形式下载公告 PDF 从正文抠日期**，`pypdf` 也不再需要（见 D46）。② **单位**：两家的「派息比例 / 送股比例 / 转增比例」**都是每 10 股**，落库 `dividends.dividend_per_share` / `stock_dividend` / `transfer_share` **必须 ÷10**（唯一换算点 `scripts/apply_dividend_history.py::per_10_to_per_share`）。③ **删除纪律**：`deleted_empty`（源无分红记录）与 `deleted_fake` 的"报告期未命中"分支**只在 `stock_meta.is_listed = TRUE` 时**才允许执行；非在市/未知一律进 `kept_coverage_gap` 并改 `reason=source_coverage_gap`；`stock_meta` 读不到时按"全部未知"fail-closed（宁可不删）。**为什么**：结构化源给的是官方口径字段，而 PDF 正文解析要长期维护十几种真实行文形态、命中率只是概率（`reports/121` 实测 74%）；单位漏 ÷10 是 10 倍级数据错误且不会报错；**退市股"源里查不到"≠"没有分红"**——实测 8,467 行隔离记录里 **5,001 行**属已退市股票，反例 600087（`*ST 长油`）隔离区有 12 行真实派息（0.35/0.5/0.3…）而巨潮返回 0 行、东财直接抛异常，按空删除就真的把数据删没了 | `reports/123` §2–§3、§6①；`reports/121`（已 superseded）；`scripts/fetch_dividend_history.py`、`scripts/apply_dividend_history.py`、`tests/regression/test_dividend_history_structured.py` |
| D49 | **现金分红金额未知时绝不写 0**：源把「派息比例」留空（巨潮对 B 股常见）时，只允许回填**其它**空字段（公告日/送股/转增），金额字段一律不写；**插入也按 NULL 写**，绝不写 0。实测 **200541 2007-06-08**：巨潮派息比例 = `NaN`，但分红说明写着「10转增3股派5元」，库里 `dividend_per_share = 0.5` 是**真的**——若把未知当 0 覆盖，就把真值抹成 0。与"回填 NULL"通道的边界：金额通道不碰其它字段、回填通道不碰金额。**为什么**：0 会被下游当成"该期真的不分红"，`ttm_dividend_yield` / 派息率 / 分红融资比会整体偏低且**静默不报错**；本轮按此规则插入时现金口径未知按 NULL 写入 **18 行** | `reports/123` §3.2、§6②；`scripts/apply_dividend_history.py`（`per_10_to_per_share` 前后两处守卫）、`tests/regression/test_dividend_history_structured.py`（200541 实况用例） |
| D50 | **冷库"退役"≠ 删除：`cold_archive_state` + 恢复命令；17 张清单的唯一事实来源是 `config/cold_archive.yaml`**。现状（2026-11-15 收官）：**17 张全部 `physical_switch: switched`**，冷库包 `D:/vd-cold-archive-20261115-r17` = **17 张 / 4,619,914 行 / 411.4 MB**、`--verify-cold` 通过；库内 **17 个 `read_parquet` 视图 + 0 张停车表**（`csmar_fi_t1` 302,959 行、`csmar_risk_factors` 67,193 行仍可查）。退役登记：`config/asset_reference.yaml` 的 **11 条**零引用记录每条带 `cold_archive_state`（含恢复方式），文件头写明"**退役 = 迁冷库（Parquet + 视图），不是 `DROP TABLE`**"。恢复有两条路：`scripts/cold_archive_switch.py` 逆向（`DROP VIEW` + 把 `<表>__cold_parked` 改名回来）或 `CREATE TABLE <表> AS SELECT * FROM read_parquet('<冷库包>/<表>.parquet');`。清单纪律：**张数/表名/切换状态以 `config/cold_archive.yaml` 为唯一事实来源**（报告与 config 注释里的数字都会过期）；`physical_switch` 与 `schema.py` DDL 双向钉住（`pending` 必须有 DDL、`switched` 必须没有），新增表必须**先删 `schema.py` 的 DDL 再切**，否则 `--create-views` 整体 `EXIT_REFUSED`（有意的安全门）。**为什么**：这批表是"写路径永久关闭"的静态归档，退役的目的是把备份/哈希/重建成本移出主库，**不是**"可以删的垃圾"；清单一旦在两处各写一份就必然漂移——本轮 `config/asset_reference.yaml` 文件头就还留着"10 张已切换 / `csmar_risk_factors` pending"的旧表述（正文 `cold_archive_state` 已是 17 张口径）。另：`DROP` **不缩小** DuckDB 文件（高水位），且视图冻结**绝对路径**，搬冷库目录或离线重建后必须重跑 `--create-views` | `reports/123` §4；`config/cold_archive.yaml`、`config/asset_reference.yaml`、`scripts/cold_archive_switch.py`、`scripts/cold_archive_split.py`、`tests/regression/test_cold_archive_split.py` + `test_cold_archive_switch.py` |

---

## 15. 2026-11-16 ROCE 可研与 DSL 括号缺陷新增约束（reports/128）

| # | 硬约束/事实 | 出处 |
|---|---|---|
| C-DSL-PAREN | **DSL 复合表达式的括号在本轮之前根本不能用**：`grammar.lark` 的 `"(" expr ")"` 是 `primary` 的**未命名分支**，而 `_get_parser()` 用 `keep_all_tokens=True` → 该分支解析树含 `(`、`)` 两个 Token（3 个孩子）→ 语法里 `?primary` 的单孩子内联失效 → `Tree('primary')` 漏到上层。后果：`a / (b - c + d)`、`2 * (x + 1)` 在 `multiplicative()` 取 `.unit` 时抛 `AttributeError`（HTTP 500）；`(a + b) > 100` 在 `Validator._check_dimensions()` 同样崩；**绕过校验直接 codegen 会被兜底编译成字面量 `NULL` → 规则能保存、能运行、永远命中 0 只且不报错**。已修：`DSLTransformer.primary()` 解括号 + `CodeGen._gen_expr` 兜底由 `return "NULL"` 改为抛 `UnsupportedPeriodFunctionError`（fail-closed）+ `tests/regression/test_dsl_parenthesis.py` 12 用例。**为什么必须记住**：任何"带括号的复合指标写不出来"的报障都先看这条；且**测试里 `assert parse(...) is not None` 是假绿**（`Tree` 不是 `None`），断言必须用 `isinstance(..., ASTNode)` | `reports/128` §6.2；`app/core/dsl/parser.py`、`app/core/dsl/codegen.py`、`tests/regression/test_dsl_parenthesis.py`、`tests/regression/test_screening_config_limits.py` |
| D51 | **ROCE 口径定稿（2026-11-16 落地，schema v42，`reports/130`）**：**已动用资本 CE = 总资产 − 流动负债**（≡ 净营运资本 + **全部长期资产**，不是 `balance_sheet.fixed_assets`）、**不剔现金**、`EBIT = 利润总额 + 财务费用`、**只吃年报**、主指标 = **最近 5/10 个有效年报 ROCE 的中位数**（原书《我从达尔文那里学到的投资知识》第 2 章的「历史 ROCE」用法），门槛 20%。**三条实测判据（不是口味问题）**：① 只取 PPE 会漏油气资产/长期应收款——中国海油非流动资产 8,003 亿、`fixed_assets` 仅 85 亿，算出来 119% 压过茅台；② 按 `现金−债务` 全额剔现金会让白酒/家电分母穿零——格力已动用资本 −204 亿→+26 亿、ROCE −81%→4,405%，五粮液分母 −54 亿得 −7,194%；③ 三口径全市场对照只有 C 对得上原书标尺：A 中位 9.4%/≥20% 17.6%、B 12.1%/30.9%、**C 6.3%/5.9%**。**不设任何比例护栏**（用户裁决：极值不假、假的是数据源）；CE ≤ 0 时 ROCE 置 NULL 但 `ce` 原值必须保留，`status` 四态 `OK/INVALID_DENOMINATOR/MISSING_EBIT/NO_PRIOR_PERIOD`。正式库：`roce_history` 105,168 行（1989–2025）、快照覆盖 5,460/5,575 = 97.9%、≥20% 仅 309 只（5.5%）。**已知局限（不修）**：不含商誉（原书「先简化处理」）、分母含非经营长期资产、EBIT 用财务费用净额、新股可能只有 1 年年数（用 `roce_5y_years` 暴露） | `reports/130` §2–§5；`app/core/indicators/roce.py` |
| D52 | **字段名直译是本仓高发事故源，加总类公式落库前必须先跑恒等式自查**：`balance_sheet.fixed_assets` 是**固定资产净值（PPE）**，不是会计意义上的「固定资产」栏（= 非流动资产），也不是 `total_non_current_assets` 的别名。2026-11-16 的 ROCE 可研首版就栽在这里（`reports/128`，已 superseded）。**纪律**：任何「A + B = C」形态的复合指标，在写代码前先用真实数据验证恒等式（本次若先跑 `流动资产 + 非流动资产 = 总资产`，5 分钟即可发现映射错）；**比率类指标出现 −8,311%/+21,285% 这种量级时，先怀疑分母口径，不要先加护栏把锅甩给"公司特殊"**——护栏数量是指标口径好坏的体温计（3 条重护栏 → 5 条轻护栏 = 第二次才是真口径） | `reports/129` §1、§8；`reports/128`（superseded） |
| O-DSLEXPR | **不落库也能先算的临时口径**：期末口径 ROCE 可直接写成 DSL 表达式在筛选/自定义指标里用——`(income.total_profit@TTM + income.financial_expenses@TTM) / (balance.total_assets@LATEST - balance.total_current_liabilities@LATEST)`（实测全市场 5,393/5,575 非空、中位 4.5%；**注意**：`reports/128` 里那条含 `fixed_assets` 的写法已作废）。它**没有**平均分母、没有护栏、不区分金融业，只适合临时筛查，不能当作 ROCE 正式口径引用 | `reports/128` §6.1 |
| O-S1-LOCK | **正式库文件在 Web 服务运行期间无法被普通 `open()` 读取**（DuckDB 写连接持有 Windows 共享锁），因此 `scripts/s1-pytest.ps1` 的两个闸都是硬要求不是形式主义：8765 有监听 → `freeze S1`；`conftest.py` 的 `pytest_configure` 会对 `data/` 正式库做 sha256，服务在跑就直接 `PermissionError` → `INTERNALERROR`。**需要跑非门禁的定向 pytest 时**：用 `VD_ENV=test` + 独立 `VD_TEST_RUN_ROOT`/`VD_DUCKDB_PATH`/`VD_SQLITE_PATH` + `--basetemp <runRoot>/pytest-tmp`，并用 `VD_S1_BASELINE_FINGERPRINT=<合成的 wrapper 基线 json>`（`formal_data_root` 必须等于 `path_policy.data_root()`）跳过正式库哈希、`VD_S1_WRAPPER_OWNS_POST=<data_root>` 跳过 post 阶段。**这不等于官方 S1**：服务停掉后仍需补跑一次 wrapper | `reports/128` §6.2 脚注；`conftest.py::pytest_configure/_capture_inner_evidence`、`scripts/s1-pytest.ps1`、`scripts/s1-path-preflight.ps1` |

---

## 16. 2026-11-16 投资大师指标调研新增约束（reports/131）

| # | 硬约束/事实 | 出处 |
|---|---|---|
| I-ROCE5 | **经典比率落到 A 股前，必须先过「分母五问」**：①分母会不会 ≤0（负权益、负营运资本、留存为负）；②分子分母量纲是否一致（每股派息 ÷ 总市值 = 1e-7 这种静默失效）；③原文口径我们用的是哪种（广义「长期资产」还是狭义 PPE）；④护栏是不是拍脑袋的截断（LEAST(x,3.0) 会让 40.6% 样本顶格）；⑤早期年份样本够不够（2/54/70 只也算一个年度点）。`reports/131` 审查出 42 个大师指标里有 **13 个**犯这五类错误中的至少一类 | `reports/131` §4.1、§4.2 |
| I-NULLSEM | **「NULL 是正常的」与「NULL 是缺失」必须分野**：`goodwill`/`trading_financial_assets`/`bonds_payable`/`lease_liabilities` 的大量 NULL 是"公司本来就没有"，可 COALESCE 0；但 `total_current_assets`（金融股）、`financial_expenses`（金融股）、`report_date_close`（当时未上市）的 NULL 是结构性不适用，**COALESCE 0 会造出荒谬值**（银行 NCAVPS ≈ −全部负债/股本） | `reports/131` §3.1 各指标 traps |
| I-FINEXCL | **金融股剔除必须写进 SQL，不能只写在文字陷阱里**：本轮审查发现巴菲特「一美元测试」混入 121 只金融股、质量学派「增量 ROIC」让齐鲁银行(6.79)/江苏银行(5.90) 霸榜榜首。判据统一用 `stock_meta.sw_level1 IN ('银行','非银金融')`，**不要用 `csrc_l1='金融业'`**（同花顺/指南针/大智慧都在里面） | `reports/131` §4.2 P1-a |
| I-MCSTALE | **689009 九号公司 `indicator_ext.market_cap_at_report` 小 10 倍（已复现，2026-11-16）**：`share_capital_history` 该股全链带 `change_reason='unit_rescale:10'`，股本链被 ×10 校正过，但 `indicator_ext` 是校正前构建的，市值 = 收盘价 × 旧股本 → 恰好 1/10（2024-12-31 起 8 个报告期全是 0.1 倍；全库 43,478 观测中仅此 1 只）。连带 `book_to_market`/`tobin_q`/`ev_ebitda` 全部错 10 倍，使它被聂夫总回报率排到全市场第 1 名。**修法**：重跑 `vd data compute-extended-indicators`；**更该补的机制**：股本链变更必须触发 `indicator_ext` 重建（D28 同型） | `reports/131` §4.3 |
| I-GAPS | **数据库 5 个结构性缺口（做指标前先认）**：G1 无 GDP/宏观序列（巴菲特指标做不了，只能改名做「市值/营收」替代版）；G2 无「维持性资本支出」字段（Owner Earnings 只能用总额 capex 或折旧代理，两口径差近一倍）；G3 无信用债收益率/信用利差（格雷厄姆"2×AAA"只能用国债替代，必须改名并标注少 50–150bp 利差）；G4 无 beta/WACC/永续增长率（DCF 无法预计算，只能做参数化工具）；G5 无 gross PPE 原值（Altman Z 的 X3、Ohlson O 的部分变量只能用净值替代） | `reports/131` §5 |

---

## 17. 2026-11-16 ROCE 定稿新增约束（reports/132）

| # | 硬约束/事实 | 出处 |
|---|---|---|
| I-ROCE6 | **公式里的中文术语 ≠ 数据库列名。** 「固定资产」要区分会计科目 `balance_sheet.fixed_assets`(PPE) 与会计学意义的「全部长期资产」——两者差一个数量级：中国海油非流动资产 8,003 亿、`fixed_assets` 仅 85 亿，只取 PPE 会算出 119% 压过茅台。**取 `总资产 − 流动负债` 即取全部长期资产（会计恒等式）** | `reports/132` §1.1 |
| I-CASHX | **「剔超额现金」这类修正项在样本外会变成「制造误差项」。** 书里对印度样本的向上修正，在 A 股（现金占资产 80–90%）会把分母削到零：同花顺剔完只剩 2 亿资本、EBIT 35 亿 → **444%**；重庆啤酒分母变负；海天 90.5%、公牛 80.6%。**判据是用外部标杆校验量级**——书里标杆是 16%/22%/42%/52%，不剔现金版中位 10.3%、上限 68.4%，剔现金版上限 1,632%、45% 公司"达标"。最终**现金计入分母** | `reports/132` §1.2 |
| I-ROCE7 | **ROCE 筛选三条件（`indicator_snapshot.roce_eligible`，schema v45）**：①非金融（`sw_level1` 不含银行/非银金融——实测 121 只金融股中 100 只无 `total_current_assets`、101 只无 `financial_expenses`）；②总市值 ≥ 100 亿；③**有效 ROCE 年数 ≥ 8**（不是「上市满 N 年」：算单年 ROCE 需上一年的资本，上市满 5 年只够算出 4 个值） | `reports/132` §2 |
| I-CALIB | **派生指标必须用三个列位并列，不能只给一个中位数**：`roce_10y_median`（历史成色）+ `roce_5y_median`（趋势）+ `roce_latest`（现况）。10 年中位数会掩盖「历史好现在崩」：智飞生物 36.9%→**−66.4%**、卓胜微 44.1%→−2.6%、五粮液 31.1%→7.3%。反向的「最近才好起来」指纹是 **5 年 > 10 年**（天孚通信 18.5%→25.8%→48.4%，AI 周期） | `reports/132` §4 |
| I-GAP-CASH | **`balance_sheet` 缺「拆出资金」「其他流动资产」两列（待补）**：可筛选池 1,267 只中 **123 只（9.7%）** 的「流动资产 − 已识别经营明细」超总资产 10%（茅台 1,394 亿、汾酒 216 亿）。定稿口径下不影响 ROCE 结果（金融资产本就该留在分母），但**任何「经营性资本」衍生指标在补列前都不能做** | `reports/132` §7 |

| I-FINEFF | **金融股不能用「营运资本型」指标（不是公司不好，是指标不适用）**：119 只金融股只数占 2.1%、**市值占 21.1%**；资产负债率中位 89–93%（存款/保费是原材料）；42 只银行 **40 只缺 `total_current_assets`、41 只缺 `financial_expenses`**。硬算出来的 ROCE 是垃圾：招商银行 ROE 11.5% 但 ROCE **1.7%**（流动负债装着 80% 存款）。**量金融机构用 ROE / PB / 净息差 / 不良率 / 拨备覆盖率 / 新业务价值**，本库 `financial_sector_items` 有 33 个专用科目。排除判据用 `sw_level1`，**不要用 `csrc_l1`** | `reports/131` §11 |
| I-KLIST | **复合指标构建的 10 步检查清单**（从 ROCE 四次返工反推）：①写清指标回答/不回答什么问题 ②每一项→精确字段路径→打印 5 个样本人工核对 ③含加减先验会计恒等式 ④**用外部标杆校验量级** ⑤分母≤0 写 `status` 不设上限 ⑥查字段历史覆盖率与跨年断点 ⑦查特殊行业是否适用 ⑧派生域必须有重建触发**且**快照重发要保全 ⑨筛选用「有效数据年数」不用「上市年限」 ⑩至少三列位（历史中位/近期中位/最新值） | `reports/131` §10 |

---

## 18. 2026-11-17 返工复盘新增约束（reports/133）

这 7 条不是「规范建议」，而是**最近一个月 7 类真实返工机制**的收敛结果
（窗口内 46 commit / 23 次 fix / 60% 的修复在 96 小时内又改同一批文件）。

| # | 硬约束/事实 | 出处 |
|---|---|---|
| E-SSOT | **一个事实只能有一个实现**。任何「缺口数 / 市值 / 可信判定 / 时区 / 单位口径」在第二处再写一遍，就一定会分叉且不报错。实测：`missing_count` 三处（11,823 vs 162）、股本缺口两份 SQL（5 只 vs 26 只）、总市值三套算法（185 只差 >1%，最差 −90%）。发现第二份实现时**先合并成单一函数再改调用方** | `reports/133` §4 R1、`reports/117`/`126` |
| E-STATUS | **状态必须由事实派生，不得由「最后一个写它的人」决定**。禁止 `{**子步骤, status: ...}` 覆盖式合并；`rejected` 必须吸收（`merge_step_status`）。禁止用刷新 `calculated_at` 掩盖陈旧。证据必须带真实读数（「批量写入 N 只/M 行」而非「批量写入」） | `reports/133` §4 R2、`reports/117` P0-2、`reports/118` P1-1 |
| E-DERIVED | **派生列三件套：保全 + 重建 + 校验**。凡不由计算器 staging 产出却住在 `indicator_snapshot` 的列，必须登记 `calculator.derived_snapshot_domains()`（列名 + 重建函数），两条发布路径都走 `_derived_columns_guard()`。**只给一条发布路径打补丁 = 另一条继续清空**（2026-11-17 实测：增量路径仍在清空 ROCE 六列） | `reports/133` §4 R3、`reports/131` K5、D28 |
| E-DENOM | **每次除法过四问**：分母是什么精确字段 / 会不会 ≤0（≤0 只标 `status`，不截断不置空）/ 量级对得上外部标杆吗 / 空值是「数学不成立」还是「没抓到」。禁止用护栏（`if abs(x)>1: None`）掩盖看不懂的数字 | `reports/133` §4 R4、`docs/skills/composite-indicator/SKILL.md` |
| E-TZ | **「今天」永远是北京时间**：只用 `app.core.financial_period.cn_today()` / `cn_now()`。禁止 `date.today()`、`datetime.now(UTC).date()` 参与「距今几天 / 是否到期 / 同一交易日」。本项目已有 4 处私有 UTC+8 实现（其中一处退化成 `date.today()`），并产出过两次事故 | `reports/133` §4 R5、`reports/117` §7、`reports/118` §3 P2 |
| E-ISOLATE | **不得一票否决**：单只股票的问题只能隔离该股票（`max(50,1%)` 或占比 ≥1/3 才整批拒绝），并在响应里披露 `quarantined_codes`。熔断桶 `(adapter, data_type)`，只计「源不可用」；空适配器链是环境问题（`source_unavailable`）不是「该票没数据」 | `reports/133` §4 R6、`reports/117` P0-1/P0-4、`reports/118` P1-5 |
| E-GUARDSELF | **守卫必须自证**。新增探针/门禁/清扫逻辑时，必须同时写「它会失败」的反向用例，否则会得到 `reports/125` 那种「写了但从未生效」的守卫（哨兵精确匹配恒假 + `catch` 吞异常，空转 24 小时）。判据：一条经验没有变成脚本/测试/规则/ADR 之一，就只是文档 | `reports/133` §5、`reports/127` |

**门禁落点**（随 S1 一起跑）：`scripts/check_rework_guards.py`
（RG1 时区单源 / RG2 状态合并 / RG3 前后端单位一致 / RG4 派生域登记），
测试 `tests/regression/test_rework_guards.py`、`tests/regression/test_snapshot_derived_columns.py`。


---

---

## 19. 2026-11-18 大师指标科目复审新增约束（`reports/139`）

> 来源：把 `reports/131` 的 42 个指标逐个对到 `information_schema`（65 表 / 1,306 列）+ 4 轮只读探针。
> 这 5 条都是「不报错但会算错」型的域事实，做任何一个新指标前先看。

| # | 硬约束/事实 | 出处 |
|---|---|---|
| D53 | **`xdxr.songzhuangu` 存的是「每股送转比例」，不是「每 10 股送转 X 股」。** 用 1,056 个真实事件把 `share_capital_history` 的实际股本跳变与 `songzhuangu` 对照，分 6 档全部满足 `股本跳变 / (1 + songzhuangu) = 1.000`（按 `/10` 解释则 1.09–4.26）。因此 `app/core/indicators/extended.py:401` 的 `SUM(songzhuangu)/10.0` 是错的（实测 `bonus_share_ratio_3y` 三年累计最大只有 0.353，口径对的话 10 送 10 三年应为 3.53）；凡是「送转还原」（一美元测试的 ΔRE、Akre 腿 2 的稀释判定）都必须直接用 `1 + songzhuangu` | `reports/139` §2.4/§5.3；`.planning/2026-11-18-indicator-feasibility-audit/out/db_evidence_round2.md`（songzhuangu_unit_test）与 round3（songzhuangu_buckets） |
| D54 | **指数收盘价在 `index_valuation.extra` 的 JSON 里，不在价格表。** `price_daily_qfq`/`price_daily_raw` 只有 6 位股票代码、**0 行指数**；指数点位在 `index_valuation.extra->>'index_close'`（实测 128,074 行 / 43 个指数 / 122,695 行有值 / 2005-01-31 → 2026-11-17，由 `index_valuation_adapter.py:174/302` 写入）。任何「相对强度基准 / 市场温度计」类公式写成 `price_daily_qfq` 都是**语义空壳**（不报错、恒空） | `reports/139` §2.4；probe round2 `index_close_available` |
| D55 | **`market_cap_at_report` 的逐年覆盖率决定了「历史分位」能不能当结论**：2013 **52.7%** / 2014 **51.4%** / 2015 54.0% / 2017 59.4% / 2020 66.1% / 2022 82.1% / 2025 **96.8%**（`indicator_ext` 年报口径）。缺失不是随机的——缺失组中位总资产 5.04 亿 vs 有值组 36.62 亿（系统性漏掉小盘股）。所以**任何用市值做 10 年历史中位数/分位的指标，必须同时披露逐年覆盖率并标注「偏大盘」**；个股层面只有 689009 曾错 10 倍（已于 2026-11-17 修好，全库 `bad_rows=0`） | `reports/139` §6；probe round1 `market_cap_coverage_by_year` / `p0_9_scan` |
| D56 | **金融股判据目前有三份手写实现，且集合不一致**——`indicators/roce.py:183`（`COALESCE(sw_level1,'') NOT IN ('银行','非银金融')`）、`supplement.py:507`（`sw_level1 IN (...)`，无 COALESCE → NULL 行掉出）、`quality/null_reason_sweeper.py:752`（多一个 `'保险'` + `csrc_l1 LIKE '%金融%'` + 股票名 LIKE 兜底）。实测 `stock_meta` 里 `sw_level1` 只有 `银行`(42)/`非银金融`(79)/`NULL`(751，其中仅 52 只仍在市)，**没有 `保险` 这个 level-1 值**（保险是 `sw_level2`）。新增指标一律抄 `roce.py:183` 的写法（带 COALESCE），禁止裸 `NOT IN` | `reports/139` §2.3/§5.1；probe round1 `sector_mix`、round2 `financial_sector_counts` |
| D57 | **「空值是数学不成立还是没抓到」的落点只有 5 个 reason code**：`indicator_null_reason` 现存 129,934 行 / 69 字段；`NullReason` 枚举**已定义 6 类**（`SOURCE_EMPTY/NOT_APPLICABLE/INVALID_DENOMINATOR/NOT_DISPATCHED/MASKED_UNTRUSTED/NOT_COMPUTED`），但库里**只观察到 5 类**——`MASKED_UNTRUSTED` 要有可信门禁真正触发过才会写，属正常。新指标（尤其把「分母 ≤0」改成 `*_status` 而不是置空的那些）**必须同时把 status/reason 落库**，否则下游无法区分「这家公司数学不成立」与「这条数没采到」——这正是 R4-4 的判据 | `reports/139` §2.3/§6；probe round2 `null_reason_v2`；D37 |

| I-STRICTWIN | **多窗口中位数必须「拿满 N 年才出值」**：`MEDIAN(x) FILTER (WHERE rn <= N)` 会让只有 7 年数据的公司得到一个挂在 `roce_10y_median` 名下的 7 年中位数——名实不符。正确写法是 `CASE WHEN COUNT(x) FILTER (WHERE rn <= N) >= N THEN MEDIAN(...) END`。代价是覆盖数下降，换来「叫什么就是什么」 | `reports/133` §1.1 |
| I-ROCE8 | **ROCE 窗口与门槛（schema v46）**：窗口 5/10/20 年三档（`roce_5y_median` / `roce_10y_median` / `roce_20y_median`），每个窗口严格取满；筛选门槛 `roce_eligible` = 非金融 + 市值≥100亿 + **有效 ROCE 年数 ≥ 5**（书里「过去 5 到 10 年甚至更长时间」的下限）。20 年档需 21 份年报，只有 2006 年前上市的能进 | `reports/133` §3 |
| D58 | **`balance_sheet` 的 7 个补采科目（schema v48）的口径与恒等式**：`fixed_assets_gross`(原值)/`accumulated_depreciation`(累计折旧) 来自**新浪**（东财无这两列），其余 5 列（`other_current_assets`/`receivables_financing`/`long_term_payables`/`investment_property`/`development_expenditure`）来自**东财 by_report**。硬恒等式：**原值 − 累计折旧 − 固定资产减值准备 = `fixed_assets`（净值）**，600519 FY2025 残差 3.8e-06 元 —— 所以「用净值代原值」可以从 `approximate` 升级为 `exact` 的勾稽依据就是这个式子。三个坑：①原值/累计折旧在 **Q1/Q3 恒 NULL**（只在半年报/年报有），不许按季度用；②东财 `CURRENT_ASSET_OTHER` 与 `OTHER_CURRENT_ASSET` **不是同一科目**（实测 9 期同时非空），只能取后者、禁止 COALESCE；③银行/保险模板没有「累计折旧」列，脚本用「原值 − 净值」推导（152 行），这类行不是真值。**⚠️ 两源口径不同：禁止用「原值 − 累计折旧」反推 `balance_sheet.fixed_assets`**。全市场实测 16.07 万行里**精确闭合只有 55.0%**（88,401 行）、**库内更小 44.4%**（71,372 行）、库内更大 0.55%（888 行）、原值≤折旧 9 行 —— 说明新浪的「固定资产原值/累计折旧」覆盖范围比库内 `fixed_assets`（纯 PPE 净值）更宽。**处置**：①要原值就用 `fixed_assets_gross`、要净值就用 `fixed_assets`，两者各自独立、不得互推；②任何「用净值代原值」的公式一律标 `approximate`；③原值<净值的个别行两列置 NULL（`repair_bs_subjects_implausible.py`，只清银行 + 明显不可信的）。另：北交所老代码（43x/83x/87x，245 只 / 1.8% 行）新浪不发这 7 列，属免费源限制。**`restricted_cash`（受限货币资金）与 `inventory_provision`（存货跌价准备）补不了**——只存在于附注，东财 319 列零命中、新浪缺列，禁止走 PDF 解析（D46） | `reports/139` §11.4；`scripts/fetch_balance_sheet_subjects.py`；`.planning/2026-11-18-indicator-feasibility-audit/out/source_probe.md` |
| D59 | **`index_valuation.extra->>'index_close'` 不等于「沪深300 日线」**：122,699 行有值，但 **96% 是申万行业指数**（SW8010xx 等 31 条，**日频** 5,039 点/条）；`000300` 只有 **265 个点（月末，密度 5.1%）**。所以：①相对强度/行业对比可以用申万行业指数；②**beta 不能用 000300 日线**（只有月末点，250 日窗口只剩 ~12 个观测）；可用的替代是「全市场市值加权代理 `CAPW_MARKET`」（近 36 个月对沪深300 β=1.045 / r²=0.870）或等权 `EW_MARKET`（会把 β 系统性压到 ~0.59 倍）。`stock_beta` 表的 `benchmark`/`source`/`confidence` 三列必须如实标注用了哪一级 | `reports/139` §11.4；`scripts/compute_stock_beta.py`；`data_beta-build.md` |
| D60 | **宏观与债券曲线两个新域的口径（schema v48；AA 档 2026-11-18 补齐）**：`macro_series` —— GDP 存**亿元**（`unit='CNY_100M'`）、CPI 存百分数（`cpi_yoy` = 同比%、`cpi_mom` = 环比%、`cpi_index` = 「上年同月=100」的指数，**它不是百分数**，直接当百分比用差 100 倍）；`bond_yield_curve` —— 曲线三类来源：①`bond_china_yield` 给 **国债/商金债AAA/中短票AAA**；②**AA/AA+ 走 `bond_index_general_cbond(「市场隐含评级AA[+］信用债指数」,「平均市值法到期收益率」,「3-5年」/「1-3年」)`，2013-01-04 起 3,468 天**（写库 10,404 行，`scripts/fetch_credit_spread_aa.py`）；③国债没有同款指数接口 → 「3-5 年段」用库内国债 3Y/5Y 均值插值近似。AA(3-5Y)−国债 利差：最新 **77.46bp** / 近一年中位 69.2bp / 全历史中位 185.68bp。实测：国债 10Y 与 `treasury_yield_curve` 同日期 4,966 天 \|差\| 中位 0.0bp，商金债AAA−国债 10Y 近 1 年中位 **28.32bp**（全史 94.30bp） | `reports/139` §11.4；`scripts/fetch_macro_series.py`、`scripts/fetch_bond_yield_curve.py`；`data_macro-bond.md` |
| D61 | **分析师一致预期只能「从现在开始打点」，且源数据有重复行**：`ak.stock_profit_forecast_em` 只返回**当期快照**（无历史、不可回补），因此建 `analyst_forecast_snapshot`（主键 `snapshot_date+stock_code+fiscal_year`）并接进自动更新（每天最多 1 次，失败只 partial）。源端内部 6 页拼接重叠 → 2,802 行里 **442 只股票重复**（载荷逐字相同），去重后真实覆盖 **2,360 只 = 42.3%**（此前按行数报的 50.3% 是虚高）。**越晚开始打点，历史越短**——这是唯一一个「今晚不做就永远没有」的数据 | `reports/139` §11.4；`app/core/analyst_forecast.py`；`data_forecast-snapshot.md` |
| D62 | **当前 DuckDB schema 版本 = 48**（v47 = `roce_10y_min`/`roce_10y_cv`；v48 = `balance_sheet` 7 个补采科目 + `macro_series`/`bond_yield_curve`/`stock_beta`/`analyst_forecast_snapshot` 四表）。新增这四张表目前只有「抓取脚本」一个消费者，**按 D38 必须在下一个指标落地时接上真实消费方**（GDP→#8、CPI→#34/#41、AAA 利差→#3/#5/#35、beta→#24、一致预期→#13），否则按零引用表处理 | `reports/139` §11.4；`app/core/storage/schema.py` |
| D63 | **机构持仓域 `institution_holdings`（schema v49）**：唯一实现 `app/core/institution_holdings.py` + `scripts/fetch_institution_holdings.py`（默认 dry-run / `--yes` 单写者锁 / `--resume` / 逐期逐股隔离）。**数据源必须绕过 akshare 的 `stock_report_fund_hold`**：东财 `dataapi/zlsj/list` 返回 **22** 个字段，akshare 1.18.81 按 **23** 列名套用，导致从第 3 列起整体左移一格并丢掉 9 个字段（含 `FREESHARES_RATIO`）——「股票代码」列里出现市值就是这个原因。直接读具名字段即可，`FREESHARES_RATIO` 是**百分数**、`TOTAL_SHARES` 是**股**。口径：6 类持有人（基金/QFII/社保/券商/保险/信托）、**2005-06-30 起**、全市场 × 1 期 = 1 次请求（11 页/约 6 秒）。正式库已回补 432,523 行 / 86 期 / 5,934 只、失败 0。首个消费方 = #17 CANSLIM 的 I 项（必须过滤 `ratio_of_float IS NOT NULL`）；按 D38 已登记零引用条目 | `reports/139` §11.4；`app/core/institution_holdings.py`；`.planning/2026-11-18-indicator-feasibility-audit/out/inst_holdings_probe.md` |

| I-RANKMODE | **横截面排名不再铺扁平字段，改为条件级 `rank_mode`**（方案 C，`reports/142`）：历史上为 155 个可排名指标各铺 6 条派生字段（`{指标}_market_rank` / `_industry_percentile` / `_sw2_rank` …）= **930 条**，与 DSL 的 `rank()/rank_industry()/percentile()/zscore()` 重复实现，且把主指标淹没（ROCE 独占 21 条）。现在条件里写 `{"field":"pe_ttm","rank_mode":"sw2_rank"}`，后端 `expand_rank_modes()` 在**入口**展开成 `pe_ttm_sw2_rank` 复用既有 SQL。**旧字段名写法仍兼容**。新增派生字段前先自问：DSL 函数是不是已经能表达了？ | `reports/142` §4 |
| I-PCTUNIT | **`*_market_percentile` 存的是 0–1 小数，单位必须声明为 ratio 而不是 plain**：声明成 plain 时用户输入 90（想要前 10%）会命中 0 只且**不报错**。已实测：`>=0.9` → 399 只，`>=90` → 0 只 | `reports/142` §3 |
| I-RANKCOL | **排名口径必须三处同时可用：条件 / 排序 / 结果列**（`reports/142` §9）。只做条件会让用户"能筛不能看、能筛不能排"——筛出"行业内 PE 前 3"却无法在表格里显示它是第几。结果列用 `{"field": "pe_ttm", "rank_mode": "market_rank"}` 对象写法，由 `normalize_columns()` 归一化成 `pe_ttm_market_rank`；`run()` 与 `validate_rule_fields()` 必须同源 | `reports/142` §9 |
| I-EXPANDREC | **入口改写函数必须递归所有 dict/list 值，不能只认 `rules` 键**：`validate_rule_fields()` 的入参是 `{"conditions": {...}}`，只递归 `rules` 会让 `conditions` 里的新字段绕过展开与校验——非法值不报错、合法值只是碰巧通过，典型的「能跑不能存 / 能存不能跑」（CB-09 同型） | `reports/142` §6 |

---

## 20. 2026-11-22 股票黑名单新增约束（用户需求，无报告）

> 来源：用户要求「个股详情页一键加入黑名单，之后该股票的内容不必再出现在任何地方」。
> 落地前做了三方代码调研（前端展示面 / 后端出口 / 自动更新选股），三条裁定与四条域事实如下。

| # | 硬约束/事实 | 出处 |
|---|---|---|
| U1 | **黑名单只做读侧隐藏，绝不停止抓取**（用户 2026-11-22 裁定）。数据仍完整保留在 DuckDB、自动更新照常抓取。**不许**用 `is_listed = FALSE` 冒充黑名单：`init.py::_fetch_stock_universe()` 每天用全市场清单 UPSERT，会硬写回 `TRUE`（`update.universe_refresh_interval_days` 默认 1 天） | `app/core/blacklist.py` 模块 docstring；`app/core/init.py:286` |
| U2 | **「停止抓取」的四个硬阻碍**（所以它比「继续抓」贵得多，别再重复评估）：① 没有单一收口点，约 10 处独立 SQL 读 `stock_meta.is_listed IS TRUE`；② `auto_update_is_due()` 的判据之一是「`retry_list` 里有到期行」，停抓后历史失败行会持续拉起整轮更新并转成永久 `missing_list`；③ **readiness 是逐股阻断、不是阈值制**——停抓后市场进入新报告期，该股 `snapshot_period_alignment` 对不上即 block → `/api/ready` 503，整站受影响；④ 指数成分聚合/行业市值占比等全市场聚合会静默漂移 | `app/core/update.py:4633/1010/2096/2116/3615`、`app/core/indicators/calculator.py:535`、`app/web/main.py:213-227`、`app/core/data_quality.py:1263-1325` |
| U3 | **黑名单是用户态数据，存 SQLite 不存 DuckDB**（`stock_blacklist`，SQLite schema v20）。DuckDB 单写者+排他锁，自动更新子进程持锁时 Web 侧写会失败；`watchlist`/`screening_rules`/`manual_overrides` 同惯例。副作用：排除条件必须由 **`app/core/blacklist.py::exclusion_condition()` 唯一生成**，因为黑名单代码集合与 DuckDB 表不在同一库、无法 JOIN | `app/core/storage/schema.py`（`SQLITE_SCHEMA_V1` + v20 迁移）；`AGENTS R1` |
| U4 | **排除必须加在筛选「基础池条件」里，不能只在前端过滤**。排名/分位是跑在 `base_pool` 之上的窗口函数（`RANK()/PERCENT_RANK()/PARTITION BY csrc_l1`），池里剔掉才能让排名分母与页面可见行数自洽；只在前端 `filter` 会让百分位仍是全市场口径，且 CSV 导出在服务端生成、仍会漏出黑名单股票 | `app/core/screening/engine.py::_build_sql` / `_get_base_pool_size`（**两处条件组装必须同改**）；`engine.py` 的 `COUNT(*) OVER () AS _vd_total` |
| U5 | **单股出口的守卫只能挂一次**：`stock_detail.py` 把全部 `/{stock_code}/...`（15 个，含 PDF）挂在带 `Depends(require_visible_stock)` 的 `_guarded_router` 上，`/search` 留在 `_public_router`（它没有路径参数，走 SQL 层过滤），末尾用聚合 `router` 对外暴露单一 include 入口。**新增单股端点一律写进 `_guarded_router`**，否则结构性测试（从 OpenAPI 反向枚举全部 `{stock_code}` 路径逐个请求）会红 | `app/web/api/stock_detail.py`；`tests/regression/test_blacklist_visibility.py::test_every_stock_path_endpoint_is_behind_the_guard` |
| U6 | **黑名单股票在被屏蔽期间不删除 `watchlist` 行**，只在其上过滤显示：移出黑名单后原来自选的股票自动回来（避免"加错黑名单顺手毁掉自选"）。同理，已保存筛选结果（`screening_results.result_json` 冻结 JSON）与 CSV 导出在读取时用 `filter_rows()` 内存剔除 | `app/web/api/watchlist.py::list_watchlist`；`app/web/api/screening.py::get_saved_result` / `export_csv` |
| U7 | **「数据状态」页的重试/缺失列表按用户裁定保持照常显示**（那是运维诊断面，不是研究面）。因此 `/api/data-status/*`、`/api/readiness`、`summary` 的 `missing` 代码清单**故意不含**黑名单过滤——排查数据问题时黑名单股票必须仍然可见 | 用户 2026-11-22 裁定（详见第四问） |

| I-LABELCN | **用户可见指标名必须含中文，且后端 `display_name` 与前端 `label` 不能各说一套**（`reports/143`）：复合指标曾显示纯英文 `dividend_financing_ratio (DSL v2)`——后端 `label` 拼英文、前端 DSL 分支只读 `label`，把后端的中文 `display_name` 和前端 `FIELD_LABELS` 全绕过了。修复后优先级固定为 `display_name` → `fieldDisplayName` → label → 字段名；无中文名的复合指标统一加「复合指标」前缀。**新增字段时必须同步补中文名**，否则界面出现「资产负债表 · accumulated_depreciation」半英文。现有基础字段中文覆盖 156/156 | `reports/143` §1 |。**2026-11-16 二次复发**：把参数化字段收成基名后又漏了 4 个基名的中文名；且首版守卫测试自身有盲区（只枚举 185 个、漏 `EXTENDED_SCREENING_READY` 55 个、`STAT_FIELDS` 整段跳过）——**守卫的枚举口径必须等于实际暴露面**，否则「测试全绿但界面英文」。修法：按 `list_available_indicators` 的每个 append 站点逐一对齐，并用**变异测试**验证守卫有牙齿（删掉某条中文名必须让测试变红）
| I-COUPLING | **「同一指标 + 参数后缀」是过耦合的信号**：排名曾铺 930 个字段（155 指标 × 6），改条件级 `rank_mode` 后归零。2026-11-16 全量审计仍有 **61 个**：统计域 40 / 国债期限利差 9 / CAGR 6 / 均线 6。判据——**纯参数**（期限、年数）应收成条件参数；**需要跨值比较或有元数据审计要求**的（均线、PRD §10.7 统计域）应保留字段 | `reports/143` §2 |
| I-COMPOSE | **新需求优先用 DSL 复合指标，不要新增字段**：`app/core/dsl/` 已支持 `rank()`/`rank_industry()`/`percentile()`/`zscore()`/`normalize()`/`TTM`/`YoY`/`QoQ`/`CAGR`/rolling/lag。发布后自动出现在指标下拉且带版本与哈希锁定，筛选条件可直接引用（实测 `pe_rank = rank(pe_ttm)` 可用）。想筛「ROCE 高但 PB 低」→ 发布 `roce_5y_median / pb_mrq`，不必让后端加列 | `reports/143` §4 |

| I-PARAMCOL | **「同指标 + 参数后缀」必须收成基名 + 参数下拉，且遮蔽要向上传播**（`reports/143` §7）：`div_yield_spread_*`(9) 与 `*_cagr*`(6) 已收为 4 个入口。三条硬要求——①`expand_rank_modes()` 按**参数→排名**顺序展开（`div_yield_spread`+10y+market_rank → `div_yield_spread_10y_market_rank`）；②基名不是真实列，缺参数必须报「需要指定 xxx（可选：…）」而不是「未知字段」；③**遮蔽必须传播**——具体列被判 `masked_untrusted` 时基名也要进 `unavailable`，否则用户能选中一个注定 409 的入口 | `reports/143` §7.1–§7.2 |
| I-GRIDAUTO | **CSS Grid 自动放置 + v-if 控件 = 轨道错位事故**（`reports/143` §8.1）：条件行有 6 条轨道但子元素数量随 v-if 变化，参数下拉缺省时「排名口径」掉进 88px 轨（文本区 27px，只显示 1 字 + 省略号），第 7 个元素还会掉进 40px 序号轨。**修法**：轨道数按最大子元素数定，每个控件显式 `grid-column`，小屏断点里再解除。**另**：naive-ui `n-select` 的 `consistentMenuWidth` 默认 `true`，弹层宽度锁死为触发器宽度——窄下拉必须加 `:consistent-menu-width="false"`，否则「点开也看不全」 | `reports/143` §8.1 |
| I-SORTUI | **排序不属于筛选规则，放在结果页点表头**（`reports/143` §10，2026-11-16 用户裁定）：筛选侧已删「结果排序」区块。曾经排序写在 `rule_json` 里有三个看不见的副作用——①改排序 = 新建规则版本；②服务端 `ORDER BY` 在 `LIMIT 5000` **之前**，换排序会改变「留下哪 5000 家」；③严格模式下排序字段会被并进 strict 断言、可能过滤掉行。**筛选只负责选公司，排序只负责显示**。结果页每列 `sorter: 'default'` 为纯前端排序；导出走服务端顺序，界面已加提示 | `reports/143` §10 |
| I-SORTVER（历史） | 排序曾混进规则版本（PRD §12.5/:764 把排序定位在结果页，属实现漂移）；已于 2026-11-16 按用户裁定改为结果页表头排序，详见 I-SORTUI

## 21. 2026-11-22 多会话同仓并发：机器级环境事实（`reports/144`）

> 来源：两个 AI 会话在**同一个工作树、同一个分支**上并行开工，一次踩出 6 类冲突
> （`reports/144` §2）。规则本体在 `AGENTS W4`/`W8`，命令清单在
> `docs/skills/multi-agent-safe-session/SKILL.md`。这里只登记**硬事实**——
> 哪些东西 worktree 隔不开、哪个判据是硬编码的，免得后人当 bug 改掉。

| # | 硬约束/事实 | 出处 |
|---|---|---|
| E8 | **`git worktree` 只隔离「文件 + 分支」，隔不开「端口 / Python 进程表 / 正式库」。** 端口与进程表是机器级的；`data/` 是 gitignore 的，**worktree 里根本没有**（只读正式库要走绝对路径，跑回归走 `s1-pytest.ps1` 的临时库不受影响）。因此：①一个 worktree 一个 `web.port`，绝不两个会话抢 8765；②**全量 S1 天然机器级串行**，只能由主控跑一次 | `reports/144` §3 表；`config/default.yaml:6` |
| E9 | **S1 包装器的 preflight 是机器级判据，会被「别人的进程」拖红**，三种报错各有出处：`Python process state changed during formal capture`（`scripts/s1-path-preflight.ps1:329`，采样窗口内 Python 进程集合变了）、`Value Dashboard service is listening on 8765 - freeze S1`（同文件 `:362`）、`Process(es) survived into After capture`（存活进程不一定是本会话派生的）。**看到这三条先查是不是别人在跑，不要绕过** | `reports/144` §2 C3/C4；`AGENTS W6` |
| E10 | **`s1-path-preflight.ps1:362` 的 8765 是硬编码，而 `web.port` 可配——这是故意的**。改成读配置会让「别人把服务起在 8766 读正式库」漏网。宁可误拦不可漏拦；同一段还有正式 DuckDB 写锁检查作第二道腿 | `reports/144` §6 T3（**不动**） |
| E11 | **`.planning/` 是 gitignore 的**：任何写在里面的会话产物、租约、计划，**新克隆看不到、另一台机器看不到**。所以「文件租约」若要跨机生效，不能只落 `.planning/`（这也是 `reports/144` §6 T1 未做的原因之一）；反过来，会话结论必须回写 `docs/` | `AGENTS` 工作区边界；`.gitignore` |
| E12 | **提交边界要自洽**：`73bb498..3726e5f` 连续 5 个提交里 `app/web/api/screening.py` 都 `import app.core.blacklist`，而该模块到 `79f15e4` 才入库 → 这 5 个提交 `git archive` 出来直接 `ModuleNotFoundError`（HEAD 自洽，只影响 `git bisect`/考古）。**新建模块必须先进库，再让别人 import** | `reports/140` §14.5；`reports/144` §2 C2 |
| C10 | **多会话开工自检有执行器了**：`uv run --locked python scripts/session_preflight.py`（`AGENTS W4` 的执行器，退出码 2 = BLOCK）。查 4 项：脏工作区 / 额外 worktree / 别人的 pytest / 8765 被占。**fail-open**：探测本身失败只降级 WARN，不锁死 | `scripts/session_preflight.py`；`tests/regression/test_session_preflight.py` |
| C11 | **编码事故有门禁了（RG10）**：仓库内 `.py/.md/.vue/.ts/.yaml/.json/…` 不得含 `U+FFFD` 替换字符。零误报——它不是任何人会故意敲进源码的字符；**要演示乱码请用文字 `U+FFFD` 描述，别留字面字符**（本文件与 `reports/144` 都踩过这个自伤）。扫描跳过 `.venv`/`node_modules`/`dist` 等 | `scripts/repo_integrity_guards.py::check_no_replacement_char`；`AGENTS W1` |

| I-DATE0101 | **CSMAR 一个批次把「上年年报」写成「次年 01-01」，全库 44.9 万行**（`reports/146`）：8 张表（`csmar_balance_items` 71,858 / `report_metadata` 71,860 / `csmar_income_items` 71,525 / `cash_flow_activity` 62,370 / `cash_flow_indirect` 62,369 / `csmar_cashflow_items` 62,367 / `csmar_financial_items` 45,725 / `financial_sector_items` 1,014），全部同一批次 `8c165b0a…`（2026-09-18）。**证明方向**：与上一财年 12-31 三列全同 81.0%，与同年 12-31 全同 **0**。危害：`ORDER BY report_date` 的「最近一期」把 `2024-01-01` 当新年度期次、LAG/TTM 多伪期次、覆盖率重复计数（`cash_flow_indirect` 有 25.5% 是这类行）。**修法**：`app/core/report_periods.py` 单一实现 + `scripts/repair_nonstandard_report_dates.py`（默认 dry-run，`--apply --confirm-write` 才写，改期后强制重算 `report_type`）；写库守卫 `SupplementFetcher._drop_nonstandard_periods()`。全库 dry-run：可删重复 418,455 + 可改期 30,633 | `reports/146` §1 |
| I-QTRDEP | **季报 EBITDA 是「源确实没有」，而且小分母百分比会骗人**（`reports/146` §3）：曾有 agent 用「CSMAR 03-31 覆盖率 98.09%」推翻「季报不披露补充资料」，复核发现分母只有 **33 行**（2023-03-31 全库 33 行 / 上市 5,580 家 = 0.6%），CSMAR 的 03-31 合计 5,823 行覆盖 24 年、平均每期 **242.6 只**。**结论**：A 股一季报/三季报绝大多数不披露现金流量表补充资料 → 折旧摊销拿不到 → EBITDA 算不出来，**修不了**；处理方式是回溯到最近可算期（半年报/年报）并在界面标「2026 半年报口径」。**教训：看到覆盖率百分比先问分母** | `reports/146` §3 |
| I-ELIGNULL | **`roce_eligible` 留 NULL = 双向漏筛**（`reports/146` §5）：`SNAPSHOT_UPDATE_SQL` 聚合左表曾是 `roce_history`，无 ROCE 历史的股票（新股/金融股）在 agg 里没有行 → `UPDATE ... FROM` 碰不到 → 停在 NULL。`WHERE roce_eligible` 与 `WHERE NOT roce_eligible` **都不命中**，用户按「非金融·市值≥100亿·有效年数≥5」筛会静默漏公司（正式库曾 115 行）。修法：左表改 `indicator_snapshot` + `COALESCE(n5,0)`。**任何"布尔筛选标志"都不许留 NULL** | `reports/146` §5 |

| I-LEASE | **正式库写入必须持跨会话租约**（`reports/147`）：`scripts/session_lease.py`（O_EXCL 原子抢占 + PID 存活优先/年龄兜底 + TTL 1800s 续租 + 排队登记）、`scripts/run_db_maintenance.py`（抢锁→逐步执行→每步续租→finally 释放）、`session_preflight.py` 第 5 项检查（别人持有 → **BLOCK**）。租约在 `<data>/.locks/db-write.lease.json`。抢不到就 `enqueue`，禁止忙等/强拆 | `reports/147` §2–§3 |
| I-WTPL | **链接工作树可以共用正式库**（`reports/147` §4）：`path_policy` 额外接受 `shared_data_roots()`（`git rev-parse --git-common-dir` 的父目录/data），否则 worktree 会话永远无法跑维护脚本。**踩坑**：`subprocess.run(text=True)` 在 Windows 按 cp936 解码，中文路径变 mojibake → 必须显式 `encoding="utf-8"` | `reports/147` §4 |
| I-PARTIAL | **部分期次补采会删掉其他期次**（`reports/147` §6，我造成的真实事故）：`delete_scope='all'` 旧实现只按 `stock_code` 匹配 → 补 `2025-06-30` 一期时把 5,575 行 2025 年报 + 5,579 行 2026 半年报一起删了。修法：删除范围加 `t.report_date IN (SELECT DISTINCT report_date FROM _supp_rows)`。**补采前后必须核对各期行数**；`csmar_*_items` 是冷归档 parquet **视图**（不可删、也不该删） | `reports/147` §5–§6 |
| I-PRELIST | **「早于上市日的报表」是误报，一行都不能删**（`reports/146` §11）：5,387 只的上市前数据**紧邻上市日**（招股书口径）、断档 >3 年的 **0 只** → 不存在主体错配。434 只「上市前 ≥10 年数据」是**未上市股份公司同样报送年报**（厦门银行/上海银行/中金公司），`001267 汇绿生态` 是**重新上市**。按「早于上市日就删」处理会误删 61,405 行真实历史。**注意**：`stock_meta.listing_date` 是「本次上市」日期，不是「公司有多久财报」；按用户口径「上市时间 ≤5 年先筛掉」用它是对的，但别拿它当数据长度指标 | `reports/146` §11 |

| I-MERGED | **修复必须合进用户运行的那条分支，否则等于没修**（`reports/148` §1）：工作树里改完就报"已修复"是错的——用户看的是主检出。判定方法：在**主检出**里 grep 修复特征串（如 `_EXTENDED_COVERAGE_LOOKBACK`），命中才算真的生效；再直接调一次 `list_available_indicators` 复核数字 | `reports/148` §1 |
| I-NASELECT | **naive-ui `n-select` 的兜底选项会绕过中文解析**（`reports/148` §2）：value 不在 options 里时 `Select.mjs` 的 `wrappedFallbackOptionRef` 会合成 `label = String(value)`，而 `render-label` 拿到的就是这个兜底 label —— 于是历史字段名原样显示英文（截图 `roce_10y_median_i…`）。**修法**：给下拉加 `:fallback-option="v => ({label: fieldDisplayName(String(v), String(v)), value: v})"`。另：数据侧要在载入规则/草稿时把 `<base>_<mode>` 迁移成 `{field: base, rank_mode: mode}`，`POST /draft` 不校验字段是脏名的长期温床 | `reports/148` §2 |
| I-UNAVLIST | **「不可用」列表不能混入"改名"字段**（`reports/148` §1.3）：`dividend_yield → ttm_dividend_yield` 是同一指标换列名，算进「当前报告期不可用指标」会让这个列表**永远清不空**且语义错误。前端按 `reason==='deprecated'` 拆出单独提示行 | `reports/148` §1.3 |
| I-RE2ZS | **DuckDB 的 RE2 `\s` 不覆盖全角空格与 NBSP**（2026-11-23 实测）：`regexp_replace(name,'\s+','','g')` 对 `五　粮　液`（U+3000）和 NBSP **不生效**，必须用 **`[\s\p{Zs}]+`**；而 `'\u3000'` / `'\x{3000}'` 写法在 RE2 里直接 `InvalidInputException: invalid escape sequence`。四种空白（单空格/双空格/U+3000/NBSP/混合 tab）逐条实测过 | 本会话 p1-search |
| I-NAMESPACE | **`stock_meta.name` 内嵌空格**（数据源原样带过来）：全表 265 行带空格、**上市（搜索可见面）36 行**，真值形如 `'五 粮 液'`、`'万  科Ａ'`。直接 `name LIKE '%五粮液%'` **永远匹配不上** —— 用户报的"五粮液搜不到"就是这个。修法：**查询侧**归一（不改库数据）。另：`pinyin` 是从原始 name 生成的、**同一缺陷原样继承**（`'wu liang ye'`），裸 `OR pinyin LIKE` 会原样复现该 bug | 本会话 p1-search |
| I-SQLNOW | **`tests/support/seed.py::sql_now()` 是 SQLite 语法**：它生成 `datetime('now','±N unit')`，而 **DuckDB 没有 `datetime()` 函数**（`CatalogException: Scalar Function datetime does not exist`）；DuckDB 侧只能表达裸 `CURRENT_TIMESTAMP` / `CURRENT_DATE`，任何 `CURRENT_* ± INTERVAL` 都**不可收敛进工厂**。这是"看着能用、跑起来炸"的陷阱 —— 判"可收敛"之前必须**真跑一次** | 本会话 p6-seed-audit |
| I-STRIXEMPT | **严格可信模式的字段级 strict 血缘只覆盖极少数字段**：实测 114 个可筛选字段里**只有 2 个**有 strict 血缘 → 对**计算/派生字段**（`roce_10y_median` 0 条血缘、`total_market_cap` 33.8 万条全是 `derived_calculator`/approximate）要求抓取血缘是**范畴错误**，会让严格模式下 112 个字段**永久空集**。修法（fail-closed）：**注册表豁免**（`derived_snapshot_domains()` + `EXTENDED_COLUMNS` + `DERIVED_FIELDS`）+ **生产者证据**（全部来自 `derived_calculator`）+ **输入下沉**（SQL 派生字段的输入列仍逐个校验）；**零审计记录 ≠ 派生**，否则删血缘就能绕过 strict | 本会话 p4-strict-screen |
| I-SORTSTR | **naive-ui `sorter:'default'` 会按字典序排数值**：它两边都是 string 时走 `localeCompare`，而历史筛选结果行是 `json.dumps(default=str)` 存的 → `total_market_cap` 变成 `"1.2e+11"`，**按市值排序是错的**；number 与 string 混比还直接 `return 0`（**点了没反应**）。修法：显式数值比较器 + `@update:sorter` 跟踪方向（naive-ui 是"比较结果 × 方向系数"），空值恒排末尾 | 本会话 p3-rankmode |

## 22. 2026-11-20 个股研究的数据引用口径（`reports/153`）

> 背景：宇通客车（600066）研究中，AI 把库字段直接喂进定价模型，被用户当场抓出 3 处口径问题。全部属于"数字没错、引用方式错"，比算错数更危险。

| # | 约束 | 依据 |
|---|---|---|
| D53 | **`ttm_dividend_yield` 是严格 TTM，不是年度口径**：实现为「价格日前 12 个月内已除息的分红合计 ÷ 当日收盘」（`calculator.py::_calc_dividend_yield_and_spreads` L1560-1600）。它会**把落在窗口外的中期分红挤掉**——宇通 2025-09-11 的 0.5 元中期分红，在 2026-11-19 价格日下已出窗，于是 TTM = 2.0 元 → **6.31%**；而同花顺等厂商用**年度口径** 2.5 元 → **7.62%**，差 1.31pct。**报告里出现股息率必须同时给两个口径并声明用于决策的是哪个**；拿 TTM 值套「股息率 + g」这类需要"年度分配能力"的模型属口径失配 | `reports/153` §1.2 |
| D54 | **分红描述必须区分「年度对应」与「实际付现」**：宇通 2025 年度分红 55.35 亿 ÷ 2025 年 OCF 32.0 亿 = **0.58**（赚的够不够分）；但 2025 自然年实际除息 33.21 亿 ÷ 32.0 亿 = **1.04**（当年没动存量）；动用存量的是 **2026-05-15 那笔 44.28 亿**（÷ 32.0 = 1.38）。**把「年度对应口径」说成「当年靠存量垫」= 归错年份** | `reports/153` §1.4 |
| I-PRICEASOF | **报告里的"现价"必须与结论同一天，且落在最新交易日**：宇通 v1 用 2026-11-19 的 31.71 写"现价"，而结算日是 11-20（收 32.81），导致 PE 12.89 vs 13.34、PB 4.91 vs 5.09 全线偏低 3.5%。**凡基于价格的结论，先 `select max(trade_date)` 再取价**，并把日期写进正文 | `reports/153` §1.1 |
| I-2NDSRC | **公司行为（分红/投资/回购）必须追到公告原文，不得引用投资者提问或二手转述**：v1 里"1 亿美元基金"来自互动平台投资者提问，虽然后来证实真实（临 2026-045，Lochpine Green Fund I, LP，占 2025 年末总资产 2.2%），但当时未核验就写成了"信号转向"。**先找公告编号，再下定性结论** | `reports/153` §1.5 |
| I-RATEREAD | **读 `indicator_snapshot` 的利差列时必须逐列核对，别按顺序猜**：`div_yield_spread_10y = 4.7317` 反推 10Y = 1.5753%，与 `treasury_yield_curve` 一致；本次误读成 1.18%（那实际是 **1 年期** 1.173%）。**用 `select *` 打印列名，不要靠位置数** | `reports/153` §1.6 |


## 23. 2026-11-22 「好公司」反面推导的四条规则（李大霄）

> 用户提问：他除了「黑五类」，还讲「做好人、买好股、得好报」和一整套好公司标准，
> **能不能从好公司的反面反推坏公司特征？** 能，且这批比「黑五类」更适合做黑名单——
> 「黑五类」量的是**估值贵不贵**（会均值回归），「好公司」标准量的是**公司本身好不好**
> （更接近身份型）。已落成 4 条预置规则（`goodcompany.*`），与黑五类 5 条合计 **9 条**。

**他的好公司口径（按时间，全部有出处）**

| 时间/出处 | 说了什么 |
|---|---|
| 2012-08-25 证券时报网《优质公司有四标准》 | ①领导人是正直好人不是骗子；②为社会持续创造财富而非毁灭财富；③善待股东/员工/社区/自然；④被抛弃时价格远低于价值 |
| 2017-01 华夏时报 | 「九个字：做好人、买好股、得好报」；投资是优秀品质的大比赛 |
| 2018-01-29 界面（**他本人署名**）《如何从全球投资者角度审视蓝筹股》 | 8 个角度（规模/行业地位/成长性/全球扩张…）；**最优质 = 分红稳定、现金流扎实、资产负债表干净** |
| 2024-07 上海证券报退休访谈 | 「上市公司要**真实**，真实而不造假的上市公司才是保护散户最重要的基础」 |
| 2024-09 / 2026-01 微博财经之夜 | **好公司五个维度：对客户→对员工→对股东→对社会→对自然，顺序重要，第三才是对股东**；「掏钱 vs 塞钱」：掏钱=减持+没完没了增发融资，塞钱=持续现金分红+回购注销 |

| # | 硬约束/事实 | 出处 |
|---|---|---|
| U16 | **反推的落点只有 4 条能量化，其余是数据缺口**（诚实划界）：对股东（分红/融资）✅、资产负债表干净 ✅、现金流扎实 ✅、不造假（代理）✅；**对客户/对员工/对社会/对自然 ❌（无客服、员工流动、环保处罚、社区数据）**、**领导人是否正直 ❌（无诚信/处罚表）**、**大股东减持 ❌（无减持事件域）**、**回购注销 ⚠️（有 `buyback_events` 表但未进 `indicator_snapshot`，规则引用不到）** | `app/core/floating_blacklist.py::GOOD_COMPANY_INVERSE_RULES` 上方注释 |
| U17 | **`revenue_yoy_fy` 不在筛选白名单**（只是 `indicator_snapshot` 的物理列，`SNAPSHOT_COLUMNS` 里没有）。首版「伪成长股」误用它，规则一执行就 `ValueError: 未知筛选字段`——而当时 29 个测试全绿，因为**没有任何用例真正执行过预置规则**。现改 `revenue_yoy`(TTM 同比)，并补两道守卫：①静态——每条预置规则引用的字段必须落在 `SNAPSHOT_COLUMNS ∪ EXTENDED_COLUMNS ∪ NORMALIZED_FIELDS ∪ METADATA_COLUMNS ∪ STATEMENT_FIELDS` 里；②实跑——9 条规则逐条 `evaluate_rule()`。两条都验证过「注入坏字段会红」 | `tests/regression/test_floating_blacklist_rules.py::test_every_preset_rule_field_is_selectable` / `test_every_preset_rule_actually_executes` |
| U18 | **「上市满 N 年才评判」是相对年限，靠 `pool.min_listing_years`**（与 `max_listing_years` 对称）。「掏钱型」与「长期不分红」都设 `min_listing_years=5`：上市不满 5 年融资一次是正常发育，不能算"掏钱"；5 年 ≈ 一个完整融资-回报周期。**绝不写「2015 年之后上市」这类绝对口径**（回扣 U9） | 同上；`ScreeningEngine.run(min_listing_years=…)` |
| U19 | **小盘股规则按用户裁定改为默认开启**（原话「小盘股我是不会碰的」）。但 note 里必须保留风险提示：小盘是**风格因子不是质量问题**，2013–2015 创业板牛市、2019–2021 中小盘都打过这条的脸。改默认值时**不要**顺手删掉这条提示 | `PRESET_BY_KEY["lidaxiao.small_cap"].enabled_by_default`；`test_small_cap_is_enabled_per_user_ruling` |
| U20 | **「资产不干净」用 OR 不用 AND**：高商誉（`goodwill_ratio≥20%`）与高杠杆且利息还不动（`debt_ratio≥80%` ∧ `interest_coverage<2`）是**两种不同的病**，要求同时成立会漏掉绝大多数真实风险。已知误杀：银行/保险/地产 `debt_ratio` 天然 90% 上下，会被大面积命中 | `goodcompany.dirty_balance_sheet` |
| U21 | **「纸面利润」是造假的代理信号，不是造假证据**：`cf_to_net_profit<0.5` ∧ 净利润>0 ∧ 净利润同比>0 只说明「利润在涨、账上没钱」，要定性得再去 `indicator_ext` 看应收/存货周转。已知误杀：垫资扩张期公司、银行保险（现金流定义不同） | `goodcompany.paper_profit` |
| U23 | **「掏钱型」按上市年限分两档**（用户 2026-11-22 裁定）：上市 **5–10 年**要求分红融资比 **≥50%**；上市 **10 年以上**要求 **≥100%**（「从股东那里拿的东西要少于你送给股东的」）；**上市不满 5 年不评判**（时间太短）。实现方式是两条互不重叠的规则 `goodcompany.cash_sink_5_10`（min=5,max=10）/ `goodcompany.cash_sink_10plus`（min=10），因为一条规则只能有一套条件。想让「不满 5 年一律回避」，改「次新股」的 `max_listing_years` 1→5 即可，不必新增规则 | `app/core/floating_blacklist.py`；`test_cash_sink_is_split_by_listing_age` |
| U24 | **分红口径 = 总量，不看连续性**（用户 2026-11-22 最终裁定，推翻了本文件先前两版：「分红年份覆盖率」与「最近连续分红 N 年」**都作废**）。用户原话：「连续多少年分红什么都说明不了，分的钱很少也不好说吧。我只关注总体，不太看重细节，只要它是愿意给股东钱的就行。」→ 规则 `goodcompany.no_cash_back` **已退役**（登记在 `RETIRED_PRESET_KEYS`，启动时连同其浮动成员一并从已装库清掉——因为 `ensure_preset_rules()` 只插不删，光从代码里删对已装库不生效）。总量口径由「掏钱型」两档全权覆盖 | `app/core/floating_blacklist.py`；`test_dividend_continuity_rule_is_retired` / `test_retired_preset_is_purged_from_existing_database` |
| U26 | **「次新股」门槛 = 上市不满 5 年**（用户 2026-11-22 裁定：**「1 年还是太短了，我不希望买这么新的股票」**）。李大霄原话换算出来是 1 年（2013-12-31 距 2014-12 首次提出 342 天 ≈ 0.94 年），**规则里写 5 年**——1 年是「次新属性消失」的市场共识节点，5 年才够看清一轮完整经营与分红记录（与「掏钱型」门槛对齐）。两者不矛盾：1 年来自他的口径，5 年来自用户的风险偏好。实现仍是 `pool.max_listing_years=5` 的**相对年限**，绝不写绝对日期 | `lidaxiao.newly_listed`；`test_newly_listed_is_five_years_per_user_ruling` |
| U27 | **分红融资比 100% 是「性质分界线」，不是可调的阈值**（用户 2026-11-22 强调：「分红融资比必须大于100%。大于100%的意义是：它给股东的，比它从股东手里拿的还多。这个意义远大于 99%，就像你能达到 100 分的意义，并不是你比 99 分多了 1 分。」）。所以：**上市 10 年以上要求 `> 100%` 严格成立**（条件写成 `ratio <= 100` 即拉黑，正好 100% = 给的和拿的相等，不等于「给得更多」，同样不合格）；**上市 5–10 年容忍到 50%**（用户：「10 年以内达不到100%，我可以理解；10 年以上都分不到100%，这个我是不可以原谅的」）。**任何情况下不得把 100% 下调成 80% 之类的"务实值"** | `goodcompany.cash_sink_10plus`；`test_ten_year_cash_sink_requires_strictly_above_100_percent` |
| U28 | **黑名单规模不是问题，漏掉雷才是**（用户 2026-11-22 定调，推翻本仓库此前的「规则太多」顾虑）。用户的论证：①能投资的公司是少数，满足二八甚至九一定律；②筛掉八九十只里即使有一两只十倍股，「错过了就错过了」；③但放宽会多放进 10–20 个雷，而**本金损失不可逆**；④剩下那 10–20 只好股票里同样有十倍股，只需要抓住抓得住的；⑤小市值/过去业绩差的公司增长空间高，但「全部加起来能不能打过茅台这一只」——不对称收益结构决定了应该严格筛。**因此：不要以「黑名单太大」为由建议放宽任何阈值**；评估规则时唯一的判据是「它是否挡住了会永久损失本金的公司」 | 用户口述裁定（无报告）；`docs/skills` 无 |
| U29 | **「借贷分红」按证监会《上市公司监管指引第 3 号》原文三要素量化**（2026-11-22 新增规则）。监管原文：「对**资产负债率较高且经营活动现金流量不佳、存在大比例现金分红情形**的公司保持重点关注，防止对企业生产经营、偿债能力产生不利影响。」→ 三个条件一一对应：① `debt_ratio >= 0.60`（**监管未给数字，60% 是本仓库判断值**）；② `cf_to_net_profit < 1`（现金流没利润多 = 不佳）；③ `payout_ratio >= 0.80`（**80% 有官方出处**：沪深交易所对拟上市企业「大额分红」红线＝三年累计分红/净利润 > 80%）。外加 `income.net_profit > 0` 保证分母为正。**更严写法**：③→`>=1` 且 ②→`<=1` 时数学上严格蕴含「分红 > 经营现金流」。注意本规则**未排金融股**（银行分红该用资本充足率单独看）；要排在最前加 `is_financial = 0` | `goodcompany.debt_funded_dividend`；`test_debt_funded_dividend_mirrors_regulatory_three_elements` |
| U25 | **`is_financial` 是筛选层可用的 0/1 布尔列**（`1`=金融股：银行+非银金融+csrc 兜底），由 `base_pool` 里调用 `sector.is_financial_sql("m")` 生成，**不在 DSL 里手写第二份行业判据**（AGENTS R1）。杠杆类规则必须先过 `is_financial = 0`：银行的「负债」是存款、保险的是准备金，那是原材料不是杠杆，用同一把尺子会把整个行业先天判死刑（用户 2026-11-22 要求）。新增：`METADATA_COLUMNS['is_financial']` + `screening-format.ts` 中文名（`test_indicator_label_coverage` 会拦） | `app/core/screening/engine.py`；`test_balance_sheet_rule_excludes_financials` / `test_is_financial_field_comes_from_sector_module` |
| TODO-LATESTDIV | **✅ 已作废（2026-11-22）**：原待办是「新增 `latest_div_year` 让『最近连续分红』能保证最近一年也分了」。但用户随后裁定**分红连续性整个不看**，只保留总量口径（分红融资比），所以这个字段不需要了。留此条目只为防止有人翻到旧讨论又去做一遍 | `reports` 无；用户口述裁定 |
| U22 | **「小盘股」按市值划，不按股数划**（用户 2026-11-22 质疑后改）。李大霄原话确实是股数（2016-12-08 证券之星：「流通股本小于 5 亿股，且自由流通股本小于 1 亿股」），但这套判据有缺陷：**股数 ≠ 估值**（1 亿股 × 1 元 = 1 亿市值，1 亿股 × 100 元 = 100 亿市值），而他的论点是「小盘股比大盘股**贵**」；更关键的是**股数门槛本身也是绝对口径**——2015 年「5 亿股」≈75 亿市值，今天同名门槛对应的市值完全不同，与「2013-12-31 之后上市」同型，都会随时间漂移。现规则 = `total_market_cap` 的**全市场百分位 ≤ 0.30**（相对口径，永不失效）。门禁：`test_small_cap_uses_market_value_not_share_count` 断言条件里不得出现 `circ_shares`/`total_shares` | `app/core/floating_blacklist.py` PRESET_RULES 第 1 条；`tests/regression/test_floating_blacklist_rules.py` |

---

## 22. 2026-11-22 浮动黑名单（规则库）：语义与口径裁决

> 用户裁定：维护一个大的规则库，讨厌的规则都加进去；对整个库筛选，符合的全部拉进
> **浮动黑名单**；每次更新比较，**有进有出**。彻底拉黑不参与比较；浮动拉黑由规则直接完成，
> **前端不提供任何手工入口**——想让某只股票浮动拉黑，去改规则。
> 实现：`app/core/floating_blacklist.py` + SQLite schema **v23** + 自动更新最后一步。

| # | 硬约束/事实 | 出处 |
|---|---|---|
| U8 | **两套黑名单的分工不容混淆**：`stock_blacklist`（彻底）= 人手工点详情页、身份型事实（造假/退市/审计非标）或"我就是不想看"，**只由人增删**；`blacklist_rules` → `blacklist_floating`（浮动）= 规则算出来的特征型判断，每轮重算。读侧排除的唯一入口是 `blacklist.excluded_codes()`（并集），求值路径必须用 `blacklisted_codes()`（只含彻底） | `app/core/blacklist.py`；`app/core/floating_blacklist.py` |
| U9 | **规则里禁止绝对日期/裸年份**（用户明确要求）。原始口径来自李大霄「黑五类」，他 2015-07-27 那版把两条写成绝对时间：「次新股 = 上市日期在 **2013-12-31** 之后」「伪成长 = **2011**>30%、**2012**<20%、**2013**<10%」——这两条 2016 年就再也匹配不到东西。换算准则：**把绝对时点折算成「距他说话那一刻还有多久」，落成相对年限/相对形态**。具体：2013-12-31 距 2014-12-08（首次提出）342 天 ≈ **0.94 年**、距 2015-07-27 573 天 ≈ 1.57 年 → 取整 **1 年**（`pool.max_listing_years=1`）；三年逐级下滑 → **CAGR3 ≥ 20% 且 最新年报同比 ≤ 10%**（成长断崖）。门禁：`test_floating_blacklist_rules.py` 扫**可执行部分**（conditions/pool）不得含 `YYYY-MM-DD` 与 `(19\|20)xx`；`note` 里保留日期是故意的（那是推导说明） | `reports` 无（用户口述裁定）；`app/core/floating_blacklist.py::PRESET_RULES`；`tests/regression/test_floating_blacklist_rules.py` |
| U10 | **「题材股」只能是代理口径，且我们库里没有概念/题材标签表**（已核：`indicator_snapshot` 无概念列、无题材域）。李大霄本人也没定义"哪些股票算题材"，只给了"题材概念股中总市值<50亿"。代理 = **故事溢价 + 投机度 + 他给的市值门槛**：`total_market_cap<50亿` ∧ `turnover_rate≥5%`(20日均换手) ∧ `pb_mrq≥3` ∧ `roe<5%`。**已知误杀**：真成长股放量初期会命中。**默认关闭**。不要改用公告热词表——那会过期，等于再造一张每年要维护的关键词表 | `app/core/floating_blacklist.py` PRESET_RULES 第 5 条 |
| U11 | **浮动黑名单求值时，基础池只能剔「彻底拉黑」，绝不能剔浮动成员**。否则上一轮已浮动的股票会被规则自己筛掉 → 每轮全部"退出"→ 名单在进/出之间反复抖动。实现开关 `ScreeningEngine.run(pool_excludes_floating=False)`，且**两条分支（批量化 `_BatchedSqlite` 与单开连接）必须同口径**——第一版只接了其中一条，反向用例当场抓出（AGENTS R3 同型坑） | `app/core/screening/engine.py::run`；`test_second_run_does_not_self_exclude`（注入 bug 会红：`assert 2 == 0`） |
| U12 | **规则命中数超上限 = 不更新该规则名册，而不是静默少拉黑**。`MAX_RULE_MATCHES=5000`（与引擎 `MAX_RESULT_ROWS` 对齐）；`result["truncated"]` 为真即抛 `RuleTooBroadError`，该规则本轮标 `error` 并保留上一轮名册。理由：截断方向是"少隐藏"，属静默漏放 | `app/core/floating_blacklist.py::evaluate_rule` |
| U13 | **规则求值失败不许清空名册**：单条规则抛错时保留它上一轮的名册（report 里记 `kept_previous`），整步异常时本步骤 failed、库里原样不动。一次坏规则不该让用户已接受的浮动黑名单全部赦免 | `floating_blacklist.refresh`；`test_broken_rule_keeps_previous_roster` |
| U14 | **默认开关是裁决，不是随手**：次新/绩差/伪成长 **默认启用**；小盘股 **默认关闭**（风格因子不是质量问题，2013–2015 创业板牛市、2019–2021 中小盘都打过脸）；题材股 **默认关闭**（proxy 误杀最高）。预置规则**幂等灌入且不覆盖用户改动**（`rule_key` 唯一 + 只插缺的） | `PRESET_RULES.enabled_by_default`；`test_seeding_does_not_overwrite_user_edits` |
| U15 | **浮动黑名单的 TTL 是「规则里只用滚动/当期指标」的副产品，不是独立机制**。换手率是 20 日滚动、PB/ROE 每期重算 → 热度退了、业绩好了，下一轮自然不命中 → 自动出名单。**一旦有人在规则里写绝对日期，TTL 就消失了**（回扣 U9） | `docs/skills/...`；U9 门禁 |

---

*变更记录：2026-09-01 创建（从 reports/61/75/77/81/84/86/92/96/97/98/99/100/101/102、STATUS、config、代码核验聚合）。2026-09-01 体检修复：T3 关闭、E6 补充直连回退。2026-09-02 技术债补全：T1 评估、T2 冷归档恢复 CLI、T4 隔离分红审计 CLI、T5 东财单次探测 CLI、T6 旧单位规则审计 CLI、T7/T8/T9 核验关闭；按窗口删除重建回滚快照并清理约 70GB 旧产物。2026-09-03 性能修复：E7 更新为统一 8GB/2线程/preserve=false 且 Web 进程禁止 memory_limit()；新增 D10（auto-update status 只读 + CLI skip_if_current）、D11（重量摘要 TTL 300s）。2026-09-04 冷归档分区复审：新增 D17（外部 Parquet 按年分区规范，reports/109）。2026-09-04 港股分红域：新增 S10（datacenter ≤2 req/s、禁 push2）、D18（hk_dividends schema v20/映射与写纪律/融资仍缺失）、D19（executemany date 参数内存病理与向量化写入范式）、D20（轮转记账失控与修复），D5 schema 版本更新为 20。2026-09-05 多指数 ERP + ETF 工作台：新增 S11-S16（乐咕月度与限流、申万 50000/页并发、THS httpx/QDII null、向量化批量泛化、S1 进程冻结），schema 版本 D5 更新为 23。2026-10-20 自动更新变慢事故：新增 S18（陈旧不整段重拉）、D24（全量替换 DELETE 约 10s+/股与正式库高水位）、C-LOG（长任务 CLI 必须配置 INFO 日志）；见 reports/114。2026-10-20 分红批量/S1 提速：新增 S19（TDX 逐股建连 5s 必须批量）、D25（读锁属主判定）、C-S1（S1 固定开销来源与最小成本）；见 reports/115。2026-10-30 自动更新原生崩溃恢复：新增 R1-R5（WER/faulthandler 取证、running 标记结算、scheduler 前置、retry due、活进程卡死残留）；二次复查追加 R6-R10（301677 股本门禁、价格 raw+qfq 成对 retry、archive 视图无 storage、readiness/快照/summary 三态区分、CNINFO 100 页递归进度盲区）；UI 复查追加 R11-R12（历史股本 done/total、就绪与更新中可并存）；见 reports/116。*。2026-11-12 自动更新日志复盘修复：R6 升级为分级门禁+自动补股本通道；新增 R13-R16（lineage 形态判据、遮蔽语义、missing 口径、发布门禁分流）、S20-S22（适配器错误分类、沪市股本腾讯回填、公告截断语义）、C-RETRY、P-PRICE、D26、O-READY、D27；见 reports/117。*。2026-11-13 复审二轮：新增 O-READY2、P-QFQ、P-BATCH、P-STATS、C-STATUS2、C-READY-T（探针三道闸、qfq 写入事实触发、攒批空闲判据、统计域逐股指纹、general 截断不算 partial、readiness/门禁阈值对齐）；见 reports/118。2026-11-14 数据包价值兑现审查：新增 D28-D33（派生表必须有重建触发、手工链清单、技术指标 qfq 连续依赖、有息负债口径、数据包文档硬编码、CSMAR 原始元数据未导入）；见 reports/119。2026-11-15 第二轮数据修复（reports/122）：新增 D40-D47（金融股必须用 companyType、`exchange_gain`=公允价值变动收益、qfq 后到强制重算、告警三级 block/disclose/inventory + 字段级遮蔽、冷库 Parquet 读写纪律、AH 只用腾讯 qfq 末根算"当前"、pypdf 依赖与季度不可用字段、GIL 崩溃环境事实与分块+抓写分离规避；D44 按 2026-11-15 清单收缩为 16 张、正式库尚未执行，D46 补 pypdf 仅在 .venv 与 `uv add pypdf && uv lock`，PDF 抽样结论见 reports/121）。。2026-11-15 PDF 方案作废：D46 改写为「除权日必须走结构化源（巨潮 stock_dividend_cninfo / 东财 stock_fhps_detail_em），禁止 PDF 解析；两家派息比例都是每 10 股，落库要 ÷10」，`app/core/pdf/dividend_parser.py`、`scripts/resolve_dividend_ex_dates.py`、`tests/regression/test_dividend_pdf_parser.py` 已删除，reports/121 标 superseded，现行方案见 reports/123。冷库清单同日由 16 张增至 17 张（`csmar_risk_factors` 补登记，`physical_switch: pending`，正式库切换待主控执行），见 D44。**2026-11-15 结构化分红源与冷库收官（reports/123）：新增 D48（除权日只走结构化源——巨潮 `stock_dividend_cninfo` / 东财 `stock_fhps_detail_em`；两家派息比例都是「每 10 股」，落库必须 ÷10；退市股 `is_listed=FALSE` 不删只保留，删除类分支执行前必须查 `is_listed`）、D49（现金金额未知绝不写 0，插入按 NULL；200541 2007-06-08 实况）、D50（冷库退役 ≠ 删除：`cold_archive_state` + 两条恢复命令；17 张清单唯一事实来源是 `config/cold_archive.yaml`）；D44 的「16 张 / pending」由 D50 覆盖（**17 张全部 `switched`**，包 `D:/vd-cold-archive-20261115-r17` = 17 张 / 4,619,914 行 / 411.4 MB、`--verify-cold` 通过、库内 17 视图 + 0 停车表）；D46 引用的 `reports/121` 已标 `superseded`，`pypdf` 依赖待办随之关闭。**2026-11-16 ROCE 可研 + DSL 括号缺陷（reports/128）：新增 C-DSL-PAREN（DSL 括号解析漏成 ParseTree → 抛 AttributeError 或被编译成字面量 NULL；`primary()` 解括号 + codegen fail-closed + `assert parse(...) is not None` 属假绿断言）、D51（ROCE 口径：EBIT=利润总额+财务费用、CE=流动资产−流动负债+固定资产净值、平均取期末与去年同期、(1)(2)(3) 三条护栏与已知局限）、O-DSLEXPR（期末口径 ROCE 的临时 DSL 表达式）、O-S1-LOCK（服务在跑时正式库无法被普通 open() 读，定向 pytest 的隔离跑法与官方 S1 的区别）。**2026-11-16 晚 更正（reports/129）：D51 全面改写** —— ROCE 的已动用资本是「总资产 − 流动负债」（= 净营运资本 + **非流动资产**），`reports/128` 把「固定资产」直译成 `balance_sheet.fixed_assets`(PPE) 是错的，该报告已标 `superseded`；新增 D52（字段名直译陷阱 + 加总类公式必须先跑恒等式自查 + 分布形状是口径正确性的一阶检验）；O-DSLEXPR 的表达式同步改为 `(…total_profit@TTM + …financial_expenses@TTM) / (…total_assets@LATEST - …total_current_liabilities@LATEST)`。**2026-11-16 ROCE 落地（reports/130）：D51 定稿为「CE = 总资产 − 流动负债、不剔现金、年报中位数、无护栏」，并补三条实测判据（PPE 漏油气资产 / 剔现金分母穿零 / 三口径对照只有 C 对得上原书标尺）；O-DSLEXPR 的临时 DSL 写法（`总资产−流动负债`，不剔现金）与正式口径一致，可继续作为无落库时的对照。**2026-11-16 投资大师指标调研（reports/131）：新增 I-ROCE5（分母五问）、I-NULLSEM（NULL 两种语义分野）、I-FINEXCL（金融股剔除必须进 SQL，用 sw_level1）、I-MCSTALE（689009 市值小 10 倍 = 派生表未随股本链重建）、I-GAPS（GDP/维持性capex/信用利差/beta·WACC/gross PPE 五个缺口）。**2026-11-16 ROCE 定稿（reports/132）：新增 I-ROCE6（术语≠列名）、I-CASHX（修正项在样本外变误差项）、I-ROCE7（筛选用有效年数不用上市年限）、I-CALIB（三列位并列）、I-GAP-CASH（缺拆出资金）。四次返工的教训：比例指标要用外部标杆校验量级，看到怪数先查数据缺口不要改公式。**2026-11-16 金融股排除依据 + ROCE 复盘（reports/131 §10-§11）：新增 I-FINEFF（金融股不能用营运资本型指标，只数占2.1%但市值占21.1%，该用 ROE/PB/净息差）与 I-KLIST（复合指标构建10步检查清单）。*

**2026-11-17 返工复盘（reports/133）：新增 E-SSOT / E-STATUS / E-DERIVED / E-DENOM / E-TZ / E-ISOLATE / E-GUARDSELF 七条反返工硬约束，并落成 `scripts/check_rework_guards.py`（RG1–RG4 静态门禁）+ 快照发布期派生列守卫 + `docs/skills/composite-indicator` Skill；同日修复 4 处私有 UTC+8 实现、4 处 `datetime.now(UTC).date()` 冒充「今天」、前端 2 处单位口径漂移。**2026-11-16 ROCE 窗口改造（reports/133）：新增 I-STRICTWIN（多窗口中位数必须拿满 N 年，不能用 7 年冒充 10 年）与 I-ROCE8（5/10/20 三窗口 + 门槛降到 5 年）。**2026-11-16 排名口径方案 C（reports/142）：新增 I-RANKMODE（排名改条件级下拉，筛选项 1126→196）、I-PCTUNIT（分位存 0–1 小数，声明 plain 会静默返回 0 只）、I-EXPANDREC（入口改写必须递归所有 dict/list）。**2026-11-16 指标标签中文化 + 耦合审计（reports/143）：新增 I-LABELCN（指标名必须含中文，display_name 与 label 不能各说一套）、I-COUPLING（参数后缀是过耦合信号，还剩 61 个）、I-COMPOSE（新需求优先用 DSL 复合指标而非加字段）。**2026-11-16 参数化字段收敛 + UI 截断审计（reports/143 §7-§9）：新增 I-PARAMCOL（参数后缀收成基名+参数，遮蔽要传播）、I-GRIDAUTO（Grid 自动放置+v-if = 轨道错位，n-select 要关 consistentMenuWidth）、I-SORTVER（排序混进规则版本属实现漂移，排序还影响 5000 行截断与 strict 过滤）。**2026-11-16 报告期错记与数据瑕疵（reports/146）：新增 I-DATE0101（CSMAR 44.9 万行 01-01 错记 + 修复脚本）、I-QTRDEP（季报无折旧摊销是源确实没有；小分母百分比会骗人）、I-ELIGNULL（布尔筛选标志不许留 NULL，否则双向漏筛）；并核实 reports/131 §9.2 的 9 个 P0 已全部修复。**2026-11-16 跨会话写租约与串行维护（reports/147）：新增 I-LEASE（正式库写入必须持租约，preflight 第五项）、I-WTPL（worktree 可共用正式库；Windows subprocess 解码坑）、I-PARTIAL（部分期次补采会误删其他期次——真实事故，已修 + 双测试）；并完成 44.9 万行 01-01 清理、员工数报告期修正、declare_date 回填 96.94%、2025-06-30 折旧摊销断档补齐。另新增 I-PRELIST：核实「早于上市日的报表」为误报（0 只断档>3 年），删了会误伤 61,405 行真实历史；教训是「百分比和看起来不对都不算证据，能证伪的判据才算」。**2026-11-16 英文字段名与不可用清单（reports/148）：新增 I-MERGED（修复必须合进用户分支并用主检出复验）、I-NASELECT（naive-ui fallbackOption 绕过 fieldDisplayName；载入时迁移旧排名字段）、I-UNAVLIST（改名≠不可用，按 reason 拆开）；另补齐 sw1_* 后缀标签、CSV 表头中文化、auto-update 写租约。*

## 23. 2026-11-25 DSL 单位量纲与筛选条件量纲（`reports/167`）

> 背景：用户问「蓝筹/白马怎么量化筛」，首轮规则实跑时暴露 unit 声明与存储口径漂移。
> 结论：`unit` 只描述**存储量纲**，不描述展示格式；两条筛选链路都必须按量纲比较。

| # | 约束 | 依据 |
|---|---|---|
| I-UNITSEM | **`unit` 只表示存储量纲，不表示展示形态**。五类：`pct`（小数百分比，0.2034 = 20.34%）、`percent`（百分数百分比，20.34 = 20.34%）、`ratio`（真倍数）、`CNY`、`count`。`pct` 与 `ratio` 同为「小数无量纲」，**维度等价**（`validator.DIMENSION_CLASS`）；`percent` 是**另一种量纲**，与它们差 100 倍，直接比较必须报错。存储口径真源是 `app/core/screening/field_units.py`，`ast_nodes.py::INDICATOR_METADATA` 只是镜像——加字段时两边必须同批改，棘轮见 `tests/regression/test_dsl_unit_dimension_classes.py` | `reports/167` §2.1–§2.2 |
| I-UNITDRIFT | **曾漂移 13 个指标**（`dividend_yield`、`revenue_yoy`、`net_profit_yoy`、`deducted_profit_yoy`、`*_cagr3/5`×6、`period_return`、`annualized_volatility`、`max_drawdown`）：声明 `percent` 实存小数 → `dividend_yield > ttm_dividend_yield` 判为合法、0.0255 > 2.55 恒假、筛出 0 只且无提示。已全部改为 `pct` | `reports/167` §2.1 |
| I-RIGHTFIELD | **条件 JSON 的字段对字段比较（`right_field`）新增量纲闸门**：`_magnitude_conflict()` 只拦已确证的 100 倍对（`pct` ↔ `percent`），不扩大到金额/股数（否则会误伤「每股股息 vs 每股价格」这类合法比较）。与 DSL 侧是同一个约束的两处实现 | `reports/167` §2.3 |
| I-PCTSCALE2 | **两种"分位"字段量纲不同，别混用**：①`*_stat_{N}y_percentile`（`research_statistics`，条件字段）存 **0–100**（正式库中位 19.275）→ 想筛"自身 10 年低位前 30%"要填 `<= 30`；②引擎 `rank_mode` 生成的 `*_percentile` 是 `PERCENT_RANK()` → **0–1**（见 I-PCTUNIT）。前端 `fieldInputUnit()` 对两者都是 `label='分位', scale=1`（原值输入），因此填错尺度会**静默返回 0 只** | `reports/167` §2.5；代码 `screening/engine.py` L1295–L1307 |
| I-CYCLIC | **周期股不该按"高 ROE + 高增长"筛，否则只会在景气顶部买它们**：周期股判据改用「ROCE 10 年中位（穿越周期）+ 自身 PB 10 年分位（位置）+ 低有息负债率（扛得住）+ 连续分红（现金流为真）」，**刻意不用 PE**（周期底部盈利被压低 → PE 反而高/为负）。实测该口径 13 只，与"成长白马"规则 37 只**重叠 0 只**，两套规则互补而非替代 | `reports/167` §4 |

## 24. 2026-11-25 `roce_latest` 可筛与白马股判据（`reports/168`）

| # | 约束 | 依据 |
|---|---|---|
| I-ROCELATEST | **`roce_latest` 自 2026-11-25 起是**可筛字段**（`SNAPSHOT_COLUMNS`）**：此前只在详情页当现况对照，导致「当期 ROCE 低于自身 10 年中位」在条件 JSON 里写不出来 —— 周期股判据只剩 PB 分位一个信号，**分不清「周期底部便宜」与「结构性衰退便宜」**。它与 `roce_*_median` 同为 `pct`（小数比例），可用 `right_field` 直接互比（`{"field":"roce_latest","op":"<","right_field":"roce_10y_median"}`） | `reports/168` §2 |
| I-WHITEHORSE | **「白马」两支都要求长期资本回报，不能只看当期 ROE**：成长支 `roce_10y_median≥15%`、周期支 `roce_10y_median≥10%` + `roce_latest<roce_10y_median`（不在景气顶）+ `pb_mrq_stat_10y_percentile≤30`（自身 10 年低位）+ 有息负债率≤35% + 连续分红≥8 年。实测 91 → 82 → 63 只。**代价**：`*_10y_median` 要求拿满 10 年年报，天然排除上市不足 10 年的公司（新生代白马需另建 5 年窗口支，而不是放宽 10 年窗口） | `reports/168` §3 |
| I-VARMEASURE | **收窄筛选规则必须在引擎里逐变体实测，不许用离线 SQL 估算**：`reports/167` §4 的「周期白马 13 只」就是离线近似派生字段得到的错值（真值 59），差点把规则调反。变体表要连"对照组"（只加单个条件）一起测，否则分不清是哪个条件在起作用 | `reports/167` §4、`reports/168` §3 |

## 25. 2026-11-26 个股研究的三个数据口径裁决（福耀评审暴露；`reports/169`）

> 背景：8 个大师视角 Agent 独立跑 SQL 评福耀（600660），第 9 个核验官重算后发现 **2/8 栽在同一个坑上、且都反过来指控"库里有 bug"**。
> 证据：`docs/evidence/fuyao-600660-2026-11-26.json`（脚本计算后写入，非手抄）。

| # | 约束 | 依据 |
|---|---|---|
| I-VALRAW | **历史估值分位只能用 `price_daily_raw`（不复权），禁用 `price_daily_qfq`**。前复权把累计派息从历史价格里扣掉（600660 于 2010-06-30：qfq 4.81 vs raw 8.90；累计派息 18.21 元/股）→ 历史市值被压到荒谬值（最低 0.2 亿 vs 真实 5.1 亿），负 PE、PB 0.11 等伪值随之出现，**当前分位被系统性抬高 20–27pct**。正确算法：`price_daily_raw.close` × `share_capital_history`（按 `effective_date` 区间取股本）÷ TTM 净利。`research_statistics` 用的就是这条链，可作 1:1 校验（实测 600660 当前 PE 分位 33.1%/19.9%/29.5%，与库内 33.06/19.88/29.45 一致） | `reports/169` §6；证据 JSON `checks.qfq_vs_raw` |
| I-HYIELD | **H 股股息率不能直接取 `ah_market_snapshot.h_yield_pct`，必须自算**：`hk_dividends` 对 03606 最新 `ex_date` 只到 2026-05-14（末期 1.20 元），缺 A 股 `dividends` 里 2026-09-24 的 1.00 元中期分红 → 库内 `h_yield_pct = 2.584%` 恰好等于 1.20÷46.438，**漏掉中期分红后低估约 45%**。同一行的 `a_yield_pct`（3.869%）是对的 → **一行内两个口径不一致**。正确值（2026-11-16，H 折人民币 46.438）：毛 4.737%、扣 20% 红利税 3.790%（同期 A 3.944%） | `reports/169` §4.4/§6；证据 JSON `facts.ah` |
| I-CSMARCOV | **`csmar_disclosure_metrics` 覆盖严重滞后（600660 最新只到 2025-03-31，落后 6 个季度），不能当"当期加权 ROE"引用**。本轮 6/8 份报告（含主控预置基线）把 `24.80%` 标成"2025 加权 ROE（csmar）"，而 24.80% 的真身是**期末口径**（93.12 ÷ 375.56 期末归母权益）；csmar 里 2024-12-31 加权 ROE 是 22.72%。引用 csmar 前先 `select max(report_date)`，且 ROE 必须标口径（加权 / 期末 / 摊薄） | `reports/169` §6；证据 JSON `checks.csmar_coverage` |
| ~~I-ACCRUALSQ~~ | **已证伪，勿再追**：核验官报「`indicator_ext.accruals` 近 12 期全部显示 −0.0（浮点精度丢失）」——主控复算该列近 8 期实为 −0.0300 / −0.0078 / +0.0185 / −0.0391 …，**是核验脚本自己 `round(x,2)` 造成的显示伪值，不是库缺陷**。登记于此以免下一个人重复排查 | `reports/169` §6；主控 `verify_defects.py` 实跑输出 |

## 26. 2026-11-26 周期资源股估值的四条口径裁决（紫金评审暴露；`reports/170`）

> 背景：8 个大师视角 Agent 独立评紫金（601899），**8/8 都在 ROCE 上用上个年报的旧值、8/8 都用峰值 DPS 算买点股息率、8/8 都用不同日期的 A/H 价比**；核验官自己也在正常化 PE 上少扣了一笔财务费用。四条都是"看起来很对、算出来差 8–130%"的类型。
> 证据：`docs/evidence/zijin-601899-2026-11-26.json`（脚本计算后写入）。

| # | 约束 | 依据 |
|---|---|---|
| I-ROCEDEF | **`roce_history.ebit` 的定义是「利润总额 + 财务费用」**（FY2025 实测 807.53+20.83=828.36 与表内 828.36 吻合），**不是**「利润总额 + 利息费用」。因此从 ROCE 反推归母必须走三扣：**先减财务费用 → 再扣税 → 再扣少数股东**。少扣财务费用会让正常化归母高约 2–4%（紫金：317.6 → 309.1 亿）。另：**正常化必须用当期 CE + TTM EBIT，不能沿用上个年报**——紫金 FY2025 的 `roce 24.39% / CE 3825.25 亿` 已过时，TTM ROCE 为 **30.08%**、CE 已升至 **4148.99 亿**，用旧值会把正常化盈利低估约 8%、正常化 PE 高估约 1.6x | `reports/170` §5/§7；证据 JSON `checks.roce`、`normalization` |
| I-MINORITYBASE | **「少数股东占比」必须写明分母**：紫金 2026 年 `27.8%` 是**少数股东损益/归母净利**，而 **少数/总净利 = 22.04%（TTM）**。两者差 6pct，拿去折 EBIT 会变成**双重扣减**（先扣利息再按归母口径折）。凡引用此类比例，一律写清 `x / 归母` 或 `x / 总净利` | `reports/170` §1/§7；证据 JSON `facts.ttm.minority_over_*_pct` |
| I-AHDATE | **A/H 比价必须同日**。`hk_price_daily` 的 H 行情常滞后 A 股数日（紫金滞后 12 天：H 最新 2026-11-13、A 到 2026-11-25），拿最新 A 价去比陈旧 H 价会得出"折价/溢价"的反向结论——紫金 8/8 报告据此得出"A/H 平水（−0.17%）"，而**同日快照（2026-11-16）A 溢价 14.65%**。正确做法：只用 `ah_market_snapshot` 的同一 `as_of` 行，或把两侧都折到同一交易日 | `reports/170` §4.4/§7；证据 JSON `checks.ah` |
| I-PEAKDPS | **周期股买点的股息率必须同时给「峰值 DPS」和「正常化 DPS」两个口径**。紫金 TTM DPS 0.80（peak）→ 3.48%，按 normalised EPS 1.1625 × payout 29.8% 只有 **0.346 → 1.51%**，**低于 10Y 国债 1.5914%**。8/8 报告只给峰值口径，使买点股息率被系统性高估约 2.3 倍；周期股在盈利顶部，峰值股息率是最不可持续的那个数 | `reports/170` §3/§7；证据 JSON `facts.dividends` |

## 27. 2026-11-26 股息率/净现金/分位/机构的四条口径裁决（美的评审暴露；`reports/171`）

> 背景：8 个大师视角 Agent 独立评美的（000333），核验官发现**集体口径在 4 处错**，其中 2 处反而对空头不利（把股息率与买点人为压低、把分位精度放大）。四条都是"看起来对、复现就不对"的类型。

| # | 约束 | 依据 |
|---|---|---|
| I-DPSATTRIB | **股息率与 payout 必须按「归属年度」聚合，不能用「近 12 个月除息窗口」**。美的 2025-11-18 有一笔 0.50 元中期分红，落在 TTM 窗口（2025-11-26~2026-11-25）之外，导致窗口内只捞到 2026-06-29 的 3.80 → 股息率 **4.04% / payout 61.1%**（错），而 **FY2025 归属 DPS = 0.50+3.80 = 4.30 → 4.57% / payout 74.7%**（对）。DB 的 `indicator_snapshot.payout_ratio` 与 `dividend_yield` 都是 TTM 窗口口径，**含中期分红的公司（2025 年起美的、福耀、紫金都是）一律要自算归属年度口径** | `reports/171` §7；证据 JSON `facts.dividends` |
| I-PCTPRECISION | **短窗口的历史分位不能当精确数引用**：美的 PE 3 年窗口只有 **37 个月**，**±1 个观测 = ±2.7pp**；同一配方月频得 97.3%、日频含披露滞后得 **89.2%**。引用分位必须同时给 **窗口长度 + 频度 + 区间**（如"3 年 89–97%"），并优先用 10 年窗口下结论（美的 PE 10 年 55–58% 才是稳的）。**单点分位差 8pct 不构成"贵/便宜"的方向性证据** | `reports/171` §1/§7；证据 JSON `checks.percentiles_monthly` |
| I-NETCASHPERIOD | **净现金必须同一时点同口径相减**。`reports/158` 用 **2025-12-31 的货币资金 852.5 亿** 减 **2026-09-30 的有息负债 708.4 亿**，得出"+144 亿"；同期同口径 2025 年末是 **+243.0 亿**，当期全口径是 **+569.7 亿**（差 4 倍）。资产负债表的时点型字段（现金、有息负债、交易性金融资产）禁止跨期拼装 | `reports/171` §7；证据 JSON `checks.net_cash` |
| I-INSTALLTYPES | **"机构撤退"必须看全类型持仓，不能只看基金**。美的 2026 年基金占流通比 10.85%→5.10%（真），但同期**保险 2.64%→6.05%**（1.81→4.17 亿股），是"公募换保险"而非单边撤离。`institution_holdings` 的 `holder_type` 有 基金/券商/保险 三类，**只查一类就下"聪明钱在跑"的结论会反向误导** | `reports/171` §7；证据 JSON `checks.institution` |

## 28. 2026-11-26 高派息公司估值的三条口径裁决（宇通评审暴露；`reports/172`）

> 背景：8 个大师视角 Agent 独立评宇通（600066），核验官抓出 8 处问题。其中两条是**高派息公司特有**的口径陷阱：100% 分红会让 PB 在除息后被动冲高，也会让"扣非/归母"的 YTD 与 TTM 差出 4pp。
> 证据：`docs/evidence/yutong-600066-2026-11-26.json`（脚本计算后写入）。

| # | 约束 | 依据 |
|---|---|---|
| I-PAYOUTPB | **高派息公司的 PB 会在除息后被动冲高，PB 分位必须用 as-of 净资产并标注除息窗口**。宇通 2026-05-15 派现 44.28 亿 → 归母净资产 162.25 → **129.76 亿**，PB 因此在 2026-07-30 冲到 **5.767**（10 年最高），而当时股价 33.80 **低于**今天的 32.00 之上的某些时点——**PB 新高不等于更贵，是分母更薄**。凡 payout > 80% 的标的，说"PB 创历史新高"之前必须先看当期是否有大额除息；正确表述是给"PB 处 X 年 Y% 分位"并注明当期净资产时点 | `reports/172` §7；证据 JSON `checks.pb_history` |
| I-YTDPURITY | **`扣非/归母` 必须标 YTD 还是 TTM**：宇通 2026 前三季 **97.3%**，但 **TTM 只有 93.0%**。8/8 报告在 TTM 语境下引用 97.3%，高估利润纯度 4.4pp。凡引用该比率，写成"9M 97.3% / TTM 93.0%"两个数，或明确写口径；**利润质量的趋势判断可以用 YTD，估值判断必须用 TTM** | `reports/172` §7；证据 JSON `facts.ttm.ded_over_parent_*` |
| I-EXTDATAMARK | **报告引用库外数据必须显式标注来源与获取方式**。`reports/153` 把"2026 年 1–10 月销量 33,885 辆（−7.4%）"和"国内 6 米以上份额 28.70%"写进数据包语境而未标注，后续读者无法区分"库内事实"与"外部引用"。规则：正文凡出现库内不存在的数据，句末必须带 〔外部来源：xxx / 日期〕；`DATA_COOKBOOK` 的"库内没有的东西"清单是判据 | `reports/172` §6；`reports/153` 附录 |

## 29. 2026-11-26 送转/现金/缺失科目三条口径裁决（公牛评审暴露；`reports/173`）

> 背景：8 个大师视角 Agent 独立评公牛（603195），核验官抓出 **17 条问题**。其中最重的一条是 `reports/160` 的**整个派现表算错了 40–48%**——它把名义 DPS 乘以后来的（更大的）股本，于是在三年三次转增的公司上，把"分红每年在涨"读成"连砍三年、分红率 >100% 超发"。

| # | 约束 | 依据 |
|---|---|---|
| I-BONUSADJ | **送转公司的 DPS 跨年比较必须先复权到同一股本，或用「实派分红总额 ÷ 当年归母净利」算 payout**。公牛 2022→2025 名义 DPS 3.30→3.10→2.40→1.90 看似连砍三年，但同期做了三次转增（1.48×1.45×1.40 = 3.0 倍股本）；复权到今日股本是 **1.10→1.53→1.72→1.90（每年在涨）**，分红总额 19.84→27.64→31.01→34.35 亿（+73%）。正确做法：`payout = dividend_per_share × 除息日前一日 total_shares ÷ 当年归母净利`（用 `share_capital_history` 的 `effective_date < ex_date` 取**除息前**股本，不是除息当日的） | `reports/173` §7；证据 JSON `checks.dividend_bonus_adjust` |
| I-EXROCE | **报表 ROCE 与经营 ROCE 必须成对给出**。`roce_history` 的 CE = 总资产 − 流动负债，**含全部现金类资产**；对"账面富裕"公司（公牛 CE 172.69 亿里 162.24 亿 = **93.9%** 是货币资金+交易性金融资产），报表 ROCE 28.82% 会把经营回报低估一个数量级——剔现金后经营 ROCE 点估 **460%**（区间 155%–460%，取决于是否扣渠道应付）。同时注意：**"ROCE 单调下降"这种断言必须先数上升年数**（公牛 11 年里有 2 个上升年，"单调"不成立） | `reports/173` §7；证据 JSON `checks.roce_dual` |
| I-MISSINGROWS | **`income_statement` 缺三行：其他收益 / 公允价值变动收益 / 资产处置收益**。凡做单季归因（"断崖是不是一次性"这类判断），这三行缺失会产生残差——公牛 2026Q3 的残差是 **0.4724 亿**，约占单季归母跌幅的 20%，足以把"无一次性科目"的结论推翻。规则：做归因时先把残差显式算出来并命名（"未能归因的 X 亿"），不许默认它为零 | `reports/173` §7；证据 JSON `checks.q3_attribution` |

## 30. 2026-11-26 每股口径/分红字段/前复权负价三条裁决（神华评审暴露；`reports/174`）

> 背景：8 个大师视角 Agent 独立评中国神华（601088），核验官抓到两处**新的库内缺陷**，加上一条**跨票通用**的每股口径陷阱。神华 2026 年因收购增发，股本从 198.69 亿股增至 216.89 亿股，把这三条问题同时放大暴露了出来。

| # | 约束 | 依据 |
|---|---|---|
| I-EPSWEIGHT | **`income_statement.basic_eps` 是「当期加权股本」口径，不是今日股本口径，禁止跨期直接比较或乘今日股本**。神华 2018–2025 的 `basic_eps ÷ (归母÷今日股本)` 恒为 **1.0917**（= 216.894 ÷ 198.685，因 2026 年增发 9.2%）。用它乘今日股本算 PE 会高估 9.2%（神华 19.31 → 17.7）。**规则：PE、每股分红、每股 FCF 一律自算「金额 ÷ 今日总股本」，`basic_eps` 只作"当期报表披露值"引用**；跨年比较 EPS/每股股息必须先复权到同一股本（与 §29 `I-BONUSADJ` 同族） | `reports/174` §7；证据 JSON `checks.basic_eps_trap` |
| I-SNAPSHOTPAYOUT | **`indicator_snapshot.dps` 与 `payout_ratio` 只取「最近一笔分红」，而同表 `dividend_yield` 用的是完整 TTM 分红——同一行内两个口径，payout 会被严重低估**。神华实测：`dps=1.03`、`payout_ratio=38.38%`，而真实 FY2025 DPS **2.01**（0.98 中期 + 1.03 末期）、payout **79.1%**；同行的 `dividend_yield=3.88%` 却是对的（2.01/51.82）。**凡一年内派息 ≥2 笔的公司（神华/美的/宇通/公牛都是），`dps` 与 `payout_ratio` 两个字段一律不可直接引用**，必须用 `dividends` 表逐笔 × 除息日股本自算。这是 §27 `I-DPSATTRIB` 的升级：那一条讲"窗口"，这一条讲"**字段本身只存了一笔**" | `reports/174` §7；证据 JSON `facts.dividends.defect` |
| I-QFQNEG | **`price_daily_qfq` 会出现 0 或负价，负价票禁止用于任何比率计算**。前复权在"累计分红超过历史价格"时会把早年价格压到 0 以下——神华有 **1,432 行负价（最低 −9.55 元）**，全库至少 6 只票各 3,700+ 行负价（600809 汾酒 5,192 行、000651 格力 4,558 行、600188 兖矿 4,219 行…）。用 qfq 算市值/PE/PB 会**符号翻转**：神华用 qfq 算分位会把 10 年 PE 分位从 **97.5% 推到 100%**。这是 §25 `I-VALRAW` 的最强实证——**估值分位一律 `price_daily_raw`，qfq 只可用于画 K 线趋势图** | `reports/174` §7；证据 JSON `checks.qfq_defect` |

## 31. 2026-11-26 破净股三条判据裁决（大秦评审暴露；`reports/175`）

> 背景：8 个大师视角 Agent 独立评大秦铁路（601006），遇到本轮第一个**真破净股**（PB 0.593）且 **PE 同时在 10 年 96% 分位**。核验官把"破净"从一句感觉变成了可复算的门槛，同时推翻了一条被 8 份报告当成事实的断言。

| # | 约束 | 依据 |
|---|---|---|
| I-BROKENBOOK | **破净（PB<1）本身不是买入理由，必须反解"市场隐含要求的 ROE"**。恒等式 `PB = PE × ROE`：大秦 0.593 = 17.75 × 3.34%，**破净的全部内容是 ROE 从 ~10% 掉到 3.34%**。可复算判据：`所需 ROE = PB × (r − g) + g`。大秦在 r=8%、g=0 时**需 ROE ≥4.75%（归母 78.1 亿）**，g=2% 时需 **5.56%（91.5 亿）**；实测 TTM 3.34%（55.0 亿）/ 2026 年化 4.72%（77.65 亿）/ 加回 241 亿摊销 5.03% —— **差 0.03–0.53pp，现价 4.94 ≈ 公允价 4.91，安全边际恰为 0**。凡破净标的，一律给出"价格 × 隐含要求 ROE"对照表（大秦报告 §4 那张表），**不许把 PB 高低单独当便宜/贵** | `reports/175` §2、§4；证据 JSON `normalization.value_trap_test` |
| I-WACCLABEL | **"ROCE < 资金成本"必须标注资金成本是怎么算出来的，否则就是伪装成事实的假设**。大秦：库内 `stock_beta` 500 日 beta 仅 **0.155**（R²=0.022）→ CAPM CoE **2.45%** → ROCE 4.28% **反而高于它**；只有用 build-up（1.59% + ERP 5.0–6.5% = 6.59%–8.09%）或"我要求 8%"才得出"低于资金成本"。**规则：写"低于资金成本"必须同句给出 r 的来源与数值；用 CAPM 时同时报 beta 与 R²**（低 R² 的低 beta 会把 CoE 压到荒谬的水平） | `reports/175` §6 第 1 条；证据 JSON `checks.wacc_debate` |
| I-BUYBACKSCAN | **资本配置分析必须扫 `buyback_events`，不能只看分红与并购**。大秦有一笔 **20 亿元注销式回购**（3.8746 亿股 / 均价 5.161 元 / PB 0.620，与 `paid_in_capital` 逐位吻合）——**8 份大师报告全部漏掉**，而它是过去三年唯一一次"把钱还给股东而非买资产"，直接改变"管理层只会拿钱买资产"的判断。规则：凡评价资本配置，三件事都要扫：`dividends` / `buyback_events` / 大额投资现金流 | `reports/175` §6 第 6 条；审计 §〇 第 6 点 |
| I-DILUTIONREAD | **转债/增发摊薄必须与 DPS 变化同时读**。大秦股本 2022→2025 **+35.5%**（148.67 → 201.47 亿股，转债转股），同期 **DPS 0.48 → 0.22（−54%）**——"每股分红砍半"里有一部分是摊薄、不是政策转向；真正的政策信号是**分红总额 −41%**。规则：报"分红下降"时给三个数（DPS、股本、分红总额），只给一个就是误导 | `reports/175` §1、§6 |

## 32. 2026-11-26 特许经营权四条定价裁决（宁沪评审暴露；`reports/176`）

> 背景：8 个大师视角 Agent 独立评宁沪高速（600377），本轮第一次遇到**核心资产到期归零**的公司（575.5 亿无形资产＝收费公路特许经营权，占总资产 54.5%）。核验官抓出 8 份报告的共同方法论错误（5 份把企业价值当股权价值），并纠正了一条被广泛当事实的断言。

| # | 约束 | 依据 |
|---|---|---|
| I-EVEQUITY | **用经营性/无杠杆现金流折现时，必须扣净负债与少数股东权益，不许直接除以股本**。宁沪 8 份报告里 **5 份**把 FCFF 年金现值直接 ÷ 50.38 亿股得每股价值，漏了 **286–335 亿净负债（−6.04 元/股）** 与 **141.6 亿少数股东权益（占归母 24.2%，只贡献 4.9% 利润）**——这会让估值系统性高估 40%+。规则：DCF 出来先写三个数：**EV、净负债、少数股东权益**，再谈每股价值；`equity = EV − net_debt − minority` | `reports/176` §6 第 1 条；审计 §〇 |
| I-CONCESSION | **特许经营权/收费权类无形资产必须按"有限期 + 零终值"折现，永续假设要单独标为上界**。宁沪 575.5 亿路权到期**无偿移交政府、残值零**；有限期口径（15–32 年 × 6%–9%）合理价值 **2.78–8.27 元**，永续 g=0 是 8.46 元，**永续 + g=2% 才 14.04 元 ≈ 现价 14.41**。即：现价把"永久续期 + 温和增长"两件库内无法证实的事一次付清。另注意年限有**两个算法**：用总 D&A 26 亿得 22.1 年，只用无形资产摊销 **18.23 亿**（其余 7.67 亿是固资折旧）得 **31.6 年**——必须说明用哪个。**有形净资产 = 归母权益 − 无形资产 = −133 亿（每股 −2.64 元）**，这类公司 PB 没有清算地板 | `reports/176` §2、§6 第 4 条；证据 JSON `normalization.anchors` |
| I-CONSTRUCTREV | **营收里含"建造期收入"时必须剔除后再算任何营收比率**。宁沪 2025 年营收 202.89 亿里 **81.56 亿（40.2%）是建造期收入/成本**（新收入准则下代建服务对称确认，几乎不产生利润）→ 真通行费收入只有 **95.55 亿**，两年 CAGR **+0.23%**；真实营收 126.96→121.34 亿（**−2.24%/年**）。**`revenue_cagr5 = +20.4%` 完全是这个科目 + 地产并表造成的幻觉**。规则：`business_breakdown` 出现"建造期收入/成本"科目的票，PS、营收增速、毛利率一律用剔后口径，并同时给两个数 | `reports/176` §1、§6；证据 JSON `checks.toll_vs_construction` |
| I-ROEDENOM | **`indicator_snapshot.roe` 是平均权益口径，用 `PB = PE × ROE` 校验时必须换成期末权益口径**。宁沪：`15.75 × 10.42%（期末）= 1.641` ✓，而用快照 `roe=10.91%（平均）` 算得 **1.718 ≠ 1.641**。这是三个独立 Agent 都撞到的同一个错。规则：恒等式校验一律自算 `归母TTM ÷ 期末归母权益`；顺带 **`business_breakdown` 同一科目在 `type=1` 会有重复行**（宁沪 2024 年通行费重复计成 190.6 亿），求和前必须按 `(report_date, item_name)` 去重 | `reports/176` §6 第 3、5 条；证据 JSON `checks.roe_identity` |

## 33. 2026-11-26 有息负债完整性与重资产清算三条裁决（海螺评审暴露；`reports/177`）

> 背景：8 个大师视角 Agent 独立评海螺水泥（600585），遇到九家里**破净最深**的一家（PB 0.5001，全史 1.3% 分位）。核验官抓到一处**主控自己写错、且 5/8 份报告同错**的口径漏项，并第一次把"破净"折算成可比的清算价值。

| # | 约束 | 依据 |
|---|---|---|
| I-IBDCOMPLETE | **有息负债必须含「一年内到期的非流动负债」，否则净现金会系统性虚高**。海螺基线只取"短期借款+长期借款+应付债券+租赁负债"=208.45 亿，漏了**一年内到期非流动负债 46.55 亿** → 净现金 **216.7 虚高至应报 170.2 亿**、剔现金 PB **0.436 → 0.4508**、EV **732.1 → 778.63 亿**、EV/归母 **16.1x → 17.11x**（偏乐观 6.3%）。⚠️ **`balance_sheet` 表里没有这个独立字段**（候选列只有 `total_non_current_liabilities`），必须从年报附注或现金流量表"偿还债务支付的现金+筹资活动"反推，并在报告中标明来源。凡算净现金/EV/剔现金 PB，先把有息负债的**四个来源逐个列清** | `reports/177` §6 第 1 条；证据 JSON `checks.net_cash_correction` |
| I-LIQVALUE | **重资产破净股必须给"归母清算价值"这一条硬底线，不能只给 PB 分位**。海螺按"乐观可变现价重估全部资产 − 全部负债 − 少数股东权益"= **862.6 亿 = 16.35 元/股**，**低于现价 17.98 约 9.1%** → 现价已站在清算价值之上。撑住 PB 0.5001 的是 **903 亿固定资产（水泥窑炉，拆除残值近零）+ 373 亿矿权，合计占归母权益 67%**，而这是**需要继续投钱才能维持**的资产，不是可变现资产。规则：破净标的必须同时给 ①PB 分位 ②剔现金 PB ③**归母清算价值** 三个数，缺一个就可能把"本金收不回来"读成"打了五折" | `reports/177` §2、§6 第 2 条；证据 JSON `checks.liquidation` |
| I-EARNQUALITY | **单季利润必须做"经营性 vs 财务性"拆分**，否则会把投资收益当主营。海螺 2026Q3 归母 2.16 亿，而**利息收入+投资收益合计约 3.08 亿 > 归母** → 主营接近盈亏平衡；**2026Q2 归母 10.59 亿里有 7.09 亿是投资收益（67%）**。同时 `CFO/净利` 这个指标在**少数股东损益为负**时会被统计放大：海螺 TTM ÷合并净利 41.32 = 3.19、**÷归母 45.51 = 2.89、2025 年报口径仅 2.05**。规则：报"利润质量高"必须同时给 ①扣非/归母 ②单季财务性收益占归母比例 ③CFO÷**归母**（不是合并净利） | `reports/177` §6 第 3、4 条；证据 JSON `checks.q3_quality`、`facts.ttm` |

## 34. 2026-11-26 有限地权 / PB 判据 / cookbook 偏差四条裁决（国贸评审暴露；`reports/178`）

> 背景：8 个大师视角 Agent 独立评中国国贸（600007），遇到十家里**质量最高**的一家（ROCE 15.38% 且十年上行、零负债、毛利 58%、capex/净利 6.7%）。核验官给出一个罕见的结论：**21.68 元既不是便宜价也不是贵价，它是 8% 要求回报下的公允价**；同时抓到一条**主控自己的流程缺陷**。

| # | 约束 | 依据 |
|---|---|---|
| I-LANDTERM | **土地使用权/特许经营权必须用「有限期 + 零终值」折现，永续假设只能作上界**。国贸：把"永续"换成"27 年地权"，8% 要求回报下公允价从 **19.86 掉到 16.69（−16%）**；反解现价 21.68 → **要求回报只有 5.01%**。这是本票真正的风险点，比"PB 太高"重要得多。规则：凡 `intangible_assets` 里是土地使用权/收费权/特许经营权的标的，DCF 期限先写"剩余年限"，并从**无形资产余额 ÷ 年摊销**反推（国贸 4.17 亿 ÷ 0.15 亿 ≈ 28 年），库内无明细时必须标〔推算〕 | `reports/178` §2、§6；证据 JSON `normalization.anchors` |
| I-PBJUDGE | **PB 贵不贵可以定量算：`PB = (ROE − g) / (r − g)`，其中 g = SGR = 留存率 × ROE**。国贸：SGR = 12.34% × 12.76% = **1.57%** → r=8% 合理 PB **1.74**、r=9% → 1.51、r=10% → 1.33，**现价 2.266 只有在 r ≤ 6.5% 时才不贵**（6.5% 对应 PB 2.27）。另有一条重要推论：**资产重估对 PB 判据完全无效**——把投资性房地产按 3x 重估，PB 从 2.266 降到 1.22、ROE 从 12.86% 降到 6.86%，**PE 一动不动**。所以"成本法资产被低估"不能当买入理由，除非公司真的卖 | `reports/178` §2；证据 JSON `normalization.fair_pb` |
| I-COOKBOOKBIAS | **DATA_COOKBOOK 不许给带方向的结论性数字**。本轮 cookbook R4 给了"cap 4.5%–6.0% → 30.6–43.5 元"的示范表，结果 **8 份报告里 6 份引用了同一个「cap 5.5% → 33.6 元」的 NAV 数字**，把它当独立发现；**两份"买"票实际同源，不构成 2 个独立证据**。规则：cookbook 只写**取数配方、口径、陷阱、以及"库内没有什么"**；凡出现"某法算出某个价格"，下一轮必须删掉——否则 8 个 Agent 的独立性会被主控的框架预设污染 | `reports/178` §6.2；证据 JSON `checks.cookbook_bias` |
| I-IBDCOMPLETE-FIX | **§33 `I-IBDCOMPLETE` 的字段位置需更正：「一年内到期的非流动负债」在 `balance_sheet_ext.non_current_liab_due_1y`，不在 `balance_sheet`**。国贸该项 = **3,271,376 元（0.033 亿）**；海螺缺的那 46.55 亿用这张表本来就能取到。规则：算有息负债时**两张表都要查**——`balance_sheet` 给短期/长期借款、应付债券、租赁负债；`balance_sheet_ext` 给一年内到期非流动负债 | `reports/178` §6.3；证据 JSON `checks.ibd_field_fix` |

## 35. 2026-11-26 快照同比字段 / 前复权股本伪影 / 净现金归属三条裁决（海康评审暴露；`reports/179`）

> 背景：8 个大师视角 Agent 独立评海康威视（002415），遇到十一家里第一次「PE 全史 1.1% 分位 + 盈利创历史新高」的组合。核验官在验证这个极值时，抓出一条**影响此前全部十一份报告**的字段口径错误，以及一条会**伪造历史极值**的价格×股本错配陷阱。

| # | 约束 | 依据 |
|---|---|---|
| I-SNAPSHOTYOY | ⚠️ **`indicator_snapshot.revenue_yoy` / `net_profit_yoy` / `deducted_profit_yoy` 不是同比，而是「TTM ÷ 最新年报 − 1」**。海康：快照 `net_profit_yoy = 0.253059`，而 177.876/141.954−1 = **25.306%**、1,007.94/925.08−1 = **8.958%**（四位小数精确吻合）；**真实 TTM 同比是归母 +34.87%、营收 +8.08%**。后果：**凡是增速本身在加速的公司，这个字段都会系统性低估加速度**（本次低估 9.6pp）。规则：**任何"同比"必须自算 `TTM(t) ÷ TTM(t−1) − 1`**（用 `FY(t−1) + YTD(t) − YTD(t−1同期)` 两种 TTM 相除）；快照里这三个字段只能读作"TTM 相对上一年报的增长"，不能读作同比。**这是范围最广的一条，§25–§34 期间所有报告凡引用该字段处均需按此重读** | `reports/179` §6 第 1 条；证据 JSON `checks.snapshot_yoy_defect` |
| I-QFQSHARES | **前复权价 × 未复权股本 = 伪造历史极值**。海康两位 Agent 引用"2016-05 PE 13.98、2013-07 的 16.6"作为"历史更低估值"的证据，实际是 **qfq 价 ÷ 当期股本** 的伪影（同法可得 2011-04 的 2.45，荒谬到自证）。**正确值：2016-05 = 21.59、2013-07 = 33.18**；全史 186 个月末只有 **2022-08-31 的 17.52** 与 **2026-09-30 的 16.72** 低于今天的 17.63——**PE 17.63 是 16 年第 3 低，真极值**。后果：**伪影把多头最强的证据（真极值）削弱成了"方向对、幅度偏乐观"**。规则：历史分位一律 `price_daily_raw`（不复权）× `share_capital_history` 当日股本；**绝不可 qfq 价配未复权股本，也不可 raw 价配今日股本** | `reports/179` §6 第 2 条；证据 JSON `checks.qfq_artifact` |
| I-NETCASHSCOPE | **净现金/剔现金 PE 必须说明"是否扣少数股东权益"**。海康净现金有四个合法口径（317.02 含租赁+一年内到期 / 320.16 / 327.93 / 331.07，对应剔现金 PE 15.84/15.83/15.78/15.77），**其中约 92.71 亿属少数股东权益**，严格归母口径只剩 **224.31 亿（市值 7.2%）**，剔现金 PE 从 15.84 变 **16.37**。8 份报告全部按 317 亿的 100% 使用。规则：报净现金/剔现金 PE 时，若 `minority_interest` 占权益 >5%，必须同时给"合并口径"与"归母口径"两个数 | `reports/179` §6 第 7 条；证据 JSON `checks.net_cash` |

## 36. 2026-11-26 双低分位拆解 / 留存质量 / 账上现金回报三条裁决（晨光评审暴露；`reports/180`）

> 背景：8 个大师视角 Agent 独立评晨光股份（603899），遇到十二家里**"便宜的样子"最像便宜**的一家（PE 全史 4.9% 分位 + PB 全史 4.2% 分位 + 净现金 27.5% 市值）。核验官用一条恒等式证明：**这两个低分位是同一个事实的两种写法**；并第一次把"payout 之后的留存质量"量化成估值折价。

| # | 约束 | 依据 |
|---|---|---|
| I-DOUBLELOW | **PE 与 PB 同时处于历史低分位时，必须先用恒等式拆解，禁止当作两个独立信号**。晨光：`PB 7.50（全史中位）× ROE 21.4%→14.6% 的 0.6833 × PE 35.12→16.17 的 0.4607 = 2.361` —— **PB 的 4.2% 分位 100% 可由"ROE 掉了 32%"与"PE 掉了 54%"解释**，它不提供任何 PE 之外的新信息。规则：凡"PE 与 PB 双低"，先写 `PB = PE × ROE` 的拆解，只有当 **ROE 稳定或上行**时，"双低"才是两个信号；若 ROE 在下行，双低只是"生意变差"的两种写法 | `reports/180` §2；证据 JSON `checks.double_low_decomposition` |
| I-RETENTION | **必须用"增量 ROCE = ΔEBIT ÷ ΔCE"检验留存利润的质量**，并把它折成估值差。晨光：`2021→2025 增量 ROCE = **−4.75%**`、`2023→2025 = **−14.26%**` —— **公司留了 30% 利润（payout 69.7%），拿去做的增量投资回报是负的**。量化后果：**"零增长合理价 23.2"只在 100% 分红假设下成立；70% payout + −4.8% 增量回报下合理价是 16.5–18.2 元**。另一面：**剔现金盈利收益率 8.5%，但股东实际拿到的现金回报只有 5.96%**（股息 4.31% + 回购 1.65%），差的 **2.5pp** 就是资本配置折价。规则：凡 payout <90%，必须算增量 ROCE 并给出"全额分红价"与"实际 payout 价"两个估值 | `reports/180` §2；证据 JSON `checks.incremental_roce`、`checks.cash_return_gap` |
| I-BASEPEAK | **"历史峰值"必须用 `order by parent_net_profit desc limit 3` 机械确认，不能凭印象**。晨光基线手册（主控自己写的）标"2021 峰值 15.18"，实际 **2023 年 15.27 才是峰值**；**8 份报告 7/7 全部沿用该错**，导致"距峰值跌幅"从 −13.9% 被算成 −13.4%。同类还有：**FCF 19.2 亿含一次性营运资本释放**（自建 capex 滚算 5.61 亿 → 17.2 亿，剔一次性后约 13.9 亿，收益率从 9.0% 降到 6.5%）；**真实 TTM 扣非同比 +0.52%** 而非 −3%；**"校边店 8 万家"是外部假设**（库内 `company_profile` 为"近 7 万家零售终端 + 900 余家零售大店"）。规则：凡写"峰值/历史新高/FCF/N 万家"，必须给出该数字的 SQL 或表名字段 | `reports/180` §7；证据 JSON `checks.peak_correction`、`checks.baseline_errors` |
