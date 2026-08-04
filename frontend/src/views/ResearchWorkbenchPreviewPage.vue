<script setup lang="ts">
import { computed, ref } from 'vue'
import { NButton, NTag } from 'naive-ui'

type Condition = {
  field: string
  label: string
  operator: string
  value: string
}

const isStrict = ref(true)
const ran = ref(false)
const activePreset = ref<'value' | 'quality'>('value')
const conditions = ref<Condition[]>([
  { field: 'pe_ttm', label: '市盈率（PE-TTM）', operator: '小于', value: '15' },
  { field: 'roe', label: '净资产收益率（ROE）', operator: '大于等于', value: '15%' },
  { field: 'debt_ratio', label: '资产负债率', operator: '小于', value: '55%' },
])

const presets = {
  value: [
    { field: 'pe_ttm', label: '市盈率（PE-TTM）', operator: '小于', value: '15' },
    { field: 'roe', label: '净资产收益率（ROE）', operator: '大于等于', value: '15%' },
    { field: 'debt_ratio', label: '资产负债率', operator: '小于', value: '55%' },
  ],
  quality: [
    { field: 'revenue_yoy', label: '营业收入同比', operator: '大于', value: '10%' },
    { field: 'net_profit_yoy', label: '净利润同比', operator: '大于', value: '10%' },
    { field: 'gross_margin', label: '销售毛利率', operator: '大于等于', value: '30%' },
  ],
} as const

const stocks = [
  { code: '600519', name: '贵州茅台', industry: '白酒', pe: '19.42', pb: '7.21', roe: '32.84%', debt: '18.71%', signal: '稳健' },
  { code: '000858', name: '五 粮 液', industry: '白酒', pe: '16.88', pb: '3.46', roe: '21.65%', debt: '27.10%', signal: '关注' },
  { code: '600900', name: '长江电力', industry: '水电', pe: '18.06', pb: '3.01', roe: '16.39%', debt: '61.42%', signal: '稳健' },
  { code: '000333', name: '美的集团', industry: '白电', pe: '13.57', pb: '3.02', roe: '24.17%', debt: '64.29%', signal: '关注' },
]

const resultCount = computed(() => activePreset.value === 'value' ? 47 : 83)
const summaryText = computed(() => activePreset.value === 'value' ? '低估值 · 高回报 · 财务稳健' : '成长质量 · 盈利能力 · 经营效率')

function usePreset(preset: 'value' | 'quality') {
  activePreset.value = preset
  conditions.value = presets[preset].map((condition) => ({ ...condition }))
  ran.value = false
}

function runPreview() {
  ran.value = true
}
</script>

