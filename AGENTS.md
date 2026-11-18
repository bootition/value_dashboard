# AGENTS.md — 项目智能体规则

本文件为 AI 助手（opencode/Codex 等）在本仓库工作的强制规则。

## 0. 单一来源协议（先读这一节）

本文件是 **AI 在本仓库行为规则的唯一来源**，也是唯一的注入面。四类内容分工固定，不许混：

| 内容 | 唯一落点 | 禁止 |
|---|---|---|
| AI 该怎么干活（工作流、纪律、禁区） | **本文件**（§执行纪律 / §反返工 / §Git 纪律 / §不要删清单） | 新建 `*lesson*`、`*规则*` 文档；把规则写进报告正文；只留在对话里 |
| 一次事件的证据（数字、日志、复现） | `docs/reports/<NNN>_*.md` | 在 AGENTS.md 里堆数字 |
| 运行期硬约束 / 环境事实（DuckDB 上限、源封禁、schema） | `docs/runbooks/ops-knowledge-base.md` | 在代码注释或报告里重抄一遍 |
| 可复用操作流程（如建复合指标） | `docs/skills/*/SKILL.md` | 把 10 步清单塞进 AGENTS.md |

**规则格式**（新增必须照抄；`RG5` 强制「编号唯一 + 每块带证据 + 总行数 ≤300」）：
`### W<n|R<n> 一句话标题` + `- 规则：可执行的动作` + `- 为什么：<reports/NNN §x | tests/… | scripts/…>`。

编号约定：`W*`=工作流、`R*`=返工根因，**都只在本文件定义**；`D*/S*/E*/I*/C*` 归知识库。
注意：知识库 §10 的 `R1–R16` 是 2026-10-30 崩溃恢复项，与本文 `R1–R8` 同名不同域，
引用时必须写 `AGENTS R3` / `KB R5`。

**容量**：本文件 ≤ 300 行（`RG5` 强制）。超限按文末「规则维护」降级，不许无限追加。
**执行现实**：本仓库**没有 CI、没有 git hook**（`reports/140` 实测）——所有门禁都靠人记得跑。
这就是为什么规则必须配"一个字就能跑"的检查器，也是为什么 §常用命令 里每条都必须是可复制的一行。

## 项目一句话

A股价值投资研究与筛选工具：FastAPI + DuckDB/SQLite + Vue3 前端，CLI 入口 `vd.bat <command>`。

## 文档规则（强制，防止"读到过期结论"）

### 1. 必读顺序（任何任务开始时）

1. `docs/reference/CODE_MAP.md` — **代码地图**：想改 X 就读 Y（含 7 本词汇表 + 同名陷阱）
2. `docs/STATUS.md` — **当前状态唯一权威**，含当前裁决、剩余缺口、进行中工作
3. `docs/README.md` — 文档地图与生命周期规则
4. `docs/runbooks/ops-knowledge-base.md` — **域知识/运行期硬约束**（`D*/S*/E*/I*/C*` 定义处）。**不必通读**：
   改数据/更新/存储/口径前先 `grep -n "<关键词或编号>"` 定位；域事实一律以它为准
5. 任务相关代码/文档；做指标/筛选时读 `docs/skills/composite-indicator/SKILL.md`

### 2. 文档状态语义（front-matter `status` 字段）

| 状态 | 含义 | 处理 |
|---|---|---|
| `approved` | 当前有效 | 可作建议依据 |
| `superseded` | 已被取代（`superseded-by` 指向新文档） | **禁止**引用为当前结论；仅追溯用 |
| `historical` / `archived` | 历史决策 / 已归档 | 同上；`archived` 默认不读 |

### 3. 禁止行为

- ❌ 引用 `status: superseded` 的结论当当前状态（如 25/27/28 的 BLOCK 已被 29 更新）；引用 `docs/archive/`、
  `docs/evidence/` 当结论依据（证据 ≠ 结论）
- ❌ 把会话产物（findings/progress/task_plan）写入 `docs/` —— 一律放 `.planning/<date>-<topic>/`
- ❌ 修改 `docs/reports/` 历史报告正文（只可改 front-matter 状态字段）

### 4. 必须行为

- ✅ 建议必须标注依据路径 + `last-reviewed` 日期；新文档必须带 front-matter（status/category/last-reviewed）
- ✅ 结论/状态变化时：更新 `docs/STATUS.md` → 新报告写 `docs/reports/` → 旧文档 front-matter 标 `superseded` + `superseded-by`
- ✅ 机器证据（JSON/hash 等）只放 `docs/evidence/`
- ✅ 修订 PRD（`docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md`）时同步更新其 `last-reviewed` 并记录变更到其修订章节

