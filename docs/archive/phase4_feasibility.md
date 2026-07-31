---
title: Phase 4: PIT / Restatement / Revision Feasibility & Legal Boundaries
status: archived
category: archive
last-reviewed: 2026-07-26
---

# Phase 4: PIT / Restatement / Revision Feasibility & Legal Boundaries

> Synthesized from Phase 2 (library deep-inspection) and Phase 3 (official source deep-inspection) evidence.
> Date: 2026-07-17

---

## 4.1 Point-in-Time (PIT) Data Feasibility

### Definition
PIT data means: when querying a historical period, you receive the values **as they were known at that point in time**, before any subsequent restatements or corrections.

### Verdict: ❌ NOT achievable with free sources

| Source | PIT Capability | Evidence |
|---|---|---|
| AKShare / Eastmoney | ❌ Returns `latest_restated` only | Eastmoney's `zcfzbAjaxNew` API returns the current restated version. No version history exposed. |
| BaoStock | ❌ Returns current ratios only | `query_balance_data` etc. return latest computed indicators, no historical versions. |
| TDX .dat files (mootdx/easy_tdx) | ❌ In-place updates | TDX financial .dat files are overwritten with each update. No version snapshots preserved. |
| CNINFO announcements | ⚠️ Theoretical via PDF | Historical announcement PDFs contain original `as_reported` figures. But extracting structured data from PDFs requires OCR/parsing — PRD explicitly excludes built-in OCR (section 3.4). |
| Qlib PIT collector | ❌ Disabled | Confirmed in Wave 1: `microsoft/qlib` PIT collector is temporarily disabled, public dataset defaults to Yahoo-based data. |

### PRD Alignment
The PRD already correctly scopes this (section 8.2):
- `latest_restated` is the **default and only guaranteed**口径
- `as_reported` is "仅在可获得时用于溯源展示" — not a rigid requirement
- "免费数据源不支持对完整 `as_reported` 版本链作出刚性保证"

**Conclusion**: The PRD's existing PIT scope is realistic and achievable. No adjustment needed. The system should:
1. Default to `latest_restated` for all current indicators, historical charts, and composite indicators
2. Optionally store `as_reported` values when a user manually submits a correction (via the JSON correction template in section 17)
3. Never promise historical PIT sequences

---

## 4.2 Restatement / Revision Strategy

### What's achievable

| 口径 | Achievable? | Source | Mechanism |
|---|---|---|---|
| `latest_restated` (current values, restated) | ✅ YES | Eastmoney via AKShare | Direct API call returns latest restated values |
| `latest_restated` (historical sequence) | ✅ YES | Eastmoney via AKShare | API returns historical periods with current restated口径 |
| `as_reported` (original filing values) | ⚠️ PARTIAL | CNINFO PDFs | Theoretically extractable from historical announcement PDFs, but requires PDF parsing. Not structured. |
| `as_reported` (full version chain) | ❌ NO | None | No free source provides complete historical version chains |

### Recommended Strategy

1. **Primary data path**: Use Eastmoney (via AKShare) for all structured financial data. It provides `latest_restated` by default — this satisfies the PRD's primary口径 requirement.

2. **Audit / traceability path**: Use CNINFO to download and store the original announcement PDFs. When a user needs to verify a specific figure, they can open the PDF in the browser (satisfies PRD section 14: "已恢复本地 PDF 的浏览器打开能力"). This provides溯源 without requiring structured `as_reported` extraction.

3. **人工覆写 path**: When a user discovers a discrepancy between the structured value and the PDF, they can submit a correction via the JSON template (PRD section 17). The correction is stored separately from the source value, is auditable, and can be rolled back (PRD section 9.5).

4. **Version tracking**: Each structured financial fetch should record:
   - Report period (报告期)
   - Fetch timestamp (抓取时间)
   - Source identifier (来源标识, e.g. "eastmoney:zcfzbAjaxNew" or "tdx:gpcw20241231.zip")
   - Confidence level (`strict` for direct API values, `approximate` for computed/derived, `missing` for null)

5. **Re-fetch on update**: When running增量更新, if a restatement is detected (value changes for a past period), the system should:
   - Preserve the old value with its fetch timestamp
   - Store the new value with the new fetch timestamp
   - Flag the period as "restated" in the data status page
   - NOT silently overwrite — the old value remains queryable for audit purposes

---

## 4.3 History Limits Assessment

| Data Type | Required From | Best Source | Actual Coverage | Status |
|---|---|---|---|---|
| Financial statements (三大报表) | 2010Q1 | Eastmoney via AKShare | ~1990s for listed companies | ✅ Exceeds requirement |
| Financial statements (TDX alt) | 2010Q1 | mootdx / easy_tdx .dat files | ~1998 | ✅ Meets requirement |
| Daily prices (raw + qfq) | Listed since / 5 years min | Eastmoney via AKShare | Full listing history | ✅ Exceeds requirement |
| Daily prices (socket alt) | 5 years min | BaoStock | Default 2015-01-01, likely supports earlier | ⚠️ Verify in implementation |
| Dividends / corporate actions | Full history | CNINFO + Eastmoney | Full disclosure history | ✅ Meets requirement |
| SW industry classification | Current only | SWS Research (manual download) | Current (2026-07-16) | ✅ Meets requirement |
| BSE data | Minimum usable | CNINFO (column=bse) + Eastmoney | BSE since 2021 | ✅ Meets minimum |

---

## 4.4 Legal Boundaries Assessment

