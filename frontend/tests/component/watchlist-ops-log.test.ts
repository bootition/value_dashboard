/**
 * 自选页「操作记录」入口（v22 `watchlist_ops`，2026-11-24 用户投诉后新增）。
 *
 * 用户原话：「我从 ROCE 这个筛选分组里面移动过去的……你难道查不出这个日志来吗？」
 * 事后只能靠行数差 + 时间戳猜（还因为把 UTC 当本地时间猜错过一次）。
 *
 * 钉死的契约（前端只做三件事，措辞与时间都由后端给）：
 * 1. 侧栏有不显眼的「操作记录」入口 → 打开弹窗 → `GET /api/watchlist/ops?limit=30`；
 * 2. 列表**只显示 `created_at_local`**（本地时间），UTC 只挂在 title 上做交叉核对
 *    —— 界面里再出现一个没标注的 UTC 时间就等于把那次误判放回来；
 * 3. 按股票过滤 → 请求带 `stock_code`，前端不自己筛（日志可能不止 30 条）。
 */

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
    patch: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
  }
  return { default: mock, ...mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'

const DEFAULT_GROUP = '我的自选'

/** 后端 `GET /ops` 里的一条（字段全是后端给的，前端不再拼）。 */
function opItem(overrides: Record<string, unknown> = {}) {
  return {
    id: 12,
    op: 'move',
    op_label: '移动分组',
    stock_codes: ['000333', '002415', '600519'],
    from_group: 'ROCE遗传好公司',
    to_group: DEFAULT_GROUP,
    detail: { mode: 'move', moved: 3, rows: 3 },
    actor: 'local-ui',
    created_at_utc: '2026-11-24T05:37:00Z',
    created_at_local: '2026-11-24 13:37:00',
    created_at_raw: null,
    summary: '000333、002415 等 3 只 移动 从 ROCE遗传好公司 到「我的自选」',
    ...overrides,
  }
}

function installGet(ops: ReturnType<typeof opItem>[]) {
  ;(axios.get as Mock).mockImplementation((url: string) => {
    if (url === '/api/watchlist/ops') {
      return Promise.resolve({ data: { ops, count: ops.length } })
    }
    if (url === '/api/watchlist/list') {
      return Promise.resolve({
        data: {
          items: [], count: 0, groups: [], trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
        },
      })
    }
    if (url === '/api/watchlist/groups') {
      return Promise.resolve({ data: { groups: [], count: 0 } })
    }
    return Promise.resolve({ data: {} })
  })
}

function mountPage() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  return mount(NDialogProvider, {
    slots: { default: () => h(NMessageProvider, null, { default: () => h(WatchlistPage) }) },
    global: { plugins: [router] },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
  installGet([opItem()])
})

describe('自选页：操作记录', () => {
  it('侧栏入口 → 打开弹窗 → 请求最近 30 条，且只显示本地时间', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const entry = wrapper.find('.ops-entry-link')
    expect(entry.exists()).toBe(true)
    await entry.trigger('click')
    await flushPromises()

    expect(axios.get).toHaveBeenCalledWith('/api/watchlist/ops', { params: { limit: 30 } })
    const modal = document.body.querySelector('.n-modal')
    expect(modal).toBeTruthy()
    const text = modal!.textContent ?? ''
    // 一句话描述（后端给）
    expect(text).toContain('移动分组')
    expect(text).toContain('ROCE遗传好公司')
    // 本地时间被显示，UTC 只在 title 里
    expect(text).toContain('2026-11-24 13:37:00')
    const timeNode = modal!.querySelector('.ops-time')
    expect(timeNode?.getAttribute('title')).toContain('2026-11-24T05:37:00Z')
    expect(text).not.toContain('2026-11-24T05:37:00Z')
  })

  it('按股票过滤 → 请求带 stock_code；没有记录时如实说"还没有"', async () => {
    installGet([])
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('.ops-entry-link').trigger('click')
    await flushPromises()

    const modal = document.body.querySelector('.n-modal')!
    expect(modal.textContent).toContain('还没有任何自选操作记录')

    // 弹窗内容被 teleport 到 document.body，所以从这里查、并派发原生事件
    const input = modal.querySelector('.ops-toolbar input') as HTMLInputElement | null
    expect(input).toBeTruthy()
    input!.value = '000333'
    input!.dispatchEvent(new Event('input'))
    await flushPromises()
    const queryButton = [...modal.querySelectorAll('.ops-toolbar button')]
      .find((node) => (node.textContent ?? '').includes('查询')) as HTMLButtonElement | undefined
    expect(queryButton).toBeTruthy()
    queryButton!.click()
    await flushPromises()

    expect(axios.get).toHaveBeenCalledWith('/api/watchlist/ops', {
      params: { limit: 30, stock_code: '000333' },
    })
  })
})
