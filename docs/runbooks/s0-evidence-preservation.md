---
title: S0 证据保全运行手册
status: approved
category: runbooks
last-reviewed: 2026-07-26
---

# S0 证据保全运行手册

## 1 概述

### 1.1 目的

本文档定义 **价值仪表盘（Value Dashboard）** 项目在正式数据库已偏离批准基线、系统处于 **BLOCK / NO-GO** 状态下，执行 **S0（证据保全）** 阶段的标准操作程序。S0 的目标是：

- 冻结现场：以只读方式捕获当前所有相关文件的精确元数据与哈希；
- 记录环境：捕获操作系统级进程、网络侦听状态、Git 状态，排除正在写入的引擎；
- 保全副本：在获得明确书面批准后，将当前正式数据库文件集复制到 Git 仓库之外的独立取证根目录；
- 产生可审计的本地证据包。

S0 **不负责** 后续阶段：
- **S1**：测试/数据路径强隔离与哈希门禁（不在本文档授权范围）；
- **S2**：源码闭合（不在本文档授权范围）；
- **S3**：基于受信源的分阶段重建（不在本文档授权范围）。

### 1.2 权威依据

本文档受 **[docs/15_CURRENT_REVERIFICATION_AND_REMEDIATION_GUIDE.md](../15_CURRENT_REVERIFICATION_AND_REMEDIATION_GUIDE.md)** 管辖。项目整体阻断状态 **BLOCK / NO-GO** 不变。S0 *证据保全* 是只读操作，**不授权** S1、S2、S3 或版本发版。

### 1.3 范围

| 纳入 | 排除 |
|---|---|
| 正式数据库主文件及其侧车（`.duckdb`, `.sqlite`, `.wal`, `-wal`, `-shm`） | 数据库引擎、Python 运行时 |
| 批准基线备份双文件（元数据与哈希仅作为重建源证据） | 应用代码、配置、前端、测试 |
| 操作系统进程与端口快照 | 数据库内容读取、查询、转储、分析 |
| Git 状态（分支、远程、未跟踪） | Git 写操作 |
| 磁盘目录清单 | 将备份文件纳入取证副本集（备份是重建源证据，非事中证据） |
| | 文件复制（直至所有者书面批准） |

### 1.4 运行标识

每次 S0 执行分配一个唯一运行 ID，格式为 `s0-YYYYMMDD-HHMMSS`（UTC+8）。运行 ID 由操作员在执行开始时生成。本文档中所有 `\<run-id\>` 占位符须在记录和路径中替换为实际值。

### 1.5 本次运行状态

本文档创建于 **2026-07-23 UTC+8**，是当前 S0 运行的产物。本次运行：
- ✅ 在本工作区内执行了文档化所列的全部只读观察命令（进程、端口、元数据、哈希、目录、Git）；
- ✅ 创建了本文档 `docs/runbooks/s0-evidence-preservation.md`；
- ❌ **未创建** `docs/evidence/evidence-s0/` 目录；
- ❌ **未执行** 任何数据库文件副本；
- ❌ **未完成或签署** S0 退出检查表。

---

## 2 前置条件检查

### 2.1 工作目录

所有命令在工作目录 `D:\Mr.Q\掌控经济\value-dashboard` 下执行。确认 PowerShell 7+（`$PSVersionTable.PSVersion.Major -ge 7`）可用。

**命令：**

```powershell
Set-Location -LiteralPath 'D:\Mr.Q\掌控经济\value-dashboard'
$PSVersionTable.PSVersion
```

**预期结果：** `Major` >= 7。

**停止条件：** PowerShell 版本 < 7。升级后方可继续。

**证据路径：** 不单独保存，该检查结果应出现在命令日志中。

### 2.2 确认无运行中数据库引擎或应用进程

执行 S0 前必须确认没有任何数据库引擎、应用运行时或服务器进程正在运行。

**命令：**

```powershell
Get-Process -Name 'python','pythonw','uvicorn','duckdb','sqlite3','value-dashboard','pyinstaller','node' -ErrorAction SilentlyContinue |
  Select-Object Id, ProcessName, StartTime, Responding
```

**预期结果：** 无输出。如输出非空，记录到证据包进程清单，**停止**并寻求所有者关于 PID 级停机的书面批准。**禁止**使用 `Stop-Process` 或用通配符杀死进程。

**证据路径：** `docs\evidence-s0\<run-id>\02-process-state.txt`

### 2.3 确认无网络侦听

检查 8765（应用服务器）、5173（Vite 开发服务器）、6176（测试 runner）端口。

**命令：**

```powershell
$ports = @(8765, 5173, 6176)
foreach ($p in $ports) {
  $conn = Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue
  if ($conn) {
    $conn | Select-Object LocalPort, OwningProcess, State
  } else {
    Write-Output "Port ${p}: no listener"
  }
}
```

**预期结果：** 三个端口均输出 `Port X: no listener`。

**停止条件：** 任何端口存在侦听器。记录后停止，寻求所有者批准关闭该侦听器。

**证据路径：** `docs\evidence-s0\<run-id>\03-port-state.txt`

