"""CLI 筛选命令对"规则已删 / 原地升版本"的鲁棒性（2026-11-25）。

事故来源（独立 agent 审查点名的租约外残留）：
  * `vd screening save_result` —— 规则可以删、`screening_runs` 保留为审计，
    那条 run 的 `rule_id` 成了悬空值；照抄它写 `screening_results` 会撞
    `FOREIGN KEY (rule_id) REFERENCES screening_rules(id)` → 裸
    `sqlite3.IntegrityError` 崩 CLI。web `POST /api/screening/save` 早已把同一
    情况前置成 400 `run rule was deleted`，CLI 是唯一没跟上的执行面。
  * `vd screening export_csv` —— 改条件走 `PUT /rules/{id}` 的**原地升版本**，
    历史结果里记的 `rule_version` 不再对应规则行版本号；按
    `(rule_id, rule_version)` 查 `locked_indicators` 落空 → 报
    `saved result rule provenance is missing`。web 已改成"按 `rule_id` 兜底"。
  * `vd screening add_to_watchlist` —— 清点时新发现的**同型**：`watchlist
    .source_rule_id` 同样带外键，历史库里"结果还在、规则已删"的行会让它崩。

本文件的用例都真跑 CLI 函数（不是复述实现），断言的是**用户看得见的三件事**：
退出码、中文可读错误、不抛 traceback；导出侧断言表头与内容。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer

from app.cli.main import screening_add_to_watchlist, screening_export_csv, screening_save
from app.core.config import Config
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet
from app.core.storage.sqlite_store import SQLiteStore
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data
from tests.support.seed import Seed

_RULE_JSON = json.dumps({"conditions": [], "logic": "and"}, ensure_ascii=False)
# 真实锁定结构：`screening.run` 读它时会按 (version, content_hash) 校验。
_LOCKED = json.dumps({"pe_ttm": {"version": 1, "content_hash": "a" * 64}}, ensure_ascii=False)
_COLUMNS = ["stock_code", "name", "exchange", "pe_ttm"]
_ROW = {
    "stock_code": "000001", "name": "ready", "exchange": "SZSE",
    "pe_ttm": 9.9, "_report_date": "2024-12-31",
}


@pytest.fixture
def screenable_cli(
    database_paths: DatabasePathSet,
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DuckDBStore, SQLiteStore]:
    """铺到"筛选就绪"并把 CLI 的库上下文指到这个临时库对。

    保留真实 `screening_readiness`（不 monkeypatch 门禁）：这些用例要证明的是
    "门禁通过之后的那段溯源判断"，把门禁一起打桩就测不到真实执行顺序。
    """
    Seed(duckdb_store).stock(
        "000001", name="ready", exchange="SZSE", listing_date="2020-01-01",
        is_st=False, is_suspended=False,
    )
    insert_minimum_screenable_data(duckdb_store)
    insert_matching_trading_calendar(duckdb_store, sqlite_store)

    config = Config({}, paths=database_paths)
    monkeypatch.setattr(Config, "_instance", config)
    monkeypatch.setattr(
        Config, "load", classmethod(lambda cls, config_dir=None, paths=None: config),
    )
    monkeypatch.setattr(
        "app.cli.main._database_context",
        lambda **_kwargs: (database_paths, duckdb_store, sqlite_store),
    )
    return duckdb_store, sqlite_store


def _insert_rule(sqlite: SQLiteStore, rule_id: int, *, version: int = 1) -> None:
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO screening_rules (id, name, version, rule_json, locked_indicators, status)
               VALUES (?, ?, ?, ?, ?, 'saved')""",
            [rule_id, f"rule-{rule_id}", version, _RULE_JSON, _LOCKED],
        )


