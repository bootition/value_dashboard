---
title: S1 强测试/数据路径隔离与哈希门禁 — 实施方案
status: archived
category: archive
last-reviewed: 2026-07-26
---

# S1 强测试/数据路径隔离与哈希门禁 — 实施方案

> **当前轮次范围：** 本文档仅创建实施计划。不执行 Python、pytest、git add/commit/tag/copy、app、DB 或任何配置/代码/测试修改。所有命令标记为「未来执行 / RUN ONLY AFTER OWNER APPROVAL」。S1 能否开始由所有者决定。

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:subagent-driven-development 逐任务执行。步骤使用 `- [ ]` 跟踪。

**目标：** 消除零参数 `/ 环境 Config 回退的 Store 构造，实现 Python/pytest/CLI 与正式数据库文件的绝对路径隔离，创建初始事故源基线 `incident-2026-07-22` annotated tag，建立分层哈希账本，消除后端验证可改变正式库的根源风险（P0-05/P0-09 整改）。

**架构：**
- `DatabasePathSet` 纯数据类承载所有 DB/run 路径，由 env var 构造，**无缺省默认值**。缺失任一环境变量即构造失败。
- `path_policy.py` 零存储/DB 导入，安全用于 pytest 引导阶段。
- 两个 Store 构造函数仅接受 `DatabasePathSet` —— 无一参默认/回退到 Config。`Config.get_path()` 不再用于 DB 路径。
- 外层 PowerShell 包装器在 Python 前后捕获正式库动态状态（存在性/长度/SHA-256/sidecars），`try/finally` 确保 after 始终执行。任何 delta → exit 99、保留现场。
- 所有 Python 命令通过 `scripts/s1-pytest.ps1` 调用。第一次 Python 之前已存在纯 PowerShell 预检证明。

**技术栈：** PowerShell 7+, Python 3.11+, pytest, pathlib（纯模块零 duckdb/sqlite3 导入）

**遵循 docs/15_CURRENT_REVERIFICATION_AND_REMEDIATION_GUIDE.md 的阶段顺序：** A(批准) → B(基线) → C(PowerShell) → D(纯策略) → E(首次Python) → F(DI迁移) → G(门禁) → H(账本)

---

## 文件清单

| 路径 | 操作 | 职责 |
|---|---|---|
| `app/core/storage/path_policy.py` | 新建 | 纯路径策略：DatabasePathSet、Windows 路径规则、校验、数据根常量 |
| `tests/regression/test_path_isolation.py` | 新建 | path_policy 的纯 TDD 测试——零 DB/存储/业务导入，静态检查确保无违规 import |
| `tests/regression/test_hash_preservation.py` | 新建 | 哈希比较器逻辑测试——纯合成哨兵文件，绝不读取正式 DB 文件 |
| `scripts/s1-path-preflight.ps1` | 新建 | 纯 PowerShell 预检：env/process/run-root/formal-sidecar-state 检查 |
| `scripts/s1-pytest.ps1` | 新建 | PowerShell 包装器：`& <python> -m pytest @Args` + try/finally 前后哈希门禁 |
| `.gitignore` | 修改 | `data/*` + `!data/.hashes` 及其他排除规则 |
| `data/.hashes` | 新建 | 分层账本（S1 整改提交，非基线提交） |
| `conftest.py` | 修改 | 仅导入纯 path_policy/hash 工具；`pytest_configure` 验证 env/paths 并捕获前证据，`pytest_unconfigure` 捕获后证据；不导入 Config/Store |
| `tests/conftest.py` | 修改 | 断言内置 `tmp_path` 位于 wrapper 固定的 `VD_TEST_RUN_ROOT\pytest-tmp` 下；移除零参 Store fixture；改用 DatabasePathSet 构造 sibling DB 路径 |
| `app/core/storage/duckdb_store.py` | 修改 | `__init__` 仅接受 `DatabasePathSet`，验证后 mkdir/connect/PRAGMA/lock |
| `app/core/storage/sqlite_store.py` | 修改 | 同上 |
| `app/core/storage/schema.py` | 修改 | `init_all_schema` 接受两个显式 store 参数，无回退 |
| `app/core/config.py` | 修改 | 移除 `database.duckdb_path` / `sqlite_path` 读取路径能力；路径仅由 `path_policy` 管 |
| `app/core/init.py` | 修改 | 接受注入的 store，零参 `Store()` 不存在 |
| `app/core/backfill.py` | 修改 | 同上 |
| `app/core/update.py` | 修改 | 同上 |
| `app/core/indicators/calculator.py` | 修改 | 同上 |
| `app/core/dsl/engine.py` | 修改 | 同上 |
| `app/core/dsl/registry.py` | 修改 | 同上 |
| `app/core/dsl/validator.py` | 修改 | 同上 |
| `app/core/screening/engine.py` | 修改 | 同上 |
| `app/core/pdf/manager.py` | 修改 | 同上 |
| `app/core/pdf/correction.py` | 修改 | 同上 |
| `app/core/data_quality.py` | 修改 | 同上 |
| `app/core/backup/manager.py` | 修改 | 同上 |
| `app/cli/protocol.py` | 修改 | 同上 |
| `app/cli/main.py` | 修改 | 同上 |
| `app/web/main.py` | 修改 | 同上 |
| `app/web/api/data_status.py` | 修改 | 同上 |
| `app/web/api/screening.py` | 修改 | 同上 |
| `app/web/api/stock_detail.py` | 修改 | 同上 |
| `app/web/api/watchlist.py` | 修改 | 同上 |
| `start.bat` | 修改 | 拒绝缺失外部正式确认/路径；从不自动确认 |

---

## 阶段 A：所有者批准与冻结重验证

**前置条件：** S0 已完成并由所有者签署。`docs/evidence-s0/<run-id>/` 必须包含运行手册定义的 01-10 文件，尤其是自校验 manifest 和已签署退出检查表。外部法证副本仍仅在另行书面批准后才是必需项。**目的：** 在已签署 S0 边界上获得启动 S1 的书面批准，并确认冻结仍有效。

- [ ] **步骤 A-0：验证 S0 完成边界**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  读取而不修改 `docs/evidence-s0/<run-id>/09-manifest.txt` 和 `10-signoff-checklist.txt`，确认运行 ID 一致、01-10 文件齐全、操作员和所有者签署均存在。任一缺失则保持 `BLOCK / NO-GO`，不得继续 A-1。

- [ ] **步骤 A-1：确认无 Python 进程和端口占用**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  Get-Process -Name python* -ErrorAction SilentlyContinue | Format-Table Id, ProcessName, StartTime
  Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | Format-Table OwningProcess, State
  ```

  预期：无相关 Python/应用进程，端口 8765 空闲。任何残留都先记录 PID、命令行和端口归属并停止本阶段；只有所有者对该具体 PID 另行书面批准后才可优雅关闭，禁止通配符或按名称终止。

- [ ] **步骤 A-2：确认正式 DB 文件状态（只读元数据）**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  Get-ChildItem -LiteralPath "data" -Filter "valuedashboard.*" | Select-Object Name, Length, LastWriteTimeUtc
  Get-ChildItem -LiteralPath "data" -Filter "valuedashboard.duckdb.wal" -ErrorAction SilentlyContinue
  Get-ChildItem -LiteralPath "data" -Filter "valuedashboard.sqlite-wal" -ErrorAction SilentlyContinue
  Get-ChildItem -LiteralPath "data" -Filter "valuedashboard.sqlite-shm" -ErrorAction SilentlyContinue
  ```

  预期：仅有 `valuedashboard.duckdb` 和 `valuedashboard.sqlite`。无 `.wal` / `-wal` / `-shm` 伴生文件。记录当前长度和 LastWriteTime。

- [ ] **步骤 A-3：获取所有者书面批准启动 S1**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  展示：正式哈希（DuckDB `5186E660…68D6` / SQLite `B7B5F2FF…1959`）、零提交状态、本文档范围。获得明确批准后记录。

---

## 阶段 B：秘密审查 → 初始事故源基线 commit + annotated tag

**前置条件：** A 完成。**执行约束：** 此阶段不做任何源码修改或 .gitignore 更改。使用现有 .gitignore（已有 `data/` 排除规则）创建事故边界提交。所有 S1 整改（含 .gitignore 修改）在 tag 后的独立阶段执行。

- [ ] **步骤 B-1：秘密/第三方数据审查**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  审查以下路径是否含凭据/商业原始数据/不应追踪的内容：

  | 路径 | 审查要点 | 基线行为 |
  |---|---|---|
  | `data/` | 正式 DB/备份/缓存/日志/PDF/归档 | 排除（现有 `.gitignore` 已含 `data/`） |
  | `config/user.yaml` | 可能含凭据 | 逐文件审查；如有凭据则排除 |
  | `config/default.yaml` | 无敏感信息 | stage |
  | `config/host_spec.yaml` | 需审查 | 审查后 stage |
  | `config/field_mapping/` | 需审查 | 审查后 stage |
  | `config/sw_industry_fallback_result.json` | 派生数据 | 审查后 stage |
  | `dist/` | PyInstaller 产物 783 MiB | 排除 |
  | `frontend/dist/` | 构建产物 | 排除 |
  | `frontend/node_modules/` | 第三方依赖 | 排除 |
  | `frontend/.vite/` | Vite 缓存元数据 | 排除 |
  | `frontend/test-results/` | 测试截图/视频/产物 | 排除 |
  | `_legacy/raw_source_data/` | CSMAR 商业原始数据 | 排除（默认） |
  | `_legacy/uat_archives/` | 验收归档 | 排除（默认） |
  | `_legacy/` 其他文件 | 代码/文档需审查 | 默认排除；allowlist 审查后签名方可 stage |
  | `.omo/` | 脚本（含 DB 操作）| 默认排除；allowlist 审查后签名方可 stage |
  | `*.pem`, `*.key`, `.env*`, `uat_password_*/` | 凭据 | 排除 |
  | Commercial data (`*.dta`, 商业电子表格) | 原始数据 | 排除 |

- [ ] **步骤 B-2：确认 S0 证据包存在（不创建/修改）**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  # 断言 S0 证据目录存在（S0 完成的前提条件）
  if (-not (Test-Path -LiteralPath "docs/evidence-s0")) {
      throw "S0 evidence directory not found. S0 must be completed before S1 baseline."
  }
  $s0Runs = @(Get-ChildItem -LiteralPath "docs/evidence-s0" -Directory -ErrorAction Stop)
  if ($s0Runs.Count -eq 0) { throw "No S0 run directory found" }
  foreach ($s0Run in $s0Runs) {
      foreach ($required in @("09-manifest.txt", "10-signoff-checklist.txt")) {
          if (-not (Test-Path -LiteralPath (Join-Path $s0Run.FullName $required))) {
              throw "Incomplete S0 evidence package: $($s0Run.FullName) missing $required"
          }
      }
  }
  Write-Output "S0 evidence package exists and has manifest/signoff files — do not create or modify it in S1."

  # S1 基线证据写入独立的 S1 证据目录
  $ev = "docs/evidence-s1/baseline-commit"
  New-Item -ItemType Directory -Path $ev -Force | Out-Null
  ```

