import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import AnnouncementTimelineCard from '../../src/components/AnnouncementTimelineCard.vue'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
    isCancel: () => false,
  }
  return { default: mock, ...mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'

const PAGE_1 = {
  stock_code: '600519',
  days: 365,
  cutoff_date: '2025-11-15',
  timezone: 'Asia/Shanghai',
  limit: 20,
  offset: 0,
  count: 2,
  total: 3,
  has_more: true,
  by_category: { financial: 2, dividend: 1, other: 0 },
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
    {
      announcement_id: 'a2',
      stock_code: '600519',
      title: '2024年年度权益分派实施公告',
      category: 'dividend',
      announcement_time: '2025-06-20T02:00:00+00:00',
      announcement_date_cn: '2025-06-20',
      announcement_datetime_cn: '2025-06-20T10:00:00+08:00',
    },
  ],
  category_source: 'app.core.update.classify_announcement',
  disclaimer: '只包含已登记进 announcement_registry 的公告，不是全市场全量公告库。',
}

const PAGE_2 = {
  ...PAGE_1,
  offset: 2,
  count: 1,
  total: 3,
  has_more: false,
  items: [
    {
      announcement_id: 'a3',
      stock_code: '600519',
      title: '关于召开2025年第一次临时股东大会的通知',
      category: 'other',
      announcement_time: '2025-05-08T01:00:00+00:00',
      announcement_date_cn: '2025-05-08',
      announcement_datetime_cn: '2025-05-08T09:00:00+08:00',
    },
  ],
}

function mountCard(stockCode = '600519') {
  return mount(AnnouncementTimelineCard, { props: { stockCode } })
}

function mockPagedResponses() {
  const get = axios.get as Mock
  get.mockImplementation((_url: string, config?: { params?: Record<string, unknown> }) => {
    const offset = Number(config?.params?.offset ?? 0)
    return Promise.resolve({ data: offset === 0 ? PAGE_1 : PAGE_2 })
  })
  return get
}

describe('AnnouncementTimelineCard 公告时间线（reports/119 §5 D 类）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(axios.get as Mock).mockReset()
  })

  it('渲染北京时间日期与中文类别标签，并在 has_more 时显示「加载更多」', async () => {
    const get = mockPagedResponses()

    const wrapper = mountCard()
    await flushPromises()

    expect(get).toHaveBeenCalledTimes(1)
    const [url, config] = get.mock.calls[0] as [string, { params: Record<string, unknown> }]
    expect(url).toBe('/api/research/announcements')
    expect(config.params).toMatchObject({ code: '600519', days: 365, limit: 20, offset: 0 })

    const text = wrapper.text()
    expect(text).toContain('2025-10-30')
    expect(text).toContain('定期报告/业绩预告')
    expect(text).toContain('2025-06-20')
    expect(text).toContain('分红')
    expect(text).toContain('2025年第三季度报告')
    expect(text).toContain('共 3 条')
    expect(wrapper.findAll('.timeline-item')).toHaveLength(2)
    expect(wrapper.find('.timeline-more').exists()).toBe(true)
    // 免责声明如实展示
    expect(text).toContain('不是全市场全量公告库')
  })

  it('点击「加载更多」按已加载条数作为 offset 追加下一页，has_more=false 后按钮消失', async () => {
    const get = mockPagedResponses()

    const wrapper = mountCard()
    await flushPromises()

    await wrapper.find('.timeline-more').trigger('click')
    await flushPromises()

    expect(get).toHaveBeenCalledTimes(2)
    const [, secondConfig] = get.mock.calls[1] as [string, { params: Record<string, unknown> }]
    expect(secondConfig.params).toMatchObject({ offset: 2, limit: 20, days: 365 })

    const text = wrapper.text()
    expect(wrapper.findAll('.timeline-item')).toHaveLength(3)
    expect(text).toContain('2025-05-08')
    expect(text).toContain('其他')
    expect(text).toContain('关于召开2025年第一次临时股东大会的通知')
    expect(wrapper.find('.timeline-more').exists()).toBe(false)
  })

  it('异常走 friendlyErrorMessage（网络不可达时给启动提示）', async () => {
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
    expect(wrapper.find('.timeline-item').exists()).toBe(false)
  })

  it('空时间窗显示空态且不显示「加载更多」', async () => {
    const get = axios.get as Mock
    get.mockResolvedValue({
      data: {
        ...PAGE_1,
        count: 0,
        total: 0,
        has_more: false,
        by_category: { financial: 0, dividend: 0, other: 0 },
        items: [],
      },
    })

    const wrapper = mountCard()
    await flushPromises()

    expect(wrapper.text()).toContain('时间窗内暂无已登记公告')
    expect(wrapper.find('.timeline-more').exists()).toBe(false)
  })
})
