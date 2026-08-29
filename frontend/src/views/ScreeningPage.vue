<script setup lang="ts">
import { ref, reactive, onMounted, onUnmounted, computed, watch, nextTick } from 'vue'
import { NButton, NSwitch, NInput, NInputNumber, NSelect, NSpace, NEmpty, NModal, NAlert, useMessage, useDialog } from 'naive-ui'
import axios, { isAxiosError } from 'axios'
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
import { collectRuleFields, computeUntrustedFields } from '../helpers/screening-quality.ts'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import { applyIndicatorUnits, fieldDisplayName, fieldOptionLabel } from '../utils/screening-format.ts'

const message = useMessage()
const dialog = useDialog()
const indicators = ref<readonly ScreeningIndicator[]>([])
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
const autoUpdateRunning = ref(false)

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

interface SavedRule {
  id: number
  name: string
  version: number
  rule_json: { conditions: ScreeningRuleNode; sort?: SortRule[]; columns?: string[] }
  locked_indicators: Record<string, any>
  status: string
  created_at: string
}

const savedRules = ref<SavedRule[]>([])
const selectedRuleId = ref<number>(0)
const activeRule = computed(() => savedRules.value.find(rule => rule.id === selectedRuleId.value) || null)
const ruleName = ref('')

const ruleTree = reactive<ScreeningRuleNode>({
  id: generateRuleId(),
  logic: 'AND',
  rules: [],
})

interface SortRule {
  field: string
  direction: 'asc' | 'desc'
}

const sortRules = ref<SortRule[]>([
  { field: 'pe_ttm', direction: 'asc' },
])

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

const readinessCopy = computed(() => {
  if (qualityStatus.value === 'failed') return { state: 'failed', label: '状态读取失败' }
  if (dataReady.value === null) return { state: 'loading', label: '正在核对数据' }
  if (dataReady.value) return { state: 'ready', label: '数据已就绪' }
  // reports/79 方案 A: 更新窗口内快照口径可用
  if (autoUpdateRunning.value) return { state: 'ready', label: '更新中（快照口径）' }
  return { state: 'blocked', label: '数据尚未就绪' }
})

const indicatorOptions = computed(() =>
  indicators.value.map((i) => {
    const baseLabel = fieldDisplayName(i.name, (i as ScreeningIndicator & { label?: string }).label)
    return {
      label: fieldOptionLabel(i.name, baseLabel),
      value: i.name,
    }
  }),
)

const resultColumns = computed(() => {
  if (!results.value.length) return []
  return Object.keys(results.value[0])
})

const ruleFields = computed(() => collectRuleFields(ruleTree))

const untrustedFields = computed(() =>
  computeUntrustedFields({
    ruleFields: ruleFields.value,
    sortField: sortRules.value[0]?.field || '',
    resultColumns: resultColumns.value,
    warningCodes: warningCodes.value,
  }),
)

function addSortRule() {
  if (sortRules.value.length >= 5) {
    message.warning('最多支持5个排序规则')
    return
  }
  sortRules.value.push({ field: indicatorOptions.value[0]?.value || '', direction: 'asc' })
}

function removeSortRule(index: number) {
  sortRules.value.splice(index, 1)
}

// 2026-08-14 红队 P2-20：运行始终按已保存版本（rule_id/version）执行；
// 此前用户选择规则后再改条件/排序会继续运行却静默忽略编辑。
// 运行前检测草稿与已保存版本的差异并明确告知。
// 2026-08-26：比较时忽略条件节点的 id（id 只用于 UI 渲染/删除定位，不影响筛选语义），
// 避免加载旧版无 id 的规则被误判为“已修改”。
function stripRuleIds(node: unknown): unknown {
  if (Array.isArray(node)) return node.map(stripRuleIds)
  if (node && typeof node === 'object') {
    const result: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
      if (key === 'id') continue
      result[key] = stripRuleIds(value)
    }
    return result
  }
  return node
}

function isDraftDirty(): boolean {
  const rule = activeRule.value
  if (!rule) return false
  const savedConditions = (rule.rule_json.conditions ?? null) as unknown
  const savedSort = (rule.rule_json.sort ?? null) as unknown
  const conditionsDirty = JSON.stringify(stripRuleIds(ruleTree)) !== JSON.stringify(stripRuleIds(savedConditions))
  const sortDirty = JSON.stringify(sortRules.value) !== JSON.stringify(savedSort)
  return conditionsDirty || sortDirty
}

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
    message.error(friendlyErrorMessage(e, '筛选失败'))
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