- [ ] **步骤 B-3：准备基线提交证据**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  # 完整状态
  git status --short --ignored | Out-File -Encoding utf8 "$ev/git-status.txt"

  # 关键排除项验证（使用现有 .gitignore）
  @("data/valuedashboard.duckdb","data/valuedashboard.sqlite","dist/value-dashboard/value-dashboard.exe","frontend/node_modules","_legacy/raw_source_data","frontend/.vite","frontend/test-results") | ForEach-Object {
    $r = git check-ignore -v $_ 2>&1
    "$_ : $r" | Out-File -Encoding utf8 -Append "$ev/git-check-ignore.txt"
  }

  # staged 证据（从后续 staging 命令后收集）
  ```

- [ ] **步骤 B-4：逐组审查并 stage 允许列表**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。**禁止 `git add .` 或 `git add -A`。**

  每个路径组审查后执行。`config/` 逐文件 stage（排除 `user.yaml` 如有凭据）；`_legacy`/`.omo` 默认不 stage。使用现有 `.gitignore`（含 `data/` 排除行，不添加 `!data/.hashes`——.hashes 尚不存在）。

  ```powershell
  # 核心源码
  git add -- "app/"
  git add -- "tests/"
  git add -- "conftest.py"
  git add -- "_pytest_policy.py"

  # 配置（逐文件审查）
  git add -- "config/default.yaml"
  git add -- "config/host_spec.yaml"
  git add -- "config/field_mapping/"
  git add -- "config/sw_industry_fallback_result.json"

  # 文档：S0 小型证据包是事故基线的一部分，按原字节 stage；仅排除新 S1 运行证据
  git add -- "docs/" ":(exclude)docs/evidence-s1/**"
  git add -- "README.md"
  git add -- "pyproject.toml"
  git add -- "start.bat"
  git add -- "value-dashboard.spec"

  # 前端源码 + 审查通过的配置
  git add -- "frontend/src/"
  git add -- "frontend/public/"
  git add -- "frontend/tests/"
  git add -- "frontend/package.json"
  git add -- "frontend/package-lock.json"
  git add -- "frontend/tsconfig.json"
  git add -- "frontend/tsconfig.app.json"
  git add -- "frontend/vite.config.ts"
  git add -- "frontend/index.html"

  # 当前已部署静态资源（这是事故现场证据的一部分）
  git add -- "app/web/static/"

  # 现有 .gitignore（不加修改）
  git add -- ".gitignore"
  ```

- [ ] **步骤 B-5：预提交检查**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  git diff --cached --name-only | Out-File -Encoding utf8 "$ev/staged-files.txt"
  git diff --cached --stat | Out-File -Encoding utf8 "$ev/staged-stat.txt"
  git diff --cached --check | Out-File -Encoding utf8 "$ev/staged-whitespace.txt"

  # 大小扫描
  git diff --cached --name-only | ForEach-Object {
    $f = Resolve-Path $_ -ErrorAction SilentlyContinue
    if ($f) { "$_ : $((Get-Item $f).Length) bytes" }
  } | Out-File -Encoding utf8 "$ev/staged-size.txt"

  # 扩展名扫描（不应出现 .exe/ .dta/ .db/ .sqlite 等）
  git diff --cached --name-only |
    ForEach-Object { [System.IO.Path]::GetExtension($_) } |
    Group-Object | Sort-Object Count -Descending |
    Out-File -Encoding utf8 "$ev/staged-extensions.txt"

  # 秘密扫描（staged diff 文本模式匹配）
  git diff --cached | Select-String -Pattern "(?i)(password|secret|api[_-]?key|token|BEGIN (RSA|EC|OPENSSH) PRIVATE)" |
    Out-File -Encoding utf8 "$ev/staged-secret-scan.txt"

  # 禁止路径前缀断言
  @("dist/", "frontend/dist/", "frontend/node_modules/", "frontend/.vite/",
    "frontend/test-results/", "_legacy/raw_source_data/", "_legacy/uat_archives/",
    ".omo/", "__pycache__/", ".pytest_cache/", "build/",
    "docs/evidence-s1/") | ForEach-Object {
    $found = git diff --cached --name-only | Where-Object { $_ -like "$_*" }
    if ($found) { Write-Host "ERROR: Forbidden prefix $_ staged: $found" -ForegroundColor Red }
  }

  # 正式 DB 哈希（验证 S0 冻结状态不变）
  Get-FileHash -LiteralPath "data/valuedashboard.duckdb" -Algorithm SHA256 |
    Out-File -Encoding utf8 "$ev/formal-duckdb-hash.txt"
  Get-FileHash -LiteralPath "data/valuedashboard.sqlite" -Algorithm SHA256 |
    Out-File -Encoding utf8 "$ev/formal-sqlite-hash.txt"

  # 只 stage 本次基线证据的精确文件；不得 sweep docs/evidence-s1/
  @(
    "git-status.txt", "git-check-ignore.txt", "staged-files.txt", "staged-stat.txt",
    "staged-whitespace.txt", "staged-size.txt", "staged-extensions.txt",
    "staged-secret-scan.txt", "formal-duckdb-hash.txt", "formal-sqlite-hash.txt"
  ) | ForEach-Object { git add -- (Join-Path $ev $_) }

  $unexpectedEvidence = git diff --cached --name-only | Where-Object {
    ($_ -like "docs/evidence-s1/*") -and ($_ -notlike "$ev/*")
  }
  if ($unexpectedEvidence) { throw "Unexpected evidence staged: $unexpectedEvidence" }
  ```

  审查每个证据文件，确认无 data/dist/node_modules/商业/UAT/凭据内容。`docs/evidence-s0/` 必须按签署后的原字节进入事故基线且不得由 S1 修改；`docs/evidence-s1/` 仅允许 `$ev` 中上列精确文件进入初始基线。

- [ ] **步骤 B-6：创建初始源基线 commit + annotated tag**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。**禁止 `--amend`/`reset`/`push`（push 需另行批准）。**

  ```powershell
  git commit -m "chore: initial source baseline — incident-2026-07-22 pre-remediation

  Zero-point commit for fix/audit-remediation branch.
  Captures source tree, config, docs, deployed static assets before
  any S1 path-isolation changes.

  Incident formal hashes:
    DuckDB 5186E660E603B277B72E4EAF9988963C64B25B83882BA5ACF4BE2789A51268D6
    SQLite B7B5F2FF2D1B4D2F71512DFEBD8DC2FBD9625E51BE2D0BFDB5CAF0657EC11959
  Rebuild-source:
    DuckDB 46EBCEB6DDBCCA15D4E82D22CFA659EC1C593033DE190C08B5C54FC7211A3C91
    SQLite 228E0F53A8EBD0B99DAED8FA2D683D42F46644E8805495350EC162EFEC6596D3
  Sidecars: absent at baseline.
  "

  git tag -a incident-2026-07-22 -m "incident-2026-07-22: formal DB drift event boundary

  Do NOT delete, move, amend, or rebase past this tag.
  Remote push requires separate owner approval.
  "
  ```

