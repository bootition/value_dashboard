from __future__ import annotations

import json

from app.core.config import Config
from app.core.pdf.correction import CorrectionManager
from app.core.pdf.manager import PDFManager
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def test_published_pdf_correction_is_visible_to_indicator_calculation(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
    database_paths,
) -> None:
    Config({"pdf": {"archive_root": "archive_pdf"}}, paths=database_paths)
    pdf_path = PDFManager(sqlite=sqlite_store).hot_dir / "000001" / "notice.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-correction-source")
    manager = CorrectionManager(duck=duckdb_store, sqlite=sqlite_store)
    created = manager.create_from_json(json.dumps({
        "announcement_id": "notice-1",
        "pdf_hash": __import__("hashlib").sha256(pdf_path.read_bytes()).hexdigest(),
        "page": 3,
        "report_period": "2025-12-31",
        "stock_code": "000001",
        "reason": "PDF correction",
        "fields": [{"field_name": "total_assets", "corrected_value": 123.0}],
    }))
    override_id = created["override_id"]

    assert manager.publish(override_id)["error"].startswith("模板必须先完成影响预览")
    assert manager.validate(override_id)["valid"] is True
    assert "error" not in manager.preview_impact(override_id)
    assert manager.publish(override_id)["published_fields"] == 1

    rows = sqlite_store.query(
        "SELECT field_name, override_value, status FROM manual_overrides WHERE stock_code = ? ORDER BY id",
        ["000001"],
    )
    assert rows == [
        {"field_name": "_template", "override_value": 0.0, "status": "published"},
        {"field_name": "total_assets", "override_value": 123.0, "status": "published"},
    ]
