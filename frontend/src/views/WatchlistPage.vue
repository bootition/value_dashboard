<script setup lang="ts">
import { ref, onMounted, computed, h, watch } from 'vue'
import { useRouter, RouterLink } from 'vue-router'
import {
  NAlert, NInput, NButton, NDataTable,
  NEmpty, useMessage, useDialog, NCheckboxGroup, NCheckbox, NTag
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import axios from 'axios'
import { isIndicatorUntrusted } from '../types/data-quality.ts'
import type { IndicatorTrust } from '../types/data-quality.ts'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import { fieldDisplayName, fieldTitleWithUnit, formatFieldValue } from '../utils/screening-format.ts'

interface WatchlistItem {
  stock_code: string
  name: string
  exchange: string
  csrc_l1: string | null
  listing_date: string | null
  group_name: string
  source_rule_id: number | null
  source_result_id: number | null
  added_at: string | null
  latest_close: number | null
  pe_ttm: number | null
  pb_mrq: number | null
  roe: number | null
  gross_margin: number | null
  net_margin: number | null
  debt_ratio: number | null
  revenue_yoy: number | null
  net_profit_yoy: number | null
  dividend_yield: number | null
  total_market_cap: number | null
  circ_market_cap: number | null
  untrusted_fields?: string[]
}

interface WatchlistGroup {
  group_name: string
  cnt: number
}

interface WatchlistResponse {
  items: WatchlistItem[]
  count: number
  groups: WatchlistGroup[]
  trust?: IndicatorTrust
  auto_update_in_progress?: boolean
}

const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const items = ref<WatchlistItem[]>([])
const groups = ref<WatchlistGroup[]>([])
const trust = ref<IndicatorTrust | null>(null)
const autoUpdateInProgress = ref(false)
const loading = ref(false)
const refreshing = ref(false)
const selectedGroup = ref<string | null>('default')
const showColumnSettings = ref(false)
const addStockCode = ref('')
const addGroupName = ref('default')

const warningCodes = computed(() => trust.value?.warning_codes ?? [])
const hasUntrustedIndicators = computed(
  () => warningCodes.value.length > 0 && isIndicatorUntrusted('*', warningCodes.value)
)
const hasRefreshableRule = computed(() => items.value.some((item) => item.source_rule_id != null))

const selectedGroupLabel = computed(() => selectedGroup.value || '默认组')
const displayGroupName = (name: string) => name === 'default' ? '默认组' : name

// L1-7（报告42）: 列配置记忆（localStorage），无历史配置时用默认列
const DEFAULT_COLUMNS = ['stock_code', 'name', 'exchange', 'group_name', 'listing_date', 'total_market_cap', 'latest_close', 'pe_ttm', 'pb_mrq', 'roe', 'gross_margin', 'debt_ratio', 'source']
const COLUMNS_STORAGE_KEY = 'vd.watchlist.columns'

function loadSavedColumns(): string[] {
  try {
    const raw = localStorage.getItem(COLUMNS_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : []
  } catch {
    return []
  }
}

// 列选项必须先于 loadSavedColumns 的过滤逻辑声明：
// 2026-08-14 红队 F5 —— 此前 allColumnOptions 声明在其后（TDZ），
// 用户配置过列后每次进入自选页都在 :filter 回调中抛
// ReferenceError: Cannot access 'allColumnOptions' before initialization，
// 导致整页白屏。
const allColumnOptions = [
  { label: '股票代码', value: 'stock_code' },
  { label: '股票名称', value: 'name' },
  { label: '交易所', value: 'exchange' },
  { label: '分组', value: 'group_name' },
  { label: '上市日期', value: 'listing_date' },
  { label: fieldDisplayName('total_market_cap'), value: 'total_market_cap' },
  { label: fieldDisplayName('circ_market_cap'), value: 'circ_market_cap' },
  { label: fieldDisplayName('latest_close'), value: 'latest_close' },
  { label: fieldDisplayName('pe_ttm'), value: 'pe_ttm' },
  { label: fieldDisplayName('pb_mrq'), value: 'pb_mrq' },
  { label: fieldDisplayName('roe'), value: 'roe' },
  { label: fieldDisplayName('gross_margin'), value: 'gross_margin' },
  { label: fieldDisplayName('net_margin'), value: 'net_margin' },
  { label: fieldDisplayName('debt_ratio'), value: 'debt_ratio' },
  { label: fieldDisplayName('revenue_yoy'), value: 'revenue_yoy' },
  { label: '来源', value: 'source' },
]

const savedColumns = loadSavedColumns()
const validSaved = savedColumns.filter((col) => allColumnOptions.some((o) => o.value === col))
const selectedColumns = ref<string[]>(validSaved.length > 0 ? validSaved : [...DEFAULT_COLUMNS])

watch(selectedColumns, (cols) => {
  try {
    localStorage.setItem(COLUMNS_STORAGE_KEY, JSON.stringify(cols))
  } catch {
    // localStorage 不可用时静默降级
  }
}, { deep: true })

// L1-7（报告42）: 一键复制股票代码
async function copyStockCode(code: string) {
  try {
    await navigator.clipboard.writeText(code)
    message.success(`已复制 ${code}`)
  } catch {
    message.warning('复制失败，请手动选择复制')
  }
}

type NumericField =
  | 'latest_close' | 'pe_ttm' | 'pb_mrq' | 'roe' | 'gross_margin'
  | 'net_margin' | 'debt_ratio' | 'revenue_yoy' | 'net_profit_yoy' | 'dividend_yield'
  | 'total_market_cap' | 'circ_market_cap'

function trustedRender(field: NumericField) {
  // L0-2（报告42）: 与筛选/详情共用同一字段口径格式化
  return (row: WatchlistItem) => {
    if (isIndicatorUntrusted(field, warningCodes.value)) {
      return h(NTag, { size: 'tiny', type: 'error' }, () => '数据不可信')
    }
    return formatFieldValue(field, row[field])
  }
}

const tableColumns = computed(() => {
  const cols: DataTableColumns<WatchlistItem> = []
  const colDefs: Record<string, DataTableColumns<WatchlistItem>[number]> = {
    // L1-7: 代码列首列（名称列紧随其后），保持滚动可读由用户调整窗口宽度
    stock_code: {
      title: '代码', key: 'stock_code', width: 130,
      render: (r) => h('span', { style: 'white-space: nowrap;' }, [
        h(RouterLink, { to: `/stock/${encodeURIComponent(r.stock_code)}`, class: 'stock-link' }, { default: () => r.stock_code }),
        h(NButton, {
          text: true, size: 'tiny', type: 'primary', class: 'copy-btn',
          title: `复制 ${r.stock_code}`,
          onClick: () => void copyStockCode(r.stock_code),
        }, { default: () => '复制' }),
      ]),
    },
    name: { title: '名称', key: 'name', width: 100, render: (r) => r.name || '—' },
    exchange: { title: '交易所', key: 'exchange', width: 70, render: (r) => r.exchange || '—' },
    group_name: { title: '分组', key: 'group_name', width: 90 },
    listing_date: { title: '上市日期', key: 'listing_date', width: 96, sorter: 'default', render: (r) => r.listing_date || '—' },
    total_market_cap: { title: fieldTitleWithUnit('total_market_cap', '总市值'), key: 'total_market_cap', width: 110, sorter: 'default', render: trustedRender('total_market_cap') },
    circ_market_cap: { title: fieldTitleWithUnit('circ_market_cap', '流通市值'), key: 'circ_market_cap', width: 110, sorter: 'default', render: trustedRender('circ_market_cap') },
    latest_close: { title: fieldTitleWithUnit('latest_close', '收盘价'), key: 'latest_close', width: 80, sorter: 'default', render: trustedRender('latest_close') },
    pe_ttm: { title: fieldTitleWithUnit('pe_ttm', 'PE'), key: 'pe_ttm', width: 70, sorter: 'default', render: trustedRender('pe_ttm') },
    pb_mrq: { title: fieldTitleWithUnit('pb_mrq', 'PB'), key: 'pb_mrq', width: 70, sorter: 'default', render: trustedRender('pb_mrq') },
    roe: { title: fieldTitleWithUnit('roe', 'ROE'), key: 'roe', width: 80, sorter: 'default', render: trustedRender('roe') },
    gross_margin: { title: fieldTitleWithUnit('gross_margin', '毛利率'), key: 'gross_margin', width: 80, sorter: 'default', render: trustedRender('gross_margin') },
    net_margin: { title: fieldTitleWithUnit('net_margin', '净利率'), key: 'net_margin', width: 80, sorter: 'default', render: trustedRender('net_margin') },
    debt_ratio: { title: fieldTitleWithUnit('debt_ratio', '负债率'), key: 'debt_ratio', width: 80, sorter: 'default', render: trustedRender('debt_ratio') },
    revenue_yoy: { title: fieldTitleWithUnit('revenue_yoy', '营收YoY'), key: 'revenue_yoy', width: 90, sorter: 'default', render: trustedRender('revenue_yoy') },
    source: { title: '来源', key: 'source', width: 80, render: (r) => r.source_result_id ? '筛选' : (r.source_rule_id ? '规则' : '手动') },
  }
  for (const col of selectedColumns.value) {
    const def = colDefs[col]
    if (def) cols.push(def)
  }
  cols.push({
    title: '操作', key: 'actions', width: 120,
    render: (r) => h('div', [
      h(NButton, { size: 'tiny', type: 'info', onClick: () => router.push(`/stock/${r.stock_code}`) }, () => '查看'),
      h(NButton, { size: 'tiny', type: 'error', style: 'margin-left: 4px', onClick: () => confirmRemove(r.stock_code, r.group_name) }, () => '移除'),
    ]),
  })
  return cols
})

async function fetchWatchlist() {
  loading.value = true
  try {
    const params = selectedGroup.value ? { group: selectedGroup.value } : {}
    const resp = await axios.get<WatchlistResponse>('/api/watchlist/list', { params })
    items.value = resp.data.items
    groups.value = resp.data.groups
    trust.value = resp.data.trust ?? null
  autoUpdateInProgress.value = resp.data.auto_update_in_progress === true
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '加载自选失败'))
  } finally {
    loading.value = false
  }
}

