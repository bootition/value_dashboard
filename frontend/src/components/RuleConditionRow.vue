<script setup lang="ts">
import { computed } from 'vue'
import { NSelect, NInputNumber } from 'naive-ui'
import type { ScreeningRuleCondition } from '../types/screening.ts'
import { fieldFormat } from '../utils/screening-format.ts'

const props = defineProps<{
  condition: ScreeningRuleCondition
  ordinal: number
  indicatorOptions: Array<{ label: string; value: string }>
  opOptions: Array<{ label: string; value: string }>
}>()

const emit = defineEmits<{
  'update:field': [value: string]
  'update:op': [value: string]
  'update:value': [value: number | [number, number]]
  'update:rightField': [value: string | undefined]
  'delete': []
}>()

const compareByField = computed(() => Boolean(props.condition.right_field))
const hasValue = computed(() => !['is_not_null', 'is_null'].includes(props.condition.op))

// reports/79 三-1: 百分比字段条件值按"百分数"输入与显示。
// 2026-08-14 红队 F3 修复：区分两种存储口径——
//   pct（小数存储）：输入 2 = 2%，存 0.02；显示 0.0529 → 5.29%
//   percent（百分数存储，ttm/利差/换手率）：输入 2 = 2%，原样存 2；
//     显示 5.29 → 5.29%（不再 ×100）
// 历史统计分位字段（0-100）不受影响。单位来源：
//   /api/screening/indicators 下发的 unit 元数据（单一来源）→
//   screening-format.ts 静态集合（回退）。
const fieldFmt = computed(() => fieldFormat(props.condition.field))
const isPct = computed(() => fieldFmt.value === 'pct')
const isPercent = computed(() => fieldFmt.value === 'percent')
const showPctHint = computed(() => isPct.value || isPercent.value)

function toRaw(value: number): number {
  return isPct.value ? value / 100 : value
}

function toDisplay(value: number): number {
  // 2026-08-14 红队 P3：toFixed(4)→(6)，避免反复回显的精度漂移
  return isPct.value ? Number((value * 100).toFixed(6)) : value
}

const numericValue = computed(() =>
  typeof props.condition.value === 'number' ? toDisplay(props.condition.value) : null,
)

const rangeValues = computed((): [number, number] => {
  const current = Array.isArray(props.condition.value) ? props.condition.value : [0, 0]
  return [toDisplay(current[0]), toDisplay(current[1])]
})

function updateRange(index: number, value: number): void {
  const current = Array.isArray(props.condition.value) ? props.condition.value : [0, 0]
  const next: [number, number] = [current[0], current[1]]
  next[index] = toRaw(value)
  emit('update:value', next)
}

function toggleFieldComparison(): void {
  emit('update:rightField', compareByField.value ? undefined : props.indicatorOptions[0]?.value)
}
</script>

