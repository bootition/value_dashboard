/**
 * 筛选结果逐只「+ 加入自选」（2026-11-26 用户需求）。
 *
 * 用户原话：「我直接在按规则分组的列表里面有个『点击加入自选列表』」「那种『点击加入
 * 自选』应该是加入默认自选列表」。
 *
 * 钉死的契约：
 * 1. 每行都有独立入口 → `POST /api/watchlist/add`，**不带 group**（落默认分组）；
 * 2. 已经在该股票上的行显示「✓ 已加入」且**置灰不可点**（避免重复点）；
 * 3. 成功提示必须带**股票名**；失败（例如黑名单 400）必须把后端原因带出来，不许吞；
 * 4. 整批「保存到自选列表」的语义**不被改动**（仍然打 `/api/screening/add_to_watchlist`）。
 */

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
  return { default: mock, ...mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'

function row(code: string, name: string) {
  return {
    stock_code: code, name, exchange: 'SSE', csrc_l1: '制造业', csrc_l2: '白酒', latest_close: 10,
    pe_ttm: 12, pb_mrq: 3, roe: 0.2, gross_margin: 0.5, net_margin: 0.3,
    debt_ratio: 0.2, revenue_yoy: 0.1, net_profit_yoy: 0.1, dividend_yield: 0.03,
  }
}

const RESULT_ROWS = [row('600519', '贵州茅台'), row('000001', '平安银行')]

function mountPanel() {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  const wrapper = mount(NMessageProvider, {
    slots: {
      default: () => h(ScreeningResultsPanel, {
        results: RESULT_ROWS,
        executionTime: 5,
        basePoolSize: 100,
        dataDate: '2026-11-20',
        warningCodes: [],
        untrustedFields: [],
        qualityStatus: 'available',
        ruleTree: { id: 'r1', logic: 'AND', rules: [{ id: 'c1', field: 'pe_ttm', op: '>', value: 0 }] },
        runId: 'run-1',
        ruleId: 1,
        ruleVersion: 1,
        ruleName: '优质价值候选池',
        lockedIndicators: {},
        basePoolConfig: { include_st: false },
      }),
    },
    global: { plugins: [router] },
  })
  return wrapper
}

function messagesText(): string {
  return Array.from(document.body.querySelectorAll('.n-message'))
    .map((el) => el.textContent ?? '')
    .join(' | ')
}

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

describe('筛选结果逐只加入自选', () => {
  it('每行一个「+ 加入自选」；已加入的行显示✓并置灰', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      // 000001 已经在自选里（任一分组都算"已加入"）
      if (url === '/api/watchlist/list') {
        return Promise.resolve({ data: { items: [{ stock_code: '000001', group_name: '我的自选' }], count: 1 } })
      }
      return Promise.resolve({ data: {} })
    })
    const wrapper = mountPanel()
    await flushPromises()

    const addButtons = wrapper.findAll('.screening-watchlist-add')
    expect(addButtons).toHaveLength(1)
    expect(addButtons[0].text()).toContain('+ 加入自选')

    const added = wrapper.findAll('.screening-watchlist-added')
    expect(added).toHaveLength(1)
    expect(added[0].text()).toContain('已加入')
    // 置灰：naive-ui 的 disabled 会加 --disabled 类
    expect(added[0].attributes('disabled') !== undefined || added[0].classes().some((c) => c.includes('disabled'))).toBe(true)
  })

  it('点击逐只加入 → POST /add 不带 group，成功提示带股票名', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/watchlist/list') return Promise.resolve({ data: { items: [], count: 0 } })
      return Promise.resolve({ data: {} })
    })
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/watchlist/add') {
        return Promise.resolve({
          data: { status: 'ok', stock_code: '600519', group: '我的自选', created: true, default_group: '我的自选' },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.findAll('.screening-watchlist-add')[0].trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith('/api/watchlist/add', { stock_code: '600519' })
    expect(messagesText()).toContain('贵州茅台')
    expect(messagesText()).toContain('我的自选')
    // 点过的行立刻变成"已加入"，同一只股票不会被连点两次
    expect(wrapper.findAll('.screening-watchlist-added').length).toBe(1)
    expect(wrapper.findAll('.screening-watchlist-add').length).toBe(1)
  })

  it('加入失败（黑名单 400）→ 提示里带后端原因，不吞错误', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/watchlist/list') return Promise.resolve({ data: { items: [], count: 0 } })
      return Promise.resolve({ data: {} })
    })
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/watchlist/add') {
        return Promise.reject({
          isAxiosError: true,
          response: {
            status: 400,
            data: { detail: 'stock is blacklisted; remove it from the blacklist first' },
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.findAll('.screening-watchlist-add')[0].trigger('click')
    await flushPromises()

    const text = messagesText()
    expect(text).toContain('加入自选失败')
    expect(text).toContain('黑名单')
    // 失败的行仍然可重试（不置灰）
    expect(wrapper.findAll('.screening-watchlist-add').length).toBe(2)
  })
})
