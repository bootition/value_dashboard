"""指标空值原因（null reason）回归：reports/119 §5 P2-1。

背景：`indicator_snapshot`（61 列）/ `indicator_ext`（60 列）里的 NULL 混了
6 种完全不同的含义，前端统一渲染成「—」—— 用户分不清「没有」和「坏了」，
我们自己也失去自检信号。本模块锁死四件事：

1. **枚举单一真相**：6 类原因 + 中文文案齐全，且每个可筛选字段能归属到域；
2. **清扫器分类**：NOT_APPLICABLE / INVALID_DENOMINATOR / NOT_COMPUTED
   （输入缺失、报告期未对齐）/ NOT_DISPATCHED 各自在该判的地方判、不该判的地方闭嘴；
3. **稀疏写入**：只写「NULL 且原因已知」的单元格，幂等、可整域刷新（旧原因会被清掉）；
4. **API 聚合**：`{field: {reason: count}}` 的形状与 domain/report_date 过滤。

纪律（与实现同源）：**判不出就不写**。测试里专门有反例断言 —— 有值的单元格
一行都不许进原因表，无证据的 NULL 也不许凭空归类。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.core.quality import null_reason_sweeper as sweeper  # noqa: E402
from app.core.quality.null_reasons import (  # noqa: E402
    DOMAIN_EXT,
    DOMAIN_SNAPSHOT,
    NULL_REASON_DESCRIPTIONS,
    NULL_REASON_LABELS,
    SOURCE_DOMAINS,
    NullReason,
    NullReasonRow,
    NullReasonTableMissing,
    domain_fields,
    null_reason_table_exists,
    reason_distribution,
    upsert_reasons,
)
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.web.api import data_status  # noqa: E402

SNAPSHOT_PERIOD = date(2026, 9, 30)
EXT_PERIOD = date(2026, 6, 30)
EXT_PREVIOUS_PERIOD = date(2026, 3, 31)

EXPECTED_REASONS = {
    "SOURCE_EMPTY",
    "NOT_APPLICABLE",
    "INVALID_DENOMINATOR",
    "NOT_DISPATCHED",
    "MASKED_UNTRUSTED",
    "NOT_COMPUTED",
}


# ─── 测试辅助 ──────────────────────────────────────────────────────


def _insert(store: DuckDBStore, table: str, row: dict[str, Any]) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    with store.write_connection() as connection:
        # 留（第 4 类·多站点缺口）：`_insert()` 的表名与列清单都是**参数**，
        connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            list(row.values()),
        )


def _insert_snapshot(store: DuckDBStore, stock_code: str, **values: Any) -> None:
    _insert(
        store,
        "indicator_snapshot",
        {"stock_code": stock_code, "report_date": SNAPSHOT_PERIOD, **values},
    )


def _insert_ext(store: DuckDBStore, stock_code: str, report_date: date, **values: Any) -> None:
    _insert(
        store,
        "indicator_ext",
        {
            "stock_code": stock_code,
            "report_date": report_date,
            "calculated_at": "2026-06-30 12:00:00",
            "source": "derived_calculator",
            "data_version": 1,
            **values,
        },
    )


def _stored(store: DuckDBStore) -> list[dict[str, Any]]:
    """稀疏表的全部行，按生产查询的稳定顺序。

    生产侧只保留聚合读入口 `reason_distribution`（`reasons_for` 的逐行明细查询没有
    消费方，已删除），所以这里直接读表 —— 这些用例验的是**写进库里的内容**，
    不是查询层。
    """
    return store.read_query(
        "SELECT stock_code, report_date, field_name, reason_code, detail "
        "FROM indicator_null_reason ORDER BY report_date DESC, field_name, stock_code"
    )


def _stored_for(store: DuckDBStore, *field_names: str) -> list[dict[str, Any]]:
    wanted = set(field_names)
    return [row for row in _stored(store) if row["field_name"] in wanted]


def _reasons_map(store: DuckDBStore) -> dict[tuple[str, str], str]:
    """(stock_code, field_name) → reason_code，方便断言。"""
    return {(row["stock_code"], row["field_name"]): row["reason_code"] for row in _stored(store)}


# ─── 1. 枚举与域映射 ───────────────────────────────────────────────


def test_enum_covers_exactly_the_six_meanings() -> None:
    assert {reason.value for reason in NullReason} == EXPECTED_REASONS


def test_every_reason_has_label_and_description() -> None:
    for reason in NullReason:
        assert NULL_REASON_LABELS[reason].strip()
        assert NULL_REASON_DESCRIPTIONS[reason].strip()
    assert set(NULL_REASON_LABELS) == set(NullReason)


def test_screening_fields_map_to_disjoint_domains() -> None:
    """域划分的结构性断言（**不写魔法数字**）。

    2026-11-17（`reports/133` R1）：这里原来是 `== 61 / == 55` 两个硬编码计数，
    ROCE 落地加列后立刻变成 HEAD 上的假失败——"字段清单来自 engine 不硬编码"
    这句话在注释里写了，断言里却硬编码了。现在改成：
    ① 两域都非空；② 已知锚点字段各自归域；③ 两域不相交。
    这样新增指标列不会再把测试打红，而"域划分被破坏"仍然会红。
    """
    snapshot_fields = domain_fields(DOMAIN_SNAPSHOT)
    ext_fields = domain_fields(DOMAIN_EXT)
    assert snapshot_fields and ext_fields
    # 锚点：主快照域对外暴露的 ROCE 列 + 扩展域的自由现金流（两域各一个代表）
    # 注意：`roce_*_years` / `roce_eligible` 是**口径内部门禁**，有意不进 SNAPSHOT_COLUMNS
    # （engine.py:54 的注释 + v46 裁定），所以不能拿它们当锚点。
    assert {"roce_5y_median", "roce_10y_median"} <= snapshot_fields
    assert not ({"roce_eligible", "roce_5y_years", "roce_10y_years", "roce_20y_years"} & snapshot_fields), (
        "资格/年数列是内部门禁，不应对用户暴露；若要暴露需先改 engine.py 注释与产品口径"
    )
    assert {"free_cash_flow", "total_asset_turnover"} & ext_fields
    assert not snapshot_fields & ext_fields, "同一字段名不许跨域（域划分必须互不相交）"


def test_unknown_domain_is_rejected() -> None:
    with pytest.raises(ValueError):
        domain_fields("quarterly")


# ─── 2. 稀疏写入 / 幂等 / 整域刷新 ─────────────────────────────────


def test_upsert_writes_only_given_cells_and_is_idempotent(duckdb_store: DuckDBStore) -> None:
    rows = [
        NullReasonRow("000001", SNAPSHOT_PERIOD, "goodwill_ratio", NullReason.NOT_APPLICABLE, "无商誉"),
        NullReasonRow("000002", SNAPSHOT_PERIOD, "goodwill_ratio", NullReason.NOT_APPLICABLE, "无商誉"),
    ]
    assert upsert_reasons(duckdb_store, rows) == 2
    # 重复写同一批：不新增行（PK 幂等），也不报错
    assert upsert_reasons(duckdb_store, rows) == 2
    stored = _stored(duckdb_store)
    assert len(stored) == 2
    assert {(row["stock_code"], row["reason_code"]) for row in stored} == {
        ("000001", "NOT_APPLICABLE"),
        ("000002", "NOT_APPLICABLE"),
    }
    # 稀疏：没有传进来的单元格一行也不写
    assert _stored_for(duckdb_store, "roe") == []


def test_upsert_rejects_unknown_reason_code(duckdb_store: DuckDBStore) -> None:
    with pytest.raises(ValueError):
        upsert_reasons(
            duckdb_store,
            [NullReasonRow("000001", SNAPSHOT_PERIOD, "roe", "GUESSED", None)],  # type: ignore[arg-type]
        )


def test_replace_scope_drops_stale_reasons(duckdb_store: DuckDBStore) -> None:
    upsert_reasons(
        duckdb_store,
        [NullReasonRow("000001", SNAPSHOT_PERIOD, "roe", NullReason.INVALID_DENOMINATOR, "旧")],
    )
    # 字段修好之后整域刷新：旧原因必须消失，否则表会变成"历史错误永久挂在字段上"
    upsert_reasons(
        duckdb_store,
        [NullReasonRow("000002", SNAPSHOT_PERIOD, "goodwill_ratio", NullReason.NOT_APPLICABLE)],
        replace_scope=(SNAPSHOT_PERIOD, DOMAIN_SNAPSHOT),
    )
    stored = _stored(duckdb_store)
    assert [(row["stock_code"], row["field_name"]) for row in stored] == [
        ("000002", "goodwill_ratio")
    ]


def test_missing_table_degrades_without_ddl(duckdb_store: DuckDBStore) -> None:
    """正式库 schema 未迁移时：读侧返回「尚未生成」，写侧明确拒绝（不擅自 DDL）。"""
    with duckdb_store.write_connection() as connection:
        connection.execute("DROP TABLE indicator_null_reason")
    assert null_reason_table_exists(duckdb_store) is False

    payload = reason_distribution(duckdb_store)
    assert payload["table_ready"] is False
    assert payload["fields"] == {} and payload["total_cells"] == 0
    with pytest.raises(NullReasonTableMissing):
        upsert_reasons(
            duckdb_store,
            [NullReasonRow("000001", SNAPSHOT_PERIOD, "roe", NullReason.NOT_APPLICABLE)],
        )


# ─── 3. 清扫器：snapshot 域分类 ────────────────────────────────────


def test_snapshot_sweep_classifies_with_evidence_and_skips_the_rest(
    duckdb_store: DuckDBStore,
) -> None:
    # A: 商誉未列示 → NOT_APPLICABLE
    _insert_snapshot(duckdb_store, "600001", goodwill_ratio=None)
    _insert(duckdb_store, "balance_sheet", {"stock_code": "600001",
                                            "report_date": SNAPSHOT_PERIOD,
                                            "total_assets": 100.0, "goodwill": None})
    # B: 没有流动资产负债表项 → NOT_COMPUTED（输入缺失）
    _insert_snapshot(duckdb_store, "600002", current_ratio=None)
    _insert(duckdb_store, "balance_sheet", {"stock_code": "600002",
                                            "report_date": SNAPSHOT_PERIOD,
                                            "total_assets": 100.0})
    # C: 权益为负 → INVALID_DENOMINATOR
    _insert_snapshot(duckdb_store, "600003", roe=None)
    _insert(duckdb_store, "balance_sheet", {"stock_code": "600003",
                                            "report_date": SNAPSHOT_PERIOD,
                                            "total_equity": -20.0, "total_equity_parent": -20.0})
    # D: 全历史无分红 → NOT_APPLICABLE（TTM 股息率）
    _insert_snapshot(duckdb_store, "600004", ttm_dividend_yield=None)
    # E: 有值 → 永不写；pe_ttm 空但无证据 → 不许瞎猜
    _insert_snapshot(duckdb_store, "600005", goodwill_ratio=0.12, pe_ttm=None)

    dry = sweeper.sweep(duckdb_store, domain=DOMAIN_SNAPSHOT)

    assert dry.rows_scanned == 5
    assert dry.report_date == SNAPSHOT_PERIOD
    assert dry.dry_run is True
    assert dry.written_rows == 0, "dry-run 绝不写库"
    assert _stored(duckdb_store) == [], "dry-run 不许留下任何一行"
    # 五只都没给 pe_ttm，但没有任何确定性证据 → 全部计入"判不出"
    assert dry.unclassified["pe_ttm"] == 5

    written = sweeper.sweep(duckdb_store, domain=DOMAIN_SNAPSHOT, write=True)
    assert written.written_rows == dry.planned_rows > 0
    reasons = _reasons_map(duckdb_store)
    assert reasons[("600001", "goodwill_ratio")] == "NOT_APPLICABLE"
    assert reasons[("600002", "current_ratio")] == "NOT_COMPUTED"
    assert reasons[("600003", "roe")] == "INVALID_DENOMINATOR"
    assert reasons[("600004", "ttm_dividend_yield")] == "NOT_APPLICABLE"
    # 有值的单元格一行都不许进原因表
    assert ("600005", "goodwill_ratio") not in reasons
    # 无证据的 NULL 也不许凭空归类
    assert ("600005", "pe_ttm") not in reasons


def test_snapshot_sweep_writes_when_asked_and_purges_stale(
    duckdb_store: DuckDBStore,
) -> None:
    _insert_snapshot(duckdb_store, "600001", goodwill_ratio=None)
    _insert(duckdb_store, "balance_sheet", {"stock_code": "600001",
                                            "report_date": SNAPSHOT_PERIOD,
                                            "total_assets": 100.0, "goodwill": None})
    written = sweeper.sweep(duckdb_store, domain=DOMAIN_SNAPSHOT, write=True)
    assert written.written_rows >= 1
    assert _reasons_map(duckdb_store)[("600001", "goodwill_ratio")] == "NOT_APPLICABLE"

    # 商誉补上后重扫：reason 必须消失（否则"修好了"没人知道）
    with duckdb_store.write_connection() as connection:
        connection.execute(
            "UPDATE indicator_snapshot SET goodwill_ratio = 0.05 WHERE stock_code = '600001'"
        )
        connection.execute(
            "UPDATE balance_sheet SET goodwill = 10 WHERE stock_code = '600001'"
        )
    sweeper.sweep(duckdb_store, domain=DOMAIN_SNAPSHOT, write=True)
    assert ("600001", "goodwill_ratio") not in _reasons_map(duckdb_store)


# ─── 4. 清扫器：ext 域分类（杠杆 / 分母 / 输入缺失 / 报告期未对齐） ──


def test_ext_sweep_classifies_leverage_denominator_and_missing_inputs(
    duckdb_store: DuckDBStore,
) -> None:
    # snapshot 报告期与 ext 对齐，专门隔离出"规则本身"的行为
    for stock_code in ("600001", "600002", "600003"):
        _insert_snapshot(duckdb_store, stock_code, latest_close=10.0, report_date=EXT_PERIOD)

    # A: 亏损 → 杠杆不适用；营业利润为负 → 现金含量不适用
    _insert_ext(
        duckdb_store,
        "600001",
        EXT_PERIOD,
        leverage_financial=None,
        ocf_to_operating_profit=None,
        ebit=-1.0,
        bps=1.0,
    )
    _insert(duckdb_store, "income_statement", {"stock_code": "600001",
                                              "report_date": EXT_PERIOD,
                                              "total_profit": -5.0,
                                              "operating_profit": -2.0})
    # B: 研发费用缺失 → NOT_COMPUTED（输入缺失）
    _insert_ext(duckdb_store, "600002", EXT_PERIOD, rd_expense_ratio=None, bps=2.0)
    _insert(duckdb_store, "income_statement", {"stock_code": "600002",
                                              "report_date": EXT_PERIOD,
                                              "revenue": 100.0, "rd_expenses": None})
    # C: EBITDA 为负 → EV/EBITDA 分母无效
    _insert_ext(duckdb_store, "600003", EXT_PERIOD, ev_ebitda=None, ebitda=-3.0, bps=3.0)
    _insert(duckdb_store, "income_statement", {"stock_code": "600003",
                                              "report_date": EXT_PERIOD,
                                              "revenue": 100.0})

    result = sweeper.sweep(duckdb_store, domain=DOMAIN_EXT, write=True)

    assert result.report_date == EXT_PERIOD
    assert result.report_period_misaligned is False
    reasons = _reasons_map(duckdb_store)
    assert reasons[("600001", "leverage_financial")] == "NOT_APPLICABLE"
    assert reasons[("600001", "ocf_to_operating_profit")] == "NOT_APPLICABLE"
    assert reasons[("600002", "rd_expense_ratio")] == "NOT_COMPUTED"
    assert reasons[("600003", "ev_ebitda")] == "INVALID_DENOMINATOR"
    # 三只都有值 → 任何一行都不该被写
    assert all(field != "bps" for _, field in reasons)
    # NOT_APPLICABLE 的 detail 说清"为什么对公司不适用"，而不是干巴巴一个码
    detail = {row["stock_code"]: row["detail"]
              for row in _stored_for(duckdb_store, "leverage_financial")}
    assert "利润总额" in (detail["600001"] or "")


def test_ext_sweep_marks_report_period_misalignment_for_empty_fields(
    duckdb_store: DuckDBStore,
) -> None:
    # snapshot 比 ext 新一期 = 2026-11-14 P0 的形态（扩展指标没按新报告期重算）
    _insert_snapshot(duckdb_store, "600001", latest_close=10.0)
    _insert_ext(duckdb_store, "600001", EXT_PERIOD, bps=1.0, top_segment_share=None)
    _insert_ext(duckdb_store, "600002", EXT_PERIOD, bps=2.0, top_segment_share=None)

    result = sweeper.sweep(duckdb_store, domain=DOMAIN_EXT)

    assert result.report_period_misaligned is True
    assert "报告期未对齐" in (result.report_period_detail or "")
    # 本期整列为空的字段 → 逐行 NOT_COMPUTED（detail 指向报告期未对齐）
    assert result.by_field["top_segment_share"] == {"NOT_COMPUTED": 2}
    # 本期已有 100% 值的字段不受错配牵连（不许把零星 NULL 全推给"没对齐"）
    assert "bps" not in result.by_field


def test_ext_sweep_literal_misalignment_mode_labels_every_field(
    duckdb_store: DuckDBStore,
) -> None:
    """闸门调到 1.0 时退化为字面规则：错配则 ext 全部字段都算未对齐。"""
    _insert_snapshot(duckdb_store, "600001", latest_close=10.0)
    _insert_ext(duckdb_store, "600001", EXT_PERIOD, bps=1.0, top_segment_share=None)
    _insert_ext(duckdb_store, "600002", EXT_PERIOD, bps=2.0, top_segment_share=None)
    with duckdb_store.write_connection() as connection:
        connection.execute("UPDATE indicator_ext SET bps = NULL WHERE stock_code = '600002'")

    result = sweeper.sweep(
        duckdb_store, domain=DOMAIN_EXT, misaligned_coverage_limit=1.0
    )
    assert result.by_field["bps"] == {"NOT_COMPUTED": 1}


# ─── 5. NOT_DISPATCHED：本期整期全空 + 上期有数据 ──────────────────


def test_not_dispatched_requires_previous_period_coverage(
    duckdb_store: DuckDBStore,
) -> None:
    # 报告期已对齐（snapshot 用同一期）才能验证"整期没人抓"这一条
    for stock_code in ("600001", "600002", "600003", "600004"):
        _insert_snapshot(duckdb_store, stock_code, latest_close=10.0, report_date=EXT_PERIOD)
    with duckdb_store.write_connection() as connection:
        connection.execute(
            "DELETE FROM indicator_snapshot WHERE report_date <> ?", [EXT_PERIOD]
        )
    # 上期：employee_count 有 3/4 覆盖（>50%）
    for stock_code in ("600001", "600002", "600003"):
        _insert_ext(duckdb_store, stock_code, EXT_PREVIOUS_PERIOD, employee_count=100.0)
    # 本期：employee_count 全空（4 只）
    for stock_code in ("600001", "600002", "600003", "600004"):
        _insert_ext(duckdb_store, stock_code, EXT_PERIOD, employee_count=None)

    result = sweeper.sweep(duckdb_store, domain=DOMAIN_EXT)

    assert result.report_date == EXT_PERIOD
    assert result.report_period_misaligned is False
    assert "employee_count" in result.not_dispatched_fields
    assert result.by_field["employee_count"] == {"NOT_DISPATCHED": 4}

    upserted = sweeper.sweep(duckdb_store, domain=DOMAIN_EXT, write=True)
    assert upserted.written_rows >= 4
    rows = _stored_for(duckdb_store, "employee_count")
    assert len(rows) == 4
    assert {row["reason_code"] for row in rows} == {"NOT_DISPATCHED"}
    assert all("参考期" in (row["detail"] or "") for row in rows)


def test_not_dispatched_not_claimed_without_previous_coverage(
    duckdb_store: DuckDBStore,
) -> None:
    for stock_code in ("600001", "600002"):
        _insert_snapshot(duckdb_store, stock_code, latest_close=10.0, report_date=EXT_PERIOD)
    with duckdb_store.write_connection() as connection:
        connection.execute(
            "DELETE FROM indicator_snapshot WHERE report_date <> ?", [EXT_PERIOD]
        )
    # 上期该字段也全空 → 不能断言"本期没人抓"
    for stock_code in ("600001", "600002"):
        _insert_ext(duckdb_store, stock_code, EXT_PREVIOUS_PERIOD, employee_count=None)
        _insert_ext(duckdb_store, stock_code, EXT_PERIOD, employee_count=None)

    result = sweeper.sweep(duckdb_store, domain=DOMAIN_EXT)
    assert result.not_dispatched_fields == ()
    assert "employee_count" not in result.by_field


# ─── 6. API 聚合 ──────────────────────────────────────────────────


def _client(store: DuckDBStore) -> TestClient:
    app = FastAPI()
    app.include_router(data_status.router)
    app.state.duck = store
    return TestClient(app)


def test_api_returns_reason_distribution(duckdb_store: DuckDBStore) -> None:
    upsert_reasons(
        duckdb_store,
        [
            NullReasonRow("000001", SNAPSHOT_PERIOD, "goodwill_ratio", NullReason.NOT_APPLICABLE),
            NullReasonRow("000002", SNAPSHOT_PERIOD, "goodwill_ratio", NullReason.NOT_APPLICABLE),
            NullReasonRow("000003", SNAPSHOT_PERIOD, "goodwill_ratio", NullReason.NOT_COMPUTED),
            NullReasonRow("000001", EXT_PERIOD, "leverage_total", NullReason.NOT_APPLICABLE),
        ],
    )
    client = _client(duckdb_store)
    try:
        response = client.get(
            "/api/data-status/null-reasons",
            params={"report_date": SNAPSHOT_PERIOD.isoformat()},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["report_date"] == SNAPSHOT_PERIOD.isoformat()
        assert payload["fields"] == {
            "goodwill_ratio": {"NOT_APPLICABLE": 2, "NOT_COMPUTED": 1}
        }
        assert payload["totals"] == {"NOT_APPLICABLE": 2, "NOT_COMPUTED": 1}
        assert payload["total_cells"] == 3
        # 文案单一真相随响应下发，前端不必二次维护
        assert payload["labels"]["NOT_APPLICABLE"] == NULL_REASON_LABELS[NullReason.NOT_APPLICABLE]

        # 域过滤：snapshot 的聚合里不许出现 ext 字段
        ext_zero = client.get(
            "/api/data-status/null-reasons",
            params={"report_date": SNAPSHOT_PERIOD.isoformat(), "domain": DOMAIN_EXT},
        )
        assert ext_zero.status_code == 200
        assert ext_zero.json()["fields"] == {}
        assert ext_zero.json()["total_cells"] == 0

        ext_response = client.get(
            "/api/data-status/null-reasons",
            params={"report_date": EXT_PERIOD.isoformat(), "domain": DOMAIN_EXT},
        )
        assert ext_response.json()["fields"] == {"leverage_total": {"NOT_APPLICABLE": 1}}
    finally:
        client.close()


def test_api_rejects_bad_domain_and_date(duckdb_store: DuckDBStore) -> None:
    client = _client(duckdb_store)
    try:
        assert client.get(
            "/api/data-status/null-reasons", params={"domain": "quarterly"}
        ).status_code == 400
        assert client.get(
            "/api/data-status/null-reasons", params={"report_date": "2026/09/30"}
        ).status_code == 400
        # 无数据时也要给出稳定形状（前端不必区分空库与空结果）
        empty = client.get("/api/data-status/null-reasons").json()
        assert empty["fields"] == {}
        assert empty["total_cells"] == 0
        assert empty["report_date"] is None
        # 表存在但还没有任何清扫结果 → table_ready 仍为 True（区别于"没迁移"）
        assert empty["table_ready"] is True
    finally:
        client.close()


def test_reason_distribution_defaults_to_latest_report_date(duckdb_store: DuckDBStore) -> None:
    upsert_reasons(
        duckdb_store,
        [
            NullReasonRow("000001", EXT_PERIOD, "leverage_total", NullReason.NOT_APPLICABLE),
            NullReasonRow("000001", SNAPSHOT_PERIOD, "goodwill_ratio", NullReason.NOT_APPLICABLE),
        ],
    )
    payload = reason_distribution(duckdb_store)
    assert payload["report_date"] == SNAPSHOT_PERIOD.isoformat()
    assert payload["fields"] == {"goodwill_ratio": {"NOT_APPLICABLE": 1}}

# ─── 7. 2026-11-14 裁决：源表域 + 近零覆盖口径 + 技术指标族 ────────────


INCOME_PERIOD = date(2026, 9, 30)
INCOME_PREVIOUS = date(2026, 6, 30)
INCOME_PREVIOUS2 = date(2026, 3, 31)


def _insert_income(store: DuckDBStore, stock_code: str, report_date: date, **values: Any) -> None:
    _insert(
        store,
        "income_statement",
        {"stock_code": stock_code, "report_date": report_date, **values},
    )


def _insert_balance(store: DuckDBStore, stock_code: str, report_date: date, **values: Any) -> None:
    _insert(
        store,
        "balance_sheet",
        {"stock_code": stock_code, "report_date": report_date, **values},
    )


def test_source_fields_have_their_own_domain_with_prefix() -> None:
    """源表字段走 `income./balance./cashflow.` 前缀域，不能当指标列名去查。"""
    for name in ("income.deducted_net_profit", "balance.fixed_assets",
                 "balance.capital_adequacy_ratio", "cashflow.cf_from_operating"):
        prefix = name.split(".", 1)[0]
        assert name in domain_fields(prefix)
    assert "income.not_a_real_field" not in domain_fields("income")
    # 域之间互不重叠（字段名自带前缀，域内字段不跨域）
    all_fields = [name for domain in (DOMAIN_SNAPSHOT, DOMAIN_EXT, *SOURCE_DOMAINS)
                  for name in domain_fields(domain)]
    assert len(all_fields) == len(set(all_fields))


def test_deducted_net_profit_near_zero_coverage_is_not_dispatched(
    duckdb_store: DuckDBStore,
) -> None:
    """真实形态：本期 0%、上一期 0.4%、再上一期 80% → NOT_DISPATCHED。

    只死盯上一期（>50%）会漏掉它，所以参考期取"近两期里最好的一期"。
    """
    codes = ("600001", "600002", "600003", "600004")
    for code in codes:
        _insert_income(duckdb_store, code, INCOME_PERIOD, revenue=100.0, deducted_net_profit=None)
    # 上一期只有 1/4 有值（对应正式库 20/5,544 = 0.4%）
    _insert_income(duckdb_store, "600001", INCOME_PREVIOUS, revenue=100.0, deducted_net_profit=5.0)
    for code in ("600002", "600003", "600004"):
        _insert_income(duckdb_store, code, INCOME_PREVIOUS, revenue=100.0, deducted_net_profit=None)
    # 再上一期 3/4 有值（对应正式库 80.2%）
    for code in ("600001", "600002", "600003"):
        _insert_income(duckdb_store, code, INCOME_PREVIOUS2, revenue=100.0, deducted_net_profit=5.0)
    _insert_income(duckdb_store, "600004", INCOME_PREVIOUS2, revenue=100.0, deducted_net_profit=None)

    result = sweeper.sweep(duckdb_store, domain="income")

    assert result.report_date == INCOME_PERIOD
    assert "income.deducted_net_profit" in result.not_dispatched_fields
    assert result.by_field["income.deducted_net_profit"] == {"NOT_DISPATCHED": 4}
    reasons = sweeper.sweep(duckdb_store, domain="income", write=True)
    assert reasons.written_rows >= 4
    stored = _stored_for(duckdb_store, "income.deducted_net_profit")
    assert {row["reason_code"] for row in stored} == {"NOT_DISPATCHED"}
    assert all("参考期 2026-03-31" in (row["detail"] or "") for row in stored)


def test_fixed_assets_cliff_is_not_dispatched(duckdb_store: DuckDBStore) -> None:
    """真实形态：99.9% → 1.8% 的断崖（本期并不 ≤1%，靠断崖条件兜住）。"""
    codes = tuple(f"6000{index:02d}" for index in range(1, 13))
    for index, code in enumerate(codes):
        # 本期只有 1/12 = 8.3% 有值
        _insert_balance(
            duckdb_store, code, INCOME_PERIOD,
            total_assets=100.0, fixed_assets=10.0 if index == 0 else None,
        )
        # 上一期 12/12 = 100% 有值
        _insert_balance(
            duckdb_store, code, INCOME_PREVIOUS, total_assets=100.0, fixed_assets=10.0
        )

    result = sweeper.sweep(duckdb_store, domain="balance")

    assert "balance.fixed_assets" in result.not_dispatched_fields
    assert result.by_field["balance.fixed_assets"] == {"NOT_DISPATCHED": 11}
    written = sweeper.sweep(duckdb_store, domain="balance", write=True)
    assert written.written_rows >= 11
    rows = _stored_for(duckdb_store, "balance.fixed_assets")
    assert {row["reason_code"] for row in rows} == {"NOT_DISPATCHED"}
    assert all("断崖式塌陷" in (row["detail"] or "") for row in rows)


def test_regulatory_metrics_split_by_industry_and_history(duckdb_store: DuckDBStore) -> None:
    """6 个监管指标：非金融公司不适用；金融公司上期有值本期为空 → 没去抓。"""
    field_name = "balance.capital_adequacy_ratio"
    _insert(duckdb_store, "stock_meta",
            {"stock_code": "600001", "name": "某机悈", "exchange": "SSE", "sw_level1": "机械设备"})
    _insert(duckdb_store, "stock_meta",
            {"stock_code": "600002", "name": "某银行", "exchange": "SSE", "sw_level1": "银行"})
    # 本期两只都空
    for code in ("600001", "600002"):
        _insert_balance(duckdb_store, code, INCOME_PERIOD, total_assets=100.0,
                        capital_adequacy_ratio=None)
    # 上一期只有银行有值（对应正式库 38/5,544 = 0.7%，整期级永远凑不到 50%）
    _insert_balance(duckdb_store, "600001", INCOME_PREVIOUS, total_assets=100.0,
                    capital_adequacy_ratio=None)
    _insert_balance(duckdb_store, "600002", INCOME_PREVIOUS, total_assets=100.0,
                    capital_adequacy_ratio=13.5)

    result = sweeper.sweep(duckdb_store, domain="balance", write=True)

    assert field_name not in result.not_dispatched_fields, "整期级不该对稀疏字段下结论"
    stored = {row["stock_code"]: row for row in _stored_for(duckdb_store, field_name)}
    assert stored["600001"]["reason_code"] == "NOT_APPLICABLE"
    assert stored["600002"]["reason_code"] == "NOT_DISPATCHED"
    assert "该股在 2026-06-30 有值" in (stored["600002"]["detail"] or "")


def test_sparse_field_without_history_stays_unclassified(duckdb_store: DuckDBStore) -> None:
    """既没有参考期覆盖率、也没有个股历史 → 不写（不许瞎猜）。"""
    _insert(duckdb_store, "stock_meta",
            {"stock_code": "600001", "name": "某机械", "exchange": "SSE", "sw_level1": "机械设备"})
    _insert(duckdb_store, "stock_meta",
            {"stock_code": "600002", "name": "某银行", "exchange": "SSE", "sw_level1": "银行"})
    for code in ("600001", "600002"):
        for period in (INCOME_PERIOD, INCOME_PREVIOUS, INCOME_PREVIOUS2):
            _insert_balance(duckdb_store, code, period, total_assets=100.0,
                            risk_coverage_ratio=None, right_of_use_assets=None)

    result = sweeper.sweep(duckdb_store, domain="balance")

    # 非银行 → 行业不适用（这条有行业证据，照写）
    assert result.by_field["balance.risk_coverage_ratio"] == {"NOT_APPLICABLE": 1}
    # 银行有行业资格但从来没有值、整期覆盖率也上不去 → 判不出，不写
    assert result.unclassified.get("balance.risk_coverage_ratio") == 1
    # 普通源字段：三期全空且历史无覆盖 → 两只都判不出
    assert "balance.right_of_use_assets" not in result.by_field
    assert result.unclassified.get("balance.right_of_use_assets") == 2


# ─── 8. 2026-11-14 三季报形态：SOURCE_EMPTY vs NOT_DISPATCHED ──────────

EXT_Q3 = date(2026, 9, 30)
EXT_Q2 = date(2026, 6, 30)


def _insert_cash_flow_indirect(
    store: DuckDBStore, stock_code: str, report_date: date, **values: Any
) -> None:
    _insert(
        store,
        "cash_flow_indirect",
        {
            "stock_code": stock_code,
            "report_date": report_date,
            # 该表的必填溯源列，与生产写入路径保持一致（少了会撞 NOT NULL）
            "source": "eastmoney",
            "fetch_time": "2026-11-14 12:00:00",
            "raw_response_hash": "fixture-hash",
            "confidence": "strict",
            "batch_id": "fixture-batch",
            **values,
        },
    )


def test_undisclosed_depreciation_is_source_empty_not_missing_dispatch(
    duckdb_store: DuckDBStore,
) -> None:
    """三季报不披露"现金流量表补充资料" → 折旧摊销族判 SOURCE_EMPTY，不是"没去抓"。

    真实形态（2026-09-30）：cash_flow_indirect 有 5,574 行、cf_from_operating_indirect
    全有值，但 fixed_asset_depreciation 只有 5 行 → 整族覆盖率 0.09%，上期 99.7%。
    如果不看补充资料，整期级 NOT_DISPATCHED 会把它误判成"没去抓"。
    """
    codes = ("600001", "600002", "600003")
    for code in codes:
        # 本期：抓到了补充资料行，但折旧摊销三个科目全空（三季报常态）
        _insert_cash_flow_indirect(
            duckdb_store, code, EXT_Q3,
            cf_from_operating_indirect=100.0,
            fixed_asset_depreciation=None,
            intangible_asset_amortization=None,
            long_term_prepaid_amortization=None,
        )
        # 上期：折旧摊销齐全（99.7% 那种正常覆盖）
        _insert_cash_flow_indirect(
            duckdb_store, code, EXT_Q2,
            cf_from_operating_indirect=100.0,
            fixed_asset_depreciation=10.0,
            intangible_asset_amortization=2.0,
        )
        for period in (EXT_Q3, EXT_Q2):
            _insert_ext(duckdb_store, code, period, **{
                field_name: None for field_name in sweeper._DEPRECIATION_FIELDS
            })

    result = sweeper.sweep(duckdb_store, domain=DOMAIN_EXT, write=True)

    # 整期级兜底必须闭嘴：这一族不许走 NOT_DISPATCHED
    for field_name in sweeper._DEPRECIATION_FIELDS:
        assert field_name not in result.not_dispatched_fields
        assert result.by_field[field_name] == {"SOURCE_EMPTY": len(codes)}
    stored = _stored_for(duckdb_store, *sweeper._DEPRECIATION_FIELDS)
    assert {row["reason_code"] for row in stored} == {"SOURCE_EMPTY"}
    assert all(
        row["detail"] == "该期现金流量表补充资料未披露折旧摊销（三季报常见）"
        for row in stored
    )


def test_depreciation_family_without_source_rows_is_not_dispatched(
    duckdb_store: DuckDBStore,
) -> None:
    """该股本期连 cash_flow_indirect 行都没有 → 仍然判 NOT_DISPATCHED（我们没抓到）。"""
    for code in ("600001", "600002"):
        _insert_cash_flow_indirect(
            duckdb_store, code, EXT_Q2, cf_from_operating_indirect=100.0,
            fixed_asset_depreciation=10.0,
        )
        for period in (EXT_Q3, EXT_Q2):
            _insert_ext(duckdb_store, code, period, **{
                field_name: None for field_name in sweeper._DEPRECIATION_FIELDS
            })

    result = sweeper.sweep(duckdb_store, domain=DOMAIN_EXT, write=True)

    for field_name in sweeper._DEPRECIATION_FIELDS:
        assert field_name not in result.not_dispatched_fields
        assert result.by_field[field_name] == {"NOT_DISPATCHED": 2}
    stored = _stored_for(duckdb_store, *sweeper._DEPRECIATION_FIELDS)
    assert {row["reason_code"] for row in stored} == {"NOT_DISPATCHED"}
    assert all("cash_flow_indirect" in (row["detail"] or "") for row in stored)


def test_disclosed_depreciation_keeps_other_rules_working(duckdb_store: DuckDBStore) -> None:
    """补充资料给了折旧摊销 → 不许再判 SOURCE_EMPTY（规矩不能一刀切）。"""
    _insert_cash_flow_indirect(
        duckdb_store, "600001", EXT_Q3, cf_from_operating_indirect=100.0,
        fixed_asset_depreciation=10.0,
    )
    _insert_ext(duckdb_store, "600001", EXT_Q3, **{
        field_name: None for field_name in sweeper._DEPRECIATION_FIELDS
    })

    result = sweeper.sweep(duckdb_store, domain=DOMAIN_EXT)

    for field_name in sweeper._DEPRECIATION_FIELDS:
        assert "SOURCE_EMPTY" not in result.by_field.get(field_name, {})