---

## 3 证据采集操作

### 3.1 建立本地证据目录

在 `docs/` 下创建本次运行的本地证据目录。该目录保存操作日志和全部元数据输出，是本地 S0 证据包。

**命令：**

```powershell
$runId = 's0-20260723-<HHMMSS>'   # 操作员填入实际时间
New-Item -ItemType Directory -Path "docs\evidence-s0\${runId}"
```

**预期结果：** 目录创建成功。

**停止条件：** 目录已存在（检测 `Test-Path`）。如果存在且为空，可复用；如果非空，需确认是否为同一运行/可追加，或分配新的 `$runId`。

### 3.2 证据清单子目录结构

```
docs\evidence-s0\<run-id>\
├── 01-commands.log              # 逐条命令记录（时间戳 + 命令 + 输出摘要）
├── 02-process-state.txt         # 进程快照（全量 + 相关进程标记）
├── 03-port-state.txt            # 端口侦听快照
├── 04-file-metadata.txt         # 文件元数据清单（含侧车、备份目录）
├── 05-hash-ledger.txt           # SHA-256 哈希账本（事中哈希 + 批准重建源哈希）
├── 06-sidecar-inventory.txt     # 正式数据库侧车文件清单
├── 07-directory-summary.txt     # data/ 递归目录清单
├── 08-git-state.txt             # Git 状态
├── 09-manifest.txt              # 证据目录内文件清单（含 SHA-256 自校验）
├── 10-signoff-checklist.txt     # 退出检查表（待签署）
```

### 3.3 进程与端口快照

**3.3.1 全量进程清单**

**命令：**

```powershell
Get-Process | Select-Object Id, ProcessName, StartTime, Responding, CPU, `
  @{N='PagedMemoryMB';E={[math]::Round($_.PagedMemorySize/1MB, 2)}} |
  Sort-Object ProcessName |
  Format-Table -AutoSize -Wrap
```

**预期结果：** 输出全部运行中进程清单。

**停止条件：** 命令失败或无输出（如因权限限制无法列举进程）。记录错误并 **STOP**；不可在缺少进程快照的情况下继续。

**证据路径：** `docs\evidence-s0\<run-id>\02-process-state.txt`

以后续追加命令（`Add-Content`）将相关进程筛选结果写入同一文件。

**3.3.2 相关进程筛选**

**命令：**

```powershell
$relevantNames = @('python','pythonw','uvicorn','duckdb','sqlite3','value-dashboard','pyinstaller','node')
Get-Process |
  Where-Object { $_.ProcessName -in $relevantNames } |
  Select-Object Id, ProcessName, StartTime, Responding |
  Format-Table -AutoSize
```

**预期结果：** 无输出（无匹配进程）。

**停止条件：** 如有输出（相关进程正在运行），则 **STOP**。将结果追加记录到同一文件。寻求所有者对具体 PID 的停机关闭书面授权。**禁止**在未获书面授权时执行 `Stop-Process`。端口归属是决定性证据——侦听应用端口的进程不能仅凭进程名判断。

**证据路径：** 追加到 `docs\evidence-s0\<run-id>\02-process-state.txt`

**3.3.3 端口侦听清单**

**命令：**

```powershell
Get-NetTCPConnection -State Listen |
  Select-Object LocalAddress, LocalPort, OwningProcess, State |
  Sort-Object LocalPort |
  Format-Table -AutoSize
```

**预期结果：** 输出全部 TCP 侦听端口。

**停止条件：** 命令失败。记录错误并 **STOP**。如果命令成功执行但未发现任何侦听端口（空输出），那是有效的空快照，不是失败。

**证据路径：** `docs\evidence-s0\<run-id>\03-port-state.txt`

### 3.4 文件元数据采集

**3.4.1 正式数据库主文件**

> **安全规定：** 不得使用 `Get-Content` 读取数据库文件内容。

**命令：**

```powershell
Get-ChildItem -LiteralPath 'D:\Mr.Q\掌控经济\value-dashboard\data\valuedashboard.duckdb' |
  Select-Object FullName, Length, CreationTimeUtc, LastWriteTimeUtc, Attributes, LinkType, Target |
  Format-List

Get-ChildItem -LiteralPath 'D:\Mr.Q\掌控经济\value-dashboard\data\valuedashboard.sqlite' |
  Select-Object FullName, Length, CreationTimeUtc, LastWriteTimeUtc, Attributes, LinkType, Target |
  Format-List
```

**预期结果：**

```
valuedashboard.duckdb
  FullName      : D:\Mr.Q\掌控经济\value-dashboard\data\valuedashboard.duckdb
  Length        : 1986801664
  CreationTimeUtc : 2026-07-17 08:50:23
  LastWriteTimeUtc: 2026-07-22 01:41:26
  Attributes    : Archive
  LinkType      : (空，非链接)
  Target        : (空，非链接)

