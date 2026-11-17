---
title: 代码地图（CODE MAP）—— 想改 X 就读 Y
status: approved
category: reference
created: 2026-11-17
last-reviewed: 2026-11-17
---

# 代码地图

> **这份文件的唯一目的**：把"读懂本项目需要读 88 个模块 / 60,130 行"压缩成
> "按任务读 3–5 个文件"。配套数字与判断依据见 `reports/134`。
>
> 读法：**先看 §1 的 30 行心智模型，再从 §3 的表里找你今天要改的东西。**
> §4/§5 在你"看见一个词但不确定它什么意思"的时候查。

---

## 1. 心智模型（60 秒）

```
                      ┌───────────────────────── config/default.yaml（153 个键）
                      │
外部数据源 ──▶ adapters/（19 个适配器 + AdapterManager）
                      │        · 每个适配器只负责"取数 + 分类错误 error_kind"
                      ▼
              ┌───────────────┐         ┌────────────────────┐
              │ init.py       │ 首次全量 │ storage/           │  DuckDB（正式库 data/）
              │ backfill.py   │ 补历史   │  duckdb_store      │  SQLite（状态/锁/重试/日志）
              │ update.py     │ 日常增量 │  schema.py         │
              └───────────────┘         └────────────────────┘
                      │                          │
                      ▼                          ▼
              indicators/（calculator 算指标；extended 算扩展；roce 算 ROCE）
                      │
                      ▼
              quality/（asset_health 体检规则）+ data_quality.py（门禁/告警码/可信判定）
                      │
                      ▼
              web/api/*.py  ──▶  frontend/src/（Vue3）──▶ app/web/static/（构建产物，勿手改）
```

三条不变量，先记住它们比读代码有用：

1. **写库只有一个写者**：任何写 `data/` 的路径都必须拿 `storage/update_lock.py` 的排他锁；
   测试必须用 `app/core/storage/path_policy.py` 的隔离路径（`scripts/s1-pytest.ps1` 已封装）。
2. **状态必须由事实派生**：步骤/作业状态只经 `job_status.merge_step_status()`；`rejected` 吸收一切。
3. **派生列必须先登记**：不在 `indicators/calculator.py` staging 里的快照列，登记到
   `derived_snapshot_domains()`，否则发布时会被清空（`reports/133` R3）。

---

## 2. 三条主线（每条只看这几个文件就够）

### 2.1 日常自动更新（最常改）

| 顺序 | 文件 | 看什么 |
|---|---|---|
| 1 | `app/core/update.py` | `IncrementalUpdater`（6,575 行 / 136 方法，**先只看 `_run_incremental_update_flow` 里的 `report_step(...)` 序列**，那 25 行就是完整步骤清单） |
| 2 | `app/core/adapters/manager.py` | 多源回退、熔断、错误分类 |
| 3 | `app/core/job_status.py` | 状态怎么归并（80 行，先读它比读 update 划算） |

> 反直觉：`auto_update.py` 只负责**调度与子进程生命周期**，不负责更新内容；内容全在 `update.py`。

### 2.2 指标与筛选

| 顺序 | 文件 | 看什么 |
|---|---|---|
| 1 | `app/core/indicators/calculator.py` | 指标定义 + 快照发布（`_publish_snapshot*`、`_derived_columns_guard`） |
| 2 | `app/core/screening/engine.py` | `SNAPSHOT_COLUMNS` / `EXTENDED_COLUMNS`（**字段白名单的唯一来源**）+ DSL 执行 |
| 3 | `app/core/screening/field_units.py` | 单位口径（pct / percent / ratio），加字段必须在这登记 |
| 4 | `app/core/dsl/` | 表达式 DSL（parser/codegen/validator/engine），只在写自定义指标时需要 |

### 2.3 数据可信与展示

| 顺序 | 文件 | 看什么 |
|---|---|---|
| 1 | `app/core/data_quality.py` | 告警码 → `indicator_trust()` → 遮蔽哪些字段；发布门禁阈值 |
| 2 | `app/core/quality/asset_health.py` | 14 条体检规则 + `severity(block/disclose/inventory)` + `tier(cheap/expensive)` |
| 3 | `app/core/quality/null_reasons.py` | NULL 的 6 种含义（**先看这个再看 null_reason_sweeper**） |
| 4 | `app/web/api/stock_detail.py` | 详情页如何应用遮蔽、如何拼历史 |

