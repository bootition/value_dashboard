<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NEmpty, NSelect, NSpin } from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'

export interface MetricHistoryPoint {
  readonly report_date: string
  readonly value: number | null
}

export interface MetricHistoryStatistics {
  readonly samples: number
  readonly mean: number | null
  readonly sigma: number | null
  readonly p10: number | null
  readonly p20: number | null
  readonly p50: number | null
  readonly p80: number | null
  readonly max: number | null
  readonly current: number | null
  readonly current_date: string | null
  readonly reason: string | null
}

export interface MetricHistoryResponse {
  readonly stock_code: string
  readonly metric: string
  readonly period: 'annual' | 'quarterly' | 'ttm'
  readonly series: readonly MetricHistoryPoint[]
  readonly statistics: MetricHistoryStatistics
  readonly disclaimer: string
}

export interface MetricHistoryOption {
  readonly label: string
  readonly value: string
}

const props = withDefaults(
  defineProps<{
    readonly stockCode: string
    readonly metricOptions: readonly MetricHistoryOption[]
    readonly defaultMetric?: string
  }>(),
  { defaultMetric: '' },
)

const metric = ref(props.defaultMetric || props.metricOptions[0]?.value || '')
const loading = ref(false)
const errorText = ref('')
const data = ref<MetricHistoryResponse | null>(null)

const W = 640
const H = 230
const PAD = { top: 18, right: 16, bottom: 30, left: 56 }

const series = computed(() => data.value?.series ?? [])
const stats = computed(() => data.value?.statistics ?? {
  samples: 0, mean: null, sigma: null, p10: null, p20: null,
  p50: null, p80: null, max: null, current: null, current_date: null, reason: null,
} as MetricHistoryStatistics)

const points = computed(() =>
  series.value
    .filter((item) => item.value != null)
    .map((item) => ({ date: item.report_date, value: item.value as number })),
)
const rows = computed(() => series.value.map((item) => ({
  date: item.report_date,
  value: item.value,
})))

const dateRange = computed(() => {
  if (!rows.value.length) return { min: 0, max: 1 }
  const first = Date.parse(rows.value[0]!.date)
  const last = Date.parse(rows.value[rows.value.length - 1]!.date)
  return { min: first, max: Math.max(last, first + 86400000) }
})

const valueRange = computed(() => {
  if (!points.value.length) return { min: 0, max: 1 }
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  for (const p of points.value) {
    if (p.value < min) min = p.value
    if (p.value > max) max = p.value
  }
  for (const v of [stats.value.p10, stats.value.p50, stats.value.p80, stats.value.mean]) {
    if (v != null) {
      min = Math.min(min, v)
      max = Math.max(max, v)
    }
  }
  const span = max - min || 1
  return { min: min - span * 0.08, max: max + span * 0.08 }
})

const x = (i: number) => {
  if (rows.value.length <= 1) return PAD.left + (W - PAD.left - PAD.right) / 2
  const t = (Date.parse(rows.value[i]!.date) - dateRange.value.min) /
    (dateRange.value.max - dateRange.value.min)
  return PAD.left + t * (W - PAD.left - PAD.right)
}
const y = (v: number) =>
  PAD.top + (1 - (v - valueRange.value.min) / (valueRange.value.max - valueRange.value.min)) *
    (H - PAD.top - PAD.bottom)

const linePath = computed(() => {
  const segments: string[] = []
  let started = false
  for (let i = 0; i < rows.value.length; i += 1) {
    const value = rows.value[i]!.value
    if (value == null) {
      started = false
      continue
    }
    segments.push(`${started ? 'L' : 'M'}${x(i).toFixed(1)},${y(value).toFixed(1)}`)
    started = true
  }
  return segments.join(' ')
})

const overlayLines = computed(() => {
  const lines: Array<{ value: number; label: string; dash: string; color: string }> = []
  if (stats.value.p10 != null) lines.push({ value: stats.value.p10, label: 'P10', dash: '5,4', color: '#e05d5d' })
  if (stats.value.p50 != null) lines.push({ value: stats.value.p50, label: 'P50', dash: '0', color: '#b98a2e' })
  if (stats.value.p80 != null) lines.push({ value: stats.value.p80, label: 'P80', dash: '2,3', color: '#4a87c4' })
  if (stats.value.mean != null) lines.push({ value: stats.value.mean, label: '均值', dash: '4,3', color: '#7b6ba6' })
  return lines
})

const yTicks = computed(() => {
  const ticks: Array<{ pos: number; label: string }> = []
  const { min, max } = valueRange.value
  for (let i = 0; i <= 4; i += 1) {
    const v = min + ((max - min) * i) / 4
    ticks.push({
      pos: PAD.top + (1 - i / 4) * (H - PAD.top - PAD.bottom),
      label: v.toFixed(2),
    })
  }
  return ticks
})

const hoverIndex = ref<number | null>(null)
const hoverPoint = computed(() => {
  if (hoverIndex.value == null) return null
  const row = rows.value[hoverIndex.value]
  if (!row || row.value == null) return null
  return { date: row.date, value: row.value, x: x(hoverIndex.value), y: y(row.value) }
})

