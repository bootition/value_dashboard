/**
 * 用户可操作的错误文案（L0-5：reason code → 中文原因 + 下一步）。
 *
 * 后端错误形态：
 * - 409 + { "reason_code": "minimum_data_not_ready" | "screening_data_quality_not_ready", ... }
 * - 400/404/409 + 英文 detail 字符串（见 app/web/api/*.py）
 * - 网络/超时（ERR_NETWORK）
 */

import { isAxiosError } from 'axios'

export interface ErrorGuidance {
  title: string
  action?: string
}

/** 具体模式优先于通用模式（长字符串在前）。 */
const DETAIL_RULES: ReadonlyArray<{ match: RegExp; title: string; action?: string }> = [
  { match: /saved result rule provenance is missing/, title: '筛选结果缺少规则溯源', action: '请重新运行筛选并保存结果' },
  { match: /screening_data_quality_not_ready/, title: '数据质量不满足筛选要求', action: '请前往「数据状态」页查看警告（warning_codes），修复后再试' },
  { match: /screening_data_not_ready|screening_data_quality_not_ready/, title: '筛选数据尚未就绪', action: '请前往「数据状态」页查看就绪进度，就绪后再试' },
  { match: /minimum_data_not_ready/, title: '基础数据尚未就绪', action: '请前往「数据状态」页查看更新进度，就绪后再试' },
  { match: /draft revision conflict/, title: '筛选草稿已被其他页面更新', action: '请选择「加载服务器草稿」或「保留本地副本」' },
  { match: /concurrent rule save conflict/, title: '规则被其他操作同时修改', action: '请刷新页面后重试保存' },
  { match: /saved rule version not found/, title: '所选规则版本不存在', action: '请重新选择或保存规则' },
  { match: /server screening run not found/, title: '筛选运行记录已失效', action: '请重新运行筛选后再保存' },
  { match: /saved result rule provenance is missing/, title: '筛选结果缺少规则溯源', action: '请重新保存筛选结果' },
  { match: /saved result not found/, title: '保存的筛选结果不存在', action: '可能已被清理，请重新运行并保存' },
  { match: /no results to export/, title: '没有可导出的结果', action: '请先运行筛选并保存结果' },
  { match: /too many results to export/, title: '导出结果数超过上限（10000 行）', action: '请缩小筛选条件后重试' },
  { match: /too many stocks/, title: '股票数量超过上限（10000 只）', action: '请缩小筛选条件后重试' },
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
  { match: /expression not found/, title: '表达式不存在', action: '请刷新后重试' },
  { match: /published expressions are immutable/, title: '已发布表达式不可修改', action: '请新建表达式或创建新版本' },
  { match: /.* not found.*/i, title: '请求的资源不存在', action: '可能已被清理，请刷新页面' },
]

function detailText(e: unknown): string {
  if (isAxiosError(e)) {
    const d = e.response?.data?.detail
    if (typeof d === 'string') return d
    if (d && typeof d === 'object' && 'reason_code' in d) return String((d as { reason_code: unknown }).reason_code)
    return ''
  }
  if (e instanceof Error) return e.message
  return ''
}

export function describeApiError(e: unknown, fallback = '操作失败'): ErrorGuidance {
  if (isAxiosError(e)) {
    const status = e.response?.status
    const detail = detailText(e)
    if (detail) {
      for (const rule of DETAIL_RULES) {
        if (rule.match.test(detail)) return { title: rule.title, action: rule.action }
      }
    }
    if (e.code === 'ERR_NETWORK' || !e.response) {
      return { title: '无法连接服务器', action: '请确认服务已启动（start.bat），然后刷新页面' }
    }
    if (status === 404) return { title: '请求的资源不存在', action: '可能已被清理，请刷新页面' }
    if (status === 409) return { title: '操作冲突，提交失败', action: '目标状态可能已被其他操作改变，请刷新后重试' }
    if (status && status >= 500) return { title: '服务器内部错误', action: '请稍后重试；持续失败请查看服务端日志' }
    if (status === 400) return { title: '请求参数有误', action: '请检查输入后重试' }
    return { title: `请求失败（HTTP ${status ?? '未知'}）`, action: '请刷新后重试' }
  }
  if (e instanceof Error && e.message) return { title: e.message }
  return { title: fallback }
}

/** 单行中文错误（用于 message.error）。 */
export function friendlyErrorMessage(e: unknown, fallback = '操作失败'): string {
  const guidance = describeApiError(e, fallback)
  return guidance.action ? `${guidance.title}；${guidance.action}` : guidance.title
}