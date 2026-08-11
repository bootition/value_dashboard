<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NEmpty, NRadioButton, NRadioGroup, NSelect, NSpin } from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import type { ResearchStatisticsResponse } from '../types/stock-detail.ts'

/**
 * 历史研究统计（reports/68 P4，PRD §10.7）
 *
 * 可选序列（PE/PB/TTM股息率/10年利差）、窗口（1/3/5/10/全部）与视图：
 * - 经验分位带：P10/P20/P50/P80/窗口最大有效值 + 当前值
 * - 均值与 σ 带：μ、μ±1σ、μ±2σ + 当前 z-score
 * 统计仅描述历史位置，不假设正态分布，不构成投资提示。
 */

const props = withDefaults(
  defineProps<{
    readonly stockCode: string
    readonly defaultMetric?: 'pe_ttm' | 'pb_mrq' | 'ttm_dividend_yield' | 'spread_10y'
  }>(),
  { defaultMetric: 'pe_ttm' },
)

const METRIC_OPTIONS: Array<{ label: string; value: string }> = [
  { label: 'PE-TTM', value: 'pe_ttm' },
  { label: 'PB-MRQ', value: 'pb_mrq' },
  { label: 'TTM已实施股息率', value: 'ttm_dividend_yield' },
  { label: '股息率-国债10年利差', value: 'spread_10y' },
]
const WINDOW_OPTIONS = [
  { label: '1年', value: 1 },
  { label: '3年', value: 3 },
  { label: '5年', value: 5 },
  { label: '10年', value: 10 },
  { label: '全部', value: 99 },
]

const metric = ref<string>(props.defaultMetric)
const window = ref<number>(10)
const viewMode = ref<'band' | 'sigma'>('band')
const loading = ref(false)
const data = ref<ResearchStatisticsResponse | null>(null)
const errorText = ref('')

const W = 640
const H = 230
const PAD = { top: 16, right: 14, bottom: 28, left: 54 }

const series = computed(() => data.value?.series ?? [])
const stats = computed(() => data.value?.statistics ?? {})
const activeStats = computed(() => stats.value[`${window.value}y`] ?? {})

const hasSeries = computed(() => series.value.some((item) => item[metric.value as keyof typeof item] != null))
const usableSeries = computed(() =>
  series.value
    .filter((item) => item[metric.value as keyof typeof item] != null)
    .map((item) => ({ date: item.price_date, value: item[metric.value as keyof typeof item] as number })),
)

const valueRange = computed(() => {
  const points = usableSeries.value
  if (!points.length) return { min: 0, max: 1 }
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  for (const p of points) {
    if (p.value < min) min = p.value
    if (p.value > max) max = p.value
  }
  const s = activeStats.value
  if (viewMode.value === 'sigma' && s.sigma != null && s.mean != null) {
    min = Math.min(min, s.mean - 2 * s.sigma)
    max = Math.max(max, s.mean + 2 * s.sigma)
  }
  if (viewMode.value === 'band' && s.p80 != null) {
    if (s.p10 != null) min = Math.min(min, s.p10)
    max = Math.max(max, s.max ?? max)
  }
  const span = max - min || 1
  return { min: min - span * 0.08, max: max + span * 0.08 }
})

const x = (i: number) => PAD.left + (usableSeries.value.length === 1 ? (W - PAD.left - PAD.right) / 2 : (i / (usableSeries.value.length - 1)) * (W - PAD.left - PAD.right))
const y = (v: number) => PAD.top + (1 - (v - valueRange.value.min) / (valueRange.value.max - valueRange.value.min)) * (H - PAD.top - PAD.bottom)

const linePath = computed(() => {
  const points = usableSeries.value
  if (!points.length) return ''
  return points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(' ')
})

const yTicks = computed(() => {
  const ticks: Array<{ pos: number; label: string }> = []
  const { min, max } = valueRange.value
  for (let i = 0; i <= 4; i += 1) {
    const v = min + ((max - min) * i) / 4
    ticks.push({ pos: PAD.top + (1 - i / 4) * (H - PAD.top - PAD.bottom), label: `${v.toFixed(2)}` })
  }
  return ticks
})

const bandLines = computed(() => {
  const s = activeStats.value
  if (viewMode.value !== 'band' || s.p10 == null) return []
  const lines: Array<{ value: number; label: string; dash: string }> = []
  if (s.p10 != null) lines.push({ value: s.p10, label: 'P10', dash: '5,4' })
  if (s.p20 != null) lines.push({ value: s.p20, label: 'P20', dash: '2,3' })
  if (s.p50 != null) lines.push({ value: s.p50, label: 'P50', dash: '0' })
  if (s.p80 != null) lines.push({ value: s.p80, label: 'P80', dash: '2,3' })
  if (s.max != null) lines.push({ value: s.max, label: '最大', dash: '5,4' })
  return lines
})

