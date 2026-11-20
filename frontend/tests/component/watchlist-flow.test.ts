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
    // 2026-11-25：列配置改为**按分组**存（`vd.watchlist.columns.<编码分组名>`），
    // 这里预置的就是新 key、新形状（顺序 + 可见集合），仍然是"读到配置不能崩"的回归。
    const storageKey = `vd.watchlist.columns.${encodeURIComponent('default')}`
    localStorage.setItem(storageKey, JSON.stringify({ v: 1, order: ['name', 'stock_code'], visible: ['stock_code', 'name'] }))
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
      localStorage.removeItem(storageKey)
      localStorage.removeItem('vd.watchlist.columns')
    }
  })
})

describe('WatchlistPage 分组展示归一与空态出口（用户实测：不要英文 default、不要空白页）', () => {
  const REAL_GROUP = '往股东口袋装钱的大老爷们'

  function makeItem(code: string, groupName: string, sourceRuleId: number | null) {
    return {
      stock_code: code, name: '测试公司', exchange: 'SSE', csrc_l1: '制造业',
      listing_date: '2020-01-01', group_name: groupName,
      source_rule_id: sourceRuleId, source_result_id: null, added_at: '2026-08-01',
      latest_close: 10, pe_ttm: 10, pb_mrq: 1, roe: 0.1, gross_margin: 0.2,
      net_margin: 0.1, debt_ratio: 0.3, revenue_yoy: 0.1, net_profit_yoy: 0.1,
      dividend_yield: 0.01, total_market_cap: 100, circ_market_cap: 50,
    }
  }

  function mountPage() {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/screening', name: 'screening', component: { template: '<div>筛选页</div>' } },
        { path: '/:pathMatch(.*)*', component: { template: '<div />' } },
      ],
    })
    const wrapper = mount(NDialogProvider, {
      slots: { default: () => h(NMessageProvider, null, { default: () => h(WatchlistPage) }) },
      global: { plugins: [router] },
    })
    return { router, wrapper }
  }

  it('默认组只用中文展示：侧栏、右栏大标题、表格分组列、输入框都不出现 default', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string, config?: { params?: { group?: string } }) => {
      if (url !== '/api/watchlist/list') return Promise.resolve({ data: {} })
      const groups = [
        { group_name: 'default', cnt: 1 },
        { group_name: REAL_GROUP, cnt: 1 },
      ]
      const allItems = [makeItem('000001', 'default', null), makeItem('600519', REAL_GROUP, 42)]
      const group = config?.params?.group
      const items = group ? allItems.filter((row) => row.group_name === group) : allItems
      return Promise.resolve({
        data: {
          items, count: items.length, groups,
          trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
        },
      })
    })

    const { wrapper } = mountPage()
    await flushPromises()

    // 首次进入直接落在已存在的规则分组上（不是空的 default 占位组）
    expect(wrapper.findAll('.watchlist-card-heading h2').map((node) => node.text())).toEqual([REAL_GROUP])
    expect(wrapper.text()).toContain('显示 1 家公司')
    // 侧栏仍能看见「手动收录」，但绝不出现英文 default；且手动组排在规则分组之后
    expect(wrapper.text()).toContain('手动收录')
    expect(wrapper.text()).not.toMatch(/default/i)
    const groupLabels = wrapper.findAll('.watchlist-groups button')
      .map((node) => node.text())
      .filter((text) => text.includes(REAL_GROUP) || text.includes('手动收录'))
    expect(groupLabels).toHaveLength(2)
    expect(groupLabels[0]).toContain(REAL_GROUP)
    expect(groupLabels[1]).toContain('手动收录')

    // 添加框不再预填 default：空值 + 中文 placeholder
    const groupInput = wrapper.findAll('input').find((node) => (node.attributes('placeholder') ?? '').includes('分组名'))
    expect(groupInput).toBeTruthy()
    expect((groupInput!.element as HTMLInputElement).value).toBe('')
    expect(groupInput!.attributes('placeholder')).not.toMatch(/default/i)

    // 切到手动收录：大标题、表格分组列都必须是中文
    const manualButton = wrapper.findAll('button').find((node) => node.text().includes('手动收录'))
    expect(manualButton).toBeTruthy()
    await manualButton!.trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.watchlist-card-heading h2').map((node) => node.text())).toEqual(['手动收录'])
    expect(wrapper.text()).toContain('手动收录')
    expect(wrapper.text()).not.toMatch(/default/i)

    // 再切回真实规则分组：正常展示与切换
    const realButton = wrapper.findAll('button').find((node) => node.text().includes(REAL_GROUP))
    expect(realButton).toBeTruthy()
    await realButton!.trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.watchlist-card-heading h2').map((node) => node.text())).toEqual([REAL_GROUP])
    expect(wrapper.text()).toContain('600519')
  })

  it('没有可显示分组时不出现空白页：给出「去筛选规则页」入口，且不显示"显示 0 家公司"', async () => {
    const get = axios.get as Mock
    get.mockResolvedValue({
      data: {
        items: [], count: 0,
        // 空的手动组不应作为可选分组出现在侧栏
        groups: [{ group_name: 'default', cnt: 0 }],
        trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
      },
    })

    const { wrapper } = mountPage()
    await flushPromises()

    expect(wrapper.text()).not.toMatch(/default/i)
    expect(wrapper.text()).not.toContain('显示 0 家公司')
    expect(wrapper.text()).toContain('还没有可显示的自选分组')
    const link = wrapper.find('a[href="/screening"]')
    expect(link.exists()).toBe(true)
    expect(link.text()).toContain('去筛选规则页')
    expect(wrapper.findAll('button').some((node) => node.text().includes('刷新列表'))).toBe(true)
  })

  it('已选分组为空时也给出可操作出口，而不是一大片空白', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url !== '/api/watchlist/list') return Promise.resolve({ data: {} })
      return Promise.resolve({
        data: {
          items: [], count: 0,
          groups: [{ group_name: REAL_GROUP, cnt: 1 }],
          trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
        },
      })
    })

    const { wrapper } = mountPage()
    await flushPromises()

    expect(wrapper.text()).not.toMatch(/default/i)
    expect(wrapper.text()).toContain(`「${REAL_GROUP}」里还没有公司`)
    expect(wrapper.find('a[href="/screening"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('显示 0 家公司')
    expect(get).toHaveBeenCalled()
  })
})