async function refreshGroup() {
  if (!selectedGroup.value) return
  refreshing.value = true
  try {
    const resp = await axios.post<{ status: string; added: number; removed: number; refreshed: number }>(
      '/api/watchlist/refresh',
      { group_name: selectedGroup.value },
    )
    message.success(`已按最新数据重新筛选「${selectedGroup.value}」，更新 ${resp.data.added} 只`)
    await fetchWatchlist()
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '按最新数据重新筛选失败'))
  } finally {
    refreshing.value = false
  }
}

// L0-6（报告42）: 前端先校验 6 位数字代码；后端同样强制校验
async function addStock() {
  const code = addStockCode.value.trim()
  if (!/^\d{6}$/.test(code)) {
    message.warning('请输入 6 位股票代码（如 600519）')
    return
  }
  try {
    await axios.post('/api/watchlist/add', { stock_code: code, group_name: addGroupName.value || 'default' })
    message.success(`已添加 ${code}`)
    addStockCode.value = ''
    fetchWatchlist()
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '添加失败'))
  }
}

async function removeStock(code: string, group: string) {
  try {
    await axios.delete('/api/watchlist/remove', { data: { stock_code: code, group_name: group } })
    message.success(`已移除 ${code}`)
    fetchWatchlist()
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '移除失败'))
  }
}

