<script setup lang="ts">
import { ref, onMounted, computed, h } from 'vue'
import type { VNodeChild } from 'vue'
import { useRouter, RouterLink } from 'vue-router'
import {
  NAlert, NInput, NButton, NDataTable,
  NEmpty, NModal, useMessage, useDialog, NCheckbox, NTag, NSelect
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import axios from 'axios'
import { isIndicatorUntrusted } from '../types/data-quality.ts'
import type { IndicatorTrust } from '../types/data-quality.ts'
import { friendlyErrorMessage } from '../helpers/api-error.ts'
import { formatFieldValue } from '../utils/screening-format.ts'
import { useWatchlistColumns, resolveDropPosition } from '../composables/useWatchlistColumns.ts'
import type { WatchlistColumnSpec } from '../composables/useWatchlistColumns.ts'

interface WatchlistItem {
  stock_code: string
  name: string
  exchange: string
  csrc_l1: string | null
  listing_date: string | null
  group_name: string
  source_rule_id: number | null
  source_result_id: number | null
  added_at: string | null
  latest_close: number | null
  pe_ttm: number | null
  pb_mrq: number | null
  roe: number | null
  gross_margin: number | null
  net_margin: number | null
  debt_ratio: number | null
  revenue_yoy: number | null
  net_profit_yoy: number | null
  dividend_yield: number | null
  total_market_cap: number | null
  circ_market_cap: number | null
  untrusted_fields?: string[]
}

interface WatchlistGroup {
  group_name: string
  cnt: number
  /** 分组类型（2026-11-26 注册表）：`default`=我的自选 / `manual`=自建 / `rule`=筛选规则写入。 */
  kind?: 'default' | 'manual' | 'rule'
  /** 该分组登记的来源规则（`kind='rule'` 且只有一个规则时有值）。 */
  rule_id?: number | null
  /** 是否默认分组（后端已在每项上算好，前端不重新推断）。 */
  is_default?: boolean
  /** 该分组来自几次不同的已保存筛选结果（NULL 不计）；`GET /groups` 提供。 */
  runs?: number
  /** `source_result_id IS NULL` 的手动自选行数。 */
  manual?: number
  /** 该分组出现过的不同 `source_rule_id`（删分组对话框据此判断能否连带删规则）。 */
  rule_ids?: number[]
}

/** `DELETE /api/watchlist/groups/{name}` 的响应契约（2026-11-25 新增）。 */
interface RuleSkipInfo {
  rule_id: number
  reason_code: string
  message: string
  refs?: { results?: number; watchlist?: number; runs?: number } | null
}

interface DeleteGroupResponse {
  status: string
  group: string
  deleted_rows: number
  rule_deleted: boolean
  deleted_rule_id: number | null
  rule_delete_status: 'not_requested' | 'no_rule' | 'deleted' | 'skipped'
  rule_delete_reason: string | null
  skipped_rules: RuleSkipInfo[]
}


interface DeleteGroupTarget {
  group_name: string
  cnt: number
  runs: number
  manual: number
  rule_ids: number[]
}

interface WatchlistResponse {
  items: WatchlistItem[]
  count: number
  groups: WatchlistGroup[]
  trust?: IndicatorTrust
  auto_update_in_progress?: boolean
  /** 后端降级契约（2026-11-24）：`unavailable` = DuckDB 取名称/指标失败
   *  （通常是自动更新持锁），**分组与股票行照常返回**，只是没有名称/指标。
   *  绝不能把它读成"你没有分组"。 */
  enrichment?: 'ok' | 'unavailable'
  enrichment_error?: string | null
}

const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const items = ref<WatchlistItem[]>([])
const groups = ref<WatchlistGroup[]>([])
const trust = ref<IndicatorTrust | null>(null)
const autoUpdateInProgress = ref(false)
// DuckDB 富化是否降级：只影响"名称/指标显示得全不全"，不影响分组与行是否存在。
const enrichmentUnavailable = ref(false)
const enrichmentError = ref<string | null>(null)
const loading = ref(true)
const refreshing = ref(false)
// `default` 只是**历史**持久化 key（v21 之前前端不传分组时的落点），
// 用户可见处一律走 displayGroupName 归一成中文。
// 2026-11-26 起默认分组是注册表里的 `我的自选`（kind='default'，不可删/不可改名）。
const MANUAL_GROUP_KEY = 'default'
const MANUAL_GROUP_LABEL = '手动收录'
const DEFAULT_GROUP_KEY = '我的自选'
// 首屏不预选任何分组：等分组列表回来后自动落到第一个真实规则分组，
// 这样用户一进来自选页看到的就是「已有的规则分组」，而不是空的占位组。
const selectedGroup = ref<string | null>(null)
const showColumnSettings = ref(false)
const addStockCode = ref('')
/** 「输入代码 + 添加」的目标分组：2026-11-26 从自由输入改成**下拉选已有分组**
 *  （自由输入会造出拼错的新分组，而用户真正想要的是「放进我建好的那个组」）。 */
const addGroupName = ref<string | null>(null)

// ── 自建分组（2026-11-26：空分组必须能存在）─────────────────────────────
const newGroupName = ref('')
const creatingGroup = ref(false)
/** 移动目标下拉里的「新建分组…」哨兵值。 */
const MOVE_TO_NEW = '__new_group__'
const renameTarget = ref<WatchlistGroup | null>(null)
const showRenameGroup = ref(false)
const renameName = ref('')
const renamingGroup = ref(false)
const renameError = ref<string | null>(null)

// ── 批量复制（选中若干只 → 复制到分组；规则分组里的行**不会被删**）────────
const checkedRowKeys = ref<Array<string | number>>([])
const moveTarget = ref<string | null>(null)
const moving = ref(false)

// ── 删除整个自选分组（2026-11-25 用户实测：此前只能一只一只删）───────────
const showDeleteGroup = ref(false)
const deleteGroupTarget = ref<DeleteGroupTarget | null>(null)
const deleteGroupRule = ref(false)
const deletingGroup = ref(false)
const deleteGroupLoading = ref(false)
/** 删除失败时把后端原因**留在对话框里**，而不是只闪一条 toast。 */
const deleteGroupError = ref<string | null>(null)

const warningCodes = computed(() => trust.value?.warning_codes ?? [])
const hasUntrustedIndicators = computed(
  () => warningCodes.value.length > 0 && isIndicatorUntrusted('*', warningCodes.value)
)
const hasRefreshableRule = computed(() => items.value.some((item) => item.source_rule_id != null))

/** 唯一的组名展示归一出口：`default` 只作为内部 key 存在。 */
const displayGroupName = (name: string) => name === MANUAL_GROUP_KEY ? MANUAL_GROUP_LABEL : name
const isManualGroup = (name: string | null) => name === MANUAL_GROUP_KEY
/** 默认分组（不可删/不可改名）。后端每项都带 `is_default`，这里只做兜底识别。 */
const isDefaultGroup = (group: Pick<WatchlistGroup, 'group_name' | 'kind' | 'is_default'>) =>
  group.is_default === true || group.kind === 'default' || group.group_name === DEFAULT_GROUP_KEY
/**
 * 侧栏分组：
 * - **空分组必须显示**（2026-11-26 核心价值：用户建一个组、再从别处把股票挪进去；
 *   改造前分组由 `GROUP BY watchlist` 派生，0 只的组根本不存在）；
 * - 唯一的例外是历史 key `default`（旧前端"手动收录"落点）：它空着的时候没有任何
 *   信息量，留在侧栏只会占位置，所以保持旧口径隐藏（有股票时照常显示、可改名可删）。
 */
const visibleGroups = computed(() => groups.value
  .filter((g) => !isManualGroup(g.group_name) || g.cnt > 0)
  .slice()
  .sort((a, b) => Number(isManualGroup(a.group_name)) - Number(isManualGroup(b.group_name))))

/** 目标分组下拉的选项（移动/添加共用；含「新建分组…」入口）。 */
const groupOptions = computed(() => visibleGroups.value.map((group) => ({
  label: `${displayGroupName(group.group_name)}（${group.cnt}）`,
  value: group.group_name,
})))

/** 选中的自选行（行 key = `分组|代码`，同一只股票在多个分组时可分别选中）。 */
const rowKeyOf = (row: WatchlistItem) => `${row.group_name}|${row.stock_code}`
const selectedRows = computed(() => items.value.filter(
  (row) => checkedRowKeys.value.includes(rowKeyOf(row)),
))
const hasVisibleGroups = computed(() => visibleGroups.value.length > 0)
const selectedGroupLabel = computed(() => selectedGroup.value ? displayGroupName(selectedGroup.value) : '')
const selectedIsManual = computed(() => isManualGroup(selectedGroup.value))
const emptyDescription = computed(
  () => selectedGroup.value ? `「${selectedGroupLabel.value}」里还没有公司` : '还没有可显示的自选分组',
)

/** 降级横幅：必须说清"是名称/指标没加载"，而不是让页面看起来像"自选没了"。 */
const enrichmentNotice = computed(() => {
  if (!enrichmentUnavailable.value) return ''
  const reason = (enrichmentError.value ?? '').slice(0, 120)
  if (enrichmentError.value === 'duckdb_read_locked' || autoUpdateInProgress.value) {
    return '数据正在自动更新，股票名称/指标暂未加载；分组与股票代码已正常显示，稍后刷新即可。'
  }
  return `股票名称/指标暂时加载失败${reason ? `（${reason}）` : ''}；分组与股票代码已正常显示，稍后刷新重试。`
})

function resolveSelectedGroup(list: WatchlistGroup[]): string | null {
  const visible = list.filter((g) => !isManualGroup(g.group_name) || g.cnt > 0)
  const current = selectedGroup.value
  if (current && visible.some((g) => g.group_name === current)) return current
  // 优先落到**有股票**的非默认分组（用户说的"已有的那个规则分组界面"）；
  // 都没有就落到默认分组「我的自选」（它一定在，且用户随时要能往里面加）。
  return visible.find((g) => !isDefaultGroup(g) && !isManualGroup(g.group_name) && g.cnt > 0)?.group_name
    ?? visible.find((g) => isDefaultGroup(g))?.group_name
    ?? visible.find((g) => !isManualGroup(g.group_name))?.group_name
    ?? visible.find((g) => isManualGroup(g.group_name))?.group_name
    ?? null
}

// ── 列配置（2026-11-25 用户实测：表头固定、「想看 PE 每次都得拉到最右边」）──
// 可选列清单 / 默认顺序 / 默认可见集合 / 存储格式全部在
// `composables/useWatchlistColumns.ts` 单源维护（改造前页内三份同源清单已合并成一份）。
// 这里只做三件事：表头拖动交互、列设置面板（勾选 + ▲▼ + 恢复默认）、把可见列渲染成表格列。
// 存储 key 按分组隔离（`vd.watchlist.columns.<编码分组名>`）→ 用户要的「固定在这个分组上」；
// 没配置过的分组 = 出厂默认 13 列，用户看到的表与改造前逐字相同。
const {
  visibleColumnSpecs, orderedColumnSpecs, visibleColumnKeySet,
  columnOrder, moveColumn, toggleColumnVisible, showAllColumns, shiftColumn, resetColumns,
} = useWatchlistColumns({ groupName: selectedGroup })

// ── 表头拖动排序 ────────────────────────────────────────────────────────────
// 用原生 HTML5 drag 事件（naive-ui 2.44 的 n-data-table 没有列拖动能力，
// 项目也没装 sortablejs/vuedraggable，不为这一个交互新增依赖）。
const dragColumnKey = ref<string | null>(null)
const dropTargetKey = ref<string | null>(null)
const dropPosition = ref<'before' | 'after'>('before')
// 拖放结束紧跟的那一次 click 属于「放下」，不能当成「点表头排序」：
// 由 `onHeaderTitleClick` 在冒泡阶段拦掉（n-data-table 的排序监听挂在 <th> 上，
// 见 naive-ui TableParts/Header.mjs 的 th onClick），所以子节点 stopPropagation 就能挡住。
// 这个"抑制"不需要定时器解除：真实的点表头一定先有 mousedown，
// 而 mousedown 一到就解除（`onHeaderTitleMousedown`）—— 拖放的幽灵 click 没有 mousedown。
const suppressHeaderClick = ref(false)

function onHeaderDragStart(key: string, event: DragEvent) {
  dragColumnKey.value = key
  dropTargetKey.value = null
  suppressHeaderClick.value = false
  if (!event.dataTransfer) return // jsdom 里没有 dataTransfer，拖动逻辑不能因此抛错
  event.dataTransfer.effectAllowed = 'move'
  try {
    // Firefox 不 setData 不会真正进入拖动状态
    event.dataTransfer.setData('text/plain', key)
  } catch {
    // 某些环境的 dataTransfer 是只读桩
  }
}

function onHeaderDragOver(key: string, event: DragEvent) {
  const source = dragColumnKey.value
  if (!source || source === key) return
  // 不 preventDefault 就不会触发 drop —— 这是 HTML5 拖放的硬要求
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  dropTargetKey.value = key
  dropPosition.value = resolveDropPosition(columnOrder.value, source, key)
}

function onHeaderDragLeave(key: string) {
  if (dropTargetKey.value === key) dropTargetKey.value = null
}

function endHeaderDrag() {
  dragColumnKey.value = null
  dropTargetKey.value = null
  // 拖完紧跟的那一次 click 一律拦掉（见 suppressHeaderClick 的注释）
  suppressHeaderClick.value = true
}

function onHeaderDrop(key: string, event: DragEvent) {
  event.preventDefault()
  const source = dragColumnKey.value
  const position = dropTargetKey.value === key
    ? dropPosition.value
    : resolveDropPosition(columnOrder.value, source ?? '', key)
  endHeaderDrag()
  if (!source || source === key) return
  moveColumn(source, key, position) // 放下即生效，并写入本分组配置
}

function onHeaderTitleClick(event: MouseEvent) {
  if (!suppressHeaderClick.value) return
  suppressHeaderClick.value = false
  event.stopPropagation()
  event.preventDefault()
}

function onHeaderTitleMousedown() {
  // 新的按压 = 新的操作意图，允许它正常冒泡到 th 触发排序
  suppressHeaderClick.value = false
}

/** 表头标题：拖动把手 + 列名。整个标题可拖，拖动时半透明、目标列显示插入位置。 */
function renderColumnTitle(spec: WatchlistColumnSpec): VNodeChild {
  const classes = ['wl-col-drag']
  if (dragColumnKey.value === spec.key) classes.push('wl-col-drag--dragging')
  if (dropTargetKey.value === spec.key && dragColumnKey.value !== null) {
    classes.push(dropPosition.value === 'before' ? 'wl-col-drag--drop-before' : 'wl-col-drag--drop-after')
  }
  return h('span', {
    class: classes,
    draggable: 'true',
    'data-column-key': spec.key,
    title: '按住拖动可调整列顺序；点一下仍然按这列排序',
    onClick: onHeaderTitleClick,
    // 真实的「点表头排序」一定先有 mousedown（拖放结束后那次幽灵 click 没有），
    // 所以 mousedown 一到就解除抑制：用户拖完立刻点排序不会丢第一下。
    onMousedown: onHeaderTitleMousedown,
    onDragstart: (event: DragEvent) => onHeaderDragStart(spec.key, event),
    onDragover: (event: DragEvent) => onHeaderDragOver(spec.key, event),
    onDragleave: () => onHeaderDragLeave(spec.key),
    onDrop: (event: DragEvent) => onHeaderDrop(spec.key, event),
    onDragend: endHeaderDrag,
  }, [
    h('span', { class: 'wl-col-drag__grip', 'aria-hidden': 'true' }, '⠿'),
    h('span', { class: 'wl-col-drag__label' }, spec.header),
  ])
}

// L1-7（报告42）: 一键复制股票代码
async function copyStockCode(code: string) {
  try {
    await navigator.clipboard.writeText(code)
    message.success(`已复制 ${code}`)
  } catch {
    message.warning('复制失败，请手动选择复制')
  }
}

type NumericField =
  | 'latest_close' | 'pe_ttm' | 'pb_mrq' | 'roe' | 'gross_margin'
  | 'net_margin' | 'debt_ratio' | 'revenue_yoy' | 'net_profit_yoy' | 'dividend_yield'
  | 'total_market_cap' | 'circ_market_cap'

function trustedRender(field: NumericField) {
  // L0-2（报告42）: 与筛选/详情共用同一字段口径格式化
  return (row: WatchlistItem) => {
    if (isIndicatorUntrusted(field, warningCodes.value)) {
      return h(NTag, { size: 'tiny', type: 'error' }, () => '数据不可信')
    }
    return formatFieldValue(field, row[field])
  }
}

/**
 * 各列**单元格**渲染。列顺序 / 表头 / 宽度 / 是否可排序都不在这里 ——
 * 那是 `useWatchlistColumns.ts` 里 `WATCHLIST_COLUMN_CATALOG` 的职责（一个事实一份实现）。
 * 表头单位化（`fieldTitleWithUnit`）与 sorter 由那份清单带过来，这里只管"格子里画什么"。
 */
const columnRenders: Record<string, (row: WatchlistItem) => VNodeChild> = {
  // L1-7: 代码列可点进详情 + 一键复制
  stock_code: (r) => h('span', { style: 'white-space: nowrap;' }, [
    h(RouterLink, { to: `/stock/${encodeURIComponent(r.stock_code)}`, class: 'stock-link' }, { default: () => r.stock_code }),
    h(NButton, {
      text: true, size: 'tiny', type: 'primary', class: 'copy-btn',
      title: `复制 ${r.stock_code}`,
      onClick: () => void copyStockCode(r.stock_code),
    }, { default: () => '复制' }),
  ]),
  // 降级时名称取不到（DuckDB 正在更新）：用代码兜底，绝不留一列空白。
  name: (r) => r.name || r.stock_code,
  exchange: (r) => r.exchange || '—',
  // `default` 只作为后端内部 key：用户可见处一律走 displayGroupName 归一成「手动收录」。
  group_name: (r) => displayGroupName(r.group_name),
  listing_date: (r) => r.listing_date || '—',
  total_market_cap: trustedRender('total_market_cap'),
  circ_market_cap: trustedRender('circ_market_cap'),
  latest_close: trustedRender('latest_close'),
  pe_ttm: trustedRender('pe_ttm'),
  pb_mrq: trustedRender('pb_mrq'),
  roe: trustedRender('roe'),
  gross_margin: trustedRender('gross_margin'),
  net_margin: trustedRender('net_margin'),
  debt_ratio: trustedRender('debt_ratio'),
  revenue_yoy: trustedRender('revenue_yoy'),
  source: (r) => (r.source_result_id ? '筛选' : (r.source_rule_id ? '规则' : '手动')),
}

const tableColumns = computed<DataTableColumns<WatchlistItem>>(() => {
  // 首列是勾选框（2026-11-26：批量移动靠它选行）。`checked-row-keys` 只负责状态，
  // 选择列必须显式声明，否则 naive-ui 不会渲染复选框。
  const cols: DataTableColumns<WatchlistItem> = [{ type: 'selection', width: 34 }]
  for (const spec of visibleColumnSpecs.value) {
    const render = columnRenders[spec.key]
    if (!render) continue
    cols.push({
      // 函数式 title：表头文字外面包了拖动把手（点表头排序照旧走 column.sorter）
      title: () => renderColumnTitle(spec),
      key: spec.key,
      width: spec.width,
      sorter: spec.sortable ? 'default' : undefined,
      render,
    })
  }
  // 「操作」列固定在最右、不参与配置（用户说的是数据列，不是行操作）
  cols.push({
    title: '操作', key: 'actions', width: 120,
    render: (r) => h('div', [
      h(NButton, { size: 'tiny', type: 'info', onClick: () => router.push(`/stock/${r.stock_code}`) }, () => '查看'),
      h(NButton, { size: 'tiny', type: 'error', style: 'margin-left: 4px', onClick: () => confirmRemove(r.stock_code, r.group_name) }, () => '移除'),
    ]),
  })
  return cols
})

function applyResponse(data: WatchlistResponse) {
  groups.value = data.groups ?? []
  trust.value = data.trust ?? null
  autoUpdateInProgress.value = data.auto_update_in_progress === true
  enrichmentUnavailable.value = data.enrichment === 'unavailable'
  enrichmentError.value = data.enrichment_error ?? null
}

async function fetchWatchlist() {
  loading.value = true
  try {
    const params = selectedGroup.value ? { group: selectedGroup.value } : {}
    const resp = await axios.get<WatchlistResponse>('/api/watchlist/list', { params })
    // 侧栏分组先落地：它只来自 SQLite，既不依赖第二趟请求，也不依赖 DuckDB
    // 富化。2026-11-24 用户实测：此前一拍请求失败就把 groups 一起炸掉，
    // 573 行的分组被渲染成「还没有分组」——是"数据全丢了"的来源。
    applyResponse(resp.data)
    // 首次进入 / 当前分组消失（例如最后一只股票被移除）时，自动落到第一个
    // 真实规则分组再取一次数：不要停留在空的占位分组上。
    const next = resolveSelectedGroup(resp.data.groups ?? [])
    if (next !== selectedGroup.value) {
      selectedGroup.value = next
      try {
        const scoped = await axios.get<WatchlistResponse>('/api/watchlist/list', {
          params: next ? { group: next } : {},
        })
        applyResponse(scoped.data)
        items.value = scoped.data.items ?? []
      } catch (e: unknown) {
        // 第二趟失败不回滚第一趟：分组与列表都已经拿到了，先显示全部自选，
        // 别让"切换分组失败"退化成"你没有分组 / 你没有公司"。
        items.value = resp.data.items ?? []
        message.warning(friendlyErrorMessage(e, '切换分组失败，先显示全部自选'))
      }
    } else {
      items.value = resp.data.items ?? []
    }
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '加载自选失败'))
  } finally {
    loading.value = false
  }
}

