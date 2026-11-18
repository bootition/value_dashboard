---
title: 第二轮复盘——返工根因、认知成本与历史包袱（多 agent 调查 + 独立审查）
status: approved
category: reports
created: 2026-11-18
last-reviewed: 2026-11-18
---

# 第二轮复盘（2026-11-18）

> 前序：`reports/133`（返工 7 类根因）、`reports/134`（认知成本）、`reports/136`（考古）、
> `reports/138`（AI 工作流浪费）。**本报告只写增量与新判据，不重复前四份的结论。**
>
> 调查方式：5 个独立 agent 分头取证（返工/认知成本/考古/门禁有效性/文档一致性）+
> 2 个独立审查 agent（事实核查、漏报红队）。**审查推翻了本轮 3 条结论，其中 1 条是本报告的
> 头条数字**——见 §6「被证伪的结论」，那才是这份报告最该被读的部分。

## 0. 三句话结论

1. **前一轮的规则确实在起作用，但"起作用"这件事目前证明不了。** 代码侧返工确实降到接近零，
   然而样本只有 11 次 fix、观测窗只有 32.5 小时、统计检验 p=0.200 —— **不足以声称改善**（§1.2）。
2. **返工没有消失，它搬到了文档侧。** `reports/139` 一个文件 6 小时内改 11 次，
   其中 5 次是纯"更正"；`docs/STATUS.md` 被改 128 次，长成 109KB 的追加式流水账。
   而 RG1–RG5 **没有一条**看得见"同一个数字在 4 个文档里手抄 4 遍"（§2）。
3. **最大的单点风险不是代码，是"没有 CI"。** 9 组门禁、6 道手工检查全部靠人记得跑；
   `.gitignore` 一行正则把测试夹具吞了**四个月**，本机永远绿、新克隆必红（§3.2）。

## 1. 返工已发生的事实与根因

### 1.1 规模基线（可复现）

```bash
git rev-list --count HEAD                     # 367
git log --format=%s | grep -cE '^fix'          # 132
git log --format=%s | grep -cE '^feat'         # 84
ls docs/reports | cut -d_ -f1 | sort | uniq -d # 127 133 139 59
grep -l 'status: superseded' docs/reports/*.md | wc -l   # 60 / 139 ≈ 43%
```

### 1.2 关键的诚实结论：改善不成立

按 `reports/133` 的判据（96 小时内与上一次 fix 的文件重叠 ≥40%）重算：

| 区段 | fix 数 | 96h 内返工 | 比例 | 95% CI (Wilson) |
|---|---|---|---|---|
| 规则落地前 | 121 | 73 | **60.3%** | [51.4, 68.6] |
| 规则落地后 | 11 | 4 | **36.4%** | [15.2, 64.6] |

**Fisher 精确检验 p = 0.200；两端 CI 大幅重叠；且切点到 HEAD 只有 32.5 小时，判据窗却是 96 小时**
—— 规则后的 fix 根本没机会被完整观测（右删失）。把窗口压到 24 小时做删失校正，POST 只剩下 **n=3**。

> **判据**：方向可能是真的（代码侧重叠确实归零），但**现在写"返工下降了 40%"是在花未来的钱**。
> 正确表述是：**"尚不能证明改善；需在 ≥96h 观测窗、≥30 次 fix 后重测。"**

**顺带被证伪的**：主控最初用"任意文件重叠"算出 72% → 17%，被审查驳回——
该方法把"后续大提交顺带碰到 1 个文件"也算返工（105 次命中里首次重叠比例中位数仅 **0.38**，
53/105 低于 0.4，例如 `0024586` 单提交 35 文件）。**口径不同会得出方向不同的小数，所以口径必须写死。**

### 1.3 返工搬到了文档侧（本轮新增判据）

| 证据 | 数字 | 复现 |
|---|---|---|
| `reports/139` 单文件单日改动 | **11 次 / 6 小时**，其中 5 次纯"更正" | `git log --oneline -- docs/reports/139_*.md` |
| `docs/STATUS.md` 被改次数 | **128 次**（占全部提交 35%） | `git log --format=%H -- docs/STATUS.md \| wc -l` |
| `STATUS.md` 体积增长 | 34.6KB(08-28) → 53.5KB(09-02) → 87.7KB(11-15) → **109KB(now)** | 见 §2.2 |
| 硬数字同错多处 | `432,343`→`432,523` 同时错在 STATUS + KB + reports/139 | `git show 957bc4a --stat` |

### 1.4 根因收敛：7 类 → 3 个元机制

`reports/133` 的 7 类根因（R1–R7）在**类别层面是对的，但在排序和粒度上是错的**：

- **排序错**：`reports/133` 把 R1「一个事实一个实现」排第一（因为它最易机械拦截），
  而按提交级命中它**最少见**。审查用 A 报告自带正则重跑 132 条 fix 得到
  `R3=37 / R6=34 / R7=30 / R2=15 / R4=15 / R1=7 / R5=4`（A 报告原写 54/48/45/42/28/12/7，
  **复现不出，已降级为不引用**）。方向仍成立：R1/R5 最稀少。
