---
title: 新人接手认知成本评估与简化方案（2026-11-17）
status: approved
category: reports
created: 2026-11-17
last-reviewed: 2026-11-17
---

# 认知成本评估：一个能力很强但完全不懂项目的人，明天来接手

> **结论先说**：这个项目的问题**不是"代码写得差"**，而是**"一个 6 万行的单人系统，
> 把 4 个月的事故史直接编码进了结构里"**。具体表现是三个数字：
>
> * 懂"日常更新"这一条链，要读 **57 个模块 / 39,958 行**；
> * 懂"整个后端"要读 **88 个模块 / 60,130 行**（= 全部运行时代码）；
> * 开工前按项目自己的规则必读的文档是 **173,775 字符**（≈ 7 小时纯阅读，还没算 131 份报告）；
>   本次新增的 `CODE_MAP` 让这个数字变成 **182,422**——多读 8.6 千字，换取少读几十万字。
>
> 好消息：**成本高度集中**。1 个文件（`update.py`，6,575 行）占了 app 代码的 10%、
> 1 个类（`IncrementalUpdater`）有 136 个方法、35 个函数超过 150 行。
> 因此下面 §5 的方案是"先拆 3 个热点 + 建 1 张地图"，而不是"重写架构"。

---

## 1. 评估口径（可复现）

```bash
python scripts/onboarding_metrics.py                       # 本次新增的度量脚本
python scripts/onboarding_metrics.py --json                # 机器可读
python scripts/onboarding_metrics.py --compare docs/reference/onboarding-baseline-2026-11-17.json
```

指标含义写在脚本 docstring 里。**故意不做成门禁**：文件行数不是"对错"，只是"贵不贵"；
需要用 `--fail-on-regression` 显式打开（见 §5 S0 的取舍说明）。

---

## 2. 硬数字（2026-11-17 实测）

| 维度 | 数字 | 为什么这是成本 |
|---|---|---|
| 运行时代码 | `app/` **105 文件 / 63,185 行** | 逐行读完 ≈ 25–30 小时；理解（非通读）≈ 2 周 |
| 维护面代码 | `scripts/` **76 文件 / 20,942 行**（其中 66 个直接连 DuckDB） | 第二条代码库，与运行时无共享仓储层 |
| 测试 | `tests/regression` **156 文件 / 43,408 行** | 测试比 app 少 30%，但 25 个按事故日期命名（`test_fix20261112_*`） |
| 最大单文件 | `app/core/update.py` **6,575 行** | 一个文件 = app 的 10.4% |
| 最大单类 | `IncrementalUpdater` **136 方法 / 6,315 行** | 编排 + 12 个域刷新 + 价格流水线 + 重试 + 公告 + 快照发布全在一起 |
| 最大单函数 | `init_duckdb_schema()` **912 行** | 一个函数里 ~60 条 DDL + 迁移 |
| 长函数 | **>150 行：35 个**；>80 行：**112 个** | 单次阅读上限通常在 50 行 |
| 大于 1000 行的模块 | **17 个** | 一半以上的核心逻辑在这 17 个文件里 |
| 入口可达闭包 | `app.core.update` → **57 模块 / 39,958 行**<br>`app.cli.main` → **88 模块 / 60,130 行**<br>`app.web.main` → **81 模块 / 54,919 行** | "改一个更新步骤要读懂多少"的真实答案 |
| 循环导入 | **4 组**（`data_quality ↔ screening.engine ↔ asset_health`、`web.main ↔ api.data_status`） | 说明"质量/筛选/展示"三者的职责边界没定清 |
| 同名顶层函数 | **19 组**跨模块重复（`_to_float` ×7、`_to_date` ×6、`_parse_date` ×3 …） | 看名字以为一样，行为不一样（§4.2） |
| 代码里的 `reports/NNN` | **325 处** | 读代码必须同时读报告才能知道"为什么这么写" |
| Day-1 必读文档 | **173,775 字符**（STATUS 55,856 / 知识库 50,011 / 合同 35,460 / PRD 23,331 / AGENTS 6,257 / README 2,860）；加入 `CODE_MAP`（8,647）后 **182,422** | 其中 STATUS 一页就 5.5 万字，且是"当前状态唯一权威" |
| 报告 | **132 份 / 2.3 万行**（含本次 133/134），其中 **59 份 superseded（45%）** | 新人第一件事是判断"哪些不能读" |
| 维护面 | **17 模块 / 3,055 行**不在运行时可达范围 | 可以先不读——但没人告诉他这件事 |

