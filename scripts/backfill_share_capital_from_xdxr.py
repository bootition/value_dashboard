"""回填 ``share_capital_history`` 漏记的送转事件（默认 dry-run）。

根因（2026-11-16 实测，正式库只读）
-----------------------------------
``share_capital_history`` 是**逐股增量抓取**的股本链：每只股票只更新到它
最后一次成功抓取的日期，链条末端**之后**发生的送转/增发/回购事件永远进不来，
ASOF 取到的仍是送转前的股本。典型样本 ``001225``：链条最后一条
2026-05-30 = 65,570,800 股，而 ``xdxr`` 在 2026-09-23 有 ``songzhuangu=0.4``，
``stock_meta.total_shares = 91,799,120``（= 65,570,800 × 1.4）——送转没有写进
股本历史，于是「历史股本口径」的总市值比「当前股本口径」低 40%。

``xdxr`` 里 ``songzhuangu > 0`` 的除权除息事件是送转的权威事件源，足以把
链条末端这一段补齐。

口径（唯一，不要在各处重写）
----------------------------
- 只处理 ``xdxr.songzhuangu > 0`` 的事件，按 ``(stock_code, event_date)`` 去重；
- 送转后总股本 = 该日**之前**最近一条 ``share_capital_history.total_shares``
  × ``(1 + songzhuangu)``，取整到股；
- 缺前值（链条起点晚于事件）/ 缺比例 / 推算值与「后一行」或 ``stock_meta``
  偏差 > ``--tolerance``（默认 1%）→ 跳过并分类计数，**绝不猜**；
- **只增不改**：``(stock_code, event_date)`` 已有行 → 跳过，绝不覆盖既有值。

安全
----
- 默认 **dry-run**：只读、只打印计划，不写库、不落备份；
- ``--yes`` 才写：写前 ``exclusive_update`` 抢单写者锁 + ``COPY
  share_capital_history TO .planning/...`` parquet 备份；一个事务内
  ``INSERT ... ON CONFLICT DO NOTHING``，并核对写入前后行数；
- 正式库只由主控执行，本脚本不自动跑。

用法::

    # 1) 看计划（只读）
    .venv\\Scripts\\python.exe scripts\\backfill_share_capital_from_xdxr.py

    # 2) 确认后写库（单写者窗口，服务应停）
    .venv\\Scripts\\python.exe scripts\\backfill_share_capital_from_xdxr.py --yes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    DatabasePathSet,
    PathIsolationError,
    resolve_and_validate_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    any_write_lock_active,
    exclusive_update,
)

logger = logging.getLogger("backfill_share_capital_from_xdxr")

SOURCE = "xdxr_backfill"
REASON_PREFIX = "xdxr_songzhuangu"

# 跳过原因（计数键必须稳定，主控按它判断该补多少行）
SKIP_EXISTS = "skip_already_recorded"
SKIP_NO_PREV = "skip_no_previous_shares"
SKIP_NO_RATIO = "skip_invalid_ratio"
SKIP_CONFLICT_NEXT = "skip_conflicts_with_next_row"
SKIP_CONFLICT_META = "skip_conflicts_with_stock_meta"
SKIP_MISSING_META = "skip_unverifiable_tail_no_meta"

_CANDIDATE_SQL = """
    WITH bonus AS (
        SELECT stock_code, event_date, MAX(songzhuangu) AS songzhuangu
        FROM xdxr
        WHERE songzhuangu IS NOT NULL AND songzhuangu > 0
        GROUP BY stock_code, event_date
    )
    SELECT b.stock_code                       AS stock_code,
           b.event_date                       AS event_date,
           b.songzhuangu                      AS songzhuangu,
           (SELECT h.total_shares FROM share_capital_history h
             WHERE h.stock_code = b.stock_code AND h.effective_date < b.event_date
             ORDER BY h.effective_date DESC LIMIT 1)      AS prev_shares,
           (SELECT h.effective_date FROM share_capital_history h
             WHERE h.stock_code = b.stock_code AND h.effective_date < b.event_date
             ORDER BY h.effective_date DESC LIMIT 1)      AS prev_date,
           (SELECT h.total_shares FROM share_capital_history h
             WHERE h.stock_code = b.stock_code AND h.effective_date > b.event_date
             ORDER BY h.effective_date ASC LIMIT 1)       AS next_shares,
           (SELECT h.effective_date FROM share_capital_history h
             WHERE h.stock_code = b.stock_code AND h.effective_date > b.event_date
             ORDER BY h.effective_date ASC LIMIT 1)       AS next_date,
           (SELECT h.total_shares FROM share_capital_history h
             WHERE h.stock_code = b.stock_code AND h.effective_date = b.event_date
             LIMIT 1)                                     AS existing_shares,
           (SELECT m.total_shares FROM stock_meta m
             WHERE m.stock_code = b.stock_code)           AS meta_shares
    FROM bonus b
    ORDER BY b.stock_code, b.event_date
