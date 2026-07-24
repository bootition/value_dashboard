<script setup lang="ts">
import { NCard, NDescriptions, NDescriptionsItem, NTag, NSpace } from 'naive-ui'
import type { StockFreshness } from '../types/data-quality.ts'

const props = defineProps<{
  readonly freshness: StockFreshness | null
}>()

function display(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return String(value)
}
</script>

<template>
  <n-card title="数据新鲜度" size="small" style="margin-bottom: 16px;">
    <n-descriptions :column="3" size="small" label-placement="left">
      <n-descriptions-item label="财务生效日期">
        {{ display(freshness?.financial_effective_date) }}
      </n-descriptions-item>
      <n-descriptions-item label="价格日期">
        {{ display(freshness?.price_date) }}
      </n-descriptions-item>
      <n-descriptions-item label="计算时间">
        {{ display(freshness?.calculated_at) }}
      </n-descriptions-item>
      <n-descriptions-item label="数据版本">
        {{ display(freshness?.data_version) }}
      </n-descriptions-item>
      <n-descriptions-item label="滞后天数">
        {{ display(freshness?.stale_days) }}
      </n-descriptions-item>
      <n-descriptions-item label="滞后警告">
        <n-space align="center" :size="8">
          <span>{{ freshness?.stale_warning ? '是' : '否' }}</span>
          <n-tag v-if="freshness?.stale_warning" size="small" type="warning">数据滞后</n-tag>
        </n-space>
      </n-descriptions-item>
    </n-descriptions>
  </n-card>
</template>
