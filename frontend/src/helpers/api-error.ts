/**
 * 用户可操作的错误文案（L0-5：reason code → 中文原因 + 下一步）。
 *
 * 后端错误形态：
 * - 409 + **结构化对象** detail：
 *   `{ reason_code, message?, warning_codes[], readiness:{missing_counts, …},
 *      masked_fields[] }`（见 `app/web/api/screening.py`、`engine.MaskedFieldError.as_detail`）
 * - 400/404/409 + 英文 detail 字符串（见 app/web/api/*.py）
 * - 网络/超时（ERR_NETWORK）
 *
 * 2026-11-23（用户实测「为什么无法筛选？」）：
 * 旧实现 `detailText()` 只做 `if ('reason_code' in d) return reason_code`，把后端
 * 已经准备好的 `message` / `warning_codes` / `readiness.missing_counts` /
 * `masked_fields` **全部丢掉**，用户于是永久只看到一句泛化的
 * 「数据质量不满足筛选要求；请前往「数据状态」页查看警告（warning_codes）」——
 * 既不知道错在哪，也不知道该修什么，还得自己去看一个英文枚举名。
 *
 * 现在 detail 一律按结构体读（`describeDetail`），并且**用户可见文案里不允许
 * 出现**：`warning_codes` / `reason_code` / snake_case 字段名 / `strict` / JSON 片段。
 * 告警码走 `warningCodeLabel()`、字段名走 `fieldDisplayName()`，缺数计数翻成
 * 「缺 X 只股票的 Y」。翻译表都在本文件外（单一来源），这里不复制词表。
 */

import { isAxiosError } from 'axios'
import { warningCodeLabel } from '../types/data-quality.ts'
import { fieldDisplayName } from '../utils/screening-format.ts'

/** 一句话原因 + 下一步（+ 已中文化的证据条目）。 */
export interface ErrorGuidance {
  title: string
  action?: string
  /**
   * 具体证据：哪几条告警、缺多少只股票的什么、哪些字段被遮蔽。
   * 可选，且**逐条都是中文**（无裸枚举、无 snake_case）。
   */
  details?: readonly string[]
}

interface GuidanceText {
  title: string
  action?: string
}

interface DetailRule extends GuidanceText {
  match: RegExp
}