valuedashboard.sqlite
  FullName      : D:\Mr.Q\掌控经济\value-dashboard\data\valuedashboard.sqlite
  Length        : 450560
  CreationTimeUtc : 2026-07-17 08:50:23
  LastWriteTimeUtc: 2026-07-21 16:21:26
  Attributes    : Archive
  LinkType      : (空，非链接)
  Target        : (空，非链接)
```

**停止条件：** 若 `LinkType` 非空（文件是符号链接/交接点），**STOP**，记录并联系所有者确认源路径可靠性。若命令失败或输出不完整，**STOP**。

**证据路径：** `docs\evidence-s0\<run-id>\04-file-metadata.txt`

**3.4.2 正式数据库侧车文件**

采集正式数据库的 WAL 和共享内存侧车元数据。**禁止**对侧车文件执行 `Get-Content`、分析其内容或推断语义。

**命令：**

```powershell
$sidecarPatterns = @(
  'data\valuedashboard.duckdb.wal',
  'data\valuedashboard.sqlite-wal',
  'data\valuedashboard.sqlite-shm'
)
foreach ($p in $sidecarPatterns) {
  $fullPath = Join-Path -Path 'D:\Mr.Q\掌控经济\value-dashboard' -ChildPath $p
  if (Test-Path -LiteralPath $fullPath) {
    Get-ChildItem -LiteralPath $fullPath |
      Select-Object FullName, Length, CreationTimeUtc, LastWriteTimeUtc, Attributes, LinkType, Target |
      Format-List
  } else {
    Write-Output "SIDECAR ABSENT: ${p}"
  }
}
```

**预期结果（当前观测，2026-07-23）：**

```
SIDECAR ABSENT: data\valuedashboard.duckdb.wal
SIDECAR ABSENT: data\valuedashboard.sqlite-wal
SIDECAR ABSENT: data\valuedashboard.sqlite-shm
```

**停止条件：** 命令失败。**STOP**。

**证据路径：** `docs\evidence-s0\<run-id>\06-sidecar-inventory.txt`

**3.4.3 批准重建源备份目录（元数据仅清单，不入取证集）**

> 以下文件是批准重建源（Approved Rebuild-Source），**不是**事中证据。仅记录元数据和哈希；**不得**将其纳入取证副本集。

**命令：**

```powershell
Get-ChildItem -LiteralPath 'D:\Mr.Q\掌控经济\value-dashboard\data\backup' |
  Select-Object FullName, Length, CreationTimeUtc, LastWriteTimeUtc, Attributes, LinkType, Target |
  Sort-Object FullName |
  Format-Table -AutoSize -Wrap
```

**预期结果：**

```
FullName    ...\backup\audit_pre_fix_20260720.duckdb      Length=1986801664  ...
FullName    ...\backup\audit_pre_fix_20260720.sqlite      Length=446464     ...
FullName    ...\backup\audit_pre_fix_20260720.sqlite-shm  Length=32768      ...
FullName    ...\backup\audit_pre_fix_20260720.sqlite-wal  Length=0          ...
FullName    ...\backup\full_20260717_223725.zip           Length=625961     ...
```

**注意：** `audit_pre_fix_20260720.sqlite-shm`（32768 字节）和 `audit_pre_fix_20260720.sqlite-wal`（0 字节）仅在本文档中记录为元数据；其存在不代表数据库引擎未关闭或数据丢失。**禁止**对其语义进行推断或处理。这些备份侧车文件 **不列入取证副本集**。

**停止条件：** 命令失败。**STOP**。

**证据路径：** `docs\evidence-s0\<run-id>\04-file-metadata.txt`（追加）

> ⚠️ 确认：本节（3.4.3）的所有文件仅在 05-hash-ledger.txt 中记录重建源哈希。**不允许**复制到外部取证根目录的 `forensics/` 下。

### 3.5 SHA-256 哈希账本

> **原则：** 只对存在路径执行 `Get-FileHash`。不对已确认不存在的侧车路径执行哈希。

**3.5.1 正式数据库对（事中哈希）**

**命令：**

```powershell
Get-FileHash -LiteralPath 'D:\Mr.Q\掌控经济\value-dashboard\data\valuedashboard.duckdb' -Algorithm SHA256
Get-FileHash -LiteralPath 'D:\Mr.Q\掌控经济\value-dashboard\data\valuedashboard.sqlite' -Algorithm SHA256
```

**预期结果：**

```
Algorithm   Hash                                                             Path
SHA256      5186E660E603B277B72E4EAF9988963C64B25B83882BA5ACF4BE2789A51268D6 ...\data\valuedashboard.duckdb
SHA256      B7B5F2FF2D1B4D2F71512DFEBD8DC2FBD9625E51BE2D0BFDB5CAF0657EC11959 ...\data\valuedashboard.sqlite
```

**状态：** **事中哈希**（Incident Hash）。当前正式文件已经偏离批准基线。

**停止条件：** 任何 `Get-FileHash` 失败或输出不完整。**STOP**。

**证据路径：** `docs\evidence-s0\<run-id>\05-hash-ledger.txt`

**3.5.2 批准重建源备份对（重建源哈希）**

**命令：**

```powershell
Get-FileHash -LiteralPath 'D:\Mr.Q\掌控经济\value-dashboard\data\backup\audit_pre_fix_20260720.duckdb' -Algorithm SHA256
Get-FileHash -LiteralPath 'D:\Mr.Q\掌控经济\value-dashboard\data\backup\audit_pre_fix_20260720.sqlite' -Algorithm SHA256
```

**预期结果：**

```
Algorithm   Hash                                                             Path
SHA256      46EBCEB6DDBCCA15D4E82D22CFA659EC1C593033DE190C08B5C54FC7211A3C91 ...\backup\audit_pre_fix_20260720.duckdb
SHA256      228E0F53A8EBD0B99DAED8FA2D683D42F46644E8805495350EC162EFEC6596D3 ...\backup\audit_pre_fix_20260720.sqlite
```

**状态：** **批准重建源哈希**（Approved Rebuild-Source Hash）。备份文件在本次观测时与批准基线一致。

**停止条件：** 任何 `Get-FileHash` 失败或输出不完整。**STOP**。如果备份哈希与批准基线 *不* 一致，则重建源也受损——这是一个独立的严重发现，须立即通知所有者。

**证据路径：** 追加到 `docs\evidence-s0\<run-id>\05-hash-ledger.txt`

**3.5.3 哈希差异说明**

```
                    | 正式 DuckDB                          | 正式 SQLite
