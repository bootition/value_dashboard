/**
 * 自选列表「列清单 / 列顺序 / 可见集合 / 按分组持久化」的单测（2026-11-25）。
 *
 * 用户需求原话：「我希望这个表头是可以拖动的……按住它之后就可以拖动，放下来之后
 * 就可以固定下来，固定在这个分组上」。所以这里钉死三件事：
 *   1. 存储 key 必须带（编码过的）分组名 —— 换分组就换配置
 *   2. 顺序 + 可见集合都要能往返（序列化 → 解析 → 同一个结果）
 *   3. 老配置遇到新列 / 已删列时不能把表搞坏（补齐 + 丢弃，而不是白屏）
 *
 * 走 `node --experimental-strip-types --test`（见 scripts/run-unit-tests.mjs），
 * 组件级拖动交互在 tests/component/watchlist-column-drag.test.ts 里覆盖。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { nextTick, ref } from 'vue'
import {
  DEFAULT_WATCHLIST_COLUMN_ORDER,
  DEFAULT_WATCHLIST_VISIBLE_COLUMNS,
  WATCHLIST_COLUMN_CATALOG,
  canonicalColumnKeys,
  defaultColumnPrefs,
  mergeColumnOrder,
  moveColumnKey,
  parseColumnPrefs,
  resolveDropPosition,
  sanitizeColumnKeys,
  serializeColumnPrefs,
  shiftColumnKey,
  useWatchlistColumns,
  watchlistColumnsStorageKey,
  WATCHLIST_COLUMNS_STORAGE_PREFIX,
} from '../src/composables/useWatchlistColumns.ts'

/** 最小 localStorage 桩：只实现 composable 真正用到的方法。 */
function createMemoryStorage(): Storage {
  const map = new Map<string, string>()
  return {
    get length() {
      return map.size
    },
    clear: () => map.clear(),
    getItem: (key: string) => (map.has(key) ? map.get(key)! : null),
    key: (index: number) => [...map.keys()][index] ?? null,
    removeItem: (key: string) => void map.delete(key),
    setItem: (key: string, value: string) => void map.set(key, String(value)),
  }
}

function installFakeLocalStorage(): Storage {
  const storage = createMemoryStorage()
  ;(globalThis as unknown as Record<string, unknown>).localStorage = storage
  return storage
}

function uninstallFakeLocalStorage(): void {
  delete (globalThis as unknown as Record<string, unknown>).localStorage
}

test('存储 key：按分组隔离，且中文/特殊字符被编码', () => {
  const groupKey = watchlistColumnsStorageKey('往股东口袋装钱的大老爷们')
  assert.ok(groupKey.startsWith(`${WATCHLIST_COLUMNS_STORAGE_PREFIX}.`), groupKey)
  // 分组名整体编码：key 里不出现裸中文，也不会被分组名里的 `.` `/` 撑开层级
  assert.doesNotMatch(groupKey, /[\u4e00-\u9fff]/, `key 里不该有裸中文：${groupKey}`)
  assert.equal(groupKey, `${WATCHLIST_COLUMNS_STORAGE_PREFIX}.${encodeURIComponent('往股东口袋装钱的大老爷们')}`)
  assert.equal(
    watchlistColumnsStorageKey('a b/c.d'),
    `${WATCHLIST_COLUMNS_STORAGE_PREFIX}.a%20b%2Fc.d`,
  )
  // 不同分组 = 不同 key；也不能撞上改造前那条全局 key
  assert.notEqual(watchlistColumnsStorageKey('价值组'), watchlistColumnsStorageKey('成长组'))
  assert.notEqual(watchlistColumnsStorageKey('default'), WATCHLIST_COLUMNS_STORAGE_PREFIX)
})

test('默认配置：顺序覆盖全部可选列，可见集合 = 改造前的默认列', () => {
  const prefs = defaultColumnPrefs()
  assert.deepEqual(prefs.order, [...DEFAULT_WATCHLIST_COLUMN_ORDER])
  assert.deepEqual(prefs.visible, [...DEFAULT_WATCHLIST_VISIBLE_COLUMNS])
  // 默认显示列必须逐字等于改造前的 13 列（不能悄悄改动用户看到的表）
  assert.deepEqual(prefs.visible, [
    'stock_code', 'name', 'exchange', 'group_name', 'listing_date',
    'total_market_cap', 'latest_close', 'pe_ttm', 'pb_mrq', 'roe',
    'gross_margin', 'debt_ratio', 'source',
  ])
  // 可见列必须是完整顺序的子序列（同一顺序只有一个来源）
  assert.deepEqual(prefs.visible, canonicalColumnKeys(prefs.order, prefs.visible))
  // 标签与宽度都在清单里（页面不再自己写一份表头）
  const pe = WATCHLIST_COLUMN_CATALOG.find((spec) => spec.key === 'pe_ttm')
  assert.ok(pe)
  assert.equal(pe.sortable, true)
  assert.equal(pe.width, 70)
  assert.match(pe.header, /PE/)
  assert.equal(pe.label, '市盈率（PE-TTM）')
})

