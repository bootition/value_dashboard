/**
 * 结果展示列 g4：15 列默认 / 条件字段自动并入 / 手动移除不回填 / 草稿版本迁移。
 *
 * 用户实测两条（2026-11-24）：
 *  1. 「结果表信息太少」——旧默认列是 8 列基础元数据 + 证监会行业，一个指标都没有；
 *  2. 「我筛 ROCE，结果表里却没有 ROCE 这一列」——条件字段没有自动并入展示列，
 *     用户看得见"命中 3 家"，却看不见"凭什么命中"。
 *
 * 这里钉死四条不变量（都是"看起来能跑、错了却没人发现"的那类）：
 *  - 默认列恰好是那 15 列（顺序 = 结果表列顺序），且 csrc_l1/l2/listing_date 退出默认；
 *  - 条件字段（含 right_field / 参数后缀 / rank_mode 展开）自动并入，去重、
 *    排在默认列之后；
 *  - 用户手动删掉的条件列**不被自动加回**（否则用户永远删不掉一列）；
 *  - 旧草稿的默认列集合按版本号迁移到 15 列，用户自定义过的列原样保留。
 */

import { describe, it, expect } from 'vitest'
import { computed, nextTick, reactive, ref } from 'vue'
import {
  conditionResultColumns,
  DEFAULT_RESULT_COLUMNS,
  DEFAULT_RESULT_COLUMNS_VERSION,
  isLegacyDefaultResultColumns,
  migrateDraftResultColumns,
  resultColumnMigrationReason,
  useScreeningColumns,
} from '../../src/composables/useScreeningColumns.ts'
import type {
  ScreeningIndicator,
  ScreeningRuleCondition,
  ScreeningRuleNode,
} from '../../src/types/screening.ts'

/** 条件节点的构造器（`cagr_years`/`tenor` 不在共享类型里，用 as 补上）。 */
function condition(partial: Record<string, unknown>): ScreeningRuleCondition {
  return { id: String(partial.field), op: '<', value: 1, ...partial } as unknown as ScreeningRuleCondition
}

function group(logic: 'AND' | 'OR', rules: ScreeningRuleNode['rules']): ScreeningRuleNode {
  return { id: `g-${logic}`, logic, rules }
}

describe('默认结果列 = 15 列（初级估值/质量/成长优先）', () => {
  it('列集合与顺序都符合规格', () => {
    expect([...DEFAULT_RESULT_COLUMNS]).toEqual([
      'stock_code', 'name', 'exchange',
      'pe_ttm', 'pb_mrq', 'ttm_dividend_yield', 'total_market_cap', 'circ_market_cap',
      'roe', 'roce_10y_median', 'gross_margin', 'net_margin', 'debt_ratio',
      'revenue_yoy', 'net_profit_yoy',
    ])
    expect(DEFAULT_RESULT_COLUMNS.length).toBe(15)
  })

  it('证监会行业/上市日期/收盘价退出默认（仍在下拉里可选）', () => {
    for (const field of ['csrc_l1', 'csrc_l2', 'listing_date', 'latest_close']) {
      expect(DEFAULT_RESULT_COLUMNS).not.toContain(field)
    }
  })
})

