<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NDataTable, NEmpty, NGrid, NGridItem, NSkeleton, NSpin, NTab, NTabs, NTag } from 'naive-ui'
import { RouterLink } from 'vue-router'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import type { DataTableColumns } from 'naive-ui'
import type { IndexOverviewItem } from '../types/index-dashboard.ts'
import EtfStrategyPanel from '../components/EtfStrategyPanel.vue'

const CACHE_KEY = 'vd-index-overview-v2'
const CACHE_TTL_MS = 5 * 60 * 1000
const VIEW_MODE_KEY = 'vd-index-view-mode'
const TAB_KEY = 'vd-index-category-tab'

const loading = ref(false)
const refreshing = ref(false)
const refreshFailed = ref(false)
const errorText = ref('')
const items = ref<IndexOverviewItem[]>([])
// 2026-09-10 用户要求：从行业指数详情返回后应保持"申万一级"标签，
// 而不是退回宽基——与视图模式一样用 localStorage 持久化。
const tab = ref<'broad' | 'industry'>(loadTab())
const viewMode = ref<'cards' | 'table'>(loadViewMode())
const mode = ref<'indices' | 'etf'>('indices')

/** 宽基按知名度/规模排序：沪深300 第一，全A指数紧随其后。 */
const BROAD_ORDER: Record<string, number> = {
  '000300': 1, 'ALL_A': 2, '000016': 3, '000905': 4, '000852': 5,
  '000906': 6, '000010': 7, '000009': 8, '399330': 9, '399673': 10,
  '000015': 11, '000903': 12, '399324': 13,
}

type CachedOverview = { savedAt: number; items: IndexOverviewItem[] }

function loadViewMode(): 'cards' | 'table' {
  try {
    return localStorage.getItem(VIEW_MODE_KEY) === 'table' ? 'table' : 'cards'
  } catch {
    return 'cards'
  }
}

function loadTab(): 'broad' | 'industry' {
  try {
    return localStorage.getItem(TAB_KEY) === 'industry' ? 'industry' : 'broad'
  } catch {
    return 'broad'
  }
}

watch(tab, (value) => {
  try {
    localStorage.setItem(TAB_KEY, value)
  } catch {
    // 隐私模式等场景下写入失败时保持内存态即可
  }
})

watch(viewMode, (value) => {
  try {
    localStorage.setItem(VIEW_MODE_KEY, value)
  } catch {
    /* localStorage 不可用时仅本次会话生效 */
  }
})

