/**
 * 筛选「结果展示列」编解码与下拉选项。
 *
 * 2026-11-22 重构：从 `views/ScreeningPage.vue` 原样抽出，**行为逐字等价**。
 * 抽取理由：这 120 行是纯映射（字段 → n-select 合成值 → 后端列定义），
 * 与组件状态无关，留在 1060 行的页面里既难读也难测。
 *
 * 合成值协议（2026-11-16，未变）：
 *   `字段`                        纯字段
 *   `字段@参数=值`                 参数化字段（如 revenue_cagr@cagr_years=5）
 *   `字段@参数=值::rank::口径`     参数化 + 排名口径
 * 合成值让 n-select 多选里「同名不同参数」的选项互不冲突。
 */

import { computed, ref } from 'vue'
import type { ComputedRef, Ref } from 'vue'
import type { RankMode, ResultColumn, ScreeningIndicator } from '../types/screening.ts'
import { fieldDisplayName } from '../utils/screening-format.ts'

/** 结果列合成值里的参数分隔符（与 decodeResultColumn 保持一致）。 */
export const PARAM_COLUMN_SEP = '@'
export const RANK_COLUMN_SEP = '::rank::'

// 结果展示列：运行前可自由增删；本次运行通过 /run 的 columns 参数生效，
// 并持久化到 draft.result_columns。这里只放 stock_meta 基础信息，
// 指标列复用 /api/screening/indicators 的 indicatorOptions。
export const RESULT_METADATA_COLUMNS: Array<{ label: string; value: string }> = [
  { label: '股票代码', value: 'stock_code' },
  { label: '股票名称', value: 'name' },
  { label: '交易所', value: 'exchange' },
  { label: '上市日期', value: 'listing_date' },
  { label: 'ST 状态', value: 'is_st' },
  { label: '停牌状态', value: 'is_suspended' },
  { label: '总股本', value: 'total_shares' },
  { label: '流通股本', value: 'circ_shares' },
  { label: '证监会一级行业', value: 'csrc_l1' },
  { label: '证监会二级行业', value: 'csrc_l2' },
]

export const DEFAULT_RESULT_COLUMNS = [
  'stock_code', 'name', 'exchange', 'listing_date',
  'csrc_l1', 'csrc_l2', 'latest_close', 'total_market_cap',
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

export interface ScreeningColumnsDeps {
  indicators: Ref<readonly ScreeningIndicator[]>
  rankModes: Ref<Array<{ label: string; value: string }>>
  /** 指标下拉（页面里由 indicatorLabelOf 生成），这里只读它的 label/value。 */
  indicatorOptions: ComputedRef<Array<{ label: string; value: string }>>
}

export function useScreeningColumns(deps: ScreeningColumnsDeps) {
  const { indicators, rankModes, indicatorOptions } = deps
  const selectedResultColumns = ref<string[]>([])

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
          label: `${base} · ${shown}`,
          value: `${indicator.name}${PARAM_COLUMN_SEP}${spec.key}=${String(value)}`,
        })
      }
    }
    // 方案 C（2026-11-16）：可排名指标额外提供 6 个「排名口径」列。
    // 用一个合成值编码（`{field}::rank::{mode}`），避免 n-select 多选里
    // 同名字段无法区分；提交给后端时再 decode 成 {field, rank_mode}。
    for (const indicator of indicators.value) {
      if (!indicator.rankable) continue
      const base = fieldDisplayName(indicator.name, indicator.name)
      for (const mode of rankModes.value) {
        options.push({
          label: `${base} · ${mode.label}`,
          value: rankColumnValue(indicator.name, mode.value),
        })
      }
    }
    return options
  })

  /**
   * 提交给后端的 columns（方案 C）：把合成取值解码成 `{field, rank_mode}`。
   * 空选择时回落到默认列，保证结果表不会只剩代码。
   */
  function selectedColumnsForRequest(): ResultColumn[] {
    const picked = selectedResultColumns.value
    if (picked.length === 0) return [...DEFAULT_RESULT_COLUMNS]
    return picked.map(decodeResultColumn)
  }

  return {
    selectedResultColumns,
    resultColumnOptions,
    selectedColumnsForRequest,
  }
}
