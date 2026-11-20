/**
 * 结果列「合成取值」编解码契约（2026-11-22 反返工护栏）。
 *
 * 守护对象：`src/composables/useScreeningColumns.ts`（2026-11-22 从
 * `views/ScreeningPage.vue` 抽出的列定义 / 编解码 / 下拉选项）。
 *
 * 为什么必须钉死：n-select 多选里同名字段无法区分，所以结果列用一个**合成字符串**
 * 在"界面选项 ↔ 后端列定义"之间转换（2026-11-16 方案 C）：
 *   `字段` / `字段@参数=值` / `字段@参数=值::rank::口径`
 * 编解码不对称的后果是静默的：导出/运行请求里列变成 `{field:"undefined"}` 或
 * 排名口径丢失（表头还在、数字全错）。抽 composable 时最容易改错这类字符串拼接。
 */

import { describe, it, expect } from 'vitest'
import { computed, reactive, ref } from 'vue'
import {
  conditionResultColumns,
  DEFAULT_RESULT_COLUMNS,
  DEFAULT_RESULT_COLUMNS_VERSION,
  decodeResultColumn,
  encodeResultColumn,
  isLegacyDefaultResultColumns,
  LEGACY_DEFAULT_RESULT_COLUMNS,
  migrateDraftResultColumns,
  rankColumnValue,
  resultColumnOptionLabel,
  RESULT_METADATA_COLUMNS,
  useScreeningColumns,
} from '../../src/composables/useScreeningColumns.ts'
import type { ScreeningIndicator, ScreeningRuleNode } from '../../src/types/screening.ts'

describe('decodeResultColumn / encodeResultColumn 对称性', () => {
  it('纯字段原样通过', () => {
    expect(decodeResultColumn('stock_code')).toBe('stock_code')
    expect(encodeResultColumn('stock_code')).toBe('stock_code')
  })

  it('参数化字段：cagr_years 转数字，tenor 保留字符串', () => {
    expect(decodeResultColumn('revenue_cagr@cagr_years=5')).toEqual({ field: 'revenue_cagr', cagr_years: 5 })
    expect(decodeResultColumn('revenue_cagr@tenor=0p25y')).toEqual({ field: 'revenue_cagr', tenor: '0p25y' })
    expect(encodeResultColumn({ field: 'revenue_cagr', cagr_years: 5 } as never)).toBe('revenue_cagr@cagr_years=5')
  })

  it('排名口径列编解码对称', () => {
    const value = rankColumnValue('pe_ttm', 'market_rank')
    expect(value).toBe('pe_ttm::rank::market_rank')
    expect(decodeResultColumn(value)).toEqual({ field: 'pe_ttm', rank_mode: 'market_rank' })
    expect(encodeResultColumn({ field: 'pe_ttm', rank_mode: 'market_rank' } as never))
      .toBe('pe_ttm::rank::market_rank')
  })

  it('参数 + 排名口径同时存在时两者都不丢', () => {
    const value = rankColumnValue('revenue_cagr@cagr_years=3', 'industry_rank')
    const decoded = decodeResultColumn(value) as unknown as Record<string, unknown>
    expect(decoded.field).toBe('revenue_cagr')
    expect(decoded.cagr_years).toBe(3)
    expect(decoded.rank_mode).toBe('industry_rank')
    // 回程
    expect(encodeResultColumn(decoded as never)).toBe(value)
  })
})

