/**
 * 自动更新进度的人话化（全局进度条 + 个股详情页共用）。
 *
 * 后端 `GET /api/data-status/auto-update` 的字段事实（见
 * `app/web/api/data_status.py::get_auto_update_status` 与
 * `app/core/auto_update.py::detail_cb`）：
 *
 * - 恒有：`state` / `enabled` / `paused` / `current_stage` / `progress` /
 *   `last_error` / `last_success_at` / `updated_at` / `latest_job`
 * - **只有更新中**：`current_stage === 'running'`、
 *   `progress.phase === 'step:<step>'`、`progress.job_id`、`progress.started_at`、
 *   `progress.live = { step, label, done, total, current, rate_per_minute, eta_seconds, updated_at }`
 *   （`live` 由指标重算的细粒度回调提供，是"已完成/总数/百分比"的唯一来源）
 * - 结束后：`progress.phase === 'done'`，`progress.status` = success/partial/failed/skipped，
 *   `progress.steps` = 每步终态，`progress.live` 被移除。
 *
 * 为什么阶段名要前端自己映射：后端 `live.label` 是更新进程中产生的展示文案
 * （本轮实测返回过乱码串），而 `live.step` / `progress.phase` 是稳定的 ASCII
 * 步骤名。用步骤名映射中文既不受后端文案乱码影响，也保证未知步骤**绝不**
 * 把英文 key 直接甩给用户。
 */

/** 后端步骤名 → 中文人话阶段名。与 `app/core/update_steps.py::STEP_ORDER` 名单一致。 */
export const AUTO_UPDATE_STAGE_LABELS: Readonly<Record<string, string>> = {
  trading_dates: '正在更新交易日历',
  prices: '正在更新股票价格',
  indicators: '正在重算指标快照',
  universe: '正在更新股票池与股本',
  announcements: '正在抓取公告',
  financials: '正在更新财务数据',
  market_actions: '正在更新分红除权',
  financial_detail_backfill: '正在回填财务明细',
  buyback: '正在更新回购明细',
  funding: '正在更新融资事件',
  treasury_curve: '正在更新国债曲线',
  cashflow_supplement: '正在补充现金流数据',
  balance_sheet_ext: '正在更新资产负债表扩展',
  deducted_profit: '正在更新扣非净利润',
  fixed_assets: '正在更新固定资产',
  regulatory_metrics: '正在更新监管指标',
  retries: '正在重试失败数据',
  indicator_ext: '正在计算扩展指标',
  roce: '正在计算资本回报率（ROCE）',
  business_overview: '正在更新业务概览',
  capital_history: '正在更新历史股本',
  research_statistics: '正在更新历史统计',
  etf_prices: '正在更新 ETF 行情',
  hk_dividends: '正在更新港股分红',
  index_valuation: '正在更新指数估值',
  analyst_forecast: '正在更新一致预期快照',
  // `report_step` 之外的独立进度事件
  check: '正在检查更新条件',
}

/** `progress.phase` 不是 `step:*` 时的阶段文案。 */
export const AUTO_UPDATE_PHASE_LABELS: Readonly<Record<string, string>> = {
  starting: '正在准备更新',
  done: '数据更新完成',
}

/** 未知步骤的兜底：宁可只说"正在更新数据"，也不显示原始英文 key。 */
export const AUTO_UPDATE_STAGE_FALLBACK = '正在更新数据'

export interface AutoUpdateLiveProgress {
  step?: string
  label?: string
  done?: number
  total?: number
  current?: string | null
  rate_per_minute?: number
  eta_seconds?: number | null
  updated_at?: string
}

export interface AutoUpdateProgress {
  phase?: string
  job_id?: string
  started_at?: string
  status?: string
  reason?: string | null
  steps?: Record<string, string>
  live?: AutoUpdateLiveProgress
  log?: Array<{ t: string; msg: string }>
}

export interface AutoUpdateStatusPayload {
  state?: string
  enabled?: boolean
  paused?: boolean
  current_stage?: string
  progress?: AutoUpdateProgress
  last_error?: string | null
  last_success_at?: string | null
  last_result?: string | null
  last_skip_reason?: string | null
  updated_at?: string | null
  latest_job?: {
    id?: number
    status?: string
    started_at?: string | null
    finished_at?: string | null
  } | null
}

/** `step:indicators` → `indicators`；其它形状原样返回（会被映射兜底吃掉）。 */
export function stepFromPhase(phase?: string | null): string | null {
  if (!phase) return null
  const match = /^step:(.+)$/.exec(phase.trim())
  return match ? match[1] : null
}

/**
 * 阶段中文名。`step` 可以是后端步骤名，也可以是 `progress.phase` 原文
 * （`step:indicators` 会先归一化）。未知值一律返回兜底文案，**不返回英文 key**。
 */
export function stageLabel(step?: string | null): string {
  if (!step) return AUTO_UPDATE_STAGE_FALLBACK
  const raw = step.trim()
  if (!raw) return AUTO_UPDATE_STAGE_FALLBACK
  const fromPhase = stepFromPhase(raw)
  const key = fromPhase ?? raw
  return AUTO_UPDATE_STAGE_LABELS[key]
    ?? AUTO_UPDATE_PHASE_LABELS[key]
    ?? AUTO_UPDATE_STAGE_FALLBACK
}

