/**
 * 个股详情页的"正在更新，先看上次数据"提示（用户原话：
 * "点到数据详情界面后，没有进度条，根本看不到正在做什么"）。
 *
 * 关键约束：
 * 1. 更新进度由 App.vue 布局层唯一那份轮询通过 provide/inject 共享，
 *    详情页**不得**自己再 polling `/api/data-status/auto-update`；
 * 2. 提示是页内说明条，不是遮罩：页面的读功能（内容/按钮）必须照常可用。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { h, nextTick, ref } from 'vue'
import StockDetailPage from '../../src/views/StockDetailPage.vue'
import { AUTO_UPDATE_STATUS_KEY } from '../../src/composables/useAutoUpdatePolling.ts'

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

const STOCK_INFO = { stock_code: '600519', name: '贵州茅台', exchange: 'SSE' }
const INDICATORS = {
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

const RUNNING_UPDATE = {
  state: 'enabled',
  enabled: true,
  paused: false,
  current_stage: 'running',
  progress: {
    phase: 'step:indicators',
    job_id: 'job-1',
    started_at: new Date(Date.now() - 125000).toISOString(),
    live: {
      step: 'indicators',
      done: 2500,
      total: 5575,
      current: '000672',
      eta_seconds: 848,
    },
  },
  last_error: null,
  latest_job: { id: 256, status: 'running' },
}

async function mountDetail(status?: unknown) {
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
  await router.push('/stock/600519')
  await router.isReady()
  const provide = status === undefined
    ? {}
    : { [AUTO_UPDATE_STATUS_KEY]: ref(status) }
  const wrapper = mount(NDialogProvider, {
    slots: {
      default: () => h(NMessageProvider, null, { default: () => h(StockDetailPage) }),
    },
    global: { plugins: [router], provide },
  })
  return { wrapper, router }
}

function mockApis() {
  const get = axios.get as Mock
  get.mockImplementation((url: string) => {
    if (url.startsWith('/api/blacklist/check/')) return Promise.resolve({ data: { blacklisted: false } })
    if (/^\/api\/stock\/[^/]+\/info$/.test(url)) return Promise.resolve({ data: STOCK_INFO })
    if (/^\/api\/stock\/[^/]+\/indicators$/.test(url)) return Promise.resolve({ data: INDICATORS })
    if (/^\/api\/stock\/[^/]+\/kline$/.test(url)) return Promise.resolve({ data: { candles: [] } })
    if (/^\/api\/stock\/[^/]+\/business-overview$/.test(url)) {
      return Promise.resolve({ data: { stock_code: '600519', profile: { status: 'missing' }, breakdown: { status: 'missing' } } })
    }
    if (url === '/api/data-status/summary') {
      return Promise.resolve({ data: { data_quality: { warning_codes: [] } } })
    }
    return Promise.resolve({ data: {} })
  })
  ;(axios.post as Mock).mockResolvedValue({ data: {} })
  ;(axios.delete as Mock).mockResolvedValue({ data: {} })
}

describe('个股详情页的更新中提示（可见 + 不阻塞）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApis()
  })

  it('更新中：显示中文阶段 + 已完成/总数 + 百分比 + "上次成功加载的内容"，页面内容仍可用', async () => {
    const { wrapper } = await mountDetail(RUNNING_UPDATE)
    await flushPromises()
    await nextTick()

    const note = wrapper.find('#detail-update-note')
    expect(note.exists()).toBe(true)
    expect(note.attributes('data-update-running')).toBe('1')
    const text = note.text()
    expect(text).toContain('正在重算指标快照')
    expect(text).toContain('2500 / 5575')
    expect(text).toContain('45%')
    expect(text).toContain('上次成功加载的内容')
    expect(text).toContain('已耗时')

    // 读功能可用：内容已渲染、不是加载态、没有遮罩
    expect(wrapper.text()).toContain('贵州茅台')
    expect(wrapper.find('.stock-detail-page').attributes('data-detail-loading')).toBe('0')
    expect(wrapper.find('.n-spin-mask').exists()).toBe(false)
    expect(wrapper.find('#overview').exists()).toBe(true)
    expect(wrapper.text()).toContain('加入黑名单')

    // 详情页不自己再开第二个轮询器：共享 App provide 的状态
    const urls = (axios.get as Mock).mock.calls.map(([url]) => String(url))
    expect(urls.filter((u) => u === '/api/data-status/auto-update')).toHaveLength(0)

    wrapper.unmount()
  })

  it('空闲：没有更新中提示', async () => {
    const { wrapper } = await mountDetail({
      state: 'enabled',
      current_stage: 'finished',
      progress: { phase: 'done', status: 'success' },
      last_error: null,
    })
    await flushPromises()
    expect(wrapper.find('#detail-update-note').exists()).toBe(false)
    expect(wrapper.text()).toContain('贵州茅台')
    wrapper.unmount()
  })

  it('没有注入状态（独立挂载）时不崩、不显示进度提示', async () => {
    const { wrapper } = await mountDetail()
    await flushPromises()
    expect(wrapper.find('#detail-update-note').exists()).toBe(false)
    expect(wrapper.text()).toContain('贵州茅台')
    wrapper.unmount()
  })
})
