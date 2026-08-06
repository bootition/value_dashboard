<script setup lang="ts">
import { computed } from 'vue'
import { NButton } from 'naive-ui'
import { isRuleNode, generateRuleId } from '../types/screening.ts'
import type { ScreeningRuleNode } from '../types/screening.ts'
import RuleConditionRow from './RuleConditionRow.vue'

// Self-import for recursive rendering in the template.
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

const logicCopy = computed(() => props.node.logic === 'AND'
  ? {
      title: props.isRoot ? '同时满足全部条件' : '组内条件全部成立',
      description: '每一条都通过，股票才会进入结果。',
      connector: '并且',
    }
  : {
      title: props.isRoot ? '满足任意一个条件' : '组内任一条件成立',
      description: '只要通过其中一条，股票就会进入结果。',
      connector: '或者',
    },
)

function getDepth(n: ScreeningRuleNode, current: number = 1): number {
  let max = current
  for (const rule of n.rules) {
    if (isRuleNode(rule)) max = Math.max(max, getDepth(rule, current + 1))
  }
  return max
}

function countConditions(n: ScreeningRuleNode): number {
  return n.rules.reduce(
    (count, rule) => count + (isRuleNode(rule) ? countConditions(rule) : 1),
    0,
  )
}

function addCondition(): void {
  if (countConditions(props.node) >= props.maxConditions) {
    emit('warn', `最多${props.maxConditions}个条件`)
    return
  }
  props.node.rules.push({ id: generateRuleId(), field: 'pe_ttm', op: '<', value: 15 })
}

function addGroup(): void {
  if (getDepth(props.node) >= props.maxDepth) {
    emit('warn', `逻辑嵌套最多${props.maxDepth}层`)
    return
  }
  props.node.rules.push({ id: generateRuleId(), logic: 'AND', rules: [] })
}

function removeAt(index: number): void {
  props.node.rules.splice(index, 1)
}

function updateField(index: number, field: string): void {
  const item = props.node.rules[index]
  if (item && !isRuleNode(item)) item.field = field
}

function updateOp(index: number, op: string): void {
  const item = props.node.rules[index]
  if (!item || isRuleNode(item)) return
  item.op = op
  if (op === 'between' && !Array.isArray(item.value)) item.value = [0, 0]
  if (['between', 'in', 'is_not_null', 'is_null'].includes(op)) item.right_field = undefined
}

function updateValue(index: number, value: number | [number, number]): void {
  const item = props.node.rules[index]
  if (item && !isRuleNode(item)) item.value = value
}

function updateRightField(index: number, value: string | undefined): void {
  const item = props.node.rules[index]
  if (item && !isRuleNode(item)) item.right_field = value
}
</script>

<template>
  <section class="rule-sheet" :class="{ 'rule-sheet--nested': !isRoot }">
    <header class="logic-statement">
      <div class="logic-copy">
        <span>{{ isRoot ? '匹配方式' : `条件组 · 第 ${depth} 层` }}</span>
        <strong>{{ logicCopy.title }}</strong>
        <p>{{ logicCopy.description }}</p>
      </div>
      <div class="logic-switch" role="radiogroup" :aria-label="isRoot ? '筛选匹配方式' : '条件组匹配方式'">
        <button type="button" :class="{ active: node.logic === 'AND' }" :aria-checked="node.logic === 'AND'" role="radio" @click="node.logic = 'AND'">
          全部成立
        </button>
        <button type="button" :class="{ active: node.logic === 'OR' }" :aria-checked="node.logic === 'OR'" role="radio" @click="node.logic = 'OR'">
          任一成立
        </button>
      </div>
      <button v-if="!isRoot" type="button" class="remove-group" @click="emit('delete')">删除此组</button>
    </header>

    <div v-if="node.rules.length" class="rule-sequence">
      <template v-for="(item, index) in node.rules" :key="item.id">
        <div v-if="index > 0" class="logic-connector" aria-hidden="true">
          <span>{{ logicCopy.connector }}</span>
        </div>
        <ScreeningRuleEditor
          v-if="isRuleNode(item)"
          :node="item"
          :depth="depth + 1"
          :max-depth="maxDepth"
          :max-conditions="maxConditions"
          :is-root="false"
          :indicator-options="indicatorOptions"
          :op-options="opOptions"
          @delete="removeAt(index)"
          @warn="(message: string) => emit('warn', message)"
        />
        <RuleConditionRow
          v-else
          :condition="item"
          :ordinal="index + 1"
          :indicator-options="indicatorOptions"
          :op-options="opOptions"
          @update:field="updateField(index, $event)"
          @update:op="updateOp(index, $event)"
          @update:value="updateValue(index, $event)"
          @update:right-field="updateRightField(index, $event)"
          @delete="removeAt(index)"
        />
      </template>
    </div>

    <div v-else class="rule-empty">
      <b>尚未设置条件</b>
      <span>先添加一条判断，例如“市盈率低于 15”。</span>
    </div>

    <footer class="rule-actions">
      <n-button size="small" @click="addCondition">添加条件</n-button>
      <n-button v-if="depth < maxDepth" size="small" quaternary @click="addGroup">添加条件组</n-button>
      <span>{{ countConditions(node) }} / {{ maxConditions }} 条</span>
    </footer>
  </section>
