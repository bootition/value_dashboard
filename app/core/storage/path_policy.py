"""Database path policy — environment-based isolation for formal/test/staging."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


# str-Enum 有意为之：VdEnv 需要与配置文件里的纯字符串等价比较
class VdEnv(str, Enum):  # noqa: UP042
    FORMAL = "formal"
    TEST = "test"
    STAGING = "staging"


class PathIsolationError(Exception):
    pass


# Frozen binaries keep code/resources under _internal but must retain mutable
# formal data beside the launcher, not inside the bundle.
_PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[3]
)
_DATA_ROOT = _PROJECT_ROOT / "data"
_FORMAL_DUCKDB = _DATA_ROOT / "valuedashboard.duckdb"
_FORMAL_SQLITE = _DATA_ROOT / "valuedashboard.sqlite"


def project_root() -> Path:
    return _PROJECT_ROOT


def data_root() -> Path:
    return _DATA_ROOT


def canonicalize_path(path: str | Path, *, require_absolute: bool = True) -> Path:
    candidate = Path(path)
    if require_absolute and not candidate.is_absolute():
        raise PathIsolationError(f"Path must be absolute: {path}")
    return candidate.resolve(strict=False)


@dataclass(frozen=True, init=False)
class DatabasePathSet:
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
        if env is None or duckdb_path is None or sqlite_path is None or run_root is None:
            raise PathIsolationError(
                "DatabasePathSet requires env, duckdb_path, sqlite_path, and run_root"
            )
        if not isinstance(env, VdEnv):
            raise PathIsolationError(f"env must be a VdEnv value, got {env!r}")
        object.__setattr__(self, "env", env)
        object.__setattr__(self, "duckdb_path", Path(duckdb_path))
        object.__setattr__(self, "sqlite_path", Path(sqlite_path))
        object.__setattr__(self, "run_root", Path(run_root))

    @classmethod
    def from_env(cls) -> DatabasePathSet:
        """Create paths from environment variables.

        2026-08-14 红队 F4：冻结态（打包 exe）下未显式提供环境变量时，
        自动推导 exe 同级 `data/` 正式路径——发行版"双击即用"入口
        （docs/runbooks/user-first-use.md）此前直接抛 PathIsolationError。
        双击 exe 本身就是显式使用意图，冻结态豁免 VD_FORMAL_ACK。
        """
        env_raw = os.environ.get("VD_ENV")
        duckdb_raw = os.environ.get("VD_DUCKDB_PATH")
        sqlite_raw = os.environ.get("VD_SQLITE_PATH")

        frozen = getattr(sys, "frozen", False)
        if frozen:
            if env_raw is None:
                env_raw = VdEnv.FORMAL.value
            if env_raw == VdEnv.FORMAL.value:
                duckdb_raw = duckdb_raw or str(_FORMAL_DUCKDB)
                sqlite_raw = sqlite_raw or str(_FORMAL_SQLITE)

        missing = [
            name
            for name, value in (
                ("VD_ENV", env_raw),
                ("VD_DUCKDB_PATH", duckdb_raw),
                ("VD_SQLITE_PATH", sqlite_raw),
            )
            if not value
        ]
        if missing:
            raise PathIsolationError(f"Missing environment variables: {', '.join(missing)}")
        try:
            env = VdEnv(env_raw)
        except ValueError as error:
            raise PathIsolationError(f"Unknown VD_ENV value: {env_raw!r}") from error

        if env is VdEnv.TEST:
            root_raw = os.environ.get("VD_TEST_RUN_ROOT")
            if not root_raw:
                raise PathIsolationError("VD_TEST_RUN_ROOT is required for test profile")
        elif env is VdEnv.STAGING:
            root_raw = os.environ.get("VD_STAGING_ROOT")
            if not root_raw:
                raise PathIsolationError("VD_STAGING_ROOT is required for staging profile")
        else:
            # 冻结态豁免 ACK（F4：双击 exe 即显式意图）
            if not getattr(sys, "frozen", False) and os.environ.get("VD_FORMAL_ACK") != "confirmed":
                raise PathIsolationError("VD_FORMAL_ACK=confirmed is required for formal profile")
            root_raw = str(Path(duckdb_raw).parent)

        return cls(
            env=env,
            duckdb_path=Path(duckdb_raw),
            sqlite_path=Path(sqlite_raw),
            run_root=Path(root_raw),
        ).validate()

    def validate(self) -> DatabasePathSet:
        run_root = canonicalize_path(self.run_root)
        duckdb_path = canonicalize_path(self.duckdb_path)
        sqlite_path = canonicalize_path(self.sqlite_path)

        if duckdb_path.parent != run_root or sqlite_path.parent != run_root:
            raise PathIsolationError("DuckDB and SQLite paths must be siblings under run_root")
        if duckdb_path == sqlite_path:
            raise PathIsolationError("DuckDB and SQLite paths must be distinct sibling files")
        if duckdb_path.name != "valuedashboard.duckdb":
            raise PathIsolationError("DuckDB path must end with valuedashboard.duckdb")
        if sqlite_path.name != "valuedashboard.sqlite":
            raise PathIsolationError("SQLite path must end with valuedashboard.sqlite")

        if self.env in {VdEnv.TEST, VdEnv.STAGING}:
            project = canonicalize_path(_PROJECT_ROOT)
            if _is_inside(run_root, project):
                raise PathIsolationError(
                    "Test/staging database roots must be outside the repository"
                )
        else:
            # 2026-11-16（reports/147）：正式库允许两种「合法根目录」——
            #   ① 主检出（main worktree）的 `data/`；
            #   ② **同一个仓库的链接工作树**（git worktree）会话共用主检出的 `data/`。
            # 为什么必须放行 ②：`data/` 是 gitignore 的，链接工作树里根本没有自己的
            # `data/`。若不放行，worktree 里的会话**永远无法**执行任何维护脚本 ——
            # 而「多会话并行、没有主控」正是本项目的常态，等于把维护动作锁死在
            # 没人负责的主检出上。
            # 安全性由另外三层保证：duckdb/sqlite 文件名必须完全匹配（下面两条）、
            # `VD_FORMAL_ACK=confirmed` 显式确认、以及**写入前必须持有跨会话写租约**
            # （`scripts/session_lease.py`，preflight 会检查）。
            formal_root = canonicalize_path(_DATA_ROOT)
            allowed_roots = {formal_root, *shared_data_roots()}
            if run_root not in allowed_roots:
                raise PathIsolationError(
                    "Formal run_root must be the repository data directory "
                    "(or the shared data dir of this repository's worktrees)"
                )
            # 冻结态豁免 ACK（F4：双击 exe 即显式意图）
            if not getattr(sys, "frozen", False) and os.environ.get("VD_FORMAL_ACK") != "confirmed":
                raise PathIsolationError("VD_FORMAL_ACK=confirmed is required for formal profile")

        return _validated_path_set(self.env, duckdb_path, sqlite_path, run_root)


#: `shared_data_roots()` 的进程内缓存（2026-11-23 性能事故）。
#:
#: 事故现场：指标全量重算实测 **33–39 只/分钟**（5,575 只 ≈ 2.4 小时）。
#: py-spy 抓栈发现每只股票都卡在这里：
#:
#:     Popen._execute_child            ← 正在 spawn 子进程！
#:     shared_data_roots(path_policy)
#:     DatabasePathSet.validate
#:     SQLiteStore._revalidate
#:     SQLiteStore.connection / query
#:     IndicatorCalculator.compute_all_for_stock
#:
#: 也就是 **每一次 sqlite.query() 都 spawn 一个 `git rev-parse`**（Windows 上
#: 进程创建 50–150 ms），每只股票要查好几次 → 单股 1.5 s。
#: 这是纯开销：仓库根在一个进程的生命周期内不可能变。
#:
#: 缓存**不削弱任何安全语义**——路径隔离守卫每次都照跑（比较的还是这份算好的集合）；
#: 真要换仓库根，换的是进程，缓存自然跟着换。
#: 空结果也缓存：非 git 环境 / 没装 git 时若不缓存，每次查询都会再 spawn 一次失败。
_SHARED_ROOTS_CACHE: tuple[Path, ...] | None = None


def shared_data_roots() -> set[Path]:
    """同一仓库**所有工作树**共用的 `data/` 目录（reports/147）。

    从当前检出执行 `git rev-parse --git-common-dir`：
    - 主检出 → 返回 `<repo>/.git`，其父目录就是仓库根；
    - 链接工作树 → 同样返回**主检出**的 `.git`，因此得到主检出的 `data/`。

    取不到（非 git 仓库 / 没有 git）时返回空集合，调用方退回只看 `_DATA_ROOT`。

    ⚠️ **结果在进程内缓存**（见 `_SHARED_ROOTS_CACHE`）：本函数被
    `SQLiteStore._revalidate()` 在**每一次查询**时调用，不缓存等于每次查询
    spawn 一个 git 进程（实测把指标全量重算拖到 2.4 小时）。
    不加锁：GIL 下最坏情况是两个线程各多跑一次 git，结果相同、无副作用。
    """
    global _SHARED_ROOTS_CACHE
    cached = _SHARED_ROOTS_CACHE
    if cached is not None:
        return set(cached)
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            # 必须显式 UTF-8：Windows 默认按 cp936 解码，中文路径会变成
            # 「鎺屾帶缁忔祹」这类 mojibake，导致路径比对永远不相等。
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _SHARED_ROOTS_CACHE = ()
        return set()
    if completed.returncode != 0 or not completed.stdout.strip():
        _SHARED_ROOTS_CACHE = ()
        return set()
    git_dir = Path(completed.stdout.strip())
    if git_dir.name != ".git":
        # 裸仓库或异常布局：不做猜测
        _SHARED_ROOTS_CACHE = ()
        return set()
    root = canonicalize_path(git_dir.parent)
    roots = (canonicalize_path(root / "data"),)
    _SHARED_ROOTS_CACHE = roots
    return set(roots)


def _is_inside(child: Path, parent: Path) -> bool:
    child_str = os.path.normcase(str(child)).rstrip("\\/")
    parent_str = os.path.normcase(str(parent)).rstrip("\\/")
    return child_str == parent_str or child_str.startswith(parent_str + os.sep)


def _validated_path_set(
    env: VdEnv,
    duckdb_path: Path,
    sqlite_path: Path,
    run_root: Path,
) -> DatabasePathSet:
    instance = object.__new__(DatabasePathSet)
    object.__setattr__(instance, "env", env)
    object.__setattr__(instance, "duckdb_path", duckdb_path)
    object.__setattr__(instance, "sqlite_path", sqlite_path)
    object.__setattr__(instance, "run_root", run_root)
    return instance


def resolve_and_validate_paths(env: VdEnv | None = None) -> DatabasePathSet:
    paths = DatabasePathSet.from_env()
    if env is not None and paths.env is not env:
        raise PathIsolationError(f"Expected VD_ENV={env.value}, got {paths.env.value}")
    return paths


def require_formal_maintenance_paths() -> DatabasePathSet:
    """Return the explicit formal profile required by repository maintenance tools."""
    return resolve_and_validate_paths(VdEnv.FORMAL)
