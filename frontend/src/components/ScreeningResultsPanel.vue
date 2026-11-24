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
  NRadioGroup,
  NRadio,
  useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type { ScreeningResult, WarningCode, ScreeningRuleNode, ScreeningExportResponse, ScreeningWatchlistResponse } from '../types/screening.ts'
import axios from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import { warningCodeLabel } from '../types/data-quality.ts'
import { fieldDisplayName, fieldTitleWithUnit, formatFieldValue } from '../utils/screening-format.ts'
import { isLegacyDefaultResultColumns } from '../composables/useScreeningColumns.ts'

/**
 * disclose 级告警（后端 200 随结果下发：`screening_warnings: [{code, severity, detail}]`）。
 *
 * 与 `warningCodes`（数据质量**硬阻断/遮蔽**码）语义不同：这里的东西**不阻断**
 * 筛选（例如交易日历只覆盖了 97.2% 的标的），但用户必须看见 —— 所以渲染成
 * 黄色提示条，而不是错误弹窗，也不占用 `quality-alert` 的 fail-closed 语义。
 */
interface ScreeningWarning {
  readonly code: string
  readonly severity?: string
  readonly detail?: string
}

const props = defineProps<{
  results: readonly ScreeningResult[]
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
  /**
   * 按设计豁免字段级 strict 校验的**自算派生字段**（`strict_exempt_fields`，
   * 2026-11-23 后端下发）。非空时必须如实披露：这类字段本来就没有抓取血缘，
   * 不能让用户以为是自己把条件写错了。
   */
  strictExemptFields?: readonly string[]
  /**
   * 页面级运行状态（P0，2026-11-24）。
   *
   * 缺省 `'ok'` 是为了向后兼容（旧调用方/单测只传 results）。
   * `'failed'` 时页面已清空 results/runId，这里负责把"作废"讲清楚并禁用所有
   * 持久化入口；`'running'` 时表格里若还有行，也只是**上一次**运行的结果。
   */
  runState?: 'idle' | 'running' | 'ok' | 'empty' | 'failed'
  /** 最近一次运行失败的原因（`runState === 'failed'` 时展示）。 */
  lastRunError?: string | null
  /** disclose 级告警（黄色提示条；不是错误）。 */
  screeningWarnings?: readonly ScreeningWarning[]
  /**
   * 这次运行的凭据（`runId`）已在服务端失效（g5，2026-11-24）。
   *
   * 两种来源：① 本地 TTL 判定（后端只保留 24h，见 `stores/screening-session.ts`）；
   * ② 保存时后端回 400 `server screening run not found`。
   *
   * 语义（用户裁定）：**仍然原样展示结果**，只把"要落库/要回服务端"的入口
   * （保存结果 / 导出 CSV / 保存到自选）置灰并说明原因 —— 绝不能因为凭据过期
   * 就把整页清空，那正是用户最讨厌的行为。
   */
  runExpired?: boolean
}>()

const emit = defineEmits<{
  (e: 'retry'): void
  (e: 'run-invalidated', reason: string): void
  /** 服务端已经不认这次运行凭据：页面保留结果、只置灰保存入口。 */
  (e: 'run-expired'): void
}>()

/** disclose 级告警（缺省空数组，旧调用方不传时什么都不渲染）。 */
const disclosedWarningList = computed(() => props.screeningWarnings ?? [])

const message = useMessage()
const showSaveDialog = ref(false)
const saveTitle = ref('')
const saveNote = ref('')
const savedResultId = ref<number | null>(null)

// ── 保存到自选列表：替换 / 合并确认（2026-11-25 用户实测）──────────────
// 现场：同一规则每次保存都用同一个分组名，后端旧语义只 upsert 不删旧行 →
// 「往股东口袋装钱的大老公司」两次运行累成 591 行，其中 220 行来自旧结果
// （63 只当前市值已 < 100 亿，而规则重跑只命中 533 只、最小 100.01 亿）。
// 这里在写入**之前**先读 `<GET /api/watchlist/groups>`：分组已存在且非空时
// 让用户明确选「替换（推荐）」或「合并」，并如实说明会删/保留哪些行。
interface WatchlistGroupSummary {
  readonly group_name: string
  readonly cnt: number
  readonly runs?: number
  readonly manual?: number
}

interface WatchlistConfirmState {
  readonly group: string
  readonly count: number
  readonly runs: number
  readonly manual: number
  readonly codes: readonly string[]
}

const showWatchlistConfirm = ref(false)
const watchlistConfirm = ref<WatchlistConfirmState | null>(null)
// 确认框**默认选中「替换」**（用户显式点保存时，最新结果才是他要的分组内容）；
// 不弹框的自动路径（分组为空 / 读不到分组现状）一律按「合并」发送，保持旧行为。
const watchlistMode = ref<'replace' | 'merge'>('replace')