- **粒度错**：51 条 fix（39%）**同时命中 ≥2 类**，7 类里有 7 条命中 ≥5 类 → 真实粒度只有 3 个。

| 元机制 | 含义 | 可预防性 |
|---|---|---|
| **G1 无完成定义（DoD）** | 口径、验收标准、存量清单三者都没有产物，也没有检查 | ✅ 可预防 |
| **G2 零 CI** | 所有门禁靠人记得跑 → 纪律是唯一执行器，而纪律不可证伪 | ✅ 可预防 |
| **G3 反馈回路断裂** | 全量 28 分钟 + 服务在跑就拒跑 + 单文件测试要凑 4 个环境变量 | ✅ 可预防 |

> 审查对 A 报告新增的 R8/R9/R10 判为**类别错误**（都是症状）：R8 与 R10 是同一件事的两次投影
> （"没先搞清楚现状就动手"），R9（并行 agent 无租约）是 G2 的触发条件而非独立病灶 ——
> 单人开发时它自动消失，但返工不消失（热点数据显示大量返工早于并行 agent 出现）。
> **R9 的可操作部分已落成 `AGENTS W8`（文件租约），其余两条并入 G1。**

## 2. 认知成本：新人接手的真实代价

### 2.1 度量（可复现）

```bash
uv run --locked python scripts/onboarding_metrics.py \
  --compare docs/reference/onboarding-baseline-2026-11-17.json
```

**5 天内全部恶化**（`reports/134` 的诊断方向正确，但它的结论已过期）：

| 指标 | 11-17 | 11-18 | 变化 |
|---|---|---|---|
| `app_loc` | 63,185 | **65,994** | +4.4% |
| `report_refs_in_code`（代码里引用 `reports/NNN`） | 325 | **420** | **+29%** |
| `day1_docs_chars` | 182,422 | 205,521 | +12.7% |
| `functions_gt_150` | 35 | 37 | +2 |
| `duplicate_helper_names` | 19 | 20 | +1 |
| `incident_named_tests` | 25 | **0** | ✅ 唯一兑现项 |

`report_refs_in_code` 涨 29% 是最值得警惕的一条：**代码正在越来越依赖"读报告才能看懂"**。

### 2.2 `STATUS.md` 是认知成本的最大单点

它是 `AGENTS.md` 指定的"必读 #2 / 当前状态唯一权威"，实测：

- **267 行 / 109KB**，单行最长 **5,589 字符**（一行里塞了 13 个报告号的历史叙述）
- 163 张表格行，日期分布 `2026-08` 154 处、`09` 76、`11` 67、`10` 13、`07` 4 —— **大部分是历史**
- 「当前裁决」表 **75 行里 29 行引用的报告已是 `superseded`（39%）**
- 全文引用 **109 个不同报告号 = 全库 139 份的 78%**（368 处）—— 真相源没缩小阅读面，反而成了全库索引

**结论**：这不是"状态文档"，是**追加式变更日志伪装成状态文档**。新人照它建认知会拿到作废口径，
而且没有任何机械信号能让他发现。

### 2.3 认知成本量化（口径已对齐）

两套口径的差异（B 报 205,521 字/9.8h vs E 报 149,832 字/6.2h）来自**必读集合不同**，不是算错：

| 口径 | 内容 | 字/小时 |
|---|---|---|
| **完整必读**（AGENTS §1 字面执行） | 6 份文档，含 109KB STATUS + 81KB KB | 205,521 字 ≈ **9.8 h** |
| **最小必读**（建议口径） | AGENTS §0+文档规则 + CODE_MAP 全文 + STATUS 最新裁决 + KB §3 | **28,413 字 ≈ 71 分钟（↓65%）** |
| 追证成本 | 沿 STATUS 里 368 处引用回查报告 | ~40 h（1.05M 字） |

**其中"白读"占比**：必读文件内可直接指认的作废/重复内容 ≈ 15.4%；追证性白读 =
**42% 的文件 / 50% 的字节**是 `superseded`。

### 2.4 一次典型改动的真实成本（新增度量）

按模块闭包（能影响到的代码总量）实测：

| 需求类型 | 闭包 | 规模 | 判断 |
|---|---|---|---|
| 加一个**指标** | `app.core.indicators.calculator` | **42 模块 / 21,950 行** | 最常改、联动最多（6 处） |
| 加一个**数据源** | `app.core.adapters.manager` | **24 模块 / 8,967 行** | 约 5:1 |

### 2.5 god object 与切分方案

