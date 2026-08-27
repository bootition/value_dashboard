import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { h } from 'vue'
import WatchlistPage from '../../src/views/WatchlistPage.vue'

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

describe('WatchlistPage 自选展示与信任遮蔽（PRD §13）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/watchlist/list') {
        return Promise.resolve({
          data: {
            items: [
              {
                stock_code: '600519', name: '贵州茅台', exchange: 'SSE', csrc_l1: '制造业',
                group_name: 'default', source_rule_id: null, source_result_id: null, added_at: '2026-08-01',
                latest_close: 1500, pe_ttm: 30, pb_mrq: 8, roe: 0.3, gross_margin: 0.9,
                net_margin: 0.5, debt_ratio: 0.2, revenue_yoy: 0.1, net_profit_yoy: 0.15,
                dividend_yield: 0.02,
              },
            ],
            count: 1,
            groups: [{ group_name: 'default', cnt: 1 }],
            trust: { warning_codes: ['LINEAGE_INVALID'], untrusted_all: true, untrusted_fields: [] },
          },
        })
      }
      if (url === '/api/watchlist/groups') {
        return Promise.resolve({ data: { groups: [{ group_name: 'default', cnt: 1 }], count: 1 } })
      }
      return Promise.resolve({ data: {} })
    })
  })

  it('渲染自选股票，LINEAGE_INVALID 时指标显示"数据不可信"', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [] })
    // 与 App.vue 一致：L0-6 移除确认依赖 NDialogProvider
    const wrapper = mount(NDialogProvider, {
      slots: {
        default: () =>
          h(NMessageProvider, null, { default: () => h(WatchlistPage) }),
      },
      global: { plugins: [router] },
    })
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('600519')
    expect(text).toContain('贵州茅台')
    // 全局不可信告警
    expect(text).toContain('数据不可信')
  })

  it('有规则来源的分组显示“按最新数据重新筛选”按钮并调用后端刷新', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/watchlist/list') {
        return Promise.resolve({
          data: {
            items: [{
              stock_code: '600519', name: '贵州茅台', exchange: 'SSE', csrc_l1: '制造业',
              group_name: '价值组', source_rule_id: 42, source_result_id: 7,
              added_at: '2026-08-01', latest_close: 1500, pe_ttm: 30,
              pb_mrq: 8, roe: 0.3, gross_margin: 0.9, net_margin: 0.5,
              debt_ratio: 0.2, revenue_yoy: 0.1, net_profit_yoy: 0.15, dividend_yield: 0.02,
            }],
            count: 1,
            groups: [{ group_name: '价值组', cnt: 1 }],
            trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
          },
        })
      }
      return Promise.resolve({ data: {} })
    })
    const post = axios.post as Mock
    post.mockResolvedValue({ data: { status: 'ok', added: 1, removed: 1, refreshed: 1 } })

    const router = createRouter({ history: createMemoryHistory(), routes: [] })
    const wrapper = mount(NDialogProvider, {
      slots: {
        default: () =>
          h(NMessageProvider, null, { default: () => h(WatchlistPage) }),
      },
      global: { plugins: [router] },
    })
    await flushPromises()
    const groupButton = wrapper.findAll('button').find((b) => b.text().includes('价值组'))
    expect(groupButton).toBeTruthy()
    await groupButton!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('按最新数据重新筛选')
    const button = wrapper.findAll('button').find((b) => b.text().includes('按最新数据重新筛选'))
    expect(button).toBeTruthy()
    await button!.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith(
      '/api/watchlist/refresh',
      expect.objectContaining({ group_name: '价值组' }),
    )
  })

  it('预置列配置后挂载不白屏（reports/80 F5 TDZ 回归）', async () => {
    // 用户在自选页配置过列 → localStorage 存有列配置 → 每次进入页面
    // 必须正常渲染（此前 filter 回调访问未初始化 allColumnOptions 抛错白屏）。
    localStorage.setItem('vd.watchlist.columns', JSON.stringify(['stock_code', 'name']))
    try {
      const router = createRouter({ history: createMemoryHistory(), routes: [] })
      const wrapper = mount(NDialogProvider, {
        slots: {
          default: () =>
            h(NMessageProvider, null, { default: () => h(WatchlistPage) }),
        },
        global: { plugins: [router] },
      })
      await flushPromises()
      expect(wrapper.text()).toContain('600519')
      expect(wrapper.text()).toContain('贵州茅台')
    } finally {
      localStorage.removeItem('vd.watchlist.columns')
    }
  })
})