// L1-7（报告42）: 列配置记忆（localStorage），刷新/重进页面不丢失。
// v2（g4，2026-11-24 用户实测）：旧键只存一个数组、没有版本号。旧版本把
// 「旧 8 列默认集合」写进 localStorage 后，即使 `/run` 已经返回 PE/ROCE 等新列，
// 面板也会把它们过滤掉 —— 用户就只看到股票代码/证监会行业。新键用对象包一层
// 版本号；旧键里的**用户自定义**列仍照常迁移，只有恰好等于旧默认集合的才丢弃。
const COLUMNS_STORAGE_KEY = 'vd.screening.columns.v2'
const LEGACY_COLUMNS_STORAGE_KEY = 'vd.screening.columns'
const COLUMNS_STORAGE_VERSION = 2

function stringColumns(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []
}

function loadSavedColumns(): string[] {
  try {
    const raw = localStorage.getItem(COLUMNS_STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as unknown
      if (parsed && typeof parsed === 'object' && 'columns' in parsed) {
        return stringColumns((parsed as { columns?: unknown }).columns)
      }
      return stringColumns(parsed)
    }
    const legacy = stringColumns(JSON.parse(localStorage.getItem(LEGACY_COLUMNS_STORAGE_KEY) ?? '[]'))
    // 旧默认列集合不是用户自定义 → 直接让本次新结果全量展示。
    return isLegacyDefaultResultColumns(legacy) ? [] : legacy
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

// ─── 运行状态（P0，2026-11-24）──────────────────────────────────────────
// 缺省 'ok'：只传 results 的旧调用方（单测/历史用法）行为不变。
const effectiveRunState = computed(() => props.runState ?? 'ok')
const runFailed = computed(() => effectiveRunState.value === 'failed')
const runInProgress = computed(() => effectiveRunState.value === 'running')
/**
 * 只有 `ok` 态才允许"把结果落盘"（保存/导出/加入自选）。
 *
 * 失败态页面已经把 `results`/`runId` 清空，这里再加一道**独立于数据形状**的
 * 闸门：即使将来某次改动忘了清 state，只要状态不是 ok，按钮也不会可点。
 */
const runStateBlocksActions = computed(() => effectiveRunState.value !== 'ok')

const OPERATIONS_ONLY_CODES = new Set(['STALE_RUNNING_JOBS', 'UNPUBLISHED_OVERRIDES'])
const operationsOnlyWarnings = computed(
  () =>
    props.warningCodes.length > 0 &&
    props.warningCodes.every((c) => OPERATIONS_ONLY_CODES.has(c)),
)
/** 运行凭据失效（g5）：结果照常显示，只是不能落库。 */
const runExpiredNow = computed(() => props.runExpired === true)
// 2026-08-27：数据质量警告不再直接阻止保存/导出。
// 保存/导出仍要求存在有效运行与规则；部分股票缺口由引擎排除，
// 警告仅作披露，不再把整个结果变成不可导出。
const durableActionsDisabled = computed(
  () =>
    props.results.length > 0 &&
    (
      runExpiredNow.value ||
      props.qualityStatus === 'failed' ||
      props.ruleId === null ||
      props.runId === null ||
      runStateBlocksActions.value
    ),
)
/**
 * 保存/导出按钮的最终禁用条件。
 *
 * 注意要覆盖"结果为空但面板还在"的失败态 —— 那时 `durableActionsDisabled`
 * 为 false（它要求 results.length > 0），所以必须单独把 runState/runId 算进来。
 */
const durableEntryDisabled = computed(
  () => runStateBlocksActions.value || props.runId === null || durableActionsDisabled.value,
)
// 自选列表只保存股票代码与规则来源，不保存当前不可信数值，
// 因此即使存在数据质量警告，也允许用户把“本次筛选命中的公司”固定到自选。
const watchlistDisabled = computed(
  () =>
    runExpiredNow.value ||
    runStateBlocksActions.value ||
    props.runId === null ||
    props.results.length === 0,
)
/** 为什么入口被禁用（灰字，避免用户对着灰按钮猜）。 */
const actionsBlockedReason = computed(() => {
  if (runExpiredNow.value) {
    return '这次运行的保存凭据已过期（服务端只保留 24 小时）：结果仍然照常显示，但保存/导出/加入自选已停用；请重新运行筛选。'
  }
  if (runFailed.value) {
    return '筛选失败：上一次结果已清空，没有可保存/导出/加入自选的数据。请点「重试运行」重新筛选。'
  }
  if (runInProgress.value) {
    return '筛选正在进行：完成前不能保存/导出/加入自选（下方若还有表格，那是上一次运行的结果）。'
  }
  if (props.runId === null) {
    return '缺少本次运行记录（runId），保存/导出/加入自选已禁用；请重新运行筛选。'
  }
  return ''
})

/**
 * 禁用按钮的 `aria-describedby` 指向真正解释原因的那块内容。
 *
 * 凭据过期 → `#run-expired-alert`；质量状态异常 → `#quality-alert`；
 * 其余情况（运行失败/进行中/无 runId）→ 灰字 `#run-actions-reason`。
 * 指向不存在的 id 比不写更糟。
 */
const actionsDescribedBy = computed(() => {
  if (runExpiredNow.value) return 'run-expired-alert'
  if (props.qualityStatus !== 'available' || hasWarnings.value) return 'quality-alert'
  return actionsBlockedReason.value ? 'run-actions-reason' : undefined
})

/**
 * disclose 级告警文案（中文优先，未知码退 detail）。
 *
 * `warningCodeLabel()` 对未登记的新码会原样返回 code —— 直接渲染就是用户
 * 看不懂的 `TRADING_CALENDAR_INCOMPLETE`。所以：
 * - 标题：已登记码 → 中文；未登记 → 中性「数据质量披露」（正文给 detail）；
 * - 正文：优先 detail（后端带具体数字，如 158/5,580 只 = 2.8%）。
 */
function disclosedWarningTitle(warning: ScreeningWarning): string {
  const label = warningCodeLabel(warning.code)
  if (label !== warning.code) return label
  return warning.detail ? '数据质量披露' : warning.code
}

function disclosedWarningBody(warning: ScreeningWarning): string {
  const label = warningCodeLabel(warning.code)
  if (label === warning.code) return warning.detail ?? ''
  return warning.detail && warning.detail !== label ? warning.detail : ''
}

/** 后端"这次 run 没了"的两句固定错误（其余错误照旧只弹 toast）。 */
function isRunInvalidatedError(e: unknown): boolean {
  const detail = runErrorDetail(e)
  return (
    /saved screening result not found/i.test(detail) ||
    /saved result rule provenance is missing/i.test(detail)
  )
}

/**
 * 后端说"这次运行凭据不在了"（g5）。
 *
 * `app/web/api/screening.py::save_screening_result` 在 `screening_runs` 里找不到
 * run_id 时回 400 `server screening run not found` —— 典型原因是服务端 24h TTL
 * 到期，或这个 run 已被 `/save` 消费过。此时**不能**污名化成"结果无效"：
 * 结果仍然是对当前规则的如实产出，只是再也落不了库。
 */
function isStaleRunCredentialError(e: unknown): boolean {
  return /run not found/i.test(runErrorDetail(e))
}

/** 从 axios 错误里取 detail 原文（不引入 isAxiosError 依赖，测试 mock 也能走）。 */
function runErrorDetail(e: unknown): string {
  if (e && typeof e === 'object' && 'response' in e) {
    const response = (e as { response?: { data?: { detail?: unknown } } }).response
    const detail = response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  return e instanceof Error ? e.message : ''
}

/**
 * 保存/导出/加入自选失败时的统一出口。
 *
 * `saved screening result not found` / `saved result rule provenance is missing`
 * 说明服务端已经不认这次运行 → 通知页面落到失败态并清空旧结果，而不是让用户
 * 对着一份保存不了的结果继续点。其余错误保持原行为（只弹 toast）。
 */
function reportActionError(e: unknown, fallback: string): void {
  const text = friendlyErrorMessage(e, fallback)
  if (isRunInvalidatedError(e)) {
    // 后端已经不认这次 run：先让用户看懂"要重新运行"，再通知页面清空旧结果。
    const reason = `这次筛选运行已失效（${text}）；请重新运行筛选后再保存/导出/加入自选。`
    message.error(reason)
    emit('run-invalidated', reason)
    return
  }
  if (isStaleRunCredentialError(e)) {
    // g5：凭据过期 ≠ 结果无效。保留结果，只把落库入口置灰 + 一条提示。
    message.warning(`这次运行的保存凭据已过期（${text}）；结果仍可查看，请重新运行后再保存。`)
    emit('run-expired')
    return
  }
  message.error(text)
}

// L0-3（报告42）: 展示以服务端 run 结果为准，前端不做二次客户端过滤，
// 保证屏幕/保存/CSV 三者一致。（用户裁定去掉严格模式开关后，页面不再有
// strict_only 切换，"谁在服务端过滤"这件事更简单：口径固定由后端决定。）
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
    localStorage.setItem(COLUMNS_STORAGE_KEY, JSON.stringify({
      version: COLUMNS_STORAGE_VERSION,
      columns: cols,
    }))
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

/**
 * 结果表的**列头排序**（2026-11-24）。
 *
 * 背景：用户裁定"结果的排名在结果那里自己选 —— 点一下表头就按市值排，点一下
 * ROE 表头就按 ROE 排"。条件行里的「排名口径」下拉已因此移除（见
 * `RuleConditionRow.vue`），**这套列头排序就是那个替代方案**，必须真的能用。
 *
 * 为什么不用 naive-ui 的 `sorter: 'default'`（原实现）：
 * `useSorter.mjs::getDefaultSorterFn` 只在**两边都是 number**时做数值比较，
 * **两边都是 string** 时用 `localeCompare`（字典序），类型不一致（一边数字
 * 一边字符串）直接返回 0 = 保持不变。于是有两个真实故障：
 *
 * 1. 后端 DECIMAL 走 SQLite 存档时被 `json.dumps(default=str)` 序列化过
 *    （`app/web/api/screening.py` 的 screening_runs/screening_results 写入），
 *    回读的行里 `total_market_cap` 是 `"1.2e+11"` 这类**数值字符串** ——
 *    字典序排序会把 "9e+10" 排到 "1.2e+11" 后面（9 < 1 是 false，反着来），
 *    用户点了表头却发现顺序不像市值。
 * 2. 同一列里混着数字和数字字符串（本次 run 返回数字、历史行是字符串）时，
 *    默认比较器返回 0 → 整列"点了没反应"。
 *
 * 因此这里用显式比较器：能转成有限数字的一律**按数值**比（百分比/亿元都不
 * 影响次序，因为格式化是单调变换），其余退回中文字典序；空值（null/undefined/
 * 空串）**恒排最后**，不会插在中间让人以为没排序。
 */
const NUMERIC_STRING_RE = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/

/** 值 → 可比较的数字；不是"数字"就返回 null（交给字典序兜底）。 */
function toSortKeyNumber(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'boolean') return value ? 1 : 0
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!trimmed || !NUMERIC_STRING_RE.test(trimmed)) return null
    const parsed = Number(trimmed)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

/** 空值判定：null / undefined / 空串都当"没有数据"。 */
function isEmptySortValue(value: unknown): boolean {
  return value === null || value === undefined || value === ''
}

/**
 * 当前排序方向（naive-ui 的 `update:sorter` 回调）。
 *
 * 空值恒排最后需要知道方向：naive-ui 是"比较结果 × 方向系数"（ascend=+1、
 * descend=-1，见 `node_modules/naive-ui/es/data-table/src/utils.mjs::
 * getFlagOfOrder`），所以要在升序时返回 +1、降序时返回 -1 才能让空值**两边
 * 都在末尾**。
 */
const activeSortState = ref<{ columnKey: string; order: 'ascend' | 'descend' | false } | null>(null)

/** 豁免字段（缺失/旧后端 → 空数组，不渲染那一行说明）。 */
const strictExemptList = computed(() => props.strictExemptFields ?? [])

function onSorterChange(state: unknown): void {
  const single = Array.isArray(state) ? state[0] : state
  if (!single || typeof single !== 'object') {
    activeSortState.value = null
    return
  }
  const record = single as { columnKey?: unknown; order?: unknown }
  activeSortState.value = {
    columnKey: String(record.columnKey ?? ''),
    order: record.order === 'ascend' || record.order === 'descend' ? record.order : false,
  }
}

/** 某一列的排序比较器：数值优先、空值恒最后、其余中文字典序。 */
function compareResultCells(left: unknown, right: unknown): number {
  const leftEmpty = isEmptySortValue(left)
  const rightEmpty = isEmptySortValue(right)
  if (leftEmpty || rightEmpty) {
    if (leftEmpty && rightEmpty) return 0
    // 空值恒排最后（见 activeSortState 注释）
    const emptyIsLast = activeSortState.value?.order !== 'descend'
    if (leftEmpty) return emptyIsLast ? 1 : -1
    return emptyIsLast ? -1 : 1
  }
  const leftNumber = toSortKeyNumber(left)
  const rightNumber = toSortKeyNumber(right)
  if (leftNumber !== null && rightNumber !== null) return leftNumber - rightNumber
  return String(left).localeCompare(String(right), 'zh-Hans-CN')
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
        // 2026-11-24：显式比较器（数值优先、空值恒最后），见 compareResultCells
        sorter: (row1, row2) => compareResultCells(row1[key], row2[key]),
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

  // 逐只「+ 加入自选」（2026-11-26）：固定在最右，不参与列配置，也不改整批保存的语义。
  cols.push({
    title: '自选',
    key: 'watchlist_action',
    width: 104,
    render: (row) => renderWatchlistAction(row),
  })
  
  return cols
})

async function saveResults() {
  if (runStateBlocksActions.value || props.runId === null) {
    message.warning(actionsBlockedReason.value || '当前没有有效的筛选运行结果，无法保存；请重新运行筛选')
    return
  }
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
    // run 已失效（not found / provenance missing）→ 页面落失败态，别继续显示旧结果。
    reportActionError(e, '保存失败')
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
  if (runStateBlocksActions.value || props.runId === null) {
    message.warning(actionsBlockedReason.value || '当前没有有效的筛选运行结果，无法导出；请重新运行筛选')
    return
  }
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
    reportActionError(e, '导出失败')
  }
}