def _insert_result(
    sqlite: SQLiteStore, result_id: int, rule_id: int, *, rule_version: int = 1,
) -> None:
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO screening_results
               (id, title, rule_id, rule_version, data_date, result_json, columns_json,
                sort_json, confidence_summary, base_pool_config)
               VALUES (?, ?, ?, ?, '2026-11-20', ?, ?, '[]', '{}', '{}')""",
            [result_id, f"title-{result_id}", rule_id, rule_version,
             json.dumps([_ROW], ensure_ascii=False), json.dumps(_COLUMNS)],
        )


def _insert_run(sqlite: SQLiteStore, run_id: str, rule_id: int, *, rule_version: int = 1) -> None:
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT INTO screening_runs
               (run_id, rule_id, rule_version, result_json, columns_json, sort_json,
                data_date, base_pool_config, strict_only, confidence_summary)
               VALUES (?, ?, ?, ?, ?, '[]', '2026-11-20', '{}', 0, '{}')""",
            [run_id, rule_id, rule_version,
             json.dumps([_ROW], ensure_ascii=False), json.dumps(_COLUMNS)],
        )


def _delete_rule_keeping_rows(sqlite: SQLiteStore, rule_id: int) -> None:
    """与 web `delete_rule` 同口径：本连接关外键后删规则，run/结果行原样保留。"""
    with sqlite.transaction() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM screening_rules WHERE id = ?", [rule_id])


def _response(captured: str) -> dict[str, Any]:
    return json.loads(captured.strip().splitlines()[-1])


def _assert_no_traceback(captured_err: str) -> None:
    assert "Traceback" not in captured_err, captured_err
    assert "IntegrityError" not in captured_err, captured_err


# ─── 缺陷 1：save_result 对"规则已删的 run" ───────────────────────


