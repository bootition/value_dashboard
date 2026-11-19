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
/** 指标下拉的选项（2026-11-16 起支持分组）。 */
export type IndicatorOption =
  | { readonly label: string; readonly value: string }
  | {
      readonly type: 'group'
      readonly label: string
      readonly key: string
      readonly children: ReadonlyArray<{ readonly label: string; readonly value: string }>
    }

export interface ScreeningIndicator {
  readonly name: string
  readonly rankable: boolean
  /** 单位口径（pct|percent|price|ratio|plain），2026-08-14 红队 F3 后端下发 */
  readonly unit?: string
  /**
   * 派生字段的母指标（2026-11-16）。
   * 每个可排名指标会派生 6 条排名/分位字段，加上统计域还有 percentile/zscore，
   * ROCE 三个窗口因此膨胀成 21 条。带此标记的默认折叠。
   */
  /**
   * 该字段的**可算基准期**（reports/145）。
   * 只在基准期早于最新报告期时下发：季报不披露折旧摊销，EBITDA 族实际用的是
   * 最近一个半年报/年报的数（如 `basis_label = "2026 半年报"`），必须让用户看见。
   */
  readonly basis_report_date?: string
  readonly basis_label?: string
  readonly latest_report_date?: string
  readonly derived_from?: string
  readonly derived_kind?: 'rank' | 'stat'
  /**
   * 参数化字段规格（reports/143 §2）。
   * 例：`revenue_cagr` → {kind:'cagr', key:'cagr_years', label:'年数', options:[3,5]}
   * 库里只有 `revenue_cagr3/5` 这类带后缀的真实列，前端让用户选参数后由后端展开。
   */
  readonly param_spec?: {
    readonly kind: string
    readonly key: string
    readonly label: string
    readonly options: ReadonlyArray<string | number>
  }
}

/** Leaf condition in a screening rule tree. */
/** 条件级排名口径（2026-11-16 方案 C）。缺省 = 用原始值。 */
export type RankMode =
  | 'market_rank'
  | 'market_percentile'
  | 'industry_rank'
  | 'industry_percentile'
  | 'sw2_rank'
  | 'sw2_percentile'

/** 结果展示列：纯字段名，或「字段 + 排名口径」（方案 C，2026-11-16）。 */
export type ResultColumn = string | { field: string; rank_mode: RankMode }

/** 排序规则项：可选排名口径。 */
export interface ScreeningSortSpec {
  field: string
  direction: 'asc' | 'desc'
  rank_mode?: RankMode
  /** 参数化字段的取值，如 cagr_years: 5 或 tenor: '10y'。 */
  cagr_years?: number
  tenor?: string
}

export interface ScreeningRuleCondition {
  readonly id: string
  field: string
  op: string
  value: number | [number, number] | null
  right_field?: string
  /**
   * 排名口径：空 = 原始值；否则后端把 field 展开成 `{field}_{rank_mode}`。
   * 过去这些是 930 条独立的扁平字段（`pe_ttm_market_rank` …），
   * 现在收成条件上的一个下拉。
   */
  rank_mode?: RankMode
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
  // 2026-08-14 红队 P2-4：strict_only 空结果的原因反馈
  readonly strict_mode_warning?: string | null
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

/**
 * Deep-clone a rule tree/array before loading it into the editor.
 *
 * The editor mutates rule values in place. Loading a saved rule with a shallow
 * copy shares the nested condition objects with the saved-rules cache; the
 * cache then changes with the editor, so dirty detection can never see a
 * difference between the draft and the saved version.
 */
export function cloneScreeningRule<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}
