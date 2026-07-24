<script setup lang="ts">
import { computed } from 'vue'
import {
  NCard, NSpace, NRadioGroup, NRadioButton, NSelect, NEmpty, NDataTable,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type { TrendResponse, FinancialTrendRow } from '../types/stock-detail.ts'
import { fmt, fmtPct } from '../utils/formatters.ts'

const props = defineProps<{
  readonly trendData: TrendResponse
}>()

const trendPeriod = defineModel<'annual' | 'quarterly' | 'ttm'>('trendPeriod', { required: true })
const trendYears = defineModel<number>('trendYears', { required: true })

const periodOptions = [
  { label: '1年', value: 1 },
  { label: '3年', value: 3 },
  { label: '5年', value: 5 },
  { label: '10年', value: 10 },
  { label: '全部', value: 99 },
]

const trendRows = computed(() => [...props.trendData.trend])

const isEmpty = computed(() => props.trendData.trend.length === 0)

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
    <n-data-table
      v-else
      size="small"
      striped
      :columns="trendColumns"
      :data="trendRows"
      :pagination="{ pageSize: 20 }"
      :scroll-x="1000"
    />
  </n-card>
</template>
