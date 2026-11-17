---
title: 四轮调查的修复落地与验收（2026-11-17）
status: approved
category: reports
created: 2026-11-17
last-reviewed: 2026-11-17
related: reports/133, reports/134, reports/136
---

# 四轮调查的修复落地

> **范围**：把 `reports/133`（反返工根因）、`reports/134`（认知成本 S1–S8）、
> `reports/136`（考古与可移除清单 §7）里**已经判定清楚、可安全执行**的部分做完；
> 有风险的（大重构、需实测的）明确留到下一轮，并在 §5 写清理由。
>
> **执行方式**：8 个并行 agent 各守一段互不重叠的文件范围，完成后由 **2 个独立 agent 验收**
> （一个做对抗性代码审查，一个逐条核验"移除声明"是否真的成立）。

---

## 1. 结论（TL;DR）

| 指标（`scripts/onboarding_metrics.py`） | 清理前 | 清理后 |
|---|---|---|
| 按事故命名的测试文件 | **25** | **0** |
| `_domestic_direct` 副本 | 1 模块 + 13 处 `with`（7 次复制史） | **0** |
| 同一函数两个公开名字（遮蔽判定） | 2 | **1** |
| 只能手工同步的配置文件 | 2 份（靠人 `cp`） | **1 份 + 构建期自动同步 + 字节相等门禁** |
| 更新步骤清单 | 只存在于 492 行代码的执行顺序里 | **`STEP_ORDER` 25 步 / 12 域**，AST 强制一致 |
| 生产 bundle 里的假数据样稿 | 1 个路由 + 123 行假行情 | **0** |
| `app/core/` 里只有测试引用的模块 | 1（`csmar_formulas.py` 198 行） | **0**（迁入 `tests/support/`） |
| 标 `approved` 却在描述已废止流程的 runbook | 1（S0 证据保全 822 行） | **0**（改 `historical`） |

**没有做的事**（不是漏，是判断）：`update.py` 完整拆分、schema 迁移折叠、`data_quality` 与
`quality/` 合并、`_to_date` 收口、`screening_readiness_cache` 删除、`null_reason_sweeper` 迁出
——原因见 §5。

---

## 2. 逐项交付

### 2.1 删除"解决已不存在问题"的防御（reports/136 §5.1）

`_domestic_direct()` 曾在 7 个适配器里各抄一份，作用是在请求前 `pop` 掉 `HTTP(S)_PROXY`。
`reports/136` 已实测它在**本机从未生效**（代理来自 Windows 注册表，`os.environ.pop()` 后
`urllib.getproxies()` 仍返回 `127.0.0.1:10808`；requests 在 env 为空时回退读注册表）。

- 删除 `app/core/adapters/_net.py` 与 13 处 `with _domestic_direct():`（去一层缩进，语义不变）；
- 审计 adapters 目录**全部 6 个 httpx 使用点**：3 处原有 `trust_env=False`（eastmoney_f10 / index_valuation / ths）保留；
  另外 3 处（`cninfo` / `czb_mof` / `hk_price`）**本轮不加**——它们会真实改变代理路由且失败静默，
  属于需要真机验证的独立决策（见 §3.3）；
- `grep -rn "_domestic_direct" app/ scripts/ tests/` → **0**。

### 2.2 一个函数两个名字（reports/136 §6.2）

`data_quality.masked_indicator_fields()` 只是 `masked_fields_for_codes()` 的别名，外部调用点 **0**。
删除别名，并在 `masked_fields_for_codes()` docstring 里写明"字段级遮蔽的唯一公开入口"；
新增 `tests/regression/test_trust_gate_single_entry.py` 钉死三件事：别名不得复活、
`indicator_trust` 仍在 `data_quality`、两条入口给出同一份遮蔽清单。
（注意：`app/web/api/screening.py` 的私有 `_masked_indicator_fields(request)` 是 HTTP 层取参
helper，**不是**这个别名，保留。）

