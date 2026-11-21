import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import axios from 'axios'
import type { Mock } from 'vitest'
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

/**
 * 用户原话："点到数据详情界面后，没有进度条，根本看不到正在做什么。"
 * 进度条挂在 App.vue 布局层：只要自动更新在跑，**任何页面**都能看到，
 * 且显示的是人话阶段名 + 已完成/总数（不是侧边栏那句干巴巴的"更新中"）。
 */
function mountAppWithAutoUpdate(autoUpdate: unknown) {
  const get = axios.get as unknown as Mock
  get.mockImplementation((url: string) => {
    if (url === '/api/data-status/summary') {
      return Promise.resolve({
        data: {
          data_quality: {
            minimum_data_readiness: { ready: true },
            warning_codes: [],
          },
        },
      })
    }
    if (url === '/api/data-status/auto-update') return Promise.resolve({ data: autoUpdate })
    return Promise.resolve({ data: {} })
  })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', name: 'screening', component: { template: '<div>筛选页内容</div>' } }],
  })
  return mount(App, { global: { plugins: [router] } })
}

describe('App 全局更新进度条（任何页面可见）', () => {
  it('更新中：布局层出现进度条，显示中文阶段 + 已完成/总数', async () => {
    const wrapper = mountAppWithAutoUpdate({
      state: 'enabled',
      enabled: true,
      paused: false,
      current_stage: 'running',
      progress: {
        phase: 'step:indicators',
        job_id: 'job-1',
        started_at: new Date(Date.now() - 65000).toISOString(),
        live: { step: 'indicators', done: 2500, total: 5575, current: '000672' },
      },
      last_error: null,
      latest_job: { id: 256, status: 'running' },
    })
    await flushPromises()

    // 页面内容照常渲染
    expect(wrapper.text()).toContain('筛选页内容')
    // 2026-11-21 用户要求：**去掉所有页面共用的悬浮进度条**，
    // 进度只在「数据中心」页里看。这里钉住"布局层不再出现全局进度条"，
    // 防止以后有人又把它挂回 App.vue。
    expect(wrapper.find('.auto-update-progress').exists()).toBe(false)
    // 侧边徽标仍同步显示更新中（这是用户要保留的）
    expect(wrapper.find('.app-data-status').text()).toContain('更新中')
    wrapper.unmount()
  })

  it('空闲：布局层不显示进度条（与更新中一致，布局层永远没有全局进度条）', async () => {
    const wrapper = mountAppWithAutoUpdate({
      state: 'enabled',
      current_stage: 'finished',
      progress: { phase: 'done', status: 'success' },
      last_error: null,
      latest_job: { id: 256, status: 'success' },
    })
    await flushPromises()
    expect(wrapper.find('.auto-update-progress').exists()).toBe(false)
    expect(wrapper.find('.app-data-status').text()).toContain('数据就绪')
    wrapper.unmount()
  })
})
