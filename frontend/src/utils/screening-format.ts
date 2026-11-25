/**
 * 字段级展示格式（L0-2：单位/口径统一，筛选/自选/详情同义显示）。
 *
 * 口径约定（与后端 app/core/screening/field_units.py 单一来源对齐，
 * 2026-08-14 红队 F3 修复）：
 * - pct     底层为小数比例，展示 ×100 加 %（roe、负债率、同比、
 *           net_profit_cagr3、period_return 等）；条件输入 ÷100
 * - percent 底层为百分数（5.29 = 5.29%），展示直接加 %、
 *           条件输入原样比较（ttm_dividend_yield、div_yield_spread_*、
 *           turnover_rate）
 * - ratio   本身即倍数（流动比率、速动比率、利息保障倍数等）
 * - price   价格，保留 2 位小数
 * - 其余数值：走 fmt()（亿/万/小数自适应）
 * - 输入/展示单位由 fieldInputUnit 统一给出：% 、亿、倍、元、年、万股等，
 *   并附带显示值 → 底层存储值的换算系数，筛选项切换时动态更新。
 *
 * 运行时元数据（applyIndicatorUnits）来自 /api/screening/indicators 的
 * unit 字段，优先级高于下方静态集合——静态集合只是无后端元数据时的回退。
 */

import { fmt, fmtPct, fmtStoredPercent } from './formatters.ts'

export type FieldFormat = 'pct' | 'percent' | 'ratio' | 'price' | 'plain'

