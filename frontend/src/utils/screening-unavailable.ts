/**
 * 「当前报告期不可用指标」展示层（/api/screening/indicators 的 `unavailable`）。
 *
 * 2026-11-22 重构：从 `views/ScreeningPage.vue`（原 1060 行）原样抽出。
 * 这里只有**纯函数**（无 ref / 无 axios / 无 i18n 状态），
 * 因此可以脱离组件单测（`tests/screening-unavailable.test.ts`）。
 *
 * 背景（reports/120 §5 J5/J9）：后端把「当前报告期不可用/被遮蔽」的字段
 * 从 indicators 移到 unavailable（字段名 + reason + coverage_pct）。
 * 只让字段消失不给理由，用户会以为功能没了——这里给出可读的理由与覆盖率。
 */

import { fieldDisplayName } from './screening-format.ts'
import { fmtStoredPercent } from './formatters.ts'

export interface UnavailableIndicator {
  readonly name?: string
  readonly field?: string
  readonly domain?: string
  readonly reason?: string
  readonly coverage_pct?: number | null
  readonly message?: string
  readonly replaced_by?: string
}

export const UNAVAILABLE_REASON_LABELS: Readonly<Record<string, string>> = {
  not_available_for_latest_period: '当前报告期不可用（该期覆盖率不足）',
  masked_untrusted: '数据质量遮蔽（当前不可信）',
  // 2026-11-16：旧列与新列是**同一个指标**，只是存储单位不同（小数 vs 百分数），
  // 说成「口径不可复现」会让用户以为数算错了。
  deprecated: '同一指标已统一单位，请改用新字段',
}

export function unavailableName(item: UnavailableIndicator): string {
  return item.name ?? item.field ?? '未命名字段'
}

export function unavailableLabel(item: UnavailableIndicator): string {
  const name = unavailableName(item)
  return name === '未命名字段' ? name : fieldDisplayName(name)
}

export function unavailableReasonText(item: UnavailableIndicator): string {
  const reason = item.reason?.trim()
  if (!reason) return '原因未标注'
  return `${UNAVAILABLE_REASON_LABELS[reason] ?? '当前不可用'}（reason: ${reason}）`
}

export function unavailableCoverageText(item: UnavailableIndicator): string {
  const coverage = item.coverage_pct
  if (typeof coverage === 'number' && Number.isFinite(coverage)) {
    return `覆盖率 ${fmtStoredPercent(coverage, 1)}`
  }
  // 2026-11-16：废弃字段（如 dividend_yield → ttm_dividend_yield）与覆盖率无关，
  // 以前统一写「覆盖率未知」，用户会以为"数据没采到"，其实是口径已统一。
  if (item.reason === 'deprecated') {
    return item.replaced_by ? `已改名为 ${fieldDisplayName(item.replaced_by)}` : '口径已统一'
  }
  if (item.reason === 'masked_untrusted') return '数据质量遮蔽'
  return '覆盖率未知'
}

export function unavailableKey(item: UnavailableIndicator): string {
  return `${unavailableName(item)}:${item.reason ?? ''}`
}
