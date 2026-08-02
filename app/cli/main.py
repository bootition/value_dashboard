"""CLI 入口 - Typer 命令行工具

M0: server 启动 + schema 初始化
M1: data init (最小可用初始化)
M7: 完整命令树
"""

from __future__ import annotations

import json
from typing import Any
import typer


def _database_context(*, initialize: bool = True):
    """Create the CLI's database pair through the validated path boundary.

    Status and evidence commands must not mutate a formal profile merely to
    inspect it, so schema initialization and interrupted-restore recovery are
    reserved for commands that explicitly need a writable composition.
    """
    from app.core.config import Config
    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.path_policy import resolve_and_validate_paths
    from app.core.storage.sqlite_store import SQLiteStore

    paths = resolve_and_validate_paths()
    Config.load_with_paths(paths)
    duck, sqlite = DuckDBStore(paths=paths), SQLiteStore(paths=paths)
    if initialize:
        from app.core.storage.schema import init_all_schema
        from app.core.backup.manager import recover_pending_restore

        init_all_schema(duckdb_store=duck, sqlite_store=sqlite)
        recover_pending_restore(paths)
    return paths, duck, sqlite


def _database_stores(*, initialize: bool = True):
    _, duck, sqlite = _database_context(initialize=initialize)
    return duck, sqlite


def _duck_store():
    return _database_stores()[0]


def _sqlite_store():
    return _database_stores()[1]


def _dsl_engine():
    from app.core.dsl.engine import DSLEngine

    _, duck, sqlite = _database_context()
    return DSLEngine(duck=duck, sqlite=sqlite)


def _screening_engine():
    from app.core.data_quality import screening_readiness
    from app.core.screening.engine import ScreeningEngine
    from app.cli.protocol import make_response

    _, duck, sqlite = _database_context()
    readiness = screening_readiness(duck, sqlite)
    if not readiness["ready"]:
        typer.echo(json.dumps(make_response(
            "screening.run", error_code="E002", error_message="minimum_data_not_ready",
            data=readiness,
        ), ensure_ascii=False))
        return
    return ScreeningEngine(duck=duck, sqlite=sqlite)


def _backup_manager(*, initialize: bool = True):
    from app.core.backup.manager import BackupManager

    _, duck, sqlite = _database_context(initialize=initialize)
    return BackupManager(duck=duck, sqlite=sqlite)


def _pdf_manager():
    from app.core.pdf.manager import PDFManager

    _, _, sqlite = _database_context()
    return PDFManager(sqlite=sqlite)


def _correction_manager():
    from app.core.pdf.correction import CorrectionManager

    _, duck, sqlite = _database_context()
    return CorrectionManager(duck=duck, sqlite=sqlite)


app = typer.Typer(
    name="value-dashboard",
    help="A股价值投资研究与筛选工具 CLI",
    no_args_is_help=True,
)


@app.command()
def server() -> None:
    """启动 Web 服务器（等同于一键启动）"""
    from app.web.main import run_server

    run_server()


@app.command()
def init() -> None:
    """初始化数据库 schema"""
    from app.core.storage.schema import init_all_schema

    _, duck, sqlite = _database_context()
    init_all_schema(duckdb_store=duck, sqlite_store=sqlite)
    from app.cli.protocol import make_response

    typer.echo(json.dumps(make_response("init", {"status": "ok"}), ensure_ascii=False))


data_app = typer.Typer(help="数据管理")
app.add_typer(data_app, name="data")


@data_app.command("init")
def data_init(
    skip_prices: bool = typer.Option(False, "--skip-prices", help="跳过价格数据"),
    skip_financials: bool = typer.Option(False, "--skip-financials", help="跳过财务数据"),
    skip_csrc: bool = typer.Option(False, "--skip-csrc", help="跳过 CSRC 行业全量抓取（先行建立最小可用）"),
) -> None:
    """最小可用初始化 (PRD §6.7)

    按顺序获取: 股票全集 → 交易日历 → 申万行业 → 近5年价格 → 核心财务
    """
    from app.cli.protocol import make_response
    from app.core.init import DataInitializer

    _, duck, sqlite = _database_context()
    initializer = DataInitializer(duck=duck, sqlite=sqlite)
    report = initializer.run_full_init(
        skip_prices=skip_prices,
        skip_financials=skip_financials,
        skip_csrc=skip_csrc,
    )

    typer.echo(json.dumps(make_response("data.init", report), ensure_ascii=False, indent=2, default=str))