### 5. 隐性知识回写规则（强制，防止"知识只存在于对话里"）

**背景教训（2026-09-01 体检）：** 大量运行期硬约束只活在报告/会话里（12GB 实验库"为什么不敢删"、DuckDB 上限、东财封禁），新会话要翻几十份报告才拼得全。现立规则：

- ✅ **对话中确认的任何固定事实/硬约束/保留期限/口径裁决，必须在本会话内写入文档**——优先登记到 `docs/runbooks/ops-knowledge-base.md`（运行期硬约束唯一常驻入口，含环境/数据源/DuckDB/口径/门禁/git 约束），同时更新其 `last-reviewed`
- ✅ 新约束涉及状态变化时，按 §4 流程更新 `docs/STATUS.md`
- ✅ 决策附着在决策物上：schema/代码级设计（quarantine 隔离表、冷热分层）在代码注释或 knowledge-base 登记意图；**AI 自己的工作流教训**一律并入 §执行纪律，不得新建 `*lesson*` 文档
- ❌ 禁止把"定了但没写"的结论只留在对话里；会话结束时对话中不应残留未落文档的固定结论
- ❌ 禁止删除/清理任何有保留期限的产物（如数据库重建回滚快照）而不在本文件/STATUS 登记依据

## 常用命令

```bash
# 测试（S1 隔离回归，保护正式数据库）
scripts/s1-pytest.ps1 tests/regression
# 静态检查
uv run --locked ruff check app tests/regression
# 全部门禁一条命令：RG1–RG9（时区/状态合并/单位/派生域/规则来源/编号/supersede/真相源/测试资产）
uv run --locked python scripts/check_rework_guards.py
# 认知成本度量（不做门禁；改结构/拆文件后用它证明"真的变简单了"）
uv run --locked python scripts/onboarding_metrics.py --compare docs/reference/onboarding-baseline-2026-11-17.json
# 前端门禁
cd frontend; npm run lint; npm run test; npm run build
# 锁文件
uv lock --locked
```

## 执行纪律（W1–W7，2026-11-17 会话实测 `reports/138`）

> 这一节管的是"AI 怎么干活"，不是"产品怎么工作"。复盘来源：一次会话光 `sleep` 就等掉约 2.5 小时、
> 两次全量回归 57 分钟且失败与改动无关、一个 agent 两小时零落盘（`reports/138`）；并行写同一文件见 `reports/140`。

### W1 改文件只用"精确匹配、不匹配就报错"的编辑，不用 sed / 内联 heredoc 拼中文或 SQL

- 规则：改文件用精确字面量替换（替换前后各校验一次）；批量生成内容先写临时文件再落盘；
  每次改完立刻 `ruff check <单文件>`。
- 为什么：`reports/138` §3.1 —— 3 次替换静默没生效、2 次控制台编码把中文写成 mojibake 直接
  `SyntaxError`、1 次写入真实换行把文件写成语法错误（29 个 ruff 报错），每次都要重读整块重写。

### W2 后台任务用作业工具等待，不用 `sleep N` + `tail`

- 规则：起后台任务后用 `job_output(wait=true, timeout_ms=…)` 阻塞等待；禁止用 `sleep` 轮询日志。
- 为什么：`reports/138` §2 —— 单会话纯 sleep ≈ **2.5 小时**，是最大单项浪费；工具本身不是瓶颈
  （python 启动 151ms、开库+聚合 329ms）。

### W3 查证类查询先聚合后取样，禁止无 `LIMIT` 的明细 dump

- 规则：先 `COUNT(*) GROUP BY 桶`；需要样本再 `LIMIT 10`；扫大表前先想"能否用主键/索引缩成区间"。
- 为什么：`reports/138` §1 —— 未加 LIMIT 的明细 dump 一次吐 14KB 被截断进 spill 文件，关键信息淹没；
  同一个聚合对 14 万行表重复跑了 3 次。

### W4 agent 在跑时不动共享资源

- 规则：任何 agent/后台任务在跑时，不重启 Web 服务、不写正式库；要验证接口就在临时库上起临时实例。
- 为什么：`reports/138` §3.3 —— 两次重启 Web 服务把 DuckDB 变成独占锁，所有只读审计/修复连接失败，
  白等两轮。

