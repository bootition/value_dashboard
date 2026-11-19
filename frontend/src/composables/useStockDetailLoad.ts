/**
 * 个股详情页的「一次进入打几路、每路怎么降级」状态机（2026-11-23 重构）。
 *
 * 从 `views/StockDetailPage.vue` **原样抽出**，行为逐字等价：`fetchAll` /
 * `isBlacklisted` / `fetchStockInfo` / `fetchIndicators` / `fetchKline` /
 * `fetchBusinessOverview` / `fetchWarningCodes` / `addToBlacklist` /
 * `removeFromBlacklist` 的函数体只做了缩进，语句、文案、URL、请求参数一字未改。
 *
 * 抽出的理由：这 5 路并发与 K 线周期、TOC 跟随三者交织在同一个 setup 作用域，
 * 谁都不敢动。收敛到这里之后，组件只负责「把 ref 绑到模板」。
 *
 * 被钉住的不变量（`tests/component/stock-detail-load.test.ts`）：
 * 1. **代际号 `generation`**：每次 fetchAll 自增，回调落地前比对，防止切股票后
 *    旧响应写进新页面；`isBlacklisted` 命中代际不符时返回 true（放弃本轮）。
 * 2. **先问黑名单，命中就不打 4 个必然 404 的详情请求**（省 4 条假错误日志）。
 * 3. **黑名单判定失败要退回正常加载**，不能把页面判成不可用（单股端点自己也有守卫）。
 * 4. 逐路降级：`/info` 404 区分「股票不存在（终态）」与「被屏蔽（可撤销）」；
 *    `/indicators` 与 `/business-overview` 的 404 升级为页面级不可用；
 *    `/kline` 失败只清空 K 线；`/data-status/summary` 失败回退 `LINEAGE_INVALID`。
 * 5. **K 线每次请求新建 AbortController 并 abort 上一路**，卸载时也要 abort
 *    （2026-08-14 红队 P3：离开页面后响应落地写已卸载组件）。
 * 6. 周期/复权变化：先写 localStorage，再用**当前代际号**重拉 K 线。
 */

import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { ComputedRef, Ref } from 'vue'
import axios, { isAxiosError } from 'axios'
import { isIndicatorUntrusted } from '../types/data-quality.ts'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import { isBlacklistedDetail } from '../types/blacklist.ts'
import type { BlacklistCheckResponse } from '../types/blacklist.ts'
import { saveKlineSettings, pageStorage } from '../utils/kline-settings.ts'
import type {
  StockInfo,
  IndicatorsResponse,
  KlineResponse,
  KlinePeriod,
  BusinessOverviewResponse,
} from '../types/stock-detail.ts'
import type { WarningCode } from '../types/data-quality.ts'

/** 只需要这三个方法，避免把 naive-ui 的类型带进 composable。 */
export interface StockDetailNotifier {
  warning(content: string): void
  success(content: string): void
  error(content: string): void
}

/** `useDialog().warning` 用到的那几个字段。 */
export interface StockDetailDialogOptions {
  title: string
  content: string
  positiveText: string
  negativeText: string
  onPositiveClick: () => Promise<void> | void
}

export interface StockDetailDialog {
  warning(options: StockDetailDialogOptions): void
}

export interface StockDetailLoadDeps {
  /** 路由里 trim 过的股票代码。 */
  readonly stockCode: ComputedRef<string>
  /** 代码是否缺失（缺失时一个请求都不发）。 */
  readonly hasStockCodeError: ComputedRef<boolean>
  /** K 线周期/复权（组件与 KlineChartCard 双向绑定，故由组件持有）。 */
  readonly klinePeriod: Ref<KlinePeriod>
  readonly adjustMode: Ref<'raw' | 'qfq'>
  readonly message: StockDetailNotifier
  readonly dialog: StockDetailDialog
  /** 数据落定后重算目录高亮（`useTocFollow` 提供）。 */
  readonly updateActiveSection: () => void
}

