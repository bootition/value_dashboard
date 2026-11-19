"""筛选就绪决策缓存的"新鲜度"回归（缺陷 A / B，2026-11-18）。

背景（已实测确认的真缺陷，不是猜测）：

* 缺陷 A —— `screening_readiness_cache_key()` 对 12 张表只取
  `COUNT(*)` + `MIN/MAX(...)`。对**就地 UPDATE / 内部替换**完全透明：
  交易日历把 03-04、03-05 两个工作日换成 03-07、03-08 两个周末后，
  (COUNT, MIN, MAX) 三元组一模一样 → 指纹不变 → 缓存命中 → 返回
  `ready=True`，而 `_has_complete_trading_calendar()` 按代码语义应为 False。
  这就是"假绿"。本文件锁定修复后的行为：**日历内部替换必须让指纹变化**。
* 缺陷 B —— `allow_stale=True` 曾让 TTL 只当"刷新触发器"：过期多久都返回
  旧决策，而后台 worker 遇写锁直接 return、异常只 `logger.warning`，
  过期决策可以被无限期当作"已就绪"返回；响应体也不带 `cached_at`/`stale`。
  修复后 stale 有硬上限（2×TTL），且后台刷新结果必须落盘留痕。

这些用例是**新增覆盖**（此前只测了"过期仍返回旧决策"这一错误期望）。
"""

from __future__ import annotations

import json
import types
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.core.data_quality import (
    SCREENING_READINESS_CACHE_KEY,
    SCREENING_READINESS_CACHE_TTL_SECONDS,
    SCREENING_READINESS_REFRESH_STATE_KEY,
    SCREENING_READINESS_STALE_MAX_AGE_SECONDS,
    _screening_readiness_refresh_worker,
    load_screening_readiness_cache,
    load_screening_readiness_entry,
    screening_readiness_cache_key,
    store_screening_readiness_cache,
)
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.web.api.screening import _require_current_screenability
from tests.conftest import insert_matching_trading_calendar, insert_minimum_screenable_data

_READY_DECISION = {"ready": True, "readiness": {"ready": True}, "warning_codes": []}
_NOT_READY_DECISION = {
    "ready": False,
    "readiness": {"ready": False},
    "warning_codes": ["TRADING_CALENDAR_UNAVAILABLE"],
}


class _FakeRequest:
    def __init__(self, duck: DuckDBStore, sqlite: SQLiteStore) -> None:
        state = types.SimpleNamespace()
        state.duck = duck
        state.sqlite = sqlite
        state.startup_readiness = {}
        self.app = types.SimpleNamespace(state=state)


def _seed_screenable(duck: DuckDBStore, sqlite: SQLiteStore) -> None:
    duck.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, listing_date, is_st, is_suspended)
           VALUES ('000001', 'Test', 'SZSE', '2020-01-01', false, false)"""
    )
    insert_minimum_screenable_data(duck)
    insert_matching_trading_calendar(duck, sqlite)


def _store_decision(
    sqlite: SQLiteStore, fingerprint: str, decision: dict, *, age_seconds: int,
) -> None:
    """Persist a decision whose recorded update time is ``age_seconds`` old."""
    updated = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    sqlite.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                         updated_at=excluded.updated_at""",
        [
            SCREENING_READINESS_CACHE_KEY,
            json.dumps({
                "fingerprint": fingerprint,
                "decision": decision,
                "updated_at": updated,
            }),
            updated,
        ],
    )


