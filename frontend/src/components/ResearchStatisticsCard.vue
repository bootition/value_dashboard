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
const hoverIndex = ref<number | null>(null)

const W = 640
const H = 230
const PAD = { top: 16, right: 14, bottom: 28, left: 54 }

const series = computed(() => data.value?.series ?? [])
const stats = computed(() => data.value?.statistics ?? {})
const activeStats = computed(() => stats.value[`${window.value}y`] ?? {})

const hasSeries = computed(() => series.value.some((item) => item[metric.value as keyof typeof item] != null))
// 窗口内全部行（含 null）：x 轴保持交易日连续，null 在画线时断笔。
const windowRows = computed(() => {
  let rows = series.value.map((item) => ({
    date: item.price_date,
    value: item[metric.value as keyof typeof item] as number | null,
  }))
  if (window.value !== 99) {
    const start = new Date()
    start.setFullYear(start.getFullYear() - window.value)
    const startMs = start.getTime()
    rows = rows.filter((item) => Date.parse(item.date) >= startMs)
  }
  return rows
})
// P4-12 修复（reports/73）：序列按所选窗口过滤（与统计窗口一致），
// 不再全历史序列 + 窗口统计线并存造成误导。
const usableSeries = computed(() =>
  windowRows.value
    .filter((item) => item.value != null)
    .map((item) => ({ date: item.date, value: item.value as number })),
)

// P4-12：x 轴按日期线性映射（节假日/停牌日自然压缩）
const dateRange = computed(() => {
  const points = windowRows.value
  if (!points.length) return { min: 0, max: 1 }
  const first = Date.parse(points[0].date)
  const last = Date.parse(points[points.length - 1].date)
  return { min: first, max: Math.max(last, first + 86400000) }
})

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

const x = (i: number) => {
  const points = windowRows.value
  if (points.length <= 1) return PAD.left + (W - PAD.left - PAD.right) / 2
  const t = (Date.parse(points[i].date) - dateRange.value.min) / (dateRange.value.max - dateRange.value.min)
  return PAD.left + t * (W - PAD.left - PAD.right)
}
const y = (v: number) => PAD.top + (1 - (v - valueRange.value.min) / (valueRange.value.max - valueRange.value.min)) * (H - PAD.top - PAD.bottom)