### W5 派活三件套：时间盒 + 落盘要求 + 失败退出路径

- 规则：给 agent 的任务书必须写死「最多 N 分钟」「每完成一个文件就落盘」「做不完交现状 + 卡点」；
  判据类任务由主控先用一条 SQL/一次复现**自证判据成立**再派出去；空闲 agent 先用 `send_message`
  催一次进度；超过时间盒仍没有文件变更就中断它、自己做。
- 为什么：`reports/138` §4 —— 一个 agent 两小时零落盘：判据本身是错的，任务书又写了"建议先 copy
  28GB 库彩排"这种没有时间盒的前置动作。

### W6 测试范围：定向优先；全量 S1 只走包装器，禁止裸跑

- 规则：改动 <5 个文件先跑定向用例；全量只在合并前跑**一次**，且必须
  `scripts/s1-pytest.ps1 tests/regression`。服务在跑时 wrapper 按设计拒绝（"freeze S1"），不得绕过。
- 为什么：`reports/138` §1/§6.4 —— 两次全量共 57 分钟且失败原因都与改动无关；**裸跑全量会派生真实的
  `app.web.main` 与 `data auto-update run` 子进程**（本轮实测留下 4 个孤儿进程，已清理）。
- 附带：新增测试按 `tests/regression/README.md` 的命名约定（`test_<域>_<行为>.py`，事故编号进 docstring）。

### W7 删除 / 取消跟踪前先 grep 契约测试

- 规则：`reports/136` §7 这类"可移除清单"只是**候选**；动手前先
  `grep -rn "<路径>" tests/ scripts/ *.spec docs/runbooks/`，有存在性断言或**运维指引**就不能删，
  并回来更正清单。
- 为什么：`reports/137` §3 —— `legacy_dangerous/` 被两条门禁当固定 fixture；`index.html` 的"必须跟踪"
  又被误读成 wheel 测试要求（实际要求的是构建后产物）。两条都差点按清单误删/误改。
  `reports/140` 又抓到一次同型：`populate_csrc_industry.py` 在代码里零引用，却是
  `docs/runbooks/ops-data-rebuild.md` 的运维步骤 —— **grep 范围漏了 runbooks 就等于漏了使用者**。

### W8 并行 agent 必须有文件租约，写同一文件先认领

- 规则：同时跑多个 agent 时，先按文件/目录划分**互斥的写域**并写进任务书；跨域修改走主控串行
  合入。每份任务书必须列出「你只准写这 N 个文件」，越界即停。
- 为什么：`reports/140` §R9 —— `0024586` 标题自带「并发会话产出」，与 `f6e9cd2` 在
  `calculator.py`/`update.py` 上重叠 5.5 小时；`reports/135` §1 P0-2 记录"另一个会话已修"。
  静态门禁只能事后抓，管不了同时写；`W1–W7` 原本**没有一条**涉及文件所有权。

## Git 纪律（强制，防止工作丢失）

**背景教训（2026-07）：** 7-26 ~ 7-31 共 5 天的审计修复、数据重建脚本、60+ 测试全部未提交，仅因 AI 默认"不主动 commit"且项目无规则兜底。现立规则如下：

### 1. 提交时机

- ✅ **会话结束前**：若工作区有未提交变更，必须提交（这是默认动作，不再等待用户明确要求）
- ✅ **里程碑完成时**：每完成一个可独立验证的阶段立即提交
- ✅ 提交前先 `git status`，确认没有误入 `data/`、构建产物、证据目录等被 ignore 的文件；禁止跨主题打包提交（按 docs/ feat/ fix/ chore/ 拆分）

### 2. 提交边界（哪些永不提交）

- `data/`（正式数据库）、`*.whl`、`app/web/static/assets/`（构建产物）、`tests/regression/<hash>/`、`docs/evidence/evidence-s0|s1/`、`_legacy/`、`.omo/`、`.opencode/`、`.planning/`、`frontend/test-results/` —— 见 `.gitignore`，如有遗漏先补 `.gitignore` 而非硬提交

### 3. Push 纪律