### 2.3 `_to_float` ×7（三种语义）→ `app/core/numbers.py`

新增 `to_float(value, *, allow_inf: bool = False)`，把**适配器目录里** 6 份 `_to_float` 收口
（`app/` 其他位置仍有 8 份定义：`supplement.py` 的 pandas 口径、`financial_guards.py` 的 bool→None 口径等，
属下一轮，见 §5；因此 `onboarding_metrics.py` 的 `duplicate_helper_names` 本轮仍是 19）。
**逐处零行为变化**：用 32 组边界输入（`None`/`""`/`"nan"`/`"inf"`/`"-inf"`/`"1e400"`/`True`/`False`/
`np.float64("nan")`/`pd.NA`/…）把"新实现 vs 从 `git show HEAD` 取回的原实现"逐对对拍，6/6 一致。
`czb_mof` 的"拒 NaN/±Inf"（`reports/73` P3-10）与另外 4 处的"NaN→None 但保留 ±Inf"都被保留，
差异写进各自注释与模块 docstring。

### 2.4 打包配置不再靠人记（reports/136 §6.1）

两份 `default.yaml`（仓库源 + `app/resources` 打包兜底）此前只能手工 `cp`，`reports/135` 里就出过
一次真实事故（打包副本 93 行 vs 源 269 行、缺 51 个 `update.*` 键、把 `cninfo` 塞回分红链）。

- 新增 `scripts/sync_packaged_config.py`：默认 sync（二进制复制 + 原子替换 + 写回逐字节校验），
  `--check` 只比较（漂移时打印 diff 摘要与修复命令，退出码 1）；
- `scripts/build-release.ps1` 在 **S1 门禁与 `npm run build` 之前**调用它，失败即中止构建；
- 原有"两份必须逐字节相等"的测试保留（只把失败提示改成指向新脚本），另加 7 条脚本级测试。

### 2.5 25 个按事故命名的测试 → 按行为命名（reports/134 §5 S7）

`test_fix20261112_*` / `test_p0_*` 全部 `git mv` 为行为名（如 `test_publish_gate_contract.py`、
`test_qfq_refetch_pending.py`），事故编号搬进 docstring 首行（25/25）；
新增 `tests/regression/README.md` 的旧名→新名映射表（25 行），因为 `docs/reports/` 里有 16 处
历史引用**不许改正文**；用例总数 **1735 → 1735**（收集与实跑均一致）。
`onboarding_metrics.py` 的 `incident_named_tests` 从 25 降到 **0**。

### 2.6 把"更新有哪几步"变成数据（reports/134 §5 S1 的第一步）

新增 `app/core/update_steps.py`：`UpdateStep(name/label/domain/note)` + `STEP_ORDER`
（**25 步 / 12 个域**，严格按 `_run_incremental_update_flow` 的真实顺序）；
`update.py::report_step()` 加 3 行白名单告警（未登记步骤打 `warning`，不改行为），
`update.py` 净增 **4 行**。新测试用 AST 扫出 29 处 `report_step(...)`、去重 25 个步骤名，
断言"代码里的每一步都在清单里、清单无重复、顺序与首现顺序一致"。
附带发现：`auto_update.py::STEP_LABELS` 是**第二份步骤标签清单**（只覆盖 25 步里的 17 步），
已在本轮 docstring 标注为待收口（S8）。

### 2.7 前端：删掉会进生产 bundle 的假数据样稿（reports/136 §5.2）

`/design-preview` 路由 + `ResearchWorkbenchPreviewPage.vue`（123 行，内联 600519/五粮液等假行情，
无导航入口、只能手输 URL）。删除后顺带清掉 `App.vue` 里 4 处 `route.meta.staticPreview` 死分支
（两个守卫、一个 skip-link 的 `v-if`、一个死 `<router-view>` 并把 `v-else` 提升为无条件渲染）。
`grep -rn "staticPreview" frontend/src` → **0**；`npm run lint` / `npm run test`（14 files / 67 tests）/
`npm run build` 全绿；构建产物里不再有 Research/Preview chunk。

