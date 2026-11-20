/**
 * 筛选页「切页保状态」的模块级会话单例（2026-11-24）。
 *
 * 用户原话：「我筛选完成之后，点到数据状态那一页，然后再点回去，为什么就自动刷新了？
 * 我的结果没保存就直接丢掉了…… 应该是我**不点浏览器刷新**，那个界面就还保持在那里。」
 *
 * 为什么是**模块级内存单例**，而不是 `<keep-alive>` 或 `sessionStorage`：
 * 1. `App.vue` 的 `<router-view />` 没有 keep-alive，路由切换会卸载页面组件，
 *    页面里 `ref()` 的结果全没了 —— 这是"切回来就空了"的根因。把**跨挂载要留的东西**
 *    搬到模块作用域即可：组件卸载不销毁模块，SPA 内切页再回来就还在。
 * 2. 「F5 才重置」这条语义**天然成立**：F5 会重建整个 JS 上下文，本模块重新初始化，
 *    状态回到初值。若改用 `sessionStorage`，F5 之后数据还在 —— 反而不符合用户认可的语义，
 *    还得再引入"本次文档是否已加载过"的标记去区分 F5 与切页，多一层易错状态。
 * 3. 不用 `<keep-alive>`：它对**所有**缓存页面都生效（数据状态页/个股详情页都在轮询、
 *    都在挂图表），一刀切会把后台轮询和图表一起留活；而且 keep-alive 下页面实例不卸载，
 *    `onMounted` 里的指标目录/草稿恢复不会再跑，一旦真需要重新同步就只能靠 `onActivated`
 *    打补丁。这里只外置"用户跑出来的东西"，其他生命周期行为一律不变。
 *
 * 因此本模块**只放跨挂载必须保住的东西**（运行结果 + 披露告警 + 当前规则绑定），
 * 编辑区条件仍由服务端草稿（`useScreeningDraft`）负责 —— 那份数据本来就是服务端持久化的。
 *
 * 失效语义（必读）：`runId` 是服务端 `screening_runs` 里的一次性凭据，
 * 后端只保留 `SCREENING_RUN_TTL_HOURS = 24` 小时，且 `/save` 成功后会直接删掉它。
 * 过期后**绝不能清空页面**（那正是用户最恨的），而是保留展示 + 把保存入口置灰 + 说明原因，
 * 见 `runExpired` / `markRunExpired()`。
 */

import { reactive } from 'vue'
import type { ScreeningResult, WarningCode } from '../types/screening.ts'
import type { ReadinessNotice } from '../utils/readiness-notice.ts'

/**
 * 服务端保留筛选运行的时长，与 `app/web/api/screening.py::SCREENING_RUN_TTL_HOURS` 对齐。
 * 这是**过期判定的保守上界**：超过它，`screening_runs` 里那行一定已被惰性清理逻辑删掉
 * （清理只在 `/run` 时执行，所以未满 24h 的行也可能已被 `/save` 消费，那种情形由
 * `markRunExpired()` 按服务端错误码兜底）。
 */
export const SERVER_RUN_TTL_MS = 24 * 60 * 60 * 1000

/** 本次结果由哪个规则版本产生（结果面板的"执行规则"与保存入口都依赖它）。 */
export interface ScreeningRunContext {
  readonly ruleId: number | null
  readonly ruleVersion: number | null
  readonly ruleName: string
}

/**
 * disclose 级告警（`screening_warnings: [{code, severity, detail}]`）。
 *
 * 与 `warningCodes`（数据质量**硬阻断/遮蔽**码）语义不同：这里的东西**不阻断**
 * 筛选，但用户必须看见（黄色提示条）。它同样是"这次运行当时"的披露内容，
 * 切页回来必须原样保留，不能被"现在的"数据状态覆盖。
 *
 * 类型与 `ScreeningPage.vue` / `ScreeningResultsPanel.vue` 里的同形结构对齐
 * （`types/screening.ts` 是跨页面共享契约，不在本轮改动范围内）。
 */
export interface ScreeningDisclosedWarning {
  readonly code: string
  readonly severity?: string
  readonly detail?: string
}

