import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { h, nextTick } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import ScreeningPage from '../../src/views/ScreeningPage.vue'
import ScreeningResultsPanel from '../../src/components/ScreeningResultsPanel.vue'
import { resetScreeningSession } from '../../src/stores/screening-session.ts'

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
import type { ScreeningResult } from '../../src/types/screening.ts'

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

function makeRouter() {
  return createRouter({ history: createMemoryHistory(), routes: [] })
}

/** 页面挂载的 axios.get 基线（草稿/规则/指标/数据质量/自动更新）。 */
function mockPageGets() {
  const get = axios.get as Mock
  get.mockImplementation((url: string) => {
    if (url === '/api/screening/draft') {
      return Promise.resolve({ data: { draft: null, revision: 0 } })
    }
    if (url === '/api/screening/rules') {
      return Promise.resolve({
        data: {
          rules: [{
            id: 1,
            name: 'test-rule',
            version: 1,
            rule_json: {
              conditions: { logic: 'AND', rules: [{ field: 'pe_ttm', op: '>', value: 0 }] },
              sort: [],
              columns: ['stock_code', 'pe_ttm'],
            },
            locked_indicators: {},
            status: 'saved',
            created_at: '2026-08-01',
          }],
        },
      })
    }
    if (url === '/api/screening/indicators') {
      return Promise.resolve({ data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 } })
    }
    if (url === '/api/data-status/summary') {
      return Promise.resolve({
        data: { data_quality: { warning_codes: [], minimum_data_readiness: { ready: true } } },
      })
    }
    if (url === '/api/data-status/auto-update') {
      return Promise.resolve({ data: { state: 'enabled', current_stage: 'finished' } })
    }
    return Promise.resolve({ data: {} })
  })
}

