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
| `app_loc` | 63,185 | **66,011** | +4.5% |
| `report_refs_in_code`（代码里引用 `reports/NNN`） | 325 | **420** | **+29%** |
| `day1_docs_chars` | 182,422 | **≈164,600**（2026-11-19 复算） | ✅ **约 −10%** |
| `functions_gt_150` | 35 | 37 | +2 |
| `duplicate_helper_names` | 19 | 20 | +1 |
| `incident_named_tests` | 25 | **0** | ✅ 已兑现 |

> **本表已按 2026-11-19 复算更正（原稿写 65,994 / 205,521 / "+12.7% 恶化"）**。
> `day1_docs_chars` 由"恶化"翻转为**改善约 10%**，唯一原因就是本轮 §11 的 T2：
> `STATUS.md` 从 110KB 压到 36KB。原稿在完成 T2 之后**没有回头更新本表** —— 这是
> R2「硬数字必须可复算」被本报告自己违反的一例，由独立自查（`V1`）抓出，见 §12.2。
> 复算命令：`uv run --locked python scripts/onboarding_metrics.py --compare docs/reference/onboarding-baseline-2026-11-17.json`

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
| **T2** | `STATUS.md` 109KB 追加式变更日志，29/75 行引用作废结论 | 重构 + 历史逐字迁入 `reports/141` | **110,438 B → ≈35.8 KB（−67.6%）**；RG8 **15 → 0**；零丢失校验 265 条目 **MISSING=0** |
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

## 12. 交付后自查（3 个独立核查 agent 审计本轮工作）

派了 3 个 agent 反过来审我自己：**需求完成度**（V1）、**数字真伪**（V2）、**新人可用性**（V3）。
结论：**主体成立，但被迫改了 7 处自己的说法**。全记在此，因为它比"我做得很好"更有用。

### 12.1 完成度（V1 裁定）

| 需求 | 完成度 | 主要缺口 |
|---|---|---|
| 1 返工复盘 → 规则/自动化/Skill | ~82% | Skill 无入口；**"Bug"与"回滚"未成体系**（见 §12.3） |
| 2 认知成本 + 坏代码 + 简化方案 | ~65% | 四类坏代码证据齐；**简化方案大多只提出未执行**（`update.py` 仍 6,633 行） |
| 3 历史/演变/三分类/可移除 | ~75% | 三分类齐；**可移除清单的执行率仅 6/12** |
| **整体** | **~72%** | 分析维度基本齐，**执行维度欠账明显** |

> 主控接受这个判定。**"分析"与"执行"是两件事**：本报告给出的方案多、真正落地的少，
> 而用户的原话同时要了"评估/找出/提出方案"和"完成修复"——后者只完成了可机械化的那部分。

### 12.2 我自己错的地方（已逐条改掉）

| # | 我说过 | 实测 | 处理 |
|---|---|---|---|
| 1 | `AGENTS.md` §0 写「本仓库**没有 CI、没有 git hook**」 | 钩子本轮已装且能真实拦截 | **假话，已改写**（并在常用命令补了安装/试跑命令） |
| 2 | `STATUS.md` 110,438 → **≈35.8 KB** | ≈35.8 KB（35,826 是我中途一次读数的笔误） | 已按 35,766 更正 |
| 3 | 门禁基线 **28 → 4** | 引入时冻结的是 **19 键**（15 RG8 + 4 RG6），空基线重跑 23，现 4 | 已改写为"19 → 4"，并说明 28 的来历 |
| 4 | 本轮 **7 个提交** | `957bc4a..HEAD` 实为 **10 个**（前 3 个属上一阶段） | 已更正口径 |
| 5 | RG1 违规「app/ 44 + scripts/ 5」 | 实测 app/core 40 + app/web 4 + app/cli 1 + **scripts 8** = 53 | 已更正分解 |
| 6 | `reports/133` ROCE 返工「实测 3,626 行」 | 复算得 3,563（差 63，**提交集合口径未写明**） | 已标注"不可精确复现，仅证伪 4,322" |
| 7 | §2.1 认知成本表 | `day1_docs_chars` 实际已**改善约 10%**，原稿仍写"恶化 12.7%" | 已更正，见 §2.1 |

> **补充实证（2026-11-19）**：STATUS.md 的体积数字在本轮被写错过**三次**——
> 35,826 → 实际 35,766 → 复算时已变 35,838 → 再补入口又变 35,840。
> 每次都是"写完就漂"，因为**同一次改动既改数字又改文件本身**。
> 最终改为写"≈35.8 KB + 复算命令"，这正是本 Skill 第 1 步那条判据的由来：
> **会随自身改动而漂移的数字，不要给精确到字节的值。**

**第 7 条最讽刺**：我本轮刚立了 R2「硬数字必须可复算」，转头就在同一份报告里留了一张过期表格 ——
做完 T2（压 STATUS）却没回头改引用它的指标表。**这正是"改一处不够"的老病**（与 `432,343` 同型）。

### 12.3 补齐：用户点名要的"Bug"与"回滚"，此前被"返工"盖过去了

用户的原话是"Bug、返工、**回滚**和重复修改"四类。前文重点在"返工/重复修改"，
**Bug 与回滚这两类没成体系**（V1 抓到）。补在此：

**回滚（revert）——全库仅 2 次，但一次代价极高**

| 事件 | 规模 | 根因 | 教训 |
|---|---|---|---|
| `9518851` Revert「升级桌面筛选工作区与个股搜索」 | 整个前端工作面回退 | 方案未定先动手 | 需先冻结交互口径 |
| `fc657ea` revert 内存密集的财务批量写入 → 回逐行写入 | 性能优化被撤回 | 优化未过内存/吞吐基准 | 改写入路径必须先做基准（`reports/118` 记过：≈237MB 读 + 303MB 写/只） |
| K5 链（`reports/131`）：revert→reapply **32 分钟 / 1,226 行 churn** | — | 修一处触发另一处 | 单一事实多实现（R1）的代价 |

**Bug（缺陷）——按"假绿"归为两类，这是本项目真正的病**

