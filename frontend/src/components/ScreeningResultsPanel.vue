<script setup lang="ts">
import type { VNode } from 'vue'
import { NEllipsis } from 'naive-ui'
import { computed, h, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  NButton,
  NDataTable,
  NTag,
  NSpace,
  NModal,
  NForm,
  NFormItem,
  NInput,
  NAlert,
  NSelect,
  useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type { ScreeningResult, WarningCode, ScreeningRuleNode, ScreeningExportResponse, ScreeningWatchlistResponse } from '../types/screening.ts'
import axios from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import { fieldDisplayName, fieldTitleWithUnit, formatFieldValue } from '../utils/screening-format.ts'

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
  // L1-4（报告42）: 导出文件名使用规则名，用户可复现/归档
  ruleName?: string
  lockedIndicators: Record<string, unknown>
  basePoolConfig: Record<string, unknown>
  // P1-C: 匹配数超过 5000 行上限时服务端显式标记，前端必须警示而非静默丢尾
  truncated?: boolean
  totalMatched?: number
  // 2026-08-14 红队 P2-4：严格模式空结果的原因反馈
  strictModeWarning?: string | null
}>()

const message = useMessage()
const showSaveDialog = ref(false)
const saveTitle = ref('')
const saveNote = ref('')
const savedResultId = ref<number | null>(null)

// L1-7（报告42）: 列配置记忆（localStorage），刷新/重进页面不丢失
const COLUMNS_STORAGE_KEY = 'vd.screening.columns'

