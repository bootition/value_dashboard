import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  collectRuleFields,
  computeUntrustedFields,
  type ScreeningRuleNode,
} from '../src/helpers/screening-quality.ts'
import { fieldDisplayName } from '../src/utils/screening-format.ts'

// ─────────────────────────────────────────────────────────────────────────────
// collectRuleFields
// ─────────────────────────────────────────────────────────────────────────────

test('collectRuleFields: flat AND node returns all condition fields', () => {
  const node: ScreeningRuleNode = {
    logic: 'AND',
    rules: [
      { field: 'pe_ttm', op: '>', value: 0 },
      { field: 'roe', op: '>', value: 0.1 },
    ],
  }
  const fields = collectRuleFields(node)
  assert.deepEqual([...fields].sort(), ['pe_ttm', 'roe'])
})

test('collectRuleFields: nested OR node collects from all depths', () => {
  const node: ScreeningRuleNode = {
    logic: 'AND',
    rules: [
      { field: 'pe_ttm', op: '>', value: 0 },
      {
        logic: 'OR',
        rules: [
          { field: 'roe', op: '>', value: 0.1 },
          { field: 'dividend_yield', op: '>', value: 0.02 },
        ],
      },
    ],
  }
  const fields = collectRuleFields(node)
  assert.deepEqual([...fields].sort(), ['dividend_yield', 'pe_ttm', 'roe'])
})

test('collectRuleFields: deeply nested (3 levels) collects all fields', () => {
  const node: ScreeningRuleNode = {
    logic: 'AND',
    rules: [
      {
        logic: 'OR',
        rules: [
          {
            logic: 'AND',
            rules: [{ field: 'pb_mrq', op: '<', value: 2 }],
          },
          { field: 'pe_ttm', op: '>', value: 0 },
        ],
      },
    ],
  }
  const fields = collectRuleFields(node)
  assert.deepEqual([...fields].sort(), ['pb_mrq', 'pe_ttm'])
})

test('collectRuleFields: empty rules returns empty set', () => {
  const node: ScreeningRuleNode = { logic: 'AND', rules: [] }
  assert.deepEqual([...collectRuleFields(node)], [])
})

// ─────────────────────────────────────────────────────────────────────────────
// computeUntrustedFields
// ─────────────────────────────────────────────────────────────────────────────

test('computeUntrustedFields: no warnings => empty', () => {
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm', 'roe'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm', 'roe', 'dividend_yield'],
    warningCodes: [],
  })
  assert.deepEqual(result, [])
})

test('computeUntrustedFields: DIVIDEND_DATES_UNVERIFIED + dividend_yield in result columns => blocks', () => {
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm', 'dividend_yield'],
    warningCodes: ['DIVIDEND_DATES_UNVERIFIED'],
  })
  assert.deepEqual(result, ['dividend_yield'])
})

test('computeUntrustedFields: DIVIDEND_DATES_UNVERIFIED + no dividend column in results => no block', () => {
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm', 'roe'],
    warningCodes: ['DIVIDEND_DATES_UNVERIFIED'],
  })
  assert.deepEqual(result, [])
})

test('computeUntrustedFields: STALE_RUNNING_JOBS alone does NOT block save/export', () => {
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm', 'roe'],
    warningCodes: ['STALE_RUNNING_JOBS'],
  })
  assert.deepEqual(result, [])
})

test('computeUntrustedFields: UNPUBLISHED_OVERRIDES alone does NOT block save/export', () => {
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm', 'roe'],
    warningCodes: ['UNPUBLISHED_OVERRIDES'],
  })
  assert.deepEqual(result, [])
})

test('computeUntrustedFields: LINEAGE_INVALID blocks all snapshot-dependent result columns', () => {
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm', 'roe', 'dividend_yield'],
    warningCodes: ['LINEAGE_INVALID'],
  })
  // LINEAGE_INVALID maps to '*' (all indicators). Metadata columns (stock_code, name) are NOT indicators.
  // But the helper returns ALL result columns that are untrusted, including non-metadata.
  // The caller decides which ones matter. The helper returns pe_ttm, roe, dividend_yield.
  assert.deepEqual(result.sort(), ['dividend_yield', 'pe_ttm', 'roe'])
})

test('computeUntrustedFields: sort field is untrusted => included', () => {
  const result = computeUntrustedFields({
    ruleFields: [],
    sortField: 'dividend_yield',
    resultColumns: ['stock_code', 'name', 'dividend_yield'],
    warningCodes: ['DIVIDEND_DATES_UNVERIFIED'],
  })
  assert.deepEqual(result, ['dividend_yield'])
})

test('computeUntrustedFields: rule field not in result columns still included if untrusted', () => {
  // Rule references dividend_yield but it's not in result columns.
  // The helper should still flag it because the rule depends on it.
  const result = computeUntrustedFields({
    ruleFields: ['dividend_yield'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm'],
    warningCodes: ['DIVIDEND_DATES_UNVERIFIED'],
  })
  assert.deepEqual(result, ['dividend_yield'])
})

test('computeUntrustedFields: metadata columns starting with _ are excluded from durable columns', () => {
  // _entry_explanation is metadata. Even if it were "untrusted" (it won't be),
  // it should not appear in the durable column list.
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm', '_entry_explanation', '_data_date'],
    warningCodes: ['LINEAGE_INVALID'],
  })
  // _entry_explanation and _data_date should NOT appear in untrusted list
  for (const field of result) {
    assert.ok(!field.startsWith('_'), `${field} should not start with _`)
  }
})

