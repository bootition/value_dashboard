<script setup lang="ts">
import { fieldDisplayName } from '../utils/screening-format.ts'
import { ref, onMounted, computed } from 'vue'
import { NCard, NStatistic, NGrid, NGridItem, NSpin, NEmpty, NTag, NDescriptions, NDescriptionsItem, NButton, NSpace, NDataTable, NAlert, NProgress } from 'naive-ui'
import axios from 'axios'
import type { DataQualityStatus } from '../types'
import { warningCodeLabel } from '../types/data-quality'
import { fmtStoredPercent } from '../utils/formatters.ts'
import { usePollTimer } from '../composables/usePolling.ts'
import { isPackaged } from '../helpers/runtime.ts'
import {
  formatElapsed,
  stageLabel,
  stepFromPhase,
  updateStepStatusLabel,
} from '../helpers/auto-update-progress.ts'

// 打包版 / 开发版的 CLI 前缀不同（见 helpers/runtime.ts）
const packagedRuntime = ref(false)
const cliPrefix = computed(() =>
  packagedRuntime.value ? 'vd' : 'python -m app.cli.main',
)

interface RetryItem {
  stock_code: string
  data_type: string
  adapter: string
  error: string
  retry_count: number
}

interface MissingItem {
  stock_code: string
  field_name: string
  reason_code: string
}

interface DataSummary {
  checking?: boolean
  stale?: boolean
  updating?: boolean
  summary_as_of?: string | null
  stock_count: number
  price_raw_count: number
  price_qfq_count: number
  price_freshness?: { target_date: string | null; active_count: number; raw_current_count: number; qfq_current_count: number; complete_count: number } | null
  price_backfill?: { earliest_date: string | null; latest_date: string | null; stock_count: number; total_rows: number; gap: { no_price: number; incomplete: number; unknown_listing_date: number; complete: number } }
  balance_sheet_count: number
  balance_sheet_range?: { earliest: string | null; latest: string | null }
  income_statement_count: number
  cash_flow_count: number
  indicator_snapshot_count: number
  csrc_industry_count: number
  retry_count: number
  missing_count: number
  last_update: string | null
  last_update_status?: string | null
  last_full_success_at?: string | null
  recent_jobs?: Array<{ finished_at: string; job_type: string; status: string }>
  pdf_tasks?: { cnt: number; pending: number }
  backup?: { cnt: number; latest: string | null; full_count: number }
  dividends?: { total_rows: number; stocks: number; earliest: string | null; latest: string | null; stock_dividend_filled: number; transfer_share_filled: number; rights_issue_filled: number } | null
  xdxr?: { total_rows: number; stocks: number; earliest: string | null; latest: string | null } | null
  share_capital?: { latest_updated: string | null; with_shares: number; with_circ_shares: number } | null
  listing_info?: { stock_list_refreshed_at: string | null; listing_info_refreshed_at: string | null } | null
  csrc_industry_refresh?: { last_refresh: string | null } | null
  readonly data_quality: DataQualityStatus
}

interface IndicatorRecomputeStatus {
  status: string
  pending_codes: string[]
  result?: { status?: string; success?: number; failed?: number; failed_codes?: string[] } | null
  error?: string | null
  started_at?: string | null
  finished_at?: string | null
}

interface AutoUpdateStatus {
  state: string
  enabled: boolean
  paused: boolean
  current_stage: string
  progress: {
    phase?: string
    job_id?: string
    started_at?: string
    status?: string
    steps?: Record<string, string>
    live?: {
      step: string
      label: string
      done: number
      total: number
      current?: string
      rate_per_minute?: number
      eta_seconds?: number | null
      updated_at?: string
    }
    log?: Array<{ t: string; msg: string }>
  }
  last_error: string | null
  last_success_at: string | null
  last_result?: string | null
  last_skip_reason?: string | null
  updated_at?: string | null
  latest_job?: {
    id: number
    status: string
    started_at: string | null
    finished_at: string | null
  } | null
}

// 控制器状态可能已显示 finished/idle，但 job_logs 里仍有 running 作业；
// 前端必须把两者合并判断，否则会看到“界面没进度，后台还在跑”。
const effectiveRunning = computed(() =>
  autoUpdate.value?.current_stage === 'running'
  || autoUpdate.value?.latest_job?.status === 'running'
)

