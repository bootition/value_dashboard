/**
 * 「排名口径」从条件行移除后的兼容处理（2026-11-24 用户裁定）。
 *
 * 背景：`rank_mode` **不是排序**，是"在本行业内排名之后再做比较"的筛选语义 ——
 * `app/core/screening/engine.py::expand_rank_modes` 会把
 * `{field: 'pe_ttm', rank_mode: 'industry_rank', op: '<', value: 20}` 展开成
 * `{field: 'pe_ttm_industry_rank'}` 的 WHERE 条件（ORDER BY 走另一个键
 * `rule["sort"]`）。用户要求条件行里不再出现这个下拉，但**历史规则不能废**：
 *
 * 1. 载入不报错；条件行里不再渲染「排名口径」（文本 / 下拉都不出现）。
 * 2. 提交 payload 原样带 `rank_mode`（后端照旧展开，语义不变）。
 * 3. 只有"换到不支持的字段"时才会清掉它（否则后端会收到不存在的列名）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { h } from 'vue'
import RuleConditionRow from '../../src/components/RuleConditionRow.vue'
import ScreeningRuleEditor from '../../src/components/ScreeningRuleEditor.vue'
import ScreeningPage from '../../src/views/ScreeningPage.vue'
import type { ScreeningRuleCondition, ScreeningRuleNode } from '../../src/types/screening.ts'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
  }
  return { default: mock, ...mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'
import { resetScreeningSession } from '../../src/stores/screening-session.ts'

const indicatorOptions = [
  { label: '市盈率（PE-TTM）', value: 'pe_ttm' },
  { label: '总市值(亿)', value: 'total_market_cap' },
]
const opOptions = [{ label: '低于', value: '<' }]

/** 一条"历史规则"里的条件：带 rank_mode，且字段本身仍然存在。 */
function legacyCondition(overrides: Partial<ScreeningRuleCondition> = {}): ScreeningRuleCondition {
  return { id: 'c1', field: 'pe_ttm', op: '<', value: 20, rank_mode: 'industry_rank', ...overrides }
}

function mountRow(condition: ScreeningRuleCondition) {
  return mount(RuleConditionRow, {
    props: { condition, ordinal: 1, indicatorOptions, opOptions },
  })
}

describe('条件行不再渲染「排名口径」下拉', () => {
  it('普通条件：没有「排名口径」文本，也没有对应的下拉/轨道', () => {
    const wrapper = mountRow(legacyCondition({ rank_mode: undefined }))
    expect(wrapper.text()).not.toContain('排名口径')
    expect(wrapper.find('[aria-label="排名口径"]').exists()).toBe(false)
    expect(wrapper.find('.condition-control--rank').exists()).toBe(false)
  })

  it('带 rank_mode 的历史条件：同样不渲染，也不因此报错', () => {
    const wrapper = mountRow(legacyCondition())
    expect(wrapper.text()).toContain('观察指标')
    expect(wrapper.text()).toContain('判断关系')
    expect(wrapper.text()).not.toContain('排名口径')
    expect(wrapper.find('[aria-label="排名口径"]').exists()).toBe(false)
    expect(wrapper.find('.condition-control--rank').exists()).toBe(false)
  })

  it('渲染条件行不会改写 condition 上的 rank_mode（只读、不动数据）', async () => {
    const condition = legacyCondition()
    const wrapper = mountRow(condition)
    await wrapper.find('input').trigger('input')
    expect(condition.rank_mode).toBe('industry_rank')
  })
})

function mountEditor(node: ScreeningRuleNode, rankableOf: (field: string) => boolean) {
  return mount(ScreeningRuleEditor, {
    props: {
      node,
      depth: 1,
      maxDepth: 3,
      maxConditions: 20,
      isRoot: true,
      indicatorOptions,
      opOptions,
      rankableOf,
    },
  })
}