// ─── 逐只「+ 加入自选」（2026-11-26 用户需求）─────────────────────────
// 用户原话：「或者我直接在按规则分组的列表里面有个『点击加入自选列表』」。
// 语义与整批「保存到自选列表」**不同且互不影响**：整批是「本次结果 → 规则分组」
// （含替换/合并确认），逐只是「这一只 → 默认分组『我的自选』」。
//
// 已加入标记来自 `GET /api/watchlist/list`（服务端事实，一次请求）：接口挂了只是
// 少一个对勾，**绝不**能把筛选结果页打成失败态，所以失败时静默保留空集合。
const watchlistCodes = ref<Set<string>>(new Set())
const watchlistAddingCode = ref<string | null>(null)
/** 默认分组名由后端下发（`/status`、`/add` 都会带），前端不写死中文字面量。 */
const watchlistDefaultGroup = ref('我的自选')

function stockLabel(row: ScreeningResult): string {
  const name = String(row.name ?? '').trim()
  const code = String(row.stock_code ?? '').trim()
  return name ? `${name}（${code}）` : code
}

async function loadWatchlistPresence(): Promise<void> {
  try {
    const resp = await axios.get<{ items?: Array<{ stock_code?: unknown; group_name?: unknown }> }>(
      '/api/watchlist/list',
    )
    watchlistCodes.value = new Set(
      (resp.data?.items ?? [])
        .map((item) => String(item.stock_code ?? ''))
        .filter(Boolean),
    )
  } catch {
    // 只影响"已加入"标记，不影响结果本身；不发错误提示（用户没要求这次读）。
    watchlistCodes.value = new Set()
  }
}

