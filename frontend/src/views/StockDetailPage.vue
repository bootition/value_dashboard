<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  NAlert, NButton, NResult, NSpace, NSpin, NTag,
  useMessage,
} from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { isIndicatorUntrusted } from '../types/data-quality.ts'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import KlineChartCard from '../components/KlineChartCard.vue'
import IndicatorGroupSection from '../components/IndicatorGroupSection.vue'
import StockTocNav from '../components/StockTocNav.vue'
import type { TocItem } from '../components/StockTocNav.vue'
import BusinessOverviewSection from '../components/BusinessOverviewSection.vue'
import ResearchStatisticsCard from '../components/ResearchStatisticsCard.vue'
import MetricHistoryChart from '../components/MetricHistoryChart.vue'
import DataFreshnessCard from '../components/DataFreshnessCard.vue'
import { fmt } from '../utils/formatters.ts'
import { loadKlineSettings, pageStorage, saveKlineSettings } from '../utils/kline-settings.ts'
import type {
  StockInfo,
  IndicatorsResponse,
  KlineResponse,
  KlinePeriod,
  BusinessOverviewResponse,
} from '../types/stock-detail.ts'
import type { WarningCode } from '../types/data-quality.ts'

const route = useRoute()
const message = useMessage()

const stockCode = computed(() => {
  const code = route.params.code
  return typeof code === 'string' ? code.trim() : ''
})
const hasStockCodeError = computed(() => !stockCode.value)
const loading = ref(false)
// L0-7（报告42）: 无效股票/无数据时呈现友好结果态，而非一页横线
const stockUnavailable = ref(false)
const generation = ref(0)
const stockInfo = ref<StockInfo | null>(null)
const indicators = ref<IndicatorsResponse | null>(null)
const klineData = ref<KlineResponse>({ candles: [] })
const businessOverview = ref<BusinessOverviewResponse | null>(null)
const warningCodes = ref<readonly WarningCode[]>([])

// K线配置（localStorage 全局记忆，非法值回退默认）
const initialKlineSettings = loadKlineSettings(pageStorage())
const klinePeriod = ref<KlinePeriod>(initialKlineSettings.period)
const adjustMode = ref<'raw' | 'qfq'>(initialKlineSettings.adjust)
const klineAbortController = ref<AbortController | null>(null)

// 财务趋势配置

const hasUntrustedIndicators = computed(
  () => warningCodes.value.length > 0 && isIndicatorUntrusted('*', warningCodes.value),
)

// reports/76 P1-2: 自动更新窗口由服务端标注（避免把窗口误读为数据不可信）
const autoUpdateInProgress = computed(
  () => indicators.value?.auto_update_in_progress === true,
)

// ─── 数据获取 ───────────────────────────────────────────────────────────
async function fetchAll() {
  if (hasStockCodeError.value) return
  const gen = ++generation.value
  loading.value = true
  stockUnavailable.value = false
  try {
    await Promise.all([
      fetchStockInfo(gen),
      fetchIndicators(gen),
      fetchKline(gen),
      fetchBusinessOverview(gen),
    ])
  } finally {
    loading.value = false
    nextTick(updateActiveSection)
  }
}

async function fetchStockInfo(gen: number) {
  try {
    const resp = await axios.get<StockInfo>(`/api/stock/${stockCode.value}/info`)
    if (gen !== generation.value) return
    stockInfo.value = resp.data
  } catch (e) {
    if (gen !== generation.value) return
    stockInfo.value = null
    if (isAxiosError(e) && e.response?.status === 404) {
      stockUnavailable.value = true
      return
    }
    message.warning(friendlyErrorMessage(e, '加载股票信息失败'))
  }
}

