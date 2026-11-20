/**
 * 自选列表「表头拖动排序 + 可隐藏 + 按分组持久化」（2026-11-25 用户实测）。
 *
 * 用户原话：「我希望这个表头是可以拖动的……按住它之后就可以拖动，放下来之后就可以
 * 固定下来，**固定在这个分组上**」+「甚至删掉」（= 隐藏，且必须能勾回来）。
 *
 * 这份用例覆盖四件事：
 *   1. 拖动 → 列顺序立刻变 + 写进 **本分组** 的 localStorage key
 *   2. 换分组再切回来 → 顺序恢复（刷新页面同理，见 tests/watchlist-columns.test.ts）
 *   3. 隐藏列之后能在同一入口勾回来，且「恢复默认」真的回到默认
 *   4. 点表头排序照旧可用；拖放结束不会误触发排序
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
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
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean }).isAxiosError === true,
  }
  return { default: mock, ...mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'

const GROUP_A = '价值组'
const GROUP_B = '成长组'
/** 分组名要编码进 key：中文分组名不许原样出现在 localStorage key 里 */
const KEY_A = `vd.watchlist.columns.${encodeURIComponent(GROUP_A)}`

/** 默认可见列的表头顺序（= 改造前用户看到的表，13 列） */
const DEFAULT_HEADERS = [
  '代码', '名称', '交易所', '分组', '上市日期', '总市值(亿)', '收盘价(元)',
  'PE(倍)', 'PB(倍)', 'ROE(%)', '毛利率(%)', '负债率(%)', '来源',
]

function makeItem(code: string, name: string, group: string, marketCap: number) {
  return {
    stock_code: code, name, exchange: 'SSE', csrc_l1: '制造业', listing_date: '2020-01-01',
    group_name: group, source_rule_id: 42, source_result_id: null, added_at: '2026-08-01',
    latest_close: 10, pe_ttm: 10, pb_mrq: 1, roe: 0.1, gross_margin: 0.2, net_margin: 0.1,
    debt_ratio: 0.3, revenue_yoy: 0.1, net_profit_yoy: 0.1, dividend_yield: 0.01,
    total_market_cap: marketCap, circ_market_cap: marketCap / 2,
  }
}

const ITEMS: Record<string, ReturnType<typeof makeItem>[]> = {
  [GROUP_A]: [
    makeItem('000001', '平安银行', GROUP_A, 100),
    makeItem('600519', '贵州茅台', GROUP_A, 300),
    makeItem('300750', '宁德时代', GROUP_A, 200),
  ],
  [GROUP_B]: [makeItem('601318', '中国平安', GROUP_B, 500)],
}

function mockWatchlistApi() {
  const get = axios.get as Mock
  get.mockImplementation((url: string, config?: { params?: { group?: string } }) => {
    if (url !== '/api/watchlist/list') return Promise.resolve({ data: {} })
    const groups = [
      { group_name: GROUP_A, cnt: ITEMS[GROUP_A].length },
      { group_name: GROUP_B, cnt: ITEMS[GROUP_B].length },
    ]
    const group = config?.params?.group
    const items = group ? [...(ITEMS[group] ?? [])] : [...ITEMS[GROUP_A], ...ITEMS[GROUP_B]]
    return Promise.resolve({
      data: {
        items, count: items.length, groups,
        trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
      },
    })
  })
}

function mountPage() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
  })
  return mount(NDialogProvider, {
    slots: { default: () => h(NMessageProvider, null, { default: () => h(WatchlistPage) }) },
    global: { plugins: [router] },
  })
}

/** 表头里真实的列顺序（拖动把手 + 列名，每个可选列一个）。 */
function headerLabels(wrapper: VueWrapper): string[] {
  return wrapper.findAll('.wl-col-drag__label').map((node) => node.text())
}

function headerCell(wrapper: VueWrapper, columnKey: string) {
  const cell = wrapper.find(`.wl-col-drag[data-column-key="${columnKey}"]`)
  expect(cell.exists(), `表头里找不到列 ${columnKey}`).toBe(true)
  return cell
}

