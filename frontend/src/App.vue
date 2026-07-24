<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { NConfigProvider, NLayout, NLayoutHeader, NLayoutContent, NMenu, NMessageProvider, NDialogProvider } from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { h } from 'vue'
import { RouterLink, RouterView } from 'vue-router'

const route = useRoute()

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
  <n-config-provider>
    <n-message-provider>
      <n-dialog-provider>
        <n-layout position="absolute">
          <n-layout-header bordered style="height: 56px; display: flex; align-items: center; padding: 0 24px;">
            <span style="font-size: 18px; font-weight: 600; margin-right: 48px;">
              Value Dashboard
            </span>
            <n-menu mode="horizontal" :options="menuOptions" :value="activeKey" />
          </n-layout-header>
          <n-layout-content style="padding: 24px;">
            <router-view />
          </n-layout-content>
        </n-layout>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