/** 一次成功 `/api/screening/run` 的全部可展示产物 + 当时的披露告警。 */
export interface ScreeningRunSnapshot {
  results: readonly ScreeningResult[]
  runId: string | null
  totalMatched: number
  executionTime: number
  basePoolSize: number
  dataDate: string | null
  truncated: boolean
  /** reports/79 方案 A：更新窗口内快照口径运行标注。 */
  autoUpdateSnapshot: boolean
  snapshotAsOf: string | null
  /** reports/140 §15.5：本次「就绪」结论的新鲜度/降级提示。 */
  readinessHint: ReadinessNotice | null
  strictModeWarning: string | null
  strictExemptFields: readonly string[]
  /** disclose 级告警（黄色提示条）。 */
  screeningWarnings?: readonly ScreeningDisclosedWarning[]
  /** 数据质量披露（warning_codes）与当时的质量状态 —— 切页回来必须原样保留。 */
  warningCodes: readonly WarningCode[]
  qualityStatus: 'loading' | 'available' | 'failed'
  dataReady: boolean | null
  /** 产生这批结果的口径（粗略回填草稿时用来判断"开关值是不是用户改的"）。 */
  strictOnly: boolean
  /** 结果产生时刻（`Date.now()`），TTL 过期判定用。 */
  createdAt: number
  /** 服务端已回收这张运行凭据：仍展示结果，但保存/导出/加入自选停用。 */
  expired: boolean
  context: ScreeningRunContext
}

export interface ScreeningSessionState {
  /** 最近一次成功运行；`null` = 本文档还没跑过（首次进入 / F5 之后）。 */
  run: ScreeningRunSnapshot | null
  /** 编辑区当前绑定的已保存规则 —— 不恢复它 `activeRule` 会变 null，保存入口被误置灰。 */
  activeRuleId: number
  ruleName: string
  /** `/run` 在飞：切走再切回时按钮仍是 loading，且不会被重复触发。 */
  running: boolean
}

function emptyState(): ScreeningSessionState {
  return { run: null, activeRuleId: 0, ruleName: '', running: false }
}

/** 模块级单例：组件卸载不销毁，F5 重建 JS 上下文时自然回到空态。 */
export const screeningSession = reactive<ScreeningSessionState>(emptyState())

/**
 * 回到"刚 F5 完"的初态。
 *
 * 生产代码只有测试会调（正常 F5 由浏览器重建模块完成），但它也把
 * 「模块级状态 = 页面级状态」这条语义显式化了：想重置就整体重置，不要逐字段清。
 */
export function resetScreeningSession(): void {
  Object.assign(screeningSession, emptyState())
}

/** 记下"这次是由哪个规则版本、按什么口径跑出来的"。 */
export function saveRunSnapshot(snapshot: Omit<ScreeningRunSnapshot, 'createdAt' | 'expired'>): void {
  screeningSession.run = { ...snapshot, createdAt: Date.now(), expired: false }
}

/** 标记当前运行凭据已失效（服务端 TTL 到期，或 `/save` 报 run not found）。 */
export function markRunExpired(): void {
  if (screeningSession.run) screeningSession.run.expired = true
}

/**
 * 丢掉"上一次成功运行"的整份产物（结果 / runId / 披露告警 …）。
 *
 * 谁该调用它：**只有运行失败**那种"本次运行产物作废"的场景（页面 `runState`
 * 落到 `failed` 时，"失败即清空"是主线语义 —— 留着旧结果会让用户把上一次的
 * 股票当成当前规则的结果，保存进自选就是数据污染）。
 *
 * 统一从这里清，而不是让页面逐个字段回写：否则「切页保状态」会在下一次挂载时
 * 把这些字段重新读回来，失败态的清空等于没发生过。
 */
export function clearRunSnapshot(): void {
  screeningSession.run = null
}

/**
 * 按 TTL 惰性判定过期（不发请求）。
 *
 * 返回是否在这次调用里**新**判定为过期 —— 调用方据此提示一次即可，不必每次重渲染都提示。
 */
export function refreshRunExpiry(now: number = Date.now()): boolean {
  const run = screeningSession.run
  if (!run || run.expired) return false
  if (now - run.createdAt <= SERVER_RUN_TTL_MS) return false
  run.expired = true
  return true
}

