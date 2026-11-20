<script setup lang="ts">
/**
 * 单条筛选条件行。
 *
 * 2026-11-24 用户裁定（第二次重申）：**条件行里不再有「排名口径」下拉**。
 * 用户原话：「筛选的时候，你不需要纠结结果是怎么排名的。结果的排名在结果
 * 那里自己选：点一下表头就按市值排，点一下 ROE 表头就按 ROE 排。你干嘛要把
 * 这个东西放在这里？」
 *
 * 因此本组件删掉了 `rankModes` / `rankable` 两个 prop 与该下拉，列宽从 7 轨
 * 收回 5/6 轨（见样式区 `.condition-line`）。
 *
 * 兼容性（数据不丢）：`ScreeningRuleCondition.rank_mode` **仍然存在于类型和
 * 提交 payload 里**。历史规则/草稿里带 `rank_mode` 的条件照旧原样提交，后端
 * `app/core/screening/engine.py::expand_rank_modes` 会把它展开成
 * `{field}_{rank_mode}` 列 —— 语义与 UI 移除前完全一致，只是这里不再提供
 * **修改入口**（也不读它、不改它：本组件对 `condition` 只在用户操作时写入
 * 对应字段）。
 */
import { computed, h } from 'vue'
import { NEllipsis, NInputNumber, NSelect } from 'naive-ui'
import type { VNode } from 'vue'
import type { IndicatorOption, ScreeningRuleCondition } from '../types/screening.ts'
import { fieldDisplayName, fieldInputUnit } from '../utils/screening-format.ts'

const props = defineProps<{
  condition: ScreeningRuleCondition
  ordinal: number
  indicatorOptions: IndicatorOption[]
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

/**
 * 下拉值的**中文兜底**（2026-11-16，reports/148）。
 *
 * naive-ui 的 `n-select` 在 value 不在 options 里时会合成一个
 * `label = String(value)` 的兜底选项（`Select.mjs` wrappedFallbackOptionRef），
 * 于是历史规则/草稿里的旧字段名（如 `roce_10y_median_industry_rank`）
 * 会**原样显示英文**并被截成「roce_10y_median_i…」。
 *
 * `render-label` 拿到的就是这个兜底选项，它不经过 `fieldDisplayName()`。
 * 用 `fallback-option` 把标签换成中文解析结果，任何"字段还在、但已不在下拉里"
 * 的情况都不会再露出英文。
 */
function fallbackIndicatorOption(value: string): { label: string; value: string } {
  return { label: fieldDisplayName(String(value), String(value)), value }
}

function toggleFieldComparison(): void {
  emit('update:rightField', compareByField.value ? undefined : firstIndicatorValue())
}
</script>

<template>
  <article class="condition-line" :class="{ 'condition-line--has-param': Boolean(paramSpec) }">
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
        :fallback-option="fallbackIndicatorOption"
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
      <n-select
        :consistent-menu-width="false"
        :fallback-option="fallbackIndicatorOption"
        :value="condition.right_field"
        :options="indicatorOptions"
        size="small"
        filterable
        :render-label="renderIndicatorLabel"
        aria-label="比较指标"
        @update:value="emit('update:rightField', $event)"
      />
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
  /*
   * 2026-11-24：删掉「排名口径」下拉后，轨道从 7 轨收回 5 轨，空出来的
   * 148px 直接分给「观察指标」和「目标值」——不留空轨、也不挤压其它列。
   *
   * 轨道数量与 DOM 元素数量**严格一致**（序号 / 指标 / 关系 / 目标值 / 操作），
   * 所以这里可以用自动放置：之前出现「控件掉进错误的窄轨」是因为 7 轨模板
   * 配 6 个元素——错位来自轨道宽窄不匹配，不是自动放置本身。
   *
   * 「参数」轨（CAGR 年数 / 国债期限）只有参数化字段才有，用
   * `.condition-line--has-param` 单独给一套 6 轨模板，避免那条轨在非参数
   * 字段上留下一个 104px 的空洞。
   */
  grid-template-columns:
    40px                  /* 1 序号 */
    minmax(220px, 2.1fr)  /* 2 观察指标 */
    minmax(110px, .6fr)   /* 3 判断关系 */
    minmax(170px, .95fr)  /* 4 目标值 */
    78px;                 /* 5 操作 */
  align-items: stretch;
  min-height: 74px;
  margin: 0 18px;
  border: 1px solid #d8ddd9;
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}

.condition-line--has-param {
  grid-template-columns:
    40px                  /* 1 序号 */
    minmax(200px, 1.8fr)  /* 2 观察指标 */
    minmax(104px, .62fr)  /* 3 参数（CAGR 年数 / 国债期限） */
    minmax(104px, .58fr)  /* 4 判断关系 */
    minmax(160px, .85fr)  /* 5 目标值 */
    78px;                 /* 6 操作 */
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
  /* 小屏回到自动放置：先解除上面写死的列位，否则会挤在同一列 */
  .condition-control--field, .condition-control--param,
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
