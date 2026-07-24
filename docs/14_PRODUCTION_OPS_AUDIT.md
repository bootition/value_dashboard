# Value Dashboard — 生产运维审计报告

**日期:** 2026-07-22  
**审查类型:** 只读生产运维、文档、流程/溯源全维度审计  
**Verdict:** **BLOCK** — 不可用于生产部署或分发给任何用户（包括个人本地生产使用）  
**项目声明用途:** 个人本地研究工具，非多用户/可再分发产品  

---

## 0. 审计范围与方法

本报告基于对以下内容的只读审查：
- 全部 13 份文档（`docs/01` 至 `docs/13`）
- `.opencode/plans/` 计划文件
- `.omo/` 研究证据与回填脚本
- `task_plan.md`、`findings.md`、`progress.md`
- `pyproject.toml`、`start.bat`、`value-dashboard.spec`
- `config/default.yaml`、`config/host_spec.yaml`
- `app/web/main.py`（Web 入口点）
- `README.md`、`_legacy/README.md`
- Git 状态、远端正交与提交历史
- `data/` 目录结构与备份文件

**约束条件:** 未运行 Python、数据库引擎、pytest 或应用程序。所有发现来源于文件内容与 git 状态。

---

## 1. 项目声明与目标用户分析

| 维度 | 事实 | 评估 |
|---|---|---|
| 声明用途 | 个人本地 A 股价值研究工具 | 清晰且克制 |
| 目标用户 | 单人，Windows 中文桌面，CLI 可接受 | 明确 |
| 是否可再分发 | 否 — 许可证、数据源条款、凭据机制都未为多人设计 | 正确 |
| 当前状态 | `BLOCK` — 正式数据未重建，数据库已漂移/冻结 | README 诚实声明 |
| 失败模式 | 代码门禁已改进，但正式数据包含壳行、占位分红、合成 lineage | 明确 |

**评估:** 项目对自身定位诚实。问题不在于声明，而在于当前状态离"个人可用"还有显著差距（数据重建、迁移、用户可见性）。

---

## 2. 启动/安装可行性

### 2.1 后端依赖安装

| 文件 | 发现 | 严重度 |
|---|---|---|
| `pyproject.toml` | `build-backend = "setuptools.backends._legacy:_Backend"` — **自 2024 年起已被 setuptools 弃用**。`pip install -e .` 或 `pip install .` 在 setuptools >= 72 下会失败 | **P0 阻断** |
| README §安装 | 承认上述问题，建议逐依赖 `pip install` 替代 | 诚实变通 |
| `pyproject.toml` 依赖清单 | 列出 19 个核心依赖 + 3 个可选 + 4 个开发依赖 | 完整 |
| Windows 依赖 | `pywin32` 仅在 `windows` extra 中，未在启动路径中自动安装 | **P2 缺口** |
| Python 版本 | 声明 >= 3.11，当前环境 3.14.2。FastAPI/Starlette 在 3.14 下有弃用警告 | P3（不阻断） |

### 2.2 前端依赖安装

| 检查项 | 发现 |
|---|---|
| `frontend/package.json` | 存在 |
| 依赖完整性 | 策略上要求 `npm install` |
| Node.js 版本 | 声明 v24.13.0 / npm 11.6.2 |
| TypeScript LSP | 未配置且已拒绝安装 — 门禁通过 `vue-tsc` + `vite build` 替代 |

### 2.3 启动方式

| 路径 | 存在 | 工作预期 |
|---|---|---|
| `start.bat` | ✅ | 检测 `dist/value-dashboard/value-dashboard.exe`，若存在则以打包模式运行；否则回退到 `python -m app.web.main`。创建 `data/logs/` 并做 8765 端口检查 |
| `app/web/main.py` | ✅ | FastAPI 应用工厂 + `run_server()` 入口 |
| CLI 入口 `app/cli/main.py` | ✅ | Typer 应用 |
| Python `-m` 方式 | ✅ | `pyproject.toml` 定义了 `scripts.vd` 指向 `app.cli.main:app` |

**关键发现:** `start.bat` 的打包模式需要 `dist/value-dashboard/value-dashboard.exe` 存在。`dist/` 目录**当前不存在** — PyInstaller 从未执行过。

---

## 3. 配置与环境审计

### 3.1 配置文件

