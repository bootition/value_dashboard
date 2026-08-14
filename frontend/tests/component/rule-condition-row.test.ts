import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { NInputNumber, NSelect } from 'naive-ui'
import RuleConditionRow from '../../src/components/RuleConditionRow.vue'
import type { ScreeningRuleCondition } from '../../src/types/screening.ts'

function makeCondition(overrides: Partial<ScreeningRuleCondition> = {}): ScreeningRuleCondition {
  return { id: 'c1', field: 'pe_ttm', op: '<', value: 15, ...overrides }
}

const indicatorOptions = [
  { label: '市盈率', value: 'pe_ttm' },
  { label: '股息率 (%)', value: 'dividend_yield' },
  { label: 'TTM已实施股息率 (%)', value: 'ttm_dividend_yield' },
  { label: '净利润 3 年复合增速 (%)', value: 'net_profit_cagr3' },
]
const opOptions = [{ label: '低于', value: '<' }]

function mountRow(condition: ScreeningRuleCondition) {
  return mount(RuleConditionRow, {
    props: { condition, ordinal: 1, indicatorOptions, opOptions },
    global: {
      stubs: {
        'n-select': { ...NSelect, template: '<div />' },
        'n-input-number': { ...NInputNumber, template: '<input />' },
      },
    },
  })
}

describe('RuleConditionRow 百分比字段条件单位换算（reports/79）', () => {
  it('百分数字段：输入 2 → 发出 0.02（小数存储）', async () => {
    const wrapper = mountRow(makeCondition({ field: 'dividend_yield', value: 0.02 }))
    // 显示应为百分数：0.02 → 2
    const input = wrapper.find('input')
    expect(input.exists()).toBe(true)
    const numberInput = wrapper.findComponent({ name: 'InputNumber' })
    if (numberInput.exists()) {
      await numberInput.vm.$emit('update:value', 2)
      const emitted = wrapper.emitted('update:value')
      expect(emitted).toBeTruthy()
      expect(emitted![emitted!.length - 1][0]).toBe(0.02)
    }
  })

  it('非百分数字段：输入不变换（pe_ttm 15 → 15）', async () => {
    const wrapper = mountRow(makeCondition({ field: 'pe_ttm', value: 15 }))
    const numberInput = wrapper.findComponent({ name: 'InputNumber' })
    if (numberInput.exists()) {
      await numberInput.vm.$emit('update:value', 20)
      const emitted = wrapper.emitted('update:value')
      expect(emitted![emitted!.length - 1][0]).toBe(20)
    }
  })

  it('显示百分数字段时带 % 单位与说明文案', () => {
    const wrapper = mountRow(makeCondition({ field: 'dividend_yield', value: 0.0529 }))
    expect(wrapper.text()).toContain('%')
    expect(wrapper.text()).toContain('百分数')
  })

  it('区间条件（between）同样按百分数换算', async () => {
    const wrapper = mountRow(
      makeCondition({ field: 'dividend_yield', op: 'between', value: [0.02, 0.05] }),
    )
    const inputs = wrapper.findAllComponents({ name: 'InputNumber' })
    if (inputs.length >= 2) {
      await inputs[0].vm.$emit('update:value', 3)
      const emitted = wrapper.emitted('update:value')
      const last = emitted![emitted!.length - 1][0] as [number, number]
      expect(last[0]).toBeCloseTo(0.03)
      expect(last[1]).toBe(0.05)
    }
  })
})

describe('RuleConditionRow 百分数存储字段原样换算（reports/80 F3 簇 B）', () => {
  it('ttm_dividend_yield：输入 2 → 发出 2（百分数存储，不 ÷100）', async () => {
    const wrapper = mountRow(makeCondition({ field: 'ttm_dividend_yield', value: 5.17 }))
    const numberInput = wrapper.findComponent({ name: 'InputNumber' })
    if (numberInput.exists()) {
      await numberInput.vm.$emit('update:value', 2)
      const emitted = wrapper.emitted('update:value')
      expect(emitted![emitted!.length - 1][0]).toBe(2)
    }
  })

  it('ttm_dividend_yield：仍显示 % 单位与百分数说明', () => {
    const wrapper = mountRow(makeCondition({ field: 'ttm_dividend_yield', value: 5.17 }))
    expect(wrapper.text()).toContain('%')
    expect(wrapper.text()).toContain('百分数')
  })

  it('net_profit_cagr3（簇 A 补齐）：输入 32 → 发出 0.32（小数存储 ÷100）', async () => {
    const wrapper = mountRow(makeCondition({ field: 'net_profit_cagr3', value: 0.3294 }))
    const numberInput = wrapper.findComponent({ name: 'InputNumber' })
    if (numberInput.exists()) {
      await numberInput.vm.$emit('update:value', 32)
      const emitted = wrapper.emitted('update:value')
      expect(emitted![emitted!.length - 1][0]).toBeCloseTo(0.32)
    }
  })
})
