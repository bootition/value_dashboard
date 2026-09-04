<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, RouterLink } from 'vue-router'
import { NAlert, NCard, NDescriptions, NDescriptionsItem, NEmpty, NSpin, NTag } from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import IndexValuationChart from '../components/IndexValuationChart.vue'
import type { ErpDetail, ValuationDetail } from '../types/index-dashboard.ts'

const route = useRoute()
const code = computed(() => String(route.params.code ?? ''))
const loading = ref(false)
const errorText = ref('')
const erp = ref<ErpDetail | null>(null)
const valuation = ref<ValuationDetail | null>(null)

function fmt(value: number | null | undefined, digits = 2, suffix = ''): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value.toFixed(digits)}${suffix}`
}

const erpSeries = computed(() => (erp.value?.series ?? []).map((p) => ({ trade_date: p.trade_date, value: p.erp })))
const peSeries = computed(() => valuation.value?.pe_series ?? [])
const pbSeries = computed(() => valuation.value?.pb_series ?? [])

async function load() {
  loading.value = true
  errorText.value = ''
  try {
    const [erpResp, valResp] = await Promise.all([
      axios.get<ErpDetail>(`/api/index/${code.value}/erp`),
      axios.get<ValuationDetail>(`/api/index/${code.value}/valuation`),
    ])
    erp.value = erpResp.data
    valuation.value = valResp.data
  } catch (error) {
    errorText.value = isAxiosError(error) ? friendlyErrorMessage(error) : String(error)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(code, load)
</script>

<template>
  <main class="page">
    <header class="page-head">
      <RouterLink to="/index" class="back-link">← 指数研究</RouterLink>
      <h1>{{ erp?.name ?? valuation?.name ?? code }}</h1>
      <div class="head-tags">
        <NTag size="small" :bordered="false">{{ erp?.category === 'industry' ? '申万一级行业' : '宽基/红利' }}</NTag>
        <NTag size="small" :bordered="false">{{ erp?.cadence === 'monthly' ? '月度序列' : '日度序列' }}</NTag>
        <NTag size="small" :bordered="false" :type="erp?.backtest_validated === false ? 'warning' : 'default'">
          {{ erp?.backtest_validated === false ? '行业ERP暂无回测验证' : 'ERP回测已验证' }}
        </NTag>
      </div>
    </header>

    <NSpin :show="loading">
      <NEmpty v-if="errorText" :description="errorText" />
      <template v-else-if="erp">
        <NCard size="small" class="stats-card">
          <NDescriptions :column="4" label-placement="top" size="small">
            <NDescriptionsItem label="当前 ERP">
              <strong class="big">{{ erp.erp == null ? '—' : `${erp.erp.toFixed(2)}%` }}</strong>
            </NDescriptionsItem>
            <NDescriptionsItem label="ERP 近10年分位">
              {{ fmt(erp.erp_percentile, 0, '%') }}
            </NDescriptionsItem>
            <NDescriptionsItem label="PE（TTM/口径）">
              {{ fmt(erp.pe) }} <span class="muted">{{ erp.pe_metric }}</span>
            </NDescriptionsItem>
            <NDescriptionsItem label="PE 近10年分位">
              {{ fmt(erp.pe_percentile, 0, '%') }}
            </NDescriptionsItem>
            <NDescriptionsItem label="PB">
              {{ fmt(erp.pb) }}
            </NDescriptionsItem>
            <NDescriptionsItem label="PB 近10年分位">
              {{ fmt(erp.pb_percentile, 0, '%') }}
            </NDescriptionsItem>
            <NDescriptionsItem label="数据截至">
              {{ erp.latest_date ?? '—' }}
            </NDescriptionsItem>
            <NDescriptionsItem label="样本数">
              {{ erp.samples }}
            </NDescriptionsItem>
          </NDescriptions>
        </NCard>

        <NAlert type="info" class="disclaimer" :show-icon="false">{{ erp.disclaimer }}</NAlert>

        <NCard size="small" title="ERP 历史与近10年分位带（20/50/80）" class="chart-card">
          <IndexValuationChart :points="erpSeries" :bands="erp.erp_bands" label="ERP（%）" color="#70a986" />
        </NCard>

        <div class="two-col">
          <NCard size="small" title="PE 历史与分位带" class="chart-card">
            <IndexValuationChart :points="peSeries" :bands="valuation?.pe_bands" label="PE" color="#4f8fc9" />
          </NCard>
          <NCard size="small" title="PB 历史与分位带" class="chart-card">
            <IndexValuationChart :points="pbSeries" :bands="valuation?.pb_bands" label="PB" color="#c98a4f" />
          </NCard>
        </div>
      </template>
    </NSpin>
  </main>
</template>

<style scoped>
.page { padding: 24px; }
.page-head { margin-bottom: 16px; }
.back-link { display: inline-block; margin-bottom: 6px; color: #57966d; text-decoration: none; font-size: 13px; }
.page-head h1 { margin: 0 0 6px; font-size: 22px; color: var(--text-h); }
.head-tags { display: flex; gap: 6px; }
.stats-card { margin-bottom: 12px; }
.big { font-size: 20px; color: var(--text-h); }
.muted { color: var(--text); font-size: 12px; }
.disclaimer { margin-bottom: 12px; }
.chart-card { margin-bottom: 12px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 900px) {
  .two-col { grid-template-columns: 1fr; }
}
</style>