async function fetchIndicators(gen: number) {
  try {
    const resp = await axios.get<IndicatorsResponse>(`/api/stock/${stockCode.value}/indicators`)
    if (gen !== generation.value) return
    indicators.value = resp.data
    // 服务端权威信任信号优先；缺失时回退到数据状态页轮询
    if (resp.data.trust) {
      warningCodes.value = resp.data.trust.warning_codes
      return
    }
  } catch (e) {
    if (gen !== generation.value) return
    indicators.value = null
    if (isAxiosError(e) && e.response?.status === 404) {
      stockUnavailable.value = true
      return
    }
    message.warning(friendlyErrorMessage(e, '加载指标数据失败'))
  }
  await fetchWarningCodes(gen)
}

async function fetchKline(gen: number) {
  if (klineAbortController.value) {
    klineAbortController.value.abort()
  }
  klineAbortController.value = new AbortController()
  try {
    const resp = await axios.get<KlineResponse>(`/api/stock/${stockCode.value}/kline`, {
      params: {
        adjust: adjustMode.value,
        period: klinePeriod.value,
      },
      signal: klineAbortController.value.signal,
    })
    if (gen !== generation.value) return
    klineData.value = resp.data
  } catch (e) {
    if (axios.isCancel(e) || gen !== generation.value) return
    klineData.value = { candles: [] }
    message.warning(friendlyErrorMessage(e, '加载K线数据失败'))
  }
}

async function fetchBusinessOverview(gen: number) {
  try {
    const resp = await axios.get<BusinessOverviewResponse>(
      `/api/stock/${stockCode.value}/business-overview`,
    )
    if (gen !== generation.value) return
    businessOverview.value = resp.data
  } catch (e) {
    if (gen !== generation.value) return
    businessOverview.value = null
    // 局部缺失（profile/breakdown status=missing）在组件内就地空态，不升页面级
    if (isAxiosError(e) && e.response?.status === 404) {
      stockUnavailable.value = true
      return
    }
    message.warning(friendlyErrorMessage(e, '加载业务概览失败'))
  }
}

async function fetchWarningCodes(gen: number) {
  try {
    const resp = await axios.get<{ data_quality: { warning_codes: readonly WarningCode[] } }>(
      '/api/data-status/summary',
    )
    if (gen !== generation.value) return
    warningCodes.value = resp.data.data_quality.warning_codes
  } catch {
    if (gen !== generation.value) return
    warningCodes.value = ['LINEAGE_INVALID']
  }
}

// ─── 图表指标下拉（一处一图，只保留有历史意义的指标）──────────────────
const operationsMetricOptions = [
  { label: 'ROE', value: 'roe' },
  { label: 'ROA', value: 'roa' },
  { label: '毛利率', value: 'gross_margin' },
  { label: '净利率', value: 'net_margin' },
  { label: 'CF/净利润', value: 'cf_to_net_profit' },
  { label: '营收YoY', value: 'revenue_yoy' },
  { label: '净利YoY', value: 'net_profit_yoy' },
  { label: '扣非YoY', value: 'deducted_profit_yoy' },
]
const safetyMetricOptions = [
  { label: '资产负债率', value: 'debt_ratio' },
  { label: '流动比率', value: 'current_ratio' },
  { label: '速动比率', value: 'quick_ratio' },
  { label: 'CF/净利润', value: 'cf_to_net_profit' },
]

// ─── 粘性目录与滚动监听 ─────────────────────────────────────────────────
const tocItems: readonly TocItem[] = [
  { id: 'overview', label: '概览' },
  { id: 'valuation', label: '估值与市场' },
  { id: 'operations', label: '经营与成长' },
  { id: 'safety', label: '财务安全' },
  { id: 'return', label: '股东回报' },
]
const activeSection = ref('overview')
const sectionEls = ref<Record<string, HTMLElement | null>>({})
// Naive UI 的实际滚动容器是 .n-layout-scroll-container，不是 window；
// 粘性目录的高亮与滚动监听必须挂到该容器，否则 window.scrollY 恒为 0。
const scrollContainer = ref<HTMLElement | null>(null)
// 嵌套 n-layout-scroll-container 会让 CSS sticky 失效，因此用 scroll 监听
// 手动 translateY，把目录固定在视口顶部附近（仅桌面宽度生效）。
const tocNaturalTop = ref(0)
const tocShift = ref(0)

