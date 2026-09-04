<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import {
  NAlert, NButton, NCard, NDataTable, NDescriptions, NDescriptionsItem, NEmpty,
  NForm, NFormItem, NInput, NInputNumber, NModal, NSelect, NSpin, NTag, useMessage,
} from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import type { DataTableColumns } from 'naive-ui'
import type { EtfDetail, EtfOverviewItem, EtfOverviewResponse } from '../types/etf-strategy.ts'
import IndexValuationChart from './IndexValuationChart.vue'

const message = useMessage()
const loading = ref(false)
const errorText = ref('')
const data = ref<EtfOverviewResponse | null>(null)

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
const detailError = ref('')

async function openDetail(etfCode: string) {
  showDetailModal.value = true
  detailLoading.value = true
  detailError.value = ''
  try {
    const resp = await axios.get<EtfDetail>(`/api/etf/${etfCode}/detail`)
    detail.value = resp.data
  } catch (error) {
    detailError.value = isAxiosError(error) ? friendlyErrorMessage(error) : String(error)
  } finally {
    detailLoading.value = false
  }
}

// ─── 录入表单 ──────────────────────────────────────────────────────────
const showTradeModal = ref(false)
const showCashModal = ref(false)
const showMetaModal = ref(false)
const saving = ref(false)
const tradeForm = ref({ etf_code: '', trade_date: new Date().toISOString().slice(0, 10), direction: 'buy', price: null as number | null, shares: null as number | null, fee: 0.0 })
const cashForm = ref({ flow_date: new Date().toISOString().slice(0, 10), direction: 'in', amount: null as number | null })
const metaForm = ref({ etf_code: '', budget: null as number | null, step_pct: 5.0, total_assets: '' })

const etfOptions = computed(() => (data.value?.items ?? []).map((item) => ({
  label: `${item.name}（${item.etf_code}）`, value: item.etf_code,
})))
const selectedMetaItem = computed(() => (data.value?.items ?? []).find((item) => item.etf_code === metaForm.value.etf_code))

function openMetaModal(item: EtfOverviewItem) {
  metaForm.value = {
    etf_code: item.etf_code,
    budget: item.budget,
    step_pct: item.step_pct,
    total_assets: data.value?.total_assets ?? '',
  }
  showMetaModal.value = true
}

async function load() {
  loading.value = true
  errorText.value = ''
  try {
    const resp = await axios.get<EtfOverviewResponse>('/api/etf/overview')
    data.value = resp.data
  } catch (error) {
    errorText.value = isAxiosError(error) ? friendlyErrorMessage(error) : String(error)
  } finally {
    loading.value = false
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
</script>

<template>
  <section>
    <NSpin :show="loading">
      <NAlert v-if="errorText" type="error" :show-icon="false">{{ errorText }}</NAlert>
      <template v-else-if="data">
        <div class="etf-toolbar">
          <NDescriptions :column="5" label-placement="top" size="small" class="etf-summary">
            <NDescriptionsItem label="策略总资产（手填）">{{ data.total_assets ?? '未设置' }}</NDescriptionsItem>
            <NDescriptionsItem label="持仓市值">{{ fmt(data.market_value) }}</NDescriptionsItem>
            <NDescriptionsItem label="浮动盈亏">{{ fmt(data.unrealized_pnl) }}</NDescriptionsItem>
            <NDescriptionsItem label="已实现盈亏">{{ fmt(data.realized_pnl) }}</NDescriptionsItem>
            <NDescriptionsItem label="累计净入金">{{ fmt(data.cash_net_in) }}</NDescriptionsItem>
          </NDescriptions>
          <div class="etf-actions">
            <NButton size="small" @click="showTradeModal = true">录入买卖</NButton>
            <NButton size="small" @click="showCashModal = true">录入资金</NButton>
          </div>
        </div>

        <NEmpty v-if="data.items.length === 0" description="还没有 ETF。先用 vd etf import-xlsx 导入，或点击下方录入。" />
        <NCard v-else size="small" title="持仓与网格">
          <NDataTable :columns="columns" :data="data.items" :bordered="false" size="small" />
          <p class="etf-hint">预算/间距可在「预算」操作中调整（每只 ETF 手动预算，单档 = 预算 ÷ 10）。</p>
        </NCard>
      </template>
    </NSpin>

    <NModal v-model:show="showDetailModal" preset="card" :title="`${detail?.name ?? ''} · 估值分位线`" style="width: 860px">
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
          </NDescriptions>
          <template v-if="detail.track_valuation">
            <IndexValuationChart
              :points="detail.track_valuation.pe_series"
              :bands="detail.track_valuation.pe_bands"
              label="PE 历史与 20/80 分位带"
              color="#4f8fc9"
              :height="200"
            />
            <IndexValuationChart
              :points="detail.track_valuation.pb_series"
              :bands="detail.track_valuation.pb_bands"
              label="PB 历史与 20/80 分位带"
              color="#c98a4f"
              :height="200"
            />
          </template>
          <NAlert v-else type="default" :show-icon="false">
            跟踪指数无估值历史（如港股/中概），信号来自同花顺跟踪指数五年分位；该源不可得时如实标注「分位不可得」。
          </NAlert>
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

    <NModal v-model:show="showMetaModal" preset="card" title="ETF 预算与设置" style="width: 420px">
      <NForm label-placement="left" label-width="96">
        <NFormItem label="ETF">
          <NSelect v-model:value="metaForm.etf_code" :options="etfOptions" />
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
.etf-summary { flex: 1; }
.etf-actions { display: flex; gap: 8px; flex: 0 0 auto; }
.etf-hint { color: var(--text); font-size: 12px; margin-top: 10px; }
.row-actions { display: flex; gap: 4px; }
.detail-stats { margin-bottom: 8px; }
</style>
