/**
 * 自动更新状态（`/api/data-status/auto-update`）的轮询与"更新中"标记。
 *
 * 2026-11-22 重构：从 `views/ScreeningPage.vue` 原样抽出，**行为逐字等价**。
 *
 * 为什么不只在挂载时读一次（reports/42 P3）：更新窗口是分钟级的，
 * 运行按钮与"快照口径"提示要随状态自动切换，所以每 10s（更新中）
 * / 60s（空闲）轮询一次；单飞 guard 避免上一次查询未返回时堆积请求。
 */

import { ref } from 'vue'
import type { Ref } from 'vue'
import axios from 'axios'
import { usePollTimer } from './usePolling.ts'

export interface AutoUpdateRunningState {
  autoUpdateRunning: Ref<boolean>
  /** 立即查一次（单飞：在飞时直接返回）。 */
  refreshAutoUpdate: () => Promise<void>
  /** 启动轮询链：initialDelayMs 后第一次查，之后按更新中/空闲选间隔。 */
  startAutoUpdatePolling: (initialDelayMs?: number) => void
}

export function useAutoUpdateRunning(): AutoUpdateRunningState {
  const autoUpdateRunning = ref(false)
  const poll = usePollTimer()
  // 单飞 guard：避免上一次查询未返回时堆积请求
  let autoUpdateInFlight = false

  async function refreshAutoUpdate(): Promise<void> {
    if (autoUpdateInFlight) return
    autoUpdateInFlight = true
    try {
      const au = await axios.get<{ state: string; current_stage: string }>(
        '/api/data-status/auto-update',
      )
      autoUpdateRunning.value = au.data.state === 'enabled' && au.data.current_stage === 'running'
    } catch {
      autoUpdateRunning.value = false
    } finally {
      autoUpdateInFlight = false
    }
  }

  function startAutoUpdatePolling(initialDelayMs = 10000) {
    poll.schedule(initialDelayMs, function tick() {
      void refreshAutoUpdate()
      poll.schedule(autoUpdateRunning.value ? 10000 : 60000, tick)
    })
  }

  return { autoUpdateRunning, refreshAutoUpdate, startAutoUpdatePolling }
}
