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
 * 默认列版本号（g4 → 2026-11-24 第三版）：**草稿/规则里存的列**按版本号 + 列内容迁移。
 *
 * v2 → v3 的原因（独立审计实测证伪「机制在、但到不了用户」）：v2 的迁移判据是
 * `version !== 2 && 恰好等于旧默认集合`，于是
 *   1. 真实草稿（`screening_drafts` revision=111）已经是 `version=2`，
 *      列是 `['stock_code','name','csrc_l2','latest_close']` —— 既不是"版本落后"，
 *      也不是"精确等于旧默认集合" → **不迁移** → 界面最终只有 4 列 + 条件列，
 *      用户永远看不到 PE/PB/ROE（尽管 15 列常量、bundle 里都在）；
 *   2. 已保存规则（`screening_rules` 48/49/50）的 `rule_json.columns` 全是旧列，
 *      选规则时 `applyLoadedRule` 用它覆盖当前列 → 同样看不到指标。
 *
 * v3 的判据见 `resultColumnMigrationReason()`：**含任一指标列就绝不迁**（尊重真自定义），
 * 纯元数据残留或精确命中历史默认集合就迁（与版本号无关，防"版本号被提前写上"）。
 */
export const DEFAULT_RESULT_COLUMNS_VERSION = 3

