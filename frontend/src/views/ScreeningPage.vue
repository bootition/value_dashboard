<script setup lang="ts">
import { ref, reactive, onMounted, computed, watch } from 'vue'
import { NButton, NSwitch, NInputNumber, NSelect, NSpace, NEmpty, NModal, NAlert, useMessage } from 'naive-ui'
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
import { fieldDisplayName } from '../utils/screening-format.ts'

const message = useMessage()
const indicators = ref<readonly ScreeningIndicator[]>([])
const loading = ref(false)
const results = ref<readonly ScreeningResult[]>([])
const executionTime = ref(0)
const basePoolSize = ref(0)
const dataDate = ref<string | null>(null)
const runId = ref<string | null>(null)
const truncated = ref(false)
const totalMatched = ref(0)
const warningCodes = ref<readonly WarningCode[]>([])
const qualityStatus = ref<'loading' | 'available' | 'failed'>('loading')

const basePool = reactive({ exclude_st: true, exclude_suspended: true, min_listing_years: 1 })
const strictOnly = ref(false)

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
  { label: '>', value: '>' },
  { label: '<', value: '<' },
  { label: '>=', value: '>=' },
  { label: '<=', value: '<=' },
  { label: '=', value: '=' },
  { label: '!=', value: '!=' },
  { label: '区间', value: 'between' },
  { label: '不为空', value: 'is_not_null' },
  { label: '为空', value: 'is_null' },
]

const indicatorOptions = computed(() =>
  indicators.value.map((i) => ({
    label: fieldDisplayName(i.name, (i as ScreeningIndicator & { label?: string }).label),
    value: i.name,
  })),
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

async function runScreening() {
  if (activeRule.value === null) {
    message.warning('请先保存并选择规则版本，再运行筛选')
    return
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
    })
    results.value = resp.data.results
    executionTime.value = resp.data.execution_time_ms
    basePoolSize.value = resp.data.base_pool_size
    dataDate.value = resp.data.data_date
    runId.value = resp.data.run_id
    truncated.value = resp.data.truncated ?? false
    totalMatched.value = resp.data.total
    if (resp.data.truncated) {
      message.warning(`结果超过 5000 条，仅展示前 5000 条（共 ${resp.data.total} 条匹配）`)
    }
    message.success(`筛选完成: ${resp.data.total} 条 (${resp.data.execution_time_ms}ms)`)
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
    if (!draft) return
    if (draft.conditions) Object.assign(ruleTree, draft.conditions)
    if (draft.sort) sortRules.value = draft.sort
    if (draft.base_pool) Object.assign(basePool, draft.base_pool)
    if (typeof draft.strict_only === 'boolean') strictOnly.value = draft.strict_only
  } catch {
    message.warning('无法恢复最近筛选草稿')
  }
}

async function saveRule() {
  if (!ruleName.value.trim()) {
    message.warning('请输入规则名称')
    return
  }
  try {
    const resp = await axios.post<{ rule_id: number; version: number }>('/api/screening/rules/save', {
      name: ruleName.value.trim(),
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
    message.success(`规则已保存为 v${resp.data.version}`)
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '保存规则失败'))
  }
}

