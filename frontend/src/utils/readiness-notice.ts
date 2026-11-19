/**
 * 筛选就绪结论的「新鲜度 / 降级」提示（后端字段 readiness_* ，见 reports/140 §13.4 / §15.5）。
 *
 * 为什么需要在前端显示：
 * 后端会告诉我们这次"就绪"判断是怎么来的 ——
 * - `readiness_source = 'stale_cache'`：用的是**过期但有硬上限**的旧结论；
 * - `readiness_degraded.active = true`：指纹对超大表退回了 `count-only`
 *   （只数行数、不看内容），**守卫强度下降**。
 *
 * 这两种情况下"数据已就绪"这句话的可信度是打折的。以前这些信息只在日志里，
 * 用户看到的是一个绿点，**那本身就是假绿的另一种形态**（看起来正常，其实守卫变弱了）。
 * 所以这里把它翻译成人话提示。
 */

/** 后端 `readiness_degraded` 的形状（可能为 null/undefined）。 */
export interface ReadinessDegraded {
  readonly active?: boolean
  readonly count_only_tables?: ReadonlyArray<string>
  readonly reasons?: ReadonlyArray<string>
}

/** 与后端 readiness_* 字段同名的子集，便于直接透传响应体。 */
export interface ReadinessFreshness {
  readonly readiness_source?: string | null
  readonly readiness_stale?: boolean | null
  readonly readiness_age_seconds?: number | null
  readonly readiness_degraded?: ReadinessDegraded | null
}

export type NoticeLevel = 'stale' | 'degraded' | 'stale_degraded'

export interface ReadinessNotice {
  readonly level: NoticeLevel
  readonly text: string
}

/** 把秒数说成人话（只用于提示，不需要精确）。 */
function humanAge(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 90) return `约 ${Math.round(seconds)} 秒前`
  const minutes = Math.round(seconds / 60)
  if (minutes < 90) return `约 ${minutes} 分钟前`
  const hours = Math.round(minutes / 60)
  return `约 ${hours} 小时前`
}

/**
 * 返回需要提示的内容；两种情况都不成立时返回 `null`（不打扰用户）。
 *
 * 判据刻意保守：**只在"确实打折"时提示**，正常的 `cache`/`computed` 一律不提示，
 * 否则每次筛选都弹一条，用户几天内就会无视它。
 */
export function readinessNotice(
  freshness: ReadinessFreshness | null | undefined,
): ReadinessNotice | null {
  if (!freshness) return null

  const stale = freshness.readiness_stale === true
  const degraded = freshness.readiness_degraded?.active === true
  if (!stale && !degraded) return null

  const age = humanAge(freshness.readiness_age_seconds)
  const parts: string[] = []
  if (stale) {
    parts.push(age ? `本次就绪结论来自${age}的缓存（已过期）` : '本次就绪结论来自过期缓存')
  }
  if (degraded) {
    const tables = freshness.readiness_degraded?.count_only_tables ?? []
    const tail = tables.length > 0 ? `（${tables.length} 张超大表只核对了行数）` : ''
    parts.push(`就绪校验已降级${tail}，可能漏掉"就地改动"`)
  }

  const level: NoticeLevel =
    stale && degraded ? 'stale_degraded' : degraded ? 'degraded' : 'stale'

  return { level, text: parts.join('；') + '。必要时请重跑一次数据体检。' }
}
