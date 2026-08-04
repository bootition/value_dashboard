<script setup lang="ts">
import { computed, ref } from 'vue'

const isStrict = ref(true)
const ran = ref(false)
const selectedRule = ref<'value' | 'quality'>('value')
const selectedNav = ref('筛选')

const rules = {
  value: {
    title: '优质价值候选池',
    description: '低估值 · 高回报 · 财务稳健',
    count: 47,
    conditions: [
      { label: '市盈率（PE-TTM）', operator: '小于', value: '15' },
      { label: '净资产收益率（ROE）', operator: '大于等于', value: '15%' },
      { label: '资产负债率', operator: '小于', value: '55%' },
    ],
  },
  quality: {
    title: '高质量成长候选池',
    description: '营收增长 · 盈利质量 · 经营效率',
    count: 83,
    conditions: [
      { label: '营业收入同比', operator: '大于', value: '10%' },
      { label: '净利润同比', operator: '大于', value: '10%' },
      { label: '销售毛利率', operator: '大于等于', value: '30%' },
    ],
  },
} as const

const activeRule = computed(() => rules[selectedRule.value])
const stocks = [
  { code: '600519', name: '贵州茅台', industry: '白酒', pe: '19.42', roe: '32.84%', signal: '稳健' },
  { code: '000858', name: '五 粮 液', industry: '白酒', pe: '16.88', roe: '21.65%', signal: '关注' },
  { code: '600900', name: '长江电力', industry: '水电', pe: '18.06', roe: '16.39%', signal: '稳健' },
  { code: '000333', name: '美的集团', industry: '白电', pe: '13.57', roe: '24.17%', signal: '关注' },
]

function selectRule(rule: 'value' | 'quality') {
  selectedRule.value = rule
  ran.value = false
}
</script>