| 文件 | 行数 | 关键结构 | 切分建议 |
|---|---|---|---|
| `app/core/update.py` | **6,633** | `IncrementalUpdater` **6,366 行 / 137 方法** | `_refresh_*` 24 个函数 1,186 行 → `update/steps/`（**复用 `update_steps.py` 已有的 12 域词表做文件名**）；`persist` 424 → `update/persist.py`；`retry` 306 → `update/retry.py`。前两步即可让 1,636 行（25%）离开 god class |
| `app/core/storage/schema.py` | 3,030 | `init_duckdb_schema` 单个 994 行函数；迁移已 67 条 | `schema/ddl_*.py`（8 个域）+ `migrations.py`（v1→v49）+ `duck.py` ≤60 行 |
| `app/cli/main.py` | 2,968 | — | 按 `vd` 子命令域名拆 |

**最被低估的复杂度**：`schema.py` 闭包只有 6 模块 / 4,279 行、fan-in 仅 2，度量上完全不显眼，
但每加一个域都必须进**同一个 994 行函数**，失败模式是"整库建不起来"或"DDL 漂移静默通过"，
且它几乎**没有自己的测试**。**行数占比小 ≠ 认知成本小——爆炸半径是全库。**

**最被高估的复杂度**：`DerivedSnapshotDomain` 注册表（11 行、1 个条目）。看似"为一个实现者引入注册表"，
实际守的是两条发布路径的静默清空（`reports/131` K5 实测 5,575 行 `roce_*` 被清成 NULL）。

## 3. 门禁有效性：写了规则 ≠ 有执行器

### 3.1 14 条规则的执行力分布

| 执行方式 | 条数 | 说明 |
|---|---|---|
| 自动门禁 | 2 | RG1（实为装饰性，见下） |
| 半自动 | 6 | 有检查器但覆盖面窄 |
| **纯自觉** | **6（43%）** | 写了但没人管 |

**RG1 的实测拦截率 = 0%**：把 6 种写法同时注入副本（`date.today()` / `datetime.now(UTC)` /
`utcnow()` / `pd.Timestamp.now()` / `ZoneInfo("Asia/Shanghai")` / `timezone(timedelta(hours=8))`），
门禁**仍然全绿**；全库**存量 28 处** `date.today()`（`app/` 23 处 / `scripts/` 5 处）也全绿。
唯一一次命中是**行内注释**（假阳性）。

> **这是本轮最反直觉的发现**：`RG1` 的正则同时产生假阳性（注释）和假阴性（6 种真实写法），
> 而 `e308911` 修的「假 DDL 名」是**同一个病**在 `asset_health.py` 的另一次发作——
> 当时被当成个案修掉了（`git show e308911` 证实只改了 `schema.py` 注释，
> `DDL_TABLE_PATTERN` 正则一行未动），**没人意识到它是所有正则门禁的共病**。

其他硬缺口：
- **RG2 弱**：只扫 2 个文件；`step.update({"status":…})` 与第二个 `{**a, **{...}}` 解包绕过。
- **RG4 fail-open**：`getattr(module, "SNAPSHOT_UPDATE_SQL")` 取不到时 `sql_columns=None` → **静默跳过**；
  且只验证"已登记的域"，从不验证"该登记的列有没有登记"（当前只登记 1 个域）。
- **反向用例形式上有、实质是自证**：RG1 的注入样例用检查器自己的模式表反向生成。

### 3.2 `.gitignore` 吞掉测试夹具（存活 4 个月，本轮修复）

`.gitignore:40` 原写 `tests/regression/[0-9a-f]*/`（本意只忽略 hash 命名的 S1 证据目录），
但 `fixtures` 首字母 **f ∈ `[0-9a-f]`** → 整个夹具目录被忽略：

```bash
git check-ignore -v tests/regression/fixtures/sina/sina_lrb_600519.json  # 命中 .gitignore:40
git ls-files tests/regression/fixtures | wc -l                            # 0（修复前）
find tests/regression/fixtures -type f | wc -l                            # 4
```

`tests/regression/test_sina_adapter.py:18` 直接 `read_bytes()` 这 4 个文件、**无 skip**。
即：**本机永远绿，新克隆必 `FileNotFoundError`** —— 与 `test_config_dead_keys.py` 自述的事故同类，
但四个月里没有任何门禁用 `git ls-files` 反查过测试资产。

### 3.3 已落地的自动化（本轮）

新增 `scripts/repo_integrity_guards.py`，四条检查，**已挂进 `check_rework_guards.py` 一条命令**（RG1–RG9）：

| 编号 | 检查 | 上线时抓到 |
|---|---|---|
| RG6 | 报告编号唯一 + front-matter 合法 | **4 组重号**（127/133/139/59）、1 份无 front-matter |
| RG7 | supersede 双向闭合 | **`133_ROCE_WINDOWS` 声称取代 `132_ROCE_FINAL`，而 132 仍 `approved`** |
| RG8 | 真相源不引用作废结论 | 「当前裁决」表 **21 行**引用 superseded |
| RG9 | 测试资产必须入库 | **fixtures 事故本尊** |