/** 用户界面使用中文优先名称；稳定字段名仍只用于 API、规则 JSON 与导出。 */
export const FIELD_LABELS: Readonly<Record<string, string>> = {
  // 扩展指标域（schema v25，2026-09-19）：周转率族 / 自由现金流族 / 杠杆族，共 18 项。
  // 自由现金流与 EBITDA 一族最初因 CSMAR 截止 2025-03-31 而覆盖率为 0，未上界面；
  // 现由 scripts/fetch_cashflow_supplement.py 从东方财富补齐后已全量开放。
  dilution_3y: '近3年融资稀释率',
  buyback_ratio_3y: '近3年回购占比',
  bonus_share_ratio_3y: '近3年送转比例',
  top_segment_share: '最大业务占比',
  cash_ratio: '现金比率',
  conservative_quick_ratio: '保守速动比率',
  debt_to_equity: '产权比率',
  tangible_net_debt_ratio: '有形净值债务率',
  ocf_to_liabilities: '经营现金流对负债',
  ebitda_to_liabilities: 'EBITDA对负债',
  cash_content_of_revenue: '营业收入现金含量',
  ocf_to_operating_profit: '营业利润现金净含量',
  accruals: '应计项目',
  tangible_asset_per_share: '每股有形资产',
  liability_per_share: '每股负债',
  capital_reserve_per_share: '每股资本公积',
  report_date_close: '报告期收盘价',
  market_cap_at_report: '报告期市值',
  tobin_q: '托宾Q',
  book_to_market: '账面市值比',
  ev_ebitda: '企业价值倍数（EV/EBITDA）',
  employee_count: '员工人数',
  revenue_per_employee: '人均创收',
  profit_per_employee: '人均创利',
  shareholder_occupation: '大股东占款率',
  bps: '每股净资产',
  bps_parent: '归属母公司每股净资产',
  revenue_per_share: '每股营业收入',
  ocf_per_share: '每股经营现金流',
  retained_earnings_per_share: '每股留存收益',
  selling_expense_ratio: '销售费用率',
  admin_expense_ratio: '管理费用率',
  rd_expense_ratio: '研发费用率',
  finance_expense_ratio: '财务费用率',
  current_asset_ratio: '流动资产占比',
  fixed_asset_ratio: '固定资产占比',
  equity_multiplier: '权益乘数',
  depreciation_amortization: '折旧摊销',
  capex: '资本支出',
  free_cash_flow: '自由现金流',
  fcf_margin: '自由现金流率',
  ebitda: '息税折旧摊销前利润（EBITDA）',
  leverage_operating: '经营杠杆',
  leverage_total: '综合杠杆',
  receivables_turnover: '应收账款周转率',
  inventory_turnover: '存货周转率',
  accounts_payable_turnover: '应付账款周转率',
  current_asset_turnover: '流动资产周转率',
  fixed_asset_turnover: '固定资产周转率',
  total_asset_turnover: '总资产周转率',
  equity_turnover: '股东权益周转率',
  operating_cycle_days: '营业周期（天）',
  ebit: '息税前利润（EBIT）',
  leverage_financial: '财务杠杆',
  operating_cash_flow: '经营活动现金流量净额',
  stock_code: '股票代码',
  name: '股票名称',
  exchange: '交易所',
  csrc_l1: '证监会一级行业',
  csrc_l2: '证监会二级行业',
  latest_close: '最新收盘价',
  pe_ttm: '市盈率（PE-TTM）',
  pb_mrq: '市净率（PB-MRQ）',
  ps_ttm: '市销率（PS-TTM）',
  pcf_ttm: '市现率（PCF-TTM）',
  dividend_yield: '股息率',
  total_market_cap: '总市值',
  circ_market_cap: '流通市值',
  // 0/1 伯尔列：金融股（银行/非银金融）。来自 app/core/sector.py，规则里用 `is_financial = 0` 做前置排除。
  is_financial: '是否金融股（0=非金融 / 1=金融）',
  roe: '净资产收益率（ROE）',
  roa: '总资产收益率（ROA）',
  gross_margin: '销售毛利率',
  net_margin: '销售净利率',
  roic: '投入资本回报率（ROIC）',
  roce_latest: '已动用资本回报率（ROCE·最近年报）',
  roce_5y_median: '已动用资本回报率（ROCE·5年中位）',
  roce_10y_median: '已动用资本回报率（ROCE·10年中位）',
  roce_20y_median: '已动用资本回报率（ROCE·20年中位）',
  roce_5y_years: 'ROCE·5年有效年数',
  roce_10y_years: 'ROCE·10年有效年数',
  roce_20y_years: 'ROCE·20年有效年数',
  roce_eligible: 'ROCE 可筛选（非金融·市值≥100亿·有效年数≥5）',
  cf_to_net_profit: '经营现金流/净利润',
  revenue_yoy: '营业收入同比(TTM口径)',
  net_profit_yoy: '净利润同比(TTM口径)',
  deducted_profit_yoy: '扣非净利润同比(TTM口径)',
  revenue_yoy_fy: '营业收入同比(年报口径)',
  net_profit_yoy_fy: '净利润同比(年报口径)',
  deducted_profit_yoy_fy: '扣非净利润同比(年报口径)',
  // 参数化基名（reports/143 §2）：年数由条件行的「年数」下拉选择，
  // 所以名字里不再写死 3/5 年。**新增字段必须同时补这里**，
  // 否则界面会出现纯英文（reports/143 §1 的 I-LABELCN 教训）。
  // 结果表头里的元数据列（2026-11-16：用户截图看到表头是 listing_date 这种纯英文）
  listing_date: '上市日期',
  is_st: '是否 ST',
  is_suspended: '是否停牌',
  total_shares: '总股本',
  circ_shares: '流通股本',
  sw_level1: '申万一级行业',
  sw_level2: '申万二级行业',
  revenue_cagr: '营收复合增速',
  net_profit_cagr: '净利润复合增速',
  deducted_profit_cagr: '扣非净利复合增速',
  revenue_cagr3: '营收 3 年复合增速',
  revenue_cagr5: '营收 5 年复合增速',
  net_profit_cagr3: '净利润 3 年复合增速',
  net_profit_cagr5: '净利润 5 年复合增速',
  deducted_profit_cagr3: '扣非净利 3 年复合增速',
  deducted_profit_cagr5: '扣非净利 5 年复合增速',
  debt_ratio: '资产负债率',
  current_ratio: '流动比率',
  quick_ratio: '速动比率',
  interest_bearing_debt: '有息负债',
  interest_coverage: '利息保障倍数',
  goodwill_ratio: '商誉占净资产比',
  payout_ratio: '派息率',
  dps: '每股股息',
  consecutive_div_years: '连续分红年数',
  cumulative_dividend_amount: 'A股累计现金分红（含回购注销）',
  cumulative_financing_amount: 'A股累计股权融资',
  // 两个名字同指一个中文名，但只有 `_pct` 是后端快照列（唯一权威）。裸名是历史
  // 发布型 DSL 指标名（app/web/api/screening.py:719），只保留显示名，
  // **不得**再把它登记进 PERCENT_FIELDS —— 那正是 2026-11-18 修掉的死键漂移。
  dividend_financing_ratio: '分红融资比（A股）',
  dividend_financing_ratio_pct: '分红融资比（A股）',
  turnover_rate: '换手率',
  ma5: '5 日均线',
  ma10: '10 日均线',
  ma20: '20 日均线',
  ma60: '60 日均线',
  ma120: '120 日均线',
  ma250: '250 日均线',
  avg_volume: '平均成交量',
  period_return: '区间收益率',
  annualized_volatility: '年化波动率',
  max_drawdown: '最大回撤',
  ttm_dividend_yield: '股息率（TTM）',
  // 期限由条件行的「国债期限」下拉选择
  div_yield_spread: '股息率-国债利差',
  div_yield_spread_0p25y: '股息率-国债0.25年利差',
  div_yield_spread_0p5y: '股息率-国债0.5年利差',
  div_yield_spread_1y: '股息率-国债1年利差',
  div_yield_spread_2y: '股息率-国债2年利差',
  div_yield_spread_3y: '股息率-国债3年利差',
  div_yield_spread_5y: '股息率-国债5年利差',
  div_yield_spread_7y: '股息率-国债7年利差',
  div_yield_spread_10y: '股息率-国债10年利差',
  div_yield_spread_30y: '股息率-国债30年利差',
}

