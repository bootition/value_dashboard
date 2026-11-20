<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted, computed, nextTick, h, watch } from 'vue'
import type { VNode } from 'vue'
import { NButton, NInput, NInputNumber, NSelect, NEmpty, NModal, NAlert, NTooltip, useMessage, useDialog } from 'naive-ui'
import { NEllipsis } from 'naive-ui'
import axios from 'axios'
import ScreeningResultsPanel from '../components/ScreeningResultsPanel.vue'
import ScreeningRuleEditor from '../components/ScreeningRuleEditor.vue'
import DslIndicatorManager from '../components/DslIndicatorManager.vue'
import type {
  ScreeningIndicator,
  ScreeningRuleNode,
  ScreeningResult,
  ScreeningRunResponse,
  DataStatusSummaryResponse,
  WarningCode,
} from '../types/screening.ts'
import { generateRuleId } from '../types/screening.ts'
import { useAutoUpdateRunning } from '../composables/useAutoUpdatePolling.ts'
import { useScreeningColumns, DEFAULT_RESULT_COLUMNS, DEFAULT_RESULT_COLUMNS_VERSION, encodeResultColumn } from '../composables/useScreeningColumns.ts'
import { useScreeningDraft } from '../composables/useScreeningDraft.ts'
import { useScreeningRules } from '../composables/useScreeningRules.ts'
import {
  clearRunSnapshot,
  markRunExpired,
  refreshRunExpiry,
  rememberEditorBinding,
  saveRunSnapshot,
  screeningSession,
} from '../stores/screening-session.ts'
import { readinessNotice } from '../utils/readiness-notice.ts'
import type { ReadinessNotice } from '../utils/readiness-notice.ts'
import { collectRuleFields, computeUntrustedFields } from '../helpers/screening-quality.ts'
import { apiErrorText } from '../helpers/api-error.ts'
import { applyIndicatorUnits, fieldDisplayName, indicatorDisplayName } from '../utils/screening-format.ts'
import {
  unavailableCoverageText,
  unavailableKey,
  unavailableLabel,
  unavailableReasonText,
} from '../utils/screening-unavailable.ts'
import type { UnavailableIndicator } from '../utils/screening-unavailable.ts'

const message = useMessage()
const dialog = useDialog()

const indicators = ref<readonly ScreeningIndicator[]>([])
/** 条件级排名口径（方案 C，后端 /indicators 下发）：空 = 原始值 */
const rankModes = ref<Array<{ label: string; value: string }>>([])
// 2026-11-14（reports/120 §5 J5/J9）：后端把「当前报告期不可用/被遮蔽」的字段
// 从 indicators 移到 unavailable（字段名 + reason + coverage_pct）。
// 只让字段消失不给理由，用户会以为功能没了——这里在指标选择区灰显并给 tooltip。
// 2026-11-22：类型与 5 个纯展示函数已抽到 utils/screening-unavailable.ts。
const unavailableIndicators = ref<readonly UnavailableIndicator[]>([])
/**
 * 「已改名」的字段（reports/148）。
 *
 * `dividend_yield` 这类不是"本期没数据"，而是**同一指标换了列名**（→ TTM 口径）。
 * 以前它混在「当前报告期不可用指标」里，用户看到"这里有 7 个指标不能用"，
 * 其中 1 个其实只是改名 —— 语义错了，也让"不可用"这个列表永远清不空。
 * 现在两者分开：真不可用才进上面的列表，改名的单独一行提示。
 */
const renamedIndicators = computed(() =>
  unavailableIndicators.value.filter((item) => item.reason === 'deprecated'),
)
const trulyUnavailableIndicators = computed(() =>
  unavailableIndicators.value.filter((item) => item.reason !== 'deprecated'),
)
const loading = ref(screeningSession.running)
/**
 * ─── 跨挂载保活的运行结果（g5「切页保状态」，2026-11-24）─────────────────
 * 路由切换会卸载本组件（`App.vue` 的 `<router-view />` 没有 keep-alive），页面里的
 * `ref()` 全部销毁 —— 这就是「跑完结果、去数据状态页、切回来就没了」的根因。
 *
 * 结果类状态改由模块级单例 `stores/screening-session.ts` 保管：组件卸载不销毁模块，
 * 切回来时在这里把上次的运行结果原样回填；F5 重建 JS 上下文 → 模块回到空态
 * （「F5 才重置」这条语义因此**天然成立**，不需要额外的文档生命周期标记）。
 *
 * 过期判定放在 setup 里先跑一次：过期的运行凭据只停用保存入口，**不清空结果**。
 */
refreshRunExpiry()
/** 本次文档里最近一次成功运行（`null` = 首次进入 / F5 之后）。 */
const restoredRun = screeningSession.run
const results = ref<readonly ScreeningResult[]>(restoredRun?.results ?? [])
const executionTime = ref(restoredRun?.executionTime ?? 0)
const basePoolSize = ref(restoredRun?.basePoolSize ?? 0)
const dataDate = ref<string | null>(restoredRun?.dataDate ?? null)
const runId = ref<string | null>(restoredRun?.runId ?? null)
const truncated = ref(restoredRun?.truncated ?? false)
const totalMatched = ref(restoredRun?.totalMatched ?? 0)
const resultsAnchor = ref<HTMLElement | null>(null)
/**
 * 运行凭据（`runId`）已失效：结果照常展示，只停用「保存结果 / 导出 / 加入自选」。
 * 见 `stores/screening-session.ts` 顶部关于 24h TTL 的说明。
 */
const runExpired = computed(() => screeningSession.run?.expired === true)

/**
 * 显式运行状态（2026-11-24 P0）。
 *
 * 缺陷：以前 `catch` 只弹 toast，`results`/`totalMatched`/`runId` 原样留在
 * state 里 —— 于是失败后结果面板继续写「N 家公司符合**当前**规则」，
 * 「保存到自选」也照旧可点，用户会把**上一次**的股票按**当前**规则名写进
 * 自选（数据污染）。首次就失败时页面又和"没跑过/没命中"完全同形。
 *
 * 现在把"跑没跑过 / 跑成功 / 成功但 0 条 / 跑失败"拆成显式状态：
 * - `idle`    没跑过
 * - `running` 正在跑（下方若还有旧结果，只是**上一次**的，动作入口一律禁用）
 * - `ok`      成功且有结果（唯一允许保存/导出/加入自选的状态）
 * - `empty`   成功但 0 条（条件太严，不是故障）
 * - `failed`  失败：清空旧结果 + 记下原因，页面可原地重试
 */
type RunState = 'idle' | 'running' | 'ok' | 'empty' | 'failed'
/**
 * `runState` 与「切页保状态」的接缝（g5）。
 *
 * 两者管的是**不同问题**，必须共存：
 * - `runState` 回答"屏幕上的这批结果还可信/可保存吗"（主线 P0 语义：失败即清空）；
 * - `screeningSession.run` 回答"切走再回来还要不要在"（用户语义：不按 F5 就保持）。
 *
 * 共存规则（唯一一条）：挂载时若有上一次成功运行的快照，就把它**如实**映射成
 * `ok`/`empty` —— 否则面板会因为 `idle` 而把保存入口永远置灰（表格还在、按钮却
 * 点不了）。反过来，运行失败时主线语义优先：`clearRunArtifacts()` 会连会话里的
 * 快照一起清掉，避免下一次挂载把"已作废"的结果又复活。
 */
