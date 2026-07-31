"""Read-only data quality signals for the status API."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import time

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

DEFAULT_MINIMUM_HISTORY_OBSERVATIONS = 1_300
DEFAULT_MINIMUM_VOLUME_OBSERVATIONS = 1_300
_SCREENING_BLOCKING_WARNINGS = {
    "FINANCIAL_SHELL_ROWS",
    "SNAPSHOT_STALE",
    "DIVIDEND_DATES_UNVERIFIED",
    "MINIMUM_DATA_NOT_READY",
    "CODE_IDENTITY_ALIAS",
    "LIVE_SCHEMA_INCOMPATIBLE",
    "LINEAGE_INVALID",
}


def screening_readiness(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    """Return one fail-closed policy decision for every screening entry point."""
    quality = build_data_quality_status(duck, sqlite)
    blocking_warnings = sorted(
        set(quality["warning_codes"]) & _SCREENING_BLOCKING_WARNINGS
    )
    if not _has_complete_trading_calendar(duck, sqlite):
        blocking_warnings.append("TRADING_CALENDAR_UNAVAILABLE")
    return {
        "ready": quality["minimum_data_readiness"]["ready"] and not blocking_warnings,
        "readiness": quality["minimum_data_readiness"],
        "warning_codes": blocking_warnings,
    }


def _has_complete_trading_calendar(duck: DuckDBStore, sqlite: SQLiteStore) -> bool:
    """Require every listed stock's raw and QFQ series to match the persisted calendar.

    A global first/last-date check permits an incomplete calendar or an internal
    per-stock gap to pass. Screening indicators read latest_close from raw but
    require adjusted series for technicals, so validate both directions for both
    tables: every calendar date in a stock's observed window has a close, and
    every close date is represented by the calendar.
    """
    try:
        calendar_dates = sorted({
            date.fromisoformat(str(row["trade_date"])[:10]).isoformat()
            for row in sqlite.query("SELECT trade_date FROM trading_dates")
        })
        if not calendar_dates:
            return False
        values = ", ".join("(CAST(? AS DATE))" for _ in calendar_dates)
        missing = duck.read_query(
            f"""
            WITH calendar(trade_date) AS (VALUES {values}),
            raw_windows AS (
                SELECT m.stock_code, MIN(r.trade_date) AS first_date, MAX(r.trade_date) AS last_date
                FROM stock_meta m
                JOIN price_daily_raw r ON r.stock_code = m.stock_code AND r.close IS NOT NULL
                WHERE m.is_listed IS TRUE
                GROUP BY m.stock_code
            ),
            qfq_windows AS (
                SELECT m.stock_code, MIN(q.trade_date) AS first_date, MAX(q.trade_date) AS last_date
                FROM stock_meta m
                JOIN price_daily_qfq q ON q.stock_code = m.stock_code AND q.close IS NOT NULL
                WHERE m.is_listed IS TRUE
                GROUP BY m.stock_code
            ),
            raw_missing_expected AS (
                SELECT w.stock_code, 'raw' AS source
                FROM raw_windows w
                JOIN calendar c ON c.trade_date BETWEEN w.first_date AND w.last_date
                LEFT JOIN price_daily_raw r
                  ON r.stock_code = w.stock_code AND r.trade_date = c.trade_date AND r.close IS NOT NULL
                WHERE r.trade_date IS NULL
            ),
            raw_missing_calendar AS (
                SELECT r.stock_code, 'raw' AS source
                FROM price_daily_raw r
                JOIN stock_meta m ON m.stock_code = r.stock_code AND m.is_listed IS TRUE
                LEFT JOIN calendar c ON c.trade_date = r.trade_date
                WHERE r.close IS NOT NULL AND c.trade_date IS NULL
            ),
            qfq_missing_expected AS (
                SELECT w.stock_code, 'qfq' AS source
                FROM qfq_windows w
                JOIN calendar c ON c.trade_date BETWEEN w.first_date AND w.last_date
                LEFT JOIN price_daily_qfq q
                  ON q.stock_code = w.stock_code AND q.trade_date = c.trade_date AND q.close IS NOT NULL
                WHERE q.trade_date IS NULL
            ),
            qfq_missing_calendar AS (
                SELECT q.stock_code, 'qfq' AS source
                FROM price_daily_qfq q
                JOIN stock_meta m ON m.stock_code = q.stock_code AND m.is_listed IS TRUE
                LEFT JOIN calendar c ON c.trade_date = q.trade_date
                WHERE q.close IS NOT NULL AND c.trade_date IS NULL
            )
            SELECT stock_code FROM raw_missing_expected
            UNION SELECT stock_code FROM raw_missing_calendar
            UNION SELECT stock_code FROM qfq_missing_expected
            UNION SELECT stock_code FROM qfq_missing_calendar
            LIMIT 1
            """,
            calendar_dates,
        )
        return not missing
    except Exception:
        return False


def share_capital_violations(duck: DuckDBStore) -> list[str]:
    """Return listed stocks whose share-capital relation fails the integrity floor."""
    rows = duck.read_query(
        """
        SELECT stock_code
        FROM stock_meta
        WHERE is_listed IS TRUE
          AND NOT (
              total_shares IS NOT NULL AND total_shares > 0
              AND circ_shares IS NOT NULL AND circ_shares > 0
              AND circ_shares <= total_shares
          )
        ORDER BY stock_code
        """
    )
    return [row["stock_code"] for row in rows]


def snapshot_publish_gate(duck: DuckDBStore, sqlite: SQLiteStore) -> dict:
    """Fail-closed quality gate consulted before an indicator snapshot is published.

    The default gate enforces the share-capital integrity floor from
    minimum_data_readiness without repeating its full-universe history scan.
    Callers that need the complete policy (lineage, trading calendar, fresh
    prices) can inject screening_readiness instead.
    """
    try:
        violations = share_capital_violations(duck)
    except Exception as error:
        return {
            "ready": False,
            "reason": "share_capital_check_unavailable",
            "error": str(error),
            "share_capital_violations": [],
            "violation_count": None,
        }
    return {
        "ready": not violations,
        "reason": None if not violations else "share_capital_integrity",
        "share_capital_violations": violations[:20],
        "violation_count": len(violations),
    }


def minimum_data_readiness(
    duck: DuckDBStore,
    sqlite: SQLiteStore | None = None,
    *,
    minimum_history_observations: int = DEFAULT_MINIMUM_HISTORY_OBSERVATIONS,
    minimum_volume_observations: int = DEFAULT_MINIMUM_VOLUME_OBSERVATIONS,
) -> dict:
    """Evaluate the current, per-stock screening floor without mutating data."""
    if minimum_history_observations < 1 or minimum_volume_observations < 1:
        raise ValueError("readiness observation thresholds must be positive")
    compatibility = _live_schema_compatibility(duck)
    if not compatibility["compatible"]:
        missing = {"schema_compatibility": compatibility["missing"][:20]}
        return {
            "ready": False,
            "stock_count": 0,
            "missing": missing,
            "missing_counts": {"schema_compatibility": len(compatibility["missing"])},
            "schema_compatibility": compatibility,
        }

    rows = duck.read_query(
        """
        WITH raw AS (
            SELECT stock_code, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,
                   COUNT(*) AS observation_count,
                   COUNT(*) FILTER (WHERE volume IS NOT NULL AND volume > 0) AS volume_count
            FROM price_daily_raw WHERE close IS NOT NULL GROUP BY stock_code
        ), qfq AS (
            SELECT stock_code, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date,
                   COUNT(*) AS observation_count
            FROM price_daily_qfq WHERE close IS NOT NULL GROUP BY stock_code
        ), complete_financials AS (
            SELECT bs.stock_code, MAX(bs.report_date) AS report_date,
                   arg_max(bs.capital_adequacy_ratio, bs.report_date) AS capital_adequacy_ratio,
                   arg_max(bs.non_performing_loan_ratio, bs.report_date) AS non_performing_loan_ratio,
                   arg_max(bs.provision_coverage_ratio, bs.report_date) AS provision_coverage_ratio,
                   arg_max(bs.risk_coverage_ratio, bs.report_date) AS risk_coverage_ratio
            FROM balance_sheet bs
            JOIN income_statement ic
              ON ic.stock_code = bs.stock_code AND ic.report_date = bs.report_date
            JOIN cash_flow cf
              ON cf.stock_code = bs.stock_code AND cf.report_date = bs.report_date
            WHERE bs.total_assets IS NOT NULL
              AND bs.total_liabilities IS NOT NULL
              AND COALESCE(bs.total_equity_parent, bs.total_equity) IS NOT NULL
              AND ic.revenue IS NOT NULL
              AND ic.parent_net_profit IS NOT NULL
               AND cf.cf_from_operating IS NOT NULL
            GROUP BY bs.stock_code
        )
        SELECT m.stock_code,
            raw.last_date AS raw_last_date,
            qfq.last_date AS qfq_last_date,
            complete_financials.report_date AS financial_report_date,
            m.listing_date IS NOT NULL AND m.is_st IS NOT NULL AND m.is_suspended IS NOT NULL
                AS has_pool_metadata,
            raw.first_date IS NOT NULL
                AND raw.first_date <= GREATEST(m.listing_date, CURRENT_DATE - INTERVAL '5 years') + INTERVAL '30 days'
                AND raw.observation_count >= LEAST(?, GREATEST(1, CAST(CEIL(
                    date_diff('day', GREATEST(m.listing_date, CURRENT_DATE - INTERVAL '5 years'), CURRENT_DATE) * 0.67
                ) AS INTEGER)))
                AS has_raw_history,
            qfq.first_date IS NOT NULL
                AND qfq.first_date <= GREATEST(m.listing_date, CURRENT_DATE - INTERVAL '5 years') + INTERVAL '30 days'
                AND qfq.observation_count >= LEAST(?, GREATEST(1, CAST(CEIL(
                    date_diff('day', GREATEST(m.listing_date, CURRENT_DATE - INTERVAL '5 years'), CURRENT_DATE) * 0.67
                ) AS INTEGER)))
                AS has_qfq_history,
            raw.last_date >= CURRENT_DATE - INTERVAL '7 days'
                AND qfq.last_date >= CURRENT_DATE - INTERVAL '7 days'
                AS has_fresh_prices,
            raw.volume_count >= LEAST(?, GREATEST(1, CAST(CEIL(
                date_diff('day', GREATEST(m.listing_date, CURRENT_DATE - INTERVAL '5 years'), CURRENT_DATE) * 0.67
            ) AS INTEGER))) AS has_meaningful_volume,
            complete_financials.report_date >= CURRENT_DATE - INTERVAL '18 months'
                AS has_complete_financial_period,
            m.total_shares IS NOT NULL AND m.total_shares > 0
              AND m.circ_shares IS NOT NULL AND m.circ_shares > 0
              AND m.circ_shares <= m.total_shares AS has_share_capital,
            EXISTS (SELECT 1 FROM xdxr action WHERE action.stock_code = m.stock_code)
              AND EXISTS (
                SELECT 1 FROM dividends dividend
                WHERE dividend.stock_code = m.stock_code
                  AND dividend.announcement_date IS NOT NULL
              ) AS has_corporate_action_or_dividend_lineage,
            CASE
                WHEN lower(coalesce(m.sw_level1, '') || ' ' || coalesce(m.sw_level2, '')) LIKE '%银行%'
                THEN complete_financials.report_date IS NOT NULL AND complete_financials.capital_adequacy_ratio IS NOT NULL
                  AND complete_financials.non_performing_loan_ratio IS NOT NULL AND complete_financials.provision_coverage_ratio IS NOT NULL
                WHEN lower(coalesce(m.sw_level1, '') || ' ' || coalesce(m.sw_level2, '')) LIKE '%证券%'
                THEN complete_financials.report_date IS NOT NULL AND complete_financials.risk_coverage_ratio IS NOT NULL
                ELSE TRUE
            END AS has_sector_financials,
            EXISTS (
                SELECT 1 FROM indicator_snapshot snapshot
                WHERE snapshot.stock_code = m.stock_code
                  AND snapshot.report_date = complete_financials.report_date
                  AND snapshot.calculated_at IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM source_audit audit
                    WHERE audit.stock_code = m.stock_code
                      AND audit.report_date = complete_financials.report_date
                      AND audit.field_name IN (
                          'total_assets', 'total_liabilities', 'total_equity', 'total_equity_parent',
                          'revenue', 'parent_net_profit', 'cf_from_operating'
                      )
                       AND audit.fetch_time > snapshot.calculated_at
                   )
            ) AS has_current_snapshot_inputs,
            EXISTS (
                SELECT 1 FROM indicator_snapshot snapshot
                JOIN price_daily_raw current_raw
                  ON current_raw.stock_code = snapshot.stock_code
                 AND current_raw.trade_date = raw.last_date
                WHERE snapshot.stock_code = m.stock_code
                  AND snapshot.report_date = complete_financials.report_date
                  AND snapshot.latest_price_date = raw.last_date
                  AND snapshot.latest_close IS NOT NULL
                  AND current_raw.close IS NOT NULL
                  AND ABS(snapshot.latest_close - current_raw.close)
                      <= GREATEST(ABS(current_raw.close), 1.0) * 0.000001
            ) AS has_coherent_snapshot
        FROM stock_meta m
        LEFT JOIN raw ON raw.stock_code = m.stock_code
        LEFT JOIN qfq ON qfq.stock_code = m.stock_code
        LEFT JOIN complete_financials ON complete_financials.stock_code = m.stock_code
        WHERE m.is_listed IS TRUE
        ORDER BY m.stock_code
        """,
        [
            minimum_history_observations,
            minimum_history_observations,
            minimum_volume_observations,
        ],
    )
    issues: dict[str, list[str]] = {
        "pool_metadata": [], "raw_history": [], "qfq_history": [],
        "price_freshness": [], "meaningful_volume": [], "financial_period": [],
        "snapshot_price_coherence": [], "share_capital": [],
        "corporate_action_dividend_lineage": [],
        "sector_financials": [], "snapshot_input_freshness": [],
        "lineage_coverage": [],
    }
    for row in rows:
        for key, column in (
            ("pool_metadata", "has_pool_metadata"),
            ("raw_history", "has_raw_history"),
            ("qfq_history", "has_qfq_history"),
            ("price_freshness", "has_fresh_prices"),
            ("meaningful_volume", "has_meaningful_volume"),
            ("financial_period", "has_complete_financial_period"),
            ("share_capital", "has_share_capital"),
            ("corporate_action_dividend_lineage", "has_corporate_action_or_dividend_lineage"),
            ("sector_financials", "has_sector_financials"),
            ("snapshot_input_freshness", "has_current_snapshot_inputs"),
            ("snapshot_price_coherence", "has_coherent_snapshot"),
        ):
            if not row[column]:
                issues[key].append(row["stock_code"])
    issues["lineage_coverage"].extend(_missing_lineage_coverage(duck, rows))
    if sqlite is not None:
        stale_override_codes = _published_override_stale_snapshot_codes(duck, sqlite)
        issues["snapshot_input_freshness"].extend(stale_override_codes)
    for key, codes in issues.items():
        issues[key] = list(dict.fromkeys(codes))
    missing = {key: codes[:20] for key, codes in issues.items() if codes}
    return {
        "ready": bool(rows) and not missing,
        "stock_count": len(rows),
        "missing": missing,
        "missing_counts": {key: len(codes) for key, codes in issues.items() if codes},
        "schema_compatibility": compatibility,
    }


def _missing_lineage_coverage(duck: DuckDBStore, readiness_rows: list[dict]) -> list[str]:
    """Fail closed if materialized inputs or snapshots lost their source graph."""
    if not readiness_rows:
        return []
    codes = [row["stock_code"] for row in readiness_rows]
    slots = ", ".join("?" for _ in codes)
    rows = duck.read_query(
        f"""SELECT audit.stock_code, audit.report_date, audit.field_name, batch.data_type
             FROM source_audit audit
             JOIN fetch_batch batch ON batch.batch_id = audit.fetch_batch_id
             JOIN raw_response_archive raw ON raw.raw_response_hash = audit.raw_response_hash
             WHERE audit.stock_code IN ({slots}) AND octet_length(raw.payload) > 0
               AND (audit.field_name IN (
                   'latest_close', 'total_assets', 'total_liabilities', 'total_equity',
                   'total_equity_parent', 'revenue', 'parent_net_profit', 'cf_from_operating'
               ) OR batch.data_type = 'indicator_snapshot')""",
        codes,
    )
    evidence: dict[str, set[tuple[str, str, str]]] = {}
    for row in rows:
        evidence.setdefault(row["stock_code"], set()).add((
            str(row["report_date"])[:10], row["field_name"], row["data_type"],
        ))
    missing: list[str] = []
    financial_fields = {
        "total_assets", "total_liabilities", "revenue", "parent_net_profit", "cf_from_operating",
    }
    for stock in readiness_rows:
        code = stock["stock_code"]
        record = evidence.get(code, set())
        raw_date = str(stock.get("raw_last_date") or "")[:10]
        qfq_date = str(stock.get("qfq_last_date") or "")[:10]
        report_date = str(stock.get("financial_report_date") or "")[:10]
        prices_ok = {
            (raw_date, "latest_close", "price_daily_raw"),
            (qfq_date, "latest_close", "price_daily_qfq"),
        } <= record
        available_financial_fields = {
            field for date, field, data_type in record
            if date == report_date and data_type in {"balance_sheet", "income_statement", "cash_flow"}
        }
        financial_ok = financial_fields <= available_financial_fields and bool(
            {"total_equity", "total_equity_parent"} & available_financial_fields
        )
        snapshot_ok = any(
            date == report_date and data_type == "indicator_snapshot"
            for date, _field, data_type in record
        )
        if not prices_ok or not financial_ok or not snapshot_ok:
            missing.append(code)
    return missing


_ARCHIVE_HASH_CACHE_TTL_SECONDS = 60.0
_archive_hash_mismatch_cache: dict[str, tuple[float, int]] = {}


def _archive_hash_mismatch_rows(
    duck: DuckDBStore, *, force_refresh: bool = False
) -> tuple[int, bool]:
    """Count archived payloads failing SHA-256 verification, cached briefly.

    Returns (mismatch_rows, cache_hit). Hot read paths reuse the process-local
    result for a short TTL instead of rescanning every archived BLOB per
    request. Fail closed: an unreadable archive reports one mismatch so
    callers still surface LINEAGE_INVALID, and failures are never cached.
    """
    cache_key = str(duck.db_path)
    now = time.monotonic()
    if not force_refresh:
        cached = _archive_hash_mismatch_cache.get(cache_key)
        if cached is not None and now - cached[0] < _ARCHIVE_HASH_CACHE_TTL_SECONDS:
            return cached[1], True
    try:
        mismatches = sum(
            1
            for archive in duck.read_query(
                "SELECT raw_response_hash, payload FROM raw_response_archive WHERE payload IS NOT NULL"
            )
            if not isinstance(archive["payload"], bytes)
            or hashlib.sha256(archive["payload"]).hexdigest() != archive["raw_response_hash"]
        )
    except Exception:
        return 1, False
    _archive_hash_mismatch_cache[cache_key] = (now, mismatches)
    return mismatches, False


# Warnings that invalidate every value read from indicator_snapshot.
_SNAPSHOT_BLOCKING_WARNINGS = frozenset({
    "FINANCIAL_SHELL_ROWS",
    "SNAPSHOT_STALE",
    "LINEAGE_INVALID",
    "MINIMUM_DATA_NOT_READY",
    "CODE_IDENTITY_ALIAS",
    "LIVE_SCHEMA_INCOMPATIBLE",
})

# Warnings that invalidate only dividend-derived indicator fields.
_DIVIDEND_BLOCKING_WARNINGS = frozenset({"DIVIDEND_DATES_UNVERIFIED"})

DIVIDEND_INDICATOR_FIELDS = (
    "consecutive_div_years",
    "dividend_yield",
    "dps",
    "payout_ratio",
)


def indicator_trust(warning_codes: list[str]) -> dict:
    """Server-authoritative trust decision for snapshot-derived indicator values.

    Mirrors the frontend isIndicatorUntrusted policy: snapshot-dependent
    warnings invalidate every indicator field, dividend warnings invalidate
    only dividend fields, and operational warnings invalidate no numeric
    field. Read-only endpoints mask the affected values instead of serving
    them as normal research data.
    """
    codes = set(warning_codes)
    return {
        "warning_codes": list(warning_codes),
        "untrusted_all": bool(codes & _SNAPSHOT_BLOCKING_WARNINGS),
        "untrusted_fields": (
            sorted(DIVIDEND_INDICATOR_FIELDS)
            if codes & _DIVIDEND_BLOCKING_WARNINGS
            else []
        ),
    }


def mask_untrusted_values(values: dict, trust: dict) -> dict:
    """Null the snapshot-derived values the server cannot vouch for (fail closed)."""
    if trust["untrusted_all"]:
        return {key: None for key in values}
    untrusted = set(trust["untrusted_fields"])
    return {
        key: (None if key in untrusted else value)
        for key, value in values.items()
    }


_WARNING_CODES_CACHE_TTL_SECONDS = 30.0
_warning_codes_cache: dict[str, tuple[float, list[str]]] = {}


def read_warning_codes(duck: DuckDBStore, sqlite: SQLiteStore) -> list[str]:
    """Return quality warning codes for read-only display, cached briefly.

    The full status build re-verifies readiness across the whole pool; hot
    read paths (stock indicators, watchlist) reuse the process-local result
    for a short TTL instead of rebuilding it per request. Fail closed: a
    build failure reports LINEAGE_INVALID, and failures are never cached.
    """
    cache_key = f"{duck.db_path}|{sqlite.db_path}"
    now = time.monotonic()
    cached = _warning_codes_cache.get(cache_key)
    if cached is not None and now - cached[0] < _WARNING_CODES_CACHE_TTL_SECONDS:
        return list(cached[1])
    try:
        warning_codes = list(build_data_quality_status(duck, sqlite)["warning_codes"])
    except Exception:
        return ["LINEAGE_INVALID"]
    _warning_codes_cache[cache_key] = (now, warning_codes)
    return list(warning_codes)


def build_data_quality_status(
    duck: DuckDBStore, sqlite: SQLiteStore, *, force_archive_hash_recheck: bool = False
) -> dict:
    """Return machine-readable freshness, completeness, lineage, and operation warnings."""
    readiness = minimum_data_readiness(duck, sqlite)
    if not readiness["schema_compatibility"]["compatible"]:
        return _incompatible_schema_status(readiness, sqlite)

    dates = _data_dates(duck)
    dividends = duck.read_query(
        """
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (
                WHERE announcement_date IS NULL AND (
                    COALESCE(dividend_per_share, 0) != 0
                    OR COALESCE(stock_dividend, 0) != 0
                    OR COALESCE(transfer_share, 0) != 0
                    OR COALESCE(rights_issue, 0) != 0
                )
            ) AS active_missing_announcement_rows,
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
        WITH audits AS (
            SELECT
                COUNT(*) FILTER (WHERE LENGTH(s.raw_response_hash) != 64) AS invalid_hash_rows,
                COUNT(*) FILTER (WHERE f.batch_id IS NULL) AS orphan_batch_rows,
                COUNT(*) FILTER (
                    WHERE LENGTH(s.raw_response_hash) = 64 AND archive.raw_response_hash IS NULL
                ) AS audit_archive_gap_rows
                , COUNT(*) FILTER (
                    WHERE archive.raw_response_hash IS NOT NULL
                      AND (archive.payload IS NULL OR OCTET_LENGTH(archive.payload) = 0)
                ) AS empty_archive_payload_rows
            FROM source_audit s
            LEFT JOIN fetch_batch f ON s.fetch_batch_id = f.batch_id
            LEFT JOIN raw_response_archive archive ON s.raw_response_hash = archive.raw_response_hash
        ), batches AS (
            SELECT COUNT(*) AS batch_archive_gap_rows
            FROM fetch_batch batch
            LEFT JOIN raw_response_archive archive ON batch.raw_response_hash = archive.raw_response_hash
            WHERE archive.raw_response_hash IS NULL
        )
        SELECT audits.*, batches.batch_archive_gap_rows,
               audits.audit_archive_gap_rows + batches.batch_archive_gap_rows
                 + audits.empty_archive_payload_rows AS archive_gap_rows
        FROM audits CROSS JOIN batches
        """
    )[0]
    # Do not trust a mutable verification flag for an immutable source record.
    # A full payload rescan still runs on cache expiry or on an explicit
    # refresh request; the short process-local TTL only keeps hot read paths
    # from rescanning every archived BLOB on each request. Verification is
    # fail closed: an unreadable archive is reported as a mismatch so callers
    # still surface LINEAGE_INVALID.
    lineage["hash_mismatch_rows"], hash_check_cached = _archive_hash_mismatch_rows(
        duck, force_refresh=force_archive_hash_recheck
    )
    lineage["archive_hash_check"] = {
        "cached": hash_check_cached,
        "ttl_seconds": int(_ARCHIVE_HASH_CACHE_TTL_SECONDS),
    }
    code_identity = duck.read_query(
        """
        WITH listed_codes AS (
            SELECT stock_code,
                   regexp_replace(upper(trim(stock_code)), '^(SH|SZ|BJ)|\\.(SH|SS|SZ|BJ)$', '', 'g') AS normalized_code
            FROM stock_meta WHERE is_listed IS TRUE
        )
        SELECT COUNT(DISTINCT raw.stock_code) AS raw_alias_rows
        FROM price_daily_raw raw
        JOIN listed_codes meta
          ON regexp_replace(upper(trim(raw.stock_code)), '^(SH|SZ|BJ)|\\.(SH|SS|SZ|BJ)$', '', 'g') = meta.normalized_code
         AND raw.stock_code != meta.stock_code
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
    if _is_stale(dates["price"], dates["indicator_snapshot"]["latest_price_date"], days=7):
        warning_codes.append("SNAPSHOT_STALE")
    if dividends["active_missing_announcement_rows"]:
        warning_codes.append("DIVIDEND_DATES_UNVERIFIED")
    if (
        lineage["invalid_hash_rows"]
        or lineage["orphan_batch_rows"]
        or lineage["archive_gap_rows"]
        or lineage["hash_mismatch_rows"]
    ):
        warning_codes.append("LINEAGE_INVALID")
    if code_identity["raw_alias_rows"]:
        warning_codes.append("CODE_IDENTITY_ALIAS")
    if operations["unpublished_overrides"]:
        warning_codes.append("UNPUBLISHED_OVERRIDES")
    if operations["running_jobs"]:
        warning_codes.append("STALE_RUNNING_JOBS")
    if not readiness["ready"]:
        warning_codes.append("MINIMUM_DATA_NOT_READY")

    return {
        "minimum_data_readiness": readiness,
        "dates": dates,
        "dividends": dividends,
        "lineage": lineage,
        "code_identity": code_identity,
        "operations": operations,
        "warning_codes": warning_codes,
    }


