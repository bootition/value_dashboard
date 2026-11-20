/**
 * 「切页保状态」回归（2026-11-24 用户投诉）。
 *
 * 用户原话：
 *   「我筛选完成之后，点到**数据状态**那一页，然后再点回去，为什么就自动刷新了？
 *    我的结果没保存就直接丢掉了…… 应该是我**不点击浏览器刷新**，那个界面就还保持在那里。」
 *
 * 根因：`App.vue` 的 `<router-view />` 没有 keep-alive，路由切换会**卸载**页面组件，
 * 页面里的 `ref()` 结果（results / runId / totalMatched / warningCodes …）随之销毁，
 * 切回来是一个"全新"的筛选页 —— 在用户眼里就是"自动刷新了"。
 *
 * 修法：把这些"用户跑出来的东西"提到模块级单例 `stores/screening-session.ts`
 * （组件卸载不销毁模块；F5 重建 JS 上下文 → 自然回到空态）。
 *
 * 本文件钉死四条不变量：
 *  1. 卸载再挂载：结果还在，且**没有发出新的 /api/screening/run**；
 *  2. 运行凭据过期（服务端只留 24h）：结果照常显示，只停用保存入口并说明原因；
 *  3. F5 语义（模块状态清空后挂载）：不再显示旧结果；
 *  4. 草稿回灌（restoreDraft 写 strict_only）不得触发自动重跑。
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

/** 一条完整的 `/run` 结果行（`ScreeningResult` 的必填元数据列一个都不能少）。 */
const RESULT_ROW: ScreeningResult = {
  stock_code: '600519',
  name: '贵州茅台',
  exchange: 'SSE',
  csrc_l1: '制造业',
  latest_close: 1680.5,
  pe_ttm: 10,
  pb_mrq: 8.2,
  roe: 0.3,
  gross_margin: 0.91,
  net_margin: 0.52,
  debt_ratio: 0.2,
  revenue_yoy: 0.15,
  dividend_yield: 0.023,
}

const RUN_OK = {
  results: [RESULT_ROW],
  total: 1,
  execution_time_ms: 42,
  base_pool_size: 4800,
  data_date: '2026-09-30',
  run_id: 'run-abc',
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', redirect: '/screening' },
      { path: '/screening', name: 'screening', component: { template: '<div />' } },
      { path: '/data-status', name: 'data-status', component: { template: '<div />' } },
    ],
  })
}

/** 与 App.vue 同构：页面依赖 n-message / n-dialog provider 与 router。 */
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