- [ ] **步骤 B-7：验证提交和 tag**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  git log --oneline -3
  git tag -l "incident-*" --format="%(refname) %(subject)"
  ```

  预期：1 个提交 + annotated tag `incident-2026-07-22`。`git status --short` 可能非空（剩余 `_legacy`/`.omo` 等未追踪），但无意外 staged 文件。`docs/evidence-s0/` 已按签署后的原字节纳入基线且未被修改。

---

## 阶段 C：纯 PowerShell 预检和包装器（首次 Python 之前）

**前置条件：** B 完成。**执行约束：** 此阶段全部 PowerShell。无 Python/pytest。

- [ ] **步骤 C-1：创建 `scripts/s1-path-preflight.ps1`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  **职责：** 在 Python 调用前验证隔离环境并捕获正式库动态状态。返回结构化的 PSObject 证据。

  **参数：**

  ```powershell
  param(
      [Parameter(Mandatory = $true)]
      [ValidateSet("Before", "After")]
      [string]$Phase,
      [string]$EvidenceDir = "docs/evidence-s1"
  )
  ```

  1. **进程检查：** `Get-Process -Name python* -ErrorAction SilentlyContinue` — 如存在则抛出 "Python process(es) running: <pids> — freeze S1"。
  2. **环境变量检查：** 必须为 `VD_ENV=test`；必须存在 `VD_DUCKDB_PATH`、`VD_SQLITE_PATH`、`VD_TEST_RUN_ROOT`；`VD_FORMAL_ACK` 必须不存在。任一不满足则抛出。
  3. **路径验证：** 三个路径必须为绝对路径且在仓库外；DuckDB/SQLite 必须是直接位于 `VD_TEST_RUN_ROOT` 下的 sibling 文件。运行根必须**当前不存在**；验证最近的已存在祖先，不创建任何路径。如发现过期残留，记录并退出，禁止自动删除后继续。
  4. **Python 可执行文件：** `Get-Command python -ErrorAction Stop | Select-Object -ExpandProperty Source` — 记录路径但不运行。
  5. **正式库状态捕获：** 逐一检查 `data/valuedashboard.duckdb`、`.sqlite`、`.duckdb.wal`、`.sqlite-wal`、`.sqlite-shm`。对每个文件记录：`exists`（bool）、`length`（int 或 null）、`sha256`（若存在，`Get-FileHash -Algorithm SHA256` 取 `.Hash` 大写）。sidecar 不存在时 exists=false, length=null, sha256=null。
  6. **输出：** `ConvertTo-Json -Depth 10` 到 stdout 和证据文件。退出码 0 为通过，非 0 为失败。

  **After 阶段精确行为：** 仅执行步骤 5（正式库状态捕获），不检查进程/环境/路径。捕获结果写入 `post/` 证据子目录。只有 5 个状态全部捕获成功才退出 0；任何读取/哈希失败都写 capture-failure 证据并非零退出。

  **`try/finally` 未在 preflight 本身使用。`finally` 在包装器（C-2）中。**

- [ ] **步骤 C-2：创建 `scripts/s1-pytest.ps1`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  **职责：** 外层包装器。支持 `-PolicyOnly` 模式（仅纯路径策略，不创建运行根）和 Normal 模式（含 DB/schema 测试，创建运行根）。`try`/`finally` 确保 After 捕获始终执行。

  ```powershell
  param(
      [switch]$PolicyOnly,
      [Parameter(ValueFromRemainingArguments = $true)]
      [string[]]$PytestArgs,
      [string]$EvidenceDir = "docs/evidence-s1"
  )
  ```

  **主逻辑（伪代码精确）：**

  ```powershell
  $ErrorActionPreference = "Stop"
  $runId = [Guid]::NewGuid().ToString("N")
  $runDir = Join-Path $EvidenceDir $runId
  $runRoot = Join-Path ([System.IO.Path]::GetTempPath()) "vd-s1-$runId"
  if (Test-Path -LiteralPath $runRoot) { throw "Generated run root unexpectedly exists: $runRoot" }
  if (Test-Path Env:VD_FORMAL_ACK) { throw "VD_FORMAL_ACK is forbidden in the test wrapper" }

  $env:VD_ENV = "test"
  $env:VD_TEST_RUN_ROOT = $runRoot
  $env:VD_DUCKDB_PATH = Join-Path $runRoot "valuedashboard.duckdb"
  $env:VD_SQLITE_PATH = Join-Path $runRoot "valuedashboard.sqlite"
  $env:VD_TEST_EVIDENCE_ROOT = $runDir
  New-Item -ItemType Directory -Path $runDir -Force | Out-Null

  if ($PolicyOnly) {
      if ($PytestArgs.Count -gt 0) { throw "PolicyOnly uses a fixed test target and accepts no PytestArgs" }
      $effectiveArgs = @("--noconftest", "-v", "--tb=short", "tests/regression/test_path_isolation.py")
  } else {
      if ($PytestArgs | Where-Object { $_ -like "--basetemp*" }) {
          throw "Caller-supplied --basetemp is forbidden; wrapper owns the test root"
      }
      $effectiveArgs = @("--basetemp", (Join-Path $runRoot "pytest-tmp")) + $PytestArgs
  }

  # Preflight Before — 验证环境，捕获前状态，确认运行根不存在
  $before = & "$PSScriptRoot/s1-path-preflight.ps1" -Phase Before -EvidenceDir $runDir
  if ($LASTEXITCODE -ne 0) { throw "Preflight Before failed" }
  $beforeObj = $before | ConvertFrom-Json

  # 仅在 Normal 模式下创建运行根（PolicyOnly 模式不创建）
  if (-not $PolicyOnly) {
      New-Item -ItemType Directory -Path $runRoot -ErrorAction Stop | Out-Null
      # 创建后立即重新检查 run root 及其已存在祖先无 reparse point
      Write-Output "Created run root: $runRoot"
  }

  $pythonExe = (Get-Command python -ErrorAction Stop).Source

  $pytestExit = 98
  $afterCaptured = $false
  $delta = $true
  try {
      & $pythonExe -m pytest @effectiveArgs
      $pytestExit = $LASTEXITCODE
  }
  finally {
      # Preflight After — 始终执行（即使 pytest 崩溃）
      $after = & "$PSScriptRoot/s1-path-preflight.ps1" -Phase After -EvidenceDir $runDir
      if ($LASTEXITCODE -eq 0) {
          $afterObj = $after | ConvertFrom-Json
          $delta = Compare-FormalState $beforeObj $afterObj $runDir
          $afterCaptured = $true
      }
  }

  # After 捕获失败和 formal delta 均优先于 pytest 退出码
  if (-not $afterCaptured) { exit 98 }
  if ($delta) { exit 99 }
  if (($pytestExit -eq 0) -and (-not $PolicyOnly)) {
      Remove-Item -LiteralPath $runRoot -Recurse -ErrorAction Stop
  }
  exit $pytestExit
  ```

  `Compare-FormalState` 辅助函数（内联或同文件）：返回 `$true`（有 delta）或 `$false`（无 delta）。

  - 对全部 5 个文件（`formal_duckdb`、`formal_sqlite` + 3 sidecars）比较 `exists`、`length`、`sha256`。
  - Sidecar 存在时的长度和 SHA256 必须一致；不存在时前后均为 false。
  - 任何 delta → 写入 `$runDir/delta-report.json`，`Write-Host "[FATAL]"` 到 stderr，返回 `$true`。
  - 无 delta → 写入 `$runDir/hash-evidence.json`，返回 `$false`。
  - 调用方：delta=$true → exit 99（优先于 pytest 退出码）；无 delta → exit 保留 pytest 退出码。
  - 全部通过（exit 0 + 无 delta）→ 删除运行根；任何失败 → 保留运行根。After 捕获失败 → 安全退出 98；formal delta → 99。

  **禁止 `Invoke-Expression`。禁止 `--timeout`（pytest-timeout 不是当前依赖）。** `-PolicyOnly` 固定加入 `--noconftest` 并固定测试文件，防止首次 Python 在 F 阶段前导入当前 `tests/conftest.py` 中的 Store 模块；该开关不得接受任意测试路径。F 完成后的 collect-only/定向/全回归均使用 Normal 模式并加载 conftest。

- [ ] **步骤 C-3：验证包装器（dry-run 模式——须通过，无 Python）**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  **正向测试（有效路径 + 无 Python）：** 设置有效临时路径（唯一，不提前存在），确认 preflight 成功通过。测试后仅清理本次创建的目录。

  ```powershell
  # 创建唯一的临时测试目录（preflight 要求 DB 父目录存在）
  $tmpBase = [System.IO.Path]::GetTempPath()
  $testDir = Join-Path $tmpBase "vd-s1-preflight-$(Get-Random)"
  New-Item -ItemType Directory -Path $testDir -Force | Out-Null

  $env:VD_ENV = "test"
  $env:VD_TEST_RUN_ROOT = "$testDir\run-root"
  $env:VD_DUCKDB_PATH = "$env:VD_TEST_RUN_ROOT\valuedashboard.duckdb"
  $env:VD_SQLITE_PATH = "$env:VD_TEST_RUN_ROOT\valuedashboard.sqlite"

  # 确认运行根不存在（DB 父目录存在）
  if (Test-Path $env:VD_TEST_RUN_ROOT) { throw "Run root already exists before test" }

  # preflight Before 应通过：父目录存在，运行根可创建且当前不存在
  & .\scripts\s1-path-preflight.ps1 -Phase Before
  # 预期退出码 0
  $beforePassed = ($LASTEXITCODE -eq 0)

  # 清理：仅删除本次创建的测试目录（使用 $testDir，不是预存在路径）
  if (-not $beforePassed) { throw "Expected positive preflight to pass" }
  Remove-Item -LiteralPath $testDir -Recurse -ErrorAction Stop
  ```

  **负向测试（不安全路径——应失败）：** 设置指向 `data/` 下的路径，确认 preflight 拒绝。

  ```powershell
  $env:VD_ENV = "test"
  $env:VD_DUCKDB_PATH = "$PWD\data\evil.duckdb"
  $env:VD_SQLITE_PATH = "$PWD\data\evil.sqlite"
  $env:VD_TEST_RUN_ROOT = "$PWD\data\runs"

  & .\scripts\s1-path-preflight.ps1 -Phase Before
  # 预期退出码非 0（路径在 data/ 下被拒绝）
  ```

- [ ] **步骤 C-4：提交阶段 C**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  git add -- "scripts/s1-path-preflight.ps1"
  git add -- "scripts/s1-pytest.ps1"
  git commit -m "feat(s1): PowerShell preflight and pytest wrapper with formal-state gate"
  ```

