/**
 * 自动更新进度"人话化"的纯函数契约（全局进度条 + 详情页提示共用）。
 *
 * 为什么要有这份测试：用户看不到进度的直接原因是**阶段名/计数只存在于后端英文
 * 字段里**。这里锁三件事：
 * 1. 英文步骤名必须映射成中文（未知步骤必须兜底，不许把原始 key 甩给用户）；
 * 2. "更新中"判定必须与数据状态页同口径（current_stage 或 latest_job）；
 * 3. 只有更新中/失败才产出视图，空闲（finished/idle）必须返回 null。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  AUTO_UPDATE_STAGE_FALLBACK,
  AUTO_UPDATE_STAGE_LABELS,
  buildUpdateProgressView,
  formatElapsed,
  formatEta,
  isAutoUpdateRunning,
  stageLabel,
  stepFromPhase,
  updatePercent,
} from '../src/helpers/auto-update-progress.ts'

const RUNNING_STATUS = {
  state: 'enabled',
  current_stage: 'running',
  progress: {
    phase: 'step:indicators',
    job_id: 'job-1',
    started_at: '2026-11-21T12:16:56.000Z',
    steps: { prices: 'success' },
    live: {
      step: 'indicators',
      // 后端 label 曾出现乱码，前端不得依赖它
      label: '锟斤拷',
      done: 2500,
      total: 5575,
      current: '000672',
      rate_per_minute: 380.4,
      eta_seconds: 848,
    },
  },
  last_error: null,
  latest_job: { id: 256, status: 'running' },
}

const NOW = Date.parse('2026-11-21T12:19:56.000Z') // 开始后 3 分钟

test('英文阶段名映射成中文人话', () => {
  assert.equal(stageLabel('prices'), '正在更新股票价格')
  assert.equal(stageLabel('indicators'), '正在重算指标快照')
  assert.equal(stageLabel('announcements'), '正在抓取公告')
  assert.equal(stageLabel('roce'), '正在计算资本回报率（ROCE）')
  // progress.phase 的 `step:xxx` 形状必须能归一化
  assert.equal(stageLabel('step:indicators'), '正在重算指标快照')
  assert.equal(stepFromPhase('step:prices'), 'prices')
})

test('未知阶段有兜底文案，绝不显示原始英文 key', () => {
  const label = stageLabel('mystery_step')
  assert.equal(label, AUTO_UPDATE_STAGE_FALLBACK)
  assert.ok(!label.includes('mystery_step'))
  assert.ok(!label.includes('_'))
  // 空值同样兜底
  assert.equal(stageLabel(null), AUTO_UPDATE_STAGE_FALLBACK)
  assert.equal(stageLabel(''), AUTO_UPDATE_STAGE_FALLBACK)
})

test('映射表覆盖后端 STEP_ORDER 的全部 26 步 + check', () => {
  const expected = [
    'trading_dates', 'prices', 'indicators', 'universe', 'announcements', 'financials',
    'market_actions', 'financial_detail_backfill', 'buyback', 'funding', 'treasury_curve',
    'cashflow_supplement', 'balance_sheet_ext', 'deducted_profit', 'fixed_assets',
    'regulatory_metrics', 'retries', 'indicator_ext', 'roce', 'business_overview',
    'capital_history', 'research_statistics', 'etf_prices', 'hk_dividends',
    'index_valuation', 'analyst_forecast', 'check',
  ]
  for (const step of expected) {
    assert.equal(typeof AUTO_UPDATE_STAGE_LABELS[step], 'string', `缺阶段映射: ${step}`)
    assert.ok(AUTO_UPDATE_STAGE_LABELS[step].startsWith('正在'), `阶段文案不是人话: ${step}`)
  }
})

test('更新中判定：current_stage 或 latest_job 任一 running', () => {
  assert.equal(isAutoUpdateRunning({ current_stage: 'running' }), true)
  assert.equal(isAutoUpdateRunning({ current_stage: 'finished', latest_job: { status: 'running' } }), true)
  assert.equal(isAutoUpdateRunning({ current_stage: 'finished', latest_job: { status: 'success' } }), false)
  assert.equal(isAutoUpdateRunning({ current_stage: 'failed' }), false)
  assert.equal(isAutoUpdateRunning(null), false)
})

test('更新中视图：中文阶段 + 已完成/总数 + 百分比 + 已耗时', () => {
  const view = buildUpdateProgressView(RUNNING_STATUS, NOW)
  assert.ok(view)
  assert.equal(view.kind, 'running')
  assert.equal(view.title, '正在重算指标快照')
  assert.equal(view.counts, '2500 / 5575')
  assert.equal(view.percent, 45) // 44.84% → 45%
  assert.equal(view.elapsed, '3 分钟')
  assert.equal(view.eta, '15 分钟') // 848s → ceil(14.13) = 15
  assert.equal(view.current, '000672')
  assert.equal(view.error, null)
})

test('更新失败视图：last_error 必须可见', () => {
  const view = buildUpdateProgressView({
    current_stage: 'failed',
    last_error: '镜像源连接超时',
    progress: { phase: 'done', status: 'failed' },
  }, NOW)
  assert.ok(view)
  assert.equal(view.kind, 'failed')
  assert.equal(view.title, '数据更新失败')
  assert.equal(view.error, '镜像源连接超时')
})

test('失败但没有错误详情时也给兜底文案，不静默', () => {
  const view = buildUpdateProgressView({ current_stage: 'failed', last_error: null }, NOW)
  assert.ok(view)
  assert.equal(view.kind, 'failed')
  assert.ok(view.error && view.error.length > 0)
})

test('空闲/完成不产出视图（进度条必须消失）', () => {
  assert.equal(buildUpdateProgressView(null, NOW), null)
  assert.equal(buildUpdateProgressView({ current_stage: 'idle' }, NOW), null)
  assert.equal(buildUpdateProgressView({
    current_stage: 'finished',
    progress: { phase: 'done', status: 'success' },
  }, NOW), null)
})

test('百分比边界：无总数返回 null，超额夹到 100', () => {
  assert.equal(updatePercent(1, 0), null)
  assert.equal(updatePercent(Number.NaN, 10), null)
  assert.equal(updatePercent(6, 5), 100)
  assert.equal(updatePercent(-3, 10), 0)
  assert.equal(updatePercent(2500, 5575), 45)
})

test('已耗时/剩余时长格式化稳健（非法值不显示负数）', () => {
  assert.equal(formatElapsed(null), '')
  assert.equal(formatElapsed('not-a-date'), '')
  assert.equal(formatElapsed('2026-11-21T12:20:00.000Z', NOW), '')
  assert.equal(formatElapsed('2026-11-21T12:18:31.000Z', NOW), '1 分 25 秒')
  assert.equal(formatElapsed('2026-11-21T11:19:56.000Z', NOW), '1 小时')
  assert.equal(formatEta(null), '')
  assert.equal(formatEta(30), '不到 1 分钟')
  assert.equal(formatEta(848), '15 分钟')
})
