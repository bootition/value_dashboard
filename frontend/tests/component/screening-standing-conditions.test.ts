import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { h } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import ScreeningPage from '../../src/views/ScreeningPage.vue'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
  }
  return { default: mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'

function makeRouter() {
  return createRouter({ history: createMemoryHistory(), routes: [] })
}

function mountScreeningPage() {
  const router = makeRouter()
  const wrapper = mount(NDialogProvider, {
    slots: {
      default: () =>
        h(NMessageProvider, null, { default: () => h(ScreeningPage) }),
    },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

describe('ScreeningPage 常驻范围条件（范围并入筛选条件，去掉滑动开关）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/screening/draft') {
        return Promise.resolve({ data: { draft: null, revision: 0 } })
      }
      if (url === '/api/screening/rules') {
        return Promise.resolve({ data: { rules: [] } })
      }
      if (url === '/api/screening/indicators') {
        return Promise.resolve({ data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 } })
      }
      if (url === '/api/data-status/summary') {
        return Promise.resolve({ data: { data_quality: { warning_codes: [], minimum_data_readiness: { ready: true } } } })
      }
      return Promise.resolve({ data: {} })
    })
    const post = axios.post as Mock
    post.mockImplementation(() => Promise.resolve({ data: {} }))
  })

  it('将 ST、停牌与最低上市年限作为常驻条件并入筛选条件区域', async () => {
    const { wrapper } = mountScreeningPage()
    await flushPromises()

    expect(wrapper.text()).toContain('常驻范围条件')
    expect(wrapper.text()).toContain('ST 股票')
    expect(wrapper.text()).toContain('停牌股票')
    expect(wrapper.text()).toContain('最低上市年限')
    expect(wrapper.text()).toContain('始终并且')
    expect(wrapper.text()).not.toContain('先确定研究范围')
  })

  it('常驻范围条件不再使用左右滑动开关', async () => {
    const { wrapper } = mountScreeningPage()
    await flushPromises()

    const standing = wrapper.find('.standing-conditions')
    expect(standing.exists()).toBe(true)
    expect(standing.findAll('input[type="checkbox"]')).toHaveLength(0)
    expect(standing.findAll('.n-switch')).toHaveLength(0)
  })

  it('运行筛选时范围仍按布尔参数传给后端，契约不变', async () => {
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
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/screening/draft') return Promise.resolve({ data: { draft: null, revision: 0 } })
      if (url === '/api/screening/rules') {
        return Promise.resolve({
          data: {
            rules: [{
              id: 1, name: 'test-rule', version: 1,
              rule_json: {
                conditions: { logic: 'AND', rules: [{ field: 'pe_ttm', op: '>', value: 0 }] },
                sort: [{ field: 'pe_ttm', direction: 'asc' }],
                columns: ['stock_code', 'pe_ttm'],
              },
              locked_indicators: {}, status: 'saved', created_at: '2026-08-01',
            }],
          },
        })
      }
      if (url === '/api/screening/indicators') {
        return Promise.resolve({ data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 } })
      }
      if (url === '/api/data-status/summary') {
        return Promise.resolve({ data: { data_quality: { warning_codes: [], minimum_data_readiness: { ready: true } } } })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()

    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    await ruleSelect.vm.$emit('update:value', 1)
    await flushPromises()

    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    expect(runButton).toBeTruthy()
    await runButton!.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/screening/run',
      expect.objectContaining({
        rule_id: 1,
        rule_version: 1,
        include_st: false,
        include_suspended: false,
        min_listing_years: 1,
      }),
    )
  })
})
