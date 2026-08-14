/**
 * Screening-page view contracts.
 *
 * These types model the exact shapes exchanged between ScreeningPage and
 * the backend endpoints under /api/screening/* and /api/data-status/summary.
 *
 * They are intentionally narrow (view-facing) and do not duplicate the
 * domain contracts in data-quality.ts.
 */
import type { DataQualityStatus, WarningCode } from './data-quality.ts'

/** Indicator entry from GET /api/screening/indicators. */
export interface ScreeningIndicator {
  readonly name: string
  readonly rankable: boolean
  /** 单位口径（pct|percent|price|ratio|plain），2026-08-14 红队 F3 后端下发 */
  readonly unit?: string
}

/** Leaf condition in a screening rule tree. */
export interface ScreeningRuleCondition {
  readonly id: string
  field: string
  op: string
  value: number | [number, number] | null
  right_field?: string
}

/** Recursive AND/OR group in a screening rule tree. */
export interface ScreeningRuleNode {
  readonly id: string
  logic: 'AND' | 'OR'
  rules: Array<ScreeningRuleNode | ScreeningRuleCondition>
}

/** Generate a unique ID for rule conditions and nodes. */
export function generateRuleId(): string {
  return crypto.randomUUID()
}

/** Known fields in a screening result row (from the backend columns list). */
export interface ScreeningResultKnownFields {
  readonly stock_code: string
  readonly name: string
  readonly exchange: string
  readonly csrc_l1: string
  readonly latest_close: number
  readonly pe_ttm: number | null
  readonly pb_mrq: number | null
  readonly roe: number | null
  readonly gross_margin: number | null
  readonly net_margin: number | null
  readonly debt_ratio: number | null
  readonly revenue_yoy: number | null
  readonly dividend_yield: number | null
  readonly _entry_explanation?: string
}

/** A single screening result row. Known fields are typed; dynamic columns are also allowed. */
export type ScreeningResult = Readonly<ScreeningResultKnownFields> &
  Readonly<Record<string, string | number | null | undefined>>

/** Response from POST /api/screening/run. */
export interface ScreeningRunResponse {
  readonly run_id: string
  readonly results: ReadonlyArray<ScreeningResult>
  readonly total: number
  readonly truncated?: boolean
  readonly execution_time_ms: number
  readonly base_pool_size: number
  readonly data_date: string | null
  readonly auto_update_in_progress?: boolean
  readonly data_as_of?: string | null
}

/** Response from GET /api/data-status/summary (only the quality slice we need). */
export interface DataStatusSummaryResponse {
  readonly data_quality: DataQualityStatus
  readonly minimum_data_readiness?: DataQualityStatus['minimum_data_readiness']
}

/** Re-export for convenience. */
export type { WarningCode }

/** Response from POST /api/screening/save. */
export interface ScreeningSaveResponse {
  readonly id: number
  readonly title: string
}

/** Response from POST /api/screening/export_csv. */
export interface ScreeningExportResponse {
  readonly csv: string
  readonly rows: number
}

/** Response from POST /api/screening/add_to_watchlist. */
export interface ScreeningWatchlistResponse {
  readonly added: number
  readonly duplicates: number
  readonly errors: number
}

/** Type guard: is the given rule item a nested group (vs a leaf condition)? */
export function isRuleNode(
  item: ScreeningRuleNode | ScreeningRuleCondition,
): item is ScreeningRuleNode {
  return 'logic' in item && (item.logic === 'AND' || item.logic === 'OR')
}
