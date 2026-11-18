/**
 * 字段单位口径契约测试（2026-08-14 红队 F3，reports/80 簇 A/B/C）。
 *
 * 用正式库真实样本值钉死两类百分比口径，防止再次漂移：
 * - 小数存储（pct）：dividend_yield=0.0529、net_profit_cagr3=0.3294 →
 *   输入需 ÷100、展示 ×100+%
 * - 百分数存储（percent）：ttm_dividend_yield=5.1724、
 *   div_yield_spread_10y=-1.3435、turnover_rate=0.3598 → 输入原样、展示直接加 %
 *
 * 2026-11-18 retro 追加「前后端单位表**双向**对拍」段落：RG3 只校验后端→前端单向，
 * 前端多出来的死键（`dividend_financing_ratio`）它永远看不见，所以这里直接读后端
 * `app/core/screening/field_units.py` 文本解析 frozenset，与前端运行时集合 `FIELD_UNIT_SETS`
 * 断言**严格相等**——多一个（死键）或少一个（缺登记）都必须红。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  FIELD_UNIT_SETS,
  fieldFormat,
  fieldInputUnit,
  fieldOptionLabel,
  fieldTitleWithUnit,
  formatFieldValue,
  applyIndicatorUnits,
} from '../src/utils/screening-format.ts'

test('百分数存储字段归类为 percent（不 ÷100、不 ×100）', () => {
  assert.equal(fieldFormat('ttm_dividend_yield'), 'percent')
  assert.equal(fieldFormat('div_yield_spread_10y'), 'percent')
  assert.equal(fieldFormat('div_yield_spread_0p25y'), 'percent')
  assert.equal(fieldFormat('div_yield_spread_30y'), 'percent')
  assert.equal(fieldFormat('turnover_rate'), 'percent')
})

test('小数存储字段归类为 pct（含红队 80 簇 A 补齐的 6 字段）', () => {
  for (const field of [
    'dividend_yield', 'roe', 'net_profit_cagr3', 'net_profit_cagr5',
    'deducted_profit_cagr3', 'deducted_profit_cagr5',
    'period_return', 'annualized_volatility', 'max_drawdown',
  ]) {
    assert.equal(fieldFormat(field), 'pct', field)
  }
})

test('倍数/价格/普通字段不受影响', () => {
  assert.equal(fieldFormat('current_ratio'), 'ratio')
  assert.equal(fieldFormat('latest_close'), 'price')
  assert.equal(fieldFormat('pe_ttm'), 'plain')
})

test('百分数存储展示：5.1724 → "5.17%"（正式库 000683 样本）', () => {
  assert.equal(formatFieldValue('ttm_dividend_yield', 5.1724), '5.17%')
  assert.equal(formatFieldValue('div_yield_spread_10y', -1.3435), '-1.34%')
  assert.equal(formatFieldValue('turnover_rate', 0.3598), '0.36%')
})

test('小数存储展示：0.3294 → "32.94%"（正式库 300750 样本）', () => {
  assert.equal(formatFieldValue('net_profit_cagr3', 0.3294), '32.94%')
  assert.equal(formatFieldValue('dividend_yield', 0.0529), '5.29%')
})

test('null 与区间外值安全', () => {
  assert.equal(formatFieldValue('ttm_dividend_yield', null), '—')
  assert.equal(formatFieldValue('ttm_dividend_yield', 'x'), 'x')
})

test('applyIndicatorUnits：后端 unit 元数据优先级最高（单一来源）', () => {
  applyIndicatorUnits({ ttm_dividend_yield: 'pct', custom_field: 'percent' })
  assert.equal(fieldFormat('ttm_dividend_yield'), 'pct')
  assert.equal(fieldFormat('custom_field'), 'percent')
  // 清除运行时覆盖，避免影响同进程后续用例
  applyIndicatorUnits({ ttm_dividend_yield: 'percent', custom_field: 'plain' })
  assert.equal(fieldFormat('ttm_dividend_yield'), 'percent')
})

test('fieldInputUnit：金额类指标以亿为单位并做换算（1000 = 1000亿）', () => {
  const unit = fieldInputUnit('total_market_cap')
  assert.equal(unit.label, '亿')
  assert.equal(unit.scale, 1e8)
  assert.equal(fieldInputUnit('circ_market_cap').label, '亿')
  assert.equal(fieldInputUnit('interest_bearing_debt').label, '亿')
  assert.equal(fieldInputUnit('income.revenue').label, '亿')
})

test('fieldInputUnit：估值/倍数/价格/年份等指标都有明确单位', () => {
  assert.equal(fieldInputUnit('pe_ttm').label, '倍')
  assert.equal(fieldInputUnit('pb_mrq').label, '倍')
  assert.equal(fieldInputUnit('current_ratio').label, '倍')
  assert.equal(fieldInputUnit('latest_close').label, '元')
  assert.equal(fieldInputUnit('dps').label, '元')
  assert.equal(fieldInputUnit('consecutive_div_years').label, '年')
  assert.equal(fieldInputUnit('avg_volume').label, '万股')
})

test('fieldInputUnit：百分比两类口径均显示 %，但换算 scale 不同', () => {
  const pct = fieldInputUnit('dividend_yield')
  assert.equal(pct.label, '%')
  assert.equal(pct.scale, 0.01)
  const percent = fieldInputUnit('ttm_dividend_yield')
  assert.equal(percent.label, '%')
  assert.equal(percent.scale, 1)
})

test('fieldInputUnit：排名/统计派生字段与下拉框显示单位', () => {
  assert.equal(fieldInputUnit('pe_ttm_market_rank').label, '名')
  assert.equal(fieldInputUnit('pe_ttm_market_percentile').label, '分位')
  assert.equal(fieldInputUnit('pe_ttm_stat_1y_percentile').label, '分位')
  assert.equal(fieldInputUnit('pe_ttm_stat_1y_zscore').label, 'σ')
  assert.equal(fieldOptionLabel('total_market_cap', '总市值'), '总市值(亿)')
  assert.equal(fieldOptionLabel('dividend_yield', '股息率'), '股息率(%)')
})

test('fieldTitleWithUnit：筛选项/表头也带统一单位', () => {
  assert.equal(fieldTitleWithUnit('total_market_cap', '总市值'), '总市值(亿)')
  assert.equal(fieldTitleWithUnit('pe_ttm', 'PE'), 'PE(倍)')
  assert.equal(fieldTitleWithUnit('dividend_yield', '股息率'), '股息率(%)')
})

test('fieldInputUnit：标准化财务表中的监管率也按百分数识别', () => {
  assert.equal(fieldFormat('balance.capital_adequacy_ratio'), 'percent')
  assert.equal(fieldInputUnit('balance.capital_adequacy_ratio').label, '%')
  assert.equal(formatFieldValue('balance.capital_adequacy_ratio', 12), '12.00%')
  assert.equal(formatFieldValue('balance.non_performing_loan_ratio', 1.5), '1.50%')
})

// ─── 前后端单位表双向对拍（2026-11-18 retro） ──────────────────────────────
//
// 事故：`PERCENT_FIELDS` 里挂着一个后端根本不存在的键 `dividend_financing_ratio`
// （后端唯一权威是 `dividend_financing_ratio_pct`：field_units.py 的
// PERCENT_STORED_FIELDS、schema.py 的列名、dsl/ast_nodes.py 的 AST 登记）。
// 门禁 RG3 只做「后端有 → 前端必须有」的单向校验，死键永远不触发它。
// 下面这条测试补反方向：**前端集合必须与后端注册表完全相等**。

/** 仓库根相对路径：frontend/tests/ → ../../ = 仓库根。 */
const BACKEND_UNITS_PATH = fileURLToPath(
  new URL('../../app/core/screening/field_units.py', import.meta.url),
)