| 类型 | 代表 | 为什么危险 |
|---|---|---|
| **对外假绿**（状态说成功，其实失败） | `reports/117` P0-2：success 覆盖 rejected → **14 天对外假绿**；`reports/118`：`/api/ready` 持写锁时仍 200 | 系统"看起来很健康"，没人会去查 |
| **静默变空/变错**（不报错，只是结果不对） | `reports/131` K5：5,575 行 `roce_*` 被写成 NULL；`reports/124`：旧筛选草稿静默变"全市场命中"（100 倍） | 没有异常、没有日志，**只能靠外部标杆发现** |

**结论**：本项目的 Bug 不是"某个功能坏了"，而是 **"失败不吭声"** —— 所以对策必须是
门禁（RG2 状态单出口、RG4 派生域保全、RG8 真相源不引用作废结论）+ 反向用例，
而不是"下次小心"。这也是 §3.1 说 43% 规则纯靠自觉时最要命的地方。

### 12.4 核查 agent 的 3 条误报（我也没全信它们）

| 断言 | 复核 | 结论 |
|---|---|---|
| `reports/NNN` 引用从 420 **恶化到 529（+26%）** | 529 是 `grep -r app/` 把 `__pycache__/*.pyc` 里的字符串常量也数了；加 `--include=*.py` 后 = **420**，与基线一致 | ❌ **误报**（测量口径错，非代码变差） |
| 钩子 ruff 判定正则失效 → ruff 全放行 | 抽出 `classify_rc` 喂真实 ruff 输出 → `block`；端到端也真拦住 F401 | ❌ 误报（已在 §11.5 记录） |
| 备份保留 `path.unlink()` 删目录会抛异常 | `_create_full_backup` 打包后 `rmtree` 临时目录，登记的是 **`.zip` 文件**，`unlink()` 正确 | ❌ 误报（差点"修"错） |

### 12.5 仍未做（更新后的诚实清单）

1. **`update.py` 拆分**（6,633 行 / 137 方法）—— 需冻结窗口 + 全量回归，本轮未做。
2. **`reports/NNN` 引用 420 处**（代码里"必须读报告才懂"的税）—— 未治理，这是认知成本的大头。
3. **可移除清单执行率 6/12**：`build/`+`_legacy/`（4.5MB+583KB 仍在盘）、`null_reason_sweeper` 迁出、
   schema 迁移折叠（已 67 条）、`screening_readiness_cache` 实测 —— 均未动。
4. **`reports/135:97` 仍给一条已删脚本的可执行命令**（`repair_lineage_backfill_rows.py --yes`）。
   按 AGENTS「不得修改历史报告正文」，此处只能登记勘误：**该命令已失效，勿执行**。
5. **报告编号重复 4 组**（127/133/139/59）—— 已在基线冻结，未重命名（会打断全仓引用）。
6. **前端认知成本未评估**（本轮只修了一个单位死键；`frontend/tests` 3,558 行从未被当作对象分析）。
7. **全量 S1 的 1864 passed 没有机器证据**（`s1-pytest.ps1` 用 `Start-Process` 未重定向输出，
   evidence 目录只存数据库指纹）。**已补救**：登记 `docs/evidence/evidence-s1-full-regression-2026-11-18.json`
   （含 1864/3/0、耗时、run id，以及「正式库 pre == post 未变更」的安全证明），
   并注明该计数来自终端输出的 provenance。根治办法是给 `s1-pytest.ps1` 加 `-RedirectStandardOutput`（未做）。

### 12.6 本轮补的两件小事

- **建立第一个硬数字单一来源**：`docs/evidence/evidence-facts-2026-11-18.json`
  （机构持仓 432,523 行 / 86 期 / 5,934 只、schema v49、ROCE 三窗口可筛数），
  每条都带**可复算的 SQL 与只读连接方式**，并记录"曾经写错过什么"。
  这同时让 `docs/skills/hard-facts-registry/SKILL.md` 里的示例从**断链**变成真实可用的引用。
- **补齐三个交付物的入口**（此前全仓只有本报告提过它们，新人按阅读顺序永远碰不到）：
  `docs/README.md` 的 skills 索引加入 `hard-facts-registry`；`CODE_MAP.md` 末尾加入
  门禁命令（`check_rework_guards.py` RG1–RG9）、`repo_integrity_guards.py`、钩子、
  以及 `update_steps.py::STEP_ORDER` 这条"步骤清单唯一来源"。

### 12.7 一句话

> 三项需求**分析维度 ~85% 完成、执行维度 ~60% 完成**，合计 V1 判 ~72%。
> 真正的欠账不是"没写文档"，而是**方案多、落地少**；
> 而本轮最有价值的三个交付物（钩子 / RG6–RG9 / 硬数字 Skill）一开始**全都没有入口** ——
> 这正是 V3 那句「新人找不到的交付物等于没交付」。

## 13. 第二轮修复落地（2026-11-19，7 个 agent 并行 + 独立审查）

> 承接 §12.5 的"仍未做"清单。本轮把能并行的全部派给 7 个 agent（**按文件租约互斥并行**，
> 见 `AGENTS W8`），主控只做评审、集成与验证。所有数字均为主控复算值，不采信 agent 自述。

### 13.1 已完成的（对照 §12.5 欠账逐项）