/** 具体模式优先于通用模式（长字符串在前）。 */
const DETAIL_RULES: ReadonlyArray<DetailRule> = [
  { match: /auto_update_in_progress|duckdb_read_locked/, title: '数据正在自动更新中', action: '请稍候片刻；更新完成后刷新页面即可恢复' },
  { match: /正在自动更新|数据库正在更新/, title: '数据正在自动更新中', action: '请稍候片刻；更新完成后刷新页面即可恢复' },
  { match: /saved result rule provenance is missing/, title: '筛选结果缺少规则溯源', action: '请重新运行筛选并保存结果' },
  { match: /screening_data_quality_not_ready/, title: '数据质量不满足筛选要求', action: '请前往「数据状态」页按告警提示修复后再试' },
  { match: /screening_data_not_ready/, title: '筛选数据尚未就绪', action: '请前往「数据状态」页查看就绪进度，就绪后再试' },
  { match: /minimum_data_not_ready/, title: '基础数据尚未就绪', action: '请前往「数据状态」页查看更新进度，就绪后再试' },
  { match: /snapshot_values_untrusted/, title: '当前指标快照整体不可信，筛选已拒绝执行', action: '请到「数据状态」页修复阻断级数据质量告警后再试（刷新页面不会恢复）' },
  { match: /masked_untrusted/, title: '规则引用了当前不可信的字段，筛选已拒绝执行', action: '请到「数据状态」页修复对应告警后再试，或先移除条件里对这些字段的引用（刷新页面不会恢复）' },
  { match: /rule_has_provenance_refs/, title: '该规则已被引用，不能删除', action: '请先删除引用它的筛选结果或自选记录（刷新页面不会解除引用）' },
  { match: /draft revision conflict|draft_revision_conflict/, title: '筛选草稿已被其他页面更新', action: '请选择「加载服务器草稿」或「保留本地副本」' },
  { match: /concurrent rule save conflict|concurrent_rule_save_conflict/, title: '规则被其他操作同时修改', action: '请刷新页面后重试保存' },
  { match: /saved rule version not found/, title: '所选规则版本不存在', action: '请重新选择或保存规则' },
  { match: /server screening run not found/, title: '筛选运行记录已失效', action: '请重新运行筛选后再保存' },
  { match: /saved result lacks report-date provenance/, title: '筛选结果缺少报告期溯源', action: '请重新运行筛选后再保存' },
  { match: /saved result not found/, title: '保存的筛选结果不存在', action: '可能已被清理，请重新运行并保存' },
  { match: /no results to export/, title: '没有可导出的结果', action: '请先运行筛选并保存结果' },
  { match: /too many results to export/, title: '导出结果数超过上限（10000 行）', action: '请缩小筛选条件后重试' },
  { match: /too many stocks/, title: '股票数量超过上限（10000 只）', action: '请缩小筛选条件后重试' },
  { match: /blacklisted stock codes cannot be added/, title: '黑名单股票不能加入自选', action: '请从结果里移除这些股票后再加入自选' },
  { match: /a saved screening result is required/, title: '请先保存筛选结果', action: '点击「保存结果」后再加入自选' },
  { match: /stock codes must come from the saved result/, title: '股票代码必须来自已保存的筛选结果', action: '请重新运行并保存结果后重试' },
  { match: /stock code is not in the source result/, title: '该股票不在来源筛选结果中', action: '请从筛选结果页加入自选' },
  { match: /source rule does not match source result/, title: '来源规则与结果不匹配', action: '请重新保存筛选结果' },
  { match: /source result is required/, title: '缺少来源筛选结果', action: '请先从筛选结果保存再加入自选' },
  { match: /stock code not found in universe/, title: '该股票代码不在股票列表中', action: '请检查代码是否为 6 位且已上市' },
  { match: /invalid stock code/, title: '股票代码格式不正确', action: '请输入 6 位数字代码（如 600519）' },
  { match: /stock not found/, title: '股票不存在', action: '请检查代码，或从筛选/自选列表进入' },
  { match: /no indicator data/, title: '该股票暂无指标数据', action: '可能为新上市或数据源暂无覆盖' },
  { match: /title is required/, title: '请填写标题', action: '保存前先为筛选结果命名' },
  { match: /rule JSON is too large/, title: '规则内容过大', action: '请精简筛选条件后重试' },
  { match: /draft payload is too large/, title: '草稿内容过大', action: '请精简筛选条件后再保存草稿' },
  { match: /expression not found/, title: '表达式不存在', action: '请刷新后重试' },
  { match: /published expressions are immutable/, title: '已发布表达式不可修改', action: '请新建表达式或创建新版本' },
  { match: /.* not found.*/i, title: '请求的资源不存在', action: '可能已被清理，请刷新页面' },
]

/**
 * reason_code 精确映射（后端下发的码 → 准确文案 + **刷新能/不能解决**）。
 *
 * 为什么必须有这张表（审计实证）：走 `DETAIL_RULES` 的正则只认英文句子
 * （`draft revision conflict`），而 reason_code 是下划线写法
 * （`draft_revision_conflict`）——两者对不上，于是**所有**未知码都掉进 409 兜底
 * 「操作冲突，请刷新后重试」。对 `masked_untrusted` / `snapshot_values_untrusted`
 * / `rule_has_provenance_refs` 这三条来说，刷新**永远无效**（它描述的是数据
 * 质量与引用关系，不是并发竞争），教用户刷新等于把人送去无效操作。
 */
