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

## 工作区边界

- `data/` — 正式数据库（只读！所有写操作必须经 CLI/维护脚本 + 单写者串行）
- `.planning/` — 会话计划与进度（AI 私有工作区）
- `docs/evidence/` — 证据只增不改
