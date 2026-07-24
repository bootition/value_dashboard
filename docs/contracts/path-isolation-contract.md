# S1 路径隔离合约：DatabasePathSet 与 fail-closed 边界

**文档状态：** DRAFT / DESIGNED — 设计级定义，非实现，非验证。  
**权威来源：** `docs/15_CURRENT_REVERIFICATION_AND_REMEDIATION_GUIDE.md` 以下章节：
  - §6 S1 （修复路线图 S1 节，第 349-412 行）—— S1 的权威设计说明、必须完成项、禁止事项和退出证据
  - §7 命令政策（第 700-758 行）—— 当前冻结命令和条件命令
  - §8 门禁表 G05/G06（第 761-799 行）—— S1 对应的哈希保存和隔离门禁验收标准
  - §11 证据账本模板（第 905-1177 行）—— 运行清单和哈希账本格式
  - §12 角色与批准矩阵（第 1181-1194 行）—— S1 需实施审核人批准
  - §13 立即可执行任务队列（第 1197-1236 行）—— S1 隔离合约设计列为立即可执行任务
  - §15 假设与局限（第 1302-1317 行）—— S0/S1 的依赖假设
**门禁等级：** BLOCK / NO-GO — 本合约约定的隔离未达成前，任何 Python 命令不得执行。  
**签署前提：** 所有者批准 S0（正式文件哈希固定为当前已偏离状态）和本 S1 设计。  
**合约状态：** 设计尚未实现，自动化验收尚未执行。

---

## 1. 合约目标

现有代码门禁（`_pytest_policy` + `test_collection_safety` AST 守卫 + `testpaths` 约束）已证明不足：
2026-07-21 和 2026-07-22 两次运行分别改变了正式 DuckDB/SQLite 文件。

**已确认的不安全条件（不依赖第二次变异的 exact 调用链证明）：**

- 零参数 `DuckDBStore()` / `SQLiteStore()` 通过 `Config.current() → get_path()` 无条件指向 `data/` 下的正式路径，且没有任何调用者验证自己运行在哪个环境下。
- 第二次 DuckDB 变异的 exact 测试/调用链**尚未追溯证明**；但该事实本身说明现有门禁不能防止意外写入。这意味着测试覆盖不足以保护正式数据，不是因为 DuckDB 存在 mmap 延迟写回 bug。
- 现有的 `--collect-only` 哈希守卫通过 subprocess 运行 pytest，但 pytest 的收集阶段仍然导入 Python 模块，模块级副作用已在第一次事故中证明可改变数据库。
- `DuckDB` 的 `read_only` 连接不被接受为充分保护：事故中测试本来就没有写成只读意图，问题在于测试本不应接触到正式路径。

本合约定义一层**强制路径策略层**，使所有数据库路径解析：

1. **在到达 mkdir / connect / PRAGMA 之前完成验证和拒绝。**
2. **正式、测试、staging、rebuild-source、forensic 各域互不相通。**
3. **任何环境变量缺失、错误、歧义在一个中央检查点统一失败。**
4. **pytest 无法通过任何代码路径访问正式路径。**

---

## 2. 环境合约（强制变量集）

### 2.1 核心环境标识

| 变量 | 合法值 | 默认 | 说明 |
|---|---|---|---|
| `VD_ENV` | `formal`, `test`, `staging` | **无默认，缺失为致命错误** | 运行环境标识。pytest 必须为 `test`；`formal` 禁止在 pytest 进程中出现。 |
| `VD_FORMAL_ACK` | `confirmed` | **无默认** | formal 访问确认令牌。**start.bat 不得自动设置此变量**；它仅检查变量值是否为 `confirmed`。若非 `confirmed` → 拒绝启动。pytest 内如果出现此变量（无论值是什么），`pytest_configure` 拒绝整个 session。 |

### 2.1a 正式数据根变量

S1 包装器向 Python 进程注入 `VD_FORMAL_DATA_ROOT` 环境变量，使 Python 端的 `resolve_and_validate_paths()` / `from_env()` 可以惰性读取正式数据根，而无需硬编码路径。这是外层包装器（PowerShell）与内层 Python 路径策略之间的唯一定向合约。

| 变量 | 设定者 | 说明 |
|---|---|---|
| `VD_FORMAL_DATA_ROOT` | `s1-pytest.ps1` 包装器 | Python 进程启动前设置。值为**正式数据根的规范化绝对路径**（如 `D:\Mr.Q\掌控经济\value-dashboard\data`）。Python 端的 `path_policy.formal_data_root()` 将**只通过此环境变量**解析，不再硬编码 `_DATA_ROOT`。包装器从 `-FormalDataRoot` 参数获取此值；若省略，则默认为 `$PSScriptRoot\..\data`（脚本派生绝对路径）。 |

### 2.2 路径变量

所有 DB 路径变量（`VD_DUCKDB_PATH`, `VD_SQLITE_PATH`）在所有 profile（formal/test/staging）下均为**强制绝对路径**。

| 变量 | 适用环境 | 要求 |
|---|---|---|
| `VD_DUCKDB_PATH` | **所有 profile** | 绝对路径。formal = 指向正式 DuckDB；test = 指向 run root 下；staging = 指向 staging root 下 |
| `VD_SQLITE_PATH` | **所有 profile** | 绝对路径。同上 |
| `VD_TEST_RUN_ROOT` | test | 绝对路径，每次 pytest 运行的**唯一临时根目录**。必须**不存在**于进程启动时。两个 DB 文件置于此根下。 |
| `VD_STAGING_ROOT` | staging | 绝对路径，staging 副本根。必须位于 repo 外（S1 合约的最简安全规则）。 |
| `VD_FORENSIC_ROOTS` | (引擎拒绝) | 分号分隔的绝对路径列表。DB 引擎在任何环境下都拒绝写入或读取这些路径下的 DB 文件。Forensic **不是**可运行/只读 DB profile。 |
| `VD_REBUILD_SOURCE_ROOT` | (S1 拒绝) | 绝对路径，指向重建来源备份（正式数据库的已知正确副本）。S1 测试期间 DB 引擎拒绝任何对该路径的读写。 |
| `VD_TEST_EVIDENCE_ROOT` | test | 可选。显式非 DB 证据目录。包装器将哈希证据、日志、前后快照写入此目录而非 `data/`。缺省时使用默认路径 `docs/evidence-s1/<run-id>`。 |

### 2.3 路径约束

- 所有路径变量必须为**绝对 fully qualified path**。相对路径或空值为致命错误。
- `VD_TEST_RUN_ROOT` 在包装器启动和 `Preflight Before` 时必须不存在。路径策略本身从不创建目录：PolicyOnly 进程接受安全但尚不存在的根；Normal/Full 进程接受由包装器在 `Before` 成功后刚创建的根，并重新检查该根及其祖先无 reparse point。任何预先存在的根都由 `Preflight Before` 作为陈旧/不可信状态拒绝。
- `VD_STAGING_ROOT` 必须位于 repo 目录树外。
- partial / malformed / 含非法字符的路径 → 统一 `PathIsolationError`，不 fallback。

### 2.4 禁止行为