const REASON_CODE_RULES: Readonly<Record<string, GuidanceText>> = {
  duckdb_read_locked: { title: '数据正在自动更新中', action: '请稍候片刻；更新完成后刷新页面即可恢复' },
  auto_update_in_progress: { title: '数据正在自动更新中', action: '请稍候片刻；更新完成后刷新页面即可恢复' },
  screening_data_quality_not_ready: { title: '数据质量不满足筛选要求', action: '请前往「数据状态」页按告警提示修复后再试' },
  screening_data_not_ready: { title: '筛选数据尚未就绪', action: '请前往「数据状态」页查看就绪进度，就绪后再试' },
  minimum_data_not_ready: { title: '基础数据尚未就绪', action: '请前往「数据状态」页查看更新进度，就绪后再试' },
  snapshot_values_untrusted: { title: '当前指标快照整体不可信，筛选已拒绝执行', action: '请到「数据状态」页修复阻断级数据质量告警后再试（刷新页面不会恢复）' },
  masked_untrusted: { title: '规则引用了当前不可信的字段，筛选已拒绝执行', action: '请到「数据状态」页修复对应告警后再试，或先移除条件里对这些字段的引用（刷新页面不会恢复）' },
  rule_has_provenance_refs: { title: '该规则已被引用，不能删除', action: '请先删除引用它的筛选结果或自选记录（刷新页面不会解除引用）' },
  draft_revision_conflict: { title: '筛选草稿已被其他页面更新', action: '请选择「加载服务器草稿」或「保留本地副本」' },
  concurrent_rule_save_conflict: { title: '规则被其他操作同时修改', action: '请刷新页面后重试保存' },
}

/** 大小写不敏感副本：reason_code 的英文短语与 detail 原文常只差大小写。 */
const DETAIL_RULES_CI: ReadonlyArray<DetailRule> = DETAIL_RULES.map((rule) => ({
  ...rule,
  match: new RegExp(rule.match.source, rule.match.flags.includes('i') ? rule.match.flags : `${rule.match.flags}i`),
}))

/**
 * `readiness.missing_counts` 的键 → 中文（键是后端内部列名，直接显示等于把
 * snake_case 甩给用户）。键集合来自 `app/core/data_quality.py` 的 `issues`
 * 字典；未登记的键走 `fieldDisplayName()`，再不行就归到「其他数据项」，
 * 保证**任何情况下**都不会把裸字段名显示出来。
 */
const READINESS_ITEM_LABELS: Readonly<Record<string, string>> = {
  pool_metadata: '基础信息（行业/指数归属）',
  raw_history: '不复权历史行情',
  qfq_history: '前复权历史行情',
  price_freshness: '最新收盘价',
  meaningful_volume: '有效成交量',
  financial_period: '完整财务报告期',
  snapshot_price_coherence: '快照价格与行情的一致性',
  share_capital: '股本数据',
  corporate_action_dividend_lineage: '除权/分红溯源',
  sector_financials: '行业财务数据',
  regulatory_fields: '监管指标字段',
  snapshot_input_freshness: '快照输入新鲜度',
  lineage_coverage: '数据溯源覆盖',
  snapshot_period_alignment: '快照报告期对齐',
  pending_financial_period: '尚在披露中的报告期',
  schema_compatibility: '库结构与当前代码的兼容性',
}

/** 结构化后的 detail（已中文化；`rawTexts` 只用于匹配，不给用户看）。 */
export interface ApiErrorDetail {
  /** 后端自带的中文主句（已清洗；空串 = 后端没给） */
  message: string
  /** 后端下发的 reason_code（可能为空） */
  reasonCode: string
  /** 仅用于查映射表的原文（英文 detail / 原始 message） */
  rawTexts: readonly string[]
  /** 已中文化的证据条目（告警名、缺数计数、遮蔽字段） */
  extras: readonly string[]
}

