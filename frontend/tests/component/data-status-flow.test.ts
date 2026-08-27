import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider } from 'naive-ui'
import { h } from 'vue'
import DataStatusPage from '../../src/views/DataStatusPage.vue'

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

describe('DataStatusPage 数据状态展示（PRD §15）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/data-status/summary') {
        return Promise.resolve({
          data: {
            stock_count: 5533,
            price_raw_count: 5525,
            price_qfq_count: 5525,
            balance_sheet_count: 5533,
            income_statement_count: 5533,
            cash_flow_count: 5533,
            indicator_snapshot_count: 5533,
            csrc_industry_count: 5533,
            retry_count: 3,
            missing_count: 5,
            last_update: '2026-08-01T09:00:00+00:00',
            recent_jobs: [{ finished_at: '2026-08-01T09:00:00+00:00', job_type: 'incremental_update', status: 'success' }],
            pdf_tasks: { cnt: 0, pending: 0 },
            backup: { cnt: 3, latest: '2026-08-01', full_count: 3 },
            dividends: { total_rows: 1000, stocks: 2000, earliest: '2020-01-01', latest: '2026-07-15' },
            xdxr: { total_rows: 900, stocks: 1800, earliest: '2020-01-01', latest: '2026-07-20' },
            share_capital: { latest_updated: '2026-08-01T08:00:00+00:00', with_shares: 5533, with_circ_shares: 5533 },
            listing_info: { stock_list_refreshed_at: '2026-08-01T08:00:00+00:00', listing_info_refreshed_at: '2026-08-01T08:05:00+00:00' },
            csrc_industry_refresh: { last_refresh: '2026-07-01T00:00:00+00:00' },
            data_quality: {
              warning_codes: ['STALE_RUNNING_JOBS'],
              minimum_data_readiness: {
                ready: true, stock_count: 5533, missing: {}, missing_counts: {},
                schema_compatibility: { compatible: true, missing: [] },
              },
              dates: {
                price: '2026-07-31',
                balance_sheet: { latest_record: '2026-06-30', latest_complete: '2026-06-30' },
                income_statement: { latest_record: '2026-06-30', latest_complete: '2026-06-30' },
                cash_flow: { latest_record: '2026-06-30', latest_complete: '2026-06-30' },
                indicator_snapshot: { latest_complete: '2026-06-30', calculated_at: '2026-08-01T08:00:00', latest_price_date: '2026-07-31' },
              },
            },
          },
        })
      }
      if (url === '/api/data-status/retry-list') {
        return Promise.resolve({ data: { count: 3, items: [{ stock_code: '000001', data_type: 'price_daily', adapter: 'tencent', error: 'timeout', retry_count: 2 }] } })
      }
      if (url === '/api/data-status/missing-list') {
        return Promise.resolve({ data: { count: 1, items: [{ stock_code: '000002', field_name: 'risk_coverage_ratio', reason_code: 'source_incomplete' }] } })
      }
      if (url === '/api/data-status/auto-update') {
        return Promise.resolve({
          data: {
            state: 'finished', enabled: true, paused: false, current_stage: 'finished',
            progress: { phase: 'done', job_id: 'job-1', steps: { universe: 'success', prices: 'success' } },
            last_error: null, last_success_at: '2026-08-01T09:00:00+00:00',
          },
        })
      }
      return Promise.resolve({ data: {} })
    })
  })

  it('渲染覆盖统计、警告、自动更新与各数据域最新日期', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [] })
    const wrapper = mount(NMessageProvider, {
      slots: { default: () => h(DataStatusPage) },
      global: { plugins: [router] },
    })
    await flushPromises()

    const text = wrapper.text()
    // 覆盖统计
    expect(text).toContain('5533')
    expect(text).toContain('STALE_RUNNING_JOBS')
    // 自动更新卡片
    expect(text).toContain('已完成')
    expect(text).toContain('job-1')
    // 各数据域最新日期（PRD §6.4/§15）
    expect(text).toContain('2026-07-31')
    expect(text).toContain('2026-07-20')
    expect(text).toContain('2026-08-01T08:00:00+00:00')
    expect(text).toContain('2026-07-01T00:00:00+00:00')
    // 最近一次更新执行来自 job_logs（partial/failed 也会刷新该时间）；时间为本地化显示
    expect(text).toContain('最近一次更新执行: 2026/8/1 17:00:00')
    expect(text).toContain('实际价格日期: 2026-07-31')
  })

  it('数据质量日期不可用时显示占位符而非崩溃', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/data-status/summary') {
        return Promise.resolve({
          data: {
            stock_count: 0,
            price_raw_count: 0,
            price_qfq_count: 0,
            balance_sheet_count: 0,
            income_statement_count: 0,
            cash_flow_count: 0,
            indicator_snapshot_count: 0,
            csrc_industry_count: 0,
            retry_count: 0,
            missing_count: 0,
            last_update: null,
            data_quality: {
              warning_codes: [],
              minimum_data_readiness: {
                ready: false, stock_count: 0, missing: {}, missing_counts: {},
                schema_compatibility: { compatible: true, missing: [] },
              },
              dates: {
                price: null,
                balance_sheet: { latest_record: null, latest_complete: null },
                income_statement: { latest_record: null, latest_complete: null },
                cash_flow: { latest_record: null, latest_complete: null },
                indicator_snapshot: { latest_complete: null, calculated_at: null, latest_price_date: null },
              },
            },
          },
        })
      }
      return Promise.resolve({ data: { count: 0, items: [] } })
    })
    const router = createRouter({ history: createMemoryHistory(), routes: [] })
    const wrapper = mount(NMessageProvider, {
      slots: { default: () => h(DataStatusPage) },
      global: { plugins: [router] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('尚未初始化')
    expect(wrapper.text()).toContain('—')
  })
})