def _live_schema_compatibility(duck: DuckDBStore) -> dict:
    """Report whether the live analytical schema can support the quality contract."""
    required = {
        "stock_meta": {"stock_code", "exchange", "listing_date", "is_listed", "is_st", "is_suspended"},
        "price_daily_raw": {"stock_code", "trade_date", "close"},
        "price_daily_qfq": {"stock_code", "trade_date", "close"},
        "balance_sheet": {"stock_code", "report_date", "total_assets", "total_liabilities", "total_equity", "total_equity_parent"},
        "income_statement": {"stock_code", "report_date", "revenue", "parent_net_profit"},
        "cash_flow": {"stock_code", "report_date", "cf_from_operating"},
        "indicator_snapshot": {"stock_code", "report_date", "latest_close", "latest_price_date", "calculated_at"},
        "dividends": {"stock_code", "announcement_date", "dividend_per_share", "stock_dividend", "transfer_share", "rights_issue"},
        "source_audit": {"stock_code", "fetch_batch_id", "raw_response_hash"},
        "fetch_batch": {"batch_id"},
        "raw_response_archive": {"raw_response_hash"},
    }
    rows = duck.read_query(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'main'"
    )
    available: dict[str, set[str]] = {}
    for row in rows:
        available.setdefault(row["table_name"], set()).add(row["column_name"])
    missing = [
        f"{table}.{column}"
        for table, columns in required.items()
        for column in sorted(columns - available.get(table, set()))
    ]
    return {"compatible": not missing, "missing": missing}


