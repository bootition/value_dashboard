<script setup lang="ts">
import { ref, onMounted, computed, h } from 'vue'
import { useRouter } from 'vue-router'
import {
  NCard, NSpace, NSelect, NInput, NButton, NDataTable,
  NEmpty, useMessage, NStatistic, NGrid, NGridItem, NCheckboxGroup, NCheckbox
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import axios, { isAxiosError } from 'axios'

interface WatchlistItem {
  stock_code: string
  name: string
  exchange: string
  sw_level1: string | null
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
}

interface WatchlistGroup {
  group_name: string
  cnt: number
}

const router = useRouter()
const message = useMessage()

const items = ref<WatchlistItem[]>([])
const groups = ref<WatchlistGroup[]>([])
const loading = ref(false)
const selectedGroup = ref<string | null>(null)
const addStockCode = ref('')
const addGroupName = ref('default')

const groupOptions = computed(() => [
  { label: '全部分组', value: '' },
  ...groups.value.map((g) => ({ label: `${g.group_name} (${g.cnt})`, value: g.group_name })),
])

const selectedColumns = ref<string[]>(['stock_code','name','exchange','group_name','latest_close','pe_ttm','pb_mrq','roe','gross_margin','debt_ratio','source'])

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

const tableColumns = computed(() => {
  const cols: DataTableColumns<WatchlistItem> = []
  const colDefs: Record<string, DataTableColumns<WatchlistItem>[number]> = {
    stock_code: { title: '代码', key: 'stock_code', width: 90 },
    name: { title: '名称', key: 'name', width: 100, render: (r) => r.name || '—' },
    exchange: { title: '交易所', key: 'exchange', width: 70, render: (r) => r.exchange || '—' },
    group_name: { title: '分组', key: 'group_name', width: 90 },
    latest_close: { title: '收盘价', key: 'latest_close', width: 80, sorter: 'default', render: (r) => r.latest_close != null ? r.latest_close.toFixed(2) : '—' },
    pe_ttm: { title: 'PE', key: 'pe_ttm', width: 70, sorter: 'default', render: (r) => r.pe_ttm != null ? r.pe_ttm.toFixed(1) : '—' },
    pb_mrq: { title: 'PB', key: 'pb_mrq', width: 70, sorter: 'default', render: (r) => r.pb_mrq != null ? r.pb_mrq.toFixed(2) : '—' },
    roe: { title: 'ROE', key: 'roe', width: 80, sorter: 'default', render: (r) => r.roe != null ? (r.roe * 100).toFixed(2) + '%' : '—' },
    gross_margin: { title: '毛利率', key: 'gross_margin', width: 80, sorter: 'default', render: (r) => r.gross_margin != null ? (r.gross_margin * 100).toFixed(2) + '%' : '—' },
    net_margin: { title: '净利率', key: 'net_margin', width: 80, sorter: 'default', render: (r) => r.net_margin != null ? (r.net_margin * 100).toFixed(2) + '%' : '—' },
    debt_ratio: { title: '负债率', key: 'debt_ratio', width: 80, sorter: 'default', render: (r) => r.debt_ratio != null ? (r.debt_ratio * 100).toFixed(2) + '%' : '—' },
    revenue_yoy: { title: '营收YoY', key: 'revenue_yoy', width: 90, sorter: 'default', render: (r) => r.revenue_yoy != null ? (r.revenue_yoy * 100).toFixed(2) + '%' : '—' },
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
      h(NButton, { size: 'tiny', type: 'error', style: 'margin-left: 4px', onClick: () => removeStock(r.stock_code, r.group_name) }, () => '移除'),
    ]),
  })
  return cols
})

async function fetchWatchlist() {
  loading.value = true
  try {
    const params = selectedGroup.value ? { group: selectedGroup.value } : {}
    const resp = await axios.get('/api/watchlist/list', { params })
    items.value = resp.data.items
    groups.value = resp.data.groups
  } catch (e: unknown) {
    const detail = isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '未知错误'
    message.error(`加载失败: ${detail}`)
  } finally {
    loading.value = false
  }
}

async function addStock() {
  if (!addStockCode.value.trim()) return
  try {
    await axios.post('/api/watchlist/add', { stock_code: addStockCode.value.trim(), group_name: addGroupName.value || 'default' })
    message.success(`已添加 ${addStockCode.value}`)
    addStockCode.value = ''
    fetchWatchlist()
  } catch (e: unknown) {
    const detail = isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '未知错误'
    message.error(`添加失败: ${detail}`)
  }
}

async function removeStock(code: string, group: string) {
  try {
    await axios.delete('/api/watchlist/remove', { data: { stock_code: code, group_name: group } })
    message.success(`已移除 ${code}`)
    fetchWatchlist()
  } catch (e: unknown) {
    const detail = isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '未知错误'
    message.error(`移除失败: ${detail}`)
  }
}

const totalCount = computed(() => items.value.length)

onMounted(fetchWatchlist)
</script>

<template>
  <div>
    <h2>自选列表</h2>
    <n-grid :cols="3" :x-gap="16" style="margin-bottom: 16px;">
      <n-grid-item><n-card size="small"><n-statistic label="总股票数" :value="totalCount" /></n-card></n-grid-item>
      <n-grid-item><n-card size="small"><n-statistic label="分组数" :value="groups.length" /></n-card></n-grid-item>
      <n-grid-item><n-card size="small"><n-statistic label="当前筛选" :value="selectedGroup || '全部'" /></n-card></n-grid-item>
    </n-grid>
    <n-card size="small" style="margin-bottom: 16px;">
      <n-space wrap>
        <n-select v-model:value="selectedGroup" :options="groupOptions" placeholder="选择分组" size="small" style="width: 200px;" @update:value="fetchWatchlist" />
        <n-input v-model:value="addStockCode" placeholder="输入股票代码" size="small" style="width: 150px;" @keyup.enter="addStock" />
        <n-input v-model:value="addGroupName" placeholder="分组名" size="small" style="width: 120px;" />
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
    <n-data-table v-if="items.length > 0" :columns="tableColumns" :data="items" :pagination="{ pageSize: 50 }" :scroll-x="1200" size="small" striped />
    <n-empty v-else description="自选列表为空，添加股票或从筛选结果加入" style="padding: 40px;" />
  </div>
</template>
