<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  NCard, NSpace, NTag, NSelect, NRadioGroup, NRadioButton,
  NEmpty, NSpin, NAlert, NDescriptions, NDescriptionsItem, NResult,
  useMessage,
} from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { dispose, init } from 'klinecharts'
import type { Chart, KLineData } from 'klinecharts'
import { isIndicatorUntrusted } from '../types/data-quality.ts'
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
const message = useMessage()

const stockCode = computed(() => {
  const code = route.params.code
  return typeof code === 'string' ? code.trim() : ''
})
const hasStockCodeError = computed(() => !stockCode.value)
const loading = ref(false)
const generation = ref(0)
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
const klineAbortController = ref<AbortController | null>(null)

const hasUntrustedIndicators = computed(() => warningCodes.value.length > 0 && isIndicatorUntrusted('*', warningCodes.value))

// 财务趋势配置
const trendPeriod = ref<'annual' | 'quarterly' | 'ttm'>('annual')
const trendYears = ref(5)

function onIndicatorTimeDimension(value: string) {
  const yearsByDimension: Record<string, number> = {
    current: 1,
    '1y': 1,
    '3y': 3,
    '5y': 5,
    '10y': 10,
    all: 99,
  }
  trendPeriod.value = 'annual'
  trendYears.value = yearsByDimension[value] ?? 5
}

async function fetchAll() {
  if (hasStockCodeError.value) return
  const gen = ++generation.value
  loading.value = true
  try {
    await Promise.all([
      fetchStockInfo(gen),
      fetchIndicators(gen),
      fetchKline(gen),
      fetchTrend(gen),
      fetchAudit(gen),
    ])
  } finally {
    loading.value = false
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
    const detail = isAxiosError(e) ? e.response?.data?.detail : null
    message.warning(`加载股票信息失败: ${detail || '网络错误'}`)
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
    const detail = isAxiosError(e) ? e.response?.data?.detail : null
    message.warning(`加载指标数据失败: ${detail || '网络错误'}`)
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
      params: { adjust: adjustMode.value, days: klineDays.value },
      signal: klineAbortController.value.signal,
    })
    if (gen !== generation.value) return
    klineData.value = resp.data
    renderKline()
  } catch (e) {
    if (axios.isCancel(e) || gen !== generation.value) return
    klineData.value = { candles: [] }
    const detail = isAxiosError(e) ? e.response?.data?.detail : null
    message.warning(`加载K线数据失败: ${detail || '网络错误'}`)
  }
}

async function fetchTrend(gen: number) {
  try {
    const resp = await axios.get<TrendResponse>(`/api/stock/${stockCode.value}/financial-trend`, {
      params: { period: trendPeriod.value, years: trendYears.value },
    })
    if (gen !== generation.value) return
    trendData.value = resp.data
  } catch (e) {
    if (gen !== generation.value) return
    trendData.value = { trend: [], period: 'annual', count: 0 }
    const detail = isAxiosError(e) ? e.response?.data?.detail : null
    message.warning(`加载财务趋势失败: ${detail || '网络错误'}`)
  }
}

async function fetchAudit(gen: number) {
  try {
    const resp = await axios.get<AuditResponse>(`/api/stock/${stockCode.value}/source-audit`)
    if (gen !== generation.value) return
    auditData.value = resp.data
  } catch (e) {
    if (gen !== generation.value) return
    auditData.value = { field_audit: [], batch_audit: [] }
    const detail = isAxiosError(e) ? e.response?.data?.detail : null
    message.warning(`加载溯源信息失败: ${detail || '网络错误'}`)
  }
}

async function fetchWarningCodes(gen: number) {
  try {
    const resp = await axios.get<{ data_quality: { warning_codes: readonly WarningCode[] } }>(
      '/api/data-status/summary'
    )
    if (gen !== generation.value) return
    warningCodes.value = resp.data.data_quality.warning_codes
  } catch {
    if (gen !== generation.value) return
    warningCodes.value = ['LINEAGE_INVALID']
  }
}

// ─── K线渲染 ────────────────────────────────────────────────────
function renderKline() {
  if (!klineRef.value || !klineData.value.candles?.length) return

  if (chartInstance.value) {
    dispose(chartInstance.value)
    chartInstance.value = null
  }

  const chart = init(klineRef.value, {
    styles: {
      grid: {
        show: true,
        horizontal: {
          color: 'rgba(0,0,0,0.05)',
        },
        vertical: {
          color: 'rgba(0,0,0,0.05)',
        },
      },
      candle: {
        priceMark: {
          last: {
            show: true,
          },
        },
        bar: {
          upColor: '#ef5350',
          downColor: '#26a69a',
          upBorderColor: '#ef5350',
          downBorderColor: '#26a69a',
          upWickColor: '#ef5350',
          downWickColor: '#26a69a',
        },
      },
      indicator: {
        lines: [
          { color: '#ff9800' },
          { color: '#2196f3' },
          { color: '#9c27b0' },
          { color: '#4caf50' },
          { color: '#f44336' },
          { color: '#00bcd4' },
        ],
      },
      xAxis: {
        tickText: {
          color: '#666',
        },
      },
      yAxis: {
        tickText: {
          color: '#666',
        },
      },
    },
  })
  
  if (!chart) return
  chartInstance.value = chart

  const candles: KLineData[] = klineData.value.candles.map((c: any) => ({
    timestamp: new Date(c.trade_date).getTime(),
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
    volume: c.volume,
    turnover: c.turnover,
    // Add MA data if available
    ma5: c.ma5,
    ma10: c.ma10,
    ma20: c.ma20,
    ma60: c.ma60,
    ma120: c.ma120,
    ma250: c.ma250,
  }))

  chart.setDataLoader({
    getBars: ({ type, callback }) => callback(type === 'init' ? candles : [], false),
  })
  chart.createIndicator({ name: 'MA', calcParams: [5, 10, 20, 60, 120, 250] }, false)
  chart.createIndicator('VOL', false)
}

// ─── 监听变化 ───────────────────────────────────────────────────
watch(adjustMode, () => fetchKline(generation.value))
watch(klineDays, () => fetchKline(generation.value))
watch(trendPeriod, () => fetchTrend(generation.value))
watch(trendYears, () => fetchTrend(generation.value))
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
          <n-descriptions-item label="证监会一级">{{ stockInfo?.csrc_l1 || '—' }}</n-descriptions-item>
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

      <!-- 全局数据可信告警 -->
      <n-alert v-if="hasUntrustedIndicators" type="warning" :show-icon="true" style="margin-bottom: 16px;">
        当前数据库状态不可信，以下指标数据可能不准确或不完整。请先检查<router-link to="/data-status">数据状态页</router-link>。
      </n-alert>

      <!-- 数据新鲜度 -->
      <DataFreshnessCard :freshness="indicators?.freshness ?? null" />

      <!-- 指标摘要 -->
      <IndicatorTabs
        :indicators="indicators"
        :trend-data="trendData"
        :warning-codes="warningCodes"
        @update:time-dimension="onIndicatorTimeDimension"
      />

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
