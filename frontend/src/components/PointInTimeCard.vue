<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NDatePicker, NEmpty, NSelect, NSpin, NTag } from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import { fmt } from '../utils/formatters.ts'
import type { AsReportedResponse, ResearchErrorDetail } from '../types'

/**
 * 「当时可见」财务值（point in time，reports/119 §5 D 类 / 2026-11-14）
 *
 * 只回答「as_of 那天我能看到的最新一期财报是哪一期、数字是多少」：
 * - 可见性（取哪一期）由 `financial_report_dates.announce_date <= as_of` 决定；
 * - 数值仍是库内该报告期唯一一份（今天的最新重述值），**不是**当年原始披露值，
 *   所以顶部必须常驻 `value_basis` 徽标与后端下发的 `disclaimer`。
 *
 * 404 的三种 reason 要分开给中文提示（后端 `app/web/api/research.py`）：
 * `no_announce_date_records` / `no_visible_period_before_as_of` /
 * `visible_period_has_no_financials`；其余异常统一走 friendlyErrorMessage。
 */

const props = defineProps<{
  readonly stockCode: string
}>()

/** 默认字段与后端 DEFAULT_AS_OF_FIELDS 对齐（三表各取一项）。 */
const FIELD_OPTIONS: Array<{ label: string; value: string }> = [
  { label: '营业收入', value: 'revenue' },
  { label: '归母净利润', value: 'parent_net_profit' },
  { label: '总资产', value: 'total_assets' },
  { label: '归母净资产', value: 'total_equity_parent' },
  { label: '经营活动现金流量净额', value: 'cf_from_operating' },
]
const DEFAULT_FIELDS: readonly string[] = FIELD_OPTIONS.map((item) => item.value)

const REPORT_TYPE_LABELS: Readonly<Record<string, string>> = {
  annual: '年报',
  semi_annual: '半年报',
  quarterly: '季报',
}

const chosenFields = ref<string[]>([...DEFAULT_FIELDS])
const asOf = ref<number | null>(defaultAsOf())
const loading = ref(false)
const errorText = ref('')
const data = ref<AsReportedResponse | null>(null)
const notFoundReason = ref('')
const notFoundMessage = ref('')
const notFoundHint = ref('')

// 连点/快速切换时只接受最后一次请求的结果，避免旧响应覆盖新条件。
let requestId = 0

/** 默认观察日：1 个季度前（按自然月回退，月末日期自动收敛到当月最后一天）。 */
function defaultAsOf(): number {
  const now = new Date()
  now.setHours(0, 0, 0, 0)
  const day = now.getDate()
  now.setDate(1)
  now.setMonth(now.getMonth() - 3)
  const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate()
  now.setDate(Math.min(day, lastDay))
  return now.getTime()
}