function setSectionRef(id: string) {
  return (el: unknown) => {
    const node =
      el instanceof HTMLElement
        ? el
        : ((el as { $el?: HTMLElement } | null)?.$el ?? null)
    sectionEls.value[id] = node
  }
}

function scrollToSection(id: string) {
  const el = sectionEls.value[id]
  if (el) {
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    el.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' })
  }
  activeSection.value = id
}

function updateActiveSection() {
  const container = scrollContainer.value
  const scrollTop = container
    ? container.scrollTop
    : (window.scrollY || document.documentElement.scrollTop || 0)
  if (window.matchMedia('(min-width: 1025px)').matches && tocNaturalTop.value > 0) {
    tocShift.value = Math.max(0, scrollTop - (tocNaturalTop.value - 24))
  } else {
    tocShift.value = 0
  }
  if (scrollTop <= 0) {
    activeSection.value = tocItems[0].id
    return
  }
  const probe = scrollTop + 96
  let current = tocItems[0].id
  for (const item of tocItems) {
    const el = sectionEls.value[item.id]
    if (el && el.getBoundingClientRect().top + scrollTop <= probe) {
      current = item.id
    }
  }
  activeSection.value = current
}

// ─── 监听变化 ───────────────────────────────────────────────────────────
watch([klinePeriod, adjustMode], () => {
  saveKlineSettings(
    {
      period: klinePeriod.value,
      adjust: adjustMode.value,
    },
    pageStorage(),
  )
  fetchKline(generation.value)
})
watch(stockCode, fetchAll)

onMounted(() => {
  scrollContainer.value = document.querySelector<HTMLElement>('.n-layout-scroll-container')
  const tocEl = document.querySelector<HTMLElement>('.stock-toc-wrap')
  if (tocEl) tocNaturalTop.value = tocEl.getBoundingClientRect().top + (scrollContainer.value?.scrollTop ?? 0)
  scrollContainer.value?.addEventListener('scroll', updateActiveSection, { passive: true })
  window.addEventListener('scroll', updateActiveSection, { passive: true })
  updateActiveSection()
  fetchAll()
})

onUnmounted(() => {
  scrollContainer.value?.removeEventListener('scroll', updateActiveSection)
  window.removeEventListener('scroll', updateActiveSection)
  // 2026-08-14 红队 P3：卸载时中止在途请求，避免离开页面后
  // 响应落地写入已卸载组件状态 / 触发无意义告警。
  klineAbortController.value?.abort()
  klineAbortController.value = null
})
</script>

