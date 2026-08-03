<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import { NConfigProvider, NLayout, NLayoutHeader, NLayoutContent, NMenu, NMessageProvider, NDialogProvider, NTag, NSpace, zhCN, dateZhCN } from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { h } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import axios from 'axios'

const route = useRoute()

// L2 V6（报告42）: 品牌与可信度表达——全局数据就绪/更新徽标
type Summary = { data_quality: { ready: boolean; warning_codes: string[] } }
const quality = ref<{ ready: boolean; warning_codes: string[] } | null>(null)

onMounted(async () => {
  try {
    const resp = await axios.get<Summary>('/api/data-status/summary')
    quality.value = resp.data.data_quality
  } catch {
    quality.value = { ready: false, warning_codes: [] }
  }
})

const statusTag = computed(() => {
  const q = quality.value
  if (q === null) return { type: 'default' as const, label: '状态加载中…', color: undefined }
  if (!q.ready) return { type: 'error' as const, label: '数据未就绪', color: undefined }
  if (q.warning_codes.length > 0) return { type: 'warning' as const, label: `警告 ${q.warning_codes.length}`, color: undefined }
  return { type: 'success' as const, label: '数据就绪', color: undefined }
})

const menuOptions: MenuOption[] = [
  {
    label: () => h(RouterLink, { to: '/screening' }, { default: () => '筛选' }),
    key: 'screening',
  },
  {
    label: () => h(RouterLink, { to: '/watchlist' }, { default: () => '自选列表' }),
    key: 'watchlist',
  },
  {
    label: () => h(RouterLink, { to: '/data-status' }, { default: () => '数据状态' }),
    key: 'data-status',
  },
]

const activeKey = computed(() => route.name as string)
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN">
    <n-message-provider>
      <n-dialog-provider>
        <n-layout position="absolute">
          <!-- L1-6: 键盘用户跳过导航直达内容 -->
          <a href="#main-content" class="skip-link">跳到主要内容</a>
          <n-layout-header bordered style="height: 56px; display: flex; align-items: center; padding: 0 24px; gap: 24px;">
            <router-link to="/screening" style="text-decoration: none; display: flex; align-items: baseline; gap: 8px;">
              <!-- L2 V6: 品牌副标题强化研究定位 -->
              <span style="font-size: 18px; font-weight: 600; color: #1f2329;">Value Dashboard</span>
              <span style="font-size: 12px; color: #667085;">本地 A 股研究</span>
            </router-link>
            <n-menu mode="horizontal" :options="menuOptions" :value="activeKey" />
            <n-space style="margin-left: auto;">
              <!-- L2 V6: 数据就绪/更新状态小徽标，点击直达数据状态页 -->
              <router-link to="/data-status">
                <n-tag :type="statusTag.type" size="small" round>{{ statusTag.label }}</n-tag>
              </router-link>
            </n-space>
          </n-layout-header>
          <n-layout-content id="main-content" tabindex="-1" style="padding: 24px;">
            <router-view />
          </n-layout-content>
        </n-layout>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>