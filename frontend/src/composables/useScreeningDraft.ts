/**
 * 筛选草稿（/api/screening/draft）存取状态机。
 *
 * 2026-11-22 重构：从 `views/ScreeningPage.vue` 原样抽出，**行为逐字等价**。
 * 原文件把「数据质量门禁 / 指标目录 / 规则树编辑 / 草稿 / 命名 / 结果列」等 9 件事
 * 塞进一个 1060 行组件，草稿这部分是唯一带**定时器 + 单飞 + 409 冲突**的状态机，
 * 抽出来才能单测（`tests/component/screening-draft-composable.test.ts`）。
 *
 * 关键不变量（改之前请先读，破一条就是行为变更）：
 * 1. `draftHydrated` 是**普通 let**，不是 ref —— 它只用于"是否允许自动保存"，
 *    不进渲染，改成 ref 会引入额外依赖追踪。
 * 2. 未保存判定基于「上次已同步快照」`lastSyncedSnapshot`（草稿恢复 / 自动保存成功 /
 *    规则加载 / 保存新版本时刷新），**不是**与目标规则比对（reports/42 P3 旧 bug）。
 * 3. 快照只含 conditions（排序已移出筛选页，2026-11-16）；否则"改排序也算未保存"。
 * 4. 409 时 `draftHydrated=false` 停用自动保存并弹选项，**不永久停用**（reports/42 L0-4）。
 * 5. 保存单飞：在飞时再触发只置 `draftSaveQueued`，落定后补发一次。
 */

import { ref, watch } from 'vue'
import type { Ref } from 'vue'
import axios, { isAxiosError } from 'axios'
import type { ResultColumn, ScreeningRuleNode } from '../types/screening.ts'

/** 常驻范围条件（与页面 `reactive` 同形）。 */
export interface ScreeningBasePool {
  exclude_st: boolean
  exclude_suspended: boolean
  min_listing_years: number
}

/** 只需要这三个方法，避免把 naive-ui 的类型带进 composable。 */
export interface DraftNotifier {
  warning(content: string): void
  success(content: string): void
  error(content: string): void
}

export interface ScreeningDraftDeps {
  /** 规则编辑区（reactive 对象，引用保持不变）。 */
  ruleTree: ScreeningRuleNode
  /** 常驻范围条件（reactive 对象，引用保持不变）。 */
  basePool: ScreeningBasePool
  strictOnly: Ref<boolean>
  selectedResultColumns: Ref<string[]>
  /** 草稿里没有 result_columns 时回落的默认列。 */
  defaultResultColumns: readonly string[]
  /** 后端列定义 → 结果列合成取值。 */
  encodeResultColumn: (column: ResultColumn) => string
  notify: DraftNotifier
  /** 冲突里选「刷新页面」时执行；默认整页重载。 */
  reloadPage?: () => void
}

const draftResponseSchema = '/api/screening/draft'