/** 机器标识符：带 `_` / `.` 分隔（`pe_ttm`、`income.revenue`）——必是字段名。 */
const MACHINE_TOKEN = /[A-Za-z][A-Za-z0-9]*(?:[._][A-Za-z0-9]+)+/g
/** 单个英文单词：`roe` 这种没有下划线的字段名同样不能原样给用户看。 */
const BARE_TOKEN = /[A-Za-z][A-Za-z0-9]*/g
/** 给用户看等于废话的机器词。 */
const MACHINE_WORDS = /\b(?:strict_only|strict|warning_codes|warning_code|reason_codes|reason_code)\b/gi
const CJK = /[\u4e00-\u9fff]/

function hasCjk(text: string): boolean {
  return CJK.test(text)
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim())
    .filter(Boolean)
}

/**
 * 把内嵌的裸字段名换成中文名。
 *
 * 两遍是有意为之：带分隔符的标识符（`pe_ttm`）**必是**字段名，查不到中文就
 * 换成「该字段」，绝不原样回显；单个英文单词只在确实是登记过的字段时才翻译
 * （否则 `api` / `summary` 这类普通词会被误伤）。
 */
function humanizeFieldTokens(text: string): string {
  const named = text.replace(MACHINE_TOKEN, (token) => {
    const label = fieldDisplayName(token, '')
    return label && label !== token ? label : '该字段'
  })
  return named.replace(BARE_TOKEN, (token) => {
    const label = fieldDisplayName(token, '')
    return label && label !== token ? label : token
  })
}

/** 去掉机器词、接口路径与 JSON 残片，并收拾替换后的标点。 */
function tidyText(text: string): string {
  let out = stripApiPaths(text.replace(MACHINE_WORDS, ''))
  out = out.replace(/[{}[\]]/g, '')
  out = out.trim()
  // 残留：成串标点、空括号、标点前的空格。
  out = out.replace(/[ \t]+/g, ' ')
  out = out.replace(/([、，；：,;:])[、，；：,;:]+/g, '$1')
  out = out.replace(/[（(]\s*[)）]/g, '')
  out = out.replace(/\s+([、，。；：)）])/g, '$1')
  out = out.replace(/([（(])\s+/g, '$1')
  out = out.replace(/[、，；：]\s*([。；])/g, '$1')
  // 中文之间不留空格（字段名被替换后原文的空格会留在句中）。
  out = out.replace(/([一-鿿]) +(?=[一-鿿])/gu, '$1')
  return out.trim()
}

/** `（见 /api/data-status/summary 的 asset_health）` 这类给开发者看的路径括号整段删掉。 */
function stripApiPaths(text: string): string {
  return text.replace(/[（(][^（()）]*\/[A-Za-z0-9._-]+\/[^（()）]*[)）]/g, '')
}

/** 后端下发的中文主句：先删接口路径（否则路径片段会被当成字段名），再中文化。 */
function sanitizeMessage(text: string): string {
  return tidyText(humanizeFieldTokens(stripApiPaths(text)))
}

/** 告警码 → 中文名；未登记的码只报数量，绝不把 `SNAPSHOT_STALE` 这种裸枚举给用户。 */
function warningLabels(codes: readonly string[]): string {
  const labels: string[] = []
  let unknown = 0
  for (const code of codes) {
    const label = warningCodeLabel(code)
    if (label && label !== code) {
      if (!labels.includes(label)) labels.push(label)
    } else {
      unknown += 1
    }
  }
  if (unknown > 0) labels.push(`${unknown} 条尚未登记文案的告警`)
  return labels.join('、')
}

/** `readiness.missing_counts` → 「缺 X 只股票的 Y」（最多列 3 条，其余报总数）。 */
function missingCountsText(readiness: unknown): string {
  if (!readiness || typeof readiness !== 'object') return ''
  const counts = (readiness as { missing_counts?: unknown }).missing_counts
  if (!counts || typeof counts !== 'object') return ''
  const entries = Object.entries(counts as Record<string, unknown>)
    .filter((entry): entry is [string, number] => typeof entry[1] === 'number' && entry[1] > 0)
    .sort((a, b) => b[1] - a[1])
  if (entries.length === 0) return ''
  const parts = entries.slice(0, 3).map(([key, count]) => `缺 ${count} 只股票的${readinessItemLabel(key)}`)
  const rest = entries.length - 3
  if (rest > 0) parts.push(`另有 ${rest} 类缺口`)
  return `数据缺口：${parts.join('；')}`
}

