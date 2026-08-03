<script setup lang="ts">
import { ref, onMounted, computed, h, watch } from 'vue'
import { useRouter, RouterLink } from 'vue-router'
import {
  NAlert, NCard, NSpace, NSelect, NInput, NButton, NDataTable,
  NEmpty, useMessage, useDialog, NStatistic, NCheckboxGroup, NCheckbox, NTag
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import axios from 'axios'
import { isIndicatorUntrusted } from '../types/data-quality.ts'
import type { IndicatorTrust } from '../types/data-quality.ts'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import { fieldTitleWithUnit, formatFieldValue } from '../utils/screening-format.ts'

interface WatchlistItem {
  stock_code: string
  name: string
  exchange: string
  csrc_l1: string | null
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
}

const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const items = ref<WatchlistItem[]>([])
const groups = ref<WatchlistGroup[]>([])
const trust = ref<IndicatorTrust | null>(null)
const loading = ref(false)
const selectedGroup = ref<string | null>(null)
const addStockCode = ref('')
const addGroupName = ref('default')

const warningCodes = computed(() => trust.value?.warning_codes ?? [])
const hasUntrustedIndicators = computed(
  () => warningCodes.value.length > 0 && isIndicatorUntrusted('*', warningCodes.value)
)

const groupOptions = computed(() => [
  { label: '全部分组', value: '' },
  ...groups.value.map((g) => ({ label: `${g.group_name} (${g.cnt})`, value: g.group_name })),
])

// L1-7（报告42）: 列配置记忆（localStorage），无历史配置时用默认列
const DEFAULT_COLUMNS = ['stock_code', 'name', 'exchange', 'group_name', 'latest_close', 'pe_ttm', 'pb_mrq', 'roe', 'gross_margin', 'debt_ratio', 'source']
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

const allColumnOptions = [
  { label: '代码', value: 'stock_code' },
  { label: '名称', value: 'name' },
  { label: '交易所', value: 'exchange' },
  { label: '分组', value: 'group_name' },
  { label: '收盘价', value: 'latest_close' },
  { label: 'PE', value: 'pe_ttm' },
  { label: 'PB', value: 'pb_mrq' },
  { label: 'ROE', value: 'roe' },
  { label: '毛利率', value: 'gross_margin' },
  { label: '净利率', value: 'net_margin' },
  { label: '负债率', value: 'debt_ratio' },
  { label: '营收YoY', value: 'revenue_yoy' },
  { label: '来源', value: 'source' },
]

type NumericField =
  | 'latest_close' | 'pe_ttm' | 'pb_mrq' | 'roe' | 'gross_margin'
  | 'net_margin' | 'debt_ratio' | 'revenue_yoy' | 'net_profit_yoy' | 'dividend_yield'

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
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '加载自选失败'))
  } finally {
    loading.value = false
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

const totalCount = computed(() => items.value.length)

onMounted(fetchWatchlist)
</script>

<template>
  <div>
    <h1 style="font-size: 24px; margin: 0 0 16px;">自选列表</h1>
    <!-- L1-1: 响应式统计卡 3→2→1 列 -->
    <div class="stats-grid stats-grid--3">
      <n-card size="small"><n-statistic label="总股票数" :value="totalCount" /></n-card>
      <n-card size="small"><n-statistic label="分组数" :value="groups.length" /></n-card>
      <n-card size="small"><n-statistic label="当前筛选" :value="selectedGroup || '全部'" /></n-card>
    </div>
    <n-card size="small" style="margin-bottom: 16px;">
      <n-space wrap>
        <n-select v-model:value="selectedGroup" :options="groupOptions" placeholder="选择分组" size="small" style="width: 200px;" @update:value="fetchWatchlist" />
        <n-input v-model:value="addStockCode" placeholder="输入股票代码" aria-label="股票代码" size="small" style="width: 150px;" @keyup.enter="addStock" />
        <n-input v-model:value="addGroupName" placeholder="分组名" aria-label="分组名" size="small" style="width: 120px;" />
        <n-button size="small" type="primary" @click="addStock">添加</n-button>
      </n-space>
    </n-card>

    <n-card size="small" style="margin-bottom: 16px;">
      <n-space align="center">
        <span style="font-size:12px;color:#999;">显示列:</span>
        <n-checkbox-group v-model:value="selectedColumns">
          <n-checkbox v-for="opt in allColumnOptions" :key="opt.value" :value="opt.value" :label="opt.label" size="small" />
        </n-checkbox-group>
      </n-space>
    </n-card>
    <n-alert v-if="hasUntrustedIndicators" type="warning" :show-icon="true" style="margin-bottom: 16px;">
      当前数据库状态不可信，指标数值已被服务端遮蔽。请先检查<router-link to="/data-status">数据状态页</router-link>。
    </n-alert>
    <n-data-table v-if="items.length > 0" :columns="tableColumns" :data="items" :pagination="{ pageSize: 50 }" :scroll-x="1200" size="small" striped />
    <n-empty v-else description="自选列表为空，添加股票或从筛选结果加入" style="padding: 40px;" />
  </div>
</template>
