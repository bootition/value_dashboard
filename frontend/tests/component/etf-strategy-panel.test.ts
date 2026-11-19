/**
 * EtfStrategyPanel 行为快照（2026-11-23 重构前先写，**拆之前必须先全绿**）。
 *
 * 为什么先写：本组件 738 行里塞了 17 个 ref / 6 个模态（详情 + 5 张录入表单）。
 * 上一轮判断「不该拆」的理由是「5 个模态没有行为测试覆盖，等价无法证明」。
 * 本文件就是那份"等价证明"的底稿：先把它钉在**现有实现**上跑绿，再动代码。
 *
 * 覆盖四类（与拆分点的对应关系）：
 * 1. 打开/关闭每一个模态 —— 拆出 `useEtfOperations` 后 show 标志仍由组件持有，
 *    但 open/close 的**复位语义**（每次打开都重置表单）在 composable 里。
 * 2. 表单字段 → 提交 payload 形状 —— 后端契约，一个字段都不能多/少/改名。
 * 3. 提交成功 → 列表刷新 —— `await load()` 必须在成功路径上（失败路径绝不许刷）。
 * 4. 校验失败 / 请求失败 —— 提示文案 + **不关弹窗、不丢输入**（后端无幂等键，
 *    静默关窗或清空表单会造成重复录入或用户重填）。
 *
 * 交互方式沿用仓库既有约定（`rule-condition-row.test.ts`）：naive-ui 组件的
 * 真实交互（虚拟列表下拉、数字输入的 blur 提交）在 jsdom 里极不稳定，
 * 因此直接向子组件 `$emit('update:value', …)`——这与用户操作走的是同一条
 * v-model 通路，且重构后组件树形状不变，测试可原样复用。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises, DOMWrapper } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { h } from 'vue'
import { NDialogProvider, NInput, NInputNumber, NMessageProvider, NModal, NSelect } from 'naive-ui'

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
import EtfStrategyPanel from '../../src/components/EtfStrategyPanel.vue'

// ─── fixtures ────────────────────────────────────────────────────────────

function position(buyCount: number) {
  return {
    etf_code: '510300',
    shares: buyCount > 0 ? 1000 : 0,
    cost_basis: buyCount > 0 ? 4000 : 0,
    avg_cost: buyCount > 0 ? 4 : null,
    realized_pnl: 0,
    buy_count: buyCount,
    sell_count: 0,
    total_buy_amount: buyCount > 0 ? 4000 : 0,
    total_buy_fee: 0,
    total_sell_amount: 0,
    total_sell_fee: 0,
    last_buy_price: buyCount > 0 ? 4 : null,
    last_sell_price: null,
    first_buy_date: buyCount > 0 ? '2026-01-05' : null,
  }
}

const ITEM_INDUSTRY = {
  etf_code: '510300',
  name: '沪深300ETF',
  category: 'industry' as const,
  track_index_code: '000300',
  track_index_name: '沪深300',
  primary_metric: 'pe' as const,
  industry_group: '宽基',
  step_pct: 5,
  budget: 10000,
  tranche_amount: 1000,
  used_budget: 4000,
  budget_left: 6000,
  signal: 'buy' as const,
  current_price: 4.12,
  market_value: 4120,
  unrealized_pnl: 120,
  remaining_buys: 6,
  next_buy_price: 3.91,
  remaining_sells: 10,
  next_sell_price: 4.33,
  sell_tranche_amount: 1000,
  sell_tranches_done: 2,
  clear_tail: false,
  enabled: true,
  position: position(5),
  valuation: {
    status: 'ok' as const,
    samples: 250,
    pe: 12.3,
    pe_percentile: 0.21,
    pb: 1.4,
    pb_percentile: 0.33,
    erp: 0.043,
    erp_percentile: 0.5,
    latest_date: '2026-11-20',
  },
  percentile: 0.21,
  percentile_label: 'PE 分位',
}

const ITEM_STRATEGY = {
  ...ITEM_INDUSTRY,
  etf_code: '159915',
  name: '创业板ETF',
  category: 'strategy' as const,
  track_index_code: '399006',
  track_index_name: '创业板指',
  primary_metric: 'pb' as const,
  industry_group: null,
  budget: 8000,
  step_pct: 5,
  position: { ...position(0), etf_code: '159915' },
  signal: 'unavailable' as const,
  percentile: null,
  percentile_label: '分位不可得',
}

const OVERVIEW = {
  items: [ITEM_INDUSTRY, ITEM_STRATEGY],
  total_assets: '100000',
  cash_net_in: 50000,
  market_value: 4120,
  unrealized_pnl: 120,
  realized_pnl: 0,
}

/** 详情模态：detail + fundamentals 两个端点。 */
const DETAIL = {
  ...ITEM_INDUSTRY,
  trades: [],
  cash_flows: [],
  track_valuation: null,
  settings: { total_assets: '100000', budget: 10000, step_pct: 5 },
}

