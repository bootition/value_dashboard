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

const GROUP = '往股东口袋装钱的大老公司'

function makeItem(code: string) {
  return {
    stock_code: code, name: '测试公司', exchange: 'SSE', csrc_l1: '制造业',
    listing_date: '2020-01-01', group_name: GROUP,
    source_rule_id: 42, source_result_id: 7, added_at: '2026-11-23',
    latest_close: 10, pe_ttm: 10, pb_mrq: 1, roe: 0.1, gross_margin: 0.2,
    net_margin: 0.1, debt_ratio: 0.3, revenue_yoy: 0.1, net_profit_yoy: 0.1,
    dividend_yield: 0.01, total_market_cap: 120, circ_market_cap: 80,
  }
}

function mountPage() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  const wrapper = mount(NDialogProvider, {
    slots: {
      default: () =>
        h(NMessageProvider, null, { default: () => h(WatchlistPage) }),
    },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

function modalRoot(): Element | null {
  return document.body.querySelector('.n-modal')
}

function modalButton(text: string): HTMLButtonElement | undefined {
  const root = modalRoot()
  return Array.from(root?.querySelectorAll('button') ?? []).find(
    (button) => button.textContent?.trim() === text,
  ) as HTMLButtonElement | undefined
}

describe('自选页：删除整个分组（2026-11-25 用户实测：此前只能一只一只删）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/watchlist/list') {
        return Promise.resolve({
          data: {
            items: [makeItem('600519'), makeItem('000858')],
            count: 2,
            groups: [{ group_name: GROUP, cnt: 2 }],
            trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
          },
        })
      }
      if (url === '/api/watchlist/groups') {
        return Promise.resolve({
          data: {
            groups: [{
              group_name: GROUP, cnt: 2, runs: 1, manual: 0, rule_ids: [42],
            }],
            count: 1,
          },
        })
      }
      return Promise.resolve({ data: {} })
    })
  })

  it('删除按钮在分组行的角落、不在名字后面（用户反馈：字重叠了）', async () => {
    // beforeEach 已把 /api/watchlist/list 与 /groups 都打桩好（分组 = GROUP，2 只）
    const { wrapper } = mountPage()
    await flushPromises()

    const row = wrapper.findAll('.group-row').at(0)!
    const del = row.find('.group-delete')
    expect(del.exists()).toBe(true)
    // ① 文案必须极短（"删除分组"4 个字塞进行内会挤掉分组名）——完整语义放 title tooltip
    expect(del.text()).toBe('删除')
    expect(del.attributes('title')).toBe(`删除分组「${GROUP}」`)
    expect(del.attributes('aria-label')).toBe(`删除分组「${GROUP}」`)
    // ② 必须是行的**同级**元素，不能嵌在 .group-select 里（嵌进去就是"名字后面的一块"）
    expect(row.find('.group-select .group-delete').exists()).toBe(false)
    // ③ 分组名仍然完整可读（省略号是 CSS 处理的视觉收束，DOM 里不丢字）
    expect(row.find('.group-select b').text()).toBe(GROUP)
  })

  it('对话框说明删多少只/是否连带删规则；勾选后 delete_rule=true', async () => {
    const del = axios.delete as Mock
    del.mockResolvedValue({
      data: {
        status: 'ok',
        group: GROUP,
        deleted_rows: 2,
        rule_deleted: false,
        deleted_rule_id: null,
        rule_delete_status: 'skipped',
        rule_delete_reason: '该规则有 1 条已保存的筛选结果引用，不能删除；请先删除这些筛选结果',
        skipped_rules: [{
          rule_id: 42,
          reason_code: 'rule_has_provenance_refs',
          message: '该规则有 1 条已保存的筛选结果引用，不能删除；请先删除这些筛选结果',
          refs: { results: 1, watchlist: 0, runs: 3 },
        }],
      },
    })

    const { wrapper } = mountPage()
    await flushPromises()

    const sidebarDelete = wrapper.findAll('.group-delete').at(0)
    expect(sidebarDelete).toBeTruthy()
    await sidebarDelete!.trigger('click')
    await flushPromises()

    const modalText = modalRoot()?.textContent ?? ''
    expect(modalText).toContain('删除自选分组')
    expect(modalText).toContain(`确定删除自选分组「${GROUP}」吗？`)
    expect(modalText).toContain('将删除该分组的 2 只股票')
    expect(modalText).toContain('同时删除对应的筛选规则（若有）')
    expect(modalText).toContain('该分组来自筛选规则 #42')
    expect(modalText).toContain('规则会保留，并在这里如实告诉你原因')

    // naive-ui 的 n-checkbox 没有原生 input：根节点是 role="checkbox"
    const checkbox = modalRoot()?.querySelector('[role="checkbox"]') as HTMLElement | null
    expect(checkbox).toBeTruthy()
    checkbox!.click()
    await flushPromises()

    const confirm = modalButton('删除分组')
    expect(confirm).toBeTruthy()
    confirm!.click()
    await flushPromises()

    expect(del).toHaveBeenCalledWith(
      `/api/watchlist/groups/${encodeURIComponent(GROUP)}`,
      { params: { delete_rule: true } },
    )
    // 分组已删、规则没删 → 必须把后端原因和引用条数展示给用户
    const bodyText = document.body.textContent ?? ''
    expect(bodyText).toContain('分组已删除，但筛选规则没有删除')
    expect(bodyText).toContain('1 条已保存的筛选结果')
    expect(bodyText).toContain('当前引用：已保存的筛选结果 1 条')
  })

  it('不勾选时 delete_rule=false，成功后刷新分组列表', async () => {
    const del = axios.delete as Mock
    del.mockResolvedValue({
      data: {
        status: 'ok',
        group: GROUP,
        deleted_rows: 2,
        rule_deleted: false,
        deleted_rule_id: null,
        rule_delete_status: 'not_requested',
        rule_delete_reason: null,
        skipped_rules: [],
      },
    })
    const get = axios.get as Mock
    const { wrapper } = mountPage()
    await flushPromises()
    const callsBefore = get.mock.calls.filter(([url]) => url === '/api/watchlist/list').length

    const sidebarDelete = wrapper.findAll('.group-delete').at(0)
    await sidebarDelete!.trigger('click')
    await flushPromises()
    const confirm = modalButton('删除分组')
    expect(confirm).toBeTruthy()
    confirm!.click()
    await flushPromises()

    expect(del).toHaveBeenCalledWith(
      `/api/watchlist/groups/${encodeURIComponent(GROUP)}`,
      { params: { delete_rule: false } },
    )
    expect(get.mock.calls.filter(([url]) => url === '/api/watchlist/list').length)
      .toBeGreaterThan(callsBefore)
    expect(document.body.textContent ?? '').toContain('已删除自选分组')
  })

  it('后端 409/404 时把原因留在对话框里（不是只闪一条提示）', async () => {
    const del = axios.delete as Mock
    del.mockRejectedValue({
      isAxiosError: true,
      response: { status: 409, data: { detail: 'watchlist group not found' } },
    })
    const { wrapper } = mountPage()
    await flushPromises()
    const sidebarDelete = wrapper.findAll('.group-delete').at(0)
    await sidebarDelete!.trigger('click')
    await flushPromises()
    modalButton('删除分组')!.click()
    await flushPromises()

    expect(modalRoot()).toBeTruthy()
    // friendlyErrorMessage 把 404 详情翻译成用户可读原因，并留在对话框里
    expect(modalRoot()?.textContent ?? '').toContain('请求的资源不存在')
  })

  it('分组来自多个规则时禁止勾选连带删除（不猜）', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/watchlist/list') {
        return Promise.resolve({
          data: {
            items: [makeItem('600519')],
            count: 1,
            groups: [{ group_name: GROUP, cnt: 1 }],
            trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
          },
        })
      }
      if (url === '/api/watchlist/groups') {
        return Promise.resolve({
          data: {
            groups: [{ group_name: GROUP, cnt: 1, runs: 2, manual: 0, rule_ids: [42, 43] }],
            count: 1,
          },
        })
      }
      return Promise.resolve({ data: {} })
    })
    const { wrapper } = mountPage()
    await flushPromises()
    const sidebarDelete = wrapper.findAll('.group-delete').at(0)
    await sidebarDelete!.trigger('click')
    await flushPromises()

    const checkbox = modalRoot()?.querySelector('[role="checkbox"]') as HTMLElement | null
    expect(checkbox?.className).toContain('n-checkbox--disabled')
    expect(modalRoot()?.textContent ?? '').toContain('2 个不同筛选规则')
    expect(modalRoot()?.textContent ?? '').toContain('不删除任何规则')
  })
})