const runState = ref<RunState>(
  restoredRun === null ? 'idle' : (restoredRun.results.length > 0 ? 'ok' : 'empty'),
)
/** 最近一次运行失败的用户可读原因（仅 `failed` 态有值）。 */
const lastRunError = ref<string | null>(null)
/**
 * disclose 级告警（后端 200 随结果下发，`screening_warnings`：
 * `[{code, severity, detail}]`；旧后端/409 只有 `disclosed_warnings` 的 code 数组）。
 * 这类告警**不阻断**筛选，但必须让用户看见（黄色提示条），不能塞进 error 弹窗。
 */
interface ScreeningWarning {
  readonly code: string
  readonly severity?: string
  readonly detail?: string
}
const screeningWarnings = ref<readonly ScreeningWarning[]>(restoredRun?.screeningWarnings ?? [])
// reports/79 方案 A: 更新窗口内快照口径运行标注
const autoUpdateSnapshot = ref(restoredRun?.autoUpdateSnapshot ?? false)
const snapshotAsOf = ref<string | null>(restoredRun?.snapshotAsOf ?? null)
// 自动更新状态轮询（更新中 → 10s，空闲 → 60s；卸载时清 timer）
const { autoUpdateRunning, refreshAutoUpdate, startAutoUpdatePolling } = useAutoUpdateRunning()

// 2026-08-27：运行按钮不再被少数股票的数据缺口全局禁用。
// 引擎会排除缺口股，后端只拦截真正的全库硬性阻断。
const runEnabled = computed(() => true)
// g5：披露告警与质量状态是「这次运行当时」的事实，切页回来必须原样保留 ——
// 否则回填的结果旁边会挂上现在的质量结论，等于把用户看到的披露偷偷改掉。
const warningCodes = ref<readonly WarningCode[]>(restoredRun?.warningCodes ?? [])
const qualityStatus = ref<'loading' | 'available' | 'failed'>(restoredRun?.qualityStatus ?? 'loading')
const dataReady = ref<boolean | null>(restoredRun?.dataReady ?? null)
// 2026-08-14 红队 P2-4：严格模式空结果反馈文案（来自 /run 响应）
const strictModeWarning = ref<string | null>(restoredRun?.strictModeWarning ?? null)
// 2026-11-23 后端新增：按设计豁免字段级 strict 校验的自算派生字段。
// 只用于把"这不是你的条件写错了"讲清楚，不影响筛选结果。
const strictExemptFields = ref<string[]>([...(restoredRun?.strictExemptFields ?? [])])

/**
 * 从 /run 响应里读一个**可选**字符串数组字段。
 *
 * `strict_exempt_fields` 是后端刚加的增量字段（旧后端没有）。响应类型
 * `ScreeningRunResponse` 是跨页面共享契约，本轮不改它，这里按 unknown 读、
 * 缺失时安全降级成空数组。
 */
function readOptionalStringList(payload: unknown, key: string): string[] {
  if (!payload || typeof payload !== 'object') return []
  const value = (payload as Record<string, unknown>)[key]
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string')
}

/**
 * 读 `/run` 结果体里的 disclose 级告警（后端已改，随 200 返回）。
 *
 * 两种形态都要兼容（都按 unknown 读，旧后端缺字段时安全降级成空数组）：
 * - `screening_warnings: [{code, severity, detail}]` —— 首选，带 detail，
 *   典型如「158/5,580 只标的日历覆盖不匹配（2.8%），其技术指标窗口计数可能偏移」；
 * - `disclosed_warnings: ["SNAPSHOT_STALE", ...]` —— 只有 code 的旧形态。
 *
 * 后端可能会同时给两份；只要 `screening_warnings` 有内容就用它（信息更全），
 * 否则退回 code 数组，避免把同一批告警渲染两遍。
 */
function readScreeningWarnings(payload: unknown): ScreeningWarning[] {
  if (!payload || typeof payload !== 'object') return []
  const record = payload as Record<string, unknown>
  const detailed = record.screening_warnings
  const collected: ScreeningWarning[] = []
  const seen = new Set<string>()
  const push = (item: ScreeningWarning) => {
    const key = item.code
    if (!key || seen.has(key)) return
    seen.add(key)
    collected.push(item)
  }
  if (Array.isArray(detailed)) {
    for (const item of detailed) {
      if (!item || typeof item !== 'object') continue
      const entry = item as Record<string, unknown>
      if (typeof entry.code !== 'string' || !entry.code) continue
      push({
        code: entry.code,
        severity: typeof entry.severity === 'string' ? entry.severity : undefined,
        detail: typeof entry.detail === 'string' && entry.detail.trim() ? entry.detail : undefined,
      })
    }
  }
  if (collected.length > 0) return collected
  for (const code of readOptionalStringList(record, 'disclosed_warnings')) {
    push({ code })
  }
  return collected
}

/**
 * 失败即清空：上一次运行派生出来的一切都不能继续代表"当前规则"。
 *
 * 只清结果还不够 —— 面板文案、数据日期、快照标注、strict 说明、披露告警都
 * 来自上一次响应；留着任何一项都会让用户把旧运行误读成当前运行。
 */
function clearRunArtifacts(): void {
  results.value = []
  totalMatched.value = 0
  runId.value = null
  truncated.value = false
  executionTime.value = 0
  basePoolSize.value = 0
  dataDate.value = null
  autoUpdateSnapshot.value = false
  snapshotAsOf.value = null
  readinessHint.value = null
  strictModeWarning.value = null
  strictExemptFields.value = []
  screeningWarnings.value = []
  // g5 关键：会话里的**上一次成功运行**也一起丢掉。只清页面 ref 的话，下一次
  // 切页回来会把它从模块单例重新回填 —— "失败即清空"等于没发生，用户会对着
  // 一份已经作废的结果点保存。
  clearRunSnapshot()
}

/**
 * 保存/导出时后端说"这次 run 已经不在了" → 必须落到失败态。
 *
 * 典型两句：`saved screening result not found`、`saved result rule provenance
 * is missing`。此时屏幕上的结果已无法被后端追溯，继续显示 = 让用户以为还能
 * 保存/导出（保存入口看到的 runId 是死的）。所以由面板发事件、页面清空并报失败。
 */
function handleRunInvalidated(reason: string): void {
  clearRunArtifacts()
  runState.value = 'failed'
  lastRunError.value = reason
  void nextTick(() => scrollToResults())
}

/**
 * 这次 `/run` **根本没执行成功**（= 旧结果没有被任何新结果取代，不能说它作废）。
 *
 * 2026-11-20 二次事故的修复点。此前 `catch` 无条件 `clearRunArtifacts()`，于是
 * 下面这些"没跑成"的失败也会把用户上一次的结果（连模块级会话快照）整份清掉：
 * - `409` + `DATA_UPDATE_IN_PROGRESS` / 写锁：后端明确说"现在不能跑"
 *   （`app/web/api/screening.py::run_screening` 的第三步，`reports/151`）。
 *   **服务端日志实证**（`data/logs/start.log`，用户实测窗口 19:42–19:46）：
 *   `筛选运行被写锁挡住且无上次成功结果` 连发 5 次，当时自动更新子进程正持 DuckDB 写锁。
 *   用户的操作就是"切回来 → 点运行 → 报错 → 结果全没了"。
 * - 网络中断 / 超时 / 主动取消：请求体都没走完，谈不上"本次结果作废"。
 *
 * 只有真正执行了、却产出不可信结果的失败（400 规则错误 / 500 / 质量门禁拒绝 /
 * 后端判 `saved result ... not found`）才继续走"失败即清空"。
 */
