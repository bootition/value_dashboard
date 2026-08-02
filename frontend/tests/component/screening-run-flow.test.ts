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

describe('ScreeningPage 运行筛选流程（PRD §12 SC8）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/screening/draft') {
        return Promise.resolve({ data: { draft: null, revision: 0 } })
      }
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
        return Promise.resolve({
          data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 },
        })
      }
      if (url === '/api/data-status/summary') {
        return Promise.resolve({ data: { data_quality: { warning_codes: [] } } })
      }
      return Promise.resolve({ data: {} })
    })
  })

  it('挂载后恢复草稿并加载规则，选择规则后可运行筛选', async () => {
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

    const { wrapper } = mountScreeningPage()
    await flushPromises()

    expect(axios.get).toHaveBeenCalledWith('/api/screening/draft')
    expect(axios.get).toHaveBeenCalledWith('/api/screening/rules')

    // 选择已保存规则（直接触发 Select 的 update:value → loadRule）
    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    expect(ruleSelect.exists()).toBe(true)
    await ruleSelect.vm.$emit('update:value', 1)
    await flushPromises()
    expect(wrapper.text()).toContain('test-rule v1')

    // 点击"运行筛选"
    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    expect(runButton).toBeTruthy()
    await runButton!.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/screening/run',
      expect.objectContaining({ rule_id: 1, rule_version: 1, min_listing_years: 1 }),
    )
    expect(wrapper.text()).toContain('600519')
    expect(wrapper.text()).toContain('贵州茅台')
  })
})
