# Value Dashboard — A股价值投资研究与筛选工具

**Verdict: 见 [docs/STATUS.md](docs/STATUS.md)** — 当前状态唯一权威。最新结论（2026-08-03 第八轮独立正式启用复审）：**PASS / 可正式启用**。F4 对抗复现已关闭，网页/CLI 导出同源；S1 408、ruff、uv lock、前端 52+10/build、PyInstaller 真实启动 smoke 均通过，正式库未变化。依据见 [docs/reports/40_SYSTEM_RED_TEAM_FORMAL_ENABLEMENT_2026-08-03.md](docs/reports/40_SYSTEM_RED_TEAM_FORMAL_ENABLEMENT_2026-08-03.md)。

CLI 使用 `vd.bat <command>`。仓库根目录下 `vd.bat` 走开发入口（`python -m app.cli.main`）并显式建立 formal profile；发行目录中与 `value-dashboard.exe` 同目录时使用打包入口。直接 `python -m app.cli.main` 在缺少 profile 环境变量时会拒绝数据库操作。

个人本地研究工具，用于 A 股基本面分析、指标计算、DSL 筛选和投资决策支持。正式数据已重建（2026-07-31，`docs/reports/29`）。

---

## 5 分钟上手（首次使用）

完整操作指南见 [docs/runbooks/user-first-use.md](docs/runbooks/user-first-use.md)。速览：

1. 双击 `start.bat` 启动，浏览器打开 `http://127.0.0.1:8765`。
2. 先看「**数据状态**」页：数据是否就绪、价格/财报截至哪天。
3. 「**筛选**」页：输入规则名称 →「保存新版本」→ 添加条件（如 `pe_ttm < 15`）→「运行筛选」。
4. 「**保存结果**」→「导出 CSV」或「加入自选」。
5. 匹配超过 5000 条时会「结果已截断」：缩小条件或确认 CSV 中的 `_truncated` 标注。
6. 个股详情页可研究：K 线、业务概览、历史股本与研究统计（PE/PB/股息率/利差的分位与 z-score）、
   国债利差比较（默认 10 年期，可切换期限）。
7. 日常维护用 `vd` CLI（`vd data auto-update status`、`vd data treasury-curve --check-only`、`vd backup`）。

---

## 架构

| 层 | 技术栈 |
|---|---|
| 后端 | Python >= 3.11, FastAPI, Typer |
| 分析型存储 | DuckDB — 财务报告、价格、分红、快照 |
| 操作型存储 | SQLite — 筛选结果、人工覆写、作业日志 |
| 前端 | Vue 3, TypeScript, Vite 8, Naive UI 2.44 |
| 数据适配器 | AKShare/Eastmoney, BaoStock, CNINFO, TDX, 财政部国债收益率（czb_mof） |

## 目录结构

