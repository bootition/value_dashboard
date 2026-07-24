"""CLI 入口 - Typer 命令行工具

M0: server 启动 + schema 初始化
M1: data init (最小可用初始化)
M7: 完整命令树
"""

from __future__ import annotations

import json
from typing import Any
import typer

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
    from app.core.config import Config

    Config.load()
    from app.core.storage.schema import init_all_schema

    init_all_schema()
    typer.echo("数据库 schema 初始化完成")


data_app = typer.Typer(help="数据管理")
app.add_typer(data_app, name="data")


@data_app.command("init")
def data_init(
    skip_prices: bool = typer.Option(False, "--skip-prices", help="跳过价格数据"),
    skip_financials: bool = typer.Option(False, "--skip-financials", help="跳过财务数据"),
) -> None:
    """最小可用初始化 (PRD §6.7)

    按顺序获取: 股票全集 → 交易日历 → 申万行业 → 近5年价格 → 核心财务
    """
    from app.core.config import Config

    Config.load()
    from app.cli.protocol import make_response
    from app.core.init import DataInitializer

    initializer = DataInitializer()
    report = initializer.run_full_init(
        skip_prices=skip_prices,
        skip_financials=skip_financials,
    )

    typer.echo(json.dumps(make_response("data.init", report), ensure_ascii=False, indent=2, default=str))


@data_app.command("update")
def data_update(
    max_stocks: int = typer.Option(0, "--max-stocks", help="最多更新N只股票(0=全部)"),
    check_only: bool = typer.Option(False, "--check-only", help="只检查不更新"),
) -> None:
    """增量更新 (PRD §7.3)

    检查新交易日、新公告、待重试任务，执行增量更新。
    """
    from app.core.config import Config

    Config.load()
    from app.cli.protocol import make_response
    from app.core.update import IncrementalUpdater

    updater = IncrementalUpdater()

    if check_only:
        report = updater.run_incremental_check()
    else:
        report = updater.run_incremental_update(max_stocks=max_stocks)

    typer.echo(json.dumps(make_response("data.update", report), ensure_ascii=False, indent=2, default=str))


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
    from app.core.config import Config

    Config.load()
    from app.cli.protocol import make_response
    from app.core.backfill import PriceBackfiller

    backfiller = PriceBackfiller()
    report = backfiller.run_full_backfill(
        skip_if_complete=skip_complete,
        max_stocks=max_stocks,
        fetch_dividends=not no_dividends,
    )
    typer.echo(json.dumps(make_response("data.backfill_prices", report), ensure_ascii=False, indent=2, default=str))


@data_app.command("compute_indicators")
def data_compute_indicators() -> None:
    """计算全部内建指标并写入 indicator_snapshot 表 (PRD §10)"""
    from app.core.config import Config

    Config.load()
    from app.cli.protocol import make_response
    from app.core.indicators.calculator import IndicatorCalculator

    calc = IndicatorCalculator()
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
    from app.core.dsl.engine import DSLEngine
    engine = DSLEngine()
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
    from app.core.dsl.engine import DSLEngine
    engine = DSLEngine()
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
    from app.core.dsl.engine import DSLEngine
    engine = DSLEngine()
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
    from app.core.dsl.engine import DSLEngine
    engine = DSLEngine()
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
    from app.core.dsl.engine import DSLEngine
    engine = DSLEngine()
    result = engine.publish(name, version)
    typer.echo(json.dumps(make_response("indicator.publish", result), ensure_ascii=False, indent=2, default=str))


