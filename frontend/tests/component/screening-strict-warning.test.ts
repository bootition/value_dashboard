/**
 * 严格模式告警：标题必须中性，不能和正文自相矛盾（2026-11-24）。
 *
 * 后端 `build_strict_mode_warning`（`app/core/screening/engine.py`）有三条分支，
 * 其中两条并不是"没有匹配"：
 *   - 只用到自算派生字段 → "空结果与可信度模式无关，是你的条件没命中"
 *   - 源字段都有 strict 血缘但值/报告期对不上 → "切换到包含近似可信数据"
 * 标题却写死成「严格可信模式无匹配」→ 用户看到"标题说无匹配、正文说无关"。
 *
 * 另外后端新增 `strict_exempt_fields`：自算派生字段按设计没有抓取血缘、已豁免
 * 字段级 strict 校验。非空时必须披露，否则用户会以为是自己条件写错了。
 */
import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider } from 'naive-ui'
import { h } from 'vue'
import ScreeningResultsPanel from '../../src/components/ScreeningResultsPanel.vue'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: () => false,
  }
  return { default: mock, ...mock }
})

function mountPanel(props: {
  strictModeWarning?: string
  strictExemptFields?: string[]
}) {
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  return mount(NMessageProvider, {
    slots: {
      default: () =>
        h(ScreeningResultsPanel, {
          results: [],
          // 用户裁定（本轮）：面板不再接收 `strictOnly` —— 严格/近似已不可选，
          // 这条披露只由后端自己下发的信号（warning / strict_exempt_fields）驱动。
          strictModeWarning: props.strictModeWarning,
          strictExemptFields: props.strictExemptFields,
          executionTime: 5,
          basePoolSize: 100,
          dataDate: '2026-06-30',
          warningCodes: [],
          untrustedFields: [],
          qualityStatus: 'available',
          ruleTree: { id: 'r1', logic: 'AND', rules: [] },
          runId: 'run-strict',
          ruleId: 1,
          ruleVersion: 1,
          lockedIndicators: {},
          basePoolConfig: { include_st: false },
        }),
    },
    global: { plugins: [router] },
  })
}

describe('严格模式告警标题与豁免字段披露', () => {
  it('标题中性：正文说"与可信度模式无关"时，标题不再写死「无匹配」', async () => {
    const wrapper = mountPanel({
      strictModeWarning: '严格可信模式无匹配：本次规则只用到本项目自算的派生字段（roce_10y_median），'
        + '它们已按设计豁免字段级 strict 校验；因此这次空结果是筛选条件本身没有命中，与可信度模式无关。',
      strictExemptFields: ['roce_10y_median'],
    })
    await flushPromises()

    const alert = wrapper.find('#strict-mode-alert')
    expect(alert.exists()).toBe(true)
    // 标题是中性词（naive-ui 把 title 渲染在 .n-alert-body__title）
    const title = alert.find('.n-alert-body__title')
    expect(title.exists(), '找不到告警标题节点').toBe(true)
    expect(title.text()).toBe('严格可信模式说明')
    // 标题区域不得再出现"无匹配"（正文允许出现，那是后端原文）
    expect(title.text()).not.toContain('无匹配')

    // 豁免字段必须点名披露 + 明确"不是你的条件写错了"
    const exemptNote = alert.find('.strict-exempt-note')
    expect(exemptNote.exists()).toBe(true)
    expect(exemptNote.text()).toContain('roce_10y_median')
    expect(exemptNote.text()).toContain('派生字段')
    expect(exemptNote.text()).toContain('不是你的条件写错了')
  })

  it('没有豁免字段时不渲染豁免说明（旧后端/无派生字段场景）', async () => {
    const wrapper = mountPanel({
      strictModeWarning: '严格可信模式无匹配：所用源数据字段都有 strict 血缘记录，但当前没有一只股票的快照值与严格记录一致。',
      strictExemptFields: [],
    })
    await flushPromises()

    expect(wrapper.find('#strict-mode-alert').exists()).toBe(true)
    expect(wrapper.find('.strict-exempt-note').exists()).toBe(false)
    expect(wrapper.text()).toContain('严格可信模式说明')
  })

  /**
   * 用户裁定（本轮）：严格模式开关移除后，本面板不再由 `strictOnly` 驱动 ——
   * 但"信息别藏起来"仍然成立：后端只要下发 `strict_exempt_fields`（例如有人用
   * CLI/API 指定了 strict_only），豁免字段就必须照常披露。标题此时不能再冒充
   * 「严格可信模式」，用中性的「字段口径说明」。
   */
  it('只有豁免字段、没有告警时也披露（标题中性：字段口径说明）', async () => {
    const wrapper = mountPanel({ strictExemptFields: ['roce_10y_median'] })
    await flushPromises()

    const alert = wrapper.find('#strict-mode-alert')
    expect(alert.exists()).toBe(true)
    expect(alert.find('.n-alert-body__title').text()).toBe('字段口径说明')
    const exemptNote = alert.find('.strict-exempt-note')
    expect(exemptNote.exists()).toBe(true)
    expect(exemptNote.text()).toContain('roce_10y_median')
    expect(exemptNote.text()).toContain('不是你的条件写错了')
  })

  it('既无告警也无豁免字段时不渲染任何口径披露', async () => {
    const wrapper = mountPanel({})
    await flushPromises()

    expect(wrapper.find('#strict-mode-alert').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('严格可信')
  })
})
