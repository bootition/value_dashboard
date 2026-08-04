<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  NCard, NTag, NTabs, NTabPane, NStatistic, NGrid, NGridItem, NDataTable, NSpace, NTooltip, NSelect, NRadioGroup, NRadioButton,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type {
  IndicatorsResponse,
  IndicatorMetric,
  TrendResponse,
  FinancialTrendRow,
} from '../types/stock-detail.ts'
import type { WarningCode } from '../types/data-quality.ts'
import { isIndicatorUntrusted } from '../types/data-quality.ts'
import { fmt, fmtPct } from '../utils/formatters.ts'

const props = defineProps<{
  readonly indicators: IndicatorsResponse | null
  readonly trendData: TrendResponse
  readonly warningCodes?: readonly WarningCode[]
}>()

const emit = defineEmits<{
  (e: 'update:timeDimension', value: string): void
}>()

const timeDimension = ref('current')

const timeDimensionOptions = [
  { label: '当前', value: 'current' },
  { label: '1年', value: '1y' },
  { label: '3年', value: '3y' },
  { label: '5年', value: '5y' },
  { label: '10年', value: '10y' },
  { label: '全部', value: 'all' },
]

function onTimeDimensionChange(value: string) {
  timeDimension.value = value
  emit('update:timeDimension', value)
}

const trendRows = computed(() => [...props.trendData.trend])

function metricValue(metric: IndicatorMetric | null | undefined): number | null {
  if (metric === null || metric === undefined) return null
  return metric.value
}

function isCurrentOnly(metric: IndicatorMetric | null | undefined): boolean {
  if (metric === null || metric === undefined) return false
  return metric.historical_capable === false
}

function isFieldUntrusted(field: string): boolean {
  return isIndicatorUntrusted(field, props.warningCodes ?? [])
}

const customFieldOptions = [
  { label: '营收', value: 'revenue' },
  { label: '归母净利', value: 'parent_net_profit' },
  { label: '毛利率', value: 'gross_margin' },
  { label: '净利率', value: 'net_margin' },
  { label: 'ROE', value: 'roe' },
  { label: '负债率', value: 'debt_ratio' },
  { label: 'EPS', value: 'basic_eps' },
  { label: '经营现金流', value: 'cf_from_operating' },
]

const selectedCustomFields = ref<string[]>(['revenue', 'parent_net_profit', 'gross_margin', 'roe'])

const filteredTrendColumns = computed(() => {
  const baseColumns: DataTableColumns<FinancialTrendRow> = [
    { title: '报告期', key: 'report_date', width: 110 },
  ]
  
  const fieldMap: Record<string, { title: string; key: string; render?: (r: FinancialTrendRow) => string }> = {
    revenue: { title: '营收', key: 'revenue', render: (r) => fmt(r.revenue, 0) },
    parent_net_profit: { title: '归母净利', key: 'parent_net_profit', render: (r) => fmt(r.parent_net_profit ?? null, 0) },
    gross_margin: { title: '毛利率', key: 'gross_margin', render: (r) => fmtPct(r.gross_margin) },
    net_margin: { title: '净利率', key: 'net_margin', render: (r) => fmtPct(r.net_margin) },
    roe: { title: 'ROE', key: 'roe', render: (r) => fmtPct(r.roe) },
    debt_ratio: { title: '负债率', key: 'debt_ratio', render: (r) => fmtPct(r.debt_ratio) },
    basic_eps: { title: 'EPS', key: 'basic_eps', render: (r) => fmt(r.basic_eps) },
    cf_from_operating: { title: '经营现金流', key: 'cf_from_operating', render: (r) => fmt(r.cf_from_operating, 0) },
  }
  
  for (const field of selectedCustomFields.value) {
    if (fieldMap[field]) {
      baseColumns.push(fieldMap[field] as any)
    }
  }
  
  return baseColumns
})
</script>

