---
title: 全项目系统性红队审查报告（第五轮，独立复核，2026-08-02）
status: superseded
category: reports
created: 2026-08-02
last-reviewed: 2026-08-02
superseded-by: reports/35_SYSTEM_RED_TEAM_FIX_2026-08-02.md
supersedes: reports/30_AUDIT_FIX_CLOSURE_2026-08-02.md
---

# 全项目系统性红队审查报告（第五轮，独立复核，2026-08-02）

> 对 `reports/29`–`reports/33` 所宣称的「代码层通过、发布级红队 P0/P1/P2
> 全部关闭」进行独立复核：不引用既有报告结论，重跑全部自动化门禁、对
> 后端/前端/存储/适配器/DSL/备份/CLI/打包做源码级审计，并用合成数据对抗
> 实测关键缺陷路径。正式数据库全程只读。
>
> **裁决：BLOCK。** 发现 4 个未被此前报告覆盖、无测试防护的 P1 数据完整
> 性缺陷，均会在主要研究流程中静默产出错误、不完整或口径错误的研究结果，
> 与项目自身的可信度合同（PRD §7.4/§9/§12/§15）冲突。

## 1. 审查方法

- 范围：权威状态与需求合同 → 后端与数据安全 → 前端与发布运维 → 门禁实测 → 对抗复现。
- 独立性：以 `docs/STATUS.md` 声明的状态为待证命题，自行核验源码、可执行门禁与证据 JSON。
- 不触碰正式数据：全部动态验证使用临时目录合成数据；正式库仅只读哈希比对。

## 2. 门禁实测结果（本次会话独立重跑）

| 门禁 | 结果 | 说明 |
|---|---|---|
| `scripts/s1-pytest.ps1 tests/regression` | **392 passed**（132.7s） | S1 路径隔离完整，正式库哈希前后不变 |
| `uv run --locked ruff check app tests/regression` | All checks passed | 零 F821/语法问题 |
| `npm run lint` | Pass | eslint src --ext .vue,.ts |
| `npm run test` | 52 node + 9 vitest 全过 | 数据质量/筛选质量/个股详情/指标可信/组件流程 |
| `npm run build` | Success（1.58s） | vue-tsc -b + vite build + sync-static |
| 正式库 SHA-256（审查前/后） | DuckDB `741C75BE…`、SQLite `3D41498F…` 不变 | 只读性证明 |
| `git status` | 干净 | 19 提交，origin 已配置 |
| 正式库证据复读 | ready=TRUE、warning_codes=[]、mismatches=0、筛选 451ms/3,878 只 | `docs/evidence/evidence-formal-*20260802.json` |

## 3. 核验通过的优势项（无发现）

- **注入面**：SQL 全参数化；DSL 语法锁定（grammar.lark 无字符串字面量、固定算子）；恢复表名白名单；无 eval/exec/subprocess/pickle。
- **路径安全**：PDF 提供、归档、备份恢复均 resolve + `relative_to` 双重校验。
- **写入纪律**：upsert 全链 `COALESCE(excluded.x, table.x)` 防空值覆盖（有测试）；快照 staging + 行数校验原子发布；`snapshot_period_mismatches` 同时作用于门禁与引擎。
- **并发**：三层跨进程写串行（`.duckdb.write.lock`、`exclusive_update`、`exclusive_maintenance`）。
- **Web 写面**：per-launch 256 位令牌 + Origin 校验 + TrustedHostMiddleware；无 CORS（跨源不可读令牌）；已测试。
- **备份**：AES-256-GCM + PBKDF2-600k + HMAC 清单 + 回滚日志 + 3 代轮换。
- **CLI**：全部正式响应含 `schema_version`；危险操作两段式 15 分钟单次 plan_id；无自由执行入口。
- **适配器**：零 DuckDB/SQLite 引用，结构上不可能直接写正式库。

## 4. BLOCK 依据：4 个未缓解 P1（全部源码确认，2 个已对抗实测复现）

> 下列缺陷均**不在** `reports/30`–`reports/33` 的关闭清单中，且无任何回归测试覆盖，
> 故「发布级红队 P0/P1/P2 全部关闭」的声明不成立。

### P1-A 季度视图单季值造假（报告期缺失时）