**综合工时估算**（能力很强、中文母语、无领域背景）：

| 目标 | 需要 | 估算 |
|---|---|---|
| 能改一个 bug 并跑通门禁 | 读地图 + 3 个文件 + 1 次 S1 | **0.5–1 天**（有 `CODE_MAP` 之后；没有则 2–3 天） |
| 能加一个比率指标不动坏别的 | 上面 + calculator/screening/field_units/schema/前端 + Skill | **2–3 天** |
| 能独立处理一次"更新 partial"事故 | 上面 + update.py + data_quality + asset_health + 117/118 报告 | **1.5–2 周**（`update.py` 是主要成本） |
| 能重写某个数据域 | 上面 + adapters/manager + 该域 scripts + 相关 6 份报告 | **3–4 周** |

---

## 3. 成本集中在哪：Top 12 热点

> 排序依据是"读它要多久 × 多少人会碰它"。每行都给**具体症状**与**最小可行改法**（不是"重写"）。

| # | 位置 | 规模 | 认知症状 | 最小可行改法 |
|---|---|---|---|---|
| 1 | `core/update.py::IncrementalUpdater` | 6,315 行 / 136 方法 | 编排、12 个域刷新、价格流水线、重试、公告、快照发布混在一个类；改任一步都要读 6 千行找"我在第几步" | 抽 `STEP_PLAN` 声明式步骤表 + 每个域一个 `steps/<domain>.py`（S1） |
| 2 | `update.py::_run_incremental_update_flow` | 492 行 / 25 个 `report_step` | 步骤顺序只存在于**代码执行顺序**里，没有清单；插入新步骤最容易放错位置 | 同上：`STEP_PLAN` 一张表，`for step in STEP_PLAN: report_step(step.run(ctx))` |
| 3 | `core/storage/schema.py::init_duckdb_schema` | 912 行 | 60 张表的 DDL + 迁移 + 注释全在一个函数；想查"这列谁加的"只能全文搜 | 按域拆 `schema/domains/<domain>.py` + 有序 migration 列表（S2） |
| 4 | `core/indicators/calculator.py` | 2,707 行 / 55 方法 | 指标计算 + 快照发布 + 派生列守卫 + lineage 混在一起；"加指标"必须读懂发布 | 拆 `calculator/`（calc / publish / lineage 三个模块）（S3） |
| 5 | `core/quality/asset_health.py` | 2,382 行 | 14 条规则 + severity + tier + 结构性可空白名单 + 前端码表映射 | 规则声明与引擎分离：`rules/<code>.py` 返回 `Finding`（S4） |
| 6 | `core/data_quality.py` | 2,017 行 | 门禁阈值、告警码、可信判定、缓存、missing 口径全在里面；与 `quality/` 并行存在 | 拆 `quality/gates.py` + `quality/trust.py`，`data_quality.py` 只留 re-export（S4） |
| 7 | `web/api/stock_detail.py` | 1,749 行 / 34 个函数 | 详情页聚合、欺骗判定、AH、时间线、历史序列全在一个路由文件 | 按 section 拆 router（S6，低优先） |
| 8 | `cli/main.py` | 2,968 行 / 92 条命令 | 命令注册散在 2 千行里；命名 65 snake_case vs 24 kebab-case 混用 | 拆 `cli/commands/<group>.py`；新命令统一 kebab-case（S6） |
| 9 | 7 份 `_domestic_direct` / 7 份 `_to_float` | 131 行重复（已消除 `_domestic_direct`） | 同名函数**语义不同**：`_to_float` 有的拒 ±Inf、有的不拒 | 统一到 `adapters/_net.py`（已做）+ `core/numbers.py::to_float(*, allow_inf)`（S5） |
| 10 | `scripts/` 66 个直连 DuckDB 的脚本 | 20,942 行 | 与运行时两套写入路径，没有共享仓储层；`repair_*` 16 个、`import_*` 12 个、`fetch_*` 11 个 | 引入薄仓储 `app/core/repo/`，脚本只调它（S6，长期） |
| 11 | `core/pdf/` 928 行 | 3 文件 | 报告 123 说"PDF 方案已删除"，但 `pdf/manager.py` 仍活着且被 `stock_detail.py` 顶层 import | 改名为 `announcement_pdf/`，并在报告 123 加一行"删的是除权日解析，不是公告下载"（S7） |
| 12 | 警示语/States 词汇 | 7 套（见 `CODE_MAP` §4） | `partial` / `disclose` / `MASKED_UNTRUSTED` 三个词都表示"不完美"，但语义完全不同 | 生成 `docs/reference/GLOSSARY.md`（已并入 CODE_MAP §4）+ 枚举集中到 `core/vocab.py`（S8） |

