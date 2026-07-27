# 前端代码审查报告 (v2)

> **项目**：Value Dashboard — A股价值投资研究与筛选工具
> **审查范围**：`frontend/src/` 全部视图、组件、类型定义、工具函数
> **初版日期**：2026-07-25
> **复核日期**：2026-07-25
> **版本**：v2（包含修复验证 + 第二轮深度扫描）

---

## 一、修复验证总览

对初版报告的 13 个问题逐一复核：

| # | 问题 | 严重度 | 状态 |
|---|------|--------|------|
| 1 | v-for key 使用 index → 状态错位 | 严重 | **已修复** |
| 2 | K线请求竞态条件 | 严重 | **已修复** |
| 3 | sortField 默认值可能不在 indicator 列表 | 中等 | **已修复** |
| 4 | is_null 操作符未暴露 | 中等 | **已修复** |
| 5 | "加入自选"缺少质量门控 | 中等 | **已修复** |
| 6 | 个股详情 fetch 静默失败 | 中等 | **已修复** |
| 7 | WatchlistPage 错误处理未用 isAxiosError | 中等 | **已修复** |
| 8 | ScreeningPage columns 硬编码 | 低 | 未修复 |
| 9 | DataStatusPage 缺少自动刷新 | 低 | 未修复 |
| 10 | WatchlistPage any 类型 | 低 | 未修复 |
| 11 | Naive UI locale 缺失 | 低 | **已修复** |
| 12 | index.html title 错误 | 低 | **已修复** |
| 13 | 路由 meta.title 未更新 document.title | 低 | **已修复** |

**修复率：10/13 (77%)**，2 个严重问题全部修复。

---

## 二、各问题修复详情

### 2.1 问题 1 — v-for key [已修复]

**变更文件**：`types/screening.ts`、`ScreeningRuleEditor.vue`、`ScreeningPage.vue`

- `ScreeningRuleCondition` 和 `ScreeningRuleNode` 均增加 `readonly id: string`
- 新增 `generateRuleId()` 函数，使用 `crypto.randomUUID()`
- `ScreeningRuleEditor.vue:97` key 改为 `:key="item.id"`
- `addCondition()` 和 `addGroup()` 生成新对象时附带 `id: generateRuleId()`
- `ScreeningPage.vue:30` 初始 `ruleTree` 也附带 `generateRuleId()`

**验证通过**：每个条件/组拥有不可变唯一标识，增删操作不再导致状态错位。

### 2.2 问题 2 — K线竞态条件 [已修复]

**变更文件**：`StockDetailPage.vue`

- 新增 `klineAbortController = ref<AbortController | null>(null)`
- `fetchKline()` 开头 abort 旧请求：`klineAbortController.value.abort()`
- axios 请求传入 `signal: klineAbortController.value.signal`
- catch 中检查 `axios.isCancel(e)` 后直接 return

**验证通过**：快速切换不复权/前复权/日期范围时，旧请求被取消，不会再覆盖新数据。

### 2.3 问题 3 — sortField 校验 [已修复]

**变更文件**：`ScreeningPage.vue:111-113`

```ts
if (indicators.value.length > 0 && !indicators.value.find(i => i.name === sortField.value)) {
  sortField.value = indicators.value[0].name
}
```

**验证通过**：indicators 加载后自动校验 sortField 合法性。

### 2.4 问题 4 — is_null 操作符 [已修复]

**变更文件**：`ScreeningPage.vue:51`

```ts
{ label: '为空', value: 'is_null' },
```

**验证通过**：用户可选择"为空"条件，RuleConditionRow 的 `v-if` 判断已兼容。

### 2.5 问题 5 — "加入自选"质量门控 [已修复]

**变更文件**：`ScreeningResultsPanel.vue:204`

```html
<n-button id="watchlist-btn" :disabled="durableActionsDisabled"
  :aria-describedby="durableActionsDisabled ? 'quality-alert' : undefined"
  @click="addToWatchlist">加入自选</n-button>
```

**验证通过**：存在不可信字段时三个操作按钮（保存/导出/加入自选）均被禁用，行为一致。

### 2.6 问题 6 — fetch 静默失败 [已修复]

**变更文件**：`StockDetailPage.vue`

所有 fetch 函数（`fetchStockInfo`, `fetchIndicators`, `fetchKline`, `fetchTrend`, `fetchAudit`）的 catch 块均添加了 `useMessage().warning()` 通知，使用 `isAxiosError` 模式提取 detail。

**注意**：`fetchWarningCodes` 仍保持静默失败（见新问题清单）。

