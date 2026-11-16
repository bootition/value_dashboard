"""`/api/data-status/indicator-recompute` 的写锁守卫不得吞掉未知异常。

历史缺陷（reports/124 §5 审计项）：
`app/web/api/data_status.py::start_indicator_recompute()` 里守卫写成

    try:
        ... update_lock_active(...) → raise HTTPException(409)
    except HTTPException:
        raise
    except Exception:
        pass          # ← 锁探测一抛错，409 保护整体失效

后果：锁探测只要抛任何异常（锁文件格式异常、导入失败、句柄失效……），
守卫静默放行，重算线程仍会创建并与更新子进程并发写正式库。

现在只吞 `OSError`（文件系统级探测失败的乐观放行，且必须记日志），
其余异常向上抛（FastAPI → 500），并带一条失败即红的用例。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.storage.update_lock as update_lock
import app.web.api.data_status as data_status

_ADMIN_TOKEN = "test-admin-token"


class _DuckStub:
    """只提供守卫需要的 `db_path`。"""

    db_path = Path("C:/vd-test/valuedashboard.duckdb")


class _SqliteStub:
    """`auto_update_state` 查询与 pending 查询都返回空。"""

    def query(self, sql: str, params=None):  # 测试桩
        return []


def _make_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(data_status, "_INDICATOR_RECOMPUTE_STATE", {
        "status": "idle",
        "pending_codes": [],
        "result": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
    })
    started: list[tuple] = []
    monkeypatch.setattr(
        data_status,
        "_recompute_worker",
        lambda *args, **kwargs: started.append(args),
    )
    app = FastAPI()
    app.include_router(data_status.router)
    app.state.sqlite = _SqliteStub()
    app.state.duck = _DuckStub()
    app.state.admin_token = _ADMIN_TOKEN
    app.state.started_workers = started
    return TestClient(app)


def _post(client: TestClient):
    return client.post(
        "/api/data-status/indicator-recompute",
        headers={"x-vd-admin-token": _ADMIN_TOKEN},
    )


def test_update_lock_guard_returns_409_when_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    monkeypatch.setattr(update_lock, "update_lock_active", lambda path: True)

    response = _post(client)

    assert response.status_code == 409
    assert "另一更新任务正在运行" in response.json()["detail"]


def test_update_lock_guard_propagates_unexpected_probe_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁探测抛非 OSError（这里用 RuntimeError 代表"锁文件格式异常/句柄坏了"）
    必须向上抛（→ 500），绝不能静默放行让重算线程与更新子进程并发写。
    """
    client = _make_client(monkeypatch)

    def _boom(path):  # 测试桩
        raise RuntimeError("lock file unreadable")

    monkeypatch.setattr(update_lock, "update_lock_active", _boom)

    with pytest.raises(RuntimeError, match="lock file unreadable"):
        _post(client)

    # 没有创建重算线程（守卫失败即中止）
    assert client.app.state.started_workers == []


def test_update_lock_guard_fails_open_only_on_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`OSError` 是唯一的乐观放行分支（文件系统探测失败），且必须留日志。"""
    client = _make_client(monkeypatch)
    warnings: list[tuple] = []
    original_warning = data_status.logger.warning

    def _capture(msg, *args, **kwargs):  # 测试桩
        warnings.append((msg, args, kwargs))
        return original_warning(msg, *args, **kwargs)

    monkeypatch.setattr(data_status.logger, "warning", _capture)

    def _oserror(path):  # 测试桩
        raise OSError("stat failed")

    monkeypatch.setattr(update_lock, "update_lock_active", _oserror)

    response = _post(client)

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert len(client.app.state.started_workers) == 1
    assert any("OSError" in str(msg) for msg, _args, _kwargs in warnings), warnings
    assert any(kwargs.get("exc_info") for _msg, _args, kwargs in warnings)


def test_stage_query_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`auto_update_state` 查询本身坏掉时同样不能静默放行。"""
    client = _make_client(monkeypatch)

    class _BrokenSqlite:
        def query(self, sql: str, params=None):  # 测试桩
            raise RuntimeError("sqlite handle closed")

    client.app.state.sqlite = _BrokenSqlite()

    with pytest.raises(RuntimeError, match="sqlite handle closed"):
        _post(client)