### 2.8 文档：S0 降级 + "不要删"清单（reports/136 §5.4、§8）

- `docs/runbooks/s0-evidence-preservation.md`（822 行，`approved`）→ `status: historical` +
  顶部 banner 指向 S1 现行做法；正文一字未删（07-22 事故证据口径）；
- `AGENTS.md` 新增「不要删清单」10 条（每条写明"看起来可删的理由 / 删了会重演的事故"）；
- `CODE_MAP.md` 新增 §9（已执行 / 已撤回 / 待实测），并修正了 §5 的同名陷阱与 §8 的旧测试命名说明；
- `README.md`、`docs/STATUS.md`、`ops-build-release-s1.md` 里指向 S0 runbook 的表述同步标注 `historical`；
- `onboarding_metrics.py` 新增指标 `stale_approved_docs`（当前 **63 份** = 标 `approved` 但
  ≥60 天未复核；这是 `reports/136` §6.9 的量化，先度量、不门禁）。

---

## 3. 撤回：两条"看起来可删、实际被契约钉住"的项

> **对 `reports/136` 的更正**：按 AGENTS.md「禁止修改 `docs/reports/` 历史报告正文」，
> 本次**没有改动 `reports/136` 的正文**。它的 §7 表格里下面两行**以本报告为准**（视为被本节取代）：
> `index.html` 取消跟踪、删除 `scripts/legacy_dangerous/`。

### 3.1 `scripts/legacy_dangerous/`：**确实不能删**（证据最硬）

它不是遗留物，而是"危险脚本隔离"约定的**固定 fixture**，被 `tests/regression/test_maintenance_scripts_safety.py`
当存在性与内容双重断言使用：

```python
:29  LEGACY_FILE   = SCRIPTS_DIR / "legacy_dangerous" / "patch_deducted_profit.py"
:30  LEGACY_README = SCRIPTS_DIR / "legacy_dangerous" / "README.md"
:60  assert LEGACY_FILE.is_file()
:61  assert LEGACY_README.is_file()
:72  def test_legacy_dangerous_script_would_be_flagged_if_scanned_directly(): ...
```

删目录 → 这 2 个用例直接变红（另有 5 条内容断言也依赖这两个文件）。
`scripts/verify_no_dangerous_maintenance.py` 因为显式排除该目录，**不会**替你报警——
所以 `reports/136` §7 里"删了以后验证器仍通过"这句话是真的，但它漏看了 pytest 侧的门禁。
**结论**：保留；若一定要删，等于重写门禁（改常量 + 2 个测试函数），不是"纯减 248 行"。

### 3.2 `app/web/static/index.html`：撤回，但**理由要改**

验收 agent 用 wheel 负向对照（清 `build/` + `--no-cache-dir` 重建）证明了真相：

- `tests/regression/test_wheel_contents.py:30` 确实断言 wheel 内含有 `app/web/static/index.html`，
  且该断言对该文件敏感；
- 但同一测试第 31 行还断言 `app/web/static/assets/` 存在，而 **assets 本来就在 `.gitignore`** →
  这个测试本来就是"**构建后产物契约**"，fresh clone 上不跑 `npm run build` 它本来就会红。
  因此"取消跟踪 index.html 会让 wheel 测试变红"这个说法**不成立**（与是否跟踪无关）。

**撤回的真正理由**（更强、更实际）：
1. **fresh clone 立刻可服务 SPA 根路径**——源码直跑 `vd web` / `python -m app.web.main` 不必先 `npm run build`；
2. `.gitignore` 有意只忽略 `app/web/static/assets/` 而不忽略 `index.html`，是既有设计，不是漏配；
3. 取消跟踪的收益（少一个构建产物进 git）远小于"干净 clone 打开页面 404"的代价。

