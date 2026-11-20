/**
 * 「切页保留，直到我按刷新」——第二轮事故回归（2026-11-20）。
 *
 * 用户原话：
 *   「我从筛选页点到数据详情页，数据详情页刷新了才显示…然后我再从数据页回到筛选页，
 *    筛选页之前运行的结果全都没了。为什么不能够暂时保留结果，直到我点击浏览器刷新
 *    才去刷新呢？」
 *
 * 上一轮（`screening-page-state.test.ts`）钉的是「SPA 切页不丢结果」，那条是绿的。
 * 但用户仍然丢结果，真实路径是**失败即清空的主线语义越界**：
 *
 *   `ScreeningPage.vue::runScreening()` 的 `catch` 无条件 `clearRunArtifacts()`
 *   → `clearRunSnapshot()` 把模块级会话快照也清掉。
 *
 * 而后端在**数据自动更新期间**会用 409 明确拒绝 `/run`（`reports/151`：
 * 「现在不能跑」），服务端日志实证用户实测窗口里连发 5 次
 * `筛选运行被写锁挡住且无上次成功结果`。于是用户的实际经历是：
 *
 *   切回筛选页（结果还在）→ 点「运行筛选」→ 409 → **整页结果被清空** →
 *   再去别的页、再回来 → 当然什么都没有。
 *
 * 本文件把「保留 / 清空」的边界双向钉死：
 *   1. 409（更新中/写锁）、网络中断、切页期间落地失败 → **保留**上一次成功的结果与快照；
 *   2. 后端执行了但产出不可信（500 / 400） → **仍然清空**（主线 P0 语义不动）；
 *   3. 只有「用户主动刷新」才重置（`resetScreeningSession` = 文档重载语义）。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { h } from 'vue'
import ScreeningPage from '../../src/views/ScreeningPage.vue'
import ScreeningResultsPanel from '../../src/components/ScreeningResultsPanel.vue'
import type { ScreeningResult } from '../../src/types/screening.ts'
import {
  resetScreeningSession,
  saveRunSnapshot,
  screeningSession,
  SERVER_RUN_TTL_MS,
} from '../../src/stores/screening-session.ts'

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

const RULES = [{
  id: 7,
  name: '低估值·行业龙头',
  version: 3,
  rule_json: {
    conditions: { logic: 'AND', rules: [{ field: 'pe_ttm', op: '<', value: 15 }] },
    columns: ['stock_code', 'pe_ttm'],
  },
  locked_indicators: {},
  status: 'saved',
  created_at: '2026-11-01',
}]

const RESULT_ROWS: ScreeningResult[] = [
  {
    stock_code: '600519', name: '贵州茅台', exchange: 'SSE', csrc_l1: '制造业',
    latest_close: 1680.5, pe_ttm: 10, pb_mrq: 8.2, roe: 0.3, gross_margin: 0.91,
    net_margin: 0.52, debt_ratio: 0.2, revenue_yoy: 0.15, dividend_yield: 0.023,
  },
  {
    stock_code: '000858', name: '五粮液', exchange: 'SZSE', csrc_l1: '制造业',
    latest_close: 140.2, pe_ttm: 8, pb_mrq: 4.1, roe: 0.25, gross_margin: 0.75,
    net_margin: 0.36, debt_ratio: 0.3, revenue_yoy: 0.1, dividend_yield: 0.031,
  },
  {
    stock_code: '601318', name: '中国平安', exchange: 'SSE', csrc_l1: '金融业',
    latest_close: 55.1, pe_ttm: 9, pb_mrq: 1.1, roe: 0.12, gross_margin: 0.4,
    net_margin: 0.1, debt_ratio: 0.9, revenue_yoy: 0.05, dividend_yield: 0.05,
  },
]

const RUN_OK = {
  results: RESULT_ROWS,
  total: 3,
  execution_time_ms: 42,
  base_pool_size: 4800,
  data_date: '2026-09-30',
  run_id: 'run-abc',
}

/** 后端写锁拒绝（`reports/151` 第三步）：本次运行**根本没有执行**。 */
function writeLockError() {
  return {
    isAxiosError: true,
    response: {
      status: 409,
      data: {
        detail: {
          reason_code: 'DATA_UPDATE_IN_PROGRESS',
          message: '数据正在自动更新，暂时不能运行筛选；更新完成后重试即可。',
        },
      },
    },
  }
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', redirect: '/screening' },
      { path: '/screening', name: 'screening', component: { template: '<div />' } },
      { path: '/stock/:code', name: 'stock-detail', component: { template: '<div />' } },
    ],
  })
}