test('序列化往返：顺序 + 可见集合一个不丢', () => {
  const order = moveColumnKey([...DEFAULT_WATCHLIST_COLUMN_ORDER], 'pe_ttm', 'name', 'before')
  const prefs = { order, visible: ['pe_ttm', 'stock_code', 'name'] }
  const parsed = parseColumnPrefs(serializeColumnPrefs(prefs))
  assert.ok(parsed)
  assert.deepEqual(parsed.order, order)
  // 可见集合按 order 归一：存进来是倒序也必须按列顺序输出
  assert.deepEqual(parsed.visible, ['stock_code', 'pe_ttm', 'name'])
  assert.equal(parsed.order[0], 'stock_code')
  assert.equal(parsed.order[1], 'pe_ttm')
})

test('解析容错：没配置/坏 JSON → null（用默认列），不抛错', () => {
  assert.equal(parseColumnPrefs(null), null)
  assert.equal(parseColumnPrefs(undefined), null)
  assert.equal(parseColumnPrefs(''), null)
  assert.equal(parseColumnPrefs('{不是 json'), null)
  assert.equal(parseColumnPrefs('"字符串"'), null)
  assert.equal(parseColumnPrefs('null'), null)
})

test('解析容错：老配置缺列/多列时补齐与丢弃，绝不把表搞坏', () => {
  // 缺列：老配置只有 3 列 → 其余列补到末尾（新列对老配置默认不显示）
  const partial = parseColumnPrefs(JSON.stringify({ order: ['name', 'stock_code', 'pe_ttm'], visible: ['name', 'stock_code'] }))
  assert.ok(partial)
  assert.deepEqual(partial.order.slice(0, 3), ['name', 'stock_code', 'pe_ttm'])
  assert.equal(partial.order.length, DEFAULT_WATCHLIST_COLUMN_ORDER.length)
  assert.deepEqual(partial.visible, ['name', 'stock_code'])
  // 多列：已删掉的列 key 必须被丢弃，且重复项去重
  const extra = parseColumnPrefs(JSON.stringify({
    order: ['ghost_column', 'name', 'name', 'pe_ttm'],
    visible: ['ghost_column', 'pe_ttm'],
  }))
  assert.ok(extra)
  assert.ok(!extra.order.includes('ghost_column'))
  assert.deepEqual(extra.order.slice(0, 2), ['name', 'pe_ttm'])
  assert.equal(extra.order.length, DEFAULT_WATCHLIST_COLUMN_ORDER.length)
  assert.deepEqual(extra.visible, ['pe_ttm'])
  // 「一列都不勾」是合法状态，不能被当成「没有配置」
  const allHidden = parseColumnPrefs(JSON.stringify({ order: [...DEFAULT_WATCHLIST_COLUMN_ORDER], visible: [] }))
  assert.ok(allHidden)
  assert.deepEqual(allHidden.visible, [])
  // 只存了顺序数组的老写法：顺序即顺序，全部可见
  const legacy = parseColumnPrefs(JSON.stringify(['name', 'pe_ttm', 'stock_code']))
  assert.ok(legacy)
  assert.deepEqual(legacy.order.slice(0, 3), ['name', 'pe_ttm', 'stock_code'])
  assert.deepEqual(legacy.visible, legacy.order)
})

test('sanitize / merge：只认清单里的列', () => {
  assert.deepEqual(sanitizeColumnKeys(['pe_ttm', 'nope', 42, 'pe_ttm', 'roe']), ['pe_ttm', 'roe'])
  assert.deepEqual(sanitizeColumnKeys('pe_ttm'), [])
  assert.deepEqual(mergeColumnOrder([]), [...DEFAULT_WATCHLIST_COLUMN_ORDER])
  assert.equal(mergeColumnOrder(['roe']).length, DEFAULT_WATCHLIST_COLUMN_ORDER.length)
})

test('拖动重排：落点在目标前/后，非法 key 原样返回', () => {
  const base = ['a', 'b', 'c', 'd']
  assert.deepEqual(moveColumnKey(base, 'a', 'c', 'before'), ['b', 'a', 'c', 'd'])
  assert.deepEqual(moveColumnKey(base, 'a', 'c', 'after'), ['b', 'c', 'a', 'd'])
  assert.deepEqual(moveColumnKey(base, 'd', 'b', 'before'), ['a', 'd', 'b', 'c'])
  assert.deepEqual(moveColumnKey(base, 'd', 'b', 'after'), ['a', 'b', 'd', 'c'])
  assert.deepEqual(moveColumnKey(base, 'a', 'a', 'after'), base)
  assert.deepEqual(moveColumnKey(base, 'x', 'c', 'after'), base)
  assert.deepEqual(moveColumnKey(base, 'a', 'x', 'after'), base)
  // 原数组不被就地改动（配置是响应式状态，副作用必须可控）
  assert.deepEqual(base, ['a', 'b', 'c', 'd'])
})

