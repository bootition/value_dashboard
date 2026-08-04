<script setup lang="ts">
import { ref, reactive, onMounted, computed, watch } from 'vue'
import { NCard, NButton, NSwitch, NInputNumber, NSelect, NSpace, NEmpty, NModal, NAlert, useMessage } from 'naive-ui'
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
  indicators.value.map((i) => ({ label: (i as ScreeningIndicator & { label?: string }).label || i.name, value: i.name })),
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
  <div>
    <h1 style="font-size: 24px; margin: 0 0 4px;">筛选</h1>
    <!-- L2 V3: 以"规则→运行→结果"为主线的研究流程 -->
    <p style="color: #667085; margin: 0 0 16px;">规则 → 运行 → 结果 → 保存/导出/加入自选</p>
    
    <n-card title="加载规则" size="small" style="margin-bottom: 16px">
        <n-space align="center" wrap>
          <span>已保存的规则:</span>
        <n-select
          v-model:value="selectedRuleId"
          :options="ruleOptions"
          size="small"
          style="width: 250px"
            @update:value="loadRule"
          />
          <n-input v-model:value="ruleName" aria-label="规则名称" size="small" placeholder="规则名称" style="width: 160px" />
          <n-button size="small" @click="saveRule">保存新版本</n-button>
        </n-space>
    </n-card>

    <DslIndicatorManager style="margin-bottom: 16px" />
    
    <n-card title="基础股票池" size="small" style="margin-bottom: 16px">
      <n-space>
        <n-switch v-model:value="basePool.exclude_st">
          <template #checked>排除ST</template>
          <template #unchecked>包含ST</template>
        </n-switch>
        <n-switch v-model:value="basePool.exclude_suspended">
          <template #checked>排除停牌</template>
          <template #unchecked>包含停牌</template>
        </n-switch>
        <span>最低上市年限:</span>
        <n-input-number
          v-model:value="basePool.min_listing_years"
          :min="0"
          :max="10"
          size="small"
        />
      </n-space>
    </n-card>

    <n-card title="数据质量" size="small" style="margin-bottom: 16px">
      <n-space>
        <n-switch v-model:value="strictOnly">
          <template #checked>仅查看 strict</template>
          <template #unchecked>包含 approximate</template>
        </n-switch>
        <span style="color: #999; font-size: 12px;">
          {{ strictOnly ? '仅显示所有使用字段均为 strict 置信度的股票' : '显示所有股票，包括 approximate 置信度' }}
        </span>
      </n-space>
    </n-card>

    <n-card title="筛选条件" size="small" style="margin-bottom: 16px">
      <ScreeningRuleEditor
        :node="ruleTree"
        :depth="1"
        :max-depth="3"
        :max-conditions="20"
        :is-root="true"
        :indicator-options="indicatorOptions"
        :op-options="opOptions"
        @warn="(msg: string) => message.warning(msg)"
      />
    </n-card>

    <n-card title="排序" size="small" style="margin-bottom: 16px">
      <n-space vertical>
        <n-space v-for="(rule, index) in sortRules" :key="index" align="center">
          <span>优先级 {{ index + 1 }}:</span>
          <n-select
            v-model:value="rule.field"
            :options="indicatorOptions"
            size="small"
            style="width: 180px"
            filterable
          />
          <n-select
            v-model:value="rule.direction"
            :options="[
              { label: '升序', value: 'asc' },
              { label: '降序', value: 'desc' },
            ]"
            size="small"
            style="width: 100px"
          />
          <n-button size="tiny" type="error" @click="removeSortRule(index)">删除</n-button>
        </n-space>
        <n-space>
          <n-button size="tiny" @click="addSortRule">+ 添加排序规则</n-button>
          <n-button type="primary" :loading="loading" @click="runScreening">运行筛选</n-button>
        </n-space>
      </n-space>
    </n-card>

    <ScreeningResultsPanel
      :results="results"
      :strict-only="strictOnly"
      :execution-time="executionTime"
      :base-pool-size="basePoolSize"
      :data-date="dataDate"
      :warning-codes="warningCodes"
      :untrusted-fields="untrustedFields"
      :quality-status="qualityStatus"
      :rule-tree="ruleTree"
      :run-id="runId"
      :rule-id="activeRule?.id ?? null"
      :rule-version="activeRule?.version ?? null"
      :rule-name="activeRule?.name ?? ''"
      :locked-indicators="activeRule?.locked_indicators ?? {}"
      :sort="sortRules"
      :base-pool-config="basePool"
      :truncated="truncated"
      :total-matched="totalMatched"
    />

    <n-empty v-if="results.length === 0" description="运行筛选后显示结果" style="padding: 40px" />

    <!-- L0-1（报告42）: 空态给出可执行的首次筛选步骤，而非只提示"运行筛选" -->
    <n-card
      v-if="results.length === 0"
      title="第一次筛选？三步开始"
      size="small"
      style="margin-top: 8px"
    >
      <n-alert
        v-if="qualityStatus === 'failed'"
        type="error"
        :show-icon="true"
        style="margin-bottom: 12px"
      >
        无法获取数据质量状态，运行结果可能不可信。请先前往<router-link to="/data-status">数据状态页</router-link>确认。
      </n-alert>
      <ol style="margin: 0; padding-left: 20px; line-height: 2;">
        <li>在「加载规则」输入名称，点击<strong>保存新版本</strong>——筛选必须先保存规则（规则版本化，用于溯源）。</li>
        <li>设置「基础股票池」（是否排除 ST/停牌、最低上市年限）与「数据质量」严格模式。</li>
        <li>在「筛选条件」添加条件（如 pe_ttm &lt; 15），在「排序」设置优先级，点击<strong>运行筛选</strong>。</li>
      </ol>
      <div style="margin-top: 8px; color: #888; font-size: 12px;">
        之后可保存结果、导出 CSV（含数据日期与规则溯源）或加入自选。
      </div>
    </n-card>

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
  </div>
</template>
