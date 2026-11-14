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
  // 2026-11-14 资产健康检查（reports/119）：`app/core/quality/asset_health.py`
  // 的 12 条规则。全部是**披露型**：默认不进任何阻断集合（不遮蔽指标、
  // 不让筛选 409），P1 只出现在 warning_codes 供状态页展示。
  // 升级为阻断型必须另开裁决（reports/119 §5）。
  | 'EXT_PERIOD_MISMATCH'
  | 'FIELD_COVERAGE_LOW'
  | 'FIELD_COVERAGE_DROP'
  | 'MANUAL_CHAIN_STALE'
  | 'SNAPSHOT_PERIOD_GAP'
  | 'STATIC_TABLE_STALE'
  | 'PACKAGE_STALE'
  | 'NULL_REASON_MISSING'
  | 'CROSSCHECK_BELOW_THRESHOLD'
  | 'SCHEMA_DDL_DRIFT'
  | 'ZERO_REFERENCE_ASSET'
  | 'DEDUCTED_PROFIT_GAP'

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
  // 资产健康（reports/119）
  'EXT_PERIOD_MISMATCH',
  'FIELD_COVERAGE_LOW',
  'FIELD_COVERAGE_DROP',
  'MANUAL_CHAIN_STALE',
  'SNAPSHOT_PERIOD_GAP',
  'STATIC_TABLE_STALE',
  'PACKAGE_STALE',
  'NULL_REASON_MISSING',
  'CROSSCHECK_BELOW_THRESHOLD',
  'SCHEMA_DDL_DRIFT',
  'ZERO_REFERENCE_ASSET',
  'DEDUCTED_PROFIT_GAP',
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
  'cumulative_dividend_amount',
  'dividend_financing_ratio_pct',
  'ttm_dividend_yield',
  'div_yield_spread_0p25y',
  'div_yield_spread_0p5y',
  'div_yield_spread_1y',
  'div_yield_spread_2y',
  'div_yield_spread_3y',
  'div_yield_spread_5y',
  'div_yield_spread_7y',
  'div_yield_spread_10y',
  'div_yield_spread_30y',
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
  // 2026-08-27：MINIMUM_DATA_NOT_READY 只代表少数股票的数据缺口，
  // 不应让全部快照指标不可信；引擎会排除这些缺口股，其余股票可正常使用。
  MINIMUM_DATA_NOT_READY: [],
  CODE_IDENTITY_ALIAS: SNAPSHOT_DEPENDENT_INDICATOR_FIELDS,
  LIVE_SCHEMA_INCOMPATIBLE: SNAPSHOT_DEPENDENT_INDICATOR_FIELDS,
  // 2026-11-14 资产健康 12 条规则：**一律不遮蔽任何数值**。
  // 后端同样没有把它们放进 `_SNAPSHOT_BLOCKING_WARNINGS` /
  // `_SCREENING_BLOCKING_WARNINGS`：它们是"数据资产该维护了"的披露，
  // 不是"这些数字不可信"。前后端口径必须一致，否则会出现
  // 页面一片"—"而 API 仍返回数值的自相矛盾。
  EXT_PERIOD_MISMATCH: [],
  FIELD_COVERAGE_LOW: [],
  FIELD_COVERAGE_DROP: [],
  MANUAL_CHAIN_STALE: [],
  SNAPSHOT_PERIOD_GAP: [],
  STATIC_TABLE_STALE: [],
  PACKAGE_STALE: [],
  NULL_REASON_MISSING: [],
  CROSSCHECK_BELOW_THRESHOLD: [],
  SCHEMA_DDL_DRIFT: [],
  ZERO_REFERENCE_ASSET: [],
  DEDUCTED_PROFIT_GAP: [],
}

// ─────────────────────────────────────────────────────────────────────────────
// 中文文案
// ─────────────────────────────────────────────────────────────────────────────

/**
 * 每个 warning code 的中文文案（状态页/结果面板直接渲染，不再显示裸英文码）。
 *
 * 文案原则：先说"哪里不对"，再说"会有什么后果"。用户看不懂 code 不是他的问题。
 * 资产健康 12 条（reports/119）与后端 `asset_health.RULE_CODES` 一一对应；
 * 新增 code 时两边必须同时改，否则 Record 的穷尽性检查会直接编译失败。
 */
export const WARNING_CODE_LABELS: Readonly<Record<KnownWarningCode, string>> = {
  FINANCIAL_SHELL_ROWS: '财务三表存在只有空壳的报告期',
  SNAPSHOT_STALE: '指标快照落后于最新行情',
  DIVIDEND_DATES_UNVERIFIED: '部分分红记录缺少公告日（除权日待核验）',
  LINEAGE_INVALID: '数据溯源链断裂（原始响应归档缺失或摘要不匹配）',
  UNPUBLISHED_OVERRIDES: '有手工覆盖值尚未发布',
  STALE_RUNNING_JOBS: '有超过 2 小时未结束的任务',
  MINIMUM_DATA_NOT_READY: '部分股票未达最小可用数据门槛',
  CODE_IDENTITY_ALIAS: '行情表存在股票代码别名（同一只股票两种写法）',
  LIVE_SCHEMA_INCOMPATIBLE: '库结构与当前代码要求的 schema 不兼容',
  EXT_PERIOD_MISMATCH: '扩展指标的报告期与界面快照不一致',
  FIELD_COVERAGE_LOW: '界面字段最新报告期覆盖率过低',
  FIELD_COVERAGE_DROP: '界面字段覆盖率较上一期断崖下跌',
  MANUAL_CHAIN_STALE: '手工派生链长时间未运行',
  SNAPSHOT_PERIOD_GAP: '部分股票的快照报告期不是最新完整三表期',
  STATIC_TABLE_STALE: '静态低频表落后应有报告期',
  PACKAGE_STALE: '导出的数据包落后于库内行情',
  NULL_REASON_MISSING: '空值未登记原因（分不清"没有"和"坏了"）',
  CROSSCHECK_BELOW_THRESHOLD: '自算指标与归档域交叉核验一致率偏低',
  SCHEMA_DDL_DRIFT: 'schema.py 声明的表与库内实际表不一致',
  ZERO_REFERENCE_ASSET: '零引用资产（导入了但没人读）',
  DEDUCTED_PROFIT_GAP: '扣非净利润最新报告期覆盖率塌陷',
}

/**
 * 取中文文案；未知 code 返回原文（前向兼容：新后端码不会显示成空白）。
 */
export function warningCodeLabel(code: WarningCode): string {
  return isKnownWarningCode(code) ? WARNING_CODE_LABELS[code] : String(code)
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
