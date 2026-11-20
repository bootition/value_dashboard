/**
 * 自选列表「可选列清单 + 当前分组列顺序 + 可见集合」的**唯一事实源**。
 *
 * 背景（2026-11-25 用户实测）：自选表头里 PE / PB / ROE 这些列是硬编码顺序，
 * 用户「想看一个数据每次都得拉到最右边」。要求：**按住表头就能拖动**、
 * **放下就固定**、**不常用的可以删掉**，而且**固定在这个分组上**（换分组/刷新都还在）。
 *
 * 改造前 `views/WatchlistPage.vue` 里有三份同源清单各自维护同一批列：
 *   1. `colDefs`（真列定义：标题/宽度/sorter/render）
 *   2. `allColumnOptions`（列设置勾选框的中文名）
 *   3. `DEFAULT_COLUMNS`（默认列 key）
 * 加一列要改三处、列顺序一处写死 → 拆成单一清单 `WATCHLIST_COLUMN_CATALOG`，
 * 宽度与可排序性也搬进来（R1：一个事实只有一个实现）。
 *
 * 配置形状（存 localStorage，按分组隔离）：
 *   `order`   —— 当前分组**完整**列顺序（被隐藏的列也留在里面，重新勾选时回到原位）
 *   `visible` —— 勾选显示的列，始终是 `order` 的子序列
 * 隐藏集合 = order − visible，**派生**，不单独存第二份（同一事实不写两处）。
 */

import { computed, ref, watch } from 'vue'
import type { ComputedRef, Ref } from 'vue'
import { fieldDisplayName, fieldTitleWithUnit } from '../utils/screening-format.ts'

/** 可选列（一项 = 表里的一个可选列，含中文名/单位/宽度/是否可点表头排序）。 */
export interface WatchlistColumnSpec {
  /** 列 key：表列 key，同时是 localStorage 里持久化的稳定标识（不要本地化） */
  key: string
  /** 表头标题（指标列带单位，如 `PE(倍)`） */
  header: string
  /** 列设置面板里的完整中文名（不带单位，面板窄） */
  label: string
  /** 列宽（px），沿用改造前的既有宽度 */
  width: number
  /** 是否可点表头排序（`sorter: 'default'`）。拖动调顺序**不影响**它 */
  sortable: boolean
}

/** 指标列：标题带单位、可排序，中文名走 screening-format 单源。 */
function indicatorColumn(key: string, shortLabel: string, width: number): WatchlistColumnSpec {
  return {
    key,
    header: fieldTitleWithUnit(key, shortLabel),
    label: fieldDisplayName(key),
    width,
    sortable: true,
  }
}

/**
 * 可选列清单（顺序 = 改造前的列顺序，逐项等价，不悄悄改用户看到的表）。
 *
 * 加新列只需在这里加一行：老分组的 `order` 由 `mergeColumnOrder` 把新列补到末尾，
 * 而 `visible` 仍按用户存的那份走 → 新列对老配置**默认隐藏**，既不打乱用户已有顺序，
 * 也不会凭空多出一列。
 */
export const WATCHLIST_COLUMN_CATALOG: ReadonlyArray<WatchlistColumnSpec> = [
  { key: 'stock_code', header: '代码', label: '股票代码', width: 130, sortable: false },
  { key: 'name', header: '名称', label: '股票名称', width: 100, sortable: false },
  { key: 'exchange', header: '交易所', label: '交易所', width: 70, sortable: false },
  { key: 'group_name', header: '分组', label: '分组', width: 90, sortable: false },
  { key: 'listing_date', header: '上市日期', label: '上市日期', width: 96, sortable: true },
  indicatorColumn('total_market_cap', '总市值', 110),
  indicatorColumn('circ_market_cap', '流通市值', 110),
  indicatorColumn('latest_close', '收盘价', 80),
  indicatorColumn('pe_ttm', 'PE', 70),
  indicatorColumn('pb_mrq', 'PB', 70),
  indicatorColumn('roe', 'ROE', 80),
  indicatorColumn('gross_margin', '毛利率', 80),
  indicatorColumn('net_margin', '净利率', 80),
  indicatorColumn('debt_ratio', '负债率', 80),
  indicatorColumn('revenue_yoy', '营收YoY', 90),
  { key: 'source', header: '来源', label: '来源', width: 80, sortable: false },
]

/** 列 key → 列定义。未知 key（历史遗留 / 更老版本存下的列）一律视为不存在。 */
export const WATCHLIST_COLUMN_BY_KEY: ReadonlyMap<string, WatchlistColumnSpec> = new Map(
  WATCHLIST_COLUMN_CATALOG.map((spec) => [spec.key, spec]),
)

