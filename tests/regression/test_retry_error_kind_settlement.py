"""事故出处：reports/117 / 2026-11-12。

Regression: 重试路径必须尊重 error_kind，不得把"合法空/源不支持"当失败。

2026-11-12 生产验证发现的最后一环：适配器已能标注 `error_kind=legal_empty`
（新股无已实施分红）与 `source_unsupported`（北交所/B 股无该源），但
`_retry_failed_tasks` 仍只看 `status != success`，于是这些任务被反复重试直到
`max_retries`，最终以 ``retry_exhausted`` 的假名义永久写进 missing_list。

修复后：重新抓取若判定为这两类，直接登记真实原因（source_empty /
source_unsupported）并移出重试队列，不消耗重试次数。
"""

from __future__ import annotations

import pytest

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from app.core.update import IncrementalUpdater


def _seed_retry(sqlite_store: SQLiteStore, *, code: str = "301688", retry_count: int = 4) -> int:
    sqlite_store.execute(
        """INSERT INTO retry_list
               (stock_code, data_type, adapter, error, retry_count, last_attempt,
                extra_json, max_retries)
           VALUES (?, 'dividends', 'manager', 'CNINFO dividends unsupported', ?, NULL, '{}', 5)""",
        [code, retry_count],
    )
    rows = sqlite_store.query(
        "SELECT id FROM retry_list WHERE stock_code = ? AND data_type = 'dividends'", [code]
    )
    return int(rows[0]["id"])


def _task(retry_id: int, *, code: str = "301688", retry_count: int = 4) -> dict:
    return {
        "id": retry_id,
        "stock_code": code,
        "data_type": "dividends",
        "adapter": "manager",
        "error": "CNINFO dividends unsupported",
        "retry_count": retry_count,
        "max_retries": 5,
        "extra_json": "{}",
        "last_attempt": None,
    }


@pytest.mark.parametrize(
    ("error_kind", "expected_reason"),
    [("legal_empty", "source_empty"), ("source_unsupported", "source_unsupported")],
)
def test_non_retryable_error_is_settled_as_missing_without_burning_retries(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch, error_kind: str, expected_reason: str,
) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    retry_id = _seed_retry(sqlite_store)

    monkeypatch.setattr(
        IncrementalUpdater, "refetch_one",
        lambda self, code, data_type, incremental=False: {
            "status": "failed",
            "error": "无已实施分红",
            "error_kind": error_kind,
        },
    )

    report = updater._retry_failed_tasks([_task(retry_id)])

    assert report["resolved_non_retryable"] == 1
    assert report["still_failing"] == 0
    assert sqlite_store.query("SELECT id FROM retry_list WHERE id = ?", [retry_id]) == []
    missing = sqlite_store.query(
        """SELECT reason_code FROM missing_list
           WHERE stock_code = '301688' AND field_name = 'dividends' AND resolved_at IS NULL"""
    )
    assert [row["reason_code"] for row in missing] == [expected_reason]


def test_real_source_failure_still_consumes_a_retry(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = IncrementalUpdater(duck=duckdb_store, sqlite=sqlite_store)
    # 从 1 开始：+1 后仍未达上限，条目应留在队列里（区别于"直接结算"）。
    retry_id = _seed_retry(sqlite_store, retry_count=1)

    monkeypatch.setattr(
        IncrementalUpdater, "refetch_one",
        lambda self, code, data_type, incremental=False: {
            "status": "failed",
            "error": "source down",
            "error_kind": "source_unavailable",
        },
    )

    report = updater._retry_failed_tasks([_task(retry_id, retry_count=1)])

    assert report["resolved_non_retryable"] == 0
    assert report["still_failing"] == 1
    rows = sqlite_store.query("SELECT retry_count FROM retry_list WHERE id = ?", [retry_id])
    assert len(rows) == 1, "真故障必须留在重试队列里"
    assert int(rows[0]["retry_count"]) == 2
    missing = sqlite_store.query(
        "SELECT reason_code FROM missing_list WHERE stock_code = '301688' AND field_name = 'dividends'"
    )
    assert missing == [], "未达上限的真故障不得进入 missing_list"