---

## 4. 四类具体问题

### 4.1 命名混乱（可机械修）

| 症状 | 证据 | 建议 |
|---|---|---|
| CLI 命名两种风格混用 | 92 条命令：65 `snake_case` + 24 `kebab-case`（同一 `data` 组里 `refresh_universe` 与 `refresh-csrc` 并存） | 新命令一律 kebab-case；旧命令保留但加 deprecation 提示 |
| 同一函数两个名字 | `masked_fields_for_codes()` / `masked_indicator_fields()` 是同一实现 | 保留一个，另一个删（调用点 9 处，机械替换） |
| "quality" 两套并行 | `data_quality.py`（门禁）vs `quality/`（体检/空值/核验） | 前者改名 `gates.py` 并入 `quality/` |
| `domain` 一词三义 | 数据域 / DSL 字段域 / 空值原因域 | 文档层先约定（CODE_MAP §5），代码层按 `data_source` / `field_domain` / `reason_scope` 区分 |
| 按事故命名的测试 | 25/156 个 `test_fix20261112_*` | 按行为重命名，事故编号移进 docstring（S7，纯机械） |

### 4.2 抽象过度（"为了防御曾经的事故"）

| 抽象 | 规模 | 是否值得 | 判断 |
|---|---|---|---|
| DSL（`core/dsl/`：parser + codegen + validator + engine + ast_nodes + derived_fields） | 2,404 行 | ✅ 值得 | 42 个大师指标调研（`reports/131`）证明"表达式化"确实是需求 |
| `storage/path_policy.py`（`VdEnv` / `DatabasePathSet` / 隔离校验） | 619 行 | ✅ 值得 | 直接来自"测试污染正式库"的事故（`incident-2026-07-22`），删了会复发 |
| `ErrorKind` 七分类 + 熔断桶 `(adapter, data_type)` | ~120 行 | ✅ 值得 | 来自"5 只股票让 3 个源同时熔断"（`reports/117` P0-4） |
| 三套适配器注册/回退链（`AdapterManager` + `SourceMetadata` + 每域 updater 注入） | 773 行 + 19 适配器 | ⚠️ 偏高 | 多源回退确实需要，但"每域各自 new 一个 manager"造成过熔断隔离失效（`reports/118` §7-3）。应收敛成**一个** registry |
| `DerivedSnapshotDomain` 注册表（本次新增） | ~120 行 | ⚠️ 中性 | 替代了"每个派生列手写补丁"的更差做法；代价是新人多一个概念。**再增加第 3 个概念前应重新评估** |
| `cli/protocol.py`（plan 过期、token、`--yes` 交换） | ~250 行 | ❓ 待评估 | 单人本地工具是否需要"会话令牌"值得问一句 |

### 4.3 职责不清晰（同一个动作有 3 个入口）

| 动作 | 入口 | 后果 |
|---|---|---|
| 写入行情/财务 | `init.py`（全量）/ `backfill.py`（缺口）/ `update.py`（增量）/ 66 个 `scripts/*.py` | 同一口径改动要动 4 处；曾经出现"三条路径口径不同"（`reports/126` 市值 3 套） |
| 判定"数据能不能信" | `data_quality.py`（告警码/阈值）+ `quality/asset_health.py`（规则）+ `missing_conditions.py`（发布门禁输入） | 三处各有一套"缺口"概念；`reports/117` 里 `missing_count` 出现过 11,823 / 162 两个数 |
| 派生值计算 | `calculator`（快照内）+ `roce.py`（外部聚合）+ `extended.py`（indicator_ext）+ `ah_metrics.py`（脚本触发） | 四种"派生"生命周期不同，新人无法从目录判断"新指标该放哪" |

