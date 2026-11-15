"""用结构化源（巨潮 / 东财）重抓分红（除权）历史 → JSONL（**只抓，不写库**）。

为什么单独一个"只抓"脚本
------------------------
本机 Python 3.12 在「长循环 + DuckDB 连接 + HTTP/sleep」组合下会触发
`Fatal Python error: PyEval_SaveThread ... GIL released` 直接崩进程（已在
`executemany` 与港股抓取循环上各复现一次，见 `docs/runbooks/ops-knowledge-base.md`
D47 的同类结论）。因此本脚本**完全不 import duckdb**：股票清单由调用方通过
`--stocks` / stdin / `--all` 提供，抓取阶段只有 HTTP + 逐行 append JSONL。
写库全部交给 scripts/apply_dividend_history.py（短事务）。

数据源
------
- ``cninfo``（默认，主源）：``akshare.stock_dividend_cninfo(symbol)``
  列：实施方案公告日期 / 分红类型 / 送股比例 / 转增比例 / 派息比例 /
  股权登记日 / 除权日 / 派息日 / 股份到账日 / 实施方案分红说明 / 报告时间。
  深历史可用（万科 1992-03-20、方正科技 1993-03-22）。
- ``eastmoney``（备用）：``akshare.stock_fhps_detail_em(symbol)``
  列：报告期 / 现金分红-现金分红比例 / 股权登记日 / 除权除息日 / 方案进度 /
  预案公告日 / 最新公告日期。字段少（无派息日），用于交叉核对或主源不可用时兜底。

单位陷阱（必须记住）
--------------------
两家的「派息比例 / 现金分红比例」都是**每 10 股**金额，「送股比例 / 转增比例」
同样是**每 10 股**股数。落库 `dividends.dividend_per_share` 是**每股**口径。
本脚本**保持源口径**输出（字段名自带 `_per_10` 单位），由 apply 阶段统一 ÷10，
避免"抓一遍换一遍"的口径歧义。

输出（两个文件，apply 都要用）
------------------------------
1. ``--out <jsonl>``：一行一个分红事件。
2. 覆盖度 sidecar：默认 ``<out 去后缀>.coverage.jsonl``，一行一只股票，
   ``status`` ∈ ``ok`` / ``empty`` / ``partial`` / ``failed``。
   apply 阶段靠它区分三类完全不同的结论：
   - ``empty``   → 源确实没有这只股票的分红记录（隔离行可判 deleted_empty）
   - ``partial`` → 有返回但没有任何可用除权日（不可判定，隔离行保留下动）
   - ``failed``  → 源没回应（隔离行保留 + 改 reason=unavailable_from_source）

用法
----
    # 1) 少量指定
    python scripts/fetch_dividend_history.py --out .planning/div/events.jsonl \\
        --stocks 600519,000002

    # 2) 主控传入清单（stdin，一行或逗号分隔均可；**不要同时加 --all**）
    type list.txt | python scripts/fetch_dividend_history.py --out .planning/div/events.jsonl

    # 3) 全 A 股（akshare 清单 5,579 只）；分批跑满 --limit 即退出，--resume 续跑
    python scripts/fetch_dividend_history.py --all --out .planning/div/events.jsonl --limit 400
    python scripts/fetch_dividend_history.py --all --out .planning/div/events.jsonl --limit 400 --resume

    # 4) 备用源
    python scripts/fetch_dividend_history.py --out ... --stocks 600519 --source eastmoney
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # 允许 `python scripts/xxx.py` 直接运行
    sys.path.insert(0, str(PROJECT_ROOT))

LOGGER = logging.getLogger("fetch_dividend_history")

SOURCE_CNINFO = "cninfo"
SOURCE_EASTMONEY = "eastmoney"
SOURCES = (SOURCE_CNINFO, SOURCE_EASTMONEY)

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

#: JSONL 事件字段（顺序即写出顺序）
EVENT_FIELDS: tuple[str, ...] = (
    "stock_code",
    "ex_date",
    "record_date",
    "pay_date",
    "announcement_date",
    "report_period",
    "report_period_raw",
    "dividend_type",
    "cash_per_10",
    "stock_per_10",
    "transfer_per_10",
    "dividend_description",
    "source",
    "fetched_at",
)

#: 覆盖度 sidecar 字段
COVERAGE_FIELDS: tuple[str, ...] = (
    "stock_code",
    "status",
    "source",
    "rows",
    "events",
    "skipped_no_ex_date",
    "unmapped_report_period",
    "error",
    "fetched_at",
)

#: 内部字段 → 巨潮列名。
#: 除权日缺失即无法定位事件（丢行）；其余列缺失只降级为 None，事件保留。
CNINFO_COLUMNS: dict[str, str | tuple[str, ...]] = {
    "announcement_date": "实施方案公告日期",
    "dividend_type": "分红类型",
    "stock_per_10": "送股比例",
    "transfer_per_10": "转增比例",
    "cash_per_10": "派息比例",
    "record_date": "股权登记日",
    "ex_date": "除权日",
    "pay_date": "派息日",
    "report_period_raw": "报告时间",
    "dividend_description": "实施方案分红说明",
}

#: 内部字段 → 东财列名（预案公告日/最新公告日期 二选一，前者优先有值者）
EASTMONEY_COLUMNS: dict[str, str | tuple[str, ...]] = {
    "announcement_date": ("最新公告日期", "预案公告日"),
    "dividend_type": "方案进度",
    "stock_per_10": "送转股份-送股比例",
    "transfer_per_10": "送转股份-转股比例",
    "cash_per_10": "现金分红-现金分红比例",
    "record_date": "股权登记日",
    "ex_date": "除权除息日",
    "pay_date": None,  # 东财无派息日
    "report_period_raw": "报告期",
    "dividend_description": "现金分红-现金分红比例描述",
}

_YEAR_RE = re.compile(r"(\d{4})")
_NULL_TEXTS = frozenset({"", "-", "--", "nan", "nat", "none", "null", "inf"})


# --------------------------------------------------------------------------- #
# 标量清洗（不依赖 pandas：DataFrame/dict/None 都能吃）
# --------------------------------------------------------------------------- #
def _to_date(value: Any) -> date | None:
    """把源里的日期洗成 ``datetime.date``；缺失/非法一律 None。

    兼容：``date`` / ``datetime`` / ``pandas.Timestamp``（datetime 子类）/
    ``pandas.NaT``（datetime 子类但 ``.date()`` 返回 NaT）/ ``NaN`` /
    ``'2024-06-19'`` / ``'20240619'`` / ``'2024/06/19'`` / 空串。
    """
    if value is None:
        return None
    if isinstance(value, datetime):  # 含 pandas.Timestamp
        parsed = value.date()
        return None if parsed != parsed else parsed  # NaT != NaT → True
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in _NULL_TEXTS:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
        for fmt in ("%Y%m%d", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None
    return None  # NaN / np.float64 / 其它标量：不是日期


def _to_float(value: Any) -> float | None:
    """把源里的数值洗成 float；缺失/NaN/非法一律 None（0.0 保留为 0.0）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.lower() in _NULL_TEXTS:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return None if number != number else number
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return None if text.lower() in _NULL_TEXTS else text
    if isinstance(value, float) and value != value:  # NaN
        return None
    if type(value).__name__ == "NaTType":
        return None
    text = str(value).strip()
    return None if text.lower() in _NULL_TEXTS else text


