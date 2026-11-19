/**
 * ETF 工作台的「5 张录入表单 + 提交」状态机（2026-11-23 重构）。
 *
 * 从 `components/EtfStrategyPanel.vue` **原样抽出**，行为逐字等价：
 * `openTradeModal` / `openCashModal` / `openCapitalModal` / `openPositionModal` /
 * `openMetaModal` / `submitTrade` / `submitCash` / `submitCapital` /
 * `submitPosition` / `submitMeta` / `doReset` 与 `metaForm.etf_code` 的 watch，
 * 函数体只做了缩进——语句、文案、URL、payload 字段顺序一字未改。
 *
 * 抽出的理由（上一轮「不该拆」的判断依据，本轮先补了测试再拆）：
 * 17 个 ref / 5 个模态确实是**同一张表单之外**的独立状态机，彼此通过
 * 「提交后 `await load()` 刷新 + 成功后关窗」这条线串起来；把它们留在一个
 * 738 行组件里，任何一次改动都要在模板与脚本之间来回跳。等价性由
 * `tests/component/etf-strategy-panel.test.ts` 的 14 条行为快照钉住
 * （拆之前先跑绿，拆完一行不改仍然绿）。
 *
 * 不变量（改这里之前先读）：
 * 1. **每次打开都复位表单**：`openTradeModal` / `openCashModal` / `openPositionModal`
 *    整对象重赋值，避免上次输入残留被再次提交（后端无幂等键）。
 * 2. **校验失败不关窗、不清空**：只提示，直接 return；用户输入原样保留。
 * 3. **成功才 `await load()`**：失败路径绝不刷新列表（否则用户可以看到
 *    "列表变了但我这次没提交"的错觉）。
 * 4. `submitMeta` 先写 `total_assets`（仅当表单非空串）再写 meta，两次 POST；
 *    meta 的其余字段取自**列表现状**（`item`），不是表单——只允许改
 *    category / budget / step_pct / total_assets。
 * 5. 弹窗内切换 ETF 时用 watch 把该只的 category/budget/step_pct 同步进表单，
 *    避免把上一只的值写进新选中的 ETF。
 */

import { computed, ref, watch } from 'vue'
import type { ComputedRef, Ref } from 'vue'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import type {
  EtfCategory,
  EtfOverviewItem,
  EtfOverviewResponse,
} from '../types/etf-strategy.ts'

/** 只需要这三个方法，避免把 naive-ui 的类型带进 composable。 */
export interface EtfOpsNotifier {
  warning(content: string): void
  success(content: string): void
  error(content: string): void
}

/** `useDialog().warning` 用到的那几个字段（「重新开始」的二次确认）。 */
export interface EtfOpsDialogOptions {
  title: string
  content: string
  positiveText: string
  negativeText: string
  onPositiveClick: () => Promise<void> | void
}

export interface EtfOpsDialog {
  warning(options: EtfOpsDialogOptions): void
}

export interface EtfOperationsDeps {
  /** `/api/etf/overview` 的当前快照（表单初值、ETF 选项、meta 现状都取自它）。 */
  readonly data: Ref<EtfOverviewResponse | null>
  /** 是否已有持仓（决定设置总资产后是否自动进入「录入初始持仓」）。 */
  readonly hasPositions: ComputedRef<boolean>
  /** 重新拉 overview（成功后调用；失败路径绝不调用）。 */
  readonly load: () => Promise<void>
  readonly message: EtfOpsNotifier
  readonly dialog: EtfOpsDialog
}