export function useStockDetailLoad(deps: StockDetailLoadDeps) {
  const { stockCode, hasStockCodeError, klinePeriod, adjustMode, message, dialog, updateActiveSection } = deps

  const loading = ref(false)
  // L0-7（报告42）: 无效股票/无数据时呈现友好结果态，而非一页横线
  const stockUnavailable = ref(false)
  // 黑名单屏蔽态：与「股票不存在」分开——这个状态带移出按钮，是自救入口。
  const stockBlacklisted = ref(false)
  const blacklistBusy = ref(false)
  const generation = ref(0)
  const stockInfo = ref<StockInfo | null>(null)
  const indicators = ref<IndicatorsResponse | null>(null)
  const klineData = ref<KlineResponse>({ candles: [] })
  const businessOverview = ref<BusinessOverviewResponse | null>(null)
  const warningCodes = ref<readonly WarningCode[]>([])
  const klineAbortController = ref<AbortController | null>(null)

  const hasUntrustedIndicators = computed(
    () => warningCodes.value.length > 0 && isIndicatorUntrusted('*', warningCodes.value),
  )

  // reports/76 P1-2: 自动更新窗口由服务端标注（避免把窗口误读为数据不可信）
  const autoUpdateInProgress = computed(
    () => indicators.value?.auto_update_in_progress === true,
  )

  // ─── 数据获取 ───────────────────────────────────────────────────────────
  async function fetchAll() {
    if (hasStockCodeError.value) return
    const gen = ++generation.value
    loading.value = true
    stockUnavailable.value = false
    stockBlacklisted.value = false
    try {
      // 先问一句「是不是被屏蔽了」：命中就直接渲染屏蔽态，不再打 4 个必然 404
      // 的详情请求（否则一次进入会留下 4 条无意义错误日志）。
      if (await isBlacklisted(gen)) return
      await Promise.all([
        fetchStockInfo(gen),
        fetchIndicators(gen),
        fetchKline(gen),
        fetchBusinessOverview(gen),
      ])
    } finally {
      loading.value = false
      nextTick(updateActiveSection)
    }
  }

  /**
   * 黑名单判定。
   *
   * 判定失败（网络/服务重启）时**退回正常加载**而不是把页面判定为可用：
   * 单股端点自己也有守卫，真被屏蔽会返回结构化 404，由 `fetchStockInfo` 兜底。
   */
  async function isBlacklisted(gen: number): Promise<boolean> {
    try {
      const resp = await axios.get<BlacklistCheckResponse>(
        `/api/blacklist/check/${stockCode.value}`,
      )
      if (gen !== generation.value) return true
      stockBlacklisted.value = resp.data.blacklisted
      return resp.data.blacklisted
    } catch {
      return false
    }
  }

  async function fetchStockInfo(gen: number) {
    try {
      const resp = await axios.get<StockInfo>(`/api/stock/${stockCode.value}/info`)
      if (gen !== generation.value) return
      stockInfo.value = resp.data
    } catch (e) {
      if (gen !== generation.value) return
      stockInfo.value = null
      if (isAxiosError(e) && e.response?.status === 404) {
        // 结构化 404：区分「代码不存在」（终态）与「被黑名单屏蔽」（可撤销）
        if (isBlacklistedDetail(e.response.data?.detail)) {
          stockBlacklisted.value = true
          return
        }
        stockUnavailable.value = true
        return
      }
      message.warning(friendlyErrorMessage(e, '加载股票信息失败'))
    }
  }

  async function fetchIndicators(gen: number) {
    try {
      const resp = await axios.get<IndicatorsResponse>(`/api/stock/${stockCode.value}/indicators`)
      if (gen !== generation.value) return
      indicators.value = resp.data
      // 服务端权威信任信号优先；缺失时回退到数据状态页轮询
      if (resp.data.trust) {
        warningCodes.value = resp.data.trust.warning_codes
        return
      }
    } catch (e) {
      if (gen !== generation.value) return
      indicators.value = null
      if (isAxiosError(e) && e.response?.status === 404) {
        stockUnavailable.value = true
        return
      }
      message.warning(friendlyErrorMessage(e, '加载指标数据失败'))
    }
    await fetchWarningCodes(gen)
  }

  async function fetchKline(gen: number) {
    if (klineAbortController.value) {
      klineAbortController.value.abort()
    }
    klineAbortController.value = new AbortController()
    try {
      const resp = await axios.get<KlineResponse>(`/api/stock/${stockCode.value}/kline`, {
        params: {
          adjust: adjustMode.value,
          period: klinePeriod.value,
        },
        signal: klineAbortController.value.signal,
      })
      if (gen !== generation.value) return
      klineData.value = resp.data
    } catch (e) {
      if (axios.isCancel(e) || gen !== generation.value) return
      klineData.value = { candles: [] }
      message.warning(friendlyErrorMessage(e, '加载K线数据失败'))
    }
  }

  async function fetchBusinessOverview(gen: number) {
    try {
      const resp = await axios.get<BusinessOverviewResponse>(
        `/api/stock/${stockCode.value}/business-overview`,
      )
      if (gen !== generation.value) return
      businessOverview.value = resp.data
    } catch (e) {
      if (gen !== generation.value) return
      businessOverview.value = null
      // 局部缺失（profile/breakdown status=missing）在组件内就地空态，不升页面级
      if (isAxiosError(e) && e.response?.status === 404) {
        stockUnavailable.value = true
        return
      }
      message.warning(friendlyErrorMessage(e, '加载业务概览失败'))
    }
  }

  // ─── 黑名单操作 ────────────────────────────────────────────────────────────
  // 产品裁定：加入前必须二次确认（防误点）；加入后原地渲染屏蔽态，
  // 屏蔽态本身带「移出黑名单」按钮，所以误加也当场救得回来。

  function confirmAddToBlacklist() {
    const label = stockInfo.value?.name || stockCode.value
    dialog.warning({
      title: '加入黑名单',
      content:
        `加入后「${label}」不会再出现在筛选、搜索、自选、个股详情和导出结果里。\n`
        + '它的行情与财报数据会保留、自动更新照常抓取，随时可以在「黑名单」页移出。',
      positiveText: '确认加入',
      negativeText: '取消',
      onPositiveClick: async () => {
        await addToBlacklist()
      },
    })
  }

  async function addToBlacklist() {
    if (blacklistBusy.value) return
    blacklistBusy.value = true
    try {
      await axios.post('/api/blacklist/add', { stock_code: stockCode.value })
      message.success('已加入黑名单')
      stockBlacklisted.value = true
    } catch (e) {
      message.error(friendlyErrorMessage(e, '加入黑名单失败'))
    } finally {
      blacklistBusy.value = false
    }
  }

  async function removeFromBlacklist() {
    if (blacklistBusy.value) return
    blacklistBusy.value = true
    try {
      await axios.delete('/api/blacklist/remove', { data: { stock_code: stockCode.value } })
      message.success('已移出黑名单')
      // 重新拉一次全量数据，而不是只翻个 flag——否则页面会停在一堆空卡片上
      await fetchAll()
    } catch (e) {
      message.error(friendlyErrorMessage(e, '移出黑名单失败'))
    } finally {
      blacklistBusy.value = false
    }
  }

  async function fetchWarningCodes(gen: number) {
    try {
      const resp = await axios.get<{ data_quality: { warning_codes: readonly WarningCode[] } }>(
        '/api/data-status/summary',
      )
      if (gen !== generation.value) return
      warningCodes.value = resp.data.data_quality.warning_codes
    } catch {
      if (gen !== generation.value) return
      warningCodes.value = ['LINEAGE_INVALID']
    }
  }

  // ─── 监听变化 ───────────────────────────────────────────────────────────
  watch([klinePeriod, adjustMode], () => {
    saveKlineSettings(
      {
        period: klinePeriod.value,
        adjust: adjustMode.value,
      },
      pageStorage(),
    )
    fetchKline(generation.value)
  })
  watch(stockCode, fetchAll)

  onMounted(() => {
    fetchAll()
  })

  onUnmounted(() => {
    // 2026-08-14 红队 P3：卸载时中止在途请求，避免离开页面后
    // 响应落地写入已卸载组件状态 / 触发无意义告警。
    klineAbortController.value?.abort()
    klineAbortController.value = null
  })

  return {
    loading,
    stockUnavailable,
    stockBlacklisted,
    blacklistBusy,
    stockInfo,
    indicators,
    klineData,
    businessOverview,
    warningCodes,
    hasUntrustedIndicators,
    autoUpdateInProgress,
    confirmAddToBlacklist,
    removeFromBlacklist,
    fetchAll,
  }
}
