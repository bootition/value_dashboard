<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  NCard,
  NButton,
  NDataTable,
  NTag,
  NSpace,
  NModal,
  NForm,
  NFormItem,
  NInput,
  NGrid,
  NGridItem,
  NStatistic,
  NAlert,
  useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type { ScreeningResult, WarningCode, ScreeningRuleNode, ScreeningSaveResponse, ScreeningExportResponse, ScreeningWatchlistResponse } from '../types/screening.ts'
import axios, { isAxiosError } from 'axios'

const props = defineProps<{
  results: readonly ScreeningResult[]
  executionTime: number
  basePoolSize: number
  dataDate: string | null
  warningCodes: readonly WarningCode[]
  untrustedFields: readonly string[]
  qualityStatus: 'loading' | 'available' | 'failed'
  ruleTree: ScreeningRuleNode
  sortField: string
  sortDirection: 'asc' | 'desc'
}>()

const emit = defineEmits<{
  // refresh emit removed — parent handles refreshes independently
}>()

const message = useMessage()
const showSaveDialog = ref(false)
const saveTitle = ref('')
const saveNote = ref('')

const hasWarnings = computed(() => props.warningCodes.length > 0)
const hasUntrustedFields = computed(() => props.untrustedFields.length > 0)
const qualityUnavailable = computed(() => props.qualityStatus !== 'available')
const OPERATIONS_ONLY_CODES = new Set(['STALE_RUNNING_JOBS', 'UNPUBLISHED_OVERRIDES'])
const operationsOnlyWarnings = computed(
  () =>
    props.warningCodes.length > 0 &&
    props.warningCodes.every((c) => OPERATIONS_ONLY_CODES.has(c)),
)
const durableActionsDisabled = computed(
  () => (hasUntrustedFields.value || qualityUnavailable.value) && props.results.length > 0,
)

const tableColumns = computed(() => {
  if (!props.results.length) return []
  const firstRow = props.results[0]
  const keys = Object.keys(firstRow).filter((k) => !k.startsWith('_'))
  const cols: DataTableColumns<ScreeningResult> = keys.map((key) => ({
    title: key,
    key,
    sorter: 'default',
    render(row) {
      const v = row[key]
      if (v === null || v === undefined) return '—'
      if (typeof v === 'number') {
        return Math.abs(v) < 0.01 && v !== 0
          ? v.toExponential(2)
          : Math.abs(v) >= 1000
            ? v.toFixed(0)
            : v.toFixed(4)
      }
      return String(v)
    },
  }))
  if (firstRow._entry_explanation !== undefined) {
    cols.push({
      title: '入选解释',
      key: '_entry_explanation',
      width: 300,
      render(row) {
        const v = row._entry_explanation
        return v != null ? String(v) : '—'
      },
    })
  }
  return cols
})

async function saveResults() {
  if (durableActionsDisabled.value) {
    message.warning('当前结果包含不可信字段或质量状态未知，无法保存')
    return
  }
  if (!saveTitle.value.trim()) {
    message.error('标题必填')
    return
  }
  try {
    await axios.post<ScreeningSaveResponse>('/api/screening/save', {
      title: saveTitle.value,
      note: saveNote.value || null,
      rule_json: { conditions: props.ruleTree },
      results: props.results,
      columns: Object.keys(props.results[0] || {}).filter((k) => !k.startsWith('_')),
      sort: [{ field: props.sortField, direction: props.sortDirection }],
      data_date: props.dataDate,
    })
    message.success('结果已保存')
    showSaveDialog.value = false
    saveTitle.value = ''
    saveNote.value = ''
  } catch (e: unknown) {
    const errMsg = isAxiosError(e) ? e.response?.data?.detail ?? e.message : e instanceof Error ? e.message : '未知错误'
    message.error(`保存失败: ${errMsg}`)
  }
}