function readinessItemLabel(key: string): string {
  const known = READINESS_ITEM_LABELS[key]
  if (known) return known
  const mapped = fieldDisplayName(key, '')
  return mapped && mapped !== key && !mapped.includes('_') ? mapped : '其他数据项'
}

/** 从结构化 detail 里抽出**给用户看**的证据条目（`message` 已点名的内容不重复列）。 */
function detailExtras(detail: Record<string, unknown>, message: string): string[] {
  const extras: string[] = []
  const codes = asStringList(detail.warning_codes)
  if (codes.length > 0) {
    const text = warningLabels(codes)
    if (text) extras.push(`数据质量告警：${text}`)
  }
  const masked = asStringList(detail.masked_fields)
  if (masked.length > 0) {
    const names = masked.map((field) => {
      const label = fieldDisplayName(field, '')
      return label && label !== field ? label : '该字段'
    })
    // 后端 message 已经把字段名逐个点出来时，别再重复列一遍。
    if (!names.every((name) => message.includes(name))) {
      extras.push(`被数据质量规则遮蔽的字段：${names.join('、')}`)
    }
  }
  const missing = missingCountsText(detail.readiness)
  if (missing) extras.push(missing)
  return extras
}

/**
 * 读后端 detail —— **取代**原来那个只返回一个字符串的 `detailText()`。
 *
 * 后端 409 的 detail 是对象（`message` / `warning_codes` / `readiness` /
 * `masked_fields`），原来只取 `reason_code` 一个字段，其余全部丢弃，用户因此
 * 拿不到任何可操作信息。这里一次性把结构读成「中文主句 + 匹配原文 + 中文证据」。
 */
export function describeDetail(e: unknown): ApiErrorDetail {
  const empty: ApiErrorDetail = { message: '', reasonCode: '', rawTexts: [], extras: [] }
  if (!isAxiosError(e)) return empty
  const data = e.response?.data as { detail?: unknown } | undefined
  const detail = data?.detail
  if (typeof detail === 'string') {
    const text = detail.trim()
    return { ...empty, rawTexts: text ? [text] : [] }
  }
  if (!detail || typeof detail !== 'object') return empty
  const d = detail as Record<string, unknown>
  const rawMessage = typeof d.message === 'string' ? d.message.trim() : ''
  const reasonCode = typeof d.reason_code === 'string'
    ? d.reason_code.trim()
    : typeof d.reason === 'string' ? d.reason.trim() : ''
  const rawTexts = [reasonCode, rawMessage].filter(Boolean)
  const message = rawMessage ? sanitizeMessage(rawMessage) : ''
  return {
    message,
    reasonCode,
    rawTexts,
    extras: detailExtras(d, message),
  }
}

/** 按「精确 reason_code → 现有中文映射表」的优先级找文案（找不到返回 undefined）。 */
function matchDetail(detail: ApiErrorDetail): GuidanceText | undefined {
  if (detail.reasonCode) {
    const exact = REASON_CODE_RULES[detail.reasonCode]
    if (exact) return exact
  }
  for (const text of detail.rawTexts) {
    for (const rule of DETAIL_RULES_CI) {
      if (rule.match.test(text)) return rule
    }
  }
  // reason_code 是英文短语的 snake_case 写法：换成空格再匹配一次，避免
  // `draft_revision_conflict` 这类码因为下划线而对不上正则。
  if (detail.reasonCode) {
    const spaced = detail.reasonCode.replace(/_/g, ' ')
    for (const rule of DETAIL_RULES_CI) {
      if (rule.match.test(spaced)) return rule
    }
  }
  return undefined
}