function loadSavedColumns(): string[] {
  try {
    const raw = localStorage.getItem(COLUMNS_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : []
  } catch {
    return []
  }
}

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
// 2026-08-27：数据质量警告不再直接阻止保存/导出。
// 保存/导出仍要求存在有效运行与规则；部分股票缺口由引擎排除，
// 警告仅作披露，不再把整个结果变成不可导出。
const durableActionsDisabled = computed(
  () => (props.qualityStatus === 'failed' || props.ruleId === null || props.runId === null) && props.results.length > 0,
)
// 自选列表只保存股票代码与规则来源，不保存当前不可信数值，
// 因此即使存在数据质量警告，也允许用户把“本次筛选命中的公司”固定到自选。
const watchlistDisabled = computed(
  () => props.runId === null || props.results.length === 0,
)

// L0-3（报告42）: 展示以服务端 run 结果为准。strict_only 切换时由 ScreeningPage
// 重新运行（服务端过滤），前端不再做二次客户端过滤，保证屏幕/保存/CSV 三者一致。
const displayedResults = computed(() => props.results)

const allAvailableColumns = computed(() => {
  if (!displayedResults.value.length) return []
  const firstRow = displayedResults.value[0]
  return Object.keys(firstRow).filter((k) => !k.startsWith('_'))
})

const selectedColumns = ref<string[]>(loadSavedColumns())

watch(allAvailableColumns, (newCols) => {
  const valid = selectedColumns.value.filter((col) => newCols.includes(col))
  selectedColumns.value = valid.length > 0 ? valid : [...newCols]
}, { immediate: true })

watch(selectedColumns, (cols) => {
  try {
    localStorage.setItem(COLUMNS_STORAGE_KEY, JSON.stringify(cols))
  } catch {
    // localStorage 不可用时静默降级（不阻断功能）
  }
}, { deep: true })

// L1-7（报告42）: 一键复制股票代码
async function copyStockCode(code: string) {
  try {
    await navigator.clipboard.writeText(code)
    message.success(`已复制 ${code}`)
  } catch {
    message.warning('复制失败，请手动选择复制')
  }
}

/**
 * 结果列下拉的标签渲染（2026-11-16）。
 *
 * 指标名可能很长（如「已动用资本回报率（ROCE·20年中位）」+ 排名口径后缀），
 * n-select 会截断且默认**没有 tooltip**；用 NEllipsis 的 tooltip 模式
 * 让鼠标悬浮能看到完整名称（与筛选页 renderOptionLabel 保持一致）。
 */
function renderColumnLabel(option: { label?: string }): VNode {
  return h(
    NEllipsis,
    { tooltip: { placement: 'right', contentStyle: 'max-width: 520px; white-space: normal;' } },
    { default: () => option.label ?? '' },
  )
}

const columnOptions = computed(() => {
  return allAvailableColumns.value.map(col => ({
    // L0-2: 表头带单位，口径与自选/详情一致
    label: fieldTitleWithUnit(col, fieldDisplayName(col)),
    value: col,
  }))
})

/**
 * 空值原因文案（reports/119 §5 P2-1）。
 *
 * 后端 `indicator_snapshot` / `indicator_ext` 的 NULL 有 6 种完全不同的含义，
 * 统一画成「—」会让用户分不清「本来没有」和「我们坏了」，也丢掉自检信号。
 * 文案单一真相在 `app/core/quality/null_reasons.py::NULL_REASON_LABELS`；
 * 这里只做离线兜底（reason code 是稳定枚举，不会随文案漂移），
 * 后端下发未知码时直接显示原始 code —— 宁可丑，也不能把没见过的原因藏起来。
 */
const NULL_REASON_LABELS: Readonly<Record<string, string>> = {
  SOURCE_EMPTY: '数据源未提供该科目',
  NOT_APPLICABLE: '该指标对这类公司不适用',
  INVALID_DENOMINATOR: '分母为 0 或负，比率无意义',
  NOT_DISPATCHED: '本期没有调度抓取/计算',
  MASKED_UNTRUSTED: '数据已入库但被可信门禁遮蔽',
  NOT_COMPUTED: '该算未算（输入缺失或报告期未对齐）',
}

/** 结果行可能带原因码的字段形态（后端尚未统一下发，先兼容三种）。 */
type NullReasonCarrier = {
  null_reasons?: Record<string, string | null | undefined>
  null_reason?: Record<string, string | null | undefined>
}

/** 取某字段的空值原因码；没有就返回 null（保持原来的「—」渲染）。 */
function nullReasonCode(row: ScreeningResult, field: string): string | null {
  const carrier = row as unknown as NullReasonCarrier & Record<string, unknown>
  const mapped = carrier.null_reasons?.[field] ?? carrier.null_reason?.[field]
  if (typeof mapped === 'string' && mapped) return mapped
  const flat = carrier[`${field}_null_reason`]
  return typeof flat === 'string' && flat ? flat : null
}

const tableColumns = computed(() => {
  if (!displayedResults.value.length) return []
  const firstRow = displayedResults.value[0]
  const cols: DataTableColumns<ScreeningResult> = []
  
  for (const key of selectedColumns.value) {
    if (key in firstRow) {
      cols.push({
        // 2026-11-16（reports/148）：列不在下拉里（旧规则/未知列）时不能露英文表头
        title: columnOptions.value.find(o => o.value === key)?.label
          || fieldTitleWithUnit(key, fieldDisplayName(key, key)),
        key,
        sorter: 'default',
        // L1-7: 固定代码列，滚动时始终可见
        fixed: key === 'stock_code' ? 'left' : undefined,
        render(row) {
          const v = row[key]
          if (key === 'stock_code' && typeof v === 'string') {
            // L1-6: 使用 router-link 导航 + 可复制
            return h('span', { class: 'stock-cell', style: 'white-space: nowrap;' }, [
              h(RouterLink, {
                to: `/stock/${encodeURIComponent(v)}`,
                class: 'stock-link',
              }, { default: () => v }),
              h(NButton, {
                text: true,
                size: 'tiny',
                type: 'primary',
                class: 'copy-btn',
                title: `复制 ${v}`,
                onClick: () => void copyStockCode(v),
              }, { default: () => '复制' }),
            ])
          }
          // L0-2: 统一字段口径格式化（百分比/价格/比值/市值）
          // reports/119 P2-1: 空单元格仍显示「—」，但若后端下了 null_reason，
          // 把「为什么不适用/为什么没算」放进 title，鼠标悬停即可区分
          // 「没有」和「坏了」。
          if (v === null || v === undefined) {
            const reasonCode = nullReasonCode(row, key)
            if (reasonCode) {
              const label = NULL_REASON_LABELS[reasonCode] ?? '未知原因'
              return h(
                'span',
                { class: 'null-reason-cell', title: `${label}（${reasonCode}）` },
                '—',
              )
            }
          }
          return formatFieldValue(key, v)
        },
      })
    }
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
    // L2 V5（报告42）: 完成反馈带结果 ID，便于之后导出/归档
    message.success(`已保存（结果 #${response.data.result_id}）`)
    showSaveDialog.value = false
    saveTitle.value = ''
    saveNote.value = ''
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '保存失败'))
  }
}

// L1-4（报告42）: 可归档复现的文件名：规则名_数据日期_结果数[_truncated].csv
function exportFileName(): string {
  const base = (props.ruleName || 'screening').replace(/[\\/:*?"<>|\s]+/g, '_')
  const date = props.dataDate ? String(props.dataDate).replace(/[\\/:]/g, '-') : 'nodate'
  const rows = props.results.length
  const suffix = props.truncated ? '_truncated' : ''
  return `${base}_${date}_${rows}${suffix}.csv`
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
      // 2026-11-16（reports/148）：CSV 表头要中文（PRD §12.1）。
      // 中文名解析只在 `fieldDisplayName()` 一处实现，后端不重复词表。
      column_labels: Object.fromEntries(
        selectedColumns.value.map((key: string) => [key, fieldDisplayName(key, key)]),
      ),
    })
    const blob = new Blob(['\ufeff' + resp.data.csv], { type: 'text/csv;charset=utf-8;bom' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = exportFileName()
    a.click()
    URL.revokeObjectURL(url)
    message.success(`已导出 ${resp.data.rows} 条（${a.download}）`)
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '导出失败'))
  }
}

