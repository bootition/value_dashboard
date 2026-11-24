"""自选列表在 DuckDB 不可用时必须降级，而不是整页"自选全没了"（2026-11-24 用户实测）。

## 事故现场

用户在自动更新窗口中打开自选列表页，看到的是：

* 左栏「公司分组 / **还没有分组**，去筛选规则页运行规则并保存到自选即可生成。」
* 右栏「**还没有可显示的自选分组** … 去筛选规则页 … 刷新列表」

而 SQLite 里

```sql
SELECT group_name, COUNT(*) FROM watchlist GROUP BY 1;
-- '往股东口袋装钱的大老公司' | 573
```

完好无损。根因在 `GET /api/watchlist/list` 的实现顺序：

1. `rows`（自选行）与 `groups`（侧栏分组）只查 **SQLite**，本来不需要 DuckDB；
2. 但"取股票名称/指标"这步要 DuckDB 的 `stock_meta` + `indicator_snapshot`；
3. 自动更新子进程持锁时这步抛 `DuckDBReadLockedError` → 旧实现直接
   `raise HTTPException(503)`；
4. 前端 `fetchWatchlist()` 一 503 就没有 `groups`，于是渲染成"你没有分组"。

**取名字失败被当成了列表失败。** 这比报错更糟：它让"数据完好"看起来像
"数据全丢"，是最不能接受的一类假象。

## 本文件钉住的不变量

1. **分组与股票行在任何 DuckDB 故障下都返回**：200 + 正确 `cnt` + 全部代码；
2. **降级有显式信号**：`enrichment == "unavailable"` + `enrichment_error`
   （持锁是稳定码 `duckdb_read_locked`），前端据此说"正在更新"而不是"你没有分组"；
3. **指标与名称可以缺，行不能缺**：降级时 `name == ""`、12 个快照字段全为
   `None`，其余身份/来源元数据（代码/分组/来源规则/加入时间）照常返回；
4. **黑名单语义不变**：降级路径下黑名单股票照样剔除，且 `groups[].cnt` 与
   `items` 同口径（侧栏数字 == 表里行数）；
5. **DuckDB 正常时行为一字不变**：名称/指标照旧富化，`enrichment == "ok"`；
6. **非锁类故障同样降级**：真正的 SQL/IO 错误写进 `enrichment_error` 与日志，
   而不是把 573 行自选变回"你没有分组"。

隔离：SQLite/DuckDB 都用 `--basetemp` 下的临时库（`sqlite_store` /
`duckdb_store` fixture），**绝不碰** `data/valuedashboard.*`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.blacklist import add_stock
from app.core.financial_period import expected_financial_period
from app.core.storage.duckdb_store import DuckDBReadLockedError, DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.watchlist import router as watchlist_router
from tests.conftest import insert_minimum_screenable_data
from tests.support.seed import CURRENT_TIMESTAMP, Seed
from tests.support.seed_sqlite import SeedSqlite

#: 用户库里唯一的分组名（逐字取自 `data/valuedashboard.sqlite`，含全角/中文标点）。
INCIDENT_GROUP = "往股东口袋装钱的大老公司"

#: 用户的真实规模：573 行。缩到 3 行会漏掉"占位符/分页/计数"这类规模相关行为。
INCIDENT_ROW_COUNT = 573

#: 自选行的来源指针：真实库里是规则 48 / 结果 10（`SELECT source_rule_id,
#: source_result_id FROM watchlist LIMIT 1`），这里用固定 1/1 便于逐字断言。
RULE_ID = 1
RESULT_ID = 1

#: 与 `app/web/api/watchlist.py::_SNAPSHOT_VALUE_FIELDS` 同口径的 12 个快照字段。
#: 降级时它们必须全为 None —— 不是 0、不是 ""，否则前端会当成真数值展示。
SNAPSHOT_VALUE_FIELDS = (
    "latest_close",
    "pe_ttm",
    "pb_mrq",
    "roe",
    "gross_margin",
    "net_margin",
    "debt_ratio",
    "revenue_yoy",
    "net_profit_yoy",
    "dividend_yield",
    "total_market_cap",
    "circ_market_cap",
)


class ReadLockedDuck:
    """DuckDB 读连接拿不到时的替身：模拟自动更新/CLI 写进程持锁。

    只实现 `list_watchlist` 真的会碰到的两个属性：

    * ``read_query`` —— 富化查询（`stock_meta` LEFT JOIN `indicator_snapshot`）
      与 `read_warning_codes` 的全库质量扫描都从这里走，一律抛持锁异常；
    * ``db_path``  —— 写窗口判定 `any_write_lock_active(duck.db_path)`
      与 warning-codes 缓存键都读它。

    这不是 mock 掉被测逻辑：被测逻辑恰恰是"这一层炸了以后上层怎么表现"，
    所以替身只负责"炸"，断言全部落在 HTTP 响应上。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.read_query_calls = 0

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        self.read_query_calls += 1
        raise DuckDBReadLockedError("另一个程序正在使用此文件，进程无法访问。")


