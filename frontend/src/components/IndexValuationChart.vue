<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NRadioButton, NRadioGroup } from 'naive-ui'
import type { IndexBands, MetricPoint } from '../types/index-dashboard.ts'

const props = withDefaults(
  defineProps<{
    points: MetricPoint[]
    label: string
    bands?: IndexBands | null
    color?: string
    height?: number
  }>(),
  { bands: null, color: '#70a986', height: 250 },
)

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

const W = 720
const H = props.height
const PAD = { top: 18, right: 18, bottom: 32, left: 58 }
const PLOT_W = W - PAD.left - PAD.right
const PLOT_H = H - PAD.top - PAD.bottom

const valid = computed(() =>
  props.points
    .filter((p) => p.value != null && p.value === p.value && p.trade_date)
    .map((p, sourceIndex) => ({
      date: p.trade_date.slice(0, 10),
      value: p.value as number,
      sourceIndex,
      ms: Date.parse(p.trade_date.slice(0, 10)),
    }))
    .filter((p) => Number.isFinite(p.ms))
    .sort((a, b) => a.ms - b.ms),
)

const windowRows = computed(() => {
  const rows = valid.value
  if (rows.length === 0) return []
  if (windowDays.value < 0) return rows
  const cutoff = rows[rows.length - 1].ms - windowDays.value * 86_400_000
  return rows.filter((row) => row.ms >= cutoff)
})

const hasData = computed(() => windowRows.value.length >= 2)

function quantile(values: number[], q: number): number | null {
  const sorted = [...values].sort((a, b) => a - b)
  if (sorted.length === 0) return null
  if (sorted.length === 1) return sorted[0]
  const pos = q * (sorted.length - 1)
  const lower = Math.floor(pos)
  const upper = Math.min(lower + 1, sorted.length - 1)
  const weight = pos - lower
  return sorted[lower] * (1 - weight) + sorted[upper] * weight
}

const windowValues = computed(() => windowRows.value.map((row) => row.value))

const bandLines = computed(() => {
  const lines: Array<{ label: string; value: number; dash: string; color: string }> = []
  for (const [q, label, dash, color] of [
    [0.2, 'P20', '5,4', '#e8963e'],
    [0.5, 'P50', '0', '#b98a2e'],
    [0.8, 'P80', '5,4', '#4a87c4'],
  ] as const) {
    const value = quantile(windowValues.value, q)
    if (value != null) lines.push({ label, value, dash, color })
  }
  return lines
})

const valueRange = computed(() => {
  const values = windowValues.value
  if (values.length === 0) return { min: 0, max: 1 }
  let min = Math.min(...values)
  let max = Math.max(...values)
  for (const line of bandLines.value) {
    min = Math.min(min, line.value)
    max = Math.max(max, line.value)
  }
  if (min === max) {
    min -= 1
    max += 1
  }
  const span = max - min
  return { min: min - span * 0.08, max: max + span * 0.08 }
})

const dateRange = computed(() => {
  const rows = windowRows.value
  if (rows.length === 0) return { min: 0, max: 1 }
  const first = rows[0].ms
  const last = rows[rows.length - 1].ms
  return { min: first, max: Math.max(last, first + 86_400_000) }
})

function xAt(ms: number): number {
  const t = (ms - dateRange.value.min) / (dateRange.value.max - dateRange.value.min)
  return PAD.left + t * PLOT_W
}

function yAt(value: number): number {
  const t = (value - valueRange.value.min) / (valueRange.value.max - valueRange.value.min)
  return PAD.top + (1 - t) * PLOT_H
}

const linePath = computed(() => {
  const rows = windowRows.value
  if (rows.length < 2) return ''
  return rows
    .map((row, index) => `${index === 0 ? 'M' : 'L'}${xAt(row.ms).toFixed(1)},${yAt(row.value).toFixed(1)}`)
    .join(' ')
})

const yTicks = computed(() => {
  const { min, max } = valueRange.value
  const ticks: Array<{ pos: number; label: string }> = []
  for (let i = 0; i <= 4; i += 1) {
    const value = min + ((max - min) * i) / 4
    ticks.push({ pos: PAD.top + (1 - i / 4) * PLOT_H, label: fmtAxis(value) })
  }
  return ticks
})

const xTicks = computed(() => {
  const rows = windowRows.value
  if (rows.length === 0) return []
  const { min, max } = dateRange.value
  const fractions = rows.length >= 5 ? [0, 0.25, 0.5, 0.75, 1] : [0, 0.5, 1]
  return fractions.map((fraction) => {
    const target = min + (max - min) * fraction
    const nearest = nearestRowForMs(target)
    return { x: xAt(nearest.ms), text: nearest.date.slice(0, 7) }
  })
})

