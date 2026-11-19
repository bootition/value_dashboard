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
 * Format a number with a fixed number of decimals (no 亿/万 scaling).
 *
 * 与 `fmt` 的区别：`fmt` 会做亿/万换算（适合"金额"），本函数只做定点小数
 * （适合"PE / 分位 / 倍数 / 覆盖率"这类无量纲数）。
 *
 * - null/undefined/非有限数 → '—'
 * - number → `toFixed(digits)` + suffix
 *
 * @param value - The value to format
 * @param digits - Number of decimal places (default 2)
 * @param suffix - Appended verbatim after the number (e.g. '%')
 */
export function fmtFixed(value: number | null | undefined, digits = 2, suffix = ''): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value.toFixed(digits)}${suffix}`
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
 * - number → `toFixed(digits)` + '%'（不做 ×100 换算）
 * - non-number → String(value)
 *
 * 前端**唯一**的"百分数原值 → 带 % 文本"出口：任何 `xxx.toFixed(n) + '%'`
 * 都应改为调用本函数（护栏测试 `tests/percent-render-single-source.test.ts`）。
 *
 * @param value - The percent-stored value to format (e.g. 5.29 → '5.29%')
 * @param digits - Number of decimal places (default 2)
 */
export function fmtStoredPercent(value: unknown, digits = 2): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    // NaN/Infinity 不可能来自 JSON，但要挡住算术中间态，避免渲染 "Infinity%"
    if (!Number.isFinite(value)) return '—'
    return value.toFixed(digits) + '%'
  }
  return String(value)
}
