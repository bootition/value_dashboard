/**
 * 更新期间自选列表**不许留白**（2026-11-27 用户口径）。
 *
 * 用户：「数据更新的时候可以先把之前缓存的数据显示出来 —— 周二的数据正在更新，
 * 那就先显示周一的。为什么一定要留空白呢？」
 *
 * 后端在写锁挡住时会回落 `screening_snapshot.duckdb`（上一轮成功更新后物化），
 * 响应为 `enrichment='screening_snapshot'` + `enrichment_as_of`。
 * 这里钉住前端两件事：
 *  ① 该状态下横幅必须说"上一次更新完成时的数据 + 时点"，**不是**"暂未加载"；
 *  ② 名称与指标必须真的渲染出来（不能是破折号/代码兜底）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'

import WatchlistPage from '../../src/views/WatchlistPage.vue'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
  }
  return { default: mock, ...mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div/>' } },
    { path: '/stock/:code', component: { template: '<div/>' } },
    { path: '/screening', component: { template: '<div/>' } },
    { path: '/data-status', component: { template: '<div/>' } },
  ],
})

function mountPage() {
  return mount(
    {
      components: { NMessageProvider, NDialogProvider, WatchlistPage },
      template: '<n-message-provider><n-dialog-provider><WatchlistPage /></n-dialog-provider></n-message-provider>',
    },
    { global: { plugins: [router] } },
  )
}

const SNAPSHOT_ITEM = {
  stock_code: '600519',
  name: '贵州茅台',
  exchange: 'SSE',
  csrc_l1: '制造业',
  listing_date: '2001-08-27',
  group_name: '我的自选',
  source_rule_id: null,
  source_result_id: null,
  added_at: '2026-11-25T00:00:00Z',
  latest_close: 1500,
  pe_ttm: 22.5,
  pb_mrq: 8.1,
  roe: 0.31,
  gross_margin: 0.91,
  net_margin: 0.52,
  debt_ratio: 0.18,
  revenue_yoy: 0.16,
  net_profit_yoy: 0.19,
  dividend_yield: 0.021,
  total_market_cap: 1.9e12,
  circ_market_cap: 1.9e12,
}

describe('自选页：更新期间显示上一轮数据（不留白）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/watchlist/list') {
        return Promise.resolve({
          data: {
            count: 1,
            enrichment: 'screening_snapshot',
            enrichment_as_of: '2026-11-25T11:41:00Z',
            groups: [{
              group_name: '我的自选', cnt: 1, kind: 'default', is_default: true,
              runs: 0, manual: 1, rule_id: null, rule_ids: [],
            }],
            items: [SNAPSHOT_ITEM],
            trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
          },
        })
      }
      return Promise.resolve({ data: { ops: [], count: 0, groups: [] } })
    })
  })

  it('横幅说明"上一次更新完成时的数据"，而不是"暂未加载"', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('上一次更新完成时的数据')
    expect(text).not.toContain('暂未加载')
  })

  it('名称与指标真的渲染出来（不是破折号、也不是拿代码兜底）', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('贵州茅台')
  })
})