--------------------|--------------------------------------|--------------------------------------
事中哈希            | 5186E660E6...A51268D6                | B7B5F2FF2D...7EC11959
批准重建源备份哈希  | 46EBCEB6DD...A3C91（不同）           | 228E0F53A8...C6596D3（不同）
状态                | 漂移 / 冻结                          | 漂移 / 冻结

                    | 备份 DuckDB                          | 备份 SQLite
--------------------|--------------------------------------|--------------------------------------
批准重建源哈希      | 46EBCEB6DD...A3C91（一致，未漂移）   | 228E0F53A8...C6596D3（一致，未漂移）

**关键区别：** 正式文件哈希与备份哈希不同是预期状态——不是正常态，正是本次事故的表现。
正式数据库在 2026-07-21/22 的 pytest 运行后发生两次变异，已偏离初始批准基线。
备份哈希保持不变，是受信的重建源。
**严禁**断言"正式哈希应等于备份哈希"。
```

### 3.6 目录快照

**命令：**

```powershell
Get-ChildItem -LiteralPath 'D:\Mr.Q\掌控经济\value-dashboard\data' -Recurse -Force |
  Select-Object FullName, Length, LastWriteTimeUtc, Attributes |
  Sort-Object FullName |
  Format-Table -AutoSize -Wrap
```

**预期结果：** `data/` 下全部文件和子目录清单。

**注意：** 该输出包括大量测试/归档子目录。虽然运行耗时可能较长（因文件数量），但这是合法的 S0 范围操作，仅读取目录元数据，**不**读取文件内容。

**停止条件：** 命令失败或输出被截断（验证输出完整）。**STOP**。

**证据路径：** `docs\evidence-s0\<run-id>\07-directory-summary.txt`

### 3.7 Git 状态快照

**命令：**

```powershell
git -C 'D:\Mr.Q\掌控经济\value-dashboard' branch --show-current
git -C 'D:\Mr.Q\掌控经济\value-dashboard' remote -v
git -C 'D:\Mr.Q\掌控经济\value-dashboard' status --short
git -C 'D:\Mr.Q\掌控经济\value-dashboard' log --oneline -5
```

**预期结果：**

```
fix/audit-remediation
origin  https://github.com/bootition/value_dashboard.git (fetch)
origin  https://github.com/bootition/value_dashboard.git (push)
?? .gitignore
?? .omo/
?? .opencode/
?? .vite/
?? README.md
?? _legacy/
?? _pytest_policy.py
?? app/
?? config/
?? conftest.py
?? docs/
?? frontend/
?? pyproject.toml
?? start.bat
?? tests/
?? value-dashboard.spec
fatal: ambiguous argument 'HEAD': unknown revision or path not in the working tree.
```

**说明：** `fix/audit-remediation` 分支包含零次提交（孤儿分支），所有文件处于未跟踪状态。`git log` 失败是预期行为（无提交历史）。远程指向 `bootition/value_dashboard.git`。

**停止条件：** 前三个命令（branch、remote、status）任意一个失败。`git log` 的 fatal 是预期行为，不构成停止条件。

**证据路径：** `docs\evidence-s0\<run-id>\08-git-state.txt`

### 3.8 运行命令日志

将本节所有被执行命令连同时间戳和摘要输出，使用 `Start-Transcript` 或逐条 `Add-Content` 记录到：

**证据路径：** `docs\evidence-s0\<run-id>\01-commands.log`

**建议格式：** 每条命令记录包括：

```
=== 2026-07-23 HH:MM:SS ===
COMMAND: <powershell command>
OUTPUT:
<command output>
--- END ---
```

---

## 4 外部取证根目录设计

### 4.1 设计原则

外部取证根目录是在 Git 工作目录之外的一个独立目录，用于保存 S0 证据包的完整副本。设计目标是：

1. 隔离性：不受 `git clean`、分支切换、`reset --hard` 影响。
2. 完整性：副本文件哈希、大小、相对路径与源文件精确一致。
3. 可审计性：副本创建时的环境快照同时保留。
4. 永久冻结：正式数据库副本是永久证据，**不得**被数据库引擎直接打开、用于暂存、测试或分析。若未来批准分析，须在后续管控下另行制作工作副本（不属于 S0/S1 范围）。

### 4.2 建议路径结构

外部取证根目录仅包含：
- **正式文件集**：当前冻结的正式 DB 主文件 + 拍照时刻存在的侧车；
- **S0 本地证据包**：`docs\evidence-s0\<run-id>\` 下全部文件。

```
D:\Mr.Q\掌控经济\value-dashboard-incident-evidence\
└── 2026-07-23\
    └── <run-id>\
        ├── 00-MANIFEST.txt          # 副本文件清单（含源路径、目标路径、大小、SHA-256）
        ├── 01-COPY-VERIFICATION.txt # 副本校验结果
        ├── 02-OWNER-APPROVAL.txt    # 所有者书面批准（含日期、签名）
        ├── forensics\               # 正式数据库文件副本（仅当前文件集）
        │   └── data\
        │       ├── valuedashboard.duckdb
        │       ├── valuedashboard.sqlite
        │       └── (拍照时刻存在的正式侧车)
        └── evidence-package\        # 本地证据包副本
            └── (docs\evidence-s0\<run-id>\ 下的全部文件)