const linePath = computed(() => {
  const rows = windowRows.value
  if (!rows.length) return ''
  const segments: string[] = []
  let started = false
  for (let i = 0; i < rows.length; i += 1) {
    const value = rows[i].value
    if (value == null) {
      started = false
      continue
    }
    segments.push(`${started ? 'L' : 'M'}${x(i).toFixed(1)},${y(value).toFixed(1)}`)
    started = true
  }
  return segments.join(' ')
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

const BAND_COLORS = ['#e05d5d', '#e8963e', '#b98a2e', '#4a87c4', '#7b6ba6']
const SIGMA_COLORS = ['#e05d5d', '#e8963e', '#b98a2e', '#4a87c4', '#7b6ba6']

const bandLines = computed(() => {
  const s = activeStats.value
  if (viewMode.value !== 'band' || s.p10 == null) return []
  const lines: Array<{ value: number; label: string; dash: string; color: string }> = []
  if (s.p10 != null) lines.push({ value: s.p10, label: 'P10', dash: '5,4', color: BAND_COLORS[0] })
  if (s.p20 != null) lines.push({ value: s.p20, label: 'P20', dash: '2,3', color: BAND_COLORS[1] })
  if (s.p50 != null) lines.push({ value: s.p50, label: 'P50', dash: '0', color: BAND_COLORS[2] })
  if (s.p80 != null) lines.push({ value: s.p80, label: 'P80', dash: '2,3', color: BAND_COLORS[3] })
  if (s.max != null) lines.push({ value: s.max, label: '最大', dash: '5,4', color: BAND_COLORS[4] })
  return lines
})

const sigmaLines = computed(() => {
  const s = activeStats.value
  if (viewMode.value !== 'sigma' || s.mean == null || s.sigma == null) return []
  const { mean, sigma } = s
  return [
    { value: mean - 2 * sigma, label: 'μ-2σ', dash: '5,4', color: SIGMA_COLORS[0] },
    { value: mean - sigma, label: 'μ-1σ', dash: '2,3', color: SIGMA_COLORS[1] },
    { value: mean, label: 'μ', dash: '0', color: SIGMA_COLORS[2] },
    { value: mean + sigma, label: 'μ+1σ', dash: '2,3', color: SIGMA_COLORS[3] },
    { value: mean + 2 * sigma, label: 'μ+2σ', dash: '5,4', color: SIGMA_COLORS[4] },
  ]
})

const overlayLines = computed(() => (viewMode.value === 'band' ? bandLines.value : sigmaLines.value))

function statRow(label: string, value: unknown, unit = ''): string {
  if (value === null || value === undefined) return `${label} —`
  if (typeof value === 'number') return `${label} ${value.toFixed(2)}${unit}`
  return `${label} ${String(value)}`
}

const hoverPoint = computed(() => {
  if (hoverIndex.value == null) return null
  const row = windowRows.value[hoverIndex.value]
  if (!row || row.value == null) return null
  return {
    index: hoverIndex.value,
    date: row.date,
    value: row.value,
    x: x(hoverIndex.value),
    y: y(row.value),
  }
})

function onChartMove(event: MouseEvent) {
  const svg = event.currentTarget as SVGSVGElement
  const rect = svg.getBoundingClientRect()
  const rows = windowRows.value
  if (!rows.length || rect.width === 0) return
  const px = ((event.clientX - rect.left) / rect.width) * W
  const plotWidth = W - PAD.left - PAD.right
  const raw = (px - PAD.left) / plotWidth
  const index = Math.round(raw * (rows.length - 1))
  if (index < 0 || index >= rows.length) {
    hoverIndex.value = null
    return
  }
  const row = rows[index]
  hoverIndex.value = row.value == null ? null : index
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
// 2026-08-14 红队 F6：与 TreasuryComparisonCard 同因——股票切换时
// 组件实例被复用且此前无 watch(stockCode)，历史分位图持续显示旧股票；
// 现清空并重拉。
watch(() => props.stockCode, () => {
  data.value = null
  fetchData()
})
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
      本卡为实时计算，与筛选使用的已发布统计域可能存在时差；股息率与国债比较已合并进“股息率-国债10年利差”序列。
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
            :stroke="line.color" stroke-width="1.4"
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
          <g v-if="hoverPoint" class="stat-crosshair">
            <line :x1="hoverPoint.x" :x2="hoverPoint.x" :y1="PAD.top" :y2="H - PAD.bottom" stroke="#365944" stroke-width="0.7" stroke-dasharray="2,3" />
            <circle :cx="hoverPoint.x" :cy="hoverPoint.y" r="4" fill="#57966d" stroke="#fff" stroke-width="1.4" />
          </g>
        </svg>
        <div v-if="hoverPoint" class="stat-hover-tip" :style="{ left: `${hoverPoint.x / W * 100}%`, top: `${hoverPoint.y / H * 100}%` }">
          <b>{{ hoverPoint.date }}</b>
          <span>{{ hoverPoint.value.toFixed(2) }}</span>
        </div>
        <div class="stat-legend">
          <span><i class="dot series" />序列</span>
          <span v-for="line in overlayLines" :key="line.label">
            <i class="dot band" :style="{ background: line.color }" />{{ line.label }}
          </span>
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
.stat-chart-frame {
  position: relative;
}
.stat-chart-frame svg {
  display: block;
  width: 100%;
  height: auto;
}
.stat-hover-tip {
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
.stat-hover-tip b,
.stat-hover-tip span {
  display: block;
}
.stat-hover-tip span {
  margin-top: 2px;
  font-weight: 700;
}
.stat-crosshair {
  pointer-events: none;
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
