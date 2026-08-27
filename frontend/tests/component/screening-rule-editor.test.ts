import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ScreeningRuleEditor from '../../src/components/ScreeningRuleEditor.vue'
import type { ScreeningRuleNode } from '../../src/types/screening.ts'

const indicatorOptions = [
  { label: '市盈率 PE-TTM', value: 'pe_ttm' },
  { label: '净资产收益率 ROE', value: 'roe' },
]

const opOptions = [
  { label: '低于', value: '<' },
  { label: '高于', value: '>' },
]

function mountEditor(node: ScreeningRuleNode) {
  return mount(ScreeningRuleEditor, {
    props: {
      node,
      depth: 1,
      maxDepth: 3,
      maxConditions: 20,
      isRoot: true,
      indicatorOptions,
      opOptions,
    },
  })
}

describe('ScreeningRuleEditor 规则语义', () => {
  it('用自然语言解释全部条件，并在条件间显示并且', () => {
    const wrapper = mountEditor({
      id: 'root',
      logic: 'AND',
      rules: [
        { id: 'a', field: 'pe_ttm', op: '<', value: 15 },
        { id: 'b', field: 'roe', op: '>', value: 10 },
      ],
    })

    expect(wrapper.text()).toContain('同时满足全部条件')
    expect(wrapper.text()).toContain('并且')
    expect(wrapper.findAll('.condition-line')).toHaveLength(2)
  })

  it('选择任一成立后更新逻辑并改为或者', async () => {
    const node: ScreeningRuleNode = {
      id: 'root',
      logic: 'AND',
      rules: [
        { id: 'a', field: 'pe_ttm', op: '<', value: 15 },
        { id: 'b', field: 'roe', op: '>', value: 10 },
      ],
    }
    const wrapper = mountEditor(node)

    await wrapper.findAll('.logic-switch button')[1].trigger('click')

    expect(node.logic).toBe('OR')
    expect(wrapper.text()).toContain('满足任意一个条件')
    expect(wrapper.text()).toContain('或者')
  })

  it('切换到区间时清除旧字段比较，保持后端规则合法', async () => {
    const node: ScreeningRuleNode = {
      id: 'root',
      logic: 'AND',
      rules: [{ id: 'a', field: 'pe_ttm', op: '<', value: 15, right_field: 'roe' }],
    }
    const wrapper = mountEditor(node)
    const row = wrapper.findComponent({ name: 'RuleConditionRow' })

    await row.vm.$emit('update:op', 'between')

    const condition = node.rules[0]
    expect('right_field' in condition && condition.right_field).toBeUndefined()
  })
})