```

**注意：** 没有 `data\backup\` 子树。批准重建源备份文件**不纳入**外部取证副本集。它们仅在本地哈希账本中作为重建源证据记录。

### 4.3 注意事项

- **路径仅为设计建议。** 实际使用前须经所有者书面批准。
- 根 `D:\Mr.Q\掌控经济\value-dashboard-incident-evidence\` 是示例，不在本手册执行时创建。
- 所有者可以批准不同路径（如另一磁盘、网络共享、可移动介质）。在任何副本操作前必须有明确书面批准。
- 目标目录必须为**全新或空目录**。不允许覆盖既有目录。

### 4.4 本运行状态

本次 S0 运行 **未创建** 外部取证根目录，**未执行** 任何副本操作。这是 S0 运行的预期状态——外部复制仅在所有者书面批准后才执行。

---

## 5 所有者批准的取证副本操作（未来阶段）

> **本段内容为未来执行参考。在当前 S0 运行中不执行。**
> 本文档创建完毕、本地证据采集章节完成，**不自动授权**外部复制。
> 外部取证副本仅在所有者书面批准后执行。

### 5.1 前置确认清单

在以下**每一项**都得到满足后方可继续：

| # | 检查项 | 确认方法 | 结果 |
|---|---|---|---|
| A | 所有者书面批准（含目标路径） | 查看 `02-OWNER-APPROVAL.txt` 或电子邮件/IM 记录 | 是/否 |
| B | 目标目录父级存在 | `Test-Path -LiteralPath <parent>` | 是/否 |
| C | 目标目录不存在或为空 | `Test-Path -LiteralPath <target>` | 否（不存在）或为空 |
| D | 源路径不是交接点/符号链接 | `(Get-Item -LiteralPath <path>).LinkType` 为空（针对每个源文件） | 是/否 |
| E | 目标路径不是 Git 仓库 | `Test-Path -LiteralPath '<target>\.git'` 为 `False` | 是/否 |
| F | 目标卷有足够可用空间（> 5 GB） | `(Get-PSDrive -Name <drive>).Free` | 是/否 |
| G | 当前无相关进程/侦听器 | 按第 2.2、2.3、3.3.2 节检查，全部通过 | 是/否 |
| H | 目标目录 ACL 允许写入 | `Get-Acl -LiteralPath <parent>` | 是/否 |

### 5.2 待复制文件集（复制前一刻原子化捕获）

复制文件集**仅**包括当前冻结的正式数据库文件：

```
# 主文件（两项，始终存在）
data\valuedashboard.duckdb
data\valuedashboard.sqlite

# 正式侧车文件（以拍照时刻实际存在为准——动态捕获）
data\valuedashboard.duckdb.wal       # 若存在
data\valuedashboard.sqlite-wal        # 若存在
data\valuedashboard.sqlite-shm        # 若存在
```

**不纳入取证副本集的路径：**
- `data\backup\*` ——批准重建源，仅做哈希/元数据记录
- 任何应用代码、配置、测试文件

如果拍照时刻侧车文件从无到有出现，如实记录并在 `00-MANIFEST.txt` 中标记，**不得**推断引擎状态。

### 5.3 COPY-ITEM 命令（参考，动态侧车捕获）

> **不得在未获所有者书面批准时执行。**

```powershell
# === 请在获得所有者书面批准后执行 ===
$projectRoot = 'D:\Mr.Q\掌控经济\value-dashboard'
$targetRoot  = '<所有者批准路径>'   # 例如 D:\Mr.Q\掌控经济\value-dashboard-incident-evidence\2026-07-23\<run-id>

