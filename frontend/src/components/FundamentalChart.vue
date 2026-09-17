<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NRadioButton, NRadioGroup } from 'naive-ui'

interface ChartPoint {
  date: string
  value: number
}

const props = defineProps<{
  bars: ChartPoint[]
  line: ChartPoint[]
  barLabel: string
  lineLabel: string
  barColor?: string
  lineColor?: string
  height?: number
}>()

const WINDOW_OPTIONS = [
  { label: '1年', value: 365 },
  { label: '3年', value: 1095 },
  { label: '5年', value: 1825 },
  { label: '10年', value: 3650 },
  { label: '全部', value: -1 },
] as const

const windowDays = ref<number>(3650)
const zoomed = ref(false)

function onZoomKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') zoomed.value = false
}
watch(zoomed, (active) => {
  if (active) window.addEventListener('keydown', onZoomKeydown)
  else window.removeEventListener('keydown', onZoomKeydown)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onZoomKeydown))

const W = 760
const H = props.height ?? 280
const PAD = { top: 22, right: 64, bottom: 34, left: 64 }
const PLOT_W = W - PAD.left - PAD.right
const PLOT_H = H - PAD.top - PAD.bottom

function toMs(date: string): number {
  return Date.parse(date.slice(0, 10))
}

const barRows = computed(() =>
  props.bars
    .filter((point) => Number.isFinite(toMs(point.date)))
    .map((point) => ({ ms: toMs(point.date), date: point.date.slice(0, 10), value: point.value }))
    .sort((a, b) => a.ms - b.ms),
)
const lineRows = computed(() =>
  props.line
    .filter((point) => Number.isFinite(toMs(point.date)))
    .map((point) => ({ ms: toMs(point.date), date: point.date.slice(0, 10), value: point.value }))
    .sort((a, b) => a.ms - b.ms),
)

const allRows = computed(() => {
  const map = new Map<number, { ms: number; date: string; bar: number | null; line: number | null }>()
  for (const row of barRows.value) {
    map.set(row.ms, { ms: row.ms, date: row.date, bar: row.value, line: null })
  }
  for (const row of lineRows.value) {
    const existing = map.get(row.ms)
    if (existing) existing.line = row.value
    else map.set(row.ms, { ms: row.ms, date: row.date, bar: null, line: row.value })
  }
  return [...map.values()].sort((a, b) => a.ms - b.ms)
})

const windowRows = computed(() => {
  const rows = allRows.value
  if (rows.length === 0) return []
  if (windowDays.value < 0) return rows
  const cutoff = rows[rows.length - 1].ms - windowDays.value * 86_400_000
  return rows.filter((row) => row.ms >= cutoff)
})

/** 2026-09-10 用户要求：业绩/市值图数据点最小跨度为一年。
 *  季报（年内累计）与月度市值点太密、看不出长期规律；每年取最后一个点——
 *  利润的年内累计口径下"最后一点"即当年值，市值即年末/当年最新值。
 *  2026-09-17 修复：两条序列各自取年内最后一个非空值。此前按整行取最后
 *  一行，最新年份常因市值点（8 月末）晚于利润点（6 月末）而丢掉利润柱。 */
const yearlyRows = computed(() => {
  const byYear = new Map<number, { ms: number; date: string; bar: number | null; line: number | null }>()
  for (const row of windowRows.value) {
    const year = Number(row.date.slice(0, 4))
    if (!Number.isFinite(year)) continue
    const merged = byYear.get(year) ?? { ms: row.ms, date: row.date, bar: null, line: null }
    if (row.bar != null) merged.bar = row.bar
    if (row.line != null) merged.line = row.line
    if (row.ms >= merged.ms) {
      merged.ms = row.ms
      merged.date = row.date
    }
    byYear.set(year, merged)
  }
  return [...byYear.values()].sort((a, b) => a.ms - b.ms)
})

const hasData = computed(() => yearlyRows.value.length >= 1)

