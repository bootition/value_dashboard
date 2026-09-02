import { describe, it, expect, beforeEach } from 'vitest'
import {
  loadKlineSettings,
  saveKlineSettings,
  DEFAULT_KLINE_SETTINGS,
  pageStorage,
} from '../../src/utils/kline-settings.ts'

const KEY = 'vd.stock-detail.kline-settings'

describe('kline-settings localStorage 持久化', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('无存档时返回默认偏好', () => {
    expect(loadKlineSettings(localStorage)).toEqual(DEFAULT_KLINE_SETTINGS)
  })

  it('合法存档被完整还原', () => {
    saveKlineSettings({ period: 'week', adjust: 'qfq' }, localStorage)
    expect(loadKlineSettings(localStorage)).toEqual({ period: 'week', adjust: 'qfq' })
  })

  it('非法值整体回退默认', () => {
    localStorage.setItem(KEY, JSON.stringify({ period: 'invalid', adjust: 'bad' }))
    expect(loadKlineSettings(localStorage)).toEqual(DEFAULT_KLINE_SETTINGS)
  })

  it('损坏 JSON 安全回退默认', () => {
    localStorage.setItem(KEY, '{ broken json')
    expect(loadKlineSettings(localStorage)).toEqual(DEFAULT_KLINE_SETTINGS)
  })

  it('部分存档只回退非法字段，保留合法字段', () => {
    localStorage.setItem(KEY, JSON.stringify({ period: 'month' }))
    expect(loadKlineSettings(localStorage)).toEqual({ period: 'month', adjust: 'raw' })
  })

  it('storage 不可用时读写均安全降级', () => {
    expect(loadKlineSettings(null)).toEqual(DEFAULT_KLINE_SETTINGS)
    expect(loadKlineSettings(undefined)).toEqual(DEFAULT_KLINE_SETTINGS)
    expect(() => saveKlineSettings(DEFAULT_KLINE_SETTINGS, null)).not.toThrow()
  })

  it('pageStorage 返回浏览器 localStorage', () => {
    expect(pageStorage()).toBe(localStorage)
  })
})