**采用基线棘轮**而非"先修出口"：`docs/evidence/repo-integrity-baseline.json` 冻结 28 条存量，
**新增违规立刻红、修好的可下调**。反向用例 `tests/regression/test_repo_integrity_guards.py`
**15 个全部通过**（按 `AGENTS R2` 要求，每条探针都有"必须失败"的用例 + 3 个防假阳性的干净树用例）。

## 4. Git 考古：有意为之 / 历史遗留 / 为已消失问题而引入

### 4.1 `reports/136` §7 的执行率：6/12

| 状态 | 项 |
|---|---|
| ✅ 已执行 6 | `/design-preview`、`masked_indicator_fields()`、`csmar_formulas` 迁出、S0 改 `historical`、`_domestic_direct` 全套、两份 `default.yaml` 同步制 |
| ⚠️ 撤回 2（理由成立） | `legacy_dangerous/`、`static/index.html` 取消跟踪 |
| ❌ **没动 4** | 删 `build/`+`_legacy/`（4.5MB + 583KB 仍在盘）、`null_reason_sweeper` 迁出、迁移折叠、`screening_readiness_cache` |

其中「本地删 `build/`/`_legacy/`」**既没执行也没进推迟表 → 是掉项，不是取舍**。

### 4.2 可安全移除（W7 契约验证后）

**零引用一次性脚本 12 个 / 1,661 行**，每个都有"问题已消失"的正向证据
（如 `cleanup_csmar_redundant` 清完 223 冗余变体、`repair_misdated_duplicates` 修完 20.6 万行日期错位）。
**但审查驳回 1 个**：`populate_csrc_industry.py` 在代码里零引用，却是
**`docs/runbooks/ops-data-rebuild.md:62` 的运维步骤** → 删了 runbook 就是假指引。
→ **确认可删 11 个 / ~1,587 行。**

**W7 抓到的两个"删了会炸"陷阱**（已写进 AGENTS「不要删清单」）：

1. `scripts/import_csmar.py` 长得和一次性脚本一模一样，但
   `test_maintenance_script_boundaries.py:7-11` 把它写进 `WRITER_SCRIPTS` 常量、`:18` 直接 `read_text()`
   → 删文件是 **`FileNotFoundError`**，连"测试失败"都不是。
2. `scripts/cold_archive_split.py`（1,333 行、单次提交、名字像迁移）被 **9 个测试 `_load_module()` 加载**，
   `cold_archive_guard.py:91` 的恢复指引也指向它 → 删了 **19GB 主库的冷库 17 视图无法重建**。

### 4.3 136 之后的新腐化（4 处）

| 项 | 证据 |
|---|---|
| `STEP_LABELS`(18 键) 与 `STEP_ORDER`(**26 步**) 仍是两份 | `reports/137` §2.6 已记，三天后仍在（注：B/C 两份报告都写成 25 步，**实为 26**） |
| `.tmp-test-run-1` / `-813` 未被 `.gitignore` 覆盖 | `git check-ignore` 不命中；`981deb9` 修过的同类问题复发 |
| `scripts/__pycache__/resolve_dividend_ex_dates.pyc` | 已删模块的残留字节码，会污染 grep/AST |
| 迁移条数 63 → **67** | `schema.py` |

### 4.4 新结构基本都有防护对象（不该删），两个例外

`check_indicator_readiness.py`、v47 `roce_10y_min/cv`、`sector.py` 金融股单一判据、
冷库 17 视图 + `cold_archive_guard.py`、`sync_packaged_config.py` —— 都能指出防护对象。

例外：**`forecast_as_of()` 零运行时调用**（护栏未接线，只被测试钉住）；
**机构持仓域没进更新链**（`grep institution app/core/update.py` = 0，无 API/前端消费者）→ 半成品。

## 5. 历史包袱的两类判据（防止"看起来旧"就删）

### 5.1 分类结果

| 类别 | 判据 | 代表 |
|---|---|---|
| **有意为之** | 能指出防的是哪次事故/哪个需求 | `path_policy.py`（`incident-2026-07-22`）、`update_lock.py`（三方并发写坏库）、`ErrorKind` 七分类（`reports/117` 熔断放大）、`cli/protocol.py`（不可逆操作） |
| **历史遗留** | 当时的技术栈/数据源/版本约束已不存在 | `dividend_yield` 废弃列 + `DEPRECATED_SCALE`、`_to_float` ×7 三种语义、`data_quality` vs `quality/` 三套 |
| **为已消失的问题引入** | 报告的结论已被推翻，代码没回滚 | `revert_sse_circ.py` + `repair_sse_circ_from_tencent.py`（**一对相反操作**，说明当时来回试口径）、`fetch_credit_spread_aa.py`（源只有 AAA，"脚本是证明拿不到"的产物） |

### 5.2 两个"看着该改、其实要改打包而不是删文件"的项

