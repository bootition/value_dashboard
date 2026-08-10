<script setup lang="ts">
import { computed } from 'vue'
import { NDataTable, NEmpty, NTag } from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import type {
  BreakdownItem,
  BusinessOverviewProvenance,
  BusinessOverviewResponse,
} from '../types/stock-detail.ts'
import { fmt } from '../utils/formatters.ts'

/**
 * 业务概览（reports/67/68 §6，东财 F10 低频独立域）。
 *
 * mode="overview"  → 首屏精简概览：一句话主营、报告期、产品/行业/地区前列标签。
 * mode="operations" → 经营章节明细：最近报告期主营构成表 + 溯源。
 *
 * 局部空态约定：profile/breakdown 的 status === "missing" 只渲染本地空态，
 * 绝不上升为页面级 stockUnavailable。
 */
const props = withDefaults(
  defineProps<{
    readonly data: BusinessOverviewResponse | null
    readonly mode?: 'overview' | 'operations'
  }>(),
  { mode: 'overview' },
)

const TYPE_ORDER = ['1', '2', '3'] as const
const TYPE_LABELS: Readonly<Record<string, string>> = { 1: '产品', 2: '行业', 3: '地区' }
const OVERVIEW_TOP = 5

const hasProfile = computed(() => props.data?.profile.status === 'ok')
const profile = computed(() => (hasProfile.value ? props.data!.profile : null))
const hasBreakdown = computed(
  () =>
    props.data?.breakdown.status === 'ok' &&
    props.data.breakdown.latest_report_date != null,
)
const breakdown = computed(() => (hasBreakdown.value ? props.data!.breakdown : null))
const latestReportDate = computed(() => breakdown.value?.latest_report_date ?? null)

/** 按固定类型顺序输出有数据的构成分组（后端已按 rank 排序）。 */
const compositionGroups = computed(() => {
  const groups: Array<{
    type: string
    label: string
    items: readonly BreakdownItem[]
  }> = []
  if (!breakdown.value) return groups
  for (const key of TYPE_ORDER) {
    const rows = breakdown.value.composition[key]
    if (rows && rows.length) {
      groups.push({ type: key, label: TYPE_LABELS[key] ?? `类型${key}`, items: rows })
    }
  }
  return groups
})

const topTagGroups = computed(() =>
  compositionGroups.value.map((group) => ({
    ...group,
    items: group.items.slice(0, OVERVIEW_TOP),
  })),
)

const compositionColumns: DataTableColumns<BreakdownItem> = [
  {
    title: '排名',
    key: 'rank',
    width: 70,
    render: (row) => (row.rank ?? '—').toString(),
  },
  {
    title: '项目',
    key: 'item_name',
    render: (row) => row.item_name ?? '—',
  },
  {
    title: '金额',
    key: 'amount',
    width: 130,
    render: (row) => fmt(row.amount, 0),
  },
  {
    title: '占比',
    key: 'ratio',
    width: 110,
    render: (row) => businessRatio(row.ratio),
  },
]

const overviewProv = computed(() => props.data?.provenance?.profile ?? null)
const breakdownProv = computed(() => props.data?.provenance?.breakdown ?? null)

function text(value: string | null | undefined): string {
  return value && value.length ? value : '—'
}

function confidenceLabel(value: string | null | undefined): string {
  if (!value || !value.length) return '—'
  if (value === 'approximate') return 'approximate'
  return value
}

function businessRatio(value: number | null): string {
  return value == null ? '—' : `${fmt(value)}%`
}

function provenanceNote(prov: BusinessOverviewProvenance | null): string {
  if (!prov) return '来源：— · 置信度：— · 抓取时间：—'
  return `来源：${text(prov.source)} · 置信度：${confidenceLabel(prov.confidence)} · 抓取时间：${text(prov.fetch_time)}`
}

const hasAnyContent = computed(() => hasProfile.value || hasBreakdown.value)
</script>

