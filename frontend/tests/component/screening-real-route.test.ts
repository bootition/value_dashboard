/**
 * 真实路由链路上的「切页保状态」复现（2026-11-24 第二轮，用户仍报"结果还是没了"）。
 *
 * 上一轮的单测（`screening-page-state.test.ts`）是**直接 mount/unmount ScreeningPage**，
 * 从来没有经过 `<router-view />` 与真实的个股详情页。本文件把链路补全：
 *
 *   挂载 App 外壳（NConfigProvider + NMessageProvider + NDialogProvider + <router-view/>）
 *   → router.push('/screening') 跑出结果
 *   → 点结果表里的股票链接（真实 RouterLink）跳到 /stock/600519（真实 StockDetailPage）
 *   → 点侧边栏「筛选」（真实 RouterLink）回到 /screening
 *   → 断言结果还在、且没有发出新的 /run、也没有被清成 idle
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NConfigProvider, NDialogProvider, NMessageProvider } from 'naive-ui'
import { defineComponent, h } from 'vue'
import ScreeningPage from '../../src/views/ScreeningPage.vue'
import StockDetailPage from '../../src/views/StockDetailPage.vue'
import ScreeningResultsPanel from '../../src/components/ScreeningResultsPanel.vue'
import {
  resetScreeningSession,
  screeningSession,
} from '../../src/stores/screening-session.ts'

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

const RULES = [{
  id: 7,
  name: '低估值·行业龙头',
  version: 3,
  rule_json: {
    conditions: { logic: 'AND', rules: [{ field: 'pe_ttm', op: '<', value: 15 }] },
    columns: ['stock_code', 'pe_ttm'],
  },
  locked_indicators: {},
  status: 'saved',
  created_at: '2026-11-01',
}]

const RESULT_ROWS = [
  { stock_code: '600519', name: '贵州茅台', exchange: 'SSE', csrc_l1: '制造业', latest_close: 1680.5 },
  { stock_code: '000858', name: '五粮液', exchange: 'SZSE', csrc_l1: '制造业', latest_close: 140.2 },
  { stock_code: '601318', name: '中国平安', exchange: 'SSE', csrc_l1: '金融业', latest_close: 55.1 },
]

const RUN_OK = {
  results: RESULT_ROWS,
  total: 3,
  execution_time_ms: 42,
  base_pool_size: 4800,
  data_date: '2026-09-30',
  run_id: 'run-abc',
}

const AppShell = defineComponent({
  name: 'AppShell',
  template: '<router-view />',
})

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', redirect: '/screening' },
      { path: '/screening', name: 'screening', component: ScreeningPage },
      { path: '/stock/:code', name: 'stock-detail', component: StockDetailPage },
      { path: '/data-status', name: 'data-status', component: { template: '<div>数据状态</div>' } },
    ],
  })
}

function mountShell() {
  const router = makeRouter()
  const wrapper = mount(NConfigProvider, {
    slots: {
      default: () => h(NDialogProvider, null, {
        default: () => h(NMessageProvider, null, {
          default: () => h(AppShell),
        }),
      }),
    },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

function mockApis() {
  const get = axios.get as Mock
  get.mockImplementation((url: string) => {
    if (url === '/api/screening/draft') {
      return Promise.resolve({ data: { draft: null, revision: 3 } })
    }
    if (url === '/api/screening/rules') return Promise.resolve({ data: { rules: RULES } })
    if (url === '/api/screening/indicators') {
      return Promise.resolve({ data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 } })
    }
    if (url === '/api/data-status/summary') {
      return Promise.resolve({
        data: {
          data_quality: {
            warning_codes: ['LINEAGE_INVALID'],
            minimum_data_readiness: { ready: true },
          },
        },
      })
    }
    if (url === '/api/data-status/auto-update') {
      return Promise.resolve({ data: { state: 'enabled', current_stage: 'finished' } })
    }
    // ─── 个股详情页的 5 路 ───
    if (url.startsWith('/api/blacklist/check/')) {
      return Promise.resolve({ data: { blacklisted: false } })
    }
    if (/^\/api\/stock\/[^/]+\/info$/.test(url)) {
      return Promise.resolve({ data: { stock_code: '600519', name: '贵州茅台' } })
    }
    if (/^\/api\/stock\/[^/]+\/indicators$/.test(url)) {
      return Promise.resolve({
        data: {
          report_date: '2026-09-30',
          latest_close: 1680.5,
          latest_price_date: '2026-10-31',
          freshness: { stale_warning: false, price_age_days: 1, financial_age_days: 30, snapshot_age_days: 1 },
          trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
          indicators: { valuation: {}, profitability: {}, growth: {}, safety: {}, shareholder_return: {} },
        },
      })
    }
    if (/^\/api\/stock\/[^/]+\/kline$/.test(url)) {
      return Promise.resolve({ data: { candles: [] } })
    }
    if (/^\/api\/stock\/[^/]+\/business-overview$/.test(url)) {
      return Promise.resolve({ data: { stock_code: '600519', profile: { status: 'missing' }, breakdown: { status: 'missing' } } })
    }
    return Promise.resolve({ data: {} })
  })
  const post = axios.post as Mock
  post.mockImplementation((url: string) => {
    if (url === '/api/screening/run') return Promise.resolve({ data: { ...RUN_OK } })
    return Promise.resolve({ data: {} })
  })
  return { get, post }
}

function runCalls(post: Mock): number {
  return post.mock.calls.filter((call) => call[0] === '/api/screening/run').length
}

async function waitFor(fn: () => boolean, timeout = 2000): Promise<void> {
  const start = Date.now()
  while (!fn()) {
    if (Date.now() - start > timeout) throw new Error('waitFor 超时')
    await flushPromises()
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
}

describe('真实路由：筛选 → 个股详情 → 筛选', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    resetScreeningSession()
    ;(axios.put as Mock).mockResolvedValue({ data: { revision: 4 } })
    ;(axios.delete as Mock).mockResolvedValue({ data: {} })
  })

  afterEach(() => {
    resetScreeningSession()
  })

  it('跑出结果 → 跳个股详情页 → 点侧边栏回来：结果还在，且没有新的 /run', async () => {
    const { post } = mockApis()
    const { wrapper, router } = mountShell()

    await router.push('/screening')
    await waitFor(() => wrapper.text().includes('运行筛选'))
    await flushPromises()

    // 选已保存规则（与线上一致：下拉选择 → loadRule）
    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    await ruleSelect.vm.$emit('update:value', 7)
    await flushPromises()

    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    expect(runButton).toBeTruthy()
    await runButton!.trigger('click')
    await flushPromises()

    expect(runCalls(post)).toBe(1)
    expect(wrapper.text()).toContain('600519')
    expect(screeningSession.run?.runId).toBe('run-abc')

    // ─── 用户点结果表里的股票代码 → 真实 RouterLink 导航 ───
    const stockLink = wrapper.find('a.stock-link')
    expect(stockLink.exists()).toBe(true)
    expect(wrapper.html()).toContain('600519')
    await stockLink.trigger('click')
    await waitFor(() => router.currentRoute.value.name === 'stock-detail')
    await flushPromises()
    // 详情页已经接管
    expect(wrapper.find('.screening-page').exists()).toBe(false)

    // ─── 用户点侧边栏「筛选」回来（真实 RouterLink 语义 = router.push('/screening')）───
    await router.push('/screening')
    await waitFor(() => wrapper.find('.screening-page').exists())
    await flushPromises()

    // ① 会话快照还在
    expect(screeningSession.run?.runId).toBe('run-abc')
    // ② 页面上结果还在
    const panel = wrapper.findComponent(ScreeningResultsPanel)
    expect(panel.exists()).toBe(true)
    expect(panel.props('results')).toHaveLength(3)
    expect(panel.props('runState')).toBe('ok')
    expect(wrapper.text()).toContain('600519')
    expect(wrapper.text()).toContain('贵州茅台')
    // ③ 没有重跑
    expect(runCalls(post)).toBe(1)

    wrapper.unmount()
  })
})
