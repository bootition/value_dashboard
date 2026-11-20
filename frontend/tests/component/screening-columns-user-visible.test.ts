/**
 * 「用户在界面上到底看得见哪些列」——v3 列迁移的**端到端**守卫（2026-11-24）。
 *
 * 为什么必须有这个文件：独立审计实测证伪了 v2 迁移机制 —— 常量、bundle、单测全在，
 * 但真实用户的路径上**一个指标列都到不了屏幕**：
 *
 *   1. 真实草稿 `screening_drafts` revision=111：
 *      `result_columns=['stock_code','name','csrc_l2','latest_close']`、
 *      `default_columns_version=2` → 旧判据 `version !== 2 && isLegacy(...)` 两条都不命中
 *      → 不迁移 → 界面 = 4 列 + 条件自动列，**没有 PE/PB/ROE**；
 *   2. 已保存规则 49 的 `rule_json.columns` 是旧 8 列 → 选规则时 `applyLoadedRule`
 *      用它覆盖当前列 → 同样没有指标；
 *   3. 规则 48/50 里含 `dividend_financing_ratio_pct` / `roce_10y_median` —— 那是
 *      **用户真选过的指标列**，绝不能被"修 bug"顺手冲掉。
 *
 * 所以本文件断言的不是"某个函数返回 true"，而是**渲染列 / 提交列**（`effectiveResultColumns`
 * 与 `selectedColumnsForRequest()`）里到底有没有 `pe_ttm`/`pb_mrq`/`roe`。
 * 三个入口共用同一份判据实现（`resultColumnMigrationReason`），这里逐一走真实 composable。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { computed, effectScope, nextTick, reactive, ref } from 'vue'
import type { EffectScope } from 'vue'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
  }
  return { default: mock, ...mock }
})

import axios from 'axios'
import { useScreeningColumns, DEFAULT_RESULT_COLUMNS } from '../../src/composables/useScreeningColumns.ts'
import { useScreeningDraft } from '../../src/composables/useScreeningDraft.ts'
import { useScreeningRules } from '../../src/composables/useScreeningRules.ts'
import type { SavedRule } from '../../src/composables/useScreeningRules.ts'
import type { ScreeningRuleNode } from '../../src/types/screening.ts'

const DRAFT_URL = '/api/screening/draft'
const INDICATOR_COLUMNS = ['pe_ttm', 'pb_mrq', 'roe']

/** 库里真实存在的旧默认列（审计实证：规则 49 / 旧草稿都是这一套）。 */
const LEGACY_8 = [
  'stock_code', 'name', 'exchange', 'listing_date',
  'csrc_l1', 'csrc_l2', 'latest_close', 'total_market_cap',
]

/**
 * `screening_drafts` 表里 revision=111 的**逐字**内容（只读核对过正式库）：
 * `result_columns` 4 列、`default_columns_version=2`、条件字段 = total_market_cap + roce_10y_median。
 * 迁移前实际展示 4 + 2 = 6 列，一个估值/盈利指标都没有。
 */
const REAL_DRAFT_111 = {
  conditions: {
    id: 'root',
    logic: 'AND',
    rules: [
      { id: 'c1', field: 'total_market_cap', op: '>', value: 5e10 },
      { id: 'c2', field: 'roce_10y_median', op: '>', value: 0.15 },
    ],
  },
  base_pool: { exclude_st: true, exclude_suspended: true, min_listing_years: 1 },
  strict_only: false,
  result_columns: ['stock_code', 'name', 'csrc_l2', 'latest_close'],
  removed_result_columns: [],
  default_columns_version: 2,
}

function makeRuleTree(): ScreeningRuleNode {
  return reactive<ScreeningRuleNode>({
    id: 'root',
    logic: 'AND',
    rules: [{ id: 'c1', field: 'roce_10y_median', op: '>', value: 0.15 }],
  } as unknown as ScreeningRuleNode)
}

/**
 * 按页面的接线方式装一套（`ScreeningPage.vue`）：同一个 `selectedResultColumns`
 * 既交给草稿 composable，也交给列 composable —— 迁移结果因此必然作用到渲染/提交。
 */