const FUNDAMENTALS = {
  etf_code: '510300',
  name: '沪深300ETF',
  method: 'sum_of_constituents',
  companies: 300,
  latest_profit: 1234.5,
  latest_market_cap: 98765.4,
  earnings: [],
  market_cap: [],
  profit_growth: [],
  profit_growth_qoq: [],
  industry_contribution: [],
  industry_contribution_qoq: [],
  disclaimer: '合计数为成分股简单加总，非基金口径。',
}

// ─── harness ─────────────────────────────────────────────────────────────

interface OverviewOptions {
  /** 让 /overview 首次返回数据、之后的请求按此值应答（用于验证"提交后刷新"） */
  overviewSequence?: number
}

function setupAxiosMock(overview: Record<string, unknown> = OVERVIEW, options: OverviewOptions = {}) {
  let overviewCalls = 0
  const get = axios.get as Mock
  get.mockImplementation((url: string) => {
    if (url.includes('/api/etf/overview')) {
      overviewCalls += 1
      if (options.overviewSequence != null && overviewCalls > options.overviewSequence) {
        return Promise.reject(Object.assign(new Error('boom'), { isAxiosError: true, response: { status: 500 } }))
      }
      return Promise.resolve({ data: overview })
    }
    if (url.includes('/detail')) return Promise.resolve({ data: DETAIL })
    if (url.includes('/fundamentals')) return Promise.resolve({ data: FUNDAMENTALS })
    if (url.includes('/reset-preview')) return Promise.resolve({ data: { current: { etf_meta: 3 } } })
    return Promise.resolve({ data: {} })
  })
  ;(axios.post as Mock).mockResolvedValue({ data: { status: 'ok' } })
  return {
    overviewCallCount: () => overviewCalls,
  }
}