/** 口径类别 → 后端 frozenset 名。 */
const BACKEND_SETS = {
  pct: 'PCT_DECIMAL_FIELDS',
  percent: 'PERCENT_STORED_FIELDS',
  ratio: 'RATIO_FIELDS',
  price: 'PRICE_FIELDS',
} as const

type UnitKind = keyof typeof BACKEND_SETS

/**
 * 允许「只在前端登记」的字段（口径类别 → 字段名）。
 *
 * 目前为空：前后端单位表必须逐字段相等。要加条目必须写明后端依据，否则就是
 * 给漂移开后门。
 */
const FRONTEND_ONLY_BY_DESIGN: Readonly<Record<UnitKind, readonly string[]>> = {
  pct: [],
  percent: [],
  ratio: [],
  price: [],
}

const backendUnitsSource = readFileSync(BACKEND_UNITS_PATH, 'utf-8')

/** 从后端源码解析 `<NAME>: frozenset[str] = frozenset({...})` 里的字段名。 */
function parseBackendUnitSet(source: string, name: string): Set<string> {
  const header = `${name}: frozenset[str] = frozenset({`
  const start = source.indexOf(header)
  assert.ok(start >= 0, `后端 field_units.py 里找不到集合 ${name}（改名后必须同步本测试）`)
  const bodyStart = start + header.length
  const bodyEnd = source.indexOf('})', bodyStart)
  assert.ok(bodyEnd > bodyStart, `后端集合 ${name} 的花括号未闭合`)
  const body = source
    .slice(bodyStart, bodyEnd)
    .split('\n')
    .map((line) => line.split('#')[0]) // 行尾注释里的名字不算登记
    .join('\n')
  return new Set([...body.matchAll(/"([^"]+)"/g)].map((item) => item[1]))
}

function missingFromSection(left: Iterable<string>, right: Iterable<string>): string[] {
  const rightSet = new Set(right)
  return [...left].filter((item) => !rightSet.has(item)).sort()
}

for (const kind of Object.keys(BACKEND_SETS) as UnitKind[]) {
  const backendName = BACKEND_SETS[kind]
  test(`单位表双向对拍：前端 ${kind} 集合 == 后端 ${backendName}`, () => {
    const backend = parseBackendUnitSet(backendUnitsSource, backendName)
    const frontend = new Set(FIELD_UNIT_SETS[kind])

    const frontendOnly = missingFromSection(frontend, backend).filter(
      (field) => !FRONTEND_ONLY_BY_DESIGN[kind].includes(field),
    )
    assert.deepEqual(
      frontendOnly,
      [],
      `前端 ${kind} 集合有后端 ${backendName} 不存在的死键（后端是唯一权威）：${frontendOnly.join(', ')}`,
    )

    const backendOnly = missingFromSection(backend, frontend)
    assert.deepEqual(
      backendOnly,
      [],
      `后端 ${backendName} 有字段前端 ${kind} 集合未登记（会退回 plain / 漏 ×100）：${backendOnly.join(', ')}`,
    )
  })
}

test('单位表双向对拍：前端四类集合互不重叠（一个字段只能有一个口径）', () => {
  const seen = new Map<string, UnitKind>()
  const conflicts: string[] = []
  for (const kind of Object.keys(BACKEND_SETS) as UnitKind[]) {
    for (const field of FIELD_UNIT_SETS[kind]) {
      const previous = seen.get(field)
      if (previous && previous !== kind) conflicts.push(`${field}: ${previous} + ${kind}`)
      else seen.set(field, kind)
    }
  }
  assert.deepEqual(conflicts, [], `前端同一字段被登记进多个口径：${conflicts.join('; ')}`)
})

test('单位表双向对拍：后端四类集合互不重叠', () => {
  const seen = new Map<string, string>()
  const conflicts: string[] = []
  for (const backendName of Object.values(BACKEND_SETS)) {
    for (const field of parseBackendUnitSet(backendUnitsSource, backendName)) {
      const previous = seen.get(field)
      if (previous && previous !== backendName) conflicts.push(`${field}: ${previous} + ${backendName}`)
      else seen.set(field, backendName)
    }
  }
  assert.deepEqual(conflicts, [], `后端同一字段被登记进多个口径：${conflicts.join('; ')}`)
})

test('单位表双向对拍：静态集合真的驱动 fieldFormat（文本与行为不许分叉）', () => {
  for (const kind of Object.keys(BACKEND_SETS) as UnitKind[]) {
    for (const field of FIELD_UNIT_SETS[kind]) {
      assert.equal(fieldFormat(field), kind, `${field} 应在 ${kind} 集合里被识别为 ${kind}`)
    }
  }
})

test('事故钉死：分红融资比的后端权威名只有 dividend_financing_ratio_pct', () => {
  // 裸名 `dividend_financing_ratio` 不是后端快照列：它只是历史发布型 DSL 指标名
  // （app/web/api/screening.py:719），单位由 /api/screening/indicators 运行时下发。
  assert.equal(
    FIELD_UNIT_SETS.percent.has('dividend_financing_ratio'),
    false,
    'dividend_financing_ratio 不是后端列，不得静态登记为 percent（2026-11-18 死键）',
  )
  assert.equal(FIELD_UNIT_SETS.percent.has('dividend_financing_ratio_pct'), true)
  assert.equal(fieldFormat('dividend_financing_ratio_pct'), 'percent')
  assert.equal(fieldInputUnit('dividend_financing_ratio_pct').scale, 1)
  assert.equal(formatFieldValue('dividend_financing_ratio_pct', 5.29), '5.29%')
})

test('监管率集合由 PERCENT_FIELDS 派生，不存在第二份手抄', () => {
  const balanceColumns = [...FIELD_UNIT_SETS.percent]
    .filter((field) => field.startsWith('balance.'))
    .map((field) => field.slice('balance.'.length))
    .sort()
  assert.deepEqual(balanceColumns, [
    'capital_adequacy_ratio',
    'core_tier1_capital_adequacy_ratio',
    'non_performing_loan_ratio',
    'provision_coverage_ratio',
    'risk_coverage_ratio',
    'tier1_capital_adequacy_ratio',
  ])
  for (const column of balanceColumns) {
    const unit = fieldInputUnit(`balance.${column}`)
    assert.equal(unit.label, '%', `balance.${column} 应按百分数输入`)
    assert.equal(unit.scale, 1, `balance.${column} 存储即百分数，输入不应 ÷100`)
  }
})

test('百分比展示只有一处实现：小数比例 ×100、百分数原值不 ×100', () => {
  assert.equal(formatFieldValue('roe', 0.0529), '5.29%')
  assert.equal(formatFieldValue('turnover_rate', 5.29), '5.29%')
  assert.equal(formatFieldValue('dividend_financing_ratio_pct', 5.29), '5.29%')
})