### Tier 1: Legally Clean (Statutory Disclosure)
| Source | Legal Basis | Automated Access | Redistribution |
|---|---|---|---|
| CNINFO | PRC Securities Law — statutory disclosure platform | Not explicitly prohibited | ❌ Prohibited (personal use only) |
| SSE | PRC Securities Law — exchange public data | Not explicitly prohibited | ❌ Prohibited |
| SZSE | PRC Securities Law — exchange public data | Not explicitly prohibited | ❌ Prohibited |
| BSE | PRC Securities Law — exchange public data | Not explicitly prohibited | ❌ Prohibited |

**Assessment**: Personal local automated access to statutory disclosure is legally clean. The PRD's constraint of "仅限个人本地研究用途" + "不支持数据再分发" aligns perfectly.

### Tier 2: Proprietary but Publicly Published
| Source | Legal Basis | Automated Access | Redistribution |
|---|---|---|---|
| SWS Research | Proprietary research, publicly published classification | Login likely required | ❌ Prohibited |
| Eastmoney (via AKShare) | Third-party aggregator, ToS applies | HTTP scraping — can be blocked at any time | ❌ Prohibited |
| Sina (via AKShare) | Third-party aggregator, ToS applies | HTTP scraping — can be blocked at any time | ❌ Prohibited |

**Assessment**: These sources are usable for personal research but carry access risk (rate limits, blocks, interface changes). The PRD's "适配器必须可替换" constraint is the correct mitigation. TDX protocol is a special case — it's a free TCP protocol used by retail trading software widely, with no explicit ToS.

### Tier 3: Free Service, No Explicit ToS
| Source | Legal Basis | Automated Access | Redistribution |
|---|---|---|---|
| BaoStock | Free proprietary service (baostock.com) | Socket protocol, no ToS | ❌ Assumed prohibited |
| TDX protocol | Free TCP protocol, widely used | No ToS | ❌ Assumed prohibited |

**Assessment**: Low legal risk for personal use. BaoStock is a free academic-oriented service. TDX protocol is a de facto public standard.

### Anti-Crawler / Breakage Risk Summary

| Source | Risk Level | Known Issues | Mitigation |
|---|---|---|---|
| CNINFO | ⚠️ Moderate | 403 on frequent requests, cookie occasionally needed | ≥1.5s interval, cache stock mapping, persist cookie |
| Eastmoney (via AKShare) | ⚠️ High | Xueqiu 403 (#7301), SW index broken (#7335), rate limiting | Multi-source fallback, adapter-replaceable design |
| Eastmoney (via efinance) | ❌ Severe | "Targeted by Eastmoney" (#215), captcha (#235) | Avoid efinance as primary; use AKShare instead |
| SWS Research | ⚠️ High | Transport error on direct fetch, login likely | One-time manual download + local cache |
| BSE direct | ❌ Severe | Transport error on direct fetch | Route through CNINFO (column=bse) |
| SZSE direct | ✅ Low | antiCrawler: false (RSSHub confirmed) | Direct API use is safe |
| BaoStock | ✅ Low | Socket protocol, no HTTP anti-bot | Direct use is safe |
| TDX protocol | ✅ Low | Free TCP, extended market broken (#153) | Core market data works; use easy_tdx/mootdx |

---

## 4.5 Recommended Architecture Implications

Based on the above assessment, the data layer architecture should follow this hierarchy:

```
┌─────────────────────────────────────────────────────┐
│                   Truth / Audit Layer                │
│   CNINFO (announcements + PDFs + legal originals)    │
│   - All A-shares (深沪京) + 港股 + 三板               │
│   - Free announcement API (hisAnnouncement/query)     │
│   - PDF download (static.cninfo.com.cn)              │
│   - 26 category codes covering all disclosure types  │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────┴──────────────────────────────┐
│              Running Adapter Layer (Primary)          │
│   Eastmoney via AKShare                               │
│   - Full financial statements (三大报表, 2010Q1+)     │
│   - Daily prices (raw + qfq + hfq)                   │
│   - Dividends (stock_dividend_cninfo + Eastmoney)    │
│   - BSE coverage (stock_info_bj_name_code)           │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────┴──────────────────────────────┐
│             Running Adapter Layer (Fallback)          │
│   TDX protocol via easy_tdx                           │
│   - Full financial statements (.dat files, 500+ cols) │
│   - Raw daily prices (no built-in qfq)                │
│   - Full XDXR (dividends, splits, rights, capital)    │
│   - BSE support (Market.BJ=2)                         │
│   - Very active maintenance (2026-07-13, 1 issue)     │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────┴──────────────────────────────┐
│              Supplementary Sources                    │
│   BaoStock: qfq prices (socket, low rate-limit risk)  │
│   SZSE direct API: 深市 announcements (cleanest JSON) │
│   SWS Research: SW industry classification (manual)   │
└─────────────────────────────────────────────────────┘
```

### Key Architecture Decisions

1. **CNINFO is the single truth layer** — confirmed by PRD and evidence. All PDFs for audit/traceability come from here.
2. **Eastmoney via AKShare is the primary adapter** — widest coverage, best maintenance, standardized fields. But it's HTTP-based and carries rate-limit/block risk.
3. **TDX via easy_tdx is the fallback adapter** — TCP protocol (no HTTP anti-bot), full financial statements, BSE support, very active maintenance. The qfq adjustment gap must be solved by computing from XDXR data.
4. **BaoStock is a price-only supplement** — its socket protocol has zero anti-bot risk, making it a reliable fallback for daily prices (raw + qfq). But it has NO full financial statements and NO BSE.
5. **SWS classification is a one-time manual download** — cached locally, refreshed semi-annually. No live API dependency.
6. **SZSE direct API is optional** — cleanest JSON, but 深市-only. Useful as a low-latency supplement for SZSE names if needed.
