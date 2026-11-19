/**
 * 筛选「已保存规则」CRUD 与规则装载。
 *
 * 2026-11-22 重构：从 `views/ScreeningPage.vue` 原样抽出，**行为逐字等价**。
 * 抽取理由：规则 CRUD 是页面里第二大的独立职责（约 130 行，含 5 个 axios 调用点），
 * 与"数据质量门禁 / 指标目录 / 草稿 / 结果列"无耦合，只依赖编辑区与草稿快照。
 *
 * 关键不变量（改之前请先读，破一条就是行为变更）：
 * 1. `applyLoadedRule` 必须**深拷贝** conditions —— 直接 Object.assign 会把
 *    `conditions.rules` 数组引用共享给编辑区，用户一改条件值，
 *    `savedRules` 里的"已保存版本"会同步被改掉，`isDraftDirty()` 随即判定"无修改"，
 *    于是永远按旧版本号运行（reports/42 P3）。
 * 2. 装载前只在「编辑区相对上次同步快照有未保存修改」时确认，取消保持原选中项不变。
 * 3. 保存成功后刷新快照（`syncEditorSnapshot`）并重新拉取规则列表。
 */

import { computed, ref } from 'vue'
import type { Ref } from 'vue'
import axios from 'axios'
import type { ScreeningRuleNode } from '../types/screening.ts'
import { cloneScreeningRule } from '../types/screening.ts'
import { apiErrorText, friendlyErrorMessage } from '../helpers/api-error.ts'

export interface SavedRule {
  id: number
  name: string
  version: number
  rule_json: { conditions: ScreeningRuleNode; columns?: string[] }
  locked_indicators: Record<string, unknown>
  status: string
  created_at: string
}

export interface RulesNotifier {
  warning(content: string): void
  success(content: string): void
  error(content: string): void
}

interface ConfirmOptions {
  title?: string
  content?: string
  positiveText?: string
  negativeText?: string
  onPositiveClick?: () => void | Promise<void>
}

/** naive-ui `useDialog()` 的最小结构面（只要 warning 确认框）。 */
export interface ConfirmDialog {
  warning(options: ConfirmOptions): void
}

export interface ScreeningRulesDeps {
  ruleTree: ScreeningRuleNode
  selectedResultColumns: Ref<string[]>
  defaultResultColumns: readonly string[]
  selectedColumnsForRequest: () => unknown[]
  /** 草稿：保存/装载后刷新"上次已同步快照"。 */
  syncEditorSnapshot: () => void
  /** 草稿：是否相对上次同步快照有未保存修改。 */
  hasUnsavedEdits: () => boolean
  notify: RulesNotifier
  dialog: ConfirmDialog
}

