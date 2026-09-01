---
title: 运行期硬约束知识库（Ops Knowledge Base）
status: approved
category: runbooks
created: 2026-09-01
last-reviewed: 2026-09-01
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
| E7 | DuckDB 连接统一 `memory_limit=14GB`（`config/default.yaml`，可经 user.yaml 覆盖）；12GB 曾触发冷核对 OOM | reports/98；config |

## 2. 数据源约束与限速

| # | 约束 | 依据 |
|---|---|---|
| S1 | **东财 push2/push2his 被封**（IP 级临时封锁）：F10/股本/分红源仍可用；价格已回退腾讯/BaoStock/TDX；冷却期内勿触碰 push2 系 | reports/61 |
| S2 | **北交所（920xxx）不请求东财**：无交叉源且异常会触发 cninfo_capital 熔断殃及沪深；如实记 empty（`no_cross_source:bse`），不入 retry | reports/75 |
| S3 | 东财交叉核验安全组合：**批 50 + 批间冷却 30-60s**；`update_many` 连续 8 次交叉错误 / 16 次空响应中止保护 | reports/75 |
| S4 | CNINFO 风控冷却：股本链约 4,700 只由自动更新有界续传，勿手动批量触发 | reports/74 |
| S5 | CNINFO 公告接口**每页固定 30 条**，pageNum>100 重复第 1 页——分页判断必须用实际 30 条 | reports/84/中报修复 |
| S6 | CNINFO 分红适配器 **ex_date 主源恒空**（公告接口不提供该字段，死代码保留至 M8 PDF 解析阶段）；回退链 akshare/baostock 提供 ex_date 可用 | reports/61 §3.2；manager.py |
| S7 | 源级限速（秒/请求）：cninfo 1.5、akshare_eastmoney 0.5、tdx 0.2、baostock 0.8、tencent 0.2、sina 0.35、eastmoney_f10 0.5 | config/default.yaml |
| S8 | CSRC 行业分类：**只取证监会标准**，显式 `start_date=19900101`、`end_date=今天`；源无分类如实 NULL + `missing_list(csrc_industry)`，禁止混用巨潮/申万/中证 | reports/86 |
| S9 | 价格主链：腾讯（连接复用）→ baostock → tdx；连续流水线 `price_fetch_pipeline_depth=64` | config；update.py |

## 3. DuckDB / 存储约束

| # | 约束 | 依据 |
|---|---|---|
| D1 | **单写者锁**：跨进程互斥（`app/core/storage/update_lock.py`）；所有 CLI 写命令必须经 `exclusive_update`；库层第二道 `_write_lock` | reports/84、92 |
| D2 | **DuckDB 1.5.5 同事务 `DROP INDEX`+`CREATE INDEX` bug**（`BoundIndex::CreateDeltaIndex` FATAL）：索引重建必须分事务（DROP 提交后再 CREATE）；升级评估为长期待办 | reports/81 F1 |
| D3 | `raw_response_archive` 冷热分层（schema v16）：`history`（冷）+ `active`（小）+ 视图 `raw_response_archive_all`；冷核对走 hash 集合，不触碰 BLOB | reports/96 |
| D4 | lineage hash 集合（schema v17）：`raw_response_archive_valid_hash` + `raw_response_archive_partitions`（5GB / 10万行 / 31天自动轮转，计数器维护避免每次 SUM 全扫） | reports/102 |
| D5 | 当前 schema 版本：`DUCKDB_SCHEMA_VERSION = 18`（app/core/storage/schema.py） | 代码 |
| D6 | `vd backup` 对 26GB BLOB 表须**分块导出**（raw_response_archive_history 5000 行分块），单次 COPY 会受内存限制 | reports/102 |
| D7 | **正式库 data/ 只读**：所有写操作必须经 CLI/维护脚本 + 单写者锁；S1 回归强制 `VD_ENV=test` + 正式库 SHA-256 指纹前后对比 | AGENTS.md；conftest.py |
| D8 | 数据库重建回滚快照（2026-09-01）：`data/valuedashboard.duckdb.old-20260901154717` + `.pre-rebuild-20260901`（同一 inode 硬链接，实占约 50GB）+ `valuedashboard.sqlite.pre-rebuild-20260901`；**回滚窗口 ≥ 1 个完整更新周期，观察通过后才可删除** | reports/101、102 |
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
| T1 | DuckDB 1.5.5 同事务索引 bug：升级评估（当前分事务绕过无复发路径） | reports/81 |
| T2 | P2 冷归档文件已生成，**未接入 CLI 恢复命令** | reports/102 |
| T3 | ✅ 已关闭（2026-09-01）：`config/user.yaml` 骨架已创建，覆盖通道激活；只写需覆盖的键 | 体检修复 2026-09-01 |
| T4 | CNINFO 分红 ex_date 剩余 8,467 行核验路径评估（PDF 解析 / 降级 / 明确依赖回退链） | STATUS 缺口 #4 |
| T5 | 东财行情源冷却到期后：单次探测，恢复后限速 ≤2 req/s、并发 ≤5 | STATUS |
| T6 | 08-13 单位 bug 期间保存的旧规则建议用户复核另存 | reports/81 |
| T7 | `test_dead_update_lock_does_not_mark_summary_stale` 完整 S1 中偶发 WinError 32（Windows unlink 竞态，单跑通过） | reports/77 |
| T8 | S1 既有失败 2 项待排查：国债 `test_snapshot_ttm_dividend_yield_and_spread` 种子日期矛盾；pdf 归档测试在 shim 环境 PermissionError | STATUS 进行中 |
| T9 | 前 6 名报告编号 05-24/26 全部 superseded，仅追溯用（正常） | STATUS |

## 8. 隐性知识回写规则（本文件的自我约束）

1. **对话中确认的任何新硬约束**（环境事实、数据源行为、保留期限、口径裁决）→ 当日登记到本文件对应章节，并更新 `last-reviewed`
2. **决策必须附着在决策物上**：代码/schema 级特殊设计（如 quarantine 表）在代码注释或本文件登记设计意图
3. **会话收尾清单**（每次会话结束前执行）：git commit → git push → 本文件/STATUS.md 知识提炼 → 删除实验产物（数据库副本、临时 CSV）
4. 引用本文件结论时标注：`docs/runbooks/ops-knowledge-base.md` + `last-reviewed: 2026-09-01`

---

*变更记录：2026-09-01 创建（从 reports/61/75/77/81/84/86/92/96/97/98/99/100/101/102、STATUS、config、代码核验聚合）。2026-09-01 体检修复：T3 关闭（创建 config/user.yaml 骨架）；E6 补充代理不可用时的直连回退命令。*