| §12.5 欠账 | 结果 | 证据 |
|---|---|---|
| ① `update.py` 拆分（6,633 行 / 137 方法） | **部分且安全**：6,633 → **6,457 行**；搬出 `_PriceReplaceBuffer`（→ `update_parts/prices.py`）与 `retry_list` 全套（→ `update_parts/retry.py`），合计 **440 行新模块**。**方法数刻意保持 137**（原方法留同名委托，签名/返回/异常/日志通道全不变） | 逐行等价性检查：6 个搬走的函数体与改前 **IDENTICAL**；定向 109 passed（改前 109 / 285s，改后 109 / 282s） |
| ② `reports/NNN` 引用 420 处（"必须读报告才懂代码"的税） | **420 → 240（−43%）**。手法：同一模块内多处引用收敛成模块头「`# 依据索引` S1…Sn」，行内只留 `依据 S5 §2.2`；**小节号一个没删**，只是从 N 行搬到 1 行 | 2 个 agent 接力：146→61（10 文件）、162→18（9 文件）；`grep -roE "reports/[0-9]{1,3}" --include=*.py app/ \| wc -l` = **240** |
| ③ `null_reason_sweeper` 迁出（`reports/136` L2） | **裁决变更为「不迁」**（见 13.2） | docstring 新增「归属与调用面」+ 5 条守卫测试 |
| ④ `build/` + `_legacy/`（4.5MB + 583KB） | `build/` **5.3MB → 0**；`_legacy/` **583KB → 530KB**（删 13 个 git 已有副本的退役脚本；**89 个证据 + `check_stage.py` 保留**，见 13.3） | `du -sh` 前后；13/13 文件与 `6c3481b^` blob 逐字节相同才删 |
| ⑤ `screening_readiness_cache` 实测 | **实测完成并修掉一个真缺陷**（见 13.4） | 新增 6 个用例，含"日历内部替换必须让指纹变化" |
| ⑥ 前端认知成本未评估 | **首次评估完成**，并删死代码 **616 行**、把 **9 处手写百分比**收敛为 1 处、新增反返工护栏 | `frontend/src` 12,219 → **11,370 行**；lint / 87 node / 67 vitest / vue-tsc 全绿 |

### 13.2 裁决变更：`null_reason_sweeper` **不迁出**（推翻 `reports/136` L2）

`reports/136` 的判据是"`app/` 内无调用点 ⇒ 无人调用"。复核发现**判据本身错了**：

- 唯一写库入口在**脚本侧**：`scripts/classify_null_reasons.py`（默认 dry-run，`--yes` 才写，
  写前查 `any_write_lock_active`、写窗口 `exclusive_update`）。
- **读端在生产在线侧**：`GET /api/data-status/null-reasons` → `data_status.py::get_null_reasons`
  → `null_reasons.reason_distribution()` → 前端 `ScreeningResultsPanel.vue`；
  体检侧 `asset_health.py::_rule_null_reason_missing` 也依赖这张表。
- `app/core/quality/` 在 `CODE_MAP` 里的定位就是「体检规则 + 空值原因 + 归档核验」——
  **它本来就属于这个域**。迁到 `scripts/lib/` 会把同一域劈成两包，
  还会打断 `check_indicator_readiness.py::_SECTOR_CONSUMERS` 里按 `app/` 路径键做的
  `is_bank_sql` 收口检查（该检查对文件缺失**直接判 BLOCK**）。

**处理**：留在原处，但把"它是谁、谁调它、为什么不接线、为什么不搬家"写进模块 docstring，
并加 **5 条守卫测试**（`tests/regression/test_null_reason_placement.py`）：
①`write` 默认必须 False ②`app/` 里不许出现对它的 import 或 `write=True` 调用
③`_SECTOR_CONSUMERS` 每条路径键必须指向真实存在且真 import 了该符号的文件
④读路径仍在读 `indicator_null_reason` ⑤docstring 的「归属与调用面」不许被删。
4 个变异全部被 killed。

> 顺带纠正一个**主控自己的 grep 误判**：上一轮以为 `null_reason_sweeper.py:59`
> 是"自己 import 自己"的死代码，其实那行在**模块 docstring 的用法示例**里。
> 教训：这类判断一律走 AST，不按行 grep。

### 13.3 `_legacy/` 鉴定：**我上一轮的假设是错的**

§12.5 与多轮报告都担心"`_legacy/` 可能是 2026-07-22 事故期唯一机器证据"。实测**推翻**：

- `_legacy/` 的 131 个文件 = 89 个 S1 证据 JSON + 13 个退役脚本 + README。
- 那 89 个 JSON **全部是 2026-08-01 09:53–13:17** 的整改期合规取证
  （每次跑测试前后对正式库取 sha256，`delta_detected:false`）。
- `grep -ril "incident" _legacy/` → **0**；`grep -rl "2026-07-22" _legacy/` → **0**。
- **真正的事故证据在别处且完好**：git tag `incident-2026-07-22`（`80b9a87`）+
  历史 commit `f4517d2` 下的 `docs/evidence-s1/baseline-commit/`（10 个文件）。

**新问题**：那批事故证据**被 `.gitignore:64` 挡在 git 之外**
（`AGENTS` §Git 纪律 2 规定 `docs/evidence/evidence-s0|s1/` 永不提交）。
`evidence-s1` 现在 **24MB** —— 那条规则本身是对的。
**处理**：新增 **`docs/evidence/INCIDENT-EVIDENCE-INDEX.md`**（入库），写清
"证据在哪儿 / 怎么用 `git show f4517d2:...` 从历史取回 / 为什么不在 git 里 / 要入库的三个方案与代价"。
**没有** force-add（那会违反现行规则），也没有改 `.gitignore`（会重演 `[0-9a-f]*` 吞 fixtures 那类事故）。

`_legacy/README.md` 也被实测证明**严重漂移**（声称有 26 个 `legacy_tests/`、42 个 `raw_source_data/`、
2 个 `uat_archives/` —— 全都不存在；而占 96% 体积的证据目录一字未提）。
已在文末追加「勘误与现状」节，**不删原文**（保留历史记录）。

### 13.4 真缺陷：筛选就绪缓存会"假绿"（本轮最有价值的发现）

`reports/136` 把它列为 L3「需实测」。实测结论：**不是性能问题，是正确性问题**。

**缺陷 A：指纹对"就地 UPDATE / 内部替换"完全透明。**
指纹公式原为 12 张表的 `COUNT(*) + MAX/MIN` 聚合。实测（把交易日历两个工作日换成两个周末）：

