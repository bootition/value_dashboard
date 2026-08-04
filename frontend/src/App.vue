<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import { NConfigProvider, NLayout, NLayoutContent, NLayoutSider, NMenu, NMessageProvider, NDialogProvider, zhCN, dateZhCN } from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { h } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import axios from 'axios'

const route = useRoute()

// L2 V6（报告42）: 品牌与可信度表达——全局数据就绪/更新徽标
type Summary = { data_quality: { ready: boolean; warning_codes: string[] } }
const quality = ref<{ ready: boolean; warning_codes: string[] } | null>(null)

onMounted(async () => {
  if (route.meta.staticPreview) return
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
    label: () => h(RouterLink, { to: '/stock' }, { default: () => '个股详情' }),
    key: 'stock-search',
  },
  {
    label: () => h(RouterLink, { to: '/data-status' }, { default: () => '数据状态' }),
    key: 'data-status',
  },
]

const activeKey = computed(() => route.name === 'stock-detail' ? 'stock-search' : route.name as string)
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN">
    <n-message-provider>
      <n-dialog-provider>
        <n-layout position="absolute">
          <!-- L1-6: 键盘用户跳过导航直达内容 -->
          <a v-if="!route.meta.staticPreview" href="#main-content" class="skip-link">跳到主要内容</a>
          <router-view v-if="route.meta.staticPreview" />
          <n-layout v-else has-sider class="app-shell">
            <n-layout-sider bordered :width="226" class="app-sidebar">
              <router-link to="/screening" class="app-brand"><span>V</span>value</router-link>
              <p class="app-nav-label">研究工具</p>
              <n-menu :options="menuOptions" :value="activeKey" />
              <div class="app-sidebar-foot">
                <router-link to="/data-status" class="app-data-status"><i></i>{{ statusTag.label }}</router-link>
                <p>本地 A 股研究</p>
              </div>
            </n-layout-sider>
            <n-layout-content id="main-content" tabindex="-1" class="app-main-content">
              <router-view />
            </n-layout-content>
          </n-layout>
        </n-layout>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
