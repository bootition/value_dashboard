/**
 * Data-quality contract shared between backend and frontend.
 *
 * This module models the exact shape of:
 * - `app/core/data_quality.py::build_data_quality_status`
 * - `app/web/api/stock_detail.py::build_freshness_metadata`
 *
 * It also defines the trust policy: which warning codes invalidate which
 * indicator fields. The policy is minimal and deterministic.
 */

// ─────────────────────────────────────────────────────────────────────────────
// Warning codes
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Known warning codes emitted by the backend.
 *
 * The union is closed for autocomplete, but the `WarningCode` type below
 * accepts any string to preserve forward-compat at the API boundary.
 */
export type KnownWarningCode =
  | 'FINANCIAL_SHELL_ROWS'
  | 'SNAPSHOT_STALE'
  | 'DIVIDEND_DATES_UNVERIFIED'
  | 'LINEAGE_INVALID'
  | 'UNPUBLISHED_OVERRIDES'
  | 'STALE_RUNNING_JOBS'
  | 'MINIMUM_DATA_NOT_READY'
  | 'CODE_IDENTITY_ALIAS'
  | 'LIVE_SCHEMA_INCOMPATIBLE'

/**
 * Warning code type. Accepts known codes with autocomplete, but also accepts
 * any future string the backend might emit without weakening type safety.
 *
 * The `(string & {})` trick preserves autocomplete for `KnownWarningCode`
 * while allowing unknown strings at the API boundary.
 */
export type WarningCode = KnownWarningCode | (string & {})

/**
 * Readonly tuple of all known warning codes.
 *
 * Useful for validation, documentation, and exhaustive checks.
 */
export const KNOWN_WARNING_CODES = [
  'FINANCIAL_SHELL_ROWS',
  'SNAPSHOT_STALE',
  'DIVIDEND_DATES_UNVERIFIED',
  'LINEAGE_INVALID',
  'UNPUBLISHED_OVERRIDES',
  'STALE_RUNNING_JOBS',
  'MINIMUM_DATA_NOT_READY',
  'CODE_IDENTITY_ALIAS',
  'LIVE_SCHEMA_INCOMPATIBLE',
] as const

// ─────────────────────────────────────────────────────────────────────────────
// Indicator fields
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Indicator field name. Any string is accepted, but known fields are listed
 * for autocomplete and documentation.
 */
export type IndicatorField = string

/**
 * Dividend-related indicators. These are invalidated by DIVIDEND_DATES_UNVERIFIED.
 */
export const DIVIDEND_INDICATOR_FIELDS = [
  'dividend_yield',
  'payout_ratio',
  'dps',
  'consecutive_div_years',
] as const

/**
 * Sentinel representing all indicators that depend on indicator_snapshot.
 *
 * The '*' wildcard means "any indicator field". This avoids enumerating
 * speculative fields while providing a readable representation of the policy.
 */
export const SNAPSHOT_DEPENDENT_INDICATOR_FIELDS = ['*'] as const

// ─────────────────────────────────────────────────────────────────────────────
// Trust policy
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Maps each known warning code to the indicator fields it invalidates.
 *
 * - FINANCIAL_SHELL_ROWS, SNAPSHOT_STALE, LINEAGE_INVALID → ['*'] (all indicators)
 * - DIVIDEND_DATES_UNVERIFIED → dividend-specific indicators
 * - UNPUBLISHED_OVERRIDES, STALE_RUNNING_JOBS → [] (no numerical indicators)
 *
 * The '*' sentinel in a value array means "any indicator field". Unknown
 * warning codes are not in this map and are treated as not affecting any
 * field (forward-compat).
 */
const WARNING_TO_AFFECTED_FIELDS: Readonly<Record<KnownWarningCode, readonly string[]>> = {
  FINANCIAL_SHELL_ROWS: SNAPSHOT_DEPENDENT_INDICATOR_FIELDS,
  SNAPSHOT_STALE: SNAPSHOT_DEPENDENT_INDICATOR_FIELDS,
  DIVIDEND_DATES_UNVERIFIED: DIVIDEND_INDICATOR_FIELDS,
  LINEAGE_INVALID: SNAPSHOT_DEPENDENT_INDICATOR_FIELDS,
  UNPUBLISHED_OVERRIDES: [],
  STALE_RUNNING_JOBS: [],
  MINIMUM_DATA_NOT_READY: SNAPSHOT_DEPENDENT_INDICATOR_FIELDS,
  CODE_IDENTITY_ALIAS: SNAPSHOT_DEPENDENT_INDICATOR_FIELDS,
  LIVE_SCHEMA_INCOMPATIBLE: SNAPSHOT_DEPENDENT_INDICATOR_FIELDS,
}

/**
 * Runtime type guard that narrows a string to KnownWarningCode.
 *
 * Used to safely look up warning codes in the trust policy map without
 * type assertions. Unknown/future codes return false and are ignored.
 */
function isKnownWarningCode(code: string): code is KnownWarningCode {
  for (const known of KNOWN_WARNING_CODES) {
    if (known === code) return true
  }
  return false
}

/**
 * Returns true if the given indicator field is untrusted due to any of the
 * provided warning codes.
 *
 * Deterministic: same inputs → same output. No side effects.
 *
 * @param field - The indicator field name (e.g. 'pe_ratio', 'dividend_yield')
 * @param warningCodes - Array of warning codes from the backend
 * @returns true if the field is untrusted, false otherwise
 *
 * @example
 * isIndicatorUntrusted('dividend_yield', ['DIVIDEND_DATES_UNVERIFIED']) // true
 * isIndicatorUntrusted('pe_ratio', ['DIVIDEND_DATES_UNVERIFIED']) // false
 * isIndicatorUntrusted('pe_ratio', ['LINEAGE_INVALID']) // true
 * isIndicatorUntrusted('pe_ratio', ['SOME_FUTURE_WARNING']) // false
 */
