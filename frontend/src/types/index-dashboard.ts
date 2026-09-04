/** 指数看板（多指数 ERP）后端数据契约（2026-09-05） */

export interface IndexCatalogItem {
  code: string
  name: string
  category: 'broad' | 'industry'
  source: 'legulegu' | 'sws'
  cadence: 'monthly' | 'daily'
  backtest_validated: boolean
}

export interface IndexBands {
  p10: number | null
  p20: number | null
  p50: number | null
  p80: number | null
  p90: number | null
  min: number | null
  max: number | null
}

export interface IndexOverviewItem extends IndexCatalogItem {
  status: 'ok' | 'partial' | 'unavailable' | 'error'
  samples: number
  confidence: 'high' | 'low' | 'unavailable'
  latest_date: string | null
  pe: number | null
  pe_metric: string | null
  pe_percentile: number | null
  pb: number | null
  pb_percentile: number | null
  erp: number | null
  erp_percentile: number | null
  erp_bands: IndexBands
  error?: string
}

export interface ErpPoint {
  trade_date: string
  pe: number
  treasury_yield: number
  erp: number
}

export interface ErpDetail extends IndexOverviewItem {
  series: ErpPoint[]
  disclaimer: string
}

export interface MetricPoint {
  trade_date: string
  value: number
}

export interface ValuationDetail {
  code: string
  name: string
  category: 'broad' | 'industry'
  latest_date: string | null
  pe_series: MetricPoint[]
  pe_bands: IndexBands
  pe_percentile: number | null
  pb_series: MetricPoint[]
  pb_bands: IndexBands
  pb_percentile: number | null
  pe_metric: string | null
}
