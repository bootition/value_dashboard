import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import axios from 'axios'
import App from '../../src/App.vue'

vi.mock('axios', () => ({ default: { get: vi.fn() } }))

function mountApp(ready: boolean) {
  vi.mocked(axios.get).mockResolvedValue({
    data: {
      data_quality: {
        minimum_data_readiness: { ready },
        warning_codes: [],
      },
    },
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', name: 'screening', component: { template: '<div>筛选页</div>' } }],
  })
  return mount(App, { global: { plugins: [router] } })
}

function mountCheckingApp() {
  vi.mocked(axios.get).mockResolvedValue({
    data: {
      data_quality: {
        minimum_data_readiness: { ready: false, checking: true },
        warning_codes: [],
      },
    },
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', name: 'screening', component: { template: '<div>筛选页</div>' } }],
  })
  return mount(App, { global: { plugins: [router] } })
}

describe('App 全局数据状态', () => {
  it('读取 minimum_data_readiness.ready 显示数据就绪', async () => {
    const wrapper = mountApp(true)
    await flushPromises()
    expect(wrapper.find('.app-data-status').text()).toContain('数据就绪')
  })

  it('后台 readiness 尚未完成时显示正在核对数据', async () => {
    const wrapper = mountCheckingApp()
    await flushPromises()
    expect(wrapper.find('.app-data-status').text()).toContain('正在核对数据')
  })

  it('请求失败时显示失败而不是永久加载中', async () => {
    vi.mocked(axios.get).mockRejectedValue(new Error('offline'))
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', name: 'screening', component: { template: '<div>筛选页</div>' } }],
    })
    const wrapper = mount(App, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.find('.app-data-status').text()).toContain('状态读取失败')
  })
})
