<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
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
  NSelect,
  useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type { ScreeningResult, WarningCode, ScreeningRuleNode, ScreeningExportResponse, ScreeningWatchlistResponse } from '../types/screening.ts'
import axios from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import { fieldTitleWithUnit, formatFieldValue } from '../utils/screening-format.ts'

const props = defineProps<{
  results: readonly ScreeningResult[]
  strictOnly: boolean
  executionTime: number
  basePoolSize: number
  dataDate: string | null
  warningCodes: readonly WarningCode[]
  untrustedFields: readonly string[]
  qualityStatus: 'loading' | 'available' | 'failed'
  ruleTree: ScreeningRuleNode
  runId: string | null
  ruleId: number | null
  ruleVersion: number | null
  lockedIndicators: Record<string, unknown>
  sort: ReadonlyArray<{ field: string; direction: 'asc' | 'desc' }>
  basePoolConfig: Record<string, unknown>
  // P1-C: 匹配数超过 5000 行上限时服务端显式标记，前端必须警示而非静默丢尾
  truncated?: boolean
  totalMatched?: number
}>()

const message = useMessage()
const router = useRouter()
const showSaveDialog = ref(false)
const saveTitle = ref('')
const saveNote = ref('')
const savedResultId = ref<number | null>(null)

watch(() => props.runId, () => {
  savedResultId.value = null
})

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
  () => (hasUntrustedFields.value || qualityUnavailable.value || props.ruleId === null || props.runId === null) && props.results.length > 0,
)

// L0-3（报告42）: 展示以服务端 run 结果为准。strict_only 切换时由 ScreeningPage
// 重新运行（服务端过滤），前端不再做二次客户端过滤，保证屏幕/保存/CSV 三者一致。
const displayedResults = computed(() => props.results)

const allAvailableColumns = computed(() => {
  if (!displayedResults.value.length) return []
  const firstRow = displayedResults.value[0]
  return Object.keys(firstRow).filter((k) => !k.startsWith('_'))
})

const selectedColumns = ref<string[]>([])

watch(allAvailableColumns, (newCols) => {
  if (selectedColumns.value.length === 0 && newCols.length > 0) {
    selectedColumns.value = [...newCols]
  }
}, { immediate: true })

const columnOptions = computed(() => {
  const labelMap: Record<string, string> = {
    stock_code: '股票代码',
    name: '名称',
    exchange: '交易所',
    csrc_l1: '证监会一级',
    latest_close: '最新价',
    pe_ttm: 'PE-TTM',
    pb_mrq: 'PB-MRQ',
    ps_ttm: 'PS-TTM',
    pcf_ttm: 'PCF-TTM',
    dividend_yield: '股息率',
    total_market_cap: '总市值',
    roe: 'ROE',
    roa: 'ROA',
    gross_margin: '毛利率',
    net_margin: '净利率',
    debt_ratio: '资产负债率',
    current_ratio: '流动比率',
    quick_ratio: '速动比率',
    revenue_yoy: '营收同比',
    net_profit_yoy: '净利润同比',
  }
  return allAvailableColumns.value.map(col => ({
    // L0-2: 表头带单位，口径与自选/详情一致
    label: fieldTitleWithUnit(col, labelMap[col] || col),
    value: col,
  }))
})

const tableColumns = computed(() => {
  if (!displayedResults.value.length) return []
  const firstRow = displayedResults.value[0]
  const cols: DataTableColumns<ScreeningResult> = []
  
  for (const key of selectedColumns.value) {
    if (key in firstRow) {
      cols.push({
        title: columnOptions.value.find(o => o.value === key)?.label || key,
        key,
        sorter: 'default',
        render(row) {
          const v = row[key]
          if (key === 'stock_code' && typeof v === 'string') {
            return h(NButton, {
              text: true,
              type: 'primary',
              title: `查看 ${v} 详情`,
              onClick: () => router.push(`/stock/${encodeURIComponent(v)}`),
            }, () => v)
          }
          // L0-2: 统一字段口径格式化（百分比/价格/比值/市值）
          return formatFieldValue(key, v)
        },
      })
    }
  }
  
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
    const response = await axios.post<{ status: string; result_id: number }>('/api/screening/save', {
      title: saveTitle.value,
      note: saveNote.value || null,
      run_id: props.runId,
      columns: selectedColumns.value,
    })
    savedResultId.value = response.data.result_id
    message.success('结果已保存')
    showSaveDialog.value = false
    saveTitle.value = ''
    saveNote.value = ''
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '保存失败'))
  }
}

