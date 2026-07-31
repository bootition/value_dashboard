/**
 * P1-4 contract test: server-authoritative indicator trust signal.
 *
 * The backend embeds `trust` (warning_codes / untrusted_all / untrusted_fields)
 * in /api/stock/{code}/indicators and /api/watchlist/list responses and masks
 * the affected numeric fields to null. The frontend must render 数据不可信
 * instead of a number whenever isIndicatorUntrusted flags the field, even if
 * a numeric value were still present.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  isIndicatorUntrusted,
  type IndicatorTrust,
  type WarningCode,
} from '../src/types/data-quality.ts'
import type { IndicatorsResponse } from '../src/types/stock-detail.ts'

// ─── Compile-time shape fixtures ──────────────────────────────────────────

const blockedTrustFixture: IndicatorTrust = {
  warning_codes: ['LINEAGE_INVALID'],
  untrusted_all: true,
  untrusted_fields: [],
}

const dividendTrustFixture: IndicatorTrust = {
  warning_codes: ['DIVIDEND_DATES_UNVERIFIED'],
  untrusted_all: false,
  untrusted_fields: ['consecutive_div_years', 'dividend_yield', 'dps', 'payout_ratio'],
}

const cleanTrustFixture: IndicatorTrust = {
  warning_codes: [],
  untrusted_all: false,
  untrusted_fields: [],
}

const indicatorsResponseFixture: IndicatorsResponse = {
  indicators: {
    valuation: {
      pe_ttm: { value: null, historical_capable: false, untrusted: true },
      pb_mrq: { value: null, historical_capable: false, untrusted: true },
      ps_ttm: { value: null, historical_capable: false, untrusted: true },
      pcf_ttm: { value: null, historical_capable: false, untrusted: true },
      dividend_yield: { value: null, historical_capable: false, untrusted: true },
      total_market_cap: { value: null, historical_capable: false, untrusted: true },
      circ_market_cap: { value: null, historical_capable: false, untrusted: true },
    },
    profitability: {
      roe: { value: null, historical_capable: true, untrusted: true },
      roa: { value: null, historical_capable: true, untrusted: true },
      gross_margin: { value: null, historical_capable: true, untrusted: true },
      net_margin: { value: null, historical_capable: true, untrusted: true },
      roic: { value: null, historical_capable: true, untrusted: true },
      cf_to_net_profit: { value: null, historical_capable: true, untrusted: true },
    },
    growth: {
      revenue_yoy: { value: null, historical_capable: true, untrusted: true },
      net_profit_yoy: { value: null, historical_capable: true, untrusted: true },
      deducted_profit_yoy: { value: null, historical_capable: true, untrusted: true },
      revenue_cagr3: { value: null, historical_capable: true, untrusted: true },
      revenue_cagr5: { value: null, historical_capable: true, untrusted: true },
      net_profit_cagr3: { value: null, historical_capable: true, untrusted: true },
      net_profit_cagr5: { value: null, historical_capable: true, untrusted: true },
      deducted_profit_cagr3: { value: null, historical_capable: true, untrusted: true },
      deducted_profit_cagr5: { value: null, historical_capable: true, untrusted: true },
    },
    safety: {
      debt_ratio: { value: null, historical_capable: true, untrusted: true },
      current_ratio: { value: null, historical_capable: true, untrusted: true },
      quick_ratio: { value: null, historical_capable: true, untrusted: true },
      interest_bearing_debt: { value: null, historical_capable: true, untrusted: true },
      interest_coverage: { value: null, historical_capable: true, untrusted: true },
      goodwill_ratio: { value: null, historical_capable: true, untrusted: true },
    },
    shareholder_return: {
      payout_ratio: { value: null, historical_capable: true, untrusted: true },
      dps: { value: null, historical_capable: true, untrusted: true },
      consecutive_div_years: { value: null, historical_capable: true, untrusted: true },
    },
  },
  report_date: '2025-12-31',
  latest_close: null,
  latest_price_date: '2025-01-20',
  freshness: null,
  trust: blockedTrustFixture,
}

// Mirrors the WatchlistPage trustedRender policy: tag beats number.
function renderDecision(
  field: string,
  value: number | null,
  warningCodes: readonly WarningCode[],
): string {
  if (isIndicatorUntrusted(field, warningCodes)) return '数据不可信'
  return value != null ? String(value) : '—'
}

// ─── Tests ────────────────────────────────────────────────────────────────

test('IndicatorTrust fixtures match the backend indicator_trust shape', () => {
  assert.equal(blockedTrustFixture.untrusted_all, true)
  assert.deepEqual([...blockedTrustFixture.warning_codes], ['LINEAGE_INVALID'])
  assert.equal(cleanTrustFixture.warning_codes.length, 0)
  assert.ok(dividendTrustFixture.untrusted_fields.includes('dividend_yield'))
})

test('IndicatorsResponse accepts the masked trust payload', () => {
  assert.equal(indicatorsResponseFixture.trust?.untrusted_all, true)
  assert.equal(indicatorsResponseFixture.indicators.valuation.pe_ttm.value, null)
  assert.equal(indicatorsResponseFixture.indicators.valuation.pe_ttm.untrusted, true)
  assert.equal(indicatorsResponseFixture.latest_close, null)
})

test('snapshot-blocking warnings render 数据不可信 for every watchlist numeric field', () => {
  const fields = [
    'latest_close', 'pe_ttm', 'pb_mrq', 'roe', 'gross_margin',
    'net_margin', 'debt_ratio', 'revenue_yoy', 'net_profit_yoy', 'dividend_yield',
  ] as const
  for (const field of fields) {
    assert.equal(
      renderDecision(field, 12.5, blockedTrustFixture.warning_codes),
      '数据不可信',
      `${field} must not render a number under LINEAGE_INVALID`,
    )
  }
})

test('dividend-only warning renders 数据不可信 only for dividend fields', () => {
  const codes = dividendTrustFixture.warning_codes
  assert.equal(renderDecision('dividend_yield', 0.02, codes), '数据不可信')
  assert.equal(renderDecision('payout_ratio', 0.5, codes), '数据不可信')
  assert.equal(renderDecision('pe_ttm', 12.5, codes), '12.5')
  assert.equal(renderDecision('roe', 0.2, codes), '0.2')
})

test('clean trust renders numbers and em dash for nulls', () => {
  const codes = cleanTrustFixture.warning_codes
  assert.equal(renderDecision('pe_ttm', 12.5, codes), '12.5')
  assert.equal(renderDecision('pe_ttm', null, codes), '—')
})

test('masked null values never surface as numbers even without warning codes', () => {
  assert.equal(renderDecision('pe_ttm', null, []), '—')
})
