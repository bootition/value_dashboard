/**
 * 轮询定时器的唯一生命周期实现（2026-11-22 重构）。
 *
 * 背景：`views/DataStatusPage.vue` 里曾有 **3 套**彼此复制粘贴的轮询调度
 * （`schedulePolling` / `scheduleRecomputePolling` / `scheduleHeavyPolling`），
 * 各自维护一个 `let timer` + `clearTimeout` + 在 `onBeforeUnmount` 里再清一次；
 * `views/ScreeningPage.vue` 的自动更新轮询是第 4 套。
 * 复制粘贴的代价是"清理漏一处就内存泄漏/after-unmount 请求"，而这类 bug
 * 只在上线后被 `setTimeout` 打到脸上。
 *
 * 本模块只收敛**定时器生命周期**，不碰各调用点的重排语义（那是业务逻辑，
 * 4 处各不相同：有的等请求回来再排下一次，有的立即排下一次）。调用点仍自己
 * 决定"何时排下一次、排多久"，但"清旧 + 设新 + 卸载时清零"只有这一份实现。
 *
 * 等价性：`schedule()` 先清旧 timer 再设新 timer，与原先
 * `if (t) { clearTimeout(t); t = undefined }; t = setTimeout(...)` 逐字等价；
 * `onScopeDispose` 的时机（组件卸载时 scope.stop）不晚于原先的 `onBeforeUnmount`。
 */

import { getCurrentScope, onScopeDispose } from 'vue'

export interface PollTimer {
  /** 清掉已排定的 timer（若有），delayMs 后执行一次 run。 */
  schedule(delayMs: number, run: () => void): void
  /** 只清，不排。 */
  cancel(): void
  /** 当前是否有排定未触发的 timer。 */
  isScheduled(): boolean
}

export function usePollTimer(): PollTimer {
  let timer: ReturnType<typeof setTimeout> | undefined

  function cancel() {
    if (timer !== undefined) {
      clearTimeout(timer)
      timer = undefined
    }
  }

  function schedule(delayMs: number, run: () => void) {
    cancel()
    timer = setTimeout(() => {
      timer = undefined
      run()
    }, delayMs)
  }

  // 组件卸载（effect scope 停止）时必然清干净：调用点不必再自己写 onBeforeUnmount。
  if (getCurrentScope()) onScopeDispose(cancel)

  return {
    schedule,
    cancel,
    isScheduled: () => timer !== undefined,
  }
}
