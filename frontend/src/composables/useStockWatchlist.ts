/**
 * 详情页「加入自选」按钮的三态状态机（2026-11-26 用户需求）。
 *
 * 用户原话：「有没有办法做到：在详情界面有个『加入自选』……那种『点击加入自选』
 * 应该是加入默认自选列表，然后还可以自己移动到自己的分组里。」
 *
 * 三态：
 * 1. **未加入** → 按钮「加入自选」，点击后 `POST /api/watchlist/add`（不带 group，
 *    落默认分组「我的自选」），提示 `已加入「我的自选」`；
 * 2. **已加入** → 按钮变「已在自选」（禁用态语义），旁边列出所在分组（可能多个），
 *    并提供「移出全部自选」；
 * 3. **判定失败**（网络/500）→ 退回「加入自选」但**不谎报状态**：`statusError`
 *    有值，界面提示"自选状态未知"，点击加入仍按后端幂等返回 `created=false` 兜底。
 *
 * 初始状态只来自 `GET /api/watchlist/status`（服务端事实），不在前端猜：
 * 本地缓存/列表页残留都可能在另一个标签页里被改过。
 *
 * 本模块刻意不依赖 naive-ui 类型（与 `useStockDetailLoad.ts` 同惯例）：
 * 只收一个 `notifier`，便于组件测试注入假 message。
 */

import { computed, ref } from 'vue'
import type { ComputedRef, Ref } from 'vue'
import axios from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'

/** `GET /api/watchlist/status` 的响应契约。 */
export interface WatchlistStatus {
  stock_code: string
  in_watchlist: boolean
  groups: string[]
  default_group: string
  blacklisted?: boolean
}

/** `POST /api/watchlist/add` 的响应契约（`created=false` = 已经在了，幂等）。 */
interface AddResponse {
  status: string
  stock_code: string
  group: string
  created: boolean
  default_group?: string
}

/** 只需要 success/error 两个方法，避免把 naive-ui 的类型带进 composable。 */
export interface WatchlistNotifier {
  success(content: string): void
  error(content: string): void
}

/** 后端还没返回 `default_group` 时的兜底（与后端 `DEFAULT_GROUP_NAME` 同值）。 */
const FALLBACK_DEFAULT_GROUP = '我的自选'

export interface UseStockWatchlistOptions {
  stockCode: ComputedRef<string>
  notifier: WatchlistNotifier
}

export function useStockWatchlist(options: UseStockWatchlistOptions) {
  const { stockCode, notifier } = options

  const status = ref<WatchlistStatus | null>(null)
  const busy = ref(false)
  /** 读状态失败的原因（只用于提示"状态未知"，不阻断加入操作）。 */
  const statusError = ref<string | null>(null)
  /** 代际号：切股票后旧响应不许写进新页面（与 `useStockDetailLoad` 同一手法）。 */
  let generation = 0

  const groups = computed<string[]>(() => status.value?.groups ?? [])
  const inWatchlist = computed<boolean>(() => status.value?.in_watchlist === true)
  const defaultGroup = computed<string>(() => status.value?.default_group || FALLBACK_DEFAULT_GROUP)
  const blacklisted = computed<boolean>(() => status.value?.blacklisted === true)
  /** 按钮文案：三态的唯一出口（模板里不许再写第二份条件）。 */
  const buttonLabel = computed<string>(() => (inWatchlist.value ? '已在自选' : '加入自选'))
  /** 已加入时展示所在分组：「我的自选」「重点观察」 */
  const groupsLabel = computed<string>(() => groups.value.map((name) => `「${name}」`).join(''))

  async function refresh(): Promise<void> {
    const code = stockCode.value
    if (!code) return
    const current = ++generation
    try {
      const resp = await axios.get<WatchlistStatus>('/api/watchlist/status', {
        params: { stock_code: code },
      })
      if (current !== generation) return
      status.value = resp.data
      statusError.value = null
    } catch (error: unknown) {
      if (current !== generation) return
      status.value = null
      statusError.value = friendlyErrorMessage(error, '自选状态暂时读取失败')
    }
  }

  /** 「加入自选」：进默认分组，成功/已在都给出明确反馈。 */
  async function addToWatchlist(): Promise<boolean> {
    const code = stockCode.value
    if (!code || busy.value) return false
    busy.value = true
    try {
      const resp = await axios.post<AddResponse>('/api/watchlist/add', { stock_code: code })
      const group = resp.data?.group || defaultGroup.value
      // 幂等：`created=false` 说明本来就在这个分组里，不是失败。
      notifier.success(
        resp.data?.created === false ? `${code} 已经在「${group}」里了` : `已加入「${group}」`,
      )
      await refresh()
      return true
    } catch (error: unknown) {
      // 后端 400（不在股票池 / 黑名单）必须说清原因，不能只闪一句"失败"。
      notifier.error(friendlyErrorMessage(error, '加入自选失败'))
      return false
    } finally {
      busy.value = false
    }
  }

  /** 「移出全部自选」：`DELETE /remove` 不带 group = 从所有分组移除。 */
  async function removeFromWatchlist(): Promise<boolean> {
    const code = stockCode.value
    if (!code || busy.value) return false
    busy.value = true
    try {
      await axios.delete('/api/watchlist/remove', { data: { stock_code: code } })
      notifier.success(`已把 ${code} 移出自选`)
      await refresh()
      return true
    } catch (error: unknown) {
      notifier.error(friendlyErrorMessage(error, '移出自选失败'))
      return false
    } finally {
      busy.value = false
    }
  }

  return {
    status,
    busy,
    statusError,
    groups,
    groupsLabel,
    inWatchlist,
    blacklisted,
    defaultGroup,
    buttonLabel,
    refresh,
    addToWatchlist,
    removeFromWatchlist,
  }
}

/** 测试与页面共用的只读常量（避免前端各处再抄一遍中文字面量）。 */
export const DEFAULT_WATCHLIST_GROUP = FALLBACK_DEFAULT_GROUP

export type WatchlistStatusRef = Ref<WatchlistStatus | null>