### 3.3 顺带撤回的一项代码改动：3 处 `trust_env=False`

`reports/136` §5.1 建议"删除 `_domestic_direct` 之后用 `trust_env=False` 实现直连"。执行时确实加了 3 处
（`cninfo_adapter` / `czb_mof_adapter` / `hk_price_adapter`），但独立验收 agent 实测发现：

- 这 3 处**不是等价重构**：`_domestic_direct` 只是 `os.environ.pop`（实测从未生效），
  而 `trust_env=False` 会让 httpx **跳过系统代理**（httpx 的 `trust_env` 在 Windows 上经
  `urllib.getproxies()` 读到注册表里的 `127.0.0.1:10808`）→ 公告主源与国债曲线从"走代理"变成"直连"；
- 两处失败路径都是返回空 `FetchResult`（`_make_empty_result`），**失败长得像"源没数据"，不会红**；
- 全部离线测试覆盖不到真实网络路由。

**决定**：这 3 行**从本次提交撤回**（恢复 HEAD 行为），只在代码里留下注释说明"刻意保持
`trust_env=True`，直连是独立决策"。要改必须单独走一次"真机探针 + 观察一轮真实更新"的验证，见 §5。

## 4. 验收（独立 agent + 主控门禁）

| 门禁 | 结果 |
|---|---|
| `uv run --locked ruff check app tests scripts/check_rework_guards.py scripts/onboarding_metrics.py scripts/sync_packaged_config.py` | 见 §6 |
| `python scripts/check_rework_guards.py`（RG1–RG4） | 通过 |
| `python scripts/onboarding_metrics.py` | `incident_named_tests 25 → 0`；`app_files/app_loc` 见 §6 |
| 定向 pytest（适配器 / 可信判定 / 更新编排 / 配置 / 派生列 / 门禁本体 / 重命名后 25 文件 / csmar） | 见 §6 |
| 前端 `npm run lint` + `npm run test` + `npm run build` | 14 files / 67 tests / build 通过 |
| 独立验收 agent 1（对抗性代码审查） | 见 §6.2 |
| 独立验收 agent 2（移除声明逐条核验 + 两条撤回的证伪） | 见 §6.3 |

---

## 5. 明确留到下一轮（附理由，避免"忘了"）

| 项 | 为什么这轮不做 |
|---|---|
| `update.py` 完整拆分（S1 其余部分：12 个域搬到 `steps/*.py`） | 6,315 行 / 136 方法，且流程里嵌着几十条事故教训（如"treasury 必须提前到统计域之前"）。本轮只做了"步骤清单化"这一步，剩下必须一域一 PR、每步绿灯 |
| schema 迁移折叠（63 条 → baseline + 少量 ALTER） | 会影响"旧库能否升级"，需要真实的旧版本库做升级测试；当前没有这样的 fixture |
| `data_quality.py` 与 `quality/` 合并（S4） | 4,275 + 2,017 行、9 个调用模块，且会同时动 2 个循环导入；应与 S1 分开做 |
| `_to_date` ×6 收口 | 与 `_to_float` 不同，日期解析在 6 处对"空串/非法值/时区"的处理各不相同，需要先定语义再改 |
| `screening_readiness_cache` 删除（L3） | 需要干净库冷启动实测（<2s 才可删）；本轮没有可用的计时窗口 |
| `null_reason_sweeper`（1,279 行）迁出 `app/core` | 它只被 `scripts/classify_null_reasons.py` 使用，但迁出要同步改脚本 import 与测试；与 S6（仓储层）一起做更省事 |

---

## 6. 最终门禁数值（本会话实测）

> 由主控在本轮所有 agent 完成后统一执行，命令与数字如下（详细输出见提交说明）。

### 6.1 主控门禁（在全部 agent 完成后、稳定树上执行）