function onChartMove(event: MouseEvent) {
  const svg = event.currentTarget as SVGSVGElement
  const rect = svg.getBoundingClientRect()
  if (!rows.value.length || rect.width === 0) return
  const px = ((event.clientX - rect.left) / rect.width) * W
  const raw = (px - PAD.left) / (W - PAD.left - PAD.right)
  const index = Math.round(raw * (rows.value.length - 1))
  if (index < 0 || index >= rows.value.length) {
    hoverIndex.value = null
    return
  }
  hoverIndex.value = rows.value[index]!.value == null ? null : index
}

async function fetchData() {
  loading.value = true
  errorText.value = ''
  try {
    const resp = await axios.get<MetricHistoryResponse>(
      `/api/stock/${props.stockCode}/metric-history`,
      { params: { metric: metric.value, period: 'ttm' } },
    )
    data.value = resp.data
  } catch (e) {
    if (isAxiosError(e) && e.response?.status === 404) return
    errorText.value = friendlyErrorMessage(e, '加载指标历史失败')
  } finally {
    loading.value = false
  }
}

watch(metric, fetchData)
watch(() => props.stockCode, () => {
  data.value = null
  fetchData()
})
onMounted(fetchData)
</script>

<template>
  <div class="metric-history-chart">
    <div class="mh-controls">
      <n-select
        v-model:value="metric"
        :options="(metricOptions as unknown as Array<{ label: string; value: string }>)"
        size="small"
        style="width: 190px"
        aria-label="选择指标"
      />
      <span class="mh-note">TTM · 上市以来 · latest_restated</span>
    </div>
    <n-spin :show="loading">
      <div v-if="errorText" class="mh-error">{{ errorText }}</div>
      <n-empty v-else-if="points.length === 0" description="暂无该指标历史序列" />
      <div v-else class="mh-frame">
        <svg viewBox="0 0 640 230" role="img" :aria-label="`${metric} 历史序列`">
          <g v-for="tick in yTicks" :key="tick.pos" stroke="#edf2ee" stroke-width="1">
            <line :x1="PAD.left" :x2="W - PAD.right" :y1="tick.pos" :y2="tick.pos" />
          </g>
          <g v-for="tick in yTicks" :key="`t-${tick.pos}`" fill="#85928a" font-size="9">
            <text :x="PAD.left - 6" :y="tick.pos + 3" text-anchor="end">{{ tick.label }}</text>
          </g>
          <line
            v-for="line in overlayLines"
            :key="line.label"
            :x1="PAD.left" :x2="W - PAD.right"
            :y1="y(line.value)" :y2="y(line.value)"
            :stroke="line.color" stroke-width="1.3"
            :stroke-dasharray="line.dash"
          />
          <path :d="linePath" fill="none" stroke="#57966d" stroke-width="2" />
          <rect
            :x="PAD.left" :y="PAD.top"
            :width="W - PAD.left - PAD.right"
            :height="H - PAD.top - PAD.bottom"
            fill="transparent"
            @mousemove="onChartMove"
            @mouseleave="hoverIndex = null"
          />
          <g v-if="hoverPoint">
            <line
:x1="hoverPoint.x" :x2="hoverPoint.x" :y1="PAD.top" :y2="H - PAD.bottom"
                  stroke="#365944" stroke-width="0.7" stroke-dasharray="2,3"
/>
            <circle
:cx="hoverPoint.x" :cy="hoverPoint.y" r="4" fill="#57966d"
                    stroke="#fff" stroke-width="1.4"
/>
          </g>
        </svg>
        <div
v-if="hoverPoint" class="mh-hover"
             :style="{ left: `${hoverPoint.x / W * 100}%`, top: `${hoverPoint.y / H * 100}%` }"
>
          <b>{{ hoverPoint.date }}</b>
          <span>{{ hoverPoint.value.toFixed(2) }}</span>
        </div>
        <div class="mh-legend">
          <span><i class="dot series" />序列</span>
          <span v-for="line in overlayLines" :key="line.label">
            <i class="dot band" :style="{ background: line.color }" />{{ line.label }}
          </span>
        </div>
        <p class="mh-foot">
          样本 {{ stats.samples }} ·
          当前 {{ stats.current != null ? stats.current.toFixed(2) : '—' }}
          <template v-if="stats.reason"> · {{ stats.reason }}</template>
        </p>
      </div>
    </n-spin>
  </div>
</template>

<style scoped>
.metric-history-chart {
  padding: 16px 18px 18px;
  border-radius: 12px;
  background: #fafcf9;
}
.mh-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.mh-note {
  color: #91a097;
  font-size: 10px;
}
.mh-frame {
  position: relative;
}
.mh-frame svg {
  display: block;
  width: 100%;
  height: auto;
}
.mh-hover {
  position: absolute;
  z-index: 3;
  padding: 5px 8px;
  border-radius: 7px;
  background: rgba(38, 57, 45, 0.94);
  color: #fff;
  font-size: 10px;
  pointer-events: none;
  transform: translate(-50%, calc(-100% - 9px));
  white-space: nowrap;
}
.mh-hover b,
.mh-hover span {
  display: block;
}
.mh-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 8px;
  color: #627368;
  font-size: 11px;
}
.mh-legend .dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  margin-right: 5px;
  border-radius: 50%;
}
.mh-legend .series {
  background: #57966d;
}
.mh-legend .band {
  background: #b98a2e;
}
.mh-foot {
  margin: 6px 0 0;
  color: #91a097;
  font-size: 10px;
}
.mh-error {
  padding: 16px;
  color: #c0392b;
  font-size: 12px;
}
</style>