/** 历史默认列（集合）—— 用于识别"用户从没自定义过"的旧草稿。 */
export const LEGACY_DEFAULT_RESULT_COLUMNS: ReadonlyArray<readonly string[]> = [
  // 2026-11 之前：8 列，全是基础元数据 + 收盘价，一个指标都没有。
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
    // `fieldDisplayName(pe_ttm_market_rank)` 与 mode label 重复一次「全市场排名」。
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

/**
 * 「非指标列」白名单：**只有**基础元数据 + 行情/规模快照。
 *
 * 迁移判据靠它做**反证**：一组列里只要出现**任何不在此集合**的字段
 * （`pe_ttm` / `pb_mrq` / `roe` / `roce_10y_median` / `gross_margin` /
 * 参数化 `revenue_cagr@cagr_years=5` / 排名口径 `roe::rank::…` …），
 * 就说明用户**主动选过指标** → 绝不迁移他的选择。
 *
 * 为什么用「元数据白名单」而不是「指标黑名单」：**故意 fail-safe**。将来新增一个
 * 指标字段时，白名单口径自动把它算成"用户选过指标"（宁可少迁一次，也不删用户的
 * 自定义）；反过来，黑名单口径漏登记一个新指标就会把用户的自定义列冲掉。
 *
 * `latest_close` / `total_market_cap` 算元数据（审计里的旧 8 列正好是这 8 个），
 * 所以「旧 8 列的子集」这种历史残留仍会被识别成残留 → 迁移；而只要用户点过
 * PE/PB/ROE 任何一个，就不会被误迁。
 */
export const RESULT_COLUMN_NON_INDICATOR_FIELDS: ReadonlySet<string> = new Set([
  ...RESULT_METADATA_COLUMNS.map(item => item.value),
  // 行情 / 规模快照：不是"指标"，用户无从据此表达"我要看估值/盈利"
  'latest_close',
  'total_market_cap',
])

/** 一组列里是否有**指标列**（即任何不在元数据白名单里的字段）。 */
export function hasIndicatorResultColumn(columns: readonly ResultColumn[]): boolean {
  return columns.some((column) => !RESULT_COLUMN_NON_INDICATOR_FIELDS.has(encodeResultColumn(column)))
}

/** 一组列是否**恰好等于**某个历史默认集合（顺序无关，只比集合）。 */
export function matchesLegacyDefaultResultColumns(columns: readonly ResultColumn[]): boolean {
  if (columns.length === 0) return false
  const actual = new Set(columns.map(encodeResultColumn))
  return LEGACY_DEFAULT_RESULT_COLUMNS.some((legacy) => {
    if (legacy.length !== actual.size) return false
    return legacy.every((column) => actual.has(column))
  })
}

/**
 * 是不是「历史默认列的残留」——即**不该继续当作用户意图**的一组列。
 *
 * = 精确等于某个历史默认集合（旧 8 列 / 旧 12 列）**或** 一个指标列都没有
 * （只有 `stock_code`/`name`/`csrc_*`/`latest_close`/`total_market_cap` 这类元数据）。
 * 空列表不算（空 = 用户主动清空 → 落到默认列，不叫"迁移"）。
 *
 * ⚠️ 这是**唯一**的"历史残留"判据实现：草稿恢复、规则装载、结果面板的
 * localStorage 旧键迁移全都调它，不允许各处再抄一份。
 */
export function isLegacyDefaultResultColumns(columns: readonly ResultColumn[]): boolean {
  if (columns.length === 0) return false
  return matchesLegacyDefaultResultColumns(columns) || !hasIndicatorResultColumn(columns)
}

/** 触发了哪一条迁移判据（诊断/测试用；`null` = 不迁移）。 */
export type ResultColumnMigrationReason =
  | 'legacy-default-set'
  | 'outdated-version'
  | 'no-indicator-column'

/**
 * 草稿/已保存规则里的一组结果列，是否该按新默认列重新初始化？（v3 判据唯一实现）
 *
 * 三条判据**任一命中即迁移**，但顺序至关重要：
 *
 * 1. `legacy-default-set`：集合**精确等于**历史默认集合（旧 8 列 / 旧 12 列）
 *    → 必迁，**与版本号无关**（防"版本号被提前写上"导致第二次再也不迁）。
 * 2. 硬否决：含**任一指标列** → 绝不迁。这是"尊重真自定义"的唯一出口，
 *    必须排在规则 1 之前判断"版本落后"，否则会把 v2 用户的自定义列冲掉。
 * 3. `outdated-version` / `no-indicator-column`：剩下的只可能是**纯元数据**列
 *    （一个指标都没有）—— 这几乎必然是历史默认的残留，而不是"用户主动只想要
 *    stock_code+name+csrc_l2+latest_close 这 4 列"（审计里的 revision=111 正是
 *    这一种）。规则 1（`version < 3`）在这里已被规则 3 蕴含，保留它只为在
 *    诊断里说清"到底是版本落后，还是版本已最新但列仍是残留"。
 *
 * `version >= 3` 且纯元数据 → 仍迁（判据 3 与版本无关）：宁可多显示三列指标，
 * 也不让"机制在、但到不了用户"重演。
 */
export function resultColumnMigrationReason(
  columns: readonly ResultColumn[],
  savedVersion?: number,
): ResultColumnMigrationReason | null {
  const encoded = uniqueColumns(columns.map(encodeResultColumn))
  // 空列表 = 用户主动清空 → 由调用方回落到默认列，不算"迁移"。
  if (encoded.length === 0) return null
  // 判据 2：精确命中历史默认集合 → 必迁（哪怕版本号已经是最新的 3）。
  if (matchesLegacyDefaultResultColumns(encoded)) return 'legacy-default-set'
  // 硬否决：选过任何指标列 = 真自定义 → 一个字都不动。
  if (hasIndicatorResultColumn(encoded)) return null
  // 判据 1：版本落后（且上面已确认没有任何指标列 → 不是真自定义）。
  if (savedVersion !== undefined && savedVersion < DEFAULT_RESULT_COLUMNS_VERSION) {
    return 'outdated-version'
  }
  // 判据 3：版本不落后，但一个指标列都没有 → 历史默认残留。
  return 'no-indicator-column'
}

/** `resultColumnMigrationReason()` 的布尔版（草稿/规则装载的判断入口）。 */
export function shouldMigrateResultColumns(
  columns: readonly ResultColumn[],
  savedVersion?: number,
): boolean {
  return resultColumnMigrationReason(columns, savedVersion) !== null
}

/**
 * 迁移草稿里的结果列（v3）。
 *
 * - 空列表：原样保留（由调用方回落到默认列）；
 * - 历史残留（见 `resultColumnMigrationReason`）：替换成新默认列；
 * - 含任一指标列（用户自定义过）：原样保留。
 *
 * ⚠️ 页面里真正生效的是 `useScreeningDraft::restoreDraft` / `useScreeningRules::applyLoadedRule`
 * 里的同一套判断（它们需要在回填 `selectedResultColumns` 之前决定），本函数供其它入口/测试复用。
 * 三处必须共用同一个判据实现 —— 就是上面的 `resultColumnMigrationReason`。
 */
export function migrateDraftResultColumns(
  columns: readonly ResultColumn[],
  savedVersion?: number,
): { columns: string[]; migrated: boolean } {
  const encoded = uniqueColumns(columns.map(encodeResultColumn))
  if (shouldMigrateResultColumns(encoded, savedVersion)) {
    return { columns: [...DEFAULT_RESULT_COLUMNS], migrated: true }
  }
  return { columns: encoded, migrated: false }
}

/** 单个条件（叶子）在结果表里对应的合成列。 */
function conditionColumnValues(condition: Record<string, unknown>): string[] {
  const rawField = condition.field
  if (typeof rawField !== 'string' || !rawField) return []

  // 参数化字段（reports/143 §2）：条件上的参数后缀必须原样带到展示列，
  // 否则 `revenue_cagr@cagr_years=5` 会被当成裸 `revenue_cagr` → 后端算不出。
  let value = rawField
  if (typeof condition.cagr_years === 'number') {
    value += `${PARAM_COLUMN_SEP}cagr_years=${condition.cagr_years}`
  } else if (typeof condition.tenor === 'string' && condition.tenor) {
    value += `${PARAM_COLUMN_SEP}tenor=${condition.tenor}`
  }

  const out = [value]
  const rankMode = condition.rank_mode
  if (typeof rankMode === 'string' && rankMode) {
    // 带排名口径的条件：既显示原始值，也显示排名口径值。
    out.push(rankColumnValue(value, rankMode))
  }
  return out
}

/**
 * 从当前规则树收集「条件字段」对应的展示列（g4）。
 *
 * 用户原话：「筛 ROCE 就要看得见 ROCE」。递归全部 AND/OR 组；同时纳入
 * `right_field`（指标间比较的右值）。去重后由调用方接在默认列之后。
 */
export function conditionResultColumns(node: ScreeningRuleNode): string[] {
  const out: string[] = []
  const walk = (current: ScreeningRuleNode): void => {
    if (!current || !Array.isArray(current.rules)) return
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
  /**
   * 当前规则树（g4）。传入后，条件里用到的字段会**自动并入**展示列；
   * 不传则保持旧行为（只显示默认列/用户手选），便于单测与独立使用。
   *
   * 这里收 `reactive` 对象本身即可：computed 在读取 `ruleTree.rules` 时会建立
   * 依赖追踪，条件一改，自动列跟着变，不需要调用方额外 watch。
   */
  ruleTree?: ScreeningRuleNode
}

export function useScreeningColumns(deps: ScreeningColumnsDeps) {
  const { indicators, rankModes, indicatorOptions, ruleTree } = deps
  /**
   * 用户**自己选择**的列。用空数组表示"还没自定义 → 使用默认列"。
   * 条件自动列不写进这里，否则条件删掉后会留下幽灵列，也分不清是谁移除的。
   */
  const selectedResultColumns = deps.selection ?? ref<string[]>([])
  /** 用户明确从结果表移除过的条件自动列（持久化到草稿，条件重新出现也不强塞）。 */
  const manuallyRemovedResultColumns = ref<string[]>([])

  const automaticResultColumns = computed(() =>
    ruleTree ? conditionResultColumns(ruleTree) : [],
  )

  /** 实际展示/提交的列 = 默认或用户选择 + 条件自动列（去重、手动移除的除外、排在默认列之后）。 */
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
    // 默认列 / 条件自动列的中文 + 单位兜底（2026-11-20，g4 扩到自动列）：
    // 字段当前不在 `/indicators` 可用清单里时（覆盖率不足 → 进 unavailable），
    // naive-ui 的 n-select 会拿原值当 label 渲染 → 用户看到 `roce_10y_median`
    // 这种裸英文。这里用 `fieldDisplayName`（唯一中文词表）+ `fieldTitleWithUnit`
    // 补一条，保证任何会自动出现的列在下拉里都是「中文名(单位)」。
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
   *
   * g4 起用 `effectiveResultColumns`：条件字段会自动进请求列，否则后端算出来的
   * 指标在结果表里根本没有那一列（用户筛 ROCE 却看不见 ROCE）。
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