- ✅ 每个会话的提交完成后 **必须 `git push`**（remote 已配置：`origin` → github.com/bootition/value_dashboard）
- ✅ 推送前 `git fetch` 检查远程是否有新提交，有冲突先解决再推
- ✅ 代理：本机通过 `127.0.0.1:10808` 访问 GitHub（已在 git 全局配置 http proxy），如网络变更需重新确认
- ✅ 重要历史分支/事故基线打 tag 并推送（如 `incident-2026-07-22`、`s1-path-isolation-archive-156dded`）
- ⚠️ **网络失败必须如实告知**：push 失败（如忘记开梯子、`Failed to connect to github.com`、认证失败等）时，**禁止**说"已推送/已提交完成"；必须明确告知用户「push 失败 + 原因 + 当前状态（提交在本地但未上远程）」，并提示打开梯子后重试（`git push`），直到 `git ls-remote origin` 确认远程已更新

### 4. 提交消息风格

参考现有历史（`feat:` / `fix:` / `chore:` / `docs:` 前缀 + 中文摘要 + 可选要点列表）。

### 5. 会话收尾检查清单（每次会话结束前强制逐项执行）

1. ✅ 工作区 `git status` 清零（有未提交变更则按主题提交）
2. ✅ `git push` 到 origin（代理 127.0.0.1:10808；失败必须如实告知，见 §3）
3. ✅ 按"文档规则 §5"提炼本会话确认的知识到 `docs/runbooks/ops-knowledge-base.md`（或更新其 `last-reviewed`）
4. ✅ 清实验产物：数据库副本、临时 CSV、调试脚本、`.planning/<本会话>/` 中间文件（有保留期限的先登记）
5. ✅ 本会话学到的"怎么干活"是否已并入本文件 §执行纪律（**不得**新建 lessons 文档）？证据是否已落 `docs/reports/`？

## 反返工硬规则（强制，2026-11-17 复盘 `reports/133`）

**背景教训（2026-10-18 ~ 11-17）：** 46 次提交里 23 次是 `fix`；25 次 `fix`+`chore` 中
**15 次（60%）在 96 小时内又改到上一份提交改过的同一批文件**；19 份报告里 5 份在同日/次日
被 `superseded`；ROCE 一个指标 40 小时内返工四次（4,322 行），PDF 除权日方案 26 小时建成
又被整体删除（1,769 行）。根因不是能力，是 7 类**固定生成机制**（详见 `reports/133` §4）。

> **总判据（先问这一句）**：这次修复有没有改变"下一次同类事故的生成条件"？
> 只改出口表现 = 延期付款，不算修完。

### R1 一个事实，只能有一个实现

- ❌ 禁止把同一语义（缺口数、市值、可信判定、时区、单位口径）在第二处再写一遍
- ✅ 发现第二份实现时，**先合并成单一函数**，再改调用方（`reports/117`/`126` 的教训）
- ✅ 单位/百分比口径必须同时登记 `app/core/screening/field_units.py` **与** `frontend/src/utils/screening-format.ts`
- 🔒 门禁：`scripts/check_rework_guards.py` RG3（跨语言单位一致）、RG1（时区单源）

### R2 状态必须由事实派生，不得由"最后一个写它的人"决定

- ❌ 禁止 `{**子步骤, "status": ...}` 这类覆盖式合并；状态一律走 `job_status.merge_step_status()`（`rejected` 最高、永不被覆盖）
- ❌ 禁止用"刷新时间戳"掩盖陈旧（`calculated_at` 刷成当天 ≠ 数据是新的）
- ✅ 证据必须带**真实读数**：写"批量写入 N 只/M 行"而不是"批量写入"；报告里的关键数字必须能追到日志/evidence
- ✅ **报告/STATUS/KB 里的硬数字只能"引用"不能"手抄"**：能复算的数字（行数、期数、只数、覆盖率、
  阈值）必须写进 `docs/evidence/` 的证据 JSON 并由脚本生成，正文只允许引用其路径。
  做不到复算的，写"约/量级"并标注口径，**不得给出精确到个位的数**。
  （`reports/133` §7 的升级条款已在 24 小时内被触发 ≥4 次：`432,343`→`432,523` 曾同时错在
  STATUS + KB + reports/139 三处；`79 期`→实为 86、`5,666 只`→实为 5,934 亦同型。
  这是四份调查一致指向的**单一最大返工源**，且是唯一"没有任何执行器"的那一类。）
- ✅ **"改了多少行"必须修前按同一谓词数一遍 + 修后终检**，不采信驱动返回的 rowcount
  （DuckDB 对 `UPDATE` 返回 −1；`reports/138` §3.4 里同一个坑踩了三次）
- ✅ 新增探针/门禁时，必须同时写"它会失败"的反向用例（`reports/125` 的守卫空转 24 小时的教训）
- 🔒 门禁：RG2、`tests/regression/test_rework_guards.py` §2（检查器必须抓得到）

