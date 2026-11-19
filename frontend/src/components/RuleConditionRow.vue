<script setup lang="ts">
import { computed, h } from 'vue'
import { NEllipsis, NInputNumber, NSelect } from 'naive-ui'
import type { VNode } from 'vue'
import type { IndicatorOption, ScreeningRuleCondition } from '../types/screening.ts'
import { fieldInputUnit } from '../utils/screening-format.ts'

const props = defineProps<{
  condition: ScreeningRuleCondition
  ordinal: number
  indicatorOptions: IndicatorOption[]
  /** 可用的排名口径（方案 C）；空数组则不显示该下拉 */
  rankModes?: Array<{ label: string; value: string }>
  /** 当前字段是否支持排名口径 */
  rankable?: boolean
  /** 当前字段的参数规格（如 CAGR 需要选年数）；空表示无参数 */
  paramSpec?: {
    readonly kind: string
    readonly key: string
    readonly label: string
    readonly options: ReadonlyArray<string | number>
  } | null
  opOptions: Array<{ label: string; value: string }>
}>()

const emit = defineEmits<{
  'update:field': [value: string]
  'update:op': [value: string]
  'update:value': [value: number | [number, number]]
  'update:rightField': [value: string | undefined]
  'update:rankMode': [value: string | undefined]
  'update:param': [key: string, value: string | number | undefined]
  'delete': []
}>()

const compareByField = computed(() => Boolean(props.condition.right_field))
const hasValue = computed(() => !['is_not_null', 'is_null'].includes(props.condition.op))

// 单位与换算统一由 screening-format.ts 提供：
// - pct（小数存储）：输入 2 = 2%，底层存 0.02，scale=0.01
// - percent（百分数存储）：输入 2 = 2%，底层原样存 2，scale=1
// - 总市值/金额类：输入 1000 = 1000亿，底层存 1e11，scale=1e8
// - 价格/倍数/年份/排名等：scale=1，仅显示单位提示
const inputUnit = computed(() => fieldInputUnit(props.condition.field))
const unitLabel = computed(() => inputUnit.value.label)
const inputHint = computed(() => inputUnit.value.hint ?? '')

function toRaw(value: number): number {
  return value * inputUnit.value.scale
}