### 2.7 问题 7 — WatchlistPage 错误处理 [已修复]

**变更文件**：`WatchlistPage.vue:80-85,95-98,106-109`

所有 catch 块改为 `catch (e: unknown)` + `isAxiosError` 模式：

```ts
} catch (e: unknown) {
  const detail = isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '未知错误'
  message.error(`加载失败: ${detail}`)
}
```

### 2.8 问题 11 — Naive UI locale [已修复]

**变更文件**：`App.vue:4,30`

```ts
import { ..., zhCN, dateZhCN } from 'naive-ui'
// ...
<n-config-provider :locale="zhCN" :date-locale="dateZhCN">
```

### 2.9 问题 12 — index.html title [已修复]

**变更文件**：`index.html:7`

```html
<title>Value Dashboard - A股价值投资研究</title>
```

### 2.10 问题 13 — document.title [已修复]

**变更文件**：`main.ts:48-51`

```ts
router.afterEach((to) => {
  const title = to.meta.title as string
  document.title = title ? `${title} - Value Dashboard` : 'Value Dashboard'
})
```

---

## 三、未修复问题

| # | 问题 | 严重度 | 理由 |
|---|------|--------|------|
| 8 | `ScreeningPage.vue:81-85` columns 硬编码 | 低 | 需后端配合提供可用字段元数据，属于功能增强 |
| 9 | `DataStatusPage.vue` 缺少自动刷新 | 低 | 监控类特性，需设计轮询策略和销毁时机 |
| 10 | `WatchlistPage.vue:13-14` any 类型 | 低 | 需补充 WatchlistItem/WatchlistGroup 类型定义 |

---

## 四、第二轮深度扫描 — 新发现的问题

### [中等] 新问题 14 — `StockDetailPage.vue:137-146` — fetchWarningCodes 仍静默失败

```ts
async function fetchWarningCodes() {
  try { ... } catch {
    warningCodes.value = []
  }
}
```

其他 5 个 fetch 函数已添加用户通知，但 `fetchWarningCodes` 仍无提示。虽然数据质量状态是非关键数据源，但与其他函数的修复方案不一致。

### [中等] 新问题 15 — `DataStatusPage.vue:44` — 错误处理为旧模式

```ts
} catch (e: any) {
  error.value = e.message || '加载失败'
}
```

使用 `catch (e: any)` + `e.message`，未使用 `isAxiosError` 模式。网络错误时 `e.message` 可能为 "Network Error"。

### [低] 新问题 16 — `StockDetailPage.vue:53-68` — 非K线请求缺少竞态保护

```ts
async function fetchAll() {
  await Promise.all([
    fetchStockInfo(),
    fetchIndicators(),
    fetchKline(),      // K线有 AbortController 保护
    fetchTrend(),
    fetchAudit(),
    fetchWarningCodes(),
  ])
}
```

`fetchKline` 已使用 AbortController，但其他 5 个 fetch 没有。用户快速切换股票代码时（如从自选列表连续点击），旧请求可能后到达并覆盖新股票数据。

**复现**：用户点击股票 A → 立即点击股票 B → fetchStockInfo(B) 先返回 → fetchStockInfo(A) 后返回 → 股票 B 页面显示股票 A 名称。

**修复建议**：在 `fetchAll` 中使用统一的 AbortController，或在每个 fetch 函数中检查 `stockCode` 是否仍等于当前路由参数。

### [低] 新问题 17 — `ScreeningResultsPanel.vue:209` — 模板中每次渲染创建新数组

```html
<n-data-table :data="[...results]" ... />
```

`[...results]` 在每次重新渲染时创建新数组。当结果数接近 5000 条时，有可感知的性能开销。

**修复建议**：使用 `computed` 缓存：

```ts
const tableData = computed(() => [...props.results])
```

### [低] 新问题 18 — `ScreeningRuleEditor.vue:91,108` — 直接修改 props.node

```ts
@update:value="node.logic = $event"
```

直接修改 `props.node.logic` 依赖于 `props.node` 与父组件 `reactive` 对象的引用共享。虽然 Vue 3 中 `reactive` 深度追踪使得此操作最终正确，但绕过 emit 直接修改 props 不符合 Vue 单向数据流最佳实践，也使数据变更路径难以追踪。

### [低] 新问题 19 — `IndicatorTabs.vue:98-112` — "自定义指标"tab 功能未完成

```html
<n-tab-pane name="custom" tab="自定义指标">
  <n-space vertical>
    <n-space>
      <span style="color:#999;font-size:12px;">选择字段查看趋势（逗号分隔）:</span>
    </n-space>
    <n-data-table ... />
  </n-space>
</n-tab-pane>
```

