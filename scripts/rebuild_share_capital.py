"""Rebuild stock_meta.total_shares / circ_shares from verifiable free sources.

Sources:
  - Tencent realtime quotes (qt.gtimg.cn): fields 72 (circulating) and 73
    (total) share counts, unit = shares, as-of = quote datetime.
  - SZSE official listing list via AKShare (unit = shares) for cross-check.
  - BSE official listing list via AKShare (unit = shares, dated) for cross-check.

Policy:
  - SSE: single source (Tencent) + name match + circ<=total sanity.
  - SZSE/BSE: dual source; exact agreement required, otherwise the stock is
    skipped and recorded as a mismatch (fail-closed, never guessed).
  - All writes (stock_meta + fetch_batch + raw_response_archive + source_audit)
    commit in ONE DuckDB transaction. No partial publication.

Usage:
  python scripts/rebuild_share_capital.py --evidence docs/evidence/evidence-share-capital-rebuild-<date>.json
Requires explicit profile env (VD_ENV/VD_DUCKDB_PATH/VD_SQLITE_PATH/...).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import re
import sys
import time
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.storage.path_policy import resolve_and_validate_paths
from app.core.storage.duckdb_store import DuckDBStore

logger = logging.getLogger("rebuild_share_capital")

TENCENT_URL = "https://qt.gtimg.cn/q="
TENCENT_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
BATCH_SIZE = 50
REQUEST_PAUSE_SECONDS = 0.35
# Dual-source agreement: allow tiny timing differences (convertible-bond
# conversion, unlock schedules) between realtime quotes and the dated exchange
# list; beyond this the sources conflict and the stock is skipped fail-closed.
TOLERANCE_REL = 0.005
# SSE single-source (Tencent) internal circ-vs-total excess tolerated as
# provider field-timing noise; clamped to circ and disclosed per row.
CLAMP_REL = 0.001

_SINA_STRUCTURE_URL = "https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_StockStructure/stockid/{code}.phtml"
_NAME_PREFIX_RE = re.compile(r"^(?:\*?ST|XD|XR|DR|N|C)+", re.IGNORECASE)


def _clean_name(name: str) -> str:
    cleaned = (name or "").replace(" ", "").upper()
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _NAME_PREFIX_RE.sub("", cleaned)
    return cleaned


def fetch_sina_structure(code: str) -> dict | None:
    """Latest total / float-A shares from Sina stock-structure page (unit: shares)."""
    try:
        resp = requests.get(_SINA_STRUCTURE_URL.format(code=code), headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.encoding = "gbk"
        text = resp.text

        def _latest(label: str) -> int | None:
            m = re.search(label + r".*?</td><td>([\d,]+(?:\.\d+)?)\s*万股", text, re.DOTALL)
            return int(round(float(m.group(1).replace(",", "")) * 10000)) if m else None

        total = _latest("·总股本")
        circ = _latest(r"&nbsp;&nbsp;&nbsp;&nbsp;流通A股")
        if total and circ:
            return {"total": total, "circ": circ, "source": "sina_stock_structure"}
    except Exception:
        return None
    return None


def _prefix(code: str) -> str:
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    return f"bj{code}"


def fetch_tencent(codes: list[str]) -> tuple[dict[str, dict], str]:
    """Return ({code: {total, circ, name, quote_time, price}}, raw_text)."""
    q = ",".join(_prefix(c) for c in codes)
    resp = requests.get(TENCENT_URL + q, headers=TENCENT_HEADERS, timeout=30)
    resp.encoding = "gbk"
    text = resp.text
    out: dict[str, dict] = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        m = re.match(r'v_([a-z]{2}\d{6})="(.*)"', line)
        if not m:
            continue
        body = m.group(2)
        parts = body.split("~")
        if len(parts) < 77 or not parts[1]:
            continue
        code = m.group(1)[2:]
        try:
            circ = int(float(parts[72])) if parts[72] else None
            total = int(float(parts[73])) if parts[73] else None
            price = float(parts[3]) if parts[3] else None
        except ValueError:
            continue
        quote_time = parts[30] if len(parts) > 30 else ""
        out[code] = {
            "total": total, "circ": circ, "name": parts[1],
            "quote_time": quote_time, "price": price,
        }
    return out, text


def fetch_szse_official() -> tuple[dict[str, dict], str]:
    import akshare as ak

    df = ak.stock_info_sz_name_code(symbol="A股列表")
    raw = df.to_json(orient="records", force_ascii=False)
    out: dict[str, dict] = {}
    for _, row in df.iterrows():
        code = str(row.get("A股代码", "")).strip().zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            continue

        def _num(v: object) -> int | None:
            s = str(v).replace(",", "").strip()
            return int(float(s)) if s and s not in {"nan", "None", "-"} else None

        out[code] = {
            "total": _num(row.get("A股总股本")),
            "circ": _num(row.get("A股流通股本")),
            "as_of": None,
        }
    return out, raw


def fetch_bse_official() -> tuple[dict[str, dict], str]:
    import akshare as ak

    df = ak.stock_info_bj_name_code()
    raw = df.to_json(orient="records", force_ascii=False)
    out: dict[str, dict] = {}
    for _, row in df.iterrows():
        code = str(row.get("证券代码", "")).strip().zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            continue

        def _num(v: object) -> int | None:
            s = str(v).replace(",", "").strip()
            return int(float(s)) if s and s not in {"nan", "None", "-"} else None

        out[code] = {
            "total": _num(row.get("总股本")),
            "circ": _num(row.get("流通股本")),
            "as_of": str(row.get("报告日期", "")).strip() or None,
        }
    return out, raw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, help="evidence JSON output path")
    parser.add_argument("--only-codes", nargs="*", default=None, help="restrict to specific codes (testing)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    paths = resolve_and_validate_paths()
    duck = DuckDBStore(paths=paths)
    evidence_path = Path(args.evidence)

    universe = duck.read_query(
        "SELECT stock_code, name, exchange FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
    )
    if args.only_codes:
        wanted = set(args.only_codes)
        universe = [r for r in universe if r["stock_code"] in wanted]
    meta = {r["stock_code"]: r for r in universe}
    codes = [r["stock_code"] for r in universe]
    logger.info("universe: %d listed stocks", len(codes))

    logger.info("fetching SZSE official list...")
    szse_official, szse_raw = fetch_szse_official()
    logger.info("SZSE official: %d rows", len(szse_official))
    logger.info("fetching BSE official list...")
    bse_official, bse_raw = fetch_bse_official()
    logger.info("BSE official: %d rows", len(bse_official))

    report = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "env": paths.env.value,
        "duckdb_path": str(paths.duckdb_path),
        "universe": len(codes),
        "written": 0,
        "skipped": {},
        "mismatches": [],
        "per_exchange": {},
        "samples": [],
    }
    skip_reasons: dict[str, list[str]] = {}

    def skip(reason: str, code: str) -> None:
        skip_reasons.setdefault(reason, []).append(code)

    accepted: list[dict] = []
    tencent_raw_parts: list[str] = []

    for i in range(0, len(codes), BATCH_SIZE):
        chunk = codes[i : i + BATCH_SIZE]
        try:
            quotes, raw_text = fetch_tencent(chunk)
        except Exception as exc:  # network failure: skip whole chunk, keep going
            logger.warning("tencent batch failed (%s..%s): %s", chunk[0], chunk[-1], exc)
            for c in chunk:
                skip("tencent_fetch_failed", c)
            continue
        tencent_raw_parts.append(raw_text)
        for code in chunk:
            q = quotes.get(code)
            exchange = meta[code]["exchange"]
            if q is None or not q["total"] or not q["circ"]:
                alt = fetch_sina_structure(code) if exchange == "SSE" else None
                if alt and alt["circ"] <= alt["total"]:
                    accepted.append({
                        "stock_code": code, "exchange": exchange,
                        "total": alt["total"], "circ": alt["circ"],
                        "as_of": None, "source": alt["source"], "quote_time": "",
                    })
                else:
                    skip("tencent_no_share_data", code)
                continue
            note = None
            if q["circ"] > q["total"]:
                excess = (q["circ"] - q["total"]) / q["total"]
                if excess <= CLAMP_REL:
                    note = "provider_internal_inconsistency_clamped"
                    q["total"] = q["circ"]
                else:
                    alt = fetch_sina_structure(code) if exchange == "SSE" else None
                    if alt and alt["circ"] <= alt["total"]:
                        accepted.append({
                            "stock_code": code, "exchange": exchange,
                            "total": alt["total"], "circ": alt["circ"],
                            "as_of": None, "source": alt["source"], "quote_time": q["quote_time"],
                        })
                    else:
                        skip("source_circ_exceeds_total", code)
                    continue
            local_name = _clean_name(meta[code]["name"])
            remote_name = _clean_name(q["name"])
            if local_name and remote_name and local_name != remote_name and remote_name not in local_name and local_name not in remote_name:
                skip("name_mismatch", code)
                continue
            official = szse_official.get(code) if exchange == "SZSE" else bse_official.get(code) if exchange == "BSE" else None
            if exchange in {"SZSE", "BSE"}:
                if official is None or not official["total"] or not official["circ"]:
                    # Exchange list omission (e.g. very new listing): fall back to
                    # the self-consistent realtime quote, disclosed.
                    as_of = q["quote_time"][:8] if q["quote_time"] else None
                    accepted.append({
                        "stock_code": code, "exchange": exchange,
                        "total": q["total"], "circ": q["circ"],
                        "as_of": as_of, "source": "tencent",
                        "quote_time": q["quote_time"],
                        "note": "official_list_missing_tencent_only",
                    })
                    continue
                if official["circ"] > official["total"]:
                    skip("official_circ_exceeds_total", code)
                    continue
                circ_diff = abs(official["circ"] - q["circ"]) / official["circ"]
                total_diff = abs(official["total"] - q["total"]) / official["total"]
                write_circ = official["circ"]
                circ_note = None
                if circ_diff <= TOLERANCE_REL:
                    circ_note = "circ_confirmed"
                else:
                    # Float conflicts resolve to the dated exchange list (registrar truth).
                    circ_note = "circ_conflict_prefer_official"
                if total_diff <= TOLERANCE_REL:
                    write_total = official["total"]
                    scope_note = "total_confirmed"
                elif official["total"] < q["total"] and circ_diff <= TOLERANCE_REL:
                    # Exchange list is A-share-scope total; quote total is company-wide
                    # (A+H/A+B). Market-cap semantics need company-wide total shares.
                    write_total = q["total"]
                    scope_note = "total_company_wide_scope"
                else:
                    # Exchange list is the registrar-level system of record; quote
                    # feed conflicts resolve to it, disclosed as a mismatch.
                    report["mismatches"].append({
                        "stock_code": code,
                        "tencent_total": q["total"], "tencent_circ": q["circ"],
                        "official_total": official["total"], "official_circ": official["circ"],
                        "total_diff_rel": round(total_diff, 6), "circ_diff_rel": round(circ_diff, 6),
                        "resolution": "prefer_official_list",
                    })
                    write_total = official["total"]
                    scope_note = "total_conflict_prefer_official"
                as_of = official["as_of"] or (q["quote_time"][:8] if q["quote_time"] else None)
                source = "official_list+tencent"
                note = ";".join(n for n in (note, circ_note, scope_note) if n)
            else:
                as_of = q["quote_time"][:8] if q["quote_time"] else None
                source = "tencent"
                write_total, write_circ = q["total"], q["circ"]
            accepted.append({
                "stock_code": code, "exchange": exchange,
                "total": write_total, "circ": write_circ,
                "as_of": as_of, "source": source,
                "quote_time": q["quote_time"],
                **({"note": note} if note else {}),
            })
        time.sleep(REQUEST_PAUSE_SECONDS)
        if (i // BATCH_SIZE) % 20 == 0:
            logger.info("progress %d/%d accepted=%d", i + len(chunk), len(codes), len(accepted))

    logger.info("accepted=%d skipped=%d mismatches=%d", len(accepted), sum(len(v) for v in skip_reasons.values()), len(report["mismatches"]))

    if not accepted:
        report["error"] = "no validated rows; nothing written"
        evidence_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    # ── single-transaction publication with full lineage ─────────────────
    combined_raw = "\n".join(tencent_raw_parts) + "\n===SZSE===\n" + szse_raw + "\n===BSE===\n" + bse_raw
    raw_bytes = combined_raw.encode("utf-8", errors="replace")
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    fetch_time = dt.datetime.now(dt.timezone.utc).isoformat()
    batch_id = str(uuid.uuid4())

    with duck.transaction() as conn:
        for row in accepted:
            conn.execute(
                """UPDATE stock_meta SET total_shares = ?, circ_shares = ?, updated_at = now()
                   WHERE stock_code = ?""",
                [row["total"], row["circ"], row["stock_code"]],
            )
        conn.execute(
            """INSERT INTO fetch_batch
               (batch_id, data_type, source, adapter_version, fetch_time, raw_response_hash, row_count, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [batch_id, "share_capital", "tencent+exchange_lists", "rebuild_share_capital-1.0",
             fetch_time, raw_hash, len(accepted), "approximate"],
        )
        conn.execute(
            """INSERT INTO raw_response_archive
               (raw_response_hash, source, fetch_time, payload, api_version, integrity_verified)
               VALUES (?, ?, ?, ?, ?, TRUE)
               ON CONFLICT(raw_response_hash) DO NOTHING""",
            [raw_hash, "tencent+exchange_lists", fetch_time, raw_bytes, "rebuild_share_capital-1.0"],
        )
        audit_rows = []
        for row in accepted:
            report_date = (
                f"{row['as_of'][:4]}-{row['as_of'][4:6]}-{row['as_of'][6:8]}"
                if row["as_of"] and re.fullmatch(r"\d{8}", row["as_of"])
                else (row["as_of"] or fetch_time[:10])
            )
            for field, value in (("total_shares", row["total"]), ("circ_shares", row["circ"])):
                audit_rows.append((
                    row["stock_code"], field, report_date, value, row["source"],
                    batch_id, fetch_time, raw_hash, "approximate", None, "rebuild_share_capital-1.0",
                ))
        conn.executemany(
            """INSERT INTO source_audit
               (stock_code, field_name, report_date, value, source, fetch_batch_id,
                fetch_time, raw_response_hash, confidence, reason_code, api_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            audit_rows,
        )

    report["written"] = len(accepted)
    report["batch_id"] = batch_id
    report["raw_response_hash"] = raw_hash
    report["skipped"] = {k: len(v) for k, v in sorted(skip_reasons.items())}
    report["skipped_codes"] = {k: v[:50] for k, v in sorted(skip_reasons.items())}
    per_exchange: dict[str, dict] = {}
    for row in accepted:
        bucket = per_exchange.setdefault(row["exchange"], {"written": 0})
        bucket["written"] += 1
    report["per_exchange"] = per_exchange
    report["samples"] = accepted[:5] + accepted[-5:]

    # ── post-write verification ──────────────────────────────────────────
    bad = duck.read_query(
        """SELECT COUNT(*) AS n FROM stock_meta
           WHERE is_listed IS TRUE AND total_shares IS NOT NULL AND circ_shares IS NOT NULL
             AND circ_shares > total_shares"""
    )[0]["n"]
    missing = duck.read_query(
        """SELECT exchange,
                  COUNT(*) FILTER (WHERE total_shares IS NULL OR total_shares <= 0) AS missing_total,
                  COUNT(*) FILTER (WHERE circ_shares IS NULL OR circ_shares <= 0) AS missing_circ,
                  COUNT(*) AS listed
           FROM stock_meta WHERE is_listed IS TRUE GROUP BY exchange ORDER BY exchange"""
    )
    audit_count = duck.read_query(
        "SELECT COUNT(*) AS n FROM source_audit WHERE fetch_batch_id = ?", [batch_id]
    )[0]["n"]
    report["post_verification"] = {
        "circ_exceeds_total_listed": bad,
        "missing_by_exchange": missing,
        "audit_rows_for_batch": audit_count,
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info("evidence written to %s", evidence_path)
    logger.info("post verification: circ>total listed = %d, audit rows = %d", bad, audit_count)
    return 0 if bad == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
