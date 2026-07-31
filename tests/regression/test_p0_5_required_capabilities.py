from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.dsl.ast_nodes import FIELD_METADATA
from app.core.dsl.engine import DSLEngine
from app.core.init import DataInitializer
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


class XdxrAdapterManager:
    def fetch(self, request: FetchRequest) -> FetchResult:
        assert request.data_type == "xdxr"
        raw_response = b"xdxr"
        return FetchResult(
            data=[{
                "stock_code": request.stock_codes[0], "event_date": "2025-06-30", "category": 1,
                "fenhong": 1.0, "songzhuangu": 0.2, "peigu": None, "peigujia": None,
            }],
            metadata=SourceMetadata(
                source="tdx", fetch_time=datetime.now(timezone.utc), raw_response_hash=hashlib.sha256(raw_response).hexdigest(),
                confidence="approximate",
            ),
            raw_response=raw_response,
        )


def test_financial_sector_fields_are_migrated_catalogued_mapped_and_reasoned(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    required = {
        "core_tier1_capital_adequacy_ratio", "tier1_capital_adequacy_ratio",
        "capital_adequacy_ratio", "non_performing_loan_ratio", "provision_coverage_ratio",
        "risk_coverage_ratio",
    }
    columns = duckdb_store.read_query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'balance_sheet'"
    )
    assert required <= {row["column_name"] for row in columns}
    assert {f"balance.{field}" for field in required} <= set(FIELD_METADATA)
    dsl = DSLEngine(duck=duckdb_store, sqlite=sqlite_store)
    created = dsl.create("bank_capital_ratio", "balance.capital_adequacy_ratio")
    assert dsl.validate("bank_capital_ratio", created["version"])["valid"] is True

    duckdb_store.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, sw_level1)
           VALUES ('600000', 'bank', 'SSE', '银行')"""
    )
    initializer = DataInitializer.__new__(DataInitializer)
    initializer.duck = duckdb_store
    initializer.sqlite = sqlite_store
    with duckdb_store.transaction() as conn:
        initializer._upsert_financial_row(
            conn,
            "balance_sheet",
            "600000",
            {"REPORT_DATE": "2025-12-31", "CAPITAL_ADEQUACY_RATIO": 12.5},
        )
    initializer._record_missing_financial_sector_fields(
        "600000", [{"REPORT_DATE": "2025-12-31", "CAPITAL_ADEQUACY_RATIO": 12.5}]
    )

    assert duckdb_store.read_query(
        "SELECT capital_adequacy_ratio FROM balance_sheet WHERE stock_code = '600000'"
    ) == [{"capital_adequacy_ratio": 12.5}]
    missing = sqlite_store.query(
        "SELECT field_name, reason_code FROM missing_list WHERE stock_code = '600000' ORDER BY field_name"
    )
    assert missing == [
        {"field_name": "balance.core_tier1_capital_adequacy_ratio", "reason_code": "source_field_unavailable"},
        {"field_name": "balance.non_performing_loan_ratio", "reason_code": "source_field_unavailable"},
        {"field_name": "balance.provision_coverage_ratio", "reason_code": "source_field_unavailable"},
        {"field_name": "balance.tier1_capital_adequacy_ratio", "reason_code": "source_field_unavailable"},
    ]


def test_xdxr_lifecycle_persists_corporate_actions(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    duckdb_store.write_query(
        "INSERT INTO stock_meta (stock_code, name, exchange, is_listed) VALUES ('600000', 'bank', 'SSE', TRUE)"
    )
    initializer = DataInitializer.__new__(DataInitializer)
    initializer.duck = duckdb_store
    initializer.sqlite = sqlite_store
    initializer.adapter_mgr = XdxrAdapterManager()
    initializer._batch_id = "test-xdxr"

    report = initializer._fetch_xdxr()

    assert report == {"status": "success", "total": 1, "success": 1, "failed": 0, "rows_written": 1}
    assert duckdb_store.read_query(
        "SELECT stock_code, event_date, category, fenhong, songzhuangu FROM xdxr"
    ) == [{
        "stock_code": "600000", "event_date": datetime(2025, 6, 30).date(), "category": 1,
        "fenhong": 1.0, "songzhuangu": 0.2,
    }]
    assert {row["field_name"] for row in duckdb_store.read_query(
        "SELECT field_name FROM source_audit WHERE stock_code = '600000'"
    )} >= {"fenhong", "songzhuangu"}
