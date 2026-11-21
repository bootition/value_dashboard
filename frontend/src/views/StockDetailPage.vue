<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  NAlert, NButton, NResult, NSpace, NSpin, NTag,
  useDialog, useMessage,
} from 'naive-ui'
import type { KlinePeriod } from '../types/stock-detail.ts'
import KlineChartCard from '../components/KlineChartCard.vue'
import IndicatorGroupSection from '../components/IndicatorGroupSection.vue'
import StockTocNav from '../components/StockTocNav.vue'
import BusinessOverviewSection from '../components/BusinessOverviewSection.vue'
import ResearchStatisticsCard from '../components/ResearchStatisticsCard.vue'
import PointInTimeCard from '../components/PointInTimeCard.vue'
import AnnouncementTimelineCard from '../components/AnnouncementTimelineCard.vue'
import MetricHistoryChart from '../components/MetricHistoryChart.vue'
import DataFreshnessCard from '../components/DataFreshnessCard.vue'
import { fmt } from '../utils/formatters.ts'
import { loadKlineSettings, pageStorage } from '../utils/kline-settings.ts'
// 2026-11-23 重构：本组件 805 行里「5 路并发 + 降级 + 黑名单操作」与
// 「粘性目录滚动跟随」两部分原样抽成 composable（函数体逐字未改）。
// 组件只剩：路由代码 → ref 绑定 → 模板。详见两个 composable 的头注释。
import { useStockDetailLoad } from '../composables/useStockDetailLoad.ts'
import { useTocFollow } from '../composables/useTocFollow.ts'

const route = useRoute()
const message = useMessage()
const dialog = useDialog()

const stockCode = computed(() => {
  const code = route.params.code
  return typeof code === 'string' ? code.trim() : ''
})
const hasStockCodeError = computed(() => !stockCode.value)

// 后台更新进度：由 App.vue 布局层唯一的自动更新轮询 provide 下来（不在这里发请求）。
// 没有注入（独立挂载/测试）时为 null，退回服务端在指标响应里标注的
// `auto_update_in_progress` 提示。

// K线配置（localStorage 全局记忆，非法值回退默认）
const initialKlineSettings = loadKlineSettings(pageStorage())
const klinePeriod = ref<KlinePeriod>(initialKlineSettings.period)
const adjustMode = ref<'raw' | 'qfq'>(initialKlineSettings.adjust)

// ─── 粘性目录与滚动监听 ─────────────────────────────────────────────────
const {
  tocItems, activeSection, tocShift, setSectionRef, scrollToSection, updateActiveSection,
} = useTocFollow()

// ─── 数据获取 ───────────────────────────────────────────────────────────
const {
  loading, stockUnavailable, stockBlacklisted, blacklistBusy,
  stockInfo, indicators, klineData, businessOverview,
  hasUntrustedIndicators, autoUpdateInProgress,
  servedFromCache, cacheAsOf,
  confirmAddToBlacklist, removeFromBlacklist,
} = useStockDetailLoad({
  stockCode,
  hasStockCodeError,
  klinePeriod,
  adjustMode,
  message,
  dialog,
  updateActiveSection,
})

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
</script>

<template>
  <section class="stock-detail-page" :data-detail-loading="loading ? '1' : '0'" :data-detail-cached="servedFromCache ? '1' : '0'">
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
    <!-- 黑名单屏蔽态：内容全隐藏，但保留「移出」这条路——否则误加后无法自救 -->
    <n-result
      v-else-if="stockBlacklisted"
      status="warning"
      title="已加入黑名单"
      :description="`股票 ${stockCode} 已加入黑名单，其内容不再显示。原始行情与财报数据仍保留在库中，自动更新照常抓取。`"
    >
      <template #footer>
        <n-space justify="center">
          <n-button type="primary" :loading="blacklistBusy" @click="removeFromBlacklist">
            移出黑名单
          </n-button>
          <router-link to="/blacklist"><n-button>黑名单管理</n-button></router-link>
          <router-link to="/screening"><n-button>回到筛选</n-button></router-link>
        </n-space>
      </template>
    </n-result>
    <n-spin v-else :show="loading">
      <div class="detail-topbar">
        <router-link to="/stock" class="stock-search-back">← 返回个股搜索</router-link>
      </div>
      <!-- stale-while-revalidate 标注（2026-11-20）：首帧来自上一次成功响应时
           必须把「数据截至」摆出来，不能让旧快照冒充刚取到的新数据。 -->
      <p v-if="servedFromCache" id="detail-cache-note" class="detail-cache-note">
        已即时显示上次成功加载的内容{{ cacheAsOf ? `（数据截至 ${cacheAsOf}）` : '' }}，正在后台刷新…
      </p>
      <p class="caliber-note">
        口径提示：本页默认是重述回看口径（最新重述值），回测请用『当时可见』。
      </p>

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
                <div class="identity-actions">
                  <n-button
                    size="small"
                    quaternary
                    :loading="blacklistBusy"
                    @click="confirmAddToBlacklist"
                  >
                    加入黑名单
                  </n-button>
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
            <!-- reports/119 §5 D 类：时点可见财务值 + 公告时间线（挨着历史研究统计） -->
            <div id="point-in-time" :ref="setSectionRef('point-in-time')" class="section-inner-block stock-section">
              <PointInTimeCard :stock-code="stockCode" />
            </div>
            <div id="announcements" :ref="setSectionRef('announcements')" class="section-inner-block stock-section">
              <AnnouncementTimelineCard :stock-code="stockCode" />
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
/* stale-while-revalidate 说明条（首帧来自缓存时才出现） */
.detail-cache-note {
  margin: 4px 0 8px;
  padding: 6px 10px;
  border-radius: 6px;
  background: #f2f7f3;
  color: #4b6b57;
  font-size: 12px;
}
/* 后台更新进行中的页内提示条（非遮罩，读功能不受影响） */
.stock-detail-page {
  max-width: 1380px;
}
.detail-topbar {
  margin-bottom: 21px;
}
.caliber-note {
  margin: -10px 0 18px;
  padding: 9px 12px;
  border-radius: 9px;
  background: #f3f8f3;
  color: #6c8074;
  font-size: 11px;
  line-height: 1.6;
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
.identity-actions {
  flex: 0 0 auto;
  padding-top: 2px;
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
