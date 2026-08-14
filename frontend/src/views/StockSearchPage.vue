<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'

interface SearchResult {
  stock_code: string
  name: string
  exchange: string | null
  csrc_l1: string | null
}

const router = useRouter()
const query = ref('')
const results = ref<SearchResult[]>([])
const loading = ref(false)
const searched = ref(false)
const error = ref('')
let searchTimer: ReturnType<typeof setTimeout> | undefined
// 2026-08-14 红队 P3：响应乱序竞态防护——每次请求递增序号，
// 过期响应/异常一律丢弃，只让最新一次请求落地。
let requestSeq = 0

async function search() {
  const term = query.value.trim()
  if (!term) {
    requestSeq += 1
    results.value = []
    searched.value = false
    error.value = ''
    loading.value = false
    return
  }
  const seq = ++requestSeq
  loading.value = true
  error.value = ''
  try {
    const response = await axios.get<{ items: SearchResult[] }>(
      '/api/stock/search',
      { params: { query: term }, timeout: 10000 },
    )
    if (seq !== requestSeq) return
    results.value = response.data.items
    searched.value = true
  } catch (err) {
    if (seq !== requestSeq) return
    results.value = []
    searched.value = true
    error.value = axios.isAxiosError(err) && err.code === 'ECONNABORTED'
      ? '搜索超时，请稍后重试'
      : '搜索失败，请稍后重试'
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

watch(query, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => void search(), 250)
})

function openStock(code: string) {
  void router.push(`/stock/${encodeURIComponent(code)}`)
}
</script>

<template>
  <section class="stock-search-page">
    <div class="stock-search-center">
    <div class="stock-search-heading">
      <p class="page-eyebrow">STOCK RESEARCH</p>
      <h1>个股详情</h1>
      <p>输入股票代码或名称，选择公司后进入详情研究。</p>
    </div>
    <div class="stock-search-card">
      <label for="stock-search-input">搜索股票</label>
      <div class="search-input-wrap">
        <span aria-hidden="true">⌕</span>
        <input id="stock-search-input" v-model="query" autofocus placeholder="例如：600519、贵州茅台" autocomplete="off">
        <span v-if="loading" class="search-loading">搜索中</span>
      </div>
      <p class="search-hint">支持股票代码或公司名称的部分匹配，共覆盖 5,000+ 家上市公司。</p>
      <p v-if="error" class="search-error" role="alert">{{ error }}</p>
      <div v-if="searched" class="search-results" aria-live="polite">
        <p v-if="results.length === 0" class="no-results">未找到匹配的上市公司，请检查代码或名称。</p>
        <button v-for="stock in results" :key="stock.stock_code" type="button" @click="openStock(stock.stock_code)">
          <span class="search-stock-name"><strong>{{ stock.name }}</strong><small>{{ stock.stock_code }} · {{ stock.exchange || '—' }}</small></span>
          <span class="search-industry">{{ stock.csrc_l1 || '行业待补充' }}</span>
          <span class="search-arrow">→</span>
        </button>
      </div>
    </div>
    </div>
  </section>
</template>

<style scoped>
.stock-search-page { width: 100%; min-height: calc(100vh - 95px); display: grid; place-items: center; }.stock-search-center { width: min(720px, 100%); margin-top: -70px; text-align: center; }.page-eyebrow { margin: 0; color: #8ba096; font-size: 10px; font-weight: 800; letter-spacing: .13em; }.stock-search-heading h1 { margin: 7px 0; color: #2c4034; font-size: 28px; letter-spacing: -.05em; }.stock-search-heading > p:last-child { margin: 0; color: #839088; font-size: 13px; }.stock-search-card { margin-top: 27px; padding: 29px; border-radius: 16px; background: #fff; box-shadow: 0 8px 24px rgba(48, 82, 59, .055); text-align: left; }.stock-search-card label { display: block; margin-bottom: 10px; color: #52685a; font-size: 12px; font-weight: 700; }.search-input-wrap { display: flex; align-items: center; gap: 10px; padding: 0 14px; border: 1px solid #dce9df; border-radius: 10px; background: #fbfdfb; }.search-input-wrap:focus-within { border-color: #a9ceb4; box-shadow: 0 0 0 3px #edf7ef; }.search-input-wrap > span:first-child { color: #77a888; font-size: 23px; line-height: 1; }.search-input-wrap input { width: 100%; padding: 14px 0; border: 0; outline: 0; background: transparent; color: #31463a; font: inherit; font-size: 14px; }.search-loading { color: #83a08b; font-size: 10px; white-space: nowrap; }.search-hint { margin: 10px 0 0; color: #95a198; font-size: 10px; }.search-error { margin: 10px 0 0; color: #b3543f; font-size: 11px; }.search-results { margin-top: 22px; overflow: hidden; border: 1px solid #edf1ee; border-radius: 10px; }.search-results button { display: grid; grid-template-columns: minmax(0, 1fr) 160px 20px; align-items: center; gap: 16px; width: 100%; padding: 14px 16px; border: 0; border-top: 1px solid #edf1ee; background: #fff; color: #4c6153; font: inherit; text-align: left; cursor: pointer; }.search-results button:first-of-type { border-top: 0; }.search-results button:hover { background: #f5faf6; }.search-stock-name strong, .search-stock-name small { display: block; }.search-stock-name strong { color: #354b3c; font-size: 12px; }.search-stock-name small { margin-top: 4px; color: #96a198; font-size: 10px; }.search-industry { color: #829087; font-size: 11px; }.search-arrow { color: #77a888; font-size: 15px; }.no-results { margin: 0; padding: 20px; color: #87958b; font-size: 12px; }
</style>
