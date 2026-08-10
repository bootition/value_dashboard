import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider } from 'naive-ui'
import { h } from 'vue'
import StockDetailPage from '../../src/views/StockDetailPage.vue'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
    isCancel: () => false,
  }
  return { default: mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'

const INDICATORS_UNTRUSTED = {
  report_date: '2026-06-30',
  latest_close: null,
  latest_price_date: '2026-07-31',
  freshness: { stale_warning: false, price_age_days: 1, financial_age_days: 30, snapshot_age_days: 1 },
  trust: { warning_codes: ['LINEAGE_INVALID'], untrusted_all: true, untrusted_fields: [] },
  indicators: {
    valuation: { pe_ttm: { value: null, historical_capable: false, untrusted: true }, pb_mrq: { value: null, historical_capable: false, untrusted: true } },
    profitability: { roe: { value: null, historical_capable: true, untrusted: true } },
    safety: { debt_ratio: { value: null, historical_capable: true, untrusted: true } },
  },
}

const INDICATORS_NORMAL = {
  report_date: '2026-06-30',
  latest_close: 1680.5,
  latest_price_date: '2026-07-31',
  freshness: { stale_warning: false, price_age_days: 1, financial_age_days: 30, snapshot_age_days: 1 },
  trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
  indicators: {
    valuation: { pe_ttm: { value: 25.4, historical_capable: false }, pb_mrq: { value: 8.2, historical_capable: false }, ps_ttm: { value: 12.1, historical_capable: false }, pcf_ttm: { value: null, historical_capable: false }, dividend_yield: { value: 0.023, historical_capable: false }, total_market_cap: { value: 2.1e12, historical_capable: false }, circ_market_cap: { value: 2.0e12, historical_capable: false } },
    profitability: { roe: { value: 0.32, historical_capable: true }, roa: { value: 0.25, historical_capable: true }, gross_margin: { value: 0.91, historical_capable: true }, net_margin: { value: 0.52, historical_capable: true }, roic: { value: 0.3, historical_capable: true }, cf_to_net_profit: { value: 1.1, historical_capable: true } },
    growth: { revenue_yoy: { value: 0.15, historical_capable: true }, net_profit_yoy: { value: 0.18, historical_capable: true }, deducted_profit_yoy: { value: 0.16, historical_capable: true }, revenue_cagr3: { value: 0.14, historical_capable: true }, revenue_cagr5: { value: 0.13, historical_capable: true }, net_profit_cagr5: { value: 0.16, historical_capable: true } },
    safety: { debt_ratio: { value: 0.22, historical_capable: true }, current_ratio: { value: 3.5, historical_capable: true }, quick_ratio: { value: 2.8, historical_capable: true }, interest_bearing_debt: { value: 0, historical_capable: true }, interest_coverage: { value: 150, historical_capable: true }, goodwill_ratio: { value: 0, historical_capable: true } },
    shareholder_return: { payout_ratio: { value: 0.55, historical_capable: true }, dps: { value: 25.0, historical_capable: true }, consecutive_div_years: { value: 20, historical_capable: true } },
  },
}

const KLINE_SETTINGS_KEY = 'vd.stock-detail.kline-settings'

const BUSINESS_OVERVIEW = {
  stock_code: '600519',
  profile: {
    status: 'ok',
    code: '600519',
    name: '贵州茅台',
    org_name: '贵州茅台酒股份有限公司',
    profile: '从事茅台酒及系列酒的生产与销售。',
    scope: '茅台酒及系列酒生产与销售',
    employee_num: 30000,
    csrc_industry: '酒、饮料和精制茶制造业',
    trade_market: '沪市主板',
    provenance: {
      source: 'eastmoney_f10',
      fetch_time: '2026-08-09 10:00:00',
      raw_hash: 'abc',
      confidence: 'approximate',
      batch_id: 'b1',
    },
  },
  breakdown: {
    status: 'ok',
    latest_report_date: '2025-12-31',
    composition: {
      '1': [
        { report_date: '2025-12-31', type: 1, type_label: '产品', item_name: '茅台酒', amount: 1.5e11, ratio: 85, rank: 1 },
        { report_date: '2025-12-31', type: 1, type_label: '产品', item_name: '系列酒', amount: 2e10, ratio: 12, rank: 2 },
      ],
      '2': [
        { report_date: '2025-12-31', type: 2, type_label: '行业', item_name: '白酒制造', amount: 1.7e11, ratio: 100, rank: 1 },
      ],
      '3': [
        { report_date: '2025-12-31', type: 3, type_label: '地区', item_name: '境内', amount: 1.5e11, ratio: 90, rank: 1 },
        { report_date: '2025-12-31', type: 3, type_label: '地区', item_name: '境外', amount: 2e10, ratio: 10, rank: 2 },
      ],
    },
    history: [],
    provenance: {
      source: 'eastmoney_f10',
      fetch_time: '2026-08-09 10:00:00',
      raw_hash: 'abc',
      confidence: 'approximate',
      batch_id: 'b1',
    },
  },
  provenance: {
    profile: {
      source: 'eastmoney_f10',
      fetch_time: '2026-08-09 10:00:00',
      raw_hash: 'abc',
      confidence: 'approximate',
      batch_id: 'b1',
    },
    breakdown: {
      source: 'eastmoney_f10',
      fetch_time: '2026-08-09 10:00:00',
      raw_hash: 'abc',
      confidence: 'approximate',
      batch_id: 'b1',
    },
  },
}

const BUSINESS_OVERVIEW_MISSING = {
  stock_code: '600519',
  profile: { status: 'missing' },
  breakdown: {
    status: 'missing',
    latest_report_date: null,
    composition: {},
    history: [],
    provenance: null,
  },
  provenance: { profile: null, breakdown: null },
}

async function mountDetailWithCode(code: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/stock/:code', component: { template: '<div/>' } }],
  })
  const wrapper = mount(NMessageProvider, {
    slots: { default: () => h(StockDetailPage) },
    global: { plugins: [router] },
  })
  await router.push(`/stock/${code}`)
  await router.isReady()
  return { wrapper, router }
}