# 0. 确认目标不存在或为空
if (Test-Path -LiteralPath $targetRoot) {
  $targetItems = Get-ChildItem -LiteralPath $targetRoot
  if ($targetItems.Count -gt 0) {
    Write-Error "ERROR: Target already exists and is not empty: $targetRoot"
    exit 1
  }
}

# 1. 动态捕获当前正式侧车文件集
$mainFiles = @(
  'data\valuedashboard.duckdb',
  'data\valuedashboard.sqlite'
)
$formalSidecars = @()
$sidecarPatterns = @(
  'data\valuedashboard.duckdb.wal',
  'data\valuedashboard.sqlite-wal',
  'data\valuedashboard.sqlite-shm'
)
foreach ($rel in $sidecarPatterns) {
  $fullSrc = Join-Path $projectRoot $rel
  if (Test-Path -LiteralPath $fullSrc) {
    $formalSidecars += $rel
  }
}
$sourceFiles = $mainFiles + $formalSidecars

# 2. 创建目录结构
New-Item -ItemType Directory -Path "${targetRoot}\forensics\data" -ErrorAction Stop
New-Item -ItemType Directory -Path "${targetRoot}\evidence-package" -ErrorAction Stop

# 3. 捕获复制前清单与哈希（含存在的侧车）
$sourceManifest = foreach ($rel in $sourceFiles) {
  $fullSrc = Join-Path $projectRoot $rel
  if (Test-Path -LiteralPath $fullSrc) {
    $hash  = (Get-FileHash -LiteralPath $fullSrc -Algorithm SHA256).Hash
    $item  = Get-ChildItem -LiteralPath $fullSrc
    [PSCustomObject]@{
      RelativePath = $rel
      SourceSize   = $item.Length
      SourceHash   = $hash
      Present      = $true
    }
  }
}
# 同时记录不存在的侧车（作为清单一部分）
foreach ($rel in $sidecarPatterns) {
  if ($rel -notin $sourceFiles) {
    $sourceManifest += [PSCustomObject]@{
      RelativePath = $rel
      SourceSize   = $null
      SourceHash   = 'ABSENT'
      Present      = $false
    }
  }
}
$sourceManifest | Export-Csv -LiteralPath "${targetRoot}\00-MANIFEST.txt" -NoTypeInformation

# 4. 执行复制（使用 -LiteralPath；目标必须是新目录，不允许 -Force 覆盖）
foreach ($rel in $mainFiles) {
  $fullSrc = Join-Path $projectRoot $rel
  $dstFile = Join-Path $targetRoot "forensics\$rel"
  if (Test-Path -LiteralPath $dstFile) {
    Write-Error "ERROR: Destination already exists: $dstFile"
    exit 1
  }
  Copy-Item -LiteralPath $fullSrc -Destination $dstFile -ErrorAction Stop
}
foreach ($rel in $formalSidecars) {
  $fullSrc = Join-Path $projectRoot $rel
  $dstFile = Join-Path $targetRoot "forensics\$rel"
  if (Test-Path -LiteralPath $dstFile) {
    Write-Error "ERROR: Destination already exists: $dstFile"
    exit 1
  }
  Copy-Item -LiteralPath $fullSrc -Destination $dstFile -ErrorAction Stop
}

# 5. 复制本地证据包
$localEvidence = "docs\evidence-s0\${runId}"
$fullLocalEvidence = Join-Path $projectRoot $localEvidence
if (Test-Path -LiteralPath $fullLocalEvidence) {
  Copy-Item -LiteralPath $fullLocalEvidence -Destination "${targetRoot}\evidence-package\" -Recurse -ErrorAction Stop
}

