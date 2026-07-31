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
  readonly sw_level1: string | null
  readonly sw_level2: string | null
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

export interface KlineCandle {
  readonly trade_date: string
  readonly open: number
  readonly high: number
  readonly low: number
  readonly close: number
  readonly volume?: number
  readonly turnover?: number
}

export interface KlineResponse {
  readonly candles: readonly KlineCandle[]
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