/** 完整默认顺序（含默认隐藏的列）。 */
export const DEFAULT_WATCHLIST_COLUMN_ORDER: ReadonlyArray<string> =
  WATCHLIST_COLUMN_CATALOG.map((spec) => spec.key)

/**
 * 默认显示的列 —— 与改造前 `DEFAULT_COLUMNS` 逐字相同，
 * 保证「没配置过」的用户打开自选页看到的表和以前一模一样。
 */
export const DEFAULT_WATCHLIST_VISIBLE_COLUMNS: ReadonlyArray<string> = [
  'stock_code', 'name', 'exchange', 'group_name', 'listing_date',
  'total_market_cap', 'latest_close', 'pe_ttm', 'pb_mrq', 'roe',
  'gross_margin', 'debt_ratio', 'source',
]

/** 存储 key 前缀。完整 key = 前缀 + `.` + 编码后的分组名（见 watchlistColumnsStorageKey）。 */
export const WATCHLIST_COLUMNS_STORAGE_PREFIX = 'vd.watchlist.columns'

/** 存储形状版本号（将来改形状时用它做迁移，别靠猜字段）。 */
export const WATCHLIST_COLUMN_PREFS_VERSION = 1

/**
 * 分组名 → localStorage key。
 *
 * 分组名是用户输入的中文/空格/emoji（真实案例「往股东口袋装钱的大老爷们」），
 * 直接拼进 key 会在部分浏览器/存储实现里出问题，所以 `encodeURIComponent`。
 * 注意 `default`（后端手动收录组的内部 key）也一起编码，key 里不出现裸中文。
 */
export function watchlistColumnsStorageKey(groupName: string): string {
  return `${WATCHLIST_COLUMNS_STORAGE_PREFIX}.${encodeURIComponent(groupName)}`
}

/** 一个分组存下来的列配置。 */
export interface WatchlistColumnPrefs {
  /** 完整列顺序（含隐藏列） */
  order: string[]
  /** 勾选显示的列（order 的子序列） */
  visible: string[]
}

/** 默认配置：默认顺序 + 默认可见集合。 */
export function defaultColumnPrefs(): WatchlistColumnPrefs {
  return {
    order: [...DEFAULT_WATCHLIST_COLUMN_ORDER],
    visible: [...DEFAULT_WATCHLIST_VISIBLE_COLUMNS],
  }
}

/** 只保留「可选列清单里真的存在」的 key，去重并保持原顺序。 */
export function sanitizeColumnKeys(raw: unknown): string[] {
  if (!Array.isArray(raw)) return []
  const seen = new Set<string>()
  const kept: string[] = []
  for (const item of raw) {
    if (typeof item !== 'string') continue
    if (!WATCHLIST_COLUMN_BY_KEY.has(item)) continue
    if (seen.has(item)) continue
    seen.add(item)
    kept.push(item)
  }
  return kept
}

/**
 * 存下来的顺序 → 与**当前**可选列清单对齐的完整顺序。
 *
 * 两个方向都要补：老配置缺了新加的列（补到末尾）、新配置残留了已删掉的列（丢弃）。
 * 不补的话新列永远开不出来；不丢的话表里会出现空列。
 */
export function mergeColumnOrder(order: readonly string[]): string[] {
  const merged = sanitizeColumnKeys(order)
  for (const key of DEFAULT_WATCHLIST_COLUMN_ORDER) {
    if (!merged.includes(key)) merged.push(key)
  }
  return merged
}

/** 把任意 key 集合按 `order` 排成子序列（可见集合永远跟顺序同源，避免两份顺序打架）。 */
export function canonicalColumnKeys(order: readonly string[], keys: readonly string[]): string[] {
  const picked = new Set(keys)
  return order.filter((key) => picked.has(key))
}

/**
 * 解析 localStorage 里的原始字符串。
 *
 * 返回 `null` = 这个分组**没有**可用配置（页面用默认列）；
 * 返回对象 = 用户配置过，哪怕是「一列都不勾」（合法状态，不能当成没配置）。
 */
export function parseColumnPrefs(raw: string | null | undefined): WatchlistColumnPrefs | null {
  if (!raw) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  // 兼容只存了顺序数组的老写法：顺序即顺序，全部可见。
  if (Array.isArray(parsed)) {
    const order = mergeColumnOrder(sanitizeColumnKeys(parsed))
    return { order, visible: [...order] }
  }
  if (!parsed || typeof parsed !== 'object') return null
  const record = parsed as Record<string, unknown>
  const order = mergeColumnOrder(sanitizeColumnKeys(record.order))
  const visible = Array.isArray(record.visible)
    ? canonicalColumnKeys(order, sanitizeColumnKeys(record.visible))
    // 形状里没有 visible（半残配置）：按默认可见集合兜底，总比把表清空好
    : canonicalColumnKeys(order, DEFAULT_WATCHLIST_VISIBLE_COLUMNS)
  return { order, visible }
}