/** 表格正文里每一行的股票代码（用来验证「排序真的生效了」）。 */
function rowCodes(wrapper: VueWrapper): string[] {
  return wrapper.findAll('.n-data-table-tbody tr')
    .map((row) => row.text().match(/\d{6}/)?.[0] ?? '')
    .filter((code) => code !== '')
}

/**
 * 模拟一次「按住 → 拖到目标列 → 放下」。
 * drop 之后浏览器还会补一个 dragend，这里一起发，覆盖 endHeaderDrag 的收尾。
 */
async function dragColumn(wrapper: VueWrapper, fromKey: string, toKey: string) {
  const from = headerCell(wrapper, fromKey)
  const to = headerCell(wrapper, toKey)
  await from.trigger('dragstart')
  await to.trigger('dragover')
  await to.trigger('drop')
  await from.trigger('dragend')
  await flushPromises()
}

function storedPrefs(): { order: string[]; visible: string[] } | null {
  const raw = localStorage.getItem(KEY_A)
  return raw ? (JSON.parse(raw) as { order: string[]; visible: string[] }) : null
}

async function switchGroup(wrapper: VueWrapper, groupName: string) {
  const button = wrapper.findAll('.watchlist-groups button').find((node) => node.text().includes(groupName))
  expect(button, `侧栏找不到分组「${groupName}」`).toBeTruthy()
  await button!.trigger('click')
  await flushPromises()
}

async function openColumnSettings(wrapper: VueWrapper) {
  const button = wrapper.findAll('button').find((node) => node.text().includes('列设置'))
  expect(button, '找不到「列设置」入口').toBeTruthy()
  await button!.trigger('click')
  await flushPromises()
}

/** 列设置面板里某一列的勾选框（按中文列名找 li）。 */
function columnSettingRow(wrapper: VueWrapper, label: string) {
  const row = wrapper.findAll('.column-settings-list li').find((node) => node.text().includes(label))
  expect(row, `列设置面板里找不到「${label}」`).toBeTruthy()
  return row!
}

