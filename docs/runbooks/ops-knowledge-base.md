---
title: 运行期硬约束知识库（Ops Knowledge Base）
status: approved
category: runbooks
created: 2026-09-01
last-reviewed: 2026-09-05
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

## 3. DuckDB / 存储约束

| # | 约束 | 依据 |
|---|---|---|
| D1 | **单写者锁**：跨进程互斥（`app/core/storage/update_lock.py`）；所有 CLI 写命令必须经 `exclusive_update`；库层第二道 `_write_lock` | reports/84、92 |
| D2 | **DuckDB 1.5.5 同事务 `DROP INDEX`+`CREATE INDEX` bug**（`BoundIndex::CreateDeltaIndex` FATAL）：索引重建必须分事务（DROP 提交后再 CREATE）；升级评估为长期待办 | reports/81 F1 |
| D3 | `raw_response_archive` 冷热分层（schema v16）：`history`（冷）+ `active`（小）+ 视图 `raw_response_archive_all`；冷核对走 hash 集合，不触碰 BLOB | reports/96 |
| D4 | lineage hash 集合（schema v17）：`raw_response_archive_valid_hash` + `raw_response_archive_partitions`（5GB / 10万行 / 31天自动轮转，计数器维护避免每次 SUM 全扫） | reports/102 |
| D5 | 当前 schema 版本：`DUCKDB_SCHEMA_VERSION = 20`（app/core/storage/schema.py） | 代码 |
| D6 | `vd backup` 对 26GB BLOB 表须**分块导出**（raw_response_archive_history 5000 行分块），单次 COPY 会受内存限制 | reports/102 |
| D10 | `vd data auto-update status` 是**只读**命令（只查 SQLite，不打开 DuckDB）；其它 CLI 写命令在 schema 已最新时经 `skip_if_current=True` 跳过全量幂等 DDL，避免扫描 43GB BLOB 视图 OOM | reports/104 |
| D11 | 数据状态重量摘要：后台 stale-while-revalidate，TTL 300s；空闲期前端每 300s 拉一次，更新 running→finished 时前端主动立即刷新一次。全量构建仍约 19-23s，但不得阻塞或拒绝普通查询 | reports/104；data_status.py |
| D12 | **自动更新写连接窗口会阻塞 Web 查询**：DuckDB 单写者模型下，research_statistics 全量重建的发布阶段会持续持有写连接（2026-09-03 实测约 4-6 分钟），期间普通 K 线/详情/自选请求会等待或超时。这不是连接配置冲突；优化方向是分批可见发布或快照读，而不是调大超时硬扛 | reports/104；实测 |
| D13 | `source_audit` 冷热分离：日常 readiness/lineage 只扫热表 `source_audit`；历史排查查 `source_audit_all`。归档命令 `vd data source-audit-archive --before YYYY-MM-DD`，按 id keyset 分页，每批独立事务；正式库已归档 30,039,082 行（cutoff 2025-01-01） | reports/106；app/core/source_audit_archive.py |
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
4. 引用本文件结论时标注：`docs/runbooks/ops-knowledge-base.md` + `last-reviewed: 2026-09-05`

## 9. 多指数 ERP 与 ETF 工作台新增约束（2026-09-05）

| # | 约束 | 出处 |
|---|---|---|
| S11 | 乐咕 `stock_index_pe_lg/pb_lg` 上游已改为**月末序列**（约 144~261 点，2005 至今）；沪深300 日度历史已留存正式库不回退；UI 标注 cadence=monthly | reports/111 探测 |
| S12 | 乐咕对连发请求敏感：约 4 个指数（8 次请求）后 csrf 页面解析失败（AttributeError 'NoneType'）；适配器限速 2s/请求，批量回填按逐指数 + 30s 冷却；自动更新宽基组需分日轮转，勿一次连打 12 个 | 2026-09-05 正式库回填实测 |
| S13 | 申万 `index_analysis_report` 直连：`page_size` 可给到 **50000**（3 页/11.8 万行）；单页 26~55s、3 并发墙钟约 56s；证书链不完整必须 `verify=False`；akshare 50/页路径禁用（2372 页×15s≈10h） | 2026-09-05 实测 |
| S14 | 同花顺 Financial-API Key 只存环境变量 `HITHINK_FINANCE_API_KEY`（用户级已 setx）；适配器必须用 **httpx**（akshare 会 monkeypatch requests.Session.request，`trust_env` 报 TypeError）；QDII（513130/159605）`track_index_pe_ttm_five_year_percentile` 上游恒 null → 如实 unavailable | reports/111 实测 |
| S15 | DuckDB 1.5.5 executemany 含 date/datetime 参数的病理内存问题同样适用于 `index_valuation` 批量写入（11.8 万行卡死）——>1 万行批量一律 pandas register + 单条 INSERT SELECT（D19 泛化） | reports/111 |
| S16 | S1 包装器 preflight 会冻结**本机全部 python 进程状态**：无关 python 服务（如 streamlit）运行/变化会导致"Python process state changed"或 tmp 清理 PermissionError 误报；跑 S1 前需停掉无关 python 应用 | reports/82 §5.4、2026-09-05 实测 |

---

*变更记录：2026-09-01 创建（从 reports/61/75/77/81/84/86/92/96/97/98/99/100/101/102、STATUS、config、代码核验聚合）。2026-09-01 体检修复：T3 关闭、E6 补充直连回退。2026-09-02 技术债补全：T1 评估、T2 冷归档恢复 CLI、T4 隔离分红审计 CLI、T5 东财单次探测 CLI、T6 旧单位规则审计 CLI、T7/T8/T9 核验关闭；按窗口删除重建回滚快照并清理约 70GB 旧产物。2026-09-03 性能修复：E7 更新为统一 8GB/2线程/preserve=false 且 Web 进程禁止 memory_limit()；新增 D10（auto-update status 只读 + CLI skip_if_current）、D11（重量摘要 TTL 300s）。2026-09-04 冷归档分区复审：新增 D17（外部 Parquet 按年分区规范，reports/109）。2026-09-04 港股分红域：新增 S10（datacenter ≤2 req/s、禁 push2）、D18（hk_dividends schema v20/映射与写纪律/融资仍缺失）、D19（executemany date 参数内存病理与向量化写入范式）、D20（轮转记账失控与修复），D5 schema 版本更新为 20。2026-09-05 多指数 ERP + ETF 工作台：新增 S11-S16（乐咕月度与限流、申万 50000/页并发、THS httpx/QDII null、向量化批量泛化、S1 进程冻结），schema 版本 D5 更新为 23。*