function selectGroup(name: string) {
  if (selectedGroup.value === name) return
  selectedGroup.value = name
  void fetchWatchlist()
}

async function refreshGroup() {
  if (!selectedGroup.value) return
  refreshing.value = true
  try {
    const resp = await axios.post<{ status: string; added: number; removed: number; refreshed: number }>(
      '/api/watchlist/refresh',
      { group_name: selectedGroup.value },
    )
    message.success(`已按最新数据重新筛选「${displayGroupName(selectedGroup.value)}」，更新 ${resp.data.added} 只`)
    await fetchWatchlist()
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '按最新数据重新筛选失败'))
  } finally {
    refreshing.value = false
  }
}

// L0-6（报告42）: 前端先校验 6 位数字代码；后端同样强制校验
async function addStock() {
  const code = addStockCode.value.trim()
  if (!/^\d{6}$/.test(code)) {
    message.warning('请输入 6 位股票代码（如 600519）')
    return
  }
  // 目标分组来自下拉（默认 = 当前分组）；没有任何分组时落到默认组「我的自选」。
  const targetGroup = (addGroupName.value ?? '').trim() || selectedGroup.value || DEFAULT_GROUP_KEY
  try {
    await axios.post('/api/watchlist/add', { stock_code: code, group_name: targetGroup })
    message.success(`已添加 ${code} 到「${displayGroupName(targetGroup)}」`)
    addStockCode.value = ''
    // 加完直接看这只票落到的分组，避免"加成功但列表里看不见"。
    selectedGroup.value = targetGroup
    await fetchWatchlist()
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '添加失败'))
  }
}

