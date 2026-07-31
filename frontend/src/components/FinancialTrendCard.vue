<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  NCard, NSpace, NRadioGroup, NRadioButton, NSelect, NEmpty, NDataTable, NRadio,
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

const metricOptions = [
  { label: '营收', value: 'revenue' },
  { label: '归母净利', value: 'parent_net_profit' },
  { label: '毛利率', value: 'gross_margin' },
  { label: '净利率', value: 'net_margin' },
  { label: 'ROE', value: 'roe' },
  { label: '负债率', value: 'debt_ratio' },
  { label: 'EPS', value: 'basic_eps' },
]

const trendRows = computed(() => [...props.trendData.trend])

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

// X-axis labels (report dates)
const xAxisLabels = computed(() => {
  if (isEmpty.value) return []
  
  const width = 600
  const padding = 40
  const xStep = (width - 2 * padding) / Math.max(chartData.value.points.length - 1, 1)
  
  return chartData.value.points.map((point, index) => ({
    label: point.date?.substring(0, 7) || '',
    x: padding + index * xStep,
  }))
})

const trendColumns: DataTableColumns<FinancialTrendRow> = [
  { title: '报告期', key: 'report_date', width: 110 },
  { title: '营收', key: 'revenue', render: (r) => fmt(r.revenue, 0) },
  { title: '归母净利', key: 'net_profit', render: (r) => fmt(r.net_profit, 0) },
  { title: '扣非净利', key: 'deducted_net_profit', render: (r) => fmt(r.deducted_net_profit, 0) },
  { title: '毛利率', key: 'gross_margin', render: (r) => fmtPct(r.gross_margin) },
  { title: '净利率', key: 'net_margin', render: (r) => fmtPct(r.net_margin) },
  { title: 'ROE', key: 'roe', render: (r) => fmtPct(r.roe) },
  { title: '负债率', key: 'debt_ratio', render: (r) => fmtPct(r.debt_ratio) },
  { title: 'EPS', key: 'basic_eps', render: (r) => fmt(r.basic_eps) },
  { title: '经营CF', key: 'cf_from_operating', render: (r) => fmt(r.cf_from_operating, 0) },
]
</script>

<template>
  <n-card title="财务趋势" size="small" style="margin-bottom: 16px;">
    <template #header-extra>
      <n-space>
        <n-radio-group v-model:value="trendPeriod" size="small">
          <n-radio-button value="annual">年度</n-radio-button>
          <n-radio-button value="quarterly">季度</n-radio-button>
          <n-radio-button value="ttm">TTM</n-radio-button>
        </n-radio-group>
        <n-select v-model:value="trendYears" :options="periodOptions" size="small" style="width:80px;" />
      </n-space>
    </template>
    <n-empty v-if="isEmpty" description="无财务趋势数据" style="padding: 40px;" />
    <template v-else>
      <!-- Chart View -->
      <div v-if="showChart" style="margin-bottom: 16px;">
        <n-space style="margin-bottom: 12px;">
          <n-radio-group v-model:value="chartMetric" size="small">
            <n-radio v-for="opt in metricOptions" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </n-radio>
          </n-radio-group>
        </n-space>
        
        <svg width="600" height="200" style="border: 1px solid #e0e0e0; border-radius: 4px;">
          <!-- Grid lines -->
          <line x1="40" y1="40" x2="560" y2="40" stroke="#f0f0f0" stroke-width="1" />
          <line x1="40" y1="80" x2="560" y2="80" stroke="#f0f0f0" stroke-width="1" />
          <line x1="40" y1="120" x2="560" y2="120" stroke="#f0f0f0" stroke-width="1" />
          <line x1="40" y1="160" x2="560" y2="160" stroke="#f0f0f0" stroke-width="1" />
          
          <!-- Y-axis labels -->
          <text v-for="(label, idx) in yAxisLabels" :key="idx" :x="5" :y="label.y + 4" font-size="10" fill="#666">
            {{ label.value }}
          </text>
          
          <!-- X-axis labels -->
          <text v-for="(label, idx) in xAxisLabels" :key="idx" :x="label.x - 15" y="195" font-size="9" fill="#666">
            {{ label.label }}
          </text>
          
          <!-- Line chart -->
          <path :d="chartPath" fill="none" stroke="#18a058" stroke-width="2" />
          
          <!-- Data points -->
          <circle
            v-for="(point, idx) in chartPoints"
            :key="idx"
            :cx="point.x"
            :cy="point.y"
            r="3"
            fill="#18a058"
          />
        </svg>
      </div>
      
      <!-- Toggle view -->
      <n-space style="margin-bottom: 12px;">
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
  </n-card>
</template>
