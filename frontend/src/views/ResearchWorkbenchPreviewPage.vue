<script setup lang="ts">
import { computed, ref } from 'vue'

type Condition = { label: string; operator: string; value: string }

const isStrict = ref(true)
const ran = ref(false)
const activePreset = ref<'value' | 'quality'>('value')
const conditions = ref<Condition[]>([])

const presets = {
  value: [
    { label: '市盈率（PE-TTM）', operator: '小于', value: '15' },
    { label: '净资产收益率（ROE）', operator: '大于等于', value: '15%' },
    { label: '资产负债率', operator: '小于', value: '55%' },
  ],
  quality: [
    { label: '营业收入同比', operator: '大于', value: '10%' },
    { label: '净利润同比', operator: '大于', value: '10%' },
    { label: '销售毛利率', operator: '大于等于', value: '30%' },
  ],
} as const

conditions.value = presets.value.map((condition) => ({ ...condition }))

const stocks = [
  { code: '600519', name: '贵州茅台', industry: '白酒', pe: '19.42', roe: '32.84%', cap: '1.81 万亿', status: '稳健' },
  { code: '000858', name: '五 粮 液', industry: '白酒', pe: '16.88', roe: '21.65%', cap: '4,526 亿', status: '关注' },
  { code: '600900', name: '长江电力', industry: '水电', pe: '18.06', roe: '16.39%', cap: '7,328 亿', status: '稳健' },
  { code: '000333', name: '美的集团', industry: '白电', pe: '13.57', roe: '24.17%', cap: '5,522 亿', status: '关注' },
]

const ruleTitle = computed(() => activePreset.value === 'value' ? '优质价值候选池' : '高质量成长候选池')
const resultCount = computed(() => activePreset.value === 'value' ? 47 : 83)

function usePreset(preset: 'value' | 'quality') {
  activePreset.value = preset
  conditions.value = presets[preset].map((condition) => ({ ...condition }))
  ran.value = false
}
</script>

