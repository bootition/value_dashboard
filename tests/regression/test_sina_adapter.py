"""Isolated regression tests for the Sina free financial-statement adapter.

All HTTP calls are mocked; fixtures under fixtures/sina/ are trimmed verbatim
copies of real quotes.sina.cn responses (600519 sh, 920000 BSE via sz prefix).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.sina_adapter import SinaAdapter, _paper_code
from app.core.init import DataInitializer

_FIXTURES = Path(__file__).parent / "fixtures" / "sina"


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


def _load_fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _payload_bytes(report_list: dict) -> bytes:
    periods = {}
    for date_key, items in report_list.items():
        periods[date_key] = {
            "rType": "合并期末",
            "rCurrency": "CNY",
            "data_source": "定期报告",
            "is_audit": "未审计",
            "audit_opinion": "",
            "publish_date": "20260425",
            "update_time": 1777029604,
            "is_exist_yoy": True,
            "data": list(items),
        }
    payload = {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_count": str(len(periods)),
                "report_date": [],
                "report_list": periods,
            },
        }
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _item(title: str, value: str | None) -> dict:
    return {
        "item_field": "X",
        "item_title": title,
        "item_value": value,
        "item_display_type": 2,
        "item_display": "小计",
        "item_precision": "f2",
        "item_group_no": 1,
        "item_source": "fzb",
        "item_tongbi": "",
    }


def _fixture_result(data_type: str, monkeypatch) -> FetchResult:
    fixture = {
        "balance_sheet": "sina_fzb_600519.json",
        "income_statement": "sina_lrb_600519.json",
        "cash_flow": "sina_llb_600519.json",
    }[data_type]
    content = _load_fixture(fixture)
    monkeypatch.setattr(
        "app.core.adapters.sina_adapter.requests.get",
        lambda *a, **k: _Response(content),
    )
    return SinaAdapter(rate_limit=0).fetch(
        FetchRequest(data_type=data_type, stock_codes=["600519"])
    )


# ─── 前缀规则 ───────────────────────────────────────────────────────


def test_bse_and_szse_codes_resolve_to_sz_prefix() -> None:
    assert _paper_code("920000") == "sz920000"
    assert _paper_code("000001") == "sz000001"
    assert _paper_code("300750") == "sz300750"
    assert _paper_code("400001") == "sz400001"
    assert _paper_code("600519") == "sh600519"
    assert _paper_code("abc123") is None


def test_bse_code_requests_use_sz_prefix(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_get(url, params=None, headers=None, timeout=None) -> _Response:
        calls.append({"url": url, "params": params})
        return _Response(_load_fixture("sina_fzb_920000.json"))

    monkeypatch.setattr("app.core.adapters.sina_adapter.requests.get", fake_get)

    result = SinaAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="balance_sheet", stock_codes=["920000"])
    )

    assert len(calls) == 1
    assert calls[0]["params"]["paperCode"] == "sz920000"
    assert calls[0]["params"]["source"] == "fzb"
    assert result.metadata.error is None
    assert {row["stock_code"] for row in result.data} == {"920000"}


def test_stock_codes_list_issues_one_request_per_code(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_get(url, params=None, headers=None, timeout=None) -> _Response:
        calls.append({"url": url, "params": params})
        return _Response(_load_fixture("sina_fzb_600519.json"))

    monkeypatch.setattr("app.core.adapters.sina_adapter.requests.get", fake_get)

    result = SinaAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="balance_sheet", stock_codes=["600519", "600000"])
    )

    assert len(calls) == 2
    assert [c["params"]["paperCode"] for c in calls] == ["sh600519", "sh600000"]
    assert result.raw_response.count(b'{"result":') == 2


# ─── 真实 fixture 字段映射 ──────────────────────────────────────────


def test_real_fixture_maps_all_seven_fields_from_fzb_lrb_llb(monkeypatch) -> None:
    balance = _fixture_result("balance_sheet", monkeypatch)
    assert balance.metadata.error is None
    assert balance.metadata.source == "sina"
    assert [row["report_date"] for row in balance.data] == [
        "2026-03-31", "2025-12-31", "2024-12-31",
    ]
    assert balance.data[0] == {
        "stock_code": "600519",
        "report_date": "2026-03-31",
        "total_assets": 319918844905.58,
        "total_liabilities": 38782958469.89,
        "total_equity_parent": 270894035676.27002,
        "total_equity": 281135886435.69,
    }

    income = _fixture_result("income_statement", monkeypatch)
    assert income.data[0] == {
        "stock_code": "600519",
        "report_date": "2026-03-31",
        "revenue": 53909252220.51,
        "parent_net_profit": 27242512886.45,
    }

    cashflow = _fixture_result("cash_flow", monkeypatch)
    assert cashflow.data[0] == {
        "stock_code": "600519",
        "report_date": "2026-03-31",
        "cf_from_operating": 26909891269.13,
    }

    for statement in (balance, income, cashflow):
        assert len(statement.data) == 3
        for row in statement.data[1:]:
            assert isinstance(row["report_date"], str)
            assert all(
                isinstance(value, float)
                for key, value in row.items()
                if key not in {"stock_code", "report_date"}
            )


def test_revenue_never_takes_total_operating_revenue(monkeypatch) -> None:
    items = [
        _item("营业总收入", "54702912385.230000"),
        _item("营业收入", "53909252220.510000"),
        _item("归属于母公司所有者的净利润", "27242512886.450000"),
    ]
    content = _payload_bytes({"20260331": items})
    monkeypatch.setattr(
        "app.core.adapters.sina_adapter.requests.get",
        lambda *a, **k: _Response(content),
    )

    result = SinaAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="income_statement", stock_codes=["600519"])
    )

    assert result.data[0]["revenue"] == 53909252220.51
    assert result.data[0].get("total_operating_revenue") is None


def test_missing_fields_are_not_fabricated(monkeypatch) -> None:
    items = [
        _item("流动资产", ""),
        _item("资产总计", "100.000000"),
        _item("负债合计", None),
        _item("所有者权益(或股东权益)合计", "80.000000"),
    ]
    content = _payload_bytes({"20260331": items})
    monkeypatch.setattr(
        "app.core.adapters.sina_adapter.requests.get",
        lambda *a, **k: _Response(content),
    )

    result = SinaAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="balance_sheet", stock_codes=["600519"])
    )

    assert result.data == [{
        "stock_code": "600519",
        "report_date": "2026-03-31",
        "total_assets": 100.0,
        "total_equity": 80.0,
    }]
    assert "total_liabilities" not in result.data[0]
    assert "total_equity_parent" not in result.data[0]


def test_synonym_variants_resolve_to_the_same_standard_field(monkeypatch) -> None:
    items = [
        _item("股东权益合计", "90.000000"),
        _item("所有者权益合计", "91.000000"),
        _item("归属于母公司所有者权益合计", "93.000000"),
        _item("归属于上市公司股东的净利润", "12.500000"),
        _item("所有者权益（或股东权益）合计", "92.000000"),
    ]
    content = _payload_bytes({"20260331": items})
    monkeypatch.setattr(
        "app.core.adapters.sina_adapter.requests.get",
        lambda *a, **k: _Response(content),
    )

    result = SinaAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="balance_sheet", stock_codes=["600519"])
    )

    # 同一报告期内同字段只取第一个命中项（避免同义变体重复覆盖）
    assert result.data[0]["total_equity"] == 90.0
    assert result.data[0]["total_equity_parent"] == 93.0
    assert result.data[0]["parent_net_profit"] == 12.5


# ─── 原始字节与 hash ────────────────────────────────────────────────


def test_raw_response_is_the_verbatim_content_and_hash_is_valid(monkeypatch) -> None:
    content = _load_fixture("sina_fzb_600519.json")
    monkeypatch.setattr(
        "app.core.adapters.sina_adapter.requests.get",
        lambda *a, **k: _Response(content),
    )

    result = SinaAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="balance_sheet", stock_codes=["600519"])
    )

    assert result.raw_response == content
    assert isinstance(result.raw_response, bytes)
    assert result.metadata.raw_response_hash == hashlib.sha256(content).hexdigest()
    assert result.metadata.api_version == "sina-getFinanceReport2022-1"
    assert result.metadata.confidence == "strict"


def test_http_error_becomes_empty_result_without_crash(monkeypatch) -> None:
    class _FailingResponse:
        def raise_for_status(self) -> None:
            import requests
            raise requests.HTTPError("403")

    monkeypatch.setattr(
        "app.core.adapters.sina_adapter.requests.get",
        lambda *a, **k: _FailingResponse(),
    )

    result = SinaAdapter(rate_limit=0).fetch(
        FetchRequest(data_type="balance_sheet", stock_codes=["600519"])
    )

    assert result.data == []
    assert result.metadata.error is not None
    assert result.metadata.confidence == "missing"


# ─── DataInitializer canonical 写入集成 ─────────────────────────────


def test_sina_rows_are_written_canonically_with_batch_archive_audit(
    duckdb_store, sqlite_store,
) -> None:
    initializer = DataInitializer(duck=duckdb_store, sqlite=sqlite_store)
    raw_response = _load_fixture("sina_fzb_600519.json")
    result = FetchResult(
        data=[{
            "stock_code": "600519",
            "report_date": "2026-03-31",
            "total_assets": 319918844905.58,
            "total_liabilities": 38782958469.89,
            "total_equity_parent": 270894035676.27002,
            "total_equity": 281135886435.69,
        }],
        metadata=SourceMetadata(
            source="sina",
            fetch_time=datetime.now(timezone.utc),
            raw_response_hash=hashlib.sha256(raw_response).hexdigest(),
            confidence="strict",
            api_version="sina-getFinanceReport2022-1",
        ),
        raw_response=raw_response,
    )

    with duckdb_store.transaction() as conn:
        initializer._upsert_financial_row(conn, "balance_sheet", "600519", result.data[0])
        batch_id = initializer._record_batch_in_connection(
            conn, result, "balance_sheet", len(result.data)
        )
        initializer._record_field_audit_in_connection(
            conn, result, result.data, "600519", "report_date", batch_id
        )

    row = duckdb_store.read_query(
        """SELECT stock_code, report_date, total_assets, total_liabilities,
                  total_equity, total_equity_parent, raw_data
           FROM balance_sheet WHERE stock_code = '600519'"""
    )[0]
    assert row["stock_code"] == "600519"
    assert str(row["report_date"]) == "2026-03-31"
    assert row["total_assets"] == 319918844905.58
    assert row["total_liabilities"] == 38782958469.89
    assert row["total_equity"] == 281135886435.69
    assert row["total_equity_parent"] == 270894035676.27002
    assert row["raw_data"] is not None

    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS count FROM fetch_batch WHERE batch_id = ?", [batch_id]
    )[0]["count"] == 1
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS count FROM raw_response_archive WHERE raw_response_hash = ?",
        [result.metadata.raw_response_hash],
    )[0]["count"] == 1

    audits = duckdb_store.read_query(
        "SELECT field_name, report_date FROM source_audit WHERE fetch_batch_id = ?",
        [batch_id],
    )
    assert {row["field_name"] for row in audits} == {
        "total_assets", "total_liabilities", "total_equity", "total_equity_parent",
    }
    assert all(str(row["report_date"]) == "2026-03-31" for row in audits)


def test_sina_income_rows_survive_the_completeness_gate() -> None:
    from app.core.init import DataInitializer as DI

    row = {"stock_code": "600519", "report_date": "2026-03-31",
           "revenue": 53909252220.51, "parent_net_profit": 27242512886.45}
    assert DI._financial_row_is_complete("income_statement", row)
    assert DI._financial_row_is_complete("balance_sheet", {
        "total_assets": 1.0, "total_liabilities": 1.0, "total_equity": 1.0})
    assert DI._financial_row_is_complete("cash_flow", {"cf_from_operating": 1.0})
