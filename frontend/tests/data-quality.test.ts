/**
 * RED-phase test.
 *
 * This file imports the not-yet-existing data-quality contract module.
 * Running it MUST fail with a module-resolution error until the GREEN
 * implementation lands.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  isIndicatorUntrusted,
  KNOWN_WARNING_CODES,
  DIVIDEND_INDICATOR_FIELDS,
  SNAPSHOT_DEPENDENT_INDICATOR_FIELDS,
  type DataQualityStatus,
  type StockFreshness,
} from '../src/types/data-quality.ts'

test('KNOWN_WARNING_CODES is a readonly tuple of the known codes', () => {
  assert.ok(Array.isArray(KNOWN_WARNING_CODES))
  assert.equal(KNOWN_WARNING_CODES.length, 9)
  const expected = [
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
  for (const code of expected) {
    assert.ok(KNOWN_WARNING_CODES.includes(code), `missing ${code}`)
  }
})

test('DIVIDEND_INDICATOR_FIELDS lists dividend and treasury spread indicators', () => {
  const fields = [...DIVIDEND_INDICATOR_FIELDS]
  assert.deepEqual(fields.sort(), [
    'consecutive_div_years',
    'div_yield_spread_0p25y',
    'div_yield_spread_0p5y',
    'div_yield_spread_10y',
    'div_yield_spread_1y',
    'div_yield_spread_2y',
    'div_yield_spread_30y',
    'div_yield_spread_3y',
    'div_yield_spread_5y',
    'div_yield_spread_7y',
    'dividend_yield',
    'dps',
    'payout_ratio',
    'ttm_dividend_yield',
  ])
})

test('SNAPSHOT_DEPENDENT_INDICATOR_FIELDS is exactly the wildcard sentinel', () => {
  assert.deepEqual([...SNAPSHOT_DEPENDENT_INDICATOR_FIELDS], ['*'])
})

test('DataQualityStatus shape matches backend build_data_quality_status', () => {
  const status: DataQualityStatus = {
    minimum_data_readiness: {
      ready: true,
      stock_count: 1,
      missing: {},
      missing_counts: {},
      schema_compatibility: { compatible: true, missing: [] },
    },
    dates: {
      price: '2025-01-20',
      balance_sheet: { latest_record: '2024-09-30', latest_complete: '2024-06-30' },
      income_statement: { latest_record: '2024-09-30', latest_complete: '2024-06-30' },
      cash_flow: { latest_record: '2024-09-30', latest_complete: '2024-06-30' },
      indicator_snapshot: {
        latest_complete: '2024-06-30',
        calculated_at: '2025-01-20T10:00:00',
        latest_price_date: '2025-01-20',
      },
    },
    dividends: { total_rows: 100, active_missing_announcement_rows: 5, unverified_period_end_rows: 5 },
    lineage: {
      invalid_hash_rows: 0,
      orphan_batch_rows: 0,
      audit_archive_gap_rows: 0,
      batch_archive_gap_rows: 0,
      archive_gap_rows: 0,
    },
    code_identity: { raw_alias_rows: 0 },
    operations: { unpublished_overrides: 0, running_jobs: 0 },
    warning_codes: [],
  }
  assert.equal(status.dates.price, '2025-01-20')
  assert.equal(status.dividends.total_rows, 100)
})

test('StockFreshness shape matches backend build_freshness_metadata', () => {
  const freshness: StockFreshness = {
    financial_effective_date: '2024-06-30',
    price_date: '2025-01-20',
    calculated_at: '2025-01-20T10:00:00',
    data_version: 'v1',
    stale_days: 204,
    stale_warning: false,
  }
  assert.equal(freshness.stale_days, 204)
  assert.equal(freshness.stale_warning, false)
})

test('isIndicatorUntrusted: DIVIDEND_DATES_UNVERIFIED affects dividend_yield', () => {
  assert.equal(
    isIndicatorUntrusted('dividend_yield', ['DIVIDEND_DATES_UNVERIFIED']),
    true,
  )
})

test('isIndicatorUntrusted: DIVIDEND_DATES_UNVERIFIED does NOT affect pe_ratio', () => {
  assert.equal(
    isIndicatorUntrusted('pe_ratio', ['DIVIDEND_DATES_UNVERIFIED']),
    false,
  )
})

test('isIndicatorUntrusted: LINEAGE_INVALID affects any snapshot-dependent indicator', () => {
  assert.equal(
    isIndicatorUntrusted('pe_ratio', ['LINEAGE_INVALID']),
    true,
  )
  assert.equal(
    isIndicatorUntrusted('dividend_yield', ['LINEAGE_INVALID']),
    true,
  )
})

test('isIndicatorUntrusted: SNAPSHOT_STALE affects snapshot-dependent indicator', () => {
  assert.equal(
    isIndicatorUntrusted('roe', ['SNAPSHOT_STALE']),
    true,
  )
})

test('isIndicatorUntrusted: UNPUBLISHED_OVERRIDES does NOT untrust numerical indicators', () => {
  assert.equal(
    isIndicatorUntrusted('pe_ratio', ['UNPUBLISHED_OVERRIDES']),
    false,
  )
  assert.equal(
    isIndicatorUntrusted('dividend_yield', ['UNPUBLISHED_OVERRIDES']),
    false,
  )
})

test('isIndicatorUntrusted: STALE_RUNNING_JOBS does NOT untrust numerical indicators', () => {
  assert.equal(
    isIndicatorUntrusted('pe_ratio', ['STALE_RUNNING_JOBS']),
    false,
  )
})

test('isIndicatorUntrusted: both operation warnings together do NOT untrust any indicator', () => {
  assert.equal(
    isIndicatorUntrusted('pe_ratio', ['UNPUBLISHED_OVERRIDES', 'STALE_RUNNING_JOBS']),
    false,
  )
  assert.equal(
    isIndicatorUntrusted('dividend_yield', ['UNPUBLISHED_OVERRIDES', 'STALE_RUNNING_JOBS']),
    false,
  )
})

test('isIndicatorUntrusted: unknown future warning codes are treated as not affecting any field', () => {
  assert.equal(
    isIndicatorUntrusted('pe_ratio', ['SOME_FUTURE_WARNING']),
    false,
  )
})

test('isIndicatorUntrusted: empty warning codes never untrust', () => {
  assert.equal(isIndicatorUntrusted('pe_ratio', []), false)
  assert.equal(isIndicatorUntrusted('dividend_yield', []), false)
})

test('isIndicatorUntrusted: combined warnings — one invalidating is enough', () => {
  assert.equal(
    isIndicatorUntrusted('dividend_yield', [
      'UNPUBLISHED_OVERRIDES',
      'DIVIDEND_DATES_UNVERIFIED',
    ]),
    true,
  )
})

test('isIndicatorUntrusted: FINANCIAL_SHELL_ROWS affects snapshot-dependent indicators', () => {
  assert.equal(
    isIndicatorUntrusted('pe_ratio', ['FINANCIAL_SHELL_ROWS']),
    true,
  )
})