| 文件 | 内容 | 评估 |
|---|---|---|
| `config/default.yaml` | 服务器、数据库路径、适配器限流、筛选默认值、备份策略、日志配置 | 结构完整 |
| `config/user.yaml` | 不存在 | P3 — 文档说明可通过此文件覆盖默认配置 |
| `config/host_spec.yaml` | 记录目标主机 CPU/RAM/磁盘 | 符合 PRD §19.1 PF1 |
| `config/field_mapping/` | 目录存在但内容未核查 | — |

### 3.2 关键发现

1. **数据库路径是写死的相对路径:** `data/valuedashboard.duckdb` 和 `data/valuedashboard.sqlite`。在打包场景下（Exe 可能在不同 CWD 启动），路径解析可能不一致。**P1**
2. **适配器限流配置接线已修复**（DQ-04 修复后确认）
3. **配置缺少环境区分:** 无 dev/staging/production 配置层。对于个人本地工具可接受，但如果需要克隆或演示则是缺口。**P3**
4. **无密钥/凭据外部化:** 数据源适配器可能硬编码凭据（未审计全部适配器源码）。**P2**

---

## 4. 打包与部署

### 4.1 PyInstaller 配置

| 检查项 | 发现 |
|---|---|
| `value-dashboard.spec` 存在 | ✅ 88 行完整配置 |
| 打包形态 | `--onedir` 模式（正确选择 — 一次解压，秒启） |
| 入口点 | `app/web/main.py` |
| 前端静态资源 | 包含 `frontend/dist` → `app/web/static` 的 datas 收集 |
| 隐式导入 | 列出 13 个 `hiddenimports`（正确涵盖了 duckdb/uvicorn 组件、可选适配器等） |
| 排除项 | `tkinter`、`matplotlib`、`PIL`、`IPython`、`jupyter`（合理） |

### 4.2 实际打包状态

| 检查项 | 状态 |
|---|---|
| `dist/` 目录 | ❌ **不存在**。PyInstaller 从未执行过 |
| `dist/value-dashboard/value-dashboard.exe` | ❌ 不存在 |
| `app/web/static/` | 前端构建产物可能不在正确位置 |

### 4.3 部署评估

**结论: 打包完全未完成，不可部署。P0。**

---

## 5. CI/CD

| 检查项 | 发现 |
|---|---|
| `.github/` 或 CI 配置文件 | ❌ **完全不存在** |
| 质量门禁 | 仅通过 README 声明（`vue-tsc --noEmit`、`npm run build`、Node 合约测试、`Get-FileHash`） |
| 自动化测试运行 | 无 — 所有测试手动触发 |
| 预提交钩子 | 无 |
| 依赖更新 | 无 Dependabot / Renovate |

**评估:** 完全没有任何基础设施。对于个人本地工具可以容忍，但如果目标是"可以重新部署"的场景，这是一个根本性缺口。**P1（取决于目标）**

---

## 6. 监控、日志与健康检查

### 6.1 健康检查

| 端点 | 存在 | 覆盖范围 |
|---|---|---|
| `GET /api/health` | ✅ | 返回 `status: ok`, `version: 0.1.0`, `config_loaded: true` |
| `GET /api/db/status` | ✅ | 返回 DuckDB 和 SQLite 的连接状态、路径、表列表 |

### 6.2 日志

| 检查项 | 发现 |
|---|---|
| `config/default.yaml` 日志配置 | `level: INFO`, `format: '%(asctime)s %(levelname)s %(name)s: %(message)s'` |
| `app/web/main.py` 日志 | 使用 Python `logging`，配置为 INFO 级别 |
| `start.bat` 日志重定向 | 错误输出追加到 `data/logs/start.log` |
| `data/logs/` 目录 | 存在大量 `.err.log` / `.out.log` 文件（19 个回填+数据初始化相关日志文件） |
| 日志轮转 | ❌ **无任何日志轮转或大小限制配置**。长期运行会产生无限增长的日志文件。**P2** |
| 结构化日志 | ❌ 无 JSON 日志，无请求 ID 追踪。原始 `logging` 模块。**P3** |

### 6.3 监控