- **[F1] 两份 `default.yaml` —— ⚠️ 本报告初稿的判断已自我更正，结论是"暂不消除"**

  初稿（沿用漏报审查的意见）称"用同步机制维持重复，正解是打包期映射同一份、约 5 行"。
  **该判断被证伪**，理由是实测出的两条硬约束：

  1. `pyproject.toml:57` 把 `"app.resources" = ["config/default.yaml"]` 声明为 **package-data**
     —— `uv build` / `pip install .` 在**构建期**就从源码树取这个文件。
     若把它改成 gitignore 的生成物，**新克隆直接打 wheel 会缺兜底配置**，安装版运行期读不到默认配置。
  2. `value-dashboard.spec:32` 用 `required_file('config/default.yaml')` 从仓库根取同一文件。

  所以"删副本"不是 5 行改动，而需要自定义 `build_py` 钩子在构建期生成副本，
  并补一个**wheel 安装后的真机 smoke**才能证明没退化 —— 这是一次独立会话的工作量。

  **同时实测确认现有控制是真起作用的**（不是"空转的补救措施"）：

  ```
  $ uv run --locked python scripts/sync_packaged_config.py --check      # → rc=0，逐字节相等
  # 注入 2 行漂移后重复
  $ uv run --locked python scripts/sync_packaged_config.py --check      # → rc=1
    源 config/default.yaml: 16809 字节 / 270 行
    副本 app/resources/config/default.yaml: 16823 字节 / 272 行
    修复：uv run --locked python scripts/sync_packaged_config.py
  ```

  **裁决**：保留现状；"消除重复"降级为可选优化（记为 `T5'`），**不进本轮**。
- **[F2] `docs/STATUS.md`** 需要结构性重写（只留当前裁决 + 剩余缺口，历史移 `docs/reports/`），
  不是继续追加。重写后 RG8 的 21 条基线可整体下调。

## 6. 被证伪的结论（本轮最重要的部分）

独立审查推翻/降级了下列结论，**原样保留在此以防下一轮重复走错**：

| # | 原结论 | 实测 | 处理 |
|---|---|---|---|
| 1 | 主控：返工率 72%→17%，改善显著 | **复现不出**（72%/17% 只在特定组合出现，且该组合的 PRE 是 47.9%） | **撤回** |
| 2 | A 报告：规则后返工 60%→36% | 可复现，但 **Fisher p=0.200 + 右删失**（窗 32.5h vs 判据 96h） | **降级为"尚不能证明改善"** |
| 3 | A 报告：R1–R7 各类命中数 54/48/45/42/28/12/7 | 用 A 自带正则重跑得 **37/34/30/15/15/7/4** | **不引用绝对值，只保留排序方向** |
| 4 | A 报告新增 R8/R9/R10 为独立根因 | 判为**症状**，三者收敛为 G1 | **并入元机制** |
| 5 | E 报告：KB `D51` 与 `§17 I-ROCE7` 口径互斥 | **不互斥**（一个定义口径+门槛，一个是筛选三条件）；真问题是 D51 依据的 `reports/130` 已 superseded 且未随 v45/v46 更新 | **改为"陈旧"** |
| 6 | C 报告：`populate_csrc_industry.py` 可删 | runbook 仍要求运维执行它 | **从可删清单移除** |
| 7 | A 报告：`reports/133` 头条「ROCE 返工 4,322 行」 | `git show --numstat` 复算为 **3,626 行**（+3,370/−256） | **注记：反返工报告自身违反了自己的 R2** |
| 8 | B 报告：`frontend/ 完全没测` | `frontend/tests` 实有 20 文件 / 3,322 行且已入库；真问题是**有测试但从不自动跑** | **改判** |

**另有 3 条硬数字被独立核对为真**（可放心引用）：`432,523 行`、`schema v49`、
`roce_eligible 1,406` / `5,338 / 4,214 / 1,404`（只读查库）。
**2 条为假**：`79 期` → 实为 **86**；`5,666 只` → 实为 **5,934**（均在 11-18「自审已收口」的条目里）。

## 7. 本轮已落地的产出

| 类型 | 产出 | 状态 |
|---|---|---|
| **自动化** | `scripts/repo_integrity_guards.py`（RG6–RG9）+ 基线棘轮 + **15 个反向用例** | ✅ 已挂进 `check_rework_guards.py`，一条命令跑 RG1–RG9 |
| **自动化** | 修复 `.gitignore` 夹具吞没（+4 个夹具入库） | ✅ `check-ignore` 验证：夹具不再命中、hash 目录仍忽略 |
| **规则** | `AGENTS W8`（并行 agent 文件租约） | ✅ 新增 1 条（符合"一次最多 2 条"） |
| **规则** | `AGENTS R2` 折叠「硬数字只能引用不能手抄」（`reports/133` §7 升级条款） | ✅ 合并进既有编号，不新增 |
| **规则** | `AGENTS` 不要删清单 +2 项（`cold_archive_split.py`、打包兜底配置） | ✅ 表内已加 |
| **规则** | 更正 `AGENTS` 的 `R1–R7` → `R1–R8`、`10 项` → 表实有 13 项、补"无 CI"执行现实 | ✅ |
| **Skill** | `docs/skills/hard-facts-registry/SKILL.md`（5 步 + 反模式 + 检查清单） | ✅ |
| **数据修复** | `132_ROCE_FINAL` 标 `superseded`（+`superseded-by`）；`112_` 补 front-matter | ✅ RG7 已绿 |

