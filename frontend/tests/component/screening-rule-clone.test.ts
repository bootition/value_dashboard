import { describe, it, expect } from 'vitest'
import { cloneScreeningRule } from '../../src/types/screening.ts'
import type { ScreeningRuleNode } from '../../src/types/screening.ts'

describe('cloneScreeningRule 深拷贝已保存规则', () => {
  it('修改克隆后的条件值不会污染原始规则', () => {
    const saved: ScreeningRuleNode = {
      id: 'root',
      logic: 'AND',
      rules: [{ id: 'c1', field: 'total_market_cap', op: '>', value: 30000000000 }],
    }
    const clone = cloneScreeningRule(saved)

    clone.rules[0]!.value = 10000000000

    expect(clone.rules[0]!.value).toBe(10000000000)
    expect(saved.rules[0]!.value).toBe(30000000000)
  })

  it('修改克隆后的排序数组不会污染原始排序', () => {
    const saved = [{ field: 'pe_ttm', direction: 'asc' as const }]
    const clone = cloneScreeningRule(saved)

    clone[0]!.direction = 'desc'

    expect(clone[0]!.direction).toBe('desc')
    expect(saved[0]!.direction).toBe('asc')
  })
})
