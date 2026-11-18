/**
 * Display formatting helpers for stock-detail page.
 *
 * These functions preserve the exact visible formatting currently used by
 * StockDetailPage.vue. They handle null/undefined, large values (亿/万),
 * and non-number coercion.
 */

/**
 * Format a numeric value for display.
 *
 * - null/undefined → '—'
 * - number >= 1e8 → divide by 1e8, append '亿'
 * - number >= 1e4 → divide by 1e4, append '万'
 * - number < 1e4 → fixed digits (default 2)
 * - non-number → String(value)
 *
 * @param value - The value to format
 * @param digits - Number of decimal places (default 2)
 */
export function fmt(value: unknown, digits = 2): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    const abs = Math.abs(value)
    if (abs >= 1e8) return (value / 1e8).toFixed(digits) + '亿'
    if (abs >= 1e4) return (value / 1e4).toFixed(digits) + '万'
    return value.toFixed(digits)
  }
  return String(value)
}

/**
 * Format a decimal ratio as a percentage.
 *
 * - null/undefined → '—'
 * - number → multiply by 100, fixed 2 digits, append '%'
 * - non-number → String(value)
 *
 * @param value - The decimal ratio to format (e.g. 0.25 → '25.00%')
 */
export function fmtPct(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    return (value * 100).toFixed(2) + '%'
  }
  return String(value)
}

/**
 * Format a value that is **already stored as a percentage** (5.29 = 5.29%).
 *
 * 两类百分比存储口径（见 `app/core/screening/field_units.py`）的展示实现都在
 * 本文件里，`screening-format.ts` 只调用、不再自己拼 `%`（AGENTS R1）：
 * - 小数比例（pct）→ `fmtPct`：×100 后加 %
 * - 百分数原值（percent）→ `fmtStoredPercent`：原样加 %，**不再 ×100**
 *
 * - null/undefined → '—'
 * - number → fixed 2 digits, append '%'（不做 ×100 换算）
 * - non-number → String(value)
 *
 * @param value - The percent-stored value to format (e.g. 5.29 → '5.29%')
 */
export function fmtStoredPercent(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    return value.toFixed(2) + '%'
  }
  return String(value)
}
