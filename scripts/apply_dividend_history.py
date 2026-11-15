"""把结构化源抓到的分红历史（JSONL）写进库 + 清理隔离区（2026-11-15）。

背景
----
`dividends_quarantine` 里 8,467 行 / 2,436 只的隔离理由全是
`unverified_period_end_placeholder`。实测抽样已证伪这个判定：占位日期
（06-30 / 12-31）里有很多**本来就是真除权日**（900922 → 2026-06-30 派息
0.02639；200541 → 2026-06-30；300339 → 2025-12-31；600713 → 2025-06-30）。
所以隔离不是"数据缺失"，而是"没问过结构化源"。本脚本用
`scripts/fetch_dividend_history.py` 抓到的巨潮/东财分红历史来终结这些行。

执行顺序（**不能变**）
----------------------
1. 读 JSONL → 按 `(stock_code, ex_date)` 去重/合并：
   - 完全重复行（金额+报告期都相同，巨潮会重复吐同一事件）→ 丢弃；
   - 同一天确有**两笔**不同分红（如年报 + 中期同日除权）→ **金额求和**成一行
     （`dividends` 主键是 `(stock_code, ex_date)`，不合并就只能丢一笔）；
2. 与库里现有 `dividends` 对比（三个写通道，分开计数）：
   - 没有该 `(stock_code, ex_date)` → **插入**；
   - 有但 `dividend_per_share` 为空或与 `cash_per_10/10` 相差 > 容差(默认 0.001)
     → **更新金额字段**（其它字段非空不覆盖，只补 NULL）；
   - **现金口径未知时不写金额**：巨潮对 B 股常把「派息比例」留空（实测 200541
     2007-06-08 派息比例 NaN，但分红说明写着「10转增3股派5元」，库里 0.5 是对的）。
     这种行只允许回填别的空字段；空值绝不能当 0 去覆盖库里的现金分红，插入时
     也按 NULL 写（不编 0）。
   - 金额一致、但 `announcement_date`/`stock_dividend`/`transfer_share` 在库里是
     NULL、源里有正面事实（有日期 / 正数）→ **回填 NULL**（`--no-fill-nulls` 可关）。
     实测例：600519 2006-05-19 巨潮有两行（10派3元 + 10转增10股），库里派息 0.3
     正确但 `transfer_share` 为空 → 回填 1.0；只把 NULL 改成正数，绝不覆盖已有值。
   - 都一致 → 跳过；
3. 隔离区逐条判定（四类 + 安全阀）：
   - `matched`：抓取结果里有同 `(stock_code, ex_date)` 且金额对得上 →
     按**源金额**写入 `dividends` 并从隔离表删除；
   - `deleted_fake`：没有同日期事件，但源里该股票有"报告期 == 占位日"的事件
     （说明占位期末不是除权日，真除权日另有其日）→ 删除隔离行；
     `--keep-period-missing` 时，连报告期都对不上的那些（占位日也不是源里的
     除权日）不删，只保留计数，供人工复核；
   - `deleted_empty`：源里该股票**根本没有分红记录**（真不分红）→ 删除隔离行；
   - `unavailable`：该股票抓取失败/超时（源没回应）→ **保留**隔离行，把
     `quarantine_reason` 改为 `unavailable_from_source`；
   - 另有几类**保留桶**：`kept_amount_mismatch`（日期真但金额对不上，改
     reason=source_amount_mismatch）、`kept_not_fetched`（本次没抓这只股票）、
     `kept_not_verifiable`（源有返回但没有任何可用除权日）、`kept_coverage_gap`
     （见下）。

删除必须"有正面证据"（2026-11-15 实测教训）
-------------------------------------------
`deleted_empty` / `deleted_fake(period_missing)` 都建立在"源是完整的"这个假设上，
但**退市股在巨潮/东财都查不到历史**：实测 600087（`*ST 长油`，`stock_meta.is_listed
=false`）在隔离区里有 12 行真实派息（0.35 / 0.5 / 0.3 …），巨潮返回 0 行、东财
直接抛异常。若按"源没记录 = 真不分红"删掉，就真的把数据删没了。
因此**只有 `stock_meta.is_listed = TRUE` 的股票才允许走这两个删除分支**；非在市/
未知股票一律进 `kept_coverage_gap`（保留隔离行，reason 改 `source_coverage_gap`）。
`stock_meta` 读不到时按"全部未知"处理（fail-closed，宁可不删）。
4. 写库前：`update_lock.any_write_lock_active()` 检查 → `COPY` 备份
   `dividends` / `dividends_quarantine` 到
   `.planning/maintenance-backup-dividend-<ts>/` → **单事务**写入 → 打印四类计数。

单位
----
抓取 JSONL 里 `cash_per_10 / stock_per_10 / transfer_per_10` 是**每 10 股**口径，
落库 `dividends.dividend_per_share / stock_dividend / transfer_share` 是**每股**，
本脚本统一 ÷10（`_per_10_to_per_share`）。

用法
----
    # 1) 只读 dry-run（默认）：给出将要插入/更新/移出隔离/删除/标记的确切条数
    python scripts/apply_dividend_history.py --in .planning/div/events.jsonl

    # 2) 正式写库（先备份、单事务）
    python scripts/apply_dividend_history.py --in .planning/div/events.jsonl --yes

正式库 dry-run 需要只读打开：
    VD_ENV=formal VD_FORMAL_ACK=confirmed python scripts/apply_dividend_history.py --in ...
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # 允许 `python scripts/xxx.py` 直接运行
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import resolve_and_validate_paths  # noqa: E402
from app.core.storage.update_lock import any_write_lock_active  # noqa: E402

# 与 fetch 脚本同名常量：两边必须一致（隔离区 reason 是它们之间的协议）
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

REASON_PLACEHOLDER = "unverified_period_end_placeholder"
REASON_UNAVAILABLE = "unavailable_from_source"
REASON_AMOUNT_MISMATCH = "source_amount_mismatch"
REASON_COVERAGE_GAP = "source_coverage_gap"

DEFAULT_TOLERANCE = 0.001

DIVIDEND_COLUMNS = (
    "stock_code",
    "ex_date",
    "announcement_date",
    "dividend_per_share",
    "stock_dividend",
    "transfer_share",
    "rights_issue",
    "rights_issue_price",
)


# --------------------------------------------------------------------------- #
# 标量清洗（JSONL 里日期是 ISO 字符串，DB 里是 DATE）
# --------------------------------------------------------------------------- #
def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null", "-", "--"}:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def per_10_to_per_share(value: Any) -> float | None:
    """每 10 股 → 每股（源口径 → 落库口径的唯一换算点）。"""
    number = _to_float(value)
    return None if number is None else number / 10.0


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 10)


# --------------------------------------------------------------------------- #
# 读输入
# --------------------------------------------------------------------------- #
def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                print(f"[WARN] {path}:{line_number} 不是合法 JSON，已跳过", flush=True)
                continue
            if isinstance(payload, dict):
                yield payload


def load_events(path: Path) -> list[dict[str, Any]]:
    return list(_iter_jsonl(path))


def load_coverage(path: Path | None) -> dict[str, str]:
    """``{stock_code: status}``；文件缺失 → 空 dict（按"只有抓到的股票才算数"处理）。"""
    if path is None or not path.exists():
        return {}
    coverage: dict[str, str] = {}
    for payload in _iter_jsonl(path):
        code = str(payload.get("stock_code") or "").strip()
        status = str(payload.get("status") or "").strip()
        if code and status:
            coverage[code] = status
    return coverage


def coverage_path_for(events_path: Path) -> Path:
    suffix = events_path.suffix
    stem = events_path.name[: -len(suffix)] if suffix else events_path.name
    return events_path.with_name(f"{stem}.coverage.jsonl")


# --------------------------------------------------------------------------- #
# 事件去重 / 合并
# --------------------------------------------------------------------------- #
def merge_events(events: Iterable[Mapping[str, Any]]) -> tuple[dict[tuple[str, date], dict[str, Any]], dict[str, int]]:
    """按 ``(stock_code, ex_date)`` 去重合并 → (merged, stats)。

    - 完全重复（派息/送股/转增/报告期全同）→ 丢弃（源侧重复吐同一事件）；
    - 同日多笔不同分红 → 金额相加（否则 dividends 主键放不下，会丢一笔）。
    """
    groups: dict[tuple[str, date], list[Mapping[str, Any]]] = {}
    stats = {
        "rows": 0,
        "skipped_no_stock": 0,
        "skipped_no_ex_date": 0,
        "skipped_all_zero": 0,
        "duplicate_rows_dropped": 0,
        "merged_same_day_groups": 0,
        "merged_same_day_rows": 0,
    }
    for event in events:
        stats["rows"] += 1
        code = str(event.get("stock_code") or "").strip()
        ex_date = _to_date(event.get("ex_date"))
        if not code:
            stats["skipped_no_stock"] += 1
            continue
        if ex_date is None:
            stats["skipped_no_ex_date"] += 1
            continue
        groups.setdefault((code, ex_date), []).append(event)

    merged: dict[tuple[str, date], dict[str, Any]] = {}
    for key, group in groups.items():
        unique: dict[tuple[Any, ...], Mapping[str, Any]] = {}
        for event in group:
            signature = (
                _round(per_10_to_per_share(event.get("cash_per_10"))),
                _round(per_10_to_per_share(event.get("stock_per_10"))),
                _round(per_10_to_per_share(event.get("transfer_per_10"))),
                str(event.get("report_period") or ""),
            )
            if signature in unique:
                stats["duplicate_rows_dropped"] += 1
                continue
            unique[signature] = event
        # 现金口径是否"已知"：巨潮对 B 股常常派息比例为空而说明里有钱
        # （实测 200541 2007-06-08：派息比例 NaN，说明却是「10转增3股派5元」）。
        # NaN 绝不能当成 0，否则会把库里真实的现金分红抹成 0。
        cash_known = any(
            per_10_to_per_share(event.get("cash_per_10")) is not None for event in unique.values()
        )
        cash = sum(per_10_to_per_share(event.get("cash_per_10")) or 0.0 for event in unique.values())
        stock = sum(per_10_to_per_share(event.get("stock_per_10")) or 0.0 for event in unique.values())
        transfer = sum(
            per_10_to_per_share(event.get("transfer_per_10")) or 0.0 for event in unique.values()
        )
        if cash == 0.0 and stock == 0.0 and transfer == 0.0:
            stats["skipped_all_zero"] += 1
            continue
        if len(unique) > 1:
            stats["merged_same_day_groups"] += 1
            stats["merged_same_day_rows"] += len(unique)
        first = next(iter(unique.values()))
        periods = sorted(
            {
                _to_date(event.get("report_period"))
                for event in unique.values()
                if _to_date(event.get("report_period")) is not None
            }
        )
        announcement_dates = sorted(
            {
                _to_date(event.get("announcement_date"))
                for event in unique.values()
                if _to_date(event.get("announcement_date")) is not None
            }
        )
        merged[key] = {
            "stock_code": key[0],
            "ex_date": key[1],
            "dividend_per_share": _round(cash),
            "cash_known": cash_known,
            "stock_dividend": _round(stock),
            "transfer_share": _round(transfer),
            # 公告日取最早（先公告后除权）；缺失时 None
            "announcement_date": announcement_dates[0] if announcement_dates else None,
            "report_periods": periods,
            "report_period": periods[0] if len(periods) == 1 else (periods or None),
            "dividend_type": first.get("dividend_type"),
            "source": first.get("source"),
            # 金额校验用：单笔口径 + 合并口径都留着（同日两笔时隔离行可能只对应其中一笔）
            "per_share_variants": sorted(
                {
                    _round(per_10_to_per_share(event.get("cash_per_10")) or 0.0)
                    for event in unique.values()
                }
                | {_round(cash)}
            ),
        }
    return merged, stats


# --------------------------------------------------------------------------- #
# 判定（纯函数，不碰 DB → 可单测）
# --------------------------------------------------------------------------- #
def _amounts_match(left: Any, right: Any, tolerance: float) -> bool:
    """金额一致性：任一侧缺失视为"无矛盾"（不否决），否则绝对值容差。"""
    left_value = _to_float(left)
    right_value = _to_float(right)
    if left_value is None or right_value is None:
        return True
    return abs(left_value - right_value) <= tolerance


def _needs_null_fill(current: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """库里该字段是 NULL、源里有**正面事实**（正数/有日期）→ 值得回填。

    只回填正数，不把 NULL 改写成 0.0：0.0 会让"未知"变成"确定没有"，
    语义上是倒退（例如纯现金分红的行本来是 stock_dividend=NULL）。
    """
    if current.get("announcement_date") is None and event.get("announcement_date") is not None:
        return True
    for field in ("stock_dividend", "transfer_share"):
        if current.get(field) is not None:
            continue
        value = _to_float(event.get(field))
        if value is not None and value > 0:
            return True
    return False


def _quarantine_amount_matches(row: Mapping[str, Any], event: Mapping[str, Any], tolerance: float) -> bool:
    quarantine_value = _to_float(row.get("dividend_per_share"))
    if quarantine_value is None:
        return True
    variants = event.get("per_share_variants") or [event.get("dividend_per_share")]
    return any(_amounts_match(quarantine_value, variant, tolerance) for variant in variants)


def plan_apply(
    *,
    events: Iterable[Mapping[str, Any]],
    existing: Mapping[tuple[str, date], Any],
    quarantine: Iterable[Mapping[str, Any]],
    coverage: Mapping[str, str] | None = None,
    listed_stocks: Collection[str] | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
    keep_period_missing: bool = False,
    fill_nulls: bool = True,
    sample_size: int = 5,
) -> dict[str, Any]:
    """算出这次要做的全部动作（不写库）。

    ``listed_stocks``：当前在市股票集合（来自 ``stock_meta.is_listed``）。
    **None 按"全部未知"处理 → 只删有正面证据的行**（fail-closed）。退市股在
    巨潮/东财都查不到历史（实测 600087），绝不能因为"源里没有"就删。
    """
    merged, merge_stats = merge_events(events)
    coverage_map = dict(coverage or {})
    listed = {str(code).strip() for code in (listed_stocks or ())}

    by_stock: dict[str, dict[date, dict[str, Any]]] = {}
    periods_by_stock: dict[str, dict[date, dict[str, Any]]] = {}
    for (code, ex_date), event in merged.items():
        by_stock.setdefault(code, {})[ex_date] = event
        for period in event["report_periods"]:
            periods_by_stock.setdefault(code, {}).setdefault(period, event)

    # ---- 步骤 2：dividends 插入 / 更新金额 / 回填 NULL ----
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    null_fills: list[dict[str, Any]] = []
    unchanged = 0
    skipped_cash_unknown = 0
    inserted_cash_unknown = 0
    missing = object()
    for key in sorted(merged):
        event = merged[key]
        current = existing.get(key, missing)
        if current is missing:
            if event.get("cash_known", True):
                inserts.append(event)
            else:
                inserts.append({**event, "dividend_per_share": None})
                inserted_cash_unknown += 1
            continue
        current_row = current if isinstance(current, Mapping) else {"dividend_per_share": current}
        current_value = _to_float(current_row.get("dividend_per_share"))
        cash_known = bool(event.get("cash_known", True))
        source_value = float(event["dividend_per_share"] or 0.0)
        if not cash_known:
            # 源没给现金口径 → 只能回填别的空字段，绝不能拿 0 覆盖库里的现金分红
            if fill_nulls and _needs_null_fill(current_row, event):
                null_fills.append(event)
            else:
                unchanged += 1
                skipped_cash_unknown += 1
        elif current_value is None or abs(current_value - source_value) > tolerance:
            updates.append({**event, "previous_dividend_per_share": current_value})
        elif fill_nulls and _needs_null_fill(current_row, event):
            null_fills.append(event)
        else:
            unchanged += 1

    # ---- 步骤 3：隔离区逐条判定 ----
    job = {
        "matched": [],
        "deleted_fake": [],
        "deleted_empty": [],
        "unavailable": [],
        "amount_mismatch": [],
        "not_fetched": [],
        "not_verifiable": [],
        "period_missing_kept": [],
        "coverage_gap": [],
    }
    counts = dict.fromkeys(
        (
            "quarantine_scanned",
            "matched",
            "deleted_fake",
            "deleted_fake_period_matched",
            "deleted_fake_period_missing",
            "deleted_empty",
            # unavailable / kept_amount_mismatch 的取值口径是"**本次新标记**"条数
            # （幂等：第二次跑同一份 JSONL 必须为 0），另有 *_total 记本类总量。
            "unavailable",
            "unavailable_total",
            "unavailable_already",
            "kept_amount_mismatch",
            "kept_amount_mismatch_total",
            "kept_amount_mismatch_already",
            "kept_not_fetched",
            "kept_not_verifiable",
            "kept_period_missing",
            # 非在市股票（退市/未知）源里查不到 → 保留，reason=source_coverage_gap
            "kept_coverage_gap",
            "kept_coverage_gap_empty",
            "kept_coverage_gap_period_missing",
            "kept_coverage_gap_already",
            "matched_null_amount",
        ),
        0,
    )
    for row in quarantine:
        counts["quarantine_scanned"] += 1
        code = str(row.get("stock_code") or "").strip()
        placeholder = _to_date(row.get("ex_date"))
        reason = str(row.get("quarantine_reason") or "")
        if placeholder is None:
            counts["kept_not_verifiable"] += 1
            job["not_verifiable"].append({"stock_code": code, "ex_date": None, "why": "ex_date 不可解析"})
            continue
        status = coverage_map.get(code)
        stock_events = by_stock.get(code) or {}

        if status == STATUS_FAILED:
            counts["unavailable_total"] += 1
            if reason == REASON_UNAVAILABLE:
                counts["unavailable_already"] += 1
            else:
                counts["unavailable"] += 1  # 本次新标记
            job["unavailable"].append({"stock_code": code, "ex_date": placeholder, "reason": reason})
            continue
        if status is None:
            counts["kept_not_fetched"] += 1
            job["not_fetched"].append({"stock_code": code, "ex_date": placeholder})
            continue
        if status == STATUS_EMPTY:
            # 源回答"该股没有任何分红记录"：只有在市股票才敢当成"真不分红"删掉；
            # 退市股的"空返回"是源的覆盖缺口（实测 600087 有 12 行真实派息）。
            if code in listed:
                counts["deleted_empty"] += 1
                job["deleted_empty"].append(
                    {"stock_code": code, "ex_date": placeholder, "reason": reason}
                )
            else:
                counts["kept_coverage_gap"] += 1
                counts["kept_coverage_gap_empty"] += 1
                if reason == REASON_COVERAGE_GAP:
                    counts["kept_coverage_gap_already"] += 1
                job["coverage_gap"].append(
                    {"stock_code": code, "ex_date": placeholder, "why": "source_empty", "reason": reason}
                )
            continue
        if not stock_events:
            # partial，或 ok 但没有任何可用事件 → 不可判定，保持原样
            counts["kept_not_verifiable"] += 1
            job["not_verifiable"].append(
                {"stock_code": code, "ex_date": placeholder, "why": f"coverage={status}, 可用事件 0"}
            )
            continue

        event = stock_events.get(placeholder)
        if event is not None:
            if _quarantine_amount_matches(row, event, tolerance):
                if _to_float(row.get("dividend_per_share")) is None:
                    counts["matched_null_amount"] += 1
                counts["matched"] += 1
                job["matched"].append(
                    {
                        "stock_code": code,
                        "ex_date": placeholder,
                        "quarantine_dividend_per_share": _to_float(row.get("dividend_per_share")),
                        # 现金口径未知（B 股等）→ 写 NULL，绝不编 0 也不覆盖已有值
                        "source_dividend_per_share": (
                            event["dividend_per_share"] if event.get("cash_known", True) else None
                        ),
                        "announcement_date": row.get("announcement_date"),
                        "stock_dividend": _to_float(row.get("stock_dividend")),
                        "transfer_share": _to_float(row.get("transfer_share")),
                        "rights_issue": _to_float(row.get("rights_issue")),
                        "rights_issue_price": _to_float(row.get("rights_issue_price")),
                        "source_announcement_date": event["announcement_date"],
                    }
                )
            else:
                counts["kept_amount_mismatch_total"] += 1
                if reason == REASON_AMOUNT_MISMATCH:
                    counts["kept_amount_mismatch_already"] += 1
                else:
                    counts["kept_amount_mismatch"] += 1  # 本次新标记
                job["amount_mismatch"].append(
                    {
                        "stock_code": code,
                        "ex_date": placeholder,
                        "quarantine_dividend_per_share": _to_float(row.get("dividend_per_share")),
                        "source_dividend_per_share": event["dividend_per_share"],
                        "reason": reason,
                    }
                )
            continue

        # 没有同日期事件：占位日不是源里的除权日
        period_event = (periods_by_stock.get(code) or {}).get(placeholder)
        if period_event is not None:
            counts["deleted_fake"] += 1
            counts["deleted_fake_period_matched"] += 1
            job["deleted_fake"].append(
                {
                    "stock_code": code,
                    "ex_date": placeholder,
                    "why": "period_matched",
                    "real_ex_date": period_event["ex_date"],
                    "reason": reason,
                }
            )
        elif keep_period_missing:
            counts["kept_period_missing"] += 1
            job["period_missing_kept"].append({"stock_code": code, "ex_date": placeholder})
        elif code not in listed:
            # 非在市股票：没有正面证据不敢删（同 deleted_empty 的理由）
            counts["kept_coverage_gap"] += 1
            counts["kept_coverage_gap_period_missing"] += 1
            if reason == REASON_COVERAGE_GAP:
                counts["kept_coverage_gap_already"] += 1
            job["coverage_gap"].append(
                {"stock_code": code, "ex_date": placeholder, "why": "period_missing", "reason": reason}
            )
        else:
            counts["deleted_fake"] += 1
            counts["deleted_fake_period_missing"] += 1
            job["deleted_fake"].append(
                {"stock_code": code, "ex_date": placeholder, "why": "period_missing", "reason": reason}
            )

    counts["dividend_inserted"] = len(inserts)
    counts["dividend_updated"] = len(updates)
    counts["dividend_null_filled"] = len(null_fills)
    counts["dividend_unchanged"] = unchanged
    counts["skipped_cash_unknown"] = skipped_cash_unknown
    counts["dividend_inserted_cash_unknown"] = inserted_cash_unknown
    counts["events_merged"] = len(merged)
    counts.update(merge_stats)

    # 隔离区分账必须闭合（8,467 行逐条有去向）
    classified = (
        counts["matched"]
        + counts["deleted_fake"]
        + counts["deleted_empty"]
        + counts["unavailable_total"]
        + counts["kept_amount_mismatch_total"]
        + counts["kept_not_fetched"]
        + counts["kept_not_verifiable"]
        + counts["kept_period_missing"]
        + counts["kept_coverage_gap"]
    )
    counts["quarantine_classified"] = classified
    counts["quarantine_reconciles"] = classified == counts["quarantine_scanned"]

    return {
        "counts": counts,
        "inserts": inserts,
        "updates": updates,
        "null_fills": null_fills,
        "quarantine_matched": job["matched"],
        "quarantine_deleted_fake": job["deleted_fake"],
        "quarantine_deleted_empty": job["deleted_empty"],
        "quarantine_unavailable": job["unavailable"],
        "quarantine_amount_mismatch": job["amount_mismatch"],
        "quarantine_not_fetched": job["not_fetched"],
        "quarantine_not_verifiable": job["not_verifiable"],
        "quarantine_period_missing_kept": job["period_missing_kept"],
        "quarantine_coverage_gap": job["coverage_gap"],
        "samples": {
            "inserts": inserts[:sample_size],
            "updates": updates[:sample_size],
            "null_fills": null_fills[:sample_size],
            "matched": job["matched"][:sample_size],
            "deleted_fake": job["deleted_fake"][:sample_size],
            "deleted_empty": job["deleted_empty"][:sample_size],
            "unavailable": job["unavailable"][:sample_size],
            "amount_mismatch": job["amount_mismatch"][:sample_size],
        },
    }


# --------------------------------------------------------------------------- #
# 读库
# --------------------------------------------------------------------------- #
def load_existing_dividends(duck: DuckDBStore) -> dict[tuple[str, date], dict[str, Any]]:
    """现有 dividends 行（判断"要不要更新金额 / 要不要回填 NULL"都要用到整行）。"""
    rows = duck.read_query(
        """SELECT stock_code, ex_date, announcement_date, dividend_per_share,
                  stock_dividend, transfer_share
           FROM dividends"""
    )
    existing: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        ex_date = _to_date(row["ex_date"])
        code = str(row["stock_code"] or "").strip()
        if ex_date is None or not code:
            continue
        existing[(code, ex_date)] = {
            "dividend_per_share": row["dividend_per_share"],
            "announcement_date": row["announcement_date"],
            "stock_dividend": row["stock_dividend"],
            "transfer_share": row["transfer_share"],
        }
    return existing


def load_quarantine(duck: DuckDBStore) -> list[dict[str, Any]]:
    return duck.read_query(
        """SELECT stock_code, ex_date, announcement_date, dividend_per_share,
                  stock_dividend, transfer_share, rights_issue, rights_issue_price,
                  quarantine_reason
           FROM dividends_quarantine
           ORDER BY stock_code, ex_date"""
    )


def load_listed_stocks(duck: DuckDBStore) -> set[str]:
    """在市股票集合（``stock_meta.is_listed``）。

    **fail-closed**：读不到就返回空集合 → 所有"源里没有记录"的判定都不删行
    （退市股在巨潮/东财查不到历史是实测事实，见模块 docstring）。
    """
    try:
        rows = duck.read_query("SELECT stock_code FROM stock_meta WHERE is_listed = TRUE")
    except Exception as error:  # noqa: BLE001 - 任何读失败都必须降级成"全部未知"
        print(
            f"[WARN] 读不到 stock_meta.is_listed（{type(error).__name__}: {error}）："
            "按「全部未知」处理，deleted_empty / deleted_fake(period_missing) 一律不删。",
            flush=True,
        )
        return set()
    return {str(row["stock_code"]).strip() for row in rows if row.get("stock_code")}


# --------------------------------------------------------------------------- #
# 写库（单事务）
# --------------------------------------------------------------------------- #
_INSERT_DIVIDEND = f"""
INSERT INTO dividends ({", ".join(DIVIDEND_COLUMNS)})
VALUES ({", ".join("?" for _ in DIVIDEND_COLUMNS)})
ON CONFLICT (stock_code, ex_date) DO UPDATE SET
    dividend_per_share = COALESCE(EXCLUDED.dividend_per_share, dividends.dividend_per_share)