- **不得在任何变量缺失时以静默 fallback 选择 formal。** formal 要求显式 `VD_ENV=formal` + `VD_FORMAL_ACK=confirmed`。
- **不得从 pytest 自身自动推断环境。** 必须由外部包装器设置 `VD_ENV=test`。
- **formal 不得由"变量不存在"隐式选择。** 缺少 `VD_ENV` 是致命错误，不是 formal。
- **Forensic 不是可运行 profile。** 任何 `VD_ENV` 值都不能选择 forensic；forensic 路径只作为 deny 列表注册。`read_only` DB 连接不授予 forensic 访问权。
- **`start.bat`/formal launcher 不得自动设置任何环境变量。** 它仅检查所有必需的 formal 变量已由外部提供：`VD_ENV=formal`、`VD_FORMAL_ACK=confirmed`、`VD_DUCKDB_PATH`（绝对路径）、`VD_SQLITE_PATH`（绝对路径）。任一缺失或无效则拒绝启动，输出明确错误信息。

---

## 3. 中央策略模块设计

### 3.1 模块定位

**路径：** `app/core/storage/path_policy.py`  
**理由：** 与 `duckdb_store.py` / `sqlite_store.py` 同包，自然被 schema / store 层依赖，无需跨层导入。不是私有 magic（显式导入，显式调用）。

### 3.2 核心类型

所有设计级别的名称和签名如下（非实现代码）：

```
class VdEnv(str, Enum):
    FORMAL = "formal"
    TEST = "test"
    STAGING = "staging"


@dataclass(frozen=True)
class DatabasePathSet:
    duckdb_path: Path       # 规范化绝对路径
    sqlite_path: Path       # 规范化绝对路径
    run_root: Path          # 此组路径所在的运行根（formal=repo data/，test=test run root，staging=staging root）
    env: VdEnv              # VdEnv.FORMAL | VdEnv.TEST | VdEnv.STAGING（非字符串比较）


class PathIsolationError(Exception):
    """所有路径策略违规的统一异常类型。包含具体违反的规则和环境信息。"""
```

### 3.3 核心函数

| 函数 | 职责 | 调用时机 |
|---|---|---|
| `resolve_and_validate_paths(env: VdEnv | None = None) -> DatabasePathSet` | 读 `VD_ENV`（或显式枚举参数）；按环境读取对应变量集；先拒绝非法 Windows 语法，再规范化并运行反绕过检查；返回已验证的路径集。失败则抛出 `PathIsolationError`。该函数允许安全的 test run root 尚不存在或已由包装器创建；初始“不存在”证明由 `Preflight Before` 负责。 | root conftest / formal app factory；PowerShell wrapper 不导入 Python。 |
| `deny_paths(paths: list[Path], path_set: DatabasePathSet) -> None` | 检查给定路径是否属于任何被禁类别。 | store 构造前、文件 open 前。 |
| `assert_env_not(disallowed: set[str]) -> None` | root conftest 入口用。 | `conftest.py` 的 `pytest_configure`。 |
| `canonicalize_path(p: str | Path, *, require_absolute: bool = True) -> Path` | 先以原始字符串拒绝 drive-relative、device、UNC、ADS 和保留名；再从最近的已存在祖先开始物理解析，检查 reparse point，最后进行大小写归一化和尾部分隔符处理。不得在语法拒绝前调用 `resolve()`。 | 所有路径比较前。 |

### 3.4 消除零参数 fallback

**核心原则：Store 构造函数不再调用 `Config.current()` 解析 DB 路径，且只接受已验证的 `DatabasePathSet`，不提供 `db_path` 逃生口。**

```
class DuckDBStore:
    def __init__(self, *, paths: DatabasePathSet) -> None:
        # 唯一的构造路径：注入已验证的 DatabasePathSet
        # 不接受裸 db_path：所有路径必须经过中央策略验证
        # 使用 paths.duckdb_path

class SQLiteStore:
    def __init__(self, *, paths: DatabasePathSet) -> None:
        # 同上。验证必须在 mkdir 和 _init_wal() 之前完成。
```

`init_all_schema` 同样要求显式 stores 或 `DatabasePathSet`：

```
def init_all_schema(
    duckdb_store: DuckDBStore | None = None,
    sqlite_store: SQLiteStore | None = None,
    *,
    paths: DatabasePathSet | None = None,
) -> None:
    # 必须提供 stores 和/或 paths
    # 三者都 None → PathIsolationError
```

### 3.5 Config 扩展

`Config` 无法从 YAML 提供可操作的 DB 路径。所有 DB 路径必须通过 `DatabasePathSet` 注入。YAML 中的 `database` 键保留仅作为历史/参考记录，在运行时被完全忽略。

```
Config：
    _injected_paths: DatabasePathSet | None = None

    @classmethod
    def load_with_paths(cls, paths: DatabasePathSet) -> Config:
        # 设置 _injected_paths；YAML 中的 database 键被忽略（历史参考）

    @classmethod
    def load(cls, *, paths: DatabasePathSet) -> Config:
        # 替代旧的 Config.load()。要求提供 paths 参数。
        # 不再从 YAML 读取 database 路径。

    def get_path(self, *keys: str) -> Path:
        # 如果 keys 匹配 database.* 且 _injected_paths 非空，委托给它
        # 否则 → PathIsolationError（绝不从 YAML 回退）
```

**关键规则：**

- `Config.load()` 和 `Config.load_with_paths()` **都必须**接收已验证的 `DatabasePathSet`。没有无参数的重载。
- YAML 中的 `database` 键（如 `database.duckdb_path`、`database.sqlite_path`）可能保留在文件中作为历史参考，但 `Config.get_path()` **永远不**从中解析可操作路径。所有运行时的 DB 路径解析只通过 `DatabasePathSet`。
- **所有 profile（包括 formal）运行时均忽略 YAML 中的 database 键。** `Config.load(paths=resolve_and_validate_paths())` 和 `Config.load_with_paths(paths)` 都要求外部注入 `DatabasePathSet`。Formal 环境不例外。

### 3.6 策略流程（fail-closed）

```
[test wrapper]
  ├─ 拒绝已存在的 VD_FORMAL_ACK
  ├─ 设置 VD_ENV=test，并生成唯一、外部、尚不存在的 VD_TEST_RUN_ROOT 与 sibling DB 路径
  ├─ 验证 wrapper 自身静态自检通过 → 才启动第一个 Python 进程
  ↓
[第一个 Python 进程 — 纯路径策略]
  └─ test_path_isolation.py 只导入 path_policy + stdlib + pytest
     └─ 不导入 app.core.storage 下的任何 store / duckdb / sqlite3
     └─ resolve_and_validate_paths() 正常返回
  └─ wrapper 捕获前后哈希
  ↓
[后续 Python 进程 — DB 测试]
  └─ wrapper 在 Preflight Before 通过后创建 VD_TEST_RUN_ROOT
  └─ root conftest 仅调用 resolve_and_validate_paths() 并捕获防御纵深证据，不导入 Config/Store
  └─ app/CLI 工厂随后用已验证路径加载 Config，并把同一 DatabasePathSet 注入 Store
  └─ Store 构造函数接收 DatabasePathSet
  └─ schema init / 所有操作使用已验证路径

[formal wrapper / start.bat]
  ├─ 验证所有必需变量已由外部提供：VD_ENV=formal、VD_FORMAL_ACK=confirmed、VD_DUCKDB_PATH（绝对）、VD_SQLITE_PATH（绝对）
  ├─ 不自动设置任何变量（缺失 → 拒绝启动，输出明确错误）
  ├─ Config.load(paths=resolve_and_validate_paths()) → 验证路径在正式域内
  └─ 注入 DatabasePathSet → Store 等
```

---

## 4. Windows 路径反绕过规则

以下规则由 `resolve_and_validate_paths` 和 `canonicalize_path` 共同实施。适用于每个提交给策略层的路径。