async function loadSavedRules() {
  try {
    const resp = await axios.get<{ rules: SavedRule[] }>('/api/screening/rules')
    savedRules.value = resp.data.rules
  } catch {
    message.warning('无法加载已保存的规则')
  }
}

let draftTimer: ReturnType<typeof setTimeout> | undefined
let draftHydrated = false
const draftRevision = ref(0)
let draftSaveInFlight = false
let draftSaveQueued = false
// L0-4（报告42）: 草稿 409 冲突后不再永久停用自动保存，而是给出明确选项
const showDraftConflictModal = ref(false)

// 2026-08-14 红队 P3：未保存修改判定基于"上次已同步的编辑区快照"
// （草稿恢复/自动保存/规则加载/保存新版本时刷新），而不是与目标规则
// 比较——加载目标几乎必然不同，旧逻辑会误弹覆盖确认。
let lastSyncedSnapshot = ''
function editorSnapshot(): string {
  return JSON.stringify({ conditions: ruleTree, sort: sortRules.value })
}
function syncEditorSnapshot() {
  lastSyncedSnapshot = editorSnapshot()
}
function hasUnsavedEdits(): boolean {
  return lastSyncedSnapshot !== '' && editorSnapshot() !== lastSyncedSnapshot
}

function draftPayload() {
  return {
    conditions: ruleTree,
    sort: sortRules.value,
    base_pool: { ...basePool },
    strict_only: strictOnly.value,
  }
}

async function persistDraft() {
  if (!draftHydrated) return
  if (draftSaveInFlight) {
    draftSaveQueued = true
    return
  }
  draftSaveInFlight = true
  try {
    const response = await axios.put<{ revision: number }>('/api/screening/draft', {
      draft: draftPayload(),
      revision: draftRevision.value,
    })
    draftRevision.value = response.data.revision
    syncEditorSnapshot()
  } catch (error) {
    if (isAxiosError(error) && error.response?.status === 409) {
      draftHydrated = false
      draftSaveQueued = false
      showDraftConflictModal.value = true
    } else {
      message.warning('筛选草稿自动保存失败')
    }
  } finally {
    draftSaveInFlight = false
    if (draftSaveQueued && draftHydrated) {
      draftSaveQueued = false
      void persistDraft()
    }
  }
}

async function resolveDraftConflict(choice: 'server' | 'local' | 'refresh') {
  showDraftConflictModal.value = false
  if (choice === 'refresh') {
    window.location.reload()
    return
  }
  if (choice === 'server') {
    try {
      await restoreDraft()
      draftHydrated = true
      message.success('已加载服务器草稿，自动保存已恢复')
    } catch {
      message.error('加载服务器草稿失败，请刷新页面')
    }
    return
  }
  try {
    // 保留本地：以服务器当前 revision 覆盖服务器草稿（本地编辑优先）
    const current = await axios.get<{ draft: unknown; revision?: number }>('/api/screening/draft')
    const revision = current.data.revision ?? 0
    const resp = await axios.put<{ revision: number }>('/api/screening/draft', {
      draft: draftPayload(),
      revision,
    })
    draftRevision.value = resp.data.revision
    draftHydrated = true
    syncEditorSnapshot()
    message.success('已保留本地草稿（覆盖服务器版本），自动保存已恢复')
  } catch {
    message.error('保留本地草稿失败，请刷新页面')
  }
}

function saveDraftSoon() {
  if (!draftHydrated) return
  if (draftTimer) clearTimeout(draftTimer)
  draftTimer = setTimeout(() => void persistDraft(), 400)
}

watch([ruleTree, sortRules, basePool, strictOnly], saveDraftSoon, { deep: true })

async function restoreDraft() {
  try {
    const response = await axios.get<{ draft: {
      conditions?: ScreeningRuleNode
      sort?: SortRule[]
      base_pool?: Partial<typeof basePool>
      strict_only?: boolean
    } | null; revision?: number }>('/api/screening/draft')
    const draft = response.data.draft
    draftRevision.value = response.data.revision ?? 0
    if (!draft) {
      syncEditorSnapshot()
      return
    }
    if (draft.conditions) Object.assign(ruleTree, draft.conditions)
    if (draft.sort) sortRules.value = draft.sort
    if (draft.base_pool) Object.assign(basePool, draft.base_pool)
    if (typeof draft.strict_only === 'boolean') strictOnly.value = draft.strict_only
    syncEditorSnapshot()
  } catch {
    message.warning('无法恢复最近筛选草稿')
  }
}