| 检查项 | 发现 |
|---|---|
| 指标暴露 | ❌ 无 Prometheus 端点、无 `/metrics`、无结构化应用指标 |
| 错误追踪 | ❌ 无 Sentry、无集中式错误报告 |
| 性能监控 | ❌ 无请求耗时中间件、无慢查询追踪 |
| 启动记录 | ✅ 服务器启动时记录日志行 |

**评估:** 健康检查存在但极简。日志存在但无轮转/保留策略。监控完全缺失。**P2（长时间运行后磁盘填满风险）**

---

## 7. 备份、恢复与回滚

### 7.1 备份基础设施

| 检查项 | 状态 |
|---|---|
| `data/backup/` 目录 | ✅ 存在 |
| 审计前备份（2026-07-20） | ✅ `audit_pre_fix_20260720.duckdb` (1.98 GB) + `.sqlite` (436 KB)，哈希与批准基线一致 |
| 其他备份 | `full_20260717_223725.zip` (612 KB) — 似乎是早期 SQLite 备份 |
| CLI 备份命令 | `vd backup create` / `vd backup restore` / `vd backup list` — 文档声明 |
| 备份加密 | 文档声明 AES-256-GCM 个性化数据加密 |
| 保留策略 | 声明保留最近 3 套全量备份 |

### 7.2 备份与实际数据的关系

| 时间线 | 事件 |
|---|---|
| 2026-07-20 | 审计前备份制作，哈希 DuckDB `46EB...C91`、SQLite `228E...9D3` |
| 2026-07-20→21 | Code Fix Phase 0 + Cycle 1 — 后端代码修复，声称哈希不变 |
| 2026-07-22 Cycle 2 | **第一次事故:** `python -m pytest tests/ -q --no-header` 绕过 testpaths，模块级副作用的遗留验收脚本改变了数据库。DuckDB 变 `98DF...4CC2` |
| 2026-07-22 Cycle 2 | **第二次事故:** worker 的 `python -m pytest tests/regression -q` 窗口内 DuckDB 变 `5186...A5126`，SQLite 变 `B7B5...11959` |
| 当前 | 两个正式库文件均 DRIFTED / FROZEN。备份哈希与批准基线一致但**未恢复** |

### 7.3 恢复与回滚

| 检查项 | 状态 |
|---|---|
| 回滚计划 | ❌ 无记录的回滚文档 |
| 恢复证明 | ❌ 从未在真实场景中端到端验证 |
| 正式库回滚 | ❌ 未执行 — 需要用户明确批准（正确做法） |

**评估:** 备份存在但已与当前库脱节。恢复程序从未被验证。**P0（没有可信的恢复路径）**

---

## 8. 版本号与发布管理

| 检查项 | 发现 |
|---|---|
| `pyproject.toml 版本` | `0.1.0` |
| `app/web/main.py` 版本 | HTTP API 声明 version `"0.1.0"` |
| CLI JSON `schema_version` | `"1.0"`（声明于文档） |
| `CHANGELOG.md` | ❌ 不存在 |
| Git tag | ❌ 无 tag — 无提交、无标签 |
| 版本管理策略 | ❌ 无文档化的语义版本策略 |

**评估:** 版本号存在但从未被 git tag 追踪，无 changelog。**P2**

---

## 9. 许可与数据源约束

| 项目 | 发现 |
|---|---|
| 项目许可证 | ❌ 无许可证文件 |
| 数据源 | AKShare/Eastmoney、BaoStock、CNINFO、TDX + 历史 CSMAR 商业导入 |
| CSMAR 导入数据 | 5.7 GB 原始数据在 `_legacy/raw_source_data/` — 第三方商业数据，许可条款未知 |
| 申万行业分类 | 缺少合法许可证 — 代码中当前处理为缺失（返回 null + 原因码） |
| 再分发 | README 明确声明禁止数据再分发、二次售卖或对外提供数据服务 |

**评估:** 无许可证声明是显著的遗漏，尤其是包含第三方商业数据（CSMAR）时。**P1**

---

## 10. 文档矛盾分析