function nearestRowForMs(target: number) {
  const rows = windowRows.value
  if (rows.length === 1) return rows[0]
  let low = 0
  let high = rows.length - 1
  while (low < high) {
    const mid = (low + high) >> 1
    if (rows[mid].ms < target) low = mid + 1
    else high = mid
  }
  const candidates = [Math.max(0, low - 1), low, Math.min(rows.length - 1, low + 1)]
  let best = rows[low]
  let bestDelta = Number.POSITIVE_INFINITY
  for (const index of candidates) {
    const delta = Math.abs(rows[index].ms - target)
    if (delta < bestDelta) {
      bestDelta = delta
      best = rows[index]
    }
  }
  return best
}

const hoverIndex = ref<number | null>(null)
const hoverPoint = computed(() => {
  if (hoverIndex.value == null) return null
  const row = windowRows.value[hoverIndex.value]
  if (!row) return null
  return { date: row.date, value: row.value, x: xAt(row.ms), y: yAt(row.value) }
})

function onChartMove(event: MouseEvent) {
  const overlay = event.currentTarget as SVGRectElement
  const rect = overlay.getBoundingClientRect()
  if (windowRows.value.length === 0 || rect.width === 0) return
  const raw = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
  const target = dateRange.value.min + raw * (dateRange.value.max - dateRange.value.min)
  const nearest = nearestRowForMs(target)
  hoverIndex.value = windowRows.value.indexOf(nearest)
}

const current = computed(() => {
  const rows = windowRows.value
  return rows.length ? rows[rows.length - 1] : null
})

const currentPercentile = computed(() => {
  if (!current.value) return null
  const values = windowValues.value
  if (values.length === 0) return null
  const below = values.filter((value) => value < current.value!.value).length
  return (below / values.length) * 100
})

function fmtAxis(value: number): string {
  if (Math.abs(value) >= 100) return value.toFixed(0)
  if (Math.abs(value) >= 10) return value.toFixed(1)
  return value.toFixed(2)
}
</script>

<template>
  <Teleport to="body" :disabled="!zoomed">
    <div v-if="zoomed" class="ivc-backdrop" aria-hidden="true" @click="zoomed = false" />
    <div class="ivc" :class="{ zoomed }">
      <button v-if="zoomed" class="ivc-close" type="button" aria-label="收起图表" @click="zoomed = false">✕ 收起</button>
      <div class="ivc-head">
        <div class="ivc-title-group">
          <span class="ivc-title">{{ label }}</span>
          <span v-if="current" class="ivc-current">当前 {{ current.value.toFixed(2) }}</span>
        </div>
        <div class="ivc-controls">
          <n-radio-group v-model:value="windowDays" size="small" aria-label="选择时间长度">
            <n-radio-button v-for="option in WINDOW_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </n-radio-button>
          </n-radio-group>
          <!-- 放大态的退出统一由右上角固定「✕ 收起」/Esc/点击背板承担，
               头部不再重复放退出按钮（2026-09-10 用户要求去重）。 -->
          <NButton v-if="!zoomed" size="tiny" quaternary @click="zoomed = true">
            放大
          </NButton>
        </div>
      </div>

    <div v-if="hasData" class="ivc-frame">
      <div v-if="hoverPoint" class="ivc-readout">
        <b>{{ hoverPoint.date }}</b>
        <span>{{ label }} {{ hoverPoint.value.toFixed(2) }}</span>
      </div>
      <svg :viewBox="`0 0 ${W} ${H}`" class="ivc-svg" role="img" :aria-label="`${label} 历史分位图`">
        <g v-for="tick in yTicks" :key="tick.pos" stroke="#edf2ee" stroke-width="1">
          <line :x1="PAD.left" :x2="W - PAD.right" :y1="tick.pos" :y2="tick.pos" />
        </g>
        <g v-for="tick in yTicks" :key="`t-${tick.pos}`" fill="#85928a" font-size="9">
          <text :x="PAD.left - 6" :y="tick.pos + 3" text-anchor="end">{{ tick.label }}</text>
        </g>

        <line
          v-for="line in bandLines"
          :key="line.label"
          :x1="PAD.left" :x2="W - PAD.right"
          :y1="yAt(line.value)" :y2="yAt(line.value)"
          :stroke="line.color" stroke-width="1.3" :stroke-dasharray="line.dash"
        />
        <text
          v-for="line in bandLines"
          :key="`label-${line.label}`"
          :x="W - PAD.right" :y="yAt(line.value) - 4"
          text-anchor="end" fill="#85928a" font-size="9"
        >{{ line.label }}</text>

        <path :d="linePath" fill="none" :stroke="color" stroke-width="2" stroke-linejoin="round" />

        <line
          :x1="PAD.left" :x2="W - PAD.right"
          :y1="H - PAD.bottom" :y2="H - PAD.bottom"
          stroke="#d9e2da" stroke-width="1"
        />
        <g v-for="tick in xTicks" :key="tick.text" fill="#85928a" font-size="9">
          <text :x="tick.x" :y="H - 10" text-anchor="middle">{{ tick.text }}</text>
        </g>

        <rect
          :x="PAD.left" :y="PAD.top"
          :width="PLOT_W" :height="PLOT_H"
          fill="transparent"
          @mousemove="onChartMove"
          @mouseleave="hoverIndex = null"
        />
        <g v-if="hoverPoint" class="ivc-crosshair">
          <line
            :x1="hoverPoint.x" :x2="hoverPoint.x"
            :y1="PAD.top" :y2="H - PAD.bottom"
            stroke="#365944" stroke-width="0.7" stroke-dasharray="2,3"
          />
          <line
            :x1="PAD.left" :x2="W - PAD.right"
            :y1="hoverPoint.y" :y2="hoverPoint.y"
            stroke="#365944" stroke-width="0.7" stroke-dasharray="2,3"
          />
          <circle :cx="hoverPoint.x" :cy="hoverPoint.y" r="4" :fill="color" stroke="#fff" stroke-width="1.4" />
        </g>
      </svg>

      <div class="ivc-legend">
        <span><i class="dot series" :style="{ background: color }" />{{ label }}</span>
        <span v-for="line in bandLines" :key="line.label">
          <i class="dot band" :style="{ background: line.color }" />{{ line.label }}
        </span>
      </div>

      <p class="ivc-foot">
        样本 {{ windowValues.length }} ·
        区间 {{ windowRows[0].date }} ~ {{ windowRows[windowRows.length - 1].date }}
        <template v-if="currentPercentile != null"> · 当前分位 {{ currentPercentile.toFixed(0) }}%</template>
      </p>
    </div>

    <div v-else class="ivc-empty">该时间长度下暂无足够数据</div>
    </div>
  </Teleport>