async function removeStock(code: string, group: string) {
  try {
    await axios.delete('/api/watchlist/remove', { data: { stock_code: code, group_name: group } })
    message.success(`已移除 ${code}`)
    fetchWatchlist()
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '移除失败'))
  }
}

// L0-6（报告42）: 移除前确认，防止误删
function confirmRemove(code: string, group: string) {
  dialog.warning({
    title: '移出自选',
    content: `确定将 ${code} 从「${displayGroupName(group)}」移除吗？`,
    positiveText: '确认移除',
    negativeText: '取消',
    onPositiveClick: () => void removeStock(code, group),
  })
}

/** 对话框里「是否会连带删规则」的说明（没有规则 / 唯一规则 / 多个规则三种口径）。 */
const deleteGroupRuleHint = computed(() => {
  const target = deleteGroupTarget.value
  if (!target) return ''
  if (target.rule_ids.length === 0) {
    return '该分组没有关联筛选规则，本次只会删除分组里的股票。'
  }
  if (target.rule_ids.length > 1) {
    return `该分组来自 ${target.rule_ids.length} 个不同筛选规则（${target.rule_ids.map((id) => `#${id}`).join('、')}）；`
      + '为避免误删，只删除分组，不删除任何规则。'
  }
  return `该分组来自筛选规则 #${target.rule_ids[0]}；勾选后会先删除分组，再尝试删除该规则。`
    + '若规则仍被「已保存的筛选结果」引用，规则会保留，并在这里如实告诉你原因。'
})

