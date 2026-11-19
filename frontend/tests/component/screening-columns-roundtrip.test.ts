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