### R3 派生列三件套：保全 + 重建 + 校验（缺一不可）

- ✅ 任何"不由指标计算器 staging 产出、但住在 `indicator_snapshot` 里"的列，必须在
  `app/core/indicators/calculator.py::derived_snapshot_domains()` 登记（列名 + 重建函数）
- ✅ 两条发布路径（`_publish_snapshot` / `_publish_snapshot_delta`）都走 `_derived_columns_guard()`：
  删除前按 `(stock_code, report_date)` 保全 → 重建 → 校验没有列从"有值"掉到"全空"
- ❌ 禁止只给某一条发布路径打补丁（`reports/131 §9` 只补了整表路径，增量路径当天仍在清空）
- ❌ 禁止用"名单式补丁"（`*_pending` 集合）代替重建触发；名单只能做兜底
- 🔒 门禁：RG4、`tests/regression/test_snapshot_derived_columns.py`

### R4 每一次除法都要过四问（比率类指标的最高风险在分母）

1. 分母到底是什么（精确字段路径，不是中文术语直译）？
2. 它会不会趋零或变负？≤ 0 时**只标 `status`**，不截断、不设上限、不置空；
3. 量级对得上外部标杆吗（ROCE 16%–52%、PEG<1、F-Score 0–9、Z-Score 1.8/3.0）？
4. 空值是"数学不成立"还是"数据没抓到"（写进 `indicator_null_reason`）？

- ❌ 禁止用护栏（`if abs(x) > 1: None`）掩盖看不懂的数字 —— 48 只真实 ROE > 100% 曾被静默置空
- ✅ 新复合指标一律走 `docs/skills/composite-indicator/SKILL.md` 的 10 步清单
- 📎 依据：`reports/131` §10（8 个坑）、`reports/132`（分母口径四次返工）

### R5 "今天"永远是北京时间

- ✅ 只用 `app.core.financial_period.cn_today()` / `cn_now()`；跨日比较必须先 `astimezone(CN_TZ)`
- ❌ 禁止 `date.today()`、`datetime.now(UTC).date()` 参与"距今几天 / 是否到期 / 同一交易日"判断
- 🔒 门禁：RG1；依据 `reports/117` §7、`reports/118` §3（两次 UTC/CN 边界事故）

### R6 不得一票否决：逐股隔离 + 阈值硬阻断

- ✅ 单只股票的数据问题只能隔离该股票（`max(50, 1%)` 或占比 ≥1/3 才整批拒绝），并在响应里如实披露 `quarantined_codes`
- ✅ 熔断桶必须是 `(adapter, data_type)`，只计"源不可用"；`legal_empty` / `per_symbol_data_error` 不增不减
- ✅ 空适配器链是**环境问题**（`source_unavailable`），不是"该票没数据"
- ❌ 禁止把"某只股票缺失"升级成"全站不可用"（`reports/117` 的全市场快照被拒）

### R7 口径变更必须走 Skill 清单，且必须在报告里写"为什么不选另一个口径"

- ✅ 报告必须有一节"外部核验 + 反例"（书里的标杆、手算 1 家、行业常识）
- ✅ 口径被推翻时，新报告 `supersedes` 旧报告并把旧报告 front-matter 标 `superseded`
- ✅ 结论必须附依据路径 + `last-reviewed` 日期（见文档规则 §4）
- 📎 依据：`reports/128 → 129 → 130 → 132`（同一指标 40 小时内四次返工，全是口径没定死）

## 不要删清单（reports/136 §8，强制）

**用法**：要删任何"看起来多余"的结构前，先在这张表里查一遍，并先做 W7 的契约测试 grep（含 `docs/runbooks/`）。
判据是 **"能指出它防的是哪次事故，就不算过度设计"**——下表每项都对应一次已发生的事故，永久保留。

