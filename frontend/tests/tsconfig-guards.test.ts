/**
 * 前端测试门禁的"防回退"守卫（2026-11-23）。
 *
 * 背景：`reports/140` §17.3/§17.7 记了一笔真实的债 ——
 * 根级 `tests/*.test.ts` 走 `node --experimental-strip-types --test`（**只剥类型不检查**），
 * vitest 只覆盖 `tests/component/**`，`vue-tsc` 只扫 `src/**`，
 * 于是**测试代码从来没被类型检查过**：一个已被删除的入参 `sortField` 在测试里活了两个多月，
 * 还有一条 `stock-detail` fixture 甚至**编造了后端从不返回的字段**（`sw_level1/sw_level2`），
 * 而那个文件的头注释还写着"证明只读接口接受**后端返回的确切形状**"。
 *
 * 后来补了 `tsconfig.test.json` + `typecheck:test`，但第一批跑出来的 22 条历史错误是**被整文件
 * `exclude` 掉**的 —— 那样门禁对那 7 个文件等于不存在。现在 22 条已逐条真修、`exclude` 已删。
 *
 * 这条测试把三件事钉死，防止以后再"用 exclude 藏债"或"悄悄把 tests 移出 lint/test"：
 *   1. `tsconfig.test.json` **不许有 `exclude` 键**（要排除文件必须先改这条测试并写明理由）
 *   2. `lint` 脚本必须同时扫 `src` 与 `tests`
 *   3. `test` 脚本必须串上 `typecheck:test`
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import test from 'node:test'

const here = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(here, '..')

/**
 * 读 JSON / **JSONC**。`tsconfig.test.json` 带注释（`/* ... *\/`），
 * `JSON.parse` 会直接抛 —— 所以先去注释再解析。
 * 只处理本项目实际用到的两种注释形态，够用即可（不要为此引入依赖）。
 */
function readJson(name: string): Record<string, unknown> {
  const raw = readFileSync(path.join(frontendRoot, name), 'utf8')
  const withoutComments = raw
    .replace(/\/\*[\s\S]*?\*\//g, '') // 块注释
    .replace(/^\s*\/\/.*$/gm, '') // 行注释
  return JSON.parse(withoutComments) as Record<string, unknown>
}

test('tsconfig.test.json 不许用 exclude 藏债', () => {
  const tsconfig = readJson('tsconfig.test.json')
  assert.equal(
    Object.hasOwn(tsconfig, 'exclude'),
    false,
    'tsconfig.test.json 又出现了 exclude：请逐条真修类型错误，' +
      '要排除某文件必须在 reports/140 §17 登记理由并改这条测试',
  )
})

test('typecheck:test 覆盖 src 与 tests 两个目录', () => {
  const tsconfig = readJson('tsconfig.test.json')
  const include = tsconfig.include
  assert.ok(Array.isArray(include), 'tsconfig.test.json 必须有 include 数组')
  const text = (include as string[]).join(' ')
  assert.match(text, /src/, `include 必须覆盖 src：${text}`)
  assert.match(text, /tests/, `include 必须覆盖 tests：${text}`)
})

test('lint 必须同时扫描 tests 目录', () => {
  const pkg = readJson('package.json') as { scripts?: Record<string, string> }
  const lint = pkg.scripts?.lint ?? ''
  assert.match(lint, /\bsrc\b/, `lint 脚本必须扫 src：${lint}`)
  assert.match(lint, /\btests\b/, `lint 脚本必须扫 tests（否则测试代码没有 lint 覆盖）：${lint}`)
})

test('npm test 必须带上测试代码的类型检查', () => {
  const pkg = readJson('package.json') as { scripts?: Record<string, string> }
  const testScript = pkg.scripts?.test ?? ''
  assert.match(
    testScript,
    /typecheck:test/,
    `npm test 必须串上 typecheck:test（否则测试代码类型检查会被绕过）：${testScript}`,
  )
  // 原有的两步也不许被悄悄换掉
  assert.match(testScript, /run-unit-tests\.mjs/, `npm test 必须仍走自动扫描 runner：${testScript}`)
  assert.match(testScript, /vitest run/, `npm test 必须仍跑 vitest：${testScript}`)
})
