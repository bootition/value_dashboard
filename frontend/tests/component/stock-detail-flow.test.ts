import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
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
  // 页面用 `import axios, { isAxiosError } from 'axios'`（命名导入）。
  // 只模拟 default 会让 named export 为 undefined，404 分支一进入就抛
  // TypeError——以前没人走过 404 分支所以没暴露。
  return { default: mock, isAxiosError: mock.isAxiosError, isCancel: mock.isCancel }
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
    shareholder_return: { payout_ratio: { value: 0.55, historical_capable: true }, dps: { value: 25.0, historical_capable: true }, consecutive_div_years: { value: 20, historical_capable: true }, cumulative_dividend_amount: { value: 50_000_000_000, historical_capable: false }, cumulative_financing_amount: { value: 20_000_000_000, historical_capable: false }, dividend_financing_ratio_pct: { value: 250.0, historical_capable: false } },
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
  // 加入黑名单需要二次确认弹窗（useDialog），因此必须挂 NDialogProvider；
  // 真实应用里由 App.vue 提供（n-message-provider > n-dialog-provider）。
  const wrapper = mount(NMessageProvider, {
    slots: {
      default: () =>
        h(NDialogProvider, null, { default: () => h(StockDetailPage) }),
    },
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

const RESEARCH_STATISTICS = {
  stock_code: '600519',
  metric: 'pe_ttm',
  window_years: 10,
  series: [
    { price_date: '2023-01-03', close: 20, pe_ttm: 15.2, pb_mrq: 3.1, ttm_dividend_yield: 1.2, spread_10y: -0.3 },
    { price_date: '2024-01-02', close: 22, pe_ttm: 16.8, pb_mrq: 3.4, ttm_dividend_yield: 1.4, spread_10y: -0.1 },
  ],
  statistics: {
    '1y': { samples: null, reason: 'insufficient_samples' },
    '10y': { samples: 150, p10: 10.1, p20: 12.2, p50: 15.9, p80: 18.4, max: 22.5, mean: 15.6, sigma: 2.3, zscore: 0.52, current: 16.8, coverage_pct: 96.5, reason: null },
  },
  coverage_threshold_pct: 90,
  disclaimer: '最新重述回看口径',
}

// 2026-11-14（reports/119 §5 D 类）：详情页新增「当时可见」与「公告时间线」两张卡，
// 既有流程测试需给出确定响应，避免落到兜底 `{}` 后只渲染空态。
const AS_REPORTED = {
  stock_code: '600519',
  as_of: '2024-06-30',
  frequency: 'any',
  report_date: '2024-03-31',
  announce_date: '2024-04-26',
  report_type: 'quarterly',
  source: 'cninfo',
  days_since_announce: 65,
  fields: { revenue: 46_000_000_000, parent_net_profit: 24_000_000_000, total_assets: 270_000_000_000, total_equity_parent: 200_000_000_000, cf_from_operating: 12_000_000_000 },
  missing_fields: [],
  value_tables: ['income_statement', 'balance_sheet', 'cash_flow'],
  value_basis: 'latest_restated',
  value_source: 'financial_report_dates.announce_date 定位报告期 + 三表取数',
  disclaimer: '数值取自库内该报告期唯一一份记录（今天的最新重述值），不是当年披露时的原始数字。',
  limitations: ['无公布日记录的报告期不参与判定（不假设未登记即当天可见）'],
}

const ANNOUNCEMENTS = {
  stock_code: '600519',
  days: 365,
  cutoff_date: '2025-11-15',
  timezone: 'Asia/Shanghai',
  limit: 20,
  offset: 0,
  count: 1,
  total: 1,
  has_more: false,
  by_category: { financial: 1, dividend: 0, other: 0 },
  items: [
    {
      announcement_id: 'a1',
      stock_code: '600519',
      title: '2025年第三季度报告',
      category: 'financial',
      announcement_time: '2025-10-30T08:00:00+00:00',
      announcement_date_cn: '2025-10-30',
      announcement_datetime_cn: '2025-10-30T16:00:00+08:00',
    },
  ],
  category_source: 'app.core.update.classify_announcement',
  disclaimer: '只包含已登记进 announcement_registry 的公告，不是全市场全量公告库。',
}

interface AxiosMockOptions {
  /** `/api/blacklist/check/{code}` 的应答：可见 / 被拉黑 / 端点失效 */
  blacklistCheck?: 'visible' | 'blacklisted' | 'fail'
  /** `/info` 返回结构化 404（`detail.code = stock_blacklisted`），用于覆盖兜底分支 */
  infoStructured404?: boolean
}

function setupAxiosMock(
  payload: Record<string, unknown> | null = null,
  businessOverviewPayload: Record<string, unknown> | null = null,
  treasuryPayload: Record<string, unknown> | null = null,
  statisticsPayload: Record<string, unknown> | null = null,
  options: AxiosMockOptions = {},
) {
  const klineRequests: Array<Record<string, unknown>> = []
  const get = axios.get as Mock
  get.mockImplementation((url: string, config?: { params?: Record<string, unknown> }) => {
    // 黑名单判定：默认「可见」，让既有用例不受影响
    if (url.includes('/api/blacklist/check/')) {
      if (options.blacklistCheck === 'fail') {
        return Promise.reject(new Error('blacklist check unavailable'))
      }
      return Promise.resolve({
        data: { stock_code: '600519', blacklisted: options.blacklistCheck === 'blacklisted' },
      })
    }
    if (url.includes('/info')) {
      if (options.infoStructured404) {
        return Promise.reject(Object.assign(new Error('Not Found'), {
          isAxiosError: true,
          response: {
            status: 404,
            data: { detail: { code: 'stock_blacklisted', message: '该股票已加入黑名单，内容不再显示' } },
          },
        }))
      }
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
    if (url.includes('/research-statistics')) {
      return Promise.resolve({ data: statisticsPayload ?? RESEARCH_STATISTICS })
    }
    if (url.includes('/research/as-reported')) {
      return Promise.resolve({ data: AS_REPORTED })
    }
    if (url.includes('/research/announcements')) {
      return Promise.resolve({ data: ANNOUNCEMENTS })
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
    // 缺失指标不再渲染成空卡片，因此页面不会出现普通数值或空值标记
    expect(text).not.toContain('数据不可信')
  })
})

describe('StockDetailPage 研究工作台（P1 重构）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('渲染粘性目录的七个章节', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const links = wrapper.findAll('.toc-link')
    // 2026-11-14（reports/119 §5 D 类）：估值与市场之后新增两个锚点
    // 「当时可见」「公告时间线」，目录从 5 项变为 7 项。
    expect(links.map((n) => n.text())).toEqual([
      '概览',
      '估值与市场',
      '当时可见',
      '公告时间线',
      '经营与成长',
      '财务安全',
      '股东回报',
    ])
  })

  it('渲染概览与四个摘要章节', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    expect(wrapper.find('#overview').exists()).toBe(true)
    expect(wrapper.find('#valuation').text()).toContain('估值与市场')
    expect(wrapper.find('#operations').text()).toContain('经营与成长')
    expect(wrapper.find('#safety').text()).toContain('财务安全')
    expect(wrapper.find('#return').text()).toContain('股东回报')
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

  it('localStorage 全局记忆 period/adjust 并作用于请求', async () => {
    localStorage.setItem(
      KLINE_SETTINGS_KEY,
      JSON.stringify({ period: 'week', adjust: 'qfq' }),
    )
    const klineRequests = setupAxiosMock()
    await mountDetailWithCode('600519')
    await flushPromises()

    expect(klineRequests[0]).toMatchObject({ period: 'week', adjust: 'qfq' })
  })

  it('非法 period/adjust 安全回退默认', async () => {
    localStorage.setItem(
      KLINE_SETTINGS_KEY,
      JSON.stringify({ period: 'invalid', adjust: 'bad' }),
    )
    const klineRequests = setupAxiosMock()
    await mountDetailWithCode('600519')
    await flushPromises()

    expect(klineRequests[0]).toMatchObject({ period: 'day', adjust: 'raw' })
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

  it('经营章节在固定数字原位显示单张可切换指标历史图', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const operationsText = wrapper.find('#operations').text()
    expect(operationsText).toContain('经营与成长')
    expect(operationsText).toContain('ROE')
    expect(operationsText).toContain('TTM · 上市以来 · latest_restated')
    expect(operationsText).toContain('暂无该指标历史序列')
  })

  it('missing 是局部空态，不触发页面级 stockUnavailable', async () => {
    setupAxiosMock(null, BUSINESS_OVERVIEW_MISSING)
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    expect(wrapper.text()).not.toContain('股票不存在或暂无数据')
    expect(wrapper.find('#overview').text()).toContain('暂无业务概览数据')
    expect(wrapper.find('#operations').text()).toContain('暂无该指标历史序列')
  })

  it('股东回报区域不再请求独立国债卡片，只保留一张历史统计图', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    expect(wrapper.find('#return').text()).toContain('历史研究统计')
    expect(wrapper.find('#valuation').text()).toContain('国债比较已合并进“股息率-国债10年利差”序列')
    // `Mock` 的 mock.calls 是 any[][]（每次调用是「参数数组」），旧写法把元素注解成
    // 元组 `[string]`，参数类型比 any[] 更窄 → 谓词签名不兼容。按调用元组标注后解构。
    const getCalls = (axios.get as Mock).mock.calls as Array<[string, ...unknown[]]>
    const requests = getCalls.filter(([url]) => String(url).includes('/treasury-comparison'))
    expect(requests).toEqual([])
  })

  it('估值章节展示历史研究统计（分位带与z-score）', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const valuationText = wrapper.find('#valuation').text()
    expect(valuationText).toContain('历史研究统计')
    expect(valuationText).toContain('经验分位带')
    expect(valuationText).toContain('z-score 0.52')
    expect(valuationText).toContain('样本 150')
    expect(valuationText).toContain('最新重述回看')
  })

  it('估值与股东回报各保留一张历史统计图', async () => {
    setupAxiosMock()
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const cards = wrapper.findAll('[aria-label="历史研究统计"]')
    expect(cards).toHaveLength(2)
    expect(wrapper.find('#return').text()).toContain('历史研究统计')
    expect(wrapper.find('#valuation').text()).toContain('股息率-国债10年利差')
  })
})