const dateRange = computed(() => {
  const rows = yearlyRows.value
  if (rows.length === 0) return { min: 0, max: 1 }
  return { min: rows[0].ms, max: Math.max(rows[rows.length - 1].ms, rows[0].ms + 300 * 86_400_000) }
})

const barValues = computed(() => yearlyRows.value.filter((row) => row.bar != null).map((row) => row.bar as number))
const lineValues = computed(() => yearlyRows.value.filter((row) => row.line != null).map((row) => row.line as number))

const barRange = computed(() => {
  const values = barValues.value
  if (values.length === 0) return { min: 0, max: 1 }
  // 利润可能为负（亏损年份）：范围必须同时容纳负值，否则柱子会被压成
  // 1px 或落到视口外不可见（2026-09-17）。
  const max = Math.max(...values, 0)
  const min = Math.min(...values, 0)
  if (min === max) return { min: min - 1, max: max + 1 }
  const span = max - min
  return { min: min - span * 0.05, max: max + span * 0.05 }
})
const lineRange = computed(() => {
  const values = lineValues.value
  if (values.length === 0) return { min: 0, max: 1 }
  const min = Math.min(...values)
  const max = Math.max(...values)
  if (min === max) return { min: min * 0.9, max: max * 1.1 }
  const span = max - min
  return { min: min - span * 0.08, max: max + span * 0.08 }
})

function xAt(ms: number): number {
  return PAD.left + ((ms - dateRange.value.min) / (dateRange.value.max - dateRange.value.min)) * PLOT_W
}
function yBar(value: number): number {
  const { min, max } = barRange.value
  return PAD.top + (1 - (value - min) / (max - min)) * PLOT_H
}
function yLine(value: number): number {
  const { min, max } = lineRange.value
  return PAD.top + (1 - (value - min) / (max - min)) * PLOT_H
}

const barRects = computed(() =>
  yearlyRows.value
    .filter((row) => row.bar != null)
    .map((row) => {
      const value = row.bar as number
      const zeroY = yBar(0)
      const valueY = yBar(value)
      return {
        x: xAt(row.ms),
        y: Math.min(zeroY, valueY),
        height: Math.max(1, Math.abs(zeroY - valueY)),
        date: row.date,
        value,
      }
    }),
)

const linePath = computed(() => {
  const points = yearlyRows.value.filter((row) => row.line != null)
  if (points.length < 2) return ''
  return points
    .map((row, index) => `${index === 0 ? 'M' : 'L'}${xAt(row.ms).toFixed(1)},${yLine(row.line as number).toFixed(1)}`)
    .join(' ')
})

const yTicksLeft = computed(() => {
  const { min, max } = barRange.value
  const ticks: Array<{ pos: number; label: string }> = []
  for (let i = 0; i <= 4; i += 1) {
    const value = min + ((max - min) * i) / 4
    ticks.push({ pos: PAD.top + (1 - i / 4) * PLOT_H, label: fmt(value) })
  }
  return ticks
})
const yTicksRight = computed(() => {
  const { min, max } = lineRange.value
  const ticks: Array<{ pos: number; label: string }> = []
  for (let i = 0; i <= 4; i += 1) {
    const value = min + ((max - min) * i) / 4
    ticks.push({ pos: PAD.top + (1 - i / 4) * PLOT_H, label: fmt(value) })
  }
  return ticks
})

const xTicks = computed(() => {
  const rows = yearlyRows.value
  if (rows.length === 0) return []
  const { min, max } = dateRange.value
  const fractions = rows.length >= 5 ? [0, 0.25, 0.5, 0.75, 1] : [0, 0.5, 1]
  return fractions.map((fraction) => {
    const target = min + (max - min) * fraction
    const nearest = nearestRow(target)
    return { x: xAt(nearest.ms), text: nearest.date.slice(0, 4) }
  })
})

function nearestRow(target: number) {
  const rows = yearlyRows.value
  let best = rows[0]
  let bestDelta = Number.POSITIVE_INFINITY
  for (const row of rows) {
    const delta = Math.abs(row.ms - target)
    if (delta < bestDelta) {
      bestDelta = delta
      best = row
    }
  }
  return best
}