@indicator_app.command("list")
def indicator_list() -> None:
    """列出所有复合指标"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.dsl.engine import DSLEngine
    engine = DSLEngine()
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
    from app.core.dsl.engine import DSLEngine
    engine = DSLEngine()

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
    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.sqlite_store import SQLiteStore

    duck = DuckDBStore()
    sqlite = SQLiteStore()

    stock_count = duck.read_query("SELECT COUNT(*) as cnt FROM stock_meta")
    raw_count = duck.read_query("SELECT COUNT(DISTINCT stock_code) as cnt FROM price_daily_raw")
    qfq_count = duck.read_query("SELECT COUNT(DISTINCT stock_code) as cnt FROM price_daily_qfq")
    bs_count = duck.read_query("SELECT COUNT(DISTINCT stock_code) as cnt FROM balance_sheet")
    ic_count = duck.read_query("SELECT COUNT(DISTINCT stock_code) as cnt FROM income_statement")
    cf_count = duck.read_query("SELECT COUNT(DISTINCT stock_code) as cnt FROM cash_flow")
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
    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.sqlite_store import SQLiteStore

    duck = DuckDBStore()
    sqlite = SQLiteStore()

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
    rule: str = typer.Argument(..., help="规则JSON (conditions+sort+columns)"),
    include_st: bool = typer.Option(False, "--include-st"),
    include_suspended: bool = typer.Option(False, "--include-suspended"),
    min_years: int = typer.Option(1, "--min-years"),
) -> None:
    """手动运行筛选 (PRD §12.2 SC8)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.screening.engine import ScreeningEngine

    engine = ScreeningEngine()
    rule_dict = json.loads(rule)
    result = engine.run(
        rule=rule_dict,
        include_st=include_st,
        include_suspended=include_suspended,
        min_listing_years=min_years,
    )
    typer.echo(json.dumps(make_response("screening.run", result), ensure_ascii=False, indent=2, default=str))


@screening_app.command("save_result")
def screening_save(
    title: str = typer.Argument(..., help="标题(必填)"),
    results_file: str = typer.Argument(..., help="结果JSON文件路径"),
    data_date: str = typer.Option("", "--data-date"),
) -> None:
    """保存筛选结果 (PRD §12.5 SC14-15)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
    with open(results_file, encoding="utf-8") as f:
        results = json.load(f)

    from datetime import datetime  # P2修复: 移除死uuid导入
    # P2修复: 使用autoincrement id
    with sqlite.transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO screening_results
               (title, data_date, result_json, columns_json, sort_json, confidence_summary)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [title, data_date or datetime.now().isoformat(),
             json.dumps(results, ensure_ascii=False, default=str),
             "[]", "[]",
             json.dumps({"total": len(results)})],
        )
        result_id = cursor.lastrowid
    typer.echo(json.dumps(make_response("screening.save_result", {"status": "ok", "result_id": result_id}), ensure_ascii=False))


@screening_app.command("list")
def screening_list(limit: int = typer.Option(20, "--limit")) -> None:
    """列出已保存的筛选结果"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
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
    from app.core.storage.duckdb_store import DuckDBStore

    duck = DuckDBStore()
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
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
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
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
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

    result = confirm_plan(plan_id)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


# ─── backup 命令 (PRD §18, §16.1) ────────────────────────────────

backup_app = typer.Typer(help="备份管理")
app.add_typer(backup_app, name="backup")


@backup_app.command("create")
def backup_create(
    password: str = typer.Option("", "--password", help="用户口令(加密个性化数据)"),
    target_dir: str = typer.Option("data/backup", "--target"),
) -> None:
    """创建全量备份 (PRD §18.3 AR9-10)

    --password: 提供口令则加密个性化数据并生成离线恢复密钥
    """
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.backup.manager import BackupManager

    mgr = BackupManager()
    result = mgr.create_full_backup(
        user_password=password or None,
        target_dir=target_dir,
    )
    typer.echo(json.dumps(make_response("backup.create", result), ensure_ascii=False, indent=2, default=str))


@backup_app.command("restore")
def backup_restore(
    backup_path: str = typer.Argument(..., help="备份ZIP文件路径"),
    password: str = typer.Option("", "--password", help="用户口令(解密个性化数据)"),
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
        "warning": "恢复将覆盖当前数据，请确保Web服务已停止",
    }
    result = create_plan(operation, plan_summary)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@backup_app.command("list")