---

## 阶段 D：纯 path_policy 模块 + 纯测试（文本创建 + 静态审查）

**前置条件：** C 完成。**执行约束：** 此阶段仅创建文件为文本、审查 imports 静态确认无违规。不运行 Python。

- [ ] **步骤 D-1：创建 `app/core/storage/path_policy.py`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  **导入合约：** 仅标准库（`pathlib`, `os`, `dataclasses`, `datetime`, `enum`）。**禁止** `import duckdb`、`import sqlite3`、`from app.core.storage`、`from app.core.config`、`from app.core`。

  ```python
  """Pure path policy — zero DB/storage/business imports.

  Safe to import during pytest configuration phase before any
  app-layer initialisation.
  """

  from __future__ import annotations

  import os
  from dataclasses import dataclass
  from enum import Enum
  from pathlib import Path


  class VdEnv(str, Enum):
      FORMAL = "formal"
      TEST = "test"
      STAGING = "staging"


  class PathIsolationError(Exception):
      """Base exception for all path isolation violations — rules, env vars, domain boundaries."""


  @dataclass(frozen=True)
  class DatabasePathSet:
      """Immutable set of resolved, validated database paths.

      Constructed from a complete explicit field set or from_env().
      No defaults, no Config fallback, no zero-arg constructor.
      """

      env: VdEnv
      duckdb_path: Path
      sqlite_path: Path
      run_root: Path

      def __init__(
          self,
          *,
          env: VdEnv | None = None,
          duckdb_path: Path | None = None,
          sqlite_path: Path | None = None,
          run_root: Path | None = None,
      ) -> None:
          # env/duckdb_path/sqlite_path/run_root 任一为 None → PathIsolationError。
          # 使用 object.__setattr__ 仅在 frozen dataclass 初始化期间赋值；
          # 验证后任何字段重赋值均触发 FrozenInstanceError。
          # 原始 Path 在完成语法检查前不得 resolve。

      @classmethod
      def from_env(cls) -> DatabasePathSet:
          """Construct from environment variables. Fails closed on any missing.

          Reads VD_ENV, VD_DUCKDB_PATH and VD_SQLITE_PATH.
          For test it also requires VD_TEST_RUN_ROOT; for staging it requires VD_STAGING_ROOT;
          for formal it requires VD_FORMAL_ACK=confirmed and derives run_root from the two
          sibling DB paths only after proving their parent is the canonical formal data root.
          Missing/invalid input → PathIsolationError. Returns self.validate().
          """

      def validate(self) -> DatabasePathSet:
          """Validate paths, raise PathIsolationError on violation.

          执行顺序（关键：先验证语法，再规范化）：
          1. 检查 duckdb_path/sqlite_path 为绝对语法（不以 \ 开头、无设备前缀、无 ADS、无保留名）
          2. 检查不在 data/ 下（bare string 比较，不 resolve）
          3. 找到每条路径最近的已存在祖先；test PolicyOnly 不要求直接父目录存在
          4. 对所有已存在祖先执行 reparse point 检查
          5. 要求两个 DB 路径直接位于同一 run_root 下且为 sibling；不得只检查同卷
          6. test run_root 可安全地不存在（PolicyOnly）或已由 wrapper 创建（Normal）；
             “执行前必须不存在”的权威证明由 Preflight Before 提供
          注意：Path.resolve()/等效物理解析只在 validate() 内完成原始语法拒绝、
          最近已存在祖先定位和 reparse 检查之后调用；绝不在这些检查之前解析
          返回 self 以支持链式调用
          """

  # Repository roots (resolved at import)
  _PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent
  _DATA_ROOT: Path = _PROJECT_ROOT / "data"

  def project_root() -> Path:
      """Return the resolved project root (4 levels up from path_policy.py)."""

  def data_root() -> Path:
      """Return project_root() / 'data'."""

  # Path predicates（在 validate() 中被调用，执行语法检查，不 resolve）
  def is_subpath_of_data(path: Path) -> bool:
      """True if path is under _DATA_ROOT after casefold comparison."""
  def is_drive_relative(path: Path) -> bool:
      """True if path like 'C:foo' (drive-relative, not absolute)."""
  def is_device_path(path: Path) -> bool:
      """True if path starts with \\\\?\\ or \\\\.\\ (NT namespace)."""
  def has_alternate_data_stream(path: Path) -> bool:
      """True if path string contains ':' after drive letter."""
  def is_reserved_name(path: Path) -> bool:
      """True if any path component matches a Windows reserved name (CON, NUL, PRN, AUX, LPT[1-9], COM[1-9])."""
  ```

  `DatabasePathSet.__init__` 精确设计（注意：不调用 `Path.resolve()`——验证前禁止解析）：

  ```python
  def __init__(
      self,
      *,
      env: VdEnv | None = None,
      duckdb_path: Path | None = None,
      sqlite_path: Path | None = None,
      run_root: Path | None = None,
  ) -> None:
      # Direct construction requires all fields
      if env is None or duckdb_path is None or sqlite_path is None or run_root is None:
        raise PathIsolationError(
            "DatabasePathSet requires env, duckdb_path, sqlite_path, run_root. "
            "Use from_env() for env-var construction."
        )
      object.__setattr__(self, "env", env)
      # 不调用 .resolve()：路径尚未验证，可能含设备/UNC/保留名语法
      # resolve() 在 validate() 中由 canonicalize_path 统一执行
      object.__setattr__(self, "duckdb_path", duckdb_path)
      object.__setattr__(self, "sqlite_path", sqlite_path)
      object.__setattr__(self, "run_root", run_root)

  @classmethod
  def from_env(cls) -> DatabasePathSet:
      errs: list[str] = []
      env_raw = os.environ.get("VD_ENV")
      duck_raw = os.environ.get("VD_DUCKDB_PATH")
      sqlite_raw = os.environ.get("VD_SQLITE_PATH")
      test_root_raw = os.environ.get("VD_TEST_RUN_ROOT")
      staging_root_raw = os.environ.get("VD_STAGING_ROOT")

      if not env_raw: errs.append("VD_ENV")
      if not duck_raw: errs.append("VD_DUCKDB_PATH")
      if not sqlite_raw: errs.append("VD_SQLITE_PATH")

      # VD_TEST_RUN_ROOT is mandatory for test profile only
      # Use VdEnv.TEST.value for comparison (VdEnv may not parse yet)
      if env_raw == VdEnv.TEST.value and not test_root_raw:
          errs.append("VD_TEST_RUN_ROOT (required for test profile)")
      if env_raw == VdEnv.STAGING.value and not staging_root_raw:
          errs.append("VD_STAGING_ROOT (required for staging profile)")
      if env_raw == VdEnv.FORMAL.value and os.environ.get("VD_FORMAL_ACK") != "confirmed":
          errs.append("VD_FORMAL_ACK=confirmed (required for formal profile)")

      if errs:
          raise PathIsolationError(
              f"Missing environment variables: {', '.join(errs)}"
          )

      # VdEnv(env_raw) 在未知值时抛出 ValueError；统一转为 PathIsolationError
      try:
          env = VdEnv(env_raw)
      except ValueError:
          raise PathIsolationError(
              f"Unknown VD_ENV value: {env_raw!r}. "
              f"Legal values: {[e.value for e in VdEnv]}"
          )

      duck_path = Path(duck_raw)
      sqlite_path = Path(sqlite_raw)
      run_root = (
          Path(test_root_raw) if env is VdEnv.TEST else
          Path(staging_root_raw) if env is VdEnv.STAGING else
          duck_path.parent
      )
      return cls(
          env=env,
          duckdb_path=duck_path,
          sqlite_path=sqlite_path,
          run_root=run_root,
      ).validate()
  ```

  `validate()` 按 profile 处理域：test/staging 的 DB/run root 必须在仓库外且不得命中 formal、backup、rebuild-source、forensic 或生成目录；formal 的 DB 对必须精确等于 canonical formal 文件并有 ACK。对不存在路径寻找最近的已存在祖先并检查整条已存在祖先链，不要求 PolicyOnly 的直接父目录存在。两个 DB 文件必须直接位于同一 `run_root` 下且为 sibling。验证成功后以规范化 Path 返回新的 frozen `DatabasePathSet`，原对象不被就地改写。

