/**
 * View-facing response contracts for StockDetailPage.
 *
 * These readonly interfaces model the fields consumed by StockDetailPage.vue
 * and its planned extracted components. Backend responses may contain
 * additional fields not included in these contracts.
 *
 * Source endpoints:
 * - GET /api/stock/{code}/info
 * - GET /api/stock/{code}/indicators
 * - GET /api/stock/{code}/kline
 * - GET /api/stock/{code}/financial-trend
 * - GET /api/stock/{code}/source-audit
 */

import type { IndicatorTrust, StockFreshness } from './data-quality.ts'

// ─── Stock Info ───────────────────────────────────────────────────────────

export interface StockInfo {
  readonly stock_code: string
  readonly name: string
  readonly pinyin: string
  readonly exchange: string
  readonly listing_date: string | null
  readonly is_st: boolean
  readonly is_suspended: boolean
  readonly csrc_l1: string | null
  readonly csrc_l2: string | null
  readonly latest_close: number | null
  readonly latest_price_date: string | null
}

// ─── Indicators ───────────────────────────────────────────────────────────

export interface IndicatorMetric {
  readonly value: number | null
  readonly historical_capable: boolean
  readonly untrusted?: boolean
}

export interface ValuationIndicators {
  readonly pe_ttm: IndicatorMetric
  readonly pb_mrq: IndicatorMetric
  readonly ps_ttm: IndicatorMetric
  readonly pcf_ttm: IndicatorMetric
  readonly dividend_yield: IndicatorMetric
  readonly total_market_cap: IndicatorMetric
  readonly circ_market_cap: IndicatorMetric
}

export interface ProfitabilityIndicators {
  readonly roe: IndicatorMetric
  readonly roa: IndicatorMetric
  readonly gross_margin: IndicatorMetric
  readonly net_margin: IndicatorMetric
  readonly roic: IndicatorMetric
  readonly cf_to_net_profit: IndicatorMetric
}

export interface GrowthIndicators {
  readonly revenue_yoy: IndicatorMetric
  readonly net_profit_yoy: IndicatorMetric
  readonly deducted_profit_yoy: IndicatorMetric
  readonly revenue_cagr3: IndicatorMetric
  readonly revenue_cagr5: IndicatorMetric
  readonly net_profit_cagr3: IndicatorMetric
  readonly net_profit_cagr5: IndicatorMetric
  readonly deducted_profit_cagr3: IndicatorMetric
  readonly deducted_profit_cagr5: IndicatorMetric
}

export interface SafetyIndicators {
  readonly debt_ratio: IndicatorMetric
  readonly current_ratio: IndicatorMetric
  readonly quick_ratio: IndicatorMetric
  readonly interest_bearing_debt: IndicatorMetric
  readonly interest_coverage: IndicatorMetric
  readonly goodwill_ratio: IndicatorMetric
}

export interface ShareholderReturnIndicators {
  readonly payout_ratio: IndicatorMetric
  readonly dps: IndicatorMetric
  readonly consecutive_div_years: IndicatorMetric
}

export interface IndicatorsPayload {
  readonly valuation: ValuationIndicators
  readonly profitability: ProfitabilityIndicators
  readonly growth: GrowthIndicators
  readonly safety: SafetyIndicators
  readonly shareholder_return: ShareholderReturnIndicators
}

export interface IndicatorsResponse {
  readonly indicators: IndicatorsPayload
  readonly report_date: string | null
  readonly latest_close: number | null
  readonly latest_price_date: string | null
  readonly freshness: StockFreshness | null
  readonly trust?: IndicatorTrust
}

// ─── Kline ────────────────────────────────────────────────────────────────

/**
 * K 线周期。后端对 day 直读日线，对 week/month 服务端读时聚合日线。
 */
export type KlinePeriod = 'day' | 'week' | 'month'

export interface KlineCandle {
  readonly trade_date: string
  readonly open: number
  readonly high: number
  readonly low: number
  readonly close: number
  readonly volume?: number
  readonly turnover?: number
  readonly turnover_rate?: number | null
  readonly ma5?: number | null
  readonly ma10?: number | null
  readonly ma20?: number | null
  readonly ma60?: number | null
  readonly ma120?: number | null
  readonly ma250?: number | null
}

export interface KlineResponse {
  readonly candles: readonly KlineCandle[]
  readonly adjust?: 'raw' | 'qfq'
  readonly period?: KlinePeriod
  readonly count?: number
}

// ─── Financial Trend ──────────────────────────────────────────────────────

export interface FinancialTrendRow {
  readonly report_date: string
  readonly revenue: number | null
  readonly net_profit: number | null
  readonly parent_net_profit?: number | null
  readonly deducted_net_profit: number | null
  readonly basic_eps: number | null
  readonly cf_from_operating: number | null
  readonly gross_margin: number | null
  readonly net_margin: number | null
  readonly roe: number | null
  readonly debt_ratio: number | null
}