> **容量约束的现实**：`AGENTS.md` 在动手前已是 **291/300 行**，本轮先压缩约 12 行散文才腾出空间。
> 这说明"再加一条规则"这条路已经走到头了 —— 往后只能靠**合并进既有编号**和**降级旧规则**。

## 8. 待执行（按 ROI）

| # | 动作 | 成本 | 收益 | 为什么值得先做 |
|---|---|---|---|---|
| **T1** | **装 git pre-commit hook**（RG1–RG9 + 改过的文件跑 ruff），≥300ms 级 | 半天 | 把"靠记性"变成"不可跳过" | 没有它，本报告新增的门禁同样会被人忘掉。**这是唯一能放大所有其他投入的一步** |
| **T2** | `STATUS.md` 结构性重写 + RG8 基线整批下调 | 1 天 | 必读量 ↓65%，真相源恢复可信 | `reports/134` 建议但未执行，已恶化 12.7% |
| **T3** | RG1 从正则改 AST 调用点白名单（80 行） | 半天 | 拦截率 0% → >0%，顺带 28 处存量 | 唯一"每天启用"的规则当前形同装饰 |
| **T4** | 拆 `update/prices.py` + `update/retry.py` | 2 天 | 1,636 行（25%）离开 god class | 闭包 62 模块 / 41,963 行是最大认知税 |
| **T5'** | `[F1]` 消除配置重复（**已降级为可选**：需自定义 `build_py` 钩子 + wheel 安装 smoke，见 §5.2） | 1–2 天 | 消掉 16.8KB 重复与同步脚本 | 本轮实测现有 `--check` 控制有效（rc=1 + diff + 修复命令），**不属于缺陷** |
| **T6** | `STEP_LABELS`(18) 与 `STEP_ORDER`(**26**) 合并为派生 | 2 小时 | 消掉已记三天未动的漂移 | 两份步骤清单必然继续分叉 |

## 9. 与旧结论的冲突裁决

| 冲突 | 裁决 |
|---|---|
| `reports/133` 把 R1 排第一 | 排序改为「按提交级命中」：R3 > R6 > R7 > R2 ≈ R4 > R1 > R5；R1 的"易机械拦截"优点保留（它仍是最该有门禁的） |
| B 报 9.8h vs E 报 6.2h 必读成本 | **两者都对但口径不同**（完整体检 vs 最小集），按 §2.3 分口径引用，禁止混用 |
| B/C 报 `STEP_ORDER` 25 步 | **实为 26 步**（`STEP_LABELS` 18 键、覆盖 17、多一个 `check`） |

## 10. 证据索引

- 调查产物（会话区，未入库，按 `AGENTS` 文档规则）：`.planning/2026-11-18-retro/`
  `A_rework_forensics.md` / `B_cognitive_cost.md` / `C_archaeology.md` /
  `D_guard_effectiveness.md` / `E_doc_integrity.md` / `R1_factcheck.md` / `R2_omissions.md`
- 门禁：`scripts/repo_integrity_guards.py`、`tests/regression/test_repo_integrity_guards.py`、
  基线 `docs/evidence/repo-integrity-baseline.json`
- 复现命令：见各节代码块；认知成本用 `scripts/onboarding_metrics.py --compare`

## 11. 修复落地（2026-11-18 第二轮，5 个修复 agent + 2 个独立审查）

> 本节记录"把 §8 的待执行变成已执行"的结果。**所有数字都经主控独立复算**，
> 不采信执行 agent 的自述；审查提出的结论也逐条复核过，其中 **4 条被判为误报**（见 §11.5）。

### 11.1 已修（可验证）

