import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider } from 'naive-ui'
import { h } from 'vue'
import ScreeningResultsPanel from '../../src/components/ScreeningResultsPanel.vue'

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

const RESULT_ROWS = [
  { stock_code: '600519', name: '贵州茅台', pe_ttm: 10, roe: 0.3, debt_ratio: 0.2 },
  { stock_code: '000858', name: '五粮液', pe_ttm: 8, roe: 0.25, debt_ratio: 0.3 },
]

function mountPanel() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  const wrapper = mount(NMessageProvider, {
    slots: {
      default: () =>
        h(ScreeningResultsPanel, {
          results: RESULT_ROWS,
          strictOnly: false,
          executionTime: 5,
          basePoolSize: 100,
          dataDate: '2026-06-30',
          warningCodes: [],
          untrustedFields: [],
          qualityStatus: 'available',
          ruleTree: { id: 'r1', logic: 'AND', rules: [{ field: 'pe_ttm', op: '>', value: 0 }] },
          runId: 'run-1',
          ruleId: 1,
           ruleVersion: 1,
           ruleName: '优质价值候选池',
          lockedIndicators: {},
          sort: [{ field: 'pe_ttm', direction: 'asc' }],
          basePoolConfig: { include_st: false },
        }),
    },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

describe('ScreeningResultsPanel 持久化流程（PRD §12 SC14/SC16/SC17）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/save') {
        return Promise.resolve({ data: { status: 'ok', result_id: 7 } })
      }
      if (url === '/api/screening/export_csv') {
        return Promise.resolve({ data: { csv: 'stock_code\n600519', rows: 2 } })
      }
      if (url === '/api/screening/add_to_watchlist') {
        return Promise.resolve({ data: { status: 'ok', added: 2 } })
      }
      return Promise.resolve({ data: {} })
    })
    // 导出流程触发 Blob 下载；jsdom 无 URL.createObjectURL
    vi.stubGlobal('URL', { ...URL, createObjectURL: () => 'blob:mock', revokeObjectURL: () => {} })
  })

  it('保存 → 导出 → 加入自选 完整链路', async () => {
    const post = axios.post as Mock
    const { wrapper } = mountPanel()
    await flushPromises()

    expect(wrapper.text()).toContain('600519')
    expect(wrapper.text()).toContain('贵州茅台')

    // 1) 保存结果（打开对话框并输入标题）
    const saveBtn = wrapper.find('#save-btn')
    expect(saveBtn.exists()).toBe(true)
    await saveBtn.trigger('click')
    await flushPromises()

    const titleInput = document.body.querySelector('input[placeholder="给这次筛选结果起个名字"]') as HTMLInputElement | null
    expect(titleInput).toBeTruthy()
    titleInput!.value = '测试保存'
    titleInput!.dispatchEvent(new Event('input'))
    await flushPromises()

    const confirmSave = Array.from(document.body.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('保存'),
    )
    expect(confirmSave).toBeTruthy()
    confirmSave!.click()
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/screening/save',
      expect.objectContaining({ title: '测试保存', run_id: 'run-1' }),
    )

    // 2) 导出 CSV（依赖已保存的 result_id）
    await wrapper.find('#export-btn').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/screening/export_csv', { result_id: 7 })

    // 3) 加入自选（使用筛选结果中的股票代码）
    await wrapper.find('#watchlist-btn').trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith(
      '/api/screening/add_to_watchlist',
      expect.objectContaining({
        stock_codes: ['600519', '000858'],
        group: '优质价值候选池',
        result_id: 7,
      }),
    )
  })

  it('可直接保存到自选列表：无需先手动保存结果', async () => {
    const post = axios.post as Mock
    const { wrapper } = mountPanel()
    await flushPromises()

    await wrapper.find('#watchlist-btn').trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/screening/save',
      expect.objectContaining({ run_id: 'run-1' }),
    )
    expect(post).toHaveBeenCalledWith(
      '/api/screening/add_to_watchlist',
      expect.objectContaining({
        result_id: 7,
        stock_codes: ['600519', '000858'],
      }),
    )
  })

  it('质量状态未知时禁用保存与导出（fail-closed UI）', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [] })
    const wrapper = mount(NMessageProvider, {
      slots: {
        default: () =>
          h(ScreeningResultsPanel, {
            results: RESULT_ROWS,
            strictOnly: false,
            executionTime: 5,
            basePoolSize: 100,
            dataDate: '2026-06-30',
            warningCodes: ['MINIMUM_DATA_NOT_READY'],
            untrustedFields: ['pe_ttm'],
            qualityStatus: 'failed',
            ruleTree: { id: 'r1', logic: 'AND', rules: [] },
            runId: 'run-1',
            ruleId: 1,
            ruleVersion: 1,
            lockedIndicators: {},
            sort: [],
            basePoolConfig: {},
          }),
      },
      global: { plugins: [router] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('无法获取数据质量状态')
    expect(wrapper.text()).toContain('保存和导出功能已禁用')
    expect((wrapper.find('#save-btn').element as HTMLButtonElement).disabled).toBe(true)
    expect((wrapper.find('#export-btn').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('结果超过 5000 条时显示截断警告（P1-C）', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [] })
    const wrapper = mount(NMessageProvider, {
      slots: {
        default: () =>
          h(ScreeningResultsPanel, {
            results: RESULT_ROWS,
            strictOnly: false,
            executionTime: 5,
            basePoolSize: 6000,
            dataDate: '2026-06-30',
            warningCodes: [],
            untrustedFields: [],
            qualityStatus: 'available',
            ruleTree: { id: 'r1', logic: 'AND', rules: [] },
            runId: 'run-1',
            ruleId: 1,
            ruleVersion: 1,
            lockedIndicators: {},
            sort: [],
            basePoolConfig: {},
            truncated: true,
            totalMatched: 6000,
          }),
      },
      global: { plugins: [router] },
    })
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('结果已截断')
    expect(text).toContain('6000')
    expect(text).toContain('前 5000 条')
  })
})