"""

_INSERT_DIVIDEND_FILLING_NULLS = f"""
INSERT INTO dividends ({", ".join(DIVIDEND_COLUMNS)})
VALUES ({", ".join("?" for _ in DIVIDEND_COLUMNS)})
ON CONFLICT (stock_code, ex_date) DO UPDATE SET
    dividend_per_share = COALESCE(EXCLUDED.dividend_per_share, dividends.dividend_per_share),
    announcement_date = COALESCE(dividends.announcement_date, EXCLUDED.announcement_date),
    stock_dividend = COALESCE(dividends.stock_dividend, EXCLUDED.stock_dividend),
    transfer_share = COALESCE(dividends.transfer_share, EXCLUDED.transfer_share)
"""

_UPDATE_DIVIDEND = """
-- 金额同样走 COALESCE：null_fills 通道传 None 表示"这次只补别的空字段，不动金额"
UPDATE dividends SET
    dividend_per_share = COALESCE(?, dividend_per_share),
    announcement_date = COALESCE(announcement_date, ?),
    stock_dividend = COALESCE(stock_dividend, ?),
    transfer_share = COALESCE(transfer_share, ?)
WHERE stock_code = ? AND ex_date = ?
"""

_DELETE_QUARANTINE = "DELETE FROM dividends_quarantine WHERE stock_code = ? AND ex_date = ?"

_MARK_QUARANTINE = "UPDATE dividends_quarantine SET quarantine_reason = ? WHERE stock_code = ? AND ex_date = ?"


def _positive_or_none(value: Any) -> float | None:
    """只保留正数：把 0.0 当成"没有信息"，避免把 NULL 改写成 0.0。"""
    number = _to_float(value)
    return number if number is not None and number > 0 else None


def _dividend_row(event: Mapping[str, Any]) -> list[Any]:
    return [
        event["stock_code"],
        event["ex_date"],
        event.get("announcement_date"),
        event["dividend_per_share"],
        event.get("stock_dividend"),
        event.get("transfer_share"),
        None,  # rights_issue：结构化源不提供配股
        None,  # rights_issue_price
    ]


def execute_plan(duck: DuckDBStore, plan: Mapping[str, Any]) -> dict[str, int]:
    """把 plan 在**一个事务**里落库；异常整体回滚。"""
    written = {
        "dividend_inserted": 0,
        "dividend_updated": 0,
        "dividend_null_filled": 0,
        "quarantine_moved": 0,
        "quarantine_deleted": 0,
        "quarantine_marked_unavailable": 0,
        "quarantine_marked_amount_mismatch": 0,
        "quarantine_marked_coverage_gap": 0,
    }
    with duck.transaction() as conn:
        for event in plan["inserts"]:
            conn.execute(_INSERT_DIVIDEND, _dividend_row(event))
            written["dividend_inserted"] += 1
        for event in plan["updates"]:
            conn.execute(
                _UPDATE_DIVIDEND,
                [
                    event["dividend_per_share"],
                    event.get("announcement_date"),
                    event.get("stock_dividend"),
                    event.get("transfer_share"),
                    event["stock_code"],
                    event["ex_date"],
                ],
            )
            written["dividend_updated"] += 1
        for event in plan["null_fills"]:
            # 只填空值：金额传 None（COALESCE 保持原值），送股/转增只写正数
            conn.execute(
                _UPDATE_DIVIDEND,
                [
                    None,
                    event.get("announcement_date"),
                    _positive_or_none(event.get("stock_dividend")),
                    _positive_or_none(event.get("transfer_share")),
                    event["stock_code"],
                    event["ex_date"],
                ],
            )
            written["dividend_null_filled"] += 1
        for row in plan["quarantine_matched"]:
            payload = _dividend_row(
                {
                    "stock_code": row["stock_code"],
                    "ex_date": row["ex_date"],
                    "announcement_date": row["announcement_date"]
                    or row.get("source_announcement_date"),
                    "dividend_per_share": row["source_dividend_per_share"],
                    "stock_dividend": row["stock_dividend"],
                    "transfer_share": row["transfer_share"],
                }
            )
            conn.execute(_INSERT_DIVIDEND_FILLING_NULLS, payload)
            conn.execute(_DELETE_QUARANTINE, [row["stock_code"], row["ex_date"]])
            written["quarantine_moved"] += 1
        for row in plan["quarantine_deleted_fake"] + plan["quarantine_deleted_empty"]:
            conn.execute(_DELETE_QUARANTINE, [row["stock_code"], row["ex_date"]])
            written["quarantine_deleted"] += 1
        for row in plan["quarantine_unavailable"]:
            if row.get("reason") != REASON_UNAVAILABLE:
                conn.execute(_MARK_QUARANTINE, [REASON_UNAVAILABLE, row["stock_code"], row["ex_date"]])
                written["quarantine_marked_unavailable"] += 1
        for row in plan["quarantine_amount_mismatch"]:
            if row.get("reason") != REASON_AMOUNT_MISMATCH:
                conn.execute(
                    _MARK_QUARANTINE, [REASON_AMOUNT_MISMATCH, row["stock_code"], row["ex_date"]]
                )
                written["quarantine_marked_amount_mismatch"] += 1
        for row in plan["quarantine_coverage_gap"]:
            if row.get("reason") != REASON_COVERAGE_GAP:
                conn.execute(
                    _MARK_QUARANTINE, [REASON_COVERAGE_GAP, row["stock_code"], row["ex_date"]]
                )
                written["quarantine_marked_coverage_gap"] += 1
    return written


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def format_report(plan: Mapping[str, Any], mode: str) -> str:
    counts = plan["counts"]
    lines = [
        f"=== {mode} ===",
        (
            f"事件: JSONL {counts['rows']} 行 → 去重合并 {counts['events_merged']} 个 "
            f"(stock_code, ex_date)；完全重复丢弃 {counts['duplicate_rows_dropped']} 行，"
            f"同日多笔合并 {counts['merged_same_day_groups']} 组"
            f"（涉及 {counts['merged_same_day_rows']} 行，金额求和）"
        ),
        (
            f"  跳过: 无 ex_date {counts['skipped_no_ex_date']} / 无代码 {counts['skipped_no_stock']} / "
            f"派息送转全 0 {counts['skipped_all_zero']}"
        ),
        "dividends:",
        f"  · 插入            {counts['dividend_inserted']}"
        f"（其中现金口径未知按 NULL 写入 {counts['dividend_inserted_cash_unknown']}）",
        f"  · 更新金额字段    {counts['dividend_updated']}（金额差 > 容差，其它字段非空不覆盖）",
        f"  · 金额口径未知跳过 {counts['skipped_cash_unknown']}"
        f"（源里派息比例为空，如 B 股；库里已有值一律不动）",
        f"  · 回填 NULL 字段  {counts['dividend_null_filled']}"
        f"（金额已一致，只补 announcement_date / 送股 / 转增 的空值；--no-fill-nulls 可关）",
        f"  · 一致跳过        {counts['dividend_unchanged']}",
        f"隔离区 dividends_quarantine（扫描 {counts['quarantine_scanned']} 行）:",
        f"  · matched         移出隔离 → dividends（金额以源为准）: {counts['matched']}"
        f"（其中隔离行金额为空 {counts['matched_null_amount']}）",
        f"  · deleted_fake    删除（假占位）: {counts['deleted_fake']}"
        f"（报告期命中 {counts['deleted_fake_period_matched']} / 报告期未命中 "
        f"{counts['deleted_fake_period_missing']}）",
        f"  · deleted_empty   删除（源无分红记录）: {counts['deleted_empty']}",
        f"  · unavailable     保留隔离行 + 标记 {REASON_UNAVAILABLE}: 本次新标记 "
        f"{counts['unavailable']} / 本类共 {counts['unavailable_total']}"
        f"（其中已是该标记 {counts['unavailable_already']}）",
        f"  · kept_amount_mismatch 保留 + 标记 {REASON_AMOUNT_MISMATCH}: 本次新标记 "
        f"{counts['kept_amount_mismatch']} / 本类共 {counts['kept_amount_mismatch_total']}"
        f"（其中已是该标记 {counts['kept_amount_mismatch_already']}）",
        f"  · kept_not_fetched     未抓取，保持原样: {counts['kept_not_fetched']}",
        f"  · kept_not_verifiable  源有返回但无可用除权日，保持原样: {counts['kept_not_verifiable']}",
        f"  · kept_coverage_gap    非在市股票源里查不到，保留 + 标记 {REASON_COVERAGE_GAP}: "
        f"{counts['kept_coverage_gap']}（源返回空 {counts['kept_coverage_gap_empty']} / "
        f"报告期未命中 {counts['kept_coverage_gap_period_missing']}；"
        f"已是该标记 {counts['kept_coverage_gap_already']}）",
    ]
    if counts["kept_period_missing"]:
        lines.append(
            f"  · kept_period_missing  --keep-period-missing：报告期未命中的假占位只保留: "
            f"{counts['kept_period_missing']}"
        )
    lines.append(
        f"  对账: {counts['quarantine_classified']}/{counts['quarantine_scanned']} 行有去向 "
        f"→ {'闭合' if counts['quarantine_reconciles'] else '不闭合（请检查！）'}"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把分红历史 JSONL 写进 dividends 并清理 dividends_quarantine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--in", dest="input_path", required=True, type=Path, help="fetch 产出的 JSONL")
    parser.add_argument(
        "--coverage",
        type=Path,
        default=None,
        help="覆盖度 sidecar（默认取 <in 去后缀>.coverage.jsonl；缺失则只能判 matched/empty 无法区分）",
    )
    parser.add_argument("--yes", action="store_true", help="确认写库（不加则只读 dry-run）")
    parser.add_argument(
        "--tolerance", type=float, default=DEFAULT_TOLERANCE, help=f"金额容差（默认 {DEFAULT_TOLERANCE}）"
    )
    parser.add_argument(
        "--keep-period-missing",
        action="store_true",
        help="保守模式：连报告期都对不上的假占位也不删（默认只在市股票删，非在市股票一律保留）",
    )
    parser.add_argument(
        "--no-fill-nulls",
        action="store_true",
        help="不回填 dividends 里为 NULL 的 announcement_date/送股/转增（默认回填，只填空值）",
    )
    parser.add_argument("--sample", type=int, default=5, help="每类打印几条样例（默认 5）")
    parser.add_argument("--evidence-out", type=Path, default=None, help="可选：把摘要 JSON 写到指定路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    write_mode = bool(args.yes)
    paths = resolve_and_validate_paths()

    coverage_path = args.coverage or coverage_path_for(args.input_path)
    if args.coverage is None and not coverage_path.exists():
        print(
            f"[WARN] 没找到覆盖度文件 {coverage_path}：只能按事件判定，"
            "抓取失败的股票会被算作「未抓取（保持原样）」而不是 unavailable。",
            flush=True,
        )

    events = load_events(args.input_path)
    coverage = load_coverage(coverage_path if coverage_path.exists() else None)
    if not events and not coverage:
        print(
            f"[ABORT] {args.input_path} 里既没有事件也没有覆盖度记录，拒绝运行"
            "（避免把隔离行全部误判成「源里没有」）。",
            flush=True,
        )
        return 2
    if not events:
        print(
            "[WARN] JSONL 里没有事件，但有覆盖度记录：本次只可能产生 deleted_empty / "
            "kept_* 类动作，写入前请确认 --in 不是截断文件。",
            flush=True,
        )

    duck = DuckDBStore(paths=paths)
    listed_stocks = load_listed_stocks(duck)
    print(
        f"参与判定的在市股票（stock_meta.is_listed=TRUE）: {len(listed_stocks)} 只"
        f"（不在其中的股票只走有正面证据的分支）",
        flush=True,
    )
    plan = plan_apply(
        events=events,
        existing=load_existing_dividends(duck),
        quarantine=load_quarantine(duck),
        coverage=coverage,
        listed_stocks=listed_stocks,
        tolerance=args.tolerance,
        keep_period_missing=args.keep_period_missing,
        fill_nulls=not args.no_fill_nulls,
        sample_size=args.sample,
    )
    counts = plan["counts"]
    mode = "写库模式（--yes）" if write_mode else "只读 dry-run（不写库）"
    print(format_report(plan, mode), flush=True)
    print("\n样例:\n" + json.dumps(plan["samples"], ensure_ascii=False, indent=2, default=str), flush=True)

    if not counts["quarantine_reconciles"]:
        print("[ABORT] 隔离区分账不闭合，拒绝写库。", flush=True)
        return 4

    if not write_mode:
        print(
            "\n只读 dry-run：未写库。确认无误后加 --yes 执行"
            "（写前自动 COPY 备份 dividends + dividends_quarantine 到 .planning/）。",
            flush=True,
        )
        return 0

    todo = (
        counts["dividend_inserted"]
        + counts["dividend_updated"]
        + counts["dividend_null_filled"]
        + counts["matched"]
        + counts["deleted_fake"]
        + counts["deleted_empty"]
        + counts["unavailable"]
        + counts["kept_amount_mismatch"]
        + counts["kept_coverage_gap"]
        - counts["kept_coverage_gap_already"]
    )
    if todo <= 0:
        print("\n没有需要写库的变更（幂等：第二次跑同一份 JSONL 计数应为全 0），跳过写库。", flush=True)
        return 0

    if any_write_lock_active(paths.duckdb_path):
        print("[ABORT] 检测到写锁活跃（自动更新/其它维护脚本正在写库），拒绝运行。", flush=True)
        return 3

    from scripts._maintenance_safety import backup_tables  # 延迟导入：只读路径不碰 .planning

    backup_dir = backup_tables(duck, ["dividends", "dividends_quarantine"], "dividend")
    print(f"[BACKUP] 已备份到: {backup_dir}", flush=True)

    written = execute_plan(duck, plan)
    print("\n写库完成:\n" + json.dumps(written, ensure_ascii=False, indent=2), flush=True)

    if args.evidence_out:
        args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "input": str(args.input_path),
            "coverage": str(coverage_path),
            "mode": "write",
            "counts": counts,
            "written": written,
            "backup_dir": str(backup_dir),
            "samples": plan["samples"],
        }
        args.evidence_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"证据: {args.evidence_out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
