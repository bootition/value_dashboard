<script setup lang="ts">
import { NTag } from 'naive-ui'
import type { StockFreshness } from '../types/data-quality.ts'

defineProps<{
  readonly freshness: StockFreshness | null
}>()

function display(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return String(value)
}
</script>

<template>
  <section class="freshness-workbench">
    <div class="freshness-heading"><div><p>DATA FRESHNESS</p><h2>数据时间</h2></div><n-tag v-if="freshness?.stale_warning" size="small" type="warning">数据滞后</n-tag><n-tag v-else-if="freshness" size="small" type="success">数据可用</n-tag></div>
    <div class="freshness-grid">
      <div><span>价格日期</span><strong>{{ display(freshness?.price_date) }}</strong><small>距今天 {{ display(freshness?.price_age_days) }} 天</small></div>
      <div><span>财务生效日期</span><strong>{{ display(freshness?.financial_effective_date) }}</strong><small>距今天 {{ display(freshness?.financial_age_days) }} 天</small></div>
      <div><span>指标计算时间</span><strong>{{ display(freshness?.calculated_at) }}</strong><small>快照距今天 {{ display(freshness?.snapshot_age_days) }} 天</small></div>
      <div><span>数据版本</span><strong>{{ display(freshness?.data_version) }}</strong><small>整体滞后 {{ display(freshness?.stale_days) }} 天</small></div>
    </div>
  </section>
</template>

<style scoped>
.freshness-workbench { padding: 25px; border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.freshness-heading { display: flex; justify-content: space-between; align-items: start; gap: 16px; }.freshness-heading p { margin: 0; color: #91a097; font-size: 9px; font-weight: 800; letter-spacing: .13em; }.freshness-heading h2 { margin: 7px 0 0; font-size: 18px; }.freshness-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 20px; }.freshness-grid div { min-width: 0; padding: 13px; border-radius: 9px; background: #fafcf9; }.freshness-grid span, .freshness-grid small { display: block; color: #8a978e; font-size: 10px; }.freshness-grid strong { display: block; overflow: hidden; margin: 5px 0 3px; color: #405a49; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
</style>
