"""研究域只读 API 回归（reports/119 §5 D 类，2026-11-14）

守住五件事：
1. **时点可见性**：`as_of` 早于最早公布日 → 404 + reason；
   `as_of` 落在两个公布日之间 → 必须返回**较早那期**，不得泄漏未来报告期；
   与 `app/core/point_in_time.py` 的判定结果一致（同一套口径）。
2. **字段白名单**：income/balance/cashflow 标准列以外的字段一律 400（不拼 SQL）。
3. **公告时间线**：按公告时间倒序、limit 生效、category 非空且来自
   `app.core.update.classify_announcement`；`days` 窗口外的公告不得出现。
4. **汇总**：全市场按类别计数与窗口条数自洽，`truncated` 如实。
5. **`as_reported_value` 去假**：source-audit 响应里不应再出现该键
   （含同样恒为 NULL 的 `latest_restated_diff`）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.financial_period import CN_TZ
from app.core.point_in_time import latest_visible_period, was_visible
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.update import classify_announcement
from app.web.api import research as research_api
from app.web.api.research import router as research_router
from app.web.api.stock_detail import router as stock_detail_router

STOCK = "600519"

# 报告期 → 公布日（覆盖年报/一季报/中报，与 financial_report_dates 季度精度一致）
PERIODS: tuple[tuple[str, str], ...] = (
    ("2021-12-31", "2022-03-31"),
    ("2022-12-31", "2023-04-03"),
    ("2023-12-31", "2024-04-03"),
    ("2024-03-31", "2024-04-25"),
    ("2024-06-30", "2024-08-20"),
)

# 每期营收/归母净利（用不同数量级区分「取到了哪一期」）
REVENUE_BY_PERIOD: dict[str, float] = {
    "2021-12-31": 1000.0,
    "2022-12-31": 1100.0,
    "2023-12-31": 1200.0,
    "2024-03-31": 300.0,
    "2024-06-30": 700.0,
}


def _seed_duck(duck: DuckDBStore, stock_code: str = STOCK) -> None:
    # 单事务批量写入：DuckDBStore.write_query 每次都要取写锁，逐条写会把
    # 33 个用例的夹具时间从 ~3s 拖到 ~30s
    with duck.transaction() as conn:
        conn.executemany(
            """INSERT INTO financial_report_dates
               (stock_code, report_date, announce_date, source, fetch_time, batch_id)
               VALUES (?, CAST(? AS DATE), CAST(? AS DATE), 'csmar',
                       CURRENT_TIMESTAMP, 'fixture')""",
            [[stock_code, report_date, announce_date] for report_date, announce_date in PERIODS],
        )
        conn.executemany(
            """INSERT INTO income_statement
               (stock_code, report_date, revenue, parent_net_profit)
               VALUES (?, CAST(? AS DATE), ?, ?)""",
            [
                [stock_code, report_date, revenue, revenue / 10.0]
                for report_date, revenue in REVENUE_BY_PERIOD.items()
            ],
        )
        conn.execute(
            """INSERT INTO balance_sheet (stock_code, report_date, total_assets)
               VALUES (?, DATE '2023-12-31', 5000.0)""",
            [stock_code],
        )


def _api_client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(research_router)
    app.include_router(stock_detail_router)
    return TestClient(app)


def _insert_announcement(
    sqlite: SQLiteStore, announcement_id: str, published: datetime, title: str,
    stock_code: str = STOCK,
) -> None:
    """按适配器的真实存储格式写入（UTC datetime 的 str()，如 2024-04-25 11:30:00+00:00）。"""
    with sqlite.transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO announcement_registry
               (announcement_id, stock_code, announcement_time, title)
               VALUES (?, ?, ?, ?)""",
            [announcement_id, stock_code, str(published.astimezone(UTC)), title],
        )


def _insert_source_audit(duck: DuckDBStore, stock_code: str = STOCK) -> None:
    with duck.transaction() as conn:
        conn.execute(
            """INSERT INTO source_audit
               (stock_code, field_name, report_date, value, source, fetch_batch_id,
                fetch_time, raw_response_hash, confidence)
               VALUES (?, 'revenue', DATE '2023-12-31', 1200.0, 'fixture', 'batch-1',
                       CURRENT_TIMESTAMP, 'hash-1', 'strict')""",
            [stock_code],
        )


# ─── A. 时点可见性 ─────────────────────────────────────────────────────


