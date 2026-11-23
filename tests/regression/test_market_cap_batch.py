"""`market_cap.resolve_shares_asof_batch`（时点股本批量 API）回归。

守的五件事（任务书 A/B 的验收口径）
-----------------------------------
1. **批量 == 逐个**：同一批请求，`resolve_shares_asof_batch` 的结果与逐个调用
   `resolve_shares_asof` 逐字段（`shares` / `source` / `effective_date` /
   `price_date`）且逐类型相等；覆盖 `as_of=None`（不做 as-of）、历史缺行回退
   `stock_meta`、两处都空 → `none` 三类边界。
2. **重复 code / 不同 as_of**：返回 `dict[str, ShareResolution]`，同一 code 只有
   一个条目 —— 首次出现的 `as_of` 生效（与 `dict` 语义一致，已写进 docstring）。
3. **空输入不发 SQL**：`requests=[]` → `{}`，一条查询都不许发。
4. **缺表不抛**：旧库没有 `share_capital_history`（`_is_missing_relation`）→
   整批按"无历史"回退 `stock_meta`，与单只行为一致。
5. **绝不用未来行**：`effective_date > as_of` 的股本行**永远**不许被选中
   （包括"只有未来行、连 meta 都没有"→ `none` 而不是未来值）。

"改前"基线 = `git show HEAD:app/core/market_cap.py` 里的旧单只实现
（`scripts/compare_price_domain_before_after.py --codes 5563` 另有全市场对拍）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest

from app.core.market_cap import (
    SOURCE_HISTORY,
    SOURCE_META,
    SOURCE_NONE,
    ShareResolution,
    latest_market_cap,
    resolve_shares_asof,
    resolve_shares_asof_batch,
)
from app.core.storage.duckdb_store import DuckDBStore
from tests.support.seed import Seed

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 边界样本：历史命中 / 历史缺行回退 / 两处都空 / 只有未来行 / meta 非正。
WITH_HISTORY = "000001"      # 两条历史行（05-30 100 股、09-23 140 股），meta=140
META_ONLY = "000002"         # 无历史行，meta=200
NOTHING = "000003"           # 既无历史行也无 stock_meta 行
FUTURE_ONLY = "000004"       # 只有 2026-10-08 的历史行（晚于样本 as_of），meta=250
NO_META_FUTURE = "000005"    # 只有未来历史行、连 stock_meta 都没有
ZERO_META = "000006"         # 历史缺行且 meta=0（非正 → 视为没有）

PRICE_DATE = "2026-09-30"
EARLY_DATE = "2026-06-01"    # 早于 09-23 那条历史行


# ─── 夹具 ───────────────────────────────────────────────────────────


def _seed(duck: DuckDBStore, code: str, *, meta_shares: Any, history: list[tuple[str, float]]) -> None:
    seed = Seed(duck)
    seed.stock(
        code,
        exchange="SSE",
        is_listed=True,
        total_shares=meta_shares,
        circ_shares=meta_shares,
    )
    seed.shares(
        code,
        history,
        change_reason=None,
        is_anchor=True,
        verified=True,
        # 溯源三件套与 `test_market_cap_single_source.py` 同形状（NOT NULL 列）。
        source="cninfo_capital",
        raw_hash="seed",
        batch_id="seed",
    )


@pytest.fixture
def batch_stocks(duckdb_store: DuckDBStore) -> DuckDBStore:
    """六只股票的边界样本（见模块顶部常量）。"""
    _seed(duckdb_store, WITH_HISTORY, meta_shares=140, history=[
        ("2026-05-30", 100.0), ("2026-09-23", 140.0),
    ])
    _seed(duckdb_store, META_ONLY, meta_shares=200, history=[])
    _seed(duckdb_store, NOTHING, meta_shares=None, history=[])
    _seed(duckdb_store, FUTURE_ONLY, meta_shares=250, history=[("2026-10-08", 999.0)])
    _seed(duckdb_store, NO_META_FUTURE, meta_shares=None, history=[("2026-10-08", 888.0)])
    _seed(duckdb_store, ZERO_META, meta_shares=0, history=[])
    return duckdb_store


class _SqlProbe:
    """记录 SQL 条数，其余全部转发；`break_history=True` 时模拟"旧库没有历史表"。"""

    def __init__(self, inner: Any, *, break_history: bool = False) -> None:
        self.inner = inner
        self.break_history = break_history
        self.sqls: list[str] = []

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        self.sqls.append(" ".join(sql.split()))
        if self.break_history and "share_capital_history" in sql:
            # 与 DuckDB 旧库缺表时的报错形状一致（`_is_missing_relation` 认这个）。
            raise RuntimeError(
                "Catalog Error: Table with name share_capital_history does not exist!",
            )
        return self.inner.read_query(sql, params)


class _ConnectionDuck:
    """把裸 DuckDB 连接包成 `QuerySource`（返回 dict 行），并记录 SQL。"""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self.connection = connection
        self.sqls: list[str] = []

    def read_query(self, sql: str, params: list[Any] | None = None) -> list[dict]:
        self.sqls.append(" ".join(sql.split()))
        cursor = self.connection.execute(sql, params or [])
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


# ─── ① 批量 vs 逐个（含三类边界）────────────────────────────────────


def test_batch_matches_single_per_code_field_by_field(batch_stocks: DuckDBStore) -> None:
    """批量结果必须与逐个 `resolve_shares_asof` 逐字段 + 逐类型相等。"""
    requests = [
        (WITH_HISTORY, PRICE_DATE),
        (WITH_HISTORY, EARLY_DATE),      # 早于 09-23 那条 → 100 股
        (WITH_HISTORY, None),            # 不做 as-of → 回退 meta
        (META_ONLY, PRICE_DATE),
        (NOTHING, PRICE_DATE),
        (WITH_HISTORY, "2026-05-29"),    # 早于所有历史行 → 回退 meta
        (FUTURE_ONLY, PRICE_DATE),
        (NO_META_FUTURE, PRICE_DATE),
        (ZERO_META, PRICE_DATE),
        (f"  {META_ONLY}  ", PRICE_DATE),  # 首尾空白 → 规范化后同一只
        (NOTHING, None),
    ]
    for code, as_of in requests:
        normalized = code.strip()
        single = resolve_shares_asof(batch_stocks, normalized, as_of)
        batch = resolve_shares_asof_batch(batch_stocks, [(code, as_of)])[normalized]
        assert batch == single, (normalized, as_of)
        assert type(batch) is type(single)
        # dataclass `==` 会把 100 == 100.0 判等，类型单独再钉一遍
        assert type(batch.shares) is type(single.shares), (normalized, as_of)
        assert type(batch.effective_date) is type(single.effective_date), (normalized, as_of)
        assert type(batch.price_date) is type(single.price_date), (normalized, as_of)

    # 混合一批（每只 code 只出现一次）：结果与逐只相同，键是规范化后的代码
    unique_requests = [
        (WITH_HISTORY, PRICE_DATE),
        (META_ONLY, PRICE_DATE),
        (NOTHING, PRICE_DATE),
        (FUTURE_ONLY, PRICE_DATE),
        (NO_META_FUTURE, PRICE_DATE),
        (ZERO_META, None),
    ]
    mixed = resolve_shares_asof_batch(batch_stocks, unique_requests)
    assert set(mixed) == {
        WITH_HISTORY, META_ONLY, NOTHING, FUTURE_ONLY, NO_META_FUTURE, ZERO_META,
    }, "键必须是规范化后的股票代码"
    for code, as_of in unique_requests:
        normalized = code.strip()
        assert mixed[normalized] == resolve_shares_asof(batch_stocks, normalized, as_of)


def test_batch_covers_history_meta_and_none_sources(batch_stocks: DuckDBStore) -> None:
    """三类边界各自的 source / shares / effective_date 都要对。"""
    resolved = resolve_shares_asof_batch(batch_stocks, [
        (WITH_HISTORY, PRICE_DATE),
        (META_ONLY, PRICE_DATE),
        (NOTHING, PRICE_DATE),
        (ZERO_META, PRICE_DATE),
    ])
    no_asof = resolve_shares_asof_batch(batch_stocks, [(WITH_HISTORY, None)])[WITH_HISTORY]

    history = resolved[WITH_HISTORY]
    assert history.source == SOURCE_HISTORY
    assert history.shares == 140.0
    assert history.effective_date is not None
    assert history.effective_date.isoformat() == "2026-09-23"
    assert history.price_date is not None and history.price_date.isoformat() == PRICE_DATE

    meta = resolved[META_ONLY]
    assert (meta.source, meta.shares, meta.effective_date) == (SOURCE_META, 200.0, None)

    nothing = resolved[NOTHING]
    assert (nothing.source, nothing.shares) == (SOURCE_NONE, None)

    # meta 非正（0）= 没有可用股本，不是 0 股
    assert (resolved[ZERO_META].source, resolved[ZERO_META].shares) == (SOURCE_NONE, None)

    # as_of=None：不参与 as-of，直接 meta（历史表里明明有行也不许用）
    assert no_asof.source == SOURCE_META
    assert no_asof.shares == 140.0
    assert no_asof.effective_date is None
    assert no_asof.price_date is None


# ─── ② 重复 code / 不同 as_of ───────────────────────────────────────


def test_duplicate_code_keeps_first_as_of(batch_stocks: DuckDBStore) -> None:
    """同一 code 给不同 as_of：只返回一个条目，**首次出现的 as_of 生效**。"""
    first_wins = resolve_shares_asof_batch(batch_stocks, [
        (WITH_HISTORY, EARLY_DATE),
        (WITH_HISTORY, PRICE_DATE),
    ])
    assert list(first_wins) == [WITH_HISTORY], "同一 code 只能有一个条目"
    assert first_wins[WITH_HISTORY] == resolve_shares_asof(
        batch_stocks, WITH_HISTORY, EARLY_DATE,
    )
    assert first_wins[WITH_HISTORY].shares == 100.0

    last_wins = resolve_shares_asof_batch(batch_stocks, [
        (WITH_HISTORY, PRICE_DATE),
        (WITH_HISTORY, EARLY_DATE),
    ])
    assert last_wins[WITH_HISTORY] == resolve_shares_asof(
        batch_stocks, WITH_HISTORY, PRICE_DATE,
    )
    assert last_wins[WITH_HISTORY].shares == 140.0


def test_duplicate_code_with_none_as_of_keeps_first(
    batch_stocks: DuckDBStore,
) -> None:
    """`(code, None)` 也是"一次请求"：首次出现优先，之后的 as_of 不再生效。"""
    resolved = resolve_shares_asof_batch(batch_stocks, [
        (WITH_HISTORY, None),
        (WITH_HISTORY, PRICE_DATE),
    ])
    assert resolved[WITH_HISTORY].source == SOURCE_META
    assert resolved[WITH_HISTORY].price_date is None


# ─── ③ 空输入不发 SQL ───────────────────────────────────────────────


def test_empty_requests_issue_no_sql(batch_stocks: DuckDBStore) -> None:
    counting = _SqlProbe(batch_stocks)
    assert resolve_shares_asof_batch(counting, []) == {}
    assert counting.sqls == [], counting.sqls


def test_blank_code_issues_no_sql(batch_stocks: DuckDBStore) -> None:
    """空代码在单只入口是早退分支（source=none）→ 批量版也不许为它发 SQL。"""
    counting = _SqlProbe(batch_stocks)
    resolved = resolve_shares_asof_batch(counting, [("", PRICE_DATE), ("   ", None)])
    assert counting.sqls == [], counting.sqls
    assert list(resolved) == [""]
    assert (resolved[""].source, resolved[""].shares) == (SOURCE_NONE, None)
    assert resolved[""].price_date is None


def test_single_call_delegates_to_batch_with_one_request(
    batch_stocks: DuckDBStore,
) -> None:
    """单只入口 = 批量版的「一批一只」：SQL 条数与批量一致（不多发）。"""
    single_counter = _SqlProbe(batch_stocks)
    resolve_shares_asof(single_counter, WITH_HISTORY, PRICE_DATE)
    batch_counter = _SqlProbe(batch_stocks)
    resolve_shares_asof_batch(batch_counter, [(WITH_HISTORY, PRICE_DATE)])
    assert len(single_counter.sqls) == len(batch_counter.sqls) == 1


# ─── ④ 缺表 → 整批回退 meta（不抛）─────────────────────────────────


def test_missing_history_table_falls_back_to_meta(
    batch_stocks: DuckDBStore,
) -> None:
    """`_is_missing_relation` 识别出的缺表 → 整批按「无历史」回退，不抛。"""
    broken = _SqlProbe(batch_stocks, break_history=True)

    resolved = resolve_shares_asof_batch(broken, [
        (WITH_HISTORY, PRICE_DATE),
        (META_ONLY, PRICE_DATE),
        (NOTHING, PRICE_DATE),
        (WITH_HISTORY, None),
    ])

    assert resolved[WITH_HISTORY].source == SOURCE_META
    assert resolved[WITH_HISTORY].shares == 140.0
    assert resolved[META_ONLY].source == SOURCE_META
    assert resolved[NOTHING].source == SOURCE_NONE
    # 缺表时必须真的走了 meta-only 兜底 SQL（不是"静默返回空"）
    assert any("FROM stock_meta" in sql for sql in broken.sqls), broken.sqls


def test_missing_history_table_in_real_db_does_not_raise() -> None:
    """真·缺表库（只有 stock_meta）：批量与单只都不许抛，且回退 meta。"""
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE stock_meta (stock_code VARCHAR PRIMARY KEY, total_shares BIGINT)",
        )
        connection.execute("INSERT INTO stock_meta VALUES ('000001', 100), ('000002', 200)")
        duck = _ConnectionDuck(connection)

        resolved = resolve_shares_asof_batch(duck, [
            ("000001", PRICE_DATE), ("000002", PRICE_DATE), ("000003", PRICE_DATE),
        ])
        assert (resolved["000001"].source, resolved["000001"].shares) == (SOURCE_META, 100.0)
        assert (resolved["000002"].source, resolved["000002"].shares) == (SOURCE_META, 200.0)
        assert (resolved["000003"].source, resolved["000003"].shares) == (SOURCE_NONE, None)

        batch_history_sqls = [
            sql for sql in duck.sqls if "share_capital_history" in sql
        ]
        assert len(batch_history_sqls) == 1, duck.sqls

        single = resolve_shares_asof(duck, "000001", PRICE_DATE)
        assert (single.source, single.shares) == (SOURCE_META, 100.0)
        # 单只入口 = 一批一只：也只多 1 条历史 SQL（缺表后整批回退 meta）
        assert len([sql for sql in duck.sqls if "share_capital_history" in sql]) == 2
    finally:
        connection.close()


# ─── ⑤ 绝不用未来行（as-of 核心语义）────────────────────────────────


def test_future_history_row_is_never_selected(batch_stocks: DuckDBStore) -> None:
    """`effective_date > as_of` 的历史股本**绝不许**被选中。

    `FUTURE_ONLY` 只有一条 2026-10-08 的行（999 股），as_of=2026-09-30 →
    只能是 stock_meta 的 250；`NO_META_FUTURE` 连 meta 都没有 → `none`，
    绝不能"顺手拿一条历史行"（那就是未来数据泄漏）。
    """
    resolved = resolve_shares_asof_batch(batch_stocks, [
        (FUTURE_ONLY, PRICE_DATE),
        (NO_META_FUTURE, PRICE_DATE),
        (FUTURE_ONLY, "2026-10-07"),
        (FUTURE_ONLY, "2026-10-08"),   # 恰好生效当日 → 必须用历史 999
    ])

    assert resolved[FUTURE_ONLY].source == SOURCE_META
    assert resolved[FUTURE_ONLY].shares == 250.0
    assert resolved[FUTURE_ONLY].effective_date is None

    assert resolved[NO_META_FUTURE].source == SOURCE_NONE
    assert resolved[NO_META_FUTURE].shares is None

    # 同一 code 的重复请求按"首次出现"只保留第一条（见 ②），所以这里另起一批
    on_effective_date = resolve_shares_asof_batch(batch_stocks, [
        (FUTURE_ONLY, "2026-10-08"),
    ])
    assert on_effective_date[FUTURE_ONLY].source == SOURCE_HISTORY
    assert on_effective_date[FUTURE_ONLY].shares == 999.0
    one_day_early = resolve_shares_asof_batch(batch_stocks, [
        (FUTURE_ONLY, "2026-10-07"),
    ])
    assert one_day_early[FUTURE_ONLY].source == SOURCE_META


def test_batch_asof_picks_latest_row_not_first(batch_stocks: DuckDBStore) -> None:
    """多行历史 → 取 as-of 之前**最近**一条（不是最早一条、不是最新一条）。"""
    resolved = resolve_shares_asof_batch(batch_stocks, [
        (WITH_HISTORY, PRICE_DATE),
        (WITH_HISTORY, EARLY_DATE),
        (WITH_HISTORY, "2026-05-29"),   # 早于两条 → 回退 meta
    ])
    assert resolved[WITH_HISTORY].shares == 140.0  # 首次出现的 PRICE_DATE

    assert resolve_shares_asof_batch(batch_stocks, [(WITH_HISTORY, EARLY_DATE)])[
        WITH_HISTORY
    ].effective_date.isoformat() == "2026-05-30"
    assert resolve_shares_asof_batch(batch_stocks, [(WITH_HISTORY, "2026-05-29")])[
        WITH_HISTORY
    ].source == SOURCE_META


# ─── 改前 vs 改后：旧单只实现逐字段对拍 ─────────────────────────────


def _load_head_market_cap(tmp_path: Path) -> Any:
    """把 `HEAD:app/core/market_cap.py` 落盘并独立导入 = 真·改前实现。"""
    completed = subprocess.run(
        ["git", "show", "HEAD:app/core/market_cap.py"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("取不到 HEAD 版 market_cap.py（非 git 工作区）")
    path = tmp_path / "_head_market_cap.py"
    path.write_text(completed.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_vd_head_market_cap", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_batch_matches_head_single_implementation(
    batch_stocks: DuckDBStore, tmp_path: Path,
) -> None:
    """批量版与 **HEAD 的逐股实现** 对拍（同一批边界样本 + 混合 as_of）。"""
    head = _load_head_market_cap(tmp_path)
    requests = [
        (WITH_HISTORY, PRICE_DATE),
        (WITH_HISTORY, EARLY_DATE),
        (WITH_HISTORY, None),
        (META_ONLY, PRICE_DATE),
        (NOTHING, PRICE_DATE),
        (FUTURE_ONLY, PRICE_DATE),
        (NO_META_FUTURE, PRICE_DATE),
        (ZERO_META, PRICE_DATE),
        (NOTHING, None),
    ]
    for code, as_of in requests:
        new = resolve_shares_asof_batch(batch_stocks, [(code, as_of)])[code]
        old = head.resolve_shares_asof(batch_stocks, code, as_of)
        assert new.stock_code == old.stock_code
        assert new.shares == old.shares
        assert type(new.shares) is type(old.shares)
        assert new.source == old.source
        assert new.effective_date == old.effective_date
        assert type(new.effective_date) is type(old.effective_date)
        assert new.price_date == old.price_date
        assert type(new.price_date) is type(old.price_date)


# ─── latest_market_cap 的「预解析股本」捷径不许改口径 ───────────────


def test_latest_market_cap_ignores_stale_resolution(batch_stocks: DuckDBStore) -> None:
    """传入的 `ShareResolution` 与最终 as-of 不一致时，必须自己重新解析。"""
    wrong = ShareResolution(
        stock_code=WITH_HISTORY,
        shares=1.0,
        source=SOURCE_HISTORY,
        effective_date=None,
        price_date=None,  # 与 as_of=PRICE_DATE 不一致
    )
    cap = latest_market_cap(
        batch_stocks, WITH_HISTORY, close=10.0, price_date=PRICE_DATE,
        resolution=wrong,
    )
    assert cap["shares"] == 140.0
    assert cap["shares_source"] == SOURCE_HISTORY
    assert cap["total_market_cap"] == 10.0 * 140.0


def test_latest_market_cap_uses_matching_resolution(batch_stocks: DuckDBStore) -> None:
    """as-of 一致时直接采用（不多发 SQL），结果与自解析逐位相同。"""
    counter = _SqlProbe(batch_stocks)
    resolution = resolve_shares_asof(batch_stocks, WITH_HISTORY, PRICE_DATE)
    cap = latest_market_cap(
        counter, WITH_HISTORY, close=10.0, price_date=PRICE_DATE,
        resolution=resolution,
    )
    assert counter.sqls == [], counter.sqls  # 价格与股本都给了 → 一条 SQL 都不发
    assert cap["shares"] == 140.0
    assert cap["total_market_cap"] == 10.0 * 140.0
    assert cap["price_date"].isoformat() == PRICE_DATE