function mockApis(options: { draft?: unknown; warningCodes?: string[] } = {}) {
  const get = axios.get as Mock
  get.mockImplementation((url: string) => {
    if (url === '/api/screening/draft') {
      return Promise.resolve({ data: { draft: options.draft ?? null, revision: 3 } })
    }
    if (url === '/api/screening/rules') return Promise.resolve({ data: { rules: RULES } })
    if (url === '/api/screening/indicators') {
      return Promise.resolve({ data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 } })
    }
    if (url === '/api/data-status/summary') {
      return Promise.resolve({
        data: {
          data_quality: {
            warning_codes: options.warningCodes ?? [],
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

/** 点到「运行筛选」：先选已保存规则，再点按钮（与线上操作路径一致）。 */
async function selectRuleAndRun(wrapper: ReturnType<typeof mountPage>['wrapper']) {
  const ruleSelect = wrapper.findComponent({ name: 'Select' })
  await ruleSelect.vm.$emit('update:value', 7)
  await flushPromises()
  const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
  expect(runButton).toBeTruthy()
  await runButton!.trigger('click')
  await flushPromises()
}

/** `/api/screening/run` 的调用次数 —— 本文件的核心断言口径。 */
function runCalls(post: Mock): number {
  return post.mock.calls.filter((call) => call[0] === '/api/screening/run').length
}

function countCalls(get: Mock, url: string): number {
  return get.mock.calls.filter((call) => call[0] === url).length
}

/** 预置一份"历史运行结果"（= 上一次挂载跑出来的东西）。 */
function seedRun(overrides: Partial<Parameters<typeof saveRunSnapshot>[0]> = {}) {
  saveRunSnapshot({
    results: RUN_OK.results,
    runId: RUN_OK.run_id,
    totalMatched: RUN_OK.total,
    executionTime: RUN_OK.execution_time_ms,
    basePoolSize: RUN_OK.base_pool_size,
    dataDate: RUN_OK.data_date,
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
    ...overrides,
  })
}

describe('筛选页「切页保状态」', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    // 每个 it = 一次新的页面加载（F5）
    resetScreeningSession()
    ;(axios.put as Mock).mockResolvedValue({ data: { revision: 4 } })
  })

  afterEach(() => {
    resetScreeningSession()
  })

  it('跑出结果 → 卸载（切到数据状态页）→ 重新挂载：结果还在，且没有发出新的 /run', async () => {
    const { post, get } = mockApis({ warningCodes: ['LINEAGE_INVALID'] })

    const first = mountPage()
    await flushPromises()
    await selectRuleAndRun(first.wrapper)

    expect(runCalls(post)).toBe(1)
    expect(first.wrapper.text()).toContain('600519')
    const summaryCallsAfterFirstMount = countCalls(get, '/api/data-status/summary')

    // 用户点了「数据状态」→ 筛选页组件被路由卸载（这正是丢结果的时刻）
    first.wrapper.unmount()

    // 用户点回「筛选」
    const second = mountPage()
    await flushPromises()

    // ① 结果与运行凭据都还在
    expect(second.wrapper.text()).toContain('600519')
    expect(second.wrapper.text()).toContain('贵州茅台')
    expect(screeningSession.run?.runId).toBe('run-abc')
    expect(screeningSession.run?.totalMatched).toBe(1)
    const panel = second.wrapper.findComponent(ScreeningResultsPanel)
    expect(panel.props('runId')).toBe('run-abc')
    expect(panel.props('totalMatched')).toBe(1)
    expect(panel.props('executionTime')).toBe(42)
    expect(panel.props('dataDate')).toBe('2026-09-30')

    // ② 披露告警原样保留（没有被"现在的"数据状态覆盖），且不再重取 summary
    expect(panel.props('warningCodes')).toEqual(['LINEAGE_INVALID'])
    expect(panel.props('qualityStatus')).toBe('available')
    expect(countCalls(get, '/api/data-status/summary')).toBe(summaryCallsAfterFirstMount)

    // ③ 没有自动重跑（本文件最关键的一条）
    expect(runCalls(post)).toBe(1)

    // ④ 保存入口立刻可用（ruleId 来自运行快照，不依赖 rules 重新拉取完成）
    expect((second.wrapper.find('#save-btn').element as HTMLButtonElement).disabled).toBe(false)

    second.wrapper.unmount()
  })

  it('历史结果的 runId 已失效（服务端只留 24h）：结果照常显示 + 保存入口置灰 + 明确提示', async () => {
    seedRun()
    // 把运行时刻拨到 25 小时前 = 超过后端 SCREENING_RUN_TTL_HOURS
    screeningSession.run!.createdAt = Date.now() - (SERVER_RUN_TTL_MS + 60 * 60 * 1000)
    mockApis()

    const { wrapper } = mountPage()
    await flushPromises()

    // 结果**没有被清空**（用户最恨的就是一过期就整页变白）
    expect(wrapper.text()).toContain('600519')
    expect(wrapper.text()).toContain('贵州茅台')
    expect(screeningSession.run?.expired).toBe(true)

    // 保存入口置灰 + 说明原因
    expect((wrapper.find('#save-btn').element as HTMLButtonElement).disabled).toBe(true)
    expect((wrapper.find('#export-btn').element as HTMLButtonElement).disabled).toBe(true)
    const expiredAlert = wrapper.find('#run-expired-alert')
    expect(expiredAlert.exists()).toBe(true)
    expect(expiredAlert.text()).toContain('这次结果已过期，请重新运行')
    expect(expiredAlert.text()).toContain('结果仍然照常显示')

    // 仍然没有自动重跑
    expect(runCalls(axios.post as Mock)).toBe(0)

    wrapper.unmount()
  })

  it('F5 语义：会话状态清空后挂载 → 不再显示旧结果（要走空态）', async () => {
    seedRun()
    // F5 = 模块重新初始化：模块级单例整体回初值
    resetScreeningSession()
    const { post } = mockApis()

    const { wrapper } = mountPage()
    await flushPromises()

    expect(wrapper.text()).not.toContain('600519')
    expect(wrapper.text()).toContain('运行筛选后显示候选公司')
    const panel = wrapper.findComponent(ScreeningResultsPanel)
    expect(panel.props('runId')).toBeNull()
    expect(runCalls(post)).toBe(0)

    wrapper.unmount()
  })

  it('草稿回灌 strict_only 不再触发自动重跑（原 watch(strictOnly) 的口子）', async () => {
    // 上一次已经跑出结果，且当时用的是非 strict 口径
    seedRun({ strictOnly: false })
    screeningSession.activeRuleId = 7
    // 服务端草稿里 strict_only=true：旧实现下 restoreDraft 写回 ref 会喂给
    // `watch(strictOnly)` → 400ms 后自动发一次 /run（正是用户说的"切回来自己又跑一遍"）。
    mockApis({
      draft: {
        conditions: { logic: 'AND', rules: [{ field: 'pe_ttm', op: '<', value: 15 }] },
        base_pool: { exclude_st: true, exclude_suspended: true, min_listing_years: 1 },
        strict_only: true,
        result_columns: ['stock_code', 'pe_ttm'],
      },
    })

    const { wrapper } = mountPage()
    await flushPromises()

    // 草稿确实回灌了（strict 开关被打开），说明走的不是"忽略草稿"的捷径
    expect(screeningSession.run?.runId).toBe('run-abc')
    expect(wrapper.text()).toContain('仅使用严格可信数据')
    // 等过旧的 400ms 防抖窗口，仍然一次 /run 都没有
    await new Promise((resolve) => setTimeout(resolve, 500))
    await flushPromises()
    expect(runCalls(axios.post as Mock)).toBe(0)
    // 结果也没有被重跑冲掉
    expect(wrapper.text()).toContain('600519')

    wrapper.unmount()
  })
})
