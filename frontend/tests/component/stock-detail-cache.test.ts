/**
 * 个股详情页「转个小圈才显示」的回归（2026-11-20）。
 *
 * 用户原话：「我从筛选页点到数据详情页，**数据详情页刷新了才显示**，虽然很快，
 * 但它也是**转了个小圈**之后才显示。」
 *
 * 根因不是"没缓存"，而是**渲染时序被加载态绑死**：
 * `useStockDetailLoad.fetchAll()` 每次挂载都 `loading.value = true`，模板又把整页
 * 包在 `<n-spin v-else :show="loading">` 里 → 5 路请求全部落地前，页面主体一律不可见。
 *
 * 修法：stale-while-revalidate —— 挂载时同步回填上一次成功响应（`loading` 保持
 * false），后台照旧刷新；只有"这只股票从来没成功过"才转圈。缓存必须带 `as_of` 标注。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { h, nextTick } from 'vue'
import StockDetailPage from '../../src/views/StockDetailPage.vue'
import {
  resetStockDetailCache,
  writeStockDetailCache,
} from '../../src/composables/useStockDetailCache.ts'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
    isCancel: () => false,
  }
  return { default: mock, isAxiosError: mock.isAxiosError, isCancel: mock.isCancel }
})

import axios from 'axios'
import type { Mock } from 'vitest'

const CACHED_INDICATORS = {
  report_date: '2026-06-30',
  latest_close: 1680.5,
  latest_price_date: '2026-10-31',
  freshness: { stale_warning: false, price_age_days: 1, financial_age_days: 30, snapshot_age_days: 1 },
  trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
  indicators: {
    valuation: { pe_ttm: { value: 25.4, historical_capable: false } },
    profitability: { roe: { value: 0.32, historical_capable: true } },
    growth: {},
    safety: {},
    shareholder_return: {},
  },
}

const CACHED_INFO = { stock_code: '600519', name: '贵州茅台', exchange: 'SSE' }
const CACHED_OVERVIEW = {
  stock_code: '600519',
  profile: { status: 'missing' },
  breakdown: { status: 'missing' },
}

async function mountDetail(code = '600519') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/stock', name: 'stock-search', component: { template: '<div />' } },
      { path: '/stock/:code', name: 'stock-detail', component: { template: '<div />' } },
      { path: '/screening', name: 'screening', component: { template: '<div />' } },
      { path: '/blacklist', name: 'blacklist', component: { template: '<div />' } },
      { path: '/data-status', name: 'data-status', component: { template: '<div />' } },
      { path: '/watchlist', name: 'watchlist', component: { template: '<div />' } },
    ],
  })
  // 先把路由推到详情页再挂载：这样 `useRoute()` 在 setup 里就能拿到 code
  await router.push(`/stock/${code}`)
  await router.isReady()
  const wrapper = mount(NDialogProvider, {
    slots: {
      default: () => h(NMessageProvider, null, { default: () => h(StockDetailPage) }),
    },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

function mockApis() {
  const get = axios.get as Mock
  get.mockImplementation((url: string) => {
    if (url.startsWith('/api/blacklist/check/')) return Promise.resolve({ data: { blacklisted: false } })
    if (/^\/api\/stock\/[^/]+\/info$/.test(url)) return Promise.resolve({ data: CACHED_INFO })
    if (/^\/api\/stock\/[^/]+\/indicators$/.test(url)) return Promise.resolve({ data: CACHED_INDICATORS })
    if (/^\/api\/stock\/[^/]+\/kline$/.test(url)) return Promise.resolve({ data: { candles: [] } })
    if (/^\/api\/stock\/[^/]+\/business-overview$/.test(url)) {
      return Promise.resolve({ data: CACHED_OVERVIEW })
    }
    if (url === '/api/data-status/summary') {
      return Promise.resolve({ data: { data_quality: { warning_codes: [] } } })
    }
    return Promise.resolve({ data: {} })
  })
  ;(axios.post as Mock).mockResolvedValue({ data: {} })
  ;(axios.delete as Mock).mockResolvedValue({ data: {} })
}

describe('详情页 stale-while-revalidate（切回来不该再转圈）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    resetStockDetailCache()
    mockApis()
  })

  it('没有缓存（第一次进入）：仍然按原行为转圈加载', async () => {
    const { wrapper } = await mountDetail()
    await nextTick()
    // 首帧：加载中
    expect(wrapper.find('.stock-detail-page').attributes('data-detail-loading')).toBe('1')
    await flushPromises()
    expect(wrapper.find('.stock-detail-page').attributes('data-detail-loading')).toBe('0')
    expect(wrapper.find('#detail-cache-note').exists()).toBe(false)
    wrapper.unmount()
  })

  it('有缓存（刚看过同一只股票）：首帧就有内容、不转圈、后台照旧 revalidate', async () => {
    // 上一次成功响应
    writeStockDetailCache('600519', 'info', CACHED_INFO, null)
    writeStockDetailCache('600519', 'indicators', CACHED_INDICATORS, '2026-10-31')
    writeStockDetailCache('600519', 'business-overview', CACHED_OVERVIEW, null)
    writeStockDetailCache('600519', 'kline:day:raw', { candles: [] }, '2026-10-31')

    const { wrapper } = await mountDetail()
    // 关键断言：**不用等任何 await**，首帧就已经不是加载态
    expect(wrapper.find('.stock-detail-page').attributes('data-detail-loading')).toBe('0')
    expect(wrapper.find('.stock-detail-page').attributes('data-detail-cached')).toBe('1')
    // 缓存内容已经在页面上（股票名来自 info 缓存）
    expect(wrapper.text()).toContain('贵州茅台')

    // as_of 标注必须跟着缓存走（不许把旧值冒充新数据）
    const note = wrapper.find('#detail-cache-note')
    expect(note.exists()).toBe(true)
    expect(note.text()).toContain('数据截至 2026-10-31')
    expect(note.text()).toContain('后台刷新')

    await flushPromises()

    // 后台 revalidate 照旧打了请求
    const get = axios.get as Mock
    const urls = get.mock.calls.map(([url]) => String(url))
    expect(urls.some((u) => u.endsWith('/indicators'))).toBe(true)
    expect(urls.some((u) => u.endsWith('/info'))).toBe(true)
    // 刷新落地后标注消失（内容已是新的）
    expect(wrapper.find('#detail-cache-note').exists()).toBe(false)
    wrapper.unmount()
  })

  it('后台刷新失败：已显示的内容不被清空（旧快照仍然可读）', async () => {
    writeStockDetailCache('600519', 'info', CACHED_INFO, null)
    writeStockDetailCache('600519', 'indicators', CACHED_INDICATORS, '2026-10-31')
    writeStockDetailCache('600519', 'business-overview', CACHED_OVERVIEW, null)
    writeStockDetailCache('600519', 'kline:day:raw', { candles: [] }, '2026-10-31')
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url.startsWith('/api/blacklist/check/')) return Promise.resolve({ data: { blacklisted: false } })
      return Promise.reject({ isAxiosError: true, code: 'ERR_NETWORK', message: 'Network Error' })
    })

    const { wrapper } = await mountDetail()
    await flushPromises()

    expect(wrapper.text()).toContain('贵州茅台')
    expect(wrapper.find('.stock-detail-page').attributes('data-detail-loading')).toBe('0')
    wrapper.unmount()
  })
})