const TREASURY_COMPARISON = {
  stock_code: '600519',
  tenor: 10,
  tenors_available: [0.25, 0.5, 1, 2, 3, 5, 7, 10, 30],
  max_staleness_days: 5,
  series: [
    { price_date: '2026-08-07', ttm_div_yield: 3.1, curve_yield: 1.71, spread: 1.39, curve_date: '2026-08-07', staleness_days: 0, reason: null },
    { price_date: '2026-08-06', ttm_div_yield: 3.1, curve_yield: 1.7, spread: 1.4, curve_date: '2026-08-06', staleness_days: 0, reason: null },
    { price_date: '2026-08-03', ttm_div_yield: 3.0, curve_yield: null, spread: null, curve_date: null, staleness_days: null, reason: 'curve_missing' },
  ],
  missing: false,
  provenance: { source: 'czb_mof', fetch_time: '2026-08-10 12:00:00', raw_hash: 'h', batch_id: 'b1', confidence: 'strict' },
}

function setupAxiosMock(
  payload: Record<string, unknown> | null = null,
  businessOverviewPayload: Record<string, unknown> | null = null,
  treasuryPayload: Record<string, unknown> | null = null,
) {
  const klineRequests: Array<Record<string, unknown>> = []
  const get = axios.get as Mock
  get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
    if (url.includes('/info')) {
      return Promise.resolve({ data: { stock_code: '600519', name: '贵州茅台', pinyin: 'gzmt', exchange: 'SSE', listing_date: '2001-08-27', is_st: false, is_suspended: false, latest_close: null, latest_price_date: '2026-07-31' } })
    }
    if (url.includes('/indicators')) {
      return Promise.resolve({ data: payload ?? INDICATORS_NORMAL })
    }
    if (url.includes('/kline')) {
      klineRequests.push(config?.params ?? {})
      return Promise.resolve({ data: { candles: [], adjust: 'raw', period: 'day', count: 0 } })
    }
    if (url.includes('/financial-trend')) {
      return Promise.resolve({ data: { trend: [], period: 'annual', count: 0 } })
    }
    if (url.includes('/business-overview')) {
      return Promise.resolve({ data: businessOverviewPayload ?? BUSINESS_OVERVIEW })
    }
    if (url.includes('/treasury-comparison')) {
      return Promise.resolve({ data: treasuryPayload ?? TREASURY_COMPARISON })
    }
    if (url.includes('/source-audit')) {
      return Promise.resolve({ data: { field_audit: [], batch_audit: [] } })
    }
    return Promise.resolve({ data: {} })
  })
  return klineRequests
}

describe('StockDetailPage 不可信指标统一警示（reports/27 P1-8）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    setupAxiosMock(INDICATORS_UNTRUSTED)
  })

  it('LINEAGE_INVALID 时显示全局告警且指标不渲染普通数值', async () => {
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const text = wrapper.text()
    // 全局 fail-closed 告警
    expect(text).toContain('当前数据库状态不可信')
    // 指标卡片的"数据不可信"标记（非普通数值）
    expect(text).toContain('数据不可信')
  })
})