### 4.1 规范化

| 规则 | 说明 |
|---|---|
| 绝对 fully qualified | 拒绝 `data\foo.duckdb`、`C:data\foo.duckdb`（drive-relative） |
| 分隔符统一 | `\` → `/` 内部表示归一化 |
| `.` / `..` 展开 | `X\..\Y` → `Y` 然后 `resolve()` |
| case-insensitive 比较 | `os.path.normcase()` / `.casefold()` 后的 `==` / `in` 检查 |
| 拒绝设备路径 | `\\?\`、`\\.\` 开头（NT 命名空间路径） |
| 拒绝 UNC / 网络共享 | `\\server\share`、`\\?\UNC\` — S1 一律拒绝 |
| 拒绝 alternate data streams | 路径含冒号（驱动器字母后的 `C:` 除外） |
| 拒绝 reserved names 作为 component | `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9` |

### 4.2 解析与非存在叶节点处理

| 规则 | 说明 |
|---|---|
| 从最深层已存在祖先开始向上解析 | 如果文件不存在，找到最近已存在的父目录，对这个父目录执行所有 reparse/symlink 检查 |
| 检查每个已存在 component 的 `ReparsePoint` 属性 | 使用 `GetFileAttributesW` + `FILE_ATTRIBUTE_REPARSE_POINT`，或等效 Python |
| 拒绝 reparse point **在 run root 及其任何已存在祖先中** | junction / symlink / mount point 全部拒绝 |
| 创建 run root 后**立即 re-check** | 刚创建的目录 + 其所有祖先 |
| 在 store 构造函数 open 文件前**再次 re-check** | 目标文件父目录 + 所有祖先 |
| 非存在的叶节点 | 验证最近的已存在祖先无 reparse point，再检查每个计划创建的子组件 |
| 同一卷/根规则 | DuckDB 和 SQLite 文件必须在同一物理卷下 |
| TOCTOU 缓解 | 尽量缩短检查-使用窗口；关键检查在 open 前紧耦合执行 |

### 4.3 拒绝规则（修正：精确界定 descendants，不泛化 parent）

| 拒绝类别 | 具体规则 |
|---|---|
| 精确正式文件 | `data/valuedashboard.duckdb`、`data/valuedashboard.sqlite` |
| 正式文件 sidecars | `data/valuedashboard.duckdb.wal`、`data/valuedashboard.sqlite-wal`、`data/valuedashboard.sqlite-shm` |
| 仓库 `data/` 目录及其所有 descendants | 整个 `data/` 树（含备份、credentials、PDF 等）作为 test/staging 的 DB 根完全拒绝。**例外（非 DB）：** 包装器以纯文本 `Get-Content` 读取 `data/.hashes`（仅读取，不修改）。 |
| `data/backup/` | 整个备份目录树。Formal profile 下的 `BackupManager` 允许写此路径；test/staging 拒绝。 |
| 所有已注册 forensic 根及其 descendants | `VD_FORENSIC_ROOTS` 中的每一条 |
| `VD_REBUILD_SOURCE_ROOT` 及其 descendants | S1 测试期间拒绝 DB 引擎读写 |
| 仓库根目录（repo root）及其 descendants | repo root 本身及所有子目录作为 test/staging 的 DB 根拒绝。**不拒绝 repo root 的 parent 目录**（那会错误阻断 sibling 项目）。 |
| 生成目录（作为 DB 根） | `dist/`、`node_modules/`、`frontend/dist/` |
| 商业原始数据 | `_legacy/third_party_data/` 等 |
| credentials 目录 | `data/.credentials/` |

### 4.4 重叠检测

对任意一对路径（A, B），如果以下任何条件成立，则拒绝：

- A 的规范化路径 == B 的规范化路径（case-insensitive ignore） — 同一文件。
- A 是 B 的祖先（规范化后 A 是 B 的前缀） — 父/子重叠。
- A 和 B 都指向**已存在的同一文件**（Windows file ID: `FileIndexHigh` + `FileIndexLow` + `VolumeSerialNumber` 相同）。
- A 和 B 的规范化目标相等但原始大小写/分隔符不同 — case alias。
- A 或 B 是 junction / symlink，其解析目标与对方的规范化路径重叠 — symlink alias。

**硬链接保护（尽力而为到 fail-closed）：** 如果无法证明两个文件不同（文件身份信息不可读），且冲突无法被排除（例如路径在拒绝列表中），则以最安全方式拒绝。

### 4.5 配对完整性

同一 `DatabasePathSet` 中的 DuckDB 和 SQLite 文件必须：

- 共享同一个 `run_root`
- 不跨物理卷
- 是 sibling（在同一目录级别），由同一对变量设置
- 不能被拆到不同 profile（不允许一个 formal 一个 staging）

---

## 5. 配置注入与高阶类契约

### 5.1 Store 构造函数契约

```
class DuckDBStore:
    def __init__(self, *, paths: DatabasePathSet) -> None:
        # 唯一的构造路径：注入已验证的 DatabasePathSet
        # 无零参数 fallback，无裸 db_path 逃生口

class SQLiteStore:
    def __init__(self, *, paths: DatabasePathSet) -> None:
        # 同上。验证必须在 mkdir 和 _init_wal() 之前完成。
```

两个 Store 的构造函数**不再调用 `Config.current()`** 来获取 DB 路径。路径来源只有：
1. `DatabasePathSet` 注入

### 5.2 init_all_schema 契约

```
def init_all_schema(
    duckdb_store: DuckDBStore | None = None,
    sqlite_store: SQLiteStore | None = None,
    *,
    paths: DatabasePathSet | None = None,
) -> None:
    # stores 和/或 paths 必须提供。三者都 None → PathIsolationError
```

### 5.3 高阶类契约

所有当前零参数构造 Store 的高阶类必须接受 `DatabasePathSet` 注入，不再隐式依赖 Config：

```
class DataInitializer:
    def __init__(self, *, duck: DuckDBStore | None = None, sqlite: SQLiteStore | None = None,
                 paths: DatabasePathSet | None = None) -> None:

class PriceBackfiller:
    def __init__(self, *, duck: DuckDBStore | None = None, sqlite: SQLiteStore | None = None,
                 paths: DatabasePathSet | None = None) -> None:

class IncrementalUpdater:
    def __init__(self, *, duck: DuckDBStore | None = None, sqlite: SQLiteStore | None = None,
                 paths: DatabasePathSet | None = None) -> None:

class IndicatorCalculator:
    # 已有 duck/sqlite 可选参数；添加 paths 参数和域验证
    def __init__(self, duck: DuckDBStore | None = None, sqlite: SQLiteStore | None = None,
                 *, paths: DatabasePathSet | None = None) -> None:

class DSLEngine:
    def __init__(self, *, duck: DuckDBStore | None = None,
                 registry: ExpressionRegistry | None = None,
                 paths: DatabasePathSet | None = None) -> None:

class ExpressionRegistry:
    def __init__(self, *, sqlite: SQLiteStore | None = None,
                 paths: DatabasePathSet | None = None) -> None:

class Validator:   # app/core/dsl/validator.py
    def __init__(self, registry: ExpressionRegistry | None = None,
                 *, sqlite: SQLiteStore | None = None,
                 paths: DatabasePathSet | None = None) -> None:

class ScreeningEngine:
    def __init__(self, *, duck: DuckDBStore | None = None,
                 paths: DatabasePathSet | None = None) -> None:

class PDFManager:
    def __init__(self, *, duck: DuckDBStore | None = None, sqlite: SQLiteStore | None = None,
                 paths: DatabasePathSet | None = None) -> None:

class PDFCorrectionEngine:   # app/core/pdf/correction.py
    def __init__(self, *, duck: DuckDBStore | None = None, sqlite: SQLiteStore | None = None,
                 paths: DatabasePathSet | None = None) -> None:

class BackupManager:
    def __init__(self, *, duck: DuckDBStore | None = None, sqlite: SQLiteStore | None = None,
                 paths: DatabasePathSet | None = None) -> None:
        # Test/staging: backup_dir 必须在 VD_TEST_RUN_ROOT 或 VD_STAGING_ROOT 下
        # Formal: backup_dir 默认 data/backup，仅当 VD_ENV=formal + VD_FORMAL_ACK=confirmed 时允许
        # 在 S1 test 中，不得读取 VD_REBUILD_SOURCE_ROOT
```

### 5.4 API 路由与 CLI 工厂契约

所有当前在路由处理器内零参数构造 Store 的 FastAPI 端点改为从应用启动时初始化的工厂/`app.state` 获取 stores。

```
# app/web/api/*.py: 路由函数通过 request.app.state.duck / .sqlite 获取
# app/cli/main.py: 所有命令通过 centralized AppContext 或 factory 函数获取
# app/cli/protocol.py: 所有内联 SQLiteStore() 改为注入
```

**便捷方案（S1 实现建议）：** 在 `create_app()` 中创建一次 `DatabasePathSet` 和 Store 实例，注册到 `app.state`。

**长期方案：** 依赖注入框架（如 FastAPI `Depends` + 自定义 provider）。

---

## 6. SQLite 特有规则

SQLiteStore 的构造函数当前执行：

```python
self._db_path.parent.mkdir(parents=True, exist_ok=True)
self._init_wal()  # conn = sqlite3.connect → PRAGMA 执行
```

这意味着**路径验证必须在其 mkdir 和 connect 之前完成**。策略层必须在 SQLiteStore 实例化前验证路径。设计约束：

- `resolve_and_validate_paths()` 在 store 构造前调用。
- 如果路径不合法，`PathIsolationError` 阻止代码到达 `__init__`。
- 构造函数验证 `self._db_path` 属于已注入的 `DatabasePathSet`（作为 defense-in-depth）。

---

## 7. DuckDB 特有规则

DuckDBStore 当前：

```python
self._db_path.parent.mkdir(parents=True, exist_ok=True)
```

- `read_only` 连接不被接受为充分保护。两次事故显示测试本不应接触到正式路径；问题不在于 DuckDB 的行为，而在于测试覆盖和安全边界缺失。
- 路径验证必须在 mkdir 前完成。
- `duckdb.connect(denied_path, read_only=True)` 必须被策略层阻止，不依赖 DuckDB 的内部行为。

---

## 8. pytest 合约

### 8.1 外部 PowerShell 包装器（主门禁）

两个脚本。第一个在第一个 Python 进程启动前运行，验证环境本身：

**`scripts/s1-path-preflight.ps1`** — 纯 PowerShell 状态捕获。支持 `Before` 和 `After` 两个阶段：

- **`-FormalDataRoot`** 可选参数。指定正式数据根路径（包含 `valuedashboard.duckdb` 和 `valuedashboard.sqlite` 的目录）。若省略，则默认使用 `$PSScriptRoot\..\data`（脚本派生绝对路径）。在脱离 worktree 中，由于 worktree 的 `data/` 下不含正式 DB 文件，省略此参数将导致 Before 阶段失败；调用方必须显式传递主仓库的正式数据根。
- **Before 阶段**（`-Phase Before`）：验证环境并捕获执行前状态。运行根必须不存在。
- **After 阶段**（`-Phase After`）：仅捕获执行后状态并返回。不假设运行根状态（可能已由包装器创建）。

```
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Before", "After")]
    [string]$Phase,
    [string]$EvidenceDir = "docs/evidence-s1"
)
```

**Before 阶段行为：**
1. 确认 `VD_ENV=test` 已设置。
2. 确认 `VD_DUCKDB_PATH` 和 `VD_SQLITE_PATH` 已设置且为绝对路径。
3. 确认 `VD_TEST_RUN_ROOT` 已设置且**当前不存在**。如发现过期残留，记录路径并退出 1；不得自动删除后继续。
4. 确认 `VD_FORMAL_ACK` **不**存在（如果存在 → 退出 1，因为 pytest 内禁止 formal）。
5. 确认 `VD_TEST_EVIDENCE_ROOT` 或使用默认 `docs/evidence-s1/<run-id>`。
6. 捕获正式 5 文件集（DuckDB、SQLite、3 sidecars）的 exists/length/SHA256 → 证据目录 pre/。
   - Sidecar 不存在时记录 exists=false, length=null, sha256=null。
7. 以上证据写入**证据目录**，**不写入 `data/.hashes`**。
8. 静检通过 → 退出码 0（调用方决定是否启动 Python）。

**After 阶段行为：**
1. 重新捕获正式 5 文件集的 exists/length/SHA256 → 证据目录 post/。
2. 只有 5 个状态均捕获成功时退出码为 0；任何读取/哈希失败都写入 capture-failure 证据并非零退出。

**`scripts/s1-pytest.ps1`** — pytest 执行包装器，支持 `-PolicyOnly` 开关：

```
param(
    [switch]$PolicyOnly,
    [string]$FormalDataRoot = "",
    [string]$EvidenceDir = "docs/evidence-s1",
    [switch]$PreflightOnly,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)