/** 排名类字段的展示后缀（指标 + 排名）。 */
const RANK_SUFFIX_LABELS: Readonly<Record<string, string>> = {
  market_rank: '全市场排名',
  market_percentile: '全市场分位',
  industry_rank: '证监会一级排名',
  industry_percentile: '证监会一级分位',
  // 2026-11-16（reports/148）：后端 RANK_SUFFIXES 有 8 条，前端只有 6 条 ——
  // 漏了 sw1_*，导致 `pe_ttm_sw1_rank` 这类历史字段解析失败、界面显示纯英文。
  // 注意：sw1_* 已不在 RANK_MODES（方案 C 只留 6 个口径），所以它们**只可能**
  // 出现在历史规则/草稿里，靠这两条标签兜底显示中文即可，不提供新建入口。
  sw1_rank: '申万一级排名',
  sw1_percentile: '申万一级分位',
  sw2_rank: '证监会二级排名',
  sw2_percentile: '证监会二级分位',
}

/** P4 历史统计字段（已发布统计域）指标与窗口中文名。 */
const STAT_METRIC_LABELS: Readonly<Record<string, string>> = {
  pe_ttm: '市盈率（PE-TTM）',
  pb_mrq: '市净率（PB-MRQ）',
  ttm_dividend_yield: '股息率（TTM）',
  spread_10y: '股息率-国债10年利差',
}
const STAT_WINDOW_LABELS: Readonly<Record<string, string>> = {
  '1': '1年',
  '3': '3年',
  '5': '5年',
  '10': '10年',
  '99': '全部历史',
}
const STAT_METHOD_LABELS: Readonly<Record<string, string>> = {
  percentile: '历史分位',
  zscore: 'z-score',
}

/** 标准化财务表前缀。 */
const STATEMENT_PREFIX_LABELS: Readonly<Record<string, string>> = {
  balance: '资产负债表',
  income: '利润表',
  cashflow: '现金流量表',
}

/** 资产负债表字段（标准化财务，与后端 STATEMENT_FIELDS 对齐）。 */
const BALANCE_FIELD_LABELS: Readonly<Record<string, string>> = {
  // 2026-11-16：以下字段之前缺中文名，界面会显示「资产负债表 · accumulated_depreciation」
  // 这种半英文标签，不符合 PRD §12.1「用户可见指标名称必须全中文」。
  accumulated_depreciation: '累计折旧',
  fixed_assets_gross: '固定资产原值',
  investment_property: '投资性房地产',
  development_expenditure: '开发支出',
  other_current_assets: '其他流动资产',
  receivables_financing: '应收款项融资',
  long_term_payables: '长期应付款',
  advance_from_customers: '预收款项及合同负债（合并口径）',
  advance_from_customers_ratio: '客户占款率（预收+合同负债）/总资产',
  // 负债质量三指标（段永平口径，2026-11-22）：口径写进标签，避免用户按
  // `debt_ratio`（资产负债率，含预收/应付）理解。
  interest_bearing_debt: '有息负债（短借+长借+应付债券）',
  interest_bearing_debt_ratio: '有息负债率',
  net_cash_to_assets: '净现金占总资产',
  interest_bearing_debt_to_liabilities: '有息负债占总负债',
  monetary_funds: '货币资金',
  trading_financial_assets: '交易性金融资产',
  notes_receivable: '应收票据',
  accounts_receivable: '应收账款',
  prepayments: '预付款项',
  other_receivables: '其他应收款',
  inventory: '存货',
  contract_assets: '合同资产',
  total_current_assets: '流动资产合计',
  long_term_equity_investment: '长期股权投资',
  fixed_assets: '固定资产',
  construction_in_progress: '在建工程',
  right_of_use_assets: '使用权资产',
  intangible_assets: '无形资产',
  goodwill: '商誉',
  deferred_tax_assets: '递延所得税资产',
  total_non_current_assets: '非流动资产合计',
  total_assets: '资产总计',
  short_term_loans: '短期借款',
  notes_payable: '应付票据',
  accounts_payable: '应付账款',
  prepayments_received: '预收款项',
  contract_liabilities: '合同负债',
  employee_benefits_payable: '应付职工薪酬',
  taxes_payable: '应交税费',
  total_current_liabilities: '流动负债合计',
  long_term_loans: '长期借款',
  bonds_payable: '应付债券',
  lease_liabilities: '租赁负债',
  total_non_current_liabilities: '非流动负债合计',
  total_liabilities: '负债合计',
  paid_in_capital: '实收资本',
  capital_reserve: '资本公积',
  surplus_reserve: '盈余公积',
  undistributed_profit: '未分配利润',
  minority_interest: '少数股东权益',
  total_equity: '股东权益合计',
  total_equity_parent: '归母股东权益',
  core_tier1_capital_adequacy_ratio: '核心一级资本充足率',
  tier1_capital_adequacy_ratio: '一级资本充足率',
  capital_adequacy_ratio: '资本充足率',
  non_performing_loan_ratio: '不良贷款率',
  provision_coverage_ratio: '拨备覆盖率',
  risk_coverage_ratio: '风险覆盖率',
}