function mountPage() {
  const ruleTree = makeRuleTree()
  const selectedResultColumns = ref<string[]>([])
  const removedResultColumns = ref<string[]>([])
  const indicators = ref([])
  const rankModes = ref([])
  const notify = { warning: vi.fn(), success: vi.fn(), error: vi.fn() }
  const dialog = { warning: vi.fn() }
  const scope: EffectScope = effectScope()

  const out = scope.run(() => {
    const columns = useScreeningColumns({
      indicators,
      rankModes,
      indicatorOptions: computed(() => []),
      selection: selectedResultColumns,
      ruleTree,
    })
    const draft = useScreeningDraft({
      ruleTree,
      basePool: reactive({ exclude_st: true, exclude_suspended: true, min_listing_years: 1 }),
      strictOnly: ref(false),
      selectedResultColumns,
      removedResultColumns,
      defaultResultColumns: DEFAULT_RESULT_COLUMNS,
      encodeResultColumn: (column) => (typeof column === 'string' ? column : String(column.field)),
      notify,
    })
    const rules = useScreeningRules({
      ruleTree,
      selectedResultColumns,
      defaultResultColumns: DEFAULT_RESULT_COLUMNS,
      selectedColumnsForRequest: columns.selectedColumnsForRequest,
      syncEditorSnapshot: draft.syncEditorSnapshot,
      hasUnsavedEdits: draft.hasUnsavedEdits,
      notify,
      dialog,
    })
    return { columns, draft, rules }
  })!

  return { ...out, selectedResultColumns, removedResultColumns, ruleTree, notify, scope }
}