describe('useScreeningColumns 提交给后端的 columns', () => {
  it('空选择回落到默认列（结果表不会只剩代码）', () => {
    const indicators = ref<readonly ScreeningIndicator[]>([])
    const rankModes = ref<Array<{ label: string; value: string }>>([])
    const indicatorOptions = computed(() => [])
    const cols = useScreeningColumns({ indicators, rankModes, indicatorOptions })
    expect(cols.selectedColumnsForRequest()).toEqual([...DEFAULT_RESULT_COLUMNS])
  })

  it('默认列是拷贝：调用方改动不会污染常量（下次运行仍拿到完整默认列）', () => {
    const indicators = ref<readonly ScreeningIndicator[]>([])
    const rankModes = ref<Array<{ label: string; value: string }>>([])
    const indicatorOptions = computed(() => [])
    const cols = useScreeningColumns({ indicators, rankModes, indicatorOptions })
    const first = cols.selectedColumnsForRequest()
    first.length = 1
    expect(cols.selectedColumnsForRequest()).toEqual([...DEFAULT_RESULT_COLUMNS])
  })

  it('下拉选项含元数据列 + 指标列 + 参数化选项 + 排名口径选项', () => {
    const indicators = ref<readonly ScreeningIndicator[]>([
      {
        name: 'pe_ttm',
        rankable: true,
        param_spec: { kind: 'cagr', key: 'cagr_years', label: '年数', options: [3, 5] },
      } as unknown as ScreeningIndicator,
    ])
    const rankModes = ref([{ label: '全市场排名', value: 'market_rank' }])
    const indicatorOptions = computed(() => [{ label: '市盈率（PE-TTM）(倍)', value: 'pe_ttm' }])
    const cols = useScreeningColumns({ indicators, rankModes, indicatorOptions })
    const values = cols.resultColumnOptions.value.map((o) => o.value)

    expect(values.slice(0, RESULT_METADATA_COLUMNS.length)).toEqual(RESULT_METADATA_COLUMNS.map((o) => o.value))
    expect(values).toContain('pe_ttm')
    expect(values).toContain('pe_ttm@cagr_years=3')
    expect(values).toContain('pe_ttm@cagr_years=5')
    expect(values).toContain('pe_ttm::rank::market_rank')
    // 元数据列不会因指标同名而重复
    expect(values.filter((v) => v === 'stock_code')).toHaveLength(1)
  })
})


describe('默认列：初级信息优先（2026-11-24 用户实测）', () => {
  it('含估值/质量/成长核心指标，且不再默认放证监会行业/上市日期', () => {
    const required = [
      'stock_code', 'name', 'exchange',
      'pe_ttm', 'pb_mrq', 'ttm_dividend_yield', 'total_market_cap', 'circ_market_cap',
      'roe', 'roce_10y_median', 'gross_margin', 'net_margin', 'debt_ratio',
      'revenue_yoy', 'net_profit_yoy',
    ]
    for (const field of required) {
      expect(DEFAULT_RESULT_COLUMNS, `默认列缺少 ${field}`).toContain(field)
    }
    expect(DEFAULT_RESULT_COLUMNS).not.toContain('csrc_l1')
    expect(DEFAULT_RESULT_COLUMNS).not.toContain('csrc_l2')
    expect(DEFAULT_RESULT_COLUMNS).not.toContain('listing_date')
  })

  it('每个默认列在指标清单为空时仍有中文名和单位（不露 roce_10y_median）', () => {
    const cols = useScreeningColumns({
      indicators: ref<readonly ScreeningIndicator[]>([]),
      rankModes: ref<Array<{ label: string; value: string }>>([]),
      indicatorOptions: computed(() => []),
    })
    const byValue = new Map(cols.resultColumnOptions.value.map((o) => [o.value, o.label]))
    for (const field of DEFAULT_RESULT_COLUMNS) {
      const label = byValue.get(field)
      expect(label, `${field} 没有下拉选项`).toBeTruthy()
      expect(label!, `${field} 的选项仍是英文：${label}`).toMatch(/[\u4e00-\u9fa5]/)
    }
    expect(byValue.get('total_market_cap')).toContain('亿')
    expect(byValue.get('circ_market_cap')).toContain('亿')
    expect(byValue.get('pe_ttm')).toContain('倍')
    expect(byValue.get('pb_mrq')).toContain('倍')
    expect(byValue.get('ttm_dividend_yield')).toContain('%')
    expect(byValue.get('roe')).toContain('%')
    expect(byValue.get('roce_10y_median')).toContain('%')
  })
})

describe('旧草稿默认列迁移', () => {
  it('识别旧的 8 列 / 12 列默认集合', () => {
    expect(isLegacyDefaultResultColumns([...LEGACY_DEFAULT_RESULT_COLUMNS[0]])).toBe(true)
    expect(isLegacyDefaultResultColumns([...LEGACY_DEFAULT_RESULT_COLUMNS[1]])).toBe(true)
    expect(isLegacyDefaultResultColumns(['stock_code', 'name', 'pe_ttm'])).toBe(false)
  })

  it('无版本/旧版本的旧默认集合 → 新默认；自定义列保持原样', () => {
    const old8 = migrateDraftResultColumns([...LEGACY_DEFAULT_RESULT_COLUMNS[0]], undefined)
    expect(old8.migrated).toBe(true)
    expect(old8.columns).toEqual([...DEFAULT_RESULT_COLUMNS])

    const custom = migrateDraftResultColumns(['stock_code', 'roce_10y_median'], undefined)
    expect(custom.migrated).toBe(false)
    expect(custom.columns).toEqual(['stock_code', 'roce_10y_median'])
  })

  it('版本已是最新时即使列刚好等于旧默认集合也尊重用户选择', () => {
    const result = migrateDraftResultColumns(
      [...LEGACY_DEFAULT_RESULT_COLUMNS[0]],
      DEFAULT_RESULT_COLUMNS_VERSION,
    )
    expect(result.migrated).toBe(false)
    expect(result.columns).toEqual([...LEGACY_DEFAULT_RESULT_COLUMNS[0]])
  })
})