| 文档对 | 矛盾 | 严重度 |
|---|---|---|
| `README.md` 冻结说明 vs `docs/06_DELIVERY_PLAN.md` 执行计划 | README 说所有 Python 命令冻结；DELIVERY_PLAN 仍在建议执行 `pyinstaller` 等命令 | **P0** — 计划未更新到当前冻结状态 |
| `docs/07_PROGRESS_REVIEW.md` §打包 vs `docs/09_PROGRESS_REVIEW_V3.md` §打包 | 两者都说 "PyInstaller 从未执行" 但分别属于不同日期（07-17 vs 07-17 第三轮），未在同一点收敛 | P3 — 冗余 |
| `docs/05_MILESTONE_REVIEWS.md` 说所有里程碑通过（37-40/40） vs `docs/10+11+12+13` 说 BLOCK | 里程碑审查评分针对代码结构而非数据质量。数据审计是后加层面。**关键区别:** 里程碑审查未声称数据通过验收 | **说明性而非矛盾** — 里程碑审查范围限定为代码结构 |
| `findings.md` §修复前基线 vs `README.md` §哈希表 | 原始基线哈希一致。README 记录了当前 DRIFTED 哈希。无矛盾 | 一致 |
| `docs/03_TECH_PLAN_V1.md` 说 M9 完成备份 vs 实际备份从未经端到端验证 | 计划假设备份已完成且经验收；实际未验证 | **P1** |

---

## 11. Git / 溯源审计

### 11.1 Git 状态

| 属性 | 值 |
|---|---|
| 当前分支 | `fix/audit-remediation` |
| 提交数 | **0** |
| 文件状态 | 全部 untracked |
| 远程 | `origin → https://github.com/bootition/value_dashboard.git` |
| 远程分支 | 空仓库 |
| `.gitignore` | 存在，42 行，覆盖 `data/`、`__pycache__/`、`dist/`、`node_modules/`、`*.log` 等 |

### 11.2 关键发现

1. **零提交 — 完全无版本历史。** 无法追溯到入、回滚或审计变更。**P0**
2. **所有文件均未追踪。** 如果磁盘损坏或意外删除，没有恢复途径。**P0**
3. **远程仓库为空。** 没有离站备份。**P0**
4. **`.gitignore` 排除 `data/`** — 这是安全的（数据库文件不应提交），但也意味着数据库**完全没有版本控制**。
5. **无 git tag、无分支策略、无 PR 工作流。**

### 11.3 溯源困境

由于无版本历史：
- 无法回答"这个 bug 是什么时候引入的"
- 无法回滚单个文件的变更
- 无法通过 `git bisect` 定位回归
- 审计报告中的问题无法链接到引入它们的变更

**评估: Git 基础设施处于"新建项目"水平，代码库有约 200+ 源文件。这是 P0 级的生产风险。**

---

## 12. 已完成工作追踪

### 12.1 已完成的 Cycle/Phase

| 阶段 | 完成的内容 | 证据 |
|---|---|---|
| 产品需求 (PRD) | `docs/01_PRODUCT_REQUIREMENTS_V1.md` — 803 行冻结需求 | 文档存在 |
| 技术规划 + 审查 | `docs/03_TECH_PLAN_V1.md` + `docs/04_TECH_PLAN_REVIEW_V1.md` | 两轮迭代 |
| M0-M10 里程碑执行 | 全部 11 个里程碑声称通过 — 代码结构（筛选引擎、DSL、适配器、CLI、前端页面、备份、打包配置） | `docs/05_MILESTONE_REVIEWS.md` |
| 交付与测试计划 | `docs/06_DELIVERY_PLAN.md` — 7 阶段交付计划 | 计划存在但**从未执行** |
| 三轮进度审查 | `docs/07/08/09_PROGRESS_REVIEW.md` — 数据完整性问题持续发现 | V3 指出 92% 数据完整 |
| 红队审计 V1 | `docs/10_RED_TEAM_AUDIT.md` — 41 个问题（16 P0），BLOCK | 完全执行 |
| 红队审计 V2 | `docs/11_RED_TEAM_AUDIT_V2.md` — 14 项 DQ 问题，BLOCK | 完全执行 |
| Code Fix Phase 0 | DQ-01/02/04/08/09/11/13/14 代码修复 | `docs/12_AUDIT_REMEDIATION_REPORT.md` |
| 阻塞项深度调查 | `.omo/ulw-research/20260720-current-blockers/` — 26 观察 + 24 主张 | `docs/13_CURRENT_BLOCKERS_INVESTIGATION.md` |
| Cycle 2 修复 | 后端状态聚合、CLI 真相性、重试规范化、分红原子性、前端质量展示 | `.opencode/plans/2026-07-21-remediation-cycle-2.md` |
| 前端 QA | Node 合约 46/46，vue-tsc 通过，生产构建通过，浏览器 19/19 | progress.md |
| 目录治理 | 71 个文件移入 `_legacy/`，零删除 | `_legacy/README.md` |