- [ ] **步骤 D-2：创建 `tests/regression/test_path_isolation.py`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  **导入合约：** 仅 `pytest`、`dataclasses.FrozenInstanceError` 和 path policy 的显式符号 `DatabasePathSet`、`PathIsolationError`、`VdEnv`、`canonicalize_path` 及纯 predicate helpers。**静态检查：** 文件顶部声明此文件无 DB/Store/Config/业务 import；AST 守卫验证该导入白名单。

  **测试分类及用例：**

  ```
  test_path_isolation.py — 纯路径策略测试
  ├── TestDatabasePathSetFromEnv
  │   ├── test_constructs_from_env_vars (monkeypatch 设置所有四个 var, 断言成员值)
  │   ├── test_fails_on_missing_duckdb (不设 VD_DUCKDB_PATH → PathIsolationError)
  │   ├── test_fails_on_missing_sqlite
  │   ├── test_fails_on_missing_test_run_root (VD_ENV=test 下不设 VD_TEST_RUN_ROOT)
  │   ├── test_fails_on_unknown_vd_env (VD_ENV=production → PathIsolationError，非裸 ValueError)
  │   ├── test_fails_closed_no_default (清空所有 var → PathIsolationError)
  │   ├── test_environment_changes_do_not_mutate_existing_instance (修改 env var 后构造第二个, 第一个不变)
  │   └── test_fields_are_frozen_after_construction (字段重赋值 → FrozenInstanceError)
  │
  ├── TestValidateStorePath
  │   ├── test_rejects_relative_path
  │   ├── test_rejects_data_subpath (data/valuedashboard.duckdb → error)
  │   ├── test_rejects_data_subpath_nested (data/sub/dir/test.db)
  │   ├── test_accepts_valid_external_path (tmp_path / "test.duckdb")
  │   ├── test_accepts_nonexistent_run_root_with_safe_existing_ancestor (PolicyOnly 合法)
  │   ├── test_rejects_reparse_point_in_nearest_existing_ancestor
  │   └── test_does_not_create_directory (验证 validate 后目录仍不存在)
  │
  ├── TestWindowsPathRules
  │   ├── test_absolute_path_accepted (C:\valid\path\file.db)
  │   ├── test_canonical_case (当前模块文件的 resolve(strict=True).name == name)
  │   ├── test_drive_relative_detected (C:foo → is_drive_relative=True)
  │   ├── test_drive_relative_absolute_not_detected (C:\foo → False)
  │   ├── test_unc_path_rejected (\\server\share\file.db → PathIsolationError)
  │   ├── test_device_path_local (\\.\PhysicalDrive0)
  │   ├── test_device_path_win32 (\\?\D:\path\file.db)
  │   ├── test_alternate_data_stream (file.db:Zone.Identifier → has_ads=True)
  │   ├── test_ads_absent_normal (file.db → False)
  │   ├── test_reject_reserved_names (CON, NUL, PRN, AUX, LPT1, COM1 作为路径组件 → 拒绝)
  │   ├── test_non_existing_leaf_named_con (路径中某段为 "CON" → 拒绝，因为 Windows 保留名在任何位置都被禁止)
  │   ├── test_nearest_ancestor_exists (tmp_path/ExistingDir/new_child → ok)
  │   └── test_nearest_existing_ancestor_unreadable (无法读取祖先身份/属性 → fail-closed)
  │
  ├── TestBoundaryLinkTypes (跳过需要管理员权限的用例)
  │   ├── test_reparse_point_detected (需管理员创建 junction → pytest.skip)
  │   ├── test_symlink_detected (需管理员 → skip)
  │   └── test_hardlink_alias_rejected_with_synthetic_protected_file (仅在隔离临时目录创建合成受保护哨兵及其硬链接别名，通过 file ID 拒绝；绝不链接正式/备份/法证文件)
  │
  ├── TestSubpathDataPredicate
  │   ├── test_direct_child
  │   ├── test_nested_grandchild
  │   ├── test_data_itself
  │   ├── test_sibling_dir
  │   ├── test_unrelated_external
  │   └── test_denied_exact (明确不等于 data/ 但不是其子路径)
  │
  ├── TestSiblingRunRootPair
  │   ├── test_duckdb_sqlite_sibling_under_run_root (DuckDB 和 SQLite 文件必须是同 run_root 下的 sibling，而非仅同卷)
  │   ├── test_cross_volume_rejected (不同卷的 DuckDB/SQLite → PathIsolationError)
  │   └── test_non_sibling_rejected (DB 文件不在同一 run_root 目录级 → PathIsolationError)
  │
  └── TestTOCTOURecheck
      ├── test_validate_then_modify_does_not_cache (validate 后删除父目录, 检查不依赖缓存)
      └── test_concurrent_modification_safe (不假设路径在两次调用间不变)
  ```

- [ ] **步骤 D-3：静态审查 import 确认**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  # path_policy.py 不得导入 DB/Config/同包 Store 聚合入口
  $policyBad = @("duckdb", "sqlite3", "app.core.config", "app.core.storage.duckdb_store", "app.core.storage.sqlite_store")
  $content = Get-Content "app/core/storage/path_policy.py" -Raw
  foreach ($b in $policyBad) {
      if ($content -match "import\s+$b|from\s+$b") {
          Write-Host "ERROR: path_policy.py imports $b" -ForegroundColor Red
      }
  }

  # test_path_isolation.py 允许显式导入 app.core.storage.path_policy，禁止 Store/Config/DB
  $testBad = @("duckdb", "sqlite3", "app.core.config", "app.core.storage.duckdb_store", "app.core.storage.sqlite_store")
  $testContent = Get-Content "tests/regression/test_path_isolation.py" -Raw
  foreach ($b in $testBad) {
      if ($testContent -match "import\s+$b|from\s+$b") {
          Write-Host "ERROR: test_path_isolation.py imports $b" -ForegroundColor Red
      }
  }
  # _pytest_policy.py 必须保持纯 stdlib/pathlib；任何 app/storage/DB import 均拒绝
  $policyContent = Get-Content "_pytest_policy.py" -Raw
  foreach ($b in @("duckdb", "sqlite3", "app.")) {
      if ($policyContent -match "import\s+$b|from\s+$b") {
          Write-Host "ERROR: _pytest_policy.py imports $b" -ForegroundColor Red
      }
  }
  ```

- [ ] **步骤 D-4：提交阶段 D**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  git add -- "app/core/storage/path_policy.py"
  git add -- "tests/regression/test_path_isolation.py"
  git commit -m "feat(s1): pure path_policy module with DatabasePathSet and tests"
  ```

---

## 阶段 E：首次 Python 证明（仅 `test_path_isolation.py`，在包装器下）

**前置条件：** D 完成。**执行约束：** 这是全 S1 周期的首次 Python 调用。运行根必须不存在。包装器必须在 Python 之前和之后检查正式库状态。

- [ ] **步骤 E-1：建立唯一外部运行根（静态检查）**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  $runId = [Guid]::NewGuid().ToString().Substring(0, 8)
  $runRoot = "D:\tmp\vd-s1-path-policy-$runId"
  $env:VD_ENV = "test"
  $env:VD_DUCKDB_PATH = "$runRoot\valuedashboard.duckdb"
  $env:VD_SQLITE_PATH = "$runRoot\valuedashboard.sqlite"
  $env:VD_TEST_RUN_ROOT = "$runRoot"

  # 👇 静态确认（不调用 preflight——必须先验证手动）
  if (Test-Path $runRoot) { throw "Run root already exists: $runRoot" }
  if ($runRoot -match "\\value-dashboard\\data($|\\)") { throw "Run root under data/" }
  if ($runRoot -match "\\data($|\\)") { throw "Run root contains data/ segment" }
  ```

- [ ] **步骤 E-2：首次 Python 通过包装器运行纯路径测试（运行根尚不存在）**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  **这是全 S1 周期的首次 Python 调用。运行根尚不存在并于整个阶段始终不存在。** 使用 `-PolicyOnly` 标志确保包装器不创建运行根。preflight Before 验证最近的已存在祖先安全且运行根不存在。纯粹路径策略运行，不导入 DB 模块。

  ```powershell
  & .\scripts\s1-pytest.ps1 -PolicyOnly -EvidenceDir "docs/evidence-s1/path-isolation-run"
  ```

  **预期行为：**
  - Preflight Before：捕获正式库状态（存在, 长度, SHA-256），确认运行根不存在
  - pytest（PolicyOnly）：固定带 `--noconftest`，不加载当前 root/tests conftest，不导入 Store/Config；纯测试直接调用 path policy
  - 全部 PASS（跳过需要管理员权限的用例）
  - Preflight After（finally）：重新捕获 → 前后比较 → 无 delta
  - 退出码 0。证据存于 `docs/evidence-s1/path-isolation-run/<runId>/`。
  - **运行根从未被创建**（VD_TEST_RUN_ROOT 在整个阶段保持不存在）
  - 记录捕获的正式 DuckDB 和 SQLite SHA-256 保持不变。

  **若失败：** 退出码非 0（或 delta → 99）；包装器 After 捕获仍执行；证据保存；修复后重试。

- [ ] **步骤 E-3：证据确认**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  Get-ChildItem -Path "docs/evidence-s1/path-isolation-run" -Recurse -Filter "*.json" |
    ForEach-Object {
        $c = Get-Content $_.FullName -Raw | ConvertFrom-Json
        $b = $c.before
        $a = $c.after
        $ok = ($b.formal_duckdb.sha256 -eq $a.formal_duckdb.sha256) -and
              ($b.formal_sqlite.sha256 -eq $a.formal_sqlite.sha256)
        Write-Host "$($_.Name): hash match=$ok"
    }
  ```