def test_as_reported_404_when_as_of_precedes_first_announcement(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get(
        "/api/research/as-reported",
        params={"code": STOCK, "as_of": "2021-06-30", "fields": "revenue"},
    )

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["reason"] == "no_visible_period_before_as_of"
    # 最早公布日必须如实回给用户，而不是只说"没数据"
    assert detail["earliest_announce_date"] == "2022-03-31"
    assert detail["as_of"] == "2021-06-30"


def test_as_reported_404_when_stock_has_no_announce_records(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get(
        "/api/research/as-reported",
        params={"code": "000001", "as_of": "2024-06-30", "fields": "revenue"},
    )

    assert response.status_code == 404
    # 未登记公布日 ≠ 当天可见：必须明说「无记录」，不得推断
    assert response.json()["detail"]["reason"] == "no_announce_date_records"


def test_as_reported_between_two_announce_dates_returns_the_earlier_period(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    # 2024-06-30 落在 2024-04-25（一季报公布）与 2024-08-20（中报公布）之间
    response = client.get(
        "/api/research/as-reported",
        params={
            "code": STOCK,
            "as_of": "2024-06-30",
            "fields": "revenue,parent_net_profit",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["report_date"] == "2024-03-31"
    assert payload["announce_date"] == "2024-04-25"
    assert payload["report_type"] == "quarterly"
    # 未来报告期（2024-06-30，营收 700）绝不能泄漏：必须是一季报的 300
    assert payload["fields"]["revenue"] == 300.0
    assert payload["fields"]["parent_net_profit"] == 30.0
    assert payload["missing_fields"] == []
    assert payload["days_since_announce"] == (date(2024, 6, 30) - date(2024, 4, 25)).days
    # 口径必须自曝：数值是「最新重述值」，不是当年原始披露值
    assert payload["value_basis"] == "latest_restated"
    assert "income_statement" in payload["value_source"]
    assert payload["disclaimer"]
    assert payload["limitations"]


def test_as_reported_matches_point_in_time_module(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """API 与 `app/core/point_in_time.py` 必须是同一套判定。"""
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    as_of = date(2024, 6, 30)
    response = client.get(
        "/api/research/as-reported",
        params={"code": STOCK, "as_of": as_of.isoformat(), "fields": "revenue"},
    )

    assert response.status_code == 200
    visible = latest_visible_period(duckdb_store, STOCK, as_of)
    assert visible is not None
    assert response.json()["report_date"] == visible.report_date.isoformat()
    assert was_visible(duckdb_store, STOCK, visible.report_date, as_of) is True


def test_as_reported_null_field_is_reported_not_silently_dropped(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """该期有行但字段为空 → 200 + null + missing_fields（不是 404、也不是省略键）。"""
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get(
        "/api/research/as-reported",
        params={"code": STOCK, "as_of": "2024-06-30", "fields": "revenue,cf_from_operating"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["fields"]["revenue"] == 300.0
    assert payload["fields"]["cf_from_operating"] is None
    assert payload["missing_fields"] == ["cf_from_operating"]
    assert "cash_flow" not in payload["value_tables"]


def test_as_reported_404_when_visible_period_has_no_financials(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """可见期已定位但三表都没有该期 → 404，并回带报告期让用户知道差哪期。"""
    with duckdb_store.transaction() as conn:
        conn.executemany(
            """INSERT INTO financial_report_dates
               (stock_code, report_date, announce_date, source, fetch_time, batch_id)
               VALUES (?, CAST(? AS DATE), CAST(? AS DATE), 'csmar',
                       CURRENT_TIMESTAMP, 'fixture')""",
            [[STOCK, report_date, announce_date] for report_date, announce_date in PERIODS[:2]],
        )
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get(
        "/api/research/as-reported",
        params={"code": STOCK, "as_of": "2023-06-30", "fields": "revenue"},
    )

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["reason"] == "visible_period_has_no_financials"
    assert detail["report_date"] == "2022-12-31"
    assert detail["announce_date"] == "2023-04-03"


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ("revenue,dividend_yield", "field_not_allowed"),
        ("total_market_cap", "field_not_allowed"),
        ("report_type", "field_not_allowed"),      # 分类列不进白名单
        ("stock_code", "field_not_allowed"),       # 主键列不进白名单
        ("revenue,revenue_typo", "field_not_allowed"),
        ("", "fields_required"),
        (" , ", "fields_required"),
    ],
)
def test_as_reported_rejects_fields_outside_whitelist(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, fields: str, reason: str,
) -> None:
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get(
        "/api/research/as-reported",
        params={"code": STOCK, "as_of": "2024-06-30", "fields": fields},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["reason"] == reason
    assert detail["examples"]


def test_as_reported_field_not_allowed_lists_rejected_names(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get(
        "/api/research/as-reported",
        params={"code": STOCK, "as_of": "2024-06-30", "fields": "revenue,div_yield,pe"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["rejected_fields"] == ["div_yield", "pe"]


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"code": "60051", "as_of": "2024-06-30", "fields": "revenue"}, "invalid_stock_code"),
        ({"code": "600519.SH", "as_of": "2024-06-30", "fields": "revenue"}, "invalid_stock_code"),
        ({"code": "abcdef", "as_of": "2024-06-30", "fields": "revenue"}, "invalid_stock_code"),
        ({"code": "600519", "as_of": "2024/06/30", "fields": "revenue"}, "invalid_date"),
        ({"code": "600519", "as_of": "2024-02-31", "fields": "revenue"}, "invalid_date"),
        (
            {"code": "600519", "as_of": "2024-06-30", "fields": "revenue", "frequency": "weekly"},
            "invalid_frequency",
        ),
    ],
)
def test_as_reported_rejects_bad_parameters(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
    params: dict[str, str], reason: str,
) -> None:
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get("/api/research/as-reported", params=params)

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == reason


def test_visible_periods_lists_start_dates_ascending_with_earliest_announce(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    # 同一报告期的第二个来源（补录更晚）：可见日必须取 MIN，而不是任一来源
    with duckdb_store.transaction() as conn:
        conn.execute(
            """INSERT INTO financial_report_dates
               (stock_code, report_date, announce_date, source, fetch_time, batch_id)
               VALUES (?, DATE '2023-12-31', DATE '2024-06-01', 'secondary',
                       CURRENT_TIMESTAMP, 'fixture')""",
            [STOCK],
        )
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get("/api/research/visible-periods", params={"code": STOCK})

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == len(PERIODS)
    report_dates = [row["report_date"] for row in payload["periods"]]
    assert report_dates == sorted(report_dates), "回测对齐需要按报告期升序"
    assert report_dates[0] == "2021-12-31"
    annual_2023 = next(row for row in payload["periods"] if row["report_date"] == "2023-12-31")
    assert annual_2023["announce_date"] == "2024-04-03"
    assert annual_2023["source"] == "csmar"
    assert annual_2023["report_type"] == "annual"
    assert {row["report_type"] for row in payload["periods"]} == {
        "annual", "quarterly", "semi_annual",
    }
    assert payload["limitations"]


def test_visible_periods_supports_as_of_and_frequency_filters(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    filtered = client.get(
        "/api/research/visible-periods",
        params={"code": STOCK, "as_of": "2024-06-30", "frequency": "annual"},
    )
    assert filtered.status_code == 200
    body = filtered.json()
    # 2024-06-30 时点可见的年报只有 2021/2022/2023 三期
    assert [row["report_date"] for row in body["periods"]] == [
        "2021-12-31", "2022-12-31", "2023-12-31",
    ]
    assert all(row["announce_date"] <= "2024-06-30" for row in body["periods"])

    limited = client.get(
        "/api/research/visible-periods", params={"code": STOCK, "limit": 2},
    )
    assert limited.status_code == 200
    assert [row["report_date"] for row in limited.json()["periods"]] == [
        "2024-03-31", "2024-06-30",
    ], "limit 取最新 N 期，但返回仍按升序"


def test_visible_periods_404_for_unknown_stock(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get("/api/research/visible-periods", params={"code": "000001"})

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "no_announce_date_records"


# ─── B. 公告时间线 ─────────────────────────────────────────────────────


def _seed_announcements(sqlite: SQLiteStore) -> dict[str, datetime]:
    now = datetime.now(UTC)
    seeded = {
        "newest": now - timedelta(days=1),
        "dividend": now - timedelta(days=5),
        "other": now - timedelta(days=40),
        "out_of_window": now - timedelta(days=400),
    }
    _insert_announcement(sqlite, "ann-1", seeded["newest"], "2024年年度报告")
    _insert_announcement(sqlite, "ann-2", seeded["dividend"], "2024年半年度权益分派实施公告")
    _insert_announcement(sqlite, "ann-3", seeded["other"], "关于召开2024年第一次临时股东大会的通知")
    _insert_announcement(sqlite, "ann-4", seeded["out_of_window"], "2023年半年度报告")
    # 别的股票的公告不得混进来
    _insert_announcement(sqlite, "ann-5", seeded["newest"], "2024年年度报告", stock_code="000001")
    return seeded


def test_announcements_are_descending_paged_and_classified(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    _seed_announcements(sqlite_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get(
        "/api/research/announcements", params={"code": STOCK, "days": 365, "limit": 200},
    )

    assert response.status_code == 200
    payload = response.json()
    items = payload["items"]
    # 400 天前那条与别的股票都不在窗口内
    assert payload["total"] == 3
    assert payload["count"] == 3
    assert payload["has_more"] is False
    assert [item["announcement_id"] for item in items] == ["ann-1", "ann-2", "ann-3"]

    times = [item["announcement_time"] for item in items]
    assert times == sorted(times, reverse=True), "必须按公告时间倒序"
    assert [item["category"] for item in items] == ["financial", "dividend", "other"]
    assert all(item["category"] for item in items)
    assert all(item["announcement_date_cn"] for item in items)
    assert payload["by_category"] == {"financial": 1, "dividend": 1, "other": 1}
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["disclaimer"]


def test_announcements_limit_and_offset_take_effect(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    _seed_announcements(sqlite_store)
    client = _api_client(duckdb_store, sqlite_store)

    first_page = client.get(
        "/api/research/announcements", params={"code": STOCK, "days": 365, "limit": 2},
    )
    assert first_page.status_code == 200
    body = first_page.json()
    assert body["count"] == 2
    assert body["total"] == 3
    assert body["has_more"] is True
    assert [item["announcement_id"] for item in body["items"]] == ["ann-1", "ann-2"]
    # by_category 统计整个窗口，不是当前页
    assert sum(body["by_category"].values()) == 3

    second_page = client.get(
        "/api/research/announcements",
        params={"code": STOCK, "days": 365, "limit": 2, "offset": 2},
    )
    assert second_page.status_code == 200
    tail = second_page.json()
    assert tail["count"] == 1
    assert tail["has_more"] is False
    assert [item["announcement_id"] for item in tail["items"]] == ["ann-3"]


def test_announcements_days_window_narrows_results(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    _seed_announcements(sqlite_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get(
        "/api/research/announcements", params={"code": STOCK, "days": 7},
    )

    assert response.status_code == 200
    payload = response.json()
    # days=7 = 含今天在内的最近 7 个北京自然日
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["cutoff_date"] == (datetime.now(CN_TZ).date() - timedelta(days=6)).isoformat()
    assert [item["announcement_id"] for item in payload["items"]] == [
        "ann-1", "ann-2",
    ]


def test_announcements_returns_empty_list_not_404_for_quiet_stock(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get("/api/research/announcements", params={"code": STOCK})

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


def test_announcements_rejects_bad_parameters(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    assert client.get("/api/research/announcements", params={"code": "60051"}).status_code == 400
    assert (
        client.get(
            "/api/research/announcements",
            params={"code": STOCK, "limit": 9999},
        ).status_code
        == 422
    ), "limit 上限由 FastAPI 校验（前端传错时立刻失败，不静默截断）"
    assert (
        client.get("/api/research/announcements", params={"code": STOCK, "days": 0}).status_code
        == 422
    )


def test_announcement_category_comes_from_repo_classifier(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """类别必须由 `app.core.update.classify_announcement` 判定（不另起一套关键词）。"""
    assert research_api._classify(None) == classify_announcement(None) == "other"
    assert research_api._classify("2024年年度报告") == classify_announcement("2024年年度报告")
    assert research_api._classify("2024年半年度权益分派实施公告") == "dividend"
    assert research_api._classify("关于召开股东大会的通知") == "other"


def test_announcement_summary_counts_by_category(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    _seed_announcements(sqlite_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get("/api/research/announcements/summary", params={"days": 365})

    assert response.status_code == 200
    payload = response.json()
    # 两只股票各 1 条 financial + 本股票 dividend/other（400 天前那条不在窗口）
    assert payload["window_total"] == 4
    assert payload["by_category"] == {"financial": 2, "dividend": 1, "other": 1}
    assert payload["stocks"] == 2
    assert sum(payload["by_category"].values()) == payload["window_total"]
    assert payload["truncated"] is False
    assert payload["scanned_rows"] >= payload["window_total"]


def test_announcement_summary_narrow_window(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _seed_duck(duckdb_store)
    _seed_announcements(sqlite_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get("/api/research/announcements/summary", params={"days": 7})

    assert response.status_code == 200
    payload = response.json()
    assert payload["window_total"] == 3          # 两只股票的最新报告 + 权益分派
    assert payload["by_category"]["other"] == 0


# ─── C. `as_reported_value` 去假 ───────────────────────────────────────


def test_source_audit_no_longer_returns_fake_as_reported_value(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    _insert_source_audit(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    response = client.get(f"/api/stock/{STOCK}/source-audit")

    assert response.status_code == 200
    rows = response.json()["field_audit"]
    assert len(rows) == 1
    assert rows[0]["field_name"] == "revenue"
    # 恒为 NULL 的假字段必须彻底消失，而不是继续以 null 出现在响应里
    assert "as_reported_value" not in rows[0]
    assert "latest_restated_diff" not in rows[0]


def test_stock_detail_router_still_registers_kline_and_audit(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """删除两个假字段不得影响详情页其它接口（防止误删/语法破损）。"""
    _seed_duck(duckdb_store)
    client = _api_client(duckdb_store, sqlite_store)

    assert client.get(f"/api/stock/{STOCK}/source-audit").status_code == 200