def _published_override_stale_snapshot_codes(
    duck: DuckDBStore, sqlite: SQLiteStore,
) -> list[str]:
    """Return stocks whose materialized snapshot predates a published correction."""
    overrides = sqlite.query(
        """SELECT DISTINCT stock_code FROM manual_overrides
           WHERE status = 'published' AND rolled_back_at IS NULL"""
    )
    if not overrides:
        return []
    codes = [row["stock_code"] for row in overrides]
    slots = ", ".join("?" for _ in codes)
    snapshots = duck.read_query(
        f"""SELECT stock_code, MAX(calculated_at) AS calculated_at
             FROM indicator_snapshot WHERE stock_code IN ({slots}) GROUP BY stock_code""",
        codes,
    )
    snapshot_by_code = {row["stock_code"]: row["calculated_at"] for row in snapshots}
    stale: list[str] = []
    for override in sqlite.query(
        """SELECT stock_code, MAX(created_at) AS published_at FROM manual_overrides
           WHERE status = 'published' AND rolled_back_at IS NULL GROUP BY stock_code"""
    ):
        calculated_at = snapshot_by_code.get(override["stock_code"])
        if calculated_at is None or _as_datetime(override["published_at"]) > _as_datetime(calculated_at):
            stale.append(override["stock_code"])
    return stale