function readCache(): CachedOverview | null {
  try {
    const raw = sessionStorage.getItem(CACHE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as CachedOverview
    if (!Array.isArray(parsed.items)) return null
    if (Date.now() - parsed.savedAt > CACHE_TTL_MS) return null
    return parsed
  } catch {
    return null
  }
}

function writeCache() {
  try {
    sessionStorage.setItem(CACHE_KEY, JSON.stringify({ savedAt: Date.now(), items: items.value }))
  } catch {
    /* 缓存失败不影响展示 */
  }
}

const sortedItems = computed(() =>
  [...items.value].sort((left, right) => {
    if (left.category !== right.category) return left.category === 'broad' ? -1 : 1
    if (left.category === 'broad') {
      return (BROAD_ORDER[left.code] ?? 99) - (BROAD_ORDER[right.code] ?? 99)
    }
    return left.name.localeCompare(right.name, 'zh-CN')
  }),
)

const filtered = computed(() =>
  sortedItems.value.filter((item) => item.category === tab.value),
)

const dataAsOf = computed(() => {
  const dates = items.value.map((item) => item.latest_date).filter((d): d is string => !!d)
  if (dates.length === 0) return null
  return dates.reduce((latest, current) => (current > latest ? current : latest))
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

let generation = 0
let controller: AbortController | null = null

async function load(options: { background?: boolean } = {}) {
  const gen = ++generation
  controller?.abort()
  controller = new AbortController()
  if (options.background) {
    refreshing.value = true
  } else {
    loading.value = true
  }
  errorText.value = ''
  refreshFailed.value = false
  try {
    const resp = await axios.get<{ items: IndexOverviewItem[] }>('/api/index/overview', {
      signal: controller.signal,
      timeout: 15_000,
    })
    if (gen !== generation) return
    items.value = resp.data.items
    writeCache()
  } catch (error) {
    if (axios.isCancel(error) || gen !== generation) return
    if (options.background || items.value.length > 0) {
      // 已有缓存/旧数据可看时保留内容，只提示刷新失败，不切到空错误页。
      refreshFailed.value = true
    } else {
      errorText.value = isAxiosError(error) ? friendlyErrorMessage(error) : String(error)
    }
  } finally {
    if (gen === generation) {
      loading.value = false
      refreshing.value = false
    }
  }
}

onMounted(() => {
  const cached = readCache()
  if (cached) {
    items.value = cached.items
    void load({ background: true })
  } else {
    void load()
  }
})

onBeforeUnmount(() => {
  generation += 1
  controller?.abort()
})
</script>

<template>
  <main class="page">
    <header class="page-head">
      <h1>指数研究</h1>
      <p class="page-sub">宽基与申万一级行业的估值分位与 ERP（股权风险溢价）。分位窗口：近 10 年。</p>
    </header>

    <NTabs v-model:value="mode" type="segment" size="small" class="mode-tabs">
      <NTab name="indices">指数概览</NTab>
      <NTab name="etf">ETF 轮动策略</NTab>
    </NTabs>

    <EtfStrategyPanel v-if="mode === 'etf'" />

    <NSpin v-else :show="loading">
      <NEmpty v-if="errorText" :description="errorText">
        <template #extra>
          <NButton size="small" @click="load()">重新加载</NButton>
        </template>
      </NEmpty>

      <div v-else-if="loading && items.length === 0" class="skeleton-grid" aria-label="正在加载指数数据">
        <NSkeleton v-for="i in 6" :key="i" height="128px" />
      </div>

      <template v-else>
        <NAlert v-if="refreshFailed" type="warning" :show-icon="false" class="stale-alert">
          刷新失败，当前展示的是缓存/上次成功的数据。
        </NAlert>
        <div class="toolbar">
          <NTabs v-model:value="tab" type="line" size="small">
            <NTab name="broad">宽基/红利</NTab>
            <NTab name="industry">申万一级</NTab>
          </NTabs>
          <div class="toolbar-actions">
            <span class="refresh-meta">
              <template v-if="dataAsOf">数据截至 {{ dataAsOf }}</template>
              <template v-if="refreshing"> · 更新中…</template>
            </span>
            <button type="button" class="text-button" :disabled="refreshing" @click="load()">刷新</button>
            <button type="button" class="text-button" @click="viewMode = viewMode === 'cards' ? 'table' : 'cards'">
              {{ viewMode === 'cards' ? '查看对比表' : '查看卡片' }}
            </button>
          </div>
        </div>

        <NEmpty v-if="filtered.length === 0" description="暂无指数数据" />

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
                  <span>{{ item.cadence === 'monthly' ? '月度序列' : '日度序列' }} · {{ item.samples }} 样本</span>
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
.mode-tabs { margin-bottom: 14px; }
.stale-alert { margin-bottom: 12px; }
.toolbar { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 12px; }
.toolbar-actions { display: flex; gap: 8px; align-items: center; }
.refresh-meta { color: var(--text); font-size: 12px; white-space: nowrap; }
.text-button { border: 0; background: none; color: #57966d; cursor: pointer; font-size: 13px; padding: 4px; }
.text-button:disabled { color: var(--text); cursor: default; }
.skeleton-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
@media (max-width: 900px) {
  .skeleton-grid { grid-template-columns: 1fr; }
}
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
