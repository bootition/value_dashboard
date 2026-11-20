"""个股搜索：名称内嵌空白归一化回归（2026-11-22，用户实测 bug）。

背景
----
用户输入「五粮液」搜不到，提示「未找到匹配的上市公司」。根因不在前端：
`stock_meta.name` 由数据源原样带入，正式库有 265 行名称内嵌空白
（`五 粮 液`、`万  科Ａ`、`PT 金田A`），旧 SQL `name LIKE '%五粮液%'`
对这类名称永远不命中。

守住的契约
----------
1. **核心**：名称带内嵌空白 → 搜无空白版本必须命中（不改 `stock_meta` 源数据）；
2. 单空格 / 双空格 / 全角空格 U+3000 / NBSP 一视同仁；
3. 代码匹配、`ESCAPE`（`%` / `_` 不被当通配符）、黑名单过滤、`LIMIT 20`、
   排序优先级（代码精确 > 名称精确 > 其他）逐条保持；
4. 空白 / 大小写 / 特殊字符输入不 500（空白-only 仍按原约定 400）。

隔离：`duckdb_store` / `sqlite_store` fixture（`--basetemp` 下的临时库），
**绝不碰** `data/valuedashboard.duckdb`。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.blacklist import add_stock
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed

LIQUOR = "000858"  # 五 粮 液（单空格，核心用例）
VANKE = "000002"  # 万  科Ａ（双空格）
NANBO = "000012"  # 南　玻Ａ（全角空格 U+3000）
NINGBO = "002142"  # 宁\xa0波\xa0银行（NBSP；正式库无此写法，防回归扩展到其他 Unicode 空白）
MAOTAI = "600519"  # 贵州茅台（无内嵌空白，对照组）


# ─── 装配 ──────────────────────────────────────────────────────────


def _client(duck: DuckDBStore, sqlite: SQLiteStore) -> TestClient:
    from app.web.api.stock_detail import router as stock_detail_router

    app = FastAPI()
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.include_router(stock_detail_router)
    return TestClient(app)


@pytest.fixture
def client(duckdb_store: DuckDBStore, sqlite_store: SQLiteStore) -> TestClient:
    seed = Seed(duckdb_store)
    seed.stock(LIQUOR, name="五 粮 液", exchange="SZSE", csrc_l1="制造业", is_listed=True)
    seed.stock(VANKE, name="万  科Ａ", exchange="SZSE", csrc_l1="房地产业", is_listed=True)
    seed.stock(NANBO, name="南\u3000玻Ａ", exchange="SZSE", csrc_l1="制造业", is_listed=True)
    seed.stock(NINGBO, name="宁\u00a0波\u00a0银行", exchange="SZSE", csrc_l1="金融业", is_listed=True)
    seed.stock(MAOTAI, name="贵州茅台", exchange="SSE", csrc_l1="制造业", is_listed=True)
    return _client(duckdb_store, sqlite_store)


def _search(client: TestClient, query: str) -> list[dict[str, Any]]:
    response = client.get("/api/stock/search", params={"query": query})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _codes(items: list[dict[str, Any]]) -> list[str]:
    return [row["stock_code"] for row in items]


# ─── 1. 核心用例：无空格查询命中带空格名称 ─────────────────────────


def test_compact_query_hits_name_with_embedded_ascii_space(client: TestClient) -> None:
    """用户实测的那一条：搜「五粮液」必须命中 `五 粮 液`。"""
    items = _search(client, "五粮液")

    assert _codes(items) == [LIQUOR]
    assert items[0]["name"] == "五 粮 液"


@pytest.mark.parametrize(
    ("code", "raw_name", "query"),
    [
        (VANKE, "万  科Ａ", "万科Ａ"),
        (NANBO, "南\u3000玻Ａ", "南玻Ａ"),
        (NINGBO, "宁\u00a0波\u00a0银行", "宁波银行"),
    ],
    ids=["double-space", "ideographic-space", "nbsp"],
)
def test_whitespace_variants_all_normalized(
    client: TestClient, code: str, raw_name: str, query: str
) -> None:
    items = _search(client, query)

    assert _codes(items) == [code]
    # 源数据保持原样（归一化只发生在查询侧）
    assert items[0]["name"] == raw_name


def test_query_side_whitespace_is_also_normalized(client: TestClient) -> None:
    """用户自己敲进空格/全角空格也必须命中（两侧同一正则）。"""
    for query in ("五 粮 液", "五　粮　液", " 五粮液 ", "\t五粮液\n"):
        items = _search(client, query)
        assert _codes(items) == [LIQUOR], query


# ─── 2. 原有匹配能力不回退 ────────────────────────────────────────


def test_code_match_still_works(client: TestClient) -> None:
    assert _codes(_search(client, "6005")) == [MAOTAI]  # 片段
    assert _codes(_search(client, "000858")) == [LIQUOR]  # 精确


def test_like_wildcards_are_escaped_not_interpreted(client: TestClient) -> None:
    """`%` / `_` 必须走 ESCAPE 当字面量：否则会返回全部股票。"""
    for query in ("%", "_", "%%", "__", "五%液", "\\"):
        assert _search(client, query) == [], query


def test_blacklisted_stock_disappears_from_search(
    client: TestClient, sqlite_store: SQLiteStore
) -> None:
    assert _codes(_search(client, "五粮液")) == [LIQUOR]

    add_stock(sqlite_store, LIQUOR)

    assert _search(client, "五粮液") == []
    assert _search(client, LIQUOR) == []


def test_limit_20_is_kept(client: TestClient, duckdb_store: DuckDBStore) -> None:
    seed = Seed(duckdb_store)
    for index in range(25):
        seed.stock(f"601{index:03d}", name=f"银行样本{index:02d}", exchange="SSE", is_listed=True)

    items = _search(client, "银行")

    assert len(items) == 20
    assert all("银行" in row["name"] for row in items)


def test_ordering_priority_is_kept(client: TestClient, duckdb_store: DuckDBStore) -> None:
    """名称精确（归一化后）必须排在子串命中之前 —— 与旧的 `name = ?` 优先级同义。

    故意给子串命中更小的代码：只按 `stock_code` 排会把 `000100` 放前面。
    """
    Seed(duckdb_store).stock(
        "000100", name="五粮液供应链", exchange="SZSE", is_listed=True
    )

    items = _search(client, "五粮液")

    assert _codes(items) == [LIQUOR, "000100"]


def test_search_does_not_mutate_stock_meta_names(
    client: TestClient, duckdb_store: DuckDBStore
) -> None:
    """归一化只许发生在查询侧：改源数据会污染下游（导出/详情/筛选）。"""
    _search(client, "五粮液")

    rows = duckdb_store.read_query(
        "SELECT name FROM stock_meta WHERE stock_code = ?", [LIQUOR]
    )
    assert rows[0]["name"] == "五 粮 液"


def test_exact_code_ranks_before_name_substring(
    client: TestClient, duckdb_store: DuckDBStore
) -> None:
    """代码精确仍是最优先档（`CASE WHEN stock_code = ? THEN 0`）。"""
    Seed(duckdb_store).stock(
        "000200", name="000858样本", exchange="SZSE", is_listed=True
    )

    items = _search(client, LIQUOR)

    assert _codes(items)[:2] == [LIQUOR, "000200"]


# ─── 3. 脏输入不炸 ────────────────────────────────────────────────


def test_whitespace_only_query_is_still_400(client: TestClient) -> None:
    for query in (" ", "\u3000", "\t\n", "\u00a0"):
        response = client.get("/api/stock/search", params={"query": query})
        assert response.status_code == 400, query


def test_mixed_case_and_noise_queries_do_not_crash(client: TestClient) -> None:
    for query in ("ABC", "abc", "ＡＢＣ", "wuliangye", "?*", "%_\\", "zzzzzz"):
        assert _search(client, query) == [], query
