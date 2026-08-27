"""修复 dividends_quarantine 中 ex_date 占位符行（STATUS 缺口 #4）。

背景（红队 80 P3）：2026-07-28 重建期间 50,359 行分红因无法核验
ex_date 被隔离到 dividends_quarantine（reason=unverified_period_end_placeholder），
ex_date 为期末占位（12-31 / 06-30）。xdxr（除权除息）表含真实 event_date，
category=1 与 fenhong（每股派息）可与隔离行按 股票+日期窗+派息额 匹配。

匹配规则（保守，fail-closed）：
  - 仅处理 dps>0 且 reason=unverified_period_end_placeholder 的行；
  - 候选：同股票 xdxr.category=1，event_date ∈ [占位日-30天, 占位日+460天]；
  - 派息一致：|fenhong-dps|<=0.005 或相对差<=1%；
  - 唯一候选才处理；歧义/缺失 → 保留隔离不动；
  - 目标 (stock, event_date) 已存在于 dividends 且派息一致 → 判定为重复件，
    仅从隔离表删除（去重）；不一致 → 保留隔离人工复核。

用法：
  python scripts/repair_dividend_ex_dates.py            # 只读 dry-run，输出匹配统计
  python scripts/repair_dividend_ex_dates.py --yes      # 先自动备份两表，再写库

写库前自动 parquet 备份 dividends + dividends_quarantine
（.planning/maintenance-backup-dividend-exdate-repair-<ts>/），证据 JSON 写入
docs/evidence/evidence-dividend-exdate-repair-<ts>.json。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import resolve_and_validate_paths  # noqa: E402
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402

WINDOW_BEFORE_DAYS = 30
WINDOW_AFTER_DAYS = 460
ABS_TOLERANCE = 0.005
REL_TOLERANCE = 0.01
REASON = "unverified_period_end_placeholder"

EVIDENCE_DIR = PROJECT_ROOT / "docs" / "evidence"


def _dps_matches(quarantine_dps: float, candidate_dps: float) -> bool:
    if candidate_dps is None:
        return False
    diff = abs(candidate_dps - quarantine_dps)
    if diff <= ABS_TOLERANCE:
        return True
    return quarantine_dps > 0 and diff / quarantine_dps <= REL_TOLERANCE


def collect_matches(duck: DuckDBStore) -> dict:
    rows = duck.read_query(
        """SELECT stock_code, ex_date, announcement_date, dividend_per_share,
                  stock_dividend, transfer_share, rights_issue, rights_issue_price
           FROM dividends_quarantine
           WHERE quarantine_reason = ?
             AND dividend_per_share IS NOT NULL AND dividend_per_share > 0""",
        [REASON],
    )
    candidates = duck.read_query(
        """SELECT stock_code, event_date, fenhong
           FROM xdxr WHERE category = 1 AND fenhong IS NOT NULL"""
    )
    by_stock: dict[str, list[dict]] = {}
    for row in candidates:
        by_stock.setdefault(row["stock_code"], []).append(row)

    matches: list[dict] = []
    duplicates: list[dict] = []
    skipped_no_candidate = 0
    skipped_ambiguous = 0
    skipped_conflicting_existing = 0

    existing_by_key = {
        (row["stock_code"], row["ex_date"]): row["dividend_per_share"]
        for row in duck.read_query(
            "SELECT stock_code, ex_date, dividend_per_share FROM dividends"
        )
    }

    for row in rows:
        stock = row["stock_code"]
        placeholder = row["ex_date"]
        if not isinstance(placeholder, (date, datetime)):
            skipped_no_candidate += 1
            continue
        if isinstance(placeholder, datetime):
            placeholder = placeholder.date()
        window_lo = placeholder - timedelta(days=WINDOW_BEFORE_DAYS)
        window_hi = placeholder + timedelta(days=WINDOW_AFTER_DAYS)
        dps = float(row["dividend_per_share"])
        hits = [
            c
            for c in by_stock.get(stock, [])
            if window_lo <= c["event_date"] <= window_hi and _dps_matches(dps, float(c["fenhong"]))
        ]
        if not hits:
            skipped_no_candidate += 1
            continue
        # 按派息差排序，取最接近者；并列最接近则视为歧义
        hits_sorted = sorted(hits, key=lambda c: abs(float(c["fenhong"]) - dps))
        best_diff = abs(float(hits_sorted[0]["fenhong"]) - dps)
        best = [c for c in hits_sorted if abs(float(c["fenhong"]) - dps) <= best_diff + 1e-9]
        if len(best) != 1:
            skipped_ambiguous += 1
            continue
        event_date = best[0]["event_date"]
        payload = {
            "stock_code": stock,
            "placeholder_ex_date": str(placeholder),
            "ex_date": str(event_date),
            "announcement_date": (
                str(row["announcement_date"]) if row["announcement_date"] is not None else None
            ),
            "dividend_per_share": dps,
            "stock_dividend": row["stock_dividend"],
            "transfer_share": row["transfer_share"],
            "rights_issue": row["rights_issue"],
            "rights_issue_price": row["rights_issue_price"],
            "xdxr_fenhong": float(best[0]["fenhong"]),
        }
        if (stock, event_date) in existing_by_key:
            # 同股票同除权日已存在（含 dps 为 NULL 的行）：仅当现有行派息
            # 与隔离行一致（或现有行为 NULL 派息）时判定为重复件（隔离行
            # 删除即可），不一致则保留隔离行人工复核。
            existing_dps = existing_by_key[(stock, event_date)]
            if existing_dps is None or _dps_matches(dps, float(existing_dps)):
                duplicates.append(payload)
            else:
                skipped_conflicting_existing += 1
            continue
        matches.append(payload)

    # 跨行冲突防护：多个占位行可能映射到同一真实除权日（如年中期占位与
    # 年末占位同窗命中），无法判定对应关系 → 整组保留隔离，不做写入。
    by_target: dict[tuple[str, str], list[dict]] = {}
    for m in matches:
        by_target.setdefault((m["stock_code"], m["ex_date"]), []).append(m)
    final_matches: list[dict] = []
    skipped_cross_conflict = 0
    for group in by_target.values():
        if len(group) == 1:
            final_matches.append(group[0])
        else:
            skipped_cross_conflict += len(group)

    return {
        "quarantine_rows_scanned": len(rows),
        "restorable": len(final_matches),
        "duplicates": len(duplicates),
        "skipped_no_candidate": skipped_no_candidate,
        "skipped_ambiguous": skipped_ambiguous,
        "skipped_conflicting_existing": skipped_conflicting_existing,
        "skipped_cross_conflict": skipped_cross_conflict,
        "matches": final_matches,
        "duplicate_rows": duplicates,
    }


def write_restore(duck: DuckDBStore, matches: list[dict], duplicates: list[dict]) -> dict:
    restored = 0
    with duck.write_connection() as conn:
        conn.execute("BEGIN")
        for m in matches:
            conn.execute(
                """INSERT INTO dividends
                   (stock_code, ex_date, announcement_date, dividend_per_share,
                    stock_dividend, transfer_share, rights_issue, rights_issue_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    m["stock_code"], m["ex_date"], m["announcement_date"],
                    m["dividend_per_share"], m["stock_dividend"], m["transfer_share"],
                    m["rights_issue"], m["rights_issue_price"],
                ],
            )
            conn.execute(
                "DELETE FROM dividends_quarantine WHERE stock_code = ? AND ex_date = ?",
                [m["stock_code"], m["placeholder_ex_date"]],
            )
            restored += 1
        deduped = 0
        for d in duplicates:
            conn.execute(
                "DELETE FROM dividends_quarantine WHERE stock_code = ? AND ex_date = ?",
                [d["stock_code"], d["placeholder_ex_date"]],
            )
            deduped += 1
        conn.execute("COMMIT")
    return {"restored": restored, "deduplicated": deduped}