function isRunNotExecuted(e: unknown): boolean {
  if (!e || typeof e !== 'object') return false
  const record = e as { response?: { status?: unknown }; code?: unknown }
  const status = typeof record.response?.status === 'number' ? record.response.status : null
  // 409 = 数据更新中 / 写锁挡住 / 并发冲突：本次没执行（后端如实拒绝，不是"结果无效"）。
  if (status === 409 || status === 423 || status === 429) return true
  const code = typeof record.code === 'string' ? record.code : ''
  return code === 'ERR_NETWORK' || code === 'ECONNABORTED' || code === 'ERR_CANCELED' || code === 'ETIMEDOUT'
}

/**
 * 本次运行没跑成的**用户可见提示**（页面级，不动结果区）。
 *
 * 结果区继续原样显示上一次成功运行的结果，保存/导出/加入自选也照常可用——
 * 因为那份结果的 `run_id` + 规则溯源都仍然有效，后端照样认（与"结果过期"不同）。
 */
const runAttemptNotice = ref<string | null>(null)
/** 挂载期标记：用来判定"失败落地时用户是不是已经离开这一页"（切页导致的失败）。 */
let pageUnmounted = false

const basePool = reactive({ exclude_st: true, exclude_suspended: true, min_listing_years: 1 })
/**
 * 用户裁定：**去掉「仅使用严格可信数据」开关**，筛选口径固定为「包含近似数据」。
 *
 * 这里刻意保留一个**恒为 false** 的写入口，而不是把接线一并删掉：
 * `useScreeningDraft` 的草稿契约里仍有 `strict_only`（后端能力保留，见
 * `app/core/screening/engine.py`），历史草稿里可能残留 `strict_only: true`。
 * 载入草稿时 composable 会往这个 ref 写 true，但写入口吞掉赋值 → 归一成 false，
 * 下一次自动保存就把草稿写回 false。绝不能让一个"界面上看不见、也改不掉"的
 * 严格模式在用户不知情时悄悄生效。
 */
const strictOnly = computed<boolean>({ get: () => false, set: () => {} })
const inclusionOptions = [
  { label: '排除', value: 'exclude' },
  { label: '包含', value: 'include' },
]
function setStHandling(value: string) {
  basePool.exclude_st = value !== 'include'
}
function setSuspendedHandling(value: string) {
  basePool.exclude_suspended = value !== 'include'
}

const ruleTree = reactive<ScreeningRuleNode>({
  id: generateRuleId(),
  logic: 'AND',
  rules: [],
})

// 2026-11-16 用户裁定：**筛选阶段不再设置排序**。
// 排序是"看结果"的事——结果页每列表头都能点（ScreeningResultsPanel 的 sorter），
// 点一下升序、再点降序即可。这里不再保留任何排序状态。

const opOptions = [
  { label: '高于', value: '>' },
  { label: '低于', value: '<' },
  { label: '不低于', value: '>=' },
  { label: '不高于', value: '<=' },
  { label: '等于', value: '=' },
  { label: '不等于', value: '!=' },
  { label: '位于区间', value: 'between' },
  { label: '有数据', value: 'is_not_null' },
  { label: '无数据', value: 'is_null' },
]

/**
 * 下拉选项统一渲染（2026-11-16）。
 *
 * naive-ui 的 n-select 会把超长选项截成省略号且**没有 tooltip**，
 * 用户看到「原…」「全…」「证…」只能猜。这里用 NEllipsis 的 tooltip 模式：
 * 截断处仍显示省略号，鼠标悬浮弹出完整名称。
 */
function renderOptionLabel(option: { label?: string }): VNode {
  return h(
    NEllipsis,
    { tooltip: { placement: 'right', contentStyle: 'max-width: 520px; white-space: normal;' } },
    { default: () => option.label ?? '' },
  )
}

/** 排序行的参数化字段取值（如 CAGR 年数）。 */


/** 切换排序字段的参数值，并清掉另一个家族的旧参数。 */


// 后端就绪结论的新鲜度/降级提示（readiness_* 字段，reports/140 §15.5）
const readinessHint = ref<ReadinessNotice | null>(null)

const readinessCopy = computed(() => {
  if (qualityStatus.value === 'failed') return { state: 'failed', label: '状态读取失败' }
  if (dataReady.value === null) return { state: 'loading', label: '正在核对数据' }
  if (dataReady.value) return { state: 'ready', label: '数据已就绪' }
  // reports/79 方案 A: 更新窗口内快照口径可用
  if (autoUpdateRunning.value) return { state: 'ready', label: '更新中（快照口径）' }
  return { state: 'blocked', label: '数据尚未就绪' }
})

// 方案 C（2026-11-16）：后端不再下发 930 条排名派生字段（`pe_ttm_market_rank` …）。
//
// 2026-11-24 用户裁定：**条件行里不再有「排名口径」下拉**（详见
// `RuleConditionRow.vue` 顶部注释）。`rank_modes` 仍然从 /indicators 取，但只用于
// 两件"结果侧"的事：①「结果展示列」里可加一列排名/分位（`useScreeningColumns`）；
// ② 载入历史规则时把旧排名字段名迁移成 `{field, rank_mode}`（见下方
// `migrateLegacyRankFields`），让带排名口径的老规则语义不变。

// 指标全中文名（规则：display_name → FIELD_LABELS → 后端 label → 字段名 + 单位）
// 2026-11-22：纯函数已抽到 utils/screening-format.ts 的 indicatorDisplayName。
const indicatorOptions = computed(() =>
  indicators.value.map((i) => ({ label: indicatorDisplayName(i), value: i.name })),
)

// ─── 结果展示列（合成值编解码 / 下拉选项 / 条件自动并入）────────────────────
// 2026-11-22：列定义、合成值编解码、下拉选项整块抽到 composables/useScreeningColumns.ts。
// g4（2026-11-24）：传 `ruleTree` 进去 —— 条件里用到的字段会自动并入结果列
//（筛 ROCE 就要看得见 ROCE），并且用户手动删掉的条件列不会被加回来。
const {
  selectedResultColumns,
  manuallyRemovedResultColumns,
  resultColumnsSelection,
  resultColumnOptions,
  selectedColumnsForRequest,
} = useScreeningColumns({ indicators, rankModes, indicatorOptions, ruleTree })

/**
 * 结果列多选的中文兜底（reports/148）：旧规则里的排名字段不在下拉里时不能露英文。
 */
function fallbackResultColumnOption(value: string): { label: string; value: string } {
  return { label: fieldDisplayName(String(value), String(value)), value: String(value) }
}

/**
 * 把历史规则/草稿里的**旧排名字段名**迁移成新写法（reports/148）。
 *
 * 方案 C 之后 `/indicators` 不再下发 930 个 `*_rank` 扁平字段，改由条件级
 * `rank_mode` 表达。但库里的草稿/旧规则仍可能存着 `roce_10y_median_industry_rank`
 * 这类名字 —— 下拉里找不到 → naive-ui 兜底渲染原值 → 界面显示英文。
 *
 * 迁移规则：`<base>_<suffix>` 且 `base` 在当前指标清单里、`suffix` 属于当前
 * `rank_modes` → 改写成 `{ field: base, rank_mode: suffix }`，语义完全等价
 * （后端 `expand_rank_modes` 会展开回同一个字段名）。
 * `sw1_*` 已不在 rank_modes 里 → **不迁移**，靠 `fieldDisplayName` 的中文兜底显示。
 *
 * 2026-11-19 合并 main：本函数原本写在页面的 restoreDraft/applyLoadedRule 旁边，
 * 但那两块已抽进 composable，所以改为**由页面注入**给 composable（时机不变）。
 */
