<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NAlert, NEmpty, NRadioButton, NRadioGroup, NSelect, NSpin } from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import type { TreasuryComparisonResponse } from '../types/stock-detail.ts'

/**
 * 股东回报 · 国债比较（reports/68 P3）
 *
 * 默认展示 TTM 已实施股息率相对 10 年期国债的利差历史；
 * 可切换原始双线（TTM 股息率 / 国债收益率）与可得期限。
 * 只做数据与透明计算展示，不提供比值、提示或投资结论。
 */

const props = defineProps<{
  readonly stockCode: string
}>()

const TENOR_OPTIONS: Array<{ label: string; value: number }> = [
  { label: '3月', value: 0.25 },
  { label: '6月', value: 0.5 },
  { label: '1年', value: 1 },
  { label: '2年', value: 2 },
  { label: '3年', value: 3 },
  { label: '5年', value: 5 },
  { label: '7年', value: 7 },
  { label: '10年', value: 10 },
  { label: '30年', value: 30 },
]

const tenor = ref<number>(10)
const viewMode = ref<'spread' | 'double'>('spread')
const limit = ref(250)
const loading = ref(false)
const data = ref<TreasuryComparisonResponse | null>(null)
const errorText = ref('')

const series = computed(() => data.value?.series ?? [])
const hasData = computed(() => series.value.some((item) => item.spread != null || item.curve_yield != null))

const W = 620
const H = 210
const PAD = { top: 14, right: 12, bottom: 26, left: 46 }

// P3-6 修复（reports/73）：API 按价格日降序返回，图表按时间正序（左旧右新），
// 并用日期线性映射 x 轴，节假日/停牌日自然压缩，不再按样本索引等距。
const viewRows = computed(() => {
  const rows = [...series.value].reverse()
  return rows.filter((item) =>
    viewMode.value === 'spread'
      ? item.spread != null
      : item.ttm_div_yield != null || item.curve_yield != null,
  )
})

const dateRange = computed(() => {
  const rows = viewRows.value
  if (!rows.length) return { min: 0, max: 1 }
  const first = Date.parse(rows[0].price_date)
  const last = Date.parse(rows[rows.length - 1].price_date)
  return { min: first, max: Math.max(last, first + 86400000) }
})

const x = (i: number) => {
  const rows = viewRows.value
  if (rows.length <= 1) return PAD.left + (W - PAD.left - PAD.right) / 2
  const t = (Date.parse(rows[i].price_date) - dateRange.value.min) / (dateRange.value.max - dateRange.value.min)
  return PAD.left + t * (W - PAD.left - PAD.right)
}

const valueRange = computed(() => {
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  for (const item of viewRows.value) {
    const values = viewMode.value === 'spread'
      ? [item.spread]
      : [item.ttm_div_yield, item.curve_yield]
    for (const v of values) {
      if (v == null) continue
      if (v < min) min = v
      if (v > max) max = v
    }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { min: 0, max: 1 }
  const span = max - min || 1
  return { min: min - span * 0.12, max: max + span * 0.12 }
})

function pointPath(lines: Array<number | null>): string {
  const rows = viewRows.value
  if (!rows.length) return ''
  const y = (v: number) =>
    PAD.top + (1 - (v - valueRange.value.min) / (valueRange.value.max - valueRange.value.min)) * (H - PAD.top - PAD.bottom)
  const segments: string[] = []
  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    if (lines[lineIndex] === null) continue
    const points: string[] = []
    let started = false
    for (let i = 0; i < rows.length; i += 1) {
      const v = rows[i][viewMode.value === 'spread' ? 'spread' : lineIndex === 0 ? 'ttm_div_yield' : 'curve_yield'] as number | null
      if (v == null) {
        started = false
        continue
      }
      const cmd = started ? 'L' : 'M'
      points.push(`${cmd}${x(i).toFixed(1)},${y(v).toFixed(1)}`)
      started = true
    }
    if (points.length) segments.push(points.join(' '))
  }
  return segments.join(' ')
}

// P3-1 修复（reports/73）：双线视图两条序列各自独立路径，
// 不再用同一合并路径画两遍（蓝色覆盖绿色）。
const spreadPath = computed(() => pointPath([1]))
const ttmPath = computed(() => pointPath([1, null]))
const bondPath = computed(() => pointPath([null, 1]))

const yTicks = computed(() => {
  const ticks: Array<{ pos: number; label: string }> = []
  const { min, max } = valueRange.value
  for (let i = 0; i <= 4; i += 1) {
    const v = min + ((max - min) * i) / 4
    ticks.push({
      pos: PAD.top + (1 - i / 4) * (H - PAD.top - PAD.bottom),
      label: `${v.toFixed(2)}%`,
    })
  }
  return ticks
})

async function fetchData() {
  loading.value = true
  errorText.value = ''
  try {
    const resp = await axios.get<TreasuryComparisonResponse>(
      `/api/stock/${props.stockCode}/treasury-comparison`,
      { params: { tenor: tenor.value, limit: limit.value } },
    )
    data.value = resp.data
  } catch (e) {
    if (isAxiosError(e) && e.response?.status === 404) return
    errorText.value = friendlyErrorMessage(e, '加载国债比较失败')
  } finally {
    loading.value = false
  }
}

