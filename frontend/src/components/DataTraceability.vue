<script setup lang="ts">
import { computed } from 'vue'
import { NCard, NButton, NEmpty, NDataTable } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type { AuditResponse, AuditFieldRow, AuditBatchRow } from '../types/stock-detail.ts'
import { fmt } from '../utils/formatters.ts'

const props = defineProps<{
  readonly stockCode: string
  readonly auditData: AuditResponse
}>()

const hasFieldAudit = computed(() => props.auditData.field_audit.length > 0)
const hasBatchAudit = computed(() => props.auditData.batch_audit.length > 0)
const isEmpty = computed(() => !hasFieldAudit.value && !hasBatchAudit.value)

/** Naive UI data prop expects mutable arrays; spread readonly source. */
const fieldRows = computed(() => [...props.auditData.field_audit])
const batchRows = computed(() => [...props.auditData.batch_audit])

const fieldColumns: DataTableColumns<AuditFieldRow> = [
  { title: '字段', key: 'field_name', width: 150 },
  { title: '报告期', key: 'report_date', width: 110 },
  { title: '值', key: 'value', render: (r) => fmt(r.value, 4) },
  { title: '来源', key: 'source', width: 120 },
  {
    title: '置信度',
    key: 'confidence',
    width: 80,
    render: (r) => (r.confidence === 'strict' ? 'strict' : 'approx'),
  },
  { title: '抓取时间', key: 'fetch_time', width: 160 },
]

const batchColumns: DataTableColumns<AuditBatchRow> = [
  { title: '数据类型', key: 'data_type', width: 150 },
  { title: '来源', key: 'source', width: 120 },
  { title: '行数', key: 'row_count', width: 80 },
  { title: '置信度', key: 'confidence', width: 80 },
  { title: '抓取时间', key: 'fetch_time', width: 160 },
]

const pdfHref = computed(() => `/api/stock/${props.stockCode}/pdf-list`)
</script>

<template>
  <n-card title="数据溯源" size="small">
    <template #header-extra>
      <n-button size="small" tag="a" :href="pdfHref" target="_blank">PDF列表</n-button>
    </template>
    <n-empty v-if="isEmpty" description="无溯源数据" style="padding: 20px;" />
    <template v-else>
      <h4 style="margin: 0 0 8px;">关键字段溯源</h4>
      <n-data-table
        v-if="hasFieldAudit"
        size="small"
        striped
        :columns="fieldColumns"
        :data="fieldRows"
        :pagination="{ pageSize: 10 }"
      />
      <h4 style="margin: 16px 0 8px;">批次溯源</h4>
      <n-data-table
        v-if="hasBatchAudit"
        size="small"
        striped
        :columns="batchColumns"
        :data="batchRows"
        :pagination="{ pageSize: 10 }"
      />
    </template>
  </n-card>
</template>
