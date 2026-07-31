"""scripts/sample_external_truth.py — 30-stock external truth sampling.

For each sampled listed stock, compare against the Tencent realtime quote
(independent provider):
  - latest close vs indicator_snapshot.latest_close
  - total_shares / circ_shares vs stock_meta (post-rebuild units, shares)
Tencent fields: idx72 = circulating shares, idx73 = total shares (unit: shares).

Usage (read-only against formal DB):
  python scripts/sample_external_truth.py --evidence docs/evidence-external-truth-20260731.json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import resolve_and_validate_paths

TENCENT_URL = "https://qt.gtimg.cn/q="
TENCENT_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
CLOSE_TOLERANCE = 0.02  # 2%: quote may move intraday after snapshot close
SHARE_TOLERANCE = 0.001

SAMPLE_SIZE = 30
SEED = 20260731


def _symbol(code: str) -> str:
    if code.startswith("6"):
        return f"sh{code}"
    return f"sz{code}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    paths = resolve_and_validate_paths()
    duck = DuckDBStore(paths=paths)

    # sample: 15 random + 15 forced (banks/brokers/new listings/BSE)
    rng = random.Random(args.seed)
    all_codes = [
        str(r["stock_code"]) for r in duck.read_query(
            "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE"
        )
    ]
    forced = ["600036", "601398", "000001", "601318", "600030", "601988", "600519",
              "000858", "300750", "688428", "920000", "920001", "688026", "688813", "002594"]
    forced = [c for c in forced if c in all_codes]
    sampled = forced + [c for c in rng.sample(all_codes, SAMPLE_SIZE * 2) if c not in forced]
    sampled = sampled[: SAMPLE_SIZE]

    snapshot = {
        str(r["stock_code"]): r for r in duck.read_query(
            """SELECT stock_code, latest_close, latest_price_date FROM indicator_snapshot
               WHERE report_date = (SELECT MAX(report_date) FROM indicator_snapshot)"""
        )
    }
    meta = {
        str(r["stock_code"]): r for r in duck.read_query(
            "SELECT stock_code, total_shares, circ_shares FROM stock_meta"
        )
    }

    rows: list[dict] = []
    for i in range(0, len(sampled), 50):
        codes = sampled[i : i + 50]
        q = ",".join(_symbol(c) for c in codes)
        resp = requests.get(TENCENT_URL + q, headers=TENCENT_HEADERS, timeout=30)
        resp.encoding = "gbk"
        for line in resp.text.split(";"):
            line = line.strip()
            m = re.match(r'v_([a-z]{2}\d{6})="(.*)"', line)
            if not m:
                continue
            code = m.group(2).split("~")[2] if len(m.group(2).split("~")) > 2 else ""
            parts = m.group(2).split("~")
            if len(parts) < 74 or not code:
                continue
            close = float(parts[3]) if parts[3] else None
            circ = int(float(parts[72])) if parts[72] else None
            total = int(float(parts[73])) if parts[73] else None
            snap = snapshot.get(code)
            mrow = meta.get(code, {})
            checks: dict[str, bool | None] = {}
            details: dict[str, Any] = {}
            if snap and snap["latest_close"] and close:
                diff = abs(snap["latest_close"] - close) / close
                checks["close_match"] = diff <= CLOSE_TOLERANCE
                details["snapshot_close"] = snap["latest_close"]
                details["external_close"] = close
                details["close_diff_rel"] = round(diff, 6)
            else:
                checks["close_match"] = None
                details["snapshot_close"] = snap["latest_close"] if snap else None
                details["external_close"] = close
            if mrow.get("total_shares") and total:
                diff = abs(mrow["total_shares"] - total) / total
                checks["total_shares_match"] = diff <= SHARE_TOLERANCE
                details["db_total"] = mrow["total_shares"]
                details["external_total"] = total
            else:
                checks["total_shares_match"] = None
            if mrow.get("circ_shares") and circ:
                diff = abs(mrow["circ_shares"] - circ) / circ
                checks["circ_shares_match"] = diff <= SHARE_TOLERANCE
                details["db_circ"] = mrow["circ_shares"]
                details["external_circ"] = circ
            else:
                checks["circ_shares_match"] = None
            rows.append({
                "stock_code": code,
                "checks": checks,
                "details": details,
            })
        time.sleep(0.3)

    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "sample_size": len(rows),
        "close_tolerance": CLOSE_TOLERANCE,
        "share_tolerance": SHARE_TOLERANCE,
        "rows": rows,
        "summary": {
            "close_matched": sum(1 for r in rows if r["checks"].get("close_match") is True),
            "close_mismatched": sum(1 for r in rows if r["checks"].get("close_match") is False),
            "close_unverifiable": sum(1 for r in rows if r["checks"].get("close_match") is None),
            "total_shares_matched": sum(1 for r in rows if r["checks"].get("total_shares_match") is True),
            "total_shares_mismatched": sum(1 for r in rows if r["checks"].get("total_shares_match") is False),
            "circ_shares_matched": sum(1 for r in rows if r["checks"].get("circ_shares_match") is True),
            "circ_shares_mismatched": sum(1 for r in rows if r["checks"].get("circ_shares_match") is False),
        },
    }
    evidence = Path(args.evidence)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
