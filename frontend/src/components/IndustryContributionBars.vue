<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton } from 'naive-ui'
import type { IndustryContribution } from '../types/etf-strategy.ts'

const props = withDefaults(
  defineProps<{ items: IndustryContribution[]; mode?: 'yoy' | 'qoq' }>(),
  { mode: 'yoy' },
)
const zoomed = ref(false)

const periodLabel = computed(() =>
  props.mode === 'qoq' ? '当前报告期 vs 上一报告期' : '当前报告期 vs 去年同期',
)

/** 2026-09-10 用户要求：按利润贡献率降序，负值沉底；负贡献反向（向左）画。 */
const sortedItems = computed(() =>
  [...props.items].sort((a, b) => b.contribution_pct - a.contribution_pct),
)
const maxAbsPct = computed(() =>
  Math.max(1e-9, ...props.items.map((item) => Math.abs(item.contribution_pct))),
)

function barStyle(item: IndustryContribution): Record<string, string> {
  const half = Math.min(50, (Math.abs(item.contribution_pct) / maxAbsPct.value) * 50)
  return item.delta < 0
    ? { right: '50%', width: `${half}%` }
    : { left: '50%', width: `${half}%` }
}

function onZoomKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') zoomed.value = false
}
watch(zoomed, (active) => {
  if (active) window.addEventListener('keydown', onZoomKeydown)
  else window.removeEventListener('keydown', onZoomKeydown)
})
onBeforeUnmount(() => window.removeEventListener('keydown', onZoomKeydown))
</script>

<template>
  <Teleport to="body" :disabled="!zoomed">
    <div v-if="zoomed" class="ic-backdrop" aria-hidden="true" @click="zoomed = false" />
    <div class="industry-contribution" :class="{ zoomed }">
      <button v-if="zoomed" class="ic-close" type="button" aria-label="收起图表" @click="zoomed = false">✕ 收起</button>
      <div class="ic-head">
        <div>
          <b>行业利润增长贡献</b>
          <span class="ic-unit">{{ periodLabel }} · 亿元</span>
        </div>
        <!-- 放大态退出统一由固定「✕ 收起」/Esc/背板承担，头部不重复 -->
          <NButton v-if="!zoomed" size="tiny" quaternary @click="zoomed = true">放大</NButton>
      </div>
      <div class="ic-bars">
        <div v-for="item in sortedItems" :key="item.industry" class="ic-row">
          <span class="ic-name">{{ item.industry }}</span>
          <div class="ic-track">
            <div class="ic-axis" />
            <div
              class="ic-bar"
              :class="{ negative: item.delta < 0 }"
              :style="barStyle(item)"
            />
          </div>
          <span class="ic-value" :class="{ negative: item.delta < 0 }">
            {{ item.delta >= 0 ? '+' : '' }}{{ item.delta.toFixed(0) }} 亿
            · {{ item.contribution_pct >= 0 ? '+' : '' }}{{ item.contribution_pct.toFixed(1) }}%
          </span>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.industry-contribution {
  padding: 12px;
  border-radius: 10px;
  background: #fafcf9;
}
.industry-contribution.zoomed {
  position: fixed;
  inset: 8vh 10vw;
  /* 防御性 width:auto：避免基础类宽度导致 inset right 失效向右溢出 */
  width: auto;
  box-sizing: border-box;
  z-index: 3000;
  overflow: auto;
  padding: 24px;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 18px 60px rgba(30, 50, 38, 0.35);
}
.industry-contribution.zoomed .ic-head {
  /* 为右上角固定的 ✕ 收起 按钮让位 */
  padding-right: 96px;
}
.ic-backdrop {
  position: fixed;
  inset: 0;
  z-index: 2990;
  background: rgba(24, 38, 30, 0.28);
}
.ic-close {
  position: fixed;
  top: calc(8vh + 12px);
  right: calc(10vw + 14px);
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
.ic-close:hover {
  background: #2a4636;
}
.ic-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.ic-head b { font-size: 13px; color: var(--text-h); }
.ic-unit { margin-left: 8px; color: var(--text); font-size: 11px; }
.ic-bars { display: flex; flex-direction: column; gap: 7px; }
.ic-row { display: grid; grid-template-columns: 150px 1fr 190px; align-items: center; gap: 10px; }
.ic-name { color: var(--text-h); font-size: 11px; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ic-track { position: relative; height: 12px; border-radius: 6px; background: #eef4ef; overflow: hidden; }
.ic-axis { position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: #c6d2c9; }
.ic-bar { position: absolute; top: 0; height: 100%; background: #6fae87; }
.ic-bar.negative { background: #d98a8a; }
.ic-value { color: var(--text); font-size: 11px; font-variant-numeric: tabular-nums; }
.ic-value.negative { color: #b05c5c; }
@media (max-width: 760px) {
  .ic-row { grid-template-columns: 110px 1fr; }
  .ic-value { grid-column: 2; }
}
</style>