/** 利润表字段。 */
const INCOME_FIELD_LABELS: Readonly<Record<string, string>> = {
  // 跨年符号断点归一后的派生口径（reports/131 P0-7）：损失一律为正。
  impairment_loss_positive: '资产减值损失（损失为正）',
  credit_impairment_loss_positive: '信用减值损失（损失为正）',
  total_operating_revenue: '营业总收入',
  revenue: '营业收入',
  total_operating_cost: '营业总成本',
  cost_of_revenue: '营业成本',
  taxes_and_surcharges: '税金及附加',
  selling_expenses: '销售费用',
  administrative_expenses: '管理费用',
  rd_expenses: '研发费用',
  financial_expenses: '财务费用',
  interest_expense: '利息费用',
  interest_income: '利息收入',
  asset_impairment_loss: '资产减值损失',
  credit_impairment_loss: '信用减值损失',
  exchange_gain: '公允价值变动收益',
  investment_income: '投资收益',
  operating_profit: '营业利润',
  non_operating_income: '营业外收入',
  non_operating_expenses: '营业外支出',
  total_profit: '利润总额',
  income_tax: '所得税费用',
  net_profit: '净利润',
  parent_net_profit: '归母净利润',
  minority_shareholder_profit: '少数股东损益',
  deducted_net_profit: '扣非净利润',
  basic_eps: '基本每股收益',
  diluted_eps: '稀释每股收益',
}

/** 现金流量表字段。 */
const CASHFLOW_FIELD_LABELS: Readonly<Record<string, string>> = {
  cash_received_sales: '销售商品收到的现金',
  taxes_refunded: '收到的税费返还',
  other_operating_cf_in: '其他经营现金流入',
  total_operating_cf_in: '经营现金流入小计',
  cash_paid_goods: '购买商品支付的现金',
  cash_paid_employees: '支付给职工的现金',
  cash_paid_taxes: '支付的各项税费',
  other_operating_cf_out: '其他经营现金流出',
  total_operating_cf_out: '经营现金流出小计',
  cf_from_operating: '经营活动现金流量净额',
  cf_from_investing: '投资活动现金流量净额',
  cf_from_financing: '筹资活动现金流量净额',
  exchange_rate_effect: '汇率变动影响',
  cf_net: '现金及等价物净增加额',
  cash_beginning: '期初现金及等价物',
  cash_ending: '期末现金及等价物',
}

const STATEMENT_FIELD_LABELS: Readonly<Record<string, Readonly<Record<string, string>>>> = {
  balance: BALANCE_FIELD_LABELS,
  income: INCOME_FIELD_LABELS,
  cashflow: CASHFLOW_FIELD_LABELS,
}

