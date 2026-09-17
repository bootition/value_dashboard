<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  NAlert, NButton, NCard, NDataTable, NDescriptions, NDescriptionsItem, NEmpty,
  NForm, NFormItem, NInput, NInputNumber, NModal, NRadioButton, NRadioGroup, NSelect,
  NSpin, NTab, NTabs, NTag, useDialog, useMessage,
} from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import type { DataTableColumns } from 'naive-ui'
import type { EtfCategory, EtfDetail, EtfFundamentals, EtfOverviewItem, EtfOverviewResponse } from '../types/etf-strategy.ts'
import IndexValuationChart from './IndexValuationChart.vue'
import FundamentalChart from './FundamentalChart.vue'
import IndustryContributionBars from './IndustryContributionBars.vue'

const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const errorText = ref('')
const data = ref<EtfOverviewResponse | null>(null)
const catFilter = ref<EtfCategory>('industry')

const categoryMeta: Record<EtfCategory, string> = {
  industry: '行业',
  strategy: '策略',
  market: '市场',
}
const filteredItems = computed(() =>
  (data.value?.items ?? []).filter((item) => item.category === catFilter.value),
)

const hasPositions = computed(() =>
  (data.value?.items ?? []).some((item) => item.position.buy_count > 0),
)
const hasCashActivity = computed(() => (data.value?.cash_net_in ?? 0) !== 0)
const needsSetup = computed(() => data.value != null && !hasPositions.value && !hasCashActivity.value)
const positionedCount = computed(() =>
  (data.value?.items ?? []).filter((item) => item.position.buy_count > 0).length,
)

const signalMeta: Record<string, { type: 'success' | 'error' | 'warning' | 'default'; label: string }> = {
  buy: { type: 'success', label: '买入观察区' },
  sell: { type: 'error', label: '卖出观察区' },
  neutral: { type: 'warning', label: '中性' },
  unavailable: { type: 'default', label: '分位不可得' },
}