watch(
  () => props.results,
  () => {
    void loadWatchlistPresence()
  },
  { immediate: true },
)

async function addSingleToWatchlist(row: ScreeningResult): Promise<void> {
  const code = String(row.stock_code ?? '').trim()
  if (!code || watchlistAddingCode.value) return
  watchlistAddingCode.value = code
  try {
    const resp = await axios.post<{ group?: string; created?: boolean; default_group?: string }>(
      '/api/watchlist/add',
      { stock_code: code },
    )
    const group = resp.data?.group || watchlistDefaultGroup.value
    if (resp.data?.default_group) watchlistDefaultGroup.value = resp.data.default_group
    const next = new Set(watchlistCodes.value)
    next.add(code)
    watchlistCodes.value = next
    message.success(
      resp.data?.created === false
        ? `${stockLabel(row)} 已经在「${group}」里了`
        : `已加入「${group}」：${stockLabel(row)}`,
    )
  } catch (e: unknown) {
    // **400 不许吞**：不在股票池 / 黑名单等原因由后端中文 message 带出来
    // （`friendlyErrorMessage` 优先展示后端主句）。
    message.error(`加入自选失败：${stockLabel(row)}；${friendlyErrorMessage(e, '原因未知')}`)
  } finally {
    watchlistAddingCode.value = null
  }
}

