# Value Dashboard — A股价值投资研究与筛选工具

**Verdict: BLOCK** — 详见阻断状态章节。

个人本地研究工具，用于 A 股基本面分析、指标计算、DSL 筛选和投资决策支持。当前处理审计修复阶段，代码门禁已改进，但正式数据未重建。

---

## 架构

| 层 | 技术栈 |
|---|---|
| 后端 | Python >= 3.11, FastAPI, Typer |
| 分析型存储 | DuckDB — 财务报告、价格、分红、快照 |
| 操作型存储 | SQLite — 筛选结果、人工覆写、作业日志 |
| 前端 | Vue 3, TypeScript, Vite 8, Naive UI 2.44 |
| 数据适配器 | AKShare/Eastmoney, BaoStock, CNINFO, TDX |

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
| `docs/` | 审计、ADR、产品文档 |
| `_legacy/` | 归档式遗留文件（见[下文](#legacy)） |

---

## 前提条件

- **Python >= 3.11**（当前环境 3.14.2）
- **Node.js 24.13.0 / npm 11.6.2**（前端构建）

当前 Python 版本对第三方库（FastAPI/Starlette）发出 `asyncio.iscoroutinefunction` 废弃警告，但不阻断测试或运行。

Vue / TypeScript 语言服务器 (LSP) 未配置且已拒绝安装。前端门禁通过编译器 + 构建 + 浏览器 QA 实现。

---

## 安装

### 后端依赖

```bash
# pip 方式（推荐：为每个依赖包单独安装）
pip install fastapi uvicorn duckdb typer httpx pydantic lark cryptography pyarrow PyYAML pandas pypinyin

# 可选数据源适配器
pip install akshare easy-tdx baostock

# 开发依赖（测试、lint）
pip install pytest pytest-asyncio httpx ruff
```

> **注意：** `pyproject.toml` 使用 `build-backend = "setuptools.backends._legacy:_Backend"`，此遗留构建后端须先修复/验证后方可依赖 `pip install .` 或 `pip install -e .`。当前保守方案为上方逐依赖 `pip install`；项目以 `python -m` 方式直接运行，无需打包安装。也可选用 `uv` 等外部工具（需要兼容 setuptools>=68 的构建前端）。

### 前端

```bash
cd frontend
npm install
```

---

## 启动

### 一键启动（start.bat）

`start.bat` 是推荐启动方式，行为如下：

1. 检测 `dist/value-dashboard/value-dashboard.exe` 是否存在
2. 若存在，以**打包模式**运行该 exe
3. 若不存在，回退到**开发模式**：`python -m app.web.main`
4. 启动时自动执行数据库 schema 初始化与增量检查（`init_all_schema()` + `IncrementalUpdater().run_incremental_check()`）

**注意：** 启动路径会写入正式数据库（schema 初始化可幂等，增量检查只读），因此是正常的可写运行时路径，**不是**只读审计命令。

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

# 前端 Node 测试（数据质量、个股详情、筛选质量合约）
cd frontend && node --experimental-strip-types --test tests/data-quality.test.ts tests/stock-detail.test.ts tests/screening-quality.test.ts

# 前端生产构建
cd frontend && npm run build
```

```powershell
# 只读文件哈希；不打开数据库引擎
Get-FileHash -LiteralPath `
  "data\valuedashboard.duckdb", `
  "data\valuedashboard.sqlite" -Algorithm SHA256
```

**冻结说明：** 当前禁止运行 Python、pytest、应用、CLI 和任何 DuckDB/SQLite
命令。2026-07-21/22 的两次验证运行已改变正式数据库文件；在测试路径与正式库
完成强隔离并重新取得批准前，`pytest`（包括 `--collect-only`）不再列为安全命令。

---

## 阻断状态（BLOCK）

总体审计结论：**BLOCK**。

- 代码门禁已有实质改进：DUCKDB 快照使用 staging + 事务发布、测试采集隔离、适配器限流配置接线、旧 schema 只读兼容。
- 但**正式数据未重建**：财务壳行、占位分红、合成 lineage、raw/QFQ 分裂、元数据缺失、操作测试工件仍存在。
- Cycle 2 期间正式 DuckDB/SQLite 已偏离批准基线；未经明确批准没有使用备份覆盖现库。正式库继续冻结，不能把当前文件视为已验收数据。
- UI 展示已实现：DataStatusPage 展示结构化警告码与各数据日期；StockDetail 展示六个 freshness 字段和四个分红指标"数据未验证"标签；筛选面板对相关字段不可信时门禁保存/导出，纯操作警告不阻断。以上 UI 展示不修复底层数据，正式数据重建仍待执行。
- G22（用户可见性）与 G23（外部真值 30 股抽样）**未通过**。
- 详细分析见以下报告：

| 文档 | 内容 |
|---|---|
| [docs/11_RED_TEAM_AUDIT_V2.md](docs/11_RED_TEAM_AUDIT_V2.md) | 原始红队审计 14 项问题 |
| [docs/12_AUDIT_REMEDIATION_REPORT.md](docs/12_AUDIT_REMEDIATION_REPORT.md) | Code Fix 阶段整改报告 |
| [docs/13_CURRENT_BLOCKERS_INVESTIGATION.md](docs/13_CURRENT_BLOCKERS_INVESTIGATION.md) | 当前阻塞项深度调查（含解除门禁） |

---

## 正式数据库哈希与事故状态

批准基线来自修复前备份；当前正式文件已经偏离。2026-07-22 最后一次只读
`Get-FileHash` 结果如下：

| 文件 | 批准基线/备份 SHA-256 | 当前正式文件 SHA-256 | 状态 |
|---|---|---|---|
| `data/valuedashboard.duckdb` | `46EBCEB6DDBCCA15D4E82D22CFA659EC1C593033DE190C08B5C54FC7211A3C91` | `5186E660E603B277B72E4EAF9988963C64B25B83882BA5ACF4BE2789A51268D6` | `DRIFTED / FROZEN` |
| `data/valuedashboard.sqlite` | `228E0F53A8EBD0B99DAED8FA2D683D42F46644E8805495350EC162EFEC6596D3` | `B7B5F2FF2D1B4D2F71512DFEBD8DC2FBD9625E51BE2D0BFDB5CAF0657EC11959` | `DRIFTED / FROZEN` |

**注意：** 没有 `data/.hashes` 锁文件。哈希记录在 `docs/13_CURRENT_BLOCKERS_INVESTIGATION.md` 和 `findings.md` 中。

备份位于 `data/backup/audit_pre_fix_20260720.duckdb` / `.sqlite`，当前复核仍与批准基线一致。恢复会覆盖现库，未经用户明确批准不得执行。

事故边界：

- 第一次变异已追溯到显式执行 `python -m pytest tests/ -q --no-header`；该命令绕过 `testpaths`，导入了带模块级数据库副作用的遗留验收脚本。事故后哈希一度为 DuckDB `98DF496F...4CC2B1A`、SQLite `B7B5F2FF...7EC11959`。
- 第二次 DuckDB 变化发生在 worker 执行 `python -m pytest tests/regression -q` 的 12.98 秒窗口内；文件 `LastWriteTimeUtc=2026-07-22 01:41:26`，命令后哈希变为当前 `5186E660...A51268D6`。当时未做逐进程文件写入追踪，具体测试/调用链尚未证明。
- 早先提出的“mmap 延迟写回且时间戳不变”解释已撤回；它与实际变更时间戳矛盾。

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
- **Node 合约测试：** 2026-07-22 新鲜运行 `46/46` 通过。
- **编译/构建：** `npx vue-tsc --noEmit` 和 `npm run build` 均成功。
- **浏览器功能 QA：** 完全 mock API 的 19 个场景在 375/768/1280 三种宽度全部通过；覆盖 DataStatus、StockDetail、Screening、嵌套规则、loading/404/500、保存/导出/自选 payload 和阻断状态零持久化 POST。证据见 `frontend/test-results/final-frontend-qa/`。
- **清理证明：** QA 命令正常退出，端口 6176 和 Vite/runner 进程均无残留。
- **视觉证据限制：** 当前可用独立 reviewer 均为文本模型，无法读取 PNG 像素；57 张截图存在且运行时无水平溢出，但 CJK 像素级换行、裁剪和视觉层级尚未获得 vision-capable reviewer 或人工签署。因此不把浏览器功能 PASS 外推为完整视觉 PASS。
- **可访问性修复：** `index.html` 已改为 `lang="zh-CN"`；DataStatus 失败状态已使用语义化 `NAlert type="error"`。

---

## 数据声明

本项目使用 DuckDB 作为分析型存储、SQLite 作为操作型状态存储。财务数据、价格数据、分红事件和元数据的数据源/适配器包括 AKShare/Eastmoney、BaoStock、CNINFO、TDX 所列公开来源及历史 CSMAR 商业导入。重复使用与许可条件取决于各来源条款。本工具仅用于**个人本地研究**，不构成投资建议。

数据准确性和完整性依赖于上游数据源及当前审计修复阶段。详见 `docs/` 中各报告。