- 位置：`app/web/api/stock_detail.py:94-112` `_to_single_quarter`。
- 机理：累计（YTD）口径下，以「同年度上一可得行」做差分；当某年中报缺失（源端缺口/滞后）时，Q3 的"单季值"= Q3累计 − Q1累计（吞入 Q2 全部），Q1 更会被减成负值；不返回 `null`、无原因码。同模块 `calculate_ttm_trend`（44-91 行）对同一缺口是 fail-closed（`continue` 丢弃），形成同一页面两套行为。
- 实测复现：Q1=100 + Q3=340（无 Q2）→ 推导 Q3 单季 revenue=340.0（应为 140 或 `None`）。
- 影响：季度视图（PRD §14 必选能力）产出错误财务结论；年度默认视图不受影响。
- 测试缺口：`test_stock_detail_periods.py` 仅覆盖连续 Q1→Q2 场景，无缺口夹具。

### P1-B 股票池刷新静默退市（部分响应截断时）

- 位置：`app/core/init.py:227-255` `_fetch_stock_universe`；上游 `app/core/adapters/akshare_adapter.py:259-302`。
- 机理：刷新逻辑对响应中出现的交易所执行 `UPDATE stock_meta SET is_listed=FALSE WHERE exchange IN (…)`，仅按「交易所整体缺失」防护，**无代码数量完整性检查**；AKShare 适配器逐调用吞异常，部分列表仍以 `confidence="strict"` 返回、无错误标记。截断响应 → 数千只有效股票被静默置为未上市，基础池、排名分母、市值输入同步收缩；`minimum_data_readiness` 在收缩后的池上重算仍可能返回 `ready=True`；最长静默一天（`universe_refresh_interval_days`）。
- 影响：筛选结论与全市场排名错误；可恢复（重跑刷新）但无告警、无门禁。
- 测试缺口：无部分响应/截断股票列表对 `is_listed=FALSE` 路径的模拟测试。

### P1-C 筛选结果静默截断（LIMIT 5000）

- 位置：`app/core/screening/engine.py:416`（`LIMIT 5000`）、`engine.py:234`（`"total": len(results)`）；API 不返回截断标志；前端 `ScreeningResultsPanel.vue:322` `:max="5000"` 无提示。
- 机理：正式池 5,534 只 > 5,000。用户按 PRD §12.3 合法切换基础池（包含 ST/停牌/上市不足 1 年）后，匹配全池的规则被静默截断为排序后前 5,000 行，`total` 也错误地等于 5,000；保存结果与 CSV 导出同病。
- 实测复现：合成池 6,000 只 + 全匹配规则 → 返回 5,000 行、`total=5000`、响应无 `truncated` 键。
- 影响：静默丢失排序靠后的股票——恰好可能是用户要找的标的；违反 §12.3 排名分母同步与 §6.7 当前全集可筛口径。
- 测试缺口：无「匹配数 > 5000」行为断言。

### P1-D 行业排名口径错标（CSRC 计算、申万命名）

- 位置：`app/core/screening/engine.py:366-385`（`_sw1_rank/_sw2_rank` 实际 `PARTITION BY csrc_l1[/csrc_l2]`，纯 CSRC 口径）；`app/web/api/screening.py:428-429` 以「申万一级/二级排名」标签暴露给 UI；正确命名的 `{field}_industry_rank`（CSRC）未向 UI 提供。
- 影响：方法学误导——用户以为在看申万排名，实为 CSRC 口径；且无法在 UI 选用正确命名的 CSRC 排名列。
- 测试缺口：`test_screening_strict_and_ranks.py` 只验证数值/NULL 语义，未断言列名与标签口径。

## 5. 次要发现（P2，发布阻断类前 3 项）