export function fieldDisplayName(field: string, fallback?: string): string {
  if (FIELD_LABELS[field]) return FIELD_LABELS[field]
  // P4 历史统计字段：<metric>_stat_<window>y_<method>
  const statMatch = field.match(/^(.+)_stat_(\d+)y_(percentile|zscore)$/)
  if (statMatch) {
    const metricLabel = STAT_METRIC_LABELS[statMatch[1]] ?? statMatch[1]
    const windowLabel = STAT_WINDOW_LABELS[statMatch[2]] ?? `${statMatch[2]}年`
    const methodLabel = STAT_METHOD_LABELS[statMatch[3]] ?? statMatch[3]
    return `${metricLabel} · ${windowLabel} · ${methodLabel}`
  }
  for (const [suffix, suffixLabel] of Object.entries(RANK_SUFFIX_LABELS)) {
    if (field.endsWith(`_${suffix}`)) {
      const base = field.slice(0, -(suffix.length + 1))
      const baseLabel = fieldDisplayName(base)
      return `${baseLabel} · ${suffixLabel}`
    }
  }
const separator = field.indexOf('.')
  if (separator > 0) {
    const table = field.slice(0, separator)
    const column = field.slice(separator + 1)
    const prefix = STATEMENT_PREFIX_LABELS[table]
    const columnLabel = STATEMENT_FIELD_LABELS[table]?.[column]
    if (prefix && columnLabel) return `${prefix} · ${columnLabel}`
    if (prefix && !columnLabel) return `${prefix} · ${column}`
  }
return fallback || field
}

/** 小数比例存储的百分比字段（条件输入 ÷100、展示 ×100+%）。 */
const PCT_FIELDS = new Set([
  // 2026-11-22 负债质量三指标（段永平口径）：与 `debt_ratio` 同族（0–1 的小数比例，
  // 0.1315 = 13.15%）→ 走 pct，展示 ×100 带 %，输入按 % 填（scale 0.01）。
  // 后端 field_units.py 的 PCT_DECIMAL_FIELDS 同批登记，RG3 双向对拍要求两边成对出现。
  'balance.interest_bearing_debt_ratio',
  'balance.net_cash_to_assets',
  'balance.interest_bearing_debt_to_liabilities',
  'fcf_margin',  // 扩展指标域：自由现金流率按小数比例存储
  'shareholder_occupation', 'book_to_market', 'accruals',
  'selling_expense_ratio', 'admin_expense_ratio', 'rd_expense_ratio',
  'finance_expense_ratio', 'current_asset_ratio', 'fixed_asset_ratio',
  'roe', 'roa', 'roic', 'roce_latest', 'roce_5y_median', 'roce_10y_median', 'roce_20y_median',
  'gross_margin', 'net_margin', 'debt_ratio',
  'revenue_yoy', 'net_profit_yoy', 'deducted_profit_yoy',
  'revenue_yoy_fy', 'net_profit_yoy_fy', 'deducted_profit_yoy_fy',
  'revenue_cagr3', 'revenue_cagr5',
  'net_profit_cagr3', 'net_profit_cagr5',
  'deducted_profit_cagr3', 'deducted_profit_cagr5',
  'dividend_yield', 'goodwill_ratio', 'payout_ratio',
  'period_return', 'annualized_volatility', 'max_drawdown',
])

/**
 * 百分数存储的百分比字段（输入原样比较、展示直接加 %）。
 *
 * 2026-11-18 retro（RG3 缺口）：这里曾多挂一个裸名 `dividend_financing_ratio`，
 * 后端根本没有这个快照列 —— 唯一权威是 `_pct`：
 * `app/core/screening/field_units.py:45`（PERCENT_STORED_FIELDS）、
 * `app/core/storage/schema.py:317`（列名）、
 * `app/core/dsl/ast_nodes.py:287`（AST 指标登记）。
 * 裸名只是历史发布型 DSL 指标的名字（`app/web/api/screening.py:719`），
 * 它的单位由 /api/screening/indicators 的运行时元数据下发，不在此静态登记。
 * 双向一致性由 `frontend/tests/screening-format.test.ts` 的「单位表双向对拍」段落钉死。
 */
const PERCENT_FIELDS = new Set([
  'dividend_financing_ratio_pct',
  'ttm_dividend_yield',
  'div_yield_spread_0p25y', 'div_yield_spread_0p5y', 'div_yield_spread_1y',
  'div_yield_spread_2y', 'div_yield_spread_3y', 'div_yield_spread_5y',
  'div_yield_spread_7y', 'div_yield_spread_10y', 'div_yield_spread_30y',
  'turnover_rate',
  // 标准化资产负债表中的银行/证券监管比率按百分数原值存储（如 12 = 12%）
  'balance.core_tier1_capital_adequacy_ratio',
  'balance.tier1_capital_adequacy_ratio',
  'balance.capital_adequacy_ratio',
  'balance.non_performing_loan_ratio',
  'balance.provision_coverage_ratio',
  'balance.risk_coverage_ratio',
])

