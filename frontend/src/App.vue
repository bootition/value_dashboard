<script setup lang="ts">
import { ref, onMounted, computed, provide } from 'vue'
import { useRoute } from 'vue-router'
import { NConfigProvider, NLayout, NLayoutContent, NLayoutSider, NMessageProvider, NDialogProvider, zhCN, dateZhCN } from 'naive-ui'
import { RouterLink, RouterView } from 'vue-router'
import axios from 'axios'
import type { DataQualityStatus } from './types/data-quality.ts'
import AutoUpdateProgressBar from './components/AutoUpdateProgressBar.vue'
import {
  AUTO_UPDATE_STATUS_KEY,
  useAutoUpdateRunning,
} from './composables/useAutoUpdatePolling.ts'

const route = useRoute()

type Summary = { data_quality: DataQualityStatus }
const quality = ref<DataQualityStatus | null>(null)
const qualityFailed = ref(false)
// 自动更新状态轮询只此一份：useAutoUpdatePolling 内部用 usePollTimer 管生命周期，
// 更新中 10s / 空闲 60s；App 把同一份状态 provide 给页面（详情页据此显示进度），
// 页面不得再自建第二个轮询器。
const {
  autoUpdateRunning,
  autoUpdateStatus,
  refreshAutoUpdate,
  startAutoUpdatePolling,
} = useAutoUpdateRunning()
provide(AUTO_UPDATE_STATUS_KEY, autoUpdateStatus)
// 进度条显示期间给内容区留出底部高度（进度条本身是悬浮 + pointer-events:none）。
const updateBarActive = computed(() => autoUpdateRunning.value)

onMounted(async () => {
  try {
    const resp = await axios.get<Summary>('/api/data-status/summary')
    quality.value = resp.data.data_quality
  } catch {
    quality.value = null
    qualityFailed.value = true
  }
  // 先立即读一次再挂轮询链：徽标/进度条不能等 10s 才第一次出现。
  await refreshAutoUpdate()
  startAutoUpdatePolling()
})

const statusTag = computed(() => {
  const q = quality.value
  if (qualityFailed.value) return { type: 'error' as const, label: '状态读取失败', color: undefined }
  if (q === null) return { type: 'default' as const, label: '状态加载中…', color: undefined }
  if (q.minimum_data_readiness.checking) return { type: 'default' as const, label: '正在核对数据', color: undefined }
  if (!q.minimum_data_readiness.ready) {
    return autoUpdateRunning.value
      ? { type: 'warning' as const, label: '数据未就绪 · 更新中', color: undefined }
      : { type: 'error' as const, label: '数据未就绪', color: undefined }
  }
  if (autoUpdateRunning.value) {
    return q.warning_codes.length > 0
      ? { type: 'warning' as const, label: `数据就绪 · 更新中（警告 ${q.warning_codes.length}）`, color: undefined }
      : { type: 'info' as const, label: '数据就绪 · 更新中', color: undefined }
  }
  if (q.warning_codes.length > 0) return { type: 'warning' as const, label: `警告 ${q.warning_codes.length}`, color: undefined }
  return { type: 'success' as const, label: '数据就绪', color: undefined }
})

const menuItems = [
  { label: '筛选', route: '/screening', key: 'screening', icon: 'filter' },
  { label: '指数', route: '/index', key: 'index-dashboard', icon: 'index' },
  { label: '自选列表', route: '/watchlist', key: 'watchlist', icon: 'watchlist' },
  { label: '个股详情', route: '/stock', key: 'stock-search', icon: 'stock' },
  { label: '黑名单', route: '/blacklist', key: 'blacklist', icon: 'blacklist' },
  { label: '数据状态', route: '/data-status', key: 'data-status', icon: 'status' },
]

const activeKey = computed(() => {
  if (route.name === 'stock-detail') return 'stock-search'
  if (route.name === 'index-detail') return 'index-dashboard'
  return route.name as string
})

const themeOverrides = {
  common: {
    primaryColor: '#70a986',
    primaryColorHover: '#5d9774',
    primaryColorPressed: '#4c8764',
    primaryColorSuppl: '#70a986',
    borderRadius: '8px',
    borderRadiusSmall: '6px',
    fontSize: '13px',
  },
  Button: {
    textColorPrimary: '#3e7551',
    colorPrimary: '#c3dfca',
    colorHoverPrimary: '#afd4b9',
    colorPressedPrimary: '#9acaa8',
    borderPrimary: '1px solid #c3dfca',
  },
  Input: {
    border: '1px solid #dce9df',
    borderHover: '1px solid #a9ceb4',
    borderFocus: '1px solid #a9ceb4',
    boxShadowFocus: '0 0 0 3px #edf7ef',
  },
  Select: {
    border: '1px solid #dce9df',
    borderHover: '1px solid #a9ceb4',
    borderFocus: '1px solid #a9ceb4',
  },
}
</script>

<template>
  <n-config-provider :locale="zhCN" :date-locale="dateZhCN" :theme-overrides="themeOverrides">
    <n-message-provider>
      <n-dialog-provider>
        <n-layout position="absolute">
          <!-- L1-6: 键盘用户跳过导航直达内容 -->
          <a href="#main-content" class="skip-link">跳到主要内容</a>
          <n-layout has-sider class="app-shell">
            <n-layout-sider bordered :width="226" class="app-sidebar">
              <router-link to="/screening" class="app-brand"><span>V</span>value</router-link>
              <p class="app-nav-label">研究工具</p>
              <nav class="app-nav" aria-label="研究工具">
                <router-link v-for="item in menuItems" :key="item.key" :to="item.route" :class="{ active: activeKey === item.key }">
                  <i :class="`app-nav-icon icon-${item.icon}`"></i>{{ item.label }}
                </router-link>
              </nav>
              <div class="app-sidebar-foot">
                <div class="app-data-status"><i></i>{{ statusTag.label }}</div>
                <p>本地 A 股研究</p>
              </div>
            </n-layout-sider>
            <n-layout-content
              id="main-content"
              tabindex="-1"
              class="app-main-content"
              :class="{ 'app-main-content--with-update': updateBarActive }"
            >
              <router-view />
              <AutoUpdateProgressBar :status="autoUpdateStatus" />
            </n-layout-content>
          </n-layout>
        </n-layout>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style scoped>
/* 悬浮进度条不挡点击，但仍给内容留出高度，避免最后一行被卡片压住 */
.app-main-content--with-update {
  padding-bottom: 132px;
}
@media (max-width: 640px) {
  .app-main-content--with-update {
    padding-bottom: 156px;
  }
}
</style>
