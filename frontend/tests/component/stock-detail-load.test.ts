/**
 * StockDetailPage 加载/降级/周期/TOC 行为快照（2026-11-23 重构前先写，**拆之前必须先全绿**）。
 *
 * 为什么先写：本页 805 行里三件事与生命周期交织——5 路并发拉取（含黑名单预判、
 * 代际号防串台、逐路降级）、K 线周期/复权（含 abort 在途请求）、TOC 滚动跟随。
 * 上一轮判断「不该拆」的理由是「16 个既有测试说明有真实回归风险，拆了证明不了等价」。
 * 本文件把这三块的**外部可观察行为**先钉死，再拆 `useStockDetailLoad` / `useTocFollow`。
 *
 * 与既有 `stock-detail-flow.test.ts` 的分工（既有用例一行不改）：
 * - 既有：渲染内容（目录 7 项、业务概览、统计卡、黑名单三态）。
 * - 本文件：**请求/降级/参数/滚动锚点**——也就是被搬进 composable 的那些分支。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
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
  return { default: mock, isAxiosError: mock.isAxiosError, isCancel: mock.isCancel }
})

import axios from 'axios'
import type { Mock } from 'vitest'

const KLINE_SETTINGS_KEY = 'vd.stock-detail.kline-settings'

const STOCK_INFO = {
  stock_code: '600519',
  name: '贵州茅台',
  pinyin: 'gzmt',
  exchange: 'SSE',
  listing_date: '2001-08-27',
  is_st: false,
  is_suspended: false,
  latest_close: 1680.5,
  latest_price_date: '2026-07-31',
}

const INDICATORS = {
  report_date: '2026-06-30',
  latest_close: 1680.5,
  latest_price_date: '2026-07-31',
  freshness: { stale_warning: false, price_age_days: 1, financial_age_days: 30, snapshot_age_days: 1 },
  trust: { warning_codes: [], untrusted_all: false, untrusted_fields: [] },
  indicators: { valuation: {}, profitability: {}, growth: {}, safety: {}, shareholder_return: {} },
}

/** 某一路失败时的通用拒绝值。 */
function axiosError(status: number | null, detail?: string) {
  return Object.assign(new Error(`HTTP ${status}`), {
    isAxiosError: true,
    response: status == null ? undefined : { status, data: detail ? { detail } : {} },
    code: status == null ? 'ERR_NETWORK' : undefined,
  })
}

interface Failures {
  info?: unknown
  indicators?: unknown
  kline?: unknown
  business?: unknown
  dataStatus?: unknown
  blacklist?: unknown
}

const KLINE_RESPONSE = { candles: [], adjust: 'raw', period: 'day', count: 0 }

function setupAxiosMock(failures: Failures = {}) {
  const klineCalls: Array<{ params?: Record<string, unknown>; signal?: unknown }> = []
  const urls: string[] = []
  const get = axios.get as Mock
  get.mockImplementation((url: string, config?: { params?: Record<string, unknown>; signal?: unknown }) => {
    urls.push(url)
    if (url.includes('/api/blacklist/check/')) {
      if (failures.blacklist) return Promise.reject(failures.blacklist)
      return Promise.resolve({ data: { stock_code: '600519', blacklisted: false } })
    }
    if (url.includes('/api/stock/') && url.includes('/info')) {
      if (failures.info) return Promise.reject(failures.info)
      return Promise.resolve({ data: STOCK_INFO })
    }
    if (url.includes('/indicators')) {
      if (failures.indicators) return Promise.reject(failures.indicators)
      return Promise.resolve({ data: INDICATORS })
    }
    if (url.includes('/kline')) {
      klineCalls.push({ params: config?.params, signal: config?.signal })
      if (failures.kline) return Promise.reject(failures.kline)
      return Promise.resolve({ data: KLINE_RESPONSE })
    }
    if (url.includes('/business-overview')) {
      if (failures.business) return Promise.reject(failures.business)
      return Promise.resolve({ data: { stock_code: '600519', profile: { status: 'missing' }, breakdown: { status: 'missing', composition: {}, history: [], latest_report_date: null, provenance: null }, provenance: { profile: null, breakdown: null } } })
    }
    if (url.includes('/api/data-status/summary')) {
      if (failures.dataStatus) return Promise.reject(failures.dataStatus)
      return Promise.resolve({ data: { data_quality: { warning_codes: [] } } })
    }
    return Promise.resolve({ data: {} })
  })
  return { klineCalls, urls }
}

