# AGENTS.md — 项目智能体规则

本文件为 AI 助手（opencode/Codex 等）在本仓库工作的强制规则。

## 项目一句话

A股价值投资研究与筛选工具：FastAPI + DuckDB/SQLite + Vue3 前端，CLI 入口 `vd.bat <command>`。

## 文档规则（强制，防止"读到过期结论"）

### 1. 必读顺序（任何任务开始时）

1. `docs/STATUS.md` — **当前状态唯一权威**，含当前裁决、剩余缺口、进行中工作
2. `docs/README.md` — 文档地图与生命周期规则
3. 任务相关代码/文档

### 2. 文档状态语义（front-matter `status` 字段）

| 状态 | 含义 | 处理 |
|---|---|---|
| `approved` | 当前有效 | 可作建议依据 |
| `superseded` | 已被取代（`superseded-by` 指向新文档） | **禁止**引用为当前结论；仅追溯用 |
| `historical` | 历史决策 | 同上 |
| `archived` | 已归档 | 默认不读 |

### 3. 禁止行为

- ❌ 引用 `docs/reports/` 中 `status: superseded` 的结论作为当前状态（如 25/27/28 的 BLOCK 裁决已被 29 更新）
- ❌ 引用 `docs/archive/`、`docs/evidence/` 内容作为结论依据（证据 ≠ 结论）
- ❌ 把会话产物（findings/progress/task_plan）写入 `docs/` —— 一律放 `.planning/<date>-<topic>/`
- ❌ 修改 `docs/reports/` 历史报告正文（只可改 front-matter 状态字段）

### 4. 必须行为

- ✅ 给用户建议时标注依据文档路径 + `last-reviewed` 日期
- ✅ 新文档必须带 front-matter（status/category/last-reviewed）
- ✅ 结论/状态变化时：更新 `docs/STATUS.md` → 新报告写 `docs/reports/` → 旧文档 front-matter 标 `superseded` + `superseded-by`
- ✅ 机器证据（JSON/hash 等）只放 `docs/evidence/`
- ✅ 修订 PRD（`docs/decisions/01_PRODUCT_REQUIREMENTS_V1.md`）时同步更新其 `last-reviewed` 并记录变更到其修订章节

### 5. 隐性知识回写规则（强制，防止"知识只存在于对话里"）

**背景教训（2026-09-01 体检）：** 大量运行期硬约束只存在于报告/当时会话里（如 12GB 实验库"为什么不敢删"、DuckDB 14GB 上限、东财封禁），新会话必须翻几十份报告才能拼全；`.planning/` 里甚至躺着无人敢删的 12GB 数据库副本。现立规则：

- ✅ **对话中确认的任何固定事实/硬约束/保留期限/口径裁决，必须在本会话内写入文档**——优先登记到 `docs/runbooks/ops-knowledge-base.md`（运行期硬约束唯一常驻入口，含环境/数据源/DuckDB/口径/门禁/git 约束），同时更新其 `last-reviewed`
- ✅ 新约束涉及状态变化时，按 §4 流程更新 `docs/STATUS.md`
- ✅ 决策必须附着在决策物上：schema/代码级特殊设计（如 quarantine 隔离表、冷热分层）在代码注释或 knowledge-base 登记设计意图
- ❌ 禁止把"定了但没写"的结论只留在对话里；会话结束时对话中不应残留未落文档的固定结论
- ❌ 禁止删除/清理任何有保留期限的产物（如数据库重建回滚快照）而不在本文件/STATUS 登记依据

## 常用命令

```bash
# 测试（S1 隔离回归，保护正式数据库）
scripts/s1-pytest.ps1 tests/regression
# 静态检查
uv run --locked ruff check app tests/regression
# 反返工护栏（RG1–RG4：时区单源/状态合并/单位一致/派生域登记）
uv run --locked python scripts/check_rework_guards.py
# 前端门禁
cd frontend; npm run lint; npm run test; npm run build
# 锁文件
uv lock --locked
```

## Git 纪律（强制，防止工作丢失）

**背景教训（2026-07）：** 7-26 ~ 7-31 共 5 天的审计修复、数据重建脚本、60+ 测试全部未提交，仅因 AI 默认"不主动 commit"且项目无规则兜底。现立规则如下：

### 1. 提交时机

- ✅ **会话结束前**：若工作区有未提交变更，必须提交（这是默认动作，不再等待用户明确要求）
- ✅ **里程碑完成时**：每完成一个可独立验证的阶段立即提交
- ✅ 提交前先 `git status`，确认没有误入 `data/`、构建产物、证据目录等被 ignore 的文件
- ❌ 禁止跨主题打包提交；按主题拆分（docs/ feat/ fix/ chore/）

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
4. ✅ 删除实验产物：数据库副本（`.duckdb` 拷贝）、临时 CSV、调试脚本等（先确认无保留价值，有保留期限的登记后保留）
5. ✅ 清理 `.planning/<本会话>/` 中不再需要的中间文件（保留 findings/progress/task_plan 与最终交付物）

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

### R5 "今天"永远是北京时间

- ✅ 只用 `app.core.financial_period.cn_today()` / `cn_now()`；跨日比较必须先 `astimezone(CN_TZ)`
- ❌ 禁止 `date.today()`、`datetime.now(UTC).date()` 参与"距今几天 / 是否到期 / 同一交易日"判断
- 🔒 门禁：RG1

### R6 不得一票否决：逐股隔离 + 阈值硬阻断

- ✅ 单只股票的数据问题只能隔离该股票（`max(50, 1%)` 或占比 ≥1/3 才整批拒绝），并在响应里如实披露 `quarantined_codes`
- ✅ 熔断桶必须是 `(adapter, data_type)`，只计"源不可用"；`legal_empty` / `per_symbol_data_error` 不增不减
- ✅ 空适配器链是**环境问题**（`source_unavailable`），不是"该票没数据"
- ❌ 禁止把"某只股票缺失"升级成"全站不可用"（`reports/117` 的全市场快照被拒）

### R7 口径变更必须走 Skill 清单，且必须在报告里写"为什么不选另一个口径"

- ✅ 报告必须有一节"外部核验 + 反例"（书里的标杆、手算 1 家、行业常识）
- ✅ 口径被推翻时，新报告 `supersedes` 旧报告并把旧报告 front-matter 标 `superseded`
- ✅ 结论必须附依据路径 + `last-reviewed` 日期（见文档规则 §4）

## 工作区边界

- `data/` — 正式数据库（只读！所有写操作必须经 CLI/维护脚本 + 单写者串行）
- `.planning/` — 会话计划与进度（AI 私有工作区）
- `docs/evidence/` — 证据只增不改
