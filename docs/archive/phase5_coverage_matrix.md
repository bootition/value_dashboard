# Phase 5: Coverage Matrix & Free-Only Feasibility Assessment

> Final research deliverable. Synthesized from Phase 2 (libraries), Phase 3 (official sources), and Phase 4 (PIT/legal analysis).
> Date: 2026-07-17

---

## 5.1 Data Requirement × Source Coverage Matrix

Legend: ✅ YES (verified) · ⚠️ PARTIAL (works but limited/risky) · ❌ NO · ❓ UNKNOWN

### 5.1.1 Financial Statements (三大报表)

| Requirement | AKShare (Eastmoney) | mootdx (TDX) | easy_tdx (TDX) | BaoStock | efinance | CNINFO | pytdx |
|---|---|---|---|---|---|---|---|
| Balance sheet (资产负债表) | ✅ Full, standardized | ✅ Full, 500+ fields | ✅ Full, .dat files | ❌ Ratios only | ❌ Summary only | ❌ PDF only | ⚠️ Summary only |
| Income statement (利润表) | ✅ Full, standardized | ✅ Full, 500+ fields | ✅ Full, .dat files | ❌ Ratios only | ❌ Summary only | ❌ PDF only | ⚠️ Summary only |
| Cash flow statement (现金流量表) | ✅ Full, standardized | ✅ Full, 500+ fields | ✅ Full, .dat files | ❌ Ratios only | ❌ Summary only | ❌ PDF only | ⚠️ Summary only |
| 2010Q1+ history | ✅ ~1990s | ✅ ~1998 | ✅ ~1998 | ⚠️ Default 2015 | N/A | ✅ Archived | ✅ ~1998 |
| Standardized field names | ✅ Chinese, consistent | ✅ Chinese, 583 fields | ✅ Chinese, TDX format | ❌ N/A | ❌ N/A | ❌ PDF | ⚠️ TDX format |
| Cumulative + single-quarter | ✅ API supports both | ⚠️ Cumulative only | ⚠️ Cumulative only | ❌ N/A | ❌ N/A | ❌ N/A | ⚠️ Cumulative only |

### 5.1.2 Price & Market Data

| Requirement | AKShare (Eastmoney) | mootdx (TDX) | easy_tdx (TDX) | BaoStock | efinance | CNINFO |
|---|---|---|---|---|---|---|
| Raw daily prices (不复权) | ✅ `adjust=""` | ✅ `bars()` | ✅ `GetSecurityBarsCmd` | ✅ `adjustflag="3"` | ✅ `fqt=0` | ❌ |
| Qfq daily prices (前复权) | ✅ `adjust="qfq"` | ❌ Must compute | ❌ Must compute | ✅ `adjustflag="2"` | ✅ `fqt=1` | ❌ |
| Hfq daily prices (后复权) | ✅ `adjust="hfq"` | ❌ Must compute | ❌ Must compute | ✅ `adjustflag="1"` | ✅ `fqt=2` | ❌ |
| Full listing history | ✅ | ✅ | ✅ | ⚠️ Default 2015 | ✅ | ❌ |
| Volume / turnover | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| Latest close + price date | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |

### 5.1.3 Corporate Actions

| Requirement | AKShare (Eastmoney/CNINFO) | mootdx (TDX) | easy_tdx (TDX) | BaoStock | CNINFO direct |
|---|---|---|---|---|---|
| Dividends (分红) | ✅ `stock_dividend_cninfo` | ⚠️ In .dat files | ✅ `GetXdxrInfoCmd` cat=1 | ✅ `query_dividend_data` | ✅ Category `qyfpxzcs` |
| Stock splits (送股/转增) | ⚠️ Via CNINFO | ⚠️ In .dat files | ✅ `GetXdxrInfoCmd` cat=1 | ❌ | ✅ Category `qyfpxzcs` |
| Rights issues (配股) | ⚠️ Via CNINFO | ⚠️ In .dat files | ✅ `GetXdxrInfoCmd` cat=1 | ❌ | ✅ Category `pg_szsh` |
| Share capital changes (股本变化) | ✅ `stock_share_changes_cninfo` | ⚠️ In .dat files | ✅ `GetXdxrInfoCmd` cat=2-10 | ❌ | ✅ Category `gqbd_szsh` |
| Ex-dividend dates | ✅ | ⚠️ In .dat files | ✅ XDXR records | ✅ | ✅ |