function savedRule(id: number, columns: string[]): SavedRule {
  return {
    id,
    name: `规则${id}`,
    version: 1,
    rule_json: {
      conditions: {
        id: 'root',
        logic: 'AND',
        rules: [{ id: 'r1', field: 'roce_10y_median', op: '>', value: 0.15 }],
      } as unknown as ScreeningRuleNode,
      columns,
    },
    locked_indicators: {},
    status: 'saved',
    created_at: '2026-11-24T00:00:00Z',
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('A. 草稿 revision=111（4 列纯元数据、version 已是 2）→ 用户必须看到 PE/PB/ROE', () => {
  it('restoreDraft 后：渲染列与提交列都含 pe_ttm/pb_mrq/roe', async () => {
    const page = mountPage()
    ;(axios.get as unknown as { mockResolvedValue: (v: unknown) => void }).mockResolvedValue({
      data: { draft: REAL_DRAFT_111, revision: 111 },
    })

    await page.draft.restoreDraft()
    await nextTick()

    // 迁移前（审计实证 + 正式库只读核对）：4 列 + 条件自动列 2 个
    // （total_market_cap / roce_10y_median）= 6 列，0 个估值/盈利指标。
    // 迁移后：15 列默认列；两个条件字段本来就在默认列里 → 去重后仍是 15 列。
    expect(page.selectedResultColumns.value).toEqual([...DEFAULT_RESULT_COLUMNS])
    expect(page.columns.effectiveResultColumns.value).toEqual([...DEFAULT_RESULT_COLUMNS])
    expect(page.columns.effectiveResultColumns.value).toHaveLength(15)
    for (const column of INDICATOR_COLUMNS) {
      expect(page.columns.effectiveResultColumns.value).toContain(column)
      expect(page.columns.selectedColumnsForRequest()).toContain(column)
    }
    // 条件自动列照旧并入（「筛 ROCE 就要看得见 ROCE」）
    expect(page.columns.effectiveResultColumns.value).toContain('roce_10y_median')
    expect(page.draft.draftPayload().result_columns).toEqual([...DEFAULT_RESULT_COLUMNS])
    page.scope.stop()
  })

  it('真自定义（选过 pe_ttm）→ 一个字都不动', async () => {
    const page = mountPage()
    ;(axios.get as unknown as { mockResolvedValue: (v: unknown) => void }).mockResolvedValue({
      data: {
        draft: {
          ...REAL_DRAFT_111,
          result_columns: ['stock_code', 'pe_ttm'],
          default_columns_version: 2,
        },
        revision: 7,
      },
    })

    await page.draft.restoreDraft()
    await nextTick()

    // 用户自己的 2 列一个字都没动；后面两列是**条件自动列**（既有行为，与本迁移无关）。
    expect(page.selectedResultColumns.value).toEqual(['stock_code', 'pe_ttm'])
    expect(page.columns.selectedColumnsForRequest()).toEqual([
      'stock_code', 'pe_ttm', 'total_market_cap', 'roce_10y_median',
    ])
    page.scope.stop()
  })
})

describe('B. 载入已保存规则：旧默认列要迁，真自定义列照旧', () => {
  it('规则 49（旧 8 列默认）→ 提交列含指标', () => {
    const page = mountPage()
    page.rules.applyLoadedRule(savedRule(49, [...LEGACY_8]))

    expect(page.selectedResultColumns.value).toEqual([...DEFAULT_RESULT_COLUMNS])
    for (const column of INDICATOR_COLUMNS) {
      expect(page.columns.selectedColumnsForRequest()).toContain(column)
    }
    // 只改内存，不回写数据库（applyLoadedRule 里没有任何写库动作）
    expect(axios.post).not.toHaveBeenCalled()
    expect(axios.put).not.toHaveBeenCalled()
    page.scope.stop()
  })

  it('规则 50（自带 roce_10y_median）→ 用户选择照旧，不被迁移', () => {
    const page = mountPage()
    const custom = ['stock_code', 'name', 'csrc_l2', 'latest_close', 'total_market_cap', 'roce_10y_median']
    page.rules.applyLoadedRule(savedRule(50, custom))

    expect(page.selectedResultColumns.value).toEqual(custom)
    page.scope.stop()
  })

  it('规则 48（自带 dividend_financing_ratio_pct）→ 用户选择照旧', () => {
    const page = mountPage()
    const custom = [
      'stock_code', 'name', 'exchange', 'csrc_l1', 'csrc_l2',
      'latest_close', 'total_market_cap', 'dividend_financing_ratio_pct',
    ]
    page.rules.applyLoadedRule(savedRule(48, custom))

    expect(page.selectedResultColumns.value).toEqual(custom)
    page.scope.stop()
  })

  it('规则里的 columns 为空 → 回落默认 15 列', () => {
    const page = mountPage()
    page.rules.applyLoadedRule(savedRule(51, []))

    expect(page.selectedResultColumns.value).toEqual([...DEFAULT_RESULT_COLUMNS])
    page.scope.stop()
  })
})

describe('C. 版本号被提前写成 3，列仍是旧默认集合 → 仍要迁移', () => {
  it('草稿 version=3 且列 = 旧 8 列 → 迁移', async () => {
    const page = mountPage()
    ;(axios.get as unknown as { mockResolvedValue: (v: unknown) => void }).mockResolvedValue({
      data: {
        draft: { ...REAL_DRAFT_111, result_columns: LEGACY_8, default_columns_version: 3 },
        revision: 200,
      },
    })

    await page.draft.restoreDraft()
    await nextTick()

    expect(page.selectedResultColumns.value).toEqual([...DEFAULT_RESULT_COLUMNS])
    for (const column of INDICATOR_COLUMNS) {
      expect(page.columns.selectedColumnsForRequest()).toContain(column)
    }
    page.scope.stop()
  })

  it('草稿 version=3 且列是纯元数据残留 → 迁移（判据与版本号无关）', async () => {
    const page = mountPage()
    ;(axios.get as unknown as { mockResolvedValue: (v: unknown) => void }).mockResolvedValue({
      data: {
        draft: {
          ...REAL_DRAFT_111,
          result_columns: ['stock_code', 'name', 'csrc_l2', 'latest_close'],
          default_columns_version: 3,
        },
        revision: 201,
      },
    })

    await page.draft.restoreDraft()
    await nextTick()

    expect(page.selectedResultColumns.value).toEqual([...DEFAULT_RESULT_COLUMNS])
    page.scope.stop()
  })

  it('空列表 = 用户主动清空 → 不迁移，回落到默认列', async () => {
    const page = mountPage()
    ;(axios.get as unknown as { mockResolvedValue: (v: unknown) => void }).mockResolvedValue({
      data: { draft: { ...REAL_DRAFT_111, result_columns: [] }, revision: 202 },
    })

    await page.draft.restoreDraft()
    await nextTick()

    // 不迁移 ≠ 不留列：空选择本来就该回落到默认列（既有行为）。
    expect(page.selectedResultColumns.value).toEqual([...DEFAULT_RESULT_COLUMNS])
    expect(page.columns.selectedColumnsForRequest()).toContain('pe_ttm')
    page.scope.stop()
  })
})

describe('D. 草稿只读契约：GET 是唯一请求，迁移不会偷偷写库', () => {
  it('restoreDraft 只发一次 GET，不 PUT', async () => {
    const page = mountPage()
    ;(axios.get as unknown as { mockResolvedValue: (v: unknown) => void }).mockResolvedValue({
      data: { draft: REAL_DRAFT_111, revision: 111 },
    })

    await page.draft.restoreDraft()
    await nextTick()

    expect(axios.get).toHaveBeenCalledTimes(1)
    expect(axios.get).toHaveBeenCalledWith(DRAFT_URL)
    expect(axios.put).not.toHaveBeenCalled()
    page.scope.stop()
  })
})