watch(tenor, fetchData)
// 2026-08-14 红队 F6：股票切换（/stock/A → /stock/B 复用组件实例）时
// 此前不重拉，卡片持续显示上一只股票的序列；现清空并重拉。
watch(() => props.stockCode, () => {
  data.value = null
  fetchData()
})
onMounted(fetchData)
</script>

<template>
  <section class="treasury-card" aria-label="国债比较">
    <header class="treasury-heading">
      <div>
        <p>BOND COMPARISON</p>
        <h2>国债比较</h2>
      </div>
      <div class="treasury-controls">
        <n-radio-group v-model:value="viewMode" size="small">
          <n-radio-button value="spread">利差</n-radio-button>
          <n-radio-button value="double">双线</n-radio-button>
        </n-radio-group>
        <n-select
          v-model:value="tenor"
          :options="TENOR_OPTIONS"
          size="small"
          style="width: 96px"
          aria-label="选择国债期限"
        />
      </div>
    </header>

    <p class="treasury-note">
      口径：TTM已实施股息率（过去12个月已除权现金分红 / 收盘价）减去所选期限国债收益率；
      曲线取不晚于价格日的最近发布点，最大陈旧 5 个自然日。仅作研究展示，不构成投资结论。
    </p>

    <n-alert
      v-if="data?.auto_update_in_progress"
      type="info"
      :show-icon="true"
      class="treasury-update-note"
    >
      数据正在自动更新：曲线与股息数据为更新前快照，更新完成后自动恢复。
    </n-alert>

    <n-spin :show="loading">
      <div v-if="errorText" class="treasury-error">{{ errorText }}</div>
      <div v-else-if="!hasData" class="treasury-empty">
        <n-empty description="暂无国债比较数据（曲线或股息数据缺失）" />
      </div>
      <div v-else class="treasury-chart-frame">
        <svg viewBox="0 0 620 210" role="img" :aria-label="viewMode === 'spread' ? '股息率相对国债利差历史' : 'TTM股息率与国债收益率双线历史'">
          <g v-for="tick in yTicks" :key="tick.pos" stroke="#edf2ee" stroke-width="1">
            <line :x1="PAD.left" :x2="W - PAD.right" :y1="tick.pos" :y2="tick.pos" />
          </g>
          <g v-for="tick in yTicks" :key="`t-${tick.pos}`" fill="#85928a" font-size="9">
            <text :x="PAD.left - 6" :y="tick.pos + 3" text-anchor="end">{{ tick.label }}</text>
          </g>
          <template v-if="viewMode === 'spread'">
            <path :d="spreadPath" fill="none" stroke="#57966d" stroke-width="2" />
          </template>
          <template v-else>
            <path :d="ttmPath" fill="none" stroke="#57966d" stroke-width="2" />
            <path :d="bondPath" fill="none" stroke="#185482" stroke-width="2" />
          </template>
        </svg>
        <div class="treasury-legend">
          <span v-if="viewMode === 'spread'"><i class="dot spread" />利差（TTM股息率 − 国债收益率）</span>
          <template v-else>
            <span><i class="dot div" />TTM已实施股息率</span>
            <span><i class="dot bond" />国债收益率</span>
          </template>
        </div>
        <p class="treasury-foot">
          最新样本：{{ series[0]?.price_date ?? '—' }} ·
          {{ data?.provenance?.source ?? '—' }} · 置信度 {{ data?.provenance?.confidence ?? '—' }}
        </p>
      </div>
    </n-spin>
  </section>
</template>

<style scoped>
.treasury-card {
  min-width: 0;
  padding: 22px 25px 25px;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 4px 17px rgba(48, 82, 59, 0.045);
}
.treasury-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.treasury-heading p {
  margin: 0;
  color: #91a097;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.13em;
}
.treasury-heading h2 {
  margin: 7px 0 0;
  font-size: 18px;
}
.treasury-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.treasury-note {
  margin: 12px 0 14px;
  color: #85928a;
  font-size: 11px;
  line-height: 1.6;
}
.treasury-chart-frame {
  padding: 12px;
  border-radius: 10px;
  background: #fafcf9;
}
.treasury-chart-frame svg {
  display: block;
  width: 100%;
  height: auto;
}
.treasury-error {
  padding: 20px;
  color: #c0392b;
  font-size: 12px;
}
.treasury-empty {
  padding: 24px 0 10px;
}
.treasury-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 10px;
  color: #627368;
  font-size: 11px;
}
.treasury-legend .dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  margin-right: 5px;
  border-radius: 50%;
}
.treasury-legend .spread,
.treasury-legend .div {
  background: #57966d;
}
.treasury-legend .bond {
  background: #185482;
}
.treasury-foot {
  margin: 8px 0 0;
  color: #91a097;
  font-size: 10px;
}
@media (max-width: 640px) {
  .treasury-heading {
    flex-direction: column;
  }
}
</style>
