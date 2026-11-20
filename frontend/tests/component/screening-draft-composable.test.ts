/**
 * 筛选草稿状态机契约（2026-11-22 反返工护栏）。
 *
 * 守护对象：`src/composables/useScreeningDraft.ts`（2026-11-22 从
 * `views/ScreeningPage.vue` 抽出的草稿逻辑）。
 *
 * 为什么必须钉死：这套逻辑踩过 3 次生产级坑，且都"看起来能跑"：
 * - reports/42 P3：未保存判定拿"目标规则"比对 → 加载旧版无 id 规则被误判为已修改，
 *   弹了不该弹的覆盖确认；现在基于"上次已同步快照"。
 * - reports/42 L0-4：409 冲突后永久停用自动保存 → 用户后续编辑永远不落盘；
 *   现在停用 + 弹三选一。
 * - 保存单飞：连点/连改时并发 PUT 互相覆盖 revision；现在 in-flight 时只置队列。
 *
 * 抽 composable 时最容易"顺手重排生命周期"，所以这里用假定时器把
 * 400ms 防抖、单飞合并、409 停用逐一断言。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { effectScope, nextTick, reactive, ref } from 'vue'
import type { EffectScope } from 'vue'
import type { Mock } from 'vitest'

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
import { useScreeningDraft } from '../../src/composables/useScreeningDraft.ts'
import { DEFAULT_RESULT_COLUMNS, DEFAULT_RESULT_COLUMNS_VERSION } from '../../src/composables/useScreeningColumns.ts'
import type { ScreeningRuleNode } from '../../src/types/screening.ts'

const DRAFT_URL = '/api/screening/draft'

function mountDraft(options: { defaultResultColumns?: readonly string[] } = {}) {
  const ruleTree = reactive<ScreeningRuleNode>({
    id: 'root',
    logic: 'AND',
    rules: [{ id: 'c1', field: 'pe_ttm', op: '<', value: 15 }],
  } as unknown as ScreeningRuleNode)
  const basePool = reactive({ exclude_st: true, exclude_suspended: true, min_listing_years: 1 })
  // 用户裁定（本轮）：页面上的「仅使用严格可信数据」开关已移除，ScreeningPage 传进来的
  // strictOnly 恒为 false。这里仍用本地 `ref` 直接驱动 composable —— 本文件覆盖的是
  // **草稿状态机这个通用管道**（`strict_only` 字段要原样透传/回填，后端能力保留），
  // 与实际页面传什么值无关。页面级的"恒 false"由 screening-run-flow.test.ts 断言。
  const strictOnly = ref(false)
  const selectedResultColumns = ref<string[]>(['stock_code'])
  const removedResultColumns = ref<string[]>([])
  const notify = { warning: vi.fn(), success: vi.fn(), error: vi.fn() }
  const scope: EffectScope = effectScope()
  const draft = scope.run(() => useScreeningDraft({
    ruleTree,
    basePool,
    strictOnly,
    selectedResultColumns,
    removedResultColumns,
    defaultResultColumns: options.defaultResultColumns ?? ['stock_code', 'name'],
    defaultResultColumnsVersion: DEFAULT_RESULT_COLUMNS_VERSION,
    encodeResultColumn: (column) => (typeof column === 'string' ? column : String(column.field)),
    notify,
  }))!
  return {
    draft,
    ruleTree,
    basePool,
    strictOnly,
    selectedResultColumns,
    removedResultColumns,
    notify,
    scope,
  }
}

describe('useScreeningDraft 未保存修改判定（reports/42 P3）', () => {
  it('快照未建立前不算"有未保存修改"（避免首次进入就误弹确认）', () => {
    const { draft, scope } = mountDraft()
    expect(draft.hasUnsavedEdits()).toBe(false)
    draft.syncEditorSnapshot()
    expect(draft.hasUnsavedEdits()).toBe(false)
    scope.stop()
  })

  it('改动条件 → 有未保存修改；重新同步快照 → 恢复干净', () => {
    const { draft, ruleTree, scope } = mountDraft()
    draft.syncEditorSnapshot()
    ;(ruleTree.rules[0] as unknown as { value: number }).value = 20
    expect(draft.hasUnsavedEdits()).toBe(true)

    draft.syncEditorSnapshot()
    expect(draft.hasUnsavedEdits()).toBe(false)
    scope.stop()
  })

  it('快照只含 conditions：改常驻池/严格口径/结果列不算"条件未保存"', () => {
    const { draft, basePool, strictOnly, selectedResultColumns, scope } = mountDraft()
    draft.syncEditorSnapshot()
    basePool.exclude_st = false
    strictOnly.value = true
    selectedResultColumns.value = ['stock_code', 'name']
    expect(draft.hasUnsavedEdits()).toBe(false)
    scope.stop()
  })
})

describe('useScreeningDraft 自动保存（防抖 400ms + 单飞 + 409）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('未放行（草稿尚未恢复完成）时编辑不触发保存', async () => {
    const { ruleTree, draft, scope } = mountDraft()
    ruleTree.logic = 'OR'
    await nextTick()
    await vi.advanceTimersByTimeAsync(1000)
    expect(axios.put).not.toHaveBeenCalled()
    expect(draft.draftState().hydrated).toBe(false)
    scope.stop()
  })

  it('400ms 内连续改动只发一次 PUT，payload 含四类草稿字段', async () => {
    const { draft, ruleTree, basePool, strictOnly, selectedResultColumns, scope } = mountDraft()
    ;(axios.put as Mock).mockResolvedValue({ data: { revision: 7 } })
    draft.markDraftHydrated()

    ruleTree.logic = 'OR'
    await nextTick()
    await vi.advanceTimersByTimeAsync(200)
    basePool.min_listing_years = 3
    await nextTick()
    await vi.advanceTimersByTimeAsync(200)
    strictOnly.value = true
    selectedResultColumns.value = ['stock_code', 'name']
    await nextTick()

    expect(axios.put).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(400)

    expect(axios.put).toHaveBeenCalledTimes(1)
    const [url, body] = (axios.put as Mock).mock.calls[0]
    expect(url).toBe(DRAFT_URL)
    expect(body.revision).toBe(0)
    // 透传契约：composable 只负责把注入的 strictOnly 原样写进草稿。
    // 页面（ScreeningPage）现在恒传 false，并把历史 true 归一成 false —— 见
    // screening-run-flow.test.ts「历史草稿残留 strict_only: true」用例。
    expect(body.draft.strict_only).toBe(true)
    expect(body.draft.base_pool).toEqual({ exclude_st: true, exclude_suspended: true, min_listing_years: 3 })
    expect(body.draft.result_columns).toEqual(['stock_code', 'name'])
    expect(body.draft.default_columns_version).toBe(DEFAULT_RESULT_COLUMNS_VERSION)
    expect(body.draft.removed_result_columns).toEqual([])
    expect(body.draft.conditions.logic).toBe('OR')
    // 成功后刷新快照 + 记录新 revision
    expect(draft.draftState().revision).toBe(7)
    expect(draft.hasUnsavedEdits()).toBe(false)
    scope.stop()
  })

  it('保存在飞时再次触发只排队，落定后补发一次（不并发覆盖 revision）', async () => {
    const { draft, ruleTree, scope } = mountDraft()
    let release: (value: unknown) => void = () => {}
    ;(axios.put as Mock).mockImplementationOnce(
      () => new Promise((resolve) => { release = resolve }),
    )
    ;(axios.put as Mock).mockResolvedValue({ data: { revision: 2 } })
    draft.markDraftHydrated()

    ruleTree.logic = 'OR'
    await nextTick()
    await vi.advanceTimersByTimeAsync(400)
    expect(axios.put).toHaveBeenCalledTimes(1)
    expect(draft.draftState().inFlight).toBe(true)

    ruleTree.logic = 'AND'
    await nextTick()
    await vi.advanceTimersByTimeAsync(400)
    // 在飞期间不再发第二个请求，只置队列
    expect(axios.put).toHaveBeenCalledTimes(1)
    expect(draft.draftState().queued).toBe(true)

    release({ data: { revision: 1 } })
    await vi.advanceTimersByTimeAsync(0)
    await nextTick()
    await nextTick()
    expect(axios.put).toHaveBeenCalledTimes(2)
    expect(draft.draftState().queued).toBe(false)
    scope.stop()
  })

  it('409 冲突：弹三选一弹窗 + 暂停自动保存（不再继续 PUT）', async () => {
    const { draft, ruleTree, scope } = mountDraft()
    ;(axios.put as Mock).mockRejectedValue({ isAxiosError: true, response: { status: 409 } })
    draft.markDraftHydrated()

    ruleTree.logic = 'OR'
    await nextTick()
    await vi.advanceTimersByTimeAsync(400)

    expect(draft.showDraftConflictModal.value).toBe(true)
    expect(draft.draftState().hydrated).toBe(false)

    ruleTree.logic = 'AND'
    await nextTick()
    await vi.advanceTimersByTimeAsync(4000)
    expect(axios.put).toHaveBeenCalledTimes(1)
    scope.stop()
  })

  it('非 409 失败只提示，不阻断后续自动保存', async () => {
    const { draft, ruleTree, notify, scope } = mountDraft()
    ;(axios.put as Mock).mockRejectedValueOnce(new Error('network'))
    ;(axios.put as Mock).mockResolvedValue({ data: { revision: 3 } })
    draft.markDraftHydrated()

    ruleTree.logic = 'OR'
    await nextTick()
    await vi.advanceTimersByTimeAsync(400)
    expect(notify.warning).toHaveBeenCalledWith('筛选草稿自动保存失败')
    expect(draft.draftState().hydrated).toBe(true)

    ruleTree.logic = 'AND'
    await nextTick()
    await vi.advanceTimersByTimeAsync(400)
    expect(axios.put).toHaveBeenCalledTimes(2)
    scope.stop()
  })

  it('restoreDraft：服务端无草稿时也刷新快照，避免首屏误判为已修改', async () => {
    const { draft, scope } = mountDraft()
    ;(axios.get as Mock).mockResolvedValue({ data: { draft: null, revision: 4 } })
    await draft.restoreDraft()
    expect(draft.draftState().revision).toBe(4)
    expect(draft.hasUnsavedEdits()).toBe(false)
    scope.stop()
  })

  it('restoreDraft：草稿里的 result_columns 会被解码回合成取值', async () => {
    const { draft, selectedResultColumns, scope } = mountDraft()
    ;(axios.get as Mock).mockResolvedValue({
      data: {
        draft: {
          conditions: { id: 'root', logic: 'OR', rules: [] },
          base_pool: { min_listing_years: 5 },
          strict_only: true,
          result_columns: ['stock_code', { field: 'revenue_cagr', cagr_years: 5 }],
        },
        revision: 9,
      },
    })
    await draft.restoreDraft()
    expect(selectedResultColumns.value).toEqual(['stock_code', 'revenue_cagr'])
    expect(draft.hasUnsavedEdits()).toBe(false)
    scope.stop()
  })

  it('restoreDraft：草稿没带 result_columns 且当前为空 → 回落默认列', async () => {
    const { draft, selectedResultColumns, scope } = mountDraft()
    selectedResultColumns.value = []
    ;(axios.get as Mock).mockResolvedValue({ data: { draft: { conditions: null }, revision: 0 } })
    await draft.restoreDraft()
    expect(selectedResultColumns.value).toEqual(['stock_code', 'name'])
    scope.stop()
  })

  it('restoreDraft 请求失败只提示，不抛（不阻断其余加载）', async () => {
    const { draft, notify, scope } = mountDraft()
    ;(axios.get as Mock).mockRejectedValue(new Error('boom'))
    await expect(draft.restoreDraft()).resolves.toBeUndefined()
    expect(notify.warning).toHaveBeenCalledWith('无法恢复最近筛选草稿')
    scope.stop()
  })
})


describe('旧草稿默认列迁移（默认列版本号）', () => {
  const OLD_8 = [
    'stock_code', 'name', 'exchange', 'listing_date',
    'csrc_l1', 'csrc_l2', 'latest_close', 'total_market_cap',
  ]

  it('版本缺失且 result_columns 恰好是旧 8 列默认集合 → 替换为新默认', async () => {
    const { draft, selectedResultColumns, scope } = mountDraft({
      defaultResultColumns: DEFAULT_RESULT_COLUMNS,
    })
    ;(axios.get as Mock).mockResolvedValue({
      data: {
        draft: {
          conditions: { id: 'root', logic: 'AND', rules: [] },
          result_columns: OLD_8,
        },
        revision: 3,
      },
    })
    await draft.restoreDraft()
    expect(selectedResultColumns.value).toEqual([...DEFAULT_RESULT_COLUMNS])
    expect(selectedResultColumns.value).not.toContain('csrc_l1')
    scope.stop()
  })

  it('用户自定义过列（即使版本更旧）不被覆盖', async () => {
    const { draft, selectedResultColumns, scope } = mountDraft({
      defaultResultColumns: DEFAULT_RESULT_COLUMNS,
    })
    ;(axios.get as Mock).mockResolvedValue({
      data: {
        draft: {
          conditions: { id: 'root', logic: 'AND', rules: [] },
          result_columns: ['stock_code', 'roce_10y_median'],
          default_columns_version: 0,
        },
        revision: 3,
      },
    })
    await draft.restoreDraft()
    expect(selectedResultColumns.value).toEqual(['stock_code', 'roce_10y_median'])
    scope.stop()
  })

  it('恢复手动移除状态，后续条件重算也不会加回', async () => {
    const { draft, removedResultColumns, scope } = mountDraft()
    ;(axios.get as Mock).mockResolvedValue({
      data: {
        draft: {
          conditions: { id: 'root', logic: 'AND', rules: [] },
          result_columns: ['stock_code', 'name'],
          removed_result_columns: ['roce_10y_median'],
          default_columns_version: DEFAULT_RESULT_COLUMNS_VERSION,
        },
        revision: 3,
      },
    })
    await draft.restoreDraft()
    expect(removedResultColumns.value).toEqual(['roce_10y_median'])
    scope.stop()
  })
})