def main() -> int:
    write_mode = "--yes" in sys.argv[1:]
    paths = resolve_and_validate_paths()
    if any_write_lock_active(paths.duckdb_path):
        print("检测到写锁活跃，拒绝运行（单写者串行纪律）。")
        return 3

    duck = DuckDBStore(paths=paths)
    report = collect_matches(duck)
    matches = report.pop("matches")
    duplicates = report.pop("duplicate_rows")
    summary = {
        "captured_at": datetime.now(UTC).isoformat(),
        "mode": "write" if write_mode else "dry_run",
        **report,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not write_mode:
        print("\n只读 dry-run：未写库。确认无误后加 --yes 执行（写前自动备份两表）。")
        return 0

    if not matches and not duplicates:
        print("\n无可恢复/去重行，跳过写库。")
        return 0

    from scripts._maintenance_safety import backup_tables

    backup_dir = backup_tables(duck, ["dividends", "dividends_quarantine"], "dividend-exdate-repair")
    print(f"已备份受影响表到: {backup_dir}")

    result = write_restore(duck, matches, duplicates)
    summary["written"] = result
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = {
        "summary": summary,
        "sample_restored": matches[:20],
        "sample_duplicates": duplicates[:20],
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence_path = EVIDENCE_DIR / f"evidence-dividend-exdate-repair-{stamp}.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"完成: 恢复 {result['restored']} 行 + 去重 {result['deduplicated']} 行；"
        f"证据: {evidence_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