/** 守卫拒绝删规则时，把「哪类引用、几条」拼进对话框正文。 */
function ruleSkipMessage(skipped: RuleSkipInfo): string {
  const refs = skipped.refs
  const counts: string[] = []
  if (refs?.results) counts.push(`已保存的筛选结果 ${refs.results} 条`)
  if (refs?.watchlist) counts.push(`其它分组的自选记录 ${refs.watchlist} 条`)
  return counts.length ? `${skipped.message}（当前引用：${counts.join('、')}）` : skipped.message
}

async function openDeleteGroup(group: WatchlistGroup) {
  deleteGroupTarget.value = {
    group_name: group.group_name,
    cnt: group.cnt,
    runs: group.runs ?? 0,
    manual: group.manual ?? 0,
    rule_ids: group.rule_ids ?? [],
  }
  deleteGroupRule.value = false
  deleteGroupError.value = null
  showDeleteGroup.value = true
  // 侧栏计数来自 `/list`，规则来源要看 `/groups` 的 rule_ids：只有一次额外读取，
  // 读到了才允许勾选「同时删除规则」，读不到就只删分组（绝不猜规则 id）。
  deleteGroupLoading.value = true
  try {
    const resp = await axios.get<{ groups: WatchlistGroup[] }>('/api/watchlist/groups')
    const detail = (resp.data.groups ?? []).find((g) => g.group_name === group.group_name)
    if (detail) {
      deleteGroupTarget.value = {
        group_name: detail.group_name,
        cnt: detail.cnt,
        runs: detail.runs ?? 0,
        manual: detail.manual ?? 0,
        rule_ids: detail.rule_ids ?? [],
      }
    }
  } catch {
    deleteGroupError.value = '无法读取该分组的规则来源，本次只能删除分组、不能删除规则。'
  } finally {
    deleteGroupLoading.value = false
  }
}