# 6. 校验副本（相对名称、大小、SHA-256 源/目标比对）
$verifyResults = foreach ($rel in $sourceFiles) {
  $fullSrc = Join-Path $projectRoot $rel
  $fullDst = Join-Path $targetRoot "forensics\$rel"
  $srcHash = (Get-FileHash -LiteralPath $fullSrc -Algorithm SHA256).Hash
  $dstHash = (Get-FileHash -LiteralPath $fullDst -Algorithm SHA256).Hash
  $srcSize = (Get-ChildItem -LiteralPath $fullSrc).Length
  $dstSize = (Get-ChildItem -LiteralPath $fullDst).Length
  $sizeOk  = ($srcSize -eq $dstSize)
  $hashOk  = ($srcHash -eq $dstHash)
  [PSCustomObject]@{
    RelativePath = $rel
    SourceSize   = $srcSize
    DestSize     = $dstSize
    SizeMatch    = $sizeOk
    SourceHash   = $srcHash
    DestHash     = $dstHash
    HashMatch    = $hashOk
    Status       = if ($sizeOk -and $hashOk) { 'PASS' } else { 'FAIL' }
  }
}
$verifyResults | Format-Table -AutoSize | Out-String -Width 4096
$verifyResults | Export-Csv -LiteralPath "${targetRoot}\01-COPY-VERIFICATION.txt" -NoTypeInformation
```

`Copy-Item` 后目标文件的 `CreationTimeUtc` / `LastWriteTimeUtc` 可能反映复制行为或文件系统语义，不作为逐字节一致性的判定项。原始时间戳必须在复制前 manifest 中单独保留；副本验收只以相对名称、字节数和 SHA-256 完全一致为准。

**停止条件：** 任何一个文件的 `Status` 不为 `PASS`。**立即停止**，不得使用该副本。

### 5.4 取证副本使用禁令

- **不得**将取证副本用于临时分析、测试、开发或 staging 环境。
- **不得**在未获所有者书面批准的情况下被任何数据库引擎（DuckDB CLI、SQLite CLI、Python `import duckdb/sqlite3`）直接打开或查询。
- 取证副本是**永久证据冻结副本**。若未来批准对数据内容进行分析，须在后续管控下另行制作工作副本——这不属于 S0（证据保全）或 S1（隔离门禁）的范围。
- 备份文件（批准重建源）**不纳入**取证副本集；它们仅在本地哈希账本中保留重建源证据记录。

---

## 6 授权禁令

| 操作 | 授权状态 |
|---|---|
| 读取文件元数据（`Get-ChildItem`） | **授权** |
| 计算 SHA-256 哈希（`Get-FileHash`） | **授权** |
| 采集进程/端口/环境快照 | **授权** |
| 在 `docs\evidence-s0\<run-id>\` 内创建/写入/追加文本证据文件（`New-Item`、`Out-File`、`Set-Content`、`Add-Content`） | **仅授权** 在该证据目录内；所有其他路径的写入操作均被禁止 |
| 复制文件到外部取证根目录 | **需所有者书面批准** |
| 使用 `Get-Content` 读取 .duckdb/.sqlite 文件内容 | **禁止** |
| 打开数据库引擎（Python、DuckDB CLI、SQLite CLI、任何 DB-API 连接） | **禁止** |
| 运行 pytest、应用、CLI | **禁止** |
| 运行 checkpoint、VACUUM、备份、恢复 | **禁止** |
| 在 `docs\evidence-s0\<run-id>\` **之外**修改/移动/删除文件（`Set-Content`、`Move-Item`、`Remove-Item`） | **禁止** |
| Git 写操作（add、commit、push、reset、checkout） | **禁止** |
| `Stop-Process` | **禁止**（需所有者对具体 PID 书面授权） |

---

## 7 退出检查表

> **S0 完成条件**（根据 docs/15 §6 S0 退出证据）：
> 1. 小型证据包已创建（`docs/evidence/evidence-s0/<run-id>/` 含全部预期文件）；
> 2. S1 路径隔离合约草稿已完成（`docs/contracts/path-isolation-contract.md`）；
> 3. 所有者签署退出检查表。
>
> 外部取证副本（第 4 节外部取证根目录设计）是额外可选步骤，仅在所有者书面批准后执行。外部取证副本的存在与否**不决定** S0 是否完成——但若已执行，必须在退出检查表中记录创建路径和验证结果。
>
> ⚠️ **本文档的创建本身不构成 S0 完成。**

| # | 检查项 | 完成标准 | 完成 |
|---|---|---|---|
| 1 | 全部进程/端口快照已采集并写入证据目录 | 02-process-state.txt、03-port-state.txt 存在 | □ |
| 2 | 确认无相关进程运行 | 3.3.2 节筛选输出为空 | □ |
| 3 | 确认无端口侦听 | 8765/5173/6176 均为无侦听器 | □ |
| 4 | 正式数据库主文件元数据已记录 | 04-file-metadata.txt 含 duckdb + sqlite 条目 | □ |
| 5 | 正式数据库 SHA-256 哈希已记录 | 05-hash-ledger.txt 含两行事中哈希 | □ |
| 6 | 正式侧车文件清单已采集 | 06-sidecar-inventory.txt 全部侧车已记录（存在或 ABSENT） | □ |
| 7 | 批准重建源备份元数据 + 哈希已记录 | 备份双文件哈希存在且与批准基线一致 | □ |
| 8 | 事中哈希与批准重建源哈希已明确区分 | 05-hash-ledger.txt 包含哈希差异说明表 | □ |
| 9 | 目录递归快照已采集且完整 | 07-directory-summary.txt 存在 | □ |
| 10 | Git 状态已采集 | 08-git-state.txt 含分支、远程、状态 | □ |
| 11 | 命令日志已保存 | 01-commands.log 存在且含全部执行命令 | □ |
| 12 | 本文件（runbook）已创建 | `docs/runbooks/s0-evidence-preservation.md` 存在 | □ |
| 13 | 已理解外部取证根目录仅设计、未创建、未实施 | 第 4 节已阅读；未执行任何 Copy-Item | □ |
| 14 | 已理解外部复制需所有者书面批准 | 第 5 节已阅读；未执行副本操作 | □ |
| 15 | 已理解本文档不授权 S1/S2/S3 或版本发版 | BLOCK/NO-GO 状态不变 | □ |
| 16 | 已理解取证副本是永久证据，不得被 DB 引擎直接打开 | 第 4.1、5.4 节已阅读 | □ |
| 17 | 运行 ID 已分配，所有 `\<run-id\>` 已替换 | 证据目录路径含正确的 `<run-id>` | □ |

**S0 退出声明（由操作员和所有者共同签署）：**

```
操作员: _________________  日期: _________________