function toDisplay(value: number): number {
  const display = value / inputUnit.value.scale
  // scale=1 保持原值；非 1 时保留 6 位，避免反复回显的精度漂移
  return inputUnit.value.scale === 1 ? display : Number(display.toFixed(6))
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

/**
 * 指标下拉的标签渲染（2026-11-16）。
 *
 * 指标名（尤其派生字段如 `roce_5y_median_market_percentile 全市场分位`）远超
 * 下拉宽度，原生 select 会直接截断且**无法查看全称**。这里用 NEllipsis 的
 * tooltip 模式：截断处仍有省略号，鼠标悬浮即弹出完整名称。
 */
function renderIndicatorLabel(option: { label?: string }): VNode {
  return h(
    NEllipsis,
    { tooltip: { placement: 'right', contentStyle: 'max-width: 520px; white-space: normal;' }, style: 'max-width: 100%' },
    { default: () => option.label ?? '' },
  )
}

/** 取第一个**叶子**选项的 value（分组选项本身没有 value）。 */
function firstIndicatorValue(): string | undefined {
  for (const option of props.indicatorOptions) {
    if ('value' in option) return option.value
    const child = option.children[0]
    if (child) return child.value
  }
  return undefined
}

/**
 * 排名口径选项（2026-11-16 方案 C）。
 *
 * 只有 `rankable` 的字段才显示这个下拉；首项「原始值」表示不带排名。
 * 取代了过去的 930 条扁平排名字段（`pe_ttm_market_rank` …）。
 */
const rankModeOptions = computed(() => [
  { label: '原始值', value: '' },
  ...(props.rankModes ?? []),
])

/**
 * 参数值的**中文展示**（2026-11-16 审查 P2）：
 * 库里存 `0p25y`（小数点转 p 避免字段名非法），直接显示会让人看不懂。
 */
function paramOptionLabel(kind: string, value: string | number): string {
  if (kind === 'cagr') return `${value} 年`
  if (kind === 'tenor') return `${String(value).replace('p', '.')} 年`
  return String(value)
}

/** 参数化字段的取值选项（reports/143 §2）：如 CAGR 的 3/5 年、国债期限 9 档。 */
const paramOptions = computed(() =>
  (props.paramSpec?.options ?? []).map((v) => ({
    label: paramOptionLabel(props.paramSpec?.kind ?? '', v),
    value: v,
  })),
)

function paramValue(): string | number | undefined {
  const spec = props.paramSpec
  if (!spec) return undefined
  const raw = (props.condition as unknown as Record<string, unknown>)[spec.key]
  return typeof raw === 'string' || typeof raw === 'number' ? raw : undefined
}

function onParamChange(value: string | number | null): void {
  const spec = props.paramSpec
  if (!spec) return
  emit('update:param', spec.key, value === null ? undefined : value)
}

async function onRankModeChange(value: string): Promise<void> {
  emit('update:rankMode', value === '' ? undefined : value)
}

function toggleFieldComparison(): void {
  emit('update:rightField', compareByField.value ? undefined : firstIndicatorValue())
}
</script>

<template>
  <article class="condition-line">
    <div class="condition-index" aria-hidden="true">{{ String(ordinal).padStart(2, '0') }}</div>
    <label class="condition-control condition-control--field">
      <span>观察指标</span>
      <n-select
        :consistent-menu-width="false"
        :value="condition.field"
        :options="indicatorOptions"
        size="small"
        filterable
        :render-label="renderIndicatorLabel"
        aria-label="观察指标"
        @update:value="emit('update:field', $event)"
      />
    </label>
    <label v-if="paramSpec" class="condition-control condition-control--param">
      <span>{{ paramSpec.label }}</span>
      <n-select
        :consistent-menu-width="false"
        :value="paramValue()"
        :options="paramOptions"
        size="small"
        :render-label="renderIndicatorLabel"
        :aria-label="paramSpec.label"
        @update:value="onParamChange"
      />
    </label>
    <label v-if="rankable" class="condition-control condition-control--rank">
      <span>排名口径</span>
      <n-select
        :consistent-menu-width="false"
        :value="condition.rank_mode ?? ''"
        :options="rankModeOptions"
        size="small"
        :render-label="renderIndicatorLabel"
        aria-label="排名口径"
        @update:value="onRankModeChange"
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
      <span>取值范围{{ inputHint ? '（' + inputHint + '）' : '' }}</span>
      <div>
        <n-input-number :value="rangeValues[0]" size="small" aria-label="区间下限" @update:value="updateRange(0, $event ?? 0)" />
        <i v-if="unitLabel" class="value-unit">{{ unitLabel }}</i>
        <i class="range-separator">至</i>
        <n-input-number :value="rangeValues[1]" size="small" aria-label="区间上限" @update:value="updateRange(1, $event ?? 0)" />
        <i v-if="unitLabel" class="value-unit">{{ unitLabel }}</i>
      </div>
    </div>
    <label v-else-if="compareByField && hasValue" class="condition-control condition-control--value">
      <span>比较指标</span>
      <n-select :consistent-menu-width="false" :value="condition.right_field" :options="indicatorOptions" size="small" filterable :render-label="renderIndicatorLabel" aria-label="比较指标" @update:value="emit('update:rightField', $event)" />
    </label>
    <label v-else-if="hasValue" class="condition-control condition-control--value">
      <span>目标值{{ inputHint ? '（' + inputHint + '）' : '' }}</span>
      <div class="condition-value-row">
        <n-input-number :value="numericValue" size="small" aria-label="目标值" @update:value="emit('update:value', toRaw($event ?? 0))" />
        <i v-if="unitLabel" class="value-unit">{{ unitLabel }}</i>
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
  /* 7 轨：序号 + 指标 + 参数 + 排名口径 + 关系 + 目标值 + 操作。
     2026-11-16：参数/排名口径两个下拉是 v-if 的，靠自动放置会让后续控件
     逐个左移一轨（排名口径掉进 88px 的参数轨 → 只显示「证…」，
     操作按钮掉进 40px 的序号轨）。所以每条轨道都必须显式指定列位。 */
  grid-template-columns:
    40px                  /* 1 序号 */
    minmax(200px, 1.7fr)  /* 2 观察指标 */
    minmax(104px, .6fr)   /* 3 参数（CAGR 年数 / 国债期限） */
    minmax(148px, .9fr)   /* 4 排名口径 */
    minmax(96px, .55fr)   /* 5 判断关系 */
    minmax(120px, .7fr)   /* 6 目标值 */
    76px;                 /* 7 操作 */
  align-items: stretch;
  min-height: 74px;
  margin: 0 18px;
  border: 1px solid #d8ddd9;
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}

/* 显式列位：v-if 缺省时空出该轨，不让后面的控件前移 */
.condition-index { grid-column: 1; }
.condition-control--field { grid-column: 2; }
.condition-control--param { grid-column: 3; }
.condition-control--rank { grid-column: 4; }
.condition-control--operator { grid-column: 5; }
.condition-control--value { grid-column: 6; }
.condition-tools { grid-column: 7; }

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
  /* 小屏回到自动放置：先解除上面写死的列位，否则会挤在同一列 */
  .condition-control--field, .condition-control--param, .condition-control--rank,
  .condition-control--operator { grid-column: auto; }
  .condition-index { grid-row: 1 / 3; grid-column: auto; }
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
