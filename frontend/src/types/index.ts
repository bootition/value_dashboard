/**
 * Shared types barrel.
 *
 * Re-exports the data-quality contract so consumers can import from
 * `@/types` or `../types` without knowing the internal module layout.
 */

export {
  // Warning codes
  type KnownWarningCode,
  type WarningCode,
  KNOWN_WARNING_CODES,

  // Indicator fields
  type IndicatorField,
  DIVIDEND_INDICATOR_FIELDS,
  SNAPSHOT_DEPENDENT_INDICATOR_FIELDS,

  // Trust helper
  isIndicatorUntrusted,

  // Backend contracts
  type FinancialStatementDates,
  type IndicatorSnapshotDates,
  type DataDates,
  type DividendQuality,
  type LineageQuality,
  type OperationalWarnings,
  type DataQualityStatus,
  type StockFreshness,
} from './data-quality.ts'

export {
  // Stock detail contracts
  type StockInfo,
  type IndicatorMetric,
  type ValuationIndicators,
  type ProfitabilityIndicators,
  type GrowthIndicators,
  type SafetyIndicators,
  type ShareholderReturnIndicators,
  type IndicatorsPayload,
  type IndicatorsResponse,
  type KlineCandle,
  type KlineResponse,
  type FinancialTrendRow,
  type TrendResponse,
  type AuditFieldRow,
  type AuditBatchRow,
  type AuditResponse,
} from './stock-detail.ts'