async function confirmDeleteGroup() {
  const target = deleteGroupTarget.value
  if (!target) return
  deletingGroup.value = true
  deleteGroupError.value = null
  try {
    const resp = await axios.delete<DeleteGroupResponse>(
      `/api/watchlist/groups/${encodeURIComponent(target.group_name)}`,
      { params: { delete_rule: deleteGroupRule.value } },
    )
    const data = resp.data
    let text = `已删除自选分组「${displayGroupName(target.group_name)}」，共 ${data.deleted_rows} 只`
    if (data.rule_deleted) text += `；同时删除了筛选规则 #${data.deleted_rule_id}`
    message.success(text)
    const skipped = data.skipped_rules?.[0]
    if (!data.rule_deleted && data.rule_delete_status === 'skipped' && skipped) {
      // 分组已经删了、规则没删：必须单独说清楚原因（引用类型 + 条数），
      // 不能让用户以为规则也一起没了。
      dialog.warning({
        title: '分组已删除，但筛选规则没有删除',
        content: ruleSkipMessage(skipped),
        positiveText: '知道了',
      })
    } else if (!data.rule_deleted && data.rule_delete_status === 'no_rule') {
      message.info('该分组没有关联筛选规则，只删除了分组')
    }
    showDeleteGroup.value = false
    await fetchWatchlist()
  } catch (e: unknown) {
    deleteGroupError.value = friendlyErrorMessage(e, '删除分组失败')
  } finally {
    deletingGroup.value = false
  }
}

// ── 自建分组（2026-11-26）──────────────────────────────────────────────

/** 新建空分组：成功后立刻出现在侧栏（`cnt=0`），并直接切过去。 */
async function createGroup(): Promise<void> {
  const name = newGroupName.value.trim()
  if (!name) {
    message.warning('请先输入分组名，例如「重点观察」')
    return
  }
  creatingGroup.value = true
  try {
    const resp = await axios.post<{ group: WatchlistGroup }>('/api/watchlist/groups', { name })
    const created = resp.data.group
    newGroupName.value = ''
    // 新建的分组马上可以当移动/添加目标（下拉里立刻可选）
    moveTarget.value = created.group_name
    addGroupName.value = created.group_name
    message.success(`已新建分组「${created.group_name}」，选中股票后可以移动进来`)
    await fetchWatchlist()
    selectedGroup.value = created.group_name
    await fetchWatchlist()
  } catch (e: unknown) {
    // 409 重名 / 400 空名超长：后端已给中文原因，原样展示
    message.error(friendlyErrorMessage(e, '新建分组失败'))
  } finally {
    creatingGroup.value = false
  }
}

// ── 分组改名（2026-11-26：和删除同级；默认组不显示这两个入口）─────────

function openRenameGroup(group: WatchlistGroup): void {
  renameTarget.value = group
  renameName.value = displayGroupName(group.group_name)
  renameError.value = null
  showRenameGroup.value = true
}

async function confirmRenameGroup(): Promise<void> {
  const target = renameTarget.value
  if (!target) return
  const next = renameName.value.trim()
  if (!next) {
    renameError.value = '请先输入新的分组名'
    return
  }
  renamingGroup.value = true
  renameError.value = null
  try {
    await axios.patch(
      `/api/watchlist/groups/${encodeURIComponent(target.group_name)}`,
      { new_name: next },
    )
    message.success(`分组「${displayGroupName(target.group_name)}」已改名为「${next}」`)
    // 跟随改名：当前选中分组 / 添加目标 / 移动目标都指向同一个分组，不能指丢
    if (selectedGroup.value === target.group_name) selectedGroup.value = next
    if (moveTarget.value === target.group_name) moveTarget.value = next
    if (addGroupName.value === target.group_name) addGroupName.value = next
    showRenameGroup.value = false
    await fetchWatchlist()
  } catch (e: unknown) {
    // 失败原因**留在对话框里**（409 重名 / 404 已被删 / 400 默认组保护）
    renameError.value = friendlyErrorMessage(e, '改名失败')
  } finally {
    renamingGroup.value = false
  }
}

// ── 批量复制到分组（2026-11-27：移动语义会把规则分组搞乱，改复制）────────

function onMoveTargetChange(value: string): void {
  if (value === MOVE_TO_NEW) {
    // 下拉里的「新建分组…」：清空选择、把焦点交给左侧新建输入框
    moveTarget.value = null
    const input = document.querySelector<HTMLInputElement>('.new-group-input input')
    input?.focus()
    return
  }
  moveTarget.value = value
}

async function moveSelected(): Promise<void> {
  const rows = selectedRows.value
  if (!rows.length) {
    message.warning('请先勾选要移动的股票')
    return
  }
  const target = moveTarget.value
  if (!target) {
    message.warning('请选择要移动到的分组（没有合适的可以先在左侧新建）')
    return
  }
  // 按来源分组分批提交：后端 `POST /move` 的批量口径是 (from_group → to_group)。
  const byGroup = new Map<string, string[]>()
  for (const row of rows) {
    if (row.group_name === target) continue
    const codes = byGroup.get(row.group_name) ?? []
    codes.push(row.stock_code)
    byGroup.set(row.group_name, codes)
  }
  if (byGroup.size === 0) {
    message.info(`选中的股票都已在「${displayGroupName(target)}」里`)
    return
  }
  moving.value = true
  let copied = 0
  try {
    for (const [fromGroup, codes] of byGroup) {
      // 2026-11-27（用户裁决）：从**规则分组**往自己的分组里放是**复制**，不是移动。
      // 移动会引出无解的语义问题：「下一轮筛选要不要把移出去的再移回来？」
      // 「被规则踢出之后又该怎么办？」——复制则两边互不干扰：
      // 规则分组永远由规则运行结果决定，自己的分组永远由自己决定。
      const resp = await axios.post<{ copied?: number }>('/api/watchlist/move', {
        stock_codes: codes,
        from_group: fromGroup,
        to_group: target,
        mode: 'copy',
      })
      copied += resp.data?.copied ?? codes.length
    }
    message.success(
      `已复制 ${copied} 只到「${displayGroupName(target)}」（原分组保留）`,
    )
    checkedRowKeys.value = []
    await fetchWatchlist()
  } catch (e: unknown) {
    message.error(friendlyErrorMessage(e, '复制到分组失败'))
  } finally {
    moving.value = false
  }
}