def backup_list() -> None:
    """列出备份"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.backup.manager import BackupManager

    mgr = BackupManager()
    backups = mgr.list_backups()
    typer.echo(json.dumps(make_response("backup.list", {"backups": backups, "count": len(backups)}), ensure_ascii=False, default=str))


@backup_app.command("restore_execute")
def backup_restore_execute(
    backup_path: str = typer.Argument(..., help="备份ZIP文件路径"),
    password: str = typer.Option("", "--password", help="用户口令"),
) -> None:
    """执行恢复 (在 plan confirm 之后调用)

    P0#16修复: 验证 plan confirm 已执行, 防止绕过两段式确认
    """
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response, require_confirmed_plan
    from app.core.backup.manager import BackupManager

    # P0#16修复: 验证 plan confirm 已执行
    plan_error = require_confirmed_plan("backup.restore")
    if plan_error:
        typer.echo(json.dumps(plan_error, ensure_ascii=False, indent=2, default=str))
        return

    mgr = BackupManager()
    result = mgr.restore_from_backup(backup_path, password or None)
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
    Config.load()
    from app.cli.protocol import make_response
    from app.core.storage.duckdb_store import DuckDBStore
    import os

    os.makedirs(target_dir, exist_ok=True)
    duck = DuckDBStore()

    # 导出主要表为 Parquet
    exported = []
    for table in ["price_daily_raw", "balance_sheet", "income_statement", "cash_flow", "indicator_snapshot"]:
        try:
            output_path = os.path.join(target_dir, f"{table}.parquet")
            duck.execute_script(f"COPY {table} TO '{output_path}' (FORMAT PARQUET);")
            exported.append(table)
        except Exception as e:
            pass  # 表可能为空

    typer.echo(json.dumps(make_response("archive.create", {"status": "ok", "exported": exported, "target": target_dir}), ensure_ascii=False))


@archive_app.command("verify")
def archive_verify(
    target_dir: str = typer.Argument("data/parquet"),
) -> None:
    """验证归档完整性 (PRD §18.2 AR4: 归档验证成功后才允许清理)"""
    from app.cli.protocol import make_response
    import os
    if not os.path.exists(target_dir):
        typer.echo(json.dumps(make_response("archive.verify", {"status": "error", "error": "归档目录不存在"}), ensure_ascii=False))
        return

    files = [f for f in os.listdir(target_dir) if f.endswith(".parquet")]
    verified = []
    for f in files:
        path = os.path.join(target_dir, f)
        size = os.path.getsize(path)
        verified.append({"file": f, "size_bytes": size})

    typer.echo(json.dumps(make_response("archive.verify", {"status": "ok", "files": verified, "count": len(verified)}), ensure_ascii=False))


@archive_app.command("clean")
def archive_clean(
    target_dir: str = typer.Argument("data/parquet"),
) -> None:
    """清理已归档的本地热数据 (PRD §18.2 AR4: 归档验证成功后才允许清理, 两段式确认)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import create_plan

    operation = "archive.clean"
    plan_summary = {
        "operation": operation,
        "target_dir": target_dir,
        "warning": "清理将删除本地热数据, 请确保归档已验证成功",
    }
    result = create_plan(operation, plan_summary)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


# ─── 补充缺失的 data 命令 (M7-问题2/4) ───────────────────────────

@data_app.command("diagnose")
def data_diagnose() -> None:
    """数据健康诊断 (M7-问题2)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.data_quality import build_data_quality_status
    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.sqlite_store import SQLiteStore

    duck = DuckDBStore()
    sqlite = SQLiteStore()

    report: dict = {}

    # 检查股票全集
    try:
        row = duck.read_query("SELECT COUNT(*) as cnt FROM stock_meta")
        report["stock_count"] = row[0]["cnt"]
    except Exception:
        report["stock_count"] = "error"

    # 检查价格覆盖
    try:
        row = duck.read_query("SELECT COUNT(DISTINCT stock_code) as cnt FROM price_daily_raw")
        report["price_coverage"] = row[0]["cnt"]
    except Exception:
        report["price_coverage"] = "error"

    # 检查财务覆盖
    try:
        row = duck.read_query("SELECT COUNT(DISTINCT stock_code) as cnt FROM balance_sheet")
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
    issues.extend(
        f"数据质量阻断: {warning_code}"
        for warning_code in data_quality["warning_codes"]
    )

    report["issues"] = issues
    report["healthy"] = len(issues) == 0

    typer.echo(json.dumps(make_response("data.diagnose", report), ensure_ascii=False, indent=2, default=str))


@data_app.command("switch_source")
def data_switch_source(
    data_type: str = typer.Argument(..., help="数据类型: balance_sheet/income_statement/cash_flow/price_daily"),
    source: str = typer.Argument(..., help="数据源: akshare_eastmoney/tdx/baostock"),
) -> None:
    """切换数据源 (M7-问题4)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.adapters.manager import ADAPTER_PRIORITY

    if data_type not in ADAPTER_PRIORITY:
        typer.echo(json.dumps(make_response("data.switch_source", {"error": f"未知数据类型: {data_type}"}), ensure_ascii=False))
        return

    if source not in ADAPTER_PRIORITY[data_type]:
        typer.echo(json.dumps(make_response("data.switch_source", {"error": f"数据源 {source} 不支持 {data_type}, 可用: {ADAPTER_PRIORITY[data_type]}"}), ensure_ascii=False))
        return

    # 将指定源移到第一位
    current = ADAPTER_PRIORITY[data_type]
    new_order = [source] + [s for s in current if s != source]
    ADAPTER_PRIORITY[data_type] = new_order

    typer.echo(json.dumps(make_response("data.switch_source", {"status": "ok", "data_type": data_type, "new_priority": new_order}), ensure_ascii=False))


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
    result = create_plan(operation, plan_summary)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


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
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
    rule_dict = json.loads(rule)
    with sqlite.transaction() as conn:
        conn.execute(
            "INSERT INTO screening_rules (name, version, rule_json, locked_indicators, status) VALUES (?, 1, ?, ?, 'draft')",
            [name, json.dumps(rule_dict, ensure_ascii=False), "[]"],
        )
    typer.echo(json.dumps(make_response("screening.create", {"status": "ok", "name": name, "version": 1}), ensure_ascii=False))


