<script setup lang="ts">
import { computed } from 'vue'
import { NSelect, NInputNumber, NButton } from 'naive-ui'
import type { ScreeningRuleCondition } from '../types/screening.ts'

const props = defineProps<{
  condition: ScreeningRuleCondition
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

function updateRange(index: number, value: number): void {
  const current = Array.isArray(props.condition.value) ? props.condition.value : [0, 0]
  const next: [number, number] = [current[0], current[1]]
  next[index] = value
  emit('update:value', next)
}

function toggleFieldComparison(): void {
  emit('update:rightField', compareByField.value ? undefined : props.indicatorOptions[0]?.value)
}

const numericValue = computed(() =>
  typeof props.condition.value === 'number' ? props.condition.value : null,
)
</script>

<template>
  <div style="display: flex; align-items: center; gap: 8px">
    <n-select
      :value="condition.field"
      :options="indicatorOptions"
      size="small"
      style="width: 180px"
      filterable
      @update:value="emit('update:field', $event)"
    />
    <n-select
      :value="condition.op"
      :options="opOptions"
      size="small"
      style="width: 100px"
      @update:value="emit('update:op', $event)"
    />
    <template v-if="condition.op === 'between'">
      <n-input-number
        :value="Array.isArray(condition.value) ? condition.value[0] : 0"
        size="small"
        style="width: 110px"
        @update:value="updateRange(0, $event ?? 0)"
      />
      <span>至</span>
      <n-input-number
        :value="Array.isArray(condition.value) ? condition.value[1] : 0"
        size="small"
        style="width: 110px"
        @update:value="updateRange(1, $event ?? 0)"
      />
    </template>
    <n-select
      v-else-if="compareByField && condition.op !== 'is_not_null' && condition.op !== 'is_null'"
      :value="condition.right_field"
      :options="indicatorOptions"
      size="small"
      style="width: 180px"
      filterable
      @update:value="emit('update:rightField', $event)"
    />
    <n-input-number
      v-else-if="condition.op !== 'is_not_null' && condition.op !== 'is_null'"
      :value="numericValue"
      size="small"
      style="width: 150px"
      @update:value="emit('update:value', $event ?? 0)"
    />
    <n-button
      v-if="condition.op !== 'between' && condition.op !== 'in' && condition.op !== 'is_not_null' && condition.op !== 'is_null'"
      size="tiny"
      @click="toggleFieldComparison"
    >
      {{ compareByField ? '固定值' : '字段比较' }}
    </n-button>
    <n-button size="tiny" quaternary type="error" @click="emit('delete')">删除</n-button>
  </div>
</template>