| 命令 | 结果 |
|---|---|
| `uv run --locked ruff check app tests scripts/check_rework_guards.py scripts/onboarding_metrics.py scripts/sync_packaged_config.py` | All checks passed |
| `python scripts/check_rework_guards.py` | 通过（RG1–RG4） |
| `python scripts/onboarding_metrics.py` | `incident_named_tests` **25 → 0**；`_to_float` 适配器副本 **6 → 0**（改为 `numbers.py` 具名别名）；`app/` 仅剩 2 处语义确实不同的 `_to_float`（`supplement` 的 pandas 口径、`financial_guards` 的 bool→None 口径） |
| 定向 pytest（**最终稳定树上 19 个文件**：适配器/国债/指数估值/可信判定/筛选/更新编排/配置/门禁/派生列/ROCE） | **266 passed / 0 failed** |
| 本轮各批次累计（含重命名 25 文件 237、适配器 216、更新 81、配置 11+25、可信判定 57） | **893 项次 / 0 failed** |
| 全量回归 `pytest tests/regression`（隔离 env，1735 用例） | 见 §6.4 |
| 前端 `npm run lint` / `npm run test` / `npm run build` | lint 0 problem；14 files / **67 tests passed**；build 通过（`vue-tsc -b` + vite，产物 40 chunk） |

### 6.2 独立验收 agent 1：对抗性代码审查（结论摘要）

- **A / G / C / D / E / F / H：PASS**，每项都有独立复算（不用 agent 的结论）：
  `to_float` 用 45 组边界输入 × 6 文件对拍 **0 差异**；13 处 `with` 删除用 AST 节点计数确认
  只有 `With` 消失、`Try/Except/finally` 不变；`STEP_ORDER` 用自写 AST 脚本核对 29 处调用 /
  25 个步骤名 / 顺序一致；25 组重命名用 `git show HEAD:<old>` 做 AST def 计数 **215 → 215 delta 0**；
  `--check` 的 0/1/2 三种退出码实测。
- 提出 10 条问题，其中 3 条被采纳并**当场修掉**：
  **P1** 3 处 `trust_env=False` 是真实路由翻转（不是等价清理）→ 已从本提交撤回并记录决策（§3.3）；
  **P4** 我改了 `reports/136` 正文（违反 AGENTS.md §3）→ 已 `git checkout` 还原，更正全部搬进本报告 §3；
  **P6** 测试名与被测对象分家（只测共享函数、适配器回退也绿）→ 改为 identity 断言
  `_czb._to_float is to_float_strict`。
- 其余为**记录性/低风险**：P5（`_to_float` 计数未降——已通过具名变体 + 新的
  `duplicate_helper_defs` 指标修正口径）、P7（`pdf/manager.py`、CLI 探针仍走默认代理策略，
  本轮不动）、P9（`tests/support` 依赖仓库根在 `sys.path`，pytest 下成立）、
  P10（`build-release.ps1` 用 sync 而非 `--check`：构建期自愈 + 门禁兜底，属有意取舍）。
- **P2 判断**：`app/web/static/index.html` 是 `npm run build` 产物（5 行 asset hash）。
  仓库历史上一贯把该文件随前端改动一起提交（如 `5a438b1`、`bdcef57`），且本地运行的服务
  正服务这份新产物（回滚会让浏览器 404 新 hash 的 JS），因此**保留提交**，并在提交说明里标注。

### 6.3 独立验收 agent 2：移除声明逐条核验（结论摘要）

8 条移除声明**全部成立**，且每条都用两种以上手段复核（grep / `importlib.find_spec` /
`git ls-files` / AST / `--collect-only` / `cmp`+sha256）：