describe('条件行布局：移除排名口径后不留空轨、不挤压其它列', () => {
  /** 条件行的直接子元素（= 一条网格轨道一个），取每个元素的"角色"类名。 */
  function trackRoles(wrapper: ReturnType<typeof mountRow>): string[] {
    const element = wrapper.find('.condition-line').element
    return Array.from(element.children).map((child) => {
      const classes = child.className.split(/\s+/).filter(Boolean)
      return classes[classes.length - 1] ?? ''
    })
  }

  it('普通字段：5 轨一一对应，没有 rank 轨', () => {
    const wrapper = mountRow(legacyCondition())
    expect(trackRoles(wrapper)).toEqual([
      'condition-index',
      'condition-control--field',
      'condition-control--operator',
      'condition-control--value',
      'condition-tools',
    ])
    expect(trackRoles(wrapper)).not.toContain('condition-control--rank')
    expect(wrapper.find('.condition-line').classes()).not.toContain('condition-line--has-param')
  })

  it('参数化字段（CAGR 年数）：6 轨，参数轨紧跟在指标后面', () => {
    const wrapper = mount(RuleConditionRow, {
      props: {
        condition: legacyCondition({ field: 'revenue_cagr', value: 0.2 } as Partial<ScreeningRuleCondition>),
        ordinal: 1,
        indicatorOptions,
        opOptions,
        paramSpec: { kind: 'cagr', key: 'cagr_years', label: '年数', options: [3, 5] },
      },
    })
    expect(trackRoles(wrapper)).toEqual([
      'condition-index',
      'condition-control--field',
      'condition-control--param',
      'condition-control--operator',
      'condition-control--value',
      'condition-tools',
    ])
    expect(trackRoles(wrapper)).not.toContain('condition-control--rank')
    expect(wrapper.find('.condition-line').classes()).toContain('condition-line--has-param')
  })
})

describe('编辑器：带 rank_mode 的历史条件照旧可用', () => {
  it('改数值/改关系都不会丢掉 rank_mode', async () => {
    const node: ScreeningRuleNode = {
      id: 'root',
      logic: 'AND',
      rules: [legacyCondition()],
    }
    const wrapper = mountEditor(node, (field) => field === 'pe_ttm')
    expect(wrapper.text()).not.toContain('排名口径')

    const row = wrapper.findComponent({ name: 'RuleConditionRow' })
    await row.vm.$emit('update:value', 25)
    await row.vm.$emit('update:op', '>')

    const condition = node.rules[0] as ScreeningRuleCondition
    expect(condition.value).toBe(25)
    expect(condition.op).toBe('>')
    // payload 里这个字段必须还在（后端 expand_rank_modes 依赖它）
    expect(condition.rank_mode).toBe('industry_rank')
    expect(JSON.stringify(node)).toContain('"rank_mode":"industry_rank"')
  })

  it('换成同样可排名的字段：rank_mode 保留（与参数化字段 cagr_years 的同款既有语义）', async () => {
    const node: ScreeningRuleNode = { id: 'root', logic: 'AND', rules: [legacyCondition()] }
    const wrapper = mountEditor(node, (field) => field === 'pe_ttm' || field === 'total_market_cap')
    const row = wrapper.findComponent({ name: 'RuleConditionRow' })

    await row.vm.$emit('update:field', 'total_market_cap')

    const condition = node.rules[0] as ScreeningRuleCondition
    expect(condition.field).toBe('total_market_cap')
    expect(condition.rank_mode).toBe('industry_rank')
  })

  it('换成不支持排名的字段：清掉 rank_mode（否则后端会收到不存在的列名）', async () => {
    const node: ScreeningRuleNode = { id: 'root', logic: 'AND', rules: [legacyCondition()] }
    const wrapper = mountEditor(node, (field) => field === 'pe_ttm')
    const row = wrapper.findComponent({ name: 'RuleConditionRow' })

    await row.vm.$emit('update:field', 'total_market_cap')

    const condition = node.rules[0] as ScreeningRuleCondition
    expect(condition.field).toBe('total_market_cap')
    expect(condition.rank_mode).toBeUndefined()
  })
})

function mountPage() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  return mount(NDialogProvider, {
    slots: {
      default: () => h(NMessageProvider, null, { default: () => h(ScreeningPage) }),
    },
    global: { plugins: [router] },
  })
}

