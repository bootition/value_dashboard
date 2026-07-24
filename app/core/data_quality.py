"""Read-only data quality signals for the status API."""

from __future__ import annotations

from datetime import date, datetime

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore


def build_data_quality_status(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    """Return machine-readable freshness, completeness, lineage, and operation warnings."""
    dates = _data_dates(duck)
    dividends = duck.read_query(
        """
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (
                WHERE announcement_date IS NULL
                  AND (
                      (EXTRACT(MONTH FROM ex_date) = 12 AND EXTRACT(DAY FROM ex_date) = 31)
                      OR (EXTRACT(MONTH FROM ex_date) = 6 AND EXTRACT(DAY FROM ex_date) = 30)
                  )
            ) AS unverified_period_end_rows
        FROM dividends
        """
    )[0]
    lineage = duck.read_query(
        """
        SELECT
            COUNT(*) FILTER (WHERE LENGTH(s.raw_response_hash) != 64)
                AS invalid_hash_rows,
            COUNT(*) FILTER (WHERE f.batch_id IS NULL) AS orphan_batch_rows
        FROM source_audit s
        LEFT JOIN fetch_batch f ON s.fetch_batch_id = f.batch_id
        """
    )[0]
    operations = {
        "unpublished_overrides": sqlite.query(
            """
            SELECT COUNT(*) AS count FROM manual_overrides
            WHERE status != 'published' AND rolled_back_at IS NULL
            """
        )[0]["count"],
        "running_jobs": sqlite.query(
            "SELECT COUNT(*) AS count FROM job_logs WHERE status = 'running'"
        )[0]["count"],
    }

    warning_codes: list[str] = []
    balance_dates = dates["balance_sheet"]
    income_dates = dates["income_statement"]
    if (
        balance_dates["latest_record"] != balance_dates["latest_complete"]
        or income_dates["latest_record"] != income_dates["latest_complete"]
    ):
        warning_codes.append("FINANCIAL_SHELL_ROWS")
    if _is_stale(dates["price"], dates["indicator_snapshot"]["latest_complete"]):
        warning_codes.append("SNAPSHOT_STALE")
    if dividends["unverified_period_end_rows"]:
        warning_codes.append("DIVIDEND_DATES_UNVERIFIED")
    if lineage["invalid_hash_rows"] or lineage["orphan_batch_rows"]:
        warning_codes.append("LINEAGE_INVALID")
    if operations["unpublished_overrides"]:
        warning_codes.append("UNPUBLISHED_OVERRIDES")
    if operations["running_jobs"]:
        warning_codes.append("STALE_RUNNING_JOBS")

    return {
        "dates": dates,
        "dividends": dividends,
        "lineage": lineage,
        "operations": operations,
        "warning_codes": warning_codes,
    }


def _data_dates(duck: DuckDBStore) -> dict:
    price = duck.read_query("SELECT MAX(trade_date) AS value FROM price_daily_raw")[0]["value"]
    balance = duck.read_query(
        """
        SELECT
            MAX(report_date) AS latest_record,
            MAX(report_date) FILTER (
                WHERE total_assets IS NOT NULL
                  AND total_liabilities IS NOT NULL
                  AND COALESCE(total_equity_parent, total_equity) IS NOT NULL
            ) AS latest_complete
        FROM balance_sheet
        """
    )[0]
    income = duck.read_query(
        """
        SELECT
            MAX(report_date) AS latest_record,
            MAX(report_date) FILTER (
                WHERE revenue IS NOT NULL AND parent_net_profit IS NOT NULL
            ) AS latest_complete
        FROM income_statement
        """
    )[0]
    cash_flow = duck.read_query(
        """
        SELECT
            MAX(report_date) AS latest_record,
            MAX(report_date) FILTER (WHERE cf_from_operating IS NOT NULL) AS latest_complete
        FROM cash_flow
        """
    )[0]
    snapshot_columns = duck.read_query(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'indicator_snapshot'
        """
    )
    if "calculated_at" in {row["column_name"] for row in snapshot_columns}:
        snapshot = duck.read_query(
            """
            SELECT
                MAX(report_date) AS latest_complete,
                MAX(calculated_at) AS calculated_at
            FROM indicator_snapshot
            """
        )[0]
    else:
        snapshot = {
            "latest_complete": duck.read_query(
                "SELECT MAX(report_date) AS value FROM indicator_snapshot"
            )[0]["value"],
            "calculated_at": None,
        }
    return {
        "price": _date_string(price),
        "balance_sheet": _stringify_dates(balance),
        "income_statement": _stringify_dates(income),
        "cash_flow": _stringify_dates(cash_flow),
        "indicator_snapshot": _stringify_dates(snapshot),
    }


def _stringify_dates(values: dict) -> dict:
    return {key: _date_string(value) for key, value in values.items()}


def _date_string(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _is_stale(latest: str | None, earlier: str | None, days: int = 365) -> bool:
    if latest is None or earlier is None:
        return True
    latest_date = date.fromisoformat(latest[:10])
    earlier_date = date.fromisoformat(earlier[:10])
    return (latest_date - earlier_date).days > days