"""

_INSERT_SQL = """
    INSERT INTO share_capital_history
        (stock_code, effective_date, total_shares, change_reason,
         is_anchor, verified, source, raw_hash, batch_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (stock_code, effective_date) DO NOTHING
"""


@dataclass
class Plan:
    """回填计划（纯数据结构，便于 dry-run 打印与单测）。"""

    inserts: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    events: int = 0

    def count(self, reason: str) -> int:
        return self.skipped.get(reason, 0)

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())

    @property
    def stocks(self) -> int:
        return len({row["stock_code"] for row in self.inserts})

    def summary(self) -> dict[str, Any]:
        return {
            "bonus_events": self.events,
            "rows_to_insert": len(self.inserts),
            "rows_skipped": self.skipped_total,
            "stocks_affected": self.stocks,
            "skipped_by_reason": dict(sorted(self.skipped.items())),
        }


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _derived_row(raw: dict[str, Any], batch_id: str) -> dict[str, Any] | None:
    """把一条候选事件换算成待插入行；缺前值/缺比例返回 None。"""
    prev = _finite(raw.get("prev_shares"))
    ratio = _finite(raw.get("songzhuangu"))
    if prev is None or prev <= 0:
        return None
    if ratio is None or ratio <= 0:
        return None
    shares = float(round(prev * (1.0 + ratio)))
    if shares <= 0:
        return None
    stock_code = str(raw["stock_code"])
    event_date = raw["event_date"]
    event_day = event_date if isinstance(event_date, date) else date.fromisoformat(str(event_date)[:10])
    payload = "|".join([
        REASON_PREFIX, stock_code, event_day.isoformat(),
        str(raw.get("prev_date")), repr(prev), repr(ratio), repr(shares),
    ])
    return {
        "stock_code": stock_code,
        "effective_date": event_day,
        "total_shares": shares,
        "change_reason": f"{REASON_PREFIX}={ratio:g}",
        "is_anchor": False,
        "verified": False,
        "source": SOURCE,
        "raw_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "batch_id": batch_id,
        "prev_shares": prev,
        "prev_date": raw.get("prev_date"),
        "songzhuangu": ratio,
        "next_shares": _finite(raw.get("next_shares")),
        "next_date": raw.get("next_date"),
        "meta_shares": _finite(raw.get("meta_shares")),
    }


def build_plan(rows: list[dict[str, Any]], *, tolerance: float, batch_id: str) -> Plan:
    """按「只增不改 + 可推算 + 可核验」构造回填计划（纯函数，无 I/O）。"""
    plan = Plan(events=len(rows))
    for raw in rows:
        existing = _finite(raw.get("existing_shares"))
        if existing is not None:
            plan.skipped[SKIP_EXISTS] = plan.count(SKIP_EXISTS) + 1
            continue
        row = _derived_row(raw, batch_id)
        if row is None:
            prev = _finite(raw.get("prev_shares"))
            ratio = _finite(raw.get("songzhuangu"))
            if prev is None or prev <= 0:
                reason = SKIP_NO_PREV
            elif ratio is None or ratio <= 0:
                reason = SKIP_NO_RATIO
            else:  # 推算结果非正（比例异常），按「不可推算」计
                reason = SKIP_NO_PREV
            plan.skipped[reason] = plan.count(reason) + 1
            continue

        shares = row["total_shares"]
        next_shares = row["next_shares"]
        meta_shares = row["meta_shares"]
        if next_shares is not None and next_shares > 0:
            if abs(shares / next_shares - 1.0) > tolerance:
                plan.skipped[SKIP_CONFLICT_NEXT] = plan.count(SKIP_CONFLICT_NEXT) + 1
                plan.samples.setdefault(SKIP_CONFLICT_NEXT, []).append(_sample(row))
                continue
        elif meta_shares is not None and meta_shares > 0:
            if abs(shares / meta_shares - 1.0) > tolerance:
                plan.skipped[SKIP_CONFLICT_META] = plan.count(SKIP_CONFLICT_META) + 1
                plan.samples.setdefault(SKIP_CONFLICT_META, []).append(_sample(row))
                continue
        else:
            # 链条末端且没有 stock_meta 可比 → 无任何交叉证据，不猜
            plan.skipped[SKIP_MISSING_META] = plan.count(SKIP_MISSING_META) + 1
            plan.samples.setdefault(SKIP_MISSING_META, []).append(_sample(row))
            continue
        plan.inserts.append(row)
    plan.inserts.sort(key=lambda item: (item["stock_code"], item["effective_date"]))
    return plan


def _sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "stock_code": row["stock_code"],
        "effective_date": str(row["effective_date"]),
        "songzhuangu": row["songzhuangu"],
        "prev_shares": row["prev_shares"],
        "derived_shares": row["total_shares"],
        "next_shares": row["next_shares"],
        "meta_shares": row["meta_shares"],
    }


def load_candidates(duck: DuckDBStore) -> list[dict[str, Any]]:
    return duck.read_query(_CANDIDATE_SQL)


def assert_share_capital_history_exists(duck: DuckDBStore) -> None:
    rows = duck.read_query(
        "SELECT COUNT(*) AS n FROM duckdb_tables() WHERE table_name = 'share_capital_history'"
    )
    if not rows or not rows[0]["n"]:
        raise SystemExit(
            "[ABORT] 库里没有 share_capital_history（需要先跑 schema 初始化），拒绝运行。"
        )


def backup_share_capital_history(duck: DuckDBStore, backup_dir: Path) -> Path:
    """写前把 share_capital_history 整表 COPY 成 parquet（回滚出口）。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    batch_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = (backup_dir / f"share_capital_history-{batch_id}.parquet").as_posix()
    with duck.write_connection() as conn:
        conn.execute(f"COPY share_capital_history TO '{target}' (FORMAT PARQUET)")
    logger.info("[BACKUP] share_capital_history → %s", target)
    return Path(target)


def apply_plan(duck: DuckDBStore, plan: Plan) -> dict[str, Any]:
    """单事务写入（ON CONFLICT DO NOTHING），返回实际写入行数与被守住的行数。"""
    if not plan.inserts:
        return {"inserted": 0, "kept_existing": 0, "rows_after": None}
    rows_before = int(duck.read_query("SELECT COUNT(*) AS n FROM share_capital_history")[0]["n"])
    params = [
        [
            row["stock_code"], row["effective_date"], row["total_shares"],
            row["change_reason"], row["is_anchor"], row["verified"],
            row["source"], row["raw_hash"], row["batch_id"],
        ]
        for row in plan.inserts
    ]
    with duck.transaction() as conn:
        conn.executemany(_INSERT_SQL, params)
        rows_after = int(
            conn.execute("SELECT COUNT(*) FROM share_capital_history").fetchone()[0]
        )
    inserted = rows_after - rows_before
    return {
        "inserted": inserted,
        "kept_existing": len(plan.inserts) - inserted,
        "rows_after": rows_after,
    }


def format_plan(plan: Plan, mode: str) -> str:
    summary = plan.summary()
    lines = [
        f"模式: {mode}",
        f"xdxr 送转事件（songzhuangu>0，按股票×日期去重）: {summary['bonus_events']} 条",
        f"待插入: {summary['rows_to_insert']} 行 / 受影响股票 {summary['stocks_affected']} 只",
        f"跳过: {summary['rows_skipped']} 行",
    ]
    for reason, count in summary["skipped_by_reason"].items():
        lines.append(f"  - {reason}: {count}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="用 xdxr 送转事件回填 share_capital_history（默认 dry-run，只增不改）",
    )
    ap.add_argument("--yes", action="store_true", help="确认写库（默认 dry-run，只读）")
    ap.add_argument(
        "--tolerance", type=float, default=0.01,
        help="推算股本与后一行/stock_meta 的相对容差，超限即跳过（默认 1%%）",
    )
    ap.add_argument("--sample", type=int, default=10, help="打印多少条待插入样本（默认 10）")
    ap.add_argument("--limit", type=int, default=0, help="最多插入N行（0=不限，排障用）")
    ap.add_argument(
        "--backup-dir", type=Path, default=None,
        help="写前备份目录（默认 .planning/maintenance-backup-xdxr-share-capital-<ts>）",
    )
    ap.add_argument("--json-out", type=Path, default=None, help="把计划/结果写成 JSON 证据")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.tolerance <= 0 or args.tolerance >= 1:
        print("[ABORT] --tolerance 必须在 (0, 1) 之间。", flush=True)
        return 2
    try:
        paths: DatabasePathSet = resolve_and_validate_paths()
    except PathIsolationError as error:
        print(f"[ABORT] 数据库路径未通过隔离校验: {error}", flush=True)
        return 2

    duck = DuckDBStore(paths=paths)
    assert_share_capital_history_exists(duck)

    batch_id = f"xdxr-bonus-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    rows = load_candidates(duck)
    plan = build_plan(rows, tolerance=args.tolerance, batch_id=batch_id)
    if args.limit and args.limit > 0:
        plan.inserts = plan.inserts[: args.limit]

    mode = "写库模式（--yes）" if args.yes else "只读 dry-run（不写库）"
    print(f"数据库: {paths.duckdb_path}（env={paths.env.value}）", flush=True)
    print(format_plan(plan, mode), flush=True)

    if plan.inserts:
        print("\n待插入样本（stock_code, 事件日, 送转比例, 前值 → 推算股本）:", flush=True)
        for row in plan.inserts[: max(0, args.sample)]:
            print(
                f"  {row['stock_code']} {row['effective_date']} songzhuangu={row['songzhuangu']:g} "
                f"{int(row['prev_shares'])} → {int(row['total_shares'])}"
                f"（next={row['next_shares']}, meta={row['meta_shares']}）",
                flush=True,
            )
    for reason, samples in sorted(plan.samples.items()):
        print(f"\n跳过样本 {reason}:", flush=True)
        for sample in samples[: max(0, args.sample)]:
            print(f"  {sample}", flush=True)

    report: dict[str, Any] = {
        "task": "用 xdxr 送转事件回填 share_capital_history（只增不改）",
        "executed_at": datetime.now(UTC).isoformat(),
        "database": str(paths.duckdb_path),
        "env": paths.env.value,
        "dry_run": not args.yes,
        "tolerance": args.tolerance,
        "batch_id": batch_id,
        "plan": plan.summary(),
        "samples": {
            "inserts": [_sample(row) for row in plan.inserts[: max(0, args.sample)]],
            **plan.samples,
        },
    }

    if not args.yes:
        print(
            "\n只读 dry-run：未写库。确认无误后加 --yes 执行"
            "（写前 exclusive_update + COPY share_capital_history 到 .planning/）。",
            flush=True,
        )
        report["status"] = "dry_run"
        _write_json(args.json_out, report)
        return 0

    if not plan.inserts:
        print("\n没有需要写入的行（幂等：重复执行计数应为 0），跳过写库。", flush=True)
        report["status"] = "nothing_to_do"
        _write_json(args.json_out, report)
        return 0

    if any_write_lock_active(paths.duckdb_path):
        print("[ABORT] 检测到写锁生效（自动更新/其它维护脚本正在写库），拒绝运行。", flush=True)
        return 3

    backup_dir = args.backup_dir or (
        PROJECT_ROOT / ".planning"
        / f"maintenance-backup-xdxr-share-capital-{datetime.now():%Y%m%d_%H%M%S}"
    )
    try:
        with exclusive_update(paths.duckdb_path):
            backup_path = backup_share_capital_history(duck, backup_dir)
            written = apply_plan(duck, plan)
    except UpdateLockError as error:
        print(f"[ABORT] 抢写锁失败: {error}", flush=True)
        return 3

    print("\n写库完成:", flush=True)
    print(json.dumps(written, ensure_ascii=False, indent=2), flush=True)
    report.update({
        "status": "written",
        "backup": str(backup_path),
        "written": written,
    })
    _write_json(args.json_out, report)
    return 0


def _write_json(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    print(f"证据 JSON: {path}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
