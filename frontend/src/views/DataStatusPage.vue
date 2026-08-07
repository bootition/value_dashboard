<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { NCard, NStatistic, NGrid, NGridItem, NSpin, NEmpty, NTag, NDescriptions, NDescriptionsItem, NButton, NSpace, NDataTable, NAlert, NProgress } from 'naive-ui'
import axios from 'axios'
import type { DataQualityStatus } from '../types'

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
      updated_at?: string
    }
    log?: Array<{ t: string; msg: string }>
  }
  last_error: string | null
  last_success_at: string | null
  last_result?: string | null
  last_skip_reason?: string | null
  updated_at?: string | null
}

const updateLive = computed(() => autoUpdate.value?.progress?.live || null)
const updateLog = computed(() => autoUpdate.value?.progress?.log || [])
const updateLivePct = computed(() => {
  const live = updateLive.value
  if (!live || !live.total) return 0
  return Math.min(Math.round((live.done / live.total) * 100), 100)
})

const summary = ref<DataSummary | null>(null)
const loading = ref(true)
const error = ref('')
const retryList = ref<RetryItem[]>([])
const missingList = ref<MissingItem[]>([])
const autoUpdate = ref<AutoUpdateStatus | null>(null)
const lastRefreshedAt = ref<string | null>(null)
// 轻量轮询：仅自动更新状态；in-flight guard + 递归定时，防堆积与卡死
let pollTimer: ReturnType<typeof setTimeout> | undefined
let autoInFlight = false
const refreshing = ref(false)

const isPolling = computed(() => autoUpdate.value?.current_stage === 'running')

const pct = (value: number, total: number) => {
  if (!total || total === 0) return '0%'
  // Detail tables contain multiple report periods per company. Coverage is capped
  // at the listed-stock universe rather than rendering misleading values >100%.
  return (Math.min(value, total) / total * 100).toFixed(1) + '%'
}

const priceRawPct = computed(() => summary.value ? pct(summary.value.price_raw_count, summary.value.stock_count) : '0%')
const priceQfqPct = computed(() => summary.value ? pct(summary.value.price_qfq_count, summary.value.stock_count) : '0%')
const csrcIndustryPct = computed(() => summary.value ? pct(summary.value.csrc_industry_count, summary.value.stock_count) : '0%')
const indicatorSnapshotPct = computed(() => summary.value ? pct(summary.value.indicator_snapshot_count, summary.value.stock_count) : '0%')

// 轻量路径：只拉自动更新状态，4s 递归轮询（running 时），绝不阻塞于 summary。
async function fetchAutoOverview(): Promise<void> {
  if (autoInFlight) return
  autoInFlight = true
  try {
    const autoResp = await axios.get('/api/data-status/auto-update', { timeout: 8000 })
    autoUpdate.value = autoResp.data
    lastRefreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch {
    // 保留上次状态，不打断轮询链
  } finally {
    autoInFlight = false
    schedulePolling()
  }
}

function schedulePolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = undefined
  }
  if (isPolling.value) {
    pollTimer = setTimeout(() => void fetchAutoOverview(), 4000)
  }
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
    error.value = (sumResp.data as { stale?: boolean }).stale ? '数据为自动更新期间的快照缓存（stale）' : ''
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
  void fetchAutoOverview()
  void fetchHeavy()
})
onBeforeUnmount(() => {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = undefined
  }
})

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

function skipReasonLabel(reason: string | null | undefined): string {
  const labels: Record<string, string> = {
    another_update_running: '另一更新占用写入锁',
    already_running: '更新任务已在运行',
    auto_update_disabled: '自动更新已关闭',
    auto_update_paused: '自动更新已暂停',
    prices_up_to_date: '价格已经是最新日期',
  }
  return reason ? (labels[reason] || reason) : '—'
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
        <n-button :loading="refreshing" @click="handleRefresh">刷新</n-button>
      </n-space>
    </n-space>
    <n-spin :show="loading && !summary">
      <n-alert v-if="error" type="error" title="提示" style="margin-bottom: 16px;">
        {{ error }}
      </n-alert>
      <template v-if="summary">
        <!-- L2 V3: 第一问"数据可研究吗" -->
        <n-alert
          :type="summary.data_quality.minimum_data_readiness.ready ? 'success' : 'error'"
          :show-icon="true"
          class="data-readiness"
        >
          <template #header>
            {{ summary.data_quality.minimum_data_readiness.ready ? '数据可研究' : '数据尚未就绪' }}
          </template>
          {{ summary.data_quality.minimum_data_readiness.ready
            ? `最近更新: ${summary.last_update || '—'}；价格/财报日期见下方详情。`
            : `有 ${summary.data_quality.warning_codes.length} 个警告，请查看下方详情；更新完成后自动恢复。` }}
        </n-alert>

        <p style="color: #999; margin-bottom: 16px;">
          最近更新: {{ summary.last_update || '尚未初始化' }}
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
            <n-descriptions-item label="当前阶段">{{ autoUpdate.current_stage || '—' }}</n-descriptions-item>
            <n-descriptions-item label="阶段进度">
              {{ autoUpdate.progress?.phase || '—' }}
              <span v-if="autoUpdate.progress?.steps && Object.keys(autoUpdate.progress.steps).length > 0">
                （{{ Object.entries(autoUpdate.progress.steps).map(([k, v]) => `${k}:${v}`).join('、') }}）
              </span>
            </n-descriptions-item>
            <n-descriptions-item label="作业ID">{{ autoUpdate.progress?.job_id || '—' }}</n-descriptions-item>
            <n-descriptions-item label="上次成功">{{ autoUpdate.last_success_at || '—' }}</n-descriptions-item>
            <n-descriptions-item label="最近结果">
              {{ autoUpdate.last_result || '尚未执行' }}
              <span v-if="autoUpdate.last_result === 'skipped'" style="color:#d48806;">
                （{{ skipReasonLabel(autoUpdate.last_skip_reason) }}）
              </span>
            </n-descriptions-item>
            <n-descriptions-item label="最后错误">
              <span v-if="autoUpdate.last_error" style="color:#d03050;">{{ autoUpdate.last_error }}</span>
              <span v-else>—</span>
            </n-descriptions-item>
          </n-descriptions>
          <!-- 实时逐股进度（运行中由 detail_cb 提供） -->
          <div v-if="updateLive" class="update-live">
            <div class="update-live-title">
              <n-tag size="small" type="info">{{ updateLive.label }}</n-tag>
              <span>{{ updateLive.done }} / {{ updateLive.total }}</span>
            </div>
            <n-progress
              type="line"
              :percentage="updateLivePct"
              :height="10"
              :show-indicator="false"
              processing
            />
            <p class="update-live-hint">
              正在更新 {{ updateLive.current || '—' }}
              <span v-if="updateLive.updated_at">· {{ new Date(updateLive.updated_at).toLocaleTimeString('zh-CN', { hour12: false }) }}</span>
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
        <template v-if="summary">
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
              >
                {{ code }}
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
          <n-data-table
            size="small"
            striped
            :columns="[
              {title:'股票',key:'stock_code',width:100},
              {title:'字段',key:'field_name',width:150},
              {title:'原因码',key:'reason_code',width:120},
            ]"
            :data="missingList"
            :pagination="{ pageSize: 10 }"
          />
        </n-card>

        <n-card v-if="summary.stock_count === 0">
          <n-empty description="尚未初始化数据。请运行: python -m app.cli.main data init" />
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