/** 逐只加入按钮 / 已加入标记（同一只股票重复点会被置灰，避免连点）。 */
function renderWatchlistAction(row: ScreeningResult): VNode {
  const code = String(row.stock_code ?? '').trim()
  if (watchlistCodes.value.has(code)) {
    return h(
      NButton,
      { size: 'tiny', disabled: true, class: 'screening-watchlist-added', title: '已在自选列表中' },
      { default: () => '✓ 已加入' },
    )
  }
  return h(
    NButton,
    {
      size: 'tiny',
      quaternary: true,
      type: 'primary',
      class: 'screening-watchlist-add',
      loading: watchlistAddingCode.value === code,
      title: `把 ${stockLabel(row)} 加入默认自选分组`,
      onClick: () => void addSingleToWatchlist(row),
    },
    { default: () => '+ 加入自选' },
  )
}

/** 读该分组现状（`GET /api/watchlist/groups` 是唯一事实来源，不靠本地缓存猜）。 */
async function fetchWatchlistGroup(groupName: string): Promise<WatchlistGroupSummary | null> {
  const resp = await axios.get<{ groups: WatchlistGroupSummary[] }>('/api/watchlist/groups')
  const groups = resp.data?.groups ?? []
  return groups.find((g) => g.group_name === groupName) ?? null
}

