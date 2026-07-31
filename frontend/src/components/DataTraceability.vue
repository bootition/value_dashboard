<script setup lang="ts">
import { computed, ref } from 'vue'
import { NCard, NButton, NEmpty, NDataTable, NSpace, NModal, NList, NListItem, NTag, useMessage } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type { AuditResponse, AuditFieldRow, AuditBatchRow } from '../types/stock-detail.ts'
import { fmt } from '../utils/formatters.ts'
import axios from 'axios'

const props = defineProps<{
  readonly stockCode: string
  readonly auditData: AuditResponse
}>()

const message = useMessage()
const showPdfModal = ref(false)
const pdfList = ref<Array<{
  filename: string
  size_bytes: number
  archived?: boolean
  archive_path?: string
  checksum?: string
  integrity_verified?: boolean
}>>([])
const loadingPdfs = ref(false)

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
  { title: '生效日', key: 'effective_date', width: 110 },
  { title: '版本', key: 'data_version', width: 100 },
  { title: '公式', key: 'formula', ellipsis: { tooltip: true }, minWidth: 180 },
]

const batchColumns: DataTableColumns<AuditBatchRow> = [
  { title: '数据类型', key: 'data_type', width: 150 },
  { title: '来源', key: 'source', width: 120 },
  { title: '行数', key: 'row_count', width: 80 },
  { title: '置信度', key: 'confidence', width: 80 },
  { title: '抓取时间', key: 'fetch_time', width: 160 },
]

async function loadPdfList() {
  loadingPdfs.value = true
  try {
    const resp = await axios.get(`/api/stock/${props.stockCode}/pdf-list`)
    pdfList.value = resp.data.files || []
  } catch {
    message.error('加载PDF列表失败')
  } finally {
    loadingPdfs.value = false
  }
}

function openPdfModal() {
  showPdfModal.value = true
  loadPdfList()
}

function viewPdf(filename: string, archived = false) {
  if (archived) {
    message.info('该 PDF 位于冷归档，请按页面中的恢复指引通过 CLI 恢复后查看')
    return
  }
  window.open(`/api/stock/${props.stockCode}/pdf/${filename}`, '_blank')
}

function restoreCommand(filename: string): string {
  return `python -m app.cli.main data restore_pdf ${props.stockCode} ${filename}`
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
}
</script>

<template>
  <n-card title="数据溯源" size="small">
    <template #header-extra>
      <n-space>
        <n-button size="small" @click="openPdfModal">PDF管理</n-button>
      </n-space>
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

    <!-- PDF Management Modal -->
    <n-modal v-model:show="showPdfModal" preset="dialog" title="PDF公告管理" style="width: 600px;">
      <n-space vertical>
        <n-empty v-if="pdfList.length === 0 && !loadingPdfs" description="暂无PDF文件" />
        <n-list v-else bordered>
          <n-list-item v-for="pdf in pdfList" :key="pdf.filename">
            <n-space justify="space-between" align="center" style="width: 100%;">
              <n-space>
                <n-tag size="small" type="info">PDF</n-tag>
                <n-tag v-if="pdf.archived" size="small" type="warning">冷归档</n-tag>
                <span>{{ pdf.filename }}</span>
                <span style="color: #999; font-size: 12px;">{{ formatFileSize(pdf.size_bytes) }}</span>
                <span v-if="pdf.archived" style="color: #999; font-size: 12px;">{{ pdf.archive_path }}</span>
                <span v-if="pdf.checksum" style="color: #999; font-size: 12px;">SHA-256: {{ pdf.checksum }}</span>
                <code v-if="pdf.archived" style="color: #666; font-size: 11px;">{{ restoreCommand(pdf.filename) }}</code>
              </n-space>
              <n-button size="tiny" type="primary" @click="viewPdf(pdf.filename, pdf.archived)">{{ pdf.archived ? '恢复指引' : '查看' }}</n-button>
            </n-space>
          </n-list-item>
        </n-list>
        <n-space justify="end">
          <n-button @click="showPdfModal = false">关闭</n-button>
        </n-space>
      </n-space>
    </n-modal>
  </n-card>
</template>
