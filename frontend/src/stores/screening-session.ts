/**
 * 筛选页「切页保状态」的模块级会话单例（2026-11-24）。
 *
 * 用户原话：「我筛选完成之后，点到数据状态那一页，然后再点回去，为什么就自动刷新了？
 * 我的结果没保存就直接丢掉了…… 应该是我**不点浏览器刷新**，那个界面就还保持在那里。」
 *
 * 为什么是**模块级内存单例 + sessionStorage 冷备**，而不是 `<keep-alive>`：
 * 1. `App.vue` 的 `<router-view />` 没有 keep-alive，路由切换会卸载页面组件，
 *    页面里 `ref()` 的结果全没了 —— 这是"切回来就空了"的根因。把**跨挂载要留的东西**
 *    搬到模块作用域即可：组件卸载不销毁模块，SPA 内切页再回来就还在。
 * 2. 「F5 才重置」这条语义原先被当成"天然成立"（F5 重建 JS 上下文）。**2026-11-20 实测
 *    证明这个假设不够**：模块级单例只在同一份文档里有效，任何一次文档重新加载都不分
 *    "切页"和"刷新"，一律归零。现在落一份 `sessionStorage` 冷备，并用
 *    `performance.navigation.type` 区分：`reload`（用户主动刷新）→ 清空；
 *    其余（navigate / back_forward）→ 回填。语义因此变成用户要的那句
 *    「不点浏览器刷新，界面就保持在那里」——**结构上成立**，不再靠运气。
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

/**
 * 落盘键（`sessionStorage`，per-tab）。
 *
 * 为什么需要它 —— 2026-11-20 用户第二次报「切页回来结果又没了」，实测结论：
 * **模块级单例只在同一份 JS 上下文里有效**。路由切页（SPA）确实保得住（两条
 * 组件测试钉着），但只要文档被重新加载一次（浏览器刷新、地址栏回车、页面内任何
 * `location.reload()`，例如草稿冲突弹窗里的「刷新页面」），模块重跑、单例归零，
 * 用户看到的就是"结果没了"。用户要的语义是**"只有我自己按刷新才重置"**，
 * 所以这里把「同一份文档内的切页」扩成「同一次浏览会话内的切页」：
 * 文档重新加载时先回填，只有**用户主动刷新**（navigation type = `reload`）才清。
 *
 * 三个边界都钉死了：
 * 1. 新开标签页 → `sessionStorage` 是空的 → 空态（不会把上一次会话的结果带过来）；
 * 2. F5 / Ctrl+R / 地址栏回车按下刷新 → `type === 'reload'` → **先清再空态**；
 * 3. SPA 切页 → 不重新加载文档，直接命中模块单例，连 `sessionStorage` 都不用读。
 */
const STORAGE_KEY = 'vd.screening.session.v1'

/**
 * 本次文档是怎么来的：`reload`（用户刷新）/ `navigate`（新导航）/ `back_forward`。
 *
 * 读不到 `performance`（老浏览器、部分测试环境）时返回 `'unknown'` —— 此时**不**
 * 清空（宁可多保一次，也不要再多清一次：清空正是用户最恨的行为）。
 */
export function documentLoadKind(): 'reload' | 'navigate' | 'back_forward' | 'unknown' {
  try {
    const entries = typeof performance !== 'undefined'
      ? performance.getEntriesByType?.('navigation') as PerformanceNavigationTiming[] | undefined
      : undefined
    const type = entries?.[0]?.type
    if (type === 'reload' || type === 'navigate' || type === 'back_forward') return type
  } catch {
    /* 读不到就走 unknown */
  }
  return 'unknown'
}

function storage(): Storage | null {
  try {
    return typeof sessionStorage !== 'undefined' ? sessionStorage : null
  } catch {
    // 隐私模式 / 被禁用的 Storage：退化成纯内存单例（行为 = 改造前）。
    return null
  }
}

function dropPersisted(): void {
  try {
    storage()?.removeItem(STORAGE_KEY)
  } catch {
    /* 落盘失败不影响内存态 */
  }
}

/** 把内存态写进 `sessionStorage`（`running` 不落盘：重新加载后不该继续转圈）。 */
function persist(): void {
  const store = storage()
  if (!store) return
  try {
    store.setItem(STORAGE_KEY, JSON.stringify({
      run: screeningSession.run,
      activeRuleId: screeningSession.activeRuleId,
      ruleName: screeningSession.ruleName,
    }))
  } catch {
    /* 配额/隐私模式：忽略，内存态照常工作 */
  }
}

