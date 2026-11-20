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
const loading = ref(true)
const refreshing = ref(false)
// `default` 只是后端持久化 key（历史数据无法改名，改库会丢分组），
// 用户可见处一律走 displayGroupName 归一成中文。
const MANUAL_GROUP_KEY = 'default'
const MANUAL_GROUP_LABEL = '手动收录'
// 首屏不预选任何分组：等分组列表回来后自动落到第一个真实规则分组，
// 这样用户一进来自选页看到的就是「已有的规则分组」，而不是空的占位组。
const selectedGroup = ref<string | null>(null)
const showColumnSettings = ref(false)
const addStockCode = ref('')
const addGroupName = ref('')

const warningCodes = computed(() => trust.value?.warning_codes ?? [])
const hasUntrustedIndicators = computed(
  () => warningCodes.value.length > 0 && isIndicatorUntrusted('*', warningCodes.value)
)
const hasRefreshableRule = computed(() => items.value.some((item) => item.source_rule_id != null))

/** 唯一的组名展示归一出口：`default` 只作为内部 key 存在。 */
const displayGroupName = (name: string) => name === MANUAL_GROUP_KEY ? MANUAL_GROUP_LABEL : name
const isManualGroup = (name: string | null) => name === MANUAL_GROUP_KEY
/** 空的手动组不占侧栏位置（后端 GROUP BY 不会返回 0 行分组，这里兜底）；
 *  真实规则分组排前面，手动收录作为兜底入口排最后。 */
const visibleGroups = computed(() => groups.value
  .filter((g) => !isManualGroup(g.group_name) || g.cnt > 0)
  .slice()
  .sort((a, b) => Number(isManualGroup(a.group_name)) - Number(isManualGroup(b.group_name))))
const hasVisibleGroups = computed(() => visibleGroups.value.length > 0)
const selectedGroupLabel = computed(() => selectedGroup.value ? displayGroupName(selectedGroup.value) : '')
const selectedIsManual = computed(() => isManualGroup(selectedGroup.value))
const emptyDescription = computed(
  () => selectedGroup.value ? `「${selectedGroupLabel.value}」里还没有公司` : '还没有可显示的自选分组',
)

function resolveSelectedGroup(list: WatchlistGroup[]): string | null {
  const visible = list.filter((g) => !isManualGroup(g.group_name) || g.cnt > 0)
  const current = selectedGroup.value
  if (current && visible.some((g) => g.group_name === current)) return current
  // 优先落到真实规则分组（用户说的"已有的那个规则分组界面"），
  // 只有完全没有规则分组时才回退到手动收录。
  return visible.find((g) => !isManualGroup(g.group_name))?.group_name
    ?? visible.find((g) => isManualGroup(g.group_name))?.group_name
    ?? null
}

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
    group_name: { title: '分组', key: 'group_name', width: 90, render: (r) => displayGroupName(r.group_name) },
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

function applyResponse(data: WatchlistResponse) {
  groups.value = data.groups ?? []
  trust.value = data.trust ?? null
  autoUpdateInProgress.value = data.auto_update_in_progress === true
}

async function fetchWatchlist() {
  loading.value = true
  try {
    const params = selectedGroup.value ? { group: selectedGroup.value } : {}
    const resp = await axios.get<WatchlistResponse>('/api/watchlist/list', { params })
    // 首次进入 / 当前分组消失（例如最后一只股票被移除）时，自动落到第一个
    // 真实规则分组再取一次数：不要停留在空的占位分组上。
    // 分组与列表必须一起落地，否则会先闪一帧"该分组暂无公司"。
    const next = resolveSelectedGroup(resp.data.groups ?? [])
    if (next !== selectedGroup.value) {
      selectedGroup.value = next
      const scoped = await axios.get<WatchlistResponse>('/api/watchlist/list', {
        params: next ? { group: next } : {},
      })
      applyResponse(scoped.data)
      items.value = scoped.data.items
    } else {
      applyResponse(resp.data)
      items.value = resp.data.items
    }
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '加载自选失败'))
  } finally {
    loading.value = false
  }
}