<template>
  <section class="stock-detail-page">
    <n-result
      v-if="hasStockCodeError"
      status="error"
      title="股票代码缺失"
      description="URL 中未提供股票代码，无法加载个股详情。请从筛选或自选列表进入。"
    />
    <!-- L0-7（报告42）: 无效股票/无数据时给出友好结果态与回退路径 -->
    <n-result
      v-else-if="stockUnavailable"
      status="404"
      title="股票不存在或暂无数据"
      description="请检查股票代码，或从筛选结果 / 自选列表进入个股详情。"
    >
      <template #footer>
        <n-space justify="center">
          <router-link to="/screening"><n-button type="primary">回到筛选</n-button></router-link>
          <router-link to="/watchlist"><n-button>回到自选列表</n-button></router-link>
        </n-space>
      </template>
    </n-result>
    <n-spin v-else :show="loading">
      <div class="detail-topbar">
        <router-link to="/stock" class="stock-search-back">← 返回个股搜索</router-link>
      </div>

      <div class="stock-detail-layout">
        <div class="stock-detail-main">
          <!-- 概览：身份 / 行情 / 数据状态 / 研究型 K 线 -->
          <section id="overview" :ref="setSectionRef('overview')" class="stock-section overview-section">
            <div class="identity-card">
              <div class="identity-row">
                <div class="identity-head">
                  <h1>{{ stockInfo?.name || stockCode }}</h1>
                  <span class="identity-code">{{ stockInfo?.stock_code || stockCode }}</span>
                  <n-tag v-if="stockInfo?.exchange" size="small">{{ stockInfo.exchange }}</n-tag>
                  <n-tag v-if="stockInfo?.is_st" size="small" type="warning">ST</n-tag>
                  <n-tag v-if="stockInfo?.is_suspended" size="small" type="error">停牌</n-tag>
                </div>
                <div class="identity-quote">
                  <strong class="quote-price">{{ fmt(stockInfo?.latest_close) }}</strong>
                  <span class="quote-date">最新收盘价 · {{ stockInfo?.latest_price_date || '—' }}</span>
                </div>
              </div>
              <dl class="identity-meta">
                <div class="identity-meta-item">
                  <dt>拼音</dt>
                  <dd>{{ stockInfo?.pinyin || '—' }}</dd>
                </div>
                <div class="identity-meta-item">
                  <dt>上市日期</dt>
                  <dd>{{ stockInfo?.listing_date || '—' }}</dd>
                </div>
                <div class="identity-meta-item">
                  <dt>证监会一级</dt>
                  <dd>{{ stockInfo?.csrc_l1 || '—' }}</dd>
                </div>
              </dl>
            </div>

            <BusinessOverviewSection :data="businessOverview" mode="overview" />

            <div class="data-status-block">
              <n-alert v-if="autoUpdateInProgress" type="info" :show-icon="true">
                数据正在自动更新，以下指标截至 {{ indicators?.latest_price_date ?? '最新价格日' }}，更新完成后自动恢复。详见<router-link to="/data-status">数据状态页</router-link>。
              </n-alert>
              <n-alert v-if="hasUntrustedIndicators" type="warning" :show-icon="true">
                当前数据库状态不可信，以下指标数据可能不准确或不完整。请先检查<router-link to="/data-status">数据状态页</router-link>。
              </n-alert>
              <DataFreshnessCard :freshness="indicators?.freshness ?? null" />
            </div>

            <KlineChartCard
              v-model:period="klinePeriod"
              v-model:adjust="adjustMode"
              :candles="klineData.candles"
            />
          </section>

          <!-- 估值与市场 -->
          <IndicatorGroupSection
            id="valuation"
            :ref="setSectionRef('valuation')"
            kicker="VALUATION & MARKET"
            title="估值与市场"
            :groups="[]"
          >
            <div class="section-inner-block">
              <ResearchStatisticsCard :stock-code="stockCode" default-metric="pe_ttm" />
            </div>
          </IndicatorGroupSection>

          <!-- 经营与成长 -->
          <IndicatorGroupSection
            id="operations"
            :ref="setSectionRef('operations')"
            kicker="OPERATIONS & GROWTH"
            title="经营与成长"
            :groups="[]"
          >
            <div class="section-inner-block">
              <MetricHistoryChart
                :stock-code="stockCode"
                :metric-options="operationsMetricOptions"
                default-metric="roe"
              />
            </div>
          </IndicatorGroupSection>

          <!-- 财务安全 -->
          <IndicatorGroupSection
            id="safety"
            :ref="setSectionRef('safety')"
            kicker="FINANCIAL SAFETY"
            title="财务安全"
            :groups="[]"
          >
            <div class="section-inner-block">
              <MetricHistoryChart
                :stock-code="stockCode"
                :metric-options="safetyMetricOptions"
                default-metric="debt_ratio"
              />
            </div>
          </IndicatorGroupSection>

          <!-- 股东回报 -->
          <IndicatorGroupSection
            id="return"
            :ref="setSectionRef('return')"
            kicker="SHAREHOLDER RETURN"
            title="股东回报"
            :groups="[]"
          >
            <div class="section-inner-block">
              <ResearchStatisticsCard :stock-code="stockCode" default-metric="ttm_dividend_yield" />
            </div>
          </IndicatorGroupSection>

        </div>

        <StockTocNav
          class="stock-toc-wrap"
          :style="{ transform: tocShift > 0 ? `translateY(${tocShift}px)` : undefined }"
          :items="tocItems"
          :active-id="activeSection"
          @navigate="scrollToSection"
        />
      </div>
    </n-spin>
  </section>
