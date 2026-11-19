/**
 * 字段名中文化兜底（reports/148）。
 *
 * 事故：筛选页「观察指标」下拉显示 `roce_10y_median_i…`（纯英文）。
 *
 * 根因链（两个 agent 分别从渲染路径与数据链路查证）：
 * 1. 方案 C 之后 `/indicators` 不再下发 930 个 `*_rank` 扁平字段；
 * 2. 库里草稿仍存着 `roce_10y_median_industry_rank`；
 * 3. naive-ui `n-select` 在 value 不在 options 里时会合成 `label = String(value)`
 *    的兜底选项（`Select.mjs` 的 wrappedFallbackOptionRef）；
 * 4. 条件行的 `render-label` 只做省略号截断，**不调 `fieldDisplayName()`**
 *    → 于是渲染成英文原值并被截断成 `roce_10y_median_i…`。
 *
 * 修法：
 * ① 条件行下拉加 `fallback-option`，用 `fieldDisplayName()` 生成中文标签；
 * ② 载入规则/草稿时把旧排名字段迁移成 `{field, rank_mode}`；
 * ③ 补齐前端缺失的 `sw1_rank` / `sw1_percentile` 两条后缀标签
 *   （后端 `RANK_SUFFIXES` 有 8 条，前端原先只有 6 条）。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  fieldDisplayName,
  fieldInputUnit,
  fieldOptionLabel,
} from '../src/utils/screening-format.ts'

const CJK = /[\u4e00-\u9fff]/

const RANK_SUFFIXES = [
  'market_rank',
  'market_percentile',
  'industry_rank',
  'industry_percentile',
  'sw1_rank',
  'sw1_percentile',
  'sw2_rank',
  'sw2_percentile',
]

test('fieldDisplayName：8 种排名后缀全部解析成中文', () => {
  for (const suffix of RANK_SUFFIXES) {
    const field = `pe_ttm_${suffix}`
    const label = fieldDisplayName(field)
    assert.match(label, CJK, `${suffix} 没有中文名，界面会显示英文`)
    assert.notEqual(label, field, `${suffix} 回退成了原值`)
    assert.match(label, /市盈率/, `${suffix} 的基名没解析出来`)
  }
})

test('fieldDisplayName：截图里那个字段能解析成中文', () => {
  assert.equal(
    fieldDisplayName('roce_10y_median_industry_rank'),
    '已动用资本回报率（ROCE·10年中位） · 证监会一级排名',
  )
})

test('fieldDisplayName：sw1_* 这两个曾漏掉的后缀不再退回英文', () => {
  assert.match(fieldDisplayName('pe_ttm_sw1_rank'), /申万一级排名/)
  assert.match(fieldDisplayName('pe_ttm_sw1_percentile'), /申万一级分位/)
})

test('fieldDisplayName：未知字段仍回退成原值（由调用方决定是否提示）', () => {
  assert.equal(fieldDisplayName('totally_unknown_field'), 'totally_unknown_field')
})

test('fieldInputUnit：sw1 排名不应报未知单位', () => {
  assert.equal(fieldInputUnit('pe_ttm_sw1_rank').label, '名')
  assert.equal(fieldInputUnit('pe_ttm_sw1_percentile').label, '分位')
})

test('fieldOptionLabel：排名字段带「名」单位', () => {
  const label = fieldOptionLabel(
    'pe_ttm_market_rank',
    fieldDisplayName('pe_ttm_market_rank'),
  )
  assert.match(label, /全市场排名/)
  assert.match(label, /名/)
})