function selectGroup(name: string) {
  if (selectedGroup.value === name) return
  selectedGroup.value = name
  void fetchWatchlist()
}

async function refreshGroup() {
  if (!selectedGroup.value) return
  refreshing.value = true
  try {
    const resp = await axios.post<{ status: string; added: number; removed: number; refreshed: number }>(
      '/api/watchlist/refresh',
      { group_name: selectedGroup.value },
    )
    message.success(`已按最新数据重新筛选「${displayGroupName(selectedGroup.value)}」，更新 ${resp.data.added} 只`)
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
  // 分组名留空 = 收进「手动收录」（后端内部 key 仍是 default，展示层归一）。
  const targetGroup = addGroupName.value.trim() || MANUAL_GROUP_KEY
  try {
    await axios.post('/api/watchlist/add', { stock_code: code, group_name: targetGroup })
    message.success(`已添加 ${code} 到「${displayGroupName(targetGroup)}」`)
    addStockCode.value = ''
    // 加完直接看这只票落到的分组，避免"加成功但列表里看不见"。
    selectedGroup.value = targetGroup
    await fetchWatchlist()
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
    content: `确定将 ${code} 从「${displayGroupName(group)}」移除吗？`,
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
        <div class="groups-heading"><p>自选列表</p><h2>公司分组</h2></div>
        <n-button v-for="group in visibleGroups" :key="group.group_name" :class="{ selected: selectedGroup === group.group_name }" text block @click="selectGroup(group.group_name)"><span><b>{{ displayGroupName(group.group_name) }}</b><small>{{ isManualGroup(group.group_name) ? '手动加入，不关联筛选规则' : '筛选规则来源' }}</small></span><em>{{ group.cnt }}</em></n-button>
        <p v-if="!hasVisibleGroups && !loading" class="groups-empty">还没有分组，去筛选规则页运行规则并保存到自选即可生成。</p>
        <div class="manual-add"><n-input v-model:value="addStockCode" placeholder="输入股票代码" aria-label="股票代码" size="small" @keyup.enter="addStock" /><n-input v-model:value="addGroupName" placeholder="分组名（留空则为手动收录）" aria-label="分组名" size="small" /><n-button size="small" @click="addStock">添加股票</n-button></div>
      </aside>
      <section class="watchlist-content">
        <n-alert v-if="autoUpdateInProgress" type="info" :show-icon="true" class="watchlist-warning">数据正在自动更新，以下指标为更新前快照，更新完成后自动恢复。详见<router-link to="/data-status">数据状态页</router-link>。</n-alert>
        <n-alert v-if="hasUntrustedIndicators" type="warning" :show-icon="true" class="watchlist-warning">当前数据库状态不可信，指标数值已被服务端遮蔽。请先检查<router-link to="/data-status">数据状态页</router-link>。</n-alert>
        <div class="watchlist-card">
          <template v-if="hasVisibleGroups">
            <div class="watchlist-card-heading"><div><p>{{ selectedIsManual ? '手动加入' : '筛选规则来源' }}</p><h2>{{ selectedGroupLabel }}</h2><span>{{ selectedIsManual ? '手动加入或未关联筛选来源的公司。' : '来自该筛选规则的已保存结果。' }}</span></div><div class="watchlist-actions"><span v-if="items.length > 0">显示 {{ items.length }} 家公司</span><n-button v-if="hasRefreshableRule" size="small" :loading="refreshing" @click="refreshGroup">按最新数据重新筛选</n-button><n-button v-if="items.length > 0" size="small" @click="showColumnSettings = !showColumnSettings">配置列</n-button></div></div>
            <div v-if="showColumnSettings && items.length > 0" class="watchlist-toolbar"><n-checkbox-group v-model:value="selectedColumns"><n-checkbox v-for="opt in allColumnOptions" :key="opt.value" :value="opt.value" :label="opt.label" size="small" /></n-checkbox-group></div>
            <n-data-table v-if="items.length > 0" :columns="tableColumns" :data="items" :pagination="{ pageSize: 50 }" :scroll-x="1200" size="small" striped />
            <n-empty v-else class="watchlist-empty" :description="emptyDescription"><template #extra><div class="watchlist-empty-extra"><p class="watchlist-empty-hint">去筛选规则页跑一次规则，把命中结果保存到自选，就能在这里看到公司。</p><div class="watchlist-empty-actions"><router-link class="watchlist-empty-link" to="/screening">去筛选规则页</router-link><n-button size="small" quaternary :loading="loading" @click="fetchWatchlist">刷新列表</n-button></div></div></template></n-empty>
          </template>
          <n-empty v-else-if="!loading" class="watchlist-empty" :description="emptyDescription"><template #extra><div class="watchlist-empty-extra"><p class="watchlist-empty-hint">自选分组来自筛选规则的保存结果。先到筛选规则页跑一条规则，把命中结果保存到自选，就会生成对应分组；也可以直接在左侧输入股票代码收进「手动收录」。</p><div class="watchlist-empty-actions"><router-link class="watchlist-empty-link" to="/screening">去筛选规则页</router-link><n-button size="small" quaternary :loading="loading" @click="fetchWatchlist">刷新列表</n-button></div></div></template></n-empty>
          <p v-else class="watchlist-empty-hint watchlist-loading">正在加载分组…</p>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.watchlist-page { max-width: 1380px; }.watchlist-header { margin-bottom: 27px; }.watchlist-header p, .watchlist-card-heading p, .groups-heading p { margin: 0; color: #97a199; font-size: 10px; }.watchlist-header h1 { margin: 8px 0 0; font-size: 25px; letter-spacing: -.05em; }.watchlist-header span { display: block; margin-top: 7px; color: #829087; font-size: 12px; }.watchlist-layout { display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 21px; }.watchlist-groups, .watchlist-card { border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.watchlist-groups { align-self: start; padding: 22px 14px; }.groups-heading { padding: 0 10px 13px; border-bottom: 1px solid #edf1ee; }.groups-heading h2 { margin: 7px 0 0; font-size: 17px; }.watchlist-groups :deep(.n-button) { justify-content: space-between; height: auto; margin-top: 5px; padding: 11px 10px; border-radius: 9px; color: #6e7d73; text-align: left; }.watchlist-groups :deep(.n-button.selected) { background: #eff8f1; color: #55966d; }.watchlist-groups :deep(.n-button__content) { display: flex; justify-content: space-between; width: 100%; }.watchlist-groups small, .watchlist-groups b { display: block; }.watchlist-groups small { margin-top: 4px; color: #99a39c; font-size: 9px; }.watchlist-groups em { display: grid; width: 22px; height: 22px; place-items: center; border-radius: 50%; background: #f1f5f2; color: #7c8c81; font-size: 10px; font-style: normal; }.manual-add { display: grid; gap: 8px; margin-top: 14px; padding-top: 14px; border-top: 1px solid #edf1ee; }.watchlist-card { padding: 28px 29px; }.watchlist-card-heading { display: flex; justify-content: space-between; gap: 20px; }.watchlist-card-heading h2 { margin: 7px 0 5px; font-size: 19px; }.watchlist-card-heading span { color: #829087; font-size: 11px; }.watchlist-actions { display: flex; align-items: center; gap: 10px; white-space: nowrap; }.watchlist-toolbar { margin: 20px 0; padding: 12px; border-radius: 8px; background: #fafcf9; }.watchlist-warning { margin-bottom: 16px; }.watchlist-empty { padding: 40px; }.groups-empty { margin: 14px 10px 0; color: #99a39c; font-size: 11px; line-height: 1.7; }.watchlist-empty-extra { display: grid; gap: 12px; justify-items: center; max-width: 460px; }.watchlist-empty-hint { margin: 0; color: #829087; font-size: 12px; line-height: 1.7; text-align: center; }.watchlist-empty-actions { display: flex; align-items: center; gap: 12px; }.watchlist-empty-link { display: inline-flex; align-items: center; height: 28px; padding: 0 14px; border-radius: 6px; background: #eff8f1; color: #55966d; font-size: 13px; text-decoration: none; }.watchlist-empty-link:hover { background: #e2f2e7; }.watchlist-loading { padding: 40px; color: #99a39c; }
</style>
