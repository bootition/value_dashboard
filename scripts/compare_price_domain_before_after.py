"""价格域重算「改前 vs 改后」逐列对拍 + 测速（只读，可复用）。

背景（依据 `app/core/indicators/calculator.py` 的 S10 / S11 注释）
------------------------------------------------------------------
`compute_price_sensitive_for_codes` 从「每股 13 条 SQL」改成「每批每类一次」。
这类纯提速改动最危险的失败形态**不是慢，而是悄悄算得不一样**（假绿）。
所以本脚本的验收口径是：

1. **改前/改后逐只逐列 `==`**（不是 approx）——算法没变，浮点就该 bit 级相同；
2. 字段集合也必须一致（多一列/少一列都算 mismatch）；
3. 还要报**每列非空条数**：全是 NULL 对全是 NULL 也是 mismatch=0，那种 0 没意义。

"改前"从哪来（两种基线，都可用）
-----------------------------------
* ``git``     —— `git show HEAD:app/core/indicators/calculator.py` 落到临时目录、按
  独立模块导入 = **真·改前实现**（本仓当前 HEAD 还没合这次提速，所以它就是改前）。
* ``fallback`` —— 用工作区实现但**不进预取批次**：每个 `_get_xxx` 都走"缓存 miss →
  原逐股查询"。这条基线永远可用，且不依赖 HEAD 内容（测试 smoke 用 `auto`）。

数据源：**真库只读 + 临时 SQLite 副本**
----------------------------------------
* DuckDB：临时目录里放一个 `valuedashboard.duckdb` **硬链接**指向正式库（不复制
  26 GB），只读连接打开，不写。
* SQLite：把正式库**复制**一份到临时目录（85 MB），两个 arm 都读这份副本。
* 临时目录在 `finally` 里删。测试/临时库可以用 `--in-place` 直接读给定库对。

用法
----
    VD_ENV=formal VD_FORMAL_ACK=confirmed \
    VD_DUCKDB_PATH=data/valuedashboard.duckdb VD_SQLITE_PATH=data/valuedashboard.sqlite \
    ./.venv/Scripts/python.exe scripts/compare_price_domain_before_after.py --codes 200 --bench
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 对拍时**排除**的列：每次现算的时间戳，两侧必然不同（但都必须是 datetime）。
#: `data_version` 不排除 —— 口径版本必须逐字相同。
VOLATILE_COLUMNS = ("calculated_at",)


class ComparisonError(RuntimeError):
    pass


def _load_baseline_module(destination: Path) -> Any:
    """把 HEAD 版 `calculator.py` 落盘并按独立模块导入（真·改前实现）。"""
    completed = subprocess.run(
        ["git", "show", "HEAD:app/core/indicators/calculator.py"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        raise ComparisonError(
            "取不到 HEAD 版 calculator.py（git show 失败）：" + completed.stderr.strip()
        )
    path = destination / "_baseline_calculator.py"
    path.write_text(completed.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_vd_baseline_calculator", path)
    if spec is None or spec.loader is None:
        raise ComparisonError("无法为基线模块建立 import spec")
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` 在 exec 期间会回查 `sys.modules[cls.__module__]`，必须先登记。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _build_temp_paths(
    duckdb_path: Path, sqlite_path: Path, temp_root: Path,
) -> tuple[Any, Path]:
    """真库只读（硬链接）+ 临时 SQLite 副本，返回校验通过的 DatabasePathSet。

    硬链接必须与目标同一个卷，所以临时根默认放在正式库所在盘、仓库之外
    （`_default_temp_root`）。硬链接失败（跨卷/权限）时退回"直接只读打开正式库"：
    两个 store 的读路径都是 `read_only=True` / `mode=ro`，都不会写正式库。
    """
    from app.core.storage.path_policy import DatabasePathSet, VdEnv, resolve_and_validate_paths

    linked_duck = temp_root / "valuedashboard.duckdb"
    copied_sqlite = temp_root / "valuedashboard.sqlite"
    if linked_duck.exists():
        linked_duck.unlink()
    try:
        os.link(duckdb_path, linked_duck)
    except OSError as error:  # 跨卷等情况下退回"直接只读打开正式库"
        print(f"!! 正式库硬链接失败（{error}），改为直接只读打开正式库", file=sys.stderr)
        return resolve_and_validate_paths(), temp_root
    shutil.copy2(sqlite_path, copied_sqlite)
    return DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=linked_duck,
        sqlite_path=copied_sqlite,
        run_root=temp_root,
    ).validate(), temp_root


def _default_temp_root(duckdb_path: Path) -> Path:
    """默认临时根：与正式库**同一个卷**，且在仓库之外（TEST 档要求）。"""
    # data/ 的父是仓库、祖父是仓库外目录（TEST 档要求临时根在仓库之外）。
    return Path(tempfile.mkdtemp(
        prefix="_vd_price_compare_", dir=str(duckdb_path.parent.parent.parent),
    ))


def _in_place_paths(duckdb_path: Path, sqlite_path: Path) -> Any:
    """直接用给定的库对（测试 fixture 已经是校验过的隔离根）。"""
    from app.core.storage.path_policy import DatabasePathSet, VdEnv

    return DatabasePathSet(
        env=VdEnv.TEST,
        duckdb_path=duckdb_path,
        sqlite_path=sqlite_path,
        run_root=duckdb_path.parent,
    ).validate()


def _make_calculator(calculator_class: Any, paths: Any) -> Any:
    from app.core.storage.duckdb_store import DuckDBStore
    from app.core.storage.sqlite_store import SQLiteStore

    duck = DuckDBStore(paths=paths)
    sqlite = SQLiteStore(paths=paths)
    return calculator_class(duck=duck, sqlite=sqlite, paths=paths)


def _sample(calculator: Any, count: int) -> list[tuple[str, Any]]:
    rows = list(calculator.duck.read_query(
        "SELECT stock_code, report_date FROM indicator_snapshot ORDER BY stock_code",
    ))
    if not rows:
        raise ComparisonError("indicator_snapshot 为空")
    if count >= len(rows):
        picked = rows
    else:
        step = len(rows) / float(count)
        picked = [rows[min(int(index * step), len(rows) - 1)] for index in range(count)]
    return [(row["stock_code"], row["report_date"]) for row in picked]


def _rows_for_codes(calculator: Any, codes: list[str]) -> list[tuple[str, Any]]:
    placeholders = ", ".join("?" for _ in codes)
    rows = calculator.duck.read_query(
        f"""SELECT stock_code, report_date FROM indicator_snapshot
            WHERE stock_code IN ({placeholders}) ORDER BY stock_code""",
        list(codes),
    )
    return [(row["stock_code"], row["report_date"]) for row in rows]


@contextlib.contextmanager
def _shared_read_connection(calculator: Any) -> Any:
    """共用一条只读连接 = `compute_price_sensitive_for_codes` 的生产条件。

    不这么做的话每次查询都是一次冷开（正式库量级 ~49 ms），测出来的差异
    主要是连接开销而不是取数次数。
    """
    with calculator.duck.read_connection() as connection:
        calculator._calculation_read_connection = connection
        try:
            yield
        finally:
            del calculator._calculation_read_connection


def _run_fallback(calculator: Any, codes: list[tuple[str, Any]]) -> dict[str, dict[str, Any]]:
    """**不开**预取批次：每个 `_get_xxx` 都走"缓存 miss → 原逐股查询"。"""
    out: dict[str, dict[str, Any]] = {}
    with _shared_read_connection(calculator):
        for code, report_date in codes:
            try:
                out[code] = calculator.compute_price_fields_for_stock(code, report_date)
            except Exception as error:  # noqa: BLE001 - 对拍要看到"哪只怎么死的"
                out[code] = {"__error__": f"{type(error).__name__}: {error}"}
    return out


def _run_after(
    calculator: Any,
    codes: list[tuple[str, Any]],
    *,
    chunk: int | None = None,
) -> dict[str, dict[str, Any]]:
    """改后实现，按生产调用方式跑：分批预取 + 分批丢弃缓存。"""
    size = chunk or calculator._PREFETCH_BATCH_CODES
    out: dict[str, dict[str, Any]] = {}
    with _shared_read_connection(calculator):
        for start in range(0, len(codes), size):
            window = codes[start:start + size]
            with calculator.price_domain_batch(
                [code for code, _ in window],
                report_dates={code: report_date for code, report_date in window},
            ):
                for code, report_date in window:
                    try:
                        out[code] = calculator.compute_price_fields_for_stock(
                            code, report_date,
                        )
                    except Exception as error:  # noqa: BLE001
                        out[code] = {"__error__": f"{type(error).__name__}: {error}"}
    return out


def _compare(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    columns: set[str] = set()
    for code in before:
        columns.update(before[code])
    for code in after:
        columns.update(after[code])
    checked_columns = set(columns) - set(VOLATILE_COLUMNS)

    for code, old in before.items():
        new = after.get(code)
        if new is None:
            mismatches.append({"stock_code": code, "kind": "missing_in_after"})
            continue
        old_keys = set(old) - set(VOLATILE_COLUMNS)
        new_keys = set(new) - set(VOLATILE_COLUMNS)
        if old_keys != new_keys:
            mismatches.append({
                "stock_code": code, "kind": "column_set",
                "only_before": sorted(old_keys - new_keys),
                "only_after": sorted(new_keys - old_keys),
            })
            continue
        for column in sorted(old_keys):
            left, right = old[column], new[column]
            # `==` 而不是 approx：口径没变，就应该 bit 级相同。
            if left != right or type(left) is not type(right):
                mismatches.append({
                    "stock_code": code, "kind": "value", "column": column,
                    "before": repr(left), "after": repr(right),
                    "before_type": type(left).__name__,
                    "after_type": type(right).__name__,
                })
        # 时间戳列只要求同类型（两侧都是"现在"）
        for column in VOLATILE_COLUMNS:
            if column in old and column in new and not isinstance(new[column], dt.datetime):
                mismatches.append({
                    "stock_code": code, "kind": "volatile_type", "column": column,
                })
    # 覆盖度：mismatch=0 只有在"列里确实有值"时才有意义（全 NULL 对全 NULL 也是 0）。
    coverage: dict[str, int] = {}
    for column in sorted(checked_columns):
        coverage[column] = sum(
            1 for row in before.values()
            if isinstance(row, dict) and row.get(column) is not None
        )
    return {
        "non_null_by_column": coverage,
        "stocks_before": len(before),
        "stocks_after": len(after),
        "columns": len(checked_columns),
        "column_names": sorted(checked_columns),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }


def _time_arm(fn: Any, codes: list[tuple[str, Any]]) -> dict[str, float]:
    started = time.perf_counter()
    fn(codes)
    elapsed = time.perf_counter() - started
    return {"seconds": elapsed, "per_stock_ms": elapsed * 1000.0 / max(1, len(codes))}


def compare_price_domain(
    duckdb_path: Path,
    sqlite_path: Path,
    *,
    sample: int = 200,
    codes: list[str] | None = None,
    baseline: str = "git",
    bench: bool = False,
    bench_codes: int = 0,
    scale: int = 5563,
    chunk: int | None = None,
    temp_root: Path | None = None,
    use_temp_copy: bool = True,
    stream: Any = None,
) -> dict[str, Any]:
    """对拍（+可选测速）价格域改前/改后，返回报告 dict。

    Args:
        baseline: ``"git"`` = HEAD 版实现（真·改前）；``"fallback"`` = 工作区
            实现但不进预取批次；``"auto"`` = 能取到 HEAD 就用 git，否则 fallback。
        use_temp_copy: True = 正式库只读 + 临时 SQLite 副本（默认）；
            False = 直接读给定库对（测试 fixture 已是隔离根）。
    """
    from app.core.indicators.calculator import IndicatorCalculator

    log = stream or sys.stdout
    report: dict[str, Any] = {}
    work_dir = temp_root
    owns_temp = False
    if use_temp_copy:
        if work_dir is None:
            work_dir = _default_temp_root(duckdb_path)
            owns_temp = True
        work_dir.mkdir(parents=True, exist_ok=True)
        paths, work_dir = _build_temp_paths(duckdb_path, sqlite_path, work_dir)
    else:
        paths = _in_place_paths(duckdb_path, sqlite_path)
        if work_dir is None:
            work_dir = Path(tempfile.mkdtemp(prefix="_vd_price_compare_work_"))
            owns_temp = True
        work_dir.mkdir(parents=True, exist_ok=True)
    report["paths"] = {"duckdb": str(paths.duckdb_path), "sqlite": str(paths.sqlite_path)}
    print(f"对拍数据源: DuckDB={paths.duckdb_path} SQLite={paths.sqlite_path}", file=log)

    try:
        after_calc = _make_calculator(IndicatorCalculator, paths)
        before_calc: Any = None
        resolved_baseline = baseline
        if baseline in ("git", "auto"):
            try:
                module = _load_baseline_module(work_dir)
            except ComparisonError as error:
                if baseline == "git":
                    raise
                print(f"!! 取不到 HEAD 基线（{error}），退用 fallback 基线", file=log)
            else:
                before_calc = _make_calculator(module.IndicatorCalculator, paths)
                resolved_baseline = "git"
        if before_calc is None:
            before_calc = _make_calculator(IndicatorCalculator, paths)
            resolved_baseline = "fallback"
        report["baseline"] = resolved_baseline

        sample_rows = _rows_for_codes(after_calc, codes) if codes else _sample(after_calc, sample)
        if not sample_rows:
            raise ComparisonError("对拍样本为空")
        print(
            f"对拍样本 {len(sample_rows)} 只（基线={resolved_baseline}）："
            f"{sample_rows[0][0]} … {sample_rows[-1][0]}",
            file=log,
        )

        run_before = (
            (lambda window: _run_baseline(before_calc, window))
            if resolved_baseline == "git"
            else (lambda window: _run_fallback(before_calc, window))
        )
        before = run_before(sample_rows)
        after = _run_after(after_calc, sample_rows, chunk=chunk)
        comparison = _compare(before, after)
        report["comparison"] = comparison
        print(
            f"对拍结果: 股票 {comparison['stocks_before']} 只 / 字段 {comparison['columns']} 列 / "
            f"mismatch={comparison['mismatch_count']}",
            file=log,
        )
        for item in comparison["mismatches"][:10]:
            print("  ✗", json.dumps(item, ensure_ascii=False), file=log)

        if bench:
            bench_rows = _sample(after_calc, bench_codes) if bench_codes else sample_rows
            print(f"测速样本 {len(bench_rows)} 只", file=log)
            # 预热（进程内 catalog / sqlite 页缓存），保证两个 arm 起点公平
            run_before(bench_rows[:5])
            _run_after(after_calc, bench_rows[:5], chunk=chunk)
            timings = []
            for label, fn in (
                ("before", run_before),
                ("after", lambda window: _run_after(after_calc, window, chunk=chunk)),
            ):
                arm = _time_arm(fn, bench_rows)
                arm["label"] = label
                timings.append(arm)
                print(
                    f"  {label:<7} {arm['seconds']:8.2f}s  "
                    f"({arm['per_stock_ms']:.2f} ms/只) → "
                    f"{arm['per_stock_ms'] * scale / 1000.0 / 60.0:.2f} min / {scale} 只",
                    file=log,
                )
            before_s, after_s = timings[0]["seconds"], timings[1]["seconds"]
            speedup = before_s / after_s if after_s else float("inf")
            print(f"  加速比: {speedup:.2f}×", file=log)
            report["bench"] = {
                "codes": len(bench_rows), "scale": scale,
                "arms": timings, "speedup": speedup,
            }
        return report
    finally:
        if owns_temp and work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


def _run_baseline(calculator: Any, codes: list[tuple[str, Any]]) -> dict[str, dict[str, Any]]:
    """HEAD 版实现（它没有批次预取，逐股 13 条 SQL）。"""
    out: dict[str, dict[str, Any]] = {}
    with _shared_read_connection(calculator):
        for code, report_date in codes:
            try:
                out[code] = calculator.compute_price_fields_for_stock(code, report_date)
            except Exception as error:  # noqa: BLE001
                out[code] = {"__error__": f"{type(error).__name__}: {error}"}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", type=int, default=200)
    parser.add_argument("--bench", action="store_true", help="顺便测改前/改后耗时")
    parser.add_argument(
        "--bench-codes", type=int, default=0,
        help="测速样本数（默认 = --codes；可单独放大到 2000）",
    )
    parser.add_argument("--scale", type=int, default=5563, help="折算全市场只数")
    parser.add_argument("--chunk", type=int, default=0, help="改后 arm 的预取批次大小")
    parser.add_argument("--baseline", default="git", choices=("git", "fallback", "auto"))
    parser.add_argument("--duckdb", default=None, help="显式指定 DuckDB（默认按 VD_ENV 解析）")
    parser.add_argument("--sqlite", default=None, help="显式指定 SQLite（默认按 VD_ENV 解析）")
    parser.add_argument("--in-place", action="store_true", help="不做临时副本，直接读给定库对")
    parser.add_argument("--json", default=None)
    parser.add_argument("--temp-root", default=None)
    args = parser.parse_args(argv)

    from app.core.storage.path_policy import resolve_and_validate_paths

    if args.duckdb and args.sqlite:
        duckdb_path, sqlite_path = Path(args.duckdb), Path(args.sqlite)
    else:
        resolved = resolve_and_validate_paths()
        duckdb_path, sqlite_path = resolved.duckdb_path, resolved.sqlite_path
    report = compare_price_domain(
        duckdb_path, sqlite_path,
        sample=args.codes,
        baseline=args.baseline,
        bench=args.bench,
        bench_codes=args.bench_codes,
        scale=args.scale,
        chunk=args.chunk or None,
        temp_root=Path(args.temp_root) if args.temp_root else None,
        use_temp_copy=not args.in_place,
    )
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"已写 {args.json}")
    return 0 if report["comparison"]["mismatch_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