const hoverIndex = ref<number | null>(null)
const hoverRow = computed(() => (hoverIndex.value == null ? null : yearlyRows.value[hoverIndex.value]))

function onChartMove(event: MouseEvent) {
  const overlay = event.currentTarget as SVGRectElement
  const rect = overlay.getBoundingClientRect()
  if (yearlyRows.value.length === 0 || rect.width === 0) return
  const raw = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
  const target = dateRange.value.min + raw * (dateRange.value.max - dateRange.value.min)
  hoverIndex.value = yearlyRows.value.indexOf(nearestRow(target))
}

function fmt(value: number): string {
  if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(1)}万亿`
  if (Math.abs(value) >= 100) return `${value.toFixed(0)}`
  return value.toFixed(1)
}
</script>

<template>
  <Teleport to="body" :disabled="!zoomed">
    <div v-if="zoomed" class="fund-backdrop" aria-hidden="true" @click="zoomed = false" />
    <div class="fundamental" :class="{ zoomed }">
      <button v-if="zoomed" class="fund-close" type="button" aria-label="收起图表" @click="zoomed = false">✕ 收起</button>
      <div class="fund-head">
        <div>
          <b class="fund-title">{{ barLabel }} × {{ lineLabel }}</b>
          <span class="fund-unit">单位：亿元</span>
        </div>
        <div class="fund-controls">
          <n-radio-group v-model:value="windowDays" size="small" aria-label="选择时间长度">
            <n-radio-button v-for="option in WINDOW_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </n-radio-button>
          </n-radio-group>
          <!-- 放大态退出统一由固定「✕ 收起」/Esc/背板承担，头部不重复 -->
          <NButton v-if="!zoomed" size="tiny" quaternary @click="zoomed = true">放大</NButton>
        </div>
      </div>

    <div v-if="hasData" class="fund-frame">
      <div v-if="hoverRow" class="fund-readout">
        <b>{{ hoverRow.date }}</b>
        <span v-if="hoverRow.bar != null">{{ barLabel }} {{ hoverRow.bar.toFixed(1) }} 亿</span>
        <span v-if="hoverRow.line != null">{{ lineLabel }} {{ hoverRow.line.toFixed(1) }} 亿</span>
      </div>
      <svg :viewBox="`0 0 ${W} ${H}`" class="fund-svg" role="img" :aria-label="`${barLabel}与${lineLabel}叠加图`">
        <g v-for="tick in yTicksLeft" :key="`l-${tick.pos}`" stroke="#edf2ee" stroke-width="1">
          <line :x1="PAD.left" :x2="W - PAD.right" :y1="tick.pos" :y2="tick.pos" />
        </g>
        <g v-for="tick in yTicksLeft" :key="`lt-${tick.pos}`" fill="#85928a" font-size="9">
          <text :x="PAD.left - 6" :y="tick.pos + 3" text-anchor="end">{{ tick.label }}</text>
        </g>
        <g v-for="tick in yTicksRight" :key="`rt-${tick.pos}`" fill="#85928a" font-size="9">
          <text :x="W - PAD.right + 6" :y="tick.pos + 3">{{ tick.label }}</text>
        </g>

        <rect
          v-for="(rect, index) in barRects"
          :key="rect.date"
          :x="rect.x - Math.max(2, PLOT_W / Math.max(barRects.length, 1) / 3)"
          :width="Math.max(3, (PLOT_W / Math.max(barRects.length, 1)) * 0.6)"
          :y="rect.y"
          :height="rect.height"
          fill="#e3b078"
          :opacity="index === barRects.length - 1 ? 1 : 0.78"
        />
        <path :d="linePath" fill="none" stroke="#4f8fc9" stroke-width="2" stroke-linejoin="round" />

        <line :x1="PAD.left" :x2="W - PAD.right" :y1="H - PAD.bottom" :y2="H - PAD.bottom" stroke="#d9e2da" />
        <g v-for="tick in xTicks" :key="tick.text" fill="#85928a" font-size="9">
          <text :x="tick.x" :y="H - 10" text-anchor="middle">{{ tick.text }}</text>
        </g>

        <rect
          :x="PAD.left" :y="PAD.top" :width="PLOT_W" :height="PLOT_H"
          fill="transparent"
          @mousemove="onChartMove"
          @mouseleave="hoverIndex = null"
        />
        <g v-if="hoverRow" class="fund-crosshair">
          <line
            :x1="xAt(hoverRow.ms)" :x2="xAt(hoverRow.ms)"
            :y1="PAD.top" :y2="H - PAD.bottom"
            stroke="#365944" stroke-width="0.7" stroke-dasharray="2,3"
          />
          <circle
            v-if="hoverRow.line != null"
            :cx="xAt(hoverRow.ms)" :cy="yLine(hoverRow.line)" r="4" fill="#4f8fc9" stroke="#fff" stroke-width="1.4"
          />
          <circle
            v-if="hoverRow.bar != null"
            :cx="xAt(hoverRow.ms)" :cy="yBar(hoverRow.bar)" r="3.4" fill="#e3b078" stroke="#fff" stroke-width="1.4"
          />
        </g>
      </svg>

      <div class="fund-legend">
        <span><i class="swatch bar" />{{ barLabel }}（左轴，亿元）</span>
        <span><i class="swatch line" />{{ lineLabel }}（右轴，亿元）</span>
      </div>
    </div>
    <div v-else class="fund-empty">暂无基本面数据</div>
    </div>
  </Teleport>
</template>

<style scoped>
.fundamental {
  width: 100%;
}
.fundamental.zoomed {
  position: fixed;
  inset: 4vh 4vw;
  /* 同 IndexValuationChart：覆盖基础类 width:100%，否则向右溢出 4vw */
  width: auto;
  box-sizing: border-box;
  z-index: 3000;
  overflow: auto;
  padding: 24px;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 18px 60px rgba(30, 50, 38, 0.35);
}
.fundamental.zoomed .fund-controls {
  /* 为右上角固定的 ✕ 收起 按钮让位 */
  padding-right: 96px;
}
.fund-backdrop {
  position: fixed;
  inset: 0;
  z-index: 2990;
  background: rgba(24, 38, 30, 0.28);
}
.fund-close {
  position: fixed;
  top: calc(4vh + 12px);
  right: calc(4vw + 14px);
  z-index: 10;
  padding: 7px 14px;
  border: 1px solid #2a4636;
  border-radius: 9px;
  background: #365944;
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(24, 38, 30, 0.3);
}
.fund-close:hover {
  background: #2a4636;
}
.fund-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.fund-title { font-size: 13px; color: var(--text-h); }
.fund-unit { margin-left: 8px; color: var(--text); font-size: 11px; }
.fund-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fund-frame { position: relative; padding: 12px; border-radius: 10px; background: #fafcf9; }
.fund-svg { display: block; width: 100%; height: auto; cursor: crosshair; }
.fund-readout {
  position: absolute; top: 18px; left: 18px; z-index: 3;
  padding: 6px 9px; border-radius: 8px; border: 1px solid #dfe8e0;
  background: rgba(255, 255, 255, 0.94); box-shadow: 0 3px 10px rgba(38, 57, 45, 0.1);
  font-size: 11px; color: var(--text-h); pointer-events: none; white-space: nowrap;
}
.fund-readout b { display: block; }
.fund-readout span { display: block; margin-top: 2px; font-weight: 700; font-variant-numeric: tabular-nums; }
.fund-crosshair { pointer-events: none; }
.fund-legend { display: flex; gap: 14px; margin-top: 8px; color: #627368; font-size: 11px; }
.fund-legend .swatch { display: inline-block; width: 10px; height: 10px; margin-right: 5px; border-radius: 3px; }
.fund-legend .bar { background: #e3b078; }
.fund-legend .line { background: #4f8fc9; }
.fund-empty { padding: 32px 0; text-align: center; color: var(--text); }
</style>