async function addToWatchlist() {
  // 失败态/无 runId 时按钮本就禁用，这里再兜一道：绝不能用旧 results 写自选。
  if (runStateBlocksActions.value || props.runId === null) {
    message.warning(actionsBlockedReason.value || '当前没有有效的筛选运行结果，无法加入自选；请重新运行筛选')
    return
  }
  const codes = displayedResults.value.map((r) => r.stock_code)
  if (!codes.length) return
  // 筛选结果按规则名归组；手动加入的股票仍由自选列表使用默认组。
  const groupName = props.ruleName || 'default'
  try {
    const existing = await fetchWatchlistGroup(groupName)
    if (existing && existing.cnt > 0) {
      // 分组已存在且非空：必须先让用户选「替换」还是「合并」，不能默默累加。
      watchlistConfirm.value = {
        group: groupName,
        count: existing.cnt,
        runs: existing.runs ?? 0,
        manual: existing.manual ?? 0,
        codes,
      }
      watchlistMode.value = 'replace'
      showWatchlistConfirm.value = true
      return
    }
  } catch {
    // 读不到分组现状时**不猜**：按保守的「合并」继续（不删任何已有股票），
    // 并在提示里说明原因，避免把一次查询失败变成静默的累加或误删。
    message.warning('无法读取自选分组现状，本次按「合并」保存，不会删除任何已有股票')
    await performAddToWatchlist(groupName, 'merge', codes)
    return
  }
  // 分组为空/不存在：替换与合并结果等价，按旧行为发合并请求（不带 replace_group）。
  await performAddToWatchlist(groupName, 'merge', codes)
}

async function performAddToWatchlist(
  groupName: string,
  mode: 'replace' | 'merge',
  codes: readonly string[],
) {
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
    const replace = mode === 'replace'
    const resp = await axios.post<ScreeningWatchlistResponse>('/api/screening/add_to_watchlist', {
        stock_codes: [...codes],
        group: groupName,
        result_id: resultId,
        replace_group: replace,
    })
    const data = resp.data
    if (replace) {
      const removed = data.removed ?? 0
      const inserted = data.inserted ?? data.added
      const keptManual = data.manual_rows_preserved ?? 0
      message.success(
        `已替换自选分组「${groupName}」：替换了 ${removed} 只历史结果 / 新增 ${inserted} 只`
        + (keptManual > 0 ? `，另有 ${keptManual} 只手动添加的股票保持不变` : ''),
      )
    } else {
      const inserted = data.inserted ?? data.added
      const updated = data.updated ?? 0
      message.success(
        `已合并到自选分组「${groupName}」：新增 ${inserted} 只、更新 ${updated} 只（共 ${data.added} 只）`,
      )
    }
  } catch (e: unknown) {
    reportActionError(e, '保存到自选列表失败')
  }
}

async function confirmWatchlistSave() {
  const banner = watchlistConfirm.value
  if (!banner) return
  showWatchlistConfirm.value = false
  await performAddToWatchlist(banner.group, watchlistMode.value, banner.codes)
}
</script>

