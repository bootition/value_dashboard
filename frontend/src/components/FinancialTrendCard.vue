<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  NSpace, NRadioGroup, NRadioButton, NSelect, NEmpty, NDataTable, NRadio,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type { TrendResponse, FinancialTrendRow } from '../types/stock-detail.ts'
import { fmt, fmtPct } from '../utils/formatters.ts'

const props = defineProps<{
  readonly trendData: TrendResponse
}>()

const trendPeriod = defineModel<'annual' | 'quarterly' | 'ttm'>('trendPeriod', { required: true })
const trendYears = defineModel<number>('trendYears', { required: true })

const showChart = ref(true)
const chartMetric = ref<keyof FinancialTrendRow>('revenue')

const periodOptions = [
  { label: '1年', value: 1 },
  { label: '3年', value: 3 },
  { label: '5年', value: 5 },
  { label: '10年', value: 10 },
  { label: '全部', value: 99 },
]

const allMetricOptions = [
  { label: '营收', value: 'revenue' },
  { label: '归母净利', value: 'parent_net_profit' },
  { label: '毛利率', value: 'gross_margin' },
  { label: '净利率', value: 'net_margin' },
  { label: 'ROE', value: 'roe' },
  { label: '负债率', value: 'debt_ratio' },
  { label: 'EPS', value: 'basic_eps' },
]


const trendRows = computed(() => [...props.trendData.trend])

// 只提供历史序列中确实存在数据的指标，避免整列都是 “—”。
const availableMetricKeys = computed(() => {
  const keys = new Set<string>()
  for (const row of trendRows.value) {
    for (const option of allMetricOptions) {
      const value = row[option.value as keyof FinancialTrendRow]
      if (value !== null && value !== undefined) keys.add(option.value)
    }
  }
  return keys
})
const metricOptions = computed(() =>
  allMetricOptions.filter((option) => availableMetricKeys.value.has(option.value)),
)
watch(metricOptions, (options) => {
  if (options.length > 0 && !options.some((option) => option.value === chartMetric.value)) {
    chartMetric.value = options[0]!.value as keyof FinancialTrendRow
  }
}, { immediate: true })

const isEmpty = computed(() => props.trendData.trend.length === 0)

// Chart data preparation
const chartData = computed(() => {
  if (isEmpty.value) return { points: [], minY: 0, maxY: 0 }
  
  const points = trendRows.value.flatMap(row => {
    const value = row[chartMetric.value] as number | null
    return value === null || value === undefined ? [] : [{ date: row.report_date, value }]
  })
  const values = points.map(point => point.value)
  
  if (values.length === 0) return { points: [], minY: 0, maxY: 0 }
  
  const minY = Math.min(...values)
  const maxY = Math.max(...values)
  
  return { points, minY, maxY }
})

// Generate SVG path for line chart
const chartPath = computed(() => {
  const { points, minY, maxY } = chartData.value
  if (points.length === 0) return ''
  
  const width = 600
  const height = 200
  const padding = 40
  
  const xStep = (width - 2 * padding) / Math.max(points.length - 1, 1)
  const yRange = maxY - minY || 1
  
  return points.map((point, index) => {
    const x = padding + index * xStep
    const y = height - padding - ((point.value - minY) / yRange) * (height - 2 * padding)
    return `${index === 0 ? 'M' : 'L'} ${x} ${y}`
  }).join(' ')
})

// Generate SVG points for line chart
const chartPoints = computed(() => {
  const { points, minY, maxY } = chartData.value
  if (points.length === 0) return []
  
  const width = 600
  const height = 200
  const padding = 40
  
  const xStep = (width - 2 * padding) / Math.max(points.length - 1, 1)
  const yRange = maxY - minY || 1
  
  return points.map((point, index) => {
    const x = padding + index * xStep
    const y = height - padding - ((point.value - minY) / yRange) * (height - 2 * padding)
    return { x, y, value: point.value }
  })
})

