<script setup lang="ts">
import { NResult, NButton } from 'naive-ui'
</script>

<template>
  <div style="padding: 48px 0;">
    <n-result
      status="404"
      title="页面不存在"
      description="您访问的页面不存在或已被移除"
    >
      <template #footer>
        <n-button type="primary" @click="$router.push('/screening')">返回筛选页</n-button>
      </template>
    </n-result>
  </div>
</template>
