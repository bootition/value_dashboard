<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NButton, NEmpty, NSelect, NSpin, NTag } from 'naive-ui'
import axios from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import type {
  AnnouncementCategory,
  AnnouncementRow,
  AnnouncementsResponse,
} from '../types'

/**
 * 公告时间线（reports/119 §5 D 类 / 2026-11-14）
 *
 * 数据来自 `announcement_registry`（CNINFO 定期报告/权益分派类目差分登记），
 * 只含「已登记」的公告，不是全市场全量公告库。
 * - 展示日期用后端算好的 `announcement_date_cn`（北京时间自然日），
 *   不要把 UTC 的 `announcement_time` 截断成日期；
 * - `category` 是内部触发分类，UI 翻成中文 tag；
 * - 分页以 `has_more` 为准，offset 用已加载条数，避免重复/漏条。
 */

type TagType = 'default' | 'info' | 'success' | 'warning' | 'error'

const props = defineProps<{
  readonly stockCode: string
}>()

const DAY_OPTIONS = [
  { label: '最近 90 天', value: 90 },
  { label: '最近 180 天', value: 180 },
  { label: '最近 365 天', value: 365 },
  { label: '最近 3 年', value: 1095 },
]
const PAGE_SIZE = 20

const CATEGORY_LABELS: Readonly<Record<AnnouncementCategory, string>> = {
  financial: '定期报告/业绩预告',
  dividend: '分红',
  other: '其他',
}
const CATEGORY_TAG_TYPES: Readonly<Record<AnnouncementCategory, TagType>> = {
  financial: 'info',
  dividend: 'success',
  other: 'default',
}

const days = ref(365)
const items = ref<AnnouncementRow[]>([])
const meta = ref<AnnouncementsResponse | null>(null)
const loading = ref(false)
const loadingMore = ref(false)
const errorText = ref('')

// 换时间窗 / 换股票时丢弃旧分页响应，避免旧数据拼进新列表。
let requestId = 0

const hasMore = computed(() => meta.value?.has_more === true)
const total = computed(() => meta.value?.total ?? 0)
const byCategory = computed(() => meta.value?.by_category ?? null)
const disclaimer = computed(() => meta.value?.disclaimer ?? '')

function categoryLabel(category: AnnouncementCategory | string): string {
  return CATEGORY_LABELS[category as AnnouncementCategory] ?? CATEGORY_LABELS.other
}

function categoryTagType(category: AnnouncementCategory | string): TagType {
  return CATEGORY_TAG_TYPES[category as AnnouncementCategory] ?? 'default'
}

function itemKey(item: AnnouncementRow, index: number): string {
  return item.announcement_id ?? `${item.announcement_date_cn}-${item.title ?? 'item'}-${index}`
}

async function fetchPage(offset: number, append: boolean): Promise<void> {
  if (!props.stockCode) return
  const current = ++requestId
  if (append) {
    loadingMore.value = true
  } else {
    loading.value = true
    items.value = []
    meta.value = null
  }
  errorText.value = ''
  try {
    const resp = await axios.get<AnnouncementsResponse>('/api/research/announcements', {
      params: {
        code: props.stockCode,
        days: days.value,
        limit: PAGE_SIZE,
        offset,
      },
    })
    if (current !== requestId) return
    const payload = resp.data
    const rows = Array.isArray(payload?.items) ? payload.items : []
    items.value = append ? [...items.value, ...rows] : rows
    meta.value = payload
  } catch (e) {
    if (current !== requestId) return
    errorText.value = friendlyErrorMessage(e, '加载公告时间线失败')
    if (!append) {
      items.value = []
      meta.value = null
    }
  } finally {
    if (current === requestId) {
      loading.value = false
      loadingMore.value = false
    }
  }
}

function loadMore(): void {
  void fetchPage(items.value.length, true)
}

watch(days, () => {
  void fetchPage(0, false)
})

watch(
  () => props.stockCode,
  () => {
    void fetchPage(0, false)
  },
)

onMounted(() => {
  void fetchPage(0, false)
})
</script>