def _as_datetime(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _incompatible_schema_status(readiness: dict, sqlite: SQLiteStore) -> dict:
    """Keep the status API truthful instead of querying a known-incompatible database."""
    operations = {
        "unpublished_overrides": sqlite.query(
            "SELECT COUNT(*) AS count FROM manual_overrides WHERE status != 'published' AND rolled_back_at IS NULL"
        )[0]["count"],
        "running_jobs": sqlite.query("SELECT COUNT(*) AS count FROM job_logs WHERE status = 'running'")[0]["count"],
    }
    return {
        "minimum_data_readiness": readiness,
        "dates": {
            "price": None,
            "balance_sheet": {"latest_record": None, "latest_complete": None},
            "income_statement": {"latest_record": None, "latest_complete": None},
            "cash_flow": {"latest_record": None, "latest_complete": None},
            "indicator_snapshot": {"latest_complete": None, "calculated_at": None, "latest_price_date": None},
        },
        "dividends": {"total_rows": 0, "active_missing_announcement_rows": 0, "unverified_period_end_rows": 0},
        "lineage": {
            "invalid_hash_rows": 0, "orphan_batch_rows": 0,
            "audit_archive_gap_rows": 0, "batch_archive_gap_rows": 0, "archive_gap_rows": 0,
        },
        "code_identity": {"raw_alias_rows": 0},
        "operations": operations,
        "warning_codes": ["LIVE_SCHEMA_INCOMPATIBLE", "MINIMUM_DATA_NOT_READY"],
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
                MAX(calculated_at) AS calculated_at,
                MAX(latest_price_date) AS latest_price_date
            FROM indicator_snapshot
            """
        )[0]
    else:
        snapshot = {
            "latest_complete": duck.read_query(
                "SELECT MAX(report_date) AS value FROM indicator_snapshot"
            )[0]["value"],
            "calculated_at": None,
            "latest_price_date": None,
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