def _read_refresh_state(sqlite: SQLiteStore) -> dict:
    """读 ``data_refresh_state`` 里的筛就绪刷新记账（{} = 从未记账）。

    生产侧只有**写**方（``record_screening_readiness_refresh_outcome``，由
    后台刷新 worker 在跳过/失败/成功三条路径上调用），没有生产读方；原先
    暴露给测试的 ``read_screening_readiness_refresh_state()`` 因此删掉了。
    这里直接按原键读，断言内容与删前完全一致（包括"从未记账 == {}"）。
    """
    rows = sqlite.query(
        "SELECT value FROM data_refresh_state WHERE key = ?",
        [SCREENING_READINESS_REFRESH_STATE_KEY],
    )
    if not rows or not rows[0].get("value"):
        return {}
    try:
        payload = json.loads(rows[0]["value"])
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def test_fingerprint_changes_when_calendar_rows_replaced_in_place(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """缺陷 A：日历"内部替换"（COUNT/MIN/MAX 全等）必须改变指纹并让缓存失效。

    ⚠️ 这是本缺陷的核心复现：改动前后
    ``SELECT COUNT(*), MIN(trade_date), MAX(trade_date)`` 完全相同，
    旧指纹因此完全不变 → 缓存命中 → 对外 `ready=True`（假绿）。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    baseline = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert baseline is not None
    store_screening_readiness_cache(sqlite_store, baseline, _READY_DECISION)

    legacy_before = sqlite_store.query(
        "SELECT COUNT(*) AS n, MIN(trade_date) AS lo, MAX(trade_date) AS hi FROM trading_dates"
    )[0]

    # 典型调休/回滚修正（与缺陷报告里那条实验完全同型）：把两个**工作日**
    # 换成区间内的两个**周末**日，保证 COUNT / MIN / MAX 三元组完全不变。
    dates = [
        str(row["trade_date"])
        for row in sqlite_store.query("SELECT trade_date FROM trading_dates ORDER BY trade_date")
    ]
    lo, hi = datetime.fromisoformat(dates[0][:10]), datetime.fromisoformat(dates[-1][:10])
    existing = set(dates)
    replace = dates[1:3]  # 不碰 MIN / MAX 两行
    weekend_donors: list[str] = []
    cursor_day = lo + timedelta(days=1)
    while cursor_day < hi and len(weekend_donors) < 2:
        iso = cursor_day.date().isoformat()
        if cursor_day.weekday() >= 5 and iso not in existing:
            weekend_donors.append(iso)
        cursor_day += timedelta(days=1)
    assert len(weekend_donors) == 2, "fixture 区间内应存在两个未被占用的周末日"
    sqlite_store.execute(
        "DELETE FROM trading_dates WHERE trade_date IN (?, ?)", replace,
    )
    sqlite_store.execute(
        "INSERT INTO trading_dates (trade_date) VALUES (?), (?)", weekend_donors,
    )

    legacy_after = sqlite_store.query(
        "SELECT COUNT(*) AS n, MIN(trade_date) AS lo, MAX(trade_date) AS hi FROM trading_dates"
    )[0]
    # 前提校验：这正是旧指纹看不见的那种改动（旧实现下指纹必然不变）。
    assert legacy_after["n"] == legacy_before["n"]
    assert str(legacy_after["lo"]) == str(legacy_before["lo"])
    assert str(legacy_after["hi"]) == str(legacy_before["hi"])

    after = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after is not None
    assert after != baseline, "日历内部替换后指纹必须变化，否则就是假绿"

    # 调用方会用**新**指纹去查缓存：必须 miss（然后同步重算），
    # 绝不能把 baseline 时代的 ready=True 决策当成当前结论返回。
    assert load_screening_readiness_cache(sqlite_store, after) is None
    assert load_screening_readiness_cache(sqlite_store, after, allow_stale=True) is None
    assert load_screening_readiness_entry(sqlite_store, after, allow_stale=True) is None
    # 反向验证：旧指纹那条记录仍在（只是再也匹配不上当前库状态），
    # 说明"失效"来自内容指纹而不是记录被删。
    assert load_screening_readiness_cache(sqlite_store, baseline) == _READY_DECISION


def test_fingerprint_changes_when_published_override_payload_edited_in_place(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """同型盲区：`manual_overrides` 就地改 payload 不刷新 created_at。"""
    _seed_screenable(duckdb_store, sqlite_store)
    sqlite_store.execute(
        """INSERT INTO manual_overrides
           (stock_code, field_name, report_date, override_value, reason, status, created_at)
           VALUES ('000001', 'total_assets', '2025-12-31', 200, 'verified', 'published',
                   '2026-01-01T00:00:00+00:00')"""
    )
    baseline = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert baseline is not None

    # 就地改 payload：COUNT 与 MAX(created_at) 都不变。
    sqlite_store.execute(
        "UPDATE manual_overrides SET override_value = 999 WHERE field_name = 'total_assets'"
    )
    legacy = sqlite_store.query(
        """SELECT COUNT(*) AS n, MAX(created_at) AS created_max
           FROM manual_overrides WHERE status = 'published' AND rolled_back_at IS NULL"""
    )[0]
    assert legacy["n"] == 1
    assert legacy["created_max"] == "2026-01-01T00:00:00+00:00"

    after = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after is not None and after != baseline


def test_fingerprint_changes_when_financial_row_restated_in_place(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """同型盲区：财务表就地重述（`repair_financials.py` 场景）。"""
    _seed_screenable(duckdb_store, sqlite_store)
    baseline = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert baseline is not None

    # 就地改一个数值列：COUNT 与 MAX(report_date) 都不变。
    duckdb_store.write_query(
        "UPDATE balance_sheet SET total_assets = total_assets + 7 WHERE stock_code = '000001'"
    )
    legacy = duckdb_store.read_query(
        "SELECT COUNT(*) AS n, MAX(report_date) AS m FROM balance_sheet"
    )[0]
    assert legacy["n"] >= 1

    after = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert after is not None and after != baseline


def test_gate_serves_stale_cache_within_hard_limit_and_exposes_freshness(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """硬上限内的 stale 仍可用（stale-while-revalidate 保留），但必须自证陈旧。"""
    _seed_screenable(duckdb_store, sqlite_store)
    fingerprint = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert fingerprint is not None
    _store_decision(
        sqlite_store, fingerprint, _READY_DECISION, age_seconds=SCREENING_READINESS_CACHE_TTL_SECONDS + 60,
    )

    gate = _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))

    assert gate["lock_active"] is False
    assert gate["readiness_source"] == "stale_cache"
    assert gate["readiness_stale"] is True
    assert gate["readiness_cached_at"] is not None
    assert gate["readiness_age_seconds"] > SCREENING_READINESS_CACHE_TTL_SECONDS


def test_gate_refuses_stale_ready_decision_beyond_hard_limit(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺陷 B：超过 2×TTL 的过期决策不得再对外宣称 `ready=True`。

    没有硬上限时，这段代码会拿到三天前的 `ready=True` 并**直接放行**（假绿）。
    修复后标签命中失败 → 强制同步重算 → 走下面的 `screening_readiness`。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    fingerprint = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert fingerprint is not None
    _store_decision(
        sqlite_store, fingerprint, _READY_DECISION,
        age_seconds=SCREENING_READINESS_STALE_MAX_AGE_SECONDS + 60,
    )

    # 硬上限外的条目绝不能被读到（无论 fresh 还是 stale 语义）。
    assert load_screening_readiness_cache(sqlite_store, fingerprint) is None
    assert load_screening_readiness_cache(sqlite_store, fingerprint, allow_stale=True) is None
    assert load_screening_readiness_entry(sqlite_store, fingerprint, allow_stale=True) is None

    calls: list[int] = []

    def _fake_readiness(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
        calls.append(1)
        return _NOT_READY_DECISION

    monkeypatch.setattr("app.core.data_quality.screening_readiness", _fake_readiness)

    with pytest.raises(HTTPException) as exc:
        _require_current_screenability(_FakeRequest(duckdb_store, sqlite_store))

    assert exc.value.status_code == 409
    assert exc.value.detail["reason_code"] == "minimum_data_not_ready"
    assert exc.value.detail["readiness_source"] == "computed"
    assert exc.value.detail["readiness_stale"] is False
    # 证明走的是"同步重算"分支，而不是静默复用过期决策。
    assert calls == [1]


def test_missing_updated_at_fails_closed_instead_of_bypassing_the_cap(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """缺 `updated_at` 的缓存行不得被当作新鲜放行（2026-11-19 审查拍到的一个真洞）。

    原实现把年龄闸门整块写在 `if age_seconds is not None:` 里，于是：
    缓存行**没有** `updated_at`（旧格式 / 迁移残留）→ `age_seconds=None`
    → **TTL 与 2×TTL 硬上限同时被跳过** → 旧决策被无限期当作"已就绪"返回。
    即"stale 有硬上限"只对写了时间戳的行成立。

    修复后：时间戳缺失或解析失败一律 fail-closed（返回 None，让调用方重算）。
    """
    _seed_screenable(duckdb_store, sqlite_store)
    fingerprint = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert fingerprint is not None

    # 注意：`data_refresh_state.updated_at` 列是 NOT NULL，所以"缺时间戳"的真实
    # 形态是**JSON payload 里没有 `updated_at` 字段**（旧版本写下的行 / 迁移残留），
    # 而列上的值另有其用。原实现只看 payload → `age_seconds=None` → 闸门整块跳过。
    fresh_col = datetime.now(UTC).isoformat()
    sqlite_store.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                         updated_at=excluded.updated_at""",
        [
            SCREENING_READINESS_CACHE_KEY,
            json.dumps({"fingerprint": fingerprint, "decision": _READY_DECISION}),
            fresh_col,
        ],
    )

    assert load_screening_readiness_entry(sqlite_store, fingerprint) is None
    assert load_screening_readiness_cache(sqlite_store, fingerprint) is None
    assert (
        load_screening_readiness_entry(sqlite_store, fingerprint, allow_stale=True)
        is None
    ), "没有时间戳 = 无法判定年龄 = 不得以 stale 名义放行"

    # payload 里写了但无法解析，同样必须 fail-closed
    sqlite_store.execute(
        """INSERT INTO data_refresh_state (key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                         updated_at=excluded.updated_at""",
        [
            SCREENING_READINESS_CACHE_KEY,
            json.dumps({
                "fingerprint": fingerprint,
                "decision": _READY_DECISION,
                "updated_at": "not-a-timestamp",
            }),
            fresh_col,
        ],
    )

    assert load_screening_readiness_entry(sqlite_store, fingerprint, allow_stale=True) is None