function statusFallback(status: number | undefined): ErrorGuidance {
  if (status === 404) return { title: '请求的资源不存在', action: '可能已被清理，请刷新页面' }
  if (status === 409) {
    // 未知冲突码：刷新**不一定**有用（数据质量/引用关系类冲突刷新永远无效），
    // 所以这里不再承诺「刷新后重试」，只给能真正推进的动作。
    return { title: '操作冲突，提交失败', action: '请确认当前条件与数据状态后重试；若反复出现，请联系维护者查看服务端日志' }
  }
  if (status === 413) return { title: '提交的内容过大', action: '请精简内容后重试' }
  if (status && status >= 500) return { title: '服务器内部错误', action: '请稍后重试；持续失败请查看服务端日志' }
  if (status === 400) return { title: '请求参数有误', action: '请检查输入后重试' }
  return { title: `请求失败（HTTP ${status ?? '未知'}）`, action: '请刷新后重试' }
}

export function describeApiError(e: unknown, fallback = '操作失败'): ErrorGuidance {
  if (!isAxiosError(e)) {
    if (e instanceof Error && e.message) return { title: e.message }
    return { title: fallback }
  }
  const status = e.response?.status
  const detail = describeDetail(e)
  const matched = matchDetail(detail)
  const extras = detail.extras
  if (detail.message) {
    // 优先级 1：后端已经写好的中文主句（行动建议仍取映射表，二者互补）。
    return {
      title: detail.message,
      action: matched?.action,
      ...(extras.length > 0 ? { details: extras } : {}),
    }
  }
  if (matched) {
    // 优先级 2：reason_code / detail 原文的现有中文映射。
    return {
      title: matched.title,
      action: matched.action,
      ...(extras.length > 0 ? { details: extras } : {}),
    }
  }
  if (e.code === 'ERR_NETWORK' || !e.response) {
    return { title: '无法连接服务器', action: '请确认服务已启动（start.bat），然后刷新页面' }
  }
  // 优先级 3：HTTP 状态兜底 + 已有的中文证据（有证据就照常附上）。
  return { ...statusFallback(status), ...(extras.length > 0 ? { details: extras } : {}) }
}

/** 单行中文错误（用于 message.error）。 */
export function friendlyErrorMessage(e: unknown, fallback = '操作失败'): string {
  const guidance = describeApiError(e, fallback)
  const parts = [guidance.title]
  if (guidance.action) parts.push(guidance.action)
  if (guidance.details) parts.push(...guidance.details)
  return parts.map((part) => part.replace(/[。；;，,]+$/u, '')).filter(Boolean).join('；')
}
/**
 * 请求失败文案（400 校验消息 / 409 结构化 detail / 其余统一映射）。
 *
 * 2026-11-23 修 bug（审计实证）：这里原本对 400 **无条件** `return detail.trim()`，
 * 于是 `saved rule version not found` / `rule JSON is too large` /
 * `title is required` 这些**映射表里本来就有中文**的英文串被原样甩给用户。
 * 现在顺序固定为：映射表 → （仅当后端原文已是中文）原文 → 中文兜底，
 * 英文原文永远不再直接出现在界面上。
 *
 * 2026-11-22：原为 `views/ScreeningPage.vue` 内的私有函数，随规则 CRUD 抽到
 * `composables/useScreeningRules.ts` 后成为两处共用，故上收到本文件（单一来源）。
 */
export function apiErrorText(e: unknown, fallback: string): string {
  if (isAxiosError(e)) {
    const detail = describeDetail(e)
    const mapped = detail.message || matchDetail(detail)
    if (!mapped) {
      // 没有结构化内容、也没有映射命中时，只有「后端本来就是中文」的校验消息
      // （如「min_listing_years 必须在 0-100 之间」）配得上原文展示，且内嵌的
      // 裸字段名要先换中文。
      const raw = detail.rawTexts.find((text) => hasCjk(text))
      if (raw) return tidyText(humanizeFieldTokens(stripApiPaths(raw)))
    }
  }
  return friendlyErrorMessage(e, fallback)
}