describe('条件字段自动并入结果列（筛 ROCE 必须看到 ROCE）', () => {
  function makeCols(ruleTree: ScreeningRuleNode) {
    return useScreeningColumns({
      ruleTree,
      indicators: ref<readonly ScreeningIndicator[]>([]),
      rankModes: ref<Array<{ label: string; value: string }>>([]),
      indicatorOptions: computed(() => []),
    })
  }

  it('递归收集嵌套条件字段，并放在默认列之后', () => {
    const ruleTree = reactive<ScreeningRuleNode>({
      id: 'root',
      logic: 'AND',
      rules: [
        { id: 'c1', field: 'total_market_cap', op: '>', value: 100 },
        {
          id: 'g1',
          logic: 'OR',
          rules: [{ id: 'c2', field: 'current_ratio', op: '>', value: 1 }],
        },
      ],
    } as unknown as ScreeningRuleNode)
    const cols = makeCols(ruleTree)
    const request = cols.selectedColumnsForRequest()
    expect(request).toContain('current_ratio')
    expect(request.slice(0, DEFAULT_RESULT_COLUMNS.length)).toEqual([...DEFAULT_RESULT_COLUMNS])
    expect(request.at(-1)).toBe('current_ratio')
  })

  it('参数化条件保留参数；rank_mode 同时给出原始指标和排名列', () => {
    const ruleTree = reactive<ScreeningRuleNode>({
      id: 'root',
      logic: 'AND',
      rules: [
        { id: 'c1', field: 'revenue_cagr', op: '>', value: 0.1, cagr_years: 5 },
        { id: 'c2', field: 'pe_ttm', op: '<', value: 20, rank_mode: 'market_rank' },
      ],
    } as unknown as ScreeningRuleNode)
    const columns = conditionResultColumns(ruleTree)
    expect(columns).toContain('revenue_cagr@cagr_years=5')
    expect(columns).toContain('pe_ttm')
    expect(columns).toContain('pe_ttm::rank::market_rank')

    const cols = makeCols(ruleTree)
    expect(cols.selectedColumnsForRequest()).toContainEqual({ field: 'revenue_cagr', cagr_years: 5 })
    expect(cols.selectedColumnsForRequest()).toContainEqual({ field: 'pe_ttm', rank_mode: 'market_rank' })
  })

  it('手动移除条件列后不再强行加回；重新勾选则恢复', () => {
    const ruleTree = reactive<ScreeningRuleNode>({
      id: 'root',
      logic: 'AND',
      rules: [{ id: 'c1', field: 'roce_10y_median', op: '>', value: 0.1 }],
    } as unknown as ScreeningRuleNode)
    const cols = makeCols(ruleTree)
    cols.resultColumnsSelection.value = cols.resultColumnsSelection.value.filter(
      (column) => column !== 'roce_10y_median',
    )
    expect(cols.selectedColumnsForRequest()).not.toContain('roce_10y_median')
    expect(cols.manuallyRemovedResultColumns.value).toContain('roce_10y_median')

    // 规则条件仍在 → computed 重算也不会绕过"手动移除"状态。
    expect(cols.effectiveResultColumns.value).not.toContain('roce_10y_median')

    cols.resultColumnsSelection.value = [
      ...cols.resultColumnsSelection.value,
      'roce_10y_median',
    ]
    expect(cols.selectedColumnsForRequest()).toContain('roce_10y_median')
    expect(cols.manuallyRemovedResultColumns.value).not.toContain('roce_10y_median')
  })
})


describe('合成列名的中文兜底', () => {
  it('排名口径与参数化字段也显示中文名 + 单位，不露字段名', () => {
    const rank = resultColumnOptionLabel(
      'pe_ttm::rank::market_rank',
      [{ label: '全市场排名', value: 'market_rank' }],
    )
    expect(rank.label).toContain('市盈率')
    expect(rank.label).toContain('排名')
    expect(rank.label).not.toContain('pe_ttm')

    const cagr = resultColumnOptionLabel('revenue_cagr@cagr_years=5')
    expect(cagr.label).toContain('营收')
    expect(cagr.label).toContain('5')
    expect(cagr.label).toContain('%')
    expect(cagr.label).not.toContain('revenue_cagr')
  })
})