/** 按本地年月日格式化，避免 `toISOString()` 在东八区把日期减一天。 */
function toIsoDate(timestamp: number): string {
  const d = new Date(timestamp)
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${month}-${day}`
}

function fieldLabel(name: string): string {
  return FIELD_OPTIONS.find((item) => item.value === name)?.label ?? name
}

function detailValue(detail: ResearchErrorDetail | undefined, key: string): string | null {
  if (!detail) return null
  const value = (detail as unknown as Record<string, unknown>)[key]
  return typeof value === 'string' && value.trim() ? value : null
}

const notFoundTitle = computed(() => {
  if (notFoundReason.value === 'no_announce_date_records') return '没有公布日记录'
  if (notFoundReason.value === 'no_visible_period_before_as_of') return '所选日期早于最早公布日'
  if (notFoundReason.value === 'visible_period_has_no_financials') return '可见报告期没有财务数据'
  return '没有可用数据'
})

const fieldRows = computed(() => {
  if (!data.value) return []
  const values = data.value.fields ?? {}
  const missing = data.value.missing_fields ?? []
  return chosenFields.value.map((name) => ({
    name,
    label: fieldLabel(name),
    value: values[name] ?? null,
    missing: missing.includes(name),
  }))
})

const reportTypeText = computed(() => {
  const type = data.value?.report_type
  if (!type) return '—'
  return REPORT_TYPE_LABELS[type] ?? type
})

function describeNotFound(reason: string, detail: ResearchErrorDetail | undefined): {
  message: string
  hint: string
} {
  const earliest = detailValue(detail, 'earliest_announce_date')
  const reportDate = detailValue(detail, 'report_date')
  if (reason === 'no_announce_date_records') {
    return {
      message: '该股票没有任何公布日记录（financial_report_dates 未登记），后端不做可见性推断，因此无法确定「当时可见」的报告期。',
      hint: '可先到「数据状态」页确认财务报告日采集进度；未登记公布日的报告期不会参与判定。',
    }
  }
  if (reason === 'no_visible_period_before_as_of') {
    return {
      message: `所选日期早于该股最早登记的公布日${earliest ? `（${earliest}）` : ''}，当天还看不到任何一期财报数据。`,
      hint: '请选择一个更晚的观察日（不早于最早公布日）后重试。',
    }
  }
  if (reason === 'visible_period_has_no_financials') {
    return {
      message: `已定位到可见报告期${reportDate ? `（${reportDate}）` : ''}，但利润表 / 资产负债表 / 现金流量表都没有该期数据。`,
      hint: '这是数据覆盖缺口，不是查询参数问题；可换一个观察日或到「数据状态」页查看采集进度。',
    }
  }
  return {
    message: reason ? `该查询没有可用数据（reason: ${reason}）。` : '该查询没有可用数据。',
    hint: '请调整观察日或字段后重试。',
  }
}

async function fetchData(): Promise<void> {
  if (!props.stockCode) return
  if (chosenFields.value.length === 0) {
    requestId += 1
    loading.value = false
    data.value = null
    notFoundReason.value = ''
    notFoundMessage.value = ''
    notFoundHint.value = ''
    errorText.value = '请至少选择一个财务字段。'
    return
  }
  if (asOf.value === null) {
    requestId += 1
    loading.value = false
    data.value = null
    notFoundReason.value = ''
    notFoundMessage.value = ''
    notFoundHint.value = ''
    errorText.value = '请选择观察日。'
    return
  }
  const current = ++requestId
  loading.value = true
  errorText.value = ''
  notFoundReason.value = ''
  notFoundMessage.value = ''
  notFoundHint.value = ''
  data.value = null
  try {
    const resp = await axios.get<AsReportedResponse>('/api/research/as-reported', {
      params: {
        code: props.stockCode,
        as_of: toIsoDate(asOf.value),
        fields: chosenFields.value.join(','),
      },
    })
    if (current !== requestId) return
    data.value = resp.data
  } catch (e) {
    if (current !== requestId) return
    data.value = null
    if (isAxiosError(e) && e.response?.status === 404) {
      const detail = e.response.data?.detail as ResearchErrorDetail | undefined
      const reason = detailValue(detail, 'reason') ?? ''
      const described = describeNotFound(reason, detail)
      notFoundReason.value = reason
      notFoundMessage.value = described.message
      notFoundHint.value = described.hint
      return
    }
    errorText.value = friendlyErrorMessage(e, '加载「当时可见」财务值失败')
  } finally {
    if (current === requestId) loading.value = false
  }
}

watch(
  [asOf, chosenFields],
  () => {
    void fetchData()
  },
  { deep: true },
)

watch(
  () => props.stockCode,
  () => {
    void fetchData()
  },
)

onMounted(() => {
  void fetchData()
})
</script>

<template>
  <section class="pit-card" aria-label="当时可见财务值">
    <header class="pit-heading">
      <div>
        <p>POINT IN TIME</p>
        <h2>当时可见财务值</h2>
      </div>
      <div class="pit-controls">
        <n-date-picker
          v-model:value="asOf"
          type="date"
          size="small"
          style="width: 150px"
          aria-label="选择观察日"
        />
        <n-select
          v-model:value="chosenFields"
          :options="FIELD_OPTIONS"
          multiple
          :max-tag-count="2"
          size="small"
          style="width: 320px"
          placeholder="选择财务字段"
          aria-label="选择财务字段"
        />
      </div>
    </header>

    <p class="pit-note">
      「当时可见」只决定取哪一期财报；数字仍是库内该报告期唯一一份（今天的最新重述值），
      不还原当年披露时的原始数值。回测时应使用公告日之后才可见的口径。
    </p>

    <div v-if="data?.value_basis" class="pit-badges">
      <n-tag type="warning" size="small">口径 {{ data.value_basis }}</n-tag>
      <span class="pit-badge-note">最新重述值，不是当年原始披露值</span>
    </div>
    <p v-if="data?.disclaimer" class="pit-disclaimer">{{ data.disclaimer }}</p>

    <n-spin :show="loading">
      <div v-if="errorText" class="pit-error">{{ errorText }}</div>
      <div v-else-if="notFoundMessage" class="pit-notice" role="status">
        <strong>{{ notFoundTitle }}</strong>
        <p>{{ notFoundMessage }}</p>
        <p v-if="notFoundHint" class="pit-notice-hint">{{ notFoundHint }}</p>
      </div>
      <div v-else-if="data" class="pit-body">
        <dl class="pit-meta">
          <div><dt>观察日</dt><dd>{{ data.as_of || '—' }}</dd></div>
          <div><dt>可见报告期</dt><dd>{{ data.report_date || '—' }}</dd></div>
          <div><dt>报告期类型</dt><dd>{{ reportTypeText }}</dd></div>
          <div><dt>公布日</dt><dd>{{ data.announce_date || '—' }}</dd></div>
          <div><dt>公布后</dt><dd>{{ data.days_since_announce != null ? `${data.days_since_announce} 天` : '—' }}</dd></div>
          <div><dt>公布日来源</dt><dd>{{ data.source || '—' }}</dd></div>
        </dl>

        <div class="pit-fields">
          <div
            v-for="row in fieldRows"
            :key="row.name"
            class="pit-field"
            :class="{ missing: row.value == null }"
          >
            <span class="pit-field-label">{{ row.label }}</span>
            <strong class="pit-field-value">{{ fmt(row.value) }}</strong>
            <small v-if="row.value == null" class="pit-field-note">{{ row.missing ? '该期无此字段值' : '暂无数据' }}</small>
          </div>
        </div>

        <p v-if="data.missing_fields?.length" class="pit-foot">
          缺失字段：{{ data.missing_fields.map((name) => fieldLabel(name)).join('、') }}
        </p>
        <p v-if="data.value_tables?.length" class="pit-foot">
          数值来源表：{{ data.value_tables.join('、') }}
        </p>
        <p v-if="data.value_source" class="pit-foot">取数口径：{{ data.value_source }}</p>
        <ul v-if="data.limitations?.length" class="pit-limitations">
          <li v-for="item in data.limitations" :key="item">{{ item }}</li>
        </ul>
      </div>
      <div v-else class="pit-empty">
        <n-empty description="选择观察日与字段后查询「当时可见」财务值" />
      </div>
    </n-spin>
  </section>
</template>

<style scoped>
.pit-card {
  min-width: 0;
  padding: 22px 25px 25px;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 4px 17px rgba(48, 82, 59, 0.045);
}
.pit-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.pit-heading p {
  margin: 0;
  color: #91a097;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.13em;
}
.pit-heading h2 {
  margin: 7px 0 0;
  font-size: 18px;
}
.pit-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.pit-note {
  margin: 12px 0 12px;
  color: #85928a;
  font-size: 11px;
  line-height: 1.6;
}
.pit-badges {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.pit-badge-note {
  color: #a08a45;
  font-size: 10px;
}
.pit-disclaimer {
  margin: 0 0 14px;
  padding: 9px 11px;
  border-radius: 9px;
  background: #fdf7e8;
  color: #8b7433;
  font-size: 10px;
  line-height: 1.6;
}
.pit-body {
  display: grid;
  gap: 13px;
}
.pit-meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 10px;
  margin: 0;
  padding: 12px;
  border-radius: 10px;
  background: #fafcf9;
}
.pit-meta div {
  min-width: 0;
}
.pit-meta dt {
  color: #98a39b;
  font-size: 9px;
}
.pit-meta dd {
  overflow: hidden;
  margin: 4px 0 0;
  color: #405a49;
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pit-fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px;
}
.pit-field {
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid #edf1ee;
  border-radius: 10px;
  background: #fff;
}
.pit-field.missing {
  background: #fafcf9;
  opacity: 0.75;
}
.pit-field-label {
  display: block;
  overflow: hidden;
  color: #8a978e;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pit-field-value {
  display: block;
  margin-top: 7px;
  color: #365944;
  font-size: 18px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.pit-field.missing .pit-field-value {
  color: #9aa69f;
}
.pit-field-note {
  display: block;
  margin-top: 4px;
  color: #a3ada6;
  font-size: 9px;
}
.pit-foot {
  margin: 0;
  color: #91a097;
  font-size: 10px;
  line-height: 1.6;
}
.pit-limitations {
  margin: 0;
  padding-left: 18px;
  color: #9aa69f;
  font-size: 10px;
  line-height: 1.7;
}
.pit-error {
  padding: 18px;
  color: #c0392b;
  font-size: 12px;
}
.pit-notice {
  padding: 15px 17px;
  border-radius: 10px;
  background: #fbf4ef;
  color: #8a6042;
}
.pit-notice strong {
  display: block;
  margin-bottom: 6px;
  font-size: 13px;
}
.pit-notice p {
  margin: 0;
  font-size: 11px;
  line-height: 1.7;
}
.pit-notice-hint {
  margin-top: 6px !important;
  color: #9b8069;
  font-size: 10px !important;
}
.pit-empty {
  padding: 22px 0 8px;
}
@media (max-width: 760px) {
  .pit-controls {
    width: 100%;
  }
  .pit-controls :deep(.n-date-picker),
  .pit-controls :deep(.n-select) {
    width: 100% !important;
  }
}
</style>