### 5.1.4 Market Coverage

| Requirement | AKShare | mootdx | easy_tdx | BaoStock | efinance | CNINFO |
|---|---|---|---|---|---|---|
| SSE (上交所) | ✅ | ✅ Market=1 | ✅ Market.SH | ✅ sh. prefix | ✅ | ✅ |
| SZSE (深交所) | ✅ | ✅ Market=0 | ✅ Market.SZ | ✅ sz. prefix | ✅ | ✅ |
| BSE (北交所) | ✅ `stock_info_bj_name_code` | ✅ v0.8.7+ | ✅ Market.BJ=2 | ❌ | ⚠️ Via Eastmoney | ✅ column=bse |
| ST / *ST identification | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Suspension (停牌) status | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Listing date | ✅ | ✅ | ✅ | ✅ `query_stock_basic` | ✅ | ✅ |

### 5.1.5 Industry Classification

| Requirement | AKShare | mootdx | easy_tdx | BaoStock | SWS Research | CNINFO |
|---|---|---|---|---|---|---|
| 申万 Level-1 (current) | ⚠️ Broken (#7335) | ❌ | ❌ | ❌ | ✅ Manual download | ✅ `stock_industry_category_cninfo` |
| 申万 Level-2 (current) | ⚠️ Broken (#7335) | ❌ | ❌ | ❌ | ✅ Manual download | ⚠️ Via cninfo |
| 证监会行业 (CSRC) | ✅ | ❌ | ❌ | ✅ `query_stock_industry` | ❌ | ✅ |

### 5.1.6 Reliability & Maintenance

| Dimension | AKShare | mootdx | easy_tdx | BaoStock | efinance |
|---|---|---|---|---|---|
| Last commit | 2026-05-27 | 2024-07-16 | 2026-07-13 | PyPI 2026-07-10 | 2026-03-18 |
| Last release | v1.18.64 (2026-05) | v0.11.7 (2024-05) | v1.20.4 (2026-07) | 0.9.3 (2026-07) | v0.5.5 (2025-03) |
| Open issues | 50 | 97 | 1 | N/A | 152 |
| Rate-limit risk | ⚠️ High (HTTP) | ✅ Low (TCP) | ✅ Low (TCP) | ✅ Low (Socket) | ❌ Severe (HTTP) |
| Maintenance verdict | ✅ Active | ⚠️ Stale | ✅ Very active | ✅ Active | ❌ Stale |

---

## 5.2 Free-Only Feasibility Assessment

### 5.2.1 PRD Hard Requirements vs. Free Source Coverage

| PRD Requirement (Section) | Free Source Available? | Primary | Fallback | Verdict |
|---|---|---|---|---|
| §6.4 Raw daily prices | ✅ | AKShare (Eastmoney) | BaoStock (socket) | ✅ Feasible |
| §6.4 Qfq daily prices | ✅ | AKShare (Eastmoney) | BaoStock (socket) | ✅ Feasible |
| §6.4 Dividends | ✅ | CNINFO (truth) + AKShare | easy_tdx XDXR | ✅ Feasible |
| §6.4 送股/转增/配股/拆股 | ✅ | easy_tdx XDXR | CNINFO categories | ✅ Feasible |
| §6.4 股本变化 | ✅ | AKShare (cninfo) | easy_tdx XDXR | ✅ Feasible |
| §6.4 最近收盘价 + 日期 | ✅ | AKShare | BaoStock / TDX | ✅ Feasible |
| §6.5 三大报表 2010Q1+ | ✅ | AKShare (Eastmoney) | mootdx/easy_tdx (.dat) | ✅ Feasible |
| §6.5 累计值 + 单季度值 | ⚠️ | AKShare (API supports both) | TDX (cumulative only, compute single-quarter) | ⚠️ Feasible with computation |
| §6.6 银行资本充足率等 | ⚠️ | AKShare (Eastmoney F10) | CNINFO PDFs | ⚠️ Partial — may need PDF辅助 |
| §6.6 证券公司风险覆盖率 | ⚠️ | AKShare (Eastmoney F10) | CNINFO PDFs | ⚠️ Partial — may need PDF辅助 |
| §6.7 当前上市股票全集 | ✅ | AKShare (`stock_info_a_code_name`) | BaoStock (`query_all_stock`) | ✅ Feasible |
| §6.7 ST/停牌/上市日期 | ✅ | AKShare | BaoStock | ✅ Feasible |
| §6.7 最小核心财务集 | ✅ | AKShare (Eastmoney) | TDX .dat files | ✅ Feasible |
| §6.7 申万一级/二级 | ⚠️ | SWS manual download | CNINFO (CSRC, not SW) | ⚠️ Feasible with manual step |
| §10 内建指标 (估值/盈利/成长/安全/股东回报/行情) | ✅ | Computed from above data | — | ✅ Feasible (system computes) |
| §12.3 默认股票池排除 ST/停牌/<1年 | ✅ | AKShare listing info | — | ✅ Feasible |
| §14 个股详情页 K线 + 均线 | ✅ | AKShare prices | BaoStock / TDX | ✅ Feasible |
| §14 PDF 浏览器打开 | ✅ | CNINFO PDF download | — | ✅ Feasible |
| §16 CLI + OpenCode 协议 | ✅ | System internal | — | ✅ Feasible (no external dependency) |

### 5.2.2 Overall Feasibility Verdict

**✅ V1 is feasible with 100% free data sources.**

All PRD hard requirements can be met with a combination of:
1. **CNINFO** (truth/disclosure layer — announcements, PDFs, legal originals)
2. **AKShare wrapping Eastmoney** (primary running adapter — structured financials, prices, qfq, dividends)
3. **easy_tdx wrapping TDX** (fallback adapter — full financials via .dat, XDXR, BSE, TCP protocol)
4. **BaoStock** (price supplement — socket protocol, zero anti-bot risk, qfq support)
5. **SWS Research** (industry classification — one-time manual download + local cache)

### 5.2.3 Identified Risks & Mitigations

| Risk | Impact | Probability | Mitigation |
|---|---|---|---|
| Eastmoney blocks AKShare endpoints | High — loses primary financial + price source | Medium (history of breakage) | Fallback to TDX (easy_tdx) for financials + BaoStock for prices. Adapter-replaceable design (PRD §A.1) |
| CNINFO rate-limits or adds anti-bot | Medium — loses truth layer access | Low (statutory disclosure, legal mandate) | ≥1.5s interval, cookie persistence, cache szse_stock.json. SZSE direct API as partial fallback for 深市 |
| SW industry classification becomes unavailable via AKShare | Medium — loses programmatic SW access | High (already broken, issue #7335) | One-time manual download from SWS Research website, cache locally, refresh semi-annually |
| TDX protocol changes | Low — loses fallback adapter | Low (stable protocol, decades-old) | easy_tdx + mootdx both implement it; rustdx as Rust alternative |
| BaoStock service shuts down | Low — loses price supplement | Low (active as of 2026-07-10) | Eastmoney + TDX both provide prices; BaoStock is supplement only |
| Financial statement field drift (Eastmoney renames fields) | Medium — breaks standardization | Medium (happens periodically) | Version-locked field mapping table; adapter handles remapping; CNINFO PDFs as audit reference |
| 单季度值 not directly available from TDX | Low — requires computation | Certain | Compute single-quarter = current cumulative - prior cumulative. This is standard financial analysis. |

### 5.2.4 Gaps That Cannot Be Closed with Free Sources

| Gap | PRD Stance | Resolution |
|---|---|---|
| True PIT (point-in-time) historical data | PRD §8.2: "不承诺完整 as_reported 版本链" | Accept `latest_restated` as default. Store fetch timestamps for audit. Manual correction via JSON template (§17). |
| XBRL structured financials for A-shares | Not required by PRD | Use Eastmoney structured API + CNINFO PDFs for audit. |
| BSE deep historical data | PRD §6.2: "北交所为最小可用覆盖" | CNINFO column=bse covers announcements. Eastmoney covers BSE prices + financials. Accept thinner coverage. |
| 银行净息差 / 证券净资本 / 保险偿付能力 | PRD §6.6: "仅属尽力而为或 PDF 辅助" | Mark as `missing` or `approximate` when not available. Allow manual correction via JSON template. |
| Real-time intraday prices | PRD §6.4: "不要求盘中实时更新" | Use latest completed trading day close. Not a gap. |

---

## 5.3 Recommended Source Architecture (Final)

```
PRIORITY 1 — TRUTH LAYER (不可替代)
├── CNINFO (cninfo.com.cn)
│   ├── Announcement API: POST /new/hisAnnouncement/query
│   ├── Stock mapping: GET /new/data/szse_stock.json
│   ├── PDF download: GET static.cninfo.com.cn/{path}
│   ├── Coverage: 深沪京 + 港股 + 三板 (all A-shares)
│   ├── 26 category codes (年报/半年报/季报/分红/配股/股本变动/解禁...)
│   ├── Rate limit: ≥1.5s interval, persist cookie
│   └── Legal: Statutory disclosure, personal use clean
│
PRIORITY 2 — PRIMARY ADAPTER (可替换)
├── AKShare wrapping Eastmoney
│   ├── Financial statements: stock_balance_sheet_by_report_em etc.
│   ├── Prices: stock_zh_a_hist(adjust=""/"qfq"/"hfq")
│   ├── Dividends: stock_dividend_cninfo
│   ├── BSE: stock_info_bj_name_code
│   ├── Listing info: stock_info_a_code_name
│   └── Risk: HTTP-based, rate-limit possible, adapter must be replaceable
│
PRIORITY 3 — FALLBACK ADAPTER (可替换)
├── easy_tdx wrapping TDX protocol
│   ├── Financial statements: GetReportFileCmd (.dat files, 500+ fields)
│   ├── Summary finance: GetFinanceInfoCmd
│   ├── XDXR (dividends/splits/rights): GetXdxrInfoCmd
│   ├── Prices: GetSecurityBarsCmd (raw only, compute qfq from XDXR)
│   ├── BSE: Market.BJ=2 (confirmed)
│   ├── cninfo integration: CninfoClient
│   └── Advantage: TCP protocol, zero HTTP anti-bot risk
│
PRIORITY 4 — SUPPLEMENTARY SOURCES
├── BaoStock (socket, zero anti-bot)
│   └── Prices: query_history_k_data_plus (raw/qfq/hfq) — price fallback only
├── SZSE direct API (cleanest JSON, 深市 only)
│   └── Announcements: POST szse.cn/api/disc/announcement/annList
└── SWS Research (manual download)
    └── Industry classification: one-time download, cache locally
```

### Adapter Replaceability Contract

Per PRD §A.1: "适配器必须可替换，且必须保留原始溯源材料"

Each adapter must implement a common interface:
```
interface FinancialDataAdapter:
    fetch_balance_sheet(code, period) → DataFrame + metadata
    fetch_income_statement(code, period) → DataFrame + metadata
    fetch_cash_flow_statement(code, period) → DataFrame + metadata
    fetch_daily_prices(code, start, end, adjust) → DataFrame + metadata
    fetch_dividends(code) → DataFrame + metadata
    fetch_listing_info(code) → dict + metadata
    fetch_xdxr(code) → DataFrame + metadata  # for qfq computation
```

Each result carries metadata: `{source, fetch_time, confidence, source_url, raw_response_hash}`

The system records which adapter provided each value, enabling:
- Source switching without data loss
- Cross-source validation when multiple adapters cover the same field
- Audit trail linking structured values back to CNINFO PDFs

---

## 5.4 Conclusion

The research phase is complete. V1 is **fully feasible** with 100% free data sources. The key findings are:

1. **CNINFO is confirmed as the truth/disclosure layer** — legally authoritative, programmatically accessible, covers all A-shares including BSE.
2. **AKShare (Eastmoney) is the primary running adapter** — widest coverage, best maintenance, full financial statements with qfq prices.
3. **easy_tdx (TDX protocol) is the critical fallback adapter** — TCP-based (no HTTP anti-bot), full financials via .dat files, BSE support, very active maintenance, full XDXR records. This is the insurance policy against Eastmoney breakage.
4. **BaoStock is a price-only supplement** — socket protocol with zero anti-bot risk, provides qfq prices as a reliable fallback.
5. **SWS industry classification requires a one-time manual download** — cached locally, refreshed semi-annually. No live API dependency.
6. **PIT/as_reported is not achievable** with free sources — but the PRD already correctly scopes this. `latest_restated` is the default, with manual correction via JSON templates for exceptions.
7. **BSE direct access is unreliable** — route all BSE retrieval through CNINFO and Eastmoney.
8. **All PRD hard requirements are met** — no blocking gaps identified.

The project is ready to proceed to **technical planning** (tech stack selection, system architecture, module design, implementation roadmap).