const updateLive = computed(() => autoUpdate.value?.progress?.live || null)
// 阶段/步骤名一律走中文映射：后端 `current_stage` 是 `running`、`progress.phase` 是
// `step:indicators`、`progress.steps` 的键是 `prices/universe/...` —— 直接显示就是英文，
// 用户看不懂（2026-11-21 用户明确要求"数据中心界面能看到正在做什么"）。
const updateStageLabel = computed(() => {
  const status = autoUpdate.value
  const step = stepFromPhase(status?.progress?.phase) || status?.current_stage || ''
  return stageLabel(step)
})
const updateStepChips = computed(() => {
  const steps = (autoUpdate.value?.progress?.steps ?? {}) as Record<string, string>
  return Object.entries(steps).map(([key, value]) => ({
    key,
    // 步骤已结束时用"更新股票价格·完成"而不是"正在更新股票价格·完成"
    // （进行时态 + 完成态拼在一起自相矛盾）。
    label: stageLabel(key).replace(/^正在/, ''),
    statusLabel: updateStepStatusLabel(value),
    status: String(value),
  }))
})
/**
 * 步骤标签的颜色（2026-11-22 用户要求：「如果不需要继续做了，那就直接跳成绿色，
 * 为什么还要留成灰色？」）。
 *
 * 口径：**只有真失败是红、部分完成是黄**；其余状态（完成 / 跳过 / 顺延下一轮 /
 * 源侧无数据 …）全都是**绿的 "已完成"**——因为它们都表示"这轮对它没有待办"，
 * 灰色会让人以为"没做完"。
 */
function stepChipType(status: string): 'success' | 'warning' | 'error' {
  const key = (status || '').toLowerCase()
  if (key === 'failed' || key === 'error' || key === 'blocked') return 'error'
  if (key === 'partial') return 'warning'
  return 'success'
}

const updateElapsedLabel = computed(() =>
  formatElapsed(autoUpdate.value?.progress?.started_at ?? null),
)
const updateLog = computed(() => autoUpdate.value?.progress?.log || [])
const updateLivePct = computed(() => {
  const live = updateLive.value
  if (!live || !live.total) return 0
  return Math.min(Math.round((live.done / live.total) * 100), 100)
})