function mountPage() {
  const router = makeRouter()
  const wrapper = mount(NDialogProvider, {
    slots: {
      default: () => h(NMessageProvider, null, { default: () => h(ScreeningPage) }),
    },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

function mockApis() {
  const get = axios.get as Mock
  get.mockImplementation((url: string) => {
    if (url === '/api/screening/draft') return Promise.resolve({ data: { draft: null, revision: 3 } })
    if (url === '/api/screening/rules') return Promise.resolve({ data: { rules: RULES } })
    if (url === '/api/screening/indicators') {
      return Promise.resolve({ data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 } })
    }
    if (url === '/api/data-status/summary') {
      return Promise.resolve({
        data: {
          data_quality: {
            warning_codes: ['LINEAGE_INVALID'],
            minimum_data_readiness: { ready: true },
          },
        },
      })
    }
    if (url === '/api/data-status/auto-update') {
      return Promise.resolve({ data: { state: 'enabled', current_stage: 'finished' } })
    }
    return Promise.resolve({ data: {} })
  })
  const post = axios.post as Mock
  post.mockImplementation((url: string) => {
    if (url === '/api/screening/run') return Promise.resolve({ data: { ...RUN_OK } })
    return Promise.resolve({ data: {} })
  })
  return { get, post }
}

function runCalls(post: Mock): number {
  return post.mock.calls.filter((call) => call[0] === '/api/screening/run').length
}

async function selectRuleAndRun(wrapper: ReturnType<typeof mountPage>['wrapper']) {
  const ruleSelect = wrapper.findComponent({ name: 'Select' })
  await ruleSelect.vm.$emit('update:value', 7)
  await flushPromises()
  const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
  expect(runButton).toBeTruthy()
  await runButton!.trigger('click')
  await flushPromises()
}

function seedRun() {
  saveRunSnapshot({
    results: RESULT_ROWS,
    runId: 'run-abc',
    totalMatched: 3,
    executionTime: 42,
    basePoolSize: 4800,
    dataDate: '2026-09-30',
    truncated: false,
    autoUpdateSnapshot: false,
    snapshotAsOf: null,
    readinessHint: null,
    strictModeWarning: null,
    strictExemptFields: [],
    warningCodes: ['LINEAGE_INVALID'],
    qualityStatus: 'available',
    dataReady: true,
    strictOnly: false,
    context: { ruleId: 7, ruleVersion: 3, ruleName: '低估值·行业龙头' },
  })
}

describe('「保留到我刷新」—— 失败与切页的边界', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    resetScreeningSession()
    ;(axios.put as Mock).mockResolvedValue({ data: { revision: 4 } })
  })

  afterEach(() => {
    resetScreeningSession()
  })

  it('跑出 3 条 → 切到详情页 → 切回：结果还在，且没有发出新的 /run', async () => {
    const { post } = mockApis()
    const first = mountPage()
    await flushPromises()
    await selectRuleAndRun(first.wrapper)
    expect(runCalls(post)).toBe(1)
    expect(first.wrapper.text()).toContain('600519')
    first.wrapper.unmount()

    const second = mountPage()
    await flushPromises()
    const panel = second.wrapper.findComponent(ScreeningResultsPanel)
    expect(panel.props('results')).toHaveLength(3)
    expect(panel.props('runState')).toBe('ok')
    expect(second.wrapper.text()).toContain('600519')
    expect(runCalls(post)).toBe(1)
    second.wrapper.unmount()
  })

  it('切回后点「运行筛选」被 409（数据更新中/写锁）拒绝：结果与快照都保留，只加一条说明', async () => {
    seedRun()
    const { post } = mockApis()
    ;(axios.post as Mock).mockImplementation((url: string) => {
      if (url === '/api/screening/run') return Promise.reject(writeLockError())
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('600519')

    // 用户点了「运行筛选」（这就是实测里那 5 次 409 的来源）
    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    await ruleSelect.vm.$emit('update:value', 7)
    await flushPromises()
    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    await runButton!.trigger('click')
    await flushPromises()

    expect(runCalls(post)).toBe(1)
    // ① 结果没有被清空，也没有落到"失败=清空"态
    const panel = wrapper.findComponent(ScreeningResultsPanel)
    expect(panel.props('results')).toHaveLength(3)
    expect(panel.props('runState')).toBe('ok')
    expect(panel.props('runId')).toBe('run-abc')
    expect(wrapper.text()).toContain('600519')
    expect(wrapper.text()).toContain('贵州茅台')
    // ② 会话快照也还在（下一轮切页不会读到空）
    expect(screeningSession.run?.runId).toBe('run-abc')
    // ③ 页面级提示讲清"这次没跑成，但结果还在"
    const notice = wrapper.find('#run-attempt-notice')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('本次运行没有执行')
    expect(notice.text()).toContain('上一次成功运行的结果')
    // ④ 上一次的结果仍然可保存（runId + 规则溯源都还有效）
    expect((wrapper.find('#save-btn').element as HTMLButtonElement).disabled).toBe(false)

    // ⑤ 再切一次页回来：结果仍然在，说明 409 没有偷偷清掉快照
    wrapper.unmount()
    const again = mountPage()
    await flushPromises()
    expect(again.wrapper.findComponent(ScreeningResultsPanel).props('results')).toHaveLength(3)
    expect(again.wrapper.text()).toContain('600519')
    expect(runCalls(post)).toBe(1)
    again.wrapper.unmount()
  })

  it('网络中断（请求没走完）同样保留结果', async () => {
    seedRun()
    mockApis()
    ;(axios.post as Mock).mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return Promise.reject({ isAxiosError: true, code: 'ERR_NETWORK', message: 'Network Error' })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountPage()
    await flushPromises()
    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    await ruleSelect.vm.$emit('update:value', 7)
    await flushPromises()
    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    await runButton!.trigger('click')
    await flushPromises()

    expect(wrapper.findComponent(ScreeningResultsPanel).props('results')).toHaveLength(3)
    expect(wrapper.text()).toContain('600519')
    expect(screeningSession.run?.runId).toBe('run-abc')
    wrapper.unmount()
  })

  it('切页期间才落地的失败（用户已经在别的页）：不清空任何东西', async () => {
    seedRun()
    const { post } = mockApis()
    let rejectRun: ((reason: unknown) => void) | null = null
    ;(axios.post as Mock).mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return new Promise((_resolve, reject) => { rejectRun = reject })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountPage()
    await flushPromises()
    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    await ruleSelect.vm.$emit('update:value', 7)
    await flushPromises()
    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    await runButton!.trigger('click')
    await flushPromises()
    expect(runCalls(post)).toBe(1)

    // 用户点了「数据详情」→ 筛选页卸载；此时在途的 /run 才失败（500 也一样）
    wrapper.unmount()
    expect(rejectRun).toBeTruthy()
    rejectRun!({ isAxiosError: true, response: { status: 500, data: { detail: '服务器内部错误' } } })
    await flushPromises()

    // 切页导致的失败绝不清空：快照仍在
    expect(screeningSession.run?.runId).toBe('run-abc')
    expect(screeningSession.run?.results).toHaveLength(3)

    const back = mountPage()
    await flushPromises()
    expect(back.wrapper.text()).toContain('600519')
    back.wrapper.unmount()
  })

  it('后端真跑坏了（500）：仍然清空并落失败态（主线 P0 语义不动）', async () => {
    seedRun()
    mockApis()
    ;(axios.post as Mock).mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return Promise.reject({
          isAxiosError: true,
          response: { status: 500, data: { detail: '服务器内部错误' } },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountPage()
    await flushPromises()
    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    await ruleSelect.vm.$emit('update:value', 7)
    await flushPromises()
    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    await runButton!.trigger('click')
    await flushPromises()

    const panel = wrapper.findComponent(ScreeningResultsPanel)
    expect(panel.props('runState')).toBe('failed')
    expect(panel.props('results')).toEqual([])
    expect(panel.props('runId')).toBeNull()
    expect(wrapper.text()).not.toContain('600519')
    expect(screeningSession.run).toBeNull()
    wrapper.unmount()
  })

  it('只有「用户刷新」才重置：会话清空后挂载走空态，不会自动重跑', async () => {
    seedRun()
    // resetScreeningSession = 文档重新加载且 navigation type=reload 的语义
    resetScreeningSession()
    const { post } = mockApis()

    const { wrapper } = mountPage()
    await flushPromises()

    expect(wrapper.text()).not.toContain('600519')
    expect(wrapper.find('#run-idle-state').exists()).toBe(true)
    expect(wrapper.text()).toContain('还没有运行筛选')
    expect(runCalls(post)).toBe(0)
    wrapper.unmount()
  })

  it('运行凭据 24h 过期：结果照常显示（不因过期而清空）', async () => {
    seedRun()
    screeningSession.run!.createdAt = Date.now() - (SERVER_RUN_TTL_MS + 60 * 60 * 1000)
    mockApis()

    const { wrapper } = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('600519')
    expect(screeningSession.run?.expired).toBe(true)
    expect(wrapper.find('#run-expired-alert').exists()).toBe(true)
    wrapper.unmount()
  })
})
