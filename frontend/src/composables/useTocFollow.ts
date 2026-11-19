/**
 * 个股详情页的「粘性目录 + 滚动跟随」（2026-11-23 重构）。
 *
 * 从 `views/StockDetailPage.vue` **原样抽出**，行为逐字等价：函数体只做了缩进，
 * 语句、常量（探针 96px / 目录 7 项 / 桌面断点 1025px / 目录吸附偏移 24px）一字未改。
 * 抽出的理由：这段与「5 路并发拉取」通过 `finally { nextTick(updateActiveSection) }`
 * 交织在同一文件里，两件事都在 component 作用域，导致谁都不敢动。
 *
 * 被钉住的行为（`tests/component/stock-detail-load.test.ts` 里 TOC 那一组）：
 * 1. 滚动容器优先用 `.n-layout-scroll-container`（Naive UI 的真实滚动容器），
 *    取不到才退回 window/documentElement —— 否则 `window.scrollY` 恒为 0，目录永远不动。
 * 2. 当前章节判定：`scrollTop <= 0` 无条件回首项；否则取「视口 top ≤ 96px」的最后一项。
 * 3. 目录吸顶不靠 CSS sticky（嵌套容器会失效），靠 scroll 监听手算 translateY，
 *    且只在 ≥1025px 且量到过自然位置（tocNaturalTop > 0）时生效。
 * 4. 点击目录项立即置 active（不等滚动事件），并尊重 prefers-reduced-motion。
 */

import { onMounted, onUnmounted, ref } from 'vue'
import type { Ref } from 'vue'
import type { TocItem } from '../components/StockTocNav.vue'

/** 目录锚点顺序（模板里的章节顺序必须与之一致）。 */
export const STOCK_DETAIL_TOC_ITEMS: readonly TocItem[] = [
  { id: 'overview', label: '概览' },
  { id: 'valuation', label: '估值与市场' },
  { id: 'point-in-time', label: '当时可见' },
  { id: 'announcements', label: '公告时间线' },
  { id: 'operations', label: '经营与成长' },
  { id: 'safety', label: '财务安全' },
  { id: 'return', label: '股东回报' },
]

export interface TocFollow {
  readonly tocItems: readonly TocItem[]
  readonly activeSection: Ref<string>
  readonly tocShift: Ref<number>
  setSectionRef(id: string): (el: unknown) => void
  scrollToSection(id: string): void
  updateActiveSection(): void
}

export function useTocFollow(): TocFollow {
  const tocItems = STOCK_DETAIL_TOC_ITEMS
  const activeSection = ref('overview')
  const sectionEls = ref<Record<string, HTMLElement | null>>({})
  // Naive UI 的实际滚动容器是 .n-layout-scroll-container，不是 window；
  // 粘性目录的高亮与滚动监听必须挂到该容器，否则 window.scrollY 恒为 0。
  const scrollContainer = ref<HTMLElement | null>(null)
  // 嵌套 n-layout-scroll-container 会让 CSS sticky 失效，因此用 scroll 监听
  // 手动 translateY，把目录固定在视口顶部附近（仅桌面宽度生效）。
  const tocNaturalTop = ref(0)
  const tocShift = ref(0)

  function setSectionRef(id: string) {
    return (el: unknown) => {
      const node =
        el instanceof HTMLElement
          ? el
          : ((el as { $el?: HTMLElement } | null)?.$el ?? null)
      sectionEls.value[id] = node
    }
  }

  function scrollToSection(id: string) {
    const el = sectionEls.value[id]
    if (el) {
      const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
      el.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' })
    }
    activeSection.value = id
  }

  function updateActiveSection() {
    const container = scrollContainer.value
    const scrollTop = container
      ? container.scrollTop
      : (window.scrollY || document.documentElement.scrollTop || 0)
    if (window.matchMedia('(min-width: 1025px)').matches && tocNaturalTop.value > 0) {
      tocShift.value = Math.max(0, scrollTop - (tocNaturalTop.value - 24))
    } else {
      tocShift.value = 0
    }
    if (scrollTop <= 0) {
      activeSection.value = tocItems[0].id
      return
    }
    const probe = scrollTop + 96
    let current = tocItems[0].id
    for (const item of tocItems) {
      const el = sectionEls.value[item.id]
      if (el && el.getBoundingClientRect().top + scrollTop <= probe) {
        current = item.id
      }
    }
    activeSection.value = current
  }

  onMounted(() => {
    scrollContainer.value = document.querySelector<HTMLElement>('.n-layout-scroll-container')
    const tocEl = document.querySelector<HTMLElement>('.stock-toc-wrap')
    if (tocEl) tocNaturalTop.value = tocEl.getBoundingClientRect().top + (scrollContainer.value?.scrollTop ?? 0)
    scrollContainer.value?.addEventListener('scroll', updateActiveSection, { passive: true })
    window.addEventListener('scroll', updateActiveSection, { passive: true })
    updateActiveSection()
  })

  onUnmounted(() => {
    scrollContainer.value?.removeEventListener('scroll', updateActiveSection)
    window.removeEventListener('scroll', updateActiveSection)
  })

  return { tocItems, activeSection, tocShift, setSectionRef, scrollToSection, updateActiveSection }
}