// L0-6（报告42）: 移除前确认，防止误删
function confirmRemove(code: string, group: string) {
  dialog.warning({
    title: '移出自选',
    content: `确定将 ${code} 从「${group}」移除吗？`,
    positiveText: '确认移除',
    negativeText: '取消',
    onPositiveClick: () => void removeStock(code, group),
  })
}

onMounted(fetchWatchlist)
</script>

<template>
  <section class="watchlist-page">
    <header class="watchlist-header"><div><p>研究工具 / 自选列表</p><h1>我的自选</h1><span>按手动收录与筛选规则来源组织研究中的公司。</span></div></header>
    <div class="watchlist-layout">
      <aside class="watchlist-groups">
        <div class="groups-heading"><p>WATCHLIST GROUPS</p><h2>公司分组</h2></div>
        <n-button v-for="group in groups" :key="group.group_name" :class="{ selected: selectedGroup === group.group_name }" text block @click="selectedGroup = group.group_name; fetchWatchlist()"><span><b>{{ displayGroupName(group.group_name) }}</b><small>{{ group.group_name === 'default' ? '手动收录' : '筛选规则来源' }}</small></span><em>{{ group.cnt }}</em></n-button>
        <div class="manual-add"><n-input v-model:value="addStockCode" placeholder="输入股票代码" aria-label="股票代码" size="small" @keyup.enter="addStock" /><n-input v-model:value="addGroupName" placeholder="手动分组名" aria-label="分组名" size="small" /><n-button size="small" @click="addStock">添加股票</n-button></div>
      </aside>
      <section class="watchlist-content">
        <n-alert v-if="autoUpdateInProgress" type="info" :show-icon="true" class="watchlist-warning">数据正在自动更新，以下指标为更新前快照，更新完成后自动恢复。详见<router-link to="/data-status">数据状态页</router-link>。</n-alert>
        <n-alert v-if="hasUntrustedIndicators" type="warning" :show-icon="true" class="watchlist-warning">当前数据库状态不可信，指标数值已被服务端遮蔽。请先检查<router-link to="/data-status">数据状态页</router-link>。</n-alert>
        <div class="watchlist-card"><div class="watchlist-card-heading"><div><p>{{ selectedGroupLabel === '默认组' ? '手动收录' : '筛选规则来源' }}</p><h2>{{ selectedGroupLabel }}</h2><span>{{ selectedGroupLabel === '默认组' ? '手动加入或未关联筛选来源的公司。' : '来自该筛选规则的已保存结果。' }}</span></div><div class="watchlist-actions"><span>显示 {{ items.length }} 家公司</span><n-button v-if="hasRefreshableRule" size="small" :loading="refreshing" @click="refreshGroup">按最新数据重新筛选</n-button><n-button size="small" @click="showColumnSettings = !showColumnSettings">配置列</n-button></div></div><div v-if="showColumnSettings" class="watchlist-toolbar"><n-checkbox-group v-model:value="selectedColumns"><n-checkbox v-for="opt in allColumnOptions" :key="opt.value" :value="opt.value" :label="opt.label" size="small" /></n-checkbox-group></div><n-data-table v-if="items.length > 0" :columns="tableColumns" :data="items" :pagination="{ pageSize: 50 }" :scroll-x="1200" size="small" striped /><n-empty v-else description="该分组暂无公司" class="watchlist-empty" /></div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.watchlist-page { max-width: 1380px; }.watchlist-header { margin-bottom: 27px; }.watchlist-header p, .watchlist-card-heading p, .groups-heading p { margin: 0; color: #97a199; font-size: 10px; }.watchlist-header h1 { margin: 8px 0 0; font-size: 25px; letter-spacing: -.05em; }.watchlist-header span { display: block; margin-top: 7px; color: #829087; font-size: 12px; }.watchlist-layout { display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 21px; }.watchlist-groups, .watchlist-card { border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.watchlist-groups { align-self: start; padding: 22px 14px; }.groups-heading { padding: 0 10px 13px; border-bottom: 1px solid #edf1ee; }.groups-heading h2 { margin: 7px 0 0; font-size: 17px; }.watchlist-groups :deep(.n-button) { justify-content: space-between; height: auto; margin-top: 5px; padding: 11px 10px; border-radius: 9px; color: #6e7d73; text-align: left; }.watchlist-groups :deep(.n-button.selected) { background: #eff8f1; color: #55966d; }.watchlist-groups :deep(.n-button__content) { display: flex; justify-content: space-between; width: 100%; }.watchlist-groups small, .watchlist-groups b { display: block; }.watchlist-groups small { margin-top: 4px; color: #99a39c; font-size: 9px; }.watchlist-groups em { display: grid; width: 22px; height: 22px; place-items: center; border-radius: 50%; background: #f1f5f2; color: #7c8c81; font-size: 10px; font-style: normal; }.manual-add { display: grid; gap: 8px; margin-top: 14px; padding-top: 14px; border-top: 1px solid #edf1ee; }.watchlist-card { padding: 28px 29px; }.watchlist-card-heading { display: flex; justify-content: space-between; gap: 20px; }.watchlist-card-heading h2 { margin: 7px 0 5px; font-size: 19px; }.watchlist-card-heading span { color: #829087; font-size: 11px; }.watchlist-actions { display: flex; align-items: center; gap: 10px; white-space: nowrap; }.watchlist-toolbar { margin: 20px 0; padding: 12px; border-radius: 8px; background: #fafcf9; }.watchlist-warning { margin-bottom: 16px; }.watchlist-empty { padding: 40px; }
</style>