describe('StockDetailPage 研究工作台（P1 重构）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('渲染粘性目录的六个章节', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const links = wrapper.findAll('.toc-link')
    expect(links.map((n) => n.text())).toEqual([
      '概览',
      '估值与市场',
      '经营与成长',
      '财务安全',
      '股东回报',
      '来源材料',
    ])
  })

  it('渲染概览与四个摘要章节及来源材料', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    expect(wrapper.find('#overview').exists()).toBe(true)
    expect(wrapper.find('#valuation').text()).toContain('估值与市场')
    expect(wrapper.find('#operations').text()).toContain('经营与成长')
    expect(wrapper.find('#safety').text()).toContain('财务安全')
    expect(wrapper.find('#return').text()).toContain('股东回报')
    expect(wrapper.find('#sources').text()).toContain('来源材料')
    // 首屏身份 / 行情 / 数据状态
    expect(wrapper.find('#overview').text()).toContain('贵州茅台')
    expect(wrapper.find('#overview').text()).toContain('最新收盘价')
  })

  it('点击目录项平滑滚动并高亮当前章节', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const scrollIntoView = vi.fn()
    Element.prototype.scrollIntoView = scrollIntoView as unknown as typeof Element.prototype.scrollIntoView

    await wrapper.find('.toc-link[href="#safety"]').trigger('click')
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' })
    expect(wrapper.find('.toc-link.active').text()).toBe('财务安全')
  })

  it('localStorage 全局记忆 period/adjust/range 并作用于请求', async () => {
    localStorage.setItem(
      KLINE_SETTINGS_KEY,
      JSON.stringify({ period: 'week', adjust: 'qfq', range: 500 }),
    )
    const klineRequests = setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    expect(klineRequests[0]).toMatchObject({ period: 'week', adjust: 'qfq', days: 500 })
  })

  it('非法 period/adjust/range 安全回退默认', async () => {
    localStorage.setItem(
      KLINE_SETTINGS_KEY,
      JSON.stringify({ period: 'invalid', adjust: 'bad', range: 123 }),
    )
    const klineRequests = setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    expect(klineRequests[0]).toMatchObject({ period: 'day', adjust: 'raw', days: 250 })
  })

  it('切换周期后写入 localStorage 并重新拉取 K 线', async () => {
    const klineRequests = setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const weekButton = wrapper.findAll('.n-radio-button').find((w) => w.text().includes('周K'))
    expect(weekButton).toBeTruthy()
    await weekButton!.find('input').trigger('change')
    await flushPromises()

    const saved = JSON.parse(localStorage.getItem(KLINE_SETTINGS_KEY)!)
    expect(saved).toMatchObject({ period: 'week' })
    expect(klineRequests[klineRequests.length - 1]).toMatchObject({ period: 'week' })
  })
})

describe('StockDetailPage 业务概览（reports/67/68）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('首屏 identity 后显示 profile 摘要、报告期与每类 top 标签及溯源', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const overviewText = wrapper.find('#overview').text()
    // identity 在前，业务概览在后
    expect(overviewText.indexOf('贵州茅台')).toBeGreaterThanOrEqual(0)
    expect(overviewText.indexOf('贵州茅台')).toBeLessThan(overviewText.indexOf('业务概览'))
    // 一句话主营 + 报告期
    expect(overviewText).toContain('从事茅台酒及系列酒的生产与销售。')
    expect(overviewText).toContain('报告期 2025-12-31')
    // 每类 top 标签
    expect(overviewText).toContain('茅台酒')
    expect(overviewText).toContain('85.00%')
    expect(overviewText).toContain('白酒制造')
    // 溯源：approximate / source / fetch_time
    expect(overviewText).toContain('approximate')
    expect(overviewText).toContain('eastmoney_f10')
    expect(overviewText).toContain('2026-08-09 10:00:00')
  })

  it('经营章节显示最近报告期主营构成明细表', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const operationsText = wrapper.find('#operations').text()
    expect(operationsText).toContain('主营构成')
    expect(operationsText).toContain('报告期 2025-12-31')
    // 表格列与行
    expect(operationsText).toContain('排名')
    expect(operationsText).toContain('占比')
    expect(operationsText).toContain('系列酒')
    expect(operationsText).toContain('境内')
  })

  it('missing 是局部空态，不触发页面级 stockUnavailable', async () => {
    setupAxiosMock(null, BUSINESS_OVERVIEW_MISSING)
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    expect(wrapper.text()).not.toContain('股票不存在或暂无数据')
    expect(wrapper.find('#overview').text()).toContain('暂无业务概览数据')
    expect(wrapper.find('#operations').text()).toContain('暂无主营构成数据')
  })

  it('股东回报章节展示国债比较（利差默认）', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const returnText = wrapper.find('#return').text()
    expect(returnText).toContain('国债比较')
    expect(returnText).toContain('利差')
    expect(returnText).toContain('TTM已实施股息率')
    expect(returnText).toContain('czb_mof')
    // 默认请求 10 年期限
    const requests = (axios.get as Mock).mock.calls
      .filter(([url]: [string]) => String(url).includes('/treasury-comparison'))
      .map(([, config]: [string, { params?: Record<string, unknown> } | undefined]) => config?.params)
    expect(requests[0]).toMatchObject({ tenor: 10, limit: 250 })
  })

  it('国债曲线缺失时显示局部空态', async () => {
    const missing = {
      ...TREASURY_COMPARISON,
      series: [],
      missing: true,
      provenance: null,
    }
    setupAxiosMock(null, null, missing)
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const returnText = wrapper.find('#return').text()
    expect(returnText).toContain('暂无国债比较数据')
  })
})
