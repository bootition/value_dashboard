<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NCard, NDataTable, NEmpty, NGrid, NGridItem, NSpin, NTab, NTabs, NTag } from 'naive-ui'
import { RouterLink } from 'vue-router'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import type { DataTableColumns } from 'naive-ui'
import type { IndexOverviewItem } from '../types/index-dashboard.ts'

const loading = ref(false)
const errorText = ref('')
const items = ref<IndexOverviewItem[]>([])
const tab = ref<'all' | 'broad' | 'industry'>('all')
const viewMode = ref<'cards' | 'table'>('cards')

const filtered = computed(() => {
  if (tab.value === 'all') return items.value
  return items.value.filter((item) => item.category === tab.value)
})

function zone(percentile: number | null): { type: 'success' | 'warning' | 'default' | 'error'; label: string } {
  if (percentile == null) return { type: 'default', label: '—' }
  if (percentile < 20) return { type: 'success', label: `低估 ${percentile.toFixed(0)}%` }
  if (percentile > 80) return { type: 'error', label: `高估 ${percentile.toFixed(0)}%` }
  return { type: 'warning', label: `中性 ${percentile.toFixed(0)}%` }
}

function fmt(value: number | null, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toFixed(digits)
}

const columns: DataTableColumns<IndexOverviewItem> = [
  { title: '指数', key: 'name', render: (row) => `${row.name}（${row.code}）` },
  { title: '类型', key: 'category', render: (row) => (row.category === 'broad' ? '宽基/红利' : '申万一级') },
  { title: 'PE', key: 'pe', render: (row) => fmt(row.pe) },
  { title: 'PE分位', key: 'pe_percentile', render: (row) => `${fmt(row.pe_percentile, 0)}%` },
  { title: 'PB', key: 'pb', render: (row) => fmt(row.pb) },
  { title: 'PB分位', key: 'pb_percentile', render: (row) => `${fmt(row.pb_percentile, 0)}%` },
  { title: 'ERP', key: 'erp', render: (row) => (row.erp == null ? '—' : `${row.erp.toFixed(2)}%`) },
  { title: 'ERP分位', key: 'erp_percentile', render: (row) => `${fmt(row.erp_percentile, 0)}%` },
  { title: '数据截至', key: 'latest_date' },
]

onMounted(async () => {
  loading.value = true
  try {
    const resp = await axios.get<{ items: IndexOverviewItem[] }>('/api/index/overview')
    items.value = resp.data.items
  } catch (error) {
    errorText.value = isAxiosError(error) ? friendlyErrorMessage(error) : String(error)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <main class="page">
    <header class="page-head">
      <h1>指数研究</h1>
      <p class="page-sub">宽基与申万一级行业的估值分位与 ERP（股权风险溢价）。分位窗口：近 10 年。</p>
    </header>

    <NSpin :show="loading">
      <NEmpty v-if="errorText" :description="errorText" />
      <template v-else>
        <div class="toolbar">
          <NTabs v-model:value="tab" type="line" size="small">
            <NTab name="all">全部</NTab>
            <NTab name="broad">宽基/红利</NTab>
            <NTab name="industry">申万一级</NTab>
          </NTabs>
          <div class="toolbar-actions">
            <button type="button" class="text-button" @click="viewMode = viewMode === 'cards' ? 'table' : 'cards'">
              {{ viewMode === 'cards' ? '查看对比表' : '查看卡片' }}
            </button>
          </div>
        </div>

        <NEmpty v-if="!loading && filtered.length === 0" description="暂无指数数据" />

        <NDataTable v-if="viewMode === 'table'" :columns="columns" :data="filtered" :bordered="false" size="small" />

        <NGrid v-else :cols="3" :x-gap="12" :y-gap="12" responsive="screen" item-responsive>
          <NGridItem v-for="item in filtered" :key="item.code" span="3 m:1">
            <RouterLink :to="`/index/${item.code}`" class="index-card-link">
              <NCard size="small" class="index-card" :class="{ unavailable: item.status === 'unavailable' }">
                <div class="card-head">
                  <div>
                    <span class="card-name">{{ item.name }}</span>
                    <span class="card-code">{{ item.code }}</span>
                  </div>
                  <NTag :type="zone(item.pe_percentile).type" size="small" :bordered="false">
                    {{ zone(item.pe_percentile).label }}
                  </NTag>
                </div>
                <div class="card-grid">
                  <div class="metric"><span class="metric-label">PE</span><span class="metric-value">{{ fmt(item.pe) }}</span></div>
                  <div class="metric"><span class="metric-label">PB</span><span class="metric-value">{{ fmt(item.pb) }}</span></div>
                  <div class="metric"><span class="metric-label">ERP</span><span class="metric-value">{{ item.erp == null ? '—' : `${fmt(item.erp)}%` }}</span></div>
                  <div class="metric"><span class="metric-label">ERP分位</span><span class="metric-value">{{ `${fmt(item.erp_percentile, 0)}%` }}</span></div>
                </div>
                <div class="card-foot">
                  <span>{{ item.category === 'broad' ? '月度序列' : '日度序列' }} · {{ item.samples }} 样本</span>
                  <span>{{ item.latest_date ?? '无数据' }}</span>
                </div>
              </NCard>
            </RouterLink>
          </NGridItem>
        </NGrid>
      </template>
    </NSpin>
  </main>
</template>

<style scoped>
.page { padding: 24px; }
.page-head h1 { margin: 0 0 4px; font-size: 22px; color: var(--text-h); }
.page-sub { margin: 0 0 16px; color: var(--text); font-size: 13px; }
.toolbar { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 12px; }
.toolbar-actions { display: flex; gap: 8px; }
.text-button { border: 0; background: none; color: #57966d; cursor: pointer; font-size: 13px; padding: 4px; }
.index-card-link { text-decoration: none; display: block; }
.index-card { height: 100%; }
.index-card:hover { border-color: #a9ceb4; }
.index-card.unavailable { opacity: 0.55; }
.card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.card-name { font-weight: 700; color: var(--text-h); margin-right: 6px; }
.card-code { color: var(--text); font-size: 12px; }
.card-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.metric { display: flex; flex-direction: column; gap: 2px; }
.metric-label { color: var(--text); font-size: 11px; }
.metric-value { color: var(--text-h); font-weight: 600; font-variant-numeric: tabular-nums; }
.card-foot { display: flex; justify-content: space-between; color: var(--text); font-size: 11px; margin-top: 10px; }
</style>
