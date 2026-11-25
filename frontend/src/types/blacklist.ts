/**
 * 股票黑名单契约（`/api/blacklist/*`）。
 *
 * 语义：加入黑名单的股票不再出现在**任何研究面**（筛选/搜索/自选/详情/导出），
 * 但数据库原始数据一行不动、自动更新照常抓取。
 * 判定与排除的唯一实现见后端 `app/core/blacklist.py`。
 */

/** `GET /api/blacklist/list` 的一行。名称/行业来自 stock_meta（可能为空）。 */
export interface BlacklistItem {
  readonly stock_code: string
  readonly name: string
  readonly exchange: string
  readonly csrc_l1: string | null
  /** 可选备注（加入时不填则为 null） */
  readonly reason: string | null
  readonly created_at: string | null
}

export interface BlacklistListResponse {
  readonly items: ReadonlyArray<BlacklistItem>
  readonly count: number
}

/**
 * 单股端点被屏蔽时的 404 `detail` 结构。
 *
 * 后端刻意用**结构体**而不是字符串：前端要能区分
 * 「这个代码不存在」与「这个代码被黑名单屏蔽」——后者要渲染屏蔽态
 * （带移出按钮的自救入口），前者是错误终态。
 */
export interface BlacklistedDetail {
  readonly code: 'stock_blacklisted'
  readonly message: string
}

export function isBlacklistedDetail(detail: unknown): detail is BlacklistedDetail {
  return (
    typeof detail === 'object' &&
    detail !== null &&
    (detail as { code?: unknown }).code === 'stock_blacklisted'
  )
}

// ─── 浮动黑名单（规则驱动，2026-11-22 用户裁定）─────────────────────
//
// 与彻底拉黑的分工：彻底拉黑是人手工点、身份型；浮动是规则库自动算、特征型。
// **浮动拉黑没有任何手工入口** —— 想让某只股票浮动拉黑，去改规则。

/** 一条浮动黑名单规则。`conditions` 与筛选规则同一套结构。 */
export interface BlacklistRule {
  readonly id: number
  /** 预置规则才有（如 `lidaxiao.newly_listed`），手工规则为 null */
  readonly rule_key: string | null
  readonly name: string
  /** 推导说明：依据 + 时间锚定换算 + 已知误差 */
  readonly note: string | null
  readonly enabled: boolean
  readonly conditions: Record<string, unknown>
  readonly pool: Record<string, unknown>
  readonly origin: 'preset' | 'manual'
  readonly matched: number
}

export interface BlacklistRuleListResponse {
  readonly rules: ReadonlyArray<BlacklistRule>
  readonly count: number
}

/** 试算结果：开这条规则之前先看会拉黑多少只。 */
export interface BlacklistRulePreviewResponse {
  readonly rule_id: number
  readonly matched: number
  readonly sample: ReadonlyArray<{ readonly stock_code: string; readonly name: string }>
}

export interface BlacklistRuleHit {
  readonly rule_id: number
  readonly name: string
}

export interface FloatingBlacklistItem {
  readonly stock_code: string
  readonly name: string
  readonly rules: ReadonlyArray<BlacklistRuleHit>
}

export interface FloatingBlacklistRun {
  readonly run_at: string
  readonly rules_evaluated: number
  readonly entered: number
  readonly exited: number
  readonly status: string
}

export interface FloatingBlacklistResponse {
  readonly items: ReadonlyArray<FloatingBlacklistItem>
  readonly count: number
  readonly returned: number
  readonly last_run: FloatingBlacklistRun | null
}

export interface FloatingBlacklistEvent {
  readonly run_at: string
  readonly stock_code: string
  readonly event: 'entered' | 'exited'
  readonly rule_id: number
  readonly rule_name: string | null
  readonly stock_name: string
}

export interface FloatingBlacklistEventsResponse {
  readonly events: ReadonlyArray<FloatingBlacklistEvent>
  readonly count: number
}

/**
 * 屏蔽态的性质。
 *
 * `permanent` 才给「移出黑名单」按钮；`floating` 只解释命中了哪几条规则，
 * 并把人指向规则库——**不给任何手动移出入口**（用户裁定）。
 */
export interface BlacklistCheckResponse {
  readonly stock_code: string
  readonly blacklisted: boolean
  readonly scope: 'permanent' | 'floating' | null
  readonly rules: ReadonlyArray<BlacklistRuleHit>
}