export function useEtfOperations(deps: EtfOperationsDeps) {
  const { data, hasPositions, load, message, dialog } = deps

  // ─── 录入表单 ──────────────────────────────────────────────────────────
  const showTradeModal = ref(false)
  const showCashModal = ref(false)
  const showMetaModal = ref(false)
  const saving = ref(false)
  const tradeForm = ref({ etf_code: '', trade_date: new Date().toISOString().slice(0, 10), direction: 'buy', price: null as number | null, shares: null as number | null, fee: 0.0 })
  const cashForm = ref({ flow_date: new Date().toISOString().slice(0, 10), direction: 'in', amount: null as number | null })
  const metaForm = ref({ etf_code: '', category: 'industry' as EtfCategory, budget: null as number | null, step_pct: 5.0, total_assets: '' })

  function openTradeModal() {
    // 每次打开都复位，避免上次输入残留被再次提交（后端无幂等键）。
    tradeForm.value = {
      etf_code: '',
      trade_date: new Date().toISOString().slice(0, 10),
      direction: 'buy',
      price: null,
      shares: null,
      fee: 0,
    }
    showTradeModal.value = true
  }

  function openCashModal() {
    cashForm.value = {
      flow_date: new Date().toISOString().slice(0, 10),
      direction: 'in',
      amount: null,
    }
    showCashModal.value = true
  }

  // ─── 新用户初始化通道 ──────────────────────────────────────────────────
  const showCapitalModal = ref(false)
  const showPositionModal = ref(false)
  const resetting = ref(false)
  const capitalForm = ref<{ total_assets: number | null }>({ total_assets: null })
  const positionForm = ref({
    etf_code: '',
    trade_date: new Date().toISOString().slice(0, 10),
    price: null as number | null,
    shares: null as number | null,
    fee: 0.0,
    budget: null as number | null,
    step_pct: 5.0,
  })

  const selectedPositionItem = computed(() =>
    (data.value?.items ?? []).find((item) => item.etf_code === positionForm.value.etf_code),
  )

  function openCapitalModal() {
    const parsed = Number(data.value?.total_assets)
    capitalForm.value.total_assets = Number.isFinite(parsed) && parsed > 0 ? parsed : null
    showCapitalModal.value = true
  }

  async function submitCapital() {
    const value = capitalForm.value.total_assets
    if (value == null || !Number.isFinite(value) || value <= 0) {
      message.warning('请输入大于 0 的策略总资产')
      return
    }
    saving.value = true
    try {
      await axios.post('/api/etf/settings', { key: 'total_assets', value: String(value) })
      message.success('策略总资产已保存')
      showCapitalModal.value = false
      await load()
      if (!hasPositions.value) openPositionModal()
    } catch (error) {
      message.error(isAxiosError(error) ? friendlyErrorMessage(error) : String(error))
    } finally {
      saving.value = false
    }
  }

  function openPositionModal() {
    const first = data.value?.items[0]
    positionForm.value = {
      etf_code: first?.etf_code ?? '',
      trade_date: new Date().toISOString().slice(0, 10),
      price: null,
      shares: null,
      fee: 0,
      budget: null,
      step_pct: 5.0,
    }
    showPositionModal.value = true
  }

  async function submitPosition() {
    const item = selectedPositionItem.value
    if (!item) {
      message.warning('请选择 ETF')
      return
    }
    if (positionForm.value.price == null || positionForm.value.shares == null || positionForm.value.shares <= 0) {
      message.warning('请填写初始持仓的买入价格与份额')
      return
    }
    saving.value = true
    try {
      await axios.post('/api/etf/bootstrap', {
        total_assets: null,
        positions: [{
          etf_code: item.etf_code,
          name: item.name,
          category: item.category,
          track_index_code: item.track_index_code,
          track_index_name: item.track_index_name,
          primary_metric: item.primary_metric,
          industry_group: item.industry_group,
          budget: positionForm.value.budget ?? 0,
          step_pct: positionForm.value.step_pct,
          shares: positionForm.value.shares,
          price: positionForm.value.price,
          trade_date: positionForm.value.trade_date,
          fee: positionForm.value.fee,
        }],
      })
      message.success('初始持仓已录入')
      showPositionModal.value = false
      await load()
    } catch (error) {
      message.error(isAxiosError(error) ? friendlyErrorMessage(error) : String(error))
    } finally {
      saving.value = false
    }
  }

  async function doReset() {
    resetting.value = true
    try {
      const preview = await axios.get<{ current: Record<string, number> }>('/api/etf/reset-preview', { timeout: 15_000 })
      const counts = Object.entries(preview.data.current)
        .map(([name, count]) => `${name}=${count}`)
        .join('、')
      dialog.warning({
        title: '清空策略并重新开始？',
        content: `将删除全部 ETF 元数据、交易流水、资金流水与设置（当前 ${counts}），然后重建默认观察池。行情与指数估值数据会保留。`,
        positiveText: '确认清空',
        negativeText: '取消',
        onPositiveClick: async () => {
          try {
            await axios.post('/api/etf/reset', { purge_meta: true }, { timeout: 30_000 })
            message.success('已回到新用户状态，默认观察池已重建')
            showCapitalModal.value = true
            capitalForm.value.total_assets = null
            await load()
          } catch (error) {
            message.error(isAxiosError(error) ? friendlyErrorMessage(error) : String(error))
          }
        },
      })
    } catch (error) {
      message.error(isAxiosError(error) ? friendlyErrorMessage(error) : String(error))
    } finally {
      resetting.value = false
    }
  }

  const etfOptions = computed(() => (data.value?.items ?? []).map((item) => ({
    label: `${item.name}（${item.etf_code}）`, value: item.etf_code,
  })))
  const selectedMetaItem = computed(() => (data.value?.items ?? []).find((item) => item.etf_code === metaForm.value.etf_code))

  function openMetaModal(item: EtfOverviewItem) {
    metaForm.value = {
      etf_code: item.etf_code,
      category: item.category,
      budget: item.budget,
      step_pct: item.step_pct,
      total_assets: data.value?.total_assets ?? '',
    }
    showMetaModal.value = true
  }

  // 弹窗内切换 ETF 时同步该只的预算/层级/间距，避免把上一只的值写进新选中的 ETF。
  watch(() => metaForm.value.etf_code, () => {
    const item = selectedMetaItem.value
    if (!item) return
    metaForm.value.category = item.category
    metaForm.value.budget = item.budget
    metaForm.value.step_pct = item.step_pct
  })

  async function submitTrade() {
    if (!tradeForm.value.etf_code || tradeForm.value.price == null || tradeForm.value.shares == null) {
      message.warning('请填写 ETF、价格与份额')
      return
    }
    saving.value = true
    try {
      await axios.post('/api/etf/trades', tradeForm.value)
      message.success('交易已录入')
      showTradeModal.value = false
      await load()
    } catch (error) {
      message.error(isAxiosError(error) ? friendlyErrorMessage(error) : String(error))
    } finally {
      saving.value = false
    }
  }

  async function submitCash() {
    if (cashForm.value.amount == null || cashForm.value.amount <= 0) {
      message.warning('请填写金额')
      return
    }
    saving.value = true
    try {
      await axios.post('/api/etf/cash-flows', cashForm.value)
      message.success('资金流水已录入')
      showCashModal.value = false
      await load()
    } catch (error) {
      message.error(isAxiosError(error) ? friendlyErrorMessage(error) : String(error))
    } finally {
      saving.value = false
    }
  }

  async function submitMeta() {
    const item = selectedMetaItem.value
    if (!item) return
    saving.value = true
    try {
      if (metaForm.value.total_assets !== '') {
        await axios.post('/api/etf/settings', { key: 'total_assets', value: String(metaForm.value.total_assets) })
      }
      await axios.post('/api/etf/meta', {
        etf_code: item.etf_code,
        name: item.name,
        category: metaForm.value.category,
        track_index_code: item.track_index_code,
        track_index_name: item.track_index_name,
        primary_metric: item.primary_metric,
        industry_group: item.industry_group,
        budget: metaForm.value.budget ?? 0,
        step_pct: metaForm.value.step_pct,
        enabled: item.enabled,
      })
      message.success('预算/设置已保存')
      showMetaModal.value = false
      await load()
    } catch (error) {
      message.error(isAxiosError(error) ? friendlyErrorMessage(error) : String(error))
    } finally {
      saving.value = false
    }
  }

  return {
    showTradeModal,
    showCashModal,
    showMetaModal,
    showCapitalModal,
    showPositionModal,
    saving,
    resetting,
    tradeForm,
    cashForm,
    metaForm,
    capitalForm,
    positionForm,
    etfOptions,
    openTradeModal,
    openCashModal,
    openCapitalModal,
    openPositionModal,
    openMetaModal,
    doReset,
    submitTrade,
    submitCash,
    submitCapital,
    submitPosition,
    submitMeta,
  }
}