<template>
  <div class="ramtabs-preview">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">V</span><span>value</span></div>
      <nav aria-label="样稿导航">
        <button class="nav-item active" type="button"><i class="nav-icon icon-grid"></i><span>研究首页</span></button>
        <button class="nav-item" type="button"><i class="nav-icon icon-filter"></i><span>条件筛选</span></button>
        <button class="nav-item" type="button"><i class="nav-icon icon-book"></i><span>自选公司</span></button>
        <button class="nav-item" type="button"><i class="nav-icon icon-chart"></i><span>研究记录</span></button>
      </nav>
      <div class="sidebar-bottom">
        <button class="nav-item" type="button"><i class="nav-icon icon-data"></i><span>数据状态</span></button>
        <button class="profile" type="button"><span class="avatar">Q</span><span><b>研究账户</b><small>本地工作区</small></span><em>•••</em></button>
      </div>
    </aside>

    <main class="dashboard">
      <header class="topbar">
        <div><p class="crumb">研究工作台 / 条件筛选</p><h1>早上好，开始今天的研究</h1></div>
        <div class="top-actions"><button class="round-action" type="button" aria-label="通知">◇</button><button class="help-button" type="button">？</button><span class="header-avatar">Q</span></div>
      </header>

      <section class="hero-card">
        <div class="hero-copy">
          <span class="mini-label">VALUE DASHBOARD</span>
          <h2>从数据开始，<br>建立你的投资判断。</h2>
          <p>用清晰的规则发现候选公司，用可追溯的数据完成长期研究。</p>
          <button class="hero-button" type="button">继续上次研究 <span>→</span></button>
        </div>
        <div class="hero-art" aria-hidden="true"><div class="orbit orbit-one"></div><div class="orbit orbit-two"></div><div class="hero-disc"><span>47</span><small>候选公司</small></div><div class="float-card card-a"><b>+ 12.4%</b><span>盈利增长</span></div><div class="float-card card-b"><b>15.8</b><span>平均市盈率</span></div></div>
      </section>

      <section class="metrics" aria-label="研究数据概览">
        <article><span class="metric-icon icon-filter"></span><div><p>可筛选公司</p><strong>5,533</strong><small>沪深北 A 股</small></div></article>
        <article><span class="metric-icon icon-chart"></span><div><p>已保存规则</p><strong>12</strong><small>本月运行 36 次</small></div></article>
        <article><span class="metric-icon icon-data"></span><div><p>数据状态</p><strong class="green">已就绪</strong><small>截至 2026 年 8 月 3 日</small></div></article>
      </section>

      <section class="content-grid">
        <article class="rule-card">
          <div class="card-heading"><div><span class="mini-label">SCREENING RULE</span><h2>{{ ruleTitle }}</h2></div><button class="ghost-button" type="button">编辑规则</button></div>
          <div class="preset-tabs"><button :class="{ active: activePreset === 'value' }" type="button" @click="usePreset('value')">价值筛选</button><button :class="{ active: activePreset === 'quality' }" type="button" @click="usePreset('quality')">质量成长</button><button type="button" disabled>我的模板</button></div>
          <div class="conditions">
            <div class="condition-label"><span>筛选条件</span><button type="button">+ 添加</button></div>
            <div v-for="(condition, index) in conditions" :key="condition.label" class="condition"><span class="condition-number">0{{ index + 1 }}</span><strong>{{ condition.label }}</strong><span>{{ condition.operator }}</span><b>{{ condition.value }}</b><button type="button" aria-label="删除条件">×</button></div>
          </div>
          <div class="trust-row"><span class="switch" :class="{ on: isStrict }" role="switch" :aria-checked="isStrict" tabindex="0" @click="isStrict = !isStrict" @keydown.enter="isStrict = !isStrict"><i></i></span><div><strong>仅使用严格可信数据</strong><p>排除口径不完整或近似计算的指标。</p></div><a href="#">了解口径</a></div>
        </article>

        <aside class="run-card">
          <div class="run-card-top"><span class="status-dot"></span><span>数据可用于研究</span><button type="button">⋮</button></div>
          <div class="run-number"><strong>{{ resultCount }}</strong><span>预计候选公司</span></div>
          <div class="run-lines"><p><span>股票范围</span><b>沪深北 A 股</b></p><p><span>筛选条件</span><b>{{ conditions.length }} 项</b></p><p><span>排序方式</span><b>市盈率从低到高</b></p><p><span>数据截至</span><b>2026-08-03</b></p></div>
          <button class="run-button" type="button" @click="ran = true">运行筛选 <span>→</span></button>
          <p v-if="ran" class="completed">已完成 · 248 ms · {{ resultCount }} 家符合条件</p>
          <p v-else class="run-note">结果可保存为快照、导出 CSV 或加入自选。</p>
        </aside>
      </section>

      <section class="results-card">
        <div class="card-heading"><div><span class="mini-label">LATEST SCREEN</span><h2>候选公司</h2></div><div class="result-tools"><button class="ghost-button" type="button">配置列</button><button class="export-button" type="button">导出结果</button></div></div>
        <div class="table-summary"><span><b>{{ resultCount }}</b> 家公司符合当前研究规则</span><span>平均市盈率 <b>16.98</b></span><span>平均净资产收益率 <b>23.76%</b></span><span class="strict"><i></i>严格可信</span></div>
        <div class="table-shell"><table><thead><tr><th>公司</th><th>行业</th><th>市盈率（PE-TTM）</th><th>净资产收益率（ROE）</th><th>总市值</th><th>研究信号</th></tr></thead><tbody><tr v-for="stock in stocks" :key="stock.code"><td><strong>{{ stock.name }}</strong><span>{{ stock.code }}</span></td><td>{{ stock.industry }}</td><td>{{ stock.pe }}</td><td>{{ stock.roe }}</td><td>{{ stock.cap }}</td><td><span class="signal" :class="stock.status === '稳健' ? 'signal-good' : 'signal-warn'">{{ stock.status }}</span></td></tr></tbody></table></div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.ramtabs-preview { --ink: #17221c; --muted: #79847e; --green: #197147; --green-deep: #0d5134; --line: #e4e9e5; min-height: 100vh; display: flex; background: #f5f7f5; color: var(--ink); font-family: system-ui, 'Microsoft YaHei', sans-serif; }
.sidebar { position: fixed; inset: 0 auto 0 0; display: flex; flex-direction: column; width: 230px; padding: 28px 17px 18px; box-sizing: border-box; background: #fff; border-right: 1px solid #edf0ee; }.brand { display: flex; align-items: center; gap: 9px; margin: 0 12px 42px; color: #183425; font-size: 20px; font-weight: 750; letter-spacing: -.05em; }.brand-mark { display: grid; width: 28px; height: 28px; place-items: center; border-radius: 9px; background: var(--green-deep); color: #fff; font-size: 14px; }.sidebar nav { display: grid; gap: 5px; }.nav-item { display: flex; align-items: center; gap: 13px; width: 100%; padding: 11px 12px; border: 0; border-radius: 9px; background: transparent; color: #708078; font: inherit; font-size: 13px; text-align: left; cursor: pointer; }.nav-item.active { background: #edf7f0; color: #146941; font-weight: 700; }.nav-item:hover { background: #f4f7f4; color: #255a3d; }.nav-icon, .metric-icon { position: relative; display: inline-block; width: 16px; height: 16px; flex: 0 0 16px; border: 1.6px solid currentColor; border-radius: 4px; box-sizing: border-box; }.icon-grid::before { content: ''; position: absolute; inset: 3px; border: 1px solid currentColor; border-radius: 1px; }.icon-filter { border: 0; border-radius: 0; }.icon-filter::before { content: ''; position: absolute; inset: 2px 1px; border: 1.6px solid currentColor; clip-path: polygon(0 0, 100% 0, 61% 48%, 61% 100%, 40% 82%, 40% 48%); }.icon-book { border-radius: 2px 6px 6px 2px; }.icon-book::before { content: ''; position: absolute; left: 4px; top: 2px; height: 10px; border-left: 1px solid currentColor; }.icon-chart { border: 0; border-radius: 0; border-bottom: 1.6px solid currentColor; border-left: 1.6px solid currentColor; }.icon-chart::after { content: ''; position: absolute; width: 11px; height: 7px; left: 2px; top: 3px; border-top: 1.6px solid currentColor; transform: skewY(-32deg); }.icon-data { border-radius: 50%; }.icon-data::before, .icon-data::after { content: ''; position: absolute; left: 3px; right: 3px; border-top: 1px solid currentColor; }.icon-data::before { top: 5px; }.icon-data::after { top: 9px; }.sidebar-bottom { margin-top: auto; }.profile { display: flex; align-items: center; gap: 9px; width: 100%; margin-top: 12px; padding: 12px; border: 0; border-top: 1px solid var(--line); background: transparent; text-align: left; cursor: pointer; }.avatar, .header-avatar { display: grid; place-items: center; width: 29px; height: 29px; border-radius: 50%; background: #dbece0; color: #126440; font-size: 12px; font-weight: 800; }.profile b, .profile small { display: block; }.profile b { font-size: 11px; }.profile small { margin-top: 2px; color: var(--muted); font-size: 10px; }.profile em { margin-left: auto; color: #8d9891; font-style: normal; }
.dashboard { width: calc(100% - 230px); max-width: 1520px; min-width: 1030px; margin-left: 230px; padding: 34px 48px 58px; box-sizing: border-box; }.topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 29px; }.crumb { margin: 0 0 7px; color: #839087; font-size: 11px; }.topbar h1 { margin: 0; font-size: 23px; letter-spacing: -.04em; }.top-actions { display: flex; align-items: center; gap: 10px; }.round-action, .help-button { display: grid; width: 32px; height: 32px; place-items: center; border: 1px solid #e1e7e2; border-radius: 50%; background: #fff; color: #75837a; cursor: pointer; }.header-avatar { margin-left: 4px; width: 32px; height: 32px; }
.hero-card { position: relative; display: flex; min-height: 238px; overflow: hidden; padding: 36px 42px; border-radius: 20px; background: linear-gradient(111deg, #124b32, #187148); color: #fff; box-shadow: 0 16px 35px rgba(21, 91, 56, .17); box-sizing: border-box; }.hero-copy { position: relative; z-index: 2; }.mini-label { color: #809088; font-size: 9px; font-weight: 800; letter-spacing: .15em; }.hero-card .mini-label { color: #b2d7bf; }.hero-card h2 { margin: 11px 0 10px; font-size: 28px; line-height: 1.15; letter-spacing: -.045em; }.hero-card p { max-width: 405px; margin: 0; color: #d4e9da; font-size: 12px; line-height: 1.65; }.hero-button { margin-top: 21px; padding: 9px 13px 9px 15px; border: 0; border-radius: 8px; background: #fff; color: #125637; font: inherit; font-size: 11px; font-weight: 700; cursor: pointer; }.hero-button span { margin-left: 9px; font-size: 15px; }.hero-art { position: absolute; width: 430px; height: 300px; right: 58px; top: -30px; }.orbit { position: absolute; border: 1px solid rgba(255, 255, 255, .17); border-radius: 50%; }.orbit-one { width: 300px; height: 300px; left: 42px; top: 0; }.orbit-two { width: 220px; height: 220px; left: 82px; top: 40px; }.hero-disc { position: absolute; display: grid; width: 128px; height: 128px; left: 128px; top: 86px; place-items: center; align-content: center; border: 11px solid rgba(255, 255, 255, .15); border-radius: 50%; background: rgba(8, 60, 36, .63); box-shadow: inset 0 0 0 1px rgba(255,255,255,.25); }.hero-disc span { font-size: 32px; font-weight: 750; letter-spacing: -.06em; }.hero-disc small { margin-top: -3px; color: #c3dfcc; font-size: 10px; }.float-card { position: absolute; display: grid; gap: 3px; padding: 12px 15px; border: 1px solid rgba(255,255,255,.25); border-radius: 10px; background: rgba(255,255,255,.12); backdrop-filter: blur(10px); }.float-card b { font-size: 16px; }.float-card span { color: #d3e9da; font-size: 9px; }.card-a { left: 4px; top: 57px; }.card-b { right: 4px; bottom: 44px; }
.metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; margin: 22px 0; }.metrics article { display: flex; align-items: center; gap: 13px; min-height: 82px; padding: 0 19px; border-radius: 13px; background: #fff; box-shadow: 0 3px 14px rgba(34, 53, 42, .045); }.metric-icon { display: grid; width: 34px; height: 34px; place-items: center; border-color: #cce3d5; background: #f2f9f4; color: #197147; }.metrics p, .metrics small { margin: 0; color: #849088; font-size: 10px; }.metrics strong { display: block; margin: 3px 0 1px; font-size: 19px; letter-spacing: -.04em; }.metrics .green { color: #167148; font-size: 16px; }
.content-grid { display: grid; grid-template-columns: minmax(600px, 1.6fr) minmax(280px, .72fr); gap: 21px; }.rule-card, .run-card, .results-card { border-radius: 15px; background: #fff; box-shadow: 0 4px 17px rgba(34, 53, 42, .05); }.rule-card { padding: 26px 28px; }.card-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }.card-heading h2 { margin: 6px 0 0; font-size: 18px; letter-spacing: -.035em; }.ghost-button { padding: 7px 10px; border: 1px solid #e1e7e2; border-radius: 7px; background: #fff; color: #557064; font: inherit; font-size: 11px; cursor: pointer; }.preset-tabs { display: flex; gap: 7px; margin: 23px 0; }.preset-tabs button { padding: 8px 11px; border: 0; border-radius: 7px; background: #f5f7f5; color: #748078; font: inherit; font-size: 11px; cursor: pointer; }.preset-tabs button.active { background: #eaf6ee; color: #136943; font-weight: 700; }.preset-tabs button:disabled { opacity: .45; cursor: not-allowed; }.condition-label { display: flex; justify-content: space-between; margin-bottom: 8px; color: #7a877f; font-size: 11px; }.condition-label button { border: 0; background: transparent; color: #157047; font: inherit; font-size: 11px; cursor: pointer; }.condition { display: grid; grid-template-columns: 31px 1.65fr .72fr .55fr 15px; align-items: center; gap: 9px; min-height: 42px; border-top: 1px solid #edf0ee; font-size: 11px; }.condition-number { color: #9aa49e; font-size: 10px; }.condition strong { font-size: 11px; }.condition > span:not(.condition-number) { color: #77857c; }.condition b { color: #146e47; }.condition button { border: 0; background: transparent; color: #a1aaa4; font-size: 17px; cursor: pointer; }.trust-row { display: flex; align-items: center; gap: 10px; margin-top: 20px; padding-top: 17px; border-top: 1px solid #edf0ee; }.switch { display: block; width: 31px; height: 18px; padding: 2px; border-radius: 20px; background: #cbd3ce; cursor: pointer; box-sizing: border-box; }.switch i { display: block; width: 14px; height: 14px; border-radius: 50%; background: #fff; transition: transform .15s; }.switch.on { background: #1c744b; }.switch.on i { transform: translateX(13px); }.trust-row div { flex: 1; }.trust-row strong { display: block; font-size: 11px; }.trust-row p { margin: 3px 0 0; color: #89948e; font-size: 10px; }.trust-row a { color: #2d7552; font-size: 10px; text-decoration: none; }
.run-card { padding: 23px; background: #153e2b; color: #fff; box-shadow: 0 12px 24px rgba(17, 71, 44, .17); }.run-card-top { display: flex; align-items: center; gap: 7px; color: #c1dbca; font-size: 10px; }.run-card-top button { margin-left: auto; border: 0; background: transparent; color: #b8d4c1; cursor: pointer; }.status-dot, .strict i { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #7dd29b; }.run-number { margin: 22px 0 19px; }.run-number strong { display: block; font-size: 50px; line-height: .9; letter-spacing: -.07em; }.run-number span { display: block; margin-top: 7px; color: #b6d1be; font-size: 11px; }.run-lines { padding: 13px 0; border-top: 1px solid rgba(255,255,255,.14); border-bottom: 1px solid rgba(255,255,255,.14); }.run-lines p { display: flex; justify-content: space-between; margin: 7px 0; color: #b2c9ba; font-size: 10px; }.run-lines b { color: #fff; font-weight: 600; }.run-button { width: 100%; margin-top: 20px; padding: 11px; border: 0; border-radius: 7px; background: #fff; color: #125537; font: inherit; font-size: 11px; font-weight: 750; cursor: pointer; }.run-button span { margin-left: 8px; }.run-note, .completed { margin: 11px 0 0; color: #b4cfbd; font-size: 10px; line-height: 1.5; text-align: center; }.completed { color: #9de4b5; }
.results-card { margin-top: 21px; padding: 26px 28px 28px; }.result-tools { display: flex; gap: 8px; }.export-button { padding: 7px 11px; border: 0; border-radius: 7px; background: #eaf6ee; color: #176d46; font: inherit; font-size: 11px; font-weight: 700; cursor: pointer; }.table-summary { display: flex; gap: 24px; margin: 21px 0 15px; color: #7b8880; font-size: 10px; }.table-summary b { color: #39483f; }.table-summary .strict { margin-left: auto; color: #1b7049; }.strict i { margin-right: 5px; }.table-shell { overflow: hidden; border: 1px solid #edf0ee; border-radius: 9px; } table { width: 100%; border-collapse: collapse; font-size: 11px; font-variant-numeric: tabular-nums; } th { padding: 10px 13px; background: #f8faf8; color: #849088; font-size: 9px; font-weight: 700; text-align: left; white-space: nowrap; } td { padding: 12px 13px; border-top: 1px solid #edf0ee; color: #536158; } td strong { display: block; color: #26342b; font-size: 11px; } td > span:not(.signal) { display: block; margin-top: 2px; color: #9ca69f; font-size: 9px; }.signal { display: inline-block; padding: 3px 6px; border-radius: 5px; font-size: 9px; }.signal-good { background: #eaf6ee; color: #197148; }.signal-warn { background: #faf3e5; color: #a26c11; }
</style>