export function isIndicatorUntrusted(
  field: IndicatorField,
  warningCodes: readonly WarningCode[],
): boolean {
  for (const code of warningCodes) {
    // Only act on known codes. Unknown codes are ignored (forward-compat).
    if (!isKnownWarningCode(code)) continue
    const affectedFields = WARNING_TO_AFFECTED_FIELDS[code]

    // '*' means all indicators are affected
    if (affectedFields.includes('*')) return true

    // Check if the specific field is in the affected list
    if (affectedFields.includes(field)) return true
  }
  return false
}

// ─────────────────────────────────────────────────────────────────────────────
// Backend contract: DataQualityStatus
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Date range for a financial statement table.
 *
 * - latest_record: MAX(report_date) — the most recent row, even if incomplete
 * - latest_complete: MAX(report_date) WHERE key fields are non-null
 */
export interface FinancialStatementDates {
  readonly latest_record: string | null
  readonly latest_complete: string | null
}

/**
 * Date range for indicator_snapshot table.
 *
 * - latest_complete: MAX(report_date)
 * - calculated_at: MAX(calculated_at) — when the snapshot was computed
 */
export interface IndicatorSnapshotDates {
  readonly latest_complete: string | null
  readonly calculated_at: string | null
  readonly latest_price_date: string | null
}

/**
 * All date-related freshness signals from the backend.
 */
export interface DataDates {
  readonly price: string | null
  readonly balance_sheet: FinancialStatementDates
  readonly income_statement: FinancialStatementDates
  readonly cash_flow: FinancialStatementDates
  readonly indicator_snapshot: IndicatorSnapshotDates
}

/**
 * Dividend data quality signals.
 *
 * - total_rows: COUNT(*) FROM dividends
 * - unverified_period_end_rows: rows where announcement_date IS NULL and
 *   ex_date is a period-end (12-31 or 6-30)
 */
export interface DividendQuality {
  readonly total_rows: number
  readonly active_missing_announcement_rows: number
  readonly unverified_period_end_rows: number
}

/**
 * Lineage quality signals.
 *
 * - invalid_hash_rows: rows where LENGTH(raw_response_hash) != 64
 * - orphan_batch_rows: rows where batch_id IS NULL
 */
export interface LineageQuality {
  readonly invalid_hash_rows: number
  readonly orphan_batch_rows: number
  readonly audit_archive_gap_rows: number
  readonly batch_archive_gap_rows: number
  readonly archive_gap_rows: number
}

export interface CodeIdentityQuality {
  readonly raw_alias_rows: number
}

/**
 * Operational warnings.
 *
 * - unpublished_overrides: manual overrides not yet published
 * - running_jobs: jobs currently running (stale if count > 0 for too long)
 */
export interface OperationalWarnings {
  readonly unpublished_overrides: number
  readonly running_jobs: number
}

/**
 * Full data-quality status from backend `build_data_quality_status`.
 *
 * This is the exact shape returned by the API. All fields are readonly.
 */
export interface DataQualityStatus {
  readonly dates: DataDates
  readonly minimum_data_readiness: MinimumDataReadiness
  readonly dividends: DividendQuality
  readonly lineage: LineageQuality
  readonly code_identity: CodeIdentityQuality
  readonly operations: OperationalWarnings
  readonly warning_codes: readonly WarningCode[]
}

export interface MinimumDataReadiness {
  readonly ready: boolean
  readonly checking?: boolean
  readonly cached?: boolean
  readonly checked_at?: string
  readonly stock_count: number
  readonly missing: Readonly<Record<string, readonly string[]>>
  readonly missing_counts: Readonly<Record<string, number>>
  readonly schema_compatibility: {
    readonly compatible: boolean
    readonly missing: readonly string[]
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Backend contract: IndicatorTrust
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Server-authoritative trust signal embedded in read-only indicator responses.
 *
 * From backend `app/core/data_quality.py::indicator_trust`. When
 * `untrusted_all` is true, every snapshot-derived numeric field in the same
 * response has been masked to null by the server; `untrusted_fields` lists
 * the explicitly invalidated fields otherwise (e.g. dividend indicators
 * under DIVIDEND_DATES_UNVERIFIED).
 */
export interface IndicatorTrust {
  readonly warning_codes: readonly WarningCode[]
  readonly untrusted_all: boolean
  readonly untrusted_fields: readonly string[]
}

// ─────────────────────────────────────────────────────────────────────────────
// Backend contract: StockFreshness
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Freshness metadata for a single stock's indicators.
 *
 * From backend `build_freshness_metadata`.
 *
 * - financial_effective_date: date of the financial data used
 * - price_date: date of the price data used
 * - calculated_at: when the indicators were computed
 * - data_version: version tag for the data pipeline
 * - stale_days: days between price_date and financial_effective_date
 * - stale_warning: true if stale_days is null or > 365
 */
export interface StockFreshness {
  readonly financial_effective_date: string | null
  readonly price_date: string | null
  readonly calculated_at: string | null
  readonly data_version: string | null
  readonly stale_days: number | null
  readonly stale_warning: boolean
  readonly price_age_days?: number | null
  readonly financial_age_days?: number | null
  readonly snapshot_age_days?: number | null
}
