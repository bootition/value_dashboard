/**
 * K 线查看偏好（period / adjust）的全局持久化。
 *
 * 通过 localStorage 记忆用户在个股研究中的 K 线偏好，跨股票、跨会话生效。
 * 读取时对非法值做安全回退，避免脏数据破坏页面。storage 不可用时静默忽略。
 */

import type { KlinePeriod } from '../types/stock-detail.ts'

export const KLINE_PERIODS = ['day', 'week', 'month'] as const
export type KlineAdjust = 'raw' | 'qfq'
export const KLINE_ADJUSTS = ['raw', 'qfq'] as const
export interface KlineSettings {
  readonly period: KlinePeriod
  readonly adjust: KlineAdjust
}

export const DEFAULT_KLINE_SETTINGS: KlineSettings = {
  period: 'day',
  adjust: 'raw',
}

const STORAGE_KEY = 'vd.stock-detail.kline-settings'

function isKlinePeriod(value: unknown): value is KlinePeriod {
  return (KLINE_PERIODS as readonly unknown[]).includes(value)
}

function isKlineAdjust(value: unknown): value is KlineAdjust {
  return (KLINE_ADJUSTS as readonly unknown[]).includes(value)
}

/**
 * 读取持久化偏好。任何异常（无 storage / 损坏 JSON / 非法值）都回退到默认。
 */
export function loadKlineSettings(
  storage: Pick<Storage, 'getItem'> | null | undefined,
): KlineSettings {
  if (!storage) return { ...DEFAULT_KLINE_SETTINGS }
  try {
    const raw = storage.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULT_KLINE_SETTINGS }
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return { ...DEFAULT_KLINE_SETTINGS }
    const candidate = parsed as Partial<KlineSettings>
    return {
      period: isKlinePeriod(candidate.period) ? candidate.period : DEFAULT_KLINE_SETTINGS.period,
      adjust: isKlineAdjust(candidate.adjust) ? candidate.adjust : DEFAULT_KLINE_SETTINGS.adjust,
    }
  } catch {
    return { ...DEFAULT_KLINE_SETTINGS }
  }
}

/**
 * 写入持久化偏好。写入失败（隐私模式 / 配额）时静默忽略，不影响页面。
 */
export function saveKlineSettings(
  settings: KlineSettings,
  storage: Pick<Storage, 'setItem'> | null | undefined,
): void {
  if (!storage) return
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(settings))
  } catch {
    // localStorage 不可用时静默忽略
  }
}

/**
 * 安全获取页面 localStorage；不可用时返回 null。
 */
export function pageStorage(): Pick<Storage, 'getItem' | 'setItem'> | null {
  try {
    if (typeof window !== 'undefined' && window.localStorage) return window.localStorage
  } catch {
    return null
  }
  return null
}
