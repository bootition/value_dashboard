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

## 常用命令

```bash
# 测试（S1 隔离回归，保护正式数据库）
scripts/s1-pytest.ps1 tests/regression
# 静态检查
uv run --locked ruff check app tests/regression
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

## 工作区边界

- `data/` — 正式数据库（只读！所有写操作必须经 CLI/维护脚本 + 单写者串行）
- `.planning/` — 会话计划与进度（AI 私有工作区）
- `docs/evidence/` — 证据只增不改