async function mountPage(code: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/stock/:code', component: { template: '<div/>' } }],
  })
  const wrapper = mount(NMessageProvider, {
    slots: {
      default: () => h(NDialogProvider, null, { default: () => h(StockDetailPage) }),
    },
    attachTo: document.body,
    global: { plugins: [router] },
  })
  await router.push(`/stock/${code}`)
  await router.isReady()
  await flushPromises()
  await flushPromises()
  return { wrapper, router }
}

function messagesText(): string {
  return Array.from(document.body.querySelectorAll('.n-message'))
    .map((el) => el.textContent ?? '')
    .join('\n')
}

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

// ─── ① 路由代码校验 ──────────────────────────────────────────────────────

describe('StockDetailPage 路由代码校验', () => {
  it('代码为空白时直接渲染「股票代码缺失」，且一个请求都不发', async () => {
    const { urls } = setupAxiosMock()
    const { wrapper } = await mountPage('   ')

    expect(wrapper.text()).toContain('股票代码缺失')
    expect(urls).toEqual([])
  })

  it('代码不存在（结构化 404，非黑名单）时渲染回退结果页并给两条出路', async () => {
    setupAxiosMock({ info: axiosError(404, 'stock not found') })
    const { wrapper } = await mountPage('000000')

    const text = wrapper.text()
    expect(text).toContain('股票不存在或暂无数据')
    expect(text).toContain('回到筛选')
    expect(text).toContain('回到自选列表')
    // 终态页不该再渲染研究正文
    expect(wrapper.find('#overview').exists()).toBe(false)
  })

  it('代码经 trim 后进入请求路径（前导/尾随空格被裁掉）', async () => {
    const { urls } = setupAxiosMock()
    await mountPage('%20600519%20')

    const stockUrls = urls.filter((u) => u.startsWith('/api/stock/'))
    expect(stockUrls.length).toBeGreaterThan(0)
    for (const url of stockUrls) expect(url).toContain('/api/stock/600519/')
  })
})

// ─── ② 5 路并发的降级 ────────────────────────────────────────────────────

