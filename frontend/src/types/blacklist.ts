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

/** `GET /api/blacklist/check/{code}`。详情页进入时先问一句。 */
export interface BlacklistCheckResponse {
  readonly stock_code: string
  readonly blacklisted: boolean
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
