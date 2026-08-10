<script setup lang="ts">
/**
 * 粘性目录：锚点链接原生可聚焦、可回车触发（键盘可操作）；
 * 点击交由父组件做平滑滚动，当前章节高亮由父组件滚动监听驱动。
 */

export interface TocItem {
  readonly id: string
  readonly label: string
}

withDefaults(
  defineProps<{
    readonly items: readonly TocItem[]
    readonly activeId: string
    readonly title?: string
  }>(),
  { title: '本页目录' },
)

const emit = defineEmits<{
  (e: 'navigate', id: string): void
}>()
</script>

<template>
  <nav class="stock-toc" aria-label="本页章节导航">
    <p class="toc-title">{{ title }}</p>
    <a
      v-for="item in items"
      :key="item.id"
      class="toc-link"
      :class="{ active: item.id === activeId }"
      :href="`#${item.id}`"
      @click.prevent="emit('navigate', item.id)"
    >
      {{ item.label }}
    </a>
  </nav>
</template>

<style scoped>
.stock-toc {
  padding: 16px 14px;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 4px 17px rgba(48, 82, 59, 0.045);
}
.toc-title {
  margin: 0 8px 10px;
  color: #9aa69f;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.13em;
}
.toc-link {
  display: block;
  padding: 9px 10px;
  border-radius: 8px;
  color: #6e7d73;
  font-size: 12px;
  text-decoration: none;
}
.toc-link:hover {
  background: #f7faf7;
}
.toc-link:focus-visible {
  outline: 2px solid #57966d;
  outline-offset: 2px;
}
.toc-link.active {
  background: #eff8f1;
  color: #57966d;
  font-weight: 700;
}
@media (max-width: 1024px) {
  .stock-toc {
    display: flex;
    align-items: center;
    gap: 4px;
    overflow-x: auto;
    padding: 10px 12px;
  }
  .toc-title {
    flex: 0 0 auto;
    margin: 0 6px 0 0;
  }
  .toc-link {
    flex: 0 0 auto;
    padding: 7px 10px;
    white-space: nowrap;
  }
}
</style>
