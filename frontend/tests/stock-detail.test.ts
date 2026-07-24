/**
 * Stock-detail API contract and formatter tests.
 *
 * Compile-time fixtures prove the readonly interfaces accept the exact
 * shape the backend returns. Runtime tests pin the visible formatting
 * helpers (fmt / fmtPct) that the current StockDetailPage consumes.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { fmt, fmtPct } from '../src/utils/formatters.ts'
import type {
  StockInfo,
  IndicatorMetric,
  ValuationIndicators,
  ProfitabilityIndicators,
  GrowthIndicators,
  SafetyIndicators,
  ShareholderReturnIndicators,
  IndicatorsPayload,
  IndicatorsResponse,
  KlineCandle,
  KlineResponse,
  FinancialTrendRow,
  TrendResponse,
  AuditFieldRow,
  AuditBatchRow,
  AuditResponse,
} from '../src/types/stock-detail.ts'

// ─── Compile-time shape fixtures ──────────────────────────────────────────

const stockInfoFixture: StockInfo = {
  stock_code: '600519',
  name: '贵州茅台',
  pinyin: 'gzmt',
  exchange: 'SSE',
  listing_date: '2001-08-27',
  is_st: false,
  is_suspended: false,
  sw_level1: '食品饮料',
  sw_level2: '白酒',
  latest_close: 1680.5,
  latest_price_date: '2025-01-20',
}

const metricFixture: IndicatorMetric = {
  value: 25.4,
  historical_capable: false,
}

const valuationFixture: ValuationIndicators = {
  pe_ttm: metricFixture,
  pb_mrq: { value: 8.2, historical_capable: false },
  ps_ttm: { value: 12.1, historical_capable: false },
  pcf_ttm: { value: null, historical_capable: false },
  dividend_yield: { value: 0.023, historical_capable: false },
  total_market_cap: { value: 2.1e12, historical_capable: false },
  circ_market_cap: { value: 2.0e12, historical_capable: false },
}

const profitabilityFixture: ProfitabilityIndicators = {
  roe: { value: 0.32, historical_capable: true },
  roa: { value: 0.25, historical_capable: true },
  gross_margin: { value: 0.91, historical_capable: true },
  net_margin: { value: 0.52, historical_capable: true },
  roic: { value: 0.30, historical_capable: true },
  cf_to_net_profit: { value: 1.1, historical_capable: true },
}

const growthFixture: GrowthIndicators = {
  revenue_yoy: { value: 0.15, historical_capable: true },
  net_profit_yoy: { value: 0.18, historical_capable: true },
  deducted_profit_yoy: { value: 0.16, historical_capable: true },
  revenue_cagr3: { value: 0.14, historical_capable: true },
  revenue_cagr5: { value: 0.13, historical_capable: true },
  net_profit_cagr3: { value: 0.17, historical_capable: true },
  net_profit_cagr5: { value: 0.16, historical_capable: true },
}

const safetyFixture: SafetyIndicators = {
  debt_ratio: { value: 0.22, historical_capable: true },
  current_ratio: { value: 3.5, historical_capable: true },
  quick_ratio: { value: 2.8, historical_capable: true },
  interest_bearing_debt: { value: 0, historical_capable: true },
  interest_coverage: { value: 150, historical_capable: true },
  goodwill_ratio: { value: 0, historical_capable: true },
}

const shareholderReturnFixture: ShareholderReturnIndicators = {
  payout_ratio: { value: 0.55, historical_capable: true },
  dps: { value: 25.0, historical_capable: true },
  consecutive_div_years: { value: 20, historical_capable: true },
}

const indicatorsPayloadFixture: IndicatorsPayload = {
  valuation: valuationFixture,
  profitability: profitabilityFixture,
  growth: growthFixture,
  safety: safetyFixture,
  shareholder_return: shareholderReturnFixture,
}

const indicatorsResponseFixture: IndicatorsResponse = {
  indicators: indicatorsPayloadFixture,
  report_date: '2024-09-30',
  latest_close: 1680.5,
  latest_price_date: '2025-01-20',
  freshness: {
    financial_effective_date: '2024-09-30',
    price_date: '2025-01-20',
    calculated_at: '2025-01-20T10:00:00',
    data_version: 'v1',
    stale_days: 112,
    stale_warning: false,
  },
}

const klineCandleFixture: KlineCandle = {
  trade_date: '2025-01-20',
  open: 1650.0,
  high: 1690.0,
  low: 1645.0,
  close: 1680.5,
  volume: 12345678,
  turnover: 2.1e9,
}

const klineResponseFixture: KlineResponse = {
  candles: [klineCandleFixture],
}

const trendRowFixture: FinancialTrendRow = {
  report_date: '2024-12-31',
  revenue: 1.5e10,
  net_profit: 8.0e9,
  parent_net_profit: 7.8e9,
  deducted_net_profit: 7.7e9,
  basic_eps: 62.0,
  cf_from_operating: 9.0e9,
  gross_margin: 0.92,
  net_margin: 0.52,
  debt_ratio: 0.22,
  roe: 0.40,
}

const trendResponseFixture: TrendResponse = {
  trend: [trendRowFixture],
  period: 'annual',
  count: 1,
}

const auditFieldRowFixture: AuditFieldRow = {
  field_name: 'pe_ttm',
  report_date: '2024-09-30',
  value: 25.4,
  source: 'indicator_snapshot',
  confidence: 'strict',
  fetch_time: '2025-01-20T10:00:00',
}

const auditBatchRowFixture: AuditBatchRow = {
  data_type: 'income_statement',
  source: 'baostock',
  row_count: 100,
  confidence: 'strict',
  fetch_time: '2025-01-20T10:00:00',
}

const auditResponseFixture: AuditResponse = {
  field_audit: [auditFieldRowFixture],
  batch_audit: [auditBatchRowFixture],
}

// ─── Compile-time shape tests ─────────────────────────────────────────────

test('fixtures prove accepted shape', () => {
  assert.equal(stockInfoFixture.stock_code, '600519')
  assert.ok(indicatorsResponseFixture.freshness !== null)
  assert.equal(klineResponseFixture.candles.length, 1)
  assert.equal(trendResponseFixture.trend[0].roe, 0.40)
  assert.equal(auditResponseFixture.field_audit[0].confidence, 'strict')
})

// ─── fmt runtime tests ────────────────────────────────────────────────────

test('fmt: null returns em dash', () => {
  assert.equal(fmt(null), '—')
})

test('fmt: undefined returns em dash', () => {
  assert.equal(fmt(undefined), '—')
})

test('fmt: ordinary number with default 2 digits', () => {
  assert.equal(fmt(1234.5678), '1234.57')
})

test('fmt: large value >= 1e8 uses 亿', () => {
  assert.equal(fmt(2.1e12), '21000.00亿')
  assert.equal(fmt(1.5e8), '1.50亿')
})

test('fmt: large value >= 1e4 uses 万', () => {
  assert.equal(fmt(50000), '5.00万')
  assert.equal(fmt(12345), '1.23万')
})

test('fmt: explicit zero digits', () => {
  assert.equal(fmt(2.1e12, 0), '21000亿')
  assert.equal(fmt(1680.5, 0), '1681')
})

test('fmt: string conversion', () => {
  assert.equal(fmt('hello'), 'hello')
})

// ─── fmtPct runtime tests ─────────────────────────────────────────────────

test('fmtPct: null returns em dash', () => {
  assert.equal(fmtPct(null), '—')
})

test('fmtPct: number multiplies by 100 and adds %', () => {
  assert.equal(fmtPct(0.25), '25.00%')
  assert.equal(fmtPct(0.023), '2.30%')
})

test('fmtPct: string conversion', () => {
  assert.equal(fmtPct('N/A'), 'N/A')
})