function fmt(value: number | null | undefined, digits = 2, suffix = ''): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value.toFixed(digits)}${suffix}`
}

const columns: DataTableColumns<EtfOverviewItem> = [
  { title: 'ETF', key: 'name', render: (row) => `${row.name}（${row.etf_code}）` },
  {
    title: '层级', key: 'category',
    render: (row) => categoryMeta[row.category] ?? row.category,
  },
  { title: '跟踪指数', key: 'track_index_name', render: (row) => row.track_index_name ?? '待配置' },
  {
    title: '信号', key: 'signal',
    render: (row) => {
      const s = signalMeta[row.signal] ?? signalMeta.unavailable
      return h(NTag, { type: s.type, size: 'small', bordered: false }, { default: () => s.label })
    },
  },
  {
    title: '主指标分位', key: 'percentile',
    render: (row) => `${row.percentile_label} ${fmt(row.percentile, 0, '%')}`,
  },
  { title: '现价', key: 'current_price', render: (row) => fmt(row.current_price, 3) },
  { title: '持仓市值', key: 'market_value', render: (row) => fmt(row.market_value) },
  { title: '浮动盈亏', key: 'unrealized_pnl', render: (row) => fmt(row.unrealized_pnl) },
  { title: '下档买入价', key: 'next_buy_price', render: (row) => fmt(row.next_buy_price, 3) },
  { title: '剩余买入', key: 'remaining_buys', render: (row) => (row.remaining_buys > 0 ? `${row.remaining_buys} 档` : '已用尽') },
  { title: '预算剩余', key: 'budget_left', render: (row) => fmt(row.budget_left) },
  { title: '下档卖出价', key: 'next_sell_price', render: (row) => fmt(row.next_sell_price, 3) },
  { title: '卖出进度', key: 'sell_progress', render: (row) => (row.clear_tail ? '清尾仓' : `${row.sell_tranches_done}/10 档`) },
  {
    title: '操作', key: 'actions',
    render: (row) => h('div', { class: 'row-actions' }, [
      h(NButton, { size: 'tiny', onClick: () => openDetail(row.etf_code) }, { default: () => '详情' }),
      h(NButton, { size: 'tiny', onClick: () => openMetaModal(row) }, { default: () => '预算' }),
    ]),
  },
]

// ─── 详情（分位线图） ──────────────────────────────────────────────────
const showDetailModal = ref(false)
const detailLoading = ref(false)
const detail = ref<EtfDetail | null>(null)
const fundamentals = ref<EtfFundamentals | null>(null)
const detailError = ref('')
const fundamentalsError = ref('')

const fundGrowthMode = ref<'yoy' | 'qoq'>('yoy')
const fundEarningsPoints = computed(() =>
  (fundamentals.value?.earnings ?? []).map((point) => ({ date: point.report_date, value: point.value })),
)
const fundMarketCapPoints = computed(() =>
  (fundamentals.value?.market_cap ?? []).map((point) => ({ date: point.trade_date, value: point.value })),
)
const fundGrowthPoints = computed(() => {
  const series = fundGrowthMode.value === 'qoq'
    ? (fundamentals.value?.profit_growth_qoq ?? [])
    : (fundamentals.value?.profit_growth ?? [])
  return series.map((point) => ({ trade_date: point.report_date, value: point.value }))
})
const contributionItems = computed(() =>
  fundGrowthMode.value === 'qoq'
    ? (fundamentals.value?.industry_contribution_qoq ?? [])
    : (fundamentals.value?.industry_contribution ?? []),
)

let detailGeneration = 0
let detailController: AbortController | null = null

/** 关闭详情弹窗：作废在途请求，避免慢请求回填到别的 ETF。 */
function closeDetail() {
  detailGeneration += 1
  detailController?.abort()
  detailController = null
}

async function openDetail(etfCode: string) {
  const gen = ++detailGeneration
  detailController?.abort()
  detailController = new AbortController()
  showDetailModal.value = true
  detailLoading.value = true
  detailError.value = ''
  fundamentalsError.value = ''
  detail.value = null
  fundamentals.value = null
  try {
    const [detailResult, fundResult] = await Promise.allSettled([
      axios.get<EtfDetail>(`/api/etf/${etfCode}/detail`, { signal: detailController.signal }),
      axios.get<EtfFundamentals>(`/api/etf/${etfCode}/fundamentals`, { timeout: 60_000, signal: detailController.signal }),
    ])
    if (gen !== detailGeneration) return
    if (detailResult.status === 'fulfilled') {
      detail.value = detailResult.value.data
    } else {
      detailError.value = isAxiosError(detailResult.reason) ? friendlyErrorMessage(detailResult.reason) : String(detailResult.reason)
    }
    if (fundResult.status === 'fulfilled') {
      fundamentals.value = fundResult.value.data
    } else {
      fundamentalsError.value = isAxiosError(fundResult.reason) ? friendlyErrorMessage(fundResult.reason) : String(fundResult.reason)
    }
  } finally {
    if (gen === detailGeneration) detailLoading.value = false
  }
}

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

let generation = 0
let controller: AbortController | null = null

async function load() {
  const gen = ++generation
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  errorText.value = ''
  try {
    const resp = await axios.get<EtfOverviewResponse>('/api/etf/overview', {
      signal: controller.signal,
      timeout: 15_000,
    })
    if (gen !== generation) return
    data.value = resp.data
  } catch (error) {
    if (axios.isCancel(error) || gen !== generation) return
    errorText.value = isAxiosError(error) ? friendlyErrorMessage(error) : String(error)
  } finally {
    if (gen === generation) loading.value = false
  }
}

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

onMounted(load)
onBeforeUnmount(() => {
  generation += 1
  controller?.abort()
  closeDetail()
})
</script>

<template>
  <section>
    <NSpin :show="loading">
      <div v-if="errorText && data" class="etf-refresh-warning">
        <NAlert type="warning" :show-icon="false">{{ errorText }}</NAlert>
        <NButton size="small" @click="load()">重试</NButton>
      </div>
      <template v-if="data">
        <NCard v-if="needsSetup" size="small" title="策略初始化向导" class="etf-onboarding">
          <p class="etf-onboarding-copy">
            当前没有持仓与资金流水。按下面顺序完成初始化，系统会按你的预算和网格设置自动计算买卖档位。
          </p>
          <div class="etf-onboarding-grid">
            <div class="etf-step">
              <span class="etf-step-no">1</span>
              <div>
                <b>策略总资产</b>
                <p>{{ data.total_assets ?? '未设置' }} · 用于盈亏与资产视图</p>
              </div>
              <NButton size="small" type="primary" @click="openCapitalModal">
                {{ data.total_assets ? '修改' : '设置' }}
              </NButton>
            </div>
            <div class="etf-step">
              <span class="etf-step-no">2</span>
              <div>
                <b>初始持仓标的</b>
                <p>当前已录入 {{ positionedCount }} 只 · 默认观察池 {{ data.items.length }} 只</p>
              </div>
              <NButton size="small" type="primary" @click="openPositionModal">录入持仓</NButton>
            </div>
            <div class="etf-step">
              <span class="etf-step-no">3</span>
              <div>
                <b>预算与网格</b>
                <p>在下方表格每只 ETF 的「预算」里设置单档金额</p>
              </div>
            </div>
          </div>
        </NCard>

        <div class="etf-toolbar">
          <NDescriptions :column="5" label-placement="top" size="small" class="etf-summary">
            <NDescriptionsItem label="策略总资产（手填）">{{ data.total_assets ?? '未设置' }}</NDescriptionsItem>
            <NDescriptionsItem label="持仓市值">{{ fmt(data.market_value) }}</NDescriptionsItem>
            <NDescriptionsItem label="浮动盈亏">{{ fmt(data.unrealized_pnl) }}</NDescriptionsItem>
            <NDescriptionsItem label="已实现盈亏">{{ fmt(data.realized_pnl) }}</NDescriptionsItem>
            <NDescriptionsItem label="累计净入金">{{ fmt(data.cash_net_in) }}</NDescriptionsItem>
          </NDescriptions>
          <div class="etf-actions">
            <NButton size="small" @click="openCapitalModal">设置总资产</NButton>
            <NButton size="small" @click="openPositionModal">录入初始持仓</NButton>
            <NButton size="small" @click="openTradeModal">录入买卖</NButton>
            <NButton size="small" @click="openCashModal">录入资金</NButton>
            <NButton size="small" type="error" quaternary :loading="resetting" @click="doReset">重新开始</NButton>
          </div>
        </div>

        <NTabs v-model:value="catFilter" type="line" size="small" class="etf-tabs">
          <NTab name="industry">行业</NTab>
          <NTab name="strategy">策略</NTab>
          <NTab name="market">市场</NTab>
        </NTabs>

        <NEmpty v-if="filteredItems.length === 0" description="该层级暂无 ETF" />
        <NCard v-else size="small" title="持仓与网格">
          <NDataTable :columns="columns" :data="filteredItems" :bordered="false" size="small" />
          <p class="etf-hint">预算/间距/层级可在「预算」操作中调整（每只 ETF 手动预算，单档 = 预算 ÷ 10）。</p>
        </NCard>
      </template>
      <template v-else-if="errorText">
        <NAlert type="error" :show-icon="false">{{ errorText }}</NAlert>
        <NButton size="small" class="etf-retry" @click="load()">重新加载</NButton>
      </template>
    </NSpin>

    <NModal v-model:show="showDetailModal" preset="card" :title="`${detail?.name ?? ''} · 估值与基本面`" style="width: 980px; max-width: 96vw" @after-leave="closeDetail">
      <NSpin :show="detailLoading">
        <NAlert v-if="detailError" type="error" :show-icon="false">{{ detailError }}</NAlert>
        <template v-else-if="detail">
          <NDescriptions :column="4" label-placement="top" size="small" class="detail-stats">
            <NDescriptionsItem label="信号">
              <NTag :type="(signalMeta[detail.signal] ?? signalMeta.unavailable).type" size="small" :bordered="false">
                {{ (signalMeta[detail.signal] ?? signalMeta.unavailable).label }}
              </NTag>
            </NDescriptionsItem>
            <NDescriptionsItem label="主指标分位">{{ detail.percentile_label }} {{ fmt(detail.percentile, 0, '%') }}</NDescriptionsItem>
            <NDescriptionsItem label="持仓成本">{{ fmt(detail.position.avg_cost, 3) }}</NDescriptionsItem>
            <NDescriptionsItem label="已实现盈亏">{{ fmt(detail.position.realized_pnl) }}</NDescriptionsItem>
            <NDescriptionsItem label="PE（TTM/口径）">{{ fmt(detail.valuation?.pe) }}</NDescriptionsItem>
            <NDescriptionsItem label="PB">{{ fmt(detail.valuation?.pb) }}</NDescriptionsItem>
            <NDescriptionsItem label="ERP">{{ detail.valuation?.erp == null ? '—' : `${detail.valuation.erp.toFixed(2)}%` }}</NDescriptionsItem>
            <NDescriptionsItem label="估值截至">{{ detail.valuation?.latest_date ?? '—' }}</NDescriptionsItem>
          </NDescriptions>
          <template v-if="detail.track_valuation">
            <IndexValuationChart
              :points="detail.track_valuation.pe_series"
              :bands="detail.track_valuation.pe_bands"
              label="PE 历史与分位带"
              color="#4f8fc9"
              :height="220"
            />
            <IndexValuationChart
              :points="detail.track_valuation.pb_series"
              :bands="detail.track_valuation.pb_bands"
              label="PB 历史与分位带"
              color="#c98a4f"
              :height="220"
            />
          </template>
          <NAlert v-else type="default" :show-icon="false">
            跟踪指数无估值历史（如港股/中概），信号来自同花顺跟踪指数五年分位；该源不可得时如实标注「分位不可得」。
          </NAlert>

          <NCard size="small" title="成分股业绩与市值" class="fund-card">
            <NAlert v-if="fundamentalsError" type="error" :show-icon="false">{{ fundamentalsError }}</NAlert>
            <template v-else-if="fundamentals">
              <p class="fund-meta">
                口径：{{ fundamentals.method }} · 覆盖 {{ fundamentals.companies }} 家公司 ·
                最新业绩合计 {{ fundamentals.latest_profit == null ? '—' : `${fundamentals.latest_profit.toFixed(0)} 亿` }} ·
                最新市值合计 {{ fundamentals.latest_market_cap == null ? '—' : `${fundamentals.latest_market_cap.toFixed(0)} 亿` }}
              </p>
              <FundamentalChart
                :bars="fundEarningsPoints"
                :line="fundMarketCapPoints"
                bar-label="归母净利润合计"
                line-label="总市值"
                :height="250"
              />
              <NAlert type="info" :show-icon="false" class="fund-disclaimer">{{ fundamentals.disclaimer }}</NAlert>
            </template>
          </NCard>

          <template v-if="fundamentals && detail.etf_code === 'ALL_A'">
            <NCard size="small" title="利润增长与行业贡献" class="fund-card">
              <div class="fund-mode">
                <n-radio-group v-model:value="fundGrowthMode" size="small">
                  <n-radio-button value="yoy">同比</n-radio-button>
                  <n-radio-button value="qoq">环比</n-radio-button>
                </n-radio-group>
              </div>
              <IndexValuationChart
                :points="fundGrowthPoints"
                :label="fundGrowthMode === 'qoq' ? '利润增速-环比（%）' : '利润增速-同比（%）'"
                color="#6fae87"
                :height="220"
              />
              <IndustryContributionBars :items="contributionItems" :mode="fundGrowthMode" />
            </NCard>
          </template>
        </template>
      </NSpin>
    </NModal>

    <NModal v-model:show="showTradeModal" preset="card" title="录入买卖" style="width: 480px">
      <NForm label-placement="left" label-width="72">
        <NFormItem label="ETF">
          <NSelect v-model:value="tradeForm.etf_code" :options="etfOptions" placeholder="选择 ETF" />
        </NFormItem>
        <NFormItem label="方向">
          <NSelect
v-model:value="tradeForm.direction" :options="[
            { label: '买入', value: 'buy' }, { label: '卖出', value: 'sell' },
          ]"
/>
        </NFormItem>
        <NFormItem label="日期">
          <NInput v-model:value="tradeForm.trade_date" placeholder="YYYY-MM-DD" />
        </NFormItem>
        <NFormItem label="价格">
          <NInputNumber v-model:value="tradeForm.price" :step="0.001" style="width: 100%" />
        </NFormItem>
        <NFormItem label="份额">
          <NInputNumber v-model:value="tradeForm.shares" :step="100" style="width: 100%" />
        </NFormItem>
        <NFormItem label="手续费">
          <NInputNumber v-model:value="tradeForm.fee" :step="0.1" style="width: 100%" />
        </NFormItem>
      </NForm>
      <template #footer>
        <NButton type="primary" :loading="saving" @click="submitTrade">保存</NButton>
      </template>
    </NModal>

    <NModal v-model:show="showCashModal" preset="card" title="录入资金流水" style="width: 420px">
      <NForm label-placement="left" label-width="72">
        <NFormItem label="日期">
          <NInput v-model:value="cashForm.flow_date" placeholder="YYYY-MM-DD" />
        </NFormItem>
        <NFormItem label="类型">
          <NSelect
v-model:value="cashForm.direction" :options="[
            { label: '入金', value: 'in' }, { label: '出金', value: 'out' },
          ]"
/>
        </NFormItem>
        <NFormItem label="金额">
          <NInputNumber v-model:value="cashForm.amount" :step="100" style="width: 100%" />
        </NFormItem>
      </NForm>
      <template #footer>
        <NButton type="primary" :loading="saving" @click="submitCash">保存</NButton>
      </template>
    </NModal>

    <NModal v-model:show="showCapitalModal" preset="card" title="策略总资产" style="width: 420px">
      <NForm label-placement="left" label-width="96">
        <NFormItem label="总资产（元）">
          <NInputNumber v-model:value="capitalForm.total_assets" :min="0" :step="100" placeholder="如 100000" style="width: 100%" />
        </NFormItem>
      </NForm>
      <template #footer>
        <NButton type="primary" :loading="saving" @click="submitCapital">保存</NButton>
      </template>
    </NModal>

    <NModal v-model:show="showPositionModal" preset="card" title="录入初始持仓" style="width: 520px">
      <NForm label-placement="left" label-width="96">
        <NFormItem label="ETF">
          <NSelect v-model:value="positionForm.etf_code" :options="etfOptions" filterable placeholder="选择持有标的" />
        </NFormItem>
        <NFormItem label="买入日期">
          <NInput v-model:value="positionForm.trade_date" placeholder="YYYY-MM-DD" />
        </NFormItem>
        <NFormItem label="买入价格">
          <NInputNumber v-model:value="positionForm.price" :step="0.001" style="width: 100%" />
        </NFormItem>
        <NFormItem label="持有份额">
          <NInputNumber v-model:value="positionForm.shares" :min="0" :step="100" style="width: 100%" />
        </NFormItem>
        <NFormItem label="手续费">
          <NInputNumber v-model:value="positionForm.fee" :min="0" :step="0.1" style="width: 100%" />
        </NFormItem>
        <NFormItem label="预算（元）">
          <NInputNumber v-model:value="positionForm.budget" :min="0" :step="100" placeholder="单只 ETF 总预算，单档 = 预算 ÷ 10" style="width: 100%" />
        </NFormItem>
        <NFormItem label="网格间距（%）">
          <NInputNumber v-model:value="positionForm.step_pct" :min="1" :max="10" :step="1" style="width: 100%" />
        </NFormItem>
      </NForm>
      <template #footer>
        <NButton type="primary" :loading="saving" @click="submitPosition">保存并开始网格</NButton>
      </template>
    </NModal>

    <NModal v-model:show="showMetaModal" preset="card" title="ETF 预算与设置" style="width: 420px">
      <NForm label-placement="left" label-width="96">
        <NFormItem label="ETF">
          <NSelect v-model:value="metaForm.etf_code" :options="etfOptions" />
        </NFormItem>
        <NFormItem label="层级">
          <NSelect
v-model:value="metaForm.category" :options="[
            { label: '行业', value: 'industry' },
            { label: '策略', value: 'strategy' },
            { label: '市场', value: 'market' },
          ]"
/>
        </NFormItem>
        <NFormItem label="预算（元）">
          <NInputNumber v-model:value="metaForm.budget" :step="100" style="width: 100%" />
        </NFormItem>
        <NFormItem label="网格间距（%）">
          <NInputNumber v-model:value="metaForm.step_pct" :min="1" :max="10" :step="1" style="width: 100%" />
        </NFormItem>
        <NFormItem label="策略总资产">
          <NInput v-model:value="metaForm.total_assets" placeholder="如 4100.99" />
        </NFormItem>
      </NForm>
      <template #footer>
        <NButton type="primary" :loading="saving" @click="submitMeta">保存</NButton>
      </template>
    </NModal>
  </section>
</template>

<style scoped>
.etf-toolbar { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.etf-retry { margin-top: 8px; }
.etf-refresh-warning { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.etf-summary { flex: 1; }
.etf-actions { display: flex; gap: 8px; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; }
.etf-onboarding { margin-bottom: 12px; }
.etf-onboarding-copy { margin: 0 0 12px; color: var(--text); font-size: 12px; line-height: 1.6; }
.etf-onboarding-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.etf-step { display: flex; align-items: center; gap: 10px; padding: 10px; border: 1px solid #e3ece4; border-radius: 8px; background: #fafcf9; }
.etf-step-no { display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; border-radius: 50%; background: #c3dfca; color: #3e7551; font-size: 12px; font-weight: 700; flex: 0 0 auto; }
.etf-step b { color: var(--text-h); font-size: 12px; }
.etf-step p { margin: 3px 0 0; color: var(--text); font-size: 11px; line-height: 1.5; }
.etf-tabs { margin-bottom: 10px; }
.etf-hint { color: var(--text); font-size: 12px; margin-top: 10px; }
.row-actions { display: flex; gap: 4px; }
.detail-stats { margin-bottom: 8px; }
.fund-card { margin-top: 10px; }
.fund-meta { margin: 0 0 8px; color: var(--text); font-size: 12px; line-height: 1.6; }
.fund-mode { display: flex; justify-content: flex-end; margin-bottom: 8px; }
.fund-disclaimer { margin-top: 8px; }
@media (max-width: 1100px) {
  .etf-onboarding-grid { grid-template-columns: 1fr; }
  .etf-toolbar { flex-direction: column; }
  .etf-actions { justify-content: flex-start; }
}
</style>