<template>
  <section class="timeline-card" aria-label="公告时间线">
    <header class="timeline-heading">
      <div>
        <p>ANNOUNCEMENTS</p>
        <h2>公告时间线</h2>
      </div>
      <div class="timeline-controls">
        <n-select
          v-model:value="days"
          :options="DAY_OPTIONS"
          size="small"
          style="width: 130px"
          aria-label="选择公告时间窗"
        />
      </div>
    </header>

    <p class="timeline-note">
      只包含已登记进公告库的公告（增量更新按 CNINFO 定期报告 / 权益分派类目差分登记），
      不是全市场全量公告；类别是按标题关键词的内部触发分类，不是交易所一级分类。
    </p>

    <n-spin :show="loading">
      <div v-if="errorText" class="timeline-error">{{ errorText }}</div>
      <div v-else-if="items.length === 0" class="timeline-empty">
        <n-empty description="时间窗内暂无已登记公告" />
      </div>
      <template v-else>
        <ol class="timeline-list">
          <li v-for="(item, index) in items" :key="itemKey(item, index)" class="timeline-item">
            <time class="timeline-date" :datetime="item.announcement_date_cn">
              {{ item.announcement_date_cn }}
            </time>
            <n-tag size="small" :type="categoryTagType(item.category)" :bordered="false">
              {{ categoryLabel(item.category) }}
            </n-tag>
            <!-- 2026-11-16：公告标题最长 101 字（announcement_registry 实测），
                 实际可用约 50 字，必须给 title 才能看全。 -->
            <span class="timeline-title" :title="item.title || '（无标题）'">{{ item.title || '（无标题）' }}</span>
          </li>
        </ol>
        <div class="timeline-footer">
          <span class="timeline-count">
            共 {{ total }} 条（最近 {{ days }} 天）
            <template v-if="byCategory">
              · {{ CATEGORY_LABELS.financial }} {{ byCategory.financial ?? 0 }}
              · {{ CATEGORY_LABELS.dividend }} {{ byCategory.dividend ?? 0 }}
              · {{ CATEGORY_LABELS.other }} {{ byCategory.other ?? 0 }}
            </template>
          </span>
          <n-button
            v-if="hasMore"
            class="timeline-more"
            size="small"
            :loading="loadingMore"
            @click="loadMore"
          >
            加载更多
          </n-button>
        </div>
      </template>
    </n-spin>

    <p v-if="disclaimer" class="timeline-disclaimer">{{ disclaimer }}</p>
  </section>
</template>

<style scoped>
.timeline-card {
  min-width: 0;
  padding: 22px 25px 25px;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 4px 17px rgba(48, 82, 59, 0.045);
}
.timeline-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.timeline-heading p {
  margin: 0;
  color: #91a097;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.13em;
}
.timeline-heading h2 {
  margin: 7px 0 0;
  font-size: 18px;
}
.timeline-controls {
  display: flex;
  align-items: center;
  gap: 10px;
}
.timeline-note {
  margin: 12px 0 14px;
  color: #85928a;
  font-size: 11px;
  line-height: 1.6;
}
.timeline-list {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
  border-top: 1px solid #edf1ee;
}
.timeline-item {
  display: grid;
  grid-template-columns: 92px auto minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  padding: 11px 2px;
  border-bottom: 1px solid #f1f4f2;
}
.timeline-date {
  color: #6f8076;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.timeline-title {
  overflow: hidden;
  color: #405a49;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.timeline-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.timeline-count {
  color: #91a097;
  font-size: 10px;
}
.timeline-error {
  padding: 18px;
  color: #c0392b;
  font-size: 12px;
}
.timeline-empty {
  padding: 22px 0 8px;
}
.timeline-disclaimer {
  margin: 12px 0 0;
  color: #a3ada6;
  font-size: 10px;
  line-height: 1.6;
}
@media (max-width: 720px) {
  .timeline-item {
    grid-template-columns: auto minmax(0, 1fr);
  }
  .timeline-date {
    grid-column: 1;
  }
  .timeline-title {
    grid-column: 1 / -1;
    white-space: normal;
  }
}
</style>