const PRICE_FIELDS = new Set([
  'report_date_close',
  'latest_close', 'open', 'high', 'low', 'close',
  'ma5', 'ma10', 'ma20', 'ma60', 'ma120', 'ma250',
])

const RATIO_FIELDS = new Set([
  'current_ratio', 'quick_ratio', 'cf_to_net_profit', 'interest_coverage',
  // 扩展指标域：周转率与杠杆本身即倍数
  'receivables_turnover', 'inventory_turnover', 'accounts_payable_turnover',
  'current_asset_turnover', 'fixed_asset_turnover', 'total_asset_turnover',
  'equity_turnover', 'leverage_financial', 'leverage_operating', 'leverage_total',
  'equity_multiplier', 'tobin_q', 'ev_ebitda',
  'dilution_3y', 'buyback_ratio_3y', 'bonus_share_ratio_3y', 'top_segment_share',
  'cash_ratio', 'conservative_quick_ratio', 'debt_to_equity',
  // 2026-11-17（reports/133 RG3）：后端 field_units.py 把这两个登记为倍数，
  // 前端此前一个漏登记（退回默认口径）、一个错登记成小数比例（100 倍误差）。
  // `cash_content_of_revenue` = 销售商品收到的现金 / 营业收入 ≈ 1.0；
  // `balance.advance_from_customers_ratio` = (预收+合同负债) / 负债合计。
  'cash_content_of_revenue', 'balance.advance_from_customers_ratio',
  'tangible_net_debt_ratio', 'ocf_to_liabilities', 'ebitda_to_liabilities',
  'ocf_to_operating_profit',
])

/**
 * 四张静态单位表（**运行时实际使用的同一批 Set 对象**，非副本）。
 *
 * 用途只有一个：给 `frontend/tests/screening-format.test.ts` 的「单位表双向对拍」段落做
 * 「前端集合 == 后端 app/core/screening/field_units.py」的**双向**对拍。
 * RG3 只校验后端→前端单向（前端多出来的键它看不见），死键就是那样漏的。
 */
export const FIELD_UNIT_SETS: Readonly<{
  readonly pct: ReadonlySet<string>
  readonly percent: ReadonlySet<string>
  readonly ratio: ReadonlySet<string>
  readonly price: ReadonlySet<string>
}> = {
  pct: PCT_FIELDS,
  percent: PERCENT_FIELDS,
  ratio: RATIO_FIELDS,
  price: PRICE_FIELDS,
}

/** 使用“倍”作为输入/展示单位的估值倍数类指标。 */
const MULTIPLE_FIELDS = new Set([
  'pe_ttm', 'pb_mrq', 'ps_ttm', 'pcf_ttm',
])

/** 以“亿”为筛选输入单位的金额类指标（底层统一存储为元）。 */
const MONEY_FIELDS = new Set([
  'total_market_cap', 'circ_market_cap', 'interest_bearing_debt',
  'cumulative_dividend_amount', 'cumulative_financing_amount',
])

/** 以“元”为单位的每股/价格类指标（价格字段已由 fieldFormat 处理，这里补充 EPS 等）。 */
const PER_SHARE_FIELDS = new Set([
  'dps', 'basic_eps', 'diluted_eps',
])

const YEAR_FIELDS = new Set(['consecutive_div_years'])

/** 平均成交量底层单位为股；筛选输入按万股，展示更符合日常阅读。 */
const VOLUME_FIELDS = new Set(['avg_volume'])

/**
 * 标准化资产负债表中以百分数表达的银行类监管指标（裸列名，仅用于 `balance.<col>` 前缀判定）。
 *
 * 事实只存一份：`PERCENT_FIELDS` 里的 `balance.*` 全名。这里按前缀派生，
 * 不再手抄第二份"哪些监管比率按百分数存"（AGENTS R1：一个事实一个实现）。
 */
const STATEMENT_PERCENT_FIELDS = new Set(
  [...PERCENT_FIELDS]
    .filter((field) => field.startsWith('balance.'))
    .map((field) => field.slice('balance.'.length)),
)

/** 条件输入框的单位与换算元数据。 */
export interface FieldInputUnit {
  /** 输入框旁展示的单位符号，如 %、亿、倍、元、年、万股。 */
  readonly label: string
  /**
   * 用户填写的展示值 → 底层存储值的乘数。
   * 例如 pct 填 2（2%）→ 0.02，scale=0.01；
   * 总市值填 1000（1000亿）→ 1e11，scale=1e8。
   */
  readonly scale: number
  /** 可选输入提示文案。 */
  readonly hint?: string
}