export function useScreeningDraft(deps: ScreeningDraftDeps) {
  const {
    ruleTree,
    basePool,
    strictOnly,
    selectedResultColumns,
    defaultResultColumns,
    encodeResultColumn,
    notify,
  } = deps
  const reloadPage = deps.reloadPage ?? (() => { window.location.reload() })

  let draftTimer: ReturnType<typeof setTimeout> | undefined
  let draftHydrated = false
  const draftRevision = ref(0)
  let draftSaveInFlight = false
  let draftSaveQueued = false
  // L0-4（报告42）: 草稿 409 冲突后不再永久停用自动保存，而是给出明确选项
  const showDraftConflictModal = ref(false)

  // 2026-08-14 红队 P3：未保存修改判定基于"上次已同步的编辑区快照"
  // （草稿恢复/自动保存/规则加载/保存新版本时刷新），而不是与目标规则
  // 比较——加载目标几乎必然不同，旧逻辑会误弹覆盖确认。
  let lastSyncedSnapshot = ''

  function editorSnapshot(): string {
    // 排序已移到结果页（2026-11-16），编辑器快照只含条件；
    // 否则残留的 sort 会让『改排序也算未保存修改』。
    return JSON.stringify({ conditions: ruleTree })
  }

  function syncEditorSnapshot() {
    lastSyncedSnapshot = editorSnapshot()
  }

  function hasUnsavedEdits(): boolean {
    return lastSyncedSnapshot !== '' && editorSnapshot() !== lastSyncedSnapshot
  }

  function draftPayload() {
    return {
      conditions: ruleTree,
      base_pool: { ...basePool },
      strict_only: strictOnly.value,
      result_columns: selectedResultColumns.value,
    }
  }

  async function persistDraft() {
    if (!draftHydrated) return
    if (draftSaveInFlight) {
      draftSaveQueued = true
      return
    }
    draftSaveInFlight = true
    try {
      const response = await axios.put<{ revision: number }>(draftResponseSchema, {
        draft: draftPayload(),
        revision: draftRevision.value,
      })
      draftRevision.value = response.data.revision
      syncEditorSnapshot()
    } catch (error) {
      if (isAxiosError(error) && error.response?.status === 409) {
        draftHydrated = false
        draftSaveQueued = false
        showDraftConflictModal.value = true
      } else {
        notify.warning('筛选草稿自动保存失败')
      }
    } finally {
      draftSaveInFlight = false
      if (draftSaveQueued && draftHydrated) {
        draftSaveQueued = false
        void persistDraft()
      }
    }
  }

  async function resolveDraftConflict(choice: 'server' | 'local' | 'refresh') {
    showDraftConflictModal.value = false
    if (choice === 'refresh') {
      reloadPage()
      return
    }
    if (choice === 'server') {
      try {
        await restoreDraft()
        draftHydrated = true
        notify.success('已加载服务器草稿，自动保存已恢复')
      } catch {
        notify.error('加载服务器草稿失败，请刷新页面')
      }
      return
    }
    try {
      // 保留本地：以服务器当前 revision 覆盖服务器草稿（本地编辑优先）
      const current = await axios.get<{ draft: unknown; revision?: number }>(draftResponseSchema)
      const revision = current.data.revision ?? 0
      const resp = await axios.put<{ revision: number }>(draftResponseSchema, {
        draft: draftPayload(),
        revision,
      })
      draftRevision.value = resp.data.revision
      draftHydrated = true
      syncEditorSnapshot()
      notify.success('已保留本地草稿（覆盖服务器版本），自动保存已恢复')
    } catch {
      notify.error('保留本地草稿失败，请刷新页面')
    }
  }

  function saveDraftSoon() {
    if (!draftHydrated) return
    if (draftTimer) clearTimeout(draftTimer)
    draftTimer = setTimeout(() => void persistDraft(), 400)
  }

  watch(
    [ruleTree, basePool, strictOnly, selectedResultColumns],
    saveDraftSoon,
    { deep: true },
  )

  async function restoreDraft() {
    try {
      const response = await axios.get<{ draft: {
        conditions?: ScreeningRuleNode
        base_pool?: Partial<ScreeningBasePool>
        strict_only?: boolean
        result_columns?: string[]
      } | null; revision?: number }>(draftResponseSchema)
      const draft = response.data.draft
      draftRevision.value = response.data.revision ?? 0
      if (!draft) {
        syncEditorSnapshot()
        return
      }
      if (draft.conditions) Object.assign(ruleTree, draft.conditions)
      if (draft.base_pool) Object.assign(basePool, draft.base_pool)
      if (typeof draft.strict_only === 'boolean') strictOnly.value = draft.strict_only
      if (Array.isArray(draft.result_columns) && draft.result_columns.length > 0) {
        selectedResultColumns.value = draft.result_columns.map(encodeResultColumn)
      } else if (selectedResultColumns.value.length === 0) {
        selectedResultColumns.value = [...defaultResultColumns]
      }
      syncEditorSnapshot()
    } catch {
      notify.warning('无法恢复最近筛选草稿')
    }
  }

  /** 挂载时恢复草稿后放行自动保存（原 `draftHydrated = true`）。 */
  function markDraftHydrated() {
    draftHydrated = true
  }

  /** 仅测试/诊断用：读当前状态，不改行为。 */
  function draftState() {
    return {
      hydrated: draftHydrated,
      revision: draftRevision.value,
      inFlight: draftSaveInFlight,
      queued: draftSaveQueued,
      hasTimer: draftTimer !== undefined,
      lastSyncedSnapshot,
    }
  }

  return {
    draftRevision,
    showDraftConflictModal,
    editorSnapshot,
    syncEditorSnapshot,
    hasUnsavedEdits,
    draftPayload,
    persistDraft,
    resolveDraftConflict,
    saveDraftSoon,
    restoreDraft,
    markDraftHydrated,
    draftState,
  }
}
