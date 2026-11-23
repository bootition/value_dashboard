"""价格域重算的逐项耗时剖面（2026-11-25，只读）。

用途
----
`compute_price_sensitive_for_codes` 生产实测约 8 分钟 / 5,563 只。这个脚本把
`compute_price_fields_for_stock` 里的每一类取数/计算单独计时，定位"每股多次
查询"到底花在哪。

**只读**：只跑 SELECT（`DuckDBStore` 的读路径是 read_only 连接），不建表、
不 UPDATE、不 DELETE。默认拒绝在正式库上开写事务。

用法
----
    VD_ENV=formal VD_FORMAL_ACK=confirmed \
    VD_DUCKDB_PATH=data/valuedashboard.duckdb VD_SQLITE_PATH=data/valuedashboard.sqlite \
    ./.venv/Scripts/python.exe scripts/profile_price_domain.py --codes 40

    # 或者用注入的 store（临时副本/测试库），脚本不去碰正式库
    ./.venv/Scripts/python.exe scripts/profile_price_domain.py --codes 40 --duckdb ... --sqlite ...
"""

from __future__ import annotations

import argparse
import contextlib
import os
import statistics
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _build_calculator(duckdb_path: str | None, sqlite_path: str | None):
    from app.core.indicators.calculator import IndicatorCalculator
    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.path_policy import DatabasePathSet, VdEnv, resolve_and_validate_paths
    from app.core.storage.sqlite_store import SQLiteStore

    if duckdb_path and sqlite_path:
        paths = DatabasePathSet(
            env=VdEnv.FORMAL if os.environ.get("VD_ENV") == "formal" else VdEnv.TEST,
            duckdb_path=Path(duckdb_path),
            sqlite_path=Path(sqlite_path),
            run_root=Path(duckdb_path).parent,
        ).validate()
    else:
        paths = resolve_and_validate_paths()
    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)
    return IndicatorCalculator(duck=duck, sqlite=sqlite, paths=paths), paths


def _sample_codes(calculator: Any, count: int) -> list[tuple[str, Any]]:
    """从快照里取确定性样本（按 stock_code 升序、等距抽样，可复现）。"""
    rows = calculator.duck.read_query(
        """SELECT stock_code, report_date FROM indicator_snapshot
           ORDER BY stock_code""",
    )
    if not rows:
        raise SystemExit("indicator_snapshot 为空，无法抽样")
    rows = list(rows)
    if count >= len(rows):
        picked = rows
    else:
        step = len(rows) / float(count)
        picked = [rows[min(int(i * step), len(rows) - 1)] for i in range(count)]
    return [(row["stock_code"], row["report_date"]) for row in picked]