| # | 原问题 | 修法 | 验证 |
|---|---|---|---|
| **T3** | RG1 拦截率 0%，28 处 `date.today()` 全绿 | 正则 → **AST 调用点检查**（别名解析 + 日期粒度判定） | 对 HEAD 快照跑新检查器得 **53 条**违规（执行 agent 自述 49，**以 53 为准**）；修完 **0 条**，门禁绿 |
| **T3b** | RG1 假阳性：注释里写 `date.today()` 会报 | AST 天然只认调用点 | 反向用例：注释/docstring/字符串里写该字样 → **0 findings** |
| **T3c** | RG4 fail-open：常量取不到则静默跳过 | 取不到即报"门禁 fail-open" | 把 roce 的 `SNAPSHOT_UPDATE_SQL` 改名 → 必红 |
| **T3d** | RG2 只扫 2 个文件，`step.update({...})` 可绕过 | 扫描面 2 → **11 个模块**；覆盖 `update()` / 二层解包 / `dict(**x, status=)` | 两种注入写法均必红；`update.py` 5 处合法写法有反向用例保护 |
| **T6** | `STEP_ORDER` 26 步 vs `STEP_LABELS` 18 键双清单 | `STEP_LABELS` 改为由 `STEP_ORDER` **派生**（27 键 = 26 步 + 非 `report_step` 的 `check`） | 17 个同名标签与旧值逐字一致，0 差异 |
| **§4.2** | 11 个零引用一次性脚本（1,587 行） | 逐个 W7 grep 后删除 | `scripts/` **92 → 81**；契约测试 54 passed；`populate_csrc_industry.py` 按审查结论**保留**（runbook 依赖） |
| **§4.3** | `scripts/__pycache__/` 有已删模块的残留字节码 | 删 **29 个**孤儿 `.pyc`（源码已不存在） | 删后孤儿 0；有效模块字节码未动 |
| **§4.3** | `.tmp-test-run-*` 未被忽略（`981deb9` 修过的同类问题复发） | `.gitignore` 加窄模式 `.tmp-test-run-*/` + `uv-*.lock` | 命中忽略；`tests/regression/fixtures/` 仍**不**被吞 |
| **T2** | `STATUS.md` 109KB 追加式变更日志，29/75 行引用作废结论 | 重构 + 历史逐字迁入 `reports/141` | **110,438 B → 35,826 B（−67.6%）**；RG8 **15 → 0**；零丢失校验 265 条目 **MISSING=0** |
| **R2 漏报** | 前端单位表死键 `dividend_financing_ratio`（后端只有 `_pct`） | 删死键，后端为唯一权威；两处百分数渲染收敛为一处 | 四类集合 38/18/30/12 **双向相等**；注入漂移 → 测试必红（2 fail / 3 fail，还原 23 绿） |
| **T1** | **无 CI、无 git hook**，9 组门禁全靠记性 | `.githooks/pre-commit` + 幂等安装脚本，已 `core.hooksPath=.githooks` | 无暂存 **0.25s / exit 0**；暂存 1 个 `.py` 约 **3.5–4s**；真实拦红已验证（RG6 重号、ruff F401） |
| 新增 | 钩子文件在 `autocrlf=true` 下会被检出成 CRLF → Linux 上 bad interpreter | 新增 `.gitattributes`（`.githooks/*`、`*.sh` 强制 LF；不写 `* text=auto` 避免全仓重归一化） | 仅影响新文件（仓库原本无跟踪 `.sh`） |

### 11.2 RG6–RG9 基线：28 → 4

只剩报告编号重复 4 组（`127/133/139/59`）。**这 4 组刻意不修**：重命名会打断全仓引用
（`STATUS` 里 368 处、代码里 420 处 `reports/NNN`），而 RG6 已把它们锁进基线、不会变多。
其中 **`133` 那一组才是真风险**（`STATUS` 用 "reports/133" 指返工复盘，而 `133_ROCE_WINDOWS`
取代了 ROCE 定稿），已给两份文件加 `number-collision-note` front-matter 消歧。

### 11.3 明确定义为"不该改"的（避免下一轮重复讨论）

- **51 处 naive `datetime.now()` 仍保留**：它们全是"时刻语义"（备份目录名、证据文件名、
  日志时间戳、审计列）。改成北京时区会改动**历史产物命名**与既有 DB naive 列口径。
  RG1 只对 `today()` / 日期粒度 / `utcnow()` / 时区字面量硬报；时刻语义走白名单且**带理由**。
  白名单是**按调用点类别**而非整文件——`scripts/**` 内 `today()` 与日期格式仍会报。
- **两份 `default.yaml` 保留**（§5.2 `[F1]` 已更正）：`pyproject.toml:57` 把副本声明为
  package-data，删副本会让 wheel 缺兜底配置。实测现有 `--check` 控制**有效**（注入 2 行漂移 → rc=1）。

### 11.4 仍未做（诚实清单）

| 项 | 为什么不做 | 触发条件 |
|---|---|---|
| **T4 拆 `update.py`**（6,633 行 / `IncrementalUpdater` 137 方法） | 需要全量 S1 回归 + 服务冻结窗口；本轮改动已横跨 30 个文件，再叠加结构拆分会无法归因 | 有独立会话 + 冻结窗口时 |
| **T5' 消除配置重复** | 需自定义 `build_py` 钩子 + wheel 安装后真机 smoke（§5.2） | 有人愿意承担打包回归风险时 |
| **RG3 双向**（前端键 ⊆ 后端键） | 前端侧已有 `frontend/tests/screening-format.test.ts` 做**双向严格相等**，RG3 单向属重复但无害 | 若前端测试不在提交前跑，再补进 RG3 |
| **RG1 白名单按行标记** | 当前按"调用点类别"豁免已能防"整文件免检"；再细分收益递减 | 出现第一次白名单内真实漏报时 |