describe('StockDetailPage 5 路并发拉取与逐路降级', () => {
  it('一次进入并发打满 5 个页面自有端点（黑名单判定 + 4 路详情）', async () => {
    const { urls } = setupAxiosMock()
    await mountPage('600519')

    expect(urls).toContain('/api/blacklist/check/600519')
    expect(urls).toContain('/api/stock/600519/info')
    expect(urls).toContain('/api/stock/600519/indicators')
    expect(urls).toContain('/api/stock/600519/kline')
    expect(urls).toContain('/api/stock/600519/business-overview')
    // 每个只打一次（没有意外的重复拉取）
    expect(urls.filter((u) => u === '/api/stock/600519/info')).toHaveLength(1)
  })

  it('指标失败：告警提示 + 概览其余部分照常渲染，不升成整页不可用', async () => {
    setupAxiosMock({ indicators: axiosError(500) })
    const { wrapper } = await mountPage('600519')

    expect(messagesText()).toContain('服务器内部错误')
    expect(wrapper.text()).not.toContain('股票不存在或暂无数据')
    // 身份区（来自 /info）与目录仍在
    expect(wrapper.find('#overview').text()).toContain('贵州茅台')
    // 2026-11-22：公告时间线卡片已删除 → 目录 6 项
    expect(wrapper.findAll('.toc-link')).toHaveLength(6)
  })

  it('K 线失败：仅清空 K 线数据并告警，其余 4 路不受影响', async () => {
    setupAxiosMock({ kline: axiosError(500) })
    const { wrapper } = await mountPage('600519')

    expect(messagesText()).toContain('服务器内部错误')
    expect(wrapper.find('#overview').text()).toContain('贵州茅台')
    expect(wrapper.find('#operations').exists()).toBe(true)
  })

  it('业务概览网络错误：局部空态 + 告警，页面级不可用不触发', async () => {
    setupAxiosMock({ business: axiosError(null) })
    const { wrapper } = await mountPage('600519')

    expect(messagesText()).toContain('无法连接服务器')
    expect(wrapper.text()).not.toContain('股票不存在或暂无数据')
    expect(wrapper.find('#overview').text()).toContain('暂无业务概览数据')
  })

  it('业务概览 404：与既有实现一致地升级为页面级「股票不存在」', async () => {
    setupAxiosMock({ business: axiosError(404, 'business overview not found') })
    const { wrapper } = await mountPage('600519')

    expect(wrapper.text()).toContain('股票不存在或暂无数据')
  })

  it('数据状态汇总失败：回退为 LINEAGE_INVALID 并顶出全局不可信告警', async () => {
    setupAxiosMock({
      indicators: { ...INDICATORS, trust: undefined },
      dataStatus: axiosError(500),
    })
    const { wrapper } = await mountPage('600519')

    expect(wrapper.text()).toContain('当前数据库状态不可信')
  })

  it('黑名单判定失败不影响正常加载（退回 4 路详情）', async () => {
    const { urls } = setupAxiosMock({ blacklist: axiosError(null) })
    const { wrapper } = await mountPage('600519')

    expect(urls).toContain('/api/stock/600519/info')
    expect(wrapper.find('#overview').text()).toContain('贵州茅台')
  })
})

// ─── ③ K 线周期切换的请求参数 ────────────────────────────────────────────

describe('StockDetailPage K 线周期/复权的请求参数', () => {
  async function clickRadio(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper'], label: string) {
    const button = wrapper.findAll('.n-radio-button').find((w) => w.text().includes(label))
    expect(button).toBeTruthy()
    await button!.find('input').trigger('change')
    await flushPromises()
  }

  it('默认按 day + raw 请求', async () => {
    const { klineCalls } = setupAxiosMock()
    await mountPage('600519')

    expect(klineCalls).toHaveLength(1)
    expect(klineCalls[0].params).toEqual({ adjust: 'raw', period: 'day' })
  })

  it('切周期 → 只改 period；切复权 → 只改 adjust，并同步 localStorage', async () => {
    const { klineCalls } = setupAxiosMock()
    const { wrapper } = await mountPage('600519')

    await clickRadio(wrapper, '周K')
    expect(klineCalls[klineCalls.length - 1].params).toEqual({ adjust: 'raw', period: 'week' })
    expect(JSON.parse(localStorage.getItem(KLINE_SETTINGS_KEY)!)).toMatchObject({ period: 'week', adjust: 'raw' })

    await clickRadio(wrapper, '前复权')
    expect(klineCalls[klineCalls.length - 1].params).toEqual({ adjust: 'qfq', period: 'week' })
    expect(JSON.parse(localStorage.getItem(KLINE_SETTINGS_KEY)!)).toMatchObject({ period: 'week', adjust: 'qfq' })

    await clickRadio(wrapper, '月K')
    expect(klineCalls[klineCalls.length - 1].params).toEqual({ adjust: 'qfq', period: 'month' })
  })

  it('切周期会 abort 上一路在途 K 线请求（换新 AbortSignal）', async () => {
    const { klineCalls } = setupAxiosMock()
    const { wrapper } = await mountPage('600519')

    const first = klineCalls[0].signal as AbortSignal
    expect(first.aborted).toBe(false)

    await clickRadio(wrapper, '周K')

    const second = klineCalls[klineCalls.length - 1].signal as AbortSignal
    expect(second).not.toBe(first)
    expect(first.aborted).toBe(true)
    expect(second.aborted).toBe(false)
  })

  it('卸载时 abort 在途 K 线请求（离开页面不再写已卸载组件）', async () => {
    const { klineCalls } = setupAxiosMock()
    const { wrapper } = await mountPage('600519')

    const signal = klineCalls[0].signal as AbortSignal
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
  })
})