describe('条件字段自动并入结果列', () => {
  it('递归 AND/OR 取 field 与 right_field，去重且保持首次出现顺序', () => {
    const tree = group('AND', [
      condition({ field: 'pe_ttm' }),
      group('OR', [
        condition({ field: 'roe' }),
        condition({ field: 'pe_ttm' }),
        condition({ field: 'pe_ttm', right_field: 'pb_mrq', op: '>' }),
      ]),
    ])
    expect(conditionResultColumns(tree)).toEqual(['pe_ttm', 'roe', 'pb_mrq'])
  })

  it('参数后缀原样带上（revenue_cagr@cagr_years=5 / tenor）', () => {
    const tree = group('AND', [
      condition({ field: 'revenue_cagr', cagr_years: 5 }),
      condition({ field: 'ebitda', tenor: '0p25y' }),
    ])
    expect(conditionResultColumns(tree)).toEqual([
      'revenue_cagr@cagr_years=5',
      'ebitda@tenor=0p25y',
    ])
  })

  it('rank_mode 展开：同时加原始列与排名口径列', () => {
    const tree = group('AND', [
      condition({ field: 'pe_ttm', rank_mode: 'market_rank' }),
    ])
    expect(conditionResultColumns(tree)).toEqual(['pe_ttm', 'pe_ttm::rank::market_rank'])
  })

  it('自动列排在默认列之后，且不与默认列重复', () => {
    const ruleTree = reactive<ScreeningRuleNode>(
      group('AND', [
        condition({ field: 'pe_ttm' }),
        condition({ field: 'revenue_cagr', cagr_years: 5 }),
      ]),
    )
    const cols = useScreeningColumns({
      indicators: ref<readonly ScreeningIndicator[]>([]),
      rankModes: ref<Array<{ label: string; value: string }>>([]),
      indicatorOptions: computed(() => []),
      ruleTree,
    })
    const effective = cols.effectiveResultColumns.value
    // 前 15 列仍是不变（= 用户看到的"默认信息"），自动列接在后面
    expect(effective.slice(0, DEFAULT_RESULT_COLUMNS.length)).toEqual([...DEFAULT_RESULT_COLUMNS])
    expect(effective.slice(DEFAULT_RESULT_COLUMNS.length)).toEqual(['revenue_cagr@cagr_years=5'])
    // pe_ttm 已在默认列里 → 不重复追加
    expect(effective.filter((column) => column === 'pe_ttm')).toHaveLength(1)
    // 提交给后端的 columns 也带上自动列（否则结果表根本没有那一列）。
    // 注意这里是**解码后**的列定义：参数化字段变成 {field, cagr_years}。
    expect(cols.selectedColumnsForRequest()).toEqual([
      ...DEFAULT_RESULT_COLUMNS,
      { field: 'revenue_cagr', cagr_years: 5 },
    ])
  })

  it('条件里出现的新字段有中文下拉选项（不露英文）', () => {
    const ruleTree = reactive<ScreeningRuleNode>(
      group('AND', [condition({ field: 'roce_10y_median', rank_mode: 'market_rank' })]),
    )
    const cols = useScreeningColumns({
      indicators: ref<readonly ScreeningIndicator[]>([]),
      rankModes: ref([{ label: '全市场排名', value: 'market_rank' }]),
      indicatorOptions: computed(() => []),
      ruleTree,
    })
    const byValue = new Map(cols.resultColumnOptions.value.map((o) => [o.value, o.label]))
    const rankLabel = byValue.get('roce_10y_median::rank::market_rank')
    expect(rankLabel).toBeTruthy()
    expect(rankLabel!).toMatch(/[\u4e00-\u9fa5]/)
  })
})

