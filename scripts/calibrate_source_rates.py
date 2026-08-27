"""多数据源限速校准实验（一次性维护/研究工具）。

对 A 股价格源逐个用固定请求间隔做小样本连续抓取，实测单请求延迟分布与
罚点（>30s/>60s），据此落地可安全使用的 rate_limit_interval。

只使用 600519 样本并发请求，不触碰任何数据库。
用法:
  python scripts/calibrate_source_rates.py
  python scripts/calibrate_source_rates.py --source tencent --interval 0.3 --count 12
  python scripts/calibrate_source_rates.py --out docs/evidence/evidence-source-rates-2026-08-07.json
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from app.core.adapters.base import FetchRequest
from app.core.adapters.manager import AdapterManager

SOURCES = ["akshare_eastmoney", "tdx", "tencent", "sina", "baostock"]
DEFAULT_COUNT = 8
SAMPLE_CODE = "600519"
SAMPLE_START = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
SAMPLE_END = datetime.now().strftime("%Y-%m-%d")
REQUEST_TIMEOUT_S = 150.0


def probe(adapter, interval: float, count: int, code: str) -> dict:
    latencies: list[float] = []
    penalties30 = 0
    penalties60 = 0
    errors = 0

    def one() -> None:
        nonlocal errors, penalties30, penalties60
        started = time.monotonic()
        req = FetchRequest(
            data_type="price_daily",
            stock_codes=[code],
            start_date=SAMPLE_START,
            end_date=SAMPLE_END,
            adjust="raw",
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(adapter.fetch, req)
                result = fut.result(timeout=REQUEST_TIMEOUT_S)
            if result.metadata.error:
                errors += 1
        except Exception:
            errors += 1
        elapsed = time.monotonic() - started
        latencies.append(elapsed)
        if elapsed >= 60:
            penalties60 += 1
        elif elapsed >= 30:
            penalties30 += 1

    for i in range(count):
        one()
        if i + 1 < count:
            time.sleep(interval)

    latencies.sort()
    n = len(latencies)

    def pct(p: float) -> int | None:
        if not n:
            return None
        return round(latencies[min(n - 1, int((n - 1) * p))] * 1000)

    return {
        "requests": n,
        "min_ms": round(latencies[0] * 1000) if n else None,
        "p50_ms": pct(0.5),
        "p90_ms": pct(0.9),
        "max_ms": round(latencies[-1] * 1000) if n else None,
        "penalty_30s": penalties30,
        "penalty_60s": penalties60,
        "errors": errors,
    }


def run(source: str, interval: float, count: int) -> dict:
    manager = AdapterManager()
    adapter = manager.get_adapter(source)
    if adapter is None:
        return {"error": "adapter not registered"}
    started = time.monotonic()
    try:
        stats = probe(adapter, interval, count, SAMPLE_CODE)
        stats["total_wall_s"] = round(time.monotonic() - started, 1)
        return stats
    finally:
        manager.close()


def run_source(source: str, intervals: list[float], count: int) -> tuple[str, dict]:
    """Calibrate one source serially so intervals do not contaminate each other."""
    row: dict = {}
    for interval in intervals:
        print(f"[run] {source} interval={interval}s count={count} ...", flush=True)
        stats = run(source, interval, count)
        row[str(interval)] = stats
        print(json.dumps(stats, ensure_ascii=False))
    return source, row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=SOURCES, default=None)
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    sources = [args.source] if args.source else SOURCES
    intervals = [args.interval] if args.interval is not None else ([0.6, 1.0] if args.source else [0.6])

    report: dict = {"generated_at": datetime.now().isoformat(), "sample": SAMPLE_CODE, "results": {}}
    parallel = max(1, min(args.parallel, len(sources)))
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = [pool.submit(run_source, source, intervals, args.count) for source in sources]
        for future in as_completed(futures):
            source, row = future.result()
            report["results"][source] = row

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[wrote] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
