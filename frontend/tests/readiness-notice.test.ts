/**
 * 就绪结论「新鲜度/降级」提示的契约（utils/readiness-notice.ts）。
 *
 * 为什么要测：这两个提示是**用户唯一能看见"这次就绪判断打了折"的地方**。
 * 后端字段对、前端不显示 = 等于没有（reports/140 §15.5 的"假绿另一种形态"）。
 * 同时要防两个反向问题：
 *   ① 正常情况**不许**弹提示（每次筛选都弹一条，几天内用户就无视它了）；
 *   ② 字段缺失/脏值**不许**抛错（后端旧版本没这些字段时前端仍要能跑）。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import { readinessNotice } from '../src/utils/readiness-notice.ts'

test('字段缺失或为空时不给提示', () => {
  assert.equal(readinessNotice(null), null)
  assert.equal(readinessNotice(undefined), null)
  assert.equal(readinessNotice({}), null)
})

test('正常来源（缓存命中/刚算出来）不许打扰用户', () => {
  assert.equal(readinessNotice({ readiness_source: 'cache' }), null)
  assert.equal(readinessNotice({ readiness_source: 'computed', readiness_stale: false }), null)
  // degraded 对象存在但 active=false —— 也不提示
  assert.equal(readinessNotice({ readiness_degraded: { active: false } }), null)
})

test('过期缓存：要说明是过期，并尽量给出多久以前', () => {
  const notice = readinessNotice({ readiness_stale: true, readiness_age_seconds: 900 })
  assert.ok(notice)
  assert.equal(notice.level, 'stale')
  assert.match(notice.text, /过期/)
  assert.match(notice.text, /15 分钟前/)
  // 不许把秒数原样糊给用户
  assert.doesNotMatch(notice.text, /900/)
})

test('过期时长非法（负数/NaN/缺失）时不崩、也不瞎说时长', () => {
  for (const age of [undefined, null, Number.NaN, -5]) {
    const notice = readinessNotice({ readiness_stale: true, readiness_age_seconds: age })
    assert.ok(notice, `age=${String(age)} 应仍提示`)
    assert.equal(notice.level, 'stale')
    assert.doesNotMatch(notice.text, /NaN|-\d/)
  }
})

test('降级：要说明降级了、以及有几张超大表只核了行数', () => {
  const notice = readinessNotice({
    readiness_degraded: {
      active: true,
      count_only_tables: ['source_audit', 'price_daily_qfq'],
    },
  })
  assert.ok(notice)
  assert.equal(notice.level, 'degraded')
  assert.match(notice.text, /降级/)
  assert.match(notice.text, /2 张超大表/)
  assert.match(notice.text, /就地改动/)
})

test('过期 + 降级同时成立时，两件事都要说', () => {
  const notice = readinessNotice({
    readiness_stale: true,
    readiness_age_seconds: 3600,
    readiness_degraded: { active: true, count_only_tables: ['a'] },
  })
  assert.ok(notice)
  assert.equal(notice.level, 'stale_degraded')
  assert.match(notice.text, /过期/)
  assert.match(notice.text, /降级/)
  assert.match(notice.text, /1 张超大表/)
})

test('降级但没有给出表清单时，不显示「0 张」', () => {
  const notice = readinessNotice({ readiness_degraded: { active: true } })
  assert.ok(notice)
  assert.doesNotMatch(notice.text, /0 张/)
})
