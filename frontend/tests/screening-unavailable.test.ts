/**
 * 「当前报告期不可用指标」展示文案契约（2026-11-22 反返工护栏）。
 *
 * 守护对象：`src/utils/screening-unavailable.ts`（2026-11-22 从
 * `views/ScreeningPage.vue` 抽出的 5 个纯函数）。
 *
 * 为什么值得钉死：这些文案是"用户以为功能没了"的唯一防线
 * （reports/120 §5 J5/J9），历史上踩过两次：
 * 1. 废弃字段（dividend_yield → ttm_dividend_yield）与覆盖率无关，
 *    却统一写「覆盖率未知」，用户以为数据没采到（2026-11-16 修复）；
 * 2. 有 reason 但没登记中文标签时，文案退化成一个看不懂的英文码。
 * 抽文件时最容易"顺手改文案"，所以这里逐字断言。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  UNAVAILABLE_REASON_LABELS,
  unavailableCoverageText,
  unavailableKey,
  unavailableLabel,
  unavailableName,
  unavailableReasonText,
} from '../src/utils/screening-unavailable.ts'

test('unavailableName：name 优先，其次 field，都没有给占位', () => {
  assert.equal(unavailableName({ name: 'pe_ttm', field: 'pe' }), 'pe_ttm')
  assert.equal(unavailableName({ field: 'roe' }), 'roe')
  assert.equal(unavailableName({}), '未命名字段')
})

test('unavailableLabel：未命名字段直接返回占位，不给它套单位', () => {
  assert.equal(unavailableLabel({}), '未命名字段')
  // 有名字时走 FIELD_LABELS 中文映射（不是原始字段名）
  assert.equal(unavailableLabel({ name: 'pe_ttm' }), '市盈率（PE-TTM）')
})

test('unavailableReasonText：登记过的 reason 给中文+原文码', () => {
  const text = unavailableReasonText({ name: 'x', reason: 'masked_untrusted' })
  assert.match(text, /数据质量遮蔽/)
  assert.match(text, /reason: masked_untrusted/)
})

test('unavailableReasonText：未登记 reason 不退化，仍带上英文码', () => {
  const text = unavailableReasonText({ name: 'x', reason: 'some_new_reason' })
  assert.match(text, /当前不可用/)
  assert.match(text, /reason: some_new_reason/)
})

test('unavailableReasonText：没有 reason 时写明「原因未标注」，不编造', () => {
  assert.equal(unavailableReasonText({ name: 'x' }), '原因未标注')
  assert.equal(unavailableReasonText({ name: 'x', reason: '   ' }), '原因未标注')
})

test('unavailableCoverageText：有覆盖率时按存储百分数展示（不 ×100）', () => {
  assert.equal(unavailableCoverageText({ name: 'x', coverage_pct: 12.34 }), '覆盖率 12.3%')
  // 0 是合法覆盖率，不能被当成"未知"
  assert.equal(unavailableCoverageText({ name: 'x', coverage_pct: 0 }), '覆盖率 0.0%')
})

test('unavailableCoverageText：deprecated 与覆盖率无关，不能写「覆盖率未知」', () => {
  assert.equal(
    unavailableCoverageText({ reason: 'deprecated', replaced_by: 'ttm_dividend_yield' }),
    '已改名为 股息率（TTM）',
  )
  assert.equal(unavailableCoverageText({ reason: 'deprecated' }), '口径已统一')
  assert.equal(unavailableCoverageText({ reason: 'masked_untrusted' }), '数据质量遮蔽')
  assert.equal(unavailableCoverageText({ name: 'x' }), '覆盖率未知')
})

test('unavailableKey：同一字段不同 reason 不撞 key（v-for key 稳定性）', () => {
  const a = unavailableKey({ name: 'pe_ttm', reason: 'masked_untrusted' })
  const b = unavailableKey({ name: 'pe_ttm', reason: 'deprecated' })
  assert.notEqual(a, b)
  assert.equal(unavailableKey({ field: 'roe' }), 'roe:')
})

test('reason 标签表覆盖后端三种 reason（漏一个就是英文码泄漏到界面）', () => {
  assert.deepEqual(Object.keys(UNAVAILABLE_REASON_LABELS).sort(), [
    'deprecated',
    'masked_untrusted',
    'not_available_for_latest_period',
  ])
})
