/**
 * 筛选「结果展示列」编解码、默认列与下拉选项。
 *
 * 2026-11-22 重构：从 `views/ScreeningPage.vue` 原样抽出，**行为逐字节等价**。
 * 2026-11-20/24 用户实测后补两条硬规则：
 *   1. 默认列必须放**初级估值/质量/成长指标**，不能再被证监会行业等元数据占满；
 *   2. 当前规则条件里用到的字段必须自动出现在结果表（筛 ROCE 就要看得见 ROCE）。
 *
 * 合成值协议（2026-11-16，未变）：
 *   `字段`                        纯字段
 *   `字段@参数=值`                 参数化字段（如 revenue_cagr@cagr_years=5）
 *   `字段@参数=值::rank::口径`     参数化 + 排名口径
 * 合成值让 n-select 多选里「同名不同参数」的选项互不冲突。
 */

import { computed, ref } from 'vue'
import type { ComputedRef, Ref } from 'vue'
import type { RankMode, ResultColumn, ScreeningIndicator, ScreeningRuleNode } from '../types/screening.ts'
import { fieldDisplayName, fieldTitleWithUnit } from '../utils/screening-format.ts'

/** 结果列合成值里的参数分隔符（与 decodeResultColumn 保持一致）。 */
export const PARAM_COLUMN_SEP = '@'
export const RANK_COLUMN_SEP = '::rank::'

/** 元数据列（`stock_meta`）的选项：中文名 + 单位都走 `screening-format` 的唯一实现。 */
function metadataColumn(field: string): { label: string; value: string } {
  return {
    label: fieldTitleWithUnit(field, fieldDisplayName(field, field)),
    value: field,
  }
}

// 结果展示列：运行前可自由增删；本次运行通过 /run 的 columns 参数生效，
// 并持久化到 draft.result_columns。这里只放 stock_meta 基础信息，
// 指标列复用 /api/screening/indicators 的 indicatorOptions。
export const RESULT_METADATA_COLUMNS: Array<{ label: string; value: string }> = [
  metadataColumn('stock_code'),
  metadataColumn('name'),
  metadataColumn('exchange'),
  metadataColumn('listing_date'),
  metadataColumn('is_st'),
  metadataColumn('is_suspended'),
  metadataColumn('total_shares'),
  metadataColumn('circ_shares'),
  metadataColumn('csrc_l1'),
  metadataColumn('csrc_l2'),
]

/**
 * 默认结果列：**初级信息优先**（用户 2026-11-24 原话：结果表信息太少）。
 *
 * 顺序即结果表列顺序：
 *   基础 → 估值 → 盈利/质量 → 成长。
 * `csrc_l1` / `csrc_l2` / `listing_date` 仍保留下拉可选，但**不再占默认位置**。
 *
 * 为什么股息率用 `ttm_dividend_yield`：`dividend_yield` 已被后端标记
 * DEPRECATED（分母用 now()，不可复现），默认列里放它会让每次运行都冒废弃警告。
 */
export const DEFAULT_RESULT_COLUMNS: readonly string[] = [
  // 基础
  'stock_code', 'name', 'exchange',
  // 估值
  'pe_ttm', 'pb_mrq', 'ttm_dividend_yield', 'total_market_cap', 'circ_market_cap',
  // 盈利 / 质量
  'roe', 'roce_10y_median', 'gross_margin', 'net_margin', 'debt_ratio',
  // 成长
  'revenue_yoy', 'net_profit_yoy',
]

/**
 * 默认列版本号：草稿里的列若是旧默认集合，按版本号迁移到新默认。
 *
 * 只比较**集合**，不比较顺序：旧草稿可能被 JSON 序列化/去重过。用户自己
 * 增删过的列（不等于旧默认集合）绝不覆盖。
 */
export const DEFAULT_RESULT_COLUMNS_VERSION = 2

/** 历史默认列（集合）——用于识别"用户从没自定义过"的旧草稿。 */
export const LEGACY_DEFAULT_RESULT_COLUMNS: ReadonlyArray<readonly string[]> = [
  // 2026-11 之前：8 列，全是基础元数据，一个指标都没有。
  ['stock_code', 'name', 'exchange', 'listing_date', 'csrc_l1', 'csrc_l2', 'latest_close', 'total_market_cap'],
  // 上一轮：12 列，已经带核心指标，但仍缺流通市值/毛利率/净利率/负债率。
  [
    'stock_code', 'name', 'exchange', 'total_market_cap', 'pe_ttm', 'pb_mrq',
    'roe', 'roce_10y_median', 'ttm_dividend_yield', 'revenue_yoy',
    'net_profit_yoy', 'latest_close',
  ],
]