---

## 3. 「我要改 X」索引

| 我要改… | 从这里进 | 联动改（容易漏） |
|---|---|---|
| 加一个指标（比率类） | `docs/skills/composite-indicator/SKILL.md` → `indicators/calculator.py` | `screening/engine.py`(白名单) `screening/field_units.py`(单位) `storage/schema.py`(列) `frontend/src/utils/screening-format.ts`(单位/标签) `derived_snapshot_domains()`(若外部算) |
| 加一个数据源 | `core/adapters/<name>_adapter.py` + `config/default.yaml` 的井号链 | `adapters/manager.py`(错误分类) `schema.py`(源名 Literal) `tests/regression/test_adapter_configuration.py` |
| 改更新顺序/加一步 | `update.py::_run_incremental_update_flow`（`report_step` 序列） | `job_status.merge_step_status()` 的归并、前端数据状态页字段 |
| 改发布门禁阈值 | `data_quality.py::gate_hard_block_threshold` | readiness (`web/main.py`)、`data_status.py`（必须同源） |
| 改体检规则 | `quality/asset_health.py`（规则表 + severity/tier） | `data_quality.py::_SNAPSHOT_BLOCKING_WARNINGS`、前端 `data-quality.ts` 的码表 |
| 改"空值为什么空" | `quality/null_reasons.py`（6 种）→ `null_reason_sweeper.py`（判定） | 前端 tooltip 文案表 |
| 改分红/除权 | `app/core/update.py::_refresh_market_actions` + `dividends` 写入路径 | 隔离区 `dividends_quarantine`、`asset_health` 的 `DIVIDEND_DATES_UNVERIFIED` |
| 改口径/单位 | `screening/field_units.py` | `frontend/src/utils/screening-format.ts`（RG3 门禁会拦） |
| 改 A+H 相关 | `core/ah_metrics.py`（算）+ `scripts/refresh_ah_market_snapshot.py`（落库） | `ah_market_snapshot` 表、`/api/stock/{code}/ah-metrics` |
| 改库结构 | `storage/schema.py`（DDL + `schema_migrations`） | `docs/STATUS.md` 的 schema 版本、`data_quality` 的 `SCHEMA_DDL_DRIFT` |
| 改前端页面 | `frontend/src/views/*.vue` + `frontend/src/types/*.ts` + `frontend/src/http.ts` | `npm run build` 会同步到 `app/web/static/`（`sync-static.mjs`），**不要手改 static/** |
| 加 CLI 命令 | `app/cli/main.py`（Typer 子 app） | 命名风格：现有 65 个 snake_case 与 24 个 kebab-case 混用，**新增一律 kebab-case** |
| 跑/修数据 | `scripts/` 下的 `repair_*`（16 个）/ `fetch_*`（11 个）/ `import_*`（12 个），默认 dry-run、`--yes` 落库 | 危险脚本规范见 `scripts/verify_no_dangerous_maintenance.py` |
| **删任何"看起来多余"的东西** | `docs/reports/136` §7（可移除清单）/ §8（不要删清单）→ 本文件 §9 | 先确认它防的是哪次事故；`AGENTS.md` §8 的 10 项永久保留 |

---

## 4. 七本词汇表（同一个词在不同地方含义不同，这是本项目最大的沟通成本）

| 词汇 | 取值 | 定义在哪 | 用途 |
|---|---|---|---|
| **作业/步骤状态** | `rejected > failed/error > partial > success/ok > skipped` | `core/job_status.py::_STATUS_SEVERITY` | 更新任务对外状态；`rejected` 吸收一切 |
| **适配器错误分类** | `none / legal_empty / per_symbol_data_error / source_unsupported / source_unavailable / deadline / truncated_ok` | `core/adapters/base.py::ErrorKind` | 决定是否计入熔断、是否重试 |
| **告警码** | `SNAPSHOT_STALE / LINEAGE_INVALID / MINIMUM_DATA_NOT_READY / ...`（26 个） | `core/data_quality.py` | 进 `warning_codes`，决定是否遮蔽整站/字段 |
| **体检严重度** | `block / disclose / inventory` | `quality/asset_health.py` | 只有 block 进 `warning_codes` / 退出码 |
| **体检成本档** | `cheap / expensive` | `quality/asset_health.py` | 每轮跑 vs 只给 `vd data doctor` |
| **空值原因** | `SOURCE_EMPTY / NOT_APPLICABLE / INVALID_DENOMINATOR / NOT_DISPATCHED / MASKED_UNTRUSTED / NOT_COMPUTED` | `quality/null_reasons.py::NullReason` | 解释"为什么这个格子是空的" |
| **可信/遮蔽** | `untrusted_all`（整站）/ `masked_fields`（字段级） | `data_quality.py::indicator_trust()` | 决定 API 返回 null 还是数值 |

> 一句话区分：**`partial` 是"这轮没全成功"，`disclose` 是"问题存在但不遮数字"，
> `MASKED_UNTRUSTED` 是"这个格子本来有值，但被判为不可信所以不显示"。**

---

## 5. 六组"同名不同物"（新人最容易踩）

| 现象 | 事实 |
|---|---|
| `data_quality.py` vs `quality/` | 前者是**门禁/告警/可信判定**（2,017 行）；后者是**体检规则 + 空值原因 + 归档核验**（4,275 行）。两者不是父子关系，是两套并行的"质量"概念 |
| `masked_fields_for_codes()` vs `masked_indicator_fields()` | 曾**同一个函数两个名字**（别名）。2026-11-17 已删除别名（`reports/137`），现在只有 `masked_fields_for_codes()` 一个公开入口，由 `test_trust_gate_single_entry.py` 守护 |
| `app/core/pdf/` | `reports/123` 说"PDF 方案已删除"——删的是**分红除权日 PDF 解析链**；`pdf/manager.py` 是**公告 PDF 下载/归档**，仍然活着（CLI `vd data download_pdf`） |
| `scripts/` vs `app/core/` 里的同名能力 | 75 个脚本里有 66 个**直接连 DuckDB**；它们承载了"一次性/维护"逻辑，不在运行时主链上。改运行时要看 `app/`，改历史数据要看 `scripts/`，两者**没有共享的仓储层** |
| `update.py` vs `init.py` vs `backfill.py` | 三个都能写行情/财务：`init`=首次全量、`backfill`=补历史缺口、`update`=日常增量。**同一个数据可能有三条写入路径**，改口径要三处一起看 |
| `domain` | 至少三种用法：①数据域（treasury/business）②DSL 字段域（`balance.`/`income.`）③空值原因域（`snapshot`/`ext`）。看上下文判断 |

---

## 6. 运行时 vs 维护面（决定你"要不要读"）

按 `reports/134` 实测：

- **运行时**（`app.web.main` ∪ `app.cli.main` ∪ `core.auto_update` 可达）：**88 模块 / 60,130 行**
- **维护面**（只被 `scripts/` 或测试调用）：**17 模块 / 3,055 行**
  - `quality/null_reason_sweeper.py` 1,279 行（只由 `scripts/classify_null_reasons.py` 使用）
  - `ah_metrics.py` 935 行（只由 `scripts/refresh_ah_market_snapshot.py` 使用）
  - `adapters/hk_price_adapter.py` 496 行、`storage/cold_archive_guard.py` 115 行
  - （`csmar_formulas.py` 198 行已于 2026-11-17 迁到 `tests/support/csmar_field_verdicts.py`，见 `reports/137`）

> 新人第一周**不要读维护面**。等你要跑数据修复时再按 §3 最后一行进。

---

## 7. 读法（1 天 / 3 天 / 1 周）

**第 1 天（4 小时，只求能改一个 bug）**
1. `AGENTS.md`（6 千字，规则与红线）
2. 本文件 §1–§3
3. `core/job_status.py`（80 行，整个状态语义都在这里）+ `core/update.py::_run_incremental_update_flow`（只看 `report_step(...)` 序列那 25 行）
4. 跑一次 `scripts/s1-pytest.ps1 tests/regression`（了解门禁长什么样）

**第 2–3 天（建立"为什么这么多门禁"的直觉）**
5. `docs/reports/133`（7 类返工根因）+ `docs/reports/117`/`118`（一次真实事故的完整链条）
6. `core/data_quality.py` 的 `indicator_trust()` 与 `gate_hard_block_threshold()`
7. `quality/asset_health.py` 的 severity/tier 注释块（不用读 14 条规则实现）

**第 1 周（按任务深入）**
8. `screening/engine.py` + `dsl/`（如果要做指标/筛选）
9. `codegraph`/import 图：`uv run python scripts/onboarding_metrics.py --compare <baseline>`
10. 需要历史时再按 §3 查 `docs/reports/NNN`（**注意 59/134 已 superseded（44%），先看 front-matter**）；
    要找某个测试测什么，先看 `tests/regression/README.md` 的旧名→新名映射表

---

## 8. 读代码时反复会遇到的 5 个"为什么"

| 疑问 | 答案（一句话） |
|---|---|
| 为什么每个模块顶部都有 `reports/NNN`？ | 代码里 325 处引用报告：这些是**事故编号**，不是设计文档。行为看 docstring 第一段，编号只在追责/溯源时看 |
| 为什么有的函数 500 行不分拆？ | 多数是"事故修复时把 5 个分支塞进同一事务"的历史产物（`_update_prices_incremental` 501 行）。拆分方案见 `reports/134` §5 S1 |
| 为什么判空/判可信要跨 8 个文件？ | 可信判定只有 `indicator_trust()` 一个出口，但那 8 个文件都在**调用**它；改判据只改 `data_quality.py` |
| 为什么有 19 个 adapter 又有 19 个 `_to_float`？ | `_to_float` 在 7 个模块各有一份，**语义还不一样**（有的拒 inf、有的不拒）——这是已知债务，见 `reports/134` §5 S5 |
| 为什么测试文件叫 `test_fix20261112_*`？ | 以前按事故日期命名；2026-11-17 已全部改为按行为命名（`incident_named_tests` 25 → 0），旧名→新名映射见 `tests/regression/README.md`；新测试请按 `test_<域>_<行为>.py` 命名 |

---

## 9. 已确认可移除 / 不要删（`reports/136` §7 / §8）

**要删东西之前只读两处**：`docs/reports/136` **§7 可移除清单**（按 L0→L3 风险排序，可直接执行）
与 **§8 不要删清单**（每一条都对应一次已发生的事故）。

- **已执行（2026-11-17，见 `reports/137`）**：`/design-preview` 路由 + `ResearchWorkbenchPreviewPage.vue`（123 行假数据）+ `App.vue` 里 4 处 `staticPreview` 死分支、`masked_indicator_fields()` 纯别名、`csmar_formulas.py` 迁入 `tests/support/`、S0 runbook 改 `historical`、`_domestic_direct` 全套（`adapters/_net.py` 已删）、25 个事故命名测试改为行为命名、打包配置改为构建期自动同步。
- **已撤回（复核后发现不能删/不能改）**：① `scripts/legacy_dangerous/` 不能删——`test_maintenance_scripts_safety.py` 把它当固定 fixture（存在性 + 内容断言）；② `app/web/static/index.html` 保留跟踪——不是为了过 wheel 测试（那测试本来就要求构建后产物），而是为了让 fresh clone 直接能服务 SPA 根路径；③ 3 处 `trust_env=False` 从清理提交撤回——它会真实改变代理路由且失败静默，属需真机验证的独立决策。理由与证据见 `reports/137` §3。
- **待实测/下一轮**：`screening_readiness_cache`（先测干净库冷启动耗时）、schema 迁移折叠、`null_reason_sweeper` 迁出、`_to_float` 剩余 8 份（`supplement`/`financial_guards` 等）、`_to_date` ×6、`data_quality` 与 `quality/` 合并、`update.py` 完整拆分（见 `reports/137` §5）。
- **不要删**：`path_policy`/S1 包装器、`update_lock`、`ErrorKind` 七分类、`merge_step_status`、门禁阈值 `max(50,1%)`、`field_units` 双份、`DerivedSnapshotDomain`、`cold_archive` 视图、`cli/protocol` 的 plan→confirm、`dividend_yield` shim —— 逐条理由与对应事故见 `AGENTS.md` §8。
- **L3 需实测**：`screening_readiness_cache`（`reports/136` §6.5）先测干净库冷启动耗时再决定，未实测前不动。
