/**
 * 自动更新状态（`/api/data-status/auto-update`）的轮询与"更新中"标记。
 *
 * 2026-11-22 重构：从 `views/ScreeningPage.vue` 原样抽出，**行为逐字等价**。
 * 2026-11-24：为"全局更新进度条"补出完整状态快照（`autoUpdateStatus`），
 * 并把"更新中"判定收敛到 `helpers/auto-update-progress.ts::isAutoUpdateRunning`
 * （与数据状态页同一口径：控制器 `current_stage` 或 `latest_job.status` 任一为
 * running）。**仍然只有这一个轮询器**：App.vue 复用本 composable 的实例，
 * 详情页通过 `AUTO_UPDATE_STATUS_KEY` 注入读取，不自己再发请求。
 *
 * 为什么不只在挂载时读一次（reports/42 P3）：更新窗口是分钟级的，
 * 运行按钮与"快照口径"提示要随状态自动切换，所以每 10s（更新中）
 * / 60s（空闲）轮询一次；单飞 guard 避免上一次查询未返回时堆积请求。
 */

import { ref } from 'vue'
import type { InjectionKey, Ref } from 'vue'
import axios from 'axios'
import { usePollTimer } from './usePolling.ts'
import { isAutoUpdateRunning } from '../helpers/auto-update-progress.ts'
import type { AutoUpdateStatusPayload } from '../helpers/auto-update-progress.ts'

/** App.vue 在布局层 provide，个股详情页等注入同一个状态（共享轮询结果）。 */
export const AUTO_UPDATE_STATUS_KEY: InjectionKey<Ref<AutoUpdateStatusPayload | null>> =
  Symbol('vd:auto-update-status')

export interface AutoUpdateRunningState {
  autoUpdateRunning: Ref<boolean>
  /** 最近一次成功读取的完整状态；全局进度条 / 详情页提示据此渲染。 */
  autoUpdateStatus: Ref<AutoUpdateStatusPayload | null>
  /** 立即查一次（单飞：在飞时直接返回）。 */
  refreshAutoUpdate: () => Promise<void>
  /** 启动轮询链：initialDelayMs 后第一次查，之后按更新中/空闲选间隔。 */
  startAutoUpdatePolling: (initialDelayMs?: number) => void
}

export function useAutoUpdateRunning(): AutoUpdateRunningState {
  const autoUpdateRunning = ref(false)
  const autoUpdateStatus = ref<AutoUpdateStatusPayload | null>(null)
  const poll = usePollTimer()
  // 单飞 guard：避免上一次查询未返回时堆积请求
  let autoUpdateInFlight = false

  async function refreshAutoUpdate(): Promise<void> {
    if (autoUpdateInFlight) return
    autoUpdateInFlight = true
    try {
      const au = await axios.get<AutoUpdateStatusPayload>(
        '/api/data-status/auto-update',
        { timeout: 8000 },
      )
      autoUpdateStatus.value = au.data
      autoUpdateRunning.value = isAutoUpdateRunning(au.data)
    } catch {
      // 读失败不得把"正在更新"永久挂在界面上；下一次轮询会纠正。
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

  return { autoUpdateRunning, autoUpdateStatus, refreshAutoUpdate, startAutoUpdatePolling }
}