test('computeUntrustedFields: csrc_l2 不会被误判；MINIMUM_DATA_NOT_READY 不再让全部指标不可信', () => {
  const result = computeUntrustedFields({
    ruleFields: ['csrc_l2', 'total_market_cap', 'dividend_financing_ratio_pct'],
    sortField: 'csrc_l2',
    resultColumns: ['stock_code', 'name', 'csrc_l2', 'total_market_cap', 'dividend_financing_ratio_pct'],
    warningCodes: ['MINIMUM_DATA_NOT_READY', 'DIVIDEND_DATES_UNVERIFIED'],
  })
  // 只有分红类字段仍受 DIVIDEND_DATES_UNVERIFIED 影响；csrc_l2 和 total_market_cap 不再误伤。
  assert.deepEqual(result, ['dividend_financing_ratio_pct'])
})

test('computeUntrustedFields: empty results => empty', () => {
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm'],
    sortField: 'pe_ttm',
    resultColumns: [],
    warningCodes: ['DIVIDEND_DATES_UNVERIFIED'],
  })
  // rule field dividend_yield is not in ruleFields here; pe_ttm is not dividend.
  // So nothing is untrusted.
  assert.deepEqual(result, [])
})

test('computeUntrustedFields: unknown future warning code does NOT block', () => {
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm'],
    warningCodes: ['SOME_FUTURE_WARNING'],
  })
  assert.deepEqual(result, [])
})

test('computeUntrustedFields: combined operations warnings do NOT block', () => {
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm', 'dividend_yield'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm', 'dividend_yield'],
    warningCodes: ['STALE_RUNNING_JOBS', 'UNPUBLISHED_OVERRIDES'],
  })
  assert.deepEqual(result, [])
})

test('computeUntrustedFields: nested dividend rule + dividend in results => blocks', () => {
  const node: ScreeningRuleNode = {
    logic: 'AND',
    rules: [
      { field: 'pe_ttm', op: '>', value: 0 },
      {
        logic: 'OR',
        rules: [{ field: 'dividend_yield', op: '>', value: 0.02 }],
      },
    ],
  }
  const ruleFields = collectRuleFields(node)
  const result = computeUntrustedFields({
    ruleFields,
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm', 'dividend_yield'],
    warningCodes: ['DIVIDEND_DATES_UNVERIFIED'],
  })
  assert.deepEqual(result, ['dividend_yield'])
})

test('computeUntrustedFields: global all-indicator warning (FINANCIAL_SHELL_ROWS) blocks all result indicator columns', () => {
  const result = computeUntrustedFields({
    ruleFields: ['pe_ttm'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm', 'roe', 'dividend_yield'],
    warningCodes: ['FINANCIAL_SHELL_ROWS'],
  })
  // FINANCIAL_SHELL_ROWS maps to '*' which means all indicators.
  // stock_code and name are metadata (not indicator fields), but the helper
  // uses isIndicatorUntrusted which treats '*' as affecting any field passed to it.
  // So the helper returns all result columns (including stock_code/name).
  // The caller is responsible for filtering metadata.
  assert.ok(result.includes('pe_ttm'))
  assert.ok(result.includes('roe'))
  assert.ok(result.includes('dividend_yield'))
})

test('computeUntrustedFields: union of sources — field in rule but not in results still flagged', () => {
  // Rule references dividend_yield, results don't contain it.
  // But sort field pe_ttm is affected by LINEAGE_INVALID.
  const result = computeUntrustedFields({
    ruleFields: ['dividend_yield'],
    sortField: 'pe_ttm',
    resultColumns: ['stock_code', 'name', 'pe_ttm'],
    warningCodes: ['DIVIDEND_DATES_UNVERIFIED', 'LINEAGE_INVALID'],
  })
  // dividend_yield from rule, pe_ttm from sort+result.
  assert.ok(result.includes('dividend_yield'))
  assert.ok(result.includes('pe_ttm'))
})


// ─────────────────────────────────────────────────────────────────────────────
// fieldDisplayName 中文化（筛选/自选/详情共用）
// ─────────────────────────────────────────────────────────────────────────────

test('fieldDisplayName: 核心指标不再显示纯英文', () => {
  assert.equal(fieldDisplayName('period_return'), '区间收益率')
  assert.equal(fieldDisplayName('annualized_volatility'), '年化波动率')
  assert.equal(fieldDisplayName('max_drawdown'), '最大回撤')
  assert.equal(fieldDisplayName('turnover_rate'), '换手率')
  assert.equal(fieldDisplayName('roic'), '投入资本回报率（ROIC）')
  assert.equal(fieldDisplayName('consecutive_div_years'), '连续分红年数')
  assert.equal(fieldDisplayName('ma20'), '20 日均线')
  assert.equal(fieldDisplayName('interest_bearing_debt'), '有息负债')
})

test('fieldDisplayName: 排名后缀字段组合为中文', () => {
  assert.equal(fieldDisplayName('pe_ttm_market_rank'), '市盈率（PE-TTM） · 全市场排名')
  assert.equal(fieldDisplayName('roe_industry_percentile'), '净资产收益率（ROE） · 证监会一级分位')
  assert.equal(fieldDisplayName('period_return_sw2_rank'), '区间收益率 · 证监会二级排名')
})

test('fieldDisplayName: 标准化财务表字段带中文表前缀', () => {
  assert.equal(fieldDisplayName('balance.total_assets'), '资产负债表 · 资产总计')
  assert.equal(fieldDisplayName('income.parent_net_profit'), '利润表 · 归母净利润')
  assert.equal(fieldDisplayName('cashflow.cf_from_operating'), '现金流量表 · 经营活动现金流量净额')
  assert.equal(fieldDisplayName('exchange'), '交易所')
})