| 路径 | 职责 |
|---|---|
| `app/core/` | 核心逻辑：适配器、存储、DSL、指标、筛选、PDF |
| `app/web/` | FastAPI 服务器、API 路由、前端静态资源 |
| `app/cli/` | Typer CLI 入口 |
| `config/` | YAML 配置（默认 + 用户覆盖） |
| `data/` | 正式数据库 (`valuedashboard.duckdb`, `valuedashboard.sqlite`) |
| `frontend/` | Vue 3 + TypeScript + Vite 前端项目 |
| `tests/regression/` | 回归测试与采集安全门禁（最近一次 worker 运行报告 61 项） |
| `tests/conftest.py` | 测试配置与 fixture |
| `tests/collection_probe/` | 测试采集探针 |
| `docs/` | 文档中心：STATUS / decisions / reports / runbooks / contracts / evidence / archive（地图见 [docs/README.md](docs/README.md)） |
| `_legacy/` | 归档式遗留文件（见[下文](#legacy)） |

---

## 前提条件

- **Python >= 3.11**（当前环境 3.14.2）
- **Node.js >= 20.19**（O5：Vite 8 与 `--experimental-strip-types` 测试要求；`frontend/package.json` 已声明 `engines.node >=20.19.0`，当前环境 24.13.0）

当前 Python 版本对第三方库（FastAPI/Starlette）发出 `asyncio.iscoroutinefunction` 废弃警告，但不阻断测试或运行。

Vue / TypeScript 语言服务器 (LSP) 未配置且已拒绝安装。前端门禁通过编译器 + 构建 + 浏览器 QA 实现。

---

## 安装

### 后端依赖

```bash
# uv 方式（推荐，锁文件已在仓库内；含数据源适配器 akshare/baostock/easy-tdx，
# 必须带 --all-extras 或 --extra data-sources，否则 .venv 缺适配器、自动更新
# 依赖 akshare 的步骤会失败，见 docs/reports/77 N1）
uv sync --locked --all-extras

# pip 方式（为每个依赖包单独安装）
pip install fastapi uvicorn duckdb typer httpx pydantic lark cryptography pyarrow PyYAML pandas pypinyin

# 可选数据源适配器
pip install akshare easy-tdx baostock

# 开发依赖（测试、lint）
pip install pytest pytest-asyncio httpx ruff
```

### 前端

```bash
cd frontend
npm install
```

---

## 启动

### 一键启动（start.bat）

`start.bat` 是推荐启动方式（发行版）与开发版通用入口：

1. 若与 `start.bat` 同目录存在 `value-dashboard.exe`（发行布局），以**打包模式**运行该 exe。
2. 否则回退到**开发模式**：`python -m app.web.main`（仓库根目录开发路径）。
3. 启动时自动执行数据库 schema 初始化与后台自动更新（`init_all_schema()` + 自动更新控制器）。

**注意：** 启动路径会写入正式数据库（schema 初始化可幂等），因此是正常的可写运行时路径，**不是**只读审计命令。

### CLI 启动

```bash
# 入口别名（若已安装）
vd

# 直接运行
python -m app.cli.main

# 仅启动 Web 服务器
python -m app.cli.main server
```

### 开发模式

```bash
python -m app.web.main
```

服务器默认监听 `http://127.0.0.1:8765`，自动打开浏览器。配置见 `config/default.yaml`（可被 `config/user.yaml` 覆盖）。

### 前端开发

```bash
cd frontend
npm run dev    # Vite 开发服务器
npm run build  # 类型检查 + 生产构建
```

---

## CLI

入口 `vd` 或 `python -m app.cli.main`，支持以下顶层命令：

| 命令 | 用途 |
|---|---|
| `server` | 启动 Web 服务器 |
| `init` | 初始化数据库 schema |
| `status` | 查询数据库状态 |
| `data` | 数据管理（init / update / backfill 等子命令） |
| `indicator` | 指标计算（支持 DSL） |
| `discover` | 探查字段、指标、适配器、来源、模式 |
| `screening` | 筛选条件管理 |
| `override` | 人工覆写管理 |
| `plan` | 风险操作确认 |
| `backup` | 数据库备份 |
| `archive` | 归档管理 |

CLI 命令根据子命令不同，可读取或写入正式数据库。详见 `--help`。

---

## 当前允许的安全验证命令

```bash
# 前端类型检查
cd frontend && npx vue-tsc --noEmit

# 前端 Node 测试（数据质量、个股详情、筛选质量、指标可信合约）
cd frontend && npm run test

# 前端生产构建
cd frontend && npm run build
```

```powershell
# 只读文件哈希；不打开数据库引擎
Get-FileHash -LiteralPath `
  "data\valuedashboard.duckdb", `
  "data\valuedashboard.sqlite" -Algorithm SHA256
```

**冻结说明（历史）：** 2026-07-21/22 的两次验证运行曾改变正式数据库文件，此后建立了 S1 路径隔离（`scripts/s1-pytest.ps1`）并在 2026-07-31 完成全市场数据重建。正式库写操作必须经 CLI/维护脚本且单写者串行，详见 [docs/runbooks/s0-evidence-preservation.md](docs/runbooks/s0-evidence-preservation.md) 与 [docs/STATUS.md](docs/STATUS.md)。

---

## 当前状态

**项目状态唯一权威见 [docs/STATUS.md](docs/STATUS.md)**。要点（2026-08-11）：

- 代码层门禁通过（S1 隔离回归、Ruff、前端 lint/55 Node + 40 组件测试/build）。
- 数据层 ready=TRUE、warning_codes=[]（正式库，详见 STATUS）。
- P1-P4 功能已实施：个股研究工作台、业务概览、国债曲线与利差、历史股本链与历史研究统计；
  2026-08-11 系统红队审查（`docs/reports/73`）发现的 P1/P2/P3 问题已全部修复并通过回归。
- 历史审计结论（BLOCK 等）已被 `docs/reports/29`–`40` 与 `docs/STATUS.md` 取代，**请勿再引用为当前结论**。

历史审计链（仅追溯用，均 superseded）：

| 文档 | 内容 |
|---|---|
| [docs/reports/11_RED_TEAM_AUDIT_V2.md](docs/reports/11_RED_TEAM_AUDIT_V2.md) | 原始红队审计 14 项问题 |
| [docs/reports/12_AUDIT_REMEDIATION_REPORT.md](docs/reports/12_AUDIT_REMEDIATION_REPORT.md) | Code Fix 阶段整改报告 |
| [docs/reports/13_CURRENT_BLOCKERS_INVESTIGATION.md](docs/reports/13_CURRENT_BLOCKERS_INVESTIGATION.md) | 当前阻塞项深度调查（含解除门禁） |

---

## 正式数据库状态

2026-07-31 全市场数据重建完成（`docs/reports/29`）：股本、价格、分红/公司行动、
财务 lineage 与快照全部重建，`ready=TRUE`、`warning_codes=[]`；2026-08-02
只读复验一致（证据：`docs/evidence/evidence-formal-*20260802.json`）。

历史（2026-07-21/22 两次验证运行曾改变正式库文件）已通过 S1 路径隔离
（`scripts/s1-pytest.ps1`）与单写者串行纪律修复；事故基线详见
`docs/reports/13`。正式库写操作必须经 CLI/维护脚本且单写者串行，见
`docs/runbooks/s0-evidence-preservation.md` 与 `docs/STATUS.md`。

---

## _legacy 归档

[_legacy/README.md](_legacy/README.md) 记录了 2026-07-22 非破坏性目录整理：

- **71 个文件 / 6,756,894,870 字节** 从活动目录移入 `_legacy/`
- 分类：遗留测试 (26)、独立脚本 (1)、第三方源数据 (42)、UAT 归档 (2)
- 目录清理当时的回归基线在清理前后保持 **48 项**不变；后续新增采集安全门禁后，worker 最近一次报告为 61 项，但该 Python 运行不构成正式库不变证明
- 保护排除：`data/`、`tests/regression/`、`tests/conftest.py`、`tests/collection_probe/`、规划文件、规范、agent 目录
- 零删除、零数据库访问、零网络调用、零包变更

---

## 前端 QA

Vue / TypeScript 语言服务器 (LSP) 在当前工作区未配置且已拒绝安装。前端质量验证通过以下途径完成：

- **编译器门禁：** `vue-tsc -b`（构建时类型检查）
- **构建门禁：** `vite build`（生产构建）
- **Node 合约测试：** `npm run test` = 52 项纯 Node 合约 + 10 项 vitest 组件流程（筛选运行/保存/导出/加入自选、数据状态、自选信任遮蔽、个股不可信告警、写令牌拦截器）全部通过。
- **可访问性修复：** `index.html` 已改为 `lang="zh-CN"`；DataStatus 失败状态已使用语义化 `NAlert type="error"`。

---

## 数据声明

本项目使用 DuckDB 作为分析型存储、SQLite 作为操作型状态存储。财务数据、价格数据、分红事件和元数据的数据源/适配器包括 AKShare/Eastmoney、BaoStock、CNINFO、TDX 所列公开来源及历史 CSMAR 商业导入。重复使用与许可条件取决于各来源条款。本工具仅用于**个人本地研究**，不构成投资建议。

数据准确性和完整性依赖于上游数据源及当前审计修复阶段。当前状态与剩余缺口见 [docs/STATUS.md](docs/STATUS.md)。
