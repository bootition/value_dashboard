import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import PointInTimeCard from '../../src/components/PointInTimeCard.vue'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
    isCancel: () => false,
  }
  // 命名导出必须与 default 一起提供：组件里 `import axios, { isAxiosError } from 'axios'`。
  return { default: mock, ...mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'

const AS_REPORTED = {
  stock_code: '600519',
  as_of: '2024-06-30',
  frequency: 'any',
  report_date: '2024-03-31',
  announce_date: '2024-04-26',
  report_type: 'quarterly',
  source: 'cninfo',
  days_since_announce: 65,
  fields: {
    revenue: 46_000_000_000,
    parent_net_profit: 24_000_000_000,
    total_assets: 270_000_000_000,
    total_equity_parent: 200_000_000_000,
    cf_from_operating: 12_000_000_000,
  },
  missing_fields: [],
  value_tables: ['income_statement', 'balance_sheet', 'cash_flow'],
  value_basis: 'latest_restated',
  value_source: 'financial_report_dates.announce_date 定位报告期 + 三表取数',
  disclaimer:
    '数值取自库内该报告期唯一一份记录（今天的最新重述值），不是当年披露时的原始数字；可见性只决定「取哪一期」。',
  limitations: ['无公布日记录的报告期不参与判定（不假设未登记即当天可见）'],
}

const DEFAULT_FIELDS = 'revenue,parent_net_profit,total_assets,total_equity_parent,cf_from_operating'

function mountCard(stockCode = '600519') {
  return mount(PointInTimeCard, { props: { stockCode } })
}

describe('PointInTimeCard 当时可见财务值（reports/119 §5 D 类）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(axios.get as Mock).mockReset()
  })

  it('默认按「1 个季度前」与五个默认字段请求，并渲染口径徽标、免责声明与财务值', async () => {
    const get = axios.get as Mock
    get.mockResolvedValue({ data: AS_REPORTED })

    const wrapper = mountCard()
    await flushPromises()

    expect(get).toHaveBeenCalledTimes(1)
    const [url, config] = get.mock.calls[0] as [string, { params: Record<string, string> }]
    expect(url).toBe('/api/research/as-reported')
    expect(config.params.code).toBe('600519')
    expect(config.params.fields).toBe(DEFAULT_FIELDS)
    expect(config.params.as_of).toMatch(/^\d{4}-\d{2}-\d{2}$/)

    // 默认观察日 ≈ 3 个自然月前（给时区/月末留 10 天容差）
    const daysAgo = (Date.now() - Date.parse(config.params.as_of)) / 86_400_000
    expect(daysAgo).toBeGreaterThan(80)
    expect(daysAgo).toBeLessThan(100)

    const text = wrapper.text()
    // 顶部口径徽标 + disclaimer
    expect(text).toContain('latest_restated')
    expect(text).toContain('不是当年披露时的原始数字')
    // 可见报告期与公布日
    expect(text).toContain('2024-03-31')
    expect(text).toContain('2024-04-26')
    expect(text).toContain('65 天')
    // 字段值与来源
    expect(text).toContain('460.00亿')
    expect(text).toContain('2700.00亿')
    expect(text).toContain('income_statement')
    expect(text).toContain('无公布日记录的报告期不参与判定')
  })

  it.each([
    ['no_announce_date_records', '没有任何公布日记录'],
    ['no_visible_period_before_as_of', '所选日期早于该股最早登记的公布日（2010-04-30）'],
    ['visible_period_has_no_financials', '利润表 / 资产负债表 / 现金流量表都没有该期数据'],
  ])('404 reason=%s 显示对应的中文提示', async (reason, expected) => {
    const get = axios.get as Mock
    get.mockRejectedValue({
      isAxiosError: true,
      response: {
        status: 404,
        data: {
          detail: {
            reason,
            message: 'backend message',
            earliest_announce_date: '2010-04-30',
            report_date: '2023-12-31',
            announce_date: '2024-04-26',
          },
        },
      },
    })

    const wrapper = mountCard()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain(expected)
    // 不应把后端英文 message 当作用户提示
    expect(text).not.toContain('backend message')
  })

  it('非 404 异常走 friendlyErrorMessage（网络不可达时给启动提示）', async () => {
    const get = axios.get as Mock
    get.mockRejectedValue({
      isAxiosError: true,
      code: 'ERR_NETWORK',
      response: undefined,
    })

    const wrapper = mountCard()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('无法连接服务器')
    expect(text).toContain('start.bat')
  })

  it('响应缺字段时降级为缺失值，不崩溃', async () => {
    const get = axios.get as Mock
    get.mockResolvedValue({
      data: {
        ...AS_REPORTED,
        fields: { revenue: 1_000_000 },
        missing_fields: ['parent_net_profit'],
      },
    })

    const wrapper = mountCard()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('100.00万')
    expect(text).toContain('缺失字段：归母净利润')
    expect(text).toContain('该期无此字段值')
  })
})