/** 排名口径列合成值。 */
export function rankColumnValue(field: string, mode: string): string {
  return `${field}${RANK_COLUMN_SEP}${mode}`
}

/** 合成取值 → 后端列定义（字符串或 {field, rank_mode?, cagr_years?, tenor?}）。 */
export function decodeResultColumn(value: string): ResultColumn {
  let rest = value
  let rankMode: string | undefined
  const at = rest.indexOf(RANK_COLUMN_SEP)
  if (at >= 0) {
    rankMode = rest.slice(at + RANK_COLUMN_SEP.length)
    rest = rest.slice(0, at)
  }
  let field = rest
  const extra: Record<string, unknown> = {}
  const pAt = rest.indexOf(PARAM_COLUMN_SEP)
  if (pAt >= 0) {
    field = rest.slice(0, pAt)
    const matched = rest.slice(pAt).match(/^@([A-Za-z_]+)=(.*)$/)
    if (matched) {
      const [, key, raw] = matched
      extra[key] = key === 'cagr_years' ? Number(raw) : raw
    }
  }
  if (!rankMode && Object.keys(extra).length === 0) return field
  const out: Record<string, unknown> = { field, ...extra }
  if (rankMode) out.rank_mode = rankMode as RankMode
  return out as unknown as ResultColumn
}

/** 后端列定义 → 合成取值（载入草稿/已保存规则时用）。 */
export function encodeResultColumn(column: ResultColumn): string {
  if (typeof column === 'string') return column
  const anyCol = column as unknown as Record<string, unknown>
  const paramKey = anyCol.cagr_years !== undefined
    ? 'cagr_years'
    : anyCol.tenor !== undefined ? 'tenor' : ''
  let out = String(anyCol.field ?? '')
  if (paramKey) out += `${PARAM_COLUMN_SEP}${paramKey}=${String(anyCol[paramKey])}`
  if (anyCol.rank_mode) out += `${RANK_COLUMN_SEP}${String(anyCol.rank_mode)}`
  return out
}

/**
 * 合成值 → 下拉选项的中文标签（带单位）。
 *
 * 用于兜底：条件自动列/历史 rank_mode 列不在 `/indicators` 当前清单时，
 * naive-ui 会直接把 `roce_10y_median` 或 `pe_ttm::rank::market_rank` 当文字渲染。
 */
export function resultColumnOptionLabel(
  value: string,
  rankModes: ReadonlyArray<{ label: string; value: string }> = [],
): { label: string; value: string } {
  const decoded = decodeResultColumn(value)
  if (typeof decoded === 'string') return metadataColumn(decoded)
  const record = decoded as unknown as Record<string, unknown>
  let field = String(record.field ?? '')
  if (typeof record.cagr_years === 'number') field += String(record.cagr_years)
  else if (typeof record.tenor === 'string' && record.tenor) field += `_${record.tenor}`
  if (typeof record.rank_mode === 'string' && record.rank_mode) {
    // 先得出参数展开后的基名标签（带单位），再拼排名口径，避免
    // `fieldDisplayName(pe_ttm_market_rank)` 与 mode label 重复一次“全市场排名”。
    const baseLabel = fieldTitleWithUnit(field, fieldDisplayName(field, field))
    const rankField = `${field}_${record.rank_mode}`
    const modeLabel = rankModes.find((mode) => mode.value === record.rank_mode)?.label
    return {
      label: modeLabel
        ? `${baseLabel} · ${modeLabel}`
        : fieldTitleWithUnit(rankField, fieldDisplayName(rankField, baseLabel)),
      value,
    }
  }
  return {
    label: fieldTitleWithUnit(field, fieldDisplayName(field, field)),
    value,
  }
}

/** 去重并保持首次出现顺序。 */
function uniqueColumns(columns: readonly string[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const column of columns) {
    if (!column || seen.has(column)) continue
    seen.add(column)
    out.push(column)
  }
  return out
}