function autoRuleName(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `未命名规则 ${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`
}

async function persistRule(name: string): Promise<{ rule_id: number; version: number } | null> {
  try {
    const resp = await axios.post<{ rule_id: number; version: number }>('/api/screening/rules/save', {
      name,
      rule_json: {
        conditions: ruleTree,
        sort: sortRules.value,
        columns: Array.from(new Set([
          'stock_code', 'name', 'exchange', 'csrc_l1', 'csrc_l2', 'latest_close',
          ...sortRules.value.map(item => item.field),
          ...Array.from(ruleFields.value),
        ])),
      },
      locked_indicators: {},
      status: 'saved',
    })
    await loadSavedRules()
    selectedRuleId.value = resp.data.rule_id
    syncEditorSnapshot()
    message.success(`规则已保存为 v${resp.data.version}`)
    return resp.data
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '保存规则失败'))
    return null
  }
}

async function saveRule(): Promise<{ rule_id: number; version: number } | null> {
  const name = ruleName.value.trim()
  if (!name) {
    message.warning('请输入规则名称')
    return null
  }
  return persistRule(name)
}

function deleteSelectedRule(): void {
  const rule = activeRule.value
  if (!rule) return
  dialog.warning({
    title: '删除选中的规则？',
    content: `将删除「${rule.name} v${rule.version}」。已有保存结果或自选来源的规则不能删除。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await axios.delete(`/api/screening/rules/${rule.id}`)
        const wasSelected = selectedRuleId.value === rule.id
        await loadSavedRules()
        if (wasSelected) selectedRuleId.value = 0
        message.success('规则已删除')
      } catch (e: unknown) {
        message.error(friendlyErrorMessage(e, '删除规则失败'))
      }
    },
  })
}

function applyLoadedRule(rule: SavedRule) {
  // 先重置编辑区再覆盖，确保目标规则不含 conditions/sort 时不残留旧内容
  ruleTree.logic = 'AND'
  ruleTree.rules.splice(0, ruleTree.rules.length)
  if (rule.rule_json.conditions) {
    Object.assign(ruleTree, rule.rule_json.conditions)
  }

  // Load sort rules
  sortRules.value = (rule.rule_json.sort && rule.rule_json.sort.length > 0)
    ? [...rule.rule_json.sort]
    : []

  message.success(`已加载规则: ${rule.name} v${rule.version}`)
}

function loadRule(ruleId: number) {
  if (!ruleId || ruleId === 0) return

  const rule = savedRules.value.find(r => r.id === ruleId)
  if (!rule) return

  // 2026-08-14 红队 P3：加载已保存规则会覆盖编辑区草稿；仅当编辑区有
  // 未保存修改（相对上次已同步快照）时确认，取消保持原选中项不变。
  const apply = () => {
    selectedRuleId.value = ruleId
    applyLoadedRule(rule)
    syncEditorSnapshot()
  }
  if (!hasUnsavedEdits()) {
    apply()
    return
  }
  dialog.warning({
    title: '覆盖未保存的草稿？',
    content: `加载「${rule.name} v${rule.version}」将覆盖编辑区里未保存的修改。`,
    positiveText: '覆盖并加载',
    negativeText: '取消',
    onPositiveClick: apply,
  })
}

const ruleOptions = computed(() => {
  const options: Array<{ label: string; value: number }> = [
    { label: '选择已保存的规则...', value: 0 },
  ]
  for (const r of savedRules.value) {
    options.push({
      label: `${r.name} v${r.version}`,
      value: r.id,
    })
  }
  return options
})

onMounted(async () => {
  await restoreDraft()
  draftHydrated = true
  await loadSavedRules()
  
  try {
    const resp = await axios.get<{ indicators: readonly ScreeningIndicator[] }>(
      '/api/screening/indicators',
    )
    indicators.value = resp.data.indicators
    // 2026-08-14 红队 F3：应用后端下发的单位元数据（单一来源），
    // 修复小数/百分数双口径下条件输入与结果展示的 100 倍误差。
    const units: Record<string, string> = {}
    for (const indicator of resp.data.indicators) {
      if (indicator.unit) units[indicator.name] = indicator.unit
    }
    applyIndicatorUnits(units)
    // 确保 sortRules 的第一个字段在可用指标列表中
    if (indicators.value.length > 0 && sortRules.value.length > 0) {
      const firstField = sortRules.value[0].field
      if (!indicators.value.find(i => i.name === firstField)) {
        sortRules.value[0].field = indicators.value[0].name
      }
    }
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
  autoUpdateTimer = window.setTimeout(function pollAutoUpdate() {
    refreshAutoUpdate()
    autoUpdateTimer = window.setTimeout(pollAutoUpdate, autoUpdateRunning.value ? 10000 : 60000)
  }, 10000)
})

// 单飞 guard：避免上一次查询未返回时堆积请求
let autoUpdateInFlight = false
let autoUpdateTimer: number | undefined

async function refreshAutoUpdate(): Promise<void> {
  if (autoUpdateInFlight) return
  autoUpdateInFlight = true
  try {
    const au = await axios.get<{ state: string; current_stage: string }>(
      '/api/data-status/auto-update',
    )
    autoUpdateRunning.value = au.data.state === 'enabled' && au.data.current_stage === 'running'
  } catch {
    autoUpdateRunning.value = false
  } finally {
    autoUpdateInFlight = false
  }
}

onUnmounted(() => {
  if (autoUpdateTimer !== undefined) {
    window.clearTimeout(autoUpdateTimer)
    autoUpdateTimer = undefined
  }
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
          <n-select :value="selectedRuleId" :options="ruleOptions" size="small" @update:value="loadRule" />
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
              <label><span>不少于</span><n-input-number v-model:value="basePool.min_listing_years" :min="0" size="small" aria-label="最低上市年限" /></label>
              <p>上市满 {{ basePool.min_listing_years }} 年后进入基础股票池</p>
            </div>
          </div>
          <div class="conditions-join" aria-hidden="true"><span>并且</span></div>
          <ScreeningRuleEditor class="conditions-workbench" :node="ruleTree" :depth="1" :max-depth="3" :max-conditions="20" :is-root="true" :indicator-options="indicatorOptions" :op-options="opOptions" @warn="(msg: string) => message.warning(msg)" />
        </div>

        <div class="screening-section">
          <div class="screening-section-title"><div><b>02</b><h3>结果排序</h3></div></div>
          <n-space vertical>
            <n-space v-for="(rule, index) in sortRules" :key="index" align="center">
              <span class="control-label">优先级 {{ index + 1 }}</span>
              <n-select v-model:value="rule.field" :options="indicatorOptions" size="small" style="width: 220px" filterable />
              <n-select v-model:value="rule.direction" :options="[{ label: '升序', value: 'asc' }, { label: '降序', value: 'desc' }]" size="small" style="width: 100px" />
              <n-button size="tiny" quaternary type="error" @click="removeSortRule(index)">删除</n-button>
            </n-space>
            <n-button size="tiny" @click="addSortRule">+ 添加排序规则</n-button>
          </n-space>
        </div>

        <DslIndicatorManager class="dsl-manager" />
      </section>

      <aside class="screening-run-panel">
        <div class="screening-ready" :data-state="readinessCopy.state"><i></i>{{ readinessCopy.label }}</div>
        <div class="screening-run-title"><p>当前规则</p><h2>{{ activeRule?.name || '未命名筛选草稿' }}</h2></div>
        <div class="screening-run-data"><p><span>筛选条件</span><b>{{ ruleFields.size }} 项</b></p><p><span>排序方式</span><b>{{ sortRules.length }} 项</b></p><p><span>可信度</span><b>{{ strictOnly ? '严格可信' : '包含近似值' }}</b></p><p><span>数据日期</span><b>{{ dataDate || '运行后确认' }}</b></p></div>
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
      <ScreeningResultsPanel :results="results" :strict-only="strictOnly" :strict-mode-warning="strictModeWarning" :execution-time="executionTime" :base-pool-size="basePoolSize" :data-date="dataDate" :warning-codes="warningCodes" :untrusted-fields="untrustedFields" :quality-status="qualityStatus" :rule-tree="ruleTree" :run-id="runId" :rule-id="activeRule?.id ?? null" :rule-version="activeRule?.version ?? null" :rule-name="activeRule?.name ?? ''" :locked-indicators="activeRule?.locked_indicators ?? {}" :sort="sortRules" :base-pool-config="basePool" :truncated="truncated" :total-matched="totalMatched" />
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