async function mountPanel(): Promise<VueWrapper> {
  const wrapper = mount(NMessageProvider, {
    slots: {
      default: () => h(NDialogProvider, null, { default: () => h(EtfStrategyPanel) }),
    },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

/** 面板根组件（providers 之下的那个 EtfStrategyPanel 实例）。 */
function panel(wrapper: VueWrapper) {
  return wrapper.findComponent(EtfStrategyPanel)
}

function modalByTitle(wrapper: VueWrapper, title: string) {
  const found = wrapper.findAllComponents(NModal).find((m) => m.props('title') === title)
  if (!found) throw new Error(`找不到标题为「${title}」的模态`)
  return found
}

/** 当前可见模态内容（naive-ui 把 preset=card 的内容 teleport 到 body）。 */
function bodyText(): string {
  return document.body.textContent ?? ''
}

function buttonByText(text: string): HTMLButtonElement {
  const hit = Array.from(document.body.querySelectorAll('button')).find(
    (b) => (b.textContent ?? '').trim() === text,
  )
  if (!hit) throw new Error(`找不到文案为「${text}」的按钮`)
  return hit as HTMLButtonElement
}

async function clickButton(text: string) {
  buttonByText(text).click()
  await flushPromises()
}

/** 打开某模态内部的第 n 个 NSelect/NInputNumber/NInput 并回填值。 */
async function setSelect(wrapper: VueWrapper, index: number, value: unknown) {
  const selects = panel(wrapper).findAllComponents(NSelect)
  await selects[index].vm.$emit('update:value', value)
  await flushPromises()
}

async function setNumber(wrapper: VueWrapper, index: number, value: unknown) {
  const inputs = panel(wrapper).findAllComponents(NInputNumber)
  await inputs[index].vm.$emit('update:value', value)
  await flushPromises()
}

/**
 * 纯文本输入。**必须排掉 NInputNumber 内部包着的那个 NInput**，
 * 否则下标会整体错位（naive-ui 的 NInputNumber 渲染时内含一个 NInput）。
 */
async function setText(wrapper: VueWrapper, index: number, value: unknown) {
  const inputs = panel(wrapper)
    .findAllComponents(NInput)
    .filter((c) => !(c.element as HTMLElement).closest('.n-input-number'))
  await inputs[index].vm.$emit('update:value', value)
  await flushPromises()
}

/** 点关闭按钮：必须在**当前打开的那个** .n-modal 里找，避免命中上一个模态的残留节点。 */
async function closeModalByTitle(title: string) {
  const modal = Array.from(document.body.querySelectorAll('.n-modal')).find(
    (el) => (el.textContent ?? '').includes(title),
  )
  if (!modal) throw new Error(`body 里找不到标题为「${title}」的模态`)
  const close = modal.querySelector('.n-base-close') as HTMLElement | null
  if (!close) throw new Error(`标题为「${title}」的模态没有关闭按钮`)
  await new DOMWrapper(close).trigger('click')
  await flushPromises()
  await flushPromises()
}

function postCalls(): Array<[string, unknown]> {
  return (axios.post as Mock).mock.calls as Array<[string, unknown]>
}

// ─── tests ───────────────────────────────────────────────────────────────

describe('EtfStrategyPanel 模态开关（拆分前置快照）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    setupAxiosMock()
  })

  it('概览渲染后可通过工具栏/表格行打开并关闭 5 个录入模态', async () => {
    const wrapper = await mountPanel()

    const cases: Array<[string, string]> = [
      ['设置总资产', '策略总资产'],
      ['录入初始持仓', '录入初始持仓'],
      ['录入买卖', '录入买卖'],
      ['录入资金', '录入资金流水'],
      ['预算', 'ETF 预算与设置'],
    ]
    for (const [trigger, title] of cases) {
      await clickButton(trigger)
      expect(modalByTitle(wrapper, title).props('show')).toBe(true)
      expect(bodyText()).toContain(title)
      await closeModalByTitle(title)
      expect(modalByTitle(wrapper, title).props('show')).toBe(false)
    }
  })

  it('表格行内「详情」打开详情模态（标题带 ETF 名）', async () => {
    const wrapper = await mountPanel()

    await clickButton('详情')
    expect(modalByTitle(wrapper, '沪深300ETF · 估值与基本面').props('show')).toBe(true)
    await closeModalByTitle('估值与基本面')
    expect(modalByTitle(wrapper, '沪深300ETF · 估值与基本面').props('show')).toBe(false)
  })

  it('重新开始先取预览再弹确认框，未确认前不发 POST', async () => {
    await mountPanel()
    await clickButton('重新开始')
    expect(bodyText()).toContain('确认清空')
    expect(bodyText()).toContain('etf_meta=3')
    expect(postCalls()).toEqual([])
  })
})