- [ ] **步骤 E-4：提交阶段 E**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  # 只 stage E 阶段实际产生且已审查的精确 run 目录
  git add -- "docs/evidence-s1/path-isolation-run/<run-id>"
  git commit -m "feat(s1): first Python proof — pure path isolation tests pass under wrapper

  All tests/regression/test_path_isolation.py tests PASS.
  Formal DB state unchanged before/after all runs (verified by s1-pytest.ps1 gate).
  "
  ```

---

## 阶段 F：Config/Store/DI 迁移（TDD，零参 Store 淘汰）

**前置条件：** E 完成。**执行约束：** 每个文件组独立提交。`DuckDBStore`/`SQLiteStore` 构造器不再接受 `None` 或调用 `Config.current()`。

- [ ] **步骤 F-1：修改 `duckdb_store.py` 和 `sqlite_store.py`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  `__init__(self, *, paths: DatabasePathSet)` — 无一参构造，无位置参数。构造函数体：
  1. 调用 `paths.validate()`（或信任调用方已验证）。
  2. 存储 `self._path_set = paths`。
  3. `self._db_path = paths.duckdb_path`（或 `.sqlite_path`）。
  4. 验证后、副作用前检查：`self._db_path.parent.mkdir(parents=True, exist_ok=True)`。
  5. SQLite：再执行 `_init_wal()`；DuckDB：设置 `_lock_path` 等。
  6. 移除 `from app.core.config import Config` 和 `cfg.get_path()` 调用。

  `sqlite_store.py` 不再调用 `Config.current()`。`duckdb_store.py` 不再调用 `cfg.get_path("database", "duckdb_path")`。

- [ ] **步骤 F-2：修改 `schema.py`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```python
  def init_all_schema(
      duckdb_store: DuckDBStore | None = None,
      sqlite_store: SQLiteStore | None = None,
      *,
      paths: DatabasePathSet | None = None,
  ) -> None:
      """初始化所有 schema；不得在没有完整 stores 或已验证 paths 时运行。"""
      if paths is None and (duckdb_store is None or sqlite_store is None):
          raise PathIsolationError("init_all_schema requires both stores or validated paths")
      if paths is not None:
          duckdb_store = duckdb_store or DuckDBStore(paths=paths)
          sqlite_store = sqlite_store or SQLiteStore(paths=paths)
          # 提供的 store 必须与 paths 属于同一已验证域，否则 PathIsolationError
      assert duckdb_store is not None and sqlite_store is not None
      init_duckdb_schema(duckdb_store)
      init_sqlite_schema(sqlite_store)
  ```

  移除此函数内的 `DuckDBStore()` / `SQLiteStore()` 零参调用。所有调用方在 stage F 迁移。

- [ ] **步骤 F-3：修改 `app/core/config.py`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  从 `Config` 类和中删除 `database.duckdb_path` / `sqlite_path` 的读取。路径管理完全由 `path_policy` 负责。`Config` 保留：`server`、`adapters`、`screening`、`backup`、`logging` 键。

- [ ] **步骤 F-4~F-22：逐文件 DI 迁移**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  **只在进程组合根解析一次路径：** `app/web/main.py` 的 app factory 和 `app/cli/main.py` 的 AppContext factory 各自调用一次 `resolve_and_validate_paths()`，随后用同一实例加载 Config、构造两个 Store，并向下游注入。库类、路由和 protocol **不得**自行调用 `from_env()` 或重新读取环境变量。

  ```python
  from app.core.storage.path_policy import resolve_and_validate_paths
  from app.core.storage.duckdb_store import DuckDBStore
  from app.core.storage.sqlite_store import SQLiteStore

  # 仅存在于 Web/CLI composition root
  paths = resolve_and_validate_paths()
  config = Config.load(paths=paths)
  duck = DuckDBStore(paths=paths)
  sqlite = SQLiteStore(paths=paths)

  # 业务类接受 paths 和/或已构造 stores；两者均缺失时 PathIsolationError。
  # API 路由从 request.app.state 取得实例；protocol 从 AppContext 取得实例。
  ```

  迁移文件（每个独立提交或按提交分组合并）：

  | # | 文件 | 零参 Store 模式 |
  |---|---|---|
  | F-4 | `app/core/storage/schema.py` | `init_all_schema()` 隐式创建 Store |
  | F-5 | `app/core/init.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-6 | `app/core/backfill.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-7 | `app/core/update.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-8 | `app/core/indicators/calculator.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-9 | `app/core/dsl/engine.py` | `DuckDBStore()` |
  | F-10 | `app/core/dsl/registry.py` | `SQLiteStore()` |
  | F-11 | `app/core/dsl/validator.py` | `SQLiteStore()` |
  | F-12 | `app/core/screening/engine.py` | `DuckDBStore()` |
  | F-13 | `app/core/pdf/manager.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-14 | `app/core/pdf/correction.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-15 | `app/core/data_quality.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-16 | `app/core/backup/manager.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-17 | `app/cli/protocol.py` | `SQLiteStore()` |
  | F-18 | `app/cli/main.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-19 | `app/web/main.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-20 | `app/web/api/data_status.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-21 | `app/web/api/screening.py` | `SQLiteStore()` |
  | F-22 | `app/web/api/stock_detail.py` | `DuckDBStore()` |
  | F-23 | `app/web/api/watchlist.py` | `DuckDBStore()`, `SQLiteStore()` |
  | F-24 | `start.bat` | 添加以下变量的显式存在性检查，任一缺失则拒绝启动：`VD_FORMAL_ACK`（值必须为 `confirmed`）、`VD_ENV`（值必须为 `formal`）、`VD_DUCKDB_PATH`、`VD_SQLITE_PATH`。**不得自动设置 `VD_FORMAL_ACK` 或 `VD_ENV`。** |

- [ ] **步骤 F-25：修改 `tests/conftest.py`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  Normal wrapper 已拒绝调用者覆盖 `--basetemp`，并固定 `--basetemp <VD_TEST_RUN_ROOT>\pytest-tmp`。因此保留 pytest 内置 `tmp_path` fixture，只在 DB fixture 中断言其 canonical path 位于 canonical `VD_TEST_RUN_ROOT` 内，再用该目录下的 sibling DB 文件构造 `DatabasePathSet`：

  ```python
  @pytest.fixture
  def database_paths(tmp_path: Path) -> DatabasePathSet:
      run_root = Path(os.environ["VD_TEST_RUN_ROOT"])
      if not run_root.is_dir():
          raise RuntimeError("wrapper-created VD_TEST_RUN_ROOT is missing")
      canonical_run_root = canonicalize_path(run_root)
      canonical_tmp = canonicalize_path(tmp_path)
      if canonical_run_root not in canonical_tmp.parents:
          raise PathIsolationError(f"tmp_path escaped VD_TEST_RUN_ROOT: {canonical_tmp}")
      path_set = DatabasePathSet(
          env=VdEnv.TEST,
          duckdb_path=canonical_tmp / "test.duckdb",
          sqlite_path=canonical_tmp / "test.sqlite",
          run_root=canonical_tmp,
      ).validate()
      return path_set


  @pytest.fixture
  def duckdb_store(database_paths: DatabasePathSet) -> DuckDBStore:
      store = DuckDBStore(paths=database_paths)
      init_duckdb_schema(store)
      return store
  ```

- [ ] **步骤 F-26：修改 `tests/regression/test_collection_safety.py`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  **删除** 递归的 pytest 子进程哈希测试（该测试本身运行 Python 子进程，曾导致正式库漂移事故）。**保留/增强** 静态防御检查和 `testpaths` 证明：

  - `test_root_conftest_exists` ✓（保留）
  - `test_root_conftest_imports_policy` ✓（保留）
  - `test_root_conftest_contains_pytest_ignore_collect` ✓（保留）
  - 新增 `test_pyproject_testpaths_is_regression_only`：读取 `pyproject.toml` 验证 `testpaths` = `["tests/regression"]`
  - 新增 `test_no_legacy_tests_in_collection`：扫描文件系统确认 `tests/regression/` 不包含指向 `_legacy/` 的引用
  - **不** 保留子进程哈希验证测试（由外层包装器和 `test_hash_preservation.py` 覆盖）

- [ ] **步骤 F-27：创建 `tests/regression/test_hash_preservation.py`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  **设计规则（纯合成哨兵测试，不读取正式文件）：**
  - 使用 `tmp_path` 创建**隔离的合成哨兵文件**。绝不读取 `data/` 下的正式 DB 或 sidecar 文件。
  - 测试的是哈希比较器本身的正确性：捕获 → 模拟变化 → 检测差异。
  - 外层 PowerShell 包装器是正式的哈希门禁（比较真实正式文件的前后状态）。此 Python 测试仅验证比较逻辑在受控条件下工作。

  导入合约：仅 `pytest`、`hashlib`、`pathlib`。不导入 `DatabasePathSet`、`Config` 或任何业务模块。

  ```
  test_hash_preservation.py — 合成哨兵哈希比较器测试
  ├── TestSentinelCapture
  │   ├── test_capture_single_file (创建哨兵文件，计算 SHA-256，断言返回大写十六进制字符串)
  │   ├── test_capture_nonexistent_file (不存在的路径返回 None)
  │   └── test_capture_empty_file (空文件返回 SHA-256 = E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855)
  │
  ├── TestSentinelCompareUnchanged
  │   ├── test_identical_files_match (同一哨兵前后捕获 → assert_equal 通过)
  │   ├── test_identical_sidecars_match (多个哨兵文件全部不变 → 比较器返回无差异)
  │   └── test_nonexistent_remains_nonexistent (前后均不存在 → 视为一致)
  │
  ├── TestSentinelCompareChanged
  │   ├── test_content_change_detected (修改哨兵文件一个字节 → 比较器检测到 SHA-256 差异)
  │   ├── test_file_deleted_detected (删除哨兵 → exists 从 true 变 false → 检测)
  │   ├── test_file_created_detected (创建新哨兵 → exists 从 false 变 true → 检测)
  │   ├── test_length_change_detected (追加内容使长度变化 → 虽哈希也可能变，但长度检查先行)
  │   └── test_sidecar_appearance_detected (原不存在的 sidecar 哨兵出现 → 检测)
  │
  └── TestSidecarTriplet
      ├── test_all_three_present (三文件哨兵组全部存在 → 正确捕获状态)
      ├── test_all_three_absent (三文件哨兵组全部不存在 → 正确捕获 absent)
      └── test_mixed_presence (部分存在/部分缺失 → 正确捕获混合状态)
  ```