```

**两种模式共同的启动步骤：**
1. 确认当前目录是仓库根，并确认 `VD_FORMAL_ACK` 不存在；若存在则拒绝，不得清除后继续。
2. 设置 `VD_FORMAL_DATA_ROOT`（从 `-FormalDataRoot` 或默认 `$PSScriptRoot\..\data` 解析）。此环境变量是 Python 端的唯一正式根来源；Python `path_policy.formal_data_root()` 将只通过 `os.environ["VD_FORMAL_DATA_ROOT"]` 惰性解析，不再硬编码 `_DATA_ROOT` 常量。生产代码中不存在硬编码主仓库路径。
3. 生成唯一 run ID、外部且尚不存在的 `VD_TEST_RUN_ROOT`，并将 `VD_DUCKDB_PATH`/`VD_SQLITE_PATH` 设置为该根下的 sibling 文件。
4. 设置 `VD_ENV=test` 和本次唯一的 `VD_TEST_EVIDENCE_ROOT`。这些是 test wrapper 变量，不是 formal gate 变量。
5. 调用 `Preflight Before`。若发现 run root 已存在，不得自动删除；保留现场并失败。

**PolicyOnly 模式**（`-PolicyOnly`，运行根始终不存在）：
1. **Preflight Before** 捕获状态 → 验证运行根不存在。
2. 在 F 阶段迁移 `tests/conftest.py` 之前，固定运行 `python -m pytest --noconftest tests/regression/test_path_isolation.py -v --tb=short`。`--noconftest` 是 PolicyOnly 专用的 bootstrap 边界，防止 pytest 导入当前仍含 Store 导入的 `tests/conftest.py`；此模式不接受调用者追加/替换测试路径。
3. `try`/`finally`：**Preflight After** 捕获状态。
4. 比较 Before vs After 的 exists/length/SHA256。任何 delta → exit 99。否则保留 pytest 退出码。
5. 不创建 VD_TEST_RUN_ROOT。

**Normal 模式**（无 `-PolicyOnly`，完整 DB/schema 测试）：
1. **Preflight Before** 捕获状态 → 验证运行根不存在。
2. 创建 `VD_TEST_RUN_ROOT`（权威证明：运行前不存在，现在创建），随即重新检查目录及祖先无 reparse point。
3. 拒绝调用者提供 `--basetemp`，由 wrapper 固定追加 `--basetemp <VD_TEST_RUN_ROOT>\pytest-tmp`，再运行 `python -m pytest @PytestArgs`（含 DB/schema/regression 测试）。因此 pytest 内置 `tmp_path` 也位于受控 run root 下。
4. `try`/`finally`：**Preflight After** 捕获状态。
5. 比较 Before vs After 的 exists/length/SHA256。任何 delta → exit 99（优先于 pytest 退出码）。否则保留 pytest 退出码。
6. 全部通过（exit 0 + 无 delta）→ 删除 `VD_TEST_RUN_ROOT`（证据目录保留）。
7. 任何失败 → 保留 `VD_TEST_RUN_ROOT` + 证据目录 → 退出码保持（delta 时 99）。

如果 `Preflight After` 本身无法完整捕获任一正式文件状态，包装器不得把它当成“无 delta”；应写入 capture-failure 证据、保留运行根并以非零安全退出码失败。

**包装器是 Python 进程的边界守卫。它不是递归 subprocess 测试，其正确性不由被测 Python 测试验证。** PolicyOnly 的 `--noconftest` 不构成通用 pytest 绕过：它只能由包装器对固定的纯策略文件使用；F 完成后的 collect-only、定向和完整回归全部使用 Normal 模式并加载 root/tests conftest。

### 8.2 root conftest 增强（Python 防御纵深）

`conftest.py`（仓库根目录）扩展为：

```python
def pytest_configure(config):
    """验证测试环境变量和路径隔离。仅导入 path_policy 和 stdlib。无 Config/Store/业务导入。"""
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

    # 内层防御纵深：捕获会话前正式哈希（纯文件 I/O，无 Config/Store）
    _capture_inner_evidence(config, phase="pre")
```

```python
def pytest_unconfigure(config):
    """防御纵深：在全部 teardown 后捕获正式文件哈希。
    不使用 DB 引擎（纯文件 I/O，逐块 hashlib 更新）。证据写入 VD_TEST_EVIDENCE_ROOT（非 data/）。
    不在此断言（wrapper 是权威门禁）。
    """
    _capture_inner_evidence(config, phase="post")