def test_save_result_refuses_a_run_whose_rule_was_deleted(
    screenable_cli: tuple[DuckDBStore, SQLiteStore],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, sqlite = screenable_cli
    _insert_rule(sqlite, 1)
    _insert_run(sqlite, "run-deleted-rule", 1)
    _delete_rule_keeping_rows(sqlite, 1)

    with pytest.raises(typer.Exit) as exit_info:
        screening_save(title="t1", run_id="run-deleted-rule")

    captured = capsys.readouterr()
    _assert_no_traceback(captured.err)
    assert exit_info.value.exit_code != 0
    response = _response(captured.out)
    assert response["result"]["status"] == "error"
    assert response["result"]["error_code"] == "E001"
    # 与 web `run rule was deleted` 同语义的中文可读文案（不是英文机器码）。
    assert "规则已被删除" in response["result"]["error_message"]
    assert "重新运行筛选" in response["result"]["error_message"]
    assert response["result"]["data"]["reason"] == "run_rule_deleted"
    # 失败必须不留半套写入：run 行仍在（用户还能重跑），结果表没有新增行。
    assert len(sqlite.query("SELECT 1 FROM screening_runs WHERE run_id = 'run-deleted-rule'")) == 1
    assert sqlite.query("SELECT id FROM screening_results") == []


def test_save_result_normal_path_still_saves(
    screenable_cli: tuple[DuckDBStore, SQLiteStore],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, sqlite = screenable_cli
    _insert_rule(sqlite, 2)
    _insert_run(sqlite, "run-ok", 2)

    screening_save(title="t-ok", run_id="run-ok")

    response = _response(capsys.readouterr().out)
    assert response["result"]["status"] == "ok"
    assert response["result"]["data"]["status"] == "ok"
    saved = sqlite.query("SELECT title, rule_id, rule_version FROM screening_results")
    assert [(row["title"], row["rule_id"], row["rule_version"]) for row in saved] == [
        ("t-ok", 2, 1),
    ]
    assert sqlite.query("SELECT 1 FROM screening_runs WHERE run_id = 'run-ok'") == []


# ─── 缺陷 2：export_csv 对"原地升版本"的历史结果 ─────────────────


def test_export_csv_survives_an_in_place_rule_version_bump(
    screenable_cli: tuple[DuckDBStore, SQLiteStore],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, sqlite = screenable_cli
    _insert_rule(sqlite, 3, version=1)
    _insert_result(sqlite, 3, 3, rule_version=1)
    # 模拟 web `PUT /rules/{id}` 的原地升版本：同一 id，version 1 → 2。
    with sqlite.transaction() as conn:
        conn.execute("UPDATE screening_rules SET version = 2 WHERE id = 3")
    target = tmp_path / "export.csv"

    screening_export_csv(result_id=3, output_file=str(target))

    captured = capsys.readouterr()
    _assert_no_traceback(captured.err)
    response = _response(captured.out)
    assert response["result"]["status"] == "ok", response
    assert response["result"]["data"]["rows"] == 1
    assert target.is_file()

    lines = target.read_text(encoding="utf-8-sig").splitlines()
    header = lines[0].split(",")
    assert header[:4] == _COLUMNS
    # 表头/行照旧打印**结果自己的** run-time 版本号，不是规则行上的新版本。
    row = next(__import__("csv").reader([lines[1]]))
    assert row[header.index("_rule_id")] == "3"
    assert row[header.index("_rule_version")] == "1"
    # 指标锁按 rule_id 兜底取当前行，所以这格必须有内容而不是空的。
    assert "content_hash" in row[header.index("_locked_indicators")]
    assert row[0] == "000001"
    assert row[header.index("pe_ttm")] == "9.9"


def test_export_csv_still_refuses_when_the_rule_row_is_gone_entirely(
    screenable_cli: tuple[DuckDBStore, SQLiteStore],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """兜底只放宽 version，不放宽"规则整条记录都没了"。"""
    _, sqlite = screenable_cli
    _insert_rule(sqlite, 4)
    _insert_result(sqlite, 4, 4)
    _delete_rule_keeping_rows(sqlite, 4)
    target = tmp_path / "gone.csv"

    screening_export_csv(result_id=4, output_file=str(target))

    response = _response(capsys.readouterr().out)
    assert response["result"]["status"] == "error"
    assert response["result"]["error_code"] == "E001"
    assert response["result"]["error_message"] == "saved result rule provenance is missing"
    assert not target.exists()


def test_export_csv_normal_path_matches_the_rule_version(
    screenable_cli: tuple[DuckDBStore, SQLiteStore],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, sqlite = screenable_cli
    _insert_rule(sqlite, 5, version=1)
    _insert_result(sqlite, 5, 5, rule_version=1)
    target = tmp_path / "normal.csv"

    screening_export_csv(result_id=5, output_file=str(target))

    captured = capsys.readouterr()
    _assert_no_traceback(captured.err)
    assert _response(captured.out)["result"]["status"] == "ok"
    assert target.read_text(encoding="utf-8-sig").splitlines()[0].startswith("stock_code,")


# ─── 同型清点：add_to_watchlist 的外键源 ─────────────────────────


def test_add_to_watchlist_tolerates_a_deleted_source_rule(
    screenable_cli: tuple[DuckDBStore, SQLiteStore],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, sqlite = screenable_cli
    _insert_rule(sqlite, 6)
    _insert_result(sqlite, 6, 6)
    _delete_rule_keeping_rows(sqlite, 6)

    screening_add_to_watchlist(result_id=6, group="g1")

    captured = capsys.readouterr()
    _assert_no_traceback(captured.err)
    response = _response(captured.out)
    assert response["result"]["status"] == "ok", response
    assert response["result"]["data"]["added"] == 1
    assert response["result"]["data"]["rule_provenance"] == "rule_deleted"
    rows = sqlite.query("SELECT stock_code, group_name, source_rule_id, source_result_id FROM watchlist")
    assert [(row["stock_code"], row["group_name"], row["source_rule_id"], row["source_result_id"])
            for row in rows] == [("000001", "g1", None, 6)]


def test_add_to_watchlist_keeps_the_rule_source_when_the_rule_exists(
    screenable_cli: tuple[DuckDBStore, SQLiteStore],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, sqlite = screenable_cli
    _insert_rule(sqlite, 7)
    _insert_result(sqlite, 7, 7)

    screening_add_to_watchlist(result_id=7, group="g2")

    response = _response(capsys.readouterr().out)
    assert response["result"]["status"] == "ok"
    assert "rule_provenance" not in response["result"]["data"]
    rows = sqlite.query("SELECT source_rule_id, source_result_id FROM watchlist")
    assert [(row["source_rule_id"], row["source_result_id"]) for row in rows] == [(7, 7)]
