/**
 * Screening quality helpers.
 *
 * Pure functions that derive which indicator fields are untrusted for the
 * current screening result, given the rule tree, active sort field, actual
 * result columns, and the warning codes reported by the backend.
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
const METADATA_FIELDS = new Set(['stock_code', 'name', 'exchange', 'csrc_l1', 'latest_close'])

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
  readonly sortField: string | null
  readonly resultColumns: Iterable<string>
  readonly warningCodes: readonly WarningCode[]
}

/**
 * Compute the list of indicator fields that are untrusted AND relevant to
 * the current screening result.
 *
 * A field is "relevant" if it appears in any of:
 *   - the rule tree (referenced by a condition)
 *   - the active sort field
 *   - the actual visible result columns
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
  if (inputs.sortField && !inputs.sortField.startsWith('_')) {
    candidateFields.add(inputs.sortField)
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
