<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  NCard, NSpace, NTag, NSelect, NRadioGroup, NRadioButton,
  NEmpty, NSpin, NDescriptions, NDescriptionsItem, NResult,
} from 'naive-ui'
import axios from 'axios'
import { dispose, init } from 'klinecharts'
import type { Chart, KLineData } from 'klinecharts'
import IndicatorTabs from '../components/IndicatorTabs.vue'
import FinancialTrendCard from '../components/FinancialTrendCard.vue'
import DataTraceability from '../components/DataTraceability.vue'
import DataFreshnessCard from '../components/DataFreshnessCard.vue'
import { fmt } from '../utils/formatters.ts'
import type {
  StockInfo,
  IndicatorsResponse,
  KlineResponse,
  TrendResponse,
  AuditResponse,
} from '../types/stock-detail.ts'
import type { WarningCode } from '../types/data-quality.ts'

const route = useRoute()

const stockCode = computed(() => {
  const code = route.params.code
  return typeof code === 'string' ? code.trim() : ''
})
const hasStockCodeError = computed(() => !stockCode.value)
const loading = ref(false)
const stockInfo = ref<StockInfo | null>(null)
const indicators = ref<IndicatorsResponse | null>(null)
const klineData = ref<KlineResponse>({ candles: [] })
const trendData = ref<TrendResponse>({ trend: [], period: 'annual', count: 0 })
const auditData = ref<AuditResponse>({ field_audit: [], batch_audit: [] })
const warningCodes = ref<readonly WarningCode[]>([])

// K线配置
const adjustMode = ref<'raw' | 'qfq'>('raw')
const klineDays = ref(250)
const klineRef = ref<HTMLElement>()
const chartInstance = ref<Chart | null>(null)

// 财务趋势配置
const trendPeriod = ref<'annual' | 'quarterly' | 'ttm'>('annual')
const trendYears = ref(5)

async function fetchAll() {
  if (hasStockCodeError.value) return
  loading.value = true
  try {
    await Promise.all([
      fetchStockInfo(),
      fetchIndicators(),
      fetchKline(),
      fetchTrend(),
      fetchAudit(),
      fetchWarningCodes(),
    ])
  } finally {
    loading.value = false
  }
}

async function fetchStockInfo() {
  try {
    const resp = await axios.get<StockInfo>(`/api/stock/${stockCode.value}/info`)
    stockInfo.value = resp.data
  } catch {
    stockInfo.value = null
  }
}

async function fetchIndicators() {
  try {
    const resp = await axios.get<IndicatorsResponse>(`/api/stock/${stockCode.value}/indicators`)
    indicators.value = resp.data
  } catch {
    indicators.value = null
  }
}

async function fetchKline() {
  try {
    const resp = await axios.get<KlineResponse>(`/api/stock/${stockCode.value}/kline`, {
      params: { adjust: adjustMode.value, days: klineDays.value },
    })
    klineData.value = resp.data
    renderKline()
  } catch {
    klineData.value = { candles: [] }
  }
}

async function fetchTrend() {
  try {
    const resp = await axios.get<TrendResponse>(`/api/stock/${stockCode.value}/financial-trend`, {
      params: { period: trendPeriod.value, years: trendYears.value },
    })
    trendData.value = resp.data
  } catch {
    trendData.value = { trend: [], period: 'annual', count: 0 }
  }
}

async function fetchAudit() {
  try {
    const resp = await axios.get<AuditResponse>(`/api/stock/${stockCode.value}/source-audit`)
    auditData.value = resp.data
  } catch {
    auditData.value = { field_audit: [], batch_audit: [] }
  }
}

async function fetchWarningCodes() {
  try {
    const resp = await axios.get<{ data_quality: { warning_codes: readonly WarningCode[] } }>(
      '/api/data-status/summary'
    )
    warningCodes.value = resp.data.data_quality.warning_codes
  } catch {
    warningCodes.value = []
  }
}