onMounted(fetchWatchlist)
</script>

<template>
  <section class="watchlist-page">
    <header class="watchlist-header"><div><p>研究工具 / 自选列表</p><h1>我的自选</h1><span>按手动收录与筛选规则来源组织研究中的公司。</span></div></header>
    <div class="watchlist-layout">
      <aside class="watchlist-groups">
        <div class="groups-heading"><p>自选列表</p><h2>公司分组</h2></div>
        <div v-for="group in visibleGroups" :key="group.group_name" class="group-row">
          <n-button class="group-select" :class="{ selected: selectedGroup === group.group_name }" text block @click="selectGroup(group.group_name)"><span><b>{{ displayGroupName(group.group_name) }}</b><small>{{ isManualGroup(group.group_name) ? '手动加入，不关联筛选规则' : '筛选规则来源' }}</small></span><em>{{ group.cnt }}</em></n-button>
          <!-- 2026-11-22（用户反馈：按钮压在分组名上）：删除**不占行内宽度**，
               改成右上角的小按钮，只在鼠标悬停/键盘聚焦时出现。
               `.group-select` 右侧留 34px 给它，所以长分组名不会再被它挤出去。 -->
          <!-- 重命名 / 删除同级放在角落；**默认分组不显示这两个入口**
               （后端也会 400 拒绝：`我的自选` 不可删、不可改名）。 -->
          <span v-if="!isDefaultGroup(group)" class="group-actions">
            <button
              class="group-rename"
              type="button"
              :title="`重命名分组「${displayGroupName(group.group_name)}」`"
              :aria-label="`重命名分组「${displayGroupName(group.group_name)}」`"
              @click.stop="openRenameGroup(group)"
            >改名</button>
            <button
              class="group-delete"
              type="button"
              :title="`删除分组「${displayGroupName(group.group_name)}」`"
              :aria-label="`删除分组「${displayGroupName(group.group_name)}」`"
              @click.stop="openDeleteGroup(group)"
            >删除</button>
          </span>
        </div>
        <p v-if="!hasVisibleGroups && !loading" class="groups-empty">还没有分组，去筛选规则页运行规则并保存到自选即可生成。</p>
        <!-- 新建分组（2026-11-26）：输入名字 → 立刻出现一个 0 只的空分组 -->
        <div class="new-group">
          <n-input
            v-model:value="newGroupName"
            class="new-group-input"
            placeholder="新分组名（如：重点观察）"
            aria-label="新分组名"
            size="small"
            @keyup.enter="createGroup"
          />
          <n-button size="small" :loading="creatingGroup" @click="createGroup">+ 新建分组</n-button>
        </div>
        <div class="manual-add">
          <n-input v-model:value="addStockCode" placeholder="输入股票代码" aria-label="股票代码" size="small" @keyup.enter="addStock" />
          <n-select
            v-model:value="addGroupName"
            :options="groupOptions"
            placeholder="分组名（默认：我的自选）"
            aria-label="分组名"
            size="small"
            clearable
          />
          <n-button size="small" @click="addStock">添加股票</n-button>
        </div>
      </aside>
      <section class="watchlist-content">
        <n-alert v-if="enrichmentUnavailable" type="info" :show-icon="true" class="watchlist-warning">{{ enrichmentNotice }}<n-button size="tiny" quaternary :loading="loading" @click="fetchWatchlist">刷新列表</n-button></n-alert>
        <n-alert v-else-if="autoUpdateInProgress" type="info" :show-icon="true" class="watchlist-warning">数据正在自动更新，以下指标为更新前快照，更新完成后自动恢复。详见<router-link to="/data-status">数据状态页</router-link>。</n-alert>
        <n-alert v-if="hasUntrustedIndicators" type="warning" :show-icon="true" class="watchlist-warning">当前数据库状态不可信，指标数值已被服务端遮蔽。请先检查<router-link to="/data-status">数据状态页</router-link>。</n-alert>
        <div class="watchlist-card">
          <template v-if="hasVisibleGroups">
            <div class="watchlist-card-heading"><div><p>{{ selectedIsManual ? '手动加入' : '筛选规则来源' }}</p><h2>{{ selectedGroupLabel }}</h2><span>{{ selectedIsManual ? '手动加入或未关联筛选来源的公司。' : '来自该筛选规则的已保存结果。' }}</span></div><div class="watchlist-actions"><span v-if="items.length > 0">显示 {{ items.length }} 家公司</span><n-button v-if="hasRefreshableRule" size="small" :loading="refreshing" @click="refreshGroup">按最新数据重新筛选</n-button><n-button v-if="items.length > 0" size="small" @click="showColumnSettings = !showColumnSettings">列设置</n-button></div></div>
            <div v-if="showColumnSettings && items.length > 0" class="watchlist-toolbar">
              <div class="column-settings-head">
                <p class="column-settings-hint">按住表头可拖动调整列顺序，放下即保存到「{{ selectedGroupLabel }}」这个分组；不常看的列可以取消勾选，随时勾回来。</p>
                <div class="column-settings-actions">
                  <n-button size="tiny" quaternary @click="showAllColumns">全部显示</n-button>
                  <n-button size="tiny" quaternary @click="resetColumns">恢复默认</n-button>
                </div>
              </div>
              <ul class="column-settings-list">
                <li v-for="(spec, index) in orderedColumnSpecs" :key="spec.key" :class="{ 'is-hidden': !visibleColumnKeySet.has(spec.key) }">
                  <n-checkbox :checked="visibleColumnKeySet.has(spec.key)" :label="spec.label" size="small" @update:checked="(checked: boolean) => toggleColumnVisible(spec.key, checked)" />
                  <span class="column-settings-move">
                    <n-button text size="tiny" :disabled="index === 0" :title="`上移「${spec.label}」`" @click="shiftColumn(spec.key, -1)">▲</n-button>
                    <n-button text size="tiny" :disabled="index === orderedColumnSpecs.length - 1" :title="`下移「${spec.label}」`" @click="shiftColumn(spec.key, 1)">▼</n-button>
                  </span>
                </li>
              </ul>
              <p class="column-settings-hint">「操作」列固定在最右，不参与配置。顺序与勾选按分组记住，刷新页面也还在。</p>
            </div>
            <!-- 批量移动（2026-11-26）：勾选若干只 → 下拉选目标分组 → 移动。
                 下拉里含「新建分组…」（跳回左侧新建），所以"新建 + 移动"是一条顺路。 -->
            <div v-if="items.length > 0" class="move-bar">
              <span class="move-bar-count">已选 {{ selectedRows.length }} 只</span>
              <n-select
                class="move-bar-select"
                :value="moveTarget"
                :options="[...groupOptions, { label: '＋ 新建分组…', value: MOVE_TO_NEW }]"
                placeholder="复制到哪个分组"
                aria-label="复制到分组"
                size="small"
                :disabled="selectedRows.length === 0"
                @update:value="onMoveTargetChange"
              />
              <n-button
                size="small"
                type="primary"
                :loading="moving"
                :disabled="selectedRows.length === 0 || !moveTarget"
                @click="moveSelected"
              >复制到分组</n-button>
            </div>
            <n-data-table
              v-if="items.length > 0"
              :columns="tableColumns"
              :data="items"
              :row-key="rowKeyOf"
              v-model:checked-row-keys="checkedRowKeys"
              :pagination="{ pageSize: 50 }"
              :scroll-x="1240"
              size="small"
              striped
            />
            <n-empty v-else-if="!loading" class="watchlist-empty" :description="emptyDescription"><template #extra><div class="watchlist-empty-extra"><p class="watchlist-empty-hint">去筛选规则页跑一次规则，把命中结果保存到自选，就能在这里看到公司。</p><div class="watchlist-empty-actions"><router-link class="watchlist-empty-link" to="/screening">去筛选规则页</router-link><n-button size="small" quaternary :loading="loading" @click="fetchWatchlist">刷新列表</n-button></div></div></template></n-empty>
            <p v-else class="watchlist-empty-hint watchlist-loading">正在加载自选…</p>
          </template>
          <n-empty v-else-if="!loading" class="watchlist-empty" :description="emptyDescription"><template #extra><div class="watchlist-empty-extra"><p class="watchlist-empty-hint">自选分组来自筛选规则的保存结果。先到筛选规则页跑一条规则，把命中结果保存到自选，就会生成对应分组；也可以直接在左侧输入股票代码收进「手动收录」。</p><div class="watchlist-empty-actions"><router-link class="watchlist-empty-link" to="/screening">去筛选规则页</router-link><n-button size="small" quaternary :loading="loading" @click="fetchWatchlist">刷新列表</n-button></div></div></template></n-empty>
          <p v-else class="watchlist-empty-hint watchlist-loading">正在加载分组…</p>
        </div>
      </section>
    </div>

    <!-- 重命名分组（2026-11-26）：默认分组没有这个入口（后端也会拒绝）。 -->
    <n-modal v-model:show="showRenameGroup" preset="dialog" title="重命名分组" style="width: 460px">
      <template v-if="renameTarget">
        <p class="delete-group-summary">
          把「<b>{{ displayGroupName(renameTarget.group_name) }}</b>」改成：
        </p>
        <n-input
          v-model:value="renameName"
          placeholder="新分组名"
          aria-label="新分组名"
          size="small"
          @keyup.enter="confirmRenameGroup"
        />
        <p class="delete-group-hint">
          该分组里的 {{ renameTarget.cnt }} 只股票会一起搬到新名字下，不会丢失。
        </p>
        <n-alert v-if="renameError" type="error" :show-icon="true" class="delete-group-error">
          {{ renameError }}
        </n-alert>
      </template>
      <template #action>
        <n-button @click="showRenameGroup = false">取消</n-button>
        <n-button type="primary" :loading="renamingGroup" @click="confirmRenameGroup">保存新名字</n-button>
      </template>
    </n-modal>

    <!-- 删除整个自选分组（2026-11-25 新增）。说明会删多少只、是否连带删规则；
         规则删不掉时（被已保存结果引用）把后端原因原样展示。 -->
    <n-modal v-model:show="showDeleteGroup" preset="dialog" title="删除自选分组" style="width: 560px">
      <template v-if="deleteGroupTarget">
        <p class="delete-group-summary">
          确定删除自选分组「<b>{{ displayGroupName(deleteGroupTarget.group_name) }}</b>」吗？
        </p>
        <p class="delete-group-detail">
          将删除该分组的 <b>{{ deleteGroupTarget.cnt }}</b> 只股票
          （其中手动添加 {{ deleteGroupTarget.manual }} 只、来自
          {{ deleteGroupTarget.runs }} 次历史筛选运行）。
          只影响这个分组，其它分组不受影响。
        </p>
        <n-checkbox
          v-model:checked="deleteGroupRule"
          :disabled="deleteGroupTarget.rule_ids.length !== 1 || deleteGroupLoading"
        >
          同时删除对应的筛选规则（若有）
        </n-checkbox>
        <p class="delete-group-hint">{{ deleteGroupRuleHint }}</p>
        <n-alert v-if="deleteGroupError" type="error" :show-icon="true" class="delete-group-error">
          {{ deleteGroupError }}
        </n-alert>
      </template>
      <template #action>
        <n-button @click="showDeleteGroup = false">取消</n-button>
        <n-button type="error" :loading="deletingGroup" @click="confirmDeleteGroup">删除分组</n-button>
      </template>
    </n-modal>

  </section>