<template>
  <section class="indicator-workbench">
    <div class="indicator-toolbar">
      <span>时间维度</span>
      <n-radio-group :value="timeDimension" @update:value="onTimeDimensionChange" size="small">
        <n-radio-button v-for="option in timeDimensionOptions" :key="option.value" :value="option.value">
          {{ option.label }}
        </n-radio-button>
      </n-radio-group>
      <n-tag v-if="timeDimension !== 'current'" size="small" type="info">
        显示历史趋势数据
      </n-tag>
    </div>
    
    <n-tabs type="line" style="margin-bottom: 16px;">
      <n-tab-pane name="valuation" tab="估值">
          <n-grid :cols="4" :x-gap="12" :y-gap="12">
          <n-grid-item><n-card size="small"><n-statistic label="市盈率（PE-TTM）" :value="fmt(metricValue(indicators?.indicators?.valuation?.pe_ttm))" /><n-tooltip v-if="isCurrentOnly(indicators?.indicators?.valuation?.pe_ttm)" trigger="hover"><template #trigger><n-tag size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></template>此指标依赖最新收盘价，只能用于当前时点展示，不可生成历史序列</n-tooltip><n-tag v-if="isFieldUntrusted('pe_ttm')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
          <n-grid-item><n-card size="small"><n-statistic label="市净率（PB-MRQ）" :value="fmt(metricValue(indicators?.indicators?.valuation?.pb_mrq))" /><n-tooltip v-if="isCurrentOnly(indicators?.indicators?.valuation?.pb_mrq)" trigger="hover"><template #trigger><n-tag size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></template>此指标依赖最新收盘价，只能用于当前时点展示，不可生成历史序列</n-tooltip><n-tag v-if="isFieldUntrusted('pb_mrq')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
          <n-grid-item><n-card size="small"><n-statistic label="市销率（PS-TTM）" :value="fmt(metricValue(indicators?.indicators?.valuation?.ps_ttm))" /><n-tooltip v-if="isCurrentOnly(indicators?.indicators?.valuation?.ps_ttm)" trigger="hover"><template #trigger><n-tag size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></template>此指标依赖最新收盘价，只能用于当前时点展示，不可生成历史序列</n-tooltip><n-tag v-if="isFieldUntrusted('ps_ttm')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
          <n-grid-item><n-card size="small"><n-statistic label="市现率（PCF-TTM）" :value="fmt(metricValue(indicators?.indicators?.valuation?.pcf_ttm))" /><n-tooltip v-if="isCurrentOnly(indicators?.indicators?.valuation?.pcf_ttm)" trigger="hover"><template #trigger><n-tag size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></template>此指标依赖最新收盘价，只能用于当前时点展示，不可生成历史序列</n-tooltip><n-tag v-if="isFieldUntrusted('pcf_ttm')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
          <n-grid-item><n-card size="small"><n-statistic label="股息率" :value="fmtPct(metricValue(indicators?.indicators?.valuation?.dividend_yield))" /><n-tooltip v-if="isCurrentOnly(indicators?.indicators?.valuation?.dividend_yield)" trigger="hover"><template #trigger><n-tag size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></template>此指标依赖最新收盘价，只能用于当前时点展示，不可生成历史序列</n-tooltip><n-tag v-if="isFieldUntrusted('dividend_yield')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
          <n-grid-item><n-card size="small"><n-statistic label="总市值" :value="fmt(metricValue(indicators?.indicators?.valuation?.total_market_cap), 0)" /><n-tooltip v-if="isCurrentOnly(indicators?.indicators?.valuation?.total_market_cap)" trigger="hover"><template #trigger><n-tag size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></template>此指标依赖最新收盘价，只能用于当前时点展示，不可生成历史序列</n-tooltip><n-tag v-if="isFieldUntrusted('total_market_cap')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
          <n-grid-item><n-card size="small"><n-statistic label="流通市值" :value="fmt(metricValue(indicators?.indicators?.valuation?.circ_market_cap), 0)" /><n-tooltip v-if="isCurrentOnly(indicators?.indicators?.valuation?.circ_market_cap)" trigger="hover"><template #trigger><n-tag size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></template>此指标依赖最新收盘价，只能用于当前时点展示，不可生成历史序列</n-tooltip><n-tag v-if="isFieldUntrusted('circ_market_cap')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        </n-grid>
      </n-tab-pane>
    <n-tab-pane name="profitability" tab="盈利">
      <n-grid :cols="4" :x-gap="12" :y-gap="12">
        <n-grid-item><n-card size="small"><n-statistic label="净资产收益率（ROE）" :value="fmtPct(metricValue(indicators?.indicators?.profitability?.roe))" /><n-tag v-if="isFieldUntrusted('roe')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="总资产收益率（ROA）" :value="fmtPct(metricValue(indicators?.indicators?.profitability?.roa))" /><n-tag v-if="isFieldUntrusted('roa')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="毛利率" :value="fmtPct(metricValue(indicators?.indicators?.profitability?.gross_margin))" /><n-tag v-if="isFieldUntrusted('gross_margin')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="净利率" :value="fmtPct(metricValue(indicators?.indicators?.profitability?.net_margin))" /><n-tag v-if="isFieldUntrusted('net_margin')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="ROIC" :value="fmtPct(metricValue(indicators?.indicators?.profitability?.roic))" /><n-tag v-if="isFieldUntrusted('roic')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="CF/净利润" :value="fmt(metricValue(indicators?.indicators?.profitability?.cf_to_net_profit))" /><n-tag v-if="isFieldUntrusted('cf_to_net_profit')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
      </n-grid>
    </n-tab-pane>
    <n-tab-pane name="growth" tab="成长">
      <n-grid :cols="4" :x-gap="12" :y-gap="12">
        <n-grid-item><n-card size="small"><n-statistic label="营收YoY" :value="fmtPct(metricValue(indicators?.indicators?.growth?.revenue_yoy))" /><n-tag v-if="isFieldUntrusted('revenue_yoy')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="净利YoY" :value="fmtPct(metricValue(indicators?.indicators?.growth?.net_profit_yoy))" /><n-tag v-if="isFieldUntrusted('net_profit_yoy')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="扣非YoY" :value="fmtPct(metricValue(indicators?.indicators?.growth?.deducted_profit_yoy))" /><n-tag v-if="isFieldUntrusted('deducted_profit_yoy')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="营收CAGR3" :value="fmtPct(metricValue(indicators?.indicators?.growth?.revenue_cagr3))" /><n-tag v-if="isFieldUntrusted('revenue_cagr3')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="营收CAGR5" :value="fmtPct(metricValue(indicators?.indicators?.growth?.revenue_cagr5))" /><n-tag v-if="isFieldUntrusted('revenue_cagr5')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="净利CAGR5" :value="fmtPct(metricValue(indicators?.indicators?.growth?.net_profit_cagr5))" /><n-tag v-if="isFieldUntrusted('net_profit_cagr5')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
      </n-grid>
    </n-tab-pane>
    <n-tab-pane name="safety" tab="安全">
      <n-grid :cols="4" :x-gap="12" :y-gap="12">
        <n-grid-item><n-card size="small"><n-statistic label="资产负债率" :value="fmtPct(metricValue(indicators?.indicators?.safety?.debt_ratio))" /><n-tag v-if="isFieldUntrusted('debt_ratio')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="流动比率" :value="fmt(metricValue(indicators?.indicators?.safety?.current_ratio))" /><n-tag v-if="isFieldUntrusted('current_ratio')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="速动比率" :value="fmt(metricValue(indicators?.indicators?.safety?.quick_ratio))" /><n-tag v-if="isFieldUntrusted('quick_ratio')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="有息负债" :value="fmt(metricValue(indicators?.indicators?.safety?.interest_bearing_debt), 0)" /><n-tag v-if="isFieldUntrusted('interest_bearing_debt')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="利息保障倍数" :value="fmt(metricValue(indicators?.indicators?.safety?.interest_coverage))" /><n-tag v-if="isFieldUntrusted('interest_coverage')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="商誉占比" :value="fmtPct(metricValue(indicators?.indicators?.safety?.goodwill_ratio))" /><n-tag v-if="isFieldUntrusted('goodwill_ratio')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
      </n-grid>
    </n-tab-pane>
    <n-tab-pane name="return" tab="股东回报">
      <n-grid :cols="4" :x-gap="12" :y-gap="12">
        <n-grid-item><n-card size="small"><n-statistic label="分红率" :value="fmtPct(metricValue(indicators?.indicators?.shareholder_return?.payout_ratio))" /><n-tag v-if="isFieldUntrusted('payout_ratio')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="每股股息" :value="fmt(metricValue(indicators?.indicators?.shareholder_return?.dps))" /><n-tag v-if="isFieldUntrusted('dps')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="连续分红年数" :value="metricValue(indicators?.indicators?.shareholder_return?.consecutive_div_years) ?? '—'" /><n-tag v-if="isFieldUntrusted('consecutive_div_years')" size="tiny" type="error" style="margin-top:4px">数据不可信</n-tag></n-card></n-grid-item>
      </n-grid>
    </n-tab-pane>
    <n-tab-pane name="custom" tab="自定义指标">
      <n-space vertical>
        <n-space>
          <span style="color:#999;font-size:12px;">选择字段查看趋势:</span>
          <n-select
            v-model:value="selectedCustomFields"
            :options="customFieldOptions"
            multiple
            size="small"
            style="width: 400px"
            placeholder="选择要显示的字段"
          />
        </n-space>
        <n-data-table
          size="small"
          striped
          :columns="filteredTrendColumns"
          :data="trendRows"
          :pagination="{ pageSize: 20 }"
          :scroll-x="800"
        />
      </n-space>
    </n-tab-pane>
  </n-tabs>
   </section>
</template>

<style scoped>
.indicator-workbench { padding: 25px; border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.indicator-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; color: #839087; font-size: 11px; }.indicator-workbench :deep(.n-tabs-nav) { margin-bottom: 16px; }.indicator-workbench :deep(.n-card) { border-radius: 10px; box-shadow: none; background: #fafcf9; }.indicator-workbench :deep(.n-statistic__label) { color: #7e8c82; font-size: 10px; }.indicator-workbench :deep(.n-statistic__value) { color: #3c5847; font-size: 19px; }.indicator-workbench :deep(.n-radio-button--checked) { color: #4d956b; }
</style>
