"""Web 服务入口 - FastAPI 服务器"""

from __future__ import annotations

import logging
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import (
    DatabasePathSet,
    PathIsolationError,
    resolve_and_validate_paths,
)
from app.core.storage.schema import init_all_schema
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def create_app(
    *,
    paths: DatabasePathSet | None = None,
    config: Config | None = None,
    duck: DuckDBStore | None = None,
    sqlite: SQLiteStore | None = None,
) -> FastAPI:
    """创建 FastAPI 应用实例"""
    if paths is None:
        if config is not None or duck is not None or sqlite is not None:
            raise PathIsolationError("Injected web dependencies require explicit paths")
        paths = resolve_and_validate_paths()
    validated = paths.validate()
    cfg = config or Config.load_with_paths(paths)
    duck_store = duck or DuckDBStore(paths=paths)
    sqlite_store = sqlite or SQLiteStore(paths=paths)

    if cfg.get_path("database", "duckdb_path") != validated.duckdb_path:
        raise PathIsolationError("Web config DuckDB path does not match injected paths")
    if cfg.get_path("database", "sqlite_path") != validated.sqlite_path:
        raise PathIsolationError("Web config SQLite path does not match injected paths")
    if duck_store.db_path != validated.duckdb_path:
        raise PathIsolationError("Web DuckDB store does not match injected paths")
    if sqlite_store.db_path != validated.sqlite_path:
        raise PathIsolationError("Web SQLite store does not match injected paths")

    app = FastAPI(
        title="Value Dashboard",
        description="A股价值投资研究与筛选工具",
        version="0.1.0",
    )
    app.state.paths = paths
    app.state.config = cfg
    app.state.duck = duck_store
    app.state.sqlite = sqlite_store

    # ─── 注册 API 路由 ──────────────────────────────────────────────
    from app.web.api.data_status import router as data_status_router
    from app.web.api.screening import router as screening_router
    from app.web.api.stock_detail import router as stock_detail_router
    from app.web.api.watchlist import router as watchlist_router
    from app.web.api.dsl import router as dsl_router
    app.include_router(data_status_router)
    app.include_router(screening_router)
    app.include_router(stock_detail_router)
    app.include_router(watchlist_router)
    app.include_router(dsl_router)

    # ─── 健康检查 ──────────────────────────────────────────────────
    @app.get("/api/health")
    async def health_check() -> dict:
        return {
            "status": "ok",
            "version": "0.1.0",
            "config_loaded": True,
        }

    # ─── 数据库状态 ──────────────────────────────────────────────
    @app.get("/api/db/status")
    async def db_status(request: Request) -> dict:
        duck = request.app.state.duck
        sqlite = request.app.state.sqlite

        duck_ok = False
        sqlite_ok = False
        duck_tables: list[str] = []
        sqlite_tables: list[str] = []

        try:
            tables = duck.read_query(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            )
            duck_tables = [t["table_name"] for t in tables]
            duck_ok = True
        except Exception as e:
            logger.warning(f"DuckDB 状态检查失败: {e}")

        try:
            tables = sqlite.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            sqlite_tables = [t["name"] for t in tables]
            sqlite_ok = True
        except Exception as e:
            logger.warning(f"SQLite 状态检查失败: {e}")

        return {
            "duckdb": {
                "connected": duck_ok,
                "path": str(duck.db_path),
                "tables": duck_tables,
            },
            "sqlite": {
                "connected": sqlite_ok,
                "path": str(sqlite.db_path),
                "tables": sqlite_tables,
            },
        }

    # ─── 前端静态资源托管 ────────────────────────────────────────
    static_dir = cfg.project_root / "app" / "web" / "static"
    if static_dir.exists():
        # P2修复: 检查assets目录存在
        _assets_dir = static_dir / "assets"
        if _assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> HTMLResponse:
            """SPA fallback：所有非 API 路径返回 index.html"""
            index_path = static_dir / "index.html"
            if index_path.exists():
                return HTMLResponse(index_path.read_text(encoding="utf-8"))
            return HTMLResponse(
                "<h1>Value Dashboard</h1><p>前端尚未构建。请运行 <code>cd frontend && npm run build</code></p>",
                status_code=200,
            )
    else:
        @app.get("/")
        async def root() -> HTMLResponse:
            return HTMLResponse(
                "<h1>Value Dashboard</h1><p>前端尚未构建。请运行 <code>cd frontend && npm run build</code></p>",
            )

    return app


def _ensure_formal_env_vars() -> None:
    """Set default environment variables for formal (production) use.

    This is a convenience for users who run ``start.bat`` or ``python -m app.web.main``
    without manually configuring the path-isolation environment.  If the variables
    are already set (e.g. by tests or CI), this function does nothing.
    """
    import os

    if os.environ.get("VD_ENV"):
        return

    project_root = Path(__file__).resolve().parent.parent.parent
    os.environ["VD_ENV"] = "formal"
    os.environ["VD_DUCKDB_PATH"] = str(project_root / "data" / "valuedashboard.duckdb")
    os.environ["VD_SQLITE_PATH"] = str(project_root / "data" / "valuedashboard.sqlite")
    os.environ["VD_FORMAL_ACK"] = "confirmed"


def run_server() -> None:
    """启动 Web 服务器（一键启动入口）"""
    _ensure_formal_env_vars()
    paths = resolve_and_validate_paths()
    cfg = Config.load_with_paths(paths)
    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 初始化数据库 schema
    logger.info("正在初始化数据库...")
    init_all_schema(duckdb_store=duck, sqlite_store=sqlite)

    # PRD §7.3: 启动时执行简单增量检查
    try:
        from app.core.update import IncrementalUpdater
        updater = IncrementalUpdater(duck=duck, sqlite=sqlite)
        check_report = updater.run_incremental_check()
        if check_report["needs_update"]:
            logger.info(f"增量检查: 需要更新 (新交易日={len(check_report['new_trading_days'])}, "
                        f"待重试={len(check_report['retry_tasks'])})")
            logger.info("提示: 运行 'python -m app.cli.main data update' 执行增量更新")
        else:
            logger.info("增量检查: 数据已是最新")
    except Exception as e:
        logger.warning(f"增量检查失败(非致命): {e}")

    # 启动服务器
    server_cfg = cfg["server"]
    host = server_cfg["host"]
    port = server_cfg["port"]

    if server_cfg.get("open_browser", True):
        url = f"http://{host}:{port}"
        logger.info(f"将在浏览器打开 {url}")
        webbrowser.open(url)

    logger.info(f"Value Dashboard 启动中... http://{host}:{port}")
    app = create_app(paths=paths, config=cfg, duck=duck, sqlite=sqlite)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
