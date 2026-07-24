<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import { NCard, NButton, NSwitch, NInputNumber, NSelect, NSpace, NEmpty, useMessage } from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import ScreeningResultsPanel from '../components/ScreeningResultsPanel.vue'
import ScreeningRuleEditor from '../components/ScreeningRuleEditor.vue'
import type {
  ScreeningIndicator,
  ScreeningRuleNode,
  ScreeningResult,
  ScreeningRunResponse,
  DataStatusSummaryResponse,
  WarningCode,
} from '../types/screening.ts'
import { collectRuleFields, computeUntrustedFields } from '../helpers/screening-quality.ts'

const message = useMessage()
const indicators = ref<readonly ScreeningIndicator[]>([])
const loading = ref(false)
const results = ref<readonly ScreeningResult[]>([])
const executionTime = ref(0)
const basePoolSize = ref(0)
const dataDate = ref<string | null>(null)
const warningCodes = ref<readonly WarningCode[]>([])
const qualityStatus = ref<'loading' | 'available' | 'failed'>('loading')

const basePool = reactive({ exclude_st: true, exclude_suspended: true, min_listing_years: 1 })

const ruleTree = reactive<ScreeningRuleNode>({
  logic: 'AND',
  rules: [
    { field: 'pe_ttm', op: '>', value: 0 },
    { field: 'pe_ttm', op: '<', value: 100 },
    { field: 'roe', op: '>', value: 0.1 },
  ],
})

const sortField = ref('pe_ttm')
const sortDirection = ref<'asc' | 'desc'>('asc')

const opOptions = [
  { label: '>', value: '>' },
  { label: '<', value: '<' },
  { label: '>=', value: '>=' },
  { label: '<=', value: '<=' },
  { label: '=', value: '=' },
  { label: '!=', value: '!=' },
  { label: '不为空', value: 'is_not_null' },
]

const indicatorOptions = computed(() =>
  indicators.value.map((i) => ({ label: i.name, value: i.name })),
)

const resultColumns = computed(() => {
  if (!results.value.length) return []
  return Object.keys(results.value[0])
})

const ruleFields = computed(() => collectRuleFields(ruleTree))

const untrustedFields = computed(() =>
  computeUntrustedFields({
    ruleFields: ruleFields.value,
    sortField: sortField.value,
    resultColumns: resultColumns.value,
    warningCodes: warningCodes.value,
  }),
)

async function runScreening() {
  loading.value = true
  try {
    const resp = await axios.post<ScreeningRunResponse>('/api/screening/run', {
      rule: {
        conditions: ruleTree,
        sort: [{ field: sortField.value, direction: sortDirection.value }],
        columns: [
          'stock_code', 'name', 'exchange', 'sw_level1', 'latest_close',
          'pe_ttm', 'pb_mrq', 'roe', 'gross_margin', 'net_margin',
          'debt_ratio', 'revenue_yoy', 'dividend_yield',
        ],
      },
      include_st: !basePool.exclude_st,
      include_suspended: !basePool.exclude_suspended,
      min_listing_years: basePool.min_listing_years,
    })
    results.value = resp.data.results
    executionTime.value = resp.data.execution_time_ms
    basePoolSize.value = resp.data.base_pool_size
    dataDate.value = resp.data.data_date
    message.success(`筛选完成: ${resp.data.total} 条 (${resp.data.execution_time_ms}ms)`)
  } catch (e: unknown) {
    const detail = isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : null
    message.error(`筛选失败: ${detail || '未知错误'}`)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    const resp = await axios.get<{ indicators: readonly ScreeningIndicator[] }>(
      '/api/screening/indicators',
    )
    indicators.value = resp.data.indicators
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
    <h2>筛选</h2>
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
      <n-space>
        <n-select
          v-model:value="sortField"
          :options="indicatorOptions"
          size="small"
          style="width: 180px"
          filterable
        />
        <n-select
          v-model:value="sortDirection"
          :options="[
            { label: '升序', value: 'asc' },
            { label: '降序', value: 'desc' },
          ]"
          size="small"
          style="width: 100px"
        />
        <n-button type="primary" :loading="loading" @click="runScreening">运行筛选</n-button>
      </n-space>
    </n-card>

    <ScreeningResultsPanel
      :results="results"
      :execution-time="executionTime"
      :base-pool-size="basePoolSize"
      :data-date="dataDate"
      :warning-codes="warningCodes"
      :untrusted-fields="untrustedFields"
      :quality-status="qualityStatus"
      :rule-tree="ruleTree"
      :sort-field="sortField"
      :sort-direction="sortDirection"
    />

    <n-empty v-if="results.length === 0" description="运行筛选后显示结果" style="padding: 40px" />
  </div>
</template>
