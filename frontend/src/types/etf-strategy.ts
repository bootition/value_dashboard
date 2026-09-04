/** ETF 轮动工作台后端数据契约（2026-09-05） */

import type { ValuationDetail } from './index-dashboard.ts'

export interface EtfMetaLike {
  etf_code: string
  name: string
  track_index_code: string | null
  track_index_name: string | null
  primary_metric: 'pe' | 'pb'
  industry_group: string | null
  budget: number
  step_pct: number
  enabled: boolean
  note: string | null
}

export interface EtfPosition {
  etf_code: string
  shares: number
  cost_basis: number
  avg_cost: number | null
  realized_pnl: number
  buy_count: number
  sell_count: number
  total_buy_amount: number
  total_buy_fee: number
  total_sell_amount: number
  total_sell_fee: number
  last_buy_price: number | null
  last_sell_price: number | null
  first_buy_date: string | null
}

export interface EtfValuationSummary {
  status: 'ok' | 'partial' | 'unavailable' | 'error'
  samples: number
  pe: number | null
  pe_percentile: number | null
  pb: number | null
  pb_percentile: number | null
  erp: number | null
  erp_percentile: number | null
  latest_date: string | null
}

export interface EtfOverviewItem {
  etf_code: string
  name: string
  track_index_code: string | null
  track_index_name: string | null
  primary_metric: 'pe' | 'pb'
  industry_group: string | null
  step_pct: number
  budget: number
  tranche_amount: number
  used_budget: number
  budget_left: number
  signal: 'buy' | 'sell' | 'neutral' | 'unavailable'
  current_price: number | null
  market_value: number | null
  unrealized_pnl: number | null
  remaining_buys: number
  next_buy_price: number | null
  remaining_sells: number
  next_sell_price: number | null
  sell_tranche_amount: number
  sell_tranches_done: number
  clear_tail: boolean
  enabled: boolean
  position: EtfPosition
  valuation: EtfValuationSummary
  percentile: number | null
  percentile_label: string
}

export interface EtfOverviewResponse {
  items: EtfOverviewItem[]
  total_assets: string | null
  cash_net_in: number
  market_value: number
  unrealized_pnl: number
  realized_pnl: number
}

export interface EtfTradeRow {
  id: number
  etf_code: string
  trade_date: string
  direction: 'buy' | 'sell'
  price: number
  shares: number
  amount: number
  fee: number
  note: string | null
}

export interface EtfCashFlowRow {
  id: number
  flow_date: string
  direction: 'in' | 'out'
  amount: number
  note: string | null
}

export interface EtfDetail extends EtfOverviewItem {
  trades: EtfTradeRow[]
  cash_flows: EtfCashFlowRow[]
  track_valuation: ValuationDetail | null
  settings: { total_assets: string | null; budget: number; step_pct: number }
}