@data_app.command("refresh_universe")
def data_refresh_universe() -> None:
    """Refresh the current listed universe and its verified pool metadata."""
    from app.cli.protocol import make_response
    from app.core.init import DataInitializer

    _, duck, sqlite = _database_context()
    initializer = DataInitializer(duck=duck, sqlite=sqlite)
    universe = initializer._fetch_stock_universe()
    metadata = (
        initializer._fetch_listing_info()
        if universe.get("status") == "success"
        else {"status": "skipped", "reason": "universe_not_refreshed"}
    )
    result = {
        "status": "success" if metadata.get("status") == "success" else "partial",
        "universe": universe,
        "pool_metadata": metadata,
    }
    typer.echo(
        json.dumps(
            make_response("data.refresh_universe", result),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@data_app.command("update")
def data_update(
    max_stocks: int = typer.Option(0, "--max-stocks", help="最多更新N只股票(0=全部)"),
    check_only: bool = typer.Option(False, "--check-only", help="只检查不更新"),
    stocks: str = typer.Option("", "--stocks", help="只更新指定股票，逗号分隔（如 000001,600519）"),
) -> None:
    """增量更新 (PRD §7.3)

    检查新交易日、新公告、待重试任务，执行增量更新。
    """
    from app.cli.protocol import make_response
    from app.core.update import IncrementalUpdater

    _, duck, sqlite = _database_context()
    updater = IncrementalUpdater(duck=duck, sqlite=sqlite)

    if check_only:
        report = updater.run_incremental_check()
    elif stocks.strip():
        # 指定股票更新（PRD §16.1）：只刷新这些股票的核心数据
        codes = [c.strip() for c in stocks.split(",") if c.strip()]
        report = {
            "status": "success",
            "targeted": len(codes),
            "results": {},
        }
        for code in codes:
            report["results"][code] = {
                data_type: updater.refetch_one(code, data_type)
                for data_type in ("price_daily", "balance_sheet", "income_statement", "cash_flow", "dividends", "xdxr")
            }
    else:
        report = updater.run_incremental_update(max_stocks=max_stocks)

    typer.echo(json.dumps(make_response("data.update", report), ensure_ascii=False, indent=2, default=str))


@data_app.command("replenish_missing_core_data")
def data_replenish_missing_core_data(
    max_stocks: int = typer.Option(0, "--max-stocks", min=0, help="最多补齐 N 只股票，0=全部缺项"),
) -> None:
    """Only fetch listed stocks missing snapshot-required prices or financial fields."""
    from app.cli.protocol import make_response
    from app.core.update import IncrementalUpdater

    _, duck, sqlite = _database_context()
    result = IncrementalUpdater(duck=duck, sqlite=sqlite).replenish_missing_core_data(max_stocks)
    typer.echo(
        json.dumps(
            make_response("data.replenish_missing_core_data", result),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@data_app.command("backfill-prices")
def data_backfill_prices(
    max_stocks: int = typer.Option(0, "--max-stocks", help="最多处理N只股票(0=全部)"),
    skip_complete: bool = typer.Option(True, "--skip-complete", help="跳过已有充足历史的股票"),
    no_dividends: bool = typer.Option(False, "--no-dividends", help="跳过分红回填"),
) -> None:
    """历史价格回填 (PRD §6.1 D4: 上市以来全部可得数据)

    将 price_daily 从"近5年"扩展到"上市以来全部"。
    同时回填 dividends 送股/转增字段。
    """
    from app.cli.protocol import make_response
    from app.core.backfill import PriceBackfiller

    _, duck, sqlite = _database_context()
    backfiller = PriceBackfiller(duck=duck, sqlite=sqlite)
    report = backfiller.run_full_backfill(
        skip_if_complete=skip_complete,
        max_stocks=max_stocks,
        fetch_dividends=not no_dividends,
    )
    typer.echo(json.dumps(make_response("data.backfill_prices", report), ensure_ascii=False, indent=2, default=str))


# ─── 自动更新控制 (PRD §7.3, §16.1) ───────────────────────────────

auto_update_app = typer.Typer(help="自动更新控制（开关/立即更新/暂停/继续/状态）")
data_app.add_typer(auto_update_app, name="auto-update")


def _auto_update_controller():
    from app.core.auto_update import AutoUpdateController

    _, duck, sqlite = _database_context()
    return AutoUpdateController(duck=duck, sqlite=sqlite)


@auto_update_app.command("status")
def auto_update_status() -> None:
    """查询自动更新状态（网页只读展示同源数据）"""
    from app.cli.protocol import make_response

    controller = _auto_update_controller()
    typer.echo(json.dumps(make_response("data.auto-update.status", controller.persisted_status()), ensure_ascii=False, indent=2, default=str))


@auto_update_app.command("enable")
def auto_update_enable() -> None:
    """开启自动更新（默认行为，PRD §7.3）"""
    from app.cli.protocol import make_response

    controller = _auto_update_controller()
    typer.echo(json.dumps(make_response("data.auto-update.enable", controller.enable()), ensure_ascii=False, indent=2, default=str))


@auto_update_app.command("disable")
def auto_update_disable() -> None:
    """关闭自动更新（改为完全手动模式）"""
    from app.cli.protocol import make_response

    controller = _auto_update_controller()
    typer.echo(json.dumps(make_response("data.auto-update.disable", controller.disable()), ensure_ascii=False, indent=2, default=str))


@auto_update_app.command("run")
def auto_update_run(
    max_stocks: int = typer.Option(0, "--max-stocks", help="最多更新N只股票(0=全部)"),
) -> None:
    """立即执行一次自动更新（等价手动触发）"""
    from app.cli.protocol import make_response

    controller = _auto_update_controller()
    report = controller.run_once(max_stocks=max_stocks)
    typer.echo(json.dumps(make_response("data.auto-update.run", report), ensure_ascii=False, indent=2, default=str))


@auto_update_app.command("pause")
def auto_update_pause() -> None:
    """暂停自动更新推进"""
    from app.cli.protocol import make_response

    controller = _auto_update_controller()
    typer.echo(json.dumps(make_response("data.auto-update.pause", controller.pause()), ensure_ascii=False, indent=2, default=str))


@auto_update_app.command("resume")
def auto_update_resume() -> None:
    """继续自动更新推进"""
    from app.cli.protocol import make_response

    controller = _auto_update_controller()
    typer.echo(json.dumps(make_response("data.auto-update.resume", controller.resume()), ensure_ascii=False, indent=2, default=str))


@data_app.command("compute_indicators")
def data_compute_indicators() -> None:
    """计算全部内建指标并写入 indicator_snapshot 表 (PRD §10)"""
    from app.cli.protocol import make_response
    from app.core.indicators.calculator import IndicatorCalculator

    _, duck, sqlite = _database_context()
    calc = IndicatorCalculator(duck=duck, sqlite=sqlite)
    report = calc.compute_snapshot_for_all()

    typer.echo(json.dumps(make_response("data.compute_indicators", report), ensure_ascii=False, indent=2, default=str))


# ─── DSL/复合指标命令 (PRD §16.1, §11.5) ─────────────────────────

indicator_app = typer.Typer(help="复合指标管理 (DSL)")
app.add_typer(indicator_app, name="indicator")


@indicator_app.command("create")
def indicator_create(
    name: str = typer.Argument(..., help="指标名称 (英文标识符)"),
    expression: str = typer.Argument(..., help="DSL 表达式"),
    description: str = typer.Option("", "--desc", help="中文描述"),
    direction: str = typer.Option("none", "--dir", help="higher_is_better/lower_is_better/none"),
) -> None:
    """创建复合指标草稿 (PRD §11.5 DL13)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    engine = _dsl_engine()
    result = engine.create(name, expression, description, direction)
    typer.echo(json.dumps(make_response("indicator.create", result), ensure_ascii=False, indent=2))


@indicator_app.command("validate")
def indicator_validate(
    name: str = typer.Argument(...),
    version: int = typer.Argument(...),
) -> None:
    """校验复合指标"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    engine = _dsl_engine()
    result = engine.validate(name, version)
    typer.echo(json.dumps(make_response("indicator.validate", result), ensure_ascii=False, indent=2, default=str))


@indicator_app.command("preview_single")
def indicator_preview_single(
    name: str = typer.Argument(...),
    version: int = typer.Argument(...),
    stock_code: str = typer.Argument(...),
) -> None:
    """单股预览"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    engine = _dsl_engine()
    result = engine.preview_single(name, version, stock_code)
    typer.echo(json.dumps(make_response("indicator.preview_single", result), ensure_ascii=False, indent=2, default=str))


@indicator_app.command("preview_sample")
def indicator_preview_sample(
    name: str = typer.Argument(...),
    version: int = typer.Argument(...),
    limit: int = typer.Option(10, "--limit"),
) -> None:
    """小样本预览"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    engine = _dsl_engine()
    result = engine.preview_sample(name, version, limit)
    typer.echo(json.dumps(make_response("indicator.preview_sample", result), ensure_ascii=False, indent=2, default=str))


@indicator_app.command("publish")
def indicator_publish(
    name: str = typer.Argument(...),
    version: int = typer.Argument(...),
) -> None:
    """发布复合指标 (PRD §11.5 DL14: 不可变更)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    engine = _dsl_engine()
    result = engine.publish(name, version)
    typer.echo(json.dumps(make_response("indicator.publish", result), ensure_ascii=False, indent=2, default=str))


@indicator_app.command("list")
def indicator_list() -> None:
    """列出所有复合指标"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    engine = _dsl_engine()
    result = engine.list_all()
    typer.echo(json.dumps(make_response("indicator.list", result), ensure_ascii=False, indent=2, default=str))


@indicator_app.command("discover")
def indicator_discover(
    what: str = typer.Argument("all", help="fields/indicators/functions/reason_codes/all"),
) -> None:
    """发现可用字段/指标/函数/原因码 (PRD §16.1 CL4)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    engine = _dsl_engine()

    if what == "all":
        result = {
            "fields": engine.discover_fields(),
            "indicators": engine.discover_indicators(),
            "functions": engine.discover_functions(),
            "reason_codes": engine.discover_reason_codes(),
        }
    elif what == "fields":
        result = {"fields": engine.discover_fields()}
    elif what == "indicators":
        result = {"indicators": engine.discover_indicators()}
    elif what == "functions":
        result = {"functions": engine.discover_functions()}
    elif what == "reason_codes":
        result = {"reason_codes": engine.discover_reason_codes()}
    else:
        result = {"error": f"unknown: {what}"}

    typer.echo(json.dumps(make_response("indicator.discover", result), ensure_ascii=False, indent=2))


@data_app.command("status")
def data_status() -> None:
    """查看数据覆盖状态"""
    from app.core.config import Config

    Config.load()
    from app.cli.protocol import make_response
    from app.core.data_quality import build_data_quality_status
    duck, sqlite = _database_stores(initialize=False)

    stock_count = duck.read_query("SELECT COUNT(*) as cnt FROM stock_meta WHERE is_listed IS TRUE")
    raw_count = duck.read_query(
        """SELECT COUNT(DISTINCT price.stock_code) as cnt FROM price_daily_raw price
           JOIN stock_meta stock ON stock.stock_code = price.stock_code
           WHERE stock.is_listed IS TRUE"""
    )
    qfq_count = duck.read_query(
        """SELECT COUNT(DISTINCT price.stock_code) as cnt FROM price_daily_qfq price
           JOIN stock_meta stock ON stock.stock_code = price.stock_code
           WHERE stock.is_listed IS TRUE"""
    )
    bs_count = duck.read_query(
        """SELECT COUNT(DISTINCT statement.stock_code) as cnt FROM balance_sheet statement
           JOIN stock_meta stock ON stock.stock_code = statement.stock_code
           WHERE stock.is_listed IS TRUE"""
    )
    ic_count = duck.read_query(
        """SELECT COUNT(DISTINCT statement.stock_code) as cnt FROM income_statement statement
           JOIN stock_meta stock ON stock.stock_code = statement.stock_code
           WHERE stock.is_listed IS TRUE"""
    )
    cf_count = duck.read_query(
        """SELECT COUNT(DISTINCT statement.stock_code) as cnt FROM cash_flow statement
           JOIN stock_meta stock ON stock.stock_code = statement.stock_code
           WHERE stock.is_listed IS TRUE"""
    )
    retry_count = sqlite.query("SELECT COUNT(*) as cnt FROM retry_list")
    missing_count = sqlite.query("SELECT COUNT(*) as cnt FROM missing_list")

    data = {
        "stock_count": stock_count[0]["cnt"],
        "raw_price_count": raw_count[0]["cnt"],
        "qfq_price_count": qfq_count[0]["cnt"],
        "balance_sheet_count": bs_count[0]["cnt"],
        "income_statement_count": ic_count[0]["cnt"],
        "cash_flow_count": cf_count[0]["cnt"],
        "retry_count": retry_count[0]["cnt"],
        "missing_count": missing_count[0]["cnt"],
        "data_quality": build_data_quality_status(duck, sqlite),
    }
    typer.echo(json.dumps(make_response("data.status", data), ensure_ascii=False, indent=2, default=str))


@app.command()
def status() -> None:
    """检查数据库连接状态"""
    from app.core.config import Config

    Config.load()
    from app.cli.protocol import make_response
    duck, sqlite = _database_stores(initialize=False)

    data: dict[str, Any] = {}
    try:
        tables = duck.read_query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        )
        data["duckdb"] = {"connected": True, "tables": len(tables)}
    except Exception as e:
        data["duckdb"] = {"connected": False, "error": str(e)}

    try:
        tables = sqlite.query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        data["sqlite"] = {"connected": True, "tables": len(tables)}
    except Exception as e:
        data["sqlite"] = {"connected": False, "error": str(e)}

    typer.echo(json.dumps(make_response("status", data), ensure_ascii=False, indent=2, default=str))




# ─── discover 命令 (PRD §16.1 CL4, §16.2 CL6) ────────────────────

discover_app = typer.Typer(help="发现字段/指标/函数/原因码/能力")
app.add_typer(discover_app, name="discover")


@discover_app.command("schema")
def discover_schema() -> None:
    """获取 JSON schema"""
    from app.cli.protocol import get_schema, make_response
    schema = get_schema()
    response = make_response("discover.schema", data=schema)
    typer.echo(json.dumps(response, ensure_ascii=False, indent=2))


@discover_app.command("capabilities")
def discover_capabilities() -> None:
    """获取能力清单"""
    from app.cli.protocol import get_capabilities, make_response
    caps = get_capabilities()
    response = make_response("discover.capabilities", data=caps)
    typer.echo(json.dumps(response, ensure_ascii=False, indent=2))


@discover_app.command("examples")
def discover_examples() -> None:
    """获取示例"""
    from app.cli.protocol import get_examples, make_response
    examples = get_examples()
    response = make_response("discover.examples", data=examples)
    typer.echo(json.dumps(response, ensure_ascii=False, indent=2))


# ─── screening 命令 (PRD §16.1, M3遗留) ──────────────────────────

screening_app = typer.Typer(help="筛选规则管理")
app.add_typer(screening_app, name="screening")


@screening_app.command("run")
def screening_run(
    rule_id: int = typer.Argument(..., help="已保存规则 ID"),
    version: int = typer.Option(..., "--version", help="规则版本"),
    include_st: bool = typer.Option(False, "--include-st"),
    include_suspended: bool = typer.Option(False, "--include-suspended"),
    min_years: int = typer.Option(1, "--min-years"),
    strict_only: bool = typer.Option(False, "--strict-only"),
) -> None:
    """手动运行筛选 (PRD §12.2 SC8)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.data_quality import screening_readiness
    from app.core.screening.engine import ScreeningEngine

    _, duck, sqlite = _database_context()
    decision = screening_readiness(duck, sqlite)
    if not decision["ready"]:
        typer.echo(json.dumps(make_response(
            "screening.run",
            error_code="E002",
            error_message="screening_data_not_ready",
            data=decision,
        ), ensure_ascii=False))
        return
    rows = sqlite.query("SELECT rule_json, locked_indicators FROM screening_rules WHERE id = ? AND version = ?", [rule_id, version])
    if not rows:
        typer.echo(json.dumps(make_response("screening.run", error_code="E001", error_message="saved rule version not found"), ensure_ascii=False))
        return
    engine = ScreeningEngine(duck=duck, sqlite=sqlite)
    rule_dict = json.loads(rows[0]["rule_json"])
    locked_indicators = json.loads(rows[0]["locked_indicators"] or "{}")
    result = engine.run(
        rule=rule_dict,
        include_st=include_st,
        include_suspended=include_suspended,
        min_listing_years=min_years,
        strict_only=strict_only,
        locked_indicators=locked_indicators,
    )
    for stock in result["results"]:
        stock["_entry_explanation"] = engine.generate_entry_explanation(stock, rule_dict.get("conditions", {}))
    from app.web.api.screening import _attach_result_report_dates
    _attach_result_report_dates(duck, result["results"])
    run_id = __import__("uuid").uuid4().hex
    sqlite.execute(
        """INSERT INTO screening_runs
           (run_id, rule_id, rule_version, result_json, columns_json, sort_json, data_date,
            base_pool_config, strict_only, confidence_summary)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            run_id, rule_id, version, json.dumps(result["results"], ensure_ascii=False, default=str),
            json.dumps(rule_dict.get("columns", []), ensure_ascii=False),
            json.dumps(rule_dict.get("sort", []), ensure_ascii=False), result["data_date"],
            json.dumps({"include_st": include_st, "include_suspended": include_suspended, "min_listing_years": min_years}),
            strict_only, json.dumps({"strict_only": strict_only, "locked_indicators": locked_indicators}),
        ],
    )
    result["run_id"] = run_id
    typer.echo(json.dumps(make_response("screening.run", result), ensure_ascii=False, indent=2, default=str))


@screening_app.command("save_result")
def screening_save(
    title: str = typer.Argument(..., help="标题(必填)"),
    run_id: str = typer.Argument(..., help="服务端筛选运行 ID"),
) -> None:
    """保存筛选结果 (PRD §12.5 SC14-15)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.data_quality import screening_readiness

    _, duck, sqlite = _database_context()
    decision = screening_readiness(duck, sqlite)
    if not decision["ready"]:
        typer.echo(json.dumps(make_response(
            "screening.save_result", error_code="E002", error_message="screening_data_not_ready",
            data=decision,
        ), ensure_ascii=False))
        return
    if not title.strip():
        typer.echo(json.dumps(make_response("screening.save_result", error_code="E001", error_message="title is required"), ensure_ascii=False))
        return
    with sqlite.transaction() as conn:
        run = conn.execute("SELECT * FROM screening_runs WHERE run_id = ?", [run_id]).fetchone()
        if run is None:
            typer.echo(json.dumps(make_response("screening.save_result", error_code="E001", error_message="server screening run not found"), ensure_ascii=False))
            return
        cursor = conn.execute(
            """INSERT INTO screening_results
               (title, rule_id, rule_version, data_date, result_json, columns_json, sort_json,
                confidence_summary, base_pool_config)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [title, run["rule_id"], run["rule_version"], run["data_date"], run["result_json"],
              run["columns_json"], run["sort_json"], run["confidence_summary"], run["base_pool_config"]],
        )
        result_id = cursor.lastrowid
        conn.execute("DELETE FROM screening_runs WHERE run_id = ?", [run_id])
    typer.echo(json.dumps(make_response("screening.save_result", {"status": "ok", "result_id": result_id}), ensure_ascii=False))


@screening_app.command("list")
def screening_list(limit: int = typer.Option(20, "--limit")) -> None:
    """列出已保存的筛选结果"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    sqlite = _sqlite_store()
    rows = sqlite.query(
        "SELECT id, title, data_date, created_at FROM screening_results ORDER BY created_at DESC LIMIT ?",
        [limit],
    )
    typer.echo(json.dumps(make_response("screening.list", {"results": rows, "count": len(rows)}), ensure_ascii=False, default=str))


# ─── override 命令 (PRD §9.5, §16.1) ─────────────────────────────

override_app = typer.Typer(help="人工覆写管理")
app.add_typer(override_app, name="override")


@override_app.command("list_conflicts")
def override_list() -> None:
    """查看来源冲突"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    duck = _duck_store()
    try:
        rows = duck.read_query(
            "SELECT stock_code, field_name, source, value, confidence "
            "FROM source_audit WHERE confidence != 'strict' "
            "ORDER BY stock_code, field_name LIMIT 50"
        )
        typer.echo(json.dumps(make_response("override.list_conflicts", {"conflicts": rows, "count": len(rows)}), ensure_ascii=False, default=str))
    except Exception as e:
        typer.echo(json.dumps(make_response("override.list_conflicts", {"error": str(e)}), ensure_ascii=False))


@override_app.command("submit")
def override_submit(
    stock_code: str = typer.Argument(...),
    field_name: str = typer.Argument(...),
    value: float = typer.Argument(...),
    reason: str = typer.Option("manual", "--reason"),
    report_date: str = typer.Option("", "--report-date"),
) -> None:
    """提交人工校正 (PRD §9.5 R7)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    sqlite = _sqlite_store()
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO manual_overrides
               (stock_code, field_name, report_date, override_value, reason)
               VALUES (?, ?, ?, ?, ?)""",
            [stock_code, field_name, report_date or None, value, reason],
        )
    typer.echo(json.dumps(make_response("override.submit", {"status": "ok", "stock_code": stock_code, "field": field_name, "value": value}), ensure_ascii=False))


@override_app.command("revoke")
def override_revoke(
    override_id: int = typer.Argument(...),
) -> None:
    """撤销人工覆写 (PRD §9.5 R7: 可回滚)"""
    from datetime import datetime
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    sqlite = _sqlite_store()
    sqlite.execute(
        "UPDATE manual_overrides SET rolled_back_at = ? WHERE id = ?",
        [datetime.now().isoformat(), override_id],
    )
    typer.echo(json.dumps(make_response("override.revoke", {"status": "ok", "revoked": override_id}), ensure_ascii=False))


# ─── plan 命令 (PRD §16.3 CL10-11) ───────────────────────────────

plan_app = typer.Typer(help="危险操作两段式确认")
app.add_typer(plan_app, name="plan")


@plan_app.command("confirm")
def plan_confirm(plan_id: str = typer.Argument(...)) -> None:
    """确认危险操作 (PRD §16.3 CL11: 15分钟有效期)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import confirm_plan

    _, _, sqlite = _database_context()
    result = confirm_plan(plan_id, sqlite=sqlite)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


# ─── backup 命令 (PRD §18, §16.1) ────────────────────────────────

backup_app = typer.Typer(help="备份管理")
app.add_typer(backup_app, name="backup")


@backup_app.command("create")
def backup_create(
    password: str = typer.Option("", "--password", help="用户口令(加密个性化数据)"),
    prompt_password: bool = typer.Option(
        False,
        "--prompt-password",
        help="在隐藏输入提示中读取口令，避免将其写入命令行历史",
    ),
    target_dir: str = typer.Option("data/backup", "--target"),
) -> None:
    """创建全量备份 (PRD §18.3 AR9-10)

    --password: 提供口令则加密个性化数据并生成离线恢复密钥
    --prompt-password: 在隐藏输入提示中读取口令，不能与 --password 同时使用
    """
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    if password and prompt_password:
        raise typer.BadParameter("--password and --prompt-password cannot be used together")
    if prompt_password:
        password = typer.prompt("Backup password", hide_input=True, confirmation_prompt=True)

    mgr = _backup_manager()
    result = mgr.create_full_backup(
        user_password=password or None,
        target_dir=target_dir,
    )
    typer.echo(json.dumps(make_response("backup.create", result), ensure_ascii=False, indent=2, default=str))


@backup_app.command("restore")
def backup_restore(
    backup_path: str = typer.Argument(..., help="备份ZIP文件路径"),
    password: str = typer.Option("", "--password", help="用户口令(解密个性化数据)"),
    recovery_key: str = typer.Option("", "--recovery-key", help="离线恢复密钥(忘记口令时使用)"),
) -> None:
    """恢复备份 (PRD §18.2 AR5: 只能通过CLI, 两段式确认)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import create_plan

    # restore 是危险操作 → 必须两段式确认
    operation = "backup.restore"
    plan_summary = {
        "operation": operation,
        "backup_path": backup_path,
        "password_provided": bool(password),
        "recovery_key_provided": bool(recovery_key),
        "warning": "恢复将覆盖当前数据，请确保Web服务已停止",
    }
    _, _, sqlite = _database_context()
    result = create_plan(operation, plan_summary, sqlite=sqlite)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@backup_app.command("list")
def backup_list() -> None:
    """列出备份"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    mgr = _backup_manager(initialize=False)
    backups = mgr.list_backups()
    typer.echo(json.dumps(make_response("backup.list", {"backups": backups, "count": len(backups)}), ensure_ascii=False, default=str))


@backup_app.command("restore_execute")
def backup_restore_execute(
    backup_path: str = typer.Argument(..., help="备份ZIP文件路径"),
    plan_id: str = typer.Option(..., "--plan-id", help="已确认的恢复计划 ID"),
    password: str = typer.Option("", "--password", help="用户口令"),
    recovery_key: str = typer.Option("", "--recovery-key", help="离线恢复密钥"),
) -> None:
    """执行恢复 (在 plan confirm 之后调用)

    P0#16修复: 验证 plan confirm 已执行, 防止绕过两段式确认
    """
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import consume_confirmed_plan, make_response
    from app.core.backup.manager import BackupManager

    # P0#16修复: 验证 plan confirm 已执行
    _, duck, sqlite = _database_context()
    plan_error, plan_summary = consume_confirmed_plan(
        "backup.restore", plan_id=plan_id, sqlite=sqlite
    )
    if plan_error:
        typer.echo(json.dumps(plan_error, ensure_ascii=False, indent=2, default=str))
        return

    if plan_summary.get("backup_path") != backup_path:
        typer.echo(json.dumps(make_response(
            "backup.restore_execute",
            error_code="E001",
            error_message="backup_path does not match the confirmed plan",
        ), ensure_ascii=False, indent=2))
        return

    mgr = BackupManager(duck=duck, sqlite=sqlite)
    result = mgr.restore_from_backup(
        backup_path,
        user_password=password or None,
        recovery_key=recovery_key or None,
    )
    typer.echo(json.dumps(make_response("backup.restore_execute", result), ensure_ascii=False, indent=2, default=str))


# ─── archive 命令 (PRD §18.1-18.2) ───────────────────────────────

archive_app = typer.Typer(help="冷归档管理")
app.add_typer(archive_app, name="archive")


@archive_app.command("create")
def archive_create(
    target_dir: str = typer.Option("data/parquet", "--target"),
) -> None:
    """创建冷归档 (PRD §18.1 AR1: 热数据→冷归档)"""
    from app.core.config import Config
    from app.cli.protocol import make_response
    from app.core.archive import DataArchiveManager

    paths, duck, sqlite = _database_context()
    Config.load_with_paths(paths)
    try:
        result = DataArchiveManager(duck, paths, sqlite).create(target_dir)
    except Exception as error:
        result = {"status": "error", "error": str(error)}
    typer.echo(json.dumps(make_response("archive.create", result), ensure_ascii=False))


@archive_app.command("verify")
def archive_verify(
    target_dir: str = typer.Argument("data/parquet"),
) -> None:
    """验证归档完整性 (PRD §18.2 AR4: 归档验证成功后才允许清理)"""
    from app.core.archive import DataArchiveManager
    from app.core.config import Config
    from app.cli.protocol import make_response

    paths, duck, sqlite = _database_context()
    Config.load_with_paths(paths)
    try:
        result = DataArchiveManager(duck, paths, sqlite).verify(target_dir)
    except Exception as error:
        result = {"status": "error", "error": str(error)}
    typer.echo(json.dumps(make_response("archive.verify", result), ensure_ascii=False))


@archive_app.command("clean")
def archive_clean(
    target_dir: str = typer.Argument("data/parquet"),
) -> None:
    """清理已归档的本地热数据 (PRD §18.2 AR4: 归档验证成功后才允许清理, 两段式确认)"""
    from app.core.archive import DataArchiveManager
    from app.core.config import Config
    from app.cli.protocol import create_plan

    operation = "archive.clean"
    paths, duck, sqlite = _database_context()
    Config.load_with_paths(paths)
    try:
        archive_root = DataArchiveManager(duck, paths, sqlite).resolve_target(target_dir)
    except Exception as error:
        typer.echo(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return
    plan_summary = {
        "operation": operation,
        "target_dir": str(archive_root),
        "warning": "清理将删除本地热数据, 请确保归档已验证成功",
    }
    result = create_plan(operation, plan_summary, sqlite=sqlite)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@archive_app.command("clean_execute")
def archive_clean_execute(
    target_dir: str = typer.Argument("data/parquet"),
    plan_id: str = typer.Option(..., "--plan-id", help="已确认的清理计划 ID"),
) -> None:
    """执行已确认的归档清理，只删除已验证的公共热表数据。"""
    from app.core.archive import ARCHIVE_TABLES, DataArchiveManager
    from app.core.config import Config
    from app.cli.protocol import consume_confirmed_plan, make_response

    paths, duck, sqlite = _database_context()
    Config.load_with_paths(paths)
    try:
        archive_manager = DataArchiveManager(duck, paths, sqlite)
        archive_root = archive_manager.resolve_target(target_dir)
    except Exception as error:
        typer.echo(json.dumps(make_response("archive.clean_execute", error_code="E001", error_message=str(error)), ensure_ascii=False))
        return
    plan_error, summary = consume_confirmed_plan("archive.clean", plan_id=plan_id, sqlite=sqlite)
    if plan_error:
        typer.echo(json.dumps(plan_error, ensure_ascii=False, indent=2))
        return
    if summary.get("target_dir") != str(archive_root):
        typer.echo(json.dumps(make_response("archive.clean_execute", error_code="E001", error_message="target_dir does not match the confirmed plan"), ensure_ascii=False))
        return
    verified, verification_error = archive_manager.delete_verified_hot_data(target_dir)
    if not verified:
        typer.echo(json.dumps(make_response("archive.clean_execute", error_code="E002", error_message=f"archive verification failed: {verification_error}"), ensure_ascii=False))
        return
    typer.echo(json.dumps(make_response("archive.clean_execute", {"status": "success", "cleared_tables": list(ARCHIVE_TABLES), "archive": str(archive_root)}), ensure_ascii=False))


# ─── 补充缺失的 data 命令 (M7-问题2/4) ───────────────────────────

@data_app.command("diagnose")
def data_diagnose() -> None:
    """数据健康诊断 (M7-问题2)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.data_quality import build_data_quality_status
    duck, sqlite = _database_stores(initialize=False)

    report: dict = {}

    # 检查股票全集
    try:
        row = duck.read_query("SELECT COUNT(*) as cnt FROM stock_meta WHERE is_listed IS TRUE")
        report["stock_count"] = row[0]["cnt"]
    except Exception:
        report["stock_count"] = "error"

    # 检查价格覆盖
    try:
        row = duck.read_query(
            """SELECT COUNT(DISTINCT price.stock_code) as cnt FROM price_daily_raw price
               JOIN stock_meta stock ON stock.stock_code = price.stock_code
               WHERE stock.is_listed IS TRUE"""
        )
        report["price_coverage"] = row[0]["cnt"]
    except Exception:
        report["price_coverage"] = "error"

    # 检查财务覆盖
    try:
        row = duck.read_query(
            """SELECT COUNT(DISTINCT statement.stock_code) as cnt FROM balance_sheet statement
               JOIN stock_meta stock ON stock.stock_code = statement.stock_code
               WHERE stock.is_listed IS TRUE"""
        )
        report["financial_coverage"] = row[0]["cnt"]
    except Exception:
        report["financial_coverage"] = "error"

    # 检查指标快照
    try:
        row = duck.read_query("SELECT COUNT(*) as cnt FROM indicator_snapshot")
        report["indicator_snapshot"] = row[0]["cnt"]
    except Exception:
        report["indicator_snapshot"] = "error"

    # 检查重试/缺失
    try:
        row = sqlite.query("SELECT COUNT(*) as cnt FROM retry_list")
        report["retry_count"] = row[0]["cnt"]
    except Exception:
        report["retry_count"] = "error"

    try:
        row = sqlite.query("SELECT COUNT(*) as cnt FROM missing_list")
        report["missing_count"] = row[0]["cnt"]
    except Exception:
        report["missing_count"] = "error"

    data_quality = build_data_quality_status(duck, sqlite)
    report["data_quality"] = data_quality

    # 健康评估
    issues: list[str] = []
    if report.get("stock_count", 0) == 0:
        issues.append("股票全集为空, 请运行: vd data init")
    if report.get("price_coverage", 0) == 0 and report.get("stock_count", 0) > 0:
        issues.append("无价格数据, 请运行: vd data init (不带 --skip-prices)")
    if report.get("financial_coverage", 0) == 0 and report.get("stock_count", 0) > 0:
        issues.append("无财务数据, 请运行: vd data init (不带 --skip-financials)")
    if report.get("indicator_snapshot", 0) == 0 and report.get("financial_coverage", 0) > 0:
        issues.append("指标快照为空, 请运行: vd data compute_indicators")
    if report.get("retry_count", 0) > 0:
        issues.append(f"有 {report['retry_count']} 个待重试任务, 请运行: vd data update")
    blocking_warnings = {
        "FINANCIAL_SHELL_ROWS",
        "SNAPSHOT_STALE",
        "MINIMUM_DATA_NOT_READY",
        "DIVIDEND_DATES_UNVERIFIED",
    }
    issues.extend(
        f"数据质量阻断: {warning_code}"
        for warning_code in data_quality["warning_codes"]
        if warning_code in blocking_warnings
    )
    report["operational_warnings"] = [
        warning_code
        for warning_code in data_quality["warning_codes"]
        if warning_code not in blocking_warnings
    ]

    report["issues"] = issues
    report["healthy"] = len(issues) == 0

    typer.echo(json.dumps(make_response("data.diagnose", report), ensure_ascii=False, indent=2, default=str))


@data_app.command("reconcile_jobs")
def data_reconcile_jobs(
    older_than_hours: int = typer.Option(24, "--older-than-hours", min=1, max=24 * 365),
) -> None:
    """Create a confirmation plan for stale running jobs; no records change yet."""
    from datetime import datetime, timedelta, timezone

    from app.cli.protocol import create_plan

    _, _, sqlite = _database_context()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    jobs = sqlite.query(
        """SELECT id, job_type, started_at FROM job_logs
           WHERE status = 'running' AND started_at < ? ORDER BY started_at""",
        [cutoff.isoformat()],
    )
    result = create_plan(
        "data.reconcile_jobs",
        {
            "older_than_hours": older_than_hours,
            "cutoff": cutoff.isoformat(),
            "job_ids": [job["id"] for job in jobs],
            "count": len(jobs),
            "action": "mark stale running jobs failed; preserve original details and add reconciliation reason",
        },
        sqlite=sqlite,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@data_app.command("reconcile_jobs_execute")
def data_reconcile_jobs_execute(
    plan_id: str = typer.Option(..., "--plan-id", help="confirmed reconciliation plan ID"),
) -> None:
    """Execute a confirmed stale-job reconciliation plan."""
    from datetime import datetime, timezone

    from app.cli.protocol import consume_confirmed_plan, make_response

    _, _, sqlite = _database_context()
    error, summary = consume_confirmed_plan("data.reconcile_jobs", plan_id=plan_id, sqlite=sqlite)
    if error:
        typer.echo(json.dumps(error, ensure_ascii=False, indent=2, default=str))
        return
    job_ids = summary.get("job_ids", [])
    if not isinstance(job_ids, list) or not all(isinstance(job_id, int) for job_id in job_ids):
        typer.echo(json.dumps(make_response(
            "data.reconcile_jobs_execute", error_code="E001", error_message="invalid confirmed job plan"
        ), ensure_ascii=False, indent=2))
        return
    reconciled = 0
    with sqlite.transaction() as conn:
        for job_id in job_ids:
            row = conn.execute("SELECT details_json FROM job_logs WHERE id = ? AND status = 'running'", [job_id]).fetchone()
            if row is None:
                continue
            try:
                details = json.loads(row["details_json"] or "{}")
            except json.JSONDecodeError:
                details = {"legacy_details": row["details_json"]}
            details["reconciliation"] = {
                "reason_code": "stale_running_job",
                "plan_id": plan_id,
                "reconciled_at": datetime.now(timezone.utc).isoformat(),
            }
            conn.execute(
                """UPDATE job_logs SET status = 'failed', finished_at = ?, details_json = ?
                   WHERE id = ? AND status = 'running'""",
                [datetime.now(timezone.utc).isoformat(), json.dumps(details, ensure_ascii=False), job_id],
            )
            reconciled += 1
    typer.echo(json.dumps(make_response(
        "data.reconcile_jobs_execute", {"status": "success", "reconciled": reconciled, "plan_id": plan_id}
    ), ensure_ascii=False, indent=2, default=str))


@data_app.command("quarantine_legacy_records")
def data_quarantine_legacy_records() -> None:
    """Create a confirmation plan to quarantine unsupported legacy lineage and dividends."""
    from app.cli.protocol import create_plan
    from app.core.data_maintenance import legacy_quarantine_summary

    _, duck, sqlite = _database_context()
    summary = legacy_quarantine_summary(duck)
    result = create_plan("data.quarantine_legacy_records", summary, sqlite=sqlite)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@data_app.command("quarantine_legacy_records_execute")
def data_quarantine_legacy_records_execute(
    plan_id: str = typer.Option(..., "--plan-id", help="confirmed legacy quarantine plan ID"),
) -> None:
    """Execute a confirmed legacy-record quarantine without deleting retained evidence."""
    from app.cli.protocol import consume_confirmed_plan, make_response
    from app.core.data_maintenance import legacy_quarantine_summary, quarantine_legacy_records

    _, duck, sqlite = _database_context()
    error, planned = consume_confirmed_plan("data.quarantine_legacy_records", plan_id=plan_id, sqlite=sqlite)
    if error:
        typer.echo(json.dumps(error, ensure_ascii=False, indent=2, default=str))
        return
    current = legacy_quarantine_summary(duck)
    if current != planned:
        typer.echo(json.dumps(make_response(
            "data.quarantine_legacy_records_execute",
            error_code="E001",
            error_message="legacy record set changed after plan creation",
        ), ensure_ascii=False, indent=2))
        return
    result = quarantine_legacy_records(duck)
    result["plan_id"] = plan_id
    typer.echo(json.dumps(make_response(
        "data.quarantine_legacy_records_execute", result
    ), ensure_ascii=False, indent=2, default=str))


@data_app.command("switch_source")
def data_switch_source(
    data_type: str = typer.Argument(..., help="数据类型: balance_sheet/income_statement/cash_flow/price_daily"),
    source: str = typer.Argument(..., help="数据源: akshare_eastmoney/tdx/baostock"),
) -> None:
    """切换数据源 (M7-问题4)"""
    from app.core.config import Config, is_frozen_runtime
    Config.load()
    from app.cli.protocol import make_response
    from app.core.adapters.manager import build_adapter_priority
    import yaml

    if is_frozen_runtime():
        typer.echo(json.dumps(make_response(
            "data.switch_source", error_code="E002",
            error_message="frozen releases do not support persistent adapter configuration",
        ), ensure_ascii=False))
        return

    priorities = build_adapter_priority(None)
    if data_type not in priorities:
        typer.echo(json.dumps(make_response("data.switch_source", error_code="E001", error_message=f"未知数据类型: {data_type}"), ensure_ascii=False))
        return

    if source not in priorities[data_type]:
        typer.echo(json.dumps(make_response("data.switch_source", error_code="E001", error_message=f"数据源 {source} 不支持 {data_type}, 可用: {priorities[data_type]}"), ensure_ascii=False))
        return

    current = priorities[data_type]
    new_order = [source] + [s for s in current if s != source]
    cfg = Config.current()
    user_config = cfg.project_root / "config" / "user.yaml"
    existing = yaml.safe_load(user_config.read_text(encoding="utf-8")) if user_config.exists() else {}
    if not isinstance(existing, dict):
        existing = {}
    adapters = existing.setdefault("adapters", {})
    primary = adapters.setdefault("primary", {})
    primary[data_type] = new_order
    user_config.write_text(yaml.safe_dump(existing, allow_unicode=True, sort_keys=True), encoding="utf-8")

    typer.echo(json.dumps(make_response("data.switch_source", {"status": "ok", "data_type": data_type, "new_priority": new_order, "persisted": str(user_config)}), ensure_ascii=False))


@data_app.command("refetch")
def data_refetch(
    stock_code: str = typer.Argument(..., help="股票代码"),
    data_type: str = typer.Option("price_daily", "--type", help="数据类型"),
) -> None:
    """指定范围重抓 (M7-问题4, 危险操作, 两段式确认)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import create_plan

    operation = "data.refetch"
    plan_summary = {
        "operation": operation,
        "stock_code": stock_code,
        "data_type": data_type,
        "warning": "重抓将覆盖该股票的现有数据",
    }
    _, _, sqlite = _database_context()
    result = create_plan(operation, plan_summary, sqlite=sqlite)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@data_app.command("refetch_execute")
def data_refetch_execute(
    stock_code: str = typer.Argument(..., help="股票代码"),
    data_type: str = typer.Option("price_daily", "--type", help="数据类型"),
    plan_id: str = typer.Option(..., "--plan-id", help="已确认的重抓计划 ID"),
) -> None:
    """执行已确认的单股票重抓并保留可追溯的持久化结果。"""
    from app.cli.protocol import consume_confirmed_plan, make_response
    from app.core.update import IncrementalUpdater

    _, duck, sqlite = _database_context()
    plan_error, summary = consume_confirmed_plan("data.refetch", plan_id=plan_id, sqlite=sqlite)
    if plan_error:
        typer.echo(json.dumps(plan_error, ensure_ascii=False, indent=2))
        return
    if summary.get("stock_code") != stock_code or summary.get("data_type") != data_type:
        typer.echo(json.dumps(make_response("data.refetch_execute", error_code="E001", error_message="arguments do not match the confirmed plan"), ensure_ascii=False))
        return
    result = IncrementalUpdater(duck=duck, sqlite=sqlite).refetch_one(stock_code, data_type)
    typer.echo(json.dumps(make_response("data.refetch_execute", result), ensure_ascii=False, indent=2, default=str))


# ─── 补充缺失的 screening 命令 (M7-问题1) ───────────────────────

@screening_app.command("create")
def screening_create(
    name: str = typer.Argument(..., help="规则名称"),
    rule: str = typer.Argument(..., help="规则JSON"),
) -> None:
    """创建筛选规则 (M7-问题1)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    sqlite = _sqlite_store()
    rule_dict = json.loads(rule)
    from app.web.api.screening import resolve_rule_indicator_locks
    try:
        locks = resolve_rule_indicator_locks(sqlite, rule_dict, {})
    except ValueError as error:
        typer.echo(json.dumps(make_response("screening.create", error_code="E001", error_message=str(error)), ensure_ascii=False))
        return
    with sqlite.transaction() as conn:
        existing = conn.execute("SELECT MAX(version) FROM screening_rules WHERE name = ?", [name]).fetchone()[0]
        version = (existing or 0) + 1
        conn.execute(
            "INSERT INTO screening_rules (name, version, rule_json, locked_indicators, status) VALUES (?, ?, ?, ?, 'draft')",
            [name, version, json.dumps(rule_dict, ensure_ascii=False), json.dumps(locks, ensure_ascii=False)],
        )
    typer.echo(json.dumps(make_response("screening.create", {"status": "ok", "name": name, "version": version}), ensure_ascii=False))


@screening_app.command("export_csv")
def screening_export_csv(
    result_id: int = typer.Argument(..., help="已保存筛选结果 ID"),
    output_file: str = typer.Argument(..., help="输出CSV文件路径"),
) -> None:
    """导出CSV (M7-问题1, PRD §12.5 SC16)"""
    from app.cli.protocol import make_response
    import csv
    from app.core.data_quality import screening_readiness
    from app.web.api.screening import _csv_cell, _field_provenance
    _, duck, sqlite = _database_context()
    decision = screening_readiness(duck, sqlite)
    if not decision["ready"]:
        typer.echo(json.dumps(make_response(
            "screening.export_csv", error_code="E002", error_message="screening_data_not_ready", data=decision,
        ), ensure_ascii=False))
        return
    saved = sqlite.query("SELECT * FROM screening_results WHERE id = ?", [result_id])
    if not saved:
        typer.echo(json.dumps(make_response("screening.export_csv", error_code="E001", error_message="saved result not found"), ensure_ascii=False))
        return
    record = saved[0]
    results = json.loads(record["result_json"])

    if not results:
        typer.echo(json.dumps(make_response("screening.export_csv", {"error": "no results"}), ensure_ascii=False))
        return

    keys = json.loads(record["columns_json"])
    try:
        provenance = _field_provenance(duck, sqlite, results, keys)
    except ValueError as error:
        typer.echo(json.dumps(make_response(
            "screening.export_csv", error_code="E002", error_message=str(error),
        ), ensure_ascii=False))
        return
    rule = sqlite.query(
        "SELECT locked_indicators FROM screening_rules WHERE id = ? AND version = ?",
        [record["rule_id"], record["rule_version"]],
    )
    if not rule:
        typer.echo(json.dumps(make_response(
            "screening.export_csv", error_code="E001", error_message="saved result rule provenance is missing",
        ), ensure_ascii=False))
        return

    with open(output_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        summary = json.loads(record["confidence_summary"] or "{}")
        writer.writerow(keys + [
            "_data_date", "_rule_id", "_rule_version", "_locked_indicators",
            "_strict_only", "_field_provenance", "_entry_explanation",
        ])
        for index, row in enumerate(results):
            writer.writerow([_csv_cell(row.get(key, "")) for key in keys] + [
                record["data_date"], record["rule_id"], record["rule_version"],
                rule[0]["locked_indicators"], summary.get("strict_only", False),
                json.dumps(provenance[index], ensure_ascii=False, sort_keys=True, default=str),
                _csv_cell(row.get("_entry_explanation", "")),
            ])

    typer.echo(json.dumps(make_response("screening.export_csv", {"status": "ok", "rows": len(results), "file": output_file}), ensure_ascii=False))


@screening_app.command("add_to_watchlist")
def screening_add_to_watchlist(
    result_id: int = typer.Argument(..., help="已保存筛选结果 ID"),
    group: str = typer.Option("screening", "--group"),
) -> None:
    """将筛选结果加入自选 (M7-问题1, PRD §12.5 SC17)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.data_quality import screening_readiness
    _, duck, sqlite = _database_context()
    decision = screening_readiness(duck, sqlite)
    if not decision["ready"]:
        typer.echo(json.dumps(make_response(
            "screening.add_to_watchlist", error_code="E002", error_message="screening_data_not_ready", data=decision,
        ), ensure_ascii=False))
        return
    saved = sqlite.query("SELECT rule_id, result_json FROM screening_results WHERE id = ?", [result_id])
    if not saved:
        typer.echo(json.dumps(make_response("screening.add_to_watchlist", error_code="E001", error_message="saved result not found"), ensure_ascii=False))
        return
    record = saved[0]
    results = json.loads(record["result_json"])

    added = 0
    with sqlite.transaction() as conn:
        for row in results:
            code = row.get("stock_code", "")
            if code:
                conn.execute(
                    """INSERT INTO watchlist (stock_code, group_name, source_rule_id, source_result_id)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(stock_code, group_name) DO UPDATE SET
                         source_rule_id=excluded.source_rule_id,
                         source_result_id=excluded.source_result_id,
                         added_at=CURRENT_TIMESTAMP""",
                    [code, group, record["rule_id"], result_id],
                )
                added += 1

    typer.echo(json.dumps(make_response("screening.add_to_watchlist", {"status": "ok", "added": added}), ensure_ascii=False))


# ─── 凭据管理 (PRD §18.3 AR12) ───────────────────────────────────

@backup_app.command("store_credential")
def backup_store_credential(
    key: str = typer.Argument(..., help="凭据键名"),
    value: str = typer.Argument(..., help="凭据值"),
) -> None:
    """存储凭据到 Windows Credential Manager (PRD §18.3 AR12)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.backup.manager import CredentialManager

    ok = CredentialManager.store_credential(key, value)
    typer.echo(json.dumps(make_response("backup.store_credential", {"status": "ok" if ok else "failed", "key": key}), ensure_ascii=False))


@backup_app.command("retrieve_credential")
def backup_retrieve_credential(
    key: str = typer.Argument(...),
) -> None:
    """从 Windows Credential Manager 读取凭据"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.backup.manager import CredentialManager

    value = CredentialManager.retrieve_credential(key)
    if value:
        typer.echo(json.dumps(make_response("backup.retrieve_credential", {"status": "ok", "key": key, "value": value}), ensure_ascii=False))
    else:
        typer.echo(json.dumps(make_response("backup.retrieve_credential", {"status": "not_found", "key": key}), ensure_ascii=False))


# ─── PDF 管理命令 (PRD §14 SD9, §17, §18) ────────────────────────

@data_app.command("download_pdf")
def data_download_pdf(
    stock_code: str = typer.Argument(..., help="股票代码"),
    max_count: int = typer.Option(3, "--max", help="最多下载数量"),
) -> None:
    """下载 CNINFO 公告 PDF (PRD §14 SD9)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    mgr = _pdf_manager()
    result = mgr.download_announcement_pdfs(stock_code, max_count=max_count)
    typer.echo(json.dumps(make_response("data.download_pdf", result), ensure_ascii=False, indent=2, default=str))


@data_app.command("list_pdfs")
def data_list_pdfs(stock_code: str = typer.Argument(...)) -> None:
    """列出新下载的 PDF"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    mgr = _pdf_manager()
    files = mgr.list_local_pdfs(stock_code)
    typer.echo(json.dumps(make_response("data.list_pdfs", {"files": files, "count": len(files)}), ensure_ascii=False, default=str))


@data_app.command("archive_pdfs")
def data_archive_pdfs(
    stock_code: str = typer.Argument("", help="股票代码(空=全部)"),
    target_dir: str = typer.Option("data/archive_pdf", "--target"),
) -> None:
    """归档 PDF 到冷存储 (PRD §18.1 AR1)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    mgr = _pdf_manager()
    result = mgr.archive_pdfs(stock_code or None, target_dir)
    typer.echo(json.dumps(make_response("data.archive_pdfs", result), ensure_ascii=False, indent=2, default=str))


@data_app.command("restore_pdf")
def data_restore_pdf(
    stock_code: str = typer.Argument(...),
    filename: str = typer.Argument(...),
) -> None:
    """从冷归档恢复 PDF (PRD §18.2 AR5: 只能通过CLI)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    mgr = _pdf_manager()
    result = mgr.restore_pdf(stock_code, filename)
    typer.echo(json.dumps(make_response("data.restore_pdf", result), ensure_ascii=False, indent=2, default=str))


# ─── 校正模板命令 (PRD §17) ──────────────────────────────────────

@override_app.command("submit_template")
def override_submit_template(
    template_file: str = typer.Argument(..., help="校正模板JSON文件路径"),
) -> None:
    """提交受控JSON校正模板 (PRD §17: 草稿→校验→预览→发布 第1步)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    with open(template_file, encoding="utf-8") as f:
        template_json = f.read()

    mgr = _correction_manager()
    result = mgr.create_from_json(template_json)
    typer.echo(json.dumps(make_response("override.submit_template", result), ensure_ascii=False, indent=2, default=str))


@override_app.command("validate_template")
def override_validate_template(
    override_id: int = typer.Argument(...),
) -> None:
    """校正模板校验 (PRD §17: 第2步)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    mgr = _correction_manager()
    result = mgr.validate(override_id)
    typer.echo(json.dumps(make_response("override.validate_template", result), ensure_ascii=False, indent=2, default=str))


@override_app.command("preview_template")
def override_preview_template(
    override_id: int = typer.Argument(...),
) -> None:
    """校正模板影响预览 (PRD §17: 第3步, PRD §9.5 R7: 可预览影响面)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    mgr = _correction_manager()
    result = mgr.preview_impact(override_id)
    typer.echo(json.dumps(make_response("override.preview_template", result), ensure_ascii=False, indent=2, default=str))


@override_app.command("publish_template")
def override_publish_template(
    override_id: int = typer.Argument(...),
) -> None:
    """确认发布校正模板 (PRD §17: 第4步, PRD §9.5 R7: 与原始值分离存储)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    mgr = _correction_manager()
    result = mgr.publish(override_id)
    typer.echo(json.dumps(make_response("override.publish_template", result), ensure_ascii=False, indent=2, default=str))


@override_app.command("list_templates")
def override_list_templates() -> None:
    """列出校正模板"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response

    mgr = _correction_manager()
    templates = mgr.list_templates()
    typer.echo(json.dumps(make_response("override.list_templates", {"templates": templates, "count": len(templates)}), ensure_ascii=False, default=str))


if __name__ == "__main__":
    app()