</template>

<style scoped>
.stock-detail-page {
  max-width: 1380px;
}
.detail-topbar {
  margin-bottom: 21px;
}
.stock-search-back {
  display: inline-block;
  color: #609477;
  font-size: 11px;
  text-decoration: none;
}
.stock-detail-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 196px;
  align-items: start;
  gap: 28px;
}
.stock-detail-main {
  display: grid;
  gap: 42px;
  min-width: 0;
}
.stock-toc-wrap {
  position: relative;
  top: 0;
  /* 目录靠右贴边，且与正文保持足够距离，避免遮住卡片内容 */
  justify-self: end;
  margin-right: 6px;
}
.section-heading p {
  margin: 0;
  color: #91a097;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.13em;
}
.section-heading h2 {
  margin: 7px 0 0;
  font-size: 20px;
}

/* 概览 */
.overview-section {
  display: grid;
  gap: 21px;
  scroll-margin-top: 24px;
}
.identity-card {
  padding: 24px 27px;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 4px 17px rgba(48, 82, 59, 0.045);
}
.identity-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}
.identity-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}
.identity-head h1 {
  margin: 0;
  font-size: 22px;
  letter-spacing: -0.04em;
}
.identity-code {
  color: #8a978e;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.identity-quote {
  flex: 0 0 auto;
  text-align: right;
}
.quote-price {
  display: block;
  color: #365944;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.05em;
}
.quote-date {
  display: block;
  margin-top: 4px;
  color: #8e9b92;
  font-size: 10px;
}
.identity-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 22px 0 0;
}
.identity-meta-item {
  min-width: 0;
  padding: 11px 13px;
  border-radius: 9px;
  background: #fafcf9;
}
.identity-meta-item dt {
  color: #8a978e;
  font-size: 10px;
}
.identity-meta-item dd {
  overflow: hidden;
  margin: 5px 0 0;
  color: #405a49;
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.data-status-block {
  display: grid;
  gap: 16px;
}
.overview-summaries {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}
.summary-panel {
  min-width: 0;
  padding: 17px 18px;
  border: 1px solid #edf1ee;
  border-radius: 13px;
  background: #fff;
  box-shadow: 0 3px 13px rgba(48, 82, 59, 0.04);
}
.summary-panel h2 {
  margin: 0 0 12px;
  color: #405a49;
  font-size: 13px;
}
.summary-panel dl,
.summary-panel dl div {
  margin: 0;
}
.summary-panel dl {
  display: grid;
  gap: 8px;
}
.summary-panel dl div {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.summary-panel dt {
  overflow: hidden;
  color: #8a978e;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.summary-panel dd {
  flex: 0 0 auto;
  margin: 0;
  color: #365944;
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.stock-section {
  scroll-margin-top: 24px;
}

/* 章节内容块 */
.section-inner-block {
  margin-top: 26px;
}
.custom-trend-block {
  padding: 22px 25px 25px;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 4px 17px rgba(48, 82, 59, 0.045);
}
.custom-trend-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  color: #839087;
  font-size: 11px;
}
.custom-trend-toolbar span {
  flex: 0 0 auto;
}
.sources-section :deep(.traceability-workbench) {
  box-shadow: none;
}

@media (max-width: 1024px) {
  .stock-detail-layout {
    grid-template-columns: minmax(0, 1fr);
  }
  .stock-toc-wrap {
    position: static;
    order: -1;
  }
  .overview-summaries {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 640px) {
  .identity-row {
    flex-direction: column;
  }
  .identity-quote {
    text-align: left;
  }
  .identity-meta {
    grid-template-columns: 1fr;
  }
  .overview-summaries {
    grid-template-columns: 1fr;
  }
}
</style>
