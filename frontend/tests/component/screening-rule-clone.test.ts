import { describe, it, expect } from 'vitest'
import { cloneScreeningRule, isRuleNode } from '../../src/types/screening.ts'
import type {
  ScreeningRuleCondition,
  ScreeningRuleNode,
  ScreeningSortSpec,
} from '../../src/types/screening.ts'

/**
 * `ScreeningRuleNode.rules` 的元素类型是「嵌套组 | 叶子条件」联合，`.value` 只存在于
 * 叶子条件上。本文件两个用例放的都是叶子条件，所以在读 `.value` 之前用类型里自带的
 * `isRuleNode()` 收窄——这是类型化的收窄，不是 `as any` 式的放宽。
 */
function asLeaf(item: ScreeningRuleNode | ScreeningRuleCondition): ScreeningRuleCondition {
  if (isRuleNode(item)) throw new Error('fixture 里只应放叶子条件，不应出现嵌套组')
  return item
}

describe('cloneScreeningRule 深拷贝已保存规则', () => {
  it('修改克隆后的条件值不会污染原始规则', () => {
    const saved: ScreeningRuleNode = {
      id: 'root',
      logic: 'AND',
      rules: [{ id: 'c1', field: 'total_market_cap', op: '>', value: 30000000000 }],
    }
    const clone = cloneScreeningRule(saved)

    const clonedCondition = asLeaf(clone.rules[0]!)
    const savedCondition = asLeaf(saved.rules[0]!)
    clonedCondition.value = 10000000000

    expect(clonedCondition.value).toBe(10000000000)
    expect(savedCondition.value).toBe(30000000000)
  })

  it('修改克隆后的排序数组不会污染原始排序', () => {
    // 排序规则的正式类型是 ScreeningSortSpec（direction: 'asc' | 'desc'）。
    // 旧写法 `direction: 'asc' as const` 把元素类型收窄成字面量 'asc'，
    // 于是往里写 'desc' 反而类型不合法；按正式类型标注即可。
    const saved: ScreeningSortSpec[] = [{ field: 'pe_ttm', direction: 'asc' }]
    const clone = cloneScreeningRule(saved)

    clone[0]!.direction = 'desc'

    expect(clone[0]!.direction).toBe('desc')
    expect(saved[0]!.direction).toBe('asc')
  })
})