async function addToWatchlist() {
  const codes = displayedResults.value.map((r) => r.stock_code)
  if (!codes.length) return
  try {
    // 自选列表只依赖“本次结果包含哪些股票”，不需要用户先手动保存结果。
    // 为保留筛选来源，这里自动保存一个结果记录，再写入自选。
    let resultId = savedResultId.value
    if (resultId === null) {
      const now = new Date()
      const pad = (n: number) => String(n).padStart(2, '0')
      const title = `${props.ruleName || '未命名规则'} ${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`
      const saved = await axios.post<{ status: string; result_id: number }>('/api/screening/save', {
        title,
        note: '自动保存，用于加入自选并保留规则来源',
        run_id: props.runId,
        columns: selectedColumns.value,
      })
      savedResultId.value = saved.data.result_id
      resultId = saved.data.result_id
    }
    const resp = await axios.post<ScreeningWatchlistResponse>('/api/screening/add_to_watchlist', {
        stock_codes: codes,
        // 筛选结果按规则名归组；手动加入的股票仍由自选列表使用默认组。
        group: props.ruleName || 'default',
        result_id: resultId,
    })
    message.success(`已保存到自选列表: ${resp.data.added} 只`)
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '保存到自选列表失败'))
  }
}
</script>

<template>
  <!-- 2026-08-14 红队 P3：strict 模式空结果的警告必须可见，不能随
       results.length>0 的条件一起隐藏（此前空结果时用户看不到原因）。 -->
  <n-alert
    v-if="props.strictOnly && props.strictModeWarning"
    id="strict-mode-alert"
    type="warning"
    title="严格可信模式无匹配"
    style="margin-bottom: 16px"
  >
    {{ props.strictModeWarning }}
  </n-alert>

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
        <div v-if="hasUntrustedFields" style="margin-top: 8px; color: #b8860b">
          受影响字段：{{ untrustedFields.map(f => fieldDisplayName(f, f)).join('、') }}。已允许保存结果/导出 CSV/保存到自选；
          但这些字段当前存在数据质量告警，请结合数据状态页谨慎使用。
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

    <section class="screening-results-card">
      <div class="results-heading"><div><p>SCREENING RESULTS</p><h2>筛选结果</h2><span>{{ totalMatched || displayedResults.length }} 家公司符合当前规则</span></div><div><n-button id="save-btn" size="small" :disabled="durableActionsDisabled" :aria-describedby="durableActionsDisabled ? 'quality-alert' : undefined" @click="showSaveDialog = true">保存结果</n-button><n-button id="export-btn" size="small" :disabled="durableActionsDisabled" :aria-describedby="durableActionsDisabled ? 'quality-alert' : undefined" @click="exportCSV">导出 CSV</n-button><n-button id="watchlist-btn" size="small" type="primary" :disabled="watchlistDisabled" :aria-describedby="watchlistDisabled ? 'quality-alert' : undefined" @click="addToWatchlist">保存到自选列表</n-button></div></div>
      <div class="result-meta"><span><b>{{ displayedResults.length }}</b> 家已展示</span><span>执行规则 <b>{{ ruleName }} v{{ ruleVersion }}</b></span><span>基础股票池 <b>{{ basePoolSize }}</b></span><span>耗时 <b>{{ executionTime }} ms</b></span><span>数据日期 <b>{{ dataDate || '—' }}</b></span><span class="sort-hint">点列头可升序/降序排列（只影响当前视图，导出按当前顺序）</span></div>
      <div class="column-config">
        <n-select
          v-model:value="selectedColumns"
          :options="columnOptions"
          multiple
          size="small"
          style="width: min(400px, 100%)"
          :render-label="renderColumnLabel"
          placeholder="选择要显示的列"
        />
      </div>

    <n-data-table
      :columns="tableColumns"
      :data="[...displayedResults]"
      :max="5000"
      :pagination="{ pageSize: 50 }"
      :scroll-x="1200"
      size="small"
      striped
    />
    </section>

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

<style scoped>
.screening-results-card { margin-top: 21px; padding: 27px 29px 29px; border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.results-heading { display: flex; justify-content: space-between; gap: 20px; }.results-heading p { margin: 0; color: #91a097; font-size: 9px; font-weight: 800; letter-spacing: .13em; }.results-heading h2 { margin: 7px 0 5px; font-size: 19px; }.results-heading span { color: #829087; font-size: 11px; }.results-heading > div:last-child { display: flex; align-items: start; gap: 8px; }.result-meta { display: flex; gap: 22px; margin: 21px 0 15px; color: #8b978f; font-size: 10px; }.result-meta b { color: #536359; }.result-meta .sort-hint { color: #a4b0a7; }.column-config { margin: 0 0 15px; padding: 12px; border-radius: 8px; background: #fafcf9; }
/* 空值原因可通过 title 悬停查看：给个下划线提示"这格有话说"，但不改颜色，
   保持与真正缺失（无原因码的「—」）同一视觉层级。 */
.null-reason-cell { border-bottom: 1px dashed #b9c5bd; cursor: help; }
</style>
