<script setup lang="ts">
/**
 * 全局自动更新进度条（App.vue 布局层固定挂载，任何页面都能看到）。
 *
 * 数据来源是 App.vue 唯一那份 `useAutoUpdateRunning()` 轮询结果（本组件**不发请求**）。
 * 显示规则（用户原话："更新中要有进度，更新完就不用了；失败也不能静默"）：
 * - 更新中：阶段中文名 + 已完成/总数 + 百分比 + 已耗时（有 live 细粒度进度时）；
 *   只有阶段、还没有 done/total 时显示不确定进度条，而不是空白。
 * - 更新失败：红色错误条，显示 `last_error`，可手动关闭。
 * - 刚结束（running → finished）：显示"数据更新完成"30 秒后自动消失。
 * - 其余（空闲）：什么都不渲染。
 *
 * 不挡操作：整条是 `position: fixed` 的悬浮卡，根节点 `pointer-events: none`，
 * 只有关闭按钮可点击；App.vue 在显示期间给内容区加底部留白。
 */
import { computed, ref, watch } from 'vue'
import {
  buildCompletedView,
  buildUpdateProgressView,
  isAutoUpdateRunning,
} from '../helpers/auto-update-progress.ts'
import type { AutoUpdateProgressView, AutoUpdateStatusPayload } from '../helpers/auto-update-progress.ts'
import { usePollTimer } from '../composables/usePolling.ts'

const props = defineProps<{
  status: AutoUpdateStatusPayload | null
  /** 测试注入固定时钟，生产不传（每次渲染取当前时间）。 */
  now?: number
}>()

const KEEP_COMPLETED_MS = 30000

const running = computed(() => isAutoUpdateRunning(props.status))
const progressView = computed(() => buildUpdateProgressView(props.status, props.now ?? Date.now()))
const completedVisible = ref(false)
const dismissedError = ref<string | null>(null)
const hideCompleted = usePollTimer()

watch(running, (isRunning, wasRunning) => {
  if (isRunning) {
    hideCompleted.cancel()
    completedVisible.value = false
    return
  }
  if (!wasRunning) return
  const finished = props.status?.current_stage === 'finished'
    || props.status?.progress?.status === 'success'
    || props.status?.progress?.status === 'partial'
  if (!finished) return
  completedVisible.value = true
  hideCompleted.schedule(KEEP_COMPLETED_MS, () => {
    completedVisible.value = false
  })
})

const activeView = computed<AutoUpdateProgressView | null>(() => {
  const view = progressView.value
  if (view?.kind === 'running') return view
  if (view?.kind === 'failed' && view.error !== dismissedError.value) return view
  if (completedVisible.value && !view) return buildCompletedView()
  return null
})

const metaText = computed(() => {
  const view = activeView.value
  if (!view) return ''
  const parts: string[] = []
  if (view.kind === 'running' && view.elapsed) parts.push(`已耗时 ${view.elapsed}`)
  if (view.kind === 'running' && view.eta) parts.push(`预计剩余约 ${view.eta}`)
  if (view.kind === 'running' && view.current) parts.push(`当前 ${view.current}`)
  if (view.kind === 'completed') parts.push('本提示 30 秒后自动消失')
  return parts.join(' · ')
})

function dismissFailure() {
  dismissedError.value = activeView.value?.error ?? null
}
</script>

<template>
  <div
    v-if="activeView"
    class="auto-update-progress"
    :data-state="activeView.kind"
    :role="activeView.kind === 'failed' ? 'alert' : 'status'"
    :aria-live="activeView.kind === 'failed' ? 'assertive' : 'polite'"
  >
    <div class="auto-update-head">
      <span class="auto-update-stage">{{ activeView.title }}</span>
      <span v-if="activeView.counts" class="auto-update-counts">{{ activeView.counts }}</span>
      <span v-if="activeView.percent !== null" class="auto-update-percent">{{ activeView.percent }}%</span>
      <button
        v-if="activeView.kind === 'failed'"
        type="button"
        class="auto-update-dismiss"
        aria-label="关闭更新失败提示"
        @click="dismissFailure"
      >×</button>
    </div>
    <div
      class="auto-update-track"
      :class="{ 'is-indeterminate': activeView.kind === 'running' && activeView.percent === null }"
    >
      <div
        v-if="activeView.percent !== null"
        class="auto-update-fill"
        :style="{ width: `${activeView.percent}%` }"
      ></div>
    </div>
    <p v-if="metaText" class="auto-update-meta">{{ metaText }}</p>
    <p v-if="activeView.error" class="auto-update-error">{{ activeView.error }}</p>
  </div>
</template>

<style scoped>
.auto-update-progress {
  position: fixed;
  z-index: 900;
  left: 50%;
  bottom: 16px;
  transform: translateX(-50%);
  box-sizing: border-box;
  width: min(680px, calc(100vw - 24px));
  padding: 10px 14px 11px;
  border: 1px solid #dbe6df;
  border-top: 3px solid #70a986;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.97);
  box-shadow: 0 6px 24px rgba(16, 24, 40, 0.14);
  color: #355142;
  font-size: 12px;
  /* 悬浮但不挡点击：只有关闭按钮重新开启 pointer-events。 */
  pointer-events: none;
}
.auto-update-progress[data-state='failed'] {
  border-top-color: #d03050;
}
.auto-update-progress[data-state='completed'] {
  border-top-color: #18a058;
}
.auto-update-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
}
.auto-update-stage {
  font-weight: 650;
  color: #2f4d3c;
}
.auto-update-counts,
.auto-update-percent {
  color: #4b6b57;
  font-variant-numeric: tabular-nums;
}
.auto-update-counts {
  font-weight: 600;
}
.auto-update-percent {
  margin-left: auto;
}
.auto-update-dismiss {
  margin-left: 4px;
  padding: 0 4px;
  border: 0;
  background: transparent;
  color: #8b978f;
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  pointer-events: auto;
}
.auto-update-dismiss:hover {
  color: #d03050;
}
.auto-update-track {
  position: relative;
  height: 8px;
  margin-top: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: #edf2ee;
}
.auto-update-fill {
  height: 100%;
  border-radius: inherit;
  background: #70a986;
  transition: width 0.3s ease;
}
[data-state='failed'] .auto-update-fill {
  background: #d03050;
}
[data-state='completed'] .auto-update-fill {
  background: #18a058;
}
.auto-update-track.is-indeterminate::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 40%;
  height: 100%;
  border-radius: inherit;
  background: #a9ceb4;
  animation: auto-update-indeterminate 1.2s ease-in-out infinite;
}
@keyframes auto-update-indeterminate {
  from { transform: translateX(-100%); }
  to { transform: translateX(250%); }
}
.auto-update-meta {
  margin: 7px 0 0;
  color: #7d8a82;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.auto-update-error {
  margin: 7px 0 0;
  padding: 5px 8px;
  border-radius: 6px;
  background: #fdf0f2;
  color: #b3244a;
  font-size: 11px;
  word-break: break-word;
}
@media (max-width: 640px) {
  .auto-update-progress {
    bottom: 8px;
    width: calc(100vw - 16px);
    padding: 9px 11px 10px;
  }
  .auto-update-percent {
    margin-left: 0;
  }
}
</style>
