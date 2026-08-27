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
  fieldInputUnit,
  fieldOptionLabel,
  fieldTitleWithUnit,
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

test('fieldInputUnit：金额类指标以亿为单位并做换算（1000 = 1000亿）', () => {
  const unit = fieldInputUnit('total_market_cap')
  assert.equal(unit.label, '亿')
  assert.equal(unit.scale, 1e8)
  assert.equal(fieldInputUnit('circ_market_cap').label, '亿')
  assert.equal(fieldInputUnit('interest_bearing_debt').label, '亿')
  assert.equal(fieldInputUnit('income.revenue').label, '亿')
})

test('fieldInputUnit：估值/倍数/价格/年份等指标都有明确单位', () => {
  assert.equal(fieldInputUnit('pe_ttm').label, '倍')
  assert.equal(fieldInputUnit('pb_mrq').label, '倍')
  assert.equal(fieldInputUnit('current_ratio').label, '倍')
  assert.equal(fieldInputUnit('latest_close').label, '元')
  assert.equal(fieldInputUnit('dps').label, '元')
  assert.equal(fieldInputUnit('consecutive_div_years').label, '年')
  assert.equal(fieldInputUnit('avg_volume').label, '万股')
})

test('fieldInputUnit：百分比两类口径均显示 %，但换算 scale 不同', () => {
  const pct = fieldInputUnit('dividend_yield')
  assert.equal(pct.label, '%')
  assert.equal(pct.scale, 0.01)
  const percent = fieldInputUnit('ttm_dividend_yield')
  assert.equal(percent.label, '%')
  assert.equal(percent.scale, 1)
})

test('fieldInputUnit：排名/统计派生字段与下拉框显示单位', () => {
  assert.equal(fieldInputUnit('pe_ttm_market_rank').label, '名')
  assert.equal(fieldInputUnit('pe_ttm_market_percentile').label, '分位')
  assert.equal(fieldInputUnit('pe_ttm_stat_1y_percentile').label, '分位')
  assert.equal(fieldInputUnit('pe_ttm_stat_1y_zscore').label, 'σ')
  assert.equal(fieldOptionLabel('total_market_cap', '总市值'), '总市值(亿)')
  assert.equal(fieldOptionLabel('dividend_yield', '股息率'), '股息率(%)')
})

test('fieldTitleWithUnit：筛选项/表头也带统一单位', () => {
  assert.equal(fieldTitleWithUnit('total_market_cap', '总市值'), '总市值(亿)')
  assert.equal(fieldTitleWithUnit('pe_ttm', 'PE'), 'PE(倍)')
  assert.equal(fieldTitleWithUnit('dividend_yield', '股息率'), '股息率(%)')
})

test('fieldInputUnit：标准化财务表中的监管率也按百分数识别', () => {
  assert.equal(fieldFormat('balance.capital_adequacy_ratio'), 'percent')
  assert.equal(fieldInputUnit('balance.capital_adequacy_ratio').label, '%')
  assert.equal(formatFieldValue('balance.capital_adequacy_ratio', 12), '12.00%')
  assert.equal(formatFieldValue('balance.non_performing_loan_ratio', 1.5), '1.50%')
})
