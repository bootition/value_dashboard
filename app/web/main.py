"""Web 服务入口 - FastAPI 服务器"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import Config, is_frozen_runtime
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import (
    DatabasePathSet,
    PathIsolationError,
    resolve_and_validate_paths,
)
from app.core.storage.schema import init_all_schema
from app.core.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# P1-6修复: 模块级互斥, 保证每个 app 实例的启动维护至多一个在运行
_STARTUP_MAINTENANCE_LOCK = threading.Lock()


def _server_host(server_config: dict) -> str:
    """The unauthenticated research service is always loopback-only."""
    if is_frozen_runtime():
        return "127.0.0.1"
    host = server_config.get("host", "127.0.0.1")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise PathIsolationError("server.host must be a loopback address")
    return host


def _run_startup_maintenance(
    duck: DuckDBStore,
    sqlite: SQLiteStore,
    startup_readiness: dict,
    readiness_cb=None,
) -> dict:
    """Run network-backed first-start work after the local web server binds."""
    from app.core.data_quality import minimum_data_readiness, store_cached_data_readiness

    current = startup_readiness
    initialization_error: str | None = None
    initialization_report: dict | None = None
    try:
        try:
            stock_rows = duck.read_query(
                "SELECT COUNT(*) AS count FROM stock_meta WHERE is_listed IS TRUE"
            )
            stock_count = int(stock_rows[0]["count"]) if stock_rows else 0
        except Exception:
            stock_count = 0
        if stock_count == 0:
            from app.core.init import DataInitializer

            logger.info("检测到空数据库，后台开始最小可用初始化...")
            initialization_report = DataInitializer(duck=duck, sqlite=sqlite).run_full_init()
            current = minimum_data_readiness(duck, sqlite)
            current["initialization"] = initialization_report
    except Exception as error:
        initialization_error = str(error)
        current = {
            "ready": False,
            "stock_count": 0,
            "missing": {"initialization": [str(error)]},
            "missing_counts": {"initialization": 1},
        }
        logger.warning("后台最小初始化失败: %s", error)

    try:
        current = store_cached_data_readiness(
            sqlite, minimum_data_readiness(duck, sqlite),
        )
        if initialization_report is not None:
            current["initialization"] = initialization_report
        if initialization_error:
            current.setdefault("missing", {})["initialization"] = [initialization_error]
            current.setdefault("missing_counts", {})["initialization"] = 1
            current["ready"] = False
        if readiness_cb is not None:
            readiness_cb(current)
        logger.info("后台数据就绪核对完成: ready=%s", current["ready"])
    except Exception as error:
        current = {
            **current,
            "checking": False,
            "readiness_error": str(error),
        }
        logger.warning("后台数据就绪核对失败: %s", error)
    try:
        from app.core.auto_update import AutoUpdateController

        controller = AutoUpdateController(duck=duck, sqlite=sqlite)
        if controller.status().get("enabled") and not controller.status().get("paused"):
            logger.info("启动后台自动更新（PRD §7.3）...")
            controller.run_once()
            logger.info("自动更新完成")
        else:
            logger.info("自动更新已关闭或暂停，跳过")
    except Exception as error:
        logger.warning("后台自动更新失败(非致命): %s", error)

    try:
        current = store_cached_data_readiness(
            sqlite, minimum_data_readiness(duck, sqlite),
        )
        if initialization_report is not None:
            current["initialization"] = initialization_report
        if readiness_cb is not None:
            readiness_cb(current)
    except Exception as error:
        logger.warning("自动更新后数据就绪复核失败(非致命): %s", error)

    # C8/C16修复(报告41): 启动时对有界操作表做 GC（过期 plan / 旧 job_logs /
    # 已解析 missing_list），幂等、保守、不涉及审计记录。
    try:
        from app.core.housekeeping import gc_operational_tables

        gc_operational_tables(sqlite)
    except Exception as error:
        logger.warning("运维清理失败(非致命): %s", error)
    return current


def _start_startup_maintenance(
    app: FastAPI, duck: DuckDBStore, sqlite: SQLiteStore, startup_readiness: dict,
) -> None:
    """Publish an immediately responsive server before slow remote work begins."""
    def worker() -> None:
        try:
            current = _run_startup_maintenance(
                duck,
                sqlite,
                startup_readiness,
                readiness_cb=lambda readiness: setattr(
                    app.state, "startup_readiness", readiness,
                ),
            )
        except Exception as error:
            logger.warning("启动维护线程失败: %s", error)
            app.state.startup_maintenance = {"status": "failed", "error": str(error)}
            return
        app.state.startup_readiness = current
        init_errors = current.get("missing", {}).get("initialization", [])
        if init_errors:
            app.state.startup_maintenance = {
                "status": "failed", "error": "; ".join(str(item) for item in init_errors),
            }
        else:
            app.state.startup_maintenance = {"status": "done", "error": None}

    with _STARTUP_MAINTENANCE_LOCK:
        state = getattr(app.state, "startup_maintenance", None) or {}
        if state.get("status") == "running":
            logger.info("启动维护已在运行，跳过重复启动")
            return
        app.state.startup_maintenance = {"status": "running", "error": None}
        threading.Thread(target=worker, name="vd-startup-maintenance", daemon=True).start()


def create_app(
    *,
    paths: DatabasePathSet | None = None,
    config: Config | None = None,
    duck: DuckDBStore | None = None,
    sqlite: SQLiteStore | None = None,
    startup_readiness: dict | None = None,
    start_maintenance_on_lifespan: bool = False,
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

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        if start_maintenance_on_lifespan:
            current = app_instance.state.startup_readiness
            _start_startup_maintenance(
                app_instance,
                app_instance.state.duck,
                app_instance.state.sqlite,
                current,
            )
        yield

    app = FastAPI(
        title="Value Dashboard",
        description="A股价值投资研究与筛选工具",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Loopback binding is not an origin boundary: reject hostile Host headers
    # and require this per-launch token for every browser-originated mutation.
    allowed_hosts = ["127.0.0.1", "localhost", "[::1]"]
    if validated.env.value in {"test", "staging"}:
        allowed_hosts.append("testserver")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.state.paths = paths
    app.state.config = cfg
    app.state.duck = duck_store
    app.state.sqlite = sqlite_store
    app.state.startup_readiness = startup_readiness
    app.state.startup_maintenance = {"status": "idle", "error": None}
    app.state.write_token = secrets.token_urlsafe(32)

    @app.middleware("http")
    async def require_local_write_token(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin:
                parsed_origin = urlsplit(origin)
                if parsed_origin.scheme != "http" or parsed_origin.netloc != request.headers.get("host", ""):
                    return JSONResponse(status_code=403, content={"detail": "cross-origin API write rejected"})
            if request.headers.get("x-vd-write-token") != request.app.state.write_token:
                return JSONResponse(status_code=403, content={"detail": "local write token required"})
        return await call_next(request)

    @app.get("/api/session")
    async def session_token(request: Request) -> dict[str, str | bool]:
        # C3(报告41): 暴露运行形态（打包版/开发版），前端据此显示正确 CLI 前缀
        return {
            "write_token": request.app.state.write_token,
            "packaged": is_frozen_runtime(),
        }

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
    async def health_check(request: Request) -> dict:
        """Report ready only after both profile databases accept a read probe."""
        try:
            request.app.state.duck.read_query("SELECT 1 AS ready")
            request.app.state.sqlite.query("SELECT 1 AS ready")
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail={"status": "unavailable", "error": str(error)},
            ) from error
        return {"status": "ok", "version": "0.1.0", "config_loaded": True}

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

        payload = {
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
        if not duck_ok or not sqlite_ok:
            raise HTTPException(status_code=503, detail=payload)
        return payload

    @app.get("/api/readiness")
    def readiness(request: Request) -> dict:
        current = request.app.state.startup_readiness
        if not isinstance(current, dict):
            from app.core.data_quality import checking_data_readiness
            current = checking_data_readiness()
        if not current["ready"]:
            raise HTTPException(status_code=503, detail=current)
        return current

    @app.get("/api/maintenance/status")
    def maintenance_status(request: Request) -> dict:
        """P1-6修复: 暴露启动维护的生命周期状态 (idle/running/done/failed)"""
        return getattr(
            request.app.state, "startup_maintenance", {"status": "idle", "error": None}
        )

    # ─── 前端静态资源托管 ────────────────────────────────────────
    static_dir = (
        Path(sys._MEIPASS) / "app" / "web" / "static"
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
        else cfg.project_root / "app" / "web" / "static"
    )
    if static_dir.exists():
        # P2修复: 检查assets目录存在
        _assets_dir = static_dir / "assets"
        if _assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

        @app.get("/favicon.svg", include_in_schema=False)
        async def serve_favicon() -> FileResponse:
            favicon_path = static_dir / "favicon.svg"
            if not favicon_path.exists():
                raise HTTPException(status_code=404, detail="favicon not found")
            return FileResponse(favicon_path, media_type="image/svg+xml")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> HTMLResponse:
            """Serve only client-side routes; missing files must remain 404s."""
            if Path(full_path).suffix:
                raise HTTPException(status_code=404, detail="static resource not found")
            # reports/76 P3-3: 未知 /api/* 必须返回 404 JSON，不得落入 SPA
            # 兜底返回 index.html（前端 fetch 未知接口时避免收到 HTML 200）。
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="API endpoint not found")
            index_path = static_dir / "index.html"
            if index_path.exists():
                return HTMLResponse(
                    index_path.read_text(encoding="utf-8"),
                    headers={"Cache-Control": "no-store"},
                )
            raise HTTPException(status_code=503, detail="frontend static bundle is incomplete")
    else:
        @app.get("/")
        def root() -> HTMLResponse:
            raise HTTPException(status_code=503, detail="frontend static bundle is unavailable")

    return app


def run_server() -> None:
    """启动 Web 服务器（一键启动入口）"""
    paths = resolve_and_validate_paths()
    cfg = Config.load_with_paths(paths)
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from app.core.backup.manager import recover_pending_restore

    # A previous process may have been killed between DuckDB and SQLite restore
    # commits. Complete its durable rollback before any schema writer opens.
    recover_pending_restore(paths)

    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)
    logger.info("正在初始化数据库...")
    # reports/79 方案 C：schema 已是最新版本时跳过幂等 DDL（正式库实测省 ~5s）
    init_all_schema(duckdb_store=duck, sqlite_store=sqlite, skip_if_current=True)

    from app.core.data_quality import checking_data_readiness, read_cached_data_readiness

    startup_readiness = read_cached_data_readiness(sqlite) or checking_data_readiness()

    # 启动服务器
    server_cfg = cfg["server"]
    host = _server_host(server_cfg)
    port = server_cfg["port"]

    if server_cfg.get("open_browser", True):
        url = f"http://{host}:{port}"
        logger.info(f"将在浏览器打开 {url}")

        # reports/79 方案 C / U5：等 uvicorn 绑定成功后再打开浏览器，
        # 消除"页面先于服务就绪"的竞态（历史偶发需手动刷新一次）。
        def _open_browser_late() -> None:
            webbrowser.open(url)

        browser_timer = threading.Timer(2.0, _open_browser_late)
        browser_timer.daemon = True
        browser_timer.start()

    logger.info(f"Value Dashboard 启动中... http://{host}:{port}")
    skip_maintenance = (
        paths.env.value in {"test", "staging"}
        and os.environ.get("VD_SKIP_STARTUP_MAINTENANCE") == "1"
    )
    app = create_app(
        paths=paths, config=cfg, duck=duck, sqlite=sqlite,
        startup_readiness=startup_readiness,
        start_maintenance_on_lifespan=not skip_maintenance,
    )
    if skip_maintenance:
        logger.info("隔离 profile 已显式跳过启动维护")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server()