def normalize_report_period(raw: Any) -> tuple[date | None, bool]:
    """把「报告期」洗成期末日期。

    返回 ``(period_end, unmapped)``：``unmapped=True`` 表示"有内容但认不出"，
    调用方应把它计进 ``unmapped_report_period`` 供人工复核（不是静默丢失）。

    巨潮是中文文本（``2025年报`` / ``2024半年报`` / ``2025三季报``），
    东财是日期（``2024-12-31``）。注意 ``半年报`` **包含** ``年报`` 三字，
    所以"半年/中报/中期"必须先判，否则半年报会被误判成 12-31。
    """
    if raw is None:
        return None, False
    direct = _to_date(raw)
    if direct is not None:
        return direct, False
    text = _to_text(raw)
    if text is None:
        return None, False
    match = _YEAR_RE.search(text)
    if match is None:
        return None, True
    year = int(match.group(1))
    rest = text[match.end():]
    if "半年" in rest or "中报" in rest or "中期" in rest or "二季" in rest or "第2季" in rest:
        month_day = (6, 30)
    elif "三季" in rest or "第3季" in rest:
        month_day = (9, 30)
    elif "一季" in rest or "第1季" in rest or "第一季" in rest:
        month_day = (3, 31)
    elif "年报" in rest or "年度" in rest or "全年" in rest or "年末" in rest:
        month_day = (12, 31)
    else:
        return None, True
    try:
        return date(year, month_day[0], month_day[1]), False
    except ValueError:  # pragma: no cover - year 已由正则保证
        return None, True