def test_background_refresh_failure_is_persisted_and_not_infinite_stale(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺陷 B：后台刷新失败要留痕，且 stale 不会因此无限期可用。"""
    _seed_screenable(duckdb_store, sqlite_store)
    fingerprint = screening_readiness_cache_key(duckdb_store, sqlite_store)
    assert fingerprint is not None
    _store_decision(
        sqlite_store, fingerprint, _READY_DECISION,
        age_seconds=SCREENING_READINESS_CACHE_TTL_SECONDS + 60,
    )
    assert _read_refresh_state(sqlite_store) == {}

    def _boom(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
        raise RuntimeError("simulated readiness scan explosion")

    monkeypatch.setattr("app.core.data_quality.screening_readiness", _boom)
    _screening_readiness_refresh_worker(
        duckdb_store, sqlite_store, fingerprint, f"{duckdb_store.db_path}|{sqlite_store.db_path}|{fingerprint}",
    )

    state = _read_refresh_state(sqlite_store)
    assert state["last_failure_at"], "刷新失败必须落盘，不能只有 logger.warning"
    assert state["consecutive_failures"] == 1
    assert "simulated readiness scan explosion" in (state["last_error"] or "")

    # 失败不会延长 stale 窗口：把同一条决策推到硬上限之外就再也读不到。
    _store_decision(
        sqlite_store, fingerprint, _READY_DECISION,
        age_seconds=SCREENING_READINESS_STALE_MAX_AGE_SECONDS + 1,
    )
    assert load_screening_readiness_cache(sqlite_store, fingerprint, allow_stale=True) is None

    # 写锁导致的跳过同样要留痕（以前是静默 return）。
    monkeypatch.setattr(
        "app.core.storage.update_lock.any_write_lock_active", lambda *_a, **_k: True,
    )
    _screening_readiness_refresh_worker(
        duckdb_store, sqlite_store, fingerprint, f"{duckdb_store.db_path}|{sqlite_store.db_path}|{fingerprint}",
    )
    state = _read_refresh_state(sqlite_store)
    assert state["last_skip_reason"] == "write_lock_active"
    assert state["consecutive_failures"] == 2

    # 成功刷新会清零失败计数（同一个 key，可被运维直接读取）。
    monkeypatch.setattr(
        "app.core.storage.update_lock.any_write_lock_active", lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "app.core.data_quality.screening_readiness", lambda *_a, **_k: _READY_DECISION,
    )
    _screening_readiness_refresh_worker(
        duckdb_store, sqlite_store, fingerprint, f"{duckdb_store.db_path}|{sqlite_store.db_path}|{fingerprint}",
    )
    state = _read_refresh_state(sqlite_store)
    assert state["consecutive_failures"] == 0
    assert state["last_success_at"]
    assert state["last_failure_at"], "失败历史不应被成功刷新抹掉"
    assert SCREENING_READINESS_REFRESH_STATE_KEY == "screening_readiness_refresh_state"


def test_stale_hard_limit_value_is_bounded_and_anchored() -> None:
    """硬上限的**数值**必须被绝对断言锚住（2026-11-19 审查欠账 ①）。

    变异实测：把 `STALE_MAX_AGE` 从 2×TTL 改成「10 年」，原有用例**仍然全绿** ——
    因为夹具是用同一个常量构造的（自证）。这里加与实现无关的绝对边界：
    上限必须 > TTL（否则 stale-while-revalidate 失去意义），且 ≤ 4×TTL
    （否则一次后台刷新坏掉就会把过期几小时的决策当"就绪"返回）。
    """
    assert SCREENING_READINESS_STALE_MAX_AGE_SECONDS > SCREENING_READINESS_CACHE_TTL_SECONDS
    assert (
        SCREENING_READINESS_STALE_MAX_AGE_SECONDS
        <= 4 * SCREENING_READINESS_CACHE_TTL_SECONDS
    ), "硬上限不得超过 4×TTL，否则 stale 窗口长到等于没有上限"
    # 与 DESIGN 决策一致：当前取值 2×TTL
    assert SCREENING_READINESS_STALE_MAX_AGE_SECONDS == 2 * SCREENING_READINESS_CACHE_TTL_SECONDS