function mountScreeningPage() {
  const router = makeRouter()
  const wrapper = mount(NDialogProvider, {
    slots: {
      default: () => h(NMessageProvider, null, { default: () => h(ScreeningPage) }),
    },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

function mountResultsPanel(props: Record<string, unknown>) {
  const router = makeRouter()
  return mount(NMessageProvider, {
    slots: {
      default: () =>
        h(ScreeningResultsPanel, {
          results: [RESULT_ROW],
          executionTime: 5,
          basePoolSize: 100,
          dataDate: '2026-06-30',
          warningCodes: [],
          untrustedFields: [],
          qualityStatus: 'available',
          ruleTree: { id: 'r1', logic: 'AND', rules: [] },
          runId: 'run-1',
          ruleId: 1,
          ruleVersion: 1,
          ruleName: '测试规则',
          lockedIndicators: {},
          basePoolConfig: {},
          ...props,
        }),
    },
    global: { plugins: [router] },
  })
}

async function chooseRuleAndRun(wrapper: ReturnType<typeof mountScreeningPage>['wrapper']) {
  const ruleSelect = wrapper.findComponent({ name: 'Select' })
  expect(ruleSelect.exists()).toBe(true)
  await ruleSelect.vm.$emit('update:value', 1)
  await flushPromises()
  const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
  expect(runButton).toBeTruthy()
  await runButton!.trigger('click')
  await flushPromises()
}

describe('ScreeningPage 显式运行状态（P0：失败不得残留旧结果）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // 「切页保状态」把运行结果提到了模块级单例（stores/screening-session.ts）：
    // 一个 it 就是一次新的页面加载（= F5），必须整体重置，否则上一个用例跑出来的
    // 结果会漏到下一个用例里，测试变成顺序相关（本文件断言的是"页面级状态机"，
    // 与"会话是否保活"无关 —— 那条由 screening-page-state.test.ts 守护）。
    resetScreeningSession()
    ;(axios.put as Mock).mockResolvedValue({ data: { revision: 1 } })
    mockPageGets()
  })

  it('先成功再失败：清空 results/runId、保存入口全禁用、出现"失败"而不是"0 家符合"，且可原地重试', async () => {
    const post = axios.post as Mock
    let runCall = 0
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        runCall += 1
        // 第 1 次成功；第 2 次失败；第 3 次（点「重试运行」）恢复成功。
        if (runCall === 2) {
          return Promise.reject({
            isAxiosError: true,
            response: { status: 500, data: { detail: 'database exploded' } },
          })
        }
        return Promise.resolve({
          data: {
            results: [RESULT_ROW],
            total: 1,
            execution_time_ms: 5,
            base_pool_size: 100,
            data_date: '2026-06-30',
            run_id: `run-${runCall}`,
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()
    const panel = wrapper.findComponent(ScreeningResultsPanel)

    await chooseRuleAndRun(wrapper)
    expect(panel.props('runState')).toBe('ok')
    expect(panel.props('runId')).toBe('run-1')
    expect(wrapper.text()).toContain('600519')

    // 第二次运行失败
    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    await runButton!.trigger('click')
    await flushPromises()

    expect(runCall).toBe(2)
    // ① 结果被清空、runId 归零 —— 旧股票不会再被当作"当前规则"的结果
    expect(panel.props('results')).toEqual([])
    expect(panel.props('runId')).toBeNull()
    expect(panel.props('runState')).toBe('failed')
    expect(wrapper.text()).not.toContain('600519')
    expect(wrapper.text()).not.toContain('家公司符合当前规则')
    // ② 页面出现"失败"文案（不是"0 家符合"），并给出后端原因
    expect(wrapper.text()).toContain('筛选失败')
    expect(wrapper.text()).toContain('服务器内部错误')
    // ③ 所有依赖 runId 的入口都在，但全部禁用（灰字原因也可见）
    for (const id of ['#save-btn', '#export-btn', '#watchlist-btn']) {
      const btn = wrapper.find(id)
      expect(btn.exists(), `${id} 应该存在（禁用而不是消失）`).toBe(true)
      expect((btn.element as HTMLButtonElement).disabled, `${id} 应该禁用`).toBe(true)
    }
    expect(wrapper.text()).toContain('上一次结果已清空')
    expect(wrapper.find('#run-actions-reason').exists()).toBe(true)

    // ④ 失败后直接重试，不需要刷新页面
    await wrapper.find('#retry-run-btn').trigger('click')
    await flushPromises()
    expect(runCall).toBe(3)
    expect(panel.props('runState')).toBe('ok')
    expect(panel.props('runId')).toBe('run-3')
    expect(wrapper.text()).toContain('600519')
  })

  it('成功但 0 条：与"失败"文案不同（空态 ①/②/③ 三者可区分）', async () => {
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return Promise.resolve({
          data: {
            results: [],
            total: 0,
            execution_time_ms: 5,
            base_pool_size: 100,
            data_date: '2026-06-30',
            run_id: 'run-empty',
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()

    // ① 没跑过
    expect(wrapper.find('#run-idle-state').exists()).toBe(true)
    expect(wrapper.text()).toContain('还没有运行筛选')
    // D3（顺手修）：不再指向早已删除的排序区
    expect(wrapper.text()).not.toContain('在「排序」设置优先级')
    expect(wrapper.text()).toContain('点表头')

    await chooseRuleAndRun(wrapper)

    const panel = wrapper.findComponent(ScreeningResultsPanel)
    expect(panel.props('runState')).toBe('empty')
    expect(panel.props('results')).toEqual([])

    // ② 跑成功但 0 条：是"条件太严"，不是故障
    expect(wrapper.find('#run-empty-state').exists()).toBe(true)
    expect(wrapper.find('#run-idle-state').exists()).toBe(false)
    expect(wrapper.text()).toContain('没有公司符合当前条件')
    expect(wrapper.text()).toContain('这不是出错')
    // ③ 与失败态不同：不能出现失败文案/作废卡片，也不能说"0 家符合当前规则"
    expect(wrapper.text()).not.toContain('筛选失败')
    expect(wrapper.text()).not.toContain('上一次结果已作废')
    expect(wrapper.text()).not.toContain('家公司符合当前规则')
    // 0 条没有可保存的东西，入口不该出现
    expect(wrapper.find('#watchlist-btn').exists()).toBe(false)
  })

  it('后端返回 run-invalidated（保存时 run 已失效）→ 页面立即落失败态并清空', async () => {
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return Promise.resolve({
          data: {
            results: [RESULT_ROW],
            total: 1,
            execution_time_ms: 5,
            base_pool_size: 100,
            data_date: '2026-06-30',
            run_id: 'run-1',
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()
    const panel = wrapper.findComponent(ScreeningResultsPanel)
    await chooseRuleAndRun(wrapper)
    expect(panel.props('runId')).toBe('run-1')

    // 模拟面板在保存时收到 `saved screening result not found`
    panel.vm.$emit('run-invalidated', '保存的筛选结果不存在；可能已被清理，请重新运行并保存')
    await nextTick()
    await flushPromises()

    expect(panel.props('runState')).toBe('failed')
    expect(panel.props('results')).toEqual([])
    expect(panel.props('runId')).toBeNull()
    expect(wrapper.text()).toContain('保存的筛选结果不存在')
    expect(wrapper.text()).not.toContain('600519')
  })

  it('首次运行就失败：不再与"没跑过/没命中"同形（失败卡直接出现）', async () => {
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return Promise.reject({
          isAxiosError: true,
          response: { status: 409, data: { detail: { reason_code: 'screening_data_quality_not_ready' } } },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()
    await chooseRuleAndRun(wrapper)

    const panel = wrapper.findComponent(ScreeningResultsPanel)
    expect(panel.props('runState')).toBe('failed')
    expect(panel.props('results')).toEqual([])
    expect(panel.props('runId')).toBeNull()
    // 失败态：既不是"没跑过"的空态卡，也不是"0 条"的空态卡
    expect(wrapper.find('#run-idle-state').exists()).toBe(false)
    expect(wrapper.find('#run-empty-state').exists()).toBe(false)
    expect(wrapper.find('#screening-run-failed').exists()).toBe(true)
    expect(wrapper.text()).toContain('数据质量不满足筛选要求')
    expect(wrapper.text()).not.toContain('家公司符合当前规则')
  })

  it('disclosed_warnings（/run 的 screening_warnings）非空 → 黄色提示条可见且中文', async () => {
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return Promise.resolve({
          data: {
            results: [RESULT_ROW],
            total: 1,
            execution_time_ms: 5,
            base_pool_size: 5580,
            data_date: '2026-06-30',
            run_id: 'run-warn',
            screening_warnings: [
              {
                code: 'TRADING_CALENDAR_INCOMPLETE',
                severity: 'disclose',
                detail: '158/5,580 只标的日历覆盖不匹配（2.8%），其技术指标窗口计数可能偏移',
              },
              { code: 'SNAPSHOT_STALE', severity: 'disclose' },
            ],
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()
    await chooseRuleAndRun(wrapper)

    // 典型披露：具体数字必须让用户看见（而不是塞进错误弹窗）
    const bar = wrapper.find('[data-warning-code="TRADING_CALENDAR_INCOMPLETE"]')
    expect(bar.exists()).toBe(true)
    expect(bar.text()).toContain('158/5,580')
    expect(bar.text()).toContain('日历覆盖不匹配')
    // 黄色（warning）而不是 error
    const alertComponent = wrapper
      .findAllComponents({ name: 'Alert' })
      .find((c) => c.text().includes('158/5,580'))
    expect(alertComponent).toBeTruthy()
    expect(alertComponent!.props('type')).toBe('warning')
    // 已知码走中文映射，绝不裸渲染枚举
    expect(wrapper.text()).toContain('指标快照落后于最新行情')
    expect(wrapper.text()).not.toContain('SNAPSHOT_STALE')
  })
})

describe('ScreeningResultsPanel 文案与保存防护（P0/B/D）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('保存时后端说 saved screening result not found → 通知页面落失败态（不再显示旧结果）', async () => {
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/save') {
        return Promise.reject({
          isAxiosError: true,
          response: { status: 400, data: { detail: 'saved screening result not found' } },
        })
      }
      return Promise.resolve({ data: {} })
    })
    const wrapper = mountResultsPanel({})
    await flushPromises()

    await wrapper.find('#save-btn').trigger('click')
    await flushPromises()
    const titleInput = document.body.querySelector(
      'input[placeholder="给这次筛选结果起个名字"]',
    ) as HTMLInputElement | null
    expect(titleInput).toBeTruthy()
    titleInput!.value = '测试保存'
    titleInput!.dispatchEvent(new Event('input'))
    await flushPromises()
    const confirmSave = Array.from(document.body.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === '保存',
    )
    expect(confirmSave).toBeTruthy()
    confirmSave!.click()
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/screening/save',
      expect.objectContaining({ run_id: 'run-1' }),
    )
    const emitted = wrapper.findComponent(ScreeningResultsPanel).emitted('run-invalidated')
    expect(emitted).toBeTruthy()
    expect(String(emitted![0]?.[0])).toContain('重新运行')
  })

  it('runId 为空（失败/未运行）时保存/导出/加入自选全禁用，并给出灰字原因', async () => {
    const wrapper = mountResultsPanel({ runId: null })
    await flushPromises()
    for (const id of ['#save-btn', '#export-btn', '#watchlist-btn']) {
      const btn = wrapper.find(id)
      expect(btn.exists(), `${id} 应该存在`).toBe(true)
      expect((btn.element as HTMLButtonElement).disabled, `${id} 应该禁用`).toBe(true)
    }
    expect(wrapper.text()).toContain('缺少本次运行记录（runId）')
  })

  it('D1：导出顺序说明如实（不再谎称"导出按当前顺序"）', async () => {
    const wrapper = mountResultsPanel({})
    await flushPromises()
    const text = wrapper.text()
    expect(text).not.toContain('导出按当前顺序')
    expect(text).toContain('导出的 CSV 按本次筛选的原始顺序')
    expect(text).toContain('只影响当前屏幕显示')
  })

  it('D2：数据质量警告码走中文映射（用户看不到 SNAPSHOT_STALE 裸枚举）', async () => {
    const wrapper = mountResultsPanel({ warningCodes: ['SNAPSHOT_STALE'] })
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('指标快照落后于最新行情')
    expect(text).not.toContain('SNAPSHOT_STALE')
  })
})