class BrokenDuck(ReadLockedDuck):
    """非锁类故障替身：真实的 SQL/IO 错误（不是"更新中"）。"""

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        self.read_query_calls += 1
        raise RuntimeError("database is locked by another connection")


def _build_client(duck: Any, sqlite: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(watchlist_router)
    return TestClient(app)



#: 默认自选分组名（2026-11-26 起 `watchlist_groups` 注册表里**永远**有这一行）。
DEFAULT_GROUP = "我的自选"


def _group(body: dict, name: str = INCIDENT_GROUP) -> dict:
    """从 `/list` 的 `groups` 里按名字取一个分组。

    2026-11-26 起 `groups` 的唯一真相源是 `watchlist_groups` 注册表：默认分组
    「我的自选」永远在，0 只的空分组也照常返回，因此**不能再写整表相等断言**
    （那等于要求"注册表里只有用户这一个分组"，与"可自建分组"直接冲突）。
    这里改成"按名字取、只钉计数"——原意（分组在任何情况下都必须回来、计数与行同口径）不变。
    """
    found = next((group for group in body["groups"] if group["group_name"] == name), None)
    assert found is not None, f"分组 {name} 没回来：{body['groups']}"
    return found


def _seed_watchlist(sqlite: SQLiteStore, codes: list[str], *, group: str = INCIDENT_GROUP) -> None:
    """按真实库的形状播种自选行（同一分组 + 明确来源 + 递减的加入时间）。

    `added_at` 逐行递减，让 `ORDER BY group_name, added_at DESC` 有确定顺序：
    否则同一批行时间戳相同，SQLite 的返回顺序是实现细节，断言会假绿。

    `watchlist.source_rule_id / source_result_id` 是**外键**（`PRAGMA
    foreign_keys=ON`），所以先造一条规则 + 一条已保存结果，让"来源规则/结果"
    这两列真的是从库里读出来的，而不是凑一个 NULL 蒙过去。
    """
    with sqlite.transaction() as conn:
        seed = SeedSqlite(sqlite).join(conn)
        seed.screening_rule("自选降级回归", 1, "{}", id=RULE_ID)
        # `screening_results` 工厂没有方法（需要 `cursor.lastrowid` 回捞 id），
        # 沿用 `test_blacklist_visibility.py` 的口径留内联：`id` 写死成 `RULE_ID`
        # 指着的固定行，断言读的就是这两列。
        conn.execute(
            """INSERT INTO screening_results
                   (id, title, note, rule_id, rule_version, data_date,
                    result_json, columns_json, sort_json, confidence_summary, base_pool_config)
               VALUES (?, '自选降级回归', 'n', ?, 1, '2026-09-16', '[]', '[]', '[]', '{}', '{}')""",
            [RESULT_ID, RULE_ID],
        )
        for index, code in enumerate(codes):
            seed.watchlist(
                code,
                group_name=group,
                source_rule_id=RULE_ID,
                source_result_id=RESULT_ID,
                added_at=f"2026-09-16 11:{53 - index // 60:02d}:{59 - index % 60:02d}",
            )


# ─── 1. 持锁降级：分组与行必须回来 ────────────────────────────────


def test_watchlist_list_degrades_when_duckdb_read_is_locked(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """事故的最小复现：DuckDB 抛持锁异常 → 200 + 完整分组 + 完整代码列表。"""
    codes = ["002961", "300938", "600851"]
    _seed_watchlist(sqlite_store, codes)
    client = _build_client(ReadLockedDuck(duckdb_store.db_path), sqlite_store)

    response = client.get("/api/watchlist/list")

    # 旧实现这里是 503 —— 前端一 503 就没有 groups，整页变成"你没有分组"。
    assert response.status_code == 200, response.text
    body = response.json()

    # 分组列表：只查 SQLite，任何时候都必须回来，且计数正确。
    assert _group(body)["cnt"] == 3
    # 自选行：代码/分组/来源全部保留，顺序按 added_at DESC。
    assert [item["stock_code"] for item in body["items"]] == codes
    assert body["count"] == len(codes)
    for item in body["items"]:
        assert item["group_name"] == INCIDENT_GROUP
        assert item["source_rule_id"] == RULE_ID
        assert item["source_result_id"] == RESULT_ID
        assert item["added_at"].startswith("2026-09-16")

    # 明确的降级信号：前端据此说"数据正在自动更新"，而不是"你没有分组"。
    assert body["enrichment"] == "unavailable"
    assert body["enrichment_error"] == "duckdb_read_locked"

    # 富化信息缺失：名称留空、指标一律 None（绝不能是 0 被当真数值）。
    for item in body["items"]:
        assert item["name"] == ""
        assert item["exchange"] == ""
        assert item["csrc_l1"] is None
        assert item["listing_date"] is None
        assert all(item[field] is None for field in SNAPSHOT_VALUE_FIELDS)


def test_watchlist_list_degrades_and_returns_all_573_rows_at_incident_scale(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """用户真实规模：573 行在同一分组 → 降级后 573 行全部可见、cnt == 573。"""
    codes = [f"{600000 + index:06d}" for index in range(INCIDENT_ROW_COUNT)]
    _seed_watchlist(sqlite_store, codes)
    client = _build_client(ReadLockedDuck(duckdb_store.db_path), sqlite_store)

    response = client.get("/api/watchlist/list")

    assert response.status_code == 200, response.text
    body = response.json()
    assert _group(body)["cnt"] == INCIDENT_ROW_COUNT
    assert body["count"] == INCIDENT_ROW_COUNT
    assert len(body["items"]) == INCIDENT_ROW_COUNT
    assert sorted(item["stock_code"] for item in body["items"]) == sorted(codes)
    # 侧栏数字与表里行数同口径：这是用户"看起来数据全丢"最直观的那两个数字。
    assert _group(body)["cnt"] == len(body["items"])


# ─── 2. 非锁类故障也降级（但不是"更新中"） ──────────────────────


def test_watchlist_list_degrades_on_non_lock_enrichment_failure(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """真正的 SQL/IO 错误不能伪装成"更新中"，但同样不许把自选变回空列表。"""
    _seed_watchlist(sqlite_store, ["002961", "300938"])
    client = _build_client(BrokenDuck(duckdb_store.db_path), sqlite_store)

    response = client.get("/api/watchlist/list")

    assert response.status_code == 200, response.text
    body = response.json()
    assert _group(body)["cnt"] == 2
    assert [item["stock_code"] for item in body["items"]] == ["002961", "300938"]
    assert body["enrichment"] == "unavailable"
    # 原因照实回传：前端有据可依地提示"加载失败"，而不是猜成"正在更新"。
    assert "RuntimeError" in body["enrichment_error"]
    assert "locked by another connection" in body["enrichment_error"]
    assert body["enrichment_error"] != "duckdb_read_locked"


# ─── 3. 黑名单语义在降级路径下不变 ───────────────────────────────


def test_watchlist_list_degrades_and_still_drops_blacklisted_rows(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """黑名单剔除是 SQLite 侧语义，不能因为 DuckDB 挂了就复活被屏蔽的股票。"""
    visible = "600519"
    blocked = ["000001", "000002"]
    _seed_watchlist(sqlite_store, [visible, *blocked])
    for code in blocked:
        add_stock(sqlite_store, code)
    client = _build_client(ReadLockedDuck(duckdb_store.db_path), sqlite_store)

    response = client.get("/api/watchlist/list")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["stock_code"] for item in body["items"]] == [visible]
    # 屏蔽期**不删除** watchlist 行：移出黑名单后原股票自动回来。
    assert sqlite_store.query("SELECT COUNT(*) AS n FROM watchlist")[0]["n"] == 3
    # 侧栏计数与可见行同口径：显示「1」就必须只有 1 行。
    assert _group(body)["cnt"] == 1
    assert _group(body)["cnt"] == len(body["items"])


# ─── 4. 正常路径行为不变 ─────────────────────────────────────────


def _last_bar_date(duck: DuckDBStore, code: str) -> str:
    """播种后 `price_daily_raw` 里这根股票的**最后一根日线**日期（YYYY-MM-DD）。

    与 `test_indicator_trust_api.py::_last_bar_date` 同一口径（两文件各自内联，
    见该函数 docstring 的完整来龙去脉）：`insert_minimum_screenable_data` 只产
    工作日 bar，`has_coherent_snapshot` 又要求快照的 `latest_price_date` 逐字等于
    `MAX(price_daily_raw.trade_date)`。写 `CURRENT_DATE` 就在周末/节假日恒不达标
    （2026-11-21 周六 00:45 4 条用例齐红即此）。
    """
    rows = duck.read_query(
        "SELECT CAST(MAX(trade_date) AS VARCHAR) AS last_bar"
        " FROM price_daily_raw WHERE stock_code = ?",
        [code],
    )
    last_bar = rows[0]["last_bar"] if rows else None
    assert last_bar, f"播种后 {code} 必须至少有一根日线（否则测试前提不成立）"
    return last_bar


def _readiness_period() -> str:
    """本次播种用的报告期 = 生产口径的「按今天应当已发布的期」（季末）。

    写死 `'2025-12-31'` 会让 `has_complete_financial_period`
    （`complete_financials.report_date >= CURRENT_DATE - INTERVAL '18 months'`）
    在 2027-07-01 之后恒不达标。`expected_financial_period()` 恒是距今 0~4 个月的
    季末，窗口余量 >1 年。
    """
    return expected_financial_period()


def _rebase_seed_period(duck: DuckDBStore, code: str, period: str) -> None:
    """把 `insert_minimum_screenable_data` 写死的 `'2025-12-31'` 期整体搬到 `period`。

    三表 / 快照 / `source_audit` 的报告期必须一起搬：readiness 还要求
    「快照期 == 最新完整三表期」(`snapshot_period_alignment`) 与「完整期上有逐字段
    血缘证据」(`lineage_coverage`，缺一条即阻断)。价格类批次
    （`price_daily_raw` / `price_daily_qfq`）的 `source_audit.report_date` 是
    "最新交易日"、不是报告期，不能动。
    """
    with duck.write_connection() as connection:
        for table in ("balance_sheet", "income_statement", "cash_flow", "indicator_snapshot"):
            # 表名来自上面四元素白名单常量，值走参数占位符。
            connection.execute(
                f"UPDATE {table} SET report_date = ? WHERE stock_code = ?",
                [period, code],
            )
        connection.execute(
            """UPDATE source_audit SET report_date = ?
               WHERE stock_code = ?
                 AND fetch_batch_id IN (
                     SELECT batch_id FROM fetch_batch
                     WHERE data_type NOT IN ('price_daily_raw', 'price_daily_qfq')
                 )""",
            [period, code],
        )


def _seed_enrichable_stock(duck: DuckDBStore, code: str, name: str) -> None:
    """一只"新鲜度/口径都合格"的股票，让 trust 决策落回空告警集。

    与 `test_indicator_trust_api.py::_seed_screenable` 同一套播种：`trust` 只有在
    全库质量扫描干净时才是 `warning_codes == []`，否则数值会被服务端遮蔽，
    "名称/指标照旧富化"这条断言就变成测遮蔽而不是测富化。
    """
    Seed(duck).stock(
        code,
        name=name,
        exchange="SSE",
        listing_date="2020-01-01",
        is_st=False,
        is_suspended=False,
    )
    insert_minimum_screenable_data(duck, code)
    period = _readiness_period()
    _rebase_seed_period(duck, code, period)
    # 九个指标全部写满：`minimum_data_readiness` 少一个就报 `MINIMUM_DATA_NOT_READY`，
    # 于是 trust 变成"不可信"、所有数值被遮蔽 —— 那样这条用例就在测遮蔽而不是富化。
    Seed(duck).snapshot(
        code,
        period,
        # `latest_close` 必须等于 `price_daily_raw` 的最新收盘（这里统一是 10），
        # `latest_price_date` 必须等于日线的最新交易日 —— 两条同时成立
        # `snapshot_price_coherence` 才达标，否则 MINIMUM_DATA_NOT_READY。
        latest_close=10,
        latest_price_date=_last_bar_date(duck, code),
        calculated_at=CURRENT_TIMESTAMP,
        pe_ttm=30.5,
        pb_mrq=8.0,
        roe=0.2,
        gross_margin=0.4,
        net_margin=0.1,
        debt_ratio=0.2,
        revenue_yoy=0.3,
        net_profit_yoy=0.25,
        dividend_yield=0.02,
        overwrite=True,
    )


def test_watchlist_list_keeps_names_and_metrics_when_duckdb_is_healthy(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """DuckDB 正常：名称/指标照旧富化，`enrichment == "ok"`（本次改动零影响）。"""
    _seed_enrichable_stock(duckdb_store, "600519", "贵州茅台")
    _seed_watchlist(sqlite_store, ["600519"])
    client = _build_client(duckdb_store, sqlite_store)

    response = client.get("/api/watchlist/list")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enrichment"] == "ok"
    assert body["enrichment_error"] is None
    item = body["items"][0]
    assert item["name"] == "贵州茅台"
    assert item["exchange"] == "SSE"
    assert item["listing_date"] == "2020-01-01"
    # 质量干净时不遮蔽：数值原样返回（与改动前的行为逐字一致）。
    assert body["trust"]["warning_codes"] == []
    assert item["latest_close"] == 10
    assert item["pe_ttm"] == 30.5
    assert item["pb_mrq"] == 8.0


# ─── 5. 空自选：没有行时不该白跑 DuckDB，也不能出现降级标志 ──────


def test_watchlist_list_empty_returns_ok_enrichment_without_touching_duckdb(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """真的没有自选时，"你没有分组"才是**正确**的提示 —— 别让降级标志混淆它。"""
    locked = ReadLockedDuck(duckdb_store.db_path)
    client = _build_client(locked, sqlite_store)

    response = client.get("/api/watchlist/list")

    assert response.status_code == 200, response.text
    body = response.json()
    # 2026-11-26：`groups` 来自注册表，默认分组「我的自选」即使 0 只也必须在，
    # 所以"真的没有自选"表现为「只有一个 cnt=0 的默认组」，不再是空列表。
    assert body["items"] == [] and body["count"] == 0
    assert body["enrichment"] == "ok" and body["enrichment_error"] is None
    assert [group["group_name"] for group in body["groups"]] == [DEFAULT_GROUP]
    assert body["groups"][0]["cnt"] == 0
    assert locked.read_query_calls == 0, "空自选不该为了富化去碰 DuckDB"


# ─── 6. 单分组过滤：`?group=` 也要降级 ────────────────────────────


@pytest.mark.parametrize("query", [{"group": INCIDENT_GROUP}, {}])
def test_watchlist_list_degrades_for_scoped_and_unscoped_requests(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    query: dict[str, str],
) -> None:
    """前端取数会连打两次（先全量定分组、再按分组取行），两次都必须降级。"""
    _seed_watchlist(sqlite_store, ["002961", "300938"])
    client = _build_client(ReadLockedDuck(duckdb_store.db_path), sqlite_store)

    response = client.get("/api/watchlist/list", params=query)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enrichment"] == "unavailable"
    assert [item["stock_code"] for item in body["items"]] == ["002961", "300938"]
    assert _group(body)["cnt"] == 2