<template>
  <!-- 2026-08-14 红队 P3：strict 空结果的警告必须可见，不能随
       results.length>0 的条件一起隐藏（此前空结果时用户看不到原因）。 -->
  <!-- 标题用中性词「说明」：后端 `build_strict_mode_warning` 有三条分支，
       其中一条正是"空结果与可信度模式无关"——写死「无匹配」会与正文自相矛盾。 -->
  <!-- 用户裁定（去掉严格模式开关）：本面板不再由 `strictOnly` 开关驱动 ——
       严格/近似已不可选，这里只按**后端自己下发的信号**披露。只要有 warning 或
       `strict_exempt_fields` 就照常显示（后端能力保留：改了 `strict_only` 的
       调用方仍会收到这些字段），信息不被藏起来，但也不再是「二选一」。 -->
  <n-alert
    v-if="props.strictModeWarning || strictExemptList.length > 0"
    id="strict-mode-alert"
    type="warning"
    :title="props.strictModeWarning ? '严格可信模式说明' : '字段口径说明'"
    style="margin-bottom: 16px"
  >
    <p v-if="props.strictModeWarning" class="strict-mode-warning">{{ props.strictModeWarning }}</p>
    <p v-if="strictExemptList.length > 0" class="strict-exempt-note">
      其中 {{ strictExemptList.length }} 个字段是本项目自算的派生字段（{{ strictExemptList.join('、') }}），
      按设计没有字段级抓取血缘、已豁免字段级 strict 校验（它们的输入字段仍逐个校验），
      不是你的条件写错了。
    </p>
  </n-alert>

  <!-- disclose 级告警（P0，2026-11-24）：后端 200 随结果下发，**不阻断**筛选，
       但必须让用户看见 —— 黄色提示条，不是错误弹窗，也不是 fail-closed 的
       quality-alert。等于 0 条结果时也照样显示（那正是最需要解释的时候）。 -->
  <n-alert
    v-for="warning in disclosedWarningList"
    :key="warning.code"
    id="disclosed-warning-alert"
    :data-warning-code="warning.code"
    class="disclosed-warning"
    type="warning"
    :show-icon="true"
    :bordered="false"
    :title="disclosedWarningTitle(warning)"
    style="margin-bottom: 10px"
  >
    <p v-if="disclosedWarningBody(warning)" class="disclosed-warning-detail">
      {{ disclosedWarningBody(warning) }}
    </p>
  </n-alert>

  <div v-if="results.length > 0 || runFailed">
    <n-alert
      v-if="!runFailed && (hasWarnings || qualityStatus === 'failed' || qualityStatus === 'loading')"
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
          <!-- 2026-11-24（P0 顺手修）：裸渲染枚举会让用户看到 SNAPSHOT_STALE。
               中文文案单一来源是 types/data-quality.ts::WARNING_CODE_LABELS；
               原始 code 放 title，排障时鼠标一悬停就能拿到。 -->
          <n-tag
            v-for="code in warningCodes"
            :key="code"
            size="small"
            type="warning"
            :title="code"
          >{{ warningCodeLabel(code) }}</n-tag>
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
      v-if="!runFailed && props.truncated"
      id="truncation-alert"
      type="warning"
      title="结果已截断"
      style="margin-bottom: 16px"
    >
      匹配总数 {{ props.totalMatched ?? '超过上限' }} 条，仅展示前 5000 条。请调整筛选条件缩小范围，
      否则保存的结果与导出的 CSV 同样只包含这 5000 条。
    </n-alert>

    <!-- 运行凭据过期（g5，2026-11-24 用户裁定）：**结果照常显示**，
         只把保存/导出/加入自选停用并把原因讲清楚。绝不因为凭据过期清空页面。 -->
    <n-alert
      v-if="runExpiredNow && !runFailed"
      id="run-expired-alert"
      type="warning"
      title="这次结果已过期，请重新运行"
      :show-icon="true"
      style="margin-bottom: 16px"
    >
      服务端只保留最近 24 小时的运行结果，本次运行的保存凭据已失效。
      <b>结果仍然照常显示</b>，但「保存结果 / 导出 CSV / 保存到自选」已停用 ——
      请点「运行筛选」重新跑一次，再保存。
    </n-alert>

    <!-- 失败态（P0）：上一次结果已被页面清空。卡片留在"结果曾经出现的地方"，
         把"作废"讲清楚，并显式禁用所有依赖 runId 的入口（灰字给出原因）。 -->
    <section v-if="runFailed" id="screening-run-failed" class="screening-results-card">
      <div class="results-heading">
        <div>
          <p>SCREENING RESULTS</p>
          <h2>筛选失败</h2>
          <span>上一次结果已作废并清空，不会按当前规则误报，也不能写入自选</span>
        </div>
        <div>
          <n-button id="save-btn" size="small" disabled title="筛选失败，结果已清空，无法保存">保存结果</n-button>
          <n-button id="export-btn" size="small" disabled title="筛选失败，结果已清空，无法导出">导出 CSV</n-button>
          <n-button id="watchlist-btn" size="small" type="primary" disabled title="筛选失败，结果已清空，无法加入自选">保存到自选列表</n-button>
        </div>
      </div>
      <n-alert
        id="run-failed-alert"
        type="error"
        title="本次运行失败"
        style="margin: 16px 0"
      >
        {{ lastRunError || '筛选失败（后端未返回原因）' }}
      </n-alert>
      <p id="run-actions-reason" class="action-blocked-reason">{{ actionsBlockedReason }}</p>
      <n-button id="retry-run-btn" type="primary" @click="emit('retry')">重试运行</n-button>
    </section>

    <section v-else class="screening-results-card">
      <div class="results-heading"><div><p>SCREENING RESULTS</p><h2>筛选结果</h2><span v-if="runInProgress">{{ totalMatched || displayedResults.length }} 家公司来自上一次运行（正在重新筛选…）</span><span v-else>{{ totalMatched || displayedResults.length }} 家公司符合当前规则</span></div><div><n-button id="save-btn" size="small" :disabled="durableEntryDisabled" :aria-describedby="durableEntryDisabled ? actionsDescribedBy : undefined" @click="showSaveDialog = true">保存结果</n-button><n-button id="export-btn" size="small" :disabled="durableEntryDisabled" :aria-describedby="durableEntryDisabled ? actionsDescribedBy : undefined" @click="exportCSV">导出 CSV</n-button><n-button id="watchlist-btn" size="small" type="primary" :disabled="watchlistDisabled" :aria-describedby="watchlistDisabled ? actionsDescribedBy : undefined" @click="addToWatchlist">保存到自选列表</n-button></div></div>
      <div class="result-meta"><span><b>{{ displayedResults.length }}</b> 家已展示</span><span>执行规则 <b>{{ ruleName }} v{{ ruleVersion }}</b></span><span>基础股票池 <b>{{ basePoolSize }}</b></span><span>耗时 <b>{{ executionTime }} ms</b></span><span>数据日期 <b>{{ dataDate || '—' }}</b></span><span class="sort-hint">点列头可升序/降序排列（只影响当前屏幕显示；导出的 CSV 按本次筛选的原始顺序，不跟随这里的排序）</span></div>
      <p v-if="actionsBlockedReason" id="run-actions-reason" class="action-blocked-reason">{{ actionsBlockedReason }}</p>
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
      @update:sorter="onSorterChange"
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
          :disabled="durableEntryDisabled"
          :aria-describedby="durableEntryDisabled ? actionsDescribedBy : undefined"
          @click="saveResults"
        >
          保存
        </n-button>
      </template>
    </n-modal>

    <!-- 保存到自选之前的分组现状确认（2026-11-25 用户实测：重复保存会累加）。
         默认选中「替换」；「手动添加」的行在任何选项下都不会被删除。 -->
    <n-modal
      v-model:show="showWatchlistConfirm"
      preset="dialog"
      title="保存到自选列表"
      style="width: 580px"
    >
      <template v-if="watchlistConfirm">
        <p class="watchlist-confirm-summary">
          自选分组「{{ watchlistConfirm.group }}」已有 <b>{{ watchlistConfirm.count }}</b> 只
          （来自 {{ watchlistConfirm.runs }} 次历史运行<template v-if="watchlistConfirm.manual > 0">，其中
          {{ watchlistConfirm.manual }} 只是你手动添加的</template>）。本次结果有
          <b>{{ watchlistConfirm.codes.length }}</b> 只，请选择本次操作会怎样：
        </p>
        <n-radio-group v-model:value="watchlistMode" class="watchlist-mode-group">
          <n-radio value="replace" class="watchlist-mode-option">
            <span class="watchlist-mode-title">替换（推荐）：分组内容 = 本次结果的 {{ watchlistConfirm.codes.length }} 只</span>
            <span class="watchlist-mode-desc">
              先删掉该分组里由历史筛选结果带进来的行（本次结果里仍然命中的会更新为本次来源）；
              手动添加的 {{ watchlistConfirm.manual }} 只股票永不删除。
            </span>
          </n-radio>
          <n-radio value="merge" class="watchlist-mode-option">
            <span class="watchlist-mode-title">合并：保留原有 {{ watchlistConfirm.count }} 只 + 追加本次结果</span>
            <span class="watchlist-mode-desc">
              不删除任何已有股票，但可能出现本次筛选已经排除的旧股票（分组会继续变大）。
            </span>
          </n-radio>
        </n-radio-group>
      </template>
      <template #action>
        <n-button @click="showWatchlistConfirm = false">取消</n-button>
        <n-button
          type="primary"
          :disabled="durableEntryDisabled"
          :aria-describedby="durableEntryDisabled ? actionsDescribedBy : undefined"
          @click="confirmWatchlistSave"
        >
          确认保存
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
/* strict 模式说明：正文 + （可选）豁免字段那行，弱化成次级信息。 */
.strict-mode-warning { margin: 0; }
.strict-exempt-note { margin: 6px 0 0; color: #8b978f; font-size: 11px; }
/* disclose 级告警：黄色提示条里的正文（具体数字），允许换行不挤成一行。 */
.disclosed-warning-detail { margin: 0; line-height: 1.6; }
/* 入口被禁用时把原因写在按钮旁边（灰字），别让用户对着灰按钮猜。 */
.action-blocked-reason { margin: 10px 0 12px; color: #9a8a6a; font-size: 11px; line-height: 1.6; }
/* 保存到自选的替换/合并确认框：两个选项各自带一行解释，不能只写选项名。 */
.watchlist-confirm-summary { margin: 0 0 14px; color: #536359; font-size: 13px; line-height: 1.8; }
.watchlist-mode-group { display: grid; gap: 10px; }
.watchlist-mode-option { display: flex; align-items: flex-start; padding: 10px 12px; border: 1px solid #e6ece8; border-radius: 9px; }
.watchlist-mode-title { display: block; color: #39463d; font-size: 13px; font-weight: 600; }
.watchlist-mode-desc { display: block; margin-top: 4px; color: #829087; font-size: 11px; line-height: 1.7; }
</style>