| 声明 | 判定 | 关键证据 |
|---|---|---|
| `_domestic_direct` / `_net.py` 彻底消失 | 成立 | `find_spec('app.core.adapters._net')` → None |
| `masked_indicator_fields` 已删 | 成立 | AST 确认 `data_quality.py` 无该 def，仅剩 screening 的私有 helper |
| `/design-preview` + 假数据页 + `staticPreview` 消失 | 成立 | 全量 44 个前端文件逐个搜 needle → 0；路由 9 条 |
| `csmar_formulas` 迁出运行时 | 成立 | `import app.core.csmar_formulas` → ModuleNotFoundError；裁定测试 10 passed |
| 25 个事故命名测试改名 | 成立 | 残留 0；映射表 25 行与新名一一对应；`--collect-only` **1735** 不变 |
| 打包配置自动同步 | 成立 | `--check` exit 0；构建脚本在 npm build/PyInstaller 之前调用；sha256 相同 |
| S0 runbook 降级 | 成立 | front-matter `status: historical` |
| `screening_readiness_cache` 未删 | 成立 | 10 处引用仍在（L3 待实测） |

两条撤回项的证据强度不同，且**其中一条的理由被它纠正**（§3.2）：
`legacy_dangerous/` 是测试固定 fixture（存在性 + 内容断言），删了 2 条门禁直接红——撤回坚实；
`index.html` 的 wheel 测试**本来就要求构建后产物**，所以"取消跟踪必然打红门禁"不成立，
撤回理由已改为"fresh clone 即可服务 SPA + 与既有 .gitignore 设计一致"。

### 6.4 全量回归：**不能裸跑**，这是一个必须记住的运维事实

本轮尝试过直接 `pytest tests/regression`（1735 用例、隔离 env）。跑到第 25 分钟时观察进程树发现：

```
550244  app.web.main               ← 某个用例在子进程里真的起了一个 Web 服务
553156  app.cli.main data auto-update run   ← 另一个用例真的起了一轮自动更新（2.9GB RSS / 604s CPU）
```

也就是说**裸跑全量会派生真实服务与真实更新子进程**（它们用的是隔离库，但仍然是真实子进程）；
终止 pytest 后这 4 个孤儿进程仍在跑，已手动清理。这正是 `scripts/s1-pytest.ps1` 存在的理由
（进程树 runner + 前后指纹 + 强制收尸），**不要绕过它跑全量**。

本轮因此改为：**官方 S1 留待服务冻结窗口**（`scripts/s1-pytest.ps1 tests/regression`），
验收采用"受影响面大定向回归"：

| 批次 | 覆盖 | 结果 |
|---|---|---|
| 适配器 + 国债 + 基金/指数估值 + 港股分红 | `_domestic_direct` 删除、`to_float` 收口、trust_env 撤回 | **216 passed / 0 failed** |
| 可信判定 + 筛选 API + 质量分级 + 数据状态 | 别名删除、单入口护栏 | **57 passed** |
| 更新编排 + job 状态 + 步骤清单 | `STEP_ORDER` 接线 | **81 passed** |
| 配置同步（11 条新用例）+ 相邻门禁（release/wheel/maintenance） | `sync_packaged_config` | **11 + 25 passed** |
| 重命名 25 文件 + csmar 裁定 | 改名与迁出 | **237 passed**（收集 1735 不变） |
| 反返工门禁 + 派生列不变量 + ROCE | RG1–RG4 与派生域守卫 | 含在上面的 `test_rework_guards` / `test_snapshot_derived_columns` / `test_roce` |

> 另：清理后 `python scripts/onboarding_metrics.py` 显示 `app_files 106 / app_loc 64,381`、
> `同名顶层函数 19 组 / 冗余定义 24 份`、`测试 0/160 按事故命名`、`过期 approved 文档 63 份`。
> 其中"冗余定义"是本轮新增的指标口径（只数 def，不数别名 import）：`_to_float` 从 ×8（冗余 7）
> 收到 ×2（冗余 1），其余 2 处是 `supplement` 的 pandas 口径与 `financial_guards` 的 bool→None 口径，
> 语义确实不同，留待下一轮。
