/**
 * 结果表 localStorage 旧列配置迁移（2026-11-24 用户实测）。
 *
 * 根因：结果面板还有一份独立的 `vd.screening.columns`。旧版本把 8 列旧默认
 * 写进 localStorage 后，即使 `/run` 已经返回 PE/ROCE，面板也会把新列过滤掉，
 * 用户就只看到股票代码/证监会行业。这里钉死两条：
 *   1. 旧默认集合 → 丢弃，展示本次返回的全部列；
 *   2. 用户真的自定义过的列 → 继续尊重。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider } from 'naive-ui'
import { h } from 'vue'
import ScreeningResultsPanel from '../../src/components/ScreeningResultsPanel.vue'
import type { ScreeningResult } from '../../src/types/screening.ts'

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

const ROW = {
  stock_code: '600519',
  name: '贵州茅台',
  exchange: 'SSE',
  listing_date: '2001-08-27',
  csrc_l1: '制造业',
  csrc_l2: '酒、饮料和精制茶制造业',
  latest_close: 1500,
  total_market_cap: 1.8e12,
  pe_ttm: 20,
  pb_mrq: 8,
  ttm_dividend_yield: 2.5,
  circ_market_cap: 1.8e12,
  roe: 0.3,
  roce_10y_median: 0.4,
  gross_margin: 0.9,
  net_margin: 0.5,
  debt_ratio: 0.2,
  revenue_yoy: 0.15,
  net_profit_yoy: 0.18,
} as unknown as ScreeningResult

function mountPanel() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  return mount(NMessageProvider, {
    slots: {
      default: () => h(ScreeningResultsPanel, {
        results: [ROW],
        strictOnly: false,
        executionTime: 5,
        basePoolSize: 100,
        dataDate: '2026-06-30',
        warningCodes: [],
        untrustedFields: [],
        qualityStatus: 'available',
        ruleTree: { id: 'r1', logic: 'AND', rules: [] },
        runId: 'run-columns',
        ruleId: 1,
        ruleVersion: 1,
        lockedIndicators: {},
        basePoolConfig: {},
      }),
    },
    global: { plugins: [router] },
  })
}

describe('结果表面板：旧 localStorage 列配置不得吞掉新列', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('旧 8 列默认集合 → 展示本次返回的 PE/ROCE 等全部列', async () => {
    localStorage.setItem('vd.screening.columns', JSON.stringify([
      'stock_code', 'name', 'exchange', 'listing_date',
      'csrc_l1', 'csrc_l2', 'latest_close', 'total_market_cap',
    ]))
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.find('th[data-col-key="pe_ttm"]').exists()).toBe(true)
    expect(wrapper.find('th[data-col-key="pb_mrq"]').exists()).toBe(true)
    expect(wrapper.find('th[data-col-key="roce_10y_median"]').exists()).toBe(true)
    expect(wrapper.find('th[data-col-key="ttm_dividend_yield"]').text()).toContain('股息率')
  })

  it('用户自定义的旧列配置仍被尊重，不强行加回', async () => {
    localStorage.setItem('vd.screening.columns', JSON.stringify(['stock_code', 'pe_ttm']))
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.find('th[data-col-key="stock_code"]').exists()).toBe(true)
    expect(wrapper.find('th[data-col-key="pe_ttm"]').exists()).toBe(true)
    expect(wrapper.find('th[data-col-key="roce_10y_median"]').exists()).toBe(false)
  })
})