describe('页面级：载入带 rank_mode 的已保存规则 → 运行 → 提交 payload 不丢字段', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // 「切页保状态」把运行结果提到了模块级单例（stores/screening-session.ts）：
    // 一个 it 就是一次新的页面加载（= F5），必须整体重置，否则上一个用例的
    // 结果会漏到下一个用例里，测试变成顺序相关。
    resetScreeningSession()
    localStorage.clear()
    ;(axios.put as Mock).mockResolvedValue({ data: { revision: 1 } })
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/screening/draft') {
        return Promise.resolve({ data: { draft: null, revision: 0 } })
      }
      if (url === '/api/screening/rules') {
        return Promise.resolve({
          data: {
            rules: [{
              id: 7,
              name: '老规则·行业内排名',
              version: 1,
              rule_json: {
                conditions: {
                  logic: 'AND',
                  rules: [{ field: 'pe_ttm', op: '<', value: 20, rank_mode: 'industry_rank' }],
                },
                sort: [],
                columns: ['stock_code', 'pe_ttm'],
              },
              locked_indicators: {},
              status: 'saved',
              created_at: '2026-08-01',
            }],
          },
        })
      }
      if (url === '/api/screening/indicators') {
        // pe_ttm 是**可排名**字段：UI 移除前这里一定会渲染「排名口径」下拉
        return Promise.resolve({
          data: {
            indicators: [{ name: 'pe_ttm', rankable: true }],
            rank_modes: [{ label: '证监会一级排名', value: 'industry_rank' }],
            count: 1,
          },
        })
      }
      if (url === '/api/data-status/summary') {
        return Promise.resolve({
          data: { data_quality: { warning_codes: [], minimum_data_readiness: { ready: true } } },
        })
      }
      if (url === '/api/data-status/auto-update') {
        return Promise.resolve({ data: { state: 'enabled', current_stage: 'finished' } })
      }
      return Promise.resolve({ data: {} })
    })
  })

  it('条件行不显示排名口径；改一个数值再运行，规则 payload 仍带 rank_mode', async () => {
    const put = axios.put as Mock
    put.mockImplementation((url: string) => {
      if (url === '/api/screening/rules/7') {
        return Promise.resolve({ data: { rule_id: 7, version: 2 } })
      }
      return Promise.resolve({ data: {} })
    })
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return Promise.resolve({
          data: {
            results: [{ stock_code: '600519', name: '贵州茅台', pe_ttm: 10 }],
            total: 1, execution_time_ms: 5, base_pool_size: 100,
            data_date: '2026-06-30', run_id: 'run-1',
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const wrapper = mountPage()
    await flushPromises()

    // 载入历史规则（第一个 Select 是规则选择器）
    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    expect(ruleSelect.exists()).toBe(true)
    await ruleSelect.vm.$emit('update:value', 7)
    await flushPromises()
    expect(wrapper.text()).toContain('老规则·行业内排名')

    // ① 条件行渲染出来了，但**没有**「排名口径」
    const rows = wrapper.findAllComponents({ name: 'RuleConditionRow' })
    expect(rows).toHaveLength(1)
    expect(wrapper.text()).toContain('观察指标')
    expect(wrapper.text()).not.toContain('排名口径')
    expect(wrapper.find('[aria-label="排名口径"]').exists()).toBe(false)

    // ② 改一个数值 → 草稿脏了 → 点运行会先**原地更新**这套规则
    //（2026-11-25：不再 POST /rules/save 追加一行同名新版本）
    await rows[0].vm.$emit('update:value', 25)
    await flushPromises()

    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    expect(runButton).toBeTruthy()
    await runButton!.trigger('click')
    await flushPromises()

    // ③ 提交给后端的规则 payload 里 rank_mode 必须还在（数据不丢）
    const updateCall = put.mock.calls.find(([url]) => url === '/api/screening/rules/7')
    expect(updateCall, '没有发出原地更新请求（PUT /rules/7）').toBeTruthy()
    // 也不许再新建同名规则
    expect(
      post.mock.calls.find(([url]) => url === '/api/screening/rules/save'),
      '草稿脏了还在 POST /rules/save 新建规则',
    ).toBeUndefined()
    const payload = updateCall![1] as {
      rule_json: { conditions: { rules: Array<Record<string, unknown>> } }
      expected_version: number
    }
    const submitted = payload.rule_json.conditions.rules[0]
    expect(submitted).toMatchObject({
      field: 'pe_ttm',
      op: '<',
      value: 25,
      rank_mode: 'industry_rank',
    })
    // 乐观并发：带上"我编辑的是哪一版"
    expect(payload.expected_version).toBe(1)
  })
})
