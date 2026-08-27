import { vi } from 'vitest'

// naive-ui 组件在 jsdom 下依赖浏览器 API，统一补齐最小桩。
// ResizeObserver 需要立即回报尺寸，否则虚拟列表（v-vl）测不到容器高度，
// 下拉选项一个都不渲染。

const fakeContentRect = () => ({
  width: 300,
  height: 400,
  top: 0,
  left: 0,
  bottom: 400,
  right: 300,
  x: 0,
  y: 0,
  toJSON: () => ({}),
})

class ResizeObserverStub {
  private callback: ResizeObserverCallback

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
  }

  observe(target: Element) {
    this.callback(
      [{ target, contentRect: fakeContentRect() }] as unknown as ResizeObserverEntry[],
      this as unknown as ResizeObserver,
    )
  }

  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', ResizeObserverStub)

if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return false
    },
  })) as unknown as (query: string) => MediaQueryList
}

if (typeof window !== 'undefined' && !window.requestAnimationFrame) {
  window.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    cb(0)
    return 0
  }) as typeof requestAnimationFrame
}

if (typeof window !== 'undefined' && !window.cancelAnimationFrame) {
  window.cancelAnimationFrame = () => {}
}

// naive-ui 虚拟列表依赖 scrollTo；jsdom 未实现
if (typeof Element !== 'undefined' && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = (() => {}) as typeof Element.prototype.scrollTo
}
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = (() => {}) as typeof Element.prototype.scrollIntoView
}