<template>
  <section
    class="business-overview-section"
    :class="mode === 'overview' ? 'business-overview-summary' : 'business-overview-operations'"
    aria-label="业务概览"
  >
    <header class="biz-heading">
      <div>
        <p>{{ mode === 'overview' ? 'BUSINESS OVERVIEW' : 'BUSINESS COMPOSITION' }}</p>
        <h2>{{ mode === 'overview' ? '业务概览' : '主营构成' }}</h2>
      </div>
      <div v-if="latestReportDate" class="biz-heading-period">
        <n-tag size="small">报告期 {{ latestReportDate }}</n-tag>
      </div>
    </header>

    <!-- 首屏精简概览 -->
    <template v-if="mode === 'overview'">
      <p v-if="profile?.profile" class="biz-summary-line">{{ profile.profile }}</p>
      <dl v-if="hasProfile" class="biz-meta">
        <div v-if="profile?.csrc_industry">
          <dt>所属行业</dt>
          <dd>{{ profile.csrc_industry }}</dd>
        </div>
        <div v-if="profile?.trade_market">
          <dt>上市板块</dt>
          <dd>{{ profile.trade_market }}</dd>
        </div>
        <div v-if="profile?.employee_num != null">
          <dt>员工数</dt>
          <dd>{{ fmt(profile.employee_num, 0) }}</dd>
        </div>
        <div v-if="profile?.scope">
          <dt>经营范围</dt>
          <dd class="biz-scope" :title="profile.scope">{{ profile.scope }}</dd>
        </div>
      </dl>

      <div v-for="group in topTagGroups" :key="group.type" class="biz-tag-group">
        <span class="biz-tag-label">{{ group.label }}</span>
        <div class="biz-tags">
          <n-tag
            v-for="item in group.items"
            :key="`${group.type}-${item.item_name ?? '未命名'}-${item.rank ?? ''}`"
            size="small"
            class="biz-tag"
          >
             {{ item.item_name ?? '—' }}<span v-if="item.ratio != null" class="biz-tag-ratio">{{
               businessRatio(item.ratio)
            }}</span>
          </n-tag>
        </div>
      </div>

      <div v-if="!hasAnyContent" class="biz-empty">
        <n-empty description="暂无业务概览数据" />
      </div>
    </template>

    <!-- 经营章节明细 -->
    <template v-else>
      <div v-if="compositionGroups.length" class="biz-comp-groups">
        <div v-for="group in compositionGroups" :key="group.type" class="biz-comp-group">
          <h3>{{ group.label }}</h3>
          <n-data-table
            size="small"
            striped
            :columns="compositionColumns"
            :data="[...group.items]"
            :pagination="{ pageSize: 10 }"
            :scroll-x="420"
          />
        </div>
      </div>
      <div v-else class="biz-empty">
        <n-empty description="暂无主营构成数据" />
      </div>
    </template>

    <p class="biz-provenance">
      {{
        mode === 'overview'
          ? provenanceNote(overviewProv)
          : provenanceNote(breakdownProv)
      }}
    </p>
  </section>
</template>

<style scoped>
.business-overview-section {
  min-width: 0;
}
.business-overview-summary {
  padding: 24px 27px;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 4px 17px rgba(48, 82, 59, 0.045);
}
.business-overview-operations {
  padding: 22px 25px 25px;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 4px 17px rgba(48, 82, 59, 0.045);
}
.biz-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.biz-heading p {
  margin: 0;
  color: #91a097;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.13em;
}
.biz-heading h2 {
  margin: 7px 0 0;
  color: #405a49;
  font-size: 18px;
}
.biz-heading-period {
  flex: 0 0 auto;
}
.biz-summary-line {
  margin: 2px 0 14px;
  color: #4a6152;
  font-size: 13px;
  line-height: 1.7;
}
.biz-meta {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin: 0 0 18px;
}
.biz-meta div {
  min-width: 0;
  padding: 11px 13px;
  border-radius: 9px;
  background: #fafcf9;
}
.biz-meta dt {
  color: #8a978e;
  font-size: 10px;
}
.biz-meta dd {
  overflow: hidden;
  margin: 5px 0 0;
  color: #405a49;
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.biz-meta .biz-scope {
  display: -webkit-box;
  white-space: normal;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-height: 1.5;
}
.biz-tag-group {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-top: 14px;
}
.biz-tag-label {
  flex: 0 0 44px;
  color: #839087;
  font-size: 11px;
  font-weight: 600;
}
.biz-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}
.biz-tag-ratio {
  margin-left: 5px;
  color: #609477;
  font-variant-numeric: tabular-nums;
}
.biz-comp-groups {
  display: grid;
  gap: 24px;
}
.biz-comp-group h3 {
  margin: 0 0 9px;
  color: #627368;
  font-size: 12px;
  font-weight: 600;
}
.biz-comp-group :deep(.n-data-table) {
  border: 1px solid #edf1ee;
  border-radius: 9px;
}
.biz-empty {
  padding: 6px 0 4px;
}
.biz-provenance {
  margin: 16px 0 0;
  color: #91a097;
  font-size: 10px;
}
@media (max-width: 1024px) {
  .biz-meta {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 640px) {
  .biz-meta {
    grid-template-columns: 1fr;
  }
  .biz-tag-group {
    flex-direction: column;
    gap: 6px;
  }
}
</style>
