/**
 * 反返工护栏 R2：「百分数原值渲染」只能有一个出口（reports/140 R2 复发点）。
 *
 * 背景：前端存在两类百分比存储口径（见 `app/core/screening/field_units.py`）：
 * - 小数比例（pct）：0.0529 → 展示 ×100 + '%'
 * - 百分数原值（percent / 分位 / 覆盖率 / ERP）：5.29 → 展示原样 + '%'，**不 ×100**
 *
 * 第二类曾被手写成 `x.toFixed(n) + '%'` 至少 9 处（IndexPage、IndexDetailPage、
 * EtfStrategyPanel、IndexValuationChart、IndustryContributionBars、
 * ResearchStatisticsCard、ScreeningPage、DataStatusPage）。它们彼此独立演化，
 * 任何一次口径调整都会漏改其中几处——2026-11-19 已收敛为
 * `formatters.ts::fmtStoredPercent(value, digits?)` 单一实现。
 *
 * 本测试是**静态门禁**：新增 `xxx.toFixed(n) + '%'` 这类手写会立刻变红，
 * 逼改动者改调 `fmtStoredPercent`。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { fmtFixed, fmtPct, fmtStoredPercent } from '../src/utils/formatters.ts'

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
const srcDir = join(frontendRoot, 'src')

/** 唯一允许出现「原值百分号」拼接的文件。 */
const ALLOWED = new Set(['utils/formatters.ts'])

/** 手写百分比渲染的三种典型形态。 */
const HAND_ROLLED: ReadonlyArray<{ name: string; pattern: RegExp }> = [
  // `${x.toFixed(1)}%` / `x.toFixed(2) + '%'` / `${row.erp.toFixed(2)}%`
  { name: 'toFixed 后直接跟 %', pattern: /\.toFixed\([^()]*\)[^;\n]{0,16}%/ },
  // 旧的自制 fmt(value, digits, '%')
  { name: "本地 fmt 传 '%' 后缀", pattern: /,\s*\d+\s*,\s*['"]%['"]/ },
  // x + '%'
  { name: "字符串拼接 '+'%" , pattern: /\+\s*['"]%['"]/ },
]

function sourceFiles(): string[] {
  return readdirSync(srcDir, { recursive: true, encoding: 'utf-8' })
    .filter((name) => name.endsWith('.ts') || name.endsWith('.vue'))
    .map((name) => name.split('\\').join('/'))
    .filter((name) => !ALLOWED.has(name))
    .sort()
}

test('src 下没有第二处手写「原值百分比」渲染', () => {
  const violations: string[] = []
  for (const rel of sourceFiles()) {
    const text = readFileSync(join(srcDir, rel), 'utf-8')
    text.split('\n').forEach((line, index) => {
      for (const rule of HAND_ROLLED) {
        if (rule.pattern.test(line)) {
          violations.push(`src/${rel}:${index + 1} [${rule.name}] ${line.trim()}`)
        }
      }
    })
  }
  assert.deepEqual(
    violations,
    [],
    `发现手写百分比渲染，请改调 formatters.ts::fmtStoredPercent 或 fmtPct：\n${violations.join('\n')}`,
  )
})

test('fmtStoredPercent 是原值口径（不 ×100），支持自定义小数位', () => {
  assert.equal(fmtStoredPercent(5.29), '5.29%')
  assert.equal(fmtStoredPercent(-1.3435), '-1.34%')
  assert.equal(fmtStoredPercent(0.3598), '0.36%')
  // 分位/覆盖率一律 0–100 原值，常用 0/1 位
  assert.equal(fmtStoredPercent(73.6, 0), '74%')
  assert.equal(fmtStoredPercent(99.94, 1), '99.9%')
  // 缺失值不再渲染成 '—%'
  assert.equal(fmtStoredPercent(null), '—')
  assert.equal(fmtStoredPercent(undefined, 0), '—')
  assert.equal(fmtStoredPercent(Number.NaN), '—')
  assert.equal(fmtStoredPercent(Number.POSITIVE_INFINITY), '—')
})

test('fmtPct 与 fmtStoredPercent 口径互不串味', () => {
  // 小数比例：必须 ×100
  assert.equal(fmtPct(0.0529), '5.29%')
  // 百分数原值：必须不 ×100
  assert.equal(fmtStoredPercent(5.29), '5.29%')
  assert.notEqual(fmtPct(5.29), fmtStoredPercent(5.29))
})

test('fmtFixed 定点小数（不做亿/万换算）并保留缺失值语义', () => {
  assert.equal(fmtFixed(12.345), '12.35')
  assert.equal(fmtFixed(12.345, 3), '12.345')
  assert.equal(fmtFixed(-0.5, 1), '-0.5')
  assert.equal(fmtFixed(12.3456, 0, '%'), '12%')
  assert.equal(fmtFixed(null), '—')
  assert.equal(fmtFixed(undefined, 2), '—')
  assert.equal(fmtFixed(Number.NaN), '—')
})
