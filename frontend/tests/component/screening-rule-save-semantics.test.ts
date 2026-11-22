/**
 * 规则"改了就新建一条"的 UX 修复（2026-11-25 用户截图投诉）。
 *
 * 用户原话：
 * 「我希望在同一套规则下，我改了点数字后，不要自动保存成一条新的规则，应该让我
 *   自己来命名。不然你搞成一大堆 V1、V2、V3 这些玩意，我以后怎么分辨呢？」
 * 「自选列表里面明明没有这个运行规则显示的分组，为什么它还删不掉？」
 *
 * 本文件锁四条**用户路径**（全部用 axios spy 断言发了/没发哪条请求）：
 * 1. 已有规则 + 草稿脏 + 点运行 → 只发 `PUT /rules/{id}`，**不发** `POST /rules/save`
 * 2. 首次使用（无 activeRule）+ 点运行 → **不发任何保存请求**，出现"请先命名"
 * 3. 「另存为新规则」名字为空 → 不发请求 + 就地校验提示
 * 4. 删除被拒 → 后端 409 里的动态条数原样显示（不许被泛化兜底吞掉）
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { h, nextTick } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import ScreeningPage from '../../src/views/ScreeningPage.vue'

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
import { resetScreeningSession } from '../../src/stores/screening-session.ts'

const SAVED_RULE = {
  id: 49,
  name: 'ROCE遗传好公司',
  version: 1,
  rule_json: {
    conditions: { logic: 'AND', rules: [{ field: 'pe_ttm', op: '>', value: 10 }] },
    sort: [],
    columns: ['stock_code', 'pe_ttm'],
  },
  locked_indicators: {},
  status: 'saved',
  created_at: '2026-11-01',
}

function mountScreeningPage() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  const wrapper = mount(NDialogProvider, {
    slots: {
      default: () => h(NMessageProvider, null, { default: () => h(ScreeningPage) }),
    },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

function stubBaseGets(rules: unknown[] = [SAVED_RULE]): void {
  const get = axios.get as Mock
  get.mockImplementation((url: string) => {
    if (url === '/api/screening/draft') {
      return Promise.resolve({ data: { draft: null, revision: 0 } })
    }
    if (url === '/api/screening/rules') {
      return Promise.resolve({ data: { rules } })
    }
    if (url === '/api/screening/indicators') {
      return Promise.resolve({
        data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 },
      })
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

function stubRun(): void {
  const post = axios.post as Mock
  post.mockImplementation((url: string) => {
    if (url === '/api/screening/run') {
      return Promise.resolve({
        data: {
          results: [{ stock_code: '600519', name: '贵州茅台', pe_ttm: 10 }],
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
}

async function selectRule(wrapper: ReturnType<typeof mountScreeningPage>['wrapper']): Promise<void> {
  const ruleSelect = wrapper.findComponent({ name: 'Select' })
  expect(ruleSelect.exists()).toBe(true)
  await ruleSelect.vm.$emit('update:value', SAVED_RULE.id)
  await flushPromises()
}

function callsTo(mock: Mock, url: string): unknown[][] {
  return mock.mock.calls.filter(([called]) => called === url)
}

describe('规则保存语义：一套规则只有一行（2026-11-25 用户投诉）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetScreeningSession()
    document.body.innerHTML = ''
    // 草稿防抖自动保存也走 axios.put：默认必须 resolve，否则收尾时抛未处理拒绝。
    ;(axios.put as Mock).mockResolvedValue({ data: { revision: 1 } })
  })

  it('改了点数字再点「运行筛选」：只发 PUT 原地更新，不发 POST /rules/save 新建', async () => {
    stubBaseGets()
    stubRun()
    const put = axios.put as Mock
    put.mockImplementation((url: string) => {
      if (url === '/api/screening/rules/49') {
        return Promise.resolve({ data: { status: 'ok', rule_id: 49, version: 2 } })
      }
      return Promise.resolve({ data: { revision: 1 } })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()
    await selectRule(wrapper)

    // 改一个条件值 → 草稿相对已保存版本变脏
    const rows = wrapper.findAllComponents({ name: 'RuleConditionRow' })
    expect(rows).toHaveLength(1)
    await rows[0]!.vm.$emit('update:value', 25)
    await flushPromises()

    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    expect(runButton).toBeTruthy()
    await runButton!.trigger('click')
    await flushPromises()

    const updates = callsTo(put, '/api/screening/rules/49')
    expect(updates, '没有发出 PUT /api/screening/rules/49 原地更新').toHaveLength(1)
    expect(
      callsTo(axios.post as Mock, '/api/screening/rules/save'),
      '草稿脏了还在 POST /rules/save 新建一条同名规则（V1/V2/V3 的来源）',
    ).toHaveLength(0)

    const payload = updates[0]![1] as {
      rule_json: { conditions: { rules: Array<Record<string, unknown>> } }
      expected_version: number
    }
    expect(payload.rule_json.conditions.rules[0]).toMatchObject({ field: 'pe_ttm', op: '>', value: 25 })
    expect(payload.expected_version).toBe(1)
    // 运行必须按**更新后**的版本发（否则后端会说版本不存在）
    expect(axios.post).toHaveBeenCalledWith(
      '/api/screening/run',
      expect.objectContaining({ rule_id: 49, rule_version: 2 }),
      expect.objectContaining({ timeout: 120_000 }),
    )
  })

  it('首次使用（没有 activeRule）点运行：不发任何保存请求，并提示"先给规则起个名字"', async () => {
    stubBaseGets([])
    stubRun()
    const { wrapper } = mountScreeningPage()
    await flushPromises()

    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    await runButton!.trigger('click')
    await flushPromises()

    // 不发 POST /rules/save（更不许 autoRuleName() 静默造名），也不发 /run
    expect(callsTo(axios.post as Mock, '/api/screening/rules/save')).toHaveLength(0)
    expect(callsTo(axios.post as Mock, '/api/screening/run')).toHaveLength(0)
    expect(wrapper.text()).toContain('请先给这套规则起个名字')
  })

  it('「另存为新规则」名字为空：不发请求，就地提示"请先输入规则名称"', async () => {
    stubBaseGets()
    stubRun()
    const { wrapper } = mountScreeningPage()
    await flushPromises()
    await selectRule(wrapper)

    const saveAsNew = wrapper.findAll('button').find((b) => b.text().includes('另存为新规则'))
    expect(saveAsNew, '缺少「另存为新规则」按钮').toBeTruthy()
    await saveAsNew!.trigger('click')
    await flushPromises()

    expect(callsTo(axios.post as Mock, '/api/screening/rules/save')).toHaveLength(0)
    expect(wrapper.text()).toContain('请先输入规则名称')
  })

  it('「另存为新规则」填了名字：POST 一次新建（用户显式动作才允许新建）', async () => {
    stubBaseGets()
    stubRun()
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/rules/save') {
        return Promise.resolve({ data: { status: 'ok', rule_id: 88, version: 1 } })
      }
      return Promise.resolve({ data: {} })
    })
    const { wrapper } = mountScreeningPage()
    await flushPromises()
    await selectRule(wrapper)

    const nameInput = wrapper
      .findAll('input')
      .find((input) => (input.attributes('placeholder') ?? '').includes('规则名称'))
    expect(nameInput).toBeTruthy()
    await nameInput!.setValue('我的第二套规则')
    await flushPromises()

    const saveAsNew = wrapper.findAll('button').find((b) => b.text().includes('另存为新规则'))
    await saveAsNew!.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/screening/rules/save',
      expect.objectContaining({ name: '我的第二套规则' }),
    )
  })

  it('删除被拒：把后端的动态条数原样显示（不许被泛化的 409 兜底吞掉）', async () => {
    stubBaseGets()
    const backendMessage =
      '该规则有 8 条已保存的筛选结果、573 条自选记录引用，不能删除；请先删除这些筛选结果或自选记录'
    ;(axios.delete as Mock).mockRejectedValue({
      isAxiosError: true,
      response: {
        status: 409,
        data: {
          detail: {
            reason_code: 'rule_has_provenance_refs',
            message: backendMessage,
            refs: { results: 8, watchlist: 573, runs: 0 },
          },
        },
      },
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()
    await selectRule(wrapper)

    const deleteButton = wrapper.findAll('button').find((b) => b.text().includes('删除选中规则'))
    expect(deleteButton, '缺少「删除选中规则」按钮').toBeTruthy()
    await deleteButton!.trigger('click')
    await flushPromises()
    await nextTick()

    // 确认对话框里的「删除」
    const confirm = Array.from(document.querySelectorAll('.n-dialog__action button')).find(
      (button) => button.textContent?.trim() === '删除',
    )
    expect(confirm, '确认对话框没有渲染出「删除」按钮').toBeTruthy()
    ;(confirm as HTMLElement).click()
    await flushPromises()
    await nextTick()
    await flushPromises()

    const shown = document.body.textContent ?? ''
    expect(shown).toContain('8 条已保存的筛选结果')
    expect(shown).toContain('573 条自选记录')
    // 旧文案里指向不明的"运行结果"不许再出现
    expect(shown).not.toContain('运行结果')
  })
})