### 12.2 未完成的阻塞项

| 项目 | 当前状态 |
|---|---|
| 正式数据重建 | ❌ 未执行 — DQ-03/05/06/07/10/11/12 数据问题 |
| 正式数据库迁移 | ❌ 未执行 — DuckDB QFQ `turnover_rate`、SQLite v2 |
| G22 用户可见性 | ❌ 未通过 — DataStatusPage、StockDetail、Screening、CLI 均未消费 quality 数据 |
| G23 外部真值 30 股抽样 | ❌ 未执行 — 需要人工签署 |
| PyInstaller 打包 | ❌ 从未执行 |
| 哈希锁文件 (`data/.hashes`) | ❌ 未创建 |
| 哈希保护测试 (`test_hash_preservation.py`) | ❌ 不存在 |
| 数据库清理 | ❌ 测试工件、未发布覆写、陈旧作业未清理 |

---

## 13. 启动检查清单

### 13.1 阻断项（BLOCKER — 必须修复）

| # | 问题 | 类别 | 优先级 |
|---|---|---|---|
| B-01 | **正式数据库已漂移/冻结 — 与批准备案哈希不匹配。** DuckDB `5186...A68` vs 基线 `46EB...C91`；SQLite `B7B5...959` vs 基线 `228E...9D3` | 数据完整性 | **P0** |
| B-02 | **零 Git 提交 — 无版本历史、无回滚能力、代码无源可溯。** 分支 `fix/audit-remediation` 零提交 | 溯源/运维 | **P0** |
| B-03 | **`pyproject.toml` 使用弃用的 `_legacy` 构建后端 — `pip install -e .` 在 setuptools >= 72 下失败** | 安装/环境 | **P0** |
| B-04 | **PyInstaller 打包从未执行 — 无部署产物** | 部署 | **P0** |
| B-05 | **正式数据未重建 — 壳行、占位分红、合成 lineage、raw/QFQ 分裂、元数据缺失** | 数据质量 | **P0** |
| B-06 | **G22 用户可见性未通过 — 质量警告未在 UI/CLI 消费；不可信值仍可用于筛选** | 功能 | **P0** |
| B-07 | **G23 外部真值 30 股抽样未执行 — 无验证数据准确性的基准** | 数据质量 | **P0** |
| B-08 | **正式数据库迁移未执行 — QFQ `turnover_rate` 缺失，SQLite 仍为 v1** | 数据管理 | **P0** |
| B-09 | **数据事故: Python 测试路径已在正式库上造成不可逆变更。当前所有 Python 命令冻结** | 安全/流程 | **P0** |

### 13.2 建议项（MUST FIX — 阻止生产准备）

| # | 问题 | 类别 | 优先级 |
|---|---|---|---|
| R-01 | **创建初始提交并推送到远程备份** | 溯源 | P1 |
| R-02 | **添加缺失的哈希锁文件 (`data/.hashes`) 和哈希保护测试** | 完整性 | P1 |
| R-03 | **记录正式的恢复/回滚计划并验证端到端** | 运维 | P1 |
| R-04 | **添加项目许可证文件（鉴于 CSMAR 商业数据和 AGPL/GPL 约束）** | 法律 | P1 |
| R-05 | **在发布或还原前完成数据库清理（测试记录、覆写、陈旧作业）** | 数据管理 | P1 |
| R-06 | **修复 `pyproject.toml` 构建后端（迁移到 `setuptools.setuptools` 或 `hatchling`）** | 安装 | P1 |
| R-07 | **建立日志轮转（Python `logging.handlers.RotatingFileHandler`）以防止磁盘填满** | 运维 | P2 |
| R-08 | **添加 CHANGELOG.md 并实施语义版本策略** | 文档 | P2 |
| R-09 | **添加环境感知配置（dev vs prod 路径、端口等）** | 配置 | P2 |
| R-10 | **解决 Python 3.14 下 FastAPI/Starlette 的弃用警告** | 兼容性 | P2 |
| R-11 | **更新 `.gitignore` 确保 `data/backup/` 和 `_legacy/uat_archives/` 不被意外提交** | 安全 | P2 |
| R-12 | **将数据源凭据外部化（通过环境变量或 Windows 凭据管理器）** | 安全 | P2 |

