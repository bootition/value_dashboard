/**
 * 反返工护栏：「轮询定时器」只能有一个出口（2026-11-22 重构）。
 *
 * 背景：`DataStatusPage.vue` 曾有 **3 套**复制粘贴的轮询调度，`ScreeningPage.vue`
 * 与 `App.vue` 各是第 4、5 套：各自 `let timer` + `clearTimeout` + 在卸载钩子里再清一遍
 * （App.vue 用的还是 `setInterval`，慢请求时会叠并发）。
 * 这类代码的 bug 全是静默的（漏清 → 两条轮询链并行、请求翻倍；漏卸载 →
 * after-unmount 请求 + 内存泄漏），且每次"再加一处轮询"都要重新踩一遍。
 * 2026-11-22 已收敛为 `composables/usePolling.ts::usePollTimer()`。
 *
 * 本测试是**静态门禁**（模板：tests/percent-render-single-source.test.ts）：
 * 谁再手写 `setInterval` / `xxxTimer = setTimeout(...)` 轮询立刻变红，逼它改调
 * `usePollTimer()`。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
const srcDir = join(frontendRoot, 'src')

/** 唯一允许直接操控轮询 timer 的文件。 */
const ALLOWED = new Set(['composables/usePolling.ts'])

/**
 * **一次性防抖**（不是轮询）的合法落点：每次触发都清掉旧 timer 再排新的，
 * 只可能跑最后一次，不存在"两条链并行 / 卸载后仍在跑"的风险，
 * 因此不受本门禁约束。逐条写明理由，避免有人顺手把新的**轮询**塞进这个名单。
 */
const DEBOUNCE_ALLOWED = new Set([
  // 筛选草稿自动保存防抖 400ms（清旧 timer 后只落一次 PUT）
  'composables/useScreeningDraft.ts',
  // strict-only 切换后重跑筛选的 400ms 防抖
  'views/ScreeningPage.vue',
  // 股票搜索 250ms 输入防抖
  'views/StockSearchPage.vue',
])

/** `pollTimer = setTimeout(` / `this.t = window.setTimeout(` 这类手写 timer 变量。 */
const TIMER_ASSIGN = /[A-Za-z_$][\w$]*Timer\s*=\s*(window\.)?setTimeout\s*\(/
/** `setInterval` 全局禁止：周期重叠无法单飞。 */
const INTERVAL = /\bsetInterval\s*\(/

/** 去掉整行注释后的代码文本（门禁只看代码，不看重构说明里的旧写法）。 */
function codeOnly(text: string): string {
  const KEEP = (line: string) => !/^\s*(\/\/|\*|\/\*)/.test(line)
  return text.split(/\r?\n/).filter(KEEP).join('\n')
}

function sourceFiles(): string[] {
  return readdirSync(srcDir, { recursive: true, encoding: 'utf-8' })
    .filter((name) => name.endsWith('.ts') || name.endsWith('.vue'))
    .map((name) => name.split('\\').join('/'))
    .sort()
}

test('src 下没有第二处手写轮询 timer 生命周期', () => {
  const violations: string[] = []
  for (const rel of sourceFiles()) {
    if (ALLOWED.has(rel)) continue
    const text = readFileSync(join(srcDir, rel), 'utf-8')
    text.split('\n').forEach((line, index) => {
      // 注释行不算（重构说明里会提到旧写法）
      if (/^\s*(\/\/|\*|\/\*)/.test(line)) return
      if (INTERVAL.test(line)) {
        violations.push(`src/${rel}:${index + 1} [setInterval] ${line.trim()}`)
        return
      }
      if (DEBOUNCE_ALLOWED.has(rel)) return
      if (TIMER_ASSIGN.test(line)) {
        violations.push(`src/${rel}:${index + 1} [timer 变量接 setTimeout] ${line.trim()}`)
      }
    })
  }
  assert.deepEqual(violations, [], `轮询 timer 必须走 composables/usePolling.ts：\n${violations.join('\n')}`)
})

test('usePollTimer 实现了"清旧 + 设新 + 卸载清理"三件事', () => {
  const text = readFileSync(join(srcDir, 'composables/usePolling.ts'), 'utf-8')
  assert.match(text, /function schedule\([\s\S]*?cancel\(\)[\s\S]*?timer = setTimeout/, 'schedule 必须先 cancel 再 setTimeout')
  assert.match(text, /onScopeDispose\(cancel\)/, 'usePollTimer 必须注册 onScopeDispose(cancel)')
})

test('轮询大户（应用外壳 / 数据状态页 / 筛选页）已改为 usePollTimer', () => {
  // 应用外壳：全局自动更新状态轮询必须复用 useAutoUpdatePolling（内部 usePollTimer），
  // 不允许 App.vue 再手写第二套 timer。
  const app = codeOnly(readFileSync(join(srcDir, 'App.vue'), 'utf-8'))
  assert.match(app, /useAutoUpdateRunning\(\)/, 'App.vue 必须复用 useAutoUpdatePolling')
  assert.match(app, /startAutoUpdatePolling\(/, 'App.vue 必须调用 startAutoUpdatePolling')
  assert.ok(!/setInterval/.test(app), 'App.vue 不应再用 setInterval')
  assert.ok(!/clearInterval|clearTimeout/.test(app), 'App.vue 不应再自己清 timer')
  assert.ok(!/setTimeout\s*\(/.test(app), 'App.vue 不应再自己排 timer（交给 usePollTimer）')

  // 数据状态页：三套轮询（自动更新状态 / 指标重算 / 重量摘要）各自一个 timer
  const dataStatus = codeOnly(readFileSync(join(srcDir, 'views/DataStatusPage.vue'), 'utf-8'))
  assert.match(dataStatus, /usePollTimer\(\)/, 'DataStatusPage 必须用 usePollTimer')
  for (const label of ['recomputePoll', 'autoPoll', 'heavyPoll']) {
    assert.ok(dataStatus.includes(`${label}.schedule(`), `DataStatusPage 缺 ${label}.schedule(...)`)
  }
  assert.ok(!/clearTimeout\s*\(/.test(dataStatus), 'DataStatusPage 不应再自己 clearTimeout')

  // 筛选页：自动更新轮询已抽到 useAutoUpdatePolling（内部用 usePollTimer）
  const screening = codeOnly(readFileSync(join(srcDir, 'views/ScreeningPage.vue'), 'utf-8'))
  assert.ok(!/window\.setTimeout\s*\(/.test(screening), 'ScreeningPage 不应再手写 window.setTimeout 轮询')
  assert.match(screening, /startAutoUpdatePolling\(/, 'ScreeningPage 必须调用 startAutoUpdatePolling')
})