/** 文档重新加载后回填（新标签页里 `sessionStorage` 为空 → 什么都不做）。 */
function hydrate(): void {
  if (documentLoadKind() === 'reload') {
    // 用户主动刷新 = 唯一允许重置的入口。
    dropPersisted()
    return
  }
  const store = storage()
  if (!store) return
  try {
    const raw = store.getItem(STORAGE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as Partial<ScreeningSessionState> | null
    if (!parsed || typeof parsed !== 'object') return
    if (parsed.run && Array.isArray(parsed.run.results)) {
      screeningSession.run = parsed.run
    }
    if (typeof parsed.activeRuleId === 'number') screeningSession.activeRuleId = parsed.activeRuleId
    if (typeof parsed.ruleName === 'string') screeningSession.ruleName = parsed.ruleName
  } catch {
    // 坏数据（旧版本格式/被截断）→ 当作没有，不要让页面崩在 setup 里。
    dropPersisted()
  }
}

/** 模块级单例：组件卸载不销毁；文档重新加载时由 `hydrate()` 从 `sessionStorage` 回填。 */
export const screeningSession = reactive<ScreeningSessionState>(emptyState())

hydrate()

/**
 * 回到"刚 F5 完"的初态。
 *
 * 生产代码里只有**用户刷新**（浏览器重建模块 + `hydrate()` 判定 `reload`）与
 * 测试会走到这里；它把「想重置就整体重置」这条语义显式化，且顺手清掉落盘副本，
 * 避免下一次挂载把已经清掉的结果又读回来。
 */
export function resetScreeningSession(): void {
  Object.assign(screeningSession, emptyState())
  dropPersisted()
}

/** 记下"这次是由哪个规则版本、按什么口径跑出来的"。 */
export function saveRunSnapshot(snapshot: Omit<ScreeningRunSnapshot, 'createdAt' | 'expired'>): void {
  screeningSession.run = { ...snapshot, createdAt: Date.now(), expired: false }
  persist()
}

/** 标记当前运行凭据已失效（服务端 TTL 到期，或 `/save` 报 run not found）。 */
export function markRunExpired(): void {
  if (screeningSession.run) screeningSession.run.expired = true
  persist()
}

/** 编辑区绑定（当前规则 / 规则名）随会话落盘。 */
export function rememberEditorBinding(ruleId: number, ruleName: string): void {
  screeningSession.activeRuleId = ruleId
  screeningSession.ruleName = ruleName
  persist()
}

/**
 * 丢掉"上一次成功运行"的整份产物（结果 / runId / 披露告警 …）。
 *
 * 谁该调用它：**只有"本次运行的产物确定作废"**那种场景（页面 `runState` 落到
 * `failed` 时，"失败即清空"是主线语义 —— 留着旧结果会让用户把上一次的股票当成
 * 当前规则的结果，保存进自选就是数据污染）。
 *
 * ⚠️ 2026-11-20 二次事故（本次修复）：这里曾被**任何** `/run` 失败无条件调用，
 * 包括 `409 数据正在自动更新 / 写锁被挡住`——那一次请求**根本没有执行**，旧结果
 * 没有被任何新结果取代。服务端日志实证（`data/logs/start.log`）：
 * `筛选运行被写锁挡住且无上次成功结果` 在用户实测窗口反复出现（19:42:33 /
 * 19:42:47 / 19:45:53 / 19:46:01 / 19:46:02 …，当时自动更新子进程正持写锁）。
 * 于是"切页回来点一下运行 → 409 → 整页结果被清空 → 再切页回来当然是空的"，
 * 用户读到的就是「筛选页之前运行的结果全都没了」。调用方现在必须先区分
 * 「没跑成」（保留）与「跑坏了」（清空），见 `ScreeningPage.vue::isRunNotExecuted`。
 *
 * 统一从这里清，而不是让页面逐个字段回写：否则「切页保状态」会在下一次挂载时
 * 把这些字段重新读回来，失败态的清空等于没发生过。
 */
export function clearRunSnapshot(): void {
  screeningSession.run = null
  persist()
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