export function useScreeningRules(deps: ScreeningRulesDeps) {
  const {
    ruleTree,
    selectedResultColumns,
    defaultResultColumns,
    selectedColumnsForRequest,
    syncEditorSnapshot,
    hasUnsavedEdits,
    notify,
    dialog,
  } = deps

  const savedRules = ref<SavedRule[]>([])
  const selectedRuleId = ref<number>(0)
  const activeRule = computed(() => savedRules.value.find(rule => rule.id === selectedRuleId.value) || null)
  const ruleName = ref('')

  async function loadSavedRules() {
    try {
      const resp = await axios.get<{ rules: SavedRule[] }>('/api/screening/rules')
      savedRules.value = resp.data.rules
    } catch {
      notify.warning('无法加载已保存的规则')
    }
  }

  // 2026-08-14 红队 P2-20：运行始终按已保存版本（rule_id/version）执行；
  // 此前用户选择规则后再改条件/排序会继续运行却静默忽略编辑。
  // 运行前检测草稿与已保存版本的差异并明确告知。
  // 2026-08-26：比较时忽略条件节点的 id（id 只用于 UI 渲染/删除定位，不影响筛选语义），
  // 避免加载旧版无 id 的规则被误判为“已修改”。
  function isDraftDirty(): boolean {
    const rule = activeRule.value
    if (!rule) return false
    const savedConditions = (rule.rule_json.conditions ?? null) as unknown
    const conditionsDirty = JSON.stringify(stripRuleIds(ruleTree)) !== JSON.stringify(stripRuleIds(savedConditions))
    return conditionsDirty
  }

  function autoRuleName(): string {
    const now = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    return `未命名规则 ${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`
  }

  async function persistRule(name: string): Promise<{ rule_id: number; version: number } | null> {
    try {
      const resp = await axios.post<{ rule_id: number; version: number }>('/api/screening/rules/save', {
        name,
        rule_json: {
          conditions: ruleTree,
          columns: selectedColumnsForRequest(),
        },
        locked_indicators: {},
        status: 'saved',
      })
      await loadSavedRules()
      selectedRuleId.value = resp.data.rule_id
      syncEditorSnapshot()
      notify.success(`规则已保存为 v${resp.data.version}`)
      return resp.data
    } catch (e: unknown) {
      notify.error(apiErrorText(e, '保存规则失败'))
      return null
    }
  }

  async function saveRule(): Promise<{ rule_id: number; version: number } | null> {
    const name = ruleName.value.trim()
    if (!name) {
      notify.warning('请输入规则名称')
      return null
    }
    return persistRule(name)
  }

  function deleteSelectedRule(): void {
    const rule = activeRule.value
    if (!rule) return
    dialog.warning({
      title: '删除选中的规则？',
      content: `将删除「${rule.name} v${rule.version}」。已有保存结果或自选来源的规则不能删除。`,
      positiveText: '删除',
      negativeText: '取消',
      onPositiveClick: async () => {
        try {
          await axios.delete(`/api/screening/rules/${rule.id}`)
          const wasSelected = selectedRuleId.value === rule.id
          await loadSavedRules()
          if (wasSelected) selectedRuleId.value = 0
          notify.success('规则已删除')
        } catch (e: unknown) {
          notify.error(friendlyErrorMessage(e, '删除规则失败'))
        }
      },
    })
  }

  function applyLoadedRule(rule: SavedRule) {
    // 先重置编辑区再覆盖，确保目标规则不含 conditions 时不残留旧内容。
    // 必须深拷贝：Object.assign(ruleTree, saved.conditions) 会把 conditions.rules
    // 数组引用共享给编辑区；用户一改条件值，savedRules 里的“已保存版本”会同步
    // 被改掉，isDraftDirty() 随即判定“无修改”，于是永远按旧版本号运行。
    const conditions = rule.rule_json.conditions
    const clone = cloneScreeningRule(
      conditions && conditions.rules
        ? conditions
        : { logic: 'AND', rules: [] },
    )
    ruleTree.logic = 'AND'
    ruleTree.rules.splice(0, ruleTree.rules.length)
    Object.assign(ruleTree, clone)

    selectedResultColumns.value = (rule.rule_json.columns && rule.rule_json.columns.length > 0)
      ? [...rule.rule_json.columns]
      : [...defaultResultColumns]

    notify.success(`已加载规则: ${rule.name} v${rule.version}`)
  }

  function loadRule(ruleId: number) {
    if (!ruleId || ruleId === 0) return

    const rule = savedRules.value.find(r => r.id === ruleId)
    if (!rule) return

    // 2026-08-14 红队 P3：加载已保存规则会覆盖编辑区草稿；仅当编辑区有
    // 未保存修改（相对上次已同步快照）时确认，取消保持原选中项不变。
    const apply = () => {
      selectedRuleId.value = ruleId
      applyLoadedRule(rule)
      syncEditorSnapshot()
    }
    if (!hasUnsavedEdits()) {
      apply()
      return
    }
    dialog.warning({
      title: '覆盖未保存的草稿？',
      content: `加载「${rule.name} v${rule.version}」将覆盖编辑区里未保存的修改。`,
      positiveText: '覆盖并加载',
      negativeText: '取消',
      onPositiveClick: apply,
    })
  }

  const ruleOptions = computed(() => {
    const options: Array<{ label: string; value: number }> = [
      { label: '选择已保存的规则...', value: 0 },
    ]
    for (const r of savedRules.value) {
      options.push({
        label: `${r.name} v${r.version}`,
        value: r.id,
      })
    }
    return options
  })

  return {
    savedRules,
    selectedRuleId,
    activeRule,
    ruleName,
    ruleOptions,
    loadSavedRules,
    isDraftDirty,
    autoRuleName,
    persistRule,
    saveRule,
    deleteSelectedRule,
    applyLoadedRule,
    loadRule,
  }
}

/** 递归去掉条件节点 id，只留筛选语义（运行前脏检查用）。 */
function stripRuleIds(node: unknown): unknown {
  if (Array.isArray(node)) return node.map(stripRuleIds)
  if (node && typeof node === 'object') {
    const result: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
      if (key === 'id') continue
      result[key] = stripRuleIds(value)
    }
    return result
  }
  return node
}
