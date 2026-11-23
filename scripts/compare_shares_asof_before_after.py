"""对比 `market_cap.resolve_shares_asof`「改动前（逐股）」与「改动后（批量 API）」。

为什么值得单独写一个对拍脚本
----------------------------
股本（`total_shares`）是**估值分母**：市值 = 股本 × 价格，再派生出 PE / PB / PS / PCF。
这里错一点，全市场估值指标就全错。而优化恰恰动了这块的取数路径
（逐股一条 as-of SQL → 一条批量 SQL + `ROW_NUMBER()` 窗口）。

所以验收不能只看"价格域 33 列对拍通过"——那是在验证**整体**；
本脚本验证的是**这一个人 API** 在**全库 × 多个 as_of** 上的逐条等价性，
含三类边界样本：
  * 有历史行 → `source='history'`（且**绝不许**选中 as_of 之后才生效的行）
  * 历史缺行 → 回退 `stock_meta.total_shares` → `source='meta'`
  * 两处都没有 → `shares=None`, `source='none'`
  * `price_date=None` → 不做 as-of，直接走 meta 回退（防未来数据泄漏）

"改动前"的真源
--------------
用 `git show HEAD:app/core/market_cap.py` 导出**真·旧实现**再 import，
不凭记忆重写一遍旧逻辑（否则对拍就变成"自己跟自己比"）。

用法
----
    ./.venv/Scripts/python.exe scripts/compare_shares_asof_before_after.py
    ./.venv/Scripts/python.exe scripts/compare_shares_asof_before_after.py --as-of 2026-11-20,2020-01-02
    ./.venv/Scripts/python.exe scripts/compare_shares_asof_before_after.py --limit 500

只读：正式库以 `read_only` 打开；不写任何表。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.storage.path_policy import (  # noqa: E402 - 必须先插入 sys.path
    DatabasePathSet,
    VdEnv,
    resolve_and_validate_paths,
)


def load_old_module() -> object:
    """从 git HEAD 导出改动前的 `market_cap` 并 import（真·旧实现）。"""
    source = subprocess.run(
        ["git", "show", "HEAD:app/core/market_cap.py"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", check=True,
    ).stdout
    target = Path(tempfile.mkdtemp()) / "old_market_cap.py"
    target.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("old_market_cap", target)
    module = importlib.util.module_from_spec(spec)
    # dataclass 需要模块已登记在 sys.modules 里才能解析注解
    sys.modules["old_market_cap"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只比前 N 只（0=全库）")
    parser.add_argument(
        "--as-of", default="2026-11-20,2020-01-02,none",
        help="逗号分隔的 as_of 列表；`none` 表示 price_date=None",
    )
    parser.add_argument("--bench", action="store_true", help="顺便对比耗时")
    args = parser.parse_args()

    from app.core.storage.duckdb_store import DuckDBStore


    dates: list[str | None] = [
        None if item.strip().lower() in ("none", "null", "") else item.strip()
        for item in args.as_of.split(",")
    ]

    old = load_old_module()
    paths, temp_root = _isolated_paths()
    try:
        duck = DuckDBStore(paths=paths)
        return _compare(duck, old, dates, args)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _isolated_paths():
    """正式库硬链接 + SQLite 副本 → 仓库外的临时 TEST 根。

    为什么不能直接 `resolve_and_validate_paths()`：正式档要求 run_root 是仓库的
    `data/`，而服务在跑时它**独占**着 DuckDB（跨进程读会 `Cannot open file`）。
    硬链接让测试进程读到的仍是同一份物理页，但走独立的库句柄；SQLite 用副本，
    保证对拍过程不写正式库一个字。
    """
    formal = resolve_and_validate_paths()
    temp_root = Path(tempfile.mkdtemp(
        prefix="_vd_shares_cmp_", dir=str(formal.duckdb_path.parent.parent.parent),
    ))
    linked = temp_root / "valuedashboard.duckdb"
    try:
        os.link(formal.duckdb_path, linked)
    except OSError as error:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise SystemExit(f"正式库硬链接失败（{error}）：为避免争抢写者，本脚本不直接开正式库") from error
    copied = temp_root / "valuedashboard.sqlite"
    shutil.copy2(formal.sqlite_path, copied)
    paths = DatabasePathSet(
        env=VdEnv.TEST, duckdb_path=linked,
        sqlite_path=copied, run_root=temp_root,
    ).validate()
    return paths, temp_root


def _compare(duck, old, dates: list[str | None], args) -> int:
    from app.core.market_cap import resolve_shares_asof_batch

    codes = [
        str(row["stock_code"]) for row in duck.read_query(
            "SELECT stock_code FROM stock_meta ORDER BY stock_code"
        )
    ]
    if args.limit > 0:
        codes = codes[: args.limit]
    print(f"全库 {len(codes)} 只 × {len(dates)} 个 as_of = {len(codes) * len(dates)} 条请求")

    started = time.perf_counter()
    old_map = {
        (code, date): old.resolve_shares_asof(duck, code, date)  # type: ignore[attr-defined]
        for date in dates for code in codes
    }
    old_elapsed = time.perf_counter() - started

    started = time.perf_counter()
    new_map = {}
    for date in dates:
        resolved = resolve_shares_asof_batch(duck, [(code, date) for code in codes])
        for code in codes:
            new_map[(code, date)] = resolved[code]
    new_elapsed = time.perf_counter() - started

    mismatches = [
        key for key in old_map
        if old_map[key].shares != new_map[key].shares
        or old_map[key].source != new_map[key].source
        or old_map[key].effective_date != new_map[key].effective_date
        or type(old_map[key].shares) is not type(new_map[key].shares)
    ]
    print(f"对拍: {len(old_map)} 条 | mismatch={len(mismatches)}")
    for key in mismatches[:5]:
        print(f"  MISMATCH {key}: 旧 {old_map[key]} → 新 {new_map[key]}")
    print("  source 分布:", dict(Counter(item.source for item in old_map.values())))
    if args.bench:
        print(
            f"  耗时: 旧 {old_elapsed:.2f}s → 新 {new_elapsed:.2f}s "
            f"({old_elapsed / max(new_elapsed, 1e-9):.1f}×)"
        )
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