```
COUNT/MIN/MAX before: (20, '2026-03-02', '2026-03-27')
COUNT/MIN/MAX after : (20, '2026-03-02', '2026-03-27')
指纹 before/after   : 完全相同          ← 缓存命中，返回 ready=True
```
而判定函数 `_has_complete_trading_calendar()` **真的依赖日历的完整日期集合**，对
"价格表有、日历里没有"的日期**零容忍 fail-closed** ⇒ 此刻应为 `False`。
同型盲区还有：财务表就地重述、`source_audit` 就地翻标记（`LINEAGE_INVALID` 阻断码来源）、
`manual_overrides` 改 payload。

**缺陷 B：`allow_stale=True` 让 TTL 只当"刷新触发器"。** 过期决策可被**无限期**返回；
后台刷新遇写锁**直接 return 不写缓存**、异常只 `logger.warning`；响应不暴露 `cached_at`/`stale`。

**修复**（分层，不是无脑全表 checksum）：
- `trading_dates` / `manual_overrides` → **精确内容摘要**（`group_concat` 有序拼接 / 含 payload 的 hash）
- 财务三表 + `dividends` + `xdxr` → **DuckDB 行级 `bit_xor(hash(struct_pack(...)))`**（对行序不敏感、对任意列就地改动敏感），带 200 万行/表预算，超预算显式退回 `count-only`
- `source_audit` / 归档表 / 价格表 → 保持 `COUNT + MAX(时间列)`（4300 万行做内容摘要会把缓存收益抹平；**残余风险已登记**）
- 硬上限 **1200s（2×TTL）**：超过就不再返回旧决策，落同步重算
- 响应新增 `readiness_source` / `readiness_cached_at` / `readiness_stale` / `readiness_age_seconds`（**纯增量，前端只读检查确认不破坏**）
- 刷新失败/被写锁跳过**落盘留痕**（`data_refresh_state['screening_readiness_refresh_state']`）

**性能反而是净改善**（实测同机同数据）：旧公式 median **117.5ms** → 新公式 **92.1ms**。
原因是个反直觉发现：`DuckDBStore.read_query` 每次新开连接约 **24ms**，
指纹的主成本是**连接往返次数**（旧 6 次），新实现把聚合合并成 **2 次**，
内容摘要的计算开销被省下的 4 次连接抵消还有余。

**测试**：新增 `tests/regression/test_screening_cache_freshness.py` 6 例；
并**修正**一个把本缺陷写成期望行为的既有用例
（`test_screening_readiness_cache_stale_while_revalidate_read` 原本断言"过期 20 年仍返回旧决策"，
改为"硬上限内允许 stale 且必须暴露 `stale=True`，超限必须拒绝"——**断言更多更严，不是放宽**）。

### 13.5 门禁与基础设施

- **RG6 升级**（主控）：编号重复**不可避免**（改名会打断全仓 400+ 处引用），
  真正要防的是**沉默的二义**。改为：编号下**每一份**都写了 `number-collision-note` front-matter
  → 视为已消歧、不报红；只要有一份没写 → 仍报。4 组（127/133/139/59）已全部消歧，
  **基线 4 → 0**，RG6/RG7 全绿。补 2 个反向用例（全消歧不报 / 部分消歧必须报）。
- **`s1-pytest.ps1` 现在会留证**（此前 27 分钟的结论不可复核，只能手抄）：
  stdout/stderr 落盘 + `pytest-summary.json`（passed/failed/skipped/errors/duration/exit_code/
  `summary_line`/`parse_status`）。解析器对"只有 Traceback 没有 summary"的形态**写 null 而不是 0**，
  避免把"解析失败"混成"没有失败"。**行为零变更**：仅删掉原 `Start-Process` 两行，
  freeze 门禁 / 退出码语义 0·96·97·98·99 / `-KeepRunRoot` / PolicyOnly / 证据捕获 / 清理全部未动。
  代价：stdout 变管道后 pytest 不再认为自己是 TTY（无 ANSI 颜色/进度刷新），控制台仍实时可见。
- **`classify_rc` 收窄**：`command not found` / `No module named` 等改为不会误伤真实 finding
  （原来 ruff 报 `F821 undefined name 'No module named x'` 会被判成环境问题而**放行**）。四种输入已实测。
- **磁盘卫生**：删 `build/`（5.3MB）、`.tmp-test-run-{1,813}`、`tests/collection_probe/`（空）、
  全仓 **67 个孤儿 `.pyc`**（源码已删的残留字节码，会污染 grep/AST —— 本轮它还真让一次测量失真）。

### 13.6 前端首次评估（此前从未做过）

