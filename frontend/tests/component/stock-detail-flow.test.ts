import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider } from 'naive-ui'
import { h } from 'vue'
import StockDetailPage from '../../src/views/StockDetailPage.vue'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
    isCancel: () => false,
  }
  return { default: mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'

const INDICATORS_UNTRUSTED = {
  report_date: '2026-06-30',
  latest_close: null,
  latest_price_date: '2026-07-31',
  freshness: { stale_warning: false, price_age_days: 1, financial_age_days: 30, snapshot_age_days: 1 },
  trust: { warning_codes: ['LINEAGE_INVALID'], untrusted_all: true, untrusted_fields: [] },
  indicators: {
    valuation: { pe_ttm: { value: null, historical_capable: false, untrusted: true }, pb_mrq: { value: null, historical_capable: false, untrusted: true } },
    profitability: { roe: { value: null, historical_capable: true, untrusted: true } },
    safety: { debt_ratio: { value: null, historical_capable: true, untrusted: true } },
  },
}

function mountDetail() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  const wrapper = mount(NMessageProvider, {
    slots: { default: () => h(StockDetailPage) },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

async function mountDetailWithCode(code: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/stock/:code', component: { template: '<div/>' } }],
  })
  const wrapper = mount(NMessageProvider, {
    slots: { default: () => h(StockDetailPage) },
    global: { plugins: [router] },
  })
  await router.push(`/stock/${code}`)
  await router.isReady()
  return { wrapper, router }
}

describe('StockDetailPage 不可信指标统一警示（reports/27 P1-8）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url.includes('/info')) {
        return Promise.resolve({ data: { stock_code: '600519', name: '贵州茅台', exchange: 'SSE', latest_close: null, latest_price_date: '2026-07-31' } })
      }
      if (url.includes('/indicators')) {
        return Promise.resolve({ data: INDICATORS_UNTRUSTED })
      }
      if (url.includes('/kline')) {
        return Promise.resolve({ data: { candles: [], adjust: 'raw', count: 0 } })
      }
      if (url.includes('/financial-trend')) {
        return Promise.resolve({ data: { trend: [], period: 'annual', count: 0 } })
      }
      if (url.includes('/source-audit')) {
        return Promise.resolve({ data: { field_audit: [], batch_audit: [] } })
      }
      return Promise.resolve({ data: {} })
    })
  })

  it('LINEAGE_INVALID 时显示全局告警且指标不渲染普通数值', async () => {
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const text = wrapper.text()
    // 全局 fail-closed 告警
    expect(text).toContain('当前数据库状态不可信')
    // 指标卡片的"数据不可信"标记（非普通数值）
    expect(text).toContain('数据不可信')
  })
})