// Y-axis labels
const yAxisLabels = computed(() => {
  const { minY, maxY } = chartData.value
  const isPercentage = ['gross_margin', 'net_margin', 'roe', 'debt_ratio'].includes(chartMetric.value)
  
  const steps = 5
  const labels = []
  for (let i = 0; i <= steps; i++) {
    const value = minY + (maxY - minY) * (i / steps)
    labels.push({
      value: isPercentage ? fmtPct(value) : fmt(value, 0),
      y: 200 - 40 - (i / steps) * (200 - 80),
    })
  }
  return labels
})

// L1-2（报告42）: X 轴标签按宽度抽样（最多 8 个），避免每点都画导致重叠
const MAX_X_LABELS = 8
const xAxisLabels = computed(() => {
  if (isEmpty.value) return []
  
  const width = 600
  const padding = 40
  const xStep = (width - 2 * padding) / Math.max(chartData.value.points.length - 1, 1)
  
  const all = chartData.value.points.map((point, index) => ({
    label: point.date?.substring(0, 7) || '',
    x: padding + index * xStep,
  }))
  if (all.length <= MAX_X_LABELS) return all
  const step = Math.ceil(all.length / MAX_X_LABELS)
  return all.filter((_, index) => index % step === 0 || index === all.length - 1)
})

// L1-2（报告42）: 数据点 tooltip（日期 + 值）
const chartPointTitles = computed(() => {
  const isPercentage = ['gross_margin', 'net_margin', 'roe', 'debt_ratio'].includes(chartMetric.value)
  return chartData.value.points.map((point) => ({
    date: point.date || '',
    value: isPercentage ? fmtPct(point.value) : fmt(point.value),
  }))
})

// L2 V4（报告42）: 数值跨越正负时绘制零值基准线
const zeroLineY = computed(() => {
  const { minY, maxY } = chartData.value
  if (minY < 0 && maxY > 0) {
    const padding = 40
    return 200 - padding - ((0 - minY) / (maxY - minY || 1)) * (200 - 2 * padding)
  }
  return null
})

const trendColumns = computed<DataTableColumns<FinancialTrendRow>>(() => {
  const columns: DataTableColumns<FinancialTrendRow> = [
    { title: '报告期', key: 'report_date', width: 110 },
  ]
  const candidates: Array<{ field: keyof FinancialTrendRow; title: string; render: (r: FinancialTrendRow) => string }> = [
    { field: 'revenue', title: '营收', render: (r) => fmt(r.revenue, 0) },
    { field: 'net_profit', title: '归母净利', render: (r) => fmt(r.net_profit, 0) },
    { field: 'deducted_net_profit', title: '扣非净利', render: (r) => fmt(r.deducted_net_profit, 0) },
    { field: 'gross_margin', title: '毛利率', render: (r) => fmtPct(r.gross_margin) },
    { field: 'net_margin', title: '净利率', render: (r) => fmtPct(r.net_margin) },
    { field: 'roe', title: 'ROE', render: (r) => fmtPct(r.roe) },
    { field: 'debt_ratio', title: '负债率', render: (r) => fmtPct(r.debt_ratio) },
    { field: 'basic_eps', title: 'EPS', render: (r) => fmt(r.basic_eps) },
    { field: 'cf_from_operating', title: '经营CF', render: (r) => fmt(r.cf_from_operating, 0) },
  ]
  for (const candidate of candidates) {
    const key = candidate.field
    const hasData = trendRows.value.some((row) => row[key] !== null && row[key] !== undefined)
    if (hasData) {
      columns.push({ title: candidate.title, key, render: candidate.render } as never)
    }
  }
  return columns
})
</script>

