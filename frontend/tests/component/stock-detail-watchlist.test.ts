/**
 * 详情页「加入自选」三态（2026-11-26 用户需求）。
 *
 * 用户原话：「有没有办法做到：在详情界面有个『加入自选』……那种『点击加入自选』应该是
 * 加入默认自选列表，然后还可以自己移动到自己的分组里。」
 *
 * 钉死的三态：
 * 1. **未加入** → 按钮「加入自选」；点击后 `POST /api/watchlist/add` **不带 group**
 *    （落默认分组），提示里必须出现「我的自选」；随后状态刷新成「已在自选」。
 * 2. **已加入** → 按钮「已在自选」+ 列出所在分组（一个或多个）+ 「移出全部自选」。
 * 3. **状态读取失败** → 仍显示「加入自选」并说明"状态未知"，**不许**谎报已在自选。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { h } from 'vue'
import StockDetailPage from '../../src/views/StockDetailPage.vue'

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

const STOCK_INFO = {
  stock_code: '600519', name: '贵州茅台', exchange: 'SSE', pinyin: 'gzmt',
  listing_date: '2001-08-27', csrc_l1: '制造业', is_st: false, is_suspended: false,
  latest_close: 1500, latest_price_date: '2026-11-20',
}
const INDICATORS = {
  stock_code: '600519', latest_price_date: '2026-11-20', warning_codes: [],
  untrusted_all: false, untrusted_fields: [], freshness: null,
}

function setupAxios(statusResponse: () => Promise<unknown>) {
  const get = axios.get as Mock
  get.mockImplementation((url: string) => {
    if (url.includes('/api/watchlist/status')) return statusResponse()
    if (url.includes('/api/blacklist/check/')) {
      return Promise.resolve({ data: { stock_code: '600519', blacklisted: false } })
    }
    if (url.includes('/info')) return Promise.resolve({ data: STOCK_INFO })
    if (url.includes('/indicators')) return Promise.resolve({ data: INDICATORS })
    if (url.includes('/kline')) return Promise.resolve({ data: { candles: [], adjust: 'raw', period: 'day', count: 0 } })
    if (url.includes('/business-overview')) {
      return Promise.resolve({
        data: {
          stock_code: '600519', profile: { status: 'missing' },
          breakdown: { status: 'missing', composition: {}, history: [], latest_report_date: null, provenance: null },
          provenance: { profile: null, breakdown: null },
        },
      })
    }
    if (url.includes('/api/data-status/summary')) {
      return Promise.resolve({ data: { data_quality: { warning_codes: [] } } })
    }
    return Promise.resolve({ data: {} })
  })
}

async function mountPage() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/stock/:code', component: { template: '<div/>' } }],
  })
  const wrapper = mount(NMessageProvider, {
    slots: { default: () => h(NDialogProvider, null, { default: () => h(StockDetailPage) }) },
    attachTo: document.body,
    global: { plugins: [router] },
  })
  await router.push('/stock/600519')
  await router.isReady()
  await flushPromises()
  await flushPromises()
  return wrapper
}

function bodyText(): string {
  return document.body.textContent ?? ''
}

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

describe('详情页「加入自选」三态', () => {
  it('未加入 → 显示「加入自选」；点击后加入默认分组并变「已在自选」', async () => {
    let inWatchlist = false
    setupAxios(() => Promise.resolve({
      data: {
        stock_code: '600519', in_watchlist: inWatchlist,
        groups: inWatchlist ? ['我的自选'] : [], default_group: '我的自选', blacklisted: false,
      },
    }))
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/watchlist/add') {
        inWatchlist = true
        return Promise.resolve({
          data: { status: 'ok', stock_code: '600519', group: '我的自选', created: true, default_group: '我的自选' },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const wrapper = await mountPage()
    const toggle = wrapper.find('#watchlist-toggle')
    expect(toggle.exists()).toBe(true)
    expect(toggle.text()).toBe('加入自选')

    await toggle.trigger('click')
    await flushPromises()

    // 关键：不传 group → 后端落默认分组
    expect(post).toHaveBeenCalledWith('/api/watchlist/add', { stock_code: '600519' })
    expect(bodyText()).toContain('已加入「我的自选」')
    expect(wrapper.find('#watchlist-toggle').text()).toBe('已在自选')
    expect(wrapper.find('#watchlist-groups').text()).toContain('我的自选')
  })

  it('已在多个分组 → 按钮「已在自选」+ 列出全部分组 + 「移出全部自选」', async () => {
    setupAxios(() => Promise.resolve({
      data: {
        stock_code: '600519', in_watchlist: true,
        groups: ['我的自选', '重点观察'], default_group: '我的自选', blacklisted: false,
      },
    }))
    const wrapper = await mountPage()

    expect(wrapper.find('#watchlist-toggle').text()).toBe('已在自选')
    const groups = wrapper.find('#watchlist-groups')
    expect(groups.text()).toContain('「我的自选」')
    expect(groups.text()).toContain('「重点观察」')
    expect(wrapper.find('#watchlist-remove').exists()).toBe(true)
  })

  it('状态读取失败 → 仍给「加入自选」，如实说状态未知（不谎报已在自选）', async () => {
    setupAxios(() => Promise.reject(new Error('Network Error')))
    const wrapper = await mountPage()

    expect(wrapper.find('#watchlist-toggle').text()).toBe('加入自选')
    expect(wrapper.text()).toContain('自选状态暂时无法读取')
    expect(wrapper.text()).not.toContain('已在自选')
  })
})
