/**
 * 错误文案契约（2026-09-17 更新窗口降级）。
 *
 * 自动更新子进程持 DuckDB 文件锁时，后端只读接口统一降级为 503
 * 「数据正在自动更新中，请稍后刷新」/ reason_code=duckdb_read_locked；
 * 前端必须把它翻译成明确的中文原因，而不是笼统的「服务器内部错误」。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { describeApiError, friendlyErrorMessage } from '../src/helpers/api-error.ts'

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
