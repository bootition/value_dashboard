/**
 * Screening quality helpers.
 *
 * Pure functions that derive which indicator fields are untrusted for the
 * current screening result, given the rule tree, the actual result columns,
 * and the warning codes reported by the backend.
 *
 * 2026-11-23 更正：这里原本还承诺了一个「活动排序字段」入参。`sortField` 已在
 * 2026-11-19（`41181e1`，reports/143 §10）随"结果排序移出筛选页"一起删除 ——
 * 文档没跟着改，于是**注释承诺了一个不存在的入参**。
 * 这正是测试代码不做类型检查时容易积累的那类漂移（见 reports/140 §17.3）。
 *
 * These helpers never touch Vue state or the DOM. They are unit-tested.
 */
import { isIndicatorUntrusted, type WarningCode } from '../types/data-quality.ts'
import { isRuleNode, type ScreeningRuleNode, type ScreeningRuleCondition } from '../types/screening.ts'

export type { ScreeningRuleNode, ScreeningRuleCondition }

/**
 * Known metadata fields that are NOT indicator fields.
 *
 * These fields come from stock_meta or price tables, not from indicator_snapshot.
 * They should not be flagged as untrusted by indicator-level warning codes.
 */
const METADATA_FIELDS = new Set(['stock_code', 'name', 'exchange', 'csrc_l1', 'csrc_l2', 'sw_level1', 'sw_level2', 'latest_close'])

/**
 * Recursively collect every indicator field referenced by a rule tree.
 *
 * Deterministic and side-effect free.
 */
export function collectRuleFields(node: ScreeningRuleNode): Set<string> {
  const out = new Set<string>()
  const walk = (n: ScreeningRuleNode): void => {
    for (const item of n.rules) {
      if (isRuleNode(item)) {
        walk(item)
      } else if (item.field) {
        out.add(item.field)
        // 比较右侧引用字段同样影响结果可信度，必须一并纳入告警覆盖。
        if (item.right_field) out.add(item.right_field)
      }
    }
  }
  walk(node)
  return out
}

/**
 * Inputs to computeUntrustedFields.
 */
export interface ComputeUntrustedInputs {
  readonly ruleFields: Iterable<string>
  readonly resultColumns: Iterable<string>
  readonly warningCodes: readonly WarningCode[]
}

/**
 * Compute the list of indicator fields that are untrusted AND relevant to
 * the current screening result.
 *
 * A field is "relevant" if it appears in any of:
 *   - the rule tree (referenced by a condition)
 *   - the actual visible result columns
 *
 * （原来还有一项「活动排序字段」，已随 `sortField` 的删除一并去掉。）
 *
 * A field is "untrusted" if isIndicatorUntrusted returns true for it given
 * the current warning codes.
 *
 * Metadata columns beginning with '_' are excluded from the output — they
 * are not durable exported/saved columns.
 *
 * The result is sorted for stable output.
 */
export function computeUntrustedFields(inputs: ComputeUntrustedInputs): string[] {
  const candidateFields = new Set<string>()

  for (const f of inputs.ruleFields) {
    if (f && !f.startsWith('_')) candidateFields.add(f)
  }
  for (const f of inputs.resultColumns) {
    if (f && !f.startsWith('_')) candidateFields.add(f)
  }

  const untrusted: string[] = []
  for (const field of candidateFields) {
    if (METADATA_FIELDS.has(field)) continue
    if (isIndicatorUntrusted(field, inputs.warningCodes)) {
      untrusted.push(field)
    }
  }
  untrusted.sort()
  return untrusted
}