</template>

<style scoped>
.ivc {
  width: 100%;
}
.ivc.zoomed {
  position: fixed;
  inset: 4vh 4vw;
  /* 基础类 .ivc 有 width:100%；fixed 下 left+right+width 同时存在时
     right 会被忽略，导致卡片向右溢出 4vw（2026-09-10 用户实锤）。
     必须显式 width:auto 让 inset 的 right 生效。 */
  width: auto;
  box-sizing: border-box;
  z-index: 3000;
  overflow: auto;
  padding: 24px;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 18px 60px rgba(30, 50, 38, 0.35);
}
.ivc.zoomed .ivc-controls {
  /* 为右上角固定的 ✕ 收起 按钮让位，避免遮挡时间选择器 */
  padding-right: 96px;
}
.ivc.zoomed .ivc-frame {
  padding: 14px 18px 18px;
}
.ivc-backdrop {
  position: fixed;
  inset: 0;
  z-index: 2990;
  background: rgba(24, 38, 30, 0.28);
}
.ivc-close {
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
.ivc-close:hover {
  background: #2a4636;
}
.ivc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.ivc-title-group {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.ivc-title {
  font-weight: 700;
  color: var(--text-h);
}
.ivc-current {
  font-size: 12px;
  color: var(--text);
}
.ivc-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.ivc-frame {
  position: relative;
  padding: 10px 12px 12px;
  border-radius: 10px;
  background: #fafcf9;
}
.ivc-svg {
  display: block;
  width: 100%;
  height: auto;
  cursor: crosshair;
}
.ivc-readout {
  position: absolute;
  top: 16px;
  left: 16px;
  z-index: 3;
  padding: 6px 9px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.94);
  border: 1px solid #dfe8e0;
  box-shadow: 0 3px 10px rgba(38, 57, 45, 0.1);
  color: var(--text-h);
  font-size: 11px;
  pointer-events: none;
  white-space: nowrap;
}
.ivc-readout b {
  display: block;
  font-variant-numeric: tabular-nums;
}
.ivc-readout span {
  display: block;
  margin-top: 2px;
  color: #365944;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.ivc-crosshair {
  pointer-events: none;
}
.ivc-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 8px;
  color: #627368;
  font-size: 11px;
}
.ivc-legend .dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  margin-right: 5px;
  border-radius: 50%;
}
.ivc-foot {
  margin: 6px 0 0;
  color: #91a097;
  font-size: 10px;
}
.ivc-empty {
  padding: 32px 0;
  text-align: center;
  color: var(--text);
}
@media (max-width: 900px) {
  .ivc-head {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
