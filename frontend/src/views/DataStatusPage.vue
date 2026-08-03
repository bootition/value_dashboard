<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { NCard, NStatistic, NGrid, NGridItem, NSpin, NEmpty, NTag, NDescriptions, NDescriptionsItem, NButton, NSpace, NDataTable, NAlert } from 'naive-ui'
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
  }
  last_error: string | null
  last_success_at: string | null
  updated_at?: string | null
}

const summary = ref<DataSummary | null>(null)
const loading = ref(true)
const error = ref('')
const retryList = ref<RetryItem[]>([])
const missingList = ref<MissingItem[]>([])
const autoUpdate = ref<AutoUpdateStatus | null>(null)
// L1-3（报告42）: 更新运行中每 12s 自动轮询；显示"上次刷新"
const lastRefreshedAt = ref<string | null>(null)
let pollTimer: ReturnType<typeof setInterval> | undefined

const isPolling = computed(() => autoUpdate.value?.state === 'running')

const pct = (value: number, total: number) => {
  if (!total || total === 0) return '0%'
  return ((value / total) * 100).toFixed(1) + '%'
}

const priceRawPct = computed(() => summary.value ? pct(summary.value.price_raw_count, summary.value.stock_count) : '0%')
const priceQfqPct = computed(() => summary.value ? pct(summary.value.price_qfq_count, summary.value.stock_count) : '0%')
const csrcIndustryPct = computed(() => summary.value ? pct(summary.value.csrc_industry_count, summary.value.stock_count) : '0%')
const balanceSheetPct = computed(() => summary.value ? pct(summary.value.balance_sheet_count, summary.value.stock_count) : '0%')
const incomeStatementPct = computed(() => summary.value ? pct(summary.value.income_statement_count, summary.value.stock_count) : '0%')
const cashFlowPct = computed(() => summary.value ? pct(summary.value.cash_flow_count, summary.value.stock_count) : '0%')
const indicatorSnapshotPct = computed(() => summary.value ? pct(summary.value.indicator_snapshot_count, summary.value.stock_count) : '0%')

async function fetchData(silent = false) {
  if (!silent) error.value = ''
  if (!silent) loading.value = true
  try {
    const [sumResp, retryResp, missResp, autoResp] = await Promise.all([
      axios.get('/api/data-status/summary'),
      axios.get('/api/data-status/retry-list'),
      axios.get('/api/data-status/missing-list'),
      axios.get('/api/data-status/auto-update'),
    ])
    summary.value = sumResp.data
    retryList.value = retryResp.data.items || []
    missingList.value = missResp.data.items || []
    autoUpdate.value = autoResp.data
    lastRefreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (e: unknown) {
    if (!silent) error.value = axios.isAxiosError(e) ? e.message : (e instanceof Error ? e.message : '加载失败')
  } finally {
    if (!silent) loading.value = false
    schedulePolling()
  }
}

// L1-3（报告42）: 更新运行中每 12 秒轮询；停止后自动取消
function schedulePolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = undefined
  }
  if (autoUpdate.value?.state === 'running') {
    pollTimer = setInterval(() => void fetchData(true), 12000)
  }
}

