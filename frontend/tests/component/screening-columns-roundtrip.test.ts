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
import { computed, ref } from 'vue'
import {
  DEFAULT_RESULT_COLUMNS,
  decodeResultColumn,
  encodeResultColumn,
  rankColumnValue,
  RESULT_METADATA_COLUMNS,
  useScreeningColumns,
  useSharedResultColumnSelection,
} from '../../src/composables/useScreeningColumns.ts'
import type { ScreeningIndicator } from '../../src/types/screening.ts'

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

describe('默认结果列必须含核心指标（2026-11-20 用户实测：点不到 ROE 表头）', () => {
  // 根因：结果面板的列下拉是 `Object.keys(firstRow)`（ScreeningResultsPanel.vue），
  // 而 firstRow 的键 = `/run` 返回的列 = 本文件提交的 columns。旧默认列里
  // 一个指标都没有 → 表头没有 ROE/ROCE → 用户「点 ROE 表头排序」时无从点起。
  const CORE_METRICS = [
    'roce_10y_median',
    'roe',
    'pe_ttm',
    'pb_mrq',
    'ttm_dividend_yield',
    'revenue_yoy',
    'net_profit_yoy',
    'total_market_cap',
  ] as const

  it('默认列包含全部核心指标', () => {
    for (const metric of CORE_METRICS) {
      expect(DEFAULT_RESULT_COLUMNS, `默认列缺少 ${metric}`).toContain(metric)
    }
  })

  it('默认列仍是纯字段名（不带 @参数 / ::rank:: 合成值）', () => {
    for (const column of DEFAULT_RESULT_COLUMNS) {
      expect(column).not.toContain('@')
      expect(column).not.toContain('::rank::')
    }
  })

  it('每个默认列在页面下拉里都有「中文名(单位)」选项（指标清单为空也不许露英文）', () => {
    const cols = useScreeningColumns({
      indicators: ref<readonly ScreeningIndicator[]>([]),
      rankModes: ref<Array<{ label: string; value: string }>>([]),
      indicatorOptions: computed(() => []),
    })
    const byValue = new Map(cols.resultColumnOptions.value.map((o) => [o.value, o.label]))
    for (const column of DEFAULT_RESULT_COLUMNS) {
      const label = byValue.get(column)
      expect(label, `${column} 没有下拉选项`).toBeTruthy()
      expect(label!, `${column} 的选项仍是英文：${label}`).toMatch(/[\u4e00-\u9fa5]/)
    }
    // 单位：市值写「亿」、百分比写「%」
    expect(byValue.get('total_market_cap')).toContain('亿')
    expect(byValue.get('roe')).toContain('%')
    expect(byValue.get('revenue_yoy')).toContain('%')
    expect(byValue.get('pe_ttm')).toContain('倍')
  })
})

describe('两个列选择入口共享同一份状态（seam）', () => {
  it('显式传入同一个 selection 时，两处读到同一份选择', () => {
    const shared = useSharedResultColumnSelection()
    const baseDeps = () => ({
      indicators: ref<readonly ScreeningIndicator[]>([]),
      rankModes: ref<Array<{ label: string; value: string }>>([]),
      indicatorOptions: computed(() => []),
      selection: shared,
    })
    const page = useScreeningColumns(baseDeps())
    const panel = useScreeningColumns(baseDeps())
    try {
      page.selectedResultColumns.value = ['stock_code', 'roe']
      expect(panel.selectedResultColumns.value).toEqual(['stock_code', 'roe'])
      expect(panel.selectedColumnsForRequest()).toEqual(['stock_code', 'roe'])
      expect(page.selectedColumnsForRequest()).toEqual(['stock_code', 'roe'])
    } finally {
      shared.value = []
    }
  })

  it('不传 selection 时互不影响（默认仍是各自独立的 ref）', () => {
    const mk = () => useScreeningColumns({
      indicators: ref<readonly ScreeningIndicator[]>([]),
      rankModes: ref<Array<{ label: string; value: string }>>([]),
      indicatorOptions: computed(() => []),
    })
    const a = mk()
    const b = mk()
    a.selectedResultColumns.value = ['stock_code']
    expect(b.selectedResultColumns.value).toEqual([])
  })
})
