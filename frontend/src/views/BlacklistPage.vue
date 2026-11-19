<script setup lang="ts">
/**
 * 黑名单管理页。
 *
 * 为什么必须有这一页：黑名单股票被从搜索/筛选/自选里全部剔除之后，就没有
 * 正常路径再进入它的详情页了——没有这一页，误加黑名单就只能手输 URL 才能撤销。
 * 这是黑名单股票**唯一**允许显示名字的地方（运维自救面，不是研究面）。
 */
import { computed, h, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  NAlert, NButton, NDataTable, NEmpty, NResult, NSpace, NSpin, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import type { BlacklistItem, BlacklistListResponse } from '../types/blacklist.ts'

const message = useMessage()

const loading = ref(false)
const items = ref<ReadonlyArray<BlacklistItem>>([])
const removingCode = ref<string>('')

const count = computed(() => items.value.length)

async function fetchBlacklist() {
  loading.value = true
  try {
    const resp = await axios.get<BlacklistListResponse>('/api/blacklist/list')
    items.value = resp.data.items
  } catch (e) {
    message.error(friendlyErrorMessage(e, '加载黑名单失败'))
  } finally {
    loading.value = false
  }
}

async function removeStock(code: string) {
  if (removingCode.value) return
  removingCode.value = code
  try {
    await axios.delete('/api/blacklist/remove', { data: { stock_code: code } })
    message.success('已移出黑名单，该股票恢复显示')
    await fetchBlacklist()
  } catch (e) {
    if (isAxiosError(e) && e.response?.status === 404) {
      // 已被别处移出：刷新到真实状态，而不是报一个看不懂的错
      message.warning('该股票已不在黑名单中')
      await fetchBlacklist()
      return
    }
    message.error(friendlyErrorMessage(e, '移出黑名单失败'))
  } finally {
    removingCode.value = ''
  }
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  return value.replace('T', ' ').slice(0, 19)
}

const columns = computed<DataTableColumns<BlacklistItem>>(() => [
  {
    title: '股票代码',
    key: 'stock_code',
    width: 120,
    render: (row) => h('strong', row.stock_code),
  },
  {
    title: '名称',
    key: 'name',
    width: 160,
    render: (row) => row.name || '—',
  },
  {
    title: '交易所',
    key: 'exchange',
    width: 100,
    render: (row) => row.exchange || '—',
  },
  {
    title: '证监会一级',
    key: 'csrc_l1',
    width: 140,
    render: (row) => row.csrc_l1 || '—',
  },
  {
    title: '加入时间',
    key: 'created_at',
    width: 180,
    render: (row) => formatTime(row.created_at),
  },
  {
    title: '备注',
    key: 'reason',
    ellipsis: { tooltip: true },
    render: (row) => row.reason || '—',
  },
  {
    title: '操作',
    key: 'actions',
    width: 120,
    render: (row) =>
      h(
        NButton,
        {
          size: 'small',
          loading: removingCode.value === row.stock_code,
          disabled: Boolean(removingCode.value) && removingCode.value !== row.stock_code,
          onClick: () => removeStock(row.stock_code),
        },
        { default: () => '移出黑名单' },
      ),
  },
])

onMounted(fetchBlacklist)
</script>

<template>
  <section class="blacklist-page">
    <header class="blacklist-header">
      <div>
        <h1>黑名单</h1>
        <p class="blacklist-subtitle">
          这里的股票不会出现在筛选、搜索、自选、个股详情与导出结果里。
        </p>
      </div>
      <n-space>
        <n-button :loading="loading" @click="fetchBlacklist">刷新</n-button>
        <router-link to="/stock"><n-button>去个股搜索</n-button></router-link>
      </n-space>
    </header>

    <n-alert type="info" :show-icon="true" class="blacklist-note">
      加入黑名单只影响显示：它们的行情与财报数据仍然完整保留、自动更新照常抓取。
      因此移出黑名单后，历史数据与自选列表里的原有记录都会立刻回来。
    </n-alert>

    <n-spin :show="loading">
      <n-result
        v-if="!loading && count === 0"
        status="success"
        title="黑名单是空的"
        description="在个股详情页点击『加入黑名单』后，该股票会出现在这里。"
      >
        <template #footer>
          <n-space justify="center">
            <router-link to="/stock"><n-button type="primary">去个股搜索</n-button></router-link>
          </n-space>
        </template>
      </n-result>

      <template v-else>
        <p class="blacklist-count">共 {{ count }} 只</p>
        <n-data-table
          :columns="columns"
          :data="[...items]"
          :row-key="(row: BlacklistItem) => row.stock_code"
          :pagination="{ pageSize: 50 }"
        >
          <template #empty>
            <n-empty description="暂无黑名单股票" />
          </template>
        </n-data-table>
      </template>
    </n-spin>
  </section>
</template>

<style scoped>
.blacklist-page {
  padding: 24px 28px 40px;
  max-width: 1200px;
}

.blacklist-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.blacklist-header h1 {
  font-size: 20px;
  margin: 0 0 4px;
}

.blacklist-subtitle {
  margin: 0;
  font-size: 13px;
  color: #6b7a70;
}

.blacklist-note {
  margin-bottom: 16px;
}

.blacklist-count {
  margin: 0 0 8px;
  font-size: 13px;
  color: #6b7a70;
}
</style>
