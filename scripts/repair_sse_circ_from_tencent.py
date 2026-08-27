"""Repair SSE circulating shares via Tencent Finance qt.gtimg.cn API.

Tencent returns quote data with 总市值 and 流通市值 as the last two fields.
Combined with latest close price from our database:
  total_shares  ≈ total_market_cap / latest_close  (already from balance sheet)
  circ_shares   ≈ cirulating_market_cap / latest_close

Tencent uses 亿 for market cap, price is in 元. So circ_shares =
  (circulating_mkt_cap_亿 * 100_000_000) / close_price_元
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import requests

from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import require_formal_maintenance_paths
from app.core.storage.schema import init_duckdb_schema

URL = "http://qt.gtimg.cn/q="
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"}
DELAY = 0.5  # seconds between batches


def parse_tencent_response(text: str):
    """Parse Tencent quote line into {code: circ_shares}.

    Index 3  = close price (元)
    Index 44 = total market cap (亿元)
    Index 45 = circulating market cap (亿元)
    """
    results = {}
    for line in text.split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        prefix = line.split("=")[0]
        code = prefix.replace("v_sh", "").replace('"', "")
        if not code.isdigit() or not code.startswith("6"):
            continue
        values = line.split("=")[1].strip().strip('"').split("~")
        if len(values) < 46:
            continue
        try:
            close_price = float(values[3])
            total_mkt = float(values[44])
            circ_mkt = float(values[45])
            if close_price > 0 and circ_mkt > 0:
                total_shares = int(total_mkt * 100_000_000 / close_price)
                circ_shares = int(circ_mkt * 100_000_000 / close_price)
                results[code] = (total_shares, circ_shares)
        except (ValueError, IndexError):
            pass
    return results


def repair():
    paths = require_formal_maintenance_paths()
    duck = DuckDBStore(paths=paths)
    init_duckdb_schema(duck)

    targets = duck.read_query(
        """SELECT stock_code FROM stock_meta
           WHERE is_listed IS TRUE AND exchange = 'SSE'
             AND (circ_shares IS NULL OR circ_shares = 0)
           ORDER BY stock_code"""
    )
    codes = [r["stock_code"] for r in targets]
    print(f"候选: {len(codes)} stocks")

    BATCH = 50
    updated = 0
    for i in range(0, len(codes), BATCH):
        batch = codes[i : i + BATCH]
        symbols = ",".join(f"sh{code}" for code in batch)
        try:
            r = requests.get(f"{URL}{symbols}", headers=HEADERS, timeout=15)
            if r.status_code != 200:
                print(f"  批次 {i}: HTTP {r.status_code}")
                time.sleep(3)
                continue
            data = parse_tencent_response(r.text)
        except Exception as e:
            print(f"  批次 {i}: {e}")
            time.sleep(3)
            continue

        if data:
            with duck.transaction() as conn:
                conn.executemany(
                    "UPDATE stock_meta SET circ_shares = ?, total_shares = COALESCE(NULLIF(total_shares,0), ?) WHERE stock_code = ?",
                    [(circ, total, k) for k, (total, circ) in data.items()],
                )
            updated += len(data)

        if (i // BATCH + 1) % 10 == 0:
            print(f"进度: {min(i+BATCH, len(codes))}/{len(codes)}, 更新 {updated}")
        time.sleep(DELAY)

    remaining = duck.read_query(
        "SELECT COUNT(*) AS cnt FROM stock_meta WHERE is_listed IS TRUE AND exchange='SSE' AND (circ_shares IS NULL OR circ_shares=0)"
    )[0]["cnt"]
    result = {"updated": updated, "remaining_null_circ": remaining}
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    repair()