// ─── ④ TOC 滚动跟随的锚点计算 ────────────────────────────────────────────

describe('StockDetailPage TOC 滚动跟随（锚点计算）', () => {
  /** 各章节在**文档**里的绝对偏移（px）。视口内 top = 文档偏移 − scrollY。 */
  const SECTION_DOC_TOPS: Record<string, number> = {
    overview: 0,
    valuation: 900,
    'point-in-time': 1400,
    operations: 1700,
    safety: 2300,
    return: 2900,
  }

  let scrollY = 0

  function stubRects() {
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      const docTop = SECTION_DOC_TOPS[(this as HTMLElement).id] ?? 9999
      const top = docTop - scrollY
      return {
        top, bottom: top + 100, left: 0, right: 0, width: 0, height: 100, x: 0, y: top,
        toJSON: () => ({}),
      } as DOMRect
    })
  }

  function setScrollY(value: number) {
    scrollY = value
    Object.defineProperty(window, 'scrollY', { value, configurable: true, writable: true })
  }

  async function scrollTo(value: number) {
    setScrollY(value)
    window.dispatchEvent(new Event('scroll'))
    await flushPromises()
  }

  function activeTocText(wrapper: Awaited<ReturnType<typeof mountPage>>['wrapper']) {
    return wrapper.find('.toc-link.active').text()
  }

  it('按「章节顶边 ≤ 视口 96px」判定当前章节（探针常量不变）', async () => {
    stubRects()
    setScrollY(0)
    setupAxiosMock()
    // 探针 96px：scrollY=1000 时，探针线在文档 1096px——
    // valuation(900) 已翻过，point-in-time(1400) 未到。
    const { wrapper } = await mountPage('600519')

    await scrollTo(0)
    expect(activeTocText(wrapper)).toBe('概览')

    await scrollTo(1000)
    expect(activeTocText(wrapper)).toBe('估值与市场')

    await scrollTo(1150)
    // point-in-time 文档顶 1400 仍高于探针线 1246
    expect(activeTocText(wrapper)).toBe('估值与市场')

    // 精确边界：scrollY = 1400 − 96 → 探针线正好压在 point-in-time 顶边
    await scrollTo(1304)
    expect(activeTocText(wrapper)).toBe('当时可见')
  })

  it('全部章节都翻过时高亮最后一项（不会越界）', async () => {
    stubRects()
    setScrollY(0)
    setupAxiosMock()
    const { wrapper } = await mountPage('600519')

    await scrollTo(4000)
    expect(activeTocText(wrapper)).toBe('股东回报')
  })

  it('滚动到顶（scrollTop ≤ 0）无条件回到首项', async () => {
    stubRects()
    setScrollY(0)
    setupAxiosMock()
    const { wrapper } = await mountPage('600519')

    await scrollTo(4000)
    expect(activeTocText(wrapper)).toBe('股东回报')

    await scrollTo(0)
    expect(activeTocText(wrapper)).toBe('概览')
  })

  it('点击目录项立即高亮该章节（不等滚动事件）', async () => {
    stubRects()
    setScrollY(0)
    setupAxiosMock()
    const { wrapper } = await mountPage('600519')

    await wrapper.find('.toc-link[href="#safety"]').trigger('click')
    await flushPromises()
    expect(activeTocText(wrapper)).toBe('财务安全')
  })
})
