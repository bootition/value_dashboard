/**
 * 研究域只读 API 契约（reports/119 §5 D 类，2026-11-14）
 *
 * 对应后端 `app/web/api/research.py`：
 * - GET /api/research/as-reported?code=600519&as_of=2024-06-30&fields=revenue,parent_net_profit
 * - GET /api/research/visible-periods?code=600519
 * - GET /api/research/announcements?code=600519&days=365&limit=200
 * - GET /api/research/announcements/summary?days=90
 *
 * 口径提醒（不得在 UI 上夸大）：
 * - `value_basis === 'latest_restated'` 恒成立：数值是库内该报告期唯一一份
 *   （今天的最新重述值），**不是**当年披露时的原始数字；可见性只决定「取哪一期」。
 * - `announce_date` 之后才允许使用该期数字（回测对齐）。
 * - `category` 是内部触发分类（financial/dividend/other），不是交易所一级分类。
 */

/** 财务字段白名单内的列名（income_statement / balance_sheet / cash_flow 标准列）。 */
export type ResearchFinancialField = string

/** 报告期类型，与后端 `report_type_of` 同口径。 */
export type ResearchReportType = 'annual' | 'semi_annual' | 'quarterly'

/** `frequency` 查询参数。 */
export type ResearchFrequency = 'any' | 'annual' | 'interim' | 'quarterly'

/** 公告类别，来自 `app.core.update.classify_announcement`。 */
export type AnnouncementCategory = 'financial' | 'dividend' | 'other'

/** 时点可见（as reported）响应：as_of 当天可见的最近一期财务值。 */
export interface AsReportedResponse {
  readonly stock_code: string
  readonly as_of: string
  readonly frequency: ResearchFrequency
  readonly report_date: string
  readonly announce_date: string
  readonly report_type: ResearchReportType
  readonly source: string
  readonly days_since_announce: number
  readonly fields: Readonly<Record<ResearchFinancialField, number | null>>
  readonly missing_fields: readonly ResearchFinancialField[]
  readonly value_tables: readonly string[]
  /** 恒为 'latest_restated'：库内无历史数值版本，不还原当年原始数字。 */
  readonly value_basis: 'latest_restated'
  readonly value_source: string
  readonly disclaimer: string
  readonly limitations: readonly string[]
}

/** 某个报告期「什么日期开始可见」。 */
export interface VisiblePeriodRow {
  readonly report_date: string
  /** 该报告期**最早**登记的公布日；此日之后才允许使用该期数字。 */
  readonly announce_date: string
  readonly report_type: ResearchReportType
  readonly source: string
}

export interface VisiblePeriodsResponse {
  readonly stock_code: string
  readonly frequency: ResearchFrequency
  readonly as_of: string | null
  readonly count: number
  readonly periods: readonly VisiblePeriodRow[]
  readonly usage: string
  readonly limitations: readonly string[]
}

export interface AnnouncementRow {
  readonly announcement_id: string | null
  readonly stock_code: string | null
  readonly title: string | null
  readonly category: AnnouncementCategory
  /** UTC ISO 时间（后端原始口径）。 */
  readonly announcement_time: string
  /** 北京时间自然日 YYYY-MM-DD（UI 展示请用这个，不要截 UTC 字符串）。 */
  readonly announcement_date_cn: string
  readonly announcement_datetime_cn: string
}

export interface AnnouncementsResponse {
  readonly stock_code: string
  readonly days: number
  readonly cutoff_date: string
  readonly timezone: string
  readonly limit: number
  readonly offset: number
  readonly count: number
  /** 整个时间窗的条数（不是当前页）。 */
  readonly total: number
  readonly has_more: boolean
  /** 整个时间窗（不是当前页）按类别计数。 */
  readonly by_category: Readonly<Record<AnnouncementCategory, number>>
  readonly items: readonly AnnouncementRow[]
  readonly category_source: string
  readonly disclaimer: string
}

export interface AnnouncementSummaryResponse {
  readonly days: number
  readonly cutoff_date: string
  readonly timezone: string
  readonly window_total: number
  readonly by_category: Readonly<Record<AnnouncementCategory, number>>
  readonly scanned_rows: number
  readonly scan_limit: number
  readonly truncated: boolean
  readonly stocks: number
  readonly generated_at: string
  readonly cache_ttl_seconds: number
  readonly category_source: string
  readonly disclaimer: string
}

/** 4xx/5xx 的 `detail` 形状：后端一律带 `reason`，便于 UI 区分「没数据」与「参数错」。 */
export interface ResearchErrorDetail {
  readonly reason: string
  readonly message: string
  readonly [key: string]: unknown
}
