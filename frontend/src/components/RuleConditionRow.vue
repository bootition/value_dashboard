<script setup lang="ts">
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
  'update:value': [value: number]
  'delete': []
}>()
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
    <n-input-number
      v-if="condition.op !== 'is_not_null' && condition.op !== 'is_null'"
      :value="condition.value"
      size="small"
      style="width: 150px"
      @update:value="emit('update:value', $event ?? 0)"
    />
    <n-button size="tiny" quaternary type="error" @click="emit('delete')">删除</n-button>
  </div>
</template>
