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
import type { ScreeningResult } from '../../src/types/screening.ts'

// 2026-11-23：显式按 ScreeningResult 标注（面板 props 就是 readonly ScreeningResult[]）。
// 旧 fixture 只写了 5 列，缺 exchange/csrc_l1/latest_close/pb_mrq/gross_margin/
// net_margin/revenue_yoy/dividend_yield —— 这些是后端 /api/screening/run 每条结果必带的
// 元数据列（types/screening.ts ScreeningResultKnownFields），缺了会让「列 → 中文本地化」
// 的覆盖被高估。这里补齐为一条完整的结果行，取值只求类型正确、不参与任何断言。
const RESULT_ROWS: ScreeningResult[] = [
  {
    stock_code: '600519',
    name: '贵州茅台',
    exchange: 'SSE',
    csrc_l1: '制造业',
    latest_close: 1680.5,
    pe_ttm: 10,
    pb_mrq: 8.2,
    roe: 0.3,
    gross_margin: 0.91,
    net_margin: 0.52,
    debt_ratio: 0.2,
    revenue_yoy: 0.15,
    dividend_yield: 0.023,
  },
  {
    stock_code: '000858',
    name: '五粮液',
    exchange: 'SZSE',
    csrc_l1: '制造业',
    latest_close: 140.2,
    pe_ttm: 8,
    pb_mrq: 4.1,
    roe: 0.25,
    gross_margin: 0.75,
    net_margin: 0.36,
    debt_ratio: 0.3,
    revenue_yoy: 0.1,
    dividend_yield: 0.031,
  },
]

function mountPanel() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  const wrapper = mount(NMessageProvider, {
    slots: {
      default: () =>
        h(ScreeningResultsPanel, {
          results: RESULT_ROWS,
          executionTime: 5,
          basePoolSize: 100,
          dataDate: '2026-06-30',
          warningCodes: [],
          untrustedFields: [],
          qualityStatus: 'available',
          ruleTree: { id: 'r1', logic: 'AND', rules: [{ id: 'c1', field: 'pe_ttm', op: '>', value: 0 }] },
          runId: 'run-1',
          ruleId: 1,
           ruleVersion: 1,
           ruleName: '优质价值候选池',
          lockedIndicators: {},
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
    // 2026-11-16（reports/148）：导出请求额外携带 column_labels，
    // 让 CSV 表头是中文（PRD §12.1）。断言放宽为「带上 result_id + 中文标签映射」。
    const exportCall = post.mock.calls.find(([url]) => url === '/api/screening/export_csv')
    expect(exportCall).toBeTruthy()
    const exportPayload = exportCall![1] as {
      result_id: number
      column_labels: Record<string, string>
    }
    expect(exportPayload.result_id).toBe(7)
    // 每个列都要有中文标签，且不能退化成原始英文键
    for (const [key, label] of Object.entries(exportPayload.column_labels)) {
      expect(label, `${key} 的表头标签为空`).toBeTruthy()
      expect(label).toMatch(/[一-鿿]/)
    }

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
