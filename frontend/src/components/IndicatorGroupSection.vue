<script setup lang="ts">
import { NTag, NTooltip } from 'naive-ui'

/**
 * 单个指标统计卡的数据描述（展示层契约，无计算逻辑）。
 */
export interface MetricStatItem {
  readonly label: string
  readonly value: string
  readonly currentOnly?: boolean
  readonly untrusted?: boolean
  readonly tooltip?: string
}

/**
 * 一个带小标题的指标组（如“盈利”“成长”）。
 */
export interface IndicatorGroup {
  readonly name?: string
  readonly items: readonly MetricStatItem[]
}

withDefaults(
  defineProps<{
    /** 粘性目录锚点 id */
    readonly id: string
    readonly kicker: string
    readonly title: string
    readonly groups: readonly IndicatorGroup[]
    readonly cols?: number
  }>(),
  { cols: 4 },
)
</script>

<template>
  <section :id="id" class="stock-section indicator-section">
    <header class="section-heading">
      <p class="section-kicker">{{ kicker }}</p>
      <h2>{{ title }}</h2>
    </header>
    <div
      v-for="(group, groupIndex) in groups"
      :key="group.name ?? groupIndex"
      class="indicator-group"
    >
      <h3 v-if="group.name" class="group-title">{{ group.name }}</h3>
      <div class="metric-grid" :style="{ '--metric-cols': cols }">
        <article v-for="item in group.items" :key="item.label" class="metric-stat">
          <p class="metric-label">{{ item.label }}</p>
          <strong class="metric-value">{{ item.value }}</strong>
          <div class="metric-tags">
            <n-tooltip v-if="item.currentOnly" trigger="hover">
              <template #trigger>
                <n-tag size="tiny" type="warning">仅当前</n-tag>
              </template>
              {{ item.tooltip ?? '此指标依赖最新收盘价，只能用于当前时点展示，不可生成历史序列' }}
            </n-tooltip>
            <n-tag v-if="item.untrusted" size="tiny" type="error">数据不可信</n-tag>
          </div>
        </article>
      </div>
    </div>
    <slot />
  </section>
</template>

<style scoped>
.section-heading {
  margin-bottom: 18px;
}
.section-heading p {
  margin: 0;
  color: #91a097;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.13em;
}
.section-heading h2 {
  margin: 7px 0 0;
  font-size: 20px;
}
.indicator-group + .indicator-group {
  margin-top: 26px;
}
.group-title {
  margin: 0 0 12px;
  color: #627368;
  font-size: 12px;
  font-weight: 600;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(var(--metric-cols, 4), minmax(0, 1fr));
  gap: 14px;
}
.metric-stat {
  min-width: 0;
  padding: 15px 17px;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 10px rgba(48, 82, 59, 0.05);
}
.metric-label {
  overflow: hidden;
  color: #89968d;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.metric-value {
  display: block;
  overflow: hidden;
  margin: 8px 0 5px;
  color: #3e5948;
  font-size: 22px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.03em;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.metric-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  min-height: 16px;
}
@media (max-width: 1100px) {
  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 640px) {
  .metric-grid {
    grid-template-columns: 1fr;
  }
}
</style>