function migrateLegacyRankFields(node: unknown): void {
  if (Array.isArray(node)) {
    for (const item of node) migrateLegacyRankFields(item)
    return
  }
  if (!node || typeof node !== 'object') return
  const record = node as Record<string, unknown>
  const field = record.field
  if (typeof field === 'string' && !record.rank_mode) {
    const modeValues = new Set(rankModes.value.map((item) => String(item.value)))
    for (const mode of modeValues) {
      const suffix = `_${mode}`
      if (!field.endsWith(suffix)) continue
      const base = field.slice(0, -suffix.length)
      if (base && indicators.value.some((item) => item.name === base)) {
        record.field = base
        record.rank_mode = mode
      }
      break
    }
  }
  for (const value of Object.values(record)) {
    if (value !== record.field && (Array.isArray(value) || (value && typeof value === 'object'))) {
      migrateLegacyRankFields(value)
    }
  }
}

/** 某字段的参数规格（如 CAGR 年数）；无参数返回 null。 */
function paramSpecOf(field: string) {
  return indicators.value.find((i) => i.name === field)?.param_spec ?? null
}

/** 某字段是否支持排名口径：后端 `rankable` 标记。 */
function rankableOf(field: string): boolean {
  return indicators.value.some((i) => i.name === field && i.rankable)
}


const resultColumns = computed(() => {
  if (!results.value.length) return []
  return Object.keys(results.value[0])
})

// ─── 当前报告期不可用指标（unavailable）────────────────────────────────
// 5 个展示函数（unavailableName/Label/ReasonText/CoverageText/Key）与
// UNAVAILABLE_REASON_LABELS 已抽到 utils/screening-unavailable.ts（纯函数，可单测）。

const ruleFields = computed(() => collectRuleFields(ruleTree))

const untrustedFields = computed(() =>
  computeUntrustedFields({
    ruleFields: ruleFields.value,
    resultColumns: resultColumns.value,
    warningCodes: warningCodes.value,
  }),
)



// 2026-08-14 红队 P2-20 / 2026-08-26：运行前检测「编辑区 vs 已保存版本」差异
// （比较时忽略条件节点 id）。实现已随规则 CRUD 抽到 useScreeningRules。