const updateEtaLabel = computed(() => {
  const seconds = updateLive.value?.eta_seconds
  if (seconds === null || seconds === undefined) return ''
  if (seconds < 60) return '不到 1 分钟'
  const minutes = Math.ceil(seconds / 60)
  if (minutes < 60) return `约 ${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `约 ${hours} 小时 ${rest} 分钟` : `约 ${hours} 小时`
})

const summary = ref<DataSummary | null>(null)
const loading = ref(true)
const error = ref('')
const retryList = ref<RetryItem[]>([])
const missingList = ref<MissingItem[]>([])
const autoUpdate = ref<AutoUpdateStatus | null>(null)
const recompute = ref<IndicatorRecomputeStatus>({ status: 'idle', pending_codes: [] })
const lastRefreshedAt = ref<string | null>(null)
let recomputeStartInFlight = false
// 三套轮询（自动更新状态 / 指标重算 / 重量摘要）各自一个 timer，
// 生命周期（清旧+设新+卸载清理）统一由 composables/usePolling.ts 提供。
const recomputePoll = usePollTimer()
const autoPoll = usePollTimer()
const heavyPoll = usePollTimer()
// 运维级重算令牌：不再从 /api/session 下发，由用户输入并保存在会话内。
const recomputeAdminToken = ref(sessionStorage.getItem('vd-admin-token') || '')
// 轻量轮询：仅自动更新状态；in-flight guard + 递归定时，防堆积与卡死
let autoInFlight = false
const refreshing = ref(false)

// running 时 4s 高频轮询；空闲/完成后 60s 低频轮询，
// 让自动更新启动/结束、stale 提示变化无需手动刷新即可看到。
// 2026-08-14 红队 P3：不再恒真——由自动更新实际阶段派生。
const isPolling = computed(() => effectiveRunning.value)

const pct = (value: number, total: number) => {
  if (!total || total === 0) return '0%'
  // Detail tables contain multiple report periods per company. Coverage is capped
  // at the listed-stock universe rather than rendering misleading values >100%.
  return fmtStoredPercent(Math.min(value, total) / total * 100, 1)
}

const priceRawPct = computed(() => summary.value ? pct(summary.value.price_raw_count, summary.value.stock_count) : '0%')
const priceQfqPct = computed(() => summary.value ? pct(summary.value.price_qfq_count, summary.value.stock_count) : '0%')
const csrcIndustryPct = computed(() => summary.value ? pct(summary.value.csrc_industry_count, summary.value.stock_count) : '0%')
const indicatorSnapshotPct = computed(() => summary.value ? pct(summary.value.indicator_snapshot_count, summary.value.stock_count) : '0%')

// 后端时间均为 UTC ISO 字符串，展示为本地时间

// 轻量路径：只拉自动更新状态，递归轮询，绝不阻塞于 summary。
async function fetchAutoOverview(): Promise<void> {
  if (autoInFlight) return
  autoInFlight = true
  const wasRunning = effectiveRunning.value
  try {
    const autoResp = await axios.get('/api/data-status/auto-update', { timeout: 8000 })
    autoUpdate.value = autoResp.data
    lastRefreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    const stillRunning = effectiveRunning.value
    // 自动更新刚结束时，若有数据修复登记的快照 pending 股票，自动触发重算。
    // 无需用户点按钮，也不需要任何终端命令（2026-08-28 提速计划）。
    if (wasRunning && !stillRunning) {
      void fetchRecomputeStatus().then(() => {
        void maybeStartPendingRecompute()
      })
      // 后端重量摘要 TTL 已放宽到 5 分钟：更新刚结束这一刻主动刷新一次，
      // 让 stale 提示立即消失，而不必等下一轮低频轮询。
      void fetchHeavy(true)
    }
  } catch {
    // 保留上次状态，不打断轮询链
  } finally {
    autoInFlight = false
    schedulePolling()
  }
}

async function fetchRecomputeStatus(): Promise<void> {
  try {
    const resp = await axios.get('/api/data-status/indicator-recompute', { timeout: 8000 })
    const data = resp.data || {}
    recompute.value = {
      ...data,
      status: typeof data.status === 'string' ? data.status : 'idle',
      pending_codes: Array.isArray(data.pending_codes) ? data.pending_codes : [],
    }
  } catch {
    // 后端未重启时接口尚不存在，静默降级
  }
}

async function maybeStartPendingRecompute(): Promise<void> {
  if (
    recompute.value.status !== 'idle'
    || recompute.value.pending_codes.length === 0
    || autoUpdate.value?.current_stage === 'running'
    || recomputeStartInFlight
    || !recomputeAdminToken.value
  ) {
    return
  }
  recomputeStartInFlight = true
  try {
    const resp = await axios.post('/api/data-status/indicator-recompute', null, {
      headers: { 'x-vd-admin-token': recomputeAdminToken.value },
      timeout: 8000,
    })
    recompute.value = resp.data
    if (recompute.value.status === 'running') scheduleRecomputePolling()
  } catch {
    // 409（自动更新恰好在开始瞬间启动）、403（令牌失效）或接口未就绪时
    // 保留原状态；下一次状态轮询会再次尝试。
  } finally {
    recomputeStartInFlight = false
  }
}

async function startRecompute(): Promise<void> {
  // 手动点击允许在 failed/skipped 状态后重试；自动路径仍只从 idle 触发。
  if (
    recompute.value.status === 'running'
    || autoUpdate.value?.current_stage === 'running'
    || recomputeStartInFlight
  ) {
    return
  }
  if (!recomputeAdminToken.value) {
    const entered = window.prompt('请输入运维令牌（保存在 data 目录 .vd-admin-token）')
    if (!entered) return
    recomputeAdminToken.value = entered.trim()
    sessionStorage.setItem('vd-admin-token', recomputeAdminToken.value)
  }
  recomputeStartInFlight = true
  try {
    const resp = await axios.post('/api/data-status/indicator-recompute', null, {
      headers: { 'x-vd-admin-token': recomputeAdminToken.value },
      timeout: 8000,
    })
    recompute.value = resp.data
    if (recompute.value.status === 'running') scheduleRecomputePolling()
  } catch (error) {
    // 403：令牌可能随服务重启失效，清掉后让用户重新输入。
    if (axios.isAxiosError(error) && error.response?.status === 403) {
      recomputeAdminToken.value = ''
      sessionStorage.removeItem('vd-admin-token')
    }
    // 409（自动更新恰好在开始瞬间启动）或接口未就绪时保留原状态。
  } finally {
    recomputeStartInFlight = false
  }
}

function scheduleRecomputePolling() {
  recomputePoll.schedule(4000, async () => {
    await fetchRecomputeStatus()
    if (recompute.value.status === 'running') {
      scheduleRecomputePolling()
    } else {
      void fetchHeavy(true)
    }
  })
}

function schedulePolling() {
  const running = effectiveRunning.value
  autoPoll.schedule(running ? 4000 : 60000, () => void fetchAutoOverview())
}

// 空闲时低频刷新重量摘要（stale 提示会在自动更新结束后自动消失）
function scheduleHeavyPolling() {
  const running = effectiveRunning.value
  // 空闲态重量摘要每 5 分钟才刷一次（后端 TTL 同为 300s）；
  // 更新运行中仍 15s 一次，保证进度结束后页面能及时收敛。
  heavyPoll.schedule(running ? 15000 : 300000, () => {
    void fetchHeavy(true)
    scheduleHeavyPolling()
  })
}

// 重量级只读摘要：summary/retry/missing 设 15s 超时，失败仅出错误提示，不阻塞自动更新卡。
async function fetchHeavy(silent = false): Promise<void> {
  if (silent) {
    if (refreshing.value) return
    refreshing.value = true
  } else {
    error.value = ''
    loading.value = true
  }
  try {
    const [sumResp, retryResp, missResp] = await Promise.all([
      axios.get('/api/data-status/summary', { timeout: 15000 }),
      axios.get('/api/data-status/retry-list', { timeout: 15000 }),
      axios.get('/api/data-status/missing-list', { timeout: 15000 }),
    ])
    summary.value = sumResp.data
    retryList.value = retryResp.data.items || []
    missingList.value = missResp.data.items || []
    // stale 不是错误：上面有专门的“更新运行中”提示，避免红/黄/绿同时出现。
    error.value = ''
  } catch (e: unknown) {
    if (!silent) error.value = axios.isAxiosError(e) ? e.message : (e instanceof Error ? e.message : '加载失败')
    else if (!summary.value) error.value = '数据摘要暂时不可用'
  } finally {
    if (!silent) loading.value = false
    refreshing.value = false
  }
}

function handleRefresh() {
  void fetchAutoOverview()
  void fetchHeavy(true)
}

onMounted(() => {
  // C3（报告41）：打包版 exe 的入口是 `vd`，开发版是 `python -m app.cli.main`。
  // 以前这里硬编码了开发版命令，打包版用户照着敲会失败。
  void isPackaged().then((p) => { packagedRuntime.value = p })
  void fetchAutoOverview()
  void fetchHeavy()
  scheduleHeavyPolling()
  void fetchRecomputeStatus().then(() => {
    if (recompute.value.status === 'running') {
      scheduleRecomputePolling()
    } else {
      // 数据修复后遗留的 pending 股票：进入页面即自动重算，无需终端命令
      void maybeStartPendingRecompute()
    }
  })
})
// 卸载清理不再写在这里：usePollTimer 内 onScopeDispose(cancel) 必然执行，
// 三套 timer 一处收敛，避免"加第 4 套轮询时忘了在 onBeforeUnmount 里补一行"。

function autoUpdateStateLabel(state: string): string {
  const labels: Record<string, string> = {
    idle: '空闲', running: '运行中', paused: '已暂停',
    disabled: '已关闭', finished: '已完成', failed: '失败',
    enabled: '已开启',
  }
  return labels[state] || state
}

function autoUpdateTagType(state: string): 'success' | 'warning' | 'error' | 'info' | 'default' {
  if (state === 'running') return 'info'
  if (state === 'finished') return 'success'
  if (state === 'failed') return 'error'
  if (state === 'paused' || state === 'disabled') return 'warning'
  return 'default'
}


</script>

<template>
  <section class="data-status-page">
    <n-space align="center" justify="space-between" class="data-status-header" wrap>
      <div><p>研究工具 / 数据状态</p><h1>数据状态</h1><span>确认研究数据的时间范围、覆盖与质量。</span></div>
      <n-space align="center" size="small">
        <!-- L1-3（报告42）: 显示上次刷新时间与自动轮询状态 -->
        <span v-if="lastRefreshedAt" style="color:#999; font-size:12px;">上次刷新 {{ lastRefreshedAt }}</span>
        <n-tag v-if="isPolling" size="small" type="info">更新运行中，每 4 秒自动刷新</n-tag>
        <n-button
          v-if="recompute.pending_codes.length > 0 || recompute.status !== 'idle'"
          :loading="recompute.status === 'running'"
          :disabled="autoUpdate?.current_stage === 'running'"
          @click="startRecompute"
        >
          {{ recompute.status === 'running' ? '正在重算指标快照…' : `重算指标快照（${recompute.pending_codes.length}）` }}
        </n-button>
        <n-button :loading="refreshing" @click="handleRefresh">刷新</n-button>
      </n-space>
    </n-space>
    <n-spin :show="loading && !summary">
      <n-alert v-if="error" type="error" title="提示" style="margin-bottom: 16px;">
        {{ error }}
      </n-alert>
      <template v-if="summary">
        <!-- L2 V3: 第一问"数据可研究吗" -->
        <!-- 2026-11-21 用户要求：更新中**不再**弹"以下为快照口径"的黄色提示。
             就绪态只反映数据本身可不可研究（绿/红），更新进行中由"自动更新"卡片里的
             中文进度说明，不再在前置位置重复告警。 -->
        <n-alert
          :type="
            summary.data_quality.minimum_data_readiness.checking ? 'info'
            : summary.data_quality.minimum_data_readiness.ready ? 'success'
            : 'error'
          "
          :show-icon="true"
          class="data-readiness"
        >
          <template #header>
            {{
              summary.data_quality.minimum_data_readiness.checking
                ? '正在核对数据'
                : summary.data_quality.minimum_data_readiness.ready
                    ? '数据可研究'
                    : '数据尚未就绪'
            }}
          </template>
          {{
            summary.data_quality.minimum_data_readiness.checking
              ? '正在后台核对数据完整性，完成后会显示最终状态。'
              : summary.data_quality.minimum_data_readiness.ready
                  ? `数据可研究：价格截至 ${summary.data_quality.dates?.price || '—'}，财报报告期截至 ${summary.data_quality.dates?.balance_sheet?.latest_complete || '—'}。`
                  : `数据尚未完全就绪，有 ${summary.data_quality.warning_codes.length} 个警告；当前更新完成后会自动恢复。`
          }}
        </n-alert>

        <!-- 2026-11-21 用户要求：不显示"历史完成结果"。只保留数据新鲜度的硬事实（实际价格日期）。 -->
        <p v-if="summary.data_quality.dates?.price" style="color: #999; margin-bottom: 16px;">
          实际价格日期: {{ summary.data_quality.dates.price }}
        </p>
      </template>

        <section class="status-workbench">
        <!-- 自动更新状态（PRD §7.3 只读展示） -->
        <n-card title="自动更新" size="small" v-if="autoUpdate">
          <n-descriptions bordered :column="4" size="small">
            <n-descriptions-item label="状态">
              <n-tag :type="autoUpdateTagType(autoUpdate.state)" size="small">
                {{ autoUpdateStateLabel(autoUpdate.state) }}
              </n-tag>
              <span
                v-if="autoUpdate.enabled && !autoUpdate.paused && autoUpdate.state !== 'running'"
                style="color:#999; margin-left:8px;"
              >（启动后自动更新）</span>
            </n-descriptions-item>
            <n-descriptions-item label="当前阶段">
              <strong>{{ updateStageLabel }}</strong>
            </n-descriptions-item>
            <n-descriptions-item label="步骤状态">
              <n-space v-if="updateStepChips.length" size="small">
                <n-tag
                  v-for="chip in updateStepChips"
                  :key="chip.key"
                  size="small"
                  :type="stepChipType(chip.status)"
                >
                  {{ chip.label }}·{{ chip.statusLabel }}
                </n-tag>
              </n-space>
              <span v-else style="color:#999">—</span>
            </n-descriptions-item>
            <n-descriptions-item label="作业ID">{{ autoUpdate.progress?.job_id || '—' }}</n-descriptions-item>
            <!-- 2026-11-21 用户要求：**删掉"最近执行 / 上次成功 / 最近结果 / 最后错误"这一整行**
                 ——「我不需要看到历史完成的结果，只需要看到正在做的就行」。
                 唯一例外：**真出错时**仍要看得见（错误是"现在有问题"，不是历史成绩），
                 所以改成下面那条只在 last_error 非空时出现的红色提示。 -->
          </n-descriptions>
          <n-alert
            v-if="autoUpdate.last_error && !effectiveRunning"
            id="auto-update-last-error"
            type="error"
            :show-icon="true"
            style="margin-top: 12px"
            title="上次更新失败"
          >
            {{ autoUpdate.last_error }}
          </n-alert>
          <n-alert
            v-if="effectiveRunning && autoUpdate.current_stage !== 'running'"
            type="info"
            :show-icon="true"
            style="margin-top: 12px"
            title="后台更新作业仍在运行"
          >
            界面控制器尚未刷新到 running 状态，但 job_logs 中的更新作业
            #{{ autoUpdate.latest_job?.id }} 仍在推进；本页会持续轮询。
          </n-alert>
          <!-- 更新进度：**只要在跑就显示**（2026-11-21 用户要求"数据中心界面能看到正在做什么"）。
               有逐股计数时用实进度条；没有计数（公告/财务等无总数的步骤）用不确定进度条，
               阶段名照样是中文 —— 不再出现"只有指标步才看得见"的空窗。 -->
          <div v-if="effectiveRunning" id="update-progress" class="update-live">
            <div class="update-live-title">
              <n-tag size="small" type="info">{{ updateStageLabel }}</n-tag>
              <span v-if="updateLive && updateLive.total">
                {{ updateLive.done }} / {{ updateLive.total }} · {{ updateLivePct }}%
              </span>
              <span v-else style="color:#999">处理中…</span>
            </div>
            <n-progress
              v-if="updateLive && updateLive.total"
              type="line"
              :percentage="updateLivePct"
              :height="10"
              :show-indicator="false"
              processing
            />
            <n-progress
              v-else
              type="line"
              :percentage="0"
              :height="10"
              :show-indicator="false"
              processing
            />
            <p class="update-live-hint">
              <span v-if="updateLive?.current">正在更新 {{ updateLive.current }}</span>
              <span v-else>正在执行「{{ updateStageLabel }}」，该步骤没有逐股计数</span>
              <span v-if="updateLive?.rate_per_minute">· {{ updateLive.rate_per_minute }} 股/分</span>
              <span v-if="updateEtaLabel">· 预计剩余 {{ updateEtaLabel }}</span>
              <span v-if="updateElapsedLabel">· 已耗时 {{ updateElapsedLabel }}</span>
            </p>
          </div>
          <!-- 更新日志（最近 10 条） -->
          <div v-if="updateLog.length" class="update-log">
            <div class="update-log-title">更新日志</div>
            <ol class="update-log-list">
              <li v-for="(entry, idx) in updateLog.slice(-10)" :key="idx">
                <span class="update-log-time">{{ entry.t }}</span>{{ entry.msg }}
              </li>
            </ol>
          </div>
          <p v-if="autoUpdate.state === 'disabled'" style="color:#999; margin:8px 0 0;">
            自动更新已关闭。可在 CLI 执行 <code>vd data auto-update enable</code> 重新开启。
          </p>
          <p v-else style="color:#999; margin:8px 0 0;">
            控制入口（开关/立即更新/暂停/继续）在 CLI：
            <code>vd data auto-update status|enable|disable|run|pause|resume</code>
          </p>
        </n-card>
        <template v-if="summary && !summary.checking">
        <!-- 数据质量警告 -->
        <n-alert
          v-if="summary.data_quality.warning_codes.length > 0"
          type="warning"
          style="margin-bottom: 16px;"
        >
          <template #header>
            数据质量警告（{{ summary.data_quality.warning_codes.length }}）
          </template>
          <n-space vertical :size="8">
            <n-space align="center" :size="8">
              <span>警告代码：</span>
              <n-tag
                v-for="code in summary.data_quality.warning_codes"
                :key="code"
                type="warning"
                size="small"
                :title="code"
              >
                {{ warningCodeLabel(code) }}
              </n-tag>
            </n-space>
            <n-descriptions bordered :column="2" size="small" label-placement="left">
              <n-descriptions-item label="价格日期">
                {{ summary.data_quality.dates.price || '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="资产负债表最新完整期">
                {{ summary.data_quality.dates.balance_sheet.latest_complete || '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="利润表最新完整期">
                {{ summary.data_quality.dates.income_statement.latest_complete || '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="现金流量表最新完整期">
                {{ summary.data_quality.dates.cash_flow.latest_complete || '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="指标快照最新完整期">
                {{ summary.data_quality.dates.indicator_snapshot.latest_complete || '—' }}
              </n-descriptions-item>
              <n-descriptions-item label="指标快照计算时间">
                {{ summary.data_quality.dates.indicator_snapshot.calculated_at || '—' }}
              </n-descriptions-item>
            </n-descriptions>
          </n-space>
        </n-alert>

        <section class="coverage-grid" aria-label="数据覆盖概览">
          <article><p>当前上市股票</p><strong>{{ summary.stock_count.toLocaleString('zh-CN') }}</strong><span>筛选基础股票池</span></article>
          <article><p>原始价格覆盖</p><strong>{{ summary.price_raw_count.toLocaleString('zh-CN') }}</strong><span>覆盖率 {{ priceRawPct }}</span></article>
          <article><p>前复权价格覆盖</p><strong>{{ summary.price_qfq_count.toLocaleString('zh-CN') }}</strong><span>覆盖率 {{ priceQfqPct }}</span></article>
          <article><p>证监会行业分类</p><strong>{{ summary.csrc_industry_count.toLocaleString('zh-CN') }}</strong><span>覆盖率 {{ csrcIndustryPct }}</span></article>
          <article><p>资产负债表记录</p><strong>{{ summary.balance_sheet_count.toLocaleString('zh-CN') }}</strong><span>多个报告期，非公司覆盖率</span></article>
          <article><p>利润表记录</p><strong>{{ summary.income_statement_count.toLocaleString('zh-CN') }}</strong><span>多个报告期，非公司覆盖率</span></article>
          <article><p>现金流量表记录</p><strong>{{ summary.cash_flow_count.toLocaleString('zh-CN') }}</strong><span>多个报告期，非公司覆盖率</span></article>
          <article><p>指标快照</p><strong>{{ summary.indicator_snapshot_count.toLocaleString('zh-CN') }}</strong><span>覆盖率 {{ indicatorSnapshotPct }}</span></article>
        </section>

        <!-- 回填状态 -->
        <n-card title="价格回填状态" size="small" v-if="summary.price_backfill">
          <n-descriptions bordered :column="4" size="small">
            <n-descriptions-item label="最早日期">{{ summary.price_backfill.earliest_date || '—' }}</n-descriptions-item>
            <n-descriptions-item label="最新日期">{{ summary.price_backfill.latest_date || '—' }}</n-descriptions-item>
            <n-descriptions-item label="覆盖股票">{{ summary.price_backfill.stock_count }}</n-descriptions-item>
            <n-descriptions-item label="总行数">{{ summary.price_backfill.total_rows }}</n-descriptions-item>
            <n-descriptions-item label="上市日未知">{{ summary.price_backfill.gap.unknown_listing_date }}</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <!-- 财务覆盖范围 -->
        <n-card title="财务报表覆盖范围" size="small" v-if="summary.balance_sheet_range">
          <n-descriptions bordered :column="3" size="small">
            <n-descriptions-item label="资产负债表">{{ summary.balance_sheet_range?.earliest }} ~ {{ summary.balance_sheet_range?.latest }}</n-descriptions-item>
            <n-descriptions-item label="利润表">{{ summary.income_statement_count }} 只</n-descriptions-item>
            <n-descriptions-item label="现金流量表">{{ summary.cash_flow_count }} 只</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <n-card title="公司行动与分红" size="small">
          <n-descriptions bordered :column="4" size="small">
            <n-descriptions-item label="分红记录">{{ summary.dividends?.total_rows ?? 0 }}</n-descriptions-item>
            <n-descriptions-item label="分红覆盖股票">{{ summary.dividends?.stocks ?? 0 }}</n-descriptions-item>
            <n-descriptions-item label="分红日期范围">{{ summary.dividends ? `${summary.dividends.earliest || '—'} ~ ${summary.dividends.latest || '—'}` : '—' }}</n-descriptions-item>
            <n-descriptions-item label="除权除息记录">{{ summary.xdxr?.total_rows ?? 0 }}（{{ summary.xdxr?.stocks ?? 0 }} 只）</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <!-- PRD §6.4/§15: 各数据域最新日期 -->
        <n-card title="各数据域最新日期" size="small">
          <n-descriptions bordered :column="3" size="small">
            <n-descriptions-item label="价格日期">
              {{ summary.data_quality.dates.price || '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="价格更新覆盖">
              {{ summary.price_freshness
                ? `${summary.price_freshness.complete_count}/${summary.price_freshness.active_count}（目标 ${summary.price_freshness.target_date || '—'}）`
                : '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="财报最新完整期">
              {{ summary.data_quality.dates.balance_sheet.latest_complete || '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="分红最新日期">
              {{ summary.dividends?.latest || '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="除权除息最新日期">
              {{ summary.xdxr?.latest || '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="股本/上市名单更新">
              {{ summary.share_capital?.latest_updated || '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="CSRC 行业刷新">
              {{ summary.csrc_industry_refresh?.last_refresh || '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="股票池刷新">
              {{ summary.listing_info?.stock_list_refreshed_at || '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="上市状态刷新">
              {{ summary.listing_info?.listing_info_refreshed_at || '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="股本字段覆盖">
              {{ summary.share_capital ? `${summary.share_capital.with_shares}/${summary.stock_count}` : '—' }}
            </n-descriptions-item>
          </n-descriptions>
        </n-card>

        <!-- 任务状态 -->
        <n-grid :cols="3" :x-gap="16">
          <n-grid-item>
            <n-card size="small">
              <n-statistic label="待重试" :value="summary.retry_count">
                <template #suffix><n-tag v-if="summary.retry_count > 0" type="warning" size="small">需关注</n-tag></template>
              </n-statistic>
            </n-card>
          </n-grid-item>
          <n-grid-item>
            <n-card size="small">
              <n-statistic label="缺失字段" :value="summary.missing_count">
                <template #suffix><n-tag v-if="summary.missing_count > 0" type="info" size="small">记录中</n-tag></template>
              </n-statistic>
            </n-card>
          </n-grid-item>
          <n-grid-item>
            <n-card size="small">
              <n-statistic label="PDF失败任务" :value="summary.pdf_tasks?.pending || 0">
                <template #suffix><n-tag v-if="(summary.pdf_tasks?.pending || 0) > 0" type="error" size="small">待处理</n-tag></template>
              </n-statistic>
            </n-card>
          </n-grid-item>
        </n-grid>

        <!-- 备份摘要 -->
        <n-card title="备份摘要" size="small" v-if="summary.backup">
          <n-descriptions bordered :column="3" size="small">
            <n-descriptions-item label="备份总数">{{ summary.backup?.cnt || 0 }}</n-descriptions-item>
            <n-descriptions-item label="全量备份">{{ summary.backup?.full_count || 0 }}</n-descriptions-item>
            <n-descriptions-item label="最近备份">{{ summary.backup?.latest || '—' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <!-- 重试列表 -->
        <n-card title="重试列表" size="small" v-if="retryList.length > 0">
          <n-data-table
            size="small"
            striped
            :columns="[
              {title:'股票',key:'stock_code',width:100},
              {title:'数据类型',key:'data_type',width:120},
              {title:'适配器',key:'adapter',width:120},
              {title:'错误',key:'error'},
              {title:'重试次数',key:'retry_count',width:80},
            ]"
            :data="retryList"
            :pagination="{ pageSize: 10 }"
          />
        </n-card>

        <!-- 缺失列表 -->
        <n-card title="缺失列表" size="small" v-if="missingList.length > 0">
          <n-alert type="info" :show-icon="false" style="margin-bottom: 12px;">
            金融监管指标（资本充足率/不良率等）与北交所增发配股当前无免费数据源，
            已从本列表隐藏并保留存档；待接入公告 PDF 解析或新数据源后再启用。
          </n-alert>
          <n-data-table
            size="small"
            striped
            :columns="[
              {title:'股票',key:'stock_code',width:100},
              // 2026-11-16（reports/148）：缺失字段列表必须显示中文名，
              // 否则用户看到的是 `cf_from_operating_indirect` 这种英文列名。
              {title:'字段',key:'field_name',width:220,
               render: (row: MissingItem) => fieldDisplayName(row.field_name, row.field_name)},
              {title:'原因码',key:'reason_code',width:120},
            ]"
            :data="missingList"
            :pagination="{ pageSize: 10 }"
          />
        </n-card>

        <n-card v-if="summary.stock_count === 0">
          <n-empty :description="`尚未初始化数据。请运行: ${cliPrefix} data init`" />
        </n-card>
        </template>
        </section>
    </n-spin>
  </section>
</template>

<style scoped>
.data-status-page { max-width: 1380px; }.data-status-header { margin-bottom: 27px; }.data-status-header p { margin: 0 0 8px; color: #97a199; font-size: 10px; }.data-status-header h1 { margin: 0; font-size: 25px; letter-spacing: -.05em; }.data-status-header span { display: block; margin-top: 7px; color: #829087; font-size: 12px; }.data-readiness { margin-bottom: 21px; border-radius: 16px; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.coverage-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 21px; }.coverage-grid article { padding: 19px 20px; border-radius: 14px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.coverage-grid p, .coverage-grid span { margin: 0; color: #8b978f; font-size: 10px; }.coverage-grid strong { display: block; margin: 7px 0 4px; color: #3c5847; font-size: 22px; font-variant-numeric: tabular-nums; letter-spacing: -.04em; }.status-workbench { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }.status-workbench > :deep(.n-grid), .status-workbench > :deep(.n-card):nth-child(1), .status-workbench > :deep(.n-card):nth-child(6), .status-workbench > :deep(.n-card):nth-child(7), .status-workbench > :deep(.n-card):nth-child(8), .status-workbench > :deep(.n-card):nth-child(9) { grid-column: 1 / -1; }.data-status-page :deep(.n-card) { border-radius: 16px; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.data-status-page :deep(.n-card-header) { padding-top: 20px; }.data-status-page :deep(.n-card__content) { padding-bottom: 20px; }
.update-live { margin-top: 14px; padding: 12px 14px; border: 1px solid #e3ebe5; border-radius: 9px; background: #f8fbf8; }.update-live-title { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }.update-live-title span { color: #4c6a58; font-size: 12px; font-variant-numeric: tabular-nums; }.update-live-hint { margin: 7px 0 0; color: #7d8a82; font-size: 10px; }.update-log { margin-top: 12px; }.update-log-title { margin-bottom: 6px; color: #8b978f; font-size: 9px; font-weight: 700; letter-spacing: .08em; }.update-log-list { margin: 0; padding: 0; list-style: none; max-height: 150px; overflow-y: auto; border: 1px solid #edf1ee; border-radius: 8px; }.update-log-list li { display: flex; gap: 8px; padding: 5px 10px; border-bottom: 1px solid #f1f4f1; color: #5f6b63; font-size: 10px; font-variant-numeric: tabular-nums; }.update-log-list li:last-child { border-bottom: 0; }.update-log-time { flex: 0 0 58px; color: #a0aaa3; }
.coverage-grid article { position: relative; padding: 18px 20px 16px; border: 1px solid #dfe6e1; border-top: 3px solid #86b798; border-radius: 12px; background: #fff; box-shadow: 0 3px 10px rgba(48, 82, 59, .04); }.coverage-grid article::after { content: ''; position: absolute; left: 0; right: 0; top: 0; bottom: 0; border-radius: 12px; pointer-events: none; }.coverage-grid p { margin: 0; color: #55685c; font-size: 10px; font-weight: 700; letter-spacing: .05em; }.coverage-grid strong { display: block; margin: 9px 0 5px; color: #27432f; font-size: 26px; font-weight: 760; font-variant-numeric: tabular-nums; letter-spacing: -.04em; }.coverage-grid span { color: #87948b; font-size: 10px; }
.data-status-page :deep(.n-descriptions-table) { width: 100%; border-collapse: collapse; }.data-status-page :deep(.n-descriptions-table td) { border: 1px solid #e6ebe7; padding: 6px 10px; vertical-align: middle; }.data-status-page :deep(.n-descriptions-table td:first-child), .data-status-page :deep(.n-descriptions-item-label) { background: #f6f9f6; color: #516257; font-weight: 700; font-size: 11px; white-space: nowrap; }.data-status-page :deep(.n-descriptions-item-content) { color: #2c4134; font-weight: 600; font-size: 12px; font-variant-numeric: tabular-nums; }.data-status-page :deep(.n-statistic-value-content) { color: #2c4436; font-weight: 760; font-variant-numeric: tabular-nums; }.data-status-page :deep(.n-statistic .n-statistic-label) { color: #6a7d70; font-weight: 600; }.data-status-workbench-card-title, .data-status-page :deep(.n-card-header .n-card-header__main) { color: #33493c; font-weight: 700; }
.update-live { border-top-color: #7fb18f; }.update-live { margin-top: 14px; padding: 12px 14px; border: 1px solid #dbe6df; border-top: 3px solid #7fb18f; border-radius: 9px; background: #f6faf6; }
</style>
