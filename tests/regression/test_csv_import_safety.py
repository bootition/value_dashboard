from __future__ import annotations

import pandas as pd
import pytest

from scripts.import_csv_to_db import _prepare_frame, import_balance_sheet
from app.web.api.screening import _csv_cell


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> _RecordingConnection:
        self.statements.append(statement)
        return self

    def fetchone(self) -> tuple[int]:
        return (1,)


def test_prepare_frame_reorders_csv_to_the_explicit_sql_target_columns() -> None:
    prepared = _prepare_frame(
        pd.DataFrame([{"report_date": "2025-12-31", "total_assets": 100, "stock_code": "1"}]),
        ["stock_code", "report_date", "total_assets", "raw_data"],
    )

    assert prepared.columns.tolist() == ["stock_code", "report_date", "total_assets", "raw_data"]
    assert prepared.iloc[0].to_dict() == {
        "stock_code": "1", "report_date": "2025-12-31", "total_assets": 100, "raw_data": None,
    }


def test_invalid_csv_cannot_delete_existing_financial_rows(tmp_path) -> None:
    csv_path = tmp_path / "balance.csv"
    pd.DataFrame([{"stock_code": "000001", "total_assets": 100}]).to_csv(csv_path, index=False)
    connection = _RecordingConnection()

    with pytest.raises(ValueError, match="report_date"):
        import_balance_sheet(connection, csv_path)

    assert connection.statements == []


def test_valid_csv_import_does_not_issue_a_delete(tmp_path) -> None:
    csv_path = tmp_path / "balance.csv"
    pd.DataFrame([{
        "stock_code": "000001", "report_date": "2025-12-31", "total_assets": 100,
    }]).to_csv(csv_path, index=False)
    connection = _RecordingConnection()

    import_balance_sheet(connection, csv_path)

    assert all("DELETE FROM balance_sheet" not in statement for statement in connection.statements)


@pytest.mark.parametrize("value", ["=cmd()", "+1+1", "-1+1", "@cmd", "\tcmd", "\rcmd"])
def test_csv_formula_prefixes_are_escaped(value: str) -> None:
    assert _csv_cell(value) == "'" + value