describe('StockDetailPage 黑名单（2026-11-22，用户需求）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('被拉黑的股票只渲染屏蔽态，且不再请求任何详情端点', async () => {
    setupAxiosMock(null, null, null, null, { blacklistCheck: 'blacklisted' })
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    expect(wrapper.text()).toContain('已加入黑名单')
    expect(wrapper.text()).toContain('移出黑名单')

    const getCalls = (axios.get as Mock).mock.calls as Array<[string, ...unknown[]]>
    const urls = getCalls.map(([url]) => String(url))
    expect(urls.some((url) => url.includes('/info'))).toBe(false)
    expect(urls.some((url) => url.includes('/indicators'))).toBe(false)
    expect(urls.some((url) => url.includes('/kline'))).toBe(false)
    expect(urls.some((url) => url.includes('/business-overview'))).toBe(false)
  })

  it('单股端点返回结构化 404 时同样进入屏蔽态（而不是股票不存在）', async () => {
    setupAxiosMock(null, null, null, null, {
      blacklistCheck: 'fail',
      infoStructured404: true,
    })
    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    expect(wrapper.text()).toContain('已加入黑名单')
    expect(wrapper.text()).not.toContain('股票不存在或暂无数据')
  })

  it('点「加入黑名单」先弹确认框，未确认前不发 POST', async () => {
    setupAxiosMock()
    const post = axios.post as Mock
    post.mockResolvedValue({ data: { status: 'ok', stock_code: '600519' } })

    const { wrapper } = await mountDetailWithCode('600519')
    await flushPromises()

    const trigger = wrapper
      .findAll('button')
      .find((button) => button.text().includes('加入黑名单'))
    expect(trigger).toBeTruthy()

    await trigger!.trigger('click')
    await flushPromises()

    // 确认框已弹出（建在 document.body 上），但还没发请求
    expect(document.body.textContent).toContain('确认加入')
    expect(post).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('已加入黑名单')
  })
})