// ─── K线渲染 ────────────────────────────────────────────────────
function renderKline() {
  if (!klineRef.value || !klineData.value.candles?.length) return

  if (chartInstance.value) {
    dispose(chartInstance.value)
    chartInstance.value = null
  }

  const chart = init(klineRef.value)
  if (!chart) return
  chartInstance.value = chart

  const candles: KLineData[] = klineData.value.candles.map((c) => ({
    timestamp: new Date(c.trade_date).getTime(),
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
    volume: c.volume,
    turnover: c.turnover,
  }))

  chart.setDataLoader({
    getBars: ({ type, callback }) => callback(type === 'init' ? candles : [], false),
  })
  chart.setSymbol({ ticker: stockCode.value, pricePrecision: 2, volumePrecision: 0 })
  chart.setPeriod({ type: 'day', span: 1 })
  chart.createIndicator('MA')
}

// ─── 监听变化 ───────────────────────────────────────────────────
watch(adjustMode, fetchKline)
watch(klineDays, fetchKline)
watch(trendPeriod, fetchTrend)
watch(trendYears, fetchTrend)
watch(stockCode, fetchAll)

onMounted(fetchAll)

onUnmounted(() => {
  if (chartInstance.value) {
    dispose(chartInstance.value)
    chartInstance.value = null
  }
})
</script>

<template>
  <div>
    <n-result
      v-if="hasStockCodeError"
      status="error"
      title="股票代码缺失"
      description="URL 中未提供股票代码，无法加载个股详情。请从筛选或自选列表进入。"
    />
    <n-spin v-else :show="loading">
      <!-- 股票头部信息 -->
      <n-card size="small" style="margin-bottom: 16px;">
        <n-space align="center" justify="space-between">
          <n-space align="center">
            <h2 style="margin: 0;">{{ stockInfo?.name || stockCode }}</h2>
            <n-tag v-if="stockInfo?.exchange" size="small">{{ stockInfo.exchange }}</n-tag>
            <n-tag v-if="stockInfo?.is_st" size="small" type="warning">ST</n-tag>
            <n-tag v-if="stockInfo?.is_suspended" size="small" type="error">停牌</n-tag>
          </n-space>
          <n-space align="center">
            <span style="font-size: 24px; font-weight: 600;">{{ fmt(stockInfo?.latest_close) }}</span>
            <span style="color: #999; font-size: 12px;">{{ stockInfo?.latest_price_date }}</span>
          </n-space>
        </n-space>
        <n-descriptions :column="4" size="small" style="margin-top: 8px;">
          <n-descriptions-item label="代码">{{ stockInfo?.stock_code }}</n-descriptions-item>
          <n-descriptions-item label="拼音">{{ stockInfo?.pinyin }}</n-descriptions-item>
          <n-descriptions-item label="上市日期">{{ stockInfo?.listing_date }}</n-descriptions-item>
          <n-descriptions-item label="申万一级">{{ stockInfo?.sw_level1 || '—' }}</n-descriptions-item>
        </n-descriptions>
      </n-card>

      <!-- K线图 -->
      <n-card title="K线图" size="small" style="margin-bottom: 16px;">
        <template #header-extra>
          <n-space>
            <n-radio-group v-model:value="adjustMode" size="small">
              <n-radio-button value="raw">不复权</n-radio-button>
              <n-radio-button value="qfq">前复权</n-radio-button>
            </n-radio-group>
            <n-select v-model:value="klineDays" :options="[{label:'250日',value:250},{label:'500日',value:500},{label:'1000日',value:1000}]" size="small" style="width:100px;" />
          </n-space>
        </template>
        <div ref="klineRef" style="height: 400px; width: 100%;"></div>
        <n-empty v-if="!klineData.candles?.length" description="无K线数据" style="padding: 40px;" />
      </n-card>

      <!-- 数据新鲜度 -->
      <DataFreshnessCard :freshness="indicators?.freshness ?? null" />

      <!-- 指标摘要 -->
      <IndicatorTabs :indicators="indicators" :trend-data="trendData" :warning-codes="warningCodes" />

      <!-- 财务趋势 -->
      <FinancialTrendCard
        v-model:trend-period="trendPeriod"
        v-model:trend-years="trendYears"
        :trend-data="trendData"
      />

      <!-- 溯源信息 + PDF -->
      <DataTraceability :stock-code="stockCode" :audit-data="auditData" />
    </n-spin>
  </div>
</template>