/** 返回某个筛选指标的输入单位；单位随所选指标动态变化。 */
export function fieldInputUnit(field: string): FieldInputUnit {
  const format = fieldFormat(field)
  if (format === 'pct') {
    return { label: '%', scale: 0.01, hint: '百分数，如 2 = 2%' }
  }
  if (format === 'percent') {
    return { label: '%', scale: 1, hint: '百分数，如 2 = 2%' }
  }
  if (format === 'price') {
    return { label: '元', scale: 1 }
  }
  if (format === 'ratio') {
    return { label: '倍', scale: 1 }
  }

  if (MULTIPLE_FIELDS.has(field)) return { label: '倍', scale: 1 }
  if (MONEY_FIELDS.has(field)) {
    return { label: '亿', scale: 1e8, hint: '填写亿，如 1000 = 1000亿' }
  }
  if (PER_SHARE_FIELDS.has(field)) return { label: '元', scale: 1 }
  if (YEAR_FIELDS.has(field)) return { label: '年', scale: 1 }
  if (VOLUME_FIELDS.has(field)) {
    return { label: '万股', scale: 1e4, hint: '填写万股' }
  }

  // 排名/分位等派生字段
  if (/_(market_rank|industry_rank|sw1_rank|sw2_rank)$/.test(field)) {
    return { label: '名', scale: 1 }
  }
  if (/_(market_percentile|industry_percentile|sw1_percentile|sw2_percentile)$/.test(field)) {
    return { label: '分位', scale: 1 }
  }
  if (/^.+_stat_\d+y_percentile$/.test(field)) {
    return { label: '分位', scale: 1 }
  }
  if (/^.+_stat_\d+y_zscore$/.test(field)) {
    return { label: 'σ', scale: 1 }
  }

  // 标准化财务报表字段
  const separator = field.indexOf('.')
  if (separator > 0) {
    const table = field.slice(0, separator)
    const column = field.slice(separator + 1)
    if (table === 'balance' && STATEMENT_PERCENT_FIELDS.has(column)) {
      return { label: '%', scale: 1, hint: '百分数，如 2 = 2%' }
    }
    if (table === 'income' && (column === 'basic_eps' || column === 'diluted_eps')) {
      return { label: '元', scale: 1 }
    }
    return { label: '亿', scale: 1e8, hint: '填写亿' }
  }

  // 未知/自定义指标：不猜测单位，按原值输入
  return { label: '', scale: 1 }
}

/** 后端下发的运行时单位元数据（优先级最高，单一来源）。 */
const RUNTIME_UNITS = new Map<string, FieldFormat>()

/** 应用 /api/screening/indicators 返回的 unit 元数据。 */
export function applyIndicatorUnits(units: Readonly<Record<string, string>>): void {
  for (const [field, unit] of Object.entries(units)) {
    if (unit === 'pct' || unit === 'percent' || unit === 'ratio' || unit === 'price' || unit === 'plain') {
      RUNTIME_UNITS.set(field, unit)
    }
  }
}

export function fieldFormat(field: string): FieldFormat {
  const runtime = RUNTIME_UNITS.get(field)
  if (runtime) return runtime
  if (PERCENT_FIELDS.has(field)) return 'percent'
  if (PCT_FIELDS.has(field)) return 'pct'
  if (PRICE_FIELDS.has(field)) return 'price'
  if (RATIO_FIELDS.has(field)) return 'ratio'
  return 'plain'
}

/** 按字段语义格式化筛选/自选表格单元格。null/undefined → '—'。 */
export function formatFieldValue(field: string, value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value !== 'number') return String(value)
  switch (fieldFormat(field)) {
    case 'pct':
      return fmtPct(value)
    case 'percent':
      // 百分数存储：直接展示（5.29 = 5.29%），不再 ×100
      return fmtStoredPercent(value)
    case 'price':
    case 'ratio':
      return value.toFixed(2)
    default: {
      const abs = Math.abs(value)
      if (abs < 0.01 && value !== 0) return value.toExponential(2)
      // 亿/万 阈值 + 两位小数的单一来源在 formatters.ts（与 StockDetailPage 同口径）
      return fmt(value)
    }
  }
}