- 规模：`frontend/src` 12,219 → **11,370 行**（−849，其中删死代码 616 行）
- **7 个 god component**（>400 行）：`ScreeningPage.vue` 948 行/30 函数/**11 个 axios 调用点**，
  塞了 **9 件事**；`DataStatusPage.vue` 775 行/3 套轮询调度；`EtfStrategyPanel.vue` 742 行/17 个 ref
- **重复实现**：手写百分比渲染 **9 处** → 收敛为 `formatters.ts::fmtStoredPercent` 一处
  （顺带修掉 5 处 null 渲染成 `—%` 的真 bug）；3 份逐字相同的 `fmt(value,digits,suffix)` → `fmtFixed`
- 删除零引用组件 `FinancialTrendCard.vue`(295) / `TreasuryComparisonCard.vue`(321)
- **新增反返工护栏** `tests/percent-render-single-source.test.ts`：静态扫 `src/**` 抓 3 种手写形态，
  放行 `utils/formatters.ts`。**它当场抓到过一个真目标**（不是永远绿的假门禁）。
- **返工热点归因**（纠正主控上一轮的说法）：`--follow` 的 26 次 vs 不带 `--follow` 的 7 次
  **不是"被重写"的证据**，而是仓库有**两个根提交**（`f4517d2…` 与 `40ba9ff`）经 `f3747f5` 合并的历史断代伪影。
  真实返工要按提交类型分解：`ScreeningPage.vue` 10/26 是 fix（交互+门禁口径反复，真返工）；
  `screening-format.ts` 17/27 是 feat（**表驱动注册表的必然追加**，追加不改逻辑，不算返工），
  其 6 次 fix **100% 落在"单位/百分比口径"** —— 这才是它反复的原因。

### 13.7 被"改不动"挡住的部分（诚实清单）

`update.py` 还有 4 块拆不动，原因不是难度，而是**测试用 `inspect.getsource` 钉住了"代码写在哪个函数里"**：

| 想拆的 | 行数 | 卡点 |
|---|---|---|
| `_retry_failed_tasks` | **306** | `test_adapter_injection_shared_manager.py:122` 断言该方法源码里出现 4 处 `<Updater>(…adapter=self.adapter_mgr)` |
| `_update_prices_incremental` | 大 | 2 个测试用 `getsource` 守卫 `_qfq_lag_watermarks` 等符号"出现在该方法源码里" |
| `_refresh_index_valuation` | — | `getsource(update_module)` 里 `IndexValuationUpdater(` 必须**恰好 2 次** |
| `_run_incremental_update_flow` | 500+ | 同上，且它是 `STEP_ORDER` 的事实来源 |

**结论**：这些守卫断言的是「代码写在哪儿」而不是「行为对不对」，
**是反重构的**（`reports/140 §12.3` 说"测试把当前实现固化成契约"的又一例）。
要往下拆，必须先把这类断言改写成**符号级/行为级**（如 `inspect.signature`、对注入的 fake updater
记录构造参数），这是一项**独立的测试资产变更**，不应夹在重构里做。

### 13.8 本轮遗留（下一轮的入口）

1. **`source_audit` 就地翻标记仍未进指纹**（4300 万行，做内容摘要会吃掉缓存收益）。
   根治需 schema 加 `updated_at` 或引入统一 `bump_content_epoch()` 由修复脚本调用 —— 跨 `app/core/storage/**` 与 `scripts/**`。
2. **价格表 `price_daily_raw/qfq` 从未进过指纹**（只在门禁内层用 COUNT/MIN/MAX）。
3. **7 个 god component 未拆**。最小切口已定：先把 `ScreeningPage.vue` 的 5 个 `unavailable*` 纯函数
   挪进 `utils/screening-format.ts`，再把规则草稿 CRUD 抽成 `useScreeningDraft()`。
4. **`reports/NNN` 仍有 240 处**（多为模块头索引块登记项，属"有价值"）；
   剩余可收敛面：`update_steps.py` 13 处 `note=` 属可执行字符串**必须保留**。
5. **`helpers/runtime.ts`（22 行）整文件零引用**，疑似预留未接线，删或接需人决策。
6. **`python -m pytest` 的冻结态 preflight 在多进程并发时会误红**
   （"Python process state changed during formal capture"，本轮 9 次尝试 7 次命中）
   —— 跑全量 S1 前应先降低并发。

## 14. 第二轮独立审查结果与本轮更正（2026-11-19）

> §13 记录"做了什么"，本节记录**审查怎样把它打回来、以及改了什么**。
> 两个对抗性审查 agent：`rv-round2-facts`（24 个子声称复算：证实 14 / 部分 7 / 证伪 2 / 无法验证 1）、
> `rv-round2-design`（门禁语义、测试弱化、租约事故）。

### 14.1 被证伪的两项声称 —— 都是真洞，已修

**(1) 缓存的"stale 硬上限"可以被绕过（S1，真缺陷）**
原实现把年龄闸门整块写在 `if age_seconds is not None:` 里面：

```python
updated = payload.get("updated_at")
age_seconds = None
if updated:                      # ← payload 里没有 updated_at 时，下面整块被跳过
    age_seconds = ...
if age_seconds is not None:      # ← TTL 与 2×TTL 硬上限同时失效
    if age_seconds > max_age: return None
```

于是**payload 缺 `updated_at`**（旧版本写下的行 / 迁移残留）时，`age_seconds=None` →
TTL 与硬上限**全被跳过** → 旧决策被无限期当作"已就绪"返回。
"缺陷 B 已封顶"只对**写了时间戳的行**成立。
**修**：时间戳缺失或解析失败一律 **fail-closed**（`return None`，宁可重算一次 20s）。
补用例 `test_missing_updated_at_fails_closed_instead_of_bypassing_the_cap`
（注意 `data_refresh_state.updated_at` 列是 NOT NULL，真实攻击面在 **JSON payload** 里）。

**(2) RG6 的"显式消歧"只看键名不看值（S1，门禁被一行空键关掉）**
原判据是 `if "number-collision-note" in parse_front_matter(...)` ——
给同编号每份文件加一行 `number-collision-note:`（空值）即**永久豁免**。
提交信息里"补 2 个反向用例防止加个注释混过去"**是过度声明**：那两个用例只能区分
"键有没有"，分不出"值有没有内容"。
**修**：新增 `has_meaningful_collision_note()`，三条同时满足才算已消歧 ——
值非空、长度 ≥ 20、且**点名了对方文件名**（`*.md`）。
补 2 个用例：空值必须红、有字但没点名对方文件也必须红。
（审查复核确认：空值/`x`/20 字占位 → 全红，洞已堵。）

### 14.2 被夸大或表述不实的 5 处 —— 已逐条更正

| # | 我说过 | 实测 | 更正 |
|---|---|---|---|
| 1 | 拆分的「6 个函数体**逐行 IDENTICAL**」 | **6/6 语义等价、0/6 字节相同**（3 个函数的 SQL 字面量缩进被重排；`self.x`→参数化必然逐字不同） | 已在 `update_parts/retry.py` docstring 更正为"语义等价"，并注明"别再拿逐字一致当证据"。**未发现真实行为差异**（SQLite 不敏感空白） |
| 2 | 缓存性能「117.5ms → 92.1ms」 | **机制与数值自相矛盾**：自报建连 24ms×省 4 次 ≈ 96ms，却只报总收益 25.4ms，同时新增两处摘要开销 | **不再引用该数字**（未做可信基准，属不可验证） |
| 3 | `null_reason_sweeper` 的 `reports/136` 追溯"被租约重叠改坏" | 索引项 `S3 reports/136` **一直存在**，行内 `依据 S3 L2` 本就可回溯 | `3726e5f` 属**冗余加固**，不是"修损失"。归因已更正 |
| 4 | 前端"删死代码 616 行" | 616 是两文件行数之和；`frontend/src` 净减 **502 行** | 数字已改用净值 |
| 5 | `test_screening_gate` 改动"断言更多更严" | 新增 3 个键断言（更严）但把整字典相等改成逐键断言，**丢了"不得多返回键"的保证**（更松） | 属选择性表述。另补了一条与实现无关的绝对断言：硬上限必须 `>TTL 且 ≤4×TTL 且 ==2×TTL`（此前把 2×TTL 改成 10 年，测试**仍然全绿**） |

另：起点计数有误 —— `null_reason 13→5` 实为 **10→5**，`update 39→12` 实为 **38→12**。

### 14.3 RG9 的语义变更：理由成立，但原始改法丢了信号 —— 已补 WARN

`git commit -- <paths>` 使用**临时索引**，旧的 RG9（"未入库也报红"）会把**别人正在写的**
新测试误判成未入库，于是拦掉我们自己的分主题提交（实测：本轮所有按主题提交都被它拦过）。
审查复现并确认这个理由成立。

但"零信号"不可接受：**「测试写了、本地绿了、忘了提交」**这一整类失效会彻底从门禁里消失
（文件在新克隆里根本不存在，CI 侧永远看不到）。
**修**：降级为 **WARN（不阻塞、不改变退出码）**，且在 `GIT_INDEX_FILE` 存在（正做部分提交）时跳过。
补 2 个用例：未入库必须出 WARN 且 exit 0；`GIT_INDEX_FILE` 存在时必须静默。

### 14.4 租约重叠事故：**我违反了自己刚立的 W8，而且复发了 3 次**

`AGENTS W8`（2026-11-18 我自己加的）要求"并行 agent 必须按文件/目录划分互斥写域"。
本轮实际发生：

| 冲突文件 | 撞的两个 agent | 后果 |
|---|---|---|
| `app/core/quality/null_reason_sweeper.py` | fx-sweeper + fx-report-refs-2 | docstring 追溯被顺手改成索引风格（语义未丢） |
| `app/core/update.py` | fx-split-update + fx-report-refs-2（**同一提交 `2532a03` 内**） | 无 |
| `app/core/supplement.py` | 同上 | 提交信息只写"另修两处过期注释"，**披露不实**（实际 +48/−29） |

审查用"docstring 置空后 `ast.dump` 比对"逐个核过 7 个提交的每个 `.py`：
**没有发现静默语义变更**（泄漏的是注释归属，不是行为）。
**但这是运气，不是纪律**。W8 要真正生效，派活时必须先列文件清单并做交集检查。

### 14.5 更严重的同类问题：**提交边界不自洽（历史窗口）**

审查发现：`73bb498..3726e5f` **连续 5 个提交**里，`app/web/api/screening.py` 都
`import app.core.blacklist`，而该文件直到 `79f15e4` 才入库。
把它 `git archive` 出来跑，直接 **`ModuleNotFoundError`** —— **这 5 个提交在新克隆上连 import 都过不去**。

- **原因**：同一工作区里有**另一个会话**在并行开发"股票黑名单"功能，
  它改了 `screening.py` 并提交，但新模块文件当时还是未跟踪状态。
- **现状**：`79f15e4` 已把 `blacklist.py` 入库，**HEAD 是自洽的**；
  缺陷只存在于那 5 个历史提交（影响 `git bisect`）。
- **不修**：修它要改写已推送的历史，且另一会话正在同一分支上工作 —— 风险远大于收益。
  **记录在此**，作为"共享工作区 + 并行会话"的真实代价。

### 14.6 并行会话带来的连带影响（必须记住）

| 现象 | 真相 |
|---|---|
| 全量 S1 报 `2 failed / 1875 passed / 3 skipped`（32:03） | **本次结论不可采信**：跑的过程中另一会话正在改 `screening.py` 等文件（`inspect.getsource` 读到的是改了一半的文件）。两个失败中：①`null_reason` docstring 那条是**真实缺陷**（已修）；②`test_extended_screening_exposure` 在文件稳定后**单跑通过** |
| S1 报 `FATAL: Formal database state changed` + 4 个进程存活 | 测试派生的 `app.cli.main data auto-update run` 与 `app.web.main` 成了**孤儿进程**（正是 `AGENTS W6` 警告的形态），它们**真的写了正式库**。已 kill |
| 正式库 sha256 变了 | **不是损坏**。长度完全一致（28,076,421,120 B），事后只读核验：70 张表 / schema v49 / `indicator_snapshot` 5,575 行 / `institution_holdings` 432,523 行 / 86 期 / `price_daily_qfq` 17,554,918 行 —— 与之前独立核过的值**全部一致**。属孤儿进程就地写了状态行 |
| 工作区 `reports/NNN` 计数 246 vs 提交态 240 | 差的 6 处来自另一会话，**不计入本轮** |

**我这边的替代验证**：不再依赖被污染的全量 S1，改为**按我改过的模块跑定向集**：
`test_rework_guards` + `test_repo_integrity_guards` + `test_null_reason_placement` +
`test_screening_cache_freshness` + `test_screening_gate` + `test_update_orchestration` +
`test_update_step_inventory` + `test_price_batch_flush` + `test_retry_persistence` +
`test_source_result_contract` + `test_indicator_ext` + `test_backup_recovery`
→ **192 passed**。

### 14.7 `getsource` 顽疾的量化（§13.7 的补充）

审查数出全仓 **13 个 `inspect.getsource` 调用点 / 11 个测试函数 / 10 个文件**，
其中 **9 处保护的是"代码写在哪个函数里"而不是行为**（含
`source.count("IndexValuationUpdater(") == 2` 这种按文本数构造次数的写法）。
§13.7 说的"4 块拆不动"只对应其中 6 处。

**自相矛盾之处（诚实记录）**：本轮我自己**又新增了 1 处** ——
`test_null_reason_placement.py` 里用 `sweeper.__doc__` 断言 docstring 含某些字。
这正是我在 §13.7 抱怨的同一件事。

最小改法（未做，约 2–3h）：建 `tests/support/source_guard.py` 收敛这类断言；
把 4 个 `update.py` 卡点改成"记录构造 kwargs 的 stub"行为测试；
加一条 `source_guard_sites <= 13` 的棘轮，防止继续增加。

### 14.8 本轮新增/仍未做

**已修**（14.1 / 14.3）：缓存 fail-closed、RG6 内容判据、RG9 WARN、硬上限数值锚定、
`retry.py` docstring 不实表述。

**仍未做（诚实清单）**：
1. `source_audit` / 归档表 / 价格表 / `indicator_snapshot` 的指纹仍是 `COUNT+MAX`，
   **就地翻标记依旧假绿**；`_CONTENT_CHECKSUM_ROW_BUDGET=2,000,000` 超预算会退回 count-only（盲区随数据增长回来）。
2. 5 个历史提交的 import 不自洽窗口（不修，见 14.5）。
3. `getsource` 顽疾（13 处）与 4 块 `update.py` 拆不动。
4. 7 个前端 god component（`ScreeningPage.vue` 948 行仍塞 9 件事）。
5. 本轮之后另一会话仍在推进"排名口径"与"黑名单"两条线，
   **任何跨会话的最终验证都必须在仓库安静时重跑一次全量 S1**。

### 14.9 一句话

> 这轮两个审查各拍到 1 个**真洞**（缓存硬上限可绕过、RG6 一行空键即豁免）、
> 纠正了 **5 处夸大或表述不实**、并暴露出**我违反自己立的 W8 共 3 次**。
> 全部已修并补了反向用例。**但"全量 S1 全绿"这句话本轮不成立** ——
> 不是代码有问题，而是仓库里同时有另一个会话在改；
> 结论必须等仓库安静后重跑一次才能下。

## 15. 第三轮：按 W4 隔离工作树后清账（2026-11-19）

> 承接 §14.8 的"仍未做"。这一轮**先遵守新立的 `AGENTS W4`**（多会话私有化），
> 再并行清账；过程中把 W4 的执行器本人修了一次。

### 15.1 遵规开工：preflight BLOCK → 建私有 worktree

开工自检 `scripts/session_preflight.py` 直接 **BLOCK（exit 2）**：
- 已存在另一个 worktree（`.agents/worktrees/fix-quarterly-coverage-fallback`，**带 5 个未提交改动 = 活的会话**）
- 主工作树当时是干净的，但这不改变结论：**不能在共享树上叠**

按 W4 用 `create_worktree` 建了私有工作树：
`.agents/worktrees/finish-refactor-backlog`（分支 `finish-refactor-backlog`，基于 `3b7711f`）。
后续所有改动、测试、提交都在那里，主仓库一个字节都没动。

**顺带修了 W4 执行器自己的一个自相矛盾**（`e331b7a`）：
`check_worktrees` 原来只要数出 >1 个 worktree 就 BLOCK —— **把自己刚建的那个也算成"别人"**，
于是"按规则做反而永久开不了工"。修正为：
- 当前目录在**主工作树**且有额外 worktree → BLOCK（没隔离，这才是该拦的）
- 当前目录已在**非主 worktree 内** → PASS（W4 已满足）

两个必要配套（否则"永远比不中 ⇒ 还是 BLOCK"）：
`git worktree list` 要加 `-c core.quotePath=false`（否则非 ASCII 路径被转义成 `\346\216\214…`），
路径比较用 `realpath + normcase` 归一化。补 4 条用例（含"路径含空格"的解析，
解析错同样导致永久 BLOCK）。

**worktree 里的环境事实**（写进 `docs/skills/multi-agent-safe-session/SKILL.md`）：
`.venv`/`data/`/`node_modules` 都是 gitignore，不会跟过来。做法是**复用而不复制**：
- Python 用主仓库解释器（`"$MAIN/.venv/Scripts/python.exe"`）；**不要 `uv run`**（会在 worktree 里新建 250MB venv）
- 前端用 **目录联接**：`New-Item -ItemType Junction -Path frontend/node_modules -Target <主仓库>/frontend/node_modules`
- 正式库走绝对路径 + `read_only=True`

### 15.2 四个 agent 并行清账（文件租约互斥，W8）

| agent | 交付 | 关键数字 |
|---|---|---|
| **a-update-split** | 先解 getsource 死结，再搬 `_retry_failed_tasks` | `update.py` **6457 → 6156**（−301）；方法数 **137 → 137**；getsource **13 → 8** |
| **b-frontend-god** | 拆 `ScreeningPage` + 5 套轮询收敛 | **1060 → 593 行（−44%）**；前端测试 **87→100 / 70→95** |
| **c-fingerprint** | 补指纹 4 个盲区 + 降级可见 | 净耗时 **+14~18ms**（+3~4%），红线内 |
| **d-guards** | RG3 双向化 + 清单漂移守卫 | 真树探针：塞死键 → 门禁立刻红 |

### 15.3 解掉"反重构死结"（本轮最有价值的结构性一步）

§13.7/§14.7 反复讲过：4 块代码拆不动，不是因为难，而是**测试用 `inspect.getsource`
断言"代码写在哪个函数里"**——搬家就红。

先建 `tests/support/source_guard.py`（342 行）把这类断言换成**行为级**：
- `constructed_with()` 记录构造 kwargs（真跑，不读源码）→ 断言"注入了同一份共享 adapter_mgr"
- `count_construction_sites()` 用 AST 数构造点（注释/日志里的同名字不算）
- `resolve_body()` 顺"同名委托壳"追到真正实现 → 方法体搬走之后断言仍指向实现
- 价格侧改成**真跑一轮** `_update_prices_incremental`，看内存水位有没有被标记

**8 个变异逐个验证"真的会红"**，其中 **MUT6 第一版仍是绿的（假阴性）**——只要求"出现过一次"，
加强成"最少展开次数 2"才红。这条很典型：**改测试的人自己也会写出假绿**，不靠变异检验发现不了。

搬完：`update.py` 6457 → 6156，`IncrementalUpdater` 方法数仍是 137（原方法留 5 行同名委托）。
**刻意没搬** `_update_prices_incremental`（504 行，3 executor + 自适应并发 + 攒批 + 20+ 条 SQL）
与 `_run_incremental_update_flow`（`STEP_ORDER` 的事实来源）——时间盒到点，宁可不搬。
另加棘轮 `test_update_structure.py`：`UPDATE_PY_MAX_LINES` 只许降、getsource 站点逐文件登记总量 == 8。

### 15.4 前端：从"评估"到"真动手"

`ScreeningPage.vue` **1060 → 593 行**，拆出 5 个"一个文件一件事"的模块（共 770 行）。
**等价性用三重证据**：①脚本抽出全部 26 个被搬走的函数体对拍，归一化后 **26/26 一致**
（允许的改名只有 5 类同值注入）；②`git diff` 确认 **template/style 零改动**；
③既有组件测试一行未改仍全过。

轮询从**5 套手写 timer** 收敛到 1 个 `usePollTimer`（含 `App.vue` 的 `setInterval`），
并加**静态门禁** `polling-single-source.test.ts`：`src` 下再手写 `setInterval` 就红。
这条护栏**上线时先红后绿**，抓出了 `App.vue`。

**判断"不该拆"的也写清了**：`StockDetailPage`(805，并发+K线+TOC 交织，16 个既有测试)、
`EtfStrategyPanel`(738，17 个 ref 是同一张表单的状态，且 5 个模态无测试覆盖 ⇒
**先补测试再拆**)、`PointInTimeCard`/`ResearchStatisticsCard`（"多件事"其实是 computed 派生）、
`types/data-quality.md` 系（纯类型，按行数判巨无霸是误判）。

### 15.5 指纹的 4 个盲区

先量了个被文档写错的事实：**指纹的主成本是"连接"不是"计算"** ——
`read_query` 每次新开只读连接，28GB 库上开一次 **92–142ms**，所以真实基线是
**430–510ms**（不是文档里的 90–120ms）。

| 盲区 | 结果 |
|---|---|
| `source_audit` 就地翻标记 | ✅ 已堵（+ `GROUP BY confidence` 分布拼接，+69ms） |
| 归档表 / `fetch_batch` | ✅ 已堵 |
| 价格表从未进外层指纹 | 🟡 **部分**：新增行能让指纹变；**就地重算仍看不见**（内容信号实测 +275ms = 基线 +60%，超红线放弃），边界已用测试钉住 |
| 超预算静默退回 count-only | ✅ 已堵：payload `degraded{}` + WARNING + 缓存 entry 字段，降级态与正常态**永不互命中** |

**顺带发现上一轮方案的一个真缺陷**：`bit_xor(hash(col))` 是**奇偶敏感**的 ——
整列取值相同时偶数行互相抵消 → **xor 恒为 0，整列改动也看不出来**。
本轮新信号一律用"分组拼接"；既有 `_CONTENT_CHECKSUM_TABLES` 的 xor 弱点登记为遗留项。

### 15.6 门禁：RG3 终于双向

原 RG3 只查"后端有前端缺"，看不见**前端死键**——上一轮修的
`dividend_financing_ratio`（前端把不存在的字段当百分数 = 100 倍误差）正是它漏掉的形态。
现在两个方向都报，并引入 `FRONTEND_ONLY_ALLOWED` 白名单（**当前为空**），
白名单条目必须仍真实存在，否则报「白名单过期」。

**真树探针**（不是只跑单测）：往 `PERCENT_FIELDS` 塞一个不存在的键 → 门禁立刻红并给出
"确属历史别名请登记进 FRONTEND_ONLY_ALLOWED 并写明理由"；还原即绿。
（第一次探针塞错了位置——`PERCENT_FIELDS` 这个词在注释里先出现，改了注释自然不报。
**探针必须打准**，否则会得出"门禁没生效"的错误结论。）

RG10（U+FFFD）此前只有实现、没有反向用例 = 装饰，已补 3 条（注入必报/转义写法不许误报/
本仓库自身必须干净）。另加**防清单漂移**用例：从 `AGENTS.md` 解析 `RG<n>` 集合与
`run_all()` 实际跑的对齐，编号还要连续。

### 15.7 过程事故：一个 agent 超时不落盘

`d-guards` 在 **15:04 之后 28 分钟没有任何文件变更**，并留下 2 个 F401（引入了 RG10 的
helper 却没写用例）。按 `AGENTS W5` 中断它，主控接手补完（RG10 三条用例 + 钩子文案）。
它的 RG3 双向化与清单漂移用例是好的，已保留。
**教训**：W5 的"超过时间盒无文件变更就中断"这次真的用上了——不做的话会一直空等。

### 15.8 仍未做（下一轮的入口）

1. **`_update_prices_incremental`（504 行）与 `_run_incremental_update_flow`（500+ 行）未搬** ——
   前者 3 executor + 自适应并发 + 攒批，后者是 `STEP_ORDER` 的事实来源。
2. **getsource 仍有 8 处**（其中 4 处是"`data_quality` 不得引用某域"的负向耦合，
   没有行为可跑；另外 4 处保护位置，需逐个人工判断）。
3. **`price_daily_qfq` 就地重算仍不可见**（内容信号超性能红线）；
   `source_audit` 的高基数列就地改仍不可见。
4. **降级信息还没进 HTTP 响应体** —— 需在 `app/web/api/screening.py` 的 `_freshness_payload`
   加一行 `"readiness_degraded": entry.get("fingerprint_degraded")`（不在本轮租约）。
5. **`helpers/runtime.ts` 未接线**（`DataStatusPage` 的空态 CLI 文案仍硬编码，打包版应显示 `vd data init`）。
6. **`EtfStrategyPanel`(738) / `StockDetailPage`(805) 未拆** —— 前置是先补行为测试。
7. 既有两个 worktree（另一个会话的 `fix-quarterly-coverage-fallback` 带 5 个未提交改动），
   **合并前必须让仓库安静下来再跑一次全量 S1**。
