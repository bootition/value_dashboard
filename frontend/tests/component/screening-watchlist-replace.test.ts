import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider } from 'naive-ui'
import { h } from 'vue'
import ScreeningResultsPanel from '../../src/components/ScreeningResultsPanel.vue'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
  }
  return { default: mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'
import type { ScreeningResult } from '../../src/types/screening.ts'

const RULE_NAME = '往股东口袋装钱的大老公司'

const RESULT_ROWS: ScreeningResult[] = [
  {
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
  },
  {
    stock_code: '000858',
    name: '五粮液',
    exchange: 'SZSE',
    csrc_l1: '制造业',
    latest_close: 140.2,
    pe_ttm: 8,
    pb_mrq: 4.1,
    roe: 0.25,
    gross_margin: 0.75,
    net_margin: 0.36,
    debt_ratio: 0.3,
    revenue_yoy: 0.1,
    dividend_yield: 0.031,
  },
]

function mountPanel() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  const wrapper = mount(NMessageProvider, {
    slots: {
      default: () =>
        h(ScreeningResultsPanel, {
          results: RESULT_ROWS,
          executionTime: 5,
          basePoolSize: 100,
          dataDate: '2026-06-30',
          warningCodes: [],
          untrustedFields: [],
          qualityStatus: 'available',
          ruleTree: { id: 'r1', logic: 'AND', rules: [{ id: 'c1', field: 'pe_ttm', op: '>', value: 0 }] },
          runId: 'run-2',
          ruleId: 48,
          ruleVersion: 1,
          ruleName: RULE_NAME,
          lockedIndicators: {},
          basePoolConfig: { include_st: false },
        }),
    },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

function findButton(text: string): HTMLButtonElement | undefined {
  return Array.from(document.body.querySelectorAll('button')).find(
    (button) => button.textContent?.trim() === text,
  ) as HTMLButtonElement | undefined
}

describe('保存到自选列表：分组已存在时的替换 / 合并确认', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/watchlist/groups') {
        return Promise.resolve({
          data: {
            groups: [
              { group_name: RULE_NAME, cnt: 591, runs: 2, manual: 3, rule_ids: [48] },
            ],
          },
        })
      }
      return Promise.resolve({ data: {} })
    })
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/save') {
        return Promise.resolve({ data: { status: 'ok', result_id: 7 } })
      }
      if (url === '/api/screening/add_to_watchlist') {
        return Promise.resolve({
          data: {
            status: 'ok',
            added: 2,
            removed: 220,
            inserted: 2,
            updated: 0,
            manual_rows_preserved: 3,
            replaced: true,
          },
        })
      }
      return Promise.resolve({ data: {} })
    })
  })

  it('分组已存在且非空时弹确认框，两个选项都在，默认选中「替换」', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()
    await wrapper.find('#watchlist-btn').trigger('click')
    await flushPromises()

    const text = document.body.textContent ?? ''
    expect(text).toContain(`自选分组「${RULE_NAME}」已有 591 只`)
    expect(text).toContain('来自 2 次历史运行')
    expect(text).toContain('替换（推荐）')
    expect(text).toContain('合并')
    expect(text).toContain('分组内容 = 本次结果的 2 只')
    expect(text).toContain('可能出现本次筛选已经排除的旧股票')

    // 默认选中「替换」：直接确认 → replace_group 必须为 true
    const confirm = findButton('确认保存')
    expect(confirm).toBeTruthy()
    confirm!.click()
    await flushPromises()
    const addCall = (axios.post as Mock).mock.calls.find(
      ([url]) => url === '/api/screening/add_to_watchlist',
    )
    expect(addCall).toBeTruthy()
    expect(addCall![1]).toEqual(expect.objectContaining({
      group: RULE_NAME,
      result_id: 7,
      replace_group: true,
    }))
    // 成功后提示要如实报「替换了 X 只 / 新增 Y 只」
    expect(document.body.textContent ?? '').toContain('替换了 220 只历史结果 / 新增 2 只')
  })

  it('选「合并」→ 请求体 replace_group 为 false', async () => {
    const { wrapper } = mountPanel()
    await flushPromises()
    await wrapper.find('#watchlist-btn').trigger('click')
    await flushPromises()

    const radios = Array.from(
      document.body.querySelectorAll<HTMLInputElement>('input[type="radio"]'),
    )
    expect(radios.length).toBe(2)
    // 第 2 个是「合并」；点它触发 n-radio-group 的 v-model
    radios[1].click()
    await flushPromises()

    const confirm = findButton('确认保存')
    expect(confirm).toBeTruthy()
    confirm!.click()
    await flushPromises()

    const addCall = (axios.post as Mock).mock.calls.find(
      ([url]) => url === '/api/screening/add_to_watchlist',
    )
    expect(addCall).toBeTruthy()
    expect(addCall![1]).toEqual(expect.objectContaining({
      group: RULE_NAME,
      replace_group: false,
    }))
    expect(document.body.textContent ?? '').toContain('已合并到自选分组')
  })

  it('分组不存在时不出确认框，自动路径保持合并（replace_group false）', async () => {
    const get = axios.get as Mock
    get.mockResolvedValue({ data: { groups: [] } })
    const { wrapper } = mountPanel()
    await flushPromises()
    await wrapper.find('#watchlist-btn').trigger('click')
    await flushPromises()

    expect(document.body.textContent ?? '').not.toContain('替换（推荐）')
    const addCall = (axios.post as Mock).mock.calls.find(
      ([url]) => url === '/api/screening/add_to_watchlist',
    )
    expect(addCall).toBeTruthy()
    expect(addCall![1]).toEqual(expect.objectContaining({ replace_group: false }))
  })

  it('读取分组现状失败时按合并继续，不删除任何已有股票', async () => {
    const get = axios.get as Mock
    get.mockRejectedValue(new Error('network down'))
    const { wrapper } = mountPanel()
    await flushPromises()
    await wrapper.find('#watchlist-btn').trigger('click')
    await flushPromises()

    const addCall = (axios.post as Mock).mock.calls.find(
      ([url]) => url === '/api/screening/add_to_watchlist',
    )
    expect(addCall).toBeTruthy()
    expect(addCall![1]).toEqual(expect.objectContaining({ replace_group: false }))
  })
})