### 4.4 需要大量上下文才能读懂的代码（`reports/NNN` 依赖）

代码里 **325 处** `reports/NNN` 引用。典型段落读起来像"事故编号 + 反事实"，
而不是"这段代码做什么"：

```python
# 2026-11-12 P0 修复：此前 update.py 用
# ``{**后一个子步骤, "full_recompute": 前一个}`` 覆盖外层 status，
# 于是"国债利差刷新成功"抹掉了"指标快照被门禁 rejected"的事实。
```

这类注释**对复盘有价值、对新人有害**（必须读 117 才能理解）。建议格式：

```python
# 契约：子步骤状态按严重度归并，rejected 永不被 success 覆盖。
# 历史：2026-11-12 曾因覆盖式合并导致 14 天假绿（reports/117 P0-2）。
```

即：**第一行必须是"现在的行为契约"，事故编号放第二行**。

---

## 5. 简化方案（分四阶段，每阶段都有"可验证的完成标准"）

> 原则：**不重写、不搬迁业务逻辑、每步都可单独上线并可回滚**；
> 每阶段结束跑一次 `onboarding_metrics.py --compare` 证明数字真的变小。

### S0 已经做了的（本次交付，零风险）

| 项 | 产出 | 效果 |
|---|---|---|
| 代码地图 | `docs/reference/CODE_MAP.md` | 把"读 88 个模块"压缩成"按任务读 3–5 个文件"；含 7 本词汇表 + 6 组同名陷阱 + 1/3/7 天读法 |
| 度量脚本 | `scripts/onboarding_metrics.py` + 基线 JSON | 让"变简单了吗"可量化，而不是感觉 |
| 重复消除（样例） | 7 份 `_domestic_direct` → `adapters/_net.py` 1 份（−131 行） | 证明"合并副本"是低风险动作；同时在 docstring 写清进程级环境变量的设计缺陷 |

### S1 拆 `update.py`（最高优先，收益最大）

**目标**：6,575 行 → 编排 ≤ 600 行 + 每域 ≤ 400 行。

1. 定义 `UpdateContext`（stores、adapter_mgr、config、progress_cb、max_stocks…）—— 现在这些东西靠 136 个方法共享 `self.*`，隐式耦合。
2. 定义 `Step` 协议：`name: str` / `run(ctx) -> dict`（返回值就是 `report_step` 的入参）。
3. 把 25 个 `report_step(...)` 抽成 `STEP_PLAN: tuple[Step, ...]`，顺序**第一次**变成数据而不是控制流。
4. 12 个 `_refresh_<domain>` 方法分别搬到 `app/core/update/steps/<domain>.py`；
   价格流水线（`_update_prices_incremental` 501 行）单独成 `steps/prices.py`，内部再拆 fetch / write / lag 三段。
5. 保留 `IncrementalUpdater` 作为**薄外观**（`run() -> aggregate_job_status`），保证 CLI/自动更新调用点零改动。

**完成标准**：`update.py` ≤ 600 行；无 >150 行函数；`STEP_PLAN` 一眼可见 25 步；
S1 全绿；`onboarding_metrics` 的 `closure::app.core.update` LOC 下降 ≥ 30%（注意：拆包不减少闭包 LOC，
所以真正的验收指标是 **`top_file_loc`** 与 **`functions_gt_150`**）。

> 坦白说明：**拆包不会降低"读懂整条链要多少 LOC"**（闭包 LOC 反而可能微增）。
> 它降的是"定位成本"和"单次阅读量"。如果目标是降闭包 LOC，只能靠 **S6 合并重复能力**。

### S2 拆 `schema.py`（机械，收益立竿见影）

- `schema/domains/{meta,prices,financials,market_actions,quality,archive,...}.py` 各导出 `DDL: str`
- `schema/migrations.py` 用有序列表表达 v1..v45；`init_duckdb_schema()` 变成 40 行循环
- 加一条测试：DDL 里出现的每张表都必须属于某个 domain 模块（防止又长回一个大函数）

