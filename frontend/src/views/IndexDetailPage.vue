<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, RouterLink } from 'vue-router'
import { NAlert, NButton, NCard, NDescriptions, NDescriptionsItem, NEmpty, NRadioButton, NRadioGroup, NSpin, NTag } from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import IndexValuationChart from '../components/IndexValuationChart.vue'
import FundamentalChart from '../components/FundamentalChart.vue'
import IndustryContributionBars from '../components/IndustryContributionBars.vue'
import type { ErpDetail, IndexDetailResponse, ValuationDetail } from '../types/index-dashboard.ts'
import type { EtfFundamentals } from '../types/etf-strategy.ts'

const route = useRoute()
const code = computed(() => String(route.params.code ?? ''))
const loading = ref(false)
const errorText = ref('')
const erp = ref<ErpDetail | null>(null)
const valuation = ref<ValuationDetail | null>(null)
const fundamentals = ref<EtfFundamentals | null>(null)
const fundamentalsError = ref('')

const fundEarningsPoints = computed(() =>
  (fundamentals.value?.earnings ?? []).map((point) => ({ date: point.report_date, value: point.value })),
)
const fundMarketCapPoints = computed(() =>
  (fundamentals.value?.market_cap ?? []).map((point) => ({ date: point.trade_date, value: point.value })),
)
const fundGrowthMode = ref<'yoy' | 'qoq'>('yoy')
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

