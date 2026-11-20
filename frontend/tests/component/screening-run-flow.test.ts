import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { h, nextTick } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import ScreeningPage from '../../src/views/ScreeningPage.vue'

vi.mock('axios', () => {
  const mock = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    isAxiosError: (e: unknown) => (e as { isAxiosError?: boolean })?.isAxiosError === true,
  }
  // 命名导出必须与 default 一起提供，否则组件里 `import { isAxiosError } from 'axios'`
  // 得到 undefined，草稿自动保存 catch 分支会二次抛错（2026-08-14 红队修复）。
  return { default: mock, ...mock }
})

import axios from 'axios'
import type { Mock } from 'vitest'

function makeRouter() {
  return createRouter({ history: createMemoryHistory(), routes: [] })
}

function mountScreeningPage() {
  const router = makeRouter()
  const wrapper = mount(NDialogProvider, {
    slots: {
      default: () =>
        h(NMessageProvider, null, { default: () => h(ScreeningPage) }),
    },
    global: { plugins: [router] },
  })
  return { wrapper, router }
}

describe('ScreeningPage 运行筛选流程（PRD §12 SC8）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const get = axios.get as Mock
    // 草稿自动保存（防抖定时器）可能在测试收尾后触发：put 必须给出 resolved 响应，
    // 否则 persistDraft 进入 catch 造成未处理拒绝（红队 P2-13 同型测试泄漏）。
    ;(axios.put as Mock).mockResolvedValue({ data: { revision: 1 } })
    get.mockImplementation((url: string) => {
      if (url === '/api/screening/draft') {
        return Promise.resolve({ data: { draft: null, revision: 0 } })
      }
      if (url === '/api/screening/rules') {
        return Promise.resolve({
          data: {
            rules: [{
              id: 1, name: 'test-rule', version: 1,
              rule_json: {
                conditions: { logic: 'AND', rules: [{ field: 'pe_ttm', op: '>', value: 0 }] },
                sort: [{ field: 'pe_ttm', direction: 'asc' }],
                columns: ['stock_code', 'pe_ttm'],
              },
              locked_indicators: {}, status: 'saved', created_at: '2026-08-01',
            }],
          },
        })
      }
      if (url === '/api/screening/indicators') {
        return Promise.resolve({
          data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 },
        })
      }
      if (url === '/api/data-status/summary') {
        return Promise.resolve({
          data: { data_quality: { warning_codes: [], minimum_data_readiness: { ready: true } } },
        })
      }
      if (url === '/api/data-status/auto-update') {
        return Promise.resolve({ data: { state: 'enabled', current_stage: 'finished' } })
      }
      return Promise.resolve({ data: {} })
    })
  })

  it('挂载后恢复草稿并加载规则，选择规则后可运行筛选', async () => {
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return Promise.resolve({
          data: {
            results: [{ stock_code: '600519', name: '贵州茅台', pe_ttm: 10 }],
            total: 1, execution_time_ms: 5, base_pool_size: 100,
            data_date: '2026-06-30', run_id: 'run-1',
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()

    expect(axios.get).toHaveBeenCalledWith('/api/screening/draft')
    expect(axios.get).toHaveBeenCalledWith('/api/screening/rules')

    // 选择已保存规则（直接触发 Select 的 update:value → loadRule）
    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    expect(ruleSelect.exists()).toBe(true)
    await ruleSelect.vm.$emit('update:value', 1)
    await flushPromises()
    expect(wrapper.text()).toContain('test-rule v1')

    // 点击"运行筛选"
    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    expect(runButton).toBeTruthy()
    await runButton!.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/screening/run',
      expect.objectContaining({ rule_id: 1, rule_version: 1, min_listing_years: 1 }),
      expect.objectContaining({ timeout: 120_000 }),
    )
    expect(wrapper.text()).toContain('600519')
    expect(wrapper.text()).toContain('贵州茅台')
  })

  it('没有已保存规则时自动保存草稿并直接运行（不强制手动命名）', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/screening/draft') {
        return Promise.resolve({ data: { draft: null, revision: 0 } })
      }
      if (url === '/api/screening/rules') {
        return Promise.resolve({
          data: {
            rules: [{
              id: 42, name: '未命名规则 2026-08-26 12:00', version: 1,
              rule_json: {
                conditions: { logic: 'AND', rules: [] },
                sort: [],
                columns: ['stock_code'],
              },
              locked_indicators: {}, status: 'saved', created_at: '2026-08-26',
            }],
          },
        })
      }
      if (url === '/api/screening/indicators') {
        return Promise.resolve({
          data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 },
        })
      }
      if (url === '/api/data-status/summary') {
        return Promise.resolve({
          data: { data_quality: { warning_codes: [], minimum_data_readiness: { ready: true } } },
        })
      }
      if (url === '/api/data-status/auto-update') {
        return Promise.resolve({ data: { state: 'enabled', current_stage: 'finished' } })
      }
      return Promise.resolve({ data: {} })
    })
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/rules/save') {
        return Promise.resolve({ data: { status: 'ok', rule_id: 42, version: 1 } })
      }
      if (url === '/api/screening/run') {
        return Promise.resolve({
          data: {
            results: [{ stock_code: '600519', name: '贵州茅台', pe_ttm: 10 }],
            total: 1, execution_time_ms: 5, base_pool_size: 100,
            data_date: '2026-06-30', run_id: 'run-auto',
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()

    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    expect(runButton).toBeTruthy()
    await runButton!.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/screening/rules/save',
      expect.objectContaining({ name: expect.stringContaining('未命名规则') }),
    )
    expect(post).toHaveBeenCalledWith(
      '/api/screening/run',
      expect.objectContaining({ rule_id: 42, rule_version: 1 }),
      expect.objectContaining({ timeout: 120_000 }),
    )
    expect(wrapper.text()).toContain('600519')
  })

  it('更新窗口内 ready=false 仍可按快照口径运行并显示标注（reports/79 方案 A）', async () => {
    const get = axios.get as Mock
    get.mockImplementation((url: string) => {
      if (url === '/api/screening/draft') {
        return Promise.resolve({ data: { draft: null, revision: 0 } })
      }
      if (url === '/api/screening/rules') {
        return Promise.resolve({
          data: {
            rules: [{
              id: 1, name: 'test-rule', version: 1,
              rule_json: {
                conditions: { logic: 'AND', rules: [{ field: 'pe_ttm', op: '>', value: 0 }] },
                sort: [{ field: 'pe_ttm', direction: 'asc' }],
                columns: ['stock_code', 'pe_ttm'],
              },
              locked_indicators: {}, status: 'saved', created_at: '2026-08-01',
            }],
          },
        })
      }
      if (url === '/api/screening/indicators') {
        return Promise.resolve({
          data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 },
        })
      }
      if (url === '/api/data-status/summary') {
        return Promise.resolve({
          data: { data_quality: { warning_codes: [], minimum_data_readiness: { ready: false } } },
        })
      }
      if (url === '/api/data-status/auto-update') {
        return Promise.resolve({ data: { state: 'enabled', current_stage: 'running' } })
      }
      return Promise.resolve({ data: {} })
    })
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return Promise.resolve({
          data: {
            results: [{ stock_code: '600519', name: '贵州茅台', pe_ttm: 10 }],
            total: 1, execution_time_ms: 5, base_pool_size: 100,
            data_date: '2026-06-30', run_id: 'run-1',
            auto_update_in_progress: true, data_as_of: '2026-08-11',
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()
    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    await ruleSelect.vm.$emit('update:value', 1)
    await flushPromises()

    // ready=false + 更新运行中：按钮必须可用
    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    expect(runButton).toBeTruthy()
    expect(runButton!.attributes('disabled')).toBeUndefined()
    await runButton!.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/screening/run',
      expect.objectContaining({ rule_id: 1, rule_version: 1 }),
      expect.objectContaining({ timeout: 120_000 }),
    )
    // 快照口径标注可见（截至日期来自 data_as_of）
    expect(wrapper.text()).toContain('2026-08-11')
    expect(wrapper.text()).toContain('快照')
  })

  /**
   * 用户裁定（本轮）：**去掉「仅使用严格可信数据」开关**，口径固定「包含近似数据」。
   *
   * 语义变更说明（不是删用例）：原用例断言的是「打开严格开关 → watch 防抖重跑 →
   * payload strict_only: true → 空结果原因反馈可见（reports/80 P2-4）」。
   * 开关已按裁定移除，于是同一条用例改成断言三件新事实，覆盖面不减反增：
   *   ① 开关/文案彻底不存在（含右侧摘要区那个绿色药丸）；
   *   ② 运行 payload 恒为 `strict_only: false`；
   *   ③ 后端能力保留 —— `/run` 若仍下发 `strict_mode_warning`，页面照常披露
   *      （"信息别藏起来"：删的是选择权，不是告知）。
   */
  it('严格模式开关已移除：payload 恒为 strict_only false，后端告警仍如实披露', async () => {
    const post = axios.post as Mock
    post.mockImplementation((url: string) => {
      if (url === '/api/screening/run') {
        return Promise.resolve({
          data: {
            results: [],
            total: 0,
            execution_time_ms: 5,
            base_pool_size: 100,
            data_date: '2026-06-30',
            run_id: 'run-no-strict',
            // 后端仍支持 strict_only（CLI/API 调用方保留），这里模拟"它照旧会说话"
            strict_mode_warning:
              '严格可信模式无匹配：以下指标没有任何 strict 血缘记录，相应条件永远无法满足 —— pe_ttm',
          },
        })
      }
      return Promise.resolve({ data: {} })
    })

    const { wrapper } = mountScreeningPage()
    await flushPromises()

    // ① 开关整个消失：容器、两个 toggle 文案、任何带「可信」字样的 Switch
    expect(wrapper.find('.screening-strict').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('仅使用严格可信数据')
    expect(wrapper.text()).not.toContain('包含近似可信数据')
    const trustSwitches = wrapper
      .findAllComponents({ name: 'Switch' })
      .filter((c) => c.text().includes('可信'))
    expect(trustSwitches).toHaveLength(0)
    // 口径信息没有一起被藏起来：摘要区是纯文字的「包含近似值」（不是开关）
    expect(wrapper.text()).toContain('包含近似值')

    const ruleSelect = wrapper.findComponent({ name: 'Select' })
    await ruleSelect.vm.$emit('update:value', 1)
    await flushPromises()

    const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
    await runButton!.trigger('click')
    await flushPromises()

    // ② payload 恒 false（旧用例这里断言的是 true）
    expect(post).toHaveBeenCalledWith(
      '/api/screening/run',
      expect.objectContaining({ strict_only: false }),
      expect.objectContaining({ timeout: 120_000 }),
    )
    // ③ 后端仍下发告警时照常展示
    expect(wrapper.text()).toContain('严格可信模式无匹配')
    expect(wrapper.text()).toContain('strict 血缘记录')
  })

  /**
   * 历史草稿迁移：`strict_only` 曾经是页面上的可选口径，草稿库里会残留 `true`。
   * 开关删掉后如果只是"不渲染"，用户会得到一个**看不见、也改不掉**的严格模式
   * （摘要区写"包含近似值"，实际按严格口径跑）。所以载入时必须归一成 false，
   * 并随下一次自动保存把草稿写回 false（自愈），运行请求也必须是 false。
   */
  it('历史草稿残留 strict_only: true → 载入归一成 false（草稿写回与运行 payload 都变 false）', async () => {
    vi.useFakeTimers()
    try {
      const get = axios.get as Mock
      get.mockImplementation((url: string) => {
        if (url === '/api/screening/draft') {
          return Promise.resolve({
            data: {
              draft: {
                conditions: { id: 'root', logic: 'AND', rules: [{ field: 'pe_ttm', op: '>', value: 0 }] },
                base_pool: { exclude_st: true, exclude_suspended: true, min_listing_years: 1 },
                strict_only: true, // ← 历史草稿残留的严格口径
                result_columns: ['stock_code', 'pe_ttm'],
              },
              revision: 9,
            },
          })
        }
        if (url === '/api/screening/rules') {
          return Promise.resolve({
            data: {
              rules: [{
                id: 1, name: 'test-rule', version: 1,
                rule_json: {
                  conditions: { logic: 'AND', rules: [{ field: 'pe_ttm', op: '>', value: 0 }] },
                  columns: ['stock_code', 'pe_ttm'],
                },
                locked_indicators: {}, status: 'saved', created_at: '2026-08-01',
              }],
            },
          })
        }
        if (url === '/api/screening/indicators') {
          return Promise.resolve({
            data: { indicators: [{ name: 'pe_ttm', rankable: true }], count: 1 },
          })
        }
        if (url === '/api/data-status/summary') {
          return Promise.resolve({
            data: { data_quality: { warning_codes: [], minimum_data_readiness: { ready: true } } },
          })
        }
        if (url === '/api/data-status/auto-update') {
          return Promise.resolve({ data: { state: 'enabled', current_stage: 'finished' } })
        }
        return Promise.resolve({ data: {} })
      })
      ;(axios.post as Mock).mockImplementation((url: string) => {
        if (url === '/api/screening/run') {
          return Promise.resolve({
            data: {
              results: [{ stock_code: '600519', name: '贵州茅台', pe_ttm: 10 }],
              total: 1, execution_time_ms: 5, base_pool_size: 100,
              data_date: '2026-06-30', run_id: 'run-migrated',
            },
          })
        }
        return Promise.resolve({ data: {} })
      })

      const { wrapper } = mountScreeningPage()
      await flushPromises()

      // 载入后：摘要区是纯文字的「包含近似值」，没有任何严格模式控件
      expect(wrapper.text()).toContain('包含近似值')
      expect(wrapper.text()).not.toContain('仅使用严格可信数据')
      expect(
        wrapper.findAllComponents({ name: 'Switch' }).filter((c) => c.text().includes('可信')),
      ).toHaveLength(0)

      // 先跑一次：即使草稿里原本是 true，请求也必须是 false（用户裁定：恒包含近似数据）
      const ruleSelect = wrapper.findComponent({ name: 'Select' })
      await ruleSelect.vm.$emit('update:value', 1)
      await flushPromises()
      const runButton = wrapper.findAll('button').find((b) => b.text().includes('运行筛选'))
      await runButton!.trigger('click')
      await flushPromises()

      expect(axios.post).toHaveBeenCalledWith(
        '/api/screening/run',
        expect.objectContaining({ strict_only: false }),
        expect.objectContaining({ timeout: 120_000 }),
      )

      // 草稿自愈：触发一次草稿状态变化（改「结果展示列」）→ 防抖 400ms → PUT
      const columnsSelect = wrapper
        .findAllComponents({ name: 'Select' })
        .find((c) => c.props('multiple') === true)
      expect(columnsSelect, '找不到「结果展示列」多选框').toBeTruthy()
      await columnsSelect!.vm.$emit('update:value', ['stock_code'])
      await nextTick()
      await vi.advanceTimersByTimeAsync(500)
      await flushPromises()

      const draftPuts = (axios.put as Mock).mock.calls
        .filter(([url]) => url === '/api/screening/draft')
      expect(draftPuts.length, '草稿没有写回').toBeGreaterThan(0)
      // 写回必须带上服务端当前 revision（9 来自 GET 草稿），否则会 409：
      // 按 revision 认本实例的写回，不按"第几条"——同文件里更早的用例留下的组件
      // 实例还挂着 400ms 防抖定时器，advance 时会替它们补一次 PUT（revision 0）。
      expect(
        draftPuts.filter(([, body]) => body.revision === 9).length,
        '写回没带上服务端 revision',
      ).toBeGreaterThan(0)
      // 反向用例（R2 纪律）：任何一次草稿写回都不许把 strict_only: true 送回服务器
      expect(draftPuts.every(([, body]) => body.draft.strict_only === false)).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })
})
