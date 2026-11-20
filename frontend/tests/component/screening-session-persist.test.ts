/**
 * 会话冷备：**只有用户自己刷新**才重置（2026-11-20 第二轮事故）。
 *
 * 上一轮把「切页保状态」做成了模块级单例，前提是"F5 重建 JS 上下文 → 自然回到空态"。
 * 实测证明这个前提把两件不同的事混成了一件：模块级单例只活在**同一份文档**里，
 * 任何一次文档重新加载（刷新、地址栏回车、页面内 `location.reload()`）都会归零，
 * 分不清"切页"还是"刷新"。用户要的是「我不点刷新，界面就保持在那里」。
 *
 * 现在多一层 `sessionStorage` 冷备 + `performance.navigation.type` 判定：
 *   - `reload`      → 清空（用户主动刷新，唯一的重置入口）
 *   - `navigate` / `back_forward` → 回填（新标签页里 storage 是空的，天然是空态）
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const STORAGE_KEY = 'vd.screening.session.v1'

const SNAPSHOT = {
  results: [{
    stock_code: '600519', name: '贵州茅台', exchange: 'SSE', csrc_l1: '制造业',
    latest_close: 1680.5, pe_ttm: 10, pb_mrq: 8.2, roe: 0.3, gross_margin: 0.91,
    net_margin: 0.52, debt_ratio: 0.2, revenue_yoy: 0.15, dividend_yield: 0.023,
  }],
  runId: 'run-abc',
  totalMatched: 1,
  executionTime: 42,
  basePoolSize: 4800,
  dataDate: '2026-09-30',
  truncated: false,
  autoUpdateSnapshot: false,
  snapshotAsOf: null,
  readinessHint: null,
  strictModeWarning: null,
  strictExemptFields: [],
  warningCodes: [],
  qualityStatus: 'available' as const,
  dataReady: true,
  strictOnly: false,
  context: { ruleId: 7, ruleVersion: 3, ruleName: '低估值·行业龙头' },
}

function stubNavigationType(type: 'reload' | 'navigate' | 'back_forward') {
  vi.spyOn(performance, 'getEntriesByType').mockReturnValue(
    [{ type }] as unknown as PerformanceEntryList,
  )
}

describe('screening-session 冷备（刷新才重置）', () => {
  beforeEach(() => {
    vi.resetModules()
    sessionStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('写入快照 = 同时落 sessionStorage（同一份文档内的切页仍走内存单例）', async () => {
    const store = await import('../../src/stores/screening-session.ts')
    store.saveRunSnapshot(SNAPSHOT)
    expect(store.screeningSession.run?.runId).toBe('run-abc')
    const raw = sessionStorage.getItem(STORAGE_KEY)
    expect(raw).toBeTruthy()
    expect(raw!).toContain('run-abc')
  })

  it('文档重新加载（navigation type = navigate）后回填：结果还在', async () => {
    const first = await import('../../src/stores/screening-session.ts')
    first.saveRunSnapshot(SNAPSHOT)
    first.rememberEditorBinding(7, '低估值·行业龙头')

    // ==== 模拟文档重新加载：模块重新求值（内存单例归零） ====
    vi.resetModules()
    stubNavigationType('navigate')
    const reloaded = await import('../../src/stores/screening-session.ts')

    expect(reloaded.screeningSession.run?.runId).toBe('run-abc')
    expect(reloaded.screeningSession.run?.results).toHaveLength(1)
    expect(reloaded.screeningSession.activeRuleId).toBe(7)
    expect(reloaded.screeningSession.ruleName).toBe('低估值·行业龙头')
    // 重新加载后不该继续转圈
    expect(reloaded.screeningSession.running).toBe(false)
  })

  it('用户主动刷新（navigation type = reload）→ 清空，落盘副本也删掉', async () => {
    const first = await import('../../src/stores/screening-session.ts')
    first.saveRunSnapshot(SNAPSHOT)
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeTruthy()

    vi.resetModules()
    stubNavigationType('reload')
    const reloaded = await import('../../src/stores/screening-session.ts')

    expect(reloaded.screeningSession.run).toBeNull()
    expect(reloaded.documentLoadKind()).toBe('reload')
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('新标签页（storage 为空）→ 空态', async () => {
    sessionStorage.clear()
    vi.resetModules()
    stubNavigationType('navigate')
    const fresh = await import('../../src/stores/screening-session.ts')
    expect(fresh.screeningSession.run).toBeNull()
  })

  it('坏数据不炸 setup：解析失败按"没有"处理并清掉', async () => {
    sessionStorage.setItem(STORAGE_KEY, '{not json')
    vi.resetModules()
    stubNavigationType('navigate')
    const store = await import('../../src/stores/screening-session.ts')
    expect(store.screeningSession.run).toBeNull()
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull()
  })
})