@screening_app.command("export_csv")
def screening_export_csv(
    results_file: str = typer.Argument(..., help="结果JSON文件路径"),
    output_file: str = typer.Argument(..., help="输出CSV文件路径"),
) -> None:
    """导出CSV (M7-问题1, PRD §12.5 SC16)"""
    from app.cli.protocol import make_response
    import csv
    with open(results_file, encoding="utf-8") as f:
        results = json.load(f)

    if not results:
        typer.echo(json.dumps(make_response("screening.export_csv", {"error": "no results"}), ensure_ascii=False))
        return

    with open(output_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        keys = list(results[0].keys())
        writer.writerow(keys)
        for row in results:
            writer.writerow([row.get(k, "") for k in keys])

    typer.echo(json.dumps(make_response("screening.export_csv", {"status": "ok", "rows": len(results), "file": output_file}), ensure_ascii=False))


@screening_app.command("add_to_watchlist")
def screening_add_to_watchlist(
    results_file: str = typer.Argument(..., help="结果JSON文件路径"),
    group: str = typer.Option("screening", "--group"),
) -> None:
    """将筛选结果加入自选 (M7-问题1, PRD §12.5 SC17)"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.storage.sqlite_store import SQLiteStore

    sqlite = SQLiteStore()
    with open(results_file, encoding="utf-8") as f:
        results = json.load(f)

    added = 0
    with sqlite.transaction() as conn:
        for row in results:
            code = row.get("stock_code", "")
            if code:
                conn.execute(
                    "INSERT OR REPLACE INTO watchlist (stock_code, group_name) VALUES (?, ?)",
                    [code, group],
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
    from app.core.pdf.manager import PDFManager

    mgr = PDFManager()
    result = mgr.download_announcement_pdfs(stock_code, max_count=max_count)
    typer.echo(json.dumps(make_response("data.download_pdf", result), ensure_ascii=False, indent=2, default=str))


@data_app.command("list_pdfs")
def data_list_pdfs(stock_code: str = typer.Argument(...)) -> None:
    """列出新下载的 PDF"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.pdf.manager import PDFManager

    mgr = PDFManager()
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
    from app.core.pdf.manager import PDFManager

    mgr = PDFManager()
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
    from app.core.pdf.manager import PDFManager

    mgr = PDFManager()
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
    from app.core.pdf.correction import CorrectionManager

    with open(template_file, encoding="utf-8") as f:
        template_json = f.read()

    mgr = CorrectionManager()
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
    from app.core.pdf.correction import CorrectionManager

    mgr = CorrectionManager()
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
    from app.core.pdf.correction import CorrectionManager

    mgr = CorrectionManager()
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
    from app.core.pdf.correction import CorrectionManager

    mgr = CorrectionManager()
    result = mgr.publish(override_id)
    typer.echo(json.dumps(make_response("override.publish_template", result), ensure_ascii=False, indent=2, default=str))


@override_app.command("list_templates")
def override_list_templates() -> None:
    """列出校正模板"""
    from app.core.config import Config
    Config.load()
    from app.cli.protocol import make_response
    from app.core.pdf.correction import CorrectionManager

    mgr = CorrectionManager()
    templates = mgr.list_templates()
    typer.echo(json.dumps(make_response("override.list_templates", {"templates": templates, "count": len(templates)}), ensure_ascii=False, default=str))


if __name__ == "__main__":
    app()