### 11.5 审查提出的 4 条误报（复核后否决）

独立审计报了 4 条"必须修"，主控逐条复现后判为**误报**。记录在此，防止下一轮重复返工：

| 审查断言 | 我的复核 | 结论 |
|---|---|---|
| **P0：钩子 ruff 判定正则 `(\.py:[0-9]+:[0-9]+:\|-->)` 用了转义括号，永远不匹配 → ruff 全放行** | 从钩子里**抽出 `classify_rc` 实体函数**跑真实 ruff 输出 `a.py:1:8: F401` → 返回 **`block`**；环境错误输出 → `env`。文件里的括号**没有转义**，是审计自己复现时的 quoting 差异 | ❌ **误报**（但据此把 env 判定收窄，见 11.6） |
| **P2：白名单漏判 `now().isoformat()[:10]` 与 `strftime(变量)`** | 在 `app/core/` 造探针文件写入这两种写法 → 门禁报 **2 条** | ❌ **误报** |
| **P1b：`ready=TRUE` 就绪结论被搬进历史档，STATUS 看不到当前就绪状态** | 被搬走的是 **2026-08 的旧快照**（`reports/62`）；当前就绪状态在 STATUS 当前表里（`reports/139`：**就绪自检 READY 6/6**）。搬旧留新是正确行为 | ❌ **误报**（但暴露 STATUS 缺"如何复算就绪"的指引） |
| **`od -tx1` 字节判断不存在**（指执行 agent 虚报） | 全仓 `grep -c "od -tx1"` = **0**，执行 agent 的自述不成立 | ✅ **属实**（改由 `.gitattributes` 从根上解决 CRLF，优于运行时探测） |

**确认为真并已修的两条**：
- `scripts/export_data_package.py` 加了 `cn_today` 导入但缺 `# noqa: E402`，
  且 `repair_prices.py` 留了未使用的 `date` 导入 —— **4 处 ruff 报错**，
  全部落在 `scripts/`（**不在 `AGENTS.md` 的 ruff 门禁范围**里，所以没人发现）。
  已修，并确认"改过的 36 个 `.py` 全部 ruff 干净"。
- RG1 白名单里 `app/cli/main.py` 的理由**事实错误**（声称与同表 `created_at` 一致，
  而 `schema.py:1430` 是 `DEFAULT CURRENT_TIMESTAMP` = UTC 基准，差 8 小时）。
  已改写为如实披露该口径不一致，并说明这是独立的口径变更、不在 RG1 范围。
  `backup/manager.py` 把"备份 ID（本地时间命名）"与"manifest created_at（UTC）"合并成一条理由，
  已拆开。

### 11.6 顺手加固（审查副产物里成立的部分）

- `classify_rc` 的"环境错误 vs 规则失败"判定，从"输出里出现 `Traceback|ModuleNotFoundError|…`
  任一关键字"收窄为**行首锚定**，避免 ruff 报的某条消息里恰好含 `ImportError` 之类字样时
  把真实 finding 误判成环境问题而**放行**。
- 白名单理由文本改为"只豁免时区、不认可口径不一致"，避免理由本身成为假绿的制度化入口（`reports/133` 的病根）。

### 11.7 钩子自身的 P0：八进制陷阱（被自己的钩子挡下来的那次提交）

钩子装上后，**主控自己的一次提交被它挡了下来**：

```
.githooks/pre-commit: line 68: 059: value too great for base (error token is "059")
```

根因：`now_ms()` 用 `${EPOCHREALTIME}` 取毫秒，拼成 `$((_s * 1000 + _ms))`。
bash 算术把**前导 0 当八进制**：

| 输入 `_ms` | 旧写法结果 | 应当结果 |
|---|---|---|
| `059` | **报错**（9 不是合法八进制位）→ 提交被拦 | `59` |
| `012` | 静默算成 **10**（少 2ms） | `12` |

因此这个钩子会**间歇性**失败。实测 200 次采样：**11%（22/200）**的毫秒段以 0 开头，
其中 **3.5%（7/200）会硬错**（提交被无故拦下），另约 7% 静默少算 1–2ms（无害但说明代码是错的）——
正好撞上装钩子时立下的原则：
**容易无故失败的门禁会在几天内被 `--no-verify` 永久绕过，比不装更糟。**
修法：两处都加 `10#` 前缀强制十进制。

> **教训（值得进 AGENTS）**：`bash`/`awk` 的算术对"看起来是数字"的字符串不做十进制保证；
> 任何来自时间戳、补零格式化、`printf` 拼接的变量，进 `$(( ))` 前都必须显式 `10#`。
> 两个独立审查（含一个专门审钩子的）都**没发现这一条**——它们只做了 `bash -n` 语法检查和
> "无暂存快速退出"路径，而语法合法、只有特定数值才触发的 bug 恰恰是语法检查的盲区。
> **门禁类代码必须用"真实数值矩阵"验证，不能只验证"能跑通一次"。**