function scrollToResults(): void {
  resultsAnchor.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function runScreening() {
  // 2026-08-26 UX：没有已保存规则时不再要求用户手动命名/先保存，
  // 自动把当前草稿保存为一个可追溯规则版本，然后直接运行。
  if (activeRule.value === null) {
    if (!ruleName.value.trim()) {
      ruleName.value = autoRuleName()
    }
    const saved = await saveRule()
    if (!saved || activeRule.value === null) {
      message.error('自动保存规则失败，请刷新后重试或手动输入规则名称')
      return
    }
  }
  // 如果当前编辑区相对已保存规则有修改，也自动保存为新版本再运行，
  // 避免“屏幕条件与实际运行条件不一致”的困扰。
  if (isDraftDirty()) {
    if (!ruleName.value.trim()) {
      ruleName.value = activeRule.value?.name || autoRuleName()
    }
    const saved = await saveRule()
    if (!saved || activeRule.value === null) {
      message.error('自动保存规则失败，请刷新后重试或手动输入规则名称')
      return
    }
  }
  loading.value = true
  // 进入 running：旧结果暂时留在屏幕上，但会由面板显式标注「上一次运行的结果」
  // 并禁用保存/导出/加入自选；本次**真跑坏**才整体清空，**没跑成**（409 写锁 /
  // 网络中断 / 切页中断）保留旧结果 —— 见 catch 里的 isRunNotExecuted()。
  runState.value = 'running'
  lastRunError.value = null
  // 新一次尝试开始：上一次"没跑成"的提示作废（成功与真失败都会重写它）。
  runAttemptNotice.value = null
  // g5：在飞的运行也要跨挂载保住 —— 切走再切回来时按钮仍是 loading，
  // 且不会因为重新挂载而再发一次 /run。
  screeningSession.running = true
  // 这次运行到底按哪个规则版本跑的：必须在发请求前定格，await 期间用户可能保存
  // 新版本。结果面板的「执行规则」与保存入口都以这份定格为准，否则结果会对错规则。
  const runRule = activeRule.value
  try {
    const resp = await axios.post<ScreeningRunResponse>('/api/screening/run', {
      rule_id: activeRule.value.id,
      rule_version: activeRule.value.version,
      include_st: !basePool.exclude_st,
      include_suspended: !basePool.exclude_suspended,
      min_listing_years: basePool.min_listing_years,
      // 用户裁定：严格模式开关已移除，口径固定「包含近似数据」。这里写死 false，
      // 即使历史草稿/已保存规则里残留 `strict_only: true` 也不会把请求带成严格口径。
      strict_only: false,
      columns: selectedColumnsForRequest(),
    }, {
      // 后端筛选门禁在无缓存冷启动时需执行一次全库数据质量核对；
      // 该请求单独放宽超时，避免默认 30s 把仍在正常计算的服务端请求
      // 从浏览器侧中止，造成“一直 loading 却永远收不到结果”。
      timeout: 120_000,
    })
    // 响应缺字段时按空处理：宁可显示 0 条，也不能让 undefined 进模板。
    const rows: readonly ScreeningResult[] = Array.isArray(resp.data.results) ? resp.data.results : []
    results.value = rows
    executionTime.value = resp.data.execution_time_ms
    basePoolSize.value = resp.data.base_pool_size
    dataDate.value = resp.data.data_date
    runId.value = resp.data.run_id
    truncated.value = resp.data.truncated ?? false
    totalMatched.value = typeof resp.data.total === 'number' ? resp.data.total : rows.length
    // reports/79 方案 A: 更新窗口内基于快照的运行，明确告知口径
    autoUpdateSnapshot.value = resp.data.auto_update_in_progress === true
    readinessHint.value = readinessNotice(resp.data)
    snapshotAsOf.value = resp.data.data_as_of ?? null
    // 2026-08-14 红队 P2-4：严格模式空结果的原因反馈（无 strict 血缘等）
    strictModeWarning.value = resp.data.strict_mode_warning ?? null
    strictExemptFields.value = readOptionalStringList(resp.data, 'strict_exempt_fields')
    // 2026-11-24（reports/150）：disclose 级告警随 200 一起返回 —— 必须看见，但不挡路。
    screeningWarnings.value = readScreeningWarnings(resp.data)
    // 一次性把整份可展示产物写进模块级会话：组件随后被卸载（用户切到别的页）
    // 也不影响，切回来时 setup 会原样回填 —— 包括 runId、披露告警与定格规则。
    saveRunSnapshot({
      results: rows,
      runId: runId.value,
      totalMatched: totalMatched.value,
      executionTime: executionTime.value,
      basePoolSize: basePoolSize.value,
      dataDate: dataDate.value,
      truncated: truncated.value,
      autoUpdateSnapshot: autoUpdateSnapshot.value,
      snapshotAsOf: snapshotAsOf.value,
      readinessHint: readinessHint.value,
      strictModeWarning: strictModeWarning.value,
      strictExemptFields: strictExemptFields.value,
      screeningWarnings: screeningWarnings.value,
      warningCodes: warningCodes.value,
      qualityStatus: qualityStatus.value,
      dataReady: dataReady.value,
      // 用户裁定：严格模式开关已移除，口径固定「包含近似数据」。快照里如实写 false，
      // 面板的 strict 说明仍由后端自己下发的信号驱动。
      strictOnly: false,
      context: {
        ruleId: runRule?.id ?? null,
        ruleVersion: runRule?.version ?? null,
        ruleName: runRule?.name ?? '',
      },
    })
    // 成功也要区分「有结果」与「成功但 0 条」：后者不是错误，是条件太严。
    runState.value = rows.length > 0 ? 'ok' : 'empty'
    if (resp.data.truncated) {
      message.warning(`结果超过 5000 条，仅展示前 5000 条（共 ${resp.data.total} 条匹配）`)
    }
    if (resp.data.auto_update_in_progress) {
      message.info(`数据自动更新中：本次筛选基于最近完整快照（数据截至 ${resp.data.data_as_of ?? dataDate.value}）`)
    }
    if (rows.length === 0) {
      message.info(`筛选完成：没有公司符合当前条件（共 ${totalMatched.value} 条匹配）。可以放宽阈值后重新运行。`)
    } else {
      message.success(`筛选完成: ${totalMatched.value} 条 (${resp.data.execution_time_ms}ms)，已滚动到结果区`)
    }
    await nextTick()
    scrollToResults()
  } catch (e: unknown) {
    // P0：**真正跑坏了**（后端执行过、但产物不可信）才算"上一次结果作废"。
    // 2026-11-20 二次事故：这里原先无条件清空，于是 409「数据正在自动更新/写锁被挡住」
    // 这种"根本没执行"的拒绝也会把用户上一次的结果连同会话快照一起清掉 —— 用户切页
    // 回来点一次运行就整页变空，读到的就是「切页把结果弄没了」。见 isRunNotExecuted()。
    const reason = apiErrorText(e, '筛选失败')
    const keepPrevious = results.value.length > 0 && (pageUnmounted || isRunNotExecuted(e))
    if (keepPrevious) {
      // 结果原样保留：runState 回到"这批结果仍然有效"的态（那份 runId 与规则溯源
      // 仍然被后端承认，保存/导出/加入自选照旧可用），失败只用页面级提示讲清楚"这次没跑"。
      runState.value = results.value.length > 0 ? 'ok' : 'empty'
      runAttemptNotice.value = `${reason}。本次运行没有执行，下面仍然是上一次成功运行的结果（可照常保存/导出），稍后重试即可。`
      if (!pageUnmounted) message.warning(runAttemptNotice.value)
    } else {
      clearRunArtifacts()
      runState.value = 'failed'
      lastRunError.value = reason
      runAttemptNotice.value = null
      if (!pageUnmounted) message.error(reason)
    }
    if (!pageUnmounted) {
      await nextTick()
      scrollToResults()
    }
  } finally {
    loading.value = false
    screeningSession.running = false
  }
}

// L0-3（报告42）原本在这里挂了一个「strict-only 切换时自动按服务端口径重跑」的
// 防抖 watch。用户裁定移除严格模式开关后，页面内不再有任何东西能改变 strict_only
// → 该 watch 永远不可能触发，随开关一起删除（重跑逻辑仍由「运行筛选」按钮承担）。

// ─── 筛选草稿（/api/screening/draft）────────────────────────────────────
// 2026-11-22：草稿状态机（快照脏检查 / 单飞保存 / 409 冲突弹窗 / 防抖 400ms）
// 整块抽到 composables/useScreeningDraft.ts，行为逐字等价（有单测）。
const {
  showDraftConflictModal,
  syncEditorSnapshot,
  hasUnsavedEdits,
  resolveDraftConflict,
  restoreDraft,
  markDraftHydrated,
} = useScreeningDraft({
  migrateConditions: migrateLegacyRankFields,
  ruleTree,
  basePool,
  strictOnly,
  selectedResultColumns,
  // g4：手动移除过的条件自动列要跟着草稿走，否则换个页面/刷新回来会被强行加回。
  removedResultColumns: manuallyRemovedResultColumns,
  defaultResultColumns: DEFAULT_RESULT_COLUMNS,
  // g4：默认列版本号 —— 旧草稿若是「旧 8 列 / 旧 12 列默认集合」，载入时迁移成 15 列。
  defaultResultColumnsVersion: DEFAULT_RESULT_COLUMNS_VERSION,
  encodeResultColumn,
  notify: message,
})

// ─── 已保存规则 CRUD / 装载 ────────────────────────────────────────────
// 2026-11-22：整块抽到 composables/useScreeningRules.ts（行为逐字等价，有单测）。
const {
  selectedRuleId,
  activeRule,
  ruleName,
  ruleOptions,
  savedRules,
  loadSavedRules,
  isDraftDirty,
  autoRuleName,
  saveRule,
  deleteSelectedRule,
  loadRule,
} = useScreeningRules({
  migrateConditions: migrateLegacyRankFields,
  ruleTree,
  selectedResultColumns,
  defaultResultColumns: DEFAULT_RESULT_COLUMNS,
  selectedColumnsForRequest,
  syncEditorSnapshot,
  hasUnsavedEdits,
  notify: message,
  dialog,
})

// ─── 结果面板的"这批结果是谁跑出来的"（g5）──────────────────────────────
// 切页回来时 `savedRules` 要重新拉取（`activeRule` 会短暂为 null），若面板直接吃
// `activeRule`，回填结果的头几百毫秒里 ruleId 是 null → 保存入口被误置灰闪一下。
// 有历史结果时以运行快照里定格的为准；没有历史结果时保持原行为（跟随编辑区当前规则）。
const runRuleId = computed(() => screeningSession.run?.context.ruleId ?? activeRule.value?.id ?? null)
const runRuleVersion = computed(
  () => screeningSession.run?.context.ruleVersion ?? activeRule.value?.version ?? null,
)
const runRuleName = computed(
  () => screeningSession.run?.context.ruleName ?? activeRule.value?.name ?? '',
)

onMounted(async () => {
  // 2026-11-24「切页保状态」：挂载时**不再重跑任何会产生结果的请求**。
  // 这里只做两件"不改变已跑结果"的事：恢复草稿（编辑区，服务端持久化）
  // 与重取规则/指标目录（它们只影响下拉选项）。
  // 关键不变量：这里没有任何代码把 results/runId 置空，也没有任何一次 /run。
  await restoreDraft()
  markDraftHydrated()
  await loadSavedRules()

  // 切回来时把编辑区重新绑回上次选中的规则版本：否则 selectedRuleId 归零 →
  // activeRule 变 null → 结果面板拿到 ruleId=null → 保存入口被误置灰。
  if (screeningSession.ruleName) ruleName.value = screeningSession.ruleName
  const lastRuleId = screeningSession.activeRuleId
  if (lastRuleId > 0 && savedRules.value.some((rule) => rule.id === lastRuleId)) {
    selectedRuleId.value = lastRuleId
  }

  try {
    const resp = await axios.get<{
      indicators?: readonly ScreeningIndicator[]
      unavailable?: readonly UnavailableIndicator[]
      rank_modes?: ReadonlyArray<{ value: string; label: string }>
    }>('/api/screening/indicators')
    // 降级：旧后端 / 抓包数据的 `indicators` 或 `unavailable` 缺失时都不崩。
    const loadedIndicators = Array.isArray(resp.data.indicators) ? resp.data.indicators : []
    indicators.value = loadedIndicators
    unavailableIndicators.value = Array.isArray(resp.data.unavailable) ? resp.data.unavailable : []
    // 方案 C：排名口径由后端下发，前端不再自己拼 6 个后缀
    rankModes.value = Array.isArray(resp.data.rank_modes)
      ? resp.data.rank_modes.map((m) => ({ label: m.label, value: m.value }))
      : []
    // 2026-08-14 红队 F3：应用后端下发的单位元数据（单一来源），
    // 修复小数/百分数双口径下条件输入与结果展示的 100 倍误差。
    const units: Record<string, string> = {}
    for (const indicator of loadedIndicators) {
      if (indicator.unit) units[indicator.name] = indicator.unit
    }
    applyIndicatorUnits(units)
  } catch {
    message.warning('无法加载指标列表')
  }

  // 数据质量披露（warning_codes / 就绪结论）是**这次运行当时**的披露内容。
  // 本文档里已经跑过结果时不再重取：否则切回来会拿"现在的"状态覆盖结果旁边的
  // 告警，等于把用户看到的披露偷偷改掉（用户要的是"界面保持在那里"）。
  // 真要看最新数据状态，用户会去数据状态页，或者重新运行。
  if (restoredRun === null) {
    try {
      const resp = await axios.get<DataStatusSummaryResponse>('/api/data-status/summary')
      warningCodes.value = resp.data.data_quality.warning_codes
      dataReady.value = resp.data.data_quality.minimum_data_readiness.ready
      qualityStatus.value = 'available'
    } catch {
      dataReady.value = null
      qualityStatus.value = 'failed'
      message.warning('无法加载数据质量状态')
    }
  }

  // reports/79 方案 A: 更新窗口内允许以最近完整快照运行（后端同口径）
  refreshAutoUpdate()
  // 2026-08-14 红队 P3：自动更新状态不再只在挂载时读一次——
  // 更新窗口（数分钟级）内运行按钮会随状态自动切换可用性。
  // 2026-11-22：轮询+单飞+卸载清理已抽到 composables/useAutoUpdatePolling.ts。
  startAutoUpdatePolling(10000)
})

// 编辑区绑定（当前规则 / 规则名）随会话保存：切走再回来，"当前规则"仍是同一个版本。
// 编辑区绑定随会话落盘：切页（含文档重新加载）回来仍是同一个规则版本。
watch([selectedRuleId, ruleName], ([id, name]) => { rememberEditorBinding(id, name) })

// 切页标记：用于区分"用户在别的页面时请求才失败"（这类失败绝不清空结果）。
onUnmounted(() => {
  pageUnmounted = true
})
</script>

<template>
  <section class="screening-page">
    <header class="screening-page-header">
      <div>
        <p>研究工具 / 筛选</p>
        <h1>条件筛选</h1>
        <span>建立研究规则，找到值得进一步阅读的公司。</span>
      </div>
    </header>

    <div class="screening-workspace">
      <section class="screening-editor-card">
        <div class="screening-card-heading">
          <div><p>RESEARCH RULE / DRAFT</p><h2>{{ activeRule?.name || '未命名筛选草稿' }}</h2><span>定义股票池、写下判断，再保存为可追溯版本。</span></div>
        </div>
        <div class="rule-load-row">
          <span>已保存规则</span>
          <n-select
            :value="selectedRuleId"
            :options="ruleOptions"
            size="small"
            :render-label="renderOptionLabel"
            @update:value="loadRule"
          />
          <n-input v-model:value="ruleName" aria-label="规则名称" size="small" placeholder="规则名称" />
          <n-button size="small" @click="saveRule">保存新版本</n-button>
          <n-button v-if="selectedRuleId > 0" size="small" quaternary type="error" @click="deleteSelectedRule">删除选中规则</n-button>
        </div>

        <div class="screening-section">
          <div class="screening-section-title screening-section-title--conditions"><div><b>01</b><h3>筛选条件</h3></div><p>范围条件始终生效；投资条件可选择全部成立或任一成立。</p></div>
          <div class="standing-conditions" aria-label="常驻范围条件">
            <div class="standing-conditions-heading">
              <div><strong>常驻范围条件</strong><span>固定参与基础股票池计算</span></div>
              <span>始终并且</span>
            </div>
            <div class="standing-condition-line">
              <div class="standing-condition-name"><span>股票状态</span><strong>ST 股票</strong></div>
<label><span>处理方式</span><n-select :value="basePool.exclude_st ? 'exclude' : 'include'" :options="inclusionOptions" size="small" aria-label="ST 股票处理方式" @update:value="setStHandling" /></label>
              <p>{{ basePool.exclude_st ? '排除名称含 ST、*ST 的股票' : '允许 ST 股票进入基础股票池' }}</p>
            </div>
            <div class="standing-condition-line">
              <div class="standing-condition-name"><span>交易状态</span><strong>停牌股票</strong></div>
              <label><span>处理方式</span><n-select :value="basePool.exclude_suspended ? 'exclude' : 'include'" :options="inclusionOptions" size="small" aria-label="停牌股票处理方式" @update:value="setSuspendedHandling" /></label>
              <p>{{ basePool.exclude_suspended ? '排除当前停牌股票' : '允许停牌股票进入基础股票池' }}</p>
            </div>
            <div class="standing-condition-line">
              <div class="standing-condition-name"><span>上市时间</span><strong>最低上市年限</strong></div>
              <label><span>不少于</span><n-input-number v-model:value="basePool.min_listing_years" :min="0" :max="100" size="small" aria-label="最低上市年限" /></label>
              <p>上市满 {{ basePool.min_listing_years }} 年后进入基础股票池</p>
            </div>
          </div>
          <div class="conditions-join" aria-hidden="true"><span>并且</span></div>
          <ScreeningRuleEditor class="conditions-workbench" :node="ruleTree" :depth="1" :max-depth="3" :max-conditions="20" :is-root="true" :indicator-options="indicatorOptions" :op-options="opOptions" :rankable-of="rankableOf" :param-spec-of="paramSpecOf" @warn="(msg: string) => message.warning(msg)" />
          <div v-if="trulyUnavailableIndicators.length > 0" class="unavailable-indicators" aria-label="当前报告期不可用指标">
            <div class="unavailable-indicators-heading">
              <strong>当前报告期不可用指标（{{ trulyUnavailableIndicators.length }}）</strong>
              <span>这些字段已从可选指标中移除；悬停查看原因与覆盖率。</span>
            </div>
            <div class="unavailable-indicators-list">
              <n-tooltip
                v-for="item in trulyUnavailableIndicators"
                :key="unavailableKey(item)"
                trigger="hover"
                placement="top"
              >
                <template #trigger>
                  <span class="unavailable-chip">
                    <span class="unavailable-chip-name">{{ unavailableLabel(item) }}</span>
                    <span class="unavailable-chip-coverage">{{ unavailableCoverageText(item) }}</span>
                  </span>
                </template>
                <div class="unavailable-tooltip">
                  <b>{{ unavailableLabel(item) }}</b>
                  <p>原因：{{ unavailableReasonText(item) }}</p>
                  <p>{{ unavailableCoverageText(item) }}</p>
                  <p v-if="item.message" class="unavailable-tooltip-message">{{ item.message }}</p>
                  <p v-if="item.replaced_by" class="unavailable-tooltip-message">替代字段：{{ item.replaced_by }}</p>
                </div>
              </n-tooltip>
            </div>
          </div>
          <!-- 已改名的字段单独提示，不再混进「不可用」计数（reports/148） -->
          <p v-if="renamedIndicators.length > 0" class="renamed-indicators-note">
            已改名（同一指标换了列名，不影响使用）：
            <template v-for="(item, index) in renamedIndicators" :key="unavailableKey(item)">
              <span v-if="index > 0">、</span>
              <b>{{ unavailableLabel(item) }}</b>
              <span v-if="item.replaced_by"> → {{ fieldDisplayName(item.replaced_by) }}</span>
            </template>
          </p>
        </div>

        <div class="screening-section">
          <div class="screening-section-title"><div><b>02</b><h3>结果展示列</h3></div><p>运行前可自由增加或删减展示信息；修改展示列不会产生新的规则版本。</p></div>
          <!-- g4：条件是"筛什么"，列是"看什么"。条件里用到的字段会自动并入这张表
               （排在默认列之后），所以这里展示的是"实际生效"的列集合
               （`resultColumnsSelection` = 用户手选/默认列 + 条件自动列 − 手动移除）。 -->
          <n-select
            v-model:value="resultColumnsSelection"
            :options="resultColumnOptions"
            :fallback-option="fallbackResultColumnOption"
            multiple
            filterable
            clearable
            :render-label="renderOptionLabel"
            size="small"
            placeholder="选择要在结果表展示的信息"
          />
          <p class="result-columns-note">
            条件里用到的字段会自动加入结果列（排在默认列之后）；手动删掉的条件列不会被自动加回。
          </p>
        </div>

        <DslIndicatorManager class="dsl-manager" />
      </section>

      <aside class="screening-run-panel">
        <div class="screening-ready" :data-state="readinessCopy.state"><i></i>{{ readinessCopy.label }}</div>
        <!-- reports/140 §15.5：就绪结论打折时必须让人看见，别只写日志 -->
        <n-alert
          v-if="readinessHint"
          class="screening-readiness-hint"
          :type="readinessHint.level === 'degraded' ? 'warning' : 'info'"
          :bordered="false"
          size="small"
        >{{ readinessHint.text }}</n-alert>
        <div class="screening-run-title"><p>当前规则</p><h2>{{ activeRule?.name || '未命名筛选草稿' }}</h2></div>
        <div class="screening-run-data"><p><span>筛选条件</span><b>{{ ruleFields.size }} 项</b></p><p><span>可信度</span><b>包含近似值</b></p><p><span>数据日期</span><b>{{ dataDate || '运行后确认' }}</b></p></div>
        <!-- 用户裁定：这里原来是「仅使用严格可信数据 / 包含近似可信数据」二选一的 n-switch
             （右侧那个绿色药丸就是它），已整个移除。口径固定，只保留一句纯文字说明。 -->
        <p class="screening-policy-note">口径固定：结果包含近似值，口径不完整或近似计算的指标照常参与筛选。</p>
        <n-button type="primary" block :loading="loading" :disabled="!runEnabled" @click="runScreening">运行筛选 →</n-button>
        <n-button v-if="results.length > 0" text type="primary" block style="margin-top: 8px" @click="scrollToResults">
          {{ runState === 'running' ? '查看上一次筛选结果' : '查看本次筛选结果' }}（{{ totalMatched || results.length }} 条）↓
        </n-button>
        <p v-if="autoUpdateRunning && dataReady === false" class="screening-run-help">数据正在自动更新：将以最近完整快照运行（后端将标注数据截至日期）。</p>
        <p v-else class="screening-run-help">运行结果可保存、导出或加入自选列表。</p>
      </aside>
    </div>

    <n-alert v-if="autoUpdateSnapshot" type="info" :show-icon="true" class="screening-snapshot-note">
      数据正在自动更新：本次结果基于最近完整快照（数据截至 {{ snapshotAsOf || dataDate || '—' }}），更新完成后重新运行即可获得最新数据。
    </n-alert>
    <!-- 「这次没跑成，但结果还在」：409 数据更新中 / 写锁挡住 / 网络中断 / 切页中断。
         绝不换掉结果区，只在它上面加一条说明（保存/导出/加入自选照旧可用）。 -->
    <n-alert
      v-if="runAttemptNotice"
      id="run-attempt-notice"
      type="warning"
      :show-icon="true"
      closable
      class="screening-run-attempt-notice"
      @close="runAttemptNotice = null"
    >{{ runAttemptNotice }}</n-alert>
    <div ref="resultsAnchor" class="screening-results-anchor">
      <ScreeningResultsPanel
        :results="results"
        :run-state="runState"
        :last-run-error="lastRunError"
        :screening-warnings="screeningWarnings"
        :strict-mode-warning="strictModeWarning"
        :strict-exempt-fields="strictExemptFields"
        :execution-time="executionTime"
        :base-pool-size="basePoolSize"
        :data-date="dataDate"
        :warning-codes="warningCodes"
        :untrusted-fields="untrustedFields"
        :quality-status="qualityStatus"
        :rule-tree="ruleTree"
        :run-id="runId"
        :rule-id="runRuleId"
        :rule-version="runRuleVersion"
        :rule-name="runRuleName"
        :locked-indicators="activeRule?.locked_indicators ?? {}"
        :base-pool-config="basePool"
        :truncated="truncated"
        :total-matched="totalMatched"
        :run-expired="runExpired"
        @retry="runScreening"
        @run-invalidated="handleRunInvalidated"
        @run-expired="markRunExpired"
      />
    </div>

    <!-- 三种"空"必须长得不一样（P0）：
         ① idle = 没跑过（给首次步骤）；② empty = 跑成功但 0 条（条件太严，不是故障）；
         ③ failed = 跑失败（原因 + 重试）—— 失败卡由 ScreeningResultsPanel 渲染在结果区，
            因为那正是"上一次结果曾经出现的地方"，用户第一眼就该看到它作废了。 -->
    <section
      v-if="results.length === 0 && runState !== 'failed'"
      class="screening-empty-card"
      :data-run-state="runState"
    >
      <template v-if="runState === 'running'">
        <n-empty id="run-running-state" description="正在运行筛选，请稍候…" />
        <div class="first-screening-help">
          <p>后端正在按当前规则计算（冷启动时首次核对数据质量可能需数十秒）。</p>
        </div>
      </template>
      <template v-else-if="runState === 'empty'">
        <n-empty id="run-empty-state" description="筛选完成：没有公司符合当前条件（0 条）" />
        <div class="first-screening-help">
          <p><strong>这不是出错</strong>：规则已完整执行，只是命中 0 家。常见原因是条件太严。</p>
          <ol>
            <li>放宽阈值（例如把 pe_ttm &lt; 15 改成 &lt; 25）。</li>
            <li>减少「并且」条件，或把条件组改成「任一成立」。</li>
            <li>检查常驻范围条件（ST、停牌、上市年限）是否把股票池收得太小。</li>
          </ol>
          <p>改完条件后直接点<strong>运行筛选</strong>即可，不需要刷新页面。</p>
        </div>
      </template>
      <template v-else>
        <n-empty id="run-idle-state" description="还没有运行筛选" />
        <!-- L0-1（报告42）: 空态给出可执行的首次筛选步骤，而非只提示"运行筛选" -->
        <div class="first-screening-help">
        <n-alert
          v-if="qualityStatus === 'failed'"
          type="error"
          :show-icon="true"
        >
          无法获取数据质量状态，运行结果可能不可信。请先前往<router-link to="/data-status">数据状态页</router-link>确认。
        </n-alert>
        <ol>
          <li>在「筛选条件」设置常驻范围条件（ST、停牌、上市年限）与投资判断，并选择数据质量口径。</li>
          <li>在「筛选条件」添加条件（如 pe_ttm &lt; 15）；排序不在这里设置 —— 结果表里点表头即可升序/降序。</li>
          <li>点击<strong>运行筛选</strong>——系统会自动把当前草稿保存为一个规则版本，用于后续溯源。</li>
        </ol>
        <div>
          之后可保存结果、导出 CSV（含数据日期与规则溯源）或加入自选。
        </div>
        </div>
      </template>
    </section>

    <!-- L0-4（报告42）: 草稿冲突恢复——加载服务器/保留本地/刷新 -->
    <n-modal
      v-model:show="showDraftConflictModal"
      preset="dialog"
      type="warning"
      title="草稿冲突"
      content="筛选草稿已在其他页面更新。为避免丢失编辑，请选择处理方式："
    >
      <template #action>
        <n-button size="small" @click="resolveDraftConflict('refresh')">刷新页面</n-button>
        <n-button size="small" @click="resolveDraftConflict('server')">加载服务器草稿</n-button>
        <n-button size="small" type="primary" @click="resolveDraftConflict('local')">保留本地副本</n-button>
      </template>
    </n-modal>
  </section>
</template>

<style scoped>
.screening-page { max-width: 1380px; color: #202622; }
.screening-page-header { margin-bottom: 27px; }
.screening-page-header p { margin: 0 0 8px; color: #97a199; font-size: 10px; }
.screening-page-header h1 { margin: 0; font-size: 25px; letter-spacing: -.05em; }
.screening-page-header span { display: block; margin-top: 7px; color: #829087; font-size: 12px; }
.screening-workspace { display: grid; grid-template-columns: minmax(650px, 1.72fr) minmax(276px, .58fr); gap: 21px; }
.screening-editor-card, .screening-empty-card { border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }
.screening-editor-card { padding: 28px 29px; }
.screening-card-heading h2 { margin: 7px 0 5px; font-size: 19px; letter-spacing: -.04em; }
.screening-card-heading p { margin: 0; color: #91a097; font-size: 9px; font-weight: 800; letter-spacing: .13em; }
.screening-card-heading span { color: #829087; font-size: 11px; }
.rule-load-row { display: grid; grid-template-columns: auto minmax(180px, 1fr) minmax(150px, .65fr) auto; align-items: center; gap: 10px; margin: 25px 0; padding: 13px 14px; border-radius: 9px; background: #fafcf9; }
.rule-load-row > span, .control-label { color: #89958c; font-size: 10px; }
.screening-section { padding: 21px 0; border-top: 1px solid #edf1ee; }
.rule-load-row + .screening-section { border-top: 1px solid #edf1ee; }
.screening-section-title > div { display: flex; align-items: baseline; gap: 11px; }
.screening-section-title b { color: #83b194; font-size: 10px; }
.screening-section-title h3 { margin: 0 0 13px; font-size: 13px; }
.screening-section-title--conditions { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; }
.screening-section-title--conditions p { max-width: 390px; margin-bottom: 13px; color: #929d95; font-size: 10px; text-align: right; }
.standing-conditions { overflow: hidden; border: 1px solid #e6ece7; border-radius: 9px; background: #fff; }
.standing-conditions-heading { display: flex; align-items: center; justify-content: space-between; padding: 11px 14px; background: #fafcf9; }
.standing-conditions-heading div { display: flex; align-items: baseline; gap: 8px; }
.standing-conditions-heading strong { color: #506358; font-size: 12px; }
.standing-conditions-heading div span { color: #98a39b; font-size: 9px; }
.standing-conditions-heading > span { padding: 3px 8px; border-radius: 999px; background: #edf7ef; color: #609574; font-size: 9px; }
.standing-condition-line { display: grid; grid-template-columns: minmax(150px, .75fr) minmax(140px, .55fr) minmax(220px, 1fr); align-items: center; gap: 16px; min-height: 62px; padding: 0 14px; border-top: 1px solid #eff2f0; }
.standing-condition-name span, .standing-condition-line label > span { display: block; margin-bottom: 3px; color: #98a39b; font-size: 9px; }
.standing-condition-name strong { color: #34443a; font-size: 12px; }
.standing-condition-line label { display: grid; grid-template-columns: 54px minmax(86px, 1fr); align-items: center; }
.standing-condition-line label > span { margin: 0; }
.standing-condition-line p { color: #7e8a82; font-size: 10px; }
.conditions-join { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 10px; height: 36px; color: #73917d; font-size: 9px; font-weight: 700; }
.conditions-join::before, .conditions-join::after { content: ''; border-top: 1px solid #edf1ee; }
.conditions-workbench { overflow: hidden; border-radius: 9px; }
.unavailable-indicators { margin-top: 14px; padding: 12px 14px; border: 1px dashed #e2e7e3; border-radius: 9px; background: #fafbfa; }
.unavailable-indicators-heading { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; }
.unavailable-indicators-heading strong { color: #8a958d; font-size: 11px; }
.unavailable-indicators-heading span { color: #a3ada6; font-size: 9px; }
.renamed-indicators-note { margin: 9px 0 0; color: #8b978f; font-size: 10px; }
.renamed-indicators-note b { color: #6d7b72; font-weight: 600; }
.unavailable-indicators-list { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 9px; }
.unavailable-chip { display: inline-flex; align-items: baseline; gap: 7px; padding: 4px 9px; border-radius: 999px; background: #f0f2f0; color: #9aa69f; cursor: help; font-size: 10px; opacity: .85; }
.unavailable-chip-name { text-decoration: line-through; text-decoration-color: #c3ccc5; }
.unavailable-chip-coverage { color: #b0b8b2; font-size: 9px; font-variant-numeric: tabular-nums; }
.unavailable-tooltip { max-width: 320px; font-size: 11px; line-height: 1.6; }
.unavailable-tooltip b { display: block; margin-bottom: 4px; }
.unavailable-tooltip p { margin: 2px 0; }
.unavailable-tooltip-message { color: #d9d2b8; }
.dsl-manager { margin-top: 4px; }
.result-columns-note { margin: 8px 0 0; color: #9aa69f; font-size: 10px; line-height: 1.5; }
.screening-run-panel { position: sticky; top: 28px; align-self: start; padding: 25px; border-radius: 16px; background: #eff7f1; box-shadow: 0 5px 17px rgba(47, 114, 74, .055); }
.screening-ready { display: flex; align-items: center; gap: 6px; color: #659d75; font-size: 10px; }
.screening-ready i { width: 6px; height: 6px; border-radius: 50%; background: #82ba94; }
.screening-ready[data-state='blocked'], .screening-ready[data-state='failed'] { color: #bd665d; }
.screening-ready[data-state='blocked'] i, .screening-ready[data-state='failed'] i { background: #d37869; }
.screening-ready[data-state='loading'] i { background: #d1bd78; }
.screening-run-title { margin: 26px 0 19px; }
.screening-run-title p { margin: 0 0 6px; color: #8a9b90; font-size: 10px; }
.screening-run-title h2 { margin: 0; color: #365944; font-size: 20px; letter-spacing: -.05em; }
.screening-run-data { padding: 13px 0; border-top: 1px solid #dbeade; border-bottom: 1px solid #dbeade; }
.screening-run-data p { display: flex; justify-content: space-between; margin: 8px 0; color: #809087; font-size: 10px; }
.screening-run-data b { color: #4d6556; font-weight: 650; }
.screening-policy-note { margin: 14px 0 0; color: #8d9b91; font-size: 9px; line-height: 1.5; }
.screening-run-help { margin: 11px 0 0; color: #8d9b91; font-size: 9px; line-height: 1.5; text-align: center; }
.screening-empty-card { margin-top: 18px; padding: 28px; }
.first-screening-help { max-width: 650px; margin: 16px auto 0; color: #68736b; font-size: 11px; line-height: 1.7; }
.first-screening-help ol { margin: 13px 0; padding-left: 20px; }
@media (max-width: 1060px) { .screening-workspace { grid-template-columns: 1fr; }.screening-run-panel { position: static; }.rule-load-row { grid-template-columns: 1fr; }.screening-section-title--conditions { display: block; }.screening-section-title--conditions p { max-width: none; text-align: left; }.standing-condition-line { grid-template-columns: 1fr minmax(150px, .7fr); }.standing-condition-line p { grid-column: 1 / -1; margin: -6px 0 10px; } }
@media (max-width: 620px) { .screening-editor-card { padding: 20px 14px; }.standing-condition-line { grid-template-columns: 1fr; gap: 8px; padding-top: 12px; padding-bottom: 12px; }.standing-condition-line p { grid-column: 1; margin: 0; }.standing-conditions-heading div span { display: none; } }

</style>