function loadRule(ruleId: number) {
  if (!ruleId || ruleId === 0) return
  
  const rule = savedRules.value.find(r => r.id === ruleId)
  if (!rule) return
  
  // Load the rule conditions
  if (rule.rule_json.conditions) {
    Object.assign(ruleTree, rule.rule_json.conditions)
  }
  
  // Load sort rules
  if (rule.rule_json.sort && rule.rule_json.sort.length > 0) {
    sortRules.value = [...rule.rule_json.sort]
  }
  
  message.success(`已加载规则: ${rule.name} v${rule.version}`)
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
    qualityStatus.value = 'available'
  } catch {
    qualityStatus.value = 'failed'
    message.warning('无法加载数据质量状态')
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
          <div><p>CURRENT RULE</p><h2>{{ activeRule?.name || '未命名筛选草稿' }}</h2><span>规则 → 运行 → 结果</span></div>
        </div>
        <div class="rule-load-row">
          <span>已保存规则</span>
          <n-select v-model:value="selectedRuleId" :options="ruleOptions" size="small" @update:value="loadRule" />
          <n-input v-model:value="ruleName" aria-label="规则名称" size="small" placeholder="规则名称" />
          <n-button size="small" @click="saveRule">保存新版本</n-button>
        </div>

        <div class="screening-section">
          <div class="screening-section-title"><div><b>01</b><h3>基础股票池</h3></div></div>
          <n-space wrap>
            <n-switch v-model:value="basePool.exclude_st"><template #checked>排除 ST</template><template #unchecked>包含 ST</template></n-switch>
            <n-switch v-model:value="basePool.exclude_suspended"><template #checked>排除停牌</template><template #unchecked>包含停牌</template></n-switch>
            <span class="control-label">最低上市年限</span>
            <n-input-number v-model:value="basePool.min_listing_years" :min="0" :max="10" size="small" />
          </n-space>
        </div>

        <div class="screening-section">
          <div class="screening-section-title"><div><b>02</b><h3>筛选条件</h3></div></div>
          <p class="screening-help">全部条件按当前逻辑组合。指标使用中文名称，并保留必要缩写核对口径。</p>
          <div class="conditions-workbench"><div class="conditions-head"><span>指标</span><span>关系</span><span>目标值</span><span></span><span></span></div><ScreeningRuleEditor :node="ruleTree" :depth="1" :max-depth="3" :max-conditions="20" :is-root="true" :indicator-options="indicatorOptions" :op-options="opOptions" @warn="(msg: string) => message.warning(msg)" /></div>
        </div>

        <div class="screening-section">
          <div class="screening-section-title"><div><b>03</b><h3>排序方式</h3></div></div>
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
        <div class="screening-ready"><i></i>{{ qualityStatus === 'available' ? '数据可用于筛选' : '数据状态加载中' }}</div>
        <div class="screening-run-title"><p>当前规则</p><h2>{{ activeRule?.name || '未命名筛选草稿' }}</h2></div>
        <div class="screening-run-data"><p><span>筛选条件</span><b>{{ ruleFields.size }} 项</b></p><p><span>排序方式</span><b>{{ sortRules.length }} 项</b></p><p><span>可信度</span><b>{{ strictOnly ? '严格可信' : '包含近似值' }}</b></p><p><span>数据日期</span><b>{{ dataDate || '运行后确认' }}</b></p></div>
        <div class="screening-strict"><n-switch v-model:value="strictOnly"><template #checked>仅使用严格可信数据</template><template #unchecked>包含近似可信数据</template></n-switch><span>{{ strictOnly ? '排除口径不完整或近似计算的指标。' : '结果会包含近似可信的数据。' }}</span></div>
        <n-button type="primary" block :loading="loading" @click="runScreening">运行筛选 →</n-button>
        <p class="screening-run-help">运行结果可保存、导出或加入自选列表。</p>
      </aside>
    </div>

    <ScreeningResultsPanel :results="results" :strict-only="strictOnly" :execution-time="executionTime" :base-pool-size="basePoolSize" :data-date="dataDate" :warning-codes="warningCodes" :untrusted-fields="untrustedFields" :quality-status="qualityStatus" :rule-tree="ruleTree" :run-id="runId" :rule-id="activeRule?.id ?? null" :rule-version="activeRule?.version ?? null" :rule-name="activeRule?.name ?? ''" :locked-indicators="activeRule?.locked_indicators ?? {}" :sort="sortRules" :base-pool-config="basePool" :truncated="truncated" :total-matched="totalMatched" />

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
        <li>在「加载规则」输入名称，点击<strong>保存新版本</strong>——筛选必须先保存规则（规则版本化，用于溯源）。</li>
        <li>设置「基础股票池」（是否排除 ST/停牌、最低上市年限）与「数据质量」严格模式。</li>
        <li>在「筛选条件」添加条件（如 pe_ttm &lt; 15），在「排序」设置优先级，点击<strong>运行筛选</strong>。</li>
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
.screening-page { max-width: 1380px; }.screening-page-header { margin-bottom: 27px; }.screening-page-header p { margin: 0 0 8px; color: #97a199; font-size: 10px; }.screening-page-header h1 { margin: 0; font-size: 25px; letter-spacing: -.05em; }.screening-page-header span { display: block; margin-top: 7px; color: #829087; font-size: 12px; }.screening-workspace { display: grid; grid-template-columns: minmax(620px, 1.6fr) minmax(282px, .72fr); gap: 21px; }.screening-editor-card, .screening-empty-card { border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.screening-editor-card { padding: 28px 29px; }.screening-card-heading h2 { margin: 7px 0 5px; font-size: 19px; letter-spacing: -.04em; }.screening-card-heading p { margin: 0; color: #91a097; font-size: 9px; font-weight: 800; letter-spacing: .13em; }.screening-card-heading span { color: #829087; font-size: 11px; }.rule-load-row { display: grid; grid-template-columns: auto minmax(180px, 1fr) minmax(120px, .65fr) auto; align-items: center; gap: 10px; margin: 25px 0; padding: 13px 14px; border-radius: 9px; background: #fafcf9; }.rule-load-row > span, .control-label { color: #89958c; font-size: 10px; }.screening-section { padding: 21px 0; border-top: 1px solid #edf1ee; }.screening-section-title > div { display: flex; align-items: baseline; gap: 9px; }.screening-section-title b { color: #83b194; font-size: 10px; }.screening-section-title h3 { margin: 0 0 13px; font-size: 13px; }.screening-help { margin: -4px 0 13px; color: #929d95; font-size: 10px; }.conditions-workbench { overflow: hidden; border: 1px solid #edf1ee; border-radius: 9px; }.conditions-head { display: grid; grid-template-columns: minmax(180px, 1.7fr) 108px minmax(130px, .8fr) auto 28px; gap: 10px; padding: 10px 12px; background: #fafcf9; color: #9ca69f; font-size: 9px; }.dsl-manager { margin-top: 3px; }.screening-run-panel { align-self: start; padding: 25px; border-radius: 16px; background: #eff7f1; box-shadow: 0 5px 17px rgba(47, 114, 74, .055); }.screening-ready { display: flex; align-items: center; gap: 6px; color: #659d75; font-size: 10px; }.screening-ready i { width: 6px; height: 6px; border-radius: 50%; background: #82ba94; }.screening-run-title { margin: 26px 0 19px; }.screening-run-title p { margin: 0 0 6px; color: #8a9b90; font-size: 10px; }.screening-run-title h2 { margin: 0; color: #365944; font-size: 20px; letter-spacing: -.05em; }.screening-run-data { padding: 13px 0; border-top: 1px solid #dbeade; border-bottom: 1px solid #dbeade; }.screening-run-data p { display: flex; justify-content: space-between; margin: 8px 0; color: #809087; font-size: 10px; }.screening-run-data b { color: #4d6556; font-weight: 650; }.screening-strict { display: grid; gap: 6px; margin: 18px 0; }.screening-strict span, .screening-run-help { color: #8d9b91; font-size: 9px; }.screening-run-help { margin: 11px 0 0; line-height: 1.5; text-align: center; }.screening-empty-card { display: grid; grid-template-columns: 220px 1fr; gap: 28px; align-items: center; margin-top: 21px; padding: 27px 29px; }.first-screening-help { color: #718077; font-size: 12px; }.first-screening-help :deep(.n-alert) { margin-bottom: 12px; }.first-screening-help ol { margin: 0; padding-left: 20px; line-height: 2; }.first-screening-help > div { margin-top: 8px; color: #87958c; font-size: 11px; }
</style>