/** 判断一组列是否**恰好等于**某个历史默认集合（顺序无关）。 */
export function isLegacyDefaultResultColumns(columns: readonly ResultColumn[]): boolean {
  if (columns.length === 0) return false
  const actual = new Set(columns.map(encodeResultColumn))
  return LEGACY_DEFAULT_RESULT_COLUMNS.some((legacy) => {
    if (legacy.length !== actual.size) return false
    return legacy.every((column) => actual.has(column))
  })
}

/**
 * 迁移草稿里的结果列。
 *
 * - 版本号已是最新：原样保留（哪怕用户手动选回了旧默认集合）；
 * - 旧版本/无版本，且列恰好是旧默认集合：替换成新默认；
 * - 其它情况（用户自定义过）：原样保留。
 */
export function migrateDraftResultColumns(
  columns: readonly ResultColumn[],
  savedVersion?: number,
): { columns: string[]; migrated: boolean } {
  const encoded = uniqueColumns(columns.map(encodeResultColumn))
  if (savedVersion !== DEFAULT_RESULT_COLUMNS_VERSION && isLegacyDefaultResultColumns(encoded)) {
    return { columns: [...DEFAULT_RESULT_COLUMNS], migrated: true }
  }
  return { columns: encoded, migrated: false }
}

/** 单个条件（叶子）在结果表里对应的合成列。 */
function conditionColumnValues(condition: Record<string, unknown>): string[] {
  const rawField = condition.field
  if (typeof rawField !== 'string' || !rawField) return []

  let value = rawField
  if (typeof condition.cagr_years === 'number') {
    value += `${PARAM_COLUMN_SEP}cagr_years=${condition.cagr_years}`
  } else if (typeof condition.tenor === 'string' && condition.tenor) {
    value += `${PARAM_COLUMN_SEP}tenor=${condition.tenor}`
  }

  const out = [value]
  const rankMode = condition.rank_mode
  if (typeof rankMode === 'string' && rankMode) {
    out.push(rankColumnValue(value, rankMode))
  }
  return out
}

/**
 * 从当前规则树收集「条件字段」对应的展示列。
 *
 * 递归全部 AND/OR 组；同时纳入 `right_field`（指标间比较）。
 * `rank_mode` 的条件会同时给出原始指标和排名口径列 —— 用户既要知道
 * "PE 是多少"，也要知道"它在全市场排第几"。
 */
export function conditionResultColumns(node: ScreeningRuleNode): string[] {
  const out: string[] = []
  const walk = (current: ScreeningRuleNode): void => {
    for (const item of current.rules) {
      if ('logic' in item && item.logic) {
        walk(item as ScreeningRuleNode)
        continue
      }
      const condition = item as unknown as Record<string, unknown>
      out.push(...conditionColumnValues(condition))
      const rightField = condition.right_field
      if (typeof rightField === 'string' && rightField) out.push(rightField)
    }
  }
  walk(node)
  return uniqueColumns(out)
}

export interface ScreeningColumnsDeps {
  indicators: Ref<readonly ScreeningIndicator[]>
  rankModes: Ref<Array<{ label: string; value: string }>>
  /** 指标下拉（页面里由 indicatorLabelOf 生成），这里只读它的 label/value。 */
  indicatorOptions: ComputedRef<Array<{ label: string; value: string }>>
  /**
   * 当前规则树。传入后，条件字段会自动并入展示列；不传则保持旧行为
   * （只显示用户默认列/手动选择），便于单测与独立使用。
   */
  ruleTree?: ScreeningRuleNode
}