test('拖动落点方向：往右拖落在目标后面，往左拖落在目标前面', () => {
  const base = ['a', 'b', 'c', 'd']
  assert.equal(resolveDropPosition(base, 'a', 'c'), 'after')
  assert.equal(resolveDropPosition(base, 'd', 'b'), 'before')
  assert.equal(resolveDropPosition(base, 'x', 'b'), 'before')
})

test('列设置 ▲▼：挪一格，越界原样返回', () => {
  const base = ['a', 'b', 'c']
  assert.deepEqual(shiftColumnKey(base, 'a', 1), ['b', 'a', 'c'])
  assert.deepEqual(shiftColumnKey(base, 'c', -1), ['a', 'c', 'b'])
  assert.deepEqual(shiftColumnKey(base, 'a', -1), base)
  assert.deepEqual(shiftColumnKey(base, 'c', 1), base)
  assert.deepEqual(shiftColumnKey(base, 'x', 1), base)
})

test('composable：拖动写入本分组 key，换分组 → 默认，切回来 → 顺序恢复', async () => {
  const storage = installFakeLocalStorage()
  try {
    const groupName = ref<string | null>('价值组')
    const first = useWatchlistColumns({ groupName })
    // 初始 = 默认列（没配置过）
    assert.deepEqual(first.visibleColumnKeys.value, [...DEFAULT_WATCHLIST_VISIBLE_COLUMNS])
    assert.equal(first.storageKey.value, watchlistColumnsStorageKey('价值组'))

    // 把 PE 拖到最前面 → 立即落盘
    first.moveColumn('pe_ttm', 'stock_code', 'before')
    assert.equal(first.visibleColumnKeys.value[0], 'pe_ttm')
    const storedKey = watchlistColumnsStorageKey('价值组')
    const stored = parseColumnPrefs(storage.getItem(storedKey))
    assert.ok(stored, '拖动后必须写入 localStorage')
    assert.equal(stored.order[0], 'pe_ttm')
    assert.equal(stored.visible[0], 'pe_ttm')

    // 隐藏一列：order 保留它（恢复勾选时回原位），visible 里没有
    first.toggleColumnVisible('pe_ttm', false)
    assert.ok(!first.visibleColumnKeys.value.includes('pe_ttm'))
    const afterHide = parseColumnPrefs(storage.getItem(storedKey))
    assert.ok(afterHide)
    assert.ok(afterHide.order.includes('pe_ttm'))
    assert.ok(!afterHide.visible.includes('pe_ttm'))
    // 重新勾回 → 回到原来的位置（第 1 列）
    first.toggleColumnVisible('pe_ttm', true)
    assert.equal(first.visibleColumnKeys.value[0], 'pe_ttm')

    // 切到另一个分组：没有配置 → 默认列；而且不能把默认值写进别的分组 key
    groupName.value = '成长组'
    await nextTick()
    assert.deepEqual(first.visibleColumnKeys.value, [...DEFAULT_WATCHLIST_VISIBLE_COLUMNS])
    assert.equal(storage.getItem(watchlistColumnsStorageKey('成长组')), null)

    // 切回价值组：PE 仍在最前（用户要的「固定在这个分组上」）
    groupName.value = '价值组'
    await nextTick()
    assert.equal(first.visibleColumnKeys.value[0], 'pe_ttm')

    // 刷新页面 = 新实例，仍从 localStorage 读回同一份顺序
    const reloaded = useWatchlistColumns({ groupName: ref<string | null>('价值组') })
    assert.equal(reloaded.visibleColumnKeys.value[0], 'pe_ttm')

    // 恢复默认：回到默认顺序 + 默认可见集合，并清掉本分组存储（= 回到「没配置过」）
    reloaded.resetColumns()
    assert.deepEqual(reloaded.visibleColumnKeys.value, [...DEFAULT_WATCHLIST_VISIBLE_COLUMNS])
    assert.equal(storage.getItem(storedKey), null)
    // 别的分组不受影响
    assert.deepEqual(reloaded.visibleColumnSpecs.value.map((spec) => spec.key), [...DEFAULT_WATCHLIST_VISIBLE_COLUMNS])
  } finally {
    uninstallFakeLocalStorage()
  }
})

test('composable：未选分组（groupName=null）时不读写存储', () => {
  const storage = installFakeLocalStorage()
  try {
    const groupName = ref<string | null>(null)
    const columns = useWatchlistColumns({ groupName })
    assert.equal(columns.storageKey.value, null)
    columns.moveColumn('pe_ttm', 'stock_code', 'before')
    assert.equal(columns.visibleColumnKeys.value[0], 'pe_ttm')
    assert.equal(storage.length, 0, '没选分组就不能凭空造一个存储 key')
    // 全部显示 / 全部隐藏都只是内存状态
    columns.showAllColumns()
    assert.equal(columns.visibleColumnKeys.value.length, DEFAULT_WATCHLIST_COLUMN_ORDER.length)
  } finally {
    uninstallFakeLocalStorage()
  }
})