"自定义指标"tab 显示了提示文字但无字段选择输入框，数据表格展示了 `trendData` 的副本（与 FinancialTrendCard 的列定义不同，此处仅 5 列），功能未完整。建议补全或暂隐藏。

### [低] 新问题 20 — `generateRuleId` 环境兼容性

```ts
export function generateRuleId(): string {
  return crypto.randomUUID()
}
```

`crypto.randomUUID()` 在 Chrome 92+ / Firefox 95+ 中可用（包括非 HTTPS 场景），但 Safari < 15.4 不支持，Node.js < 19 不支持。如果需要在较旧环境运行，需加 fallback。

**修复建议**：

```ts
export function generateRuleId(): string {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}
```

### [低] 新问题 21 — `FinancialTrendCard` 与 `IndicatorTabs` 中归母净利润字段不一致

- `FinancialTrendCard.vue:32`：`key: 'net_profit'`（与 `FinancialTrendRow.net_profit` 对应）
- `IndicatorTabs.vue:42`：`key: 'parent_net_profit'`（与 `FinancialTrendRow.parent_net_profit` 对应）

两个组件展示同一数据源的不同字段。`net_profit` 和 `parent_net_profit` 在合并报表口径下通常一致，但存在差异时可能导致用户困惑。建议统一字段名或明确标注指标含义。

---

## 五、新增问题汇总

| # | 文件:行 | 问题 | 严重度 |
|---|---------|------|--------|
| 14 | `StockDetailPage.vue:137-146` | fetchWarningCodes 仍静默失败，与其他函数不一致 | 中等 |
| 15 | `DataStatusPage.vue:44` | 错误处理为旧模式 (e: any) | 中等 |
| 16 | `StockDetailPage.vue:53-68` | 非K线请求缺少竞态保护，快速切换股票可能数据错乱 | 低 |
| 17 | `ScreeningResultsPanel.vue:209` | 模板中 `[...results]` 每次渲染创建新数组 | 低 |
| 18 | `ScreeningRuleEditor.vue:91,108` | 直接修改 props.node，不符合单向数据流 | 低 |
| 19 | `IndicatorTabs.vue:98-112` | "自定义指标"tab 功能未完成 | 低 |
| 20 | `types/screening.ts:34` | generateRuleId 缺少旧浏览器 fallback | 低 |
| 21 | `FinancialTrendCard/IndicatorTabs` | 归母净利润字段名不一致 | 低 |

---

## 六、完整问题状态矩阵

### 已修复 (10)

| # | 问题 | 严重度 |
|---|------|--------|
| 1 | v-for key 使用 index | 严重 |
| 2 | K线请求竞态条件 | 严重 |
| 3 | sortField 默认值校验 | 中等 |
| 4 | is_null 操作符缺失 | 中等 |
| 5 | "加入自选"质量门控 | 中等 |
| 6 | 个股详情 fetch 静默失败 | 中等 |
| 7 | WatchlistPage isAxiosError | 中等 |
| 11 | Naive UI locale | 低 |
| 12 | index.html title | 低 |
| 13 | document.title 更新 | 低 |

### 未修复 (3)

| # | 问题 | 严重度 |
|---|------|--------|
| 8 | columns 硬编码 | 低 |
| 9 | DataStatusPage 自动刷新 | 低 |
| 10 | WatchlistPage any 类型 | 低 |

### 新发现 (8)

| # | 问题 | 严重度 |
|---|------|--------|
| 14 | fetchWarningCodes 静默 | 中等 |
| 15 | DataStatusPage 错误处理 | 中等 |
| 16 | 非K线请求竞态 | 低 |
| 17 | results 数组展开 | 低 |
| 18 | props 直接修改 | 低 |
| 19 | 自定义指标未完成 | 低 |
| 20 | crypto 兼容性 | 低 |
| 21 | 字段名不一致 | 低 |

---

## 七、总体评价

**第二轮审查结论：修复质量高，2个严重问题全部解决。** 新增的 8 个问题均为中等/低优先级，不影响基础交互逻辑的正常使用。

- **核心链路完整可用**：筛选→结果→操作、个股详情、自选列表、数据状态均闭环
- **严重问题清零**：v-for key 和 K线竞态均已修复
- **错误处理规范化**：isAxiosError 模式已推广到大部分 catch 块（DataStatusPage 和 fetchWarningCodes 仍待跟进）
- **新发现问题可控**：无阻塞性问题，均为代码质量改进项