/**
 * "更新中"的唯一判定：控制器状态或 job_logs 里的作业任一为 running。
 * 后者覆盖"进程重启后控制器先显示 finished/idle，后台作业仍在推进"。
 */
export function isAutoUpdateRunning(status?: AutoUpdateStatusPayload | null): boolean {
  if (!status) return false
  return status.current_stage === 'running' || status.latest_job?.status === 'running'
}

/**
 * 单个更新步骤的**中文状态**（后端 `progress.steps[k]` 是 `success/skipped/failed/...`）。
 * 与 `stageLabel()` 配成一对：步骤名用 `stageLabel(key)`，状态用本函数。
 * 未知状态兜底成"进行中"以外的中性文案，**绝不把原始英文串抛给用户**。
 */
export function updateStepStatusLabel(status?: string | null): string {
  const key = typeof status === 'string' ? status.trim().toLowerCase() : ''
  switch (key) {
    case 'success':
      return '完成'
    case 'partial':
      return '部分完成'
    case 'skipped':
      return '跳过'
    case 'failed':
      return '失败'
    case 'running':
      return '进行中'
    case 'blocked':
      return '受阻'
    case 'pending':
      return '等待中'
    default:
      return key ? '未知状态' : '—'
  }
}

/** 已完成/总数 → 0–100 的整数百分比；没有总数时返回 null（不确定进度）。 */
export function updatePercent(done?: number | null, total?: number | null): number | null {
  const d = Number(done)
  const t = Number(total)
  if (!Number.isFinite(d) || !Number.isFinite(t) || t <= 0) return null
  return Math.min(100, Math.max(0, Math.round((d / t) * 100)))
}

/** 已耗时的中文时长；`startedAt` 非法/在未来时返回空串（宁可不显示也不显示负数）。 */
export function formatElapsed(startedAt?: string | null, nowMs: number = Date.now()): string {
  if (!startedAt) return ''
  const started = Date.parse(startedAt)
  if (!Number.isFinite(started)) return ''
  const seconds = Math.floor((nowMs - started) / 1000)
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 1) return '不到 1 秒'
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const restSeconds = seconds % 60
  if (minutes < 60) {
    return restSeconds > 0 ? `${minutes} 分 ${restSeconds} 秒` : `${minutes} 分钟`
  }
  const hours = Math.floor(minutes / 60)
  const restMinutes = minutes % 60
  return restMinutes > 0 ? `${hours} 小时 ${restMinutes} 分` : `${hours} 小时`
}

/** 预计剩余时长的中文文案；未知返回空串。 */
export function formatEta(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return ''
  const value = Number(seconds)
  if (!Number.isFinite(value) || value < 0) return ''
  if (value < 60) return '不到 1 分钟'
  const minutes = Math.ceil(value / 60)
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest > 0 ? `${hours} 小时 ${rest} 分钟` : `${hours} 小时`
}

export type AutoUpdateViewKind = 'running' | 'completed' | 'failed'

export interface AutoUpdateProgressView {
  kind: AutoUpdateViewKind
  /** 人话标题，如"正在重算指标快照"/"数据更新失败"。 */
  title: string
  /** `2500 / 5575`；没有计数时为 null。 */
  counts: string | null
  /** 0–100；不确定进度时为 null。 */
  percent: number | null
  /** 已耗时中文文案（不带"已耗时"前缀）。 */
  elapsed: string
  eta: string
  current: string | null
  error: string | null
}

function nonEmpty(value?: string | null): string | null {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed ? trimmed : null
}

/**
 * 把一次 auto-update 状态变成可渲染的视图模型。
 *
 * 返回 null = **不该显示任何进度 UI**（空闲/已完成且无错误），调用方据此
 * 决定显隐；这是"只在更新中/失败时显示"的单一判定出口。
 */
export function buildUpdateProgressView(
  status?: AutoUpdateStatusPayload | null,
  nowMs: number = Date.now(),
): AutoUpdateProgressView | null {
  if (!status) return null

  const error = nonEmpty(status.last_error)
  const running = isAutoUpdateRunning(status)
  const live = status.progress?.live
  const failed = status.current_stage === 'failed' || status.progress?.status === 'failed'

  if (running) {
    const step = nonEmpty(live?.step)
      ?? stepFromPhase(status.progress?.phase)
      ?? nonEmpty(status.current_stage)
    const total = Number(live?.total)
    const done = Number(live?.done)
    const hasCounts = Number.isFinite(total) && total > 0 && Number.isFinite(done)
    return {
      kind: 'running',
      title: stageLabel(step),
      counts: hasCounts ? `${Math.max(0, Math.floor(done))} / ${Math.floor(total)}` : null,
      percent: hasCounts ? updatePercent(done, total) : null,
      elapsed: formatElapsed(status.progress?.started_at, nowMs),
      eta: formatEta(live?.eta_seconds),
      current: nonEmpty(live?.current),
      error,
    }
  }

  if (failed) {
    return {
      kind: 'failed',
      title: '数据更新失败',
      counts: null,
      percent: null,
      elapsed: '',
      eta: '',
      current: null,
      error: error ?? '更新失败，后端未提供错误详情',
    }
  }

  return null
}

/** 更新刚结束时展示 30 秒的"更新完成"视图。 */
export function buildCompletedView(): AutoUpdateProgressView {
  return {
    kind: 'completed',
    title: '数据更新完成',
    counts: null,
    percent: 100,
    elapsed: '',
    eta: '',
    current: null,
    error: null,
  }
}