def _time_each(
    label: str,
    codes: list[tuple[str, Any]],
    fn: Callable[[str, Any], Any],
    *,
    scale: int,
) -> dict[str, float]:
    times: list[float] = []
    for code, report_date in codes:
        started = time.perf_counter()
        try:
            fn(code, report_date)
        except Exception as error:  # noqa: BLE001 - 剖面脚本只报不中断
            print(f"  !! {label} {code} 失败: {error}")
            continue
        times.append((time.perf_counter() - started) * 1000.0)
    if not times:
        return {"label": label, "n": 0}
    mean = statistics.fmean(times)
    return {
        "label": label,
        "n": len(times),
        "mean_ms": mean,
        "median_ms": statistics.median(times),
        "p90_ms": sorted(times)[max(0, int(len(times) * 0.9) - 1)],
        "total_ms": sum(times),
        "per_day_5563_s": mean * scale / 1000.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", type=int, default=40, help="抽样股票数（默认 40）")
    parser.add_argument("--scale", type=int, default=5563, help="折算到全市场的股票数")
    parser.add_argument("--duckdb", default=None)
    parser.add_argument("--sqlite", default=None)
    parser.add_argument("--json", default=None, help="把结果写到这个路径")
    parser.add_argument(
        "--shared-connection",
        action="store_true",
        help=(
            "整段剖面共用一条只读连接（= compute_price_sensitive_for_codes 的生产条件）。"
            "不加这一项时每次 _read_query 都是 open-per-query，正式库冷开约 49ms/次，"
            "会把『查询本身』的时间淹掉。"
        ),
    )
    args = parser.parse_args(argv)

    calculator, paths = _build_calculator(args.duckdb, args.sqlite)
    print(f"env={paths.env.value} duckdb={paths.duckdb_path}")
    codes = _sample_codes(calculator, args.codes)
    print(f"样本 {len(codes)} 只：{codes[0][0]} … {codes[-1][0]}")
    if args.shared_connection:
        print("模式：共用一条只读连接（生产条件）")
        _run_profile(calculator, codes, args)
        return 0
    print("模式：open-per-query（每次查询冷开连接）")
    _run_profile(calculator, codes, args)
    return 0


def _run_profile(calculator: Any, codes: list[tuple[str, Any]], args: Any) -> None:
    @contextlib.contextmanager
    def _shared() -> Iterator[None]:
        if not args.shared_connection:
            yield
            return
        with calculator.duck.read_connection() as connection:
            calculator._calculation_read_connection = connection
            try:
                yield
            finally:
                del calculator._calculation_read_connection

    with _shared():
        _profile_steps(calculator, codes, args)


def _profile_steps(calculator: Any, codes: list[tuple[str, Any]], args: Any) -> None:

    # 预热：让 DuckDB catalog / 配置进进程内缓存，避免第一只替所有人背冷启动。
    for code, _report_date in codes[:2]:
        calculator._get_latest_price(code)

    financials_by_code = {}

    def latest_financials(code: str, _report_date: Any) -> Any:
        rows = calculator._get_latest_financials(code)
        financials_by_code[code] = rows
        return rows

    def ttm(code: str, report_date: Any) -> Any:
        return calculator._get_ttm_data(code, report_date)

    def ttm_asof_fin(code: str, _report_date: Any) -> Any:
        fin = financials_by_code.get(code) or calculator._get_latest_financials(code)
        return calculator._get_ttm_data(code, fin.get("report_date"))

    def dividends_asof_fin(code: str, _report_date: Any) -> Any:
        fin = financials_by_code.get(code) or calculator._get_latest_financials(code)
        return calculator._get_dividend_summary(code, fin.get("report_date"))

    def shares(code: str, _report_date: Any) -> Any:
        fin = financials_by_code.get(code) or calculator._get_latest_financials(code)
        return calculator._get_shares(code, fin)

    steps: list[tuple[str, Callable[[str, Any], Any]]] = [
        ("_get_latest_financials", latest_financials),
        ("_get_latest_price", lambda code, _d: calculator._get_latest_price(code)),
        ("_get_shares", shares),
        ("_get_ttm_data", ttm_asof_fin),
        ("_get_dividend_summary", dividends_asof_fin),
        ("_calc_technical", lambda code, _d: calculator._calc_technical(code)),
        ("_calc_treasury_spread", lambda code, _d: calculator._calc_treasury_spread(code)),
        (
            "compute_price_fields_for_stock",
            lambda code, report_date: calculator.compute_price_fields_for_stock(code, report_date),
        ),
    ]
    # _get_ttm_data 的"裸"对照（不预先算 financials，便于看清它自身开销）
    steps.insert(3, ("_get_ttm_data(bare)", ttm))

    results = []
    width = max(len(label) for label, _ in steps)
    header = (
        f"{'step':<{width}}  {'n':>3}  {'mean':>9}  {'median':>9}  {'p90':>9}  "
        f"{'折算 {args.scale} 只':>14}"
    )
    print(header)
    print("-" * len(header))
    for label, fn in steps:
        row = _time_each(label, codes, fn, scale=args.scale)
        results.append(row)
        if not row.get("n"):
            print(f"{label:<{width}}  -- 全部失败")
            continue
        print(
            f"{row['label']:<{width}}  {row['n']:>3}  {row['mean_ms']:>8.1f}ms  "
            f"{row['median_ms']:>8.1f}ms  {row['p90_ms']:>8.1f}ms  "
            f"{row['per_day_5563_s'] / 60.0:>11.2f} min"
        )

    # 各步相加的"朴素模型"：这个数比真实全流程大，因为真实路径里
    # _calc_treasury_spread 的取价/取分红与 _get_latest_price/_calc_dividend_yield 重复，
    # 但两者都是每股各查一次，重复部分同样要计时，所以加总仍然有参考价值。
    summed = sum(row["mean_ms"] for row in results if row.get("n")) 
    print("-" * len(header))
    print(f"逐项加总（含重复取数的朴素模型）: {summed:.1f} ms/只 "
          f"→ {summed * args.scale / 1000.0 / 60.0:.2f} min / {args.scale} 只")

    full = next((row for row in results if row["label"] == "compute_price_fields_for_stock"), None)
    if full and full.get("n"):
        print(f"端到端 compute_price_fields_for_stock: {full['mean_ms']:.1f} ms/只 "
              f"→ {full['per_day_5563_s'] / 60.0:.2f} min / {args.scale} 只")

    if args.json:
        import json

        Path(args.json).write_text(
            json.dumps({"sample": len(codes), "scale": args.scale, "steps": results},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"已写 {args.json}")


if __name__ == "__main__":
    raise SystemExit(main())