/** 序列化（写入 localStorage 的那一份字节）。 */
export function serializeColumnPrefs(prefs: WatchlistColumnPrefs): string {
  return JSON.stringify({
    v: WATCHLIST_COLUMN_PREFS_VERSION,
    order: [...prefs.order],
    visible: [...prefs.visible],
  })
}

/**
 * 把 `fromKey` 挪到 `toKey` 的前/后（纯函数，两个 key 有任一不在 order 里就原样返回）。
 *
 * 拖动实现按「方向」决定插入点：往右拖落在目标后面、往左拖落在目标前面
 * （见 `resolveDropPosition`）—— 与常见拖拽列表一致，也不需要读鼠标像素。
 */
export function moveColumnKey(
  order: readonly string[],
  fromKey: string,
  toKey: string,
  position: 'before' | 'after' = 'before',
): string[] {
  const from = order.indexOf(fromKey)
  const to = order.indexOf(toKey)
  if (from < 0 || to < 0 || from === to) return [...order]
  const next = order.filter((key) => key !== fromKey)
  const anchor = next.indexOf(toKey)
  next.splice(position === 'after' ? anchor + 1 : anchor, 0, fromKey)
  return next
}

/** 拖动方向 → 插入位置：从右往左拖落在目标左边，从左往右拖落在目标右边。 */
export function resolveDropPosition(
  order: readonly string[],
  fromKey: string,
  toKey: string,
): 'before' | 'after' {
  const from = order.indexOf(fromKey)
  const to = order.indexOf(toKey)
  return from >= 0 && to >= 0 && from < to ? 'after' : 'before'
}

/** 列设置面板的 ▲▼：在完整顺序里挪一格（越界原样返回）。 */
export function shiftColumnKey(order: readonly string[], key: string, delta: number): string[] {
  const index = order.indexOf(key)
  const target = index + delta
  if (index < 0 || target < 0 || target >= order.length) return [...order]
  const next = [...order]
  next.splice(index, 1)
  next.splice(target, 0, key)
  return next
}

export interface UseWatchlistColumnsDeps {
  /** 当前分组名（`null` = 还没选组，先给默认列，不写存储） */
  groupName: Ref<string | null>
}

export interface UseWatchlistColumns {
  /** 完整列顺序（含隐藏列），拖动/▲▼ 改的就是它 */
  columnOrder: Ref<string[]>
  /** 可见列 key（order 的子序列），传给 n-data-table 排序用 */
  visibleColumnKeys: ComputedRef<string[]>
  /** 可见列的可选列定义，顺序即表格列顺序 */
  visibleColumnSpecs: ComputedRef<WatchlistColumnSpec[]>
  /** 全部列定义（列设置面板按当前顺序列出，含未勾选的） */
  orderedColumnSpecs: ComputedRef<WatchlistColumnSpec[]>
  /** 可见集合（Set 形态，模板里判断勾选用） */
  visibleColumnKeySet: ComputedRef<Set<string>>
  /** 当前分组的存储 key（`null` = 未选分组） */
  storageKey: ComputedRef<string | null>
  /** 拖动落地：把 fromKey 挪到 toKey 前后并立即持久化 */
  moveColumn: (fromKey: string, toKey: string, position?: 'before' | 'after') => void
  /** 设置整份可见集合（列设置勾选框用），顺序仍以 columnOrder 为准 */
  setVisibleColumns: (keys: readonly string[]) => void
  /** 勾/取消勾单列 */
  toggleColumnVisible: (key: string, visible: boolean) => void
  /** 全部显示（隐藏的列回到原位置） */
  showAllColumns: () => void
  /** 面板 ▲▼：按完整顺序挪一格 */
  shiftColumn: (key: string, delta: number) => void
  /** 恢复默认：顺序 + 可见集合回到出厂值，并清掉本分组存储 */
  resetColumns: () => void
}

/**
 * 按分组读写自选列配置。
 *
 * 分组切换时**重新载入**该分组的配置（用户要的「固定在这个分组上」）；
 * 没有配置就用默认列。所有写操作（拖动/勾选/▲▼）当场落 localStorage，
 * 不用 watch 兜底 —— 写不写是显式动作，读的时候也就不会有「切组时误写默认值」的坑。
 */