const sigmaLines = computed(() => {
  const s = activeStats.value
  if (viewMode.value !== 'sigma' || s.mean == null || s.sigma == null) return []
  const { mean, sigma } = s
  return [
    { value: mean - 2 * sigma, label: 'μ-2σ', dash: '5,4' },
    { value: mean - sigma, label: 'μ-1σ', dash: '2,3' },
    { value: mean, label: 'μ', dash: '0' },
    { value: mean + sigma, label: 'μ+1σ', dash: '2,3' },
    { value: mean + 2 * sigma, label: 'μ+2σ', dash: '5,4' },
  ]
})

const overlayLines = computed(() => (viewMode.value === 'band' ? bandLines.value : sigmaLines.value))

function statRow(label: string, value: unknown, unit = ''): string {
  if (value === null || value === undefined) return `${label} —`
  if (typeof value === 'number') return `${label} ${value.toFixed(2)}${unit}`
  return `${label} ${String(value)}`
}

async function fetchData() {
  loading.value = true
  errorText.value = ''
  try {
    const resp = await axios.get<ResearchStatisticsResponse>(
      `/api/stock/${props.stockCode}/research-statistics`,
      { params: { metric: metric.value } },
    )
    data.value = resp.data
  } catch (e) {
    if (isAxiosError(e) && e.response?.status === 404) return
    errorText.value = friendlyErrorMessage(e, '加载历史研究统计失败')
  } finally {
    loading.value = false
  }
}

watch(metric, fetchData)
onMounted(fetchData)
</script>

<template>
  <section class="stat-card" aria-label="历史研究统计">
    <header class="stat-heading">
      <div>
        <p>RESEARCH STATISTICS</p>
        <h2>历史研究统计</h2>
      </div>
      <div class="stat-controls">
        <n-select
          v-model:value="metric"
          :options="METRIC_OPTIONS"
          size="small"
          style="width: 190px"
          aria-label="选择序列"
        />
        <n-select
          v-model:value="window"
          :options="WINDOW_OPTIONS"
          size="small"
          style="width: 96px"
          aria-label="选择窗口"
        />
        <n-radio-group v-model:value="viewMode" size="small">
          <n-radio-button value="band">经验分位带</n-radio-button>
          <n-radio-button value="sigma">均值与σ带</n-radio-button>
        </n-radio-group>
      </div>
    </header>

    <p class="stat-note">
      口径：最新重述回看（历史日使用该日对应报告期当前最新重述财务值与历史有效总股本），
      不代表当时市场可见信息，不用于回测。PE≤0 不参与统计；历史股本未覆盖日 PE/PB 为缺失。
    </p>

    <n-spin :show="loading">
      <div v-if="errorText" class="stat-error">{{ errorText }}</div>
      <div v-else-if="!hasSeries" class="stat-empty">
        <n-empty description="暂无可用序列数据" />
      </div>
      <div v-else class="stat-chart-frame">
        <svg viewBox="0 0 640 230" role="img" :aria-label="`${metric} 历史序列与${viewMode === 'band' ? '经验分位带' : '均值与σ带'}`">
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
            stroke="#b98a2e" stroke-width="1"
            :stroke-dasharray="line.dash"
          />
          <path :d="linePath" fill="none" stroke="#57966d" stroke-width="2" />
        </svg>
        <div class="stat-legend">
          <span><i class="dot series" />序列</span>
          <span v-for="line in overlayLines" :key="line.label"><i class="dot band" />{{ line.label }}</span>
        </div>
        <p class="stat-foot">
          样本 {{ activeStats.samples ?? '—' }} ·
          覆盖 {{ activeStats.coverage_pct != null ? `${activeStats.coverage_pct.toFixed(1)}%` : '—' }} ·
          当前 {{ statRow('', activeStats.current) }}
          <template v-if="activeStats.zscore != null"> · z-score {{ activeStats.zscore.toFixed(2) }}</template>
          <template v-if="activeStats.reason"> · {{ activeStats.reason }}</template>
        </p>
      </div>
    </n-spin>
  </section>
</template>

<style scoped>
.stat-card {
  min-width: 0;
  padding: 22px 25px 25px;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 4px 17px rgba(48, 82, 59, 0.045);
}
.stat-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.stat-heading p {
  margin: 0;
  color: #91a097;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.13em;
}
.stat-heading h2 {
  margin: 7px 0 0;
  font-size: 18px;
}
.stat-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.stat-note {
  margin: 12px 0 14px;
  color: #85928a;
  font-size: 11px;
  line-height: 1.6;
}
.stat-chart-frame {
  padding: 12px;
  border-radius: 10px;
  background: #fafcf9;
}
.stat-chart-frame svg {
  display: block;
  width: 100%;
  height: auto;
}
.stat-error {
  padding: 20px;
  color: #c0392b;
  font-size: 12px;
}
.stat-empty {
  padding: 24px 0 10px;
}
.stat-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 10px;
  color: #627368;
  font-size: 11px;
}
.stat-legend .dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  margin-right: 5px;
  border-radius: 50%;
}
.stat-legend .series {
  background: #57966d;
}
.stat-legend .band {
  background: #b98a2e;
}
.stat-foot {
  margin: 8px 0 0;
  color: #91a097;
  font-size: 10px;
}
</style>