<template>
  <div class="screening-preview">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">V</span><span>value</span></div>
      <p class="nav-label">研究工具</p>
      <nav aria-label="主导航">
        <button v-for="item in ['筛选', '自选列表', '个股详情', '数据状态']" :key="item" :class="['nav-item', { active: selectedNav === item }]" type="button" @click="selectedNav = item">
          <i :class="['nav-icon', `icon-${item}`]"></i><span>{{ item }}</span>
        </button>
      </nav>
      <div class="sidebar-foot"><div class="data-pill"><i></i><span>数据已就绪</span></div><p>截至 2026-08-03</p><button class="profile" type="button"><span class="avatar">Q</span><span><b>本地研究账户</b><small>个人工作区</small></span><em>•••</em></button></div>
    </aside>

    <main class="workspace">
      <header class="page-header"><div><p class="crumb">研究工具 / 筛选</p><h1>条件筛选</h1><span>建立研究规则，找到值得进一步阅读的公司。</span></div><button class="new-rule" type="button">+ 新建规则</button></header>

      <section class="screening-layout">
        <article class="rule-card">
          <div class="card-top"><div><p class="section-kicker">CURRENT RULE</p><h2>{{ activeRule.title }}</h2><span>{{ activeRule.description }}</span></div><button class="more-button" type="button">•••</button></div>

          <div class="saved-rules"><span>已保存规则</span><div><button :class="{ selected: selectedRule === 'value' }" type="button" @click="selectRule('value')">优质价值</button><button :class="{ selected: selectedRule === 'quality' }" type="button" @click="selectRule('quality')">质量成长</button><button type="button">查看全部</button></div></div>

          <div class="rule-section pool-section"><div class="section-title"><div><p class="section-number">01</p><h3>基础股票池</h3></div><button type="button">编辑</button></div><div class="pool-options"><span class="selected-option">沪深北 A 股</span><span class="selected-option">排除 ST</span><span class="selected-option">排除停牌</span><span>上市满 1 年</span></div></div>

          <div class="rule-section"><div class="section-title"><div><p class="section-number">02</p><h3>筛选条件</h3></div><button type="button">+ 添加条件</button></div><p class="section-note">以下条件全部满足。中文名称后保留指标缩写，方便研究口径核对。</p><div class="condition-list"><div class="condition-head"><span>指标</span><span>关系</span><span>目标值</span><span></span></div><div v-for="condition in activeRule.conditions" :key="condition.label" class="condition-row"><strong>{{ condition.label }}</strong><span>{{ condition.operator }}</span><b>{{ condition.value }}</b><button type="button" aria-label="删除条件">×</button></div></div></div>

          <div class="rule-section sort-section"><div class="section-title"><div><p class="section-number">03</p><h3>排序方式</h3></div><button type="button">编辑</button></div><p class="sort-value"><span>第一优先级</span><b>市盈率（PE-TTM）从低到高</b></p></div>
        </article>

        <aside class="run-panel"><div class="run-status"><i></i>数据可用于筛选</div><div class="run-name"><p>当前规则</p><h2>{{ activeRule.title }}</h2></div><div class="run-data"><p><span>股票范围</span><b>5,533 家</b></p><p><span>筛选条件</span><b>{{ activeRule.conditions.length }} 项</b></p><p><span>可信度</span><b>{{ isStrict ? '严格可信' : '包含近似值' }}</b></p><p><span>数据截至</span><b>2026-08-03</b></p></div><label class="strict-option"><button :class="['switch', { on: isStrict }]" type="button" :aria-pressed="isStrict" @click="isStrict = !isStrict"><i></i></button><span><b>仅使用严格可信数据</b><small>排除口径不完整的指标</small></span></label><button class="run-button" type="button" @click="ran = true">运行筛选 <span>→</span></button><p v-if="ran" class="run-complete">已完成 · 248 ms · {{ activeRule.count }} 家符合条件</p><p v-else class="run-help">结果可保存、导出或加入自选列表。</p></aside>
      </section>

      <section class="results-card"><div class="results-top"><div><p class="section-kicker">SCREENING RESULTS</p><h2>筛选结果</h2><span>{{ ran ? `${activeRule.count} 家公司符合当前规则，以下展示前 4 家。` : '运行筛选后展示候选公司。' }}</span></div><div><button class="plain-button" type="button">配置列</button><button class="export-button" type="button">导出 CSV</button></div></div><div class="results-meta"><span><b>{{ activeRule.count }}</b> 家符合条件</span><span>平均市盈率 <b>16.98</b></span><span>平均净资产收益率 <b>23.76%</b></span><span class="trusted"><i></i>严格可信</span></div><div class="table-shell"><table><thead><tr><th>公司</th><th>行业</th><th>市盈率（PE-TTM）</th><th>净资产收益率（ROE）</th><th>研究信号</th></tr></thead><tbody><tr v-for="stock in stocks" :key="stock.code"><td><strong>{{ stock.name }}</strong><small>{{ stock.code }}</small></td><td>{{ stock.industry }}</td><td>{{ stock.pe }}</td><td>{{ stock.roe }}</td><td><span :class="['signal', stock.signal === '稳健' ? 'good' : 'watch']">{{ stock.signal }}</span></td></tr></tbody></table></div></section>
    </main>
  </div>
</template>