| 项 | 看起来可删的理由 | 删了会重演的事故 |
|---|---|---|
| `storage/path_policy.py` + `scripts/s1-pytest.ps1` + 证据捕获 | "本地工具，路径校验太重" | 2026-07-22 测试写坏正式库（`incident-2026-07-22`）——这是唯一的结构性防线 |
| `storage/update_lock.py` / `exclusive_update` | "单人工具，不会并发" | 自动更新子进程 + CLI + Web 三方并发写库 → DuckDB 损坏 |
| `adapters/base.py::ErrorKind` 七分类 | "错误就是错误，分类这么细干嘛" | `reports/117` 熔断放大：5 只股票让 3 个源同时熔断、假 `retry_exhausted`、永久 missing |
| `job_status.merge_step_status()` | "合并一个 dict 而已" | `reports/117` P0-2：success 覆盖 rejected → 14 天对外假绿 |
| 门禁阈值 `max(50,1%)` / 1/3 占比 | "为什么不能全放行或全拦" | 全拦 = 26 只新股拖死全市场发布；全放行 = 系统性单位错乱无人拦（`reports/117`/`118`） |
| `field_units.py` + 前端 `screening-format.ts` 双份 | "前端不用管单位" | `reports/80` 簇 A/B/C：显示与筛选口径分叉 → 100 倍误差 |
| `DerivedSnapshotDomain` 注册表 + `_derived_columns_guard` | "又一个概念税" | `reports/133`：ROCE 六列被快照重发清空、静默变 NULL（增量路径仍有坑） |
| `cold_archive` 17 表 Parquet 视图 | "为什么不放回主库" | 19GB 主库继续膨胀、重建变慢（`reports/109`/`123`） |
| `cli/protocol.py` 的 plan→confirm 机制 | "单人工具要什么审批令牌" | `backup restore` / 冷库归档等**不可逆**操作一条命令就能执行 |
| `dividend_yield` 废弃列 + `DEPRECATED_SCALE=100` shim | "直接删列更干净" | `reports/124`：旧筛选草稿静默变成"全市场命中"（差 100 倍） |
| `scripts/legacy_dangerous/` | "隔离目录用不上了" | `test_maintenance_scripts_safety.py` 把它当**固定 fixture**（存在性 + 内容双重断言），删了 2 条门禁直接红（`reports/137` §3.1） |
| `app/web/static/index.html`（保留跟踪） | "构建产物不该进 git" | 它让 **fresh clone 立刻可服务 SPA 根路径**；`.gitignore` 有意只忽略 `assets/`。wheel 测试要求的是"构建后产物"，不能拿它当"必须跟踪"的唯一理由（`reports/137` §3.2） |
| `scripts/cold_archive_split.py`（1,333 行） | "单次提交、命名像一次性迁移脚本" | 9 个测试 `_load_module()` 直接加载它，`cold_archive_guard.py` 的恢复指引也指向它；删了 **19GB 主库的冷库 17 视图无法重建**，且 pytest 报 `FileNotFoundError` 而非可读失败（`reports/140` §4.2） |
| `app/resources/config/default.yaml` + `sync_packaged_config.py` | "两份配置明显重复，删一份" | 它是 wheel/安装版的**打包兜底**（`config.py:84` 用 `importlib.resources` 读）；删了源码跑正常、**发布即事故**（曾漂移到 93 行、缺 51 个 `update.*` 键、把 DuckDB 上限砍到 2GB，`reports/135`）。**正解是改打包映射同一份文件，不是删** —— 见 `reports/140` §5 `[F2]` |
| 国内源适配器的代理路由（`trust_env`） | "国内源就该直连" | 改成 `trust_env=False` 会真实改变路由，且失败**静默返回空结果**，离线测试覆盖不到；必须单独做真机探针验证（`reports/137` §3.3） |

## 工作区边界

- `data/` — 正式数据库（只读！所有写操作必须经 CLI/维护脚本 + 单写者串行）
- `.planning/` — 会话计划与进度（AI 私有工作区）
- `docs/evidence/` — 证据只增不改

## 规则维护（新增 / 合并 / 降级）

- **新增**：只允许追加到本文件对应小节，格式见 §0；一次会话最多新增 2 条（防规则通胀）。
- **合并**：新教训若与既有规则同源，改写既有规则的 bullet，**不新增编号**
  （例：`reports/138` 的"不采信 rowcount"、`reports/140` 的"硬数字只能引用不能手抄"都折叠进 R2）。
- **降级**：两个复盘周期（约 2 个月）内既没再触发、也没有门禁引用的规则，正文压成一行索引
  「W<n>：一句话 → 见 reports/NNN」，细节移回对应 `docs/reports/NNN`。
- **门禁**：`scripts/check_rework_guards.py` 一条命令跑 RG1–RG9。**RG5** 查 AGENTS.md ≤300 行、
  W/R 编号唯一且每块含 `规则`+`为什么`、`docs/**` 下不得出现新的 lesson/规则 文档、被引用编号必须已定义。
