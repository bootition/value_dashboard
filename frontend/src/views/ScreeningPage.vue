<script setup lang="ts">
import { ref, reactive, onMounted, computed, watch, nextTick, h } from 'vue'
import type { VNode } from 'vue'
import { NButton, NSwitch, NInput, NInputNumber, NSelect, NEmpty, NModal, NAlert, NTooltip, useMessage, useDialog } from 'naive-ui'
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
import { useScreeningColumns, DEFAULT_RESULT_COLUMNS, encodeResultColumn } from '../composables/useScreeningColumns.ts'
import { useScreeningDraft } from '../composables/useScreeningDraft.ts'
import { useScreeningRules } from '../composables/useScreeningRules.ts'
import { readinessNotice } from '../utils/readiness-notice.ts'
import type { ReadinessNotice } from '../utils/readiness-notice.ts'
import { collectRuleFields, computeUntrustedFields } from '../helpers/screening-quality.ts'
import { apiErrorText } from '../helpers/api-error.ts'
import { applyIndicatorUnits, indicatorDisplayName } from '../utils/screening-format.ts'
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
const loading = ref(false)
const results = ref<readonly ScreeningResult[]>([])
const executionTime = ref(0)
const basePoolSize = ref(0)
const dataDate = ref<string | null>(null)
const runId = ref<string | null>(null)
const truncated = ref(false)
const totalMatched = ref(0)
const resultsAnchor = ref<HTMLElement | null>(null)
// reports/79 方案 A: 更新窗口内快照口径运行标注
const autoUpdateSnapshot = ref(false)
const snapshotAsOf = ref<string | null>(null)
// 自动更新状态轮询（更新中 → 10s，空闲 → 60s；卸载时清 timer）
const { autoUpdateRunning, refreshAutoUpdate, startAutoUpdatePolling } = useAutoUpdateRunning()

// 2026-08-27：运行按钮不再被少数股票的数据缺口全局禁用。
// 引擎会排除缺口股，后端只拦截真正的全库硬性阻断。
const runEnabled = computed(() => true)
const warningCodes = ref<readonly WarningCode[]>([])
const qualityStatus = ref<'loading' | 'available' | 'failed'>('loading')
const dataReady = ref<boolean | null>(null)
// 2026-08-14 红队 P2-4：严格模式空结果反馈文案（来自 /run 响应）
const strictModeWarning = ref<string | null>(null)

const basePool = reactive({ exclude_st: true, exclude_suspended: true, min_listing_years: 1 })
const strictOnly = ref(false)
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

// 方案 C（2026-11-16）：后端不再下发 930 条排名派生字段（`pe_ttm_market_rank` …），
// 改为条件行上的「排名口径」下拉（rankModes）。下拉里只剩主指标 + 统计域。

// 指标全中文名（规则：display_name → FIELD_LABELS → 后端 label → 字段名 + 单位）
// 2026-11-22：纯函数已抽到 utils/screening-format.ts 的 indicatorDisplayName。
const indicatorOptions = computed(() =>
  indicators.value.map((i) => ({ label: indicatorDisplayName(i), value: i.name })),
)

// ─── 结果展示列（合成值编解码 / 下拉选项）────────────────────────────────
// 2026-11-22：列定义、合成值编解码、下拉选项整块抽到 composables/useScreeningColumns.ts。
const { selectedResultColumns, resultColumnOptions, selectedColumnsForRequest } =
  useScreeningColumns({ indicators, rankModes, indicatorOptions })

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
  try {
    const resp = await axios.post<ScreeningRunResponse>('/api/screening/run', {
      rule_id: activeRule.value.id,
      rule_version: activeRule.value.version,
      include_st: !basePool.exclude_st,
      include_suspended: !basePool.exclude_suspended,
      min_listing_years: basePool.min_listing_years,
      strict_only: strictOnly.value,
      columns: selectedColumnsForRequest(),
    }, {
      // 后端筛选门禁在无缓存冷启动时需执行一次全库数据质量核对；
      // 该请求单独放宽超时，避免默认 30s 把仍在正常计算的服务端请求
      // 从浏览器侧中止，造成“一直 loading 却永远收不到结果”。
      timeout: 120_000,
    })
    results.value = resp.data.results
    executionTime.value = resp.data.execution_time_ms
    basePoolSize.value = resp.data.base_pool_size
    dataDate.value = resp.data.data_date
    runId.value = resp.data.run_id
    truncated.value = resp.data.truncated ?? false
    totalMatched.value = resp.data.total
    // reports/79 方案 A: 更新窗口内基于快照的运行，明确告知口径
    autoUpdateSnapshot.value = resp.data.auto_update_in_progress === true
    readinessHint.value = readinessNotice(resp.data)
    snapshotAsOf.value = resp.data.data_as_of ?? null
    // 2026-08-14 红队 P2-4：严格模式空结果的原因反馈（无 strict 血缘等）
    strictModeWarning.value = resp.data.strict_mode_warning ?? null
    if (resp.data.truncated) {
      message.warning(`结果超过 5000 条，仅展示前 5000 条（共 ${resp.data.total} 条匹配）`)
    }
    if (resp.data.auto_update_in_progress) {
      message.info(`数据自动更新中：本次筛选基于最近完整快照（数据截至 ${resp.data.data_as_of ?? dataDate.value}）`)
    }
    message.success(`筛选完成: ${resp.data.total} 条 (${resp.data.execution_time_ms}ms)，已滚动到结果区`)
    await nextTick()
    scrollToResults()
  } catch (e: unknown) {
    message.error(apiErrorText(e, '筛选失败'))
  } finally {
    loading.value = false
  }
}