async function exportCSV() {
  if (durableActionsDisabled.value) {
    message.warning('当前结果包含不可信字段或质量状态未知，无法导出')
    return
  }
  try {
    const resp = await axios.post<ScreeningExportResponse>('/api/screening/export_csv', {
      results: props.results,
      columns: Object.keys(props.results[0] || {}).filter((k) => !k.startsWith('_')),
      data_date: props.dataDate,
    })
    const blob = new Blob(['\ufeff' + resp.data.csv], { type: 'text/csv;charset=utf-8;bom' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `screening_${Date.now()}.csv`
    a.click()
    URL.revokeObjectURL(url)
    message.success(`已导出 ${resp.data.rows} 条`)
  } catch (e: unknown) {
    const errMsg = isAxiosError(e) ? e.response?.data?.detail ?? e.message : e instanceof Error ? e.message : '未知错误'
    message.error(`导出失败: ${errMsg}`)
  }
}

async function addToWatchlist() {
  const codes = props.results.map((r) => r.stock_code)
  if (!codes.length) return
  try {
    const resp = await axios.post<ScreeningWatchlistResponse>('/api/screening/add_to_watchlist', {
      stock_codes: codes,
      group: 'screening',
    })
    message.success(`已加入自选: ${resp.data.added} 只`)
  } catch (e: unknown) {
    const errMsg = isAxiosError(e) ? e.response?.data?.detail ?? e.message : e instanceof Error ? e.message : '未知错误'
    message.error(`加入自选失败: ${errMsg}`)
  }
}
</script>

<template>
  <div v-if="results.length > 0">
    <n-alert
      v-if="hasWarnings || qualityStatus === 'failed' || qualityStatus === 'loading'"
      id="quality-alert"
      :type="qualityStatus === 'loading' ? 'info' : qualityStatus === 'failed' ? 'error' : 'warning'"
      :title="qualityStatus === 'loading' ? '质量数据加载中' : qualityStatus === 'failed' ? '质量状态加载失败' : `数据质量警告（${warningCodes.length}）`"
      style="margin-bottom: 16px"
    >
      <template v-if="qualityStatus === 'loading'">
        质量状态数据正在加载，保存和导出功能暂时不可用。请稍候。
      </template>
      <template v-else-if="qualityStatus === 'failed'">
        无法获取数据质量状态。为确保数据可信，保存和导出功能已禁用。请刷新页面重试。
      </template>
      <template v-else>
        <n-space>
          <n-tag v-for="code in warningCodes" :key="code" size="small" type="warning">{{ code }}</n-tag>
        </n-space>
        <div v-if="hasUntrustedFields" style="margin-top: 8px; color: #d03050">
          受影响字段：{{ untrustedFields.join(', ') }}。保存和导出已禁用。
        </div>
        <div v-if="qualityUnavailable" style="margin-top: 8px; color: #d03050">
          质量状态未知，保存和导出已禁用。
        </div>
        <div v-if="operationsOnlyWarnings" style="margin-top: 8px; color: #888">
          当前仅运行类警告，不影响保存和导出。
        </div>
      </template>
    </n-alert>

    <n-grid :cols="4" :x-gap="16" style="margin-bottom: 16px">
      <n-grid-item><n-card><n-statistic label="结果数" :value="results.length" /></n-card></n-grid-item>
      <n-grid-item><n-card><n-statistic label="基础池" :value="basePoolSize" /></n-card></n-grid-item>
      <n-grid-item><n-card><n-statistic label="耗时(ms)" :value="executionTime" /></n-card></n-grid-item>
      <n-grid-item><n-card><n-statistic label="数据日期"><template #default><span>{{ dataDate || '—' }}</span><n-tag v-if="hasWarnings" size="small" type="warning" style="margin-left: 8px">{{ warningCodes.length }} 个警告</n-tag></template></n-statistic></n-card></n-grid-item>
    </n-grid>

    <n-space style="margin-bottom: 16px">
      <n-button id="save-btn" :disabled="durableActionsDisabled" :aria-describedby="durableActionsDisabled ? 'quality-alert' : undefined" @click="showSaveDialog = true">保存结果</n-button>
      <n-button id="export-btn" :disabled="durableActionsDisabled" :aria-describedby="durableActionsDisabled ? 'quality-alert' : undefined" @click="exportCSV">导出CSV</n-button>
      <n-button @click="addToWatchlist">加入自选</n-button>
    </n-space>

    <n-data-table
      :columns="tableColumns"
      :data="[...results]"
      :max="5000"
      :pagination="{ pageSize: 50 }"
      :scroll-x="1200"
      size="small"
      striped
    />

    <n-modal v-model:show="showSaveDialog" title="保存筛选结果" preset="dialog">
      <n-form>
        <n-form-item label="标题(必填)">
          <n-input v-model:value="saveTitle" placeholder="给这次筛选结果起个名字" />
        </n-form-item>
        <n-form-item label="备注(可选)">
          <n-input v-model:value="saveNote" type="textarea" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showSaveDialog = false">取消</n-button>
        <n-button
          type="primary"
          :disabled="durableActionsDisabled"
          :aria-describedby="durableActionsDisabled ? 'quality-alert' : undefined"
          @click="saveResults"
        >
          保存
        </n-button>
      </template>
    </n-modal>
  </div>
</template>