export function useWatchlistColumns(deps: UseWatchlistColumnsDeps): UseWatchlistColumns {
  const { groupName } = deps
  const columnOrder = ref<string[]>([...DEFAULT_WATCHLIST_COLUMN_ORDER])
  const visibleColumnKeysRaw = ref<string[]>([...DEFAULT_WATCHLIST_VISIBLE_COLUMNS])

  function readStorage(key: string): string | null {
    try {
      if (typeof localStorage === 'undefined') return null
      return localStorage.getItem(key)
    } catch {
      return null
    }
  }

  function writeStorage(key: string, value: string): void {
    try {
      if (typeof localStorage === 'undefined') return
      localStorage.setItem(key, value)
    } catch {
      // 隐私模式 / 配额满：静默降级为「本次会话内有效」，不能因此白屏
    }
  }

  function removeStorage(key: string): void {
    try {
      if (typeof localStorage === 'undefined') return
      localStorage.removeItem(key)
    } catch {
      // 同上
    }
  }

  /** 载入某个分组的配置（没有配置 = 默认列）。 */
  function loadGroupPrefs(name: string | null): void {
    const prefs = name ? parseColumnPrefs(readStorage(watchlistColumnsStorageKey(name))) : null
    const next = prefs ?? defaultColumnPrefs()
    columnOrder.value = next.order
    visibleColumnKeysRaw.value = canonicalColumnKeys(next.order, next.visible)
  }

  function persist(): void {
    const name = groupName.value
    if (!name) return // 还没选分组：不往任何 key 里写默认值
    writeStorage(
      watchlistColumnsStorageKey(name),
      serializeColumnPrefs({
        order: columnOrder.value,
        // 存**归一后的**可见集合：落盘的那一份必须自洽（visible 永远是 order 的子序列），
        // 否则「拖动改了 order」时 visible 还留在旧顺序，读回来虽然能修但存的是脏数据。
        visible: canonicalColumnKeys(columnOrder.value, visibleColumnKeysRaw.value),
      }),
    )
  }

  watch(groupName, (name) => loadGroupPrefs(name), { immediate: true })

  const visibleColumnKeys = computed(() =>
    canonicalColumnKeys(columnOrder.value, visibleColumnKeysRaw.value),
  )
  const visibleColumnKeySet = computed(() => new Set(visibleColumnKeys.value))
  const visibleColumnSpecs = computed(() =>
    visibleColumnKeys.value
      .map((key) => WATCHLIST_COLUMN_BY_KEY.get(key))
      .filter((spec): spec is WatchlistColumnSpec => spec !== undefined),
  )
  const orderedColumnSpecs = computed(() =>
    mergeColumnOrder(columnOrder.value)
      .map((key) => WATCHLIST_COLUMN_BY_KEY.get(key))
      .filter((spec): spec is WatchlistColumnSpec => spec !== undefined),
  )
  const storageKey = computed(() =>
    groupName.value ? watchlistColumnsStorageKey(groupName.value) : null,
  )

  function moveColumn(fromKey: string, toKey: string, position?: 'before' | 'after'): void {
    const resolved = position ?? resolveDropPosition(columnOrder.value, fromKey, toKey)
    const next = moveColumnKey(columnOrder.value, fromKey, toKey, resolved)
    if (next.join('|') === columnOrder.value.join('|')) return
    columnOrder.value = next
    persist()
  }

  function shiftColumn(key: string, delta: number): void {
    const next = shiftColumnKey(columnOrder.value, key, delta)
    if (next.join('|') === columnOrder.value.join('|')) return
    columnOrder.value = next
    persist()
  }

  function setVisibleColumns(keys: readonly string[]): void {
    visibleColumnKeysRaw.value = canonicalColumnKeys(columnOrder.value, keys)
    persist()
  }

  function toggleColumnVisible(key: string, visible: boolean): void {
    const current = new Set(visibleColumnKeys.value)
    if (visible) current.add(key)
    else current.delete(key)
    setVisibleColumns([...current])
  }

  function showAllColumns(): void {
    setVisibleColumns(columnOrder.value)
  }

  function resetColumns(): void {
    const defaults = defaultColumnPrefs()
    columnOrder.value = defaults.order
    visibleColumnKeysRaw.value = defaults.visible
    const name = groupName.value
    // 清掉存储 = 回到「这个分组没配置过」，而不是把当前顺序存成新的默认。
    if (name) removeStorage(watchlistColumnsStorageKey(name))
  }

  return {
    columnOrder,
    visibleColumnKeys,
    visibleColumnSpecs,
    orderedColumnSpecs,
    visibleColumnKeySet,
    storageKey,
    moveColumn,
    setVisibleColumns,
    toggleColumnVisible,
    showAllColumns,
    shiftColumn,
    resetColumns,
  }
}