</template>

<style scoped>
.watchlist-page { max-width: 1380px; }.watchlist-header { margin-bottom: 27px; }.watchlist-header p, .watchlist-card-heading p, .groups-heading p { margin: 0; color: #97a199; font-size: 10px; }.watchlist-header h1 { margin: 8px 0 0; font-size: 25px; letter-spacing: -.05em; }.watchlist-header span { display: block; margin-top: 7px; color: #829087; font-size: 12px; }.watchlist-layout { display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 21px; }.watchlist-groups, .watchlist-card { border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.watchlist-groups { align-self: start; padding: 22px 14px; }.groups-heading { padding: 0 10px 13px; border-bottom: 1px solid #edf1ee; }.groups-heading h2 { margin: 7px 0 0; font-size: 17px; }.watchlist-groups :deep(.group-select) { justify-content: space-between; height: auto; padding: 11px 10px; border-radius: 9px; color: #6e7d73; text-align: left; }.watchlist-groups :deep(.group-select.selected) { background: #eff8f1; color: #55966d; }.watchlist-groups :deep(.group-select .n-button__content) { display: flex; justify-content: space-between; width: 100%; }
/* 分组行：删除按钮**脱离文档流**放在右上角，只占视觉角落、不占文字宽度。
   用户口径：「删除不应该直接放在名字后面……做得小一点、隐藏一点，放在某个角落」。 */
.group-row { position: relative; margin-top: 5px; }
.group-row .group-select { width: 100%; padding-right: 34px; }
/* 分组名过长时省略，绝不与右上角按钮重叠 */
.group-row .group-select b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 改名与删除并排放在角落：两个都只在悬停/聚焦时出现 */
.group-row .group-actions { display: inline-flex; gap: 2px; }
.group-row .group-rename {
  position: absolute; top: 5px; right: 34px;
  height: 18px; padding: 0 4px;
  border: 0; border-radius: 6px;
  background: transparent; color: #6f8f7d;
  font-size: 9px; line-height: 18px; white-space: nowrap;
  cursor: pointer;
  opacity: 0;
  transition: opacity .12s ease, background .12s ease;
}
.group-row:hover .group-rename,
.group-row:focus-within .group-rename { opacity: 1; }
.group-row .group-rename:hover { background: #eff6f1; }
.group-row .group-delete {
  position: absolute; top: 5px; right: 5px;
  height: 18px; padding: 0 4px;
  border: 0; border-radius: 6px;
  background: transparent; color: #c05b5b;
  font-size: 9px; line-height: 18px; white-space: nowrap;
  cursor: pointer;
  opacity: 0;                      /* 平时隐藏：自选列表不是用来删的 */
  transition: opacity .12s ease, background .12s ease;
}
.group-row:hover .group-delete,
.group-row:focus-within .group-delete { opacity: 1; }
.group-row .group-delete:hover { background: #fdeeee; }
/* 悬停时把计数徽标让开一点，避免与角落按钮视觉相撞 */
.group-row:hover .group-select em { margin-right: 16px; }
.delete-group-summary { margin: 0 0 10px; color: #39463d; font-size: 14px; }
.delete-group-detail { margin: 0 0 12px; color: #6e7d73; font-size: 12px; line-height: 1.8; }
.delete-group-hint { margin: 8px 0 0; color: #97a199; font-size: 11px; line-height: 1.7; }
.delete-group-error { margin-top: 12px; }.watchlist-groups small, .watchlist-groups b { display: block; }.watchlist-groups small { margin-top: 4px; color: #99a39c; font-size: 9px; }.watchlist-groups em { display: grid; width: 22px; height: 22px; place-items: center; border-radius: 50%; background: #f1f5f2; color: #7c8c81; font-size: 10px; font-style: normal; }.new-group { display: grid; gap: 6px; margin-top: 14px; padding-top: 14px; border-top: 1px solid #edf1ee; }
.manual-add { display: grid; gap: 8px; margin-top: 10px; }
/* 批量移动条：勾选后才有内容，平时只是窄窄一条，不挤表格 */
.move-bar { display: flex; align-items: center; gap: 8px; margin: 0 0 12px; }
.move-bar-count { color: #6e7d73; font-size: 11px; white-space: nowrap; }
.move-bar-select { width: 190px; }.watchlist-card { padding: 28px 29px; }.watchlist-card-heading { display: flex; justify-content: space-between; gap: 20px; }.watchlist-card-heading h2 { margin: 7px 0 5px; font-size: 19px; }.watchlist-card-heading span { color: #829087; font-size: 11px; }.watchlist-actions { display: flex; align-items: center; gap: 10px; white-space: nowrap; }.watchlist-toolbar { margin: 20px 0; padding: 12px; border-radius: 8px; background: #fafcf9; }.watchlist-warning { margin-bottom: 16px; }.watchlist-empty { padding: 40px; }.groups-empty { margin: 14px 10px 0; color: #99a39c; font-size: 11px; line-height: 1.7; }.watchlist-empty-extra { display: grid; gap: 12px; justify-items: center; max-width: 460px; }.watchlist-empty-hint { margin: 0; color: #829087; font-size: 12px; line-height: 1.7; text-align: center; }.watchlist-empty-actions { display: flex; align-items: center; gap: 12px; }.watchlist-empty-link { display: inline-flex; align-items: center; height: 28px; padding: 0 14px; border-radius: 6px; background: #eff8f1; color: #55966d; font-size: 13px; text-decoration: none; }.watchlist-empty-link:hover { background: #e2f2e7; }.watchlist-loading { padding: 40px; color: #99a39c; }
/* 列设置面板（勾选显示哪些列 + ▲▼ 调顺序 + 全部显示/恢复默认） */
.column-settings-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.column-settings-actions { display: flex; flex: none; gap: 6px; white-space: nowrap; }
.column-settings-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(148px, 1fr)); gap: 4px 12px; margin: 10px 0; padding: 0; list-style: none; }
.column-settings-list li { display: flex; align-items: center; justify-content: space-between; gap: 6px; padding: 2px 6px; border-radius: 6px; }
.column-settings-list li.is-hidden { background: #f5f8f5; }
.column-settings-list li.is-hidden :deep(.n-checkbox__label) { color: #a7b1aa; }
.column-settings-move { display: inline-flex; flex: none; gap: 2px; color: #97a199; }
.column-settings-hint { margin: 0; color: #97a199; font-size: 11px; line-height: 1.6; }
/* 表头拖动把手：标题由 renderColumnTitle() 在运行时生成（不在本组件的 render 里），
   拿不到 scoped 的 data-v 属性，所以用 :deep() 从 .watchlist-page 往下命中。 */
.watchlist-page :deep(.wl-col-drag) { display: inline-flex; align-items: center; gap: 2px; max-width: 100%; padding: 1px 3px; border-radius: 4px; cursor: grab; user-select: none; transition: background-color .15s ease, box-shadow .15s ease, opacity .15s ease; }
.watchlist-page :deep(.wl-col-drag:hover) { background: #eff6f1; }
.watchlist-page :deep(.wl-col-drag:active) { cursor: grabbing; }
.watchlist-page :deep(.wl-col-drag--dragging) { opacity: .4; }
.watchlist-page :deep(.wl-col-drag__grip) { color: #bcc7bf; font-size: 9px; letter-spacing: -1px; }
.watchlist-page :deep(.wl-col-drag--drop-before) { box-shadow: inset 2px 0 0 #55966d; }
.watchlist-page :deep(.wl-col-drag--drop-after) { box-shadow: inset -2px 0 0 #55966d; }
</style>
