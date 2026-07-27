<script setup lang="ts">
import { NTag, NSelect, NButton, NSpace } from 'naive-ui'
import { isRuleNode, generateRuleId } from '../types/screening.ts'
import type { ScreeningRuleNode } from '../types/screening.ts'
import RuleConditionRow from './RuleConditionRow.vue'

// Self-import for recursive rendering in the template
import ScreeningRuleEditor from './ScreeningRuleEditor.vue'

const props = defineProps<{
  node: ScreeningRuleNode
  depth: number
  maxDepth: number
  maxConditions: number
  isRoot: boolean
  indicatorOptions: Array<{ label: string; value: string }>
  opOptions: Array<{ label: string; value: string }>
}>()

const emit = defineEmits<{
  delete: []
  warn: [message: string]
}>()

const logicOptions = [
  { label: '且 (AND)', value: 'AND' },
  { label: '或 (OR)', value: 'OR' },
]

function getDepth(n: ScreeningRuleNode, current: number = 1): number {
  let max = current
  for (const r of n.rules) {
    if (isRuleNode(r)) {
      const d = getDepth(r, current + 1)
      if (d > max) max = d
    }
  }
  return max
}

function addCondition(): void {
  if (props.node.rules.length >= props.maxConditions) {
    emit('warn', '最多20个条件')
    return
  }
  props.node.rules.push({ id: generateRuleId(), field: 'pe_ttm', op: '>', value: 0 })
}

function addGroup(): void {
  if (getDepth(props.node) >= props.maxDepth) {
    emit('warn', '逻辑嵌套最多3层')
    return
  }
  props.node.rules.push({ id: generateRuleId(), logic: 'AND', rules: [] })
}

function removeAt(idx: number): void {
  props.node.rules.splice(idx, 1)
}

function updateField(idx: number, field: string): void {
  const item = props.node.rules[idx]
  if (item && !isRuleNode(item)) {
    item.field = field
  }
}

function updateOp(idx: number, op: string): void {
  const item = props.node.rules[idx]
  if (item && !isRuleNode(item)) {
    item.op = op
  }
}

function updateValue(idx: number, value: number): void {
  const item = props.node.rules[idx]
  if (item && !isRuleNode(item)) {
    item.value = value
  }
}
</script>

<template>
  <div>
    <div v-if="isRoot" style="margin-bottom: 8px">
      <n-select
        :value="node.logic"
        :options="logicOptions"
        size="small"
        style="width: 150px"
        @update:value="node.logic = $event"
      />
    </div>

    <div
      v-for="(item, idx) in node.rules"
      :key="item.id"
      style="padding-left: 16px; margin-bottom: 4px"
    >
      <template v-if="isRuleNode(item)">
        <div style="display: flex; align-items: center; gap: 8px">
          <n-tag size="small" type="info">{{ item.logic }}</n-tag>
          <n-select
            :value="item.logic"
            :options="logicOptions"
            size="small"
            style="width: 120px"
            @update:value="item.logic = $event"
          />
          <n-button size="tiny" quaternary type="error" @click="removeAt(idx)">
            删除组
          </n-button>
        </div>
        <div style="padding-left: 24px; margin-top: 4px; border-left: 1px dashed #eee">
          <ScreeningRuleEditor
            :node="item"
            :depth="depth + 1"
            :max-depth="maxDepth"
            :max-conditions="maxConditions"
            :is-root="false"
            :indicator-options="indicatorOptions"
            :op-options="opOptions"
            @warn="(msg: string) => emit('warn', msg)"
          />
        </div>
      </template>

      <template v-else>
        <RuleConditionRow
          :condition="item"
          :indicator-options="indicatorOptions"
          :op-options="opOptions"
          @update:field="updateField(idx, $event)"
          @update:op="updateOp(idx, $event)"
          @update:value="updateValue(idx, $event)"
          @delete="removeAt(idx)"
        />
      </template>
    </div>

    <n-space style="margin-top: 8px">
      <n-button size="tiny" @click="addCondition">+ 添加条件</n-button>
      <n-button v-if="depth < maxDepth" size="tiny" @click="addGroup">+ 添加组</n-button>
    </n-space>
  </div>
</template>