// L0-3（报告42）: strict-only 切换时重新按服务端口径运行，
// 使屏幕展示、保存记录、导出 CSV 三者结果一致（不再客户端二次过滤）。
let strictRerunTimer: ReturnType<typeof setTimeout> | undefined
watch(strictOnly, () => {
  if (results.value.length === 0 || activeRule.value === null) return
  if (strictRerunTimer) clearTimeout(strictRerunTimer)
  strictRerunTimer = setTimeout(() => {
    message.info(strictOnly.value ? '正在按 strict 口径重新筛选…' : '正在按完整口径重新筛选…')
    void runScreening()
  }, 400)
})

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
  ruleTree,
  basePool,
  strictOnly,
  selectedResultColumns,
  defaultResultColumns: DEFAULT_RESULT_COLUMNS,
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
  loadSavedRules,
  isDraftDirty,
  autoRuleName,
  saveRule,
  deleteSelectedRule,
  loadRule,
} = useScreeningRules({
  ruleTree,
  selectedResultColumns,
  defaultResultColumns: DEFAULT_RESULT_COLUMNS,
  selectedColumnsForRequest,
  syncEditorSnapshot,
  hasUnsavedEdits,
  notify: message,
  dialog,
})

onMounted(async () => {
  await restoreDraft()
  markDraftHydrated()
  await loadSavedRules()
  
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

  // reports/79 方案 A: 更新窗口内允许以最近完整快照运行（后端同口径）
  refreshAutoUpdate()
  // 2026-08-14 红队 P3：自动更新状态不再只在挂载时读一次——
  // 更新窗口（数分钟级）内运行按钮会随状态自动切换可用性。
  // 2026-11-22：轮询+单飞+卸载清理已抽到 composables/useAutoUpdatePolling.ts。
  startAutoUpdatePolling(10000)
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
          <ScreeningRuleEditor class="conditions-workbench" :node="ruleTree" :depth="1" :max-depth="3" :max-conditions="20" :is-root="true" :indicator-options="indicatorOptions" :op-options="opOptions" :rank-modes="rankModes" :rankable-of="rankableOf" :param-spec-of="paramSpecOf" @warn="(msg: string) => message.warning(msg)" />
          <div v-if="unavailableIndicators.length > 0" class="unavailable-indicators" aria-label="当前报告期不可用指标">
            <div class="unavailable-indicators-heading">
              <strong>当前报告期不可用指标（{{ unavailableIndicators.length }}）</strong>
              <span>这些字段已从可选指标中移除；悬停查看原因与覆盖率。</span>
            </div>
            <div class="unavailable-indicators-list">
              <n-tooltip
                v-for="item in unavailableIndicators"
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
        </div>

        <div class="screening-section">
          <div class="screening-section-title"><div><b>02</b><h3>结果展示列</h3></div><p>运行前可自由增加或删减展示信息；修改展示列不会产生新的规则版本。</p></div>
          <n-select
            v-model:value="selectedResultColumns"
            :options="resultColumnOptions"
            multiple
            filterable
            clearable
            :render-label="renderOptionLabel"
            size="small"
            placeholder="选择要在结果表展示的信息"
          />
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
        <div class="screening-run-data"><p><span>筛选条件</span><b>{{ ruleFields.size }} 项</b></p><p><span>可信度</span><b>{{ strictOnly ? '严格可信' : '包含近似值' }}</b></p><p><span>数据日期</span><b>{{ dataDate || '运行后确认' }}</b></p></div>
        <div class="screening-strict"><n-switch v-model:value="strictOnly"><template #checked>仅使用严格可信数据</template><template #unchecked>包含近似可信数据</template></n-switch><span>{{ strictOnly ? '排除口径不完整或近似计算的指标。' : '结果会包含近似可信的数据。' }}</span></div>
        <n-button type="primary" block :loading="loading" :disabled="!runEnabled" @click="runScreening">运行筛选 →</n-button>
        <n-button v-if="results.length > 0" text type="primary" block style="margin-top: 8px" @click="scrollToResults">
          查看本次筛选结果（{{ totalMatched || results.length }} 条）↓
        </n-button>
        <p v-if="autoUpdateRunning && dataReady === false" class="screening-run-help">数据正在自动更新：将以最近完整快照运行（后端将标注数据截至日期）。</p>
        <p v-else class="screening-run-help">运行结果可保存、导出或加入自选列表。</p>
      </aside>
    </div>

    <n-alert v-if="autoUpdateSnapshot" type="info" :show-icon="true" class="screening-snapshot-note">
      数据正在自动更新：本次结果基于最近完整快照（数据截至 {{ snapshotAsOf || dataDate || '—' }}），更新完成后重新运行即可获得最新数据。
    </n-alert>
    <div ref="resultsAnchor" class="screening-results-anchor">
      <ScreeningResultsPanel :results="results" :strict-only="strictOnly" :strict-mode-warning="strictModeWarning" :execution-time="executionTime" :base-pool-size="basePoolSize" :data-date="dataDate" :warning-codes="warningCodes" :untrusted-fields="untrustedFields" :quality-status="qualityStatus" :rule-tree="ruleTree" :run-id="runId" :rule-id="activeRule?.id ?? null" :rule-version="activeRule?.version ?? null" :rule-name="activeRule?.name ?? ''" :locked-indicators="activeRule?.locked_indicators ?? {}" :base-pool-config="basePool" :truncated="truncated" :total-matched="totalMatched" />
    </div>

    <section v-if="results.length === 0" class="screening-empty-card">
      <n-empty description="运行筛选后显示候选公司" />
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
        <li>在「筛选条件」添加条件（如 pe_ttm &lt; 15），在「排序」设置优先级。</li>
        <li>点击<strong>运行筛选</strong>——系统会自动把当前草稿保存为一个规则版本，用于后续溯源。</li>
      </ol>
      <div>
        之后可保存结果、导出 CSV（含数据日期与规则溯源）或加入自选。
      </div>
      </div>
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
.unavailable-indicators-list { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 9px; }
.unavailable-chip { display: inline-flex; align-items: baseline; gap: 7px; padding: 4px 9px; border-radius: 999px; background: #f0f2f0; color: #9aa69f; cursor: help; font-size: 10px; opacity: .85; }
.unavailable-chip-name { text-decoration: line-through; text-decoration-color: #c3ccc5; }
.unavailable-chip-coverage { color: #b0b8b2; font-size: 9px; font-variant-numeric: tabular-nums; }
.unavailable-tooltip { max-width: 320px; font-size: 11px; line-height: 1.6; }
.unavailable-tooltip b { display: block; margin-bottom: 4px; }
.unavailable-tooltip p { margin: 2px 0; }
.unavailable-tooltip-message { color: #d9d2b8; }
.dsl-manager { margin-top: 4px; }
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
.screening-strict { display: grid; gap: 6px; margin: 18px 0; }
.screening-strict span { color: #8d9b91; font-size: 9px; }
.screening-run-help { margin: 11px 0 0; color: #8d9b91; font-size: 9px; line-height: 1.5; text-align: center; }
.screening-empty-card { margin-top: 18px; padding: 28px; }
.first-screening-help { max-width: 650px; margin: 16px auto 0; color: #68736b; font-size: 11px; line-height: 1.7; }
.first-screening-help ol { margin: 13px 0; padding-left: 20px; }
@media (max-width: 1060px) { .screening-workspace { grid-template-columns: 1fr; }.screening-run-panel { position: static; }.rule-load-row { grid-template-columns: 1fr; }.screening-section-title--conditions { display: block; }.screening-section-title--conditions p { max-width: none; text-align: left; }.standing-condition-line { grid-template-columns: 1fr minmax(150px, .7fr); }.standing-condition-line p { grid-column: 1 / -1; margin: -6px 0 10px; } }
@media (max-width: 620px) { .screening-editor-card { padding: 20px 14px; }.standing-condition-line { grid-template-columns: 1fr; gap: 8px; padding-top: 12px; padding-bottom: 12px; }.standing-condition-line p { grid-column: 1; margin: 0; }.standing-conditions-heading div span { display: none; } }

</style>