async function exportCSV() {
  if (durableActionsDisabled.value) {
    message.warning('当前结果包含不可信字段或质量状态未知，无法导出')
    return
  }
  if (savedResultId.value === null) {
    message.warning('请先保存当前筛选结果，再导出 CSV')
    return
  }
  try {
    const resp = await axios.post<ScreeningExportResponse>('/api/screening/export_csv', {
      result_id: savedResultId.value,
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
    message.error(friendlyErrorMessage(e, '导出失败'))
  }
}

async function addToWatchlist() {
  if (savedResultId.value === null) {
    message.warning('请先保存当前筛选结果，再加入自选')
    return
  }
  const codes = displayedResults.value.map((r) => r.stock_code)
  if (!codes.length) return
  try {
    const resp = await axios.post<ScreeningWatchlistResponse>('/api/screening/add_to_watchlist', {
        stock_codes: codes,
        group: 'screening',
        result_id: savedResultId.value,
    })
    message.success(`已加入自选: ${resp.data.added} 只`)
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '加入自选失败'))
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

    <n-alert
      v-if="props.truncated"
      id="truncation-alert"
      type="warning"
      title="结果已截断"
      style="margin-bottom: 16px"
    >
      匹配总数 {{ props.totalMatched ?? '超过上限' }} 条，仅展示前 5000 条。请调整筛选条件缩小范围，
      否则保存的结果与导出的 CSV 同样只包含这 5000 条。
    </n-alert>

    <n-grid :cols="4" :x-gap="16" style="margin-bottom: 16px">
      <n-grid-item><n-card><n-statistic label="结果数" :value="displayedResults.length" /></n-card></n-grid-item>
      <n-grid-item><n-card><n-statistic label="基础池" :value="basePoolSize" /></n-card></n-grid-item>
      <n-grid-item><n-card><n-statistic label="耗时(ms)" :value="executionTime" /></n-card></n-grid-item>
      <n-grid-item><n-card><n-statistic label="数据日期"><template #default><span>{{ dataDate || '—' }}</span><n-tag v-if="hasWarnings" size="small" type="warning" style="margin-left: 8px">{{ warningCodes.length }} 个警告</n-tag></template></n-statistic></n-card></n-grid-item>
    </n-grid>

    <n-space style="margin-bottom: 16px">
      <n-button id="save-btn" :disabled="durableActionsDisabled" :aria-describedby="durableActionsDisabled ? 'quality-alert' : undefined" @click="showSaveDialog = true">保存结果</n-button>
      <n-button id="export-btn" :disabled="durableActionsDisabled" :aria-describedby="durableActionsDisabled ? 'quality-alert' : undefined" @click="exportCSV">导出CSV</n-button>
      <n-button id="watchlist-btn" :disabled="durableActionsDisabled" :aria-describedby="durableActionsDisabled ? 'quality-alert' : undefined" @click="addToWatchlist">加入自选</n-button>
    </n-space>

    <n-card size="small" style="margin-bottom: 16px">
      <n-space align="center">
        <span>显示列:</span>
        <n-select
          v-model:value="selectedColumns"
          :options="columnOptions"
          multiple
          size="small"
          style="width: 400px"
          placeholder="选择要显示的列"
        />
      </n-space>
    </n-card>

    <n-data-table
      :columns="tableColumns"
      :data="[...displayedResults]"
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