### 13.3 改进项（SHOULD FIX — 提高健壮性）

| # | 问题 | 类别 | 优先级 |
|---|---|---|---|
| I-01 | 添加请求耗时中间件和 Prometheus `/metrics` 端点 | 可观测性 | P3 |
| I-02 | 在所有 API 端点上添加结构化 JSON 日志 | 可观测性 | P3 |
| I-03 | 添加预提交钩子（`husky` + `lint-staged`） | 质量 | P3 |
| I-04 | 为 SQLite 和 DuckDB 添加数据库连接池/复用（当前 `open-per-query` 模式） | 性能 | P3 |
| I-05 | 实施 CI 管道的基础版本（GitHub Actions workflow） | 自动化 | P3 |
| I-06 | 实现增量备份（当前只有全量） | 备份 | P3 |

---

## 14. 总体结论

### 14.1 项目生命周期定位

```
研究/原型 ──► 开发/构建 ──► 测试/QA ──► 验收 ──► 部署 ──► 运维
                    │           
              当前在这里             
              (代码结构基本完成，
               但数据基础已确认不可信)
```

项目有大量的工程投入：11 个里程碑通过代码验收，完整的适配器层、筛选引擎、DSL、CLI、前端 4 页面、备份加密基础设施。**代码层面，投入是实质性的。**

### 14.2 决定性的问题

**数据信任已经断裂。** 这不是代码问题——而是审计后发现的壳行、占位分红、合成 lineage、raw/QFQ 分裂和测试工件的产物。项目准确地将自己列为 `BLOCK`。

两个独立的事故（2026-07-22 Cycle 2 验证）改变了正式数据库——这个问题在最警惕的时刻发生在已验证的代码上。它破坏了在任何"生产"场景中信任数据的任何可能性。

### 14.3 个人本地用途 vs 可再分发产品

| 维度 | 个人本地准备度 | 可再分发准备度 |
|---|---|---|
| 代码完整性 | ⚠️ 高（代码结构完整，有门禁） | ❌ 极低 |
| 数据可信度 | ❌ BLOCKED（壳行、占位、LINEAGE 无效） | ❌ 不可分发 |
| 安装简便性 | ⚠️ 手动 `pip install` 逐个依赖 | ❌ PyInstaller 从未执行 |
| 可恢复性 | ❌ 无 git 历史、无验证回滚 | ❌ 无 CI/CD、无发布流程 |
| 法律/许可 | ⚠️ 无许可证、CSMAR 数据边界未定义 | ❌ 禁止再分发 |
| **总体** | **不可用于实际投资研究** | **不可再分发** |

### 14.4 关键建议（按障碍排序）

1. **Git 初始提交并推送到远程。** 零提交状态是最大的运维风险。
2. **在副本上执行数据重建和迁移**（非破坏性——不覆盖当前正式库）。
3. **执行并完成 G23 30 股外部真值抽样**（人工签署）。
4. **完成 G22 跨所有消费面（UI、CLI、导出）的数据质量展示。**
5. **修复 `pyproject.toml` 构建后端**并验证 `pip install`。
6. **执行 PyInstaller 打包**并验证产物。
7. **建立恢复计划**并端到端验证。
8. **添加许可证**。
9. **为正式库创建哈希锁文件**以防止未来未经检测的变更。

### 14.5 最终裁决

```
Readiness:        🔴 BLOCK (不可使用)
Git Hygiene:      🔴 零提交，零版本历史
Data Trust:       🔴 壳行、占位、LINEAGE 无效、DRIFTED 哈希
Deploy Ability:   🔴 PyInstaller 从未执行
Legal:            ⚠️ 无许可证，CSMAR 边界未定义
Documentation:    ⚠️ 全面但部分与冻结状态矛盾
Monitoring:       🔴 无轮转、无指标、无追踪
Backup/Restore:   ⚠️ 备份存在但恢复路径未验证
```

**此工具当前不得用于实际投资决策，不得分发给任何用户，也不得部署到任何生产环境。** 代码层修复是实质性的，但在上述阻断项全部解除前，总体结论必须保持 **BLOCK**。