<template>
  <article class="condition-line">
    <div class="condition-index" aria-hidden="true">{{ String(ordinal).padStart(2, '0') }}</div>
    <label class="condition-control condition-control--field">
      <span>观察指标</span>
      <n-select
        :value="condition.field"
        :options="indicatorOptions"
        size="small"
        filterable
        aria-label="观察指标"
        @update:value="emit('update:field', $event)"
      />
    </label>
    <label class="condition-control condition-control--operator">
      <span>判断关系</span>
      <n-select
        :value="condition.op"
        :options="opOptions"
        size="small"
        aria-label="判断关系"
        @update:value="emit('update:op', $event)"
      />
    </label>
    <div v-if="condition.op === 'between'" class="condition-control condition-control--value condition-range">
      <span>取值范围{{ showPctHint ? '（百分数，如 2 = 2%）' : '' }}</span>
      <div>
        <n-input-number :value="rangeValues[0]" size="small" aria-label="区间下限" @update:value="updateRange(0, $event ?? 0)" />
        <i v-if="showPctHint">%</i>
        <i v-else>至</i>
        <n-input-number :value="rangeValues[1]" size="small" aria-label="区间上限" @update:value="updateRange(1, $event ?? 0)" />
        <i v-if="showPctHint">%</i>
      </div>
    </div>
    <label v-else-if="compareByField && hasValue" class="condition-control condition-control--value">
      <span>比较指标</span>
      <n-select :value="condition.right_field" :options="indicatorOptions" size="small" filterable aria-label="比较指标" @update:value="emit('update:rightField', $event)" />
    </label>
    <label v-else-if="hasValue" class="condition-control condition-control--value">
      <span>目标值{{ showPctHint ? '（百分数，如 2 = 2%）' : '' }}</span>
      <div class="condition-value-row">
        <n-input-number :value="numericValue" size="small" aria-label="目标值" @update:value="emit('update:value', toRaw($event ?? 0))" />
        <i v-if="showPctHint" class="value-unit">%</i>
      </div>
    </label>
    <div v-else class="condition-control condition-control--value condition-no-value">
      <span>目标值</span>
      <b>无需填写</b>
    </div>
    <div class="condition-tools">
      <button v-if="hasValue && condition.op !== 'between'" type="button" @click="toggleFieldComparison">
        {{ compareByField ? '改用数值' : '与指标比较' }}
      </button>
      <button type="button" class="delete-condition" aria-label="删除条件" @click="emit('delete')">删除</button>
    </div>
  </article>
</template>

<style scoped>
.condition-line {
  display: grid;
  grid-template-columns: 42px minmax(190px, 1.6fr) minmax(120px, .72fr) minmax(170px, 1fr) 92px;
  align-items: stretch;
  min-height: 74px;
  margin: 0 18px;
  border: 1px solid #d8ddd9;
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}

.condition-index {
  display: grid;
  place-items: center;
  border-right: 1px solid #d8ddd9;
  background: #f4f5f1;
  color: #7c847e;
  font-size: 10px;
}

.condition-control {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  min-width: 0;
  padding: 10px 12px;
  border-right: 1px solid #e1e5e1;
}

.condition-control > span {
  color: #7b847d;
  font-size: 8px;
  font-weight: 700;
  letter-spacing: .12em;
}

.condition-control :deep(.n-base-selection),
.condition-control :deep(.n-input) {
  border-radius: 6px;
}

.condition-range > div { display: flex; align-items: center; gap: 7px; }
.condition-range i { color: #7c847e; font-size: 10px; font-style: normal; }
.condition-no-value b { color: #8b918d; font-size: 11px; font-weight: 500; }

.condition-value-row { display: flex; align-items: center; gap: 7px; }
.value-unit { color: #7c847e; font-size: 11px; font-style: normal; font-weight: 600; }

.condition-tools {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 7px;
  padding: 8px 10px;
}

.condition-tools button {
  padding: 0;
  border: 0;
  border-bottom: 1px solid transparent;
  background: transparent;
  color: #5d7263;
  cursor: pointer;
  font-size: 9px;
  text-align: left;
}

.condition-tools button:hover { border-bottom-color: currentColor; }
.condition-tools .delete-condition { color: #9b5a56; }

@media (max-width: 900px) {
  .condition-line { grid-template-columns: 36px 1fr 1fr; }
  .condition-index { grid-row: 1 / 3; }
  .condition-control--value { grid-column: 2 / 4; border-top: 1px solid #e1e5e1; }
  .condition-tools { grid-column: 2 / 4; flex-direction: row; justify-content: flex-end; border-top: 1px solid #e1e5e1; }
}

@media (max-width: 620px) {
  .condition-line { grid-template-columns: 32px 1fr; margin: 0 8px; }
  .condition-index { grid-row: 1 / 5; }
  .condition-control { grid-column: 2; border-top: 1px solid #e1e5e1; }
  .condition-control--field { border-top: 0; }
  .condition-control--value, .condition-tools { grid-column: 2; }
}
</style>