- [ ] **步骤 F-28：提交 DI 迁移（显式文件清单）**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。不得使用通配 stage 或 amend。

  逐项 stage：`app/core/config.py`、两个 Store、`schema.py`、F-5 至 F-23 表中的每个源码文件、`tests/conftest.py`、`tests/regression/test_collection_safety.py`、`tests/regression/test_hash_preservation.py`、`start.bat`。先运行 `git diff --cached --name-only` 与秘密/禁止路径检查，再创建新的 `feat(s1): inject validated database paths across runtime` 提交。

---

## 阶段 G：pytest 外内门禁 + collect-only → 定向 → 全回归

**前置条件：** F 完成（零参 Store 已淘汰）。**执行约束：** 所有 pytest 命令通过 `s1-pytest.ps1`。

- [ ] **步骤 G-1：增强根 `conftest.py`——早期 env 验证 + 内层 finalizer**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  根 `conftest.py` 导入合约：仅纯模块——`path_policy.py`、`_pytest_policy.py`、`hashlib`、`json`、`os`。**不导入** `Config`、`DuckDBStore`、`SQLiteStore`、`Schema`、任何业务模块。

  ```python
  """Root pytest configuration — env validation and formal-DB hash guard.

  This module imports ONLY pure path_policy and _pytest_policy.
  No app Config, storage, or business modules are imported here.
  """

  from __future__ import annotations

  import hashlib
  import json
  import os
  from pathlib import Path

  import pytest

  from _pytest_policy import is_archived_legacy_test
  from app.core.storage.path_policy import DatabasePathSet, VdEnv, resolve_and_validate_paths

  _S1_EVIDENCE_ROOT = pytest.StashKey[Path]()

  _FORMAL_FILES = [
      "data/valuedashboard.duckdb",
      "data/valuedashboard.sqlite",
      "data/valuedashboard.duckdb.wal",
      "data/valuedashboard.sqlite-wal",
      "data/valuedashboard.sqlite-shm",
  ]


  def _capture_hashes(result_path: Path) -> dict:
      """Capture formal file state without opening DB engine.

      Iterates _FORMAL_FILES. Streams each file in 64 KiB chunks and never
      loads an entire database into memory.
      Pure open/hashlib — no duckdb/sqlite3 imports.
      Returns dict keyed by filename with keys: exists, length, sha256.
      This is defense-in-depth evidence, not the authoritative gate; the outer
      PowerShell wrapper owns the decisive before/after comparison.
      """
      result = {}
      project_root = Path(__file__).resolve().parent
      for rel in _FORMAL_FILES:
          path = project_root / rel
          entry: dict = {}
          try:
              entry["exists"] = True
              h = hashlib.sha256()
              with open(path, "rb") as f:
                  while True:
                      chunk = f.read(65536)  # 64 KiB chunks
                      if not chunk:
                          break
                      h.update(chunk)
                  entry["length"] = os.fstat(f.fileno()).st_size
              entry["sha256"] = h.hexdigest().upper()
          except FileNotFoundError:
              entry["exists"] = False
              entry["length"] = None
              entry["sha256"] = None
          result[rel] = entry

      # Ensure parent directory exists before writing evidence
      result_path.parent.mkdir(parents=True, exist_ok=True)
      result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
      return result


  @pytest.hookimpl(tryfirst=True)
  def pytest_configure(config: pytest.Config) -> None:
      """Validate isolation environment.  No collect-only/help bypass.
      Pure path_policy/stdlib only — no Config/Store/business imports.
      """
      env = os.environ.get("VD_ENV", "")
      if env != VdEnv.TEST.value:
          raise SystemExit(f"FATAL: pytest requires VD_ENV={VdEnv.TEST.value}, got {env!r}")
      if "VD_FORMAL_ACK" in os.environ:
          raise SystemExit("FATAL: VD_FORMAL_ACK forbidden in pytest session")
      if not os.environ.get("VD_TEST_RUN_ROOT"):
          raise SystemExit("FATAL: VD_TEST_RUN_ROOT is required for pytest")
      if not os.environ.get("VD_DUCKDB_PATH") or not os.environ.get("VD_SQLITE_PATH"):
          raise SystemExit("FATAL: VD_DUCKDB_PATH and VD_SQLITE_PATH required for pytest")

      paths = resolve_and_validate_paths()
      assert paths.env == VdEnv.TEST

      # Capture pre-session formal hashes for defense-in-depth evidence
      # No Config.get_path() — paths are already validated by resolve_and_validate_paths
      evidence_root = Path(os.environ.get("VD_TEST_EVIDENCE_ROOT",
                         str(Path.cwd() / "docs" / "evidence-s1" / "pytest-inner")))
      evidence_root.mkdir(parents=True, exist_ok=True)
      _capture_hashes(evidence_root / "pre" / "hashes.json")

      # Store paths for pytest_unconfigure
      config.stash[_S1_EVIDENCE_ROOT] = evidence_root


  def pytest_unconfigure(config: pytest.Config) -> None:
      """Defense-in-depth: capture post-session formal hashes (no DB engine).
      Streams in 64 KiB chunks. Does NOT assert — wrapper is authoritative.
      """
      evidence_root = config.stash.get(_S1_EVIDENCE_ROOT, None)
      if evidence_root is not None:
          _capture_hashes(evidence_root / "post" / "hashes.json")
  ```

- [ ] **步骤 G-2：collect-only 通过包装器（环境已由包装器正确设置 → pytest_configure 验证通过）**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  & .\scripts\s1-pytest.ps1 -EvidenceDir "docs/evidence-s1/collect-only" "--collect-only" "-q" "--no-header"
  ```

  预期：仅列出 `tests/regression/` 下的测试。无 _legacy 测试。退出码 0。前后哈希无 delta。无环境变量 bypass——VD_ENV=test 和所有路径变量由包装器设置，`pytest_configure` 正常通过。

- [ ] **步骤 G-3：定向测试 path_isolation 和 hash_preservation**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  & .\scripts\s1-pytest.ps1 -EvidenceDir "docs/evidence-s1/path-tests" "-v" "--no-header" "-x" "tests/regression/test_path_isolation.py"
  & .\scripts\s1-pytest.ps1 -EvidenceDir "docs/evidence-s1/hash-tests" "-v" "--no-header" "-x" "tests/regression/test_hash_preservation.py"
  ```

- [ ] **步骤 G-4：全 regression 测试**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  & .\scripts\s1-pytest.ps1 -EvidenceDir "docs/evidence-s1/full-regression" "-v" "--no-header" "-x" "tests/regression/"
  ```

  预期：全部 regression 测试 PASS。退出码 0。前后正式哈希无 delta。

- [ ] **步骤 G-5：证据汇总**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  Get-ChildItem -Path "docs/evidence-s1" -Recurse -Filter "hash-evidence.json" |
    ForEach-Object {
        $c = Get-Content $_.FullName -Raw | ConvertFrom-Json
        $matched = $true
        foreach ($key in @("formal_duckdb", "formal_sqlite", "duckdb_wal", "sqlite_wal", "sqlite_shm")) {
            $beforeState = $c.before.$key
            $afterState = $c.after.$key
            if (($beforeState.exists -ne $afterState.exists) -or
                ($beforeState.length -ne $afterState.length) -or
                ($beforeState.sha256 -ne $afterState.sha256)) {
                $matched = $false
            }
        }
        "$($_.Directory.Name): $matched"
    }
  ```

- [ ] **步骤 G-6：提交 pytest 内层门禁**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。仅在 G-2 至 G-5 全部通过后执行。

  显式 stage `conftest.py`、`tests/regression/test_collection_safety.py`、`pyproject.toml`（仅当实际修改）以及逐个审查通过的 G 阶段证据 run 目录。创建新的 `test(s1): enforce isolated pytest lifecycle` 提交；不得 amend 之前的 C-F 提交。

---

## 阶段 H：data/.hashes 分层账本 + .gitignore 加固 + S1 整改提交

**前置条件：** G 完成，全部证据已收集。

- [ ] **步骤 H-1：创建 `data/.hashes`**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```yaml
  # data/.hashes — 分层哈希账本
  # data/ 下唯一被 Git 追踪的文件

  rebuild-source:
    duckdb: "46EBCEB6DDBCCA15D4E82D22CFA659EC1C593033DE190C08B5C54FC7211A3C91"
    sqlite: "228E0F53A8EBD0B99DAED8FA2D683D42F46644E8805495350EC162EFEC6596D3"
    note: "data/backup/audit_pre_fix_20260720.* — S3 staging rebuild sources"

  incident:
    duckdb: "5186E660E603B277B72E4EAF9988963C64B25B83882BA5ACF4BE2789A51268D6"
    sqlite: "B7B5F2FF2D1B4D2F71512DFEBD8DC2FBD9625E51BE2D0BFDB5CAF0657EC11959"
    note: "Formal files at incident-2026-07-22. DRIFTED_FROZEN — not trusted."

  released-formal-seed:
    duckdb: null
    sqlite: null
    note: "Set during S7 promote — must match signed staging candidate exactly."

  runtime-observed: {}
  ```