def _pick(record: Mapping[str, Any], spec: str | tuple[str, ...] | None) -> Any:
    """按列名取值；``spec`` 可以是候选列表（取第一个有值的）。"""
    if spec is None:
        return None
    candidates = (spec,) if isinstance(spec, str) else tuple(spec)
    for column in candidates:
        if column not in record:
            continue
        value = record[column]
        if _to_text(value) is None and _to_float(value) is None and _to_date(value) is None:
            continue
        return value
    return None


def _records(data: Any) -> list[Mapping[str, Any]]:
    """DataFrame / records 列表 / None → records 列表。"""
    if data is None:
        return []
    if hasattr(data, "to_dict"):
        try:
            return list(data.to_dict("records"))
        except (TypeError, ValueError):  # pragma: no cover - 非 DataFrame 的 to_dict
            return []
    return [row for row in data if isinstance(row, Mapping)]


# --------------------------------------------------------------------------- #
# 列名映射（纯函数，可单测；不联网）
# --------------------------------------------------------------------------- #
def _map_records(
    stock_code: str,
    records: Iterable[Mapping[str, Any]],
    columns: Mapping[str, str | tuple[str, ...] | None],
    *,
    fetched_at: str,
    source: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events: list[dict[str, Any]] = []
    stats = {"rows": 0, "skipped_no_ex_date": 0, "unmapped_report_period": 0}
    for record in records:
        stats["rows"] += 1
        ex_date = _to_date(_pick(record, columns.get("ex_date")))
        if ex_date is None:
            # 降级策略：没有除权日就无法定位事件（股权登记日 ≠ 除权日，不能顶替）
            stats["skipped_no_ex_date"] += 1
            continue
        raw_period = _pick(record, columns.get("report_period_raw"))
        period_end, unmapped = normalize_report_period(raw_period)
        if unmapped:
            stats["unmapped_report_period"] += 1
        record_date = _to_date(_pick(record, columns.get("record_date")))
        pay_date = _to_date(_pick(record, columns.get("pay_date")))
        announcement_date = _to_date(_pick(record, columns.get("announcement_date")))
        events.append(
            {
                "stock_code": stock_code,
                # 其余日期字段缺失时降级为 None（事件保留），只有 ex_date 是硬要求
                "ex_date": ex_date.isoformat(),
                "record_date": record_date.isoformat() if record_date else None,
                "pay_date": pay_date.isoformat() if pay_date else None,
                "announcement_date": announcement_date.isoformat() if announcement_date else None,
                "report_period": period_end.isoformat() if period_end else None,
                "report_period_raw": _to_text(raw_period),
                "dividend_type": _to_text(_pick(record, columns.get("dividend_type"))),
                "cash_per_10": _to_float(_pick(record, columns.get("cash_per_10"))),
                "stock_per_10": _to_float(_pick(record, columns.get("stock_per_10"))),
                "transfer_per_10": _to_float(_pick(record, columns.get("transfer_per_10"))),
                # 原文说明（如「10转增3股派5元（含税）」）：B 股等场景巨潮的
                # 派息比例列会是空值而说明里有金额，留给后续回填核对用。
                "dividend_description": _to_text(
                    _pick(record, columns.get("dividend_description"))
                ),
                "source": source,
                "fetched_at": fetched_at,
            }
        )
    return events, stats


def map_cninfo_records(
    stock_code: str,
    records: Iterable[Mapping[str, Any]],
    *,
    fetched_at: str,
    source: str = SOURCE_CNINFO,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """巨潮 records → 内部事件（单位仍是"每 10 股"）。"""
    return _map_records(stock_code, records, CNINFO_COLUMNS, fetched_at=fetched_at, source=source)


def map_eastmoney_records(
    stock_code: str,
    records: Iterable[Mapping[str, Any]],
    *,
    fetched_at: str,
    source: str = SOURCE_EASTMONEY,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """东财 records → 内部事件（单位仍是"每 10 股"）。"""
    return _map_records(
        stock_code, records, EASTMONEY_COLUMNS, fetched_at=fetched_at, source=source
    )


def map_records_for_source(
    source: str,
    stock_code: str,
    records: Iterable[Mapping[str, Any]],
    *,
    fetched_at: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if source == SOURCE_EASTMONEY:
        return map_eastmoney_records(stock_code, records, fetched_at=fetched_at)
    return map_cninfo_records(stock_code, records, fetched_at=fetched_at)


# --------------------------------------------------------------------------- #
# 抓取（唯一联网点；akshare 延迟导入，import 本模块不需要 akshare）
# --------------------------------------------------------------------------- #
def fetch_frame(stock_code: str, source: str) -> Any:
    import akshare as ak  # noqa: PLC0415 - 延迟导入：只抓脚本不拖慢无网/单测场景

    if source == SOURCE_EASTMONEY:
        return ak.stock_fhps_detail_em(symbol=stock_code)
    return ak.stock_dividend_cninfo(symbol=stock_code)


def fetch_stock(
    stock_code: str,
    source: str = SOURCE_CNINFO,
    *,
    fetched_at: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """抓一只股票 → ``(events, coverage_row)``；**任何异常都不抛**，只记状态。"""
    stamp = fetched_at or datetime.now(UTC).isoformat(timespec="seconds")
    coverage: dict[str, Any] = dict.fromkeys(COVERAGE_FIELDS)
    coverage.update(
        {"stock_code": stock_code, "source": source, "fetched_at": stamp, "events": 0, "rows": 0}
    )
    try:
        frame = fetch_frame(stock_code, source)
    except KeyError:
        # akshare 在「源返回空 records」时取列会抛 KeyError（巨潮 688981 实测），
        # 语义上等于"源里没有该股分红记录"，记为 empty 而不是 failed。
        coverage.update({"status": STATUS_EMPTY, "skipped_no_ex_date": 0, "unmapped_report_period": 0})
        return [], coverage
    except Exception as error:  # noqa: BLE001 - 单只失败不得中断全量抓取
        LOGGER.warning("[%s] 抓取失败: %s: %s", stock_code, type(error).__name__, error)
        coverage.update(
            {
                "status": STATUS_FAILED,
                "error": f"{type(error).__name__}: {error}",
                "skipped_no_ex_date": 0,
                "unmapped_report_period": 0,
            }
        )
        return [], coverage

    records = _records(frame)
    if not records:
        coverage.update({"status": STATUS_EMPTY, "skipped_no_ex_date": 0, "unmapped_report_period": 0})
        return [], coverage

    events, stats = map_records_for_source(source, stock_code, records, fetched_at=stamp)
    coverage.update(
        {
            "status": STATUS_OK if events else STATUS_PARTIAL,
            "rows": stats["rows"],
            "events": len(events),
            "skipped_no_ex_date": stats["skipped_no_ex_date"],
            "unmapped_report_period": stats["unmapped_report_period"],
        }
    )
    return events, coverage


# --------------------------------------------------------------------------- #
# 股票清单 / 续跑状态
# --------------------------------------------------------------------------- #
def parse_stock_codes(raw: str) -> list[str]:
    """``'600519,000002 600036'`` → 去重保序的代码列表。"""
    codes: list[str] = []
    for token in re.split(r"[\s,;，、]+", raw or ""):
        code = token.strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def read_codes_from_stdin() -> list[str]:
    """stdin 有管道数据才读（tty 直接返回空，避免挂死）。"""
    stream = sys.stdin
    if stream is None or stream.isatty():
        return []
    try:
        return parse_stock_codes(stream.read())
    except (OSError, ValueError):  # pragma: no cover - 极端 IO 情况
        return []


def fetch_a_share_universe() -> list[str]:
    """akshare 全 A 股清单（实测 5,579 只，约 7s）。不碰 DuckDB。"""
    import akshare as ak  # noqa: PLC0415 - 延迟导入

    frame = ak.stock_info_a_code_name()
    codes: list[str] = []
    seen: set[str] = set()
    for raw in frame["code"].tolist():
        code = str(raw).strip()
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def load_seen_codes(events_path: Path, coverage_path: Path) -> set[str]:
    """``--resume``：已抓成功的股票集合。

    事件 JSONL 里出现过的 stock_code 一律算已处理（按需求）；
    覆盖度里 status=ok/empty/partial 也算（这些股票本来就不会产生事件行，
    但已经问过源了）；只有 failed 允许重试。
    """
    seen: set[str] = set()
    for path in (events_path, coverage_path):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                code = _to_text(payload.get("stock_code"))
                if not code:
                    continue
                status = _to_text(payload.get("status"))
                if status in (None, STATUS_OK, STATUS_EMPTY, STATUS_PARTIAL):
                    seen.add(code)
    return seen


def coverage_path_for(events_path: Path) -> Path:
    """事件文件 → 覆盖度 sidecar 路径（apply 侧同一规则）。"""
    suffix = events_path.suffix
    stem = events_path.name[: -len(suffix)] if suffix else events_path.name
    return events_path.with_name(f"{stem}.coverage.jsonl")


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #
def _write_line(handle: Any, payload: Mapping[str, Any], fields: Sequence[str]) -> None:
    ordered = {field: payload.get(field) for field in fields}
    handle.write(json.dumps(ordered, ensure_ascii=False) + "\n")
    handle.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抓巨潮/东财分红（除权）历史为 JSONL（只抓不写库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out", required=True, type=Path, help="事件 JSONL 输出路径")
    parser.add_argument("--stocks", default=None, help="股票清单，逗号/空格分隔，如 600519,000002")
    parser.add_argument(
        "--all", action="store_true", help="抓全 A 股（akshare 清单约 5,579 只；优先于读 stdin）"
    )
    parser.add_argument("--limit", type=int, default=None, help="本次最多处理多少只（分批复跑用）")
    parser.add_argument(
        "--resume", action="store_true", help="跳过 JSONL/覆盖度里已处理过的 stock_code（追加写）"
    )
    parser.add_argument("--sleep", type=float, default=0.4, help="每只之间限速秒数（默认 0.4）")
    parser.add_argument(
        "--source", choices=SOURCES, default=SOURCE_CNINFO, help="数据源（默认 cninfo，备用 eastmoney）"
    )
    parser.add_argument("--coverage", type=Path, default=None, help="覆盖度 sidecar 路径（默认自动推导）")
    parser.add_argument("--verbose", action="store_true", help="打印每只股票的抓取明细")
    return parser


def resolve_stock_codes(args: argparse.Namespace) -> tuple[list[str], str]:
    """优先级：``--stocks`` > ``--all`` > stdin 管道。返回 ``(codes, 来源说明)``。

    ``--all`` 排在 stdin 前面是为了避免"stdin 是个不关闭的管道"时挂死：
    显式给了 --all 就绝不去读 stdin。
    """
    if args.stocks:
        return parse_stock_codes(args.stocks), "--stocks"
    if args.all:
        return fetch_a_share_universe(), "--all(akshare 全 A 股清单)"
    piped = read_codes_from_stdin()
    if piped:
        return piped, "stdin"
    raise SystemExit(
        "缺少股票清单：用 --stocks 600519,000002 / --all（全 A 股）/ 管道传入 stdin 三选一。"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    out_path: Path = args.out
    coverage_path: Path = args.coverage or coverage_path_for(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    coverage_path.parent.mkdir(parents=True, exist_ok=True)

    if not args.resume:
        for existing in (out_path, coverage_path):
            if existing.exists() and existing.stat().st_size > 0:
                raise SystemExit(
                    f"{existing} 已存在且有内容：续跑请加 --resume，重新抓请先删除或换路径。"
                )

    codes, origin = resolve_stock_codes(args)
    total_universe = len(codes)
    if args.resume:
        seen = load_seen_codes(out_path, coverage_path)
        codes = [code for code in codes if code not in seen]
    pending_before_limit = len(codes)
    if args.limit is not None and args.limit >= 0:
        codes = codes[: args.limit]

    print(
        f"股票清单来源: {origin} | 清单 {total_universe} 只 | 本次待抓 {len(codes)} 只"
        f"（--resume 后剩余 {pending_before_limit} 只）| 源 {args.source} | 限速 {args.sleep}s",
        flush=True,
    )
    print(f"事件输出: {out_path}\n覆盖度输出: {coverage_path}", flush=True)
    if not codes:
        print("没有待抓股票（全部已处理）。", flush=True)
        return 0

    started = time.monotonic()
    counters = {"ok": 0, "empty": 0, "partial": 0, "failed": 0, "events": 0}
    failures: list[dict[str, Any]] = []

    with (
        out_path.open("a", encoding="utf-8") as events_handle,
        coverage_path.open("a", encoding="utf-8") as coverage_handle,
    ):
        for index, code in enumerate(codes, start=1):
            events, coverage = fetch_stock(code, args.source)
            for event in events:
                _write_line(events_handle, event, EVENT_FIELDS)
            _write_line(coverage_handle, coverage, COVERAGE_FIELDS)
            counters[coverage["status"]] = counters.get(coverage["status"], 0) + 1
            counters["events"] += len(events)
            if coverage["status"] == STATUS_FAILED:
                failures.append({"stock_code": code, "error": coverage.get("error")})
            LOGGER.debug(
                "[%s] status=%s rows=%s events=%s skipped_no_ex_date=%s",
                code,
                coverage["status"],
                coverage.get("rows"),
                len(events),
                coverage.get("skipped_no_ex_date"),
            )
            if index % 50 == 0 or index == len(codes):
                elapsed = time.monotonic() - started
                rate = index / elapsed if elapsed > 0 else 0.0
                eta = (len(codes) - index) / rate if rate > 0 else 0.0
                print(
                    f"[{index}/{len(codes)}] ok={counters['ok']} empty={counters['empty']} "
                    f"partial={counters['partial']} failed={counters['failed']} "
                    f"events={counters['events']} elapsed={elapsed:.1f}s "
                    f"{rate:.2f}只/s eta={eta:.0f}s",
                    flush=True,
                )
            if args.sleep and index < len(codes):
                time.sleep(args.sleep)

    elapsed = time.monotonic() - started
    summary = {
        "mode": "fetch",
        "source": args.source,
        "origin": origin,
        "universe": total_universe,
        "attempted": len(codes),
        "ok": counters["ok"],
        "empty": counters["empty"],
        "partial": counters["partial"],
        "failed": counters["failed"],
        "events": counters["events"],
        "elapsed_seconds": round(elapsed, 1),
        "events_path": str(out_path),
        "coverage_path": str(coverage_path),
        "failures": failures[:20],
    }
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if counters["failed"]:
        print(
            f"\n注意：{counters['failed']} 只抓取失败，它们的分红结论**不可判定**——"
            "apply 阶段会保留对应隔离行并改 reason=unavailable_from_source。",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