<template>
  <section class="trend-workbench">
    <div class="trend-heading"><div><p>FINANCIAL TREND</p><h2>财务趋势</h2></div>
      <n-space>
        <n-radio-group v-model:value="trendPeriod" size="small">
          <n-radio-button value="annual">年度</n-radio-button>
          <n-radio-button value="quarterly">季度</n-radio-button>
          <n-radio-button value="ttm">TTM</n-radio-button>
        </n-radio-group>
        <n-select v-model:value="trendYears" :options="periodOptions" size="small" class="trend-years" />
      </n-space>
    </div>
    <n-empty v-if="isEmpty" description="无财务趋势数据" class="trend-empty" />
    <template v-else>
      <!-- Chart View -->
      <div v-if="showChart" class="trend-chart-view">
        <n-space class="trend-metric-picker">
          <n-radio-group v-model:value="chartMetric" size="small">
            <n-radio v-for="opt in metricOptions" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </n-radio>
          </n-radio-group>
        </n-space>
        
        <!-- L1-2（报告42）: viewBox 等比缩放，宽度 100% 自适应 -->
        <div class="trend-chart-frame"><svg viewBox="0 0 600 200">
          <!-- Grid lines -->
          <line x1="40" y1="40" x2="560" y2="40" stroke="#f0f0f0" stroke-width="1" />
          <line x1="40" y1="80" x2="560" y2="80" stroke="#f0f0f0" stroke-width="1" />
          <line x1="40" y1="120" x2="560" y2="120" stroke="#f0f0f0" stroke-width="1" />
          <line x1="40" y1="160" x2="560" y2="160" stroke="#f0f0f0" stroke-width="1" />
          
          <!-- Y-axis labels -->
          <text v-for="(label, idx) in yAxisLabels" :key="idx" :x="5" :y="label.y + 4" font-size="10" fill="#666">
            {{ label.value }}
          </text>
          
          <!-- X-axis labels（按宽度抽样） -->
          <text v-for="(label, idx) in xAxisLabels" :key="idx" :x="label.x - 15" y="195" font-size="9" fill="#666">
            {{ label.label }}
          </text>
          
          <!-- L2 V4: 零值基准线（正负跨越时） -->
          <line
            v-if="zeroLineY !== null"
            :x1="40"
            :y1="zeroLineY"
            :x2="560"
            :y2="zeroLineY"
            stroke="#d03050"
            stroke-width="1"
            stroke-dasharray="4 3"
            opacity="0.6"
          />

          <!-- Line chart -->
          <path :d="chartPath" fill="none" stroke="#18a058" stroke-width="2" />
          
          <!-- Data points（带原生 tooltip：日期 + 值） -->
          <circle
            v-for="(point, idx) in chartPoints"
            :key="idx"
            :cx="point.x"
            :cy="point.y"
            r="3"
            fill="#18a058"
          >
            <title>{{ chartPointTitles[idx]?.date }}：{{ chartPointTitles[idx]?.value }}</title>
          </circle>
        </svg></div>
      </div>
      
      <!-- Toggle view -->
      <n-space class="trend-view-toggle">
        <n-radio-group v-model:value="showChart" size="small">
          <n-radio-button :value="true">图表</n-radio-button>
          <n-radio-button :value="false">表格</n-radio-button>
        </n-radio-group>
      </n-space>
      
      <!-- Table View -->
      <n-data-table
        v-if="!showChart"
        size="small"
        striped
        :columns="trendColumns"
        :data="trendRows"
        :pagination="{ pageSize: 20 }"
        :scroll-x="1000"
      />
    </template>
  </section>
</template>

<style scoped>
.trend-workbench { padding: 25px; border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.trend-heading { display: flex; align-items: start; justify-content: space-between; gap: 16px; }.trend-heading p { margin: 0; color: #91a097; font-size: 9px; font-weight: 800; letter-spacing: .13em; }.trend-heading h2 { margin: 7px 0 0; font-size: 18px; }.trend-years { width: 80px; }.trend-empty { padding: 40px; }.trend-chart-view { margin-top: 21px; }.trend-metric-picker { margin-bottom: 12px; }.trend-chart-frame { padding: 12px; border-radius: 10px; background: #fafcf9; }.trend-chart-frame svg { display: block; width: 100%; max-width: 800px; height: auto; }.trend-view-toggle { margin: 16px 0 12px; }.trend-workbench :deep(.n-data-table) { border: 1px solid #edf1ee; border-radius: 9px; }
</style>