onMounted(() => void fetchData())
onBeforeUnmount(() => {
  if (pollTimer) {
    clearInterval(pollTimer)
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

</script>

<template>
  <div>
    <n-space align="center" justify="space-between" style="margin-bottom: 16px;" wrap>
      <h1 style="font-size: 24px; margin: 0;">数据状态</h1>
      <n-space align="center" size="small">
        <!-- L1-3（报告42）: 显示上次刷新时间与自动轮询状态 -->
        <span v-if="lastRefreshedAt" style="color:#999; font-size:12px;">上次刷新 {{ lastRefreshedAt }}</span>
        <n-tag v-if="isPolling" size="small" type="info">更新运行中，每 12 秒自动刷新</n-tag>
        <n-button :loading="loading" @click="fetchData()">刷新</n-button>
      </n-space>
    </n-space>
    <n-spin :show="loading">
      <n-alert v-if="error" type="error" title="加载失败" style="margin-bottom: 16px;">
        {{ error }}
      </n-alert>
      <div v-else-if="summary">
        <!-- L2 V3: 第一问"数据可研究吗" -->
        <n-alert
          :type="summary.data_quality.minimum_data_readiness.ready ? 'success' : 'error'"
          :show-icon="true"
          style="margin-bottom: 16px;"
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

        <!-- 自动更新状态（PRD §7.3 只读展示） -->
        <n-card title="自动更新" size="small" style="margin-bottom: 16px;" v-if="autoUpdate">
          <n-descriptions :column="4" size="small">
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
            <n-descriptions-item label="最后错误">
              <span v-if="autoUpdate.last_error" style="color:#d03050;">{{ autoUpdate.last_error }}</span>
              <span v-else>—</span>
            </n-descriptions-item>
          </n-descriptions>
          <p v-if="autoUpdate.state === 'disabled'" style="color:#999; margin:8px 0 0;">
            自动更新已关闭。可在 CLI 执行 <code>vd data auto-update enable</code> 重新开启。
          </p>
          <p v-else style="color:#999; margin:8px 0 0;">
            控制入口（开关/立即更新/暂停/继续）在 CLI：
            <code>vd data auto-update status|enable|disable|run|pause|resume</code>
          </p>
        </n-card>

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
            <n-descriptions :column="2" size="small" label-placement="left" bordered>
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

        <!-- 覆盖统计 -->
        <n-grid :cols="4" :x-gap="16" :y-gap="16" style="margin-bottom: 16px;">
          <n-grid-item><n-card><n-statistic label="股票总数" :value="summary.stock_count" /></n-card></n-grid-item>
          <n-grid-item><n-card><n-statistic label="Raw价格覆盖" :value="summary.price_raw_count">
            <template #suffix>{{ priceRawPct }}</template>
          </n-statistic></n-card></n-grid-item>
          <n-grid-item><n-card><n-statistic label="Qfq价格覆盖" :value="summary.price_qfq_count">
            <template #suffix>{{ priceQfqPct }}</template>
          </n-statistic></n-card></n-grid-item>
          <n-grid-item><n-card><n-statistic label="CSRC行业覆盖" :value="summary.csrc_industry_count">
            <template #suffix>{{ csrcIndustryPct }}</template>
          </n-statistic></n-card></n-grid-item>
          <n-grid-item><n-card><n-statistic label="资产负债表" :value="summary.balance_sheet_count">
            <template #suffix>{{ balanceSheetPct }}</template>
          </n-statistic></n-card></n-grid-item>
          <n-grid-item><n-card><n-statistic label="利润表" :value="summary.income_statement_count">
            <template #suffix>{{ incomeStatementPct }}</template>
          </n-statistic></n-card></n-grid-item>
          <n-grid-item><n-card><n-statistic label="现金流量表" :value="summary.cash_flow_count">
            <template #suffix>{{ cashFlowPct }}</template>
          </n-statistic></n-card></n-grid-item>
          <n-grid-item><n-card><n-statistic label="指标快照" :value="summary.indicator_snapshot_count">
            <template #suffix>{{ indicatorSnapshotPct }}</template>
          </n-statistic></n-card></n-grid-item>
        </n-grid>

        <!-- 回填状态 -->
        <n-card title="价格回填状态" size="small" style="margin-bottom: 16px;" v-if="summary.price_backfill">
          <n-descriptions :column="4" size="small">
            <n-descriptions-item label="最早日期">{{ summary.price_backfill.earliest_date || '—' }}</n-descriptions-item>
            <n-descriptions-item label="最新日期">{{ summary.price_backfill.latest_date || '—' }}</n-descriptions-item>
            <n-descriptions-item label="覆盖股票">{{ summary.price_backfill.stock_count }}</n-descriptions-item>
            <n-descriptions-item label="总行数">{{ summary.price_backfill.total_rows }}</n-descriptions-item>
            <n-descriptions-item label="上市日未知">{{ summary.price_backfill.gap.unknown_listing_date }}</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <!-- 财务覆盖范围 -->
        <n-card title="财务报表覆盖范围" size="small" style="margin-bottom: 16px;" v-if="summary.balance_sheet_range">
          <n-descriptions :column="3" size="small">
            <n-descriptions-item label="资产负债表">{{ summary.balance_sheet_range?.earliest }} ~ {{ summary.balance_sheet_range?.latest }}</n-descriptions-item>
            <n-descriptions-item label="利润表">{{ summary.income_statement_count }} 只</n-descriptions-item>
            <n-descriptions-item label="现金流量表">{{ summary.cash_flow_count }} 只</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <n-card title="公司行动与分红" size="small" style="margin-bottom: 16px;">
          <n-descriptions :column="4" size="small">
            <n-descriptions-item label="分红记录">{{ summary.dividends?.total_rows ?? 0 }}</n-descriptions-item>
            <n-descriptions-item label="分红覆盖股票">{{ summary.dividends?.stocks ?? 0 }}</n-descriptions-item>
            <n-descriptions-item label="分红日期范围">{{ summary.dividends ? `${summary.dividends.earliest || '—'} ~ ${summary.dividends.latest || '—'}` : '—' }}</n-descriptions-item>
            <n-descriptions-item label="除权除息记录">{{ summary.xdxr?.total_rows ?? 0 }}（{{ summary.xdxr?.stocks ?? 0 }} 只）</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <!-- PRD §6.4/§15: 各数据域最新日期 -->
        <n-card title="各数据域最新日期" size="small" style="margin-bottom: 16px;">
          <n-descriptions :column="3" size="small">
            <n-descriptions-item label="价格日期">
              {{ summary.data_quality.dates.price || '—' }}
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
        <n-grid :cols="3" :x-gap="16" style="margin-bottom: 16px;">
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
        <n-card title="备份摘要" size="small" style="margin-bottom: 16px;" v-if="summary.backup">
          <n-descriptions :column="3" size="small">
            <n-descriptions-item label="备份总数">{{ summary.backup?.cnt || 0 }}</n-descriptions-item>
            <n-descriptions-item label="全量备份">{{ summary.backup?.full_count || 0 }}</n-descriptions-item>
            <n-descriptions-item label="最近备份">{{ summary.backup?.latest || '—' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <!-- 重试列表 -->
        <n-card title="重试列表" size="small" style="margin-bottom: 16px;" v-if="retryList.length > 0">
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

        <n-card v-if="summary.stock_count === 0" style="margin-top: 16px;">
          <n-empty description="尚未初始化数据。请运行: python -m app.cli.main data init" />
        </n-card>
      </div>
    </n-spin>
  </div>
</template>