- [ ] **步骤 H-2：加固 .gitignore 使 `data/.hashes` 可追踪**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。这是对基线提交后 .gitignore 的首次 S1 整改修改。

  将基线中现有 `data/` 排除行替换为两条规则（注意顺序：先排除全部 data/，再白名单 .hashes）：

  ```gitignore
  # Runtime data — only .hashes ledger is tracked
  data/*
  !data/.hashes
  ```

  同时补充以下排除规则（保留现有全部规则）：

  ```gitignore
  # Build artifacts
  frontend/dist/
  dist/
  build/

  # Frontend generated state
  .vite/
  frontend/.vite/
  frontend/test-results/

  # Credentials (never)
  *.pem
  *.key
  .env
  .env.*
  uat_password_*/

  # Legacy commercial / third-party raw data (review before allowing)
  _legacy/raw_source_data/
  _legacy/uat_archives/

  # Commercial data files
  *.dta
  ```

- [ ] **步骤 H-3：验证 .gitignore 规则**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  # data/.hashes should NOT match any rule → exit 1 (no ignore rule) → means tracked
  git check-ignore -v data/.hashes 2>&1
  # Expected: exit code 1 (no match).  If exit 0 with a rule match, exception failed
  if ($LASTEXITCODE -eq 0) { Write-Host "ERROR: data/.hashes is still ignored" -ForegroundColor Red }

  # data/ should be ignored (except .hashes)
  git check-ignore -v data/valuedashboard.duckdb
  # Expected: prints "$REPO/.gitignore:N:data/*  data/valuedashboard.duckdb"

  # dist/, node_modules/ ignored
  git check-ignore -v dist/ | Out-Null
  git check-ignore -v frontend/node_modules/ | Out-Null
  ```

- [ ] **步骤 H-4：S1 整改提交**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。**不是 amend。**

  ```powershell
  git add -- "data/.hashes"
  git add -- ".gitignore"
  git commit -m "feat(s1): path isolation, DatabasePathSet, hash ledger, evidence

  - Zero-arg DuckDBStore()/SQLiteStore() eliminated across all 20+ call sites.
  - resolve_and_validate_paths() is the only runtime environment construction path.
  - Formal DB hash invariance proven across all S1 pytest sessions.
  - Outer PowerShell wrapper (s1-pytest.ps1) gates every Python invocation.
  - data/.hashes four-layer ledger: rebuild-source / incident / released-formal-seed=null / runtime-observed.
  - .gitignore hardened: data/* + !data/.hashes; build/credential exclusions.
  "
  ```

- [ ] **步骤 H-5：最终不变性证据**

  > 未来执行 / RUN ONLY AFTER OWNER APPROVAL。

  ```powershell
  Get-FileHash -LiteralPath "data/valuedashboard.duckdb" -Algorithm SHA256
  Get-FileHash -LiteralPath "data/valuedashboard.sqlite" -Algorithm SHA256
  git log --oneline -10
  git tag -l "incident-*"
  ```

  预期：哈希与 incident 值一致。git 日志显示基线 → 各阶段提交 → S1 整改提交。tag 存在。

---

## S1 接受检查清单

**来源：** 合约第 15 节（证据要求） + 基线/tag + 审查要求。全部通过 = S1 完成。

| # | 检查项 | 通过条件 |
|---|---|---|
| 1 | 设计合约文档已签署为 APPROVED | `docs/contracts/path-isolation-contract.md` 所有者签署记录存在 |
| 2 | 初始源基线 commit 存在 | `git log --oneline` 显示基线提交 |
| 3 | `incident-2026-07-22` annotated tag 存在，指向基线提交 | `git tag -l "incident-*" --format="%(refname) %(objectname)"` 输出非空 |
| 4 | 基线提交不含 data/dist/node_modules/凭据/商业数据/UAT | `git show --pretty="" --name-only HEAD` 无已排除路径 |
| 5 | 秘密/第三方数据审查完成 | 审查记录存在（`docs/evidence-s1/baseline-commit/` 下证据） |
| 6 | S0 证据包完整、已签署、按原字节进入事故基线且未被 S1 修改 | 01-10 文件、manifest 和双签名存在；基线提交包含其原字节；后续 S1 diff 不修改该目录 |
| 7 | `app/core/storage/path_policy.py` 实现并提交 | 文件在 remediation 分支上；import 审查确认零 DB/存储导入 |
| 8 | `scripts/s1-path-preflight.ps1` 实现并提交 | 文件存在；dry-run 正向/负向均已通过 |
| 9 | `scripts/s1-pytest.ps1` 实现并提交 | 文件存在；Compare-FormalState 比较 sidecars 的 exists/length/SHA256 |
| 10 | `data/.hashes` 四层结构存在 | rebuild-source / incident / released-formal-seed=null / runtime-observed 四层确认 |
| 11 | `.gitignore` 含 `data/*` + `!data/.hashes` | `git check-ignore -v data/.hashes` 退出码 1（不被忽略） |
| 12 | `test_path_isolation.py` 全部通过（纯模块，零 DB 导入） | pytest 证据 + AST 静态审查记录 |
| 13 | `test_hash_preservation.py` 全部通过（纯合成哨兵文件，不读取正式文件） | pytest 证据 |
| 14 | preflight 模拟运行（正向）：设置有效路径 → preflight 通过 → 摘要输出正确 | 人工观察记录 |
| 15 | 包装器纯策略模拟运行：preflight + pytest `test_path_isolation.py`（运行根不存在）→ 前后哈希一致 → PASS | 人工观察记录 + 证据目录含 pre/post |
| 16 | 包装器完整回归运行：preflight → `tests/regression/` 全部通过 → 前后哈希一致 → PASS | `docs/evidence-s1/full-regression/` 下证据 |
| 17 | 证据目录 `docs/evidence-s1/<run-id>/` 包含 pre/、post/ 和 hash-evidence.json | 每个执行 run 的证据子目录均含完整状态 |
| 18 | `pytest_unconfigure` 防御纵深哈希日志存在于证据目录中（无 DB 引擎） | 证据目录包含 pytest-inner 子目录的 hashes.json |
| 19 | `pytest_configure` 无 collect-only/help bypass，并调用同一纯路径策略验证；缺失/冲突变量的拒绝逻辑可证 | AST/代码审查确认 hook 无 bypass；`test_path_isolation.py` 用 monkeypatch 验证缺失 VD_ENV、存在 VD_FORMAL_ACK 和错误 profile 均触发 `PathIsolationError`；不得为证明此项而绕开 wrapper 启动 pytest |
| 20 | AST 静态守卫扩展后无假阳性/假阴性问题 | `test_collection_safety.py` 增强版通过 |
| 21 | `start.bat` 代码审查确认 `VD_FORMAL_ACK` 只检查 `=confirmed`，不自动设置；检查 `VD_ENV=formal`、显式绝对 DB 路径 | 代码审查记录 |
| 22 | `git diff --stat` 确认所有变更点已覆盖 | 源头变更 vs 合约精确变更点清单比对 |
| 23 | 正式 DuckDB、SQLite 及三个 sidecar 的 exists/length/SHA256 在全部 S1 pytest 会话前后不变 | `docs/evidence-s1/` 下每个 `hash-evidence.json` 对完整 5 文件集显示 before==after |
| 24 | 所有者签署 S1 完井确认 | 签署记录 — 确认所有 formal before/after 状态一致 |

---

## 停止 / 回滚行为

| 事件 | 行为 |
|---|---|
| pytest 测试失败（退出码非 0） | 包装器 after 捕获仍执行。证据保存。运行根保留。修复后通过新提交重试 |
| 正式库哈希变化（exit 99） | 立即停止所有 Python 操作。运行根和证据保留。执行根因分析。修复前不继续 S1 |
| Git 操作失败 | 禁止 `--amend`/`reset`/`force`。用 `git revert <bad-commit>` 创建前向回滚 |
| `incident-2026-07-22` tag | 不可变。不得删除、移动或修改 |

---

## S2 移交（对应 docs/15 的 S2 源码闭包）

S1 完成后移交给 S2：

1. **Git 历史：** `incident-2026-07-22` tag → S1 完整提交链
2. **路径隔离合约：** `path_policy.DatabasePathSet`、`.gitignore`、外层/内层门禁
3. **哈希证据：** `docs/evidence-s1/` 下全部分层证据
4. **所有者签署：** S1 接受清单签名

S2 起点（docs/15 §6.2）：增量 raw+QFQ 一致维护实现、安全/发布缺口闭包、作业 heartbeat。S2 所有 Python 继续通过 `s1-pytest.ps1` 调用。S3 为 staging 数据重建（不是 S1/S2 的范围）。

---

## 当前轮次范围确认

> 本文档仅创建此实施计划文件。不执行任何 Python、pytest、git add/commit/tag/copy、app 或 DB 命令。不使用 `git add .`。不允许 forensic DB 引擎访问。不主张正式哈希等于备份。不将测试根放在 `data/` 下。无零参 Store 回退保留。