```

### 8.3 测试 fixture

Normal wrapper 通过 `--basetemp <VD_TEST_RUN_ROOT>\pytest-tmp` 把 pytest 内置 `tmp_path` 固定到受控 run root 下。`tests/conftest.py` 保留内置 `tmp_path` 语义，但先断言其 canonical path 位于 `VD_TEST_RUN_ROOT` 内，再用同一 `tmp_path` 下的 sibling DB 文件构造 `DatabasePathSet`。所有 Store 构造使用 `DuckDBStore(paths=path_set)` 和 `SQLiteStore(paths=path_set)`。

### 8.4 现有测试的修改

| 测试文件 | 变更 |
|---|---|
| `test_collection_safety.py` | **保留并增强作为防御纵深**：保留 `test_pytest_discovers_only_regression_tests`（pyproject testpaths check）和 `TestRootConftestExists`（conftest 连线检查）。**不**移除 AST guard——将其扩展以捕获更多模式（赋值、间接导入、非模块级调用的危险导入）。`KNOWN_DANGEROUS_MUTATORS` 扩大到包含 `Config.current`、`DuckDBStore`、`SQLiteStore` 的任何模块级出现。**移除**递归 subprocess 的 `test_collect_only_does_not_modify_production_databases`（由包装器覆盖）。 |
| `tests/regression/*` | 无变更（已使用 conftest fixture）。 |

### 8.5 新增测试

| 测试文件 | 内容 |
|---|---|
| `tests/regression/test_path_isolation.py` | 路径策略的**纯单元测试**（不导入 `app.core.storage` 中的任何 store，不导入 `duckdb`/`sqlite3`）：规范化、deny 规则、Windows 反绕过、环境变量缺失/错误、重叠检测、case variants、配对完整性 |
| `tests/regression/test_hash_preservation.py` | 哈希完整性逻辑测试：使用**隔离合成哨兵文件**（非正式文件），签名→模拟状态变化→检测差异（纯 comparator 测试） |

---

## 9. 一次性测试生命周期

### 9.1 包装器生命周期（Before/After/PolicyOnly/Full 模式）

preflight 支持两种调用模式：
- `Before` — 验证环境、捕获前状态。强制要求运行根不存在。
- `After` — 重新捕获状态、比较前后。运行根可能存在（full 模式在 After 前由包装器创建）。

包装器通过 `-PolicyOnly` 开关区分模式。

```
[Preflight Before — 包装器启动时调用]
  1. 包装器生成唯一外部 run_root（VD_TEST_RUN_ROOT）— 必须不存在于进程启动时
  2. 包装器验证 run_root 不存在 → 否则退出 1（这是权威证明：运行前根不存在）
  3. 验证 VD_ENV=test、VD_DUCKDB_PATH、VD_SQLITE_PATH 已设置且为绝对路径
  4. 验证 VD_FORMAL_ACK 不存在（pytest 禁止 formal）
  5. 捕获正式 5 文件集（DuckDB、SQLite、3 sidecars）的 exists/length/SHA256 → 证据目录 pre/
  6. 启动 Python 前的所有静态检查通过

[模式选择]

  模式 A — PolicyOnly（-PolicyOnly 开关，运行根始终不存在）：
  7a. 固定运行 python -m pytest --noconftest tests/regression/test_path_isolation.py（纯路径策略）
  8a. 不加载 root/tests conftest，不导入 duckdb/sqlite3/Store/Config。仅 path_policy + stdlib + pytest
  9a. resolve_and_validate_paths() 允许运行根本不存在（验证父目录存在即可）
  10a. 运行根在整个 PolicyOnly 期间 NEVER created

  模式 B — Full（无 -PolicyOnly 开关，标准执行）：
  7b. 包装器创建 VD_TEST_RUN_ROOT（只有此时才创建）
  8b. wrapper 固定 --basetemp=<VD_TEST_RUN_ROOT>\pytest-tmp 后运行完整 pytest（含 DB/schema/regression 测试）
  9b. root conftest 的 pytest_configure 验证 VD_TEST_RUN_ROOT 存在（包装器已创建）
  10b. 测试 fixture 在 run root 内创建空的 DuckDB/SQLite 文件
  11b. schema 初始化在空文件上运行
  12b. 所有测试使用 run root 作为数据根
  13b. 测试结束后，pytest_unconfigure 捕获后状态（防御纵深）

[Preflight After — finally 块中始终执行]
  14. Preflight After 在不假设运行根状态下捕获 5 文件集的 exists/length/SHA256
  15. 比较 Before vs After。逐文件对比 exists/length/SHA256
  16. 任何 delta（包括 sidecar 变化）→ exit 99，保留运行根和证据目录
  17. 无 delta → 保留 pytest 退出码

[清理]
  18. 全部通过（pytest exit 0 + 无 delta）→ 删除运行根和 sidecars；证据目录保留
  19. 任何失败 → 保留运行根（如已创建）和证据目录 → 退出码保持（delta 时为 99，否则 pytest 退出码）
```

### 9.2 禁止操作

- **不得复制 formal/backup/forensic 数据到 test run root。**
- **所有测试 DB 是空文件 + schema 初始化产生的最小结构。**
- 需要预置数据的测试使用 `tests/fixtures/` 下的受 git 追踪的小文件，这些文件有签名的哈希。
- `BackupManager` 在 test/staging profile 下写入备份到 `VD_TEST_RUN_ROOT` 下的备份子目录，从不写入 `data/backup/`。
- `BackupManager` 在 S1 测试中不得读取 `VD_REBUILD_SOURCE_ROOT`（S1 只测试隔离性，不测试从重建源恢复）。

### 9.3 Staging 生命周期（S1 合约外）

Staging 候选测试在当前 S1 生命周期之外。它们仅在未来从批准备份创建 working copy，且：

- 使用 `VD_ENV=staging`
- `VD_STAGING_ROOT` 指向 repo 外的目录（S1 合约要求最简安全规则）
- 永远不直接接触 formal/backup/forensic/rebuild-source 路径

---

## 10. Exact 变更点清单

### 10.1 新增文件

| 文件 | 职责 | 分类 |
|---|---|---|
| `app/core/storage/path_policy.py` | 中央策略模块：`DatabasePathSet`, `resolve_and_validate_paths()`, `canonicalize_path()`, `deny_paths()`, `assert_env_not()` | 核心强制 |
| `scripts/s1-path-preflight.ps1` | PowerShell 预检：环境检查、首次哈希捕获、证据目录初始化 | 测试门禁 |
| `scripts/s1-pytest.ps1` | PowerShell 包装器：分阶段调用 pytest、前后哈希比较、清理/保留证据 | 测试门禁 |
| `tests/regression/test_path_isolation.py` | 纯路径策略单元测试（无 DB 导入） | 测试 |
| `tests/regression/test_hash_preservation.py` | 哈希完整性逻辑测试（合成哨兵文件，不动正式文件） | 测试 |

### 10.2 修改文件

| 文件 | 变更 | 分类 |
|---|---|---|
| `app/core/config.py` | 增加 `_injected_paths`、`load_with_paths()`；`load()` 要求显式 `paths`；`get_path()` 对 database 键仅委托到 `DatabasePathSet`，所有 profile 均忽略 YAML database 键 | 核心强制 |
| `app/core/storage/duckdb_store.py` | `__init__` 仅接受 `paths: DatabasePathSet`；零参数 → `PathIsolationError`；域验证 | 核心强制 |
| `app/core/storage/sqlite_store.py` | 同上；验证在 mkdir 和 _init_wal 之前 | 核心强制 |
| `app/core/storage/schema.py` | `init_all_schema()` 要求 stores 或 paths；零参数 → `PathIsolationError` | 核心强制 |
| `app/core/init.py` | `DataInitializer.__init__` 增加 `paths` 参数 | 依赖注入 |
| `app/core/backfill.py` | `PriceBackfiller.__init__` 增加 `paths` 参数 | 依赖注入 |
| `app/core/update.py` | `IncrementalUpdater.__init__` 增加 `paths` 参数 | 依赖注入 |
| `app/core/indicators/calculator.py` | `IndicatorCalculator.__init__` 增加 `paths` 参数 | 依赖注入 |
| `app/core/dsl/engine.py` | `DSLEngine.__init__` 增加 `paths` 参数 | 依赖注入 |
| `app/core/dsl/registry.py` | `ExpressionRegistry.__init__` 增加 `paths` 参数 | 依赖注入 |
| `app/core/dsl/validator.py` | 内联 `SQLiteStore()` 改为注入 | 依赖注入 |
| `app/core/screening/engine.py` | `ScreeningEngine.__init__` 增加 `paths` 参数 | 依赖注入 |
| `app/core/pdf/manager.py` | `PDFManager.__init__` 增加 `paths` 参数 | 依赖注入 |
| `app/core/pdf/correction.py` | 同上 | 依赖注入 |
| `app/core/data_quality.py` | `build_data_quality_status` 接受 store 参数 | 依赖注入 |
| `app/core/backup/manager.py` | `BackupManager.__init__` 增加 `paths` 参数；test/staging 备份输出在 run root 下；formal 允许 data/backup 仅当 `VD_ENV=formal` + `VD_FORMAL_ACK=confirmed`；S1 拒绝读取 `VD_REBUILD_SOURCE_ROOT` | 依赖注入 |
| `app/cli/protocol.py` | 内联 `SQLiteStore()` 改为注入 | 依赖注入 |
| `app/cli/main.py` | 所有内联 Store 构造改为通过 factory 或 AppContext | 依赖注入 |
| `app/web/main.py` | `create_app()` / `run_server()` 使用 `load_with_paths()`；注册 store 到 app.state | 依赖注入 |
| `app/web/api/*.py` | 路由处理函数从 app.state 获取 store | 依赖注入 |
| `conftest.py` | 增加纯 `pytest_configure` 环境/路径验证与前哈希证据、`pytest_unconfigure` 后哈希证据；不得导入 Config/Store/业务模块 | 测试门禁 |
| `tests/conftest.py` | 保留 pytest 内置 `tmp_path`，断言 wrapper 的 `--basetemp` 已将其绑定到 `VD_TEST_RUN_ROOT`；fixture 构造 sibling DB 路径并经过路径域验证 | 测试门禁 |
| `tests/regression/test_collection_safety.py` | 保留并增强 AST 守卫作为防御纵深；移除递归 subprocess 的 collect-only 测试 | 测试门禁 |
| `pyproject.toml` | `testpaths` 保留 `["tests/regression"]`；无特殊 ignore | 测试门禁 |
| `start.bat` | 验证所有必需 formal 变量已由外部提供：`VD_ENV=formal`、`VD_FORMAL_ACK=confirmed`、`VD_DUCKDB_PATH`（绝对）、`VD_SQLITE_PATH`（绝对）；不自动设置任何变量；任一缺失/无效 → 拒绝启动 | 部署 |

### 10.3 分类总览

| 类别 | 文件数 | 说明 |
|---|---|---|
| 核心强制 | 5 | `path_policy.py`, `config.py`, 两个 store, `schema.py` |
| 依赖注入 | 17 | 所有高阶类、API 路由、CLI、备份、validator、protocol |
| 测试门禁 | 7 | 2 个 wrapper 脚本, root/tests conftest, collection_safety AST 守卫, pyproject, 路径策略测试, 哈希测试 |
| 部署 | 1 | `start.bat` formal 入口 |

---

## 11. 测试矩阵和执行顺序

### 11.1 执行顺序（严格分阶段）

```
PolicyOnly 模式（运行根始终不存在）：
  Preflight Before:
    0.0  验证脚本自身静态自检
    0.1  确认 VD_ENV=test, VD_DUCKDB_PATH, VD_SQLITE_PATH 已设置且为绝对路径
    0.2  确认 VD_FORMAL_ACK 不存在（pytest 禁止 formal）
    0.3  生成 VD_TEST_RUN_ROOT（唯一、外部、不存在）— 不创建
    0.4  捕获正式 5 文件集的 exist/length/SHA256 → 证据目录 pre/
    0.5  自检通过 → 启动 Python（PolicyOnly 模式）

  Python — 纯路径策略（无 DB 导入）：
  仅运行 test_path_isolation.py。不导入 app.core.storage/duckdb/sqlite3。
  1. test_path_policy::test_environment_missing_fatal
  2. test_path_policy::test_formal_requires_ack
  3. test_path_policy::test_test_env_requires_run_root
  4. test_path_policy::test_canonicalize_windows_paths
  5. test_path_policy::test_reject_device_paths
  6. test_path_policy::test_reject_unc_paths
  7. test_path_policy::test_reject_alternate_data_streams
  8. test_path_policy::test_reject_drive_relative
  9. test_path_policy::test_resolve_reparse_point_rejection

  Preflight After（finally）:
    捕获正式 5 文件集的 exists/length/SHA256 → 证据目录 post/
    逐文件比较 pre/ vs post/ → 一致 → PASS（delta → exit 99）
    输出摘要 → 证据目录
    （VD_TEST_RUN_ROOT 始终未被创建）

Normal / Full 模式（有 DB/schema 测试）：
  Preflight Before:
    同上 0.0-0.5（运行根仍然不存在）

  Python — 路径策略第二阶段（包装器已创建 run root）：
    包装器创建 VD_TEST_RUN_ROOT
    运行 pytest（DB/schema/regression 测试）：
    10. test_store_constructors::test_duckdb_no_implicit_fallback
    11. test_store_constructors::test_sqlite_no_implicit_fallback
    12. test_store_constructors::test_deny_formal_path_in_test_env
    13. test_store_constructors::test_deny_backup_path_in_test_env
    14. test_store_constructors::test_deny_rebuild_source_in_s1
    15-18. test_windows_bypass（4 项）
    19-21. test_pair_integrity（3 项）
    22. test_schema_isolation::test_init_all_schema_on_empty_paths
    23-29. test_factories（7 项）
    30. 完整 tests/regression/ 回归套件

  Preflight After（finally）:
    捕获正式 5 文件集的 exist/length/SHA256 → 证据目录 post/
    逐文件比较 pre/ vs post/ → 一致 → PASS（delta → exit 99）
    输出摘要 → 证据目录

  清理：
    全部通过（pytest exit 0 + 无 delta）→ 删除运行根和 sidecars；证据保留
    任何失败 → 保留运行根和证据目录（delta 时 exit 99，否则 pytest 退出码）
```

### 11.2 测试属性

| 分类 | 计数（估计） | 性质 |
|---|---|---|
| 纯路径策略 | ~12 | 单元；无 DB 导入 |
| 构造函数 fail-closed | ~6 | 单元；验证 `PathIsolationError` |
| Windows 绕过 | ~8 | 单元；可能需要 mock |
| 配对完整性 | ~3 | 单元 |
| schema 隔离 | ~2 | 集成；空临时库 |
| 工厂注入 | ~10 | 集成；临时库 |
| 完整回归 | ~1（大测试集） | 系统 |
| 哈希证据 | ~3 | 单元；合成哨兵文件 |

---

## 12. Formal Hash／Sidecar 保全契约

### 12.1 哈希状态定义

监视以下文件的**存在性、长度、SHA256**（如存在）：

| 文件 | 说明 |
|---|---|
| `data/valuedashboard.duckdb` | 正式 DuckDB |
| `data/valuedashboard.sqlite` | 正式 SQLite |
| `data/valuedashboard.duckdb.wal` | DuckDB WAL sidecar（可能不存在） |
| `data/valuedashboard.sqlite-wal` | SQLite WAL sidecar（可能不存在） |
| `data/valuedashboard.sqlite-shm` | SQLite shared-memory sidecar（可能不存在） |

没有 `-wal2`/`-shm2`。

### 12.2 `data/.hashes` 内容（静态分层账本）

`data/.hashes` **不是**包装器的每次运行记录。它是一个 Git 控制的静态文件，通过独立的 S1 步骤创建/审查，包含：

- 重建来源（rebuild-source）的哈希（已知正确备份的 SHA256）
- 事故时的哈希（当前已偏离状态的 SHA256）
- 已发布正式种子的哈希（如果未来重建后更新则为最新值；初始为 null）
- 运行时观察到的空状态（如测试前不存在）

格式示例：

```
# data/.hashes — 静态哈希分层账本
# 由 S1 路径隔离实现步骤创建，非每次运行生成。
# rebuild-source: 已知正确的批准基线
rebuild-source:sha256:valuedashboard.duckdb = 46EBCEB6DDBCCA15D4E82D22CFA659EC1C593033DE190C08B5C54FC7211A3C91
rebuild-source:sha256:valuedashboard.sqlite  = 228E0F53A8EBD0B99DAED8FA2D683D42F46644E8805495350EC162EFEC6596D3
# incident: 事故发生后（当前已偏离状态）
incident:sha256:valuedashboard.duckdb      = 5186E660E603B277B72E4EAF9988963C64B25B83882BA5ACF4BE2789A51268D6
incident:sha256:valuedashboard.sqlite      = B7B5F2FF2D1B4D2F71512DFEBD8DC2FBD9625E51BE2D0BFDB5CAF0657EC11959
# released-formal-seed: 重建后签字发布的值 — 初始为 null
released-formal-seed:sha256:valuedashboard.duckdb = null
released-formal-seed:sha256:valuedashboard.sqlite = null
# runtime-observed: 包装器不写入。由独立的审计步骤捕获后人工审核决定是否更新。
```

### 12.3 包装器哈希记录（每次运行）

包装器将每次运行的哈希证据写入：

- `docs/evidence-s1/<run-id>/pre/` — 执行前的正式文件状态
- `docs/evidence-s1/<run-id>/post/` — 执行后的正式文件状态
- `docs/evidence-s1/<run-id>/summary.json` — 前后比较摘要

**包装器不写入 `data/.hashes`。**

### 12.4 Python 进程级别（防御纵深 — 非权威门禁）

- `pytest_configure`：记录 PID、开始时间、VD_ENV。
- `pytest_unconfigure`：在全部 teardown 后，**不使用 DB 引擎**，通过纯文件 I/O（`open` + `hashlib`）计算正式文件哈希。写入 `docs/evidence-s1/<run-id>/pytest-inner/post/`。
- Python 进程**不得写入 `data/` 下的任何文件**（包括 `data/.hashes`）。

### 12.5 哈希比对规则

- **只比较前后动态差异**：执行前的值 vs 执行后的值。
- **从不断言 formal 等于备份**（备份可能已在修复后更新）。
- **验收闸门：所有 formal before/after 状态必须完全一致。**
- 如果不一致 → exit 99 + 保留证据 + 输出差异（即使 pytest 也失败，formal-state delta 以 99 优先）。

---

## 13. 验收标准

### 13.1 必须通过（S1 gate）

| # | 标准 | 验证方式 |
|---|---|---|
| AC1 | 缺少 `VD_ENV` 时 `resolve_and_validate_paths()` 抛出 `PathIsolationError` | `test_environment_missing_fatal` |
| AC2 | `VD_ENV=test` 但缺少 `VD_TEST_RUN_ROOT` 时拒绝 | `test_test_env_requires_run_root` |
| AC3 | `VD_ENV=formal` 但缺少 `VD_FORMAL_ACK` 时拒绝 | `test_formal_requires_ack` |
| AC4 | `VD_FORMAL_ACK` 在 pytest 环境中出现时被拒绝 | `test_formal_ack_forbidden_in_pytest` |
| AC5 | 零参数 `DuckDBStore()` 抛出 `PathIsolationError` | `test_no_implicit_fallback` |
| AC6 | 零参数 `SQLiteStore()` 抛出 `PathIsolationError` | `test_no_implicit_fallback` |
| AC7 | 传递正式路径给 store 构造器在 test 环境抛出 `PathIsolationError` | `test_deny_formal_path_in_test_env` |
| AC8 | 传递 `data/backup/` 路径给 store 构造器在 test 环境抛出 `PathIsolationError` | `test_deny_backup_path_in_test_env` |
| AC9 | 传递 `VD_REBUILD_SOURCE_ROOT` 路径给 store 构造器在 S1 环境抛出 `PathIsolationError` | `test_deny_rebuild_source_in_s1` |
| AC10 | Windows 设备路径（`\\?\`）被拒绝 | `test_reject_device_paths` |
| AC11 | UNC 路径被拒绝 | `test_reject_unc_paths` |
| AC12 | 驱动器相对路径（`D:foo`）被拒绝 | `test_reject_drive_relative` |
| AC13 | alternate data stream 路径被拒绝 | `test_reject_ads` |
| AC14 | 大小写变体的路径重叠检测触发拒绝 | `test_case_variants` |
| AC15 | file-ID deny 逻辑可拒绝指向受保护文件的硬链接别名；测试只使用隔离的合成受保护哨兵，不对正式文件创建硬链接 | `test_hardlink_identity_with_synthetic_protected_file` |
| AC16 | 配对不完整（缺少 DuckDB 或 SQLite 变量）在 resolve 时被拒绝 | `test_pair_integrity` |
| AC17 | `run_server()` 使用 `Config.load_with_paths()` | 代码审查 |
| AC18 | 所有 CLI 命令通过注入的 store 工厂访问 DB | `test_factories::*` |
| AC19 | `scripts/s1-path-preflight.ps1` 在 Python 前捕获并验证哈希 | 脚本运行 |
| AC20 | `scripts/s1-pytest.ps1` 在完整套件执行前后捕获哈希，无变化 → exit 0 | 脚本运行 |
| AC21 | 完整 `tests/regression/` 通过包装器且所有 formal 文件不变 | 包装器运行 + AC20 |
| AC22 | 所有 API 路由通过 `app.state` 获取 store | 代码审查 + 测试 |
| AC23 | `start.bat` 验证所有必需 formal 变量已由外部提供（`VD_ENV=formal`、`VD_FORMAL_ACK=confirmed`、绝对 DB 路径），不自动设置任何变量 | 代码审查 |
| AC24 | Formal 环境显式解析/验证配对后注入，无缺失-env fallback | `test_formal_requires_explicit_paths` |
| AC25 | AST 静态守卫扩展以捕获更广泛的危险模式 | `test_collection_safety` 增强 |
| AC26 | 所有 formal before/after 哈希状态必须一致 | 包装器前后比较 + `pytest_unconfigure` 防御纵深 |
| AC27 | 所有者显式签署 S1 完井确认 | 人工 |

### 13.2 接受限制（S1 scope not covered）

| # | 限制 | 原因 |
|---|---|---|
| L1 | Staging 测试生命周期未实现 | 先决条件：S1 隔离 + 所有者批准 S3 |
| L2 | Forensics 工具链未定义 | 当前无 forensic 需求；forensic 路径已注册为 deny-only |
| L3 | 重建来源（rebuild-source）的 staging copy 操作未实现 | 先决条件：数据重建完成 + S1 通过 |
| L4 | 非 Windows 平台的路径规范化未测试 | 项目目标为 Windows 个人本地工具 |
| L5 | 性能影响未评估 | 路径验证在启动时一次完成，预期影响可忽略 |
| L6 | 现有 YAML 配置中的 database 键仍包含路径 | 所有 profile（包括 formal）运行时忽略 YAML database 键；路径仅由 `DatabasePathSet` 注入 |

---

## 14. 安全考虑与威胁模型

| 威胁 | 缓解 |
|---|---|
| 恶意环境变量注入 | 路径必须为绝对路径；中央策略验证所有输入；非法字符/格式导致 `PathIsolationError` |
| TOCTOU：检查后路径被替换为 symlink | 在 open 前重新检查 reparse point；尽量缩短检查-使用窗口 |
| TOCTOU：目录在创建后被替换为 reparse point | 创建后立即 re-check，且 store 构造器直接 open |
| Hardlink 绕过 | 使用 Windows file ID 比较检测 hardlink 同一性；无法确定时 fail-closed |
| Case 变体绕过 | 规范化中使用 `casefold()` 比较 |
| `\\?\` 绕过 | 显式 NT 命名空间前缀检查 |
| ADS 绕过 | 冒号（驱动器字母后除外）检测 |
| UNC 绕过 | `\\` 开头的非 `\\?\` 路径检测 |
| `VD_FORMAL_ACK` 被自动设置 | start.bat 不设置任何变量；所有 formal 变量必须由外部提供并验证 |
| pytest 通过 `--override-ini` 改变 testpaths | 环境变量始终优先；`pytest_configure` 检查 `VD_ENV=test` |
| Python 直接调用不经过 wrapper | root conftest 的 `pytest_configure` 检测缺失/错误的 `VD_ENV` → 拒绝 |
| 零参数 store 构造绕过程策略 | 构造函数不再调用 `Config.current()` 获取 DB 路径；零参数 → `PathIsolationError` |
| 未来重构者移除路径检查 | 中央策略模块被广泛依赖；从 Config 中消除零参数 fallback 使误用难以编译通过 |

---

## 15. 证据要求（S1 完成信号）

以下条目构成 S1 完成的**可审计证据**。所有条目必须在 remediation 分支上实现并提交（**不是 merge/push 到 main**）：

1. [ ] 设计合约文档由所有者签署为 APPROVED。
2. [ ] `app/core/storage/path_policy.py` 在 remediation 分支上实现并提交。
3. [ ] `scripts/s1-path-preflight.ps1` 在 remediation 分支上实现并提交。
4. [ ] `scripts/s1-pytest.ps1` 在 remediation 分支上实现并提交。
5. [ ] `data/.hashes` 分层账本文件存在且包含 rebuild-source、incident、released-formal-seed(null)、runtime-observed(empty)。
6. [ ] 所有 `test_path_isolation.py` 纯单元测试通过（阶段 1 — 无 DB 导入）。
7. [ ] 所有 `test_hash_preservation.py` 测试通过（合成哨兵文件 — 不动正式文件）。
8. [ ] preflight 模拟运行：设置 → 哈希捕获 → 自检通过 → 摘要输出正确（人工观察）。
9. [ ] 包装器模拟运行：preflight + pytest（纯策略阶段）→ 哈希一致 → PASS（人工观察）。
10. [ ] 完整 `tests/regression/` 通过包装器：所有测试通过，前后正式哈希一致。
11. [ ] evidence 目录 `docs/evidence-s1/<run-id>/` 包含 pre/、post/ 和 summary.json。
12. [ ] `pytest_unconfigure` 防御纵深日志存在于 evidence 目录中（无 DB 引擎）。
13. [ ] AST 静态守卫扩展后无假阳性/假阴性问题。
14. [ ] `start.bat` 代码审查确认 `VD_FORMAL_ACK` 只检查不自动设置。
15. [ ] `git diff --stat` 确认所有变更点已覆盖。
16. [ ] **所有者签署 S1 完井确认** — 确认所有 formal before/after 状态一致。

**S1 完成前不得验证门禁失败**（不使用故意改变正式文件来测试门禁）。hash 比较器的正确性通过与**隔离合成哨兵文件**的 comparator 测试验证。

**在以上所有证据到位且所有者签署前，S1 不算完成。正式门禁保持 BLOCK。**
