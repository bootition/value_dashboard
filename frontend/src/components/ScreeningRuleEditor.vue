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

function updateValue(idx: number, value: number | [number, number]): void {
  const item = props.node.rules[idx]
  if (item && !isRuleNode(item)) {
    item.value = value
  }
}

function updateRightField(idx: number, value: string | undefined): void {
  const item = props.node.rules[idx]
  if (item && !isRuleNode(item)) {
    item.right_field = value
  }
}
</script>

<template>
  <div>
    <div v-if="isRoot" class="rule-logic-row">
      <n-select
        :value="node.logic"
        :options="logicOptions"
        size="small"
        class="rule-logic-select"
        @update:value="node.logic = $event"
      />
    </div>

    <div
      v-for="(item, idx) in node.rules"
      :key="item.id"
      class="rule-editor-item"
    >
      <template v-if="isRuleNode(item)">
        <div class="rule-group-heading">
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
          <div class="rule-group-body">
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
          @update:right-field="updateRightField(idx, $event)"
          @delete="removeAt(idx)"
        />
      </template>
    </div>

    <n-space class="rule-editor-actions">
      <n-button size="tiny" @click="addCondition">+ 添加条件</n-button>
      <n-button v-if="depth < maxDepth" size="tiny" @click="addGroup">+ 添加组</n-button>
    </n-space>
  </div>
</template>

<style scoped>
.rule-logic-row { margin-bottom: 10px; }.rule-logic-select { width: 150px; }.rule-editor-item { margin-bottom: 4px; }.rule-group-heading { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-top: 1px solid #eff2f0; background: #fafcf9; }.rule-group-body { margin-top: 4px; padding-left: 18px; border-left: 1px dashed #d9e3db; }.rule-editor-actions { margin-top: 10px; }
</style>
