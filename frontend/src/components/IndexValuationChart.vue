<script setup lang="ts">
import { computed } from 'vue'
import type { IndexBands, MetricPoint } from '../types/index-dashboard.ts'

const props = withDefaults(
  defineProps<{
    points: MetricPoint[]
    label: string
    bands?: IndexBands | null
    color?: string
    height?: number
  }>(),
  { bands: null, color: '#70a986', height: 240 },
)

const W = 720
const PAD = { top: 14, right: 18, bottom: 28, left: 56 }
const innerW = W - PAD.left - PAD.right
const innerH = props.height - PAD.top - PAD.bottom

const valid = computed(() => props.points.filter((p) => p.value != null && p.value === p.value))
const hasData = computed(() => valid.value.length >= 2)

const domain = computed(() => {
  const values = valid.value.map((p) => p.value)
  let min = Math.min(...values)
  let max = Math.max(...values)
  for (const key of ['p20', 'p50', 'p80'] as const) {
    const band = props.bands?.[key]
    if (band != null && Number.isFinite(band)) {
      min = Math.min(min, band)
      max = Math.max(max, band)
    }
  }
  if (min === max) {
    min -= 1
    max += 1
  }
  const pad = (max - min) * 0.08
  return { min: min - pad, max: max + pad }
})

const scale = computed(() => {
  const n = valid.value.length
  return {
    x: (i: number): number => PAD.left + (n <= 1 ? 0 : (i / (n - 1)) * innerW),
    y: (v: number): number =>
      PAD.top + (1 - (v - domain.value.min) / (domain.value.max - domain.value.min)) * innerH,
  }
})

const linePath = computed(() => {
  if (valid.value.length < 2) return ''
  const points = valid.value
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${scale.value.x(i).toFixed(1)},${scale.value.y(p.value).toFixed(1)}`)
    .join(' ')
  return points
})

const lastPoint = computed(() => {
  if (valid.value.length === 0) return null
  const p = valid.value[valid.value.length - 1]
  return { x: scale.value.x(valid.value.length - 1), y: scale.value.y(p.value), value: p.value }
})

const bandLines = computed(() => {
  if (!props.bands) return []
  return (['p20', 'p50', '80'] as const).map((key) => {
    const value = props.bands?.[key === '80' ? 'p80' : key] ?? null
    if (value == null || value < domain.value.min || value > domain.value.max) return null
    return { key: key === '80' ? 'p80' : key, label: key === '80' ? '80%分位' : `${key === 'p20' ? '20' : '50'}%分位`, value, y: scale.value.y(value) }
  }).filter((x): x is { key: string; label: string; value: number; y: number } => x !== null)
})

const xLabels = computed(() => {
  if (valid.value.length < 2) return []
  const idx = [0, Math.floor((valid.value.length - 1) / 2), valid.value.length - 1]
  return idx.map((i) => ({
    text: valid.value[i].trade_date.slice(0, 10),
    x: scale.value.x(i),
  }))
})

function fmt(value: number): string {
  if (value >= 100) return value.toFixed(0)
  if (value >= 10) return value.toFixed(1)
  return value.toFixed(2)
}
</script>

<template>
  <div class="ivc">
    <div class="ivc-head">
      <span class="ivc-title">{{ label }}</span>
      <span v-if="lastPoint" class="ivc-current">当前 {{ fmt(lastPoint.value) }}</span>
    </div>
    <svg
      v-if="hasData"
      :viewBox="`0 0 ${W} ${height}`"
      class="ivc-svg"
      role="img"
      :aria-label="`${label} 历史分位图`"
    >
      <g class="ivc-band">
        <line
          v-for="band in bandLines"
          :key="band.key"
          x1="56"
          :x2="W - 18"
          :y1="band.y"
          :y2="band.y"
          :stroke-dasharray="band.key === 'p50' ? '2 0' : '5 4'"
          :stroke-width="band.key === 'p50' ? 1.4 : 1"
        />
        <text
          v-for="band in bandLines"
          :key="`t-${band.key}`"
          x="6"
          :y="band.y + 4"
          class="ivc-band-text"
        >{{ band.label }}</text>
      </g>
      <path :d="linePath" fill="none" :stroke="color" stroke-width="1.8" stroke-linejoin="round" />
      <circle v-if="lastPoint" :cx="lastPoint.x" :cy="lastPoint.y" r="3.2" :fill="color" />
      <g class="ivc-axis">
        <line x1="56" :x2="W - 18" :y1="height - 28" :y2="height - 28" />
        <text
          v-for="xLabel in xLabels"
          :key="xLabel.text"
          :x="xLabel.x"
          :y="height - 8"
          text-anchor="middle"
        >{{ xLabel.text }}</text>
      </g>
    </svg>
    <div v-else class="ivc-empty">暂无数据</div>
  </div>
</template>

<style scoped>
.ivc { width: 100%; }
.ivc-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
.ivc-title { font-weight: 700; color: var(--text-h); }
.ivc-current { font-size: 12px; color: var(--text); }
.ivc-svg { display: block; width: 100%; height: auto; }
.ivc-band line { stroke: var(--text); opacity: 0.55; }
.ivc-band text, .ivc-axis text { font-size: 10px; fill: var(--text); }
.ivc-empty { padding: 32px 0; text-align: center; color: var(--text); }
</style>