describe('自选列表：表头拖动排序 + 隐藏 + 按分组持久化', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockWatchlistApi()
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('默认列顺序与改造前一致，表头每列都带拖动把手', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(headerLabels(wrapper)).toEqual(DEFAULT_HEADERS)
    // 13 个可选列各有一个可拖动的表头（「操作」列不参与）
    expect(wrapper.findAll('.wl-col-drag')).toHaveLength(DEFAULT_HEADERS.length)
    expect(wrapper.findAll('.wl-col-drag').every((node) => node.attributes('draggable') === 'true')).toBe(true)
    // 没配置过就不该写存储
    expect(localStorage.getItem(KEY_A)).toBeNull()
    // 操作列永远在最后
    expect(wrapper.find('.n-data-table-thead').text()).toContain('操作')
  })

  it('拖动后列顺序立刻变化，并写入「本分组」的 localStorage key', async () => {
    const wrapper = mountPage()
    await flushPromises()

    // 把「PE」拖到「名称」前面（往左拖 → 落在目标左侧）
    await dragColumn(wrapper, 'pe_ttm', 'name')

    expect(headerLabels(wrapper)).toEqual([
      '代码', 'PE(倍)', '名称', '交易所', '分组', '上市日期', '总市值(亿)', '收盘价(元)',
      'PB(倍)', 'ROE(%)', '毛利率(%)', '负债率(%)', '来源',
    ])

    const stored = storedPrefs()
    expect(stored, `拖动后必须写入 ${KEY_A}`).toBeTruthy()
    expect(stored!.order.slice(0, 3)).toEqual(['stock_code', 'pe_ttm', 'name'])
    expect(stored!.visible.slice(0, 3)).toEqual(['stock_code', 'pe_ttm', 'name'])
    // 隐藏列仍在 order 里（这样重新勾选时能回到原位置），但不在 visible 里
    expect(stored!.order).toContain('circ_market_cap')
    expect(stored!.visible).not.toContain('circ_market_cap')
    // 别的分组不受影响
    expect(localStorage.getItem(`vd.watchlist.columns.${encodeURIComponent(GROUP_B)}`)).toBeNull()
  })

  it('拖动正在进行的表头有可见反馈（半透明 + 目标列插入位置）', async () => {
    const wrapper = mountPage()
    await flushPromises()

    const from = headerCell(wrapper, 'pe_ttm')
    const to = headerCell(wrapper, 'name')
    await from.trigger('dragstart')
    await to.trigger('dragover')
    expect(headerCell(wrapper, 'pe_ttm').classes()).toContain('wl-col-drag--dragging')
    // 往左拖 → 落点在目标左侧
    expect(headerCell(wrapper, 'name').classes()).toContain('wl-col-drag--drop-before')

    await to.trigger('drop')
    await from.trigger('dragend')
    await flushPromises()
    // 放下之后反馈消失
    expect(headerCell(wrapper, 'name').classes()).not.toContain('wl-col-drag--drop-before')
    expect(headerCell(wrapper, 'pe_ttm').classes()).not.toContain('wl-col-drag--dragging')
  })

  it('换到别的分组用默认列，切回来顺序恢复', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await dragColumn(wrapper, 'pe_ttm', 'name')
    const moved = headerLabels(wrapper)
    expect(moved[1]).toBe('PE(倍)')

    await switchGroup(wrapper, GROUP_B)
    expect(headerLabels(wrapper)).toEqual(DEFAULT_HEADERS)

    await switchGroup(wrapper, GROUP_A)
    expect(headerLabels(wrapper)).toEqual(moved)
  })

  it('列设置里取消勾选可隐藏，再勾回来回到原来的位置', async () => {
    const wrapper = mountPage()
    await flushPromises()
    // 先把 PE 拖到第 2 列，验证「隐藏再恢复」是回到原位而不是回到末尾
    await dragColumn(wrapper, 'pe_ttm', 'name')
    await openColumnSettings(wrapper)

    // 隐藏 PE：表里立刻不见，配置里记录 visible 少了它、order 仍保留
    await columnSettingRow(wrapper, '市盈率（PE-TTM）').find('.n-checkbox').trigger('click')
    await flushPromises()
    expect(headerLabels(wrapper)).not.toContain('PE(倍)')
    expect(storedPrefs()!.visible).not.toContain('pe_ttm')
    expect(storedPrefs()!.order).toContain('pe_ttm')
    // 面板仍然列着它（否则就变成不可恢复的删除了）
    expect(columnSettingRow(wrapper, '市盈率（PE-TTM）').text()).toContain('市盈率（PE-TTM）')

    // 勾回来 → 回到第 2 列
    await columnSettingRow(wrapper, '市盈率（PE-TTM）').find('.n-checkbox').trigger('click')
    await flushPromises()
    expect(headerLabels(wrapper).slice(0, 3)).toEqual(['代码', 'PE(倍)', '名称'])
    expect(storedPrefs()!.visible.slice(0, 3)).toEqual(['stock_code', 'pe_ttm', 'name'])
  })

  it('「恢复默认」把顺序与可见集合还原，并清掉本分组配置', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await dragColumn(wrapper, 'pe_ttm', 'name')
    await openColumnSettings(wrapper)
    await columnSettingRow(wrapper, '市净率（PB-MRQ）').find('.n-checkbox').trigger('click')
    await flushPromises()
    expect(headerLabels(wrapper)).not.toContain('PB(倍)')

    const resetButton = wrapper.findAll('button').find((node) => node.text().includes('恢复默认'))
    expect(resetButton, '列设置里必须有「恢复默认」').toBeTruthy()
    await resetButton!.trigger('click')
    await flushPromises()

    expect(headerLabels(wrapper)).toEqual(DEFAULT_HEADERS)
    expect(localStorage.getItem(KEY_A), '恢复默认 = 回到「这个分组没配置过」').toBeNull()
  })

  it('列设置里的 ▲▼ 也能调顺序（拖不动鼠标时的替代路径）', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await openColumnSettings(wrapper)

    // 「股票名称」默认在第 2 列，▲ 一次 → 第 1 列
    const nameRow = columnSettingRow(wrapper, '股票名称')
    await nameRow.findAll('button')[0].trigger('click')
    await flushPromises()
    expect(headerLabels(wrapper).slice(0, 2)).toEqual(['名称', '代码'])
    expect(storedPrefs()!.order.slice(0, 2)).toEqual(['name', 'stock_code'])
  })

  it('「全部显示」把被隐藏的三列（流通市值/净利率/营收YoY）也放出来，且都在正确位置', async () => {
    // 这三列的渲染函数只在「勾选显示」时才用到 —— 这条用例同时守住
    // 「可选列清单的每一项都有对应的单元格渲染」这个契约（缺了就只剩少一列的表头）。
    const wrapper = mountPage()
    await flushPromises()
    await openColumnSettings(wrapper)

    const showAll = wrapper.findAll('button').find((node) => node.text().includes('全部显示'))
    expect(showAll, '列设置里必须有「全部显示」').toBeTruthy()
    await showAll!.trigger('click')
    await flushPromises()

    expect(headerLabels(wrapper)).toEqual([
      '代码', '名称', '交易所', '分组', '上市日期', '总市值(亿)', '流通市值(亿)', '收盘价(元)',
      'PE(倍)', 'PB(倍)', 'ROE(%)', '毛利率(%)', '净利率(%)', '负债率(%)', '营收YoY(%)', '来源',
    ])
    // 多出来的列真的渲染出数值，而不是空白
    const firstRow = wrapper.findAll('.n-data-table-tbody tr')[0].text()
    expect(firstRow).toContain('50.00')  // 流通市值 = 总市值 100/2
    expect(firstRow).toContain('10.00%') // 净利率 0.1 → 10.00%
    expect(storedPrefs()!.visible).toHaveLength(16)
  })

  it('点表头排序照旧可用（每次排序都能用 sorter），拖动不会误触发排序', async () => {
    const wrapper = mountPage()
    await flushPromises()
    // 接口返回顺序：平安银行(100) / 贵州茅台(300) / 宁德时代(200)
    expect(rowCodes(wrapper)).toEqual(['000001', '600519', '300750'])

    // naive-ui 的点表头循环是 降序 → 升序 → 取消（getNextOrderOf：无 → descend → ascend → false）。
    // 真实点击一定先 mousedown（拖放之后的幽灵 click 没有 mousedown）。
    const marketCapHeader = headerCell(wrapper, 'total_market_cap')
    await marketCapHeader.trigger('mousedown')
    await marketCapHeader.trigger('click')
    await flushPromises()
    expect(rowCodes(wrapper)).toEqual(['600519', '300750', '000001'])

    // 再点一次升序
    await marketCapHeader.trigger('mousedown')
    await marketCapHeader.trigger('click')
    await flushPromises()
    expect(rowCodes(wrapper)).toEqual(['000001', '300750', '600519'])

    // 拖一次列：拖放结束紧跟的那一次 click 不能当成点表头排序
    await dragColumn(wrapper, 'pe_ttm', 'name')
    const sortedBefore = rowCodes(wrapper)
    await headerCell(wrapper, 'total_market_cap').trigger('click')
    await flushPromises()
    expect(rowCodes(wrapper), '拖放后的幽灵 click 不许改排序').toEqual(sortedBefore)

    // 之后正常点（带 mousedown）继续能排序：第三次点 = 取消排序，回到接口顺序
    const header = headerCell(wrapper, 'total_market_cap')
    await header.trigger('mousedown')
    await header.trigger('click')
    await flushPromises()
    expect(rowCodes(wrapper)).toEqual(['000001', '600519', '300750'])
  })

  it('拖动后换分组再切回来，排序与列顺序都还在', async () => {
    const wrapper = mountPage()
    await flushPromises()
    await dragColumn(wrapper, 'roe', 'stock_code')
    expect(headerLabels(wrapper)[0]).toBe('ROE(%)')
    const reordered = headerLabels(wrapper)

    await switchGroup(wrapper, GROUP_B)
    await switchGroup(wrapper, GROUP_A)
    expect(headerLabels(wrapper)).toEqual(reordered)
    expect(storedPrefs()!.order[0]).toBe('roe')
  })
})