describe('手动移除的条件列不被自动加回', () => {
  function mountWithTree() {
    const ruleTree = reactive<ScreeningRuleNode>(
      group('AND', [
        condition({ field: 'pe_ttm' }),
        condition({ field: 'revenue_cagr', cagr_years: 5 }),
      ]),
    )
    const cols = useScreeningColumns({
      indicators: ref<readonly ScreeningIndicator[]>([]),
      rankModes: ref<Array<{ label: string; value: string }>>([]),
      indicatorOptions: computed(() => []),
      ruleTree,
    })
    return { ruleTree, cols }
  }

  it('删掉自动列 → 记入 manuallyRemovedResultColumns，且不再出现在生效列里', async () => {
    const { cols } = mountWithTree()
    const removedColumn = 'revenue_cagr@cagr_years=5'
    expect(cols.effectiveResultColumns.value).toContain(removedColumn)

    cols.resultColumnsSelection.value = cols.effectiveResultColumns.value.filter(
      (column) => column !== removedColumn,
    )
    await nextTick()

    expect(cols.manuallyRemovedResultColumns.value).toEqual([removedColumn])
    expect(cols.effectiveResultColumns.value).not.toContain(removedColumn)
    expect(cols.selectedColumnsForRequest()).not.toContain(removedColumn)
  })

  it('条件重新计算（规则树变了）也不会把删掉的列塞回来', async () => {
    const { ruleTree, cols } = mountWithTree()
    const removedColumn = 'revenue_cagr@cagr_years=5'
    cols.resultColumnsSelection.value = cols.effectiveResultColumns.value.filter(
      (column) => column !== removedColumn,
    )
    await nextTick()

    // 用户又加了一个条件：规则树变了 → automaticResultColumns 重算
    ruleTree.rules.push(condition({ field: 'roe' }))
    await nextTick()

    expect(cols.effectiveResultColumns.value).toContain('roe')
    expect(cols.effectiveResultColumns.value).not.toContain(removedColumn)
    expect(cols.manuallyRemovedResultColumns.value).toEqual([removedColumn])
  })

  it('用户重新勾上 → 清掉移除记录（否则以后再也加不回来）', async () => {
    const { cols } = mountWithTree()
    const removedColumn = 'revenue_cagr@cagr_years=5'
    cols.resultColumnsSelection.value = cols.effectiveResultColumns.value.filter(
      (column) => column !== removedColumn,
    )
    await nextTick()
    expect(cols.manuallyRemovedResultColumns.value).toEqual([removedColumn])

    cols.resultColumnsSelection.value = [...cols.effectiveResultColumns.value, removedColumn]
    await nextTick()

    expect(cols.manuallyRemovedResultColumns.value).toEqual([])
    expect(cols.effectiveResultColumns.value).toContain(removedColumn)
  })
})

describe('草稿默认列版本迁移（default_columns_version = 3）', () => {
  const OLD_8 = [
    'stock_code', 'name', 'exchange', 'listing_date',
    'csrc_l1', 'csrc_l2', 'latest_close', 'total_market_cap',
  ]
  const OLD_12 = [
    'stock_code', 'name', 'exchange', 'total_market_cap', 'pe_ttm', 'pb_mrq',
    'roe', 'roce_10y_median', 'ttm_dividend_yield', 'revenue_yoy',
    'net_profit_yoy', 'latest_close',
  ]

  it('旧 8 列 / 旧 12 列都能被识别（顺序无关）', () => {
    expect(isLegacyDefaultResultColumns(OLD_8)).toBe(true)
    expect(isLegacyDefaultResultColumns([...OLD_12].reverse())).toBe(true)
    expect(isLegacyDefaultResultColumns(['stock_code', 'pe_ttm'])).toBe(false)
    expect(isLegacyDefaultResultColumns([])).toBe(false)
  })

  it('版本缺失且恰好是旧默认集合 → 迁移成新 15 列', () => {
    expect(migrateDraftResultColumns(OLD_8)).toEqual({
      columns: [...DEFAULT_RESULT_COLUMNS],
      migrated: true,
    })
    expect(migrateDraftResultColumns(OLD_12, 1).migrated).toBe(true)
  })

  it('用户自定义过（不等于任何旧默认集合）→ 原样保留', () => {
    expect(migrateDraftResultColumns(['stock_code', 'roce_10y_median'])).toEqual({
      columns: ['stock_code', 'roce_10y_median'],
      migrated: false,
    })
  })

  it('版本已是 3，但列仍是历史默认集合 → **仍然迁移**（防"版本号被提前写上"）', () => {
    // 旧 v2 判据是 `version !== 2 && isLegacy(...)`，一旦 version 先被写成最新，
    // 这份残留列就**再也**不会被迁 —— 正是「机制在、但到不了用户」的那一类。
    // v3 起判据 2（精确命中历史默认集合）与版本号无关。
    expect(migrateDraftResultColumns(OLD_8, DEFAULT_RESULT_COLUMNS_VERSION)).toEqual({
      columns: [...DEFAULT_RESULT_COLUMNS],
      migrated: true,
    })
    expect(resultColumnMigrationReason(OLD_8, DEFAULT_RESULT_COLUMNS_VERSION))
      .toBe('legacy-default-set')
  })
})
