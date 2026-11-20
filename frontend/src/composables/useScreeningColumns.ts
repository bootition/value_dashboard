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
 * 默认结果列（`/run` 未显式指定 columns 时提交的那一份）。
 *
 * 2026-11-20（用户实测：**点不到 ROE 表头**）：旧默认列全是
 * `stock_code`/`name`/`exchange`/`listing_date`/`csrc_*`/`latest_close`/
 * `total_market_cap` —— 一个**指标都没有**。而结果面板的列下拉是
 * `Object.keys(firstRow)`（`ScreeningResultsPanel.vue`），于是 ROI/ROE 这些
 * 列根本没被请求过，用户想「点 ROE 表头排序」时表头压根不存在。
 * 现在默认列必须带用户常用的核心指标。
 *
 * 每个字段都属于后端 `/run` 的合法结果列（只读核对
 * `app/core/screening/engine.py`）：
 *   - `METADATA_COLUMNS`（engine.py:647）：stock_code / name / exchange /
 *     listing_date / csrc_l1 / csrc_l2 …
 *   - `SNAPSHOT_COLUMNS`（engine.py:63-87）：total_market_cap / pe_ttm /
 *     pb_mrq / roe / roce_10y_median / revenue_yoy / net_profit_yoy /
 *     ttm_dividend_yield / latest_close
 *   - `_build_select()`（engine.py:1396-1410）：白名单列**原样**进 SELECT
 *     （不属于 METADATA/SNAPSHOT 才起别名），所以行字典的键就是下面这些字符串，
 *     前端 `Object.keys(row)` 拿到的就是它们。
 *
 * 为什么用 `ttm_dividend_yield` 而不是 `dividend_yield`：后者已被
 * `engine.py:101 DEPRECATED_COLUMNS` 标记（分母用 now()，
 * 不可复现），且 `run()` 会把 `columns` 也算进引用字段
 * （engine.py:800 `referenced_fields = ... | set(columns_spec)`），
 * 默认列里塞它会让**每一次**默认运行都冒一条 `field_deprecated:` 警告。
 */
export const DEFAULT_RESULT_COLUMNS = [
  'stock_code', 'name', 'exchange',
  'total_market_cap',
  'pe_ttm', 'pb_mrq',
  'roe', 'roce_10y_median',
  'ttm_dividend_yield',
  'revenue_yoy', 'net_profit_yoy',
  'latest_close',
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
 * 结果列选择的**共享存储**（模块级单例）。
 *
 * 2026-11-20（审计实证）：同一个筛选页上有**两个**「选择要显示的列」入口 ——
 *   1. `ScreeningPage.vue` §02「结果展示列」：走本 composable，决定 `/run`
 *      提交的 `columns`（也就是结果表**有哪几列**）；
 *   2. `ScreeningResultsPanel.vue` 结果表上方的列下拉：只决定「已返回的列里
 *      显示哪几列」。
 * 两处各存各的 `ref` + 各自的 localStorage，于是「在这里选了，到那里不生效」。
 *
 * 这个 ref 是**合并入口的唯一状态**：两个入口都 `useSharedResultColumnSelection()`
 * 拿同一个 ref，或把 `selection` 传进 `useScreeningColumns()`，就此共享。
 *
 * ⚠️ 默认**不共享**（`useScreeningColumns()` 不传 `selection` 时仍是每次调用
 * 独立的 ref）：同页多个实例/单测之间不该互相污染，共享必须是显式的。
 */
const sharedResultColumnSelection: Ref<string[]> = ref<string[]>([])

/** 取「结果展示列」的共享选择（页面与结果面板共用同一份）。 */
export function useSharedResultColumnSelection(): Ref<string[]> {
  return sharedResultColumnSelection
}

export interface ScreeningColumnsDeps {
  indicators: Ref<readonly ScreeningIndicator[]>
  rankModes: Ref<Array<{ label: string; value: string }>>
  /** 指标下拉（页面里由 indicatorLabelOf 生成），这里只读它的 label/value。 */
  indicatorOptions: ComputedRef<Array<{ label: string; value: string }>>
  /**
   * 选择状态的落点。不传 = 本次调用独占一个 ref（既有行为）；
   * 传 `useSharedResultColumnSelection()` = 与其它入口共用同一份状态。
   */
  selection?: Ref<string[]>
}

export function useScreeningColumns(deps: ScreeningColumnsDeps) {
  const { indicators, rankModes, indicatorOptions } = deps
  const selectedResultColumns = deps.selection ?? ref<string[]>([])

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
    // 默认列的中文 + 单位兜底（2026-11-20）：默认列里可能有个别指标当前不在
    // `/api/screening/indicators` 的可用清单里（覆盖率不足 → 进 unavailable）。
    // 缺失时 naive-ui 的 n-select 会拿原值当 label 渲染 → 用户看到 `roce_10y_median`
    // 这种裸英文。这里用 `fieldDisplayName`（唯一中文词表）+ `fieldTitleWithUnit`
    // 补一条，保证**每一个默认列**在页面下拉里都是「中文名(单位)」。
    for (const field of DEFAULT_RESULT_COLUMNS) {
      if (seen.has(field)) continue
      seen.add(field)
      options.push(metadataColumn(field))
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
