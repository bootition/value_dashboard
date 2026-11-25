<script setup lang="ts">
/**
 * 黑名单页 —— 三个互不相同的概念，分三个 tab：
 *
 * 1. **彻底拉黑**（`stock_blacklist`）：人手工点详情页按钮进来的，身份型事实或
 *    「我就是不想看」。只由人增删，规则不参与比较。
 * 2. **浮动黑名单**（`blacklist_floating`）：由规则库算出来的，每次数据更新重算，
 *    有进有出。**这里没有任何手工入口**——要让某只股票进来，去改规则。
 * 3. **规则库**（`blacklist_rules`）：用户讨厌的规则都丢这里，命中任一条即浮动拉黑。
 *
 * 这个页面是黑名单股票唯一允许显示名字的地方（运维自救面，不是研究面）。
 */
import { computed, h, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  NAlert, NButton, NDataTable, NEmpty, NResult, NSpace, NSpin, NSwitch, NTabPane, NTabs,
  NTag, useDialog, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import axios, { isAxiosError } from 'axios'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import type {
  BlacklistItem, BlacklistListResponse, BlacklistRule, BlacklistRuleListResponse,
  BlacklistRulePreviewResponse, FloatingBlacklistEvent, FloatingBlacklistEventsResponse,
  FloatingBlacklistItem, FloatingBlacklistResponse,
} from '../types/blacklist.ts'

const message = useMessage()
const dialog = useDialog()

const loadingPermanent = ref(false)
const loadingFloating = ref(false)
const loadingRules = ref(false)
const removingCode = ref('')
const togglingRuleId = ref<number | null>(null)

const permanent = ref<ReadonlyArray<BlacklistItem>>([])
const floating = ref<FloatingBlacklistResponse | null>(null)
const events = ref<ReadonlyArray<FloatingBlacklistEvent>>([])
const rules = ref<ReadonlyArray<BlacklistRule>>([])

// ─── 数据加载 ────────────────────────────────────────────────────

async function fetchPermanent() {
  loadingPermanent.value = true
  try {
    const resp = await axios.get<BlacklistListResponse>('/api/blacklist/list')
    permanent.value = resp.data.items
  } catch (e) {
    message.error(friendlyErrorMessage(e, '加载黑名单失败'))
  } finally {
    loadingPermanent.value = false
  }
}

async function fetchFloating() {
  loadingFloating.value = true
  try {
    const [roster, ev] = await Promise.all([
      axios.get<FloatingBlacklistResponse>('/api/blacklist/floating'),
      axios.get<FloatingBlacklistEventsResponse>('/api/blacklist/floating/events', {
        params: { limit: 50 },
      }),
    ])
    floating.value = roster.data
    events.value = ev.data.events
  } catch (e) {
    message.error(friendlyErrorMessage(e, '加载浮动黑名单失败'))
  } finally {
    loadingFloating.value = false
  }
}

async function fetchRules() {
  loadingRules.value = true
  try {
    const resp = await axios.get<BlacklistRuleListResponse>('/api/blacklist/rules')
    rules.value = resp.data.rules
  } catch (e) {
    message.error(friendlyErrorMessage(e, '加载规则库失败'))
  } finally {
    loadingRules.value = false
  }
}

function refreshAll() {
  void fetchPermanent()
  void fetchFloating()
  void fetchRules()
}

// ─── 彻底拉黑：移出 ──────────────────────────────────────────────

async function removePermanent(code: string) {
  if (removingCode.value) return
  removingCode.value = code
  try {
    await axios.delete('/api/blacklist/remove', { data: { stock_code: code } })
    message.success('已移出黑名单，该股票恢复显示')
    await fetchPermanent()
  } catch (e) {
    if (isAxiosError(e) && e.response?.status === 404) {
      message.warning('该股票已不在黑名单中')
      await fetchPermanent()
      return
    }
    message.error(friendlyErrorMessage(e, '移出黑名单失败'))
  } finally {
    removingCode.value = ''
  }
}

// ─── 规则库：开关 / 试算 / 重算 ───────────────────────────────────

async function toggleRule(rule: BlacklistRule, enabled: boolean) {
  if (togglingRuleId.value !== null) return
  togglingRuleId.value = rule.id
  try {
    await axios.post(`/api/blacklist/rules/${rule.id}/toggle`, { enabled })
    message.success(enabled ? `已启用「${rule.name}」，下次重算生效` : `已停用「${rule.name}」`)
    await fetchRules()
  } catch (e) {
    message.error(friendlyErrorMessage(e, '切换规则失败'))
  } finally {
    togglingRuleId.value = null
  }
}

/**
 * 试算：开规则之前先看会拉黑多少只。
 *
 * 规则比手工黑名单危险得多——一条写错的条件可能一次拉黑半个市场，
 * 所以「先试算再启用」是这套机制唯一的安全阀。
 */
async function previewRule(rule: BlacklistRule) {
  try {
    const resp = await axios.post<BlacklistRulePreviewResponse>(
      `/api/blacklist/rules/${rule.id}/preview`,
    )
    const { matched, sample } = resp.data
    dialog.info({
      title: `试算：${rule.name}`,
      content: () => h('div', [
        h('p', { style: 'margin:0 0 8px' }, `当前会命中 ${matched} 只股票。`),
        matched === 0
          ? h('p', { style: 'margin:0;color:#8a978e' }, '条件可能过严，也可能数据还没就绪。')
          : h('div', [
              h('p', { style: 'margin:0 0 4px;color:#8a978e' }, `前 ${sample.length} 只：`),
              h('div', { style: 'max-height:260px;overflow:auto;font-size:12px;line-height:1.8' },
                sample.map((s) => h('div', `${s.stock_code} ${s.name}`))),
            ]),
      ]),
      positiveText: '知道了',
    })
  } catch (e) {
    message.error(friendlyErrorMessage(e, '试算失败'))
  }
}

async function recompute() {
  loadingFloating.value = true
  try {
    const resp = await axios.post<{ entered: number; exited: number; rules_evaluated: number }>(
      '/api/blacklist/floating/refresh',
    )
    message.success(
      `重算完成：${resp.data.rules_evaluated} 条规则，新增 ${resp.data.entered}、移出 ${resp.data.exited}`,
    )
    await Promise.all([fetchFloating(), fetchRules()])
  } catch (e) {
    message.error(friendlyErrorMessage(e, '重算失败'))
  } finally {
    loadingFloating.value = false
  }
}

// ─── 表格列 ──────────────────────────────────────────────────────

function formatTime(value: string | null): string {
  if (!value) return '—'
  return value.replace('T', ' ').slice(0, 19)
}

const permanentColumns = computed<DataTableColumns<BlacklistItem>>(() => [
  { title: '股票代码', key: 'stock_code', width: 110,
    render: (row) => h('strong', row.stock_code) },
  { title: '名称', key: 'name', width: 150, render: (row) => row.name || '—' },
  { title: '交易所', key: 'exchange', width: 90, render: (row) => row.exchange || '—' },
  { title: '加入时间', key: 'created_at', width: 170, render: (row) => formatTime(row.created_at) },
  { title: '备注', key: 'reason', ellipsis: { tooltip: true },
    render: (row) => row.reason || '—' },
  { title: '操作', key: 'actions', width: 120,
    render: (row) => h(NButton, {
      size: 'small',
      loading: removingCode.value === row.stock_code,
      disabled: Boolean(removingCode.value) && removingCode.value !== row.stock_code,
      onClick: () => removePermanent(row.stock_code),
    }, { default: () => '移出黑名单' }) },
])

const floatingColumns = computed<DataTableColumns<FloatingBlacklistItem>>(() => [
  { title: '股票代码', key: 'stock_code', width: 110,
    render: (row) => h('strong', row.stock_code) },
  { title: '名称', key: 'name', width: 150, render: (row) => row.name || '—' },
  {
    title: '被哪几条规则拉黑',
    key: 'rules',
    render: (row) => h(NSpace, { size: 4 },
      { default: () => row.rules.map((r) => h(NTag, { size: 'small' }, { default: () => r.name })) }),
  },
])

const eventColumns = computed<DataTableColumns<FloatingBlacklistEvent>>(() => [
  { title: '时间', key: 'run_at', width: 170, render: (row) => formatTime(row.run_at) },
  { title: '动作', key: 'event', width: 90,
    render: (row) => h(NTag, {
      size: 'small',
      type: row.event === 'entered' ? 'warning' : 'success',
    }, { default: () => (row.event === 'entered' ? '进入' : '移出') }) },
  { title: '股票', key: 'stock_code', width: 200,
    render: (row) => `${row.stock_code} ${row.stock_name || ''}` },
  { title: '规则', key: 'rule_name', render: (row) => row.rule_name || '—' },
])

const ruleColumns = computed<DataTableColumns<BlacklistRule>>(() => [
  { type: 'expand', renderExpand: (row) =>
      h('div', { style: 'white-space:pre-wrap;font-size:12px;line-height:1.75;color:#4a5a4f' },
        row.note || '（无推导说明）') },
  { title: '规则', key: 'name', minWidth: 220,
    render: (row) => h('div', [
      h('strong', row.name),
      row.origin === 'preset'
        ? h(NTag, { size: 'tiny', style: 'margin-left:6px' }, { default: () => '预置' })
        : h(NTag, { size: 'tiny', type: 'info', style: 'margin-left:6px' }, { default: () => '自建' }),
    ]) },
  { title: '当前命中', key: 'matched', width: 110,
    render: (row) => (row.enabled ? `${row.matched} 只` : h('span', { style: 'color:#8a978e' }, '已停用')) },
  { title: '启用', key: 'enabled', width: 90,
    render: (row) => h(NSwitch, {
      size: 'small',
      value: row.enabled,
      loading: togglingRuleId.value === row.id,
      'onUpdate:value': (value: boolean) => toggleRule(row, value),
    }) },
  { title: '操作', key: 'actions', width: 110,
    render: (row) => h(NButton, { size: 'small', onClick: () => previewRule(row) },
      { default: () => '试算' }) },
])

onMounted(refreshAll)
</script>

<template>
  <section class="blacklist-page">
    <header class="blacklist-header">
      <div>
        <h1>黑名单</h1>
        <p class="blacklist-subtitle">
          彻底拉黑由你手工决定；浮动拉黑由规则库自动决定，每次数据更新重算。
        </p>
      </div>
      <n-space>
        <n-button :loading="loadingPermanent || loadingFloating || loadingRules" @click="refreshAll">
          刷新
        </n-button>
        <router-link to="/stock"><n-button>去个股搜索</n-button></router-link>
      </n-space>
    </header>

    <n-tabs type="line" animated>
      <!-- ─── 彻底拉黑 ─────────────────────────────────────────── -->
      <n-tab-pane name="permanent" :tab="`彻底拉黑（${permanent.length}）`">
        <n-alert type="info" :show-icon="true" class="tab-note">
          详情页点「加入黑名单」进来的。不参与规则比较，只由你手工增删。
          加入后原始行情与财报数据仍完整保留、自动更新照常抓取。
        </n-alert>
        <n-spin :show="loadingPermanent">
          <n-result
            v-if="!loadingPermanent && permanent.length === 0"
            status="success"
            title="还没有彻底拉黑的股票"
            description="在个股详情页点击「加入黑名单」后，该股票会出现在这里。"
          />
          <n-data-table
            v-else
            :columns="permanentColumns"
            :data="[...permanent]"
            :row-key="(row: BlacklistItem) => row.stock_code"
            :pagination="{ pageSize: 50 }"
          />
        </n-spin>
      </n-tab-pane>

      <!-- ─── 浮动黑名单 ───────────────────────────────────────── -->
      <n-tab-pane name="floating" :tab="`浮动黑名单（${floating?.count ?? 0}）`">
        <n-alert type="info" :show-icon="true" class="tab-note">
          规则算出来的，每次数据更新重算，有进有出。
          <strong>这里没有手工入口</strong>——想让某只股票进出，去「规则库」改规则。
        </n-alert>

        <div class="floating-toolbar">
          <span v-if="floating?.last_run" class="floating-run">
            上次重算 {{ formatTime(floating.last_run.run_at) }} ·
            规则 {{ floating.last_run.rules_evaluated }} 条 ·
            新增 {{ floating.last_run.entered }} / 移出 {{ floating.last_run.exited }}
            <n-tag v-if="floating.last_run.status !== 'ok'" size="small" type="warning">
              部分规则失败
            </n-tag>
          </span>
          <span v-else class="floating-run">还没重算过（自动更新最后一步会跑，也可以手动触发）</span>
          <n-button size="small" :loading="loadingFloating" @click="recompute">立即重算</n-button>
        </div>

        <n-spin :show="loadingFloating">
          <n-result
            v-if="!loadingFloating && (floating?.count ?? 0) === 0"
            status="success"
            title="浮动黑名单是空的"
            description="去「规则库」启用几条规则，或点上面的「立即重算」。"
          />
          <n-data-table
            v-else
            :columns="floatingColumns"
            :data="[...(floating?.items ?? [])]"
            :row-key="(row: FloatingBlacklistItem) => row.stock_code"
            :pagination="{ pageSize: 50 }"
          />
        </n-spin>

        <h3 class="section-title">最近的进出</h3>
        <n-data-table
          :columns="eventColumns"
          :data="[...events]"
          :row-key="(row: FloatingBlacklistEvent) => `${row.run_at}-${row.stock_code}-${row.event}`"
          :max-height="360"
        >
          <template #empty>
            <n-empty description="还没有进出记录" />
          </template>
        </n-data-table>
      </n-tab-pane>

      <!-- ─── 规则库 ───────────────────────────────────────────── -->
      <n-tab-pane name="rules" :tab="`规则库（${rules.length}）`">
        <n-alert type="warning" :show-icon="true" class="tab-note">
          命中**任一条**启用的规则即进浮动黑名单。
          规则里的每个指标都是滚动/当期口径，所以「热度退了、业绩好了」会自动出名单——
          <strong>不要写绝对日期</strong>（写死「2013-12-31 之后上市」的规则到第二年就失效了）。
          开规则前请先点「试算」看一眼会拉黑多少只。
        </n-alert>
        <n-spin :show="loadingRules">
          <n-data-table
            :columns="ruleColumns"
            :data="[...rules]"
            :row-key="(row: BlacklistRule) => row.id"
          >
            <template #empty>
              <n-empty description="规则库是空的" />
            </template>
          </n-data-table>
        </n-spin>
      </n-tab-pane>
    </n-tabs>
  </section>
</template>

<style scoped>
.blacklist-page {
  padding: 24px 28px 40px;
  max-width: 1240px;
}

.blacklist-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 10px;
}

.blacklist-header h1 {
  font-size: 20px;
  margin: 0 0 4px;
}

.blacklist-subtitle {
  margin: 0;
  font-size: 13px;
  color: #6b7a70;
}

.tab-note {
  margin: 12px 0 14px;
}

.floating-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.floating-run {
  font-size: 12px;
  color: #6b7a70;
}

.section-title {
  margin: 22px 0 10px;
  font-size: 14px;
  font-weight: 700;
  color: #3e7551;
}
</style>
