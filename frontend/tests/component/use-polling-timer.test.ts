/**
 * 轮询定时器生命周期契约（2026-11-22 反返工护栏）。
 *
 * 守护对象：`src/composables/usePolling.ts`（把 `DataStatusPage` 的 3 套轮询 +
 * `ScreeningPage` 的自动更新轮询收敛成一个 timer 实现）。
 *
 * 为什么必须钉死：收敛前有 4 份复制粘贴的 `let timer` + `clearTimeout` +
 * `onBeforeUnmount` 清理。这类代码的 bug 只有两个，而两个都是静默的：
 * - **漏清**：同一个 schedule 被调用两次（如状态从"运行中"切到"空闲"），
 *   旧 timer 没清掉 → 两条轮询链并行，请求量翻倍、状态互相打架。
 * - **漏卸载**：组件已卸载但 timer 还在跑 → after-unmount 请求 + 内存泄漏。
 * 所以这里直接断言"只跑最后一个""卸载后一个都不跑"。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { effectScope } from 'vue'
import { usePollTimer } from '../../src/composables/usePolling.ts'

describe('usePollTimer 生命周期', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('同一 poll 重复 schedule 只保留最后一次（不并行两条链）', () => {
    const scope = effectScope()
    const poll = scope.run(() => usePollTimer())!
    const first = vi.fn()
    const second = vi.fn()

    poll.schedule(4000, first)
    poll.schedule(60000, second)
    expect(poll.isScheduled()).toBe(true)

    vi.advanceTimersByTime(4000)
    expect(first).not.toHaveBeenCalled()
    vi.advanceTimersByTime(56000)
    expect(second).toHaveBeenCalledTimes(1)
    expect(first).not.toHaveBeenCalled()
    expect(poll.isScheduled()).toBe(false)
    scope.stop()
  })

  it('cancel() 清掉排定的任务', () => {
    const scope = effectScope()
    const poll = scope.run(() => usePollTimer())!
    const run = vi.fn()
    poll.schedule(1000, run)
    poll.cancel()
    expect(poll.isScheduled()).toBe(false)
    vi.advanceTimersByTime(5000)
    expect(run).not.toHaveBeenCalled()
    scope.stop()
  })

  it('effect scope 停止（组件卸载）后 timer 不再触发', () => {
    const scope = effectScope()
    const poll = scope.run(() => usePollTimer())!
    const run = vi.fn()
    poll.schedule(1000, run)
    scope.stop()
    vi.advanceTimersByTime(600000)
    expect(run).not.toHaveBeenCalled()
    expect(poll.isScheduled()).toBe(false)
  })

  it('回调里自我重排（重量摘要的递归链）能持续触发且始终只有一个 timer', () => {
    const scope = effectScope()
    const poll = scope.run(() => usePollTimer())!
    const ticks: number[] = []
    const tick = () => {
      ticks.push(ticks.length)
      if (ticks.length < 3) poll.schedule(15000, tick)
    }
    poll.schedule(15000, tick)

    vi.advanceTimersByTime(15000)
    expect(ticks).toHaveLength(1)
    expect(poll.isScheduled()).toBe(true)
    vi.advanceTimersByTime(15000)
    vi.advanceTimersByTime(15000)
    expect(ticks).toHaveLength(3)
    expect(poll.isScheduled()).toBe(false)
    vi.advanceTimersByTime(300000)
    expect(ticks).toHaveLength(3)
    scope.stop()
  })

  it('每次 schedule 都返回新的单飞句柄：两个 poll 之间互不影响', () => {
    const scope = effectScope()
    const a = scope.run(() => usePollTimer())!
    const b = scope.run(() => usePollTimer())!
    const runA = vi.fn()
    const runB = vi.fn()
    a.schedule(4000, runA)
    b.schedule(4000, runB)
    a.cancel()
    vi.advanceTimersByTime(4000)
    expect(runA).not.toHaveBeenCalled()
    expect(runB).toHaveBeenCalledTimes(1)
    scope.stop()
  })

  it('无 active scope 时也能用（不会因缺 scope 抛错）', () => {
    const poll = usePollTimer()
    const run = vi.fn()
    poll.schedule(100, run)
    vi.advanceTimersByTime(100)
    expect(run).toHaveBeenCalledTimes(1)
    poll.cancel()
  })
})
