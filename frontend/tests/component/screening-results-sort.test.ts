/**
 * 结果表「点表头就排序」—— 用户裁定的替代方案，必须真的能用（2026-11-24）。
 *
 * 用户原话：「结果的排名在结果那里自己选：点一下那个表头，就可以按照市值来排；
 * 点一下 ROE 这个表头，就按 ROE 来排。」条件行里的「排名口径」下拉已按此移除，
 * 所以这里**必须真点表头 → 断言行顺序变化**，而不是只检查代码里有 `sorter`。
 *
 * 点击语义（naive-ui 的 `createNextSorter` + `getNextOrderOf(false) === 'descend'`）：
 * 新列的第一次点击是**降序**，第二次升序，第三次取消排序。
 *
 * 覆盖三类真实故障：
 * 1. 数值列（总市值，底层是"元"，展示是"亿元"）：降序/升序都按**数值**。
 * 2. 数值字符串（后端 DECIMAL 存档后回读成 `"1.2e+11"` 这种字符串，
 *    见 `app/web/api/screening.py` 的 `json.dumps(..., default=str)`）：
 *    naive-ui 的 `sorter: 'default'` 会用 `localeCompare` 字典序，
 *    "9e+10" 与 "3.5e+11" 的先后会反 —— 这里断言不是那个结果。
 * 3. 空值（roce_10y_median 对金融股/历史不足的股票是 NULL）：
 *    两个方向都必须留在末尾，不能插在中间让人以为"点了没排序"。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider } from 'naive-ui'
import { h, nextTick } from 'vue'
import ScreeningResultsPanel from '../../src/components/ScreeningResultsPanel.vue'
import type { ScreeningResult } from '../../src/types/screening.ts'

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

/** 每条结果的公共列（后端 /run 必带），排序用例不关心它们的取值。 */
const BASE = {
  name: 'x',
  exchange: 'SSE',
  csrc_l1: '制造业',
  latest_close: 10,
  pe_ttm: 10,
  pb_mrq: 1,
  roe: 0.1,
  gross_margin: 0.1,
  net_margin: 0.1,
  debt_ratio: 0.1,
  revenue_yoy: 0.1,
  dividend_yield: 0.01,
}
const CODES = ['600519', '000858', '601318']

function makeRows(extra: Array<Record<string, number | string | null>>): ScreeningResult[] {
  return extra.map((fields, index) => ({
    ...BASE,
    stock_code: CODES[index] ?? `00000${index}`,
    ...fields,
  })) as unknown as ScreeningResult[]
}

function mountPanel(results: ScreeningResult[]) {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  return mount(NMessageProvider, {
    slots: {
      default: () =>
        h(ScreeningResultsPanel, {
          results,
          executionTime: 5,
          basePoolSize: 100,
          dataDate: '2026-06-30',
          warningCodes: [],
          untrustedFields: [],
          qualityStatus: 'available',
          ruleTree: { id: 'r1', logic: 'AND', rules: [] },
          runId: 'run-sort',
          ruleId: 1,
          ruleVersion: 1,
          lockedIndicators: {},
          basePoolConfig: { include_st: false },
        }),
    },
    global: { plugins: [router] },
  })
}

type Panel = ReturnType<typeof mountPanel>

/** 当前渲染出来的股票代码顺序 = 表格行顺序 = 排序结果。 */
function renderedCodes(wrapper: Panel): string[] {
  return wrapper.findAll('a.stock-link').map((node) => node.text())
}

/** 点某一列的表头（naive-ui 的 `th` 就是整个点击区域）。 */
async function clickHeader(wrapper: Panel, key: string): Promise<void> {
  const th = wrapper.find(`th[data-col-key="${key}"]`)
  expect(th.exists(), `找不到列头 ${key}`).toBe(true)
  await th.trigger('click')
  await nextTick()
  await flushPromises()
}