describe('EtfStrategyPanel 提交 payload 形状（后端契约）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    setupAxiosMock()
  })

  it('录入买卖：表单字段原样提交到 /api/etf/trades', async () => {
    const wrapper = await mountPanel()
    await clickButton('录入买卖')

    await setSelect(wrapper, 0, '159915') // ETF
    await setSelect(wrapper, 1, 'sell') // 方向
    await setText(wrapper, 0, '2026-11-02') // 日期
    await setNumber(wrapper, 0, 4.321) // 价格
    await setNumber(wrapper, 1, 500) // 份额
    await setNumber(wrapper, 2, 1.5) // 手续费

    await clickButton('保存')

    expect(postCalls()).toHaveLength(1)
    const [url, payload] = postCalls()[0]
    expect(url).toBe('/api/etf/trades')
    expect(payload).toEqual({
      etf_code: '159915',
      trade_date: '2026-11-02',
      direction: 'sell',
      price: 4.321,
      shares: 500,
      fee: 1.5,
    })
  })

  it('录入资金：表单字段原样提交到 /api/etf/cash-flows', async () => {
    const wrapper = await mountPanel()
    await clickButton('录入资金')

    await setText(wrapper, 0, '2026-11-03') // 日期
    await setSelect(wrapper, 0, 'out') // 类型
    await setNumber(wrapper, 0, 2000) // 金额

    await clickButton('保存')

    expect(postCalls()).toHaveLength(1)
    const [url, payload] = postCalls()[0]
    expect(url).toBe('/api/etf/cash-flows')
    expect(payload).toEqual({ flow_date: '2026-11-03', direction: 'out', amount: 2000 })
  })

  it('设置总资产：提交 key=total_assets 的字符串值', async () => {
    const wrapper = await mountPanel()
    await clickButton('设置总资产')

    await setNumber(wrapper, 0, 120000)
    await clickButton('保存')

    expect(postCalls()).toHaveLength(1)
    const [url, payload] = postCalls()[0]
    expect(url).toBe('/api/etf/settings')
    expect(payload).toEqual({ key: 'total_assets', value: '120000' })
  })

  it('录入初始持仓：嵌套 positions[0] 带上元数据与表单值', async () => {
    const wrapper = await mountPanel()
    await clickButton('录入初始持仓')

    await setSelect(wrapper, 0, '159915') // ETF（元数据取自列表项）
    await setText(wrapper, 0, '2026-11-04') // 买入日期
    await setNumber(wrapper, 0, 3.789) // 买入价格
    await setNumber(wrapper, 1, 2000) // 持有份额
    await setNumber(wrapper, 2, 2.5) // 手续费
    await setNumber(wrapper, 3, 6000) // 预算
    await setNumber(wrapper, 4, 7) // 网格间距

    await clickButton('保存并开始网格')

    expect(postCalls()).toHaveLength(1)
    const [url, payload] = postCalls()[0]
    expect(url).toBe('/api/etf/bootstrap')
    expect(payload).toEqual({
      total_assets: null,
      positions: [{
        etf_code: '159915',
        name: '创业板ETF',
        category: 'strategy',
        track_index_code: '399006',
        track_index_name: '创业板指',
        primary_metric: 'pb',
        industry_group: null,
        budget: 6000,
        step_pct: 7,
        shares: 2000,
        price: 3.789,
        trade_date: '2026-11-04',
        fee: 2.5,
      }],
    })
  })

  it('预算/设置：先写总资产再写 meta，meta 字段取列表现状 + 表单改动', async () => {
    const wrapper = await mountPanel()
    await clickButton('预算')

    await setSelect(wrapper, 1, 'market') // 层级
    await setNumber(wrapper, 0, 12345) // 预算
    await setNumber(wrapper, 1, 8) // 网格间距
    await setText(wrapper, 0, '150000') // 策略总资产

    await clickButton('保存')

    expect(postCalls()).toHaveLength(2)
    expect(postCalls()[0][0]).toBe('/api/etf/settings')
    expect(postCalls()[0][1]).toEqual({ key: 'total_assets', value: '150000' })
    expect(postCalls()[1][0]).toBe('/api/etf/meta')
    expect(postCalls()[1][1]).toEqual({
      etf_code: '510300',
      name: '沪深300ETF',
      category: 'market',
      track_index_code: '000300',
      track_index_name: '沪深300',
      primary_metric: 'pe',
      industry_group: '宽基',
      budget: 12345,
      step_pct: 8,
      enabled: true,
    })
  })
})