export function useScreeningColumns(deps: ScreeningColumnsDeps) {
  const { indicators, rankModes, indicatorOptions, ruleTree } = deps
  /**
   * 用户**自己选择**的列。用空数组表示"还没自定义 → 使用默认列"。
   * 条件自动列不写进这里，否则条件删掉后会留下幽灵列，也分不清谁移除的。
   */
  const selectedResultColumns = ref<string[]>([])
  /** 用户明确从结果表移除过的条件自动列（持久化到草稿，条件重新出现也不强塞）。 */
  const manuallyRemovedResultColumns = ref<string[]>([])

  const automaticResultColumns = computed(() =>
    ruleTree ? conditionResultColumns(ruleTree) : [],
  )

  /** 实际展示/提交的列 = 默认或用户选择 + 条件自动列（去重，手动移除的除外）。 */
  const effectiveResultColumns = computed(() => {
    const removed = new Set(manuallyRemovedResultColumns.value)
    const base = selectedResultColumns.value.length > 0
      ? selectedResultColumns.value
      : DEFAULT_RESULT_COLUMNS
    const merged = base.filter((column) => !removed.has(column))
    for (const column of automaticResultColumns.value) {
      if (removed.has(column) || merged.includes(column)) continue
      merged.push(column)
    }
    return merged
  })

  /**
   * 结果列下拉真正的 v-model。用户改选时：
   * - 条件自动列单独由规则驱动，不写进 `selectedResultColumns`；
   * - 从当前条件自动列里去掉的 → 记入「手动移除」，刷新/重跑不再加回；
   * - 重新勾上的 → 清掉移除记录。
   */
  const resultColumnsSelection = computed<string[]>({
    get: () => effectiveResultColumns.value,
    set: (next) => {
      const unique = uniqueColumns(next)
      const nextSet = new Set(unique)
      const autoNow = new Set(automaticResultColumns.value)
      selectedResultColumns.value = unique.filter((column) => !autoNow.has(column))

      const removed = new Set(manuallyRemovedResultColumns.value)
      for (const column of autoNow) {
        if (nextSet.has(column)) removed.delete(column)
        else removed.add(column)
      }
      manuallyRemovedResultColumns.value = [...removed]
    },
  })

  const resultColumnOptions = computed(() => {
    const seen = new Set(RESULT_METADATA_COLUMNS.map(item => item.value))
    const options: Array<{ label: string; value: string }> = [...RESULT_METADATA_COLUMNS]
    for (const indicator of indicatorOptions.value) {
      if (!('value' in indicator)) continue
      if (seen.has(indicator.value)) continue
      seen.add(indicator.value)
      options.push({ label: indicator.label, value: indicator.value })
    }
    // 参数化字段（reports/143 §2）：一个基名 + 多个参数取值各占一个选项，
    // 避免把 `revenue_cagr3` / `revenue_cagr5` 这类后缀字段铺进下拉。
    for (const indicator of indicators.value) {
      const spec = indicator.param_spec
      if (!spec) continue
      const base = fieldDisplayName(indicator.name, indicator.name)
      for (const value of spec.options) {
        // 库里存 `0p25y`（避免字段名里的小数点），界面显示「0.25 年」
        const shown = spec.kind === 'tenor'
          ? `${String(value).replace('p', '.')} 年`
          : `${String(value)} 年`
        options.push({
          label: `${fieldTitleWithUnit(indicator.name, base)} · ${shown}`,
          value: `${indicator.name}${PARAM_COLUMN_SEP}${spec.key}=${String(value)}`,
        })
      }
    }
    // 方案 C（2026-11-16）：可排名指标额外提供 6 个「排名口径」列。
    // 用一个合成值编码（`{field}::rank::{mode}`），避免 n-select 多选里
    // 同名字段无法区分；提交给后端时再 decode 成 {field, rank_mode}。
    for (const indicator of indicators.value) {
      if (!indicator.rankable) continue
      const base = fieldTitleWithUnit(indicator.name, fieldDisplayName(indicator.name, indicator.name))
      for (const mode of rankModes.value) {
        options.push({
          label: `${base} · ${mode.label}`,
          value: rankColumnValue(indicator.name, mode.value),
        })
      }
    }
    // 默认列/条件自动列的中文 + 单位兜底：字段当前不在 `/indicators`
    // 可用清单里时，naive-ui 会拿裸字段名当 label。这里保证任何会自动
    // 出现的列都有中文名，不露 `roce_10y_median` 这种英文。
    for (const field of [...DEFAULT_RESULT_COLUMNS, ...automaticResultColumns.value]) {
      if (seen.has(field)) continue
      seen.add(field)
      options.push(resultColumnOptionLabel(field, rankModes.value))
    }
    return options
  })

  /**
   * 提交给后端的 columns（方案 C）：把合成取值解码成 `{field, rank_mode}`。
   * 空选择时回落到默认列，保证结果表不会只剩代码。
   */
  function selectedColumnsForRequest(): ResultColumn[] {
    const picked = effectiveResultColumns.value
    if (picked.length === 0) return [...DEFAULT_RESULT_COLUMNS]
    return picked.map(decodeResultColumn)
  }

  return {
    selectedResultColumns,
    manuallyRemovedResultColumns,
    automaticResultColumns,
    effectiveResultColumns,
    resultColumnsSelection,
    resultColumnOptions,
    selectedColumnsForRequest,
  }
}