describe('结果表列头排序：点一下就按该列排（用户裁定的替代方案）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('总市值（元 → 展示亿元）：点表头降序、再点升序、三点取消排序，全程按数值', async () => {
    const wrapper = mountPanel(makeRows([
      { total_market_cap: 1.2e11 },  // 1200 亿
      { total_market_cap: 9e10 },    // 900 亿
      { total_market_cap: 3.5e11 },  // 3500 亿
    ]))
    await flushPromises()

    // 中文表头（带单位）+ 可排序
    const header = wrapper.find('th[data-col-key="total_market_cap"]')
    expect(header.text()).toContain('总市值')
    expect(header.classes()).toContain('n-data-table-th--sortable')

    // 未排序：后端给的原始顺序
    expect(renderedCodes(wrapper)).toEqual(['600519', '000858', '601318'])

    // 第 1 次点击：降序 3500 亿 > 1200 亿 > 900 亿
    await clickHeader(wrapper, 'total_market_cap')
    expect(renderedCodes(wrapper)).toEqual(['601318', '600519', '000858'])

    // 第 2 次点击：升序 900 亿 < 1200 亿 < 3500 亿
    await clickHeader(wrapper, 'total_market_cap')
    expect(renderedCodes(wrapper)).toEqual(['000858', '600519', '601318'])

    // 第 3 次点击：取消排序，回到原始顺序
    await clickHeader(wrapper, 'total_market_cap')
    expect(renderedCodes(wrapper)).toEqual(['600519', '000858', '601318'])
  })

  it('中文列名本身也是点击区域（点标题文字就能排序）', async () => {
    const wrapper = mountPanel(makeRows([
      { total_market_cap: 1.2e11 },
      { total_market_cap: 9e10 },
      { total_market_cap: 3.5e11 },
    ]))
    await flushPromises()

    const title = wrapper.find('th[data-col-key="total_market_cap"] .n-data-table-th__title')
    expect(title.exists()).toBe(true)
    expect(title.text()).toContain('总市值')
    await title.trigger('click')
    await nextTick()
    await flushPromises()

    expect(renderedCodes(wrapper)).toEqual(['601318', '600519', '000858'])
  })

  it('数值字符串（存档回读形态 "1.2e+11"）：按数值排，不是字典序', async () => {
    const wrapper = mountPanel(makeRows([
      { total_market_cap: '1.2e+11' },
      { total_market_cap: '9e+10' },
      { total_market_cap: '3.5e+11' },
    ]))
    await flushPromises()

    await clickHeader(wrapper, 'total_market_cap')
    // 数值降序：3.5e+11 > 1.2e+11 > 9e+10
    // 字典序降序会得到 000858("9e+10") → 601318 → 600519，即下面这个断言的镜像
    expect(renderedCodes(wrapper)).toEqual(['601318', '600519', '000858'])
  })

  it('ROCE 10 年中位（百分比小数）：按数值排，空值两个方向都留在末尾', async () => {
    const wrapper = mountPanel(makeRows([
      { roce_10y_median: 0.18 },
      { roce_10y_median: null },
      { roce_10y_median: 0.09 },
    ]))
    await flushPromises()

    const header = wrapper.find('th[data-col-key="roce_10y_median"]')
    expect(header.text()).toContain('ROCE')
    expect(header.text()).toContain('10年中位')

    // 降序：18% > 9%，NULL 在末尾（不能插在中间）
    await clickHeader(wrapper, 'roce_10y_median')
    expect(renderedCodes(wrapper)).toEqual(['600519', '601318', '000858'])

    // 升序：9% < 18%，NULL 仍在末尾
    await clickHeader(wrapper, 'roce_10y_median')
    expect(renderedCodes(wrapper)).toEqual(['601318', '600519', '000858'])
  })

  it('点另一个表头会切换到新列排序（单列排序，不叠加）', async () => {
    const wrapper = mountPanel(makeRows([
      { total_market_cap: 1.2e11, roce_10y_median: 0.18 },
      { total_market_cap: 9e10, roce_10y_median: 0.31 },
      { total_market_cap: 3.5e11, roce_10y_median: 0.09 },
    ]))
    await flushPromises()

    await clickHeader(wrapper, 'total_market_cap')
    expect(renderedCodes(wrapper)).toEqual(['601318', '600519', '000858'])

    // 改按 ROCE 排（新列 → 降序）：31% > 18% > 9%
    await clickHeader(wrapper, 'roce_10y_median')
    expect(renderedCodes(wrapper)).toEqual(['000858', '600519', '601318'])
  })
})
