<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { NEmpty, NRadioButton, NRadioGroup, NSelect } from 'naive-ui'
import { dispose, init } from 'klinecharts'
import type { Chart, KLineData } from 'klinecharts'
import type { KlineCandle, KlinePeriod } from '../types/stock-detail.ts'
import { KLINE_ADJUSTS, KLINE_PERIODS, KLINE_RANGES } from '../utils/kline-settings.ts'

const props = defineProps<{
  readonly candles: readonly KlineCandle[]
}>()

const period = defineModel<KlinePeriod>('period', { required: true })
const adjust = defineModel<'raw' | 'qfq'>('adjust', { required: true })
const range = defineModel<number>('range', { required: true })

const container = ref<HTMLElement>()
const chartInstance = ref<Chart | null>(null)

const periodOptions = [
  { label: '日K', value: 'day' },
  { label: '周K', value: 'week' },
  { label: '月K', value: 'month' },
] as const

const rangeOptions = computed(() => {
  const unit = period.value === 'day' ? '日' : period.value === 'week' ? '周' : '月'
  return KLINE_RANGES.map((n) => ({ label: `${n}${unit}`, value: n }))
})

const isEmpty = computed(() => props.candles.length === 0)

function onPeriodChange(value: string | number) {
  const v = String(value)
  if ((KLINE_PERIODS as readonly string[]).includes(v)) {
    period.value = v as KlinePeriod
  }
}

function onAdjustChange(value: string | number) {
  const v = String(value)
  if ((KLINE_ADJUSTS as readonly string[]).includes(v)) {
    adjust.value = v as 'raw' | 'qfq'
  }
}

function onRangeChange(value: string | number | null) {
  if (typeof value === 'number' && (KLINE_RANGES as readonly number[]).includes(value)) {
    range.value = value
  }
}

function mapCandles(): KLineData[] {
  return props.candles.map((c) => ({
    timestamp: new Date(c.trade_date).getTime(),
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close,
    volume: c.volume,
    turnover: c.turnover,
    ma5: c.ma5,
    ma10: c.ma10,
    ma20: c.ma20,
    ma60: c.ma60,
    ma120: c.ma120,
    ma250: c.ma250,
  }))
}

function renderChart() {
  if (!container.value) return
  if (chartInstance.value) {
    dispose(chartInstance.value)
    chartInstance.value = null
  }
  if (props.candles.length === 0) return

  const candles = mapCandles()
  let chart: Chart | null
  try {
    chart = init(container.value, {      styles: {
        grid: {
          show: true,
          horizontal: { color: 'rgba(0,0,0,0.05)' },
          vertical: { color: 'rgba(0,0,0,0.05)' },
        },
        candle: {
          priceMark: {
            last: { show: true },
          },
          bar: {
            upColor: '#ef5350',
            downColor: '#26a69a',
            upBorderColor: '#ef5350',
            downBorderColor: '#26a69a',
            upWickColor: '#ef5350',
            downWickColor: '#26a69a',
          },
        },
        indicator: {
          lines: [
            { color: '#ff9800' },
            { color: '#2196f3' },
            { color: '#9c27b0' },
            { color: '#4caf50' },
            { color: '#f44336' },
            { color: '#00bcd4' },
          ],
        },
        xAxis: { tickText: { color: '#666' } },
        yAxis: { tickText: { color: '#666' } },
      },
    })
  } catch {
    chart = null
  }
  if (!chart) return
  chartInstance.value = chart

  chart.setDataLoader({
    getBars: ({ type, callback }) => callback(type === 'init' ? candles : [], false),
  })
  // setPeriod 触发 getBars('init') 载入数据，并让十字光标/时间轴按周期格式化。
  chart.setPeriod({ type: period.value, span: 1 })
  chart.createIndicator({ name: 'MA', calcParams: [5, 10, 20, 60, 120, 250] }, false)
  chart.createIndicator('VOL', false)
}

watch(() => props.candles, renderChart)
onMounted(renderChart)

onUnmounted(() => {
  if (chartInstance.value) {
    dispose(chartInstance.value)
    chartInstance.value = null
  }
})
</script>

<template>
  <section class="kline-workbench">
    <div class="kline-heading">
      <div>
        <p class="section-kicker">PRICE TREND</p>
        <h2>价格走势</h2>
      </div>
      <div class="kline-controls">
        <n-radio-group :value="period" size="small" @update:value="onPeriodChange">
          <n-radio-button v-for="opt in periodOptions" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </n-radio-button>
        </n-radio-group>
        <n-radio-group :value="adjust" size="small" @update:value="onAdjustChange">
          <n-radio-button value="raw">不复权</n-radio-button>
          <n-radio-button value="qfq">前复权</n-radio-button>
        </n-radio-group>
        <n-select
          :value="range"
          :options="rangeOptions"
          size="small"
          style="width: 104px"
          @update:value="onRangeChange"
        />
      </div>
    </div>
    <div ref="container" class="kline-canvas"></div>
    <n-empty v-if="isEmpty" description="无K线数据" class="kline-empty" />
  </section>
</template>

<style scoped>
.kline-workbench {
  padding: 25px;
  border-radius: 16px;
  background: #fff;
  box-shadow: 0 4px 17px rgba(48, 82, 59, 0.045);
}
.kline-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.section-kicker {
  margin: 0;
  color: #91a097;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.13em;
}
.kline-heading h2 {
  margin: 7px 0 0;
  font-size: 18px;
}
.kline-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}
.kline-canvas {
  height: 400px;
  margin-top: 18px;
  width: 100%;
}
.kline-empty {
  padding: 30px;
}
@media (max-width: 760px) {
  .kline-heading {
    flex-direction: column;
  }
}
</style>
