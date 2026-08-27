"""配置加载器 - 读取 default.yaml 并合并 user.yaml 覆盖"""

import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from app.core.storage.path_policy import DatabasePathSet, PathIsolationError

# 开发模式: __file__ 的上三级目录
# 打包模式 (PyInstaller): _MEIPASS 是解压目录, sys.executable 是 exe 路径
_FROZEN = getattr(sys, "frozen", False)
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
if _FROZEN:
    # Frozen config is an immutable bundle resource; cwd never selects it.
    _BUNDLE_DIR = Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(sys.executable).parent
    _EXE_DIR = Path(sys.executable).resolve().parent
    _CONFIG_DIR = _BUNDLE_DIR / "config"
    _PROJECT_ROOT = _EXE_DIR
else:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    _CONFIG_DIR = _PROJECT_ROOT / "config"


def is_frozen_runtime() -> bool:
    """Return whether this process is executing from a frozen release."""
    return _FROZEN


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base，override 的值优先"""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


class Config:
    """全局配置单例，启动时加载一次"""

    _instance: "Config | None" = None
    _data: dict[str, Any]

    def __init__(
        self,
        data: dict[str, Any],
        *,
        paths: DatabasePathSet | None = None,
    ) -> None:
        if _FROZEN:
            data = _deep_merge(data, {"server": {"host": "127.0.0.1"}})
        server = data.get("server") if isinstance(data.get("server"), dict) else None
        if server is not None and server.get("host", "127.0.0.1") not in _LOOPBACK_HOSTS:
            raise PathIsolationError("server.host must be a loopback address")
        self._data = data
        self._paths = paths.validate() if paths is not None else None

    @classmethod
    def load(
        cls,
        config_dir: Path | None = None,
        *,
        paths: DatabasePathSet | None = None,
    ) -> "Config":
        """加载配置：先读 default.yaml，再用 user.yaml 覆盖"""
        cfg_dir = config_dir or _CONFIG_DIR
        if _FROZEN and cfg_dir.resolve() != _CONFIG_DIR.resolve():
            raise PathIsolationError("Frozen releases only load configuration from bundled resources")
        default_path = cfg_dir / "default.yaml"
        user_path = cfg_dir / "user.yaml"

        data: dict[str, Any] = {}
        if default_path.exists():
            with open(default_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        elif not _FROZEN and config_dir is None:
            # Installed wheels do not have a repository-level config directory.
            # The package resource is the supported default-config contract there.
            resource = files("app.resources").joinpath("config/default.yaml")
            data = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}

        if not _FROZEN and user_path.exists():
            with open(user_path, encoding="utf-8") as f:
                user_data = yaml.safe_load(f) or {}
            data = _deep_merge(data, user_data)

        cls._instance = cls(data, paths=paths)
        return cls._instance

    @classmethod
    def load_with_paths(
        cls,
        paths: DatabasePathSet,
        config_dir: Path | None = None,
    ) -> "Config":
        return cls.load(config_dir, paths=paths)

    @classmethod
    def current(cls) -> "Config":
        """获取已加载的配置单例"""
        if cls._instance is None:
            return cls.load()
        return cls._instance

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT

    def get_path(self, *keys: str) -> Path:
        """获取配置中的相对路径，返回绝对 Path"""
        if keys[:1] == ("database",):
            if self._paths is None:
                raise PathIsolationError(
                    "Database paths require an injected DatabasePathSet"
                )
            if keys == ("database", "duckdb_path"):
                return self._paths.duckdb_path
            if keys == ("database", "sqlite_path"):
                return self._paths.sqlite_path
            raise PathIsolationError(f"Unknown database path key: {keys!r}")
        raw: str = self._data
        for k in keys:
            raw = raw[k]
        return self.project_root / raw

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get_value(self, key: str, default: Any = None) -> Any:
        """获取配置值（实例方法）"""
        return self._data.get(key, default)