<template>
  <main class="workbench-preview">
    <section class="preview-heading">
      <div>
        <div class="eyebrow">DESIGN STUDY · SCREENING WORKBENCH</div>
        <h1>价值筛选工作台</h1>
        <p>面向长期研究的桌面筛选界面样稿。此页面使用本地演示数据，不读取或写入正式数据。</p>
      </div>
      <n-tag size="small" type="info" round>视觉样稿</n-tag>
    </section>

    <section class="workbench-shell" aria-label="筛选工作台样稿">
      <div class="editor-column">
        <div class="section-header">
          <div>
            <span class="step-mark">01</span>
            <h2>建立研究规则</h2>
          </div>
          <span class="muted">草稿已自动保存</span>
        </div>

        <div class="rule-name-row">
          <div class="rule-name">
            <span>规则名称</span>
            <strong>{{ activePreset === 'value' ? '优质价值候选池' : '高质量成长候选池' }}</strong>
          </div>
          <button class="text-button" type="button">版本 3 · 已保存</button>
        </div>

        <div class="preset-row" aria-label="规则预设">
          <button :class="['preset-button', { active: activePreset === 'value' }]" type="button" @click="usePreset('value')">
            价值筛选
          </button>
          <button :class="['preset-button', { active: activePreset === 'quality' }]" type="button" @click="usePreset('quality')">
            质量成长
          </button>
          <button class="preset-button" type="button" disabled>我的模板</button>
        </div>

        <div class="pool-panel">
          <div class="panel-title-row">
            <div>
              <span class="step-mark">02</span>
              <h2>确定股票范围</h2>
            </div>
            <span class="pool-count">5,533 家上市公司</span>
          </div>
          <div class="filter-chips">
            <button class="filter-chip is-selected" type="button">沪深北 A 股</button>
            <button class="filter-chip is-selected" type="button">排除 ST</button>
            <button class="filter-chip is-selected" type="button">排除停牌</button>
            <button class="filter-chip" type="button">上市满 1 年</button>
          </div>
        </div>

        <div class="conditions-panel">
          <div class="panel-title-row">
            <div>
              <span class="step-mark">03</span>
              <h2>添加筛选条件</h2>
            </div>
            <button class="text-button" type="button">+ 添加条件</button>
          </div>
          <p class="helper-text">全部条件同时满足。指标名采用中文优先，并保留常用缩写以便核对口径。</p>
          <div class="condition-table" role="table" aria-label="当前筛选条件">
            <div class="condition-header" role="row">
              <span>指标</span><span>关系</span><span>目标值</span><span></span>
            </div>
            <div v-for="condition in conditions" :key="condition.field" class="condition-row" role="row">
              <strong>{{ condition.label }}</strong>
              <span>{{ condition.operator }}</span>
              <b>{{ condition.value }}</b>
              <button type="button" :aria-label="`删除${condition.label}`">×</button>
            </div>
          </div>
        </div>

        <div class="quality-row">
          <div>
            <strong>数据可信度</strong>
            <span>{{ isStrict ? '只显示各项指标均严格可信的公司' : '允许显示近似可信的数据' }}</span>
          </div>
          <button :class="['toggle', { on: isStrict }]" type="button" :aria-pressed="isStrict" @click="isStrict = !isStrict">
            <i></i>
          </button>
        </div>
      </div>

      <aside class="run-column" aria-label="运行摘要">
        <div class="run-label">当前研究视图</div>
        <h2>{{ activePreset === 'value' ? '优质价值候选池' : '高质量成长候选池' }}</h2>
        <p>{{ summaryText }}</p>

        <div class="data-date">
          <span>数据截至</span>
          <strong>2026 年 8 月 3 日</strong>
          <small>价格与财务数据已就绪</small>
        </div>

        <div class="summary-grid">
          <div><span>股票范围</span><strong>5,533</strong></div>
          <div><span>筛选条件</span><strong>{{ conditions.length }}</strong></div>
          <div><span>排序方式</span><strong>市盈率</strong></div>
          <div><span>可信模式</span><strong>{{ isStrict ? '严格' : '完整' }}</strong></div>
        </div>

        <n-button type="primary" block size="large" @click="runPreview">运行筛选</n-button>
        <p class="run-hint">预计耗时不足 1 秒。运行结果可保存、导出或加入自选。</p>

        <div v-if="ran" class="run-feedback" role="status">
          <span>已完成</span>
          <strong>发现 {{ resultCount }} 家候选公司</strong>
          <small>耗时 248 ms · 严格可信</small>
        </div>

        <div class="trust-note">
          <span class="trust-dot"></span>
          <p><strong>研究提示</strong> 结果仅用于研究比较，不构成投资建议。</p>
        </div>
      </aside>
    </section>

    <section class="results-section">
      <div class="results-heading">
        <div>
          <div class="eyebrow">SCREENING RESULT</div>
          <h2>候选公司</h2>
          <p>{{ ran ? `符合当前规则的 ${resultCount} 家公司，以下展示前 4 家。` : '运行筛选后，这里将展示符合条件的公司。' }}</p>
        </div>
        <div class="result-actions">
          <button class="text-button" type="button">配置列</button>
          <button class="secondary-button" type="button">导出结果</button>
        </div>
      </div>

      <div class="result-summary">
        <div><span>平均市盈率</span><strong>16.98</strong></div>
        <div><span>平均净资产收益率</span><strong>23.76%</strong></div>
        <div><span>行业覆盖</span><strong>12 个</strong></div>
        <div><span>数据状态</span><strong class="ready-text">严格可信</strong></div>
      </div>

      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>股票</th><th>行业</th><th>市盈率（PE-TTM）</th><th>市净率（PB）</th><th>净资产收益率（ROE）</th><th>资产负债率</th><th>研究信号</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="stock in stocks" :key="stock.code">
              <td><strong>{{ stock.name }}</strong><span>{{ stock.code }}</span></td>
              <td>{{ stock.industry }}</td><td>{{ stock.pe }}</td><td>{{ stock.pb }}</td><td>{{ stock.roe }}</td><td>{{ stock.debt }}</td>
              <td><span :class="['signal', stock.signal === '稳健' ? 'signal-good' : 'signal-watch']">{{ stock.signal }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>
</template>

<style scoped>
.workbench-preview { min-width: 1180px; max-width: 1480px; margin: 0 auto; padding: 8px 8px 56px; color: #17201c; }
.preview-heading, .results-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
.preview-heading { padding: 24px 8px 28px; }
.eyebrow, .run-label { color: #708078; font-size: 10px; font-weight: 700; letter-spacing: .12em; }
h1, h2, p { margin: 0; }
h1 { margin-top: 7px; font-size: 30px; line-height: 1.2; letter-spacing: -.03em; }
h2 { font-size: 16px; letter-spacing: -.01em; }
.preview-heading p, .results-heading p { margin-top: 8px; color: #718077; font-size: 13px; }
.workbench-shell { display: grid; grid-template-columns: minmax(680px, 1.6fr) minmax(330px, .74fr); overflow: hidden; border: 1px solid #dce3de; border-radius: 12px; background: #fff; box-shadow: 0 12px 32px rgba(22, 42, 32, .06); }
.editor-column { padding: 30px 32px 32px; border-right: 1px solid #e6ebe7; }
.section-header, .panel-title-row, .rule-name-row, .quality-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.section-header > div, .panel-title-row > div { display: flex; align-items: center; gap: 10px; }
.step-mark { color: #14734d; font-size: 10px; font-weight: 800; letter-spacing: .08em; }
.muted, .helper-text { color: #87938c; font-size: 12px; }
.rule-name-row { margin-top: 22px; padding: 15px 16px; border: 1px solid #e4e9e5; border-radius: 8px; }
.rule-name { display: grid; gap: 3px; }.rule-name span, .summary-grid span, .data-date span, .result-summary span { color: #78857d; font-size: 11px; }.rule-name strong { font-size: 14px; }
.text-button, .preset-button, .filter-chip, .condition-row button { border: 0; background: transparent; color: #50705f; cursor: pointer; font: inherit; font-size: 12px; }.text-button:hover { color: #0f6a46; }
.preset-row { display: flex; gap: 8px; margin: 18px 0 24px; }.preset-button { padding: 7px 11px; border: 1px solid #e0e6e1; border-radius: 6px; color: #64736a; }.preset-button.active { border-color: #b6d7c7; background: #eff8f2; color: #09643e; font-weight: 700; }.preset-button:disabled { cursor: not-allowed; opacity: .45; }
.pool-panel, .conditions-panel { border-top: 1px solid #e7ebe8; padding-top: 22px; }.conditions-panel { margin-top: 26px; }.pool-count { color: #547262; font-size: 12px; font-weight: 600; }.filter-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 15px; }.filter-chip { padding: 7px 10px; border: 1px solid #e1e7e3; border-radius: 99px; color: #68786f; }.filter-chip.is-selected { border-color: #c7ded1; background: #f4faf6; color: #236847; }
.helper-text { margin-top: 9px; }.condition-table { margin-top: 14px; border: 1px solid #e6ebe7; border-radius: 8px; overflow: hidden; }.condition-header, .condition-row { display: grid; grid-template-columns: 1.8fr .85fr .75fr 24px; align-items: center; column-gap: 12px; padding: 10px 14px; }.condition-header { background: #f8faf8; color: #839087; font-size: 11px; }.condition-row { min-height: 42px; border-top: 1px solid #edf0ee; font-size: 12px; }.condition-row strong { font-weight: 600; }.condition-row b { color: #0d6844; }.condition-row button { color: #9ca6a0; font-size: 19px; line-height: 1; }.condition-row button:hover { color: #c13a49; }
.quality-row { margin-top: 26px; padding: 17px 0 0; border-top: 1px solid #e7ebe8; }.quality-row div { display: grid; gap: 4px; }.quality-row strong { font-size: 13px; }.quality-row span { color: #77847c; font-size: 12px; }.toggle { width: 38px; height: 22px; padding: 2px; border: 0; border-radius: 99px; background: #cbd4ce; cursor: pointer; }.toggle i { display: block; width: 18px; height: 18px; border-radius: 50%; background: #fff; transition: transform .16s ease; }.toggle.on { background: #11734c; }.toggle.on i { transform: translateX(16px); }
.run-column { display: flex; flex-direction: column; padding: 30px 27px; background: #f7faf8; }.run-column h2 { margin-top: 8px; font-size: 20px; }.run-column > p { margin-top: 8px; color: #718077; font-size: 12px; line-height: 1.6; }.data-date { display: grid; gap: 4px; margin-top: 30px; padding: 15px 0; border-top: 1px solid #dfe8e2; border-bottom: 1px solid #dfe8e2; }.data-date strong { font-size: 15px; }.data-date small { color: #17784e; font-size: 11px; }.summary-grid { display: grid; grid-template-columns: 1fr 1fr; margin: 20px 0 24px; gap: 17px; }.summary-grid div { display: grid; gap: 4px; }.summary-grid strong { font-size: 14px; }.run-hint { text-align: center; }.run-feedback { display: grid; gap: 3px; margin-top: 19px; padding: 13px; border: 1px solid #c6ddce; border-radius: 7px; background: #f0f8f3; }.run-feedback span { color: #17784e; font-size: 11px; font-weight: 700; }.run-feedback strong { font-size: 13px; }.run-feedback small { color: #6e7e74; font-size: 11px; }.trust-note { display: flex; gap: 8px; margin-top: auto; padding-top: 26px; }.trust-dot { width: 7px; height: 7px; margin-top: 5px; border-radius: 50%; background: #e5a11a; }.trust-note p { color: #718077; font-size: 11px; line-height: 1.65; }.trust-note strong { display: block; color: #415046; }
.results-section { margin-top: 24px; padding: 28px 30px 30px; border: 1px solid #dce3de; border-radius: 12px; background: #fff; }.results-heading { align-items: center; }.results-heading h2 { margin-top: 5px; font-size: 21px; }.result-actions { display: flex; align-items: center; gap: 12px; }.secondary-button { border: 1px solid #cbd8d0; border-radius: 6px; background: #fff; color: #24583f; padding: 8px 12px; font: inherit; font-size: 12px; cursor: pointer; }.result-summary { display: grid; grid-template-columns: repeat(4, 1fr); margin: 24px 0; border: 1px solid #e5eae6; border-radius: 8px; }.result-summary div { display: grid; gap: 4px; padding: 13px 16px; border-right: 1px solid #e5eae6; }.result-summary div:last-child { border: 0; }.result-summary strong { font-size: 16px; }.ready-text { color: #13744b; }.table-wrap { overflow: hidden; border: 1px solid #e4e9e5; border-radius: 8px; } table { width: 100%; border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; } th { padding: 11px 14px; background: #f8faf8; color: #748178; font-size: 10px; font-weight: 700; letter-spacing: .025em; text-align: left; white-space: nowrap; } td { padding: 13px 14px; border-top: 1px solid #edf0ee; color: #405047; } td strong { display: block; color: #1d2a22; font-size: 12px; } td > span:not(.signal) { display: block; margin-top: 3px; color: #89958e; font-size: 10px; }.signal { display: inline-block; padding: 3px 7px; border-radius: 4px; font-size: 10px; }.signal-good { background: #eaf6ee; color: #16744b; }.signal-watch { background: #fbf4e7; color: #a56a0a; }
</style>
