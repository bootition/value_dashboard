/**
 * 自选页：自建空分组 + 重命名 + 批量移动（2026-11-26 用户需求）。
 *
 * 用户原话：「自选是不是还可以自建分组？这个功能是不是应该也要有？」「还可以自己
 * 移动到自己的分组里」。
 *
 * 钉死的契约：
 * 1. 「+ 新建分组」→ `POST /api/watchlist/groups`，成功后该分组**立刻出现在侧栏**
 *    且计数是 0（空分组合法存在 —— 这是本次改造的核心价值）；
 * 2. 分组行的「改名」入口 → `PATCH /api/watchlist/groups/{name}`（默认分组不显示
 *    改名/删除两个入口）；
 * 3. 勾选表格若干只 → 选目标分组 → `POST /api/watchlist/move` 的**批量**口径。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider, NDialogProvider, NSelect } from 'naive-ui'
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

const GROUP = '重点观察'
const DEFAULT_GROUP = '我的自选'

function makeItem(code: string, groupName: string) {
  return {
    stock_code: code, name: '测试公司', exchange: 'SSE', csrc_l1: '制造业',
    listing_date: '2020-01-01', group_name: groupName,
    source_rule_id: null, source_result_id: null, added_at: '2026-11-26',
    latest_close: 10, pe_ttm: 10, pb_mrq: 1, roe: 0.1, gross_margin: 0.2,
    net_margin: 0.1, debt_ratio: 0.3, revenue_yoy: 0.1, net_profit_yoy: 0.1,
    dividend_yield: 0.01, total_market_cap: 100, circ_market_cap: 50,
  }
}

/** 后端 `/list` 的分组对象（v21 起带 kind / is_default）。 */
function groupItem(name: string, cnt: number, kind: 'default' | 'manual' | 'rule') {
  return {
    group_name: name, cnt, kind, is_default: kind === 'default',
    runs: 0, manual: cnt, rule_id: null, rule_ids: [],
  }
}

let serverGroups: ReturnType<typeof groupItem>[]
let serverItems: ReturnType<typeof makeItem>[]

function installGet() {
  const get = axios.get as Mock
  get.mockImplementation((url: string, config?: { params?: { group?: string } }) => {
    if (url === '/api/watchlist/list') {
      const group = config?.params?.group
      const items = group ? serverItems.filter((row) => row.group_name === group) : serverItems
      return Promise.resolve({
        data: {
          items, count: items.length, groups: serverGroups,
          trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
        },
      })
    }
    if (url === '/api/watchlist/groups') {
      return Promise.resolve({ data: { groups: serverGroups, count: serverGroups.length } })
    }
    return Promise.resolve({ data: {} })
  })
}

function mountPage() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  const wrapper = mount(NDialogProvider, {
    slots: { default: () => h(NMessageProvider, null, { default: () => h(WatchlistPage) }) },
    global: { plugins: [router] },
  })
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
  serverGroups = [
    groupItem(DEFAULT_GROUP, 2, 'default'),
    groupItem(GROUP, 0, 'manual'),
  ]
  serverItems = [
    makeItem('600519', DEFAULT_GROUP),
    makeItem('000858', DEFAULT_GROUP),
  ]
  installGet()
})

describe('自选页：自建分组 / 改名 / 批量移动', () => {
  it('新建分组 → 立刻出现空分组（计数 0）', async () => {
    const post = axios.post as Mock
    post.mockImplementation((url: string, body?: { name?: string }) => {
      if (url === '/api/watchlist/groups') {
        const name = String(body?.name ?? '')
        serverGroups = [...serverGroups, groupItem(name, 0, 'manual')]
        return Promise.resolve({
          data: {
            status: 'ok',
            group: { group_name: name, kind: 'manual', cnt: 0, runs: 0, manual: 0, rule_ids: [], is_default: false },
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const wrapper = mountPage()
    await flushPromises()

    const input = wrapper.find('.new-group-input input')
    expect(input.exists()).toBe(true)
    await input.setValue('低估值')
    const button = wrapper.findAll('.watchlist-groups button')
      .find((node) => node.text().includes('新建分组'))
    expect(button).toBeTruthy()
    await button!.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith('/api/watchlist/groups', { name: '低估值' })
    // 空分组必须出现在侧栏，且计数是 0
    const rows = wrapper.findAll('.group-row')
    const created = rows.find((row) => row.text().includes('低估值'))
    expect(created).toBeTruthy()
    expect(created!.find('.group-select').text()).toContain('0')
  })

  it('默认分组没有改名/删除入口；其它分组有，改名走 PATCH', async () => {
    const patch = axios.patch as Mock
    patch.mockImplementation(() => Promise.resolve({ data: { status: 'ok', new_name: '重中之重' } }))

    const wrapper = mountPage()
    await flushPromises()

    const rows = wrapper.findAll('.group-row')
    const defaultRow = rows.find((row) => row.text().includes(DEFAULT_GROUP))!
    expect(defaultRow.find('.group-rename').exists()).toBe(false)
    expect(defaultRow.find('.group-delete').exists()).toBe(false)

    const otherRow = rows.find((row) => row.text().includes(GROUP))!
    expect(otherRow.find('.group-rename').exists()).toBe(true)
    expect(otherRow.find('.group-delete').exists()).toBe(true)

    await otherRow.find('.group-rename').trigger('click')
    await flushPromises()
    const modal = document.body.querySelector('.n-modal')
    expect(modal?.textContent ?? '').toContain('重命名分组')
    const nameInput = Array.from(modal?.querySelectorAll('input') ?? []).at(-1)!
    nameInput.value = '重中之重'
    nameInput.dispatchEvent(new Event('input'))
    await flushPromises()
    const save = Array.from(modal?.querySelectorAll('button') ?? [])
      .find((button) => button.textContent?.trim() === '保存新名字')
    save!.click()
    await flushPromises()

    expect(patch).toHaveBeenCalledWith(
      `/api/watchlist/groups/${encodeURIComponent(GROUP)}`,
      { new_name: '重中之重' },
    )
  })

  it('勾选若干只 → 移动到分组 → POST /move 批量口径', async () => {
    const post = axios.post as Mock
    post.mockImplementation(() => Promise.resolve({ data: { status: 'ok', moved: 1, rows: 1 } }))

    const wrapper = mountPage()
    await flushPromises()

    // naive-ui：绑定了 checked-row-keys 后表格自动带选择列
    const checkboxes = wrapper.findAll('tbody .n-checkbox')
    expect(checkboxes.length).toBeGreaterThan(0)
    await checkboxes[0].trigger('click')
    await flushPromises()
    expect(wrapper.find('.move-bar-count').text()).toContain('1')

    const selects = wrapper.findAllComponents(NSelect)
    const moveSelect = selects.at(-1)!
    moveSelect.vm.$emit('update:value', GROUP)
    await flushPromises()

    const moveButton = wrapper.findAll('button').find((node) => node.text().includes('移动到分组'))
    expect(moveButton).toBeTruthy()
    await moveButton!.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith('/api/watchlist/move', {
      stock_codes: ['600519'],
      from_group: DEFAULT_GROUP,
      to_group: GROUP,
    })
  })
})
