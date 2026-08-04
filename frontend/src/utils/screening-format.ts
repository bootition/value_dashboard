/**
 * 字段级展示格式（L0-2：单位/口径统一，筛选/自选/详情同义显示）。
 *
 * 口径约定（与 StockDetail/IndicatorTabs 一致）：
 * - pct  字段：底层为小数比例，展示 ×100 加 %（roe、负债率、同比等）
 * - ratio 字段：本身即倍数（流动比率、速动比率、利息保障倍数等）
 * - price 字段：价格，保留 2 位小数
 * - 其余数值：走 fmt()（亿/万/小数自适应）
 */

import { fmtPct } from './formatters.ts'

export type FieldFormat = 'pct' | 'ratio' | 'price' | 'plain'

const PCT_FIELDS = new Set([
  'roe', 'roa', 'roic',
  'gross_margin', 'net_margin', 'debt_ratio',
  'revenue_yoy', 'net_profit_yoy', 'deducted_profit_yoy',
  'revenue_cagr3', 'revenue_cagr5', 'net_profit_cagr5',
  'dividend_yield', 'goodwill_ratio', 'payout_ratio',
])

const PRICE_FIELDS = new Set(['latest_close', 'open', 'high', 'low', 'close'])

const RATIO_FIELDS = new Set([
  'current_ratio', 'quick_ratio', 'cf_to_net_profit', 'interest_coverage',
])

export function fieldFormat(field: string): FieldFormat {
  if (PCT_FIELDS.has(field)) return 'pct'
  if (PRICE_FIELDS.has(field)) return 'price'
  if (RATIO_FIELDS.has(field)) return 'ratio'
  return 'plain'
}

/** 按字段语义格式化筛选/自选表格单元格。null/undefined → '—'。 */
export function formatFieldValue(field: string, value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value !== 'number') return String(value)
  switch (fieldFormat(field)) {
    case 'pct':
      return fmtPct(value)
    case 'price':
    case 'ratio':
      return value.toFixed(2)
    default: {
      const abs = Math.abs(value)
      if (abs < 0.01 && value !== 0) return value.toExponential(2)
      if (abs >= 1e8) return (value / 1e8).toFixed(2) + '亿'
      if (abs >= 1e4) return (value / 1e4).toFixed(2) + '万'
      return value.toFixed(2)
    }
  }
}

/** 常见字段的表头单位标注（"表头带单位"）。 */
export const FIELD_UNITS: Readonly<Record<string, string>> = {
  dividend_yield: '(%)',
  roe: '(%)',
  roa: '(%)',
  roic: '(%)',
  gross_margin: '(%)',
  net_margin: '(%)',
  debt_ratio: '(%)',
  revenue_yoy: '(%)',
  net_profit_yoy: '(%)',
  deducted_profit_yoy: '(%)',
  revenue_cagr3: '(%)',
  revenue_cagr5: '(%)',
  net_profit_cagr5: '(%)',
  goodwill_ratio: '(%)',
  payout_ratio: '(%)',
}

export function fieldTitleWithUnit(field: string, baseLabel: string): string {
  const unit = FIELD_UNITS[field]
  return unit ? `${baseLabel}${unit}` : baseLabel
}