describe('EtfStrategyPanel 提交成功后刷新 / 失败时保留输入', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
  })

  it('提交成功后关闭模态并重新拉取 overview', async () => {
    const mock = setupAxiosMock()
    const wrapper = await mountPanel()
    expect(mock.overviewCallCount()).toBe(1)

    await clickButton('录入买卖')
    await setSelect(wrapper, 0, '510300')
    await setNumber(wrapper, 0, 4.1)
    await setNumber(wrapper, 1, 100)
    await clickButton('保存')

    expect(mock.overviewCallCount()).toBe(2)
    expect(modalByTitle(wrapper, '录入买卖').props('show')).toBe(false)
    expect(bodyText()).toContain('交易已录入')
  })

  it('校验失败：提示文案 + 不关弹窗 + 不丢已填输入 + 不发请求', async () => {
    setupAxiosMock()
    const wrapper = await mountPanel()

    await clickButton('录入买卖')
    await setSelect(wrapper, 0, '510300') // 只填 ETF，价格/份额留空
    await clickButton('保存')

    expect(bodyText()).toContain('请填写 ETF、价格与份额')
    expect(modalByTitle(wrapper, '录入买卖').props('show')).toBe(true)
    expect(postCalls()).toEqual([])
    // 输入未丢：ETF 选择仍在
    expect(panel(wrapper).findAllComponents(NSelect)[0].props('value')).toBe('510300')
  })

  it('初始持仓校验失败：提示文案 + 保留价格输入', async () => {
    setupAxiosMock()
    const wrapper = await mountPanel()

    await clickButton('录入初始持仓')
    await setSelect(wrapper, 0, '510300')
    await setNumber(wrapper, 0, 3.5) // 只填价格，份额留空
    await clickButton('保存并开始网格')

    expect(bodyText()).toContain('请填写初始持仓的买入价格与份额')
    expect(postCalls()).toEqual([])
    expect(modalByTitle(wrapper, '录入初始持仓').props('show')).toBe(true)
    expect(panel(wrapper).findAllComponents(NInputNumber)[0].props('value')).toBe(3.5)
  })

  it('请求失败：展示后端映射文案，弹窗与输入保留，且不刷新列表', async () => {
    const mock = setupAxiosMock()
    ;(axios.post as Mock).mockRejectedValue(Object.assign(new Error('conflict'), {
      isAxiosError: true,
      response: { status: 409 },
    }))
    const wrapper = await mountPanel()

    await clickButton('录入资金')
    await setText(wrapper, 0, '2026-11-05')
    await setNumber(wrapper, 0, 3000)
    await clickButton('保存')

    expect(bodyText()).toContain('操作冲突，提交失败')
    expect(modalByTitle(wrapper, '录入资金流水').props('show')).toBe(true)
    expect(panel(wrapper).findAllComponents(NInput)[0].props('value')).toBe('2026-11-05')
    expect(mock.overviewCallCount()).toBe(1)
  })

  it('总资产必须大于 0：拒绝 0 并表示文案', async () => {
    setupAxiosMock()
    const wrapper = await mountPanel()

    await clickButton('设置总资产')
    await setNumber(wrapper, 0, 0)
    await clickButton('保存')

    expect(bodyText()).toContain('请输入大于 0 的策略总资产')
    expect(postCalls()).toEqual([])
    expect(modalByTitle(wrapper, '策略总资产').props('show')).toBe(true)
  })
})

describe('EtfStrategyPanel 详情模态的降级（两路并发）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('fundamentals 失败时只在该卡片内报错，估值区照常渲染', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url.includes('/api/etf/overview')) return Promise.resolve({ data: OVERVIEW })
      if (url.includes('/detail')) return Promise.resolve({ data: DETAIL })
      if (url.includes('/fundamentals')) {
        return Promise.reject(Object.assign(new Error('timeout'), {
          isAxiosError: true,
          code: 'ECONNABORTED',
        }))
      }
      return Promise.resolve({ data: {} })
    })

    await mountPanel()
    await clickButton('详情')

    const text = bodyText()
    expect(text).toContain('估值与基本面')
    // 详情主数据仍在
    expect(text).toContain('PE 分位')
    // 基本面卡内的局部错误
    expect(text).toContain('无法连接服务器')
    // 页面级错误提示不出现
    expect(text).not.toContain('加载 ETF 列表失败')
  })
})
