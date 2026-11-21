/**
 * 全局更新进度条组件（App.vue 布局层）。
 *
 * 用户原话："侧边菜单显示更新中，但点了数据详情后没有进度条，根本看不到正在做什么。"
 * 本文件证明用户**真的看得到**：更新中显示中文阶段 + 已完成/总数 + 百分比；
 * 空闲不显示；失败显示 last_error；未知阶段显示兜底中文。
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import AutoUpdateProgressBar from '../../src/components/AutoUpdateProgressBar.vue'

const NOW = Date.parse('2026-11-21T12:19:56.000Z')

function runningStatus() {
  return {
    state: 'enabled',
    enabled: true,
    paused: false,
    current_stage: 'running',
    progress: {
      phase: 'step:indicators',
      job_id: 'job-1',
      started_at: '2026-11-21T12:16:56.000Z',
      live: {
        step: 'indicators',
        label: '锟斤拷', // 后端实测出现乱码 label，组件必须以 step 映射为准
        done: 2500,
        total: 5575,
        current: '000672',
        eta_seconds: 848,
      },
    },
    last_error: null,
    latest_job: { id: 256, status: 'running' },
  }
}

function mountBar(status: unknown) {
  return mount(AutoUpdateProgressBar, {
    props: { status: status as never, now: NOW },
  })
}

afterEach(() => {
  vi.useRealTimers()
})

describe('全局更新进度条', () => {
  it('更新中：进度条可见，显示中文阶段 + 已完成/总数 + 百分比 + 已耗时', () => {
    const wrapper = mountBar(runningStatus())
    const bar = wrapper.find('.auto-update-progress')
    expect(bar.exists()).toBe(true)
    expect(bar.attributes('data-state')).toBe('running')
    const text = bar.text()
    expect(text).toContain('正在重算指标快照')
    expect(text).not.toContain('indicators')
    expect(text).toContain('2500 / 5575')
    expect(text).toContain('45%')
    expect(text).toContain('已耗时 3 分钟')
    expect(wrapper.find('.auto-update-fill').attributes('style')).toContain('width: 45%')
  })

  it('更新中但还没有细粒度计数时显示不确定进度（不是空白）', () => {
    const status = runningStatus()
    status.progress.phase = 'step:prices'
    // @ts-expect-error 故意去掉 live，模拟步骤刚开始抓取
    delete status.progress.live
    const wrapper = mountBar(status)
    const bar = wrapper.find('.auto-update-progress')
    expect(bar.exists()).toBe(true)
    expect(bar.text()).toContain('正在更新股票价格')
    expect(wrapper.find('.auto-update-track').classes()).toContain('is-indeterminate')
  })

  it('空闲/已完成：不显示任何进度条', () => {
    const wrapper = mountBar({
      state: 'enabled',
      current_stage: 'finished',
      progress: { phase: 'done', status: 'success', steps: { prices: 'success' } },
      last_error: null,
      latest_job: { id: 256, status: 'success' },
    })
    expect(wrapper.find('.auto-update-progress').exists()).toBe(false)
  })

  it('失败：显示 last_error，不静默', () => {
    const wrapper = mountBar({
      state: 'enabled',
      current_stage: 'failed',
      progress: { phase: 'done', status: 'failed' },
      last_error: '镜像源连接超时：tencent quotes HTTP 502',
      latest_job: { id: 257, status: 'failed' },
    })
    const bar = wrapper.find('.auto-update-progress')
    expect(bar.exists()).toBe(true)
    expect(bar.attributes('data-state')).toBe('failed')
    expect(bar.text()).toContain('数据更新失败')
    expect(bar.text()).toContain('镜像源连接超时')
    expect(wrapper.find('.auto-update-error').exists()).toBe(true)
  })

  it('失败提示可手动关闭（避免长期占屏）', async () => {
    const wrapper = mountBar({
      current_stage: 'failed',
      last_error: '磁盘空间不足',
    })
    expect(wrapper.find('.auto-update-progress').exists()).toBe(true)
    await wrapper.find('.auto-update-dismiss').trigger('click')
    expect(wrapper.find('.auto-update-progress').exists()).toBe(false)
  })

  it('未知阶段：显示兜底中文，不显示原始英文 key', () => {
    const status = runningStatus()
    ;(status.progress.live as { step: string }).step = 'mystery_step'
    const wrapper = mountBar(status)
    const text = wrapper.find('.auto-update-progress').text()
    expect(text).toContain('正在更新数据')
    expect(text).not.toContain('mystery_step')
  })

  it('更新刚结束：显示"数据更新完成"，30 秒后自动消失', async () => {
    vi.useFakeTimers()
    const wrapper = mountBar(runningStatus())
    expect(wrapper.find('.auto-update-progress').attributes('data-state')).toBe('running')

    await wrapper.setProps({
      status: {
        state: 'enabled',
        current_stage: 'finished',
        progress: { phase: 'done', status: 'success', steps: { prices: 'success' } },
        last_error: null,
        latest_job: { id: 256, status: 'success' },
      } as never,
    })
    await nextTick()
    expect(wrapper.find('.auto-update-progress').attributes('data-state')).toBe('completed')
    expect(wrapper.text()).toContain('数据更新完成')

    vi.advanceTimersByTime(30000)
    await nextTick()
    expect(wrapper.find('.auto-update-progress').exists()).toBe(false)
  })
})