1. **根 README.md 与 STATUS 严重脱节**：仍称「最终诊断与抽样确认中」、描述已废除的遗留构建后端警告、推荐的直接启动路径与同一文档所述的 profile 拒绝逻辑矛盾、保留 2026-07-22 漂移/冻结措辞——足以误导安装与运维。
2. **启动器与已签署路径隔离合同冲突**：`start.bat`/`vd.bat` 自动设置 `VD_ENV=formal` + `VD_FORMAL_ACK=confirmed`，而 `docs/contracts/path-isolation-contract.md` §2.1/§2.4/§10.2/§14 要求「不得自动设置，仅检查外部提供」。Python 层强制仍有效，但任何脚本/CI 经启动器即自动"人工确认"正式写访问。发版前须裁决：改合同或改启动器，不得并存。
3. **58 个构建产物被 git 跟踪**（`app/web/static/assets/`，`.gitignore` 已忽略但 `git ls-files` 可见），违反 AGENTS.md「永不提交构建产物」，重建前端即产生脏树，需 `git rm --cached`。
4. 筛选页草稿 409 后 `draftHydrated=false` 永久停用本会话自动保存（`ScreeningPage.vue:174-176`）。
5. strict-only 开关仅客户端过滤不重跑，保存/导出的 `_strict_only` 标志可能与 UI 开关矛盾（`ScreeningResultsPanel.vue:79-92`）。
6. 打包版恢复指引显示 `python -m app.cli.main …` 而非 `vd.bat`/exe（`DataTraceability.vue:84-86`）。
7. 写令牌单次拉取缓存；服务端重启（每次启动新令牌）后写请求 403 直至手动刷新（`frontend/src/http.ts:12-16`）。
8. Node 版本要求无 `engines` 字段且未文档化（Vite 8/`--experimental-strip-types` 需 Node ≥20.19/22.6）。
9. Runbook 仅有 S0 手册；备份/恢复、重建、自动更新运维、build-release 均无 runbook；`chain-finalize.ps1` 硬编码正式路径且证据写出治理目录。
10. 静态个性化数据（规则/自选/保存结果）在 SQLite 明文存储，加密仅覆盖备份——PRD §18.3「个性化数据必须加密」存在解释缺口，需所有者明示意图。
11. 非 Windows 下 `CredentialManager` 明文回退（dev 路径，静默行为）。
12. CSV 导出 `_csv_cell` 仅防护首字符，` =cmd()` 类单元格可滑过（`screening.py:24-27`）。
13. 杂项：DSL 草稿被依赖时删除 500 崩溃；PDF 404 与 `/api/db/status` 泄露绝对路径；watchlist 接受任意股票代码；`save_rule` 接受客户端 `status`；草稿 PUT 无大小上限；job_logs/missing/plans 无 GC；更新锁被拒时状态误记为 `failed` 而非 `skipped`；`max_stocks` 切片无 ORDER BY；过期 plan 从不清理。

## 6. 裁决

**BLOCK（不可正式启用）。**

第一性原理判断：本项目的立身之本是「数据缺口、近似值、失败与来源冲突都被明确标记，结论不静默降级」（PRD §2.3/§9），而 P1-A～P1-D 恰好都在主要研究路径上**静默**产出错误或不完整输出，且与 STATUS.md「发布级 P0/P1/P2 全部关闭」声明相悖。投资研究工具在存在无告警的错误结论路径时不能正式启用；全部自动化门禁通过（392 + ruff + 前端全链）与安全控制强劲（零 P0 安全项）仅说明质量基线高，不能抵消未缓解的正确性缺陷。

## 7. 退出条件（小而精确）

1. **P1-A**：`_to_single_quarter` 在缺少同年度上一报告期行时返回 `null` + 原因码（参照 `calculate_ttm_trend` 的 fail-closed 语义）；补 Q1+Q3 无 Q2 缺口回归测试。
2. **P1-B**：`_fetch_stock_universe` 增加代码数量完整性校验（如 ≥ 前次池 90%），不足时保留旧状态并写入重试/警告；补部分响应回归测试。
3. **P1-C**：解除/修正 `LIMIT 5000` 至全池上限，响应增加显式 `truncated` 标志（前端提示、CSV 标注），`total` 改为真实匹配数；补 >5000 匹配回归测试。
4. **P1-D**：`sw1/sw2` 后缀与标签整体迁移为 CSRC 命名（`industry_rank/industry_percentile`），UI 暴露正确命名列；补标签/命名契约回归测试。
5. **P2**：README 与 STATUS 对齐；裁决并修复启动器 vs 路径隔离合同冲突；`git rm --cached` 清理 58 个已跟踪构建产物；就 PRD §18.3 静态个性化数据加密作出所有者决定并落实/文档化。
6. 重跑全部门禁（S1 392、ruff、前端 lint/test/build），更新 STATUS.md 与证据。

## 8. 证据索引

- 门禁与复现实测机器证据：`docs/evidence/evidence-redteam-gates-20260802.json`、`docs/evidence/evidence-redteam-repro-20260802.json`
- 正式库只读基线（复读）：`docs/evidence/evidence-formal-status-20260802.json`、`docs/evidence/evidence-formal-screening-20260802.json`
- 会话产物：`.planning/2026-08-02-system-red-team/`（task_plan/findings/progress，含复现脚本结果）
