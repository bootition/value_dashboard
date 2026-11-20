/**
 * 错误文案契约（2026-09-17 更新窗口降级）。
 *
 * 自动更新子进程持 DuckDB 文件锁时，后端只读接口统一降级为 503
 * 「数据正在自动更新中，请稍后刷新」/ reason_code=duckdb_read_locked；
 * 前端必须把它翻译成明确的中文原因，而不是笼统的「服务器内部错误」。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { apiErrorText, describeApiError, friendlyErrorMessage } from '../src/helpers/api-error.ts'

function axiosError(status: number, detail: unknown) {
  return { isAxiosError: true, response: { status, data: { detail } } }
}

test('DuckDB 只读锁 503 显示“数据正在自动更新中”', () => {
  const guidance = describeApiError(axiosError(503, '数据正在自动更新中，请稍后刷新'))
  assert.equal(guidance.title, '数据正在自动更新中')
  assert.match(guidance.action ?? '', /更新完成/)
})

test('duckdb_read_locked / auto_update_in_progress reason code 同样识别', () => {
  const locked = describeApiError(
    axiosError(503, { reason_code: 'duckdb_read_locked' }),
  )
  assert.equal(locked.title, '数据正在自动更新中')

  const screening = describeApiError(
    axiosError(409, { reason_code: 'auto_update_in_progress' }),
  )
  assert.equal(screening.title, '数据正在自动更新中')
})

test('friendlyErrorMessage 不再落入“服务器内部错误”兜底', () => {
  const text = friendlyErrorMessage(axiosError(503, '数据正在自动更新中，请稍后刷新'))
  assert.match(text, /数据正在自动更新中/)
  assert.doesNotMatch(text, /服务器内部错误/)
})

// ─────────────────────────────────────────────────────────────────────────────
// 2026-11-23 用户实测「为什么无法筛选？」：后端 409 的可操作信息必须到用户眼前
// ─────────────────────────────────────────────────────────────────────────────

/** 用户可见文案里**禁止出现**的机器残留（裸枚举名 / snake_case / 机器词）。 */
const FORBIDDEN_IN_UI: ReadonlyArray<[RegExp, string]> = [
  [/warning_codes/, '裸键名 warning_codes'],
  [/reason_code/, '裸键名 reason_code'],
  [/\bstrict\b/i, '机器词 strict'],
  [/[a-z][a-z0-9]*_[a-z0-9_]+/, 'snake_case 字段名'],
  [/[{}]/, 'JSON 片段'],
]

function assertNoMachineResidue(text: string): void {
  for (const [pattern, reason] of FORBIDDEN_IN_UI) {
    assert.doesNotMatch(text, pattern, `用户可见文案里出现了${reason}：${text}`)
  }
}

/** 后端 `_require_current_screenability` 的真实 409 形状（data-quality 门禁）。 */
const READINESS_409 = {
  reason_code: 'screening_data_quality_not_ready',
  readiness: {
    ready: false,
    stock_count: 5580,
    missing: { share_capital: ['600519'] },
    missing_counts: { share_capital: 12, price_freshness: 3, lineage_coverage: 41 },
  },
  warning_codes: ['SNAPSHOT_STALE', 'STALE_RUNNING_JOBS'],
  readiness_source: 'computed',
}

test('409 结构化 detail：中文 message + 告警中文名 + 缺数计数都到用户眼前', () => {
  const text = friendlyErrorMessage(
    axiosError(409, { ...READINESS_409, message: '数据质量不满足筛选要求：命中阻断级告警，筛选已停止' }),
    '筛选失败',
  )
  // ① 后端中文 message 优先
  assert.match(text, /筛选已停止/)
  // ② 告警码翻成中文（不能再显示 SNAPSHOT_STALE 这种裸枚举）
  assert.match(text, /指标快照落后于最新行情/)
  assert.match(text, /有超过 2 小时未结束的任务/)
  assert.doesNotMatch(text, /SNAPSHOT_STALE/)
  // ③ 缺数说人话
  assert.match(text, /缺 41 只股票的数据溯源覆盖/)
  assert.match(text, /缺 12 只股票的股本数据/)
  // ④ 里里外外不许有机器残留
  assertNoMachineResidue(text)
})

test('409 结构化 detail：没有 message 时用 reason_code 现有中文映射 + 中文告警名', () => {
  const text = friendlyErrorMessage(axiosError(409, READINESS_409), '筛选失败')
  assert.match(text, /数据质量不满足筛选要求/)
  assert.match(text, /数据质量告警：指标快照落后于最新行情/)
  assert.match(text, /数据缺口：/)
  assertNoMachineResidue(text)
})

test('409 masked_untrusted：message 里内嵌的裸字段名必须换成中文', () => {
  const text = friendlyErrorMessage(axiosError(409, {
    reason_code: 'masked_untrusted',
    masked_fields: ['pe_ttm', 'roe'],
    message: '以下字段当前不可信，本次筛选已拒绝执行：pe_ttm、roe。请先修复数据质量告警（见 /api/data-status/summary 的 asset_health）后重试。',
  }), '筛选失败')
  assert.match(text, /市盈率/)
  assert.match(text, /净资产收益率/)
  assert.doesNotMatch(text, /pe_ttm|roe/)
  assert.doesNotMatch(text, /api\//)
  assertNoMachineResidue(text)
})

test('400 的英文 detail 先过映射表，不再把英文原文甩给用户', () => {
  const cases: ReadonlyArray<[string, RegExp]> = [
    ['saved rule version not found', /所选规则版本不存在/],
    ['rule JSON is too large', /规则内容过大/],
    ['title is required', /请填写标题/],
  ]
  for (const [detail, expected] of cases) {
    const text = apiErrorText(axiosError(400, detail), '保存规则失败')
    assert.match(text, expected, `${detail} 没有映射成中文：${text}`)
    assert.doesNotMatch(text, /[A-Za-z]{3,} [A-Za-z]{3,}/, `400 文案里仍有英文句子：${text}`)
  }
})

test('400 的后端中文校验消息仍按原文展示（内嵌字段名换中文）', () => {
  const text = apiErrorText(axiosError(400, 'pe_ttm 必须在 0-100 之间'), '保存失败')
  assert.match(text, /市盈率/)
  assert.match(text, /必须在 0-100 之间/)
  assertNoMachineResidue(text)
})

test('未知 reason_code 不再一律「刷新后重试」', () => {
  const text = friendlyErrorMessage(axiosError(409, { reason_code: 'brand_new_conflict' }), '保存失败')
  assert.doesNotMatch(text, /刷新后重试/)
  assert.match(text, /操作冲突，提交失败/)
})

test('刷新解决不了的 409 给准确动作，而不是让人白刷新', () => {
  const cases: ReadonlyArray<[string, RegExp, RegExp]> = [
    ['snapshot_values_untrusted', /快照整体不可信/, /刷新页面不会恢复/],
    ['masked_untrusted', /不可信的字段/, /刷新页面不会恢复/],
    ['rule_has_provenance_refs', /不能删除/, /刷新页面不会解除引用/],
  ]
  for (const [code, titleRe, actionRe] of cases) {
    const guidance = describeApiError(axiosError(409, { reason_code: code }))
    assert.match(guidance.title, titleRe, `${code} 标题不对：${guidance.title}`)
    assert.match(guidance.action ?? '', actionRe, `${code} 动作不对：${guidance.action ?? ''}`)
    assertNoMachineResidue(`${guidance.title}；${guidance.action ?? ''}`)
  }
})