**完成标准**：`init_duckdb_schema` ≤ 60 行；单文件 ≤ 500 行；`SCHEMA_DDL_DRIFT` 仍为 0。

### S3 拆 `calculator.py`（中等风险，需 ROCE/守卫回归护航）

- `publish.py`（`_publish_snapshot*` + `_derived_columns_guard` + 行数校验）
- `calc.py`（`compute_all_for_stock` 及各指标私有方法）
- `lineage.py`（`_record_derived_lineage*`）
- 保持 `IndicatorCalculator` 门面，方法体改成委托

**完成标准**：三个文件各 ≤ 1,000 行；`test_roce` / `test_snapshot_derived_columns` / `test_snapshot_atomicity` 全绿。

### S4 收口"质量"三套（消除职责重叠）

- `quality/gates.py`（阈值 + `snapshot_publish_gate`）、`quality/trust.py`（`indicator_trust` + 遮蔽）
- `quality/asset_health.py` 只保留规则声明 + 执行器；规则拆 `rules/`
- `data_quality.py` 变成 re-export 层（30 行），所有调用点不动
- 顺手解决 2 个循环导入（`data_quality ↔ screening.engine`、`web.main ↔ data_status`）

**完成标准**：`import_cycles == 0`；`grep -r "def indicator_trust"` 只有 1 处；
`masked_fields_for_codes` 与 `masked_indicator_fields` 只留一个。

### S5 数字/日期工具（低风险，直接消灭 19 组同名函数）

- `core/numbers.py`：`to_float(value, *, allow_inf=False)`、`to_int`、`is_finite`
- `core/dates.py`：`to_date` / `parse_date` / `cn_today`（`cn_today` 已在 `financial_period`，合并到一个模块）
- 逐个适配器替换；**`_to_float` 的语义差异要逐处确认**（当前 3 种：pandas NaN / 拒 Inf / 裸 float）

**完成标准**：`duplicate_helper_names` ≤ 8；适配器测试全绿。

### S6 两条代码库的边界（长期，但先立规则）

- 新增 `app/core/repo/`（薄仓储：`read_prices()` / `upsert_dividends()`…），`scripts/` 只允许调它；
- 规则写进 `AGENTS.md`：**新脚本不得 `duckdb.connect()`**（现有 66 个冻结为基线，逐步迁移）；
- 远期把 `repair_*`（16 个）按域合并成 `vd data repair <domain>`。

**完成标准**：新脚本 0 个直连；`scripts/` 行数不增长；`repair_*` 数量下降。

### S7 机械清理（可在一个下午做完，适合新人第一个 PR）

1. 25 个 `test_fix<日期>_*` 改按行为命名（事故编号进 docstring）；
2. `masked_indicator_fields` 删除，调用点改 `masked_fields_for_codes`；
3. `core/pdf/` 改名 `announcement_pdf/`，`reports/123` 补一句边界说明；
4. 删掉 `csmar_formulas.py`（198 行，只有测试调用）或移进 `tests/support/`；
5. 代码注释格式统一为"行为契约在前、事故编号在后"（先只对新增代码强制）。

**完成标准**：`incident_named_tests == 0`；`import_cycles` 不再增加。

### S8 词汇表进代码（防止再分裂成 8 套）

- `core/vocab.py`：`JobStatus` / `ErrorKind` / `Severity` / `NullReason` 四个 `StrEnum`（现在分散在 4 个文件、类型是 `Literal` 或裸字符串）
- 生成 `docs/reference/GLOSSARY.md`（从枚举 docstring 自动生成，避免手写漂移）
- 加一条测试：`app/` 中 `"status"` 字段的字符串字面量必须来自枚举

**完成标准**：新增状态码必须改枚举 → 文档自动更新。

---

## 6. 排序与取舍（为什么不是按"最丑的先改"）