<style scoped>
.screening-preview { --ink: #24342a; --muted: #849087; --line: #e3e9e4; --green: #4f9b72; --green-strong: #2f7852; --mint: #eaf5ed; display: flex; min-height: 100vh; background: #f7f9f7; color: var(--ink); font-family: system-ui, 'Microsoft YaHei', sans-serif; }.sidebar { position: fixed; inset: 0 auto 0 0; display: flex; flex-direction: column; width: 226px; padding: 27px 17px 18px; box-sizing: border-box; background: #fff; border-right: 1px solid #edf1ee; }.brand { display: flex; align-items: center; gap: 9px; margin: 0 11px 39px; color: #355142; font-size: 20px; font-weight: 750; letter-spacing: -.055em; }.brand-mark { display: grid; width: 28px; height: 28px; place-items: center; border-radius: 9px; background: #a7ceb2; color: #fff; font-size: 14px; }.nav-label { margin: 0 12px 9px; color: #a0aaa3; font-size: 9px; font-weight: 800; letter-spacing: .11em; }.sidebar nav { display: grid; gap: 5px; }.nav-item { display: flex; align-items: center; gap: 13px; width: 100%; padding: 11px 12px; border: 0; border-radius: 9px; background: transparent; color: #77867c; font: inherit; font-size: 13px; text-align: left; cursor: pointer; }.nav-item.active { background: #eff8f1; color: #4a966d; font-weight: 700; }.nav-icon { display: inline-block; width: 15px; height: 15px; border: 1.4px solid currentColor; border-radius: 4px; box-sizing: border-box; }.icon-筛选 { border-radius: 0; border: 0; border-top: 1.5px solid currentColor; transform: skewY(-33deg); }.icon-筛选::after { content: ''; display: block; width: 9px; margin: 5px 0 0 4px; border-top: 1.5px solid currentColor; }.icon-自选列表 { border-radius: 50%; }.icon-自选列表::after { content: '★'; position: relative; left: 2px; top: -2px; font-size: 10px; }.icon-个股详情 { border-radius: 50% 50% 2px 2px; }.icon-数据状态 { border-radius: 50%; }.icon-数据状态::after { content: ''; display: block; width: 7px; height: 7px; margin: 3px; border-left: 1px solid currentColor; border-bottom: 1px solid currentColor; }.sidebar-foot { margin-top: auto; }.data-pill { display: flex; align-items: center; gap: 6px; padding: 9px 10px; border-radius: 8px; background: #f4faf5; color: #5f9975; font-size: 10px; }.data-pill i, .run-status i, .trusted i { width: 6px; height: 6px; border-radius: 50%; background: #82ba94; }.sidebar-foot > p { margin: 6px 10px 13px; color: #98a39b; font-size: 9px; }.profile { display: flex; align-items: center; gap: 8px; width: 100%; padding: 12px 10px 0; border: 0; border-top: 1px solid var(--line); background: transparent; text-align: left; }.avatar { display: grid; width: 29px; height: 29px; place-items: center; border-radius: 50%; background: #dceee0; color: #65a277; font-size: 11px; font-weight: 700; }.profile b, .profile small { display: block; }.profile b { font-size: 10px; }.profile small { margin-top: 2px; color: #98a39b; font-size: 9px; }.profile em { margin-left: auto; color: #a0aaa3; font-style: normal; }
.workspace { width: calc(100% - 226px); min-width: 1030px; max-width: 1480px; margin-left: 226px; padding: 37px 49px 58px; box-sizing: border-box; }.page-header { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 27px; }.crumb { margin: 0 0 8px; color: #97a199; font-size: 10px; }.page-header h1 { margin: 0; font-size: 25px; letter-spacing: -.05em; }.page-header span { display: block; margin-top: 7px; color: #829087; font-size: 12px; }.new-rule { padding: 10px 14px; border: 0; border-radius: 8px; background: #c3dfca; color: #3e7551; font: inherit; font-size: 11px; font-weight: 700; cursor: pointer; }.screening-layout { display: grid; grid-template-columns: minmax(620px, 1.6fr) minmax(282px, .72fr); gap: 21px; }.rule-card, .results-card { border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.rule-card { padding: 28px 29px; }.card-top, .section-title, .results-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }.section-kicker { margin: 0; color: #91a097; font-size: 9px; font-weight: 800; letter-spacing: .13em; }.card-top h2, .results-top h2 { margin: 7px 0 5px; font-size: 19px; letter-spacing: -.04em; }.card-top span, .results-top > div > span { color: #829087; font-size: 11px; }.more-button { border: 0; background: transparent; color: #9aa59d; font-weight: 800; cursor: pointer; }.saved-rules { display: flex; align-items: center; gap: 16px; margin: 25px 0; padding: 13px 14px; border-radius: 9px; background: #fafcf9; }.saved-rules > span { color: #89958c; font-size: 10px; }.saved-rules div { display: flex; gap: 7px; }.saved-rules button { padding: 6px 9px; border: 0; border-radius: 6px; background: transparent; color: #859188; font: inherit; font-size: 10px; cursor: pointer; }.saved-rules button.selected { background: #eaf5ed; color: #4d976c; font-weight: 700; }.rule-section { padding: 21px 0; border-top: 1px solid #edf1ee; }.pool-section { border-top: 0; padding-top: 0; }.section-title > div { display: flex; align-items: baseline; gap: 9px; }.section-number { margin: 0; color: #83b194; font-size: 10px; font-weight: 800; }.section-title h3 { margin: 0; font-size: 13px; }.section-title button { border: 0; background: transparent; color: #67a27a; font: inherit; font-size: 10px; cursor: pointer; }.pool-options { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 14px; }.pool-options span { padding: 6px 9px; border: 1px solid #e3ebe5; border-radius: 99px; color: #7b897f; font-size: 10px; }.pool-options .selected-option { border-color: #d4e9d9; background: #f2f9f3; color: #629776; }.section-note { margin: 9px 0 13px; color: #929d95; font-size: 10px; }.condition-list { overflow: hidden; border: 1px solid #edf1ee; border-radius: 8px; }.condition-head, .condition-row { display: grid; grid-template-columns: 1.7fr .76fr .55fr 18px; align-items: center; gap: 8px; padding: 10px 13px; }.condition-head { background: #fafcfa; color: #9ca69f; font-size: 9px; }.condition-row { min-height: 23px; border-top: 1px solid #eff2f0; font-size: 11px; }.condition-row strong { font-size: 11px; }.condition-row span { color: #839087; }.condition-row b { color: #55956d; }.condition-row button { border: 0; background: transparent; color: #a6afa9; font-size: 16px; cursor: pointer; }.sort-section { padding-bottom: 0; }.sort-value { display: flex; gap: 15px; margin: 13px 0 0; color: #89958c; font-size: 10px; }.sort-value b { color: #4c5d52; font-size: 11px; }.run-panel { align-self: start; padding: 25px; border-radius: 16px; background: #eff7f1; box-shadow: 0 5px 17px rgba(47, 114, 74, .055); }.run-status { display: flex; align-items: center; gap: 6px; color: #659d75; font-size: 10px; }.run-name { margin: 26px 0 19px; }.run-name p { margin: 0 0 6px; color: #8a9b90; font-size: 10px; }.run-name h2 { margin: 0; color: #365944; font-size: 20px; letter-spacing: -.05em; }.run-data { padding: 13px 0; border-top: 1px solid #dbeade; border-bottom: 1px solid #dbeade; }.run-data p { display: flex; justify-content: space-between; margin: 8px 0; color: #809087; font-size: 10px; }.run-data b { color: #4d6556; font-weight: 650; }.strict-option { display: flex; align-items: center; gap: 9px; margin: 18px 0; cursor: pointer; }.switch { width: 31px; height: 18px; padding: 2px; border: 0; border-radius: 99px; background: #bccbc0; cursor: pointer; }.switch i { display: block; width: 14px; height: 14px; border-radius: 50%; background: #fff; transition: transform .15s; }.switch.on { background: #82b895; }.switch.on i { transform: translateX(13px); }.strict-option b, .strict-option small { display: block; }.strict-option b { color: #557160; font-size: 10px; }.strict-option small { margin-top: 3px; color: #8d9b91; font-size: 9px; }.run-button { width: 100%; padding: 11px; border: 0; border-radius: 8px; background: #a8d0b4; color: #3d7250; font: inherit; font-size: 11px; font-weight: 750; cursor: pointer; }.run-button span { margin-left: 8px; }.run-help, .run-complete { margin: 11px 0 0; color: #81998a; font-size: 9px; line-height: 1.5; text-align: center; }.run-complete { color: #5a9670; }.results-card { margin-top: 21px; padding: 27px 29px 29px; }.results-top > div:last-child { display: flex; gap: 8px; }.plain-button, .export-button { padding: 7px 10px; border-radius: 7px; font: inherit; font-size: 10px; cursor: pointer; }.plain-button { border: 1px solid #e2e9e4; background: #fff; color: #718178; }.export-button { border: 0; background: #e7f3ea; color: #5b9670; font-weight: 700; }.results-meta { display: flex; gap: 22px; margin: 21px 0 15px; color: #8b978f; font-size: 10px; }.results-meta b { color: #536359; }.trusted { margin-left: auto; color: #659d75; }.trusted i { display: inline-block; margin-right: 5px; }.table-shell { overflow: hidden; border: 1px solid #edf1ee; border-radius: 8px; } table { width: 100%; border-collapse: collapse; font-size: 11px; font-variant-numeric: tabular-nums; } th { padding: 10px 13px; background: #fafcfa; color: #98a39b; font-size: 9px; font-weight: 700; text-align: left; } td { padding: 12px 13px; border-top: 1px solid #eff2f0; color: #627068; } td strong { display: block; color: #384a3e; font-size: 11px; } td small { display: block; margin-top: 2px; color: #a0aaa2; font-size: 9px; }.signal { display: inline-block; padding: 3px 6px; border-radius: 5px; font-size: 9px; }.signal.good { background: #eaf6ed; color: #58966f; }.signal.watch { background: #faf5e9; color: #ad8d49; }
</style>