所有者: _________________  日期: _________________
```

---

## 附录 A：本文档创建时的观测快照

以下为本文档创建时的观测结果（2026-07-23 UTC+8），作为 S0 运行的基线参考。

### A.1 正式数据库

| 属性 | valuedashboard.duckdb | valuedashboard.sqlite |
|---|---|---|
| 全路径 | `...\data\valuedashboard.duckdb` | `...\data\valuedashboard.sqlite` |
| 大小（字节） | 1986801664 | 450560 |
| SHA-256 | `5186E660E603B277B72E4EAF9988963C64B25B83882BA5ACF4BE2789A51268D6` | `B7B5F2FF2D1B4D2F71512DFEBD8DC2FBD9625E51BE2D0BFDB5CAF0657EC11959` |
| 最后写入（UTC） | 2026-07-22 01:41:26 | 2026-07-21 16:21:26 |
| 创建时间（UTC） | 2026-07-17 08:50:23 | 2026-07-17 08:50:23 |
| 链接类型 | 无 | 无 |
| 哈希状态 | **事中哈希**（漂移） | **事中哈希**（漂移） |

### A.2 正式数据库侧车

| 侧车路径 | 存在 | 大小 | 备注 |
|---|---|---|---|
| `data\valuedashboard.duckdb.wal` | **否** | — | DuckDB WAL |
| `data\valuedashboard.sqlite-wal` | **否** | — | SQLite WAL |
| `data\valuedashboard.sqlite-shm` | **否** | — | SQLite 共享内存 |

### A.3 批准重建源备份（元数据仅清单，不入取证集）

| 属性 | audit_pre_fix_20260720.duckdb | audit_pre_fix_20260720.sqlite |
|---|---|---|
| 大小（字节） | 1986801664 | 446464 |
| SHA-256 | `46EBCEB6DDBCCA15D4E82D22CFA659EC1C593033DE190C08B5C54FC7211A3C91` | `228E0F53A8EBD0B99DAED8FA2D683D42F46644E8805495350EC162EFEC6596D3` |
| 哈希状态 | **批准重建源**（与基线一致） | **批准重建源**（与基线一致） |

备份目录还包含（仅元数据，不纳入取证副本集）：
- `audit_pre_fix_20260720.sqlite-shm`：32768 字节
- `audit_pre_fix_20260720.sqlite-wal`：0 字节
- `full_20260717_223725.zip`：625961 字节

### A.4 进程与端口

| 相关进程名 | 匹配实例 | 状态 |
|---|---|---|
| python / pythonw / uvicorn / duckdb / sqlite3 / value-dashboard / pyinstaller | **无** | 干净 |
| node | **6 个**（最终复核时观测） | 8765/5173/6176 均无侦听；S0 正式运行仍须记录 PID/命令行归属后再判定是否干扰 |

| 端口 | 应用 | 状态 |
|---|---|---|
| 8765 | 应用服务器 | 无侦听 |
| 5173 | Vite 开发服务器 | 无侦听 |
| 6176 | 测试 runner | 无侦听 |

### A.5 Git 状态

| 属性 | 值 |
|---|---|
| 当前分支 | `fix/audit-remediation` |
| 远程 | `origin  https://github.com/bootition/value_dashboard.git` |
| 提交数 | 0（孤儿分支） |
| 未跟踪顶层项 | 全部（16 项） |

---

## 附录 B：术语表

| 术语 | 定义 |
|---|---|
| **S0** | 证据保全阶段。只读采集现场快照，不执行分析或修复。 |
| **S1** | 测试/数据路径强隔离与哈希门禁阶段。本文档不授权 S1。 |
| **S2** | 源码闭合阶段。本文档不授权 S2。 |
| **S3** | 基于受信源的分阶段重建阶段。本文档不授权 S3。 |
| **事中哈希** | 当前正式数据库文件的 SHA-256，已偏离批准基线。 |
| **批准重建源哈希** | 备份数据库文件的 SHA-256，与重建前批准基线一致。备份文件是重建源证据，**不纳入事中取证副本集**。 |
| **BLOCK** | 项目总体阻断状态。正式数据未重建，G22/G23 未通过，不得发版。 |
| **NO-GO** | 发版禁令。即使代码改进完成，正式数据未经验收前不得发版。 |
| **侧车文件** | DuckDB 或 SQLite 在执行写入操作时创建的辅助文件（`.wal`、`-wal`、`-shm`）。 |
| **取证根目录** | 位于 Git 工作目录之外的独立目录，用于保存正式数据库副本与 S0 证据包。 |
| **<run-id>** | 每次 S0 执行的唯一标识，格式为 `s0-YYYYMMDD-HHMMSS`。 |