export interface TrendResponse {
  readonly trend: readonly FinancialTrendRow[]
  readonly period: 'annual' | 'quarterly' | 'ttm'
  readonly count: number
  readonly period_semantic?: string
}

// ─── Business Overview ────────────────────────────────────────────────────

/**
 * 业务概览溯源信息（来源 / 抓取时间 / 置信度 / 批次）。
 * 语义来自 reports/67：东财 F10 低频主源，confidence 为 "approximate"。
 */
export interface BusinessOverviewProvenance {
  readonly source: string | null
  readonly fetch_time: string | null
  readonly raw_hash?: string | null
  readonly confidence?: string | null
  readonly batch_id?: string | null
}

/** 公司资料（company_profile）。status 为 "missing" 时不含业务字段。 */
export interface CompanyProfile {
  readonly status: 'ok' | 'missing'
  readonly code?: string | null
  readonly name?: string | null
  readonly org_name?: string | null
  readonly profile?: string | null
  readonly scope?: string | null
  readonly employee_num?: number | null
  readonly csrc_industry?: string | null
  readonly trade_market?: string | null
  readonly provenance?: BusinessOverviewProvenance | null
}

/** 主营构成条目（business_breakdown）。type: 1=产品 2=行业 3=地区。 */
export interface BreakdownItem {
  readonly report_date: string
  readonly type: number
  readonly type_label: string
  readonly item_name: string | null
  readonly amount: number | null
  readonly ratio: number | null
  readonly rank: number | null
}

/** 主营构成：最近报告期 composition（按 type 分组）+ 可得历史。 */
export interface BusinessBreakdown {
  readonly status: 'ok' | 'missing'
  readonly latest_report_date: string | null
  readonly composition: Readonly<Record<string, readonly BreakdownItem[]>>
  readonly history: readonly BreakdownItem[]
  readonly provenance: BusinessOverviewProvenance | null
}

export interface BusinessOverviewResponse {
  readonly stock_code: string
  readonly profile: CompanyProfile
  readonly breakdown: BusinessBreakdown
  readonly provenance: {
    readonly profile: BusinessOverviewProvenance | null
    readonly breakdown: BusinessOverviewProvenance | null
  }
}

// ─── Source Audit ─────────────────────────────────────────────────────────

export interface AuditFieldRow {
  readonly field_name: string
  readonly report_date: string
  readonly value: number | null
  readonly source: string
  readonly confidence: string
  readonly fetch_time: string
  readonly reason_code?: string | null
  readonly is_override?: boolean
  readonly api_version?: string | null
  readonly effective_date?: string | null
  readonly data_version?: string | null
  readonly formula?: string | null
  readonly as_reported_value?: number | null
  readonly latest_restated_diff?: number | null
}

export interface AuditBatchRow {
  readonly batch_id?: string
  readonly data_type: string
  readonly source: string
  readonly row_count: number
  readonly confidence: string
  readonly fetch_time: string
}

export interface AuditResponse {
  readonly field_audit: readonly AuditFieldRow[]
  readonly batch_audit: readonly AuditBatchRow[]
}

// ─── Treasury Comparison（P3，reports/68）─────────────────────────────────

export interface TreasurySeriesPoint {
  readonly price_date: string
  readonly ttm_div_yield: number | null
  readonly curve_yield: number | null
  readonly spread: number | null
  readonly curve_date: string | null
  readonly staleness_days: number | null
  readonly reason: string | null
}

export interface TreasuryComparisonResponse {
  readonly stock_code: string
  readonly tenor: number
  readonly tenors_available: readonly number[]
  readonly max_staleness_days: number
  readonly series: readonly TreasurySeriesPoint[]
  readonly missing: boolean
  readonly provenance: {
    readonly source: string
    readonly fetch_time: string | null
    readonly raw_hash: string
    readonly batch_id: string
    readonly confidence: string
  } | null
}

// ─── Research Statistics（P4，reports/68 §5）───────────────────────────────

export interface ResearchSeriesPoint {
  readonly price_date: string
  readonly close: number
  readonly pe_ttm: number | null
  readonly pb_mrq: number | null
  readonly ttm_dividend_yield: number | null
  readonly spread_10y: number | null
}

export interface WindowStatistics {
  readonly samples?: number | null
  readonly min_date?: string | null
  readonly max_date?: string | null
  readonly p10?: number | null
  readonly p20?: number | null
  readonly p50?: number | null
  readonly p80?: number | null
  readonly max?: number | null
  readonly mean?: number | null
  readonly sigma?: number | null
  readonly zscore?: number | null
  readonly current?: number | null
  readonly coverage_pct?: number | null
  readonly reason?: string | null
}

export interface ResearchStatisticsResponse {
  readonly stock_code: string
  readonly metric: string
  readonly window_years: number
  readonly series: readonly ResearchSeriesPoint[]
  readonly statistics: Record<string, WindowStatistics>
  readonly coverage_threshold_pct: number
  readonly disclaimer: string
}
