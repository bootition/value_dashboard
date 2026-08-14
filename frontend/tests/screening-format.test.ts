/**
 * 字段单位口径契约测试（2026-08-14 红队 F3，reports/80 簇 A/B/C）。
 *
 * 用正式库真实样本值钉死两类百分比口径，防止再次漂移：
 * - 小数存储（pct）：dividend_yield=0.0529、net_profit_cagr3=0.3294 →
 *   输入需 ÷100、展示 ×100+%
 * - 百分数存储（percent）：ttm_dividend_yield=5.1724、
 *   div_yield_spread_10y=-1.3435、turnover_rate=0.3598 → 输入原样、展示直接加 %
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  fieldFormat,
  formatFieldValue,
  applyIndicatorUnits,
} from '../src/utils/screening-format.ts'

test('百分数存储字段归类为 percent（不 ÷100、不 ×100）', () => {
  assert.equal(fieldFormat('ttm_dividend_yield'), 'percent')
  assert.equal(fieldFormat('div_yield_spread_10y'), 'percent')
  assert.equal(fieldFormat('div_yield_spread_0p25y'), 'percent')
  assert.equal(fieldFormat('div_yield_spread_30y'), 'percent')
  assert.equal(fieldFormat('turnover_rate'), 'percent')
})

test('小数存储字段归类为 pct（含红队 80 簇 A 补齐的 6 字段）', () => {
  for (const field of [
    'dividend_yield', 'roe', 'net_profit_cagr3', 'net_profit_cagr5',
    'deducted_profit_cagr3', 'deducted_profit_cagr5',
    'period_return', 'annualized_volatility', 'max_drawdown',
  ]) {
    assert.equal(fieldFormat(field), 'pct', field)
  }
})

test('倍数/价格/普通字段不受影响', () => {
  assert.equal(fieldFormat('current_ratio'), 'ratio')
  assert.equal(fieldFormat('latest_close'), 'price')
  assert.equal(fieldFormat('pe_ttm'), 'plain')
})

test('百分数存储展示：5.1724 → "5.17%"（正式库 000683 样本）', () => {
  assert.equal(formatFieldValue('ttm_dividend_yield', 5.1724), '5.17%')
  assert.equal(formatFieldValue('div_yield_spread_10y', -1.3435), '-1.34%')
  assert.equal(formatFieldValue('turnover_rate', 0.3598), '0.36%')
})

test('小数存储展示：0.3294 → "32.94%"（正式库 300750 样本）', () => {
  assert.equal(formatFieldValue('net_profit_cagr3', 0.3294), '32.94%')
  assert.equal(formatFieldValue('dividend_yield', 0.0529), '5.29%')
})

test('null 与区间外值安全', () => {
  assert.equal(formatFieldValue('ttm_dividend_yield', null), '—')
  assert.equal(formatFieldValue('ttm_dividend_yield', 'x'), 'x')
})

test('applyIndicatorUnits：后端 unit 元数据优先级最高（单一来源）', () => {
  applyIndicatorUnits({ ttm_dividend_yield: 'pct', custom_field: 'percent' })
  assert.equal(fieldFormat('ttm_dividend_yield'), 'pct')
  assert.equal(fieldFormat('custom_field'), 'percent')
  // 清除运行时覆盖，避免影响同进程后续用例
  applyIndicatorUnits({ ttm_dividend_yield: 'percent', custom_field: 'plain' })
  assert.equal(fieldFormat('ttm_dividend_yield'), 'percent')
})