</template>

<style scoped>
.rule-sheet {
  border: 1px solid #cfd5d0;
  border-radius: 9px;
  background: #fff;
  overflow: hidden;
}

.rule-sheet--nested {
  margin: 0 18px;
  border-color: #9da8a0;
  border-radius: 9px;
  background: #f8f8f5;
}

.logic-statement {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) auto;
  align-items: center;
  min-height: 86px;
  border-bottom: 1px solid #cfd5d0;
  background: #f3f4ef;
}

.rule-sheet--nested .logic-statement {
  min-height: 72px;
  background: #eceee8;
}

.logic-copy {
  padding: 16px 20px;
}

.logic-copy > span {
  display: block;
  margin-bottom: 5px;
  color: #6f776f;
  font-size: 9px;
  font-weight: 700;
  line-height: 1.2;
  letter-spacing: .13em;
  text-transform: uppercase;
}

.logic-copy strong {
  display: block;
  color: #1b211d;
  font-size: 16px;
  letter-spacing: -.02em;
}

.logic-copy p {
  margin-top: 3px;
  color: #707871;
  font-size: 11px;
}

.logic-switch {
  display: grid;
  grid-template-columns: repeat(2, minmax(84px, 1fr));
  align-self: stretch;
  border-left: 1px solid #cfd5d0;
}

.logic-switch button {
  min-width: 94px;
  padding: 0 14px;
  border: 0;
  border-left: 1px solid #cfd5d0;
  background: transparent;
  color: #6e756f;
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  font-weight: 600;
}

.logic-switch button:first-child { border-left: 0; }
.logic-switch button:hover { background: #e4e8e1; color: #222a24; }
.logic-switch button.active { background: #243c2d; color: #f7f8f4; }

.remove-group {
  grid-column: 1 / -1;
  justify-self: end;
  margin: -24px 12px 8px 0;
  border: 0;
  border-bottom: 1px solid currentColor;
  background: transparent;
  color: #98504d;
  cursor: pointer;
  font-size: 10px;
}

.rule-sequence { padding: 18px 0; }

.logic-connector {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: 12px;
  height: 30px;
  margin: 0 18px;
  color: #68746b;
  font-size: 10px;
  font-weight: 700;
}

.logic-connector::before,
.logic-connector::after {
  content: '';
  border-top: 1px solid #dde1dd;
}

.rule-empty {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-height: 104px;
  padding: 0 20px;
  border-bottom: 1px solid #e1e4e1;
  color: #7b827d;
}

.rule-empty b { color: #353c37; font-size: 12px; }
.rule-empty span { margin-top: 4px; font-size: 11px; }

.rule-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 48px;
  padding: 0 18px;
  border-top: 1px solid #dfe3df;
}

.rule-actions > span {
  margin-left: auto;
  color: #858c87;
  font-size: 10px;
}

.rule-actions :deep(.n-button) { border-radius: 6px; }

@media (max-width: 760px) {
  .logic-statement { grid-template-columns: 1fr; }
  .logic-switch { min-height: 48px; border-top: 1px solid #cfd5d0; border-left: 0; }
  .rule-sheet--nested { margin: 0 8px; }
  .logic-connector { margin: 0 8px; }
}
</style>