| 优先级 | 阶段 | 理由 | 风险 |
|---|---|---|---|
| P0 | S0（地图+度量+去重样例） | 零风险、当天见效，且后续每步都靠它验收 | 无 |
| P0 | S7（机械清理） | 新人第一个 PR 的理想起点，能建立"我改得动"的信心 | 低（纯改名/删除） |
| P1 | S1（update 拆分） | 唯一占 app 10% 的文件，也是事故最密集处 | 中（编排语义，需 S1 全绿） |
| P1 | S4（质量收口） | 同时消掉 2 个循环导入和 3 套"缺口"概念 | 中 |
| P2 | S2（schema 拆分） | 纯机械，但收益是"查得到"而非"改得快" | 低 |
| P2 | S5（数字工具） | 消灭 19 组同名函数，但需逐处确认语义差异 | 中低 |
| P3 | S3（calculator 拆分） | 收益明确但已有守卫测试护航，不急 | 中 |
| P3 | S6（仓储层） | 长期最大收益，但先立规则比先写代码重要 | 高（涉及 66 个脚本） |
| P4 | S8（枚举化） | 锦上添花；CODE_MAP §4 的词汇表已能解决 80% 沟通问题 | 低 |

---

## 7. 不建议做的事（避免"简化"变"重写"）

| 想法 | 为什么不做 |
|---|---|
| 把项目改成多包/微服务 | 单人本地工具，网络边界只会增加认知成本；当前 88 模块已经是上限 |
| 删掉 `path_policy` / `update_lock` / `S1` 包装器 | 这些是**事故止血带**，删了会复发（`incident-2026-07-22`）；它们复杂但有明确收益 |
| 一次性重写 `update.py` | 6 千行里嵌着几十条事故教训；只能"逐域搬出 + 每步绿灯" |
| 给所有函数补 docstring | 15% 的注释率已经不低；问题在**注释写的是历史而不是契约**，不是数量 |
| 把 131 份报告合并成一份大文档 | 报告是审计证据（只增不改）。真正该做的是 `CODE_MAP` 这种**索引**，而不是重写历史 |

---

## 8. 验收：怎么证明"变简单了"

每阶段结束跑：

```bash
python scripts/onboarding_metrics.py --compare docs/reference/onboarding-baseline-2026-11-17.json
```

**期望方向**（写死在这里，避免以后自欺）：

| 指标 | 基线 | S1+S2+S3+S4 后目标 |
|---|---|---|
| `top_file_loc` | 6,575 | ≤ 1,600 |
| `functions_gt_150` | 35 | ≤ 10 |
| `widest_class_methods` | 136 | ≤ 40 |
| `god_modules`（>1000 行） | 17 | ≤ 10 |
| `duplicate_helper_names` | 19 | ≤ 8 |
| `import_cycles` | 4 | 0 |
| `incident_named_tests` | 25 | 0 |
| `report_refs_in_code` | 325 | 不增长（新增注释按 §4.4 格式） |
| 新人"改第一个 bug"耗时 | 2–3 天（无地图） | ≤ 1 天（有 `CODE_MAP`） |

> 注意最后一行：**S0 的地图是唯一能立刻兑现的收益**，其它阶段的收益以周计。
> 这也是为什么本次先交付地图 + 度量，而不是先动 `update.py`。

---

## 9. 附：本次顺带发现的具体缺陷

| # | 位置 | 问题 | 处置 |
|---|---|---|---|
| 1 | 7 个适配器 | `_domestic_direct()` 逐字复制 7 份，且**修改进程级 `os.environ`**（多线程下会让别的请求失去代理） | 已合并为 `adapters/_net.py::domestic_direct()`，并在 docstring 写明"正解是 `httpx.Client(trust_env=False)`" |
| 2 | `data_quality.py` | `masked_fields_for_codes` / `masked_indicator_fields` 同一实现两个名字 | 列入 S7 |
| 3 | `quality/null_reason_sweeper.py` | 1,279 行只在 `scripts/classify_null_reasons.py` 里用，却住在 `app/core` | 列入 S6/S7（搬出或标注"维护面"） |
| 4 | `csmar_formulas.py` | 198 行只有测试调用 | 列入 S7 |
| 5 | `app/core/pdf/` | 报告说"PDF 已删除"，模块仍活着且被 `stock_detail.py` 顶层 import | 列入 S7（改名 + 报告补边界说明） |
| 6 | `stock_detail.py` | 模块顶层 `from app.core.pdf.manager import PDFManager`：详情页路由与 PDF 下载强耦合（导入即加载） | 列入 S6（延后到函数内或依赖注入） |