function fmt(value: number | null | undefined, digits = 2, suffix = ''): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value.toFixed(digits)}${suffix}`
}

const erpSeries = computed(() => (erp.value?.series ?? []).map((p) => ({ trade_date: p.trade_date, value: p.erp })))
const peSeries = computed(() => valuation.value?.pe_series ?? [])
const pbSeries = computed(() => valuation.value?.pb_series ?? [])

let generation = 0
let controller: AbortController | null = null

async function load() {
  const gen = ++generation
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  errorText.value = ''
  erp.value = null
  valuation.value = null
  fundamentals.value = null
  fundamentalsError.value = ''
  try {
    try {
      const resp = await axios.get<IndexDetailResponse>(`/api/index/${code.value}/detail`, {
        signal: controller.signal,
        timeout: 15_000,
      })
      if (gen !== generation) return
      erp.value = resp.data.erp
      valuation.value = resp.data.valuation
    } catch (error) {
      // 兼容尚未重启的旧后端：合并详情 404 时退回原来的双端点。
      // 代码非法（新后端已返回“未收录”）不需要再试旧端点。
      if (!isAxiosError(error) || error.response?.status !== 404) throw error
      const responseDetail = error.response?.data?.detail
      if (typeof responseDetail === 'string' && responseDetail.includes('未收录')) throw error
      const [erpResp, valResp] = await Promise.all([
        axios.get<ErpDetail>(`/api/index/${code.value}/erp`, { signal: controller.signal, timeout: 15_000 }),
        axios.get<ValuationDetail>(`/api/index/${code.value}/valuation`, { signal: controller.signal, timeout: 15_000 }),
      ])
      if (gen !== generation) return
      erp.value = erpResp.data
      valuation.value = valResp.data
    }
    // 2026-09-10 用户要求：每个指数详情页都像全A一样展示"市值与利润的关系"
    try {
      const fundResp = await axios.get<EtfFundamentals>(`/api/index/${code.value}/fundamentals`, {
        signal: controller.signal,
        timeout: 60_000,
      })
      if (gen !== generation) return
      fundamentals.value = fundResp.data
    } catch (fundError) {
      if (!axios.isCancel(fundError)) {
        fundamentalsError.value = isAxiosError(fundError) ? friendlyErrorMessage(fundError) : String(fundError)
      }
    }
  } catch (error) {
    if (axios.isCancel(error) || gen !== generation) return
    errorText.value = isAxiosError(error) ? friendlyErrorMessage(error) : String(error)
  } finally {
    if (gen === generation) loading.value = false
  }
}

onMounted(load)
watch(code, load)
onBeforeUnmount(() => {
  generation += 1
  controller?.abort()
})
</script>

<template>
  <main class="page">
    <header class="page-head">
      <RouterLink to="/index" class="back-link">← 指数研究</RouterLink>
      <h1>{{ erp?.name ?? valuation?.name ?? code }}</h1>
      <div class="head-tags">
        <NTag size="small" :bordered="false">{{ erp?.category === 'industry' ? '申万一级行业' : '宽基/红利' }}</NTag>
        <NTag size="small" :bordered="false">{{ erp?.source === 'synthetic_all_a' ? '全市场合成口径' : erp?.cadence === 'monthly' ? '月度序列' : '日度序列' }}</NTag>
        <NTag size="small" :bordered="false" :type="erp?.backtest_validated === false ? 'warning' : 'default'">
          {{ erp?.source === 'synthetic_all_a' ? '合成口径，暂无分位回测' : erp?.backtest_validated === false ? '行业ERP暂无回测验证' : 'ERP回测已验证' }}
        </NTag>
      </div>
    </header>

    <NSpin :show="loading">
      <NEmpty v-if="errorText" :description="errorText">
        <template #extra>
          <NButton size="small" @click="load">重新加载</NButton>
        </template>
      </NEmpty>
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

        <NCard v-if="erpSeries.length >= 2" size="small" title="ERP 历史与分位带（可切换时间长度）" class="chart-card">
          <IndexValuationChart :points="erpSeries" :bands="erp.erp_bands" label="ERP（%）" color="#70a986" />
        </NCard>

        <div class="two-col">
          <NCard v-if="peSeries.length >= 2" size="small" title="PE 历史与分位带" class="chart-card">
            <IndexValuationChart :points="peSeries" :bands="valuation?.pe_bands" label="PE" color="#4f8fc9" />
          </NCard>
          <NCard v-if="pbSeries.length >= 2" size="small" title="PB 历史与分位带" class="chart-card">
            <IndexValuationChart :points="pbSeries" :bands="valuation?.pb_bands" label="PB" color="#c98a4f" />
          </NCard>
        </div>

        <NAlert v-if="fundamentalsError" type="error" :show-icon="false" class="chart-card">{{ fundamentalsError }}</NAlert>
        <template v-else-if="fundamentals">
          <NCard size="small" :title="code === 'ALL_A' ? '全市场业绩与市值' : '成分股业绩与市值'" class="chart-card">
            <p class="all-a-meta">
              口径：{{ fundamentals.method }} · {{ fundamentals.companies }} 家上市公司 ·
              最新归母净利润合计 {{ fundamentals.latest_profit == null ? '—' : `${fundamentals.latest_profit.toFixed(0)} 亿` }} ·
              最新总市值 {{ fundamentals.latest_market_cap == null ? '—' : `${fundamentals.latest_market_cap.toFixed(0)} 亿` }}
              <template v-if="fundamentals.latest_market_cap != null && fundamentals.latest_profit">
                · 市值/利润 {{ (fundamentals.latest_market_cap / fundamentals.latest_profit).toFixed(1) }} 倍
              </template>
            </p>
            <FundamentalChart
              :bars="fundEarningsPoints"
              :line="fundMarketCapPoints"
              bar-label="归母净利润合计"
              line-label="总市值"
              :height="260"
            />
          </NCard>
          <NCard size="small" :title="code === 'ALL_A' ? '全A利润增长率' : '成分股利润增长率'" class="chart-card">
            <div class="all-a-mode">
              <NRadioGroup v-model:value="fundGrowthMode" size="small">
                <NRadioButton value="yoy">同比</NRadioButton>
                <NRadioButton value="qoq">环比</NRadioButton>
              </NRadioGroup>
            </div>
            <IndexValuationChart
              :points="fundGrowthPoints"
              :label="fundGrowthMode === 'qoq' ? '利润增速-环比（%）' : '利润增速-同比（%）'"
              color="#6fae87"
              :height="230"
            />
          </NCard>
          <NCard v-if="code === 'ALL_A' && contributionItems.length > 0" size="small" title="行业利润增长贡献" class="chart-card">
            <div class="all-a-mode">
              <NRadioGroup v-model:value="fundGrowthMode" size="small" aria-label="贡献口径">
                <NRadioButton value="yoy">同比</NRadioButton>
                <NRadioButton value="qoq">环比</NRadioButton>
              </NRadioGroup>
            </div>
            <IndustryContributionBars :items="contributionItems" :mode="fundGrowthMode" />
          </NCard>
          <NAlert v-if="fundamentals.disclaimer" type="info" :show-icon="false" class="chart-card">{{ fundamentals.disclaimer }}</NAlert>
        </template>
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
.all-a-meta { margin: 0 0 10px; color: var(--text); font-size: 12px; line-height: 1.6; }
.all-a-mode { display: flex; justify-content: flex-end; margin-bottom: 8px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 900px) {
  .two-col { grid-template-columns: 1fr; }
}
</style>