/** 常见字段的表头单位标注（"表头带单位"）。 */
export const FIELD_UNITS: Readonly<Record<string, string>> = {
  dividend_yield: '(%)',
  roe: '(%)',
  roa: '(%)',
  roic: '(%)',
  gross_margin: '(%)',
  net_margin: '(%)',
  debt_ratio: '(%)',
  revenue_yoy: '(%)',
  net_profit_yoy: '(%)',
  deducted_profit_yoy: '(%)',
  revenue_yoy_fy: '(%)',
  net_profit_yoy_fy: '(%)',
  deducted_profit_yoy_fy: '(%)',
  revenue_cagr3: '(%)',
  revenue_cagr5: '(%)',
  net_profit_cagr3: '(%)',
  net_profit_cagr5: '(%)',
  deducted_profit_cagr3: '(%)',
  deducted_profit_cagr5: '(%)',
  goodwill_ratio: '(%)',
  payout_ratio: '(%)',
  period_return: '(%)',
  annualized_volatility: '(%)',
  max_drawdown: '(%)',
  turnover_rate: '(%)',
  ttm_dividend_yield: '(%)',
  div_yield_spread_0p25y: '(%)',
  div_yield_spread_0p5y: '(%)',
  div_yield_spread_1y: '(%)',
  div_yield_spread_2y: '(%)',
  div_yield_spread_3y: '(%)',
  div_yield_spread_5y: '(%)',
  div_yield_spread_7y: '(%)',
  div_yield_spread_10y: '(%)',
  div_yield_spread_30y: '(%)',
  // 金额类（底层为元，展示/输入按亿）
  total_market_cap: '(亿)',
  circ_market_cap: '(亿)',
  interest_bearing_debt: '(亿)',
  cumulative_dividend_amount: '(亿)',
  cumulative_financing_amount: '(亿)',
  // 估值/倍数
  pe_ttm: '(倍)',
  pb_mrq: '(倍)',
  ps_ttm: '(倍)',
  pcf_ttm: '(倍)',
  current_ratio: '(倍)',
  quick_ratio: '(倍)',
  cf_to_net_profit: '(倍)',
  interest_coverage: '(倍)',
  // 价格/每股
  latest_close: '(元)',
  ma5: '(元)',
  ma10: '(元)',
  ma20: '(元)',
  ma60: '(元)',
  ma120: '(元)',
  ma250: '(元)',
  dps: '(元)',
  // 其他
  consecutive_div_years: '(年)',
  avg_volume: '(万股)',
}

export function fieldTitleWithUnit(field: string, baseLabel: string): string {
  const unit = fieldInputUnit(field).label
  return unit ? `${baseLabel}(${unit})` : baseLabel
}

/** 供筛选指标下拉框使用的带单位名称，例如“总市值(亿)”。 */
export function fieldOptionLabel(field: string, baseLabel: string): string {
  const unit = fieldInputUnit(field).label
  return unit ? `${baseLabel}(${unit})` : baseLabel
}

/**
 * 指标（含 DSL 复合指标）在筛选下拉里显示的**全中文**名称。
 *
 * 2026-11-22：从 `views/ScreeningPage.vue` 抽出（纯函数，无响应式依赖）。
 * 优先级：display_name（后端给的权威中文名）→ FIELD_LABELS/STATEMENT 映射
 * → 后端 label → 字段名兜底，最后统一追加单位。
 *
 * 2026-11-16：DSL 复合指标曾经直接显示 `dividend_financing_ratio (DSL v2)`
 * 这种纯英文标签，违反 PRD §12.1「用户可见名称必须全中文」。
 */
export function indicatorDisplayName(indicator: {
  readonly name: string
  readonly label?: string
  readonly display_name?: string
  readonly dsl?: boolean
  readonly basis_label?: string
  readonly basis_report_date?: string
  readonly latest_report_date?: string
}): string {
  const baseLabel = indicator.dsl
    ? (indicator.display_name && indicator.display_name !== indicator.name
        ? `${indicator.display_name}`
        : fieldDisplayName(indicator.name, indicator.label ?? indicator.name))
    : fieldDisplayName(indicator.name, indicator.label)
  // 2026-11-19（合并 main 的 reports/145）：字段的可算基准期早于最新报告期时，
  // 必须在下拉里标出用的是哪一期 —— 季报不披露折旧摊销，
  // EBITDA 族实际基于最近半年报/年报。这段逻辑原本在页面的 indicatorLabelOf 里，
  // 本轮已并入这个单一实现（避免"抽出后丢了 main 的新行为"）。
  const basisNote =
    indicator.basis_label && indicator.basis_report_date !== indicator.latest_report_date
      ? `（${indicator.basis_label}口径）`
      : ''
  return fieldOptionLabel(indicator.name, `${baseLabel}${basisNote}`)
}
