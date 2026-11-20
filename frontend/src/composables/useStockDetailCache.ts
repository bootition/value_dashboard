/**
 * 个股详情页的「上一次成功响应」冷藏（stale-while-revalidate，2026-11-20）。
 *
 * 用户原话：「我从筛选页点到数据详情页，**数据详情页刷新了才显示**，虽然很快，
 * 但它也是**转了个小圈**之后才显示。」
 *
 * 根因（不是"没做缓存"，而是**渲染时序被加载态绑死**）：
 * `composables/useStockDetailLoad.ts::fetchAll()` 每次挂载都 `loading.value = true`，
 * 而 `StockDetailPage.vue` 的模板把**整页**包在 `<n-spin v-else :show="loading">` 里：
 *
 *     <n-spin v-else :show="loading">
 *       ...所有卡片...
 *     </n-spin>
 *
 * 于是每进一次详情页（不管刚才是不是刚看过同一只股票），5 路请求全部落地前，
 * 页面主体一律被遮住 —— 用户看到的就是"转个小圈才显示"。数据本身很快，
 * 但"先清空再转圈"这个顺序是不可见的浪费。
 *
 * 修法（**只改渲染时序，不动数据契约**）：
 * 1. 模块级 `Map` 记住每只股票上一次成功的响应体 + `asOf`（数据截至）；
 * 2. 挂载时同步回填 → 首帧就有内容（`loading` 保持 false，不转圈）；
 * 3. 后台照旧打那 5 路请求（revalidate），落地后覆盖，内容原地更新；
 * 4. 只有"这只股票从来没成功过"才走原来的空态 + 转圈；
 * 5. `as_of` 始终跟着缓存走，页面顶部据此标注「数据截至 …（正在后台刷新）」，
 *    不会把旧快照冒充成刚取的新数据。
 *
 * 为什么不落 `localStorage`：详情页数据是"当前库里的快照"，跨文档保留会让人看到
 * 隔天的旧值却没有任何视觉提示；同一份文档内的切页/来回点才是用户实际痛点。
 * 同理也不做跨 tab 共享（`sessionStorage` 也不需要）。
 */

import type { Ref } from 'vue'

/** 一次成功响应里跨挂载要留的东西（结构 = 各端点响应体，原样存储，不做二次加工）。 */
export interface StockDetailCachedPayload<T> {
  readonly data: T
  /** 数据截至（后端 `data_as_of` / 最新行情日 / 报告期），用于"数据截至"标注。 */
  readonly asOf: string | null
  /** 写入时刻（`Date.now()`）；仅用于将来的 TTL/诊断，本模块不据此丢弃。 */
  readonly cachedAt: number
}

/** 内存冷藏：key = `${stockCode}|${kind}`，`kind` 里带上影响响应的参数（周期/复权）。 */
const store = new Map<string, StockDetailCachedPayload<unknown>>()

function keyOf(stockCode: string, kind: string): string {
  return `${stockCode}|${kind}`
}

/** 读缓存（同步、无副作用）。 */
export function readStockDetailCache<T>(stockCode: string, kind: string): StockDetailCachedPayload<T> | null {
  const hit = store.get(keyOf(stockCode, kind))
  return hit ? (hit as StockDetailCachedPayload<T>) : null
}

/** 写缓存（成功响应落地时调用；失败不覆盖，保证"上一次成功"永远不会被错误清掉）。 */
export function writeStockDetailCache<T>(
  stockCode: string,
  kind: string,
  data: T,
  asOf: string | null,
): void {
  store.set(keyOf(stockCode, kind), { data, asOf, cachedAt: Date.now() })
}

/**
 * 把缓存值回填到一个 `ref`，返回命中的缓存（未命中返回 `null`）。
 *
 * 用 `Ref<T>` 而不是 `Ref<T | null>`：详情页的 ref 初值各不相同
 * （`klineData` 初值是 `{ candles: [] }`），缓存只能"有值才覆盖"。
 */
export function hydrateRefFromCache<T>(
  target: Ref<T>,
  stockCode: string,
  kind: string,
): StockDetailCachedPayload<T> | null {
  const hit = readStockDetailCache<T>(stockCode, kind)
  if (hit) target.value = hit.data
  return hit
}

/** 仅测试用：模块级缓存必须能整体重置，否则用例之间会互相污染。 */
export function resetStockDetailCache(): void {
  store.clear()
}

/** 仅测试/诊断用：当前缓存了多少个键。 */
export function stockDetailCacheSize(): number {
  return store.size
}
