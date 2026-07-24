<script setup lang="ts">
import { computed } from 'vue'
import {
  NCard, NTag, NTabs, NTabPane, NStatistic, NGrid, NGridItem, NDataTable, NSpace,
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

const trendColumns: DataTableColumns<FinancialTrendRow> = [
  { title: '报告期', key: 'report_date', width: 110 },
  { title: '营收', key: 'revenue', render: (r) => fmt(r.revenue, 0) },
  { title: '归母净利', key: 'parent_net_profit', render: (r) => fmt(r.parent_net_profit ?? null, 0) },
  { title: '毛利率', key: 'gross_margin', render: (r) => fmtPct(r.gross_margin) },
  { title: 'ROE', key: 'roe', render: (r) => fmtPct(r.roe) },
]
</script>

<template>
  <n-tabs type="line" style="margin-bottom: 16px;">
    <n-tab-pane name="valuation" tab="估值">
      <n-grid :cols="4" :x-gap="12" :y-gap="12">
        <n-grid-item><n-card size="small"><n-statistic label="PE-TTM" :value="fmt(metricValue(indicators?.indicators?.valuation?.pe_ttm))" /><n-tag v-if="isCurrentOnly(indicators?.indicators?.valuation?.pe_ttm)" size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="PB-MRQ" :value="fmt(metricValue(indicators?.indicators?.valuation?.pb_mrq))" /><n-tag v-if="isCurrentOnly(indicators?.indicators?.valuation?.pb_mrq)" size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="PS-TTM" :value="fmt(metricValue(indicators?.indicators?.valuation?.ps_ttm))" /><n-tag v-if="isCurrentOnly(indicators?.indicators?.valuation?.ps_ttm)" size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="PCF-TTM" :value="fmt(metricValue(indicators?.indicators?.valuation?.pcf_ttm))" /><n-tag v-if="isCurrentOnly(indicators?.indicators?.valuation?.pcf_ttm)" size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="股息率" :value="fmtPct(metricValue(indicators?.indicators?.valuation?.dividend_yield))" /><n-tag v-if="isCurrentOnly(indicators?.indicators?.valuation?.dividend_yield)" size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag><n-tag v-if="isFieldUntrusted('dividend_yield')" size="tiny" type="warning" style="margin-top:4px">数据未验证</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="总市值" :value="fmt(metricValue(indicators?.indicators?.valuation?.total_market_cap), 0)" /><n-tag v-if="isCurrentOnly(indicators?.indicators?.valuation?.total_market_cap)" size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="流通市值" :value="fmt(metricValue(indicators?.indicators?.valuation?.circ_market_cap), 0)" /><n-tag v-if="isCurrentOnly(indicators?.indicators?.valuation?.circ_market_cap)" size="tiny" type="warning" style="margin-top:4px">仅当前</n-tag></n-card></n-grid-item>
      </n-grid>
    </n-tab-pane>
    <n-tab-pane name="profitability" tab="盈利">
      <n-grid :cols="4" :x-gap="12" :y-gap="12">
        <n-grid-item><n-card size="small"><n-statistic label="ROE" :value="fmtPct(metricValue(indicators?.indicators?.profitability?.roe))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="ROA" :value="fmtPct(metricValue(indicators?.indicators?.profitability?.roa))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="毛利率" :value="fmtPct(metricValue(indicators?.indicators?.profitability?.gross_margin))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="净利率" :value="fmtPct(metricValue(indicators?.indicators?.profitability?.net_margin))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="ROIC" :value="fmtPct(metricValue(indicators?.indicators?.profitability?.roic))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="CF/净利润" :value="fmt(metricValue(indicators?.indicators?.profitability?.cf_to_net_profit))" /></n-card></n-grid-item>
      </n-grid>
    </n-tab-pane>
    <n-tab-pane name="growth" tab="成长">
      <n-grid :cols="4" :x-gap="12" :y-gap="12">
        <n-grid-item><n-card size="small"><n-statistic label="营收YoY" :value="fmtPct(metricValue(indicators?.indicators?.growth?.revenue_yoy))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="净利YoY" :value="fmtPct(metricValue(indicators?.indicators?.growth?.net_profit_yoy))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="扣非YoY" :value="fmtPct(metricValue(indicators?.indicators?.growth?.deducted_profit_yoy))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="营收CAGR3" :value="fmtPct(metricValue(indicators?.indicators?.growth?.revenue_cagr3))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="营收CAGR5" :value="fmtPct(metricValue(indicators?.indicators?.growth?.revenue_cagr5))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="净利CAGR5" :value="fmtPct(metricValue(indicators?.indicators?.growth?.net_profit_cagr5))" /></n-card></n-grid-item>
      </n-grid>
    </n-tab-pane>
    <n-tab-pane name="safety" tab="安全">
      <n-grid :cols="4" :x-gap="12" :y-gap="12">
        <n-grid-item><n-card size="small"><n-statistic label="资产负债率" :value="fmtPct(metricValue(indicators?.indicators?.safety?.debt_ratio))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="流动比率" :value="fmt(metricValue(indicators?.indicators?.safety?.current_ratio))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="速动比率" :value="fmt(metricValue(indicators?.indicators?.safety?.quick_ratio))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="有息负债" :value="fmt(metricValue(indicators?.indicators?.safety?.interest_bearing_debt), 0)" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="利息保障倍数" :value="fmt(metricValue(indicators?.indicators?.safety?.interest_coverage))" /></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="商誉占比" :value="fmtPct(metricValue(indicators?.indicators?.safety?.goodwill_ratio))" /></n-card></n-grid-item>
      </n-grid>
    </n-tab-pane>
    <n-tab-pane name="return" tab="股东回报">
      <n-grid :cols="4" :x-gap="12" :y-gap="12">
        <n-grid-item><n-card size="small"><n-statistic label="分红率" :value="fmtPct(metricValue(indicators?.indicators?.shareholder_return?.payout_ratio))" /><n-tag v-if="isFieldUntrusted('payout_ratio')" size="tiny" type="warning" style="margin-top:4px">数据未验证</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="每股股息" :value="fmt(metricValue(indicators?.indicators?.shareholder_return?.dps))" /><n-tag v-if="isFieldUntrusted('dps')" size="tiny" type="warning" style="margin-top:4px">数据未验证</n-tag></n-card></n-grid-item>
        <n-grid-item><n-card size="small"><n-statistic label="连续分红年数" :value="metricValue(indicators?.indicators?.shareholder_return?.consecutive_div_years) ?? '—'" /><n-tag v-if="isFieldUntrusted('consecutive_div_years')" size="tiny" type="warning" style="margin-top:4px">数据未验证</n-tag></n-card></n-grid-item>
      </n-grid>
    </n-tab-pane>
    <n-tab-pane name="custom" tab="自定义指标">
      <n-space vertical>
        <n-space>
          <span style="color:#999;font-size:12px;">选择字段查看趋势（逗号分隔）:</span>
        </n-space>
        <n-data-table
          size="small"
          striped
          :columns="trendColumns"
          :data="trendRows"
          :pagination="{ pageSize: 20 }"
          :scroll-x="800"
        />
      </n-space>
    </n-tab-pane>
  </n-tabs>
</template>
