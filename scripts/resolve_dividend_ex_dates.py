"""用「权益分派实施公告」PDF 补齐隔离区里的真实除权日（2026-11-14 建，2026-11-15 改造，reports/121）。

背景
----
`dividends_quarantine` 里 8,467 行 / 2,436 只的隔离原因全是
`unverified_period_end_placeholder`：`ex_date` 其实是**报告期末占位**
（外部数据包只有报告期末，没有真实除权日），主链靠 TDX `xdxr` 匹配不上就进了隔离区。

「权益分派实施公告」PDF 里写着真实日期（股权登记日 / 除权(息)日 / 派发日），
本脚本把它们解析出来，**核对通过**后写回 `dividends` 并把隔离行清掉。

三段式（为什么不是"一条龙"）
----------------------------
本机 Python 3.12 在「长时间循环 + DuckDB 连接 + HTTP/sleep」组合下必现
`Fatal Python error: PyEval_SaveThread ... GIL released`（已在 `executemany`
与港股抓取循环上各复现一次）。因此**抓取/解析期间绝不持有 DuckDB 连接**：

1. **只读阶段**：一次性把隔离行与 `xdxr` 载入内存字典，然后**关闭连接**；
2. **抓取/解析阶段**：纯 HTTP + 纯文本解析，逐行 append 到 JSONL（不 import duckdb、不开连接）；
3. **应用阶段**（`--apply`）：只读 JSONL，在**一次短事务**里写 `dividends` + 删隔离行
   （写前备份 + 写锁检查）。

分批复跑
--------
`--batch-size`（默认 50）跑满即退出并打印还剩多少只；`--resume` 按 JSONL 里
已有的 `(stock_code, placeholder_date)` 跳过，因此**每批用独立进程跑**是安全的，
也是本机推荐用法（避免任何长进程）：

    # 第一批（正式库：只读 + 只写 JSONL）
    python scripts/resolve_dividend_ex_dates.py --out .planning/dividend-ex-dates/plan.jsonl
    # 第二批、第三批……同样命令，自动跳过已处理的股票
    python scripts/resolve_dividend_ex_dates.py --out .planning/dividend-ex-dates/plan.jsonl --resume
    # 查看命中率
    python scripts/resolve_dividend_ex_dates.py --stats .planning/dividend-ex-dates/plan.jsonl
    # 写库（一次短事务；写前自动备份 dividends + dividends_quarantine）
    # 默认只写 xdxr 佐证条目；pdf_only 需人工确认后加 --include-pdf-only
    python scripts/resolve_dividend_ex_dates.py --apply .planning/dividend-ex-dates/plan.jsonl --yes

正文载体（不只有 PDF）
----------------------
现代公告是 `.PDF`（pypdf 提取），**2005 年前后的旧公告在巨潮上只有 `.html` 页面**
（实际是 GBK 编码，`Content-Type` 常错写成 utf-8；pypdf 拿到会抛 `PdfStreamError`）。
两种载体统一由 `_BodyTextCache` 按 URL 后缀 / `Content-Type` 分派到
`extract_pdf_text` 或 `extract_html_text`——实测柳工 000528 的 2004 年度实施公告
只能走 HTML 这条路（`reports/121` 失败形态 #8）。

核对纪律（错一天就会让回测错一年，所以宁可少补）
------------------------------------------------
1. 解析到的 `ex_date` 必须落在占位日之后、且相差 ≤ 400 天；
2. 公告日期必须落在 [占位日 - 30 天, ex_date] 之间（先公告后除权）；
3. 若 `xdxr` 在该股有 ±7 天内的除权事件：±1 天内 → `confidence="xdxr_confirmed"`；
   ±7 天内但都对不上 → **不写**（`xdxr_mismatch`）；
4. 解析不到 / 日期不自洽 → **不写**，在 JSONL 里留一行带 `status` 的痕迹并计数；
5. 公告标题的会计期间（YYYY 年年度 / 半年度 / 中期 / 第 N 季度）必须与占位日对得上
   ——实测「2024 年中期实施公告」比「2024 年年度实施公告」离占位日 2024-12-31 更近，
   只按邻近度挑公告会把中期分派写成年度分派；
6. 公告里的每股现金分红与隔离行 `dividend_per_share` 偏差 > 50% → **只标注**
   `amount_match=false`，不否决（隔离行多为"全年合计"口径，与"本期增量"天然差 2-3 倍）；
7. **写库硬规则**：只有 `confidence="xdxr_confirmed"` 的条目才默认允许写 `dividends`；
   仅凭 PDF 解析（`confidence="pdf_only"`）的条目会被 `--apply` 扣下，必须人工看过
   JSONL 后用 `--include-pdf-only` 显式放行。

只读保证
--------
`--out` / `--stats` 全程只读（读连接显式 `read_only`）。`--apply` 才写，且写前
先做「受影响表备份 + 应用级写锁检查」，写入在单个事务内完成，失败整体回滚。

依赖说明（pypdf 未在锁文件里）
------------------------------
PDF 文本提取需要 `pypdf`。本机 `.venv` 已手工 `pip install pypdf`（7.1.0），但
`pyproject.toml` / `uv.lock` **没有登记**——2026-11-14 环境无法访问 PyPI，`uv lock` 失败。

- **启用方式**：联网后执行 `uv add pypdf && uv lock`（把依赖固化进锁文件）；
- **未安装时**：脚本不会静默降级，会打印
  `解析分红公告 PDF 需要 pypdf：联网后执行 uv add pypdf && uv lock …` 并以退出码 4 结束；
- 解析逻辑本身（`app/core/pdf/dividend_parser.py`）不依赖 pypdf，单测用固定文本 fixture。
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _entry in (str(PROJECT_ROOT), str(SCRIPTS_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import httpx  # noqa: E402

from app.core.adapters.base import FetchRequest  # noqa: E402
from app.core.adapters.cninfo_adapter import CNINFOAdapter  # noqa: E402
from app.core.pdf.dividend_parser import (  # noqa: E402
    PdfSupportUnavailable,
    extract_html_text,
    extract_pdf_text,
    parse_dividend_amounts,
    parse_dividend_dates,
)
from app.core.storage.duckdb_store import DuckDBStore  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    DatabasePathSet,
    PathIsolationError,
    resolve_and_validate_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    exclusive_update,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)
# httpx 默认每个请求打一行 INFO；批量跑 2,436 只时会淹没真正的进度日志
logging.getLogger("httpx").setLevel(logging.WARNING)

QUARANTINE_REASON = "unverified_period_end_placeholder"
PLAN_SCHEMA = 1
DEFAULT_PLAN_PATH = PROJECT_ROOT / ".planning" / "dividend-ex-dates" / "plan.jsonl"

#: 占位日 → 真实除权日 的最大跨度（年报分红最晚可拖到大半年后）
MAX_GAP_DAYS = 400
#: 公告日允许早于占位日的天数（季度分红可能在报告期结束前就公告预案）
MAX_ANNOUNCE_LEAD_DAYS = 30
#: 每个占位行最多试几份候选公告 PDF（按公告日与占位日的距离排序）
MAX_ANNOUNCEMENTS_PER_ROW = 3
#: xdxr 交叉核对窗口（天）
XDXR_WINDOW_DAYS = 7
#: 视为「同一除权日」的最大偏差（天）
XDXR_STRICT_DAYS = 1
#: PDF 下载间隔（秒，源侧限速）
PDF_DELAY = 0.3
#: 提取出的正文少于这个字符数，按「扫描件无文本」处理
MIN_TEXT_CHARS = 80
#: 「每股现金分红」与隔离行外部数据相对偏差超过这个比例时，只**标注**
#: `amount_match=false` 供人工复核，不否决写库：隔离行常是"全年合计"口径，
#: 与年度实施公告的"本期增量"天然相差 2-3 倍（2026-11-15 正式库抽样实测）。
MAX_AMOUNT_DRIFT = 0.5

#: 公告标题里的会计期间 → 报告期末（用来把公告和占位日对上号）
_PERIOD_END_BY_NAME: dict[str, tuple[int, int]] = {
    "年度中期": (6, 30),
    "年度半年度": (6, 30),
    "年度": (12, 31),
    "年年度": (12, 31),
    "半年度": (6, 30),
    "中期": (6, 30),
    "第一季度": (3, 31),
    "一季度": (3, 31),
    "第三季度": (9, 30),
    "三季度": (9, 30),
    "前三季度": (9, 30),
}
#: 交替顺序有意义：`年度中期`/`年度半年度` 必须排在 `年度` 之前，
#: 否则「2010 年度中期利润分配实施公告」会被当成年度（报告期末 12-31）。
_RE_PERIOD_IN_TITLE = re.compile(
    r"(20\d{2})\s*年?\s*"
    r"(年度中期|年度半年度|年年度|年度|半年度|中期|第一季度|一季度|第三季度|三季度|前三季度)"
)

#: 标题必须命中其一
_TITLE_MUST = ("权益分派", "分红", "利润分配", "派息", "股息")
#: 标题还必须带一个"这是实施/发放"的标记。老公告（2000 年代）常写成
#: 「2004年度分红派息公告」，没有"实施"二字——实测柳工 000528 的 2004 年度
#: 实施公告因此被漏掉（reports/121 抽样失败形态 #6：老式标题无"实施"）。
_TITLE_IMPL_MARKERS = ("实施", "分红派息公告", "派息公告", "股息发放", "红利发放")
#: 标题命中即排除：优先股 / 纯 B 股（A 股代码时）/ 取消 / 预案 / 明确不分红 / 更正。
#: 「不进行」「拟不」用来挡掉「关于 2020 年度拟不进行利润分配的公告」这类反向公告
#: （实测利通电子 603629 踩到：公司当年不分红，却被隔离行当成了分派）。
_TITLE_REJECT = (
    "优先股", "境内上市外资股", "Ｂ股", "B股", "取消", "预案",
    "提示性公告", "网上说明会", "路演", "更正",
    "不进行", "不分配", "不分红", "不派发", "不派息", "拟不",
)
#: 只对 B 股代码（沪市 900xxx / 深市 200xxx）放开的排除项：
#: 否则这些代码永远配不到带"B 股"字样的实施公告。
_B_SHARE_ONLY_REJECTS = ("境内上市外资股", "Ｂ股", "B股")
#: B 股代码前缀
_B_SHARE_PREFIXES = ("900", "200")

_PDF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://www.cninfo.com.cn/",
}


# ─── 数据结构 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QuarantineRow:
    """隔离区里的一行（占位除权日）。"""

    stock_code: str
    placeholder_date: date
    dividend_per_share: float | None = None
    stock_dividend: float | None = None
    transfer_share: float | None = None
    rights_issue: float | None = None
    rights_issue_price: float | None = None


@dataclass
class TargetStock:
    stock_code: str
    rows: list[QuarantineRow]


@dataclass
class Resolution:
    """一个 (股票, 占位日) 的处理结果——JSONL 的一行。"""

    stock_code: str
    placeholder_date: date
    status: str
    ex_date: date | None = None
    announcement_date: date | None = None
    xdxr_confirmed: bool = False
    xdxr_near: list[date] = field(default_factory=list)
    amount_match: bool | None = None
    pdf_dividend_per_share: float | None = None
    #: xdxr_confirmed = 有 xdxr 除权事件同天佐证；pdf_only = 只有公告 PDF 一个来源
    confidence: str | None = None
    pdf_url: str | None = None
    title: str | None = None
    evidence: str | None = None
    note: str | None = None
    counters: dict[str, int] = field(default_factory=dict)
    payload: dict[str, float | None] = field(default_factory=dict)

    def to_json(self, *, source_db: str | None) -> dict:
        record = {
            "schema": PLAN_SCHEMA,
            "kind": "dividend_ex_date",
            "source_db": source_db,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        for key, value in asdict(self).items():
            record[key] = _jsonable(value)
        return record


def _jsonable(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


# ─── 只读阶段（读库 → 内存；读完即关连接）──────────────────────────────


def _load_quarantine_rows(conn, reason: str) -> list[QuarantineRow]:
    cursor = conn.execute(
        """
        SELECT stock_code, ex_date AS placeholder_date, dividend_per_share,
               stock_dividend, transfer_share, rights_issue, rights_issue_price
        FROM dividends_quarantine
        WHERE quarantine_reason = ? AND ex_date IS NOT NULL
        ORDER BY ex_date DESC, stock_code
        """,
        [reason],
    )
    columns = [item[0] for item in cursor.description]
    rows: list[QuarantineRow] = []
    for raw in cursor.fetchall():
        record = dict(zip(columns, raw, strict=True))
        rows.append(QuarantineRow(
            stock_code=str(record["stock_code"]),
            placeholder_date=record["placeholder_date"],
            dividend_per_share=record["dividend_per_share"],
            stock_dividend=record["stock_dividend"],
            transfer_share=record["transfer_share"],
            rights_issue=record["rights_issue"],
            rights_issue_price=record["rights_issue_price"],
        ))
    return rows


def _load_xdxr(
    conn, stock_codes: list[str], window_start: date, window_end: date,
) -> dict[str, list[tuple[date, int | None]]]:
    """一次查完目标股票的 xdxr 除权事件（内存字典，循环里不再查库）。"""
    if not stock_codes:
        return {}
    placeholders = ",".join("?" for _ in stock_codes)
    cursor = conn.execute(
        f"""
        SELECT stock_code, event_date, category
        FROM xdxr
        WHERE stock_code IN ({placeholders})
          AND event_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ORDER BY stock_code, event_date
        """,
        [*stock_codes, window_start, window_end],
    )
    out: dict[str, list[tuple[date, int | None]]] = {}
    for code, event_date, category in cursor.fetchall():
        if event_date is None:
            continue
        out.setdefault(str(code), []).append((event_date, category))
    return out


def _select_targets(
    rows: Iterable[QuarantineRow],
    *,
    done_pairs: set[tuple[str, str]],
    latest_only: bool,
    sample: int,
    seed: int,
    limit: int,
) -> list[TargetStock]:
    """按隔离行算出本轮目标股票（已完成的 (股票, 占位日) 先剔除）。"""
    by_code: dict[str, list[QuarantineRow]] = {}
    for row in rows:
        if (row.stock_code, row.placeholder_date.isoformat()) in done_pairs:
            continue
        by_code.setdefault(row.stock_code, []).append(row)
    targets = [
        TargetStock(
            stock_code=code,
            rows=sorted(items, key=lambda r: r.placeholder_date, reverse=True),
        )
        for code, items in by_code.items()
    ]
    # 默认按最近占位日排序：先用最新的数据把主链补齐
    targets.sort(key=lambda t: (t.rows[0].placeholder_date, t.stock_code), reverse=True)
    if sample:
        rng = random.Random(seed)
        targets = rng.sample(targets, min(sample, len(targets)))
        targets.sort(key=lambda t: t.stock_code)
    if latest_only:
        targets = [
            TargetStock(stock_code=t.stock_code, rows=[t.rows[0]]) for t in targets
        ]
    if limit:
        targets = targets[:limit]
    return targets


# ─── 抓取 / 解析阶段（**不碰 DuckDB**）────────────────────────────────


def _is_implementation_announcement(
    title: str | None, *, allow_b_share: bool = False,
) -> bool:
    """标题是不是「权益分派实施」公告（排除预案/优先股/纯 B 股）。"""
    text = (title or "").replace(" ", "").replace("\u3000", "")
    if not text:
        return False
    rejected = (
        tuple(bad for bad in _TITLE_REJECT if bad not in _B_SHARE_ONLY_REJECTS)
        if allow_b_share else _TITLE_REJECT
    )
    if any(bad in text for bad in rejected):
        return False
    if not any(marker in text for marker in _TITLE_IMPL_MARKERS):
        return False
    return any(good in text for good in _TITLE_MUST)


def _period_end_from_title(title: str | None) -> date | None:
    """从公告标题里的「YYYY年年度/半年度/中期/第 N 季度」推出报告期末。

    标题里没有可识别的期间（如「权益分派实施公告」「特别分红实施公告」）返回 None，
    由调用方退回按公告日邻近度排序。
    """
    text = (title or "").replace(" ", "").replace("　", "")
    match = _RE_PERIOD_IN_TITLE.search(text)
    if match is None:
        return None
    month, day = _PERIOD_END_BY_NAME[match.group(2)]
    try:
        return date(int(match.group(1)), month, day)
    except ValueError:  # pragma: no cover - 年份/月份来自正则，理论上不会失败
        return None


def _fetch_announcements(
    adapter: CNINFOAdapter, code: str, earliest: date, latest: date, today: date,
) -> list[dict]:
    """取该股实施公告候选；分类过滤没命中时再放宽到全类别查一次。"""
    start = earliest - timedelta(days=MAX_ANNOUNCE_LEAD_DAYS)
    end = min(latest + timedelta(days=MAX_GAP_DAYS), today)
    attempts: list[dict[str, object]] = [
        {"category": "category_qyfpxzcs_szsh"},
        {},
    ]
    for extra in attempts:
        request = FetchRequest(
            stock_codes=[code],
            data_type="announcements",
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            extra_params=dict(extra),
        )
        result = adapter.fetch(request)
        allow_b_share = code.startswith(_B_SHARE_PREFIXES)
        candidates = [
            row for row in (result.data or [])
            if row.get("pdf_url") and _is_implementation_announcement(
                row.get("title"), allow_b_share=allow_b_share,
            )
        ]
        if candidates:
            return candidates
    return []


class _BodyTextCache:
    """按 URL 缓存公告正文（同一份公告可能被多行候选命中）。

    两种载体都要支持：现代公告是 `.PDF`（pypdf 提取），2005 年前后的旧公告在
    巨潮上只有 `.html` 页面（pypdf 会抛 PdfStreamError，实测柳工 000528 漏整条）。
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._cache: dict[str, tuple[str | None, str | None]] = {}

    def get(self, url: str) -> tuple[str | None, str | None]:
        """返回 (text, error)；下载失败 text=None, error=原因。"""
        if url in self._cache:
            return self._cache[url]
        try:
            time.sleep(PDF_DELAY)
            response = self._client.get(url, timeout=60, headers=_PDF_HEADERS)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if url.lower().endswith((".html", ".htm")) or "html" in content_type:
                text = extract_html_text(response.content)
            else:
                text = extract_pdf_text(response.content)
        except PdfSupportUnavailable:
            raise  # 环境缺 pypdf 是全局问题，必须显式冒泡而不是计入单份 PDF 失败
        except Exception as error:  # noqa: BLE001 - 单份 PDF 失败不中断
            outcome = (None, f"{type(error).__name__}: {error}")
        else:
            outcome = (text, None)
        self._cache[url] = outcome
        return outcome


def _pick_for_row(
    row: QuarantineRow,
    announcements: list[dict],
    used_ids: set[str],
    pdf_cache: _BodyTextCache,
    xdxr_events: list[tuple[date, int | None]],
    today: date,
) -> Resolution:
    """为单个占位行挑一份公告并核对；返回 Resolution。"""
    resolution = Resolution(
        stock_code=row.stock_code,
        placeholder_date=row.placeholder_date,
        status="no_announcement",
        payload={
            "dividend_per_share": row.dividend_per_share,
            "stock_dividend": row.stock_dividend,
            "transfer_share": row.transfer_share,
            "rights_issue": row.rights_issue,
            "rights_issue_price": row.rights_issue_price,
        },
        counters={
            "announcements": len(announcements),
            "pdf_attempted": 0,
            "pdf_downloaded": 0,
            "pdf_no_text": 0,
            "pdf_parsed": 0,
            "pdf_inconsistent": 0,
            "period_skipped": 0,
            "amount_mismatch": 0,
        },
    )
    counters = resolution.counters
    low = row.placeholder_date - timedelta(days=MAX_ANNOUNCE_LEAD_DAYS)
    high = min(row.placeholder_date + timedelta(days=MAX_GAP_DAYS), today)
    in_window = [
        ann for ann in announcements
        if ann.get("announcement_date") and low <= ann["announcement_date"] <= high
        and str(ann.get("announcement_id") or ann.get("pdf_url")) not in used_ids
    ]
    # 期间对齐：标题写着「YYYY年年度/半年度/中期/第 N 季度」的公告，其报告期末
    # 必须等于占位日。实测贵州茅台占位 2024-12-31 时，「2024年中期」实施公告离
    # 占位日最近（17 天），靠邻近度排序会先抓它——期间对齐能直接把它排除，
    # 既省一次下载，也避免"把中期分派当年度分派"写错一年。
    matched, unknown = [], []
    for ann in in_window:
        period_end = _period_end_from_title(ann.get("title"))
        if period_end is None:
            unknown.append(ann)
        elif period_end == row.placeholder_date:
            matched.append(ann)
        else:
            counters["period_skipped"] += 1
    candidates = matched + unknown
    candidates.sort(key=lambda ann: abs((ann["announcement_date"] - row.placeholder_date).days))
    for ann in candidates[:MAX_ANNOUNCEMENTS_PER_ROW]:
        counters["pdf_attempted"] += 1
        # 先把"试过哪份公告"记进结果：失败时报告要靠它做失败形态归类
        resolution.title = ann.get("title")
        resolution.pdf_url = ann["pdf_url"]
        text, error = pdf_cache.get(ann["pdf_url"])
        if text is None:
            resolution.note = f"PDF 取回失败：{error}"
            continue
        counters["pdf_downloaded"] += 1
        if len(text.strip()) < MIN_TEXT_CHARS:
            counters["pdf_no_text"] += 1
            resolution.status = "pdf_no_text"
            resolution.note = f"PDF 正文仅 {len(text.strip())} 字符（疑似扫描件）"
            continue
        parsed = parse_dividend_dates(text)
        if parsed is None or parsed.ex_date is None:
            resolution.status = "parse_failed"
            resolution.note = "正文里没解析出除权(息)日"
            continue
        counters["pdf_parsed"] += 1
        gap_days = (parsed.ex_date - row.placeholder_date).days
        if not 0 <= gap_days <= MAX_GAP_DAYS:
            counters["pdf_inconsistent"] += 1
            resolution.status = "date_inconsistent"
            resolution.note = f"PDF 除权日 {parsed.ex_date} 与占位日相差 {gap_days} 天"
            continue
        if ann["announcement_date"] > parsed.ex_date:
            counters["pdf_inconsistent"] += 1
            resolution.status = "date_inconsistent"
            resolution.note = f"公告日 {ann['announcement_date']} 晚于除权日 {parsed.ex_date}"
            continue
        # 金额核对：**只作证据信号，不作否决**（2026-11-15 正式库抽样裁决）。
        # 隔离行的 dividend_per_share 常是"全年合计"（中期已派 + 年度待派），而
        # 年度实施公告只写本期增量——实测 300533 隔离行 1.2 = xdxr 0.7(中期)
        # + 0.5(年度)，605589 的 0.45 = 0.25 + 0.2，偏差 55-67% 是**正常口径差**。
        # 我们写的是 ex_date 而不是金额，金额对不上并不能否定日期；真正需要人工
        # 复核的"没有 xdxr 佐证"由 pdf_only 闸门兜（见 _plan_entries 的硬规则）。
        amounts = parse_dividend_amounts(text)
        pdf_dps = amounts[0] if amounts else None
        amount_match: bool | None = None
        if amounts and row.dividend_per_share:
            drift = min(
                abs(value - row.dividend_per_share) / abs(row.dividend_per_share)
                for value in amounts
            )
            amount_match = drift <= MAX_AMOUNT_DRIFT
            if not amount_match:
                counters["amount_mismatch"] += 1
        near = [event for event, _ in xdxr_events
                if abs((event - parsed.ex_date).days) <= XDXR_WINDOW_DAYS]
        strict_match = any(
            abs((event - parsed.ex_date).days) <= XDXR_STRICT_DAYS for event in near
        )
        if near and not strict_match:
            resolution.status = "xdxr_mismatch"
            resolution.note = f"PDF 除权日 {parsed.ex_date} 与 xdxr {sorted(near)[:3]} 不一致"
            resolution.xdxr_near = sorted(near)
            continue
        used_ids.add(str(ann.get("announcement_id") or ann["pdf_url"]))
        resolution.status = "resolved"
        resolution.ex_date = parsed.ex_date
        resolution.announcement_date = ann["announcement_date"]
        resolution.xdxr_confirmed = strict_match
        resolution.confidence = "xdxr_confirmed" if strict_match else "pdf_only"
        resolution.amount_match = amount_match
        resolution.pdf_dividend_per_share = pdf_dps
        resolution.xdxr_near = sorted(near)
        resolution.pdf_url = ann["pdf_url"]
        resolution.title = ann.get("title")
        resolution.evidence = parsed.evidence
        resolution.note = None
        return resolution
    if resolution.status == "no_announcement" and counters["pdf_downloaded"] == 0:
        if counters["pdf_attempted"]:
            resolution.status = "pdf_unavailable"
        elif counters["period_skipped"]:
            resolution.status = "other_period_only"
            resolution.note = f"只有 {counters['period_skipped']} 份别的会计期间的实施公告"
    return resolution


def _resolve_stock(
    target: TargetStock,
    adapter: CNINFOAdapter,
    pdf_cache: _BodyTextCache,
    xdxr_map: dict[str, list[tuple[date, int | None]]],
    today: date,
) -> list[Resolution]:
    """处理一只股票的全部（或最近的）占位行；只用内存数据 + HTTP。"""
    code = target.stock_code
    earliest = min(row.placeholder_date for row in target.rows)
    latest = max(row.placeholder_date for row in target.rows)
    try:
        announcements = _fetch_announcements(adapter, code, earliest, latest, today)
    except Exception as error:  # noqa: BLE001 - 单股失败不中断整批
        logger.warning("  %s 公告拉取失败：%s: %s", code, type(error).__name__, error)
        return [
            Resolution(
                stock_code=code,
                placeholder_date=row.placeholder_date,
                status="error",
                note=f"公告拉取失败：{type(error).__name__}: {error}",
            )
            for row in target.rows
        ]
    used_ids: set[str] = set()
    events = xdxr_map.get(code, [])
    return [
        _pick_for_row(row, announcements, used_ids, pdf_cache, events, today)
        for row in target.rows
    ]


# ─── JSONL ───────────────────────────────────────────────────────────


def _iter_plan(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("跳过 JSONL 第 %d 行（不是合法 JSON）", line_no)
                continue
            if record.get("kind") == "dividend_ex_date":
                yield record


def _done_pairs(path: Path) -> set[tuple[str, str]]:
    """JSONL 里已经处理过的 (股票, 占位日)——`--resume` 的判据。"""
    return {
        (str(record.get("stock_code")), str(record.get("placeholder_date")))
        for record in _iter_plan(path)
        if record.get("stock_code") and record.get("placeholder_date")
    }


def _append_resolutions(path: Path, resolutions: list[Resolution], *, source_db: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for resolution in resolutions:
            handle.write(json.dumps(
                resolution.to_json(source_db=source_db), ensure_ascii=False,
            ) + "\n")
            handle.flush()


# ─── 应用阶段（一次短事务）────────────────────────────────────────────


def _plan_entries(path: Path, *, include_pdf_only: bool = False) -> tuple[list[dict], int]:
    """从 JSONL 取出可应用条目（status=resolved，按 (股票, 占位日) 去重保首条）。

    写库路径的**硬规则**（2026-11-15，按 lead 裁决）：
    只有拿到 `xdxr` 同天佐证的条目（`confidence="xdxr_confirmed"`）才默认允许写主表；
    仅凭公告 PDF 解析出来的（`confidence="pdf_only"`）**不自动改进 `dividends`**——
    本项目已实测过"解析成错日期"的两种真实形态（小标题关键词串位、跨期公告错配），
    错一天就会让回测错一年，所以 pdf_only 必须由人看过 JSONL 的人工确认后用
    `--include-pdf-only` 显式放行。返回 (可应用条目, 被扣下的 pdf_only 条数)。
    """
    seen: set[tuple[str, str]] = set()
    entries: list[dict] = []
    held_pdf_only = 0
    for record in _iter_plan(path):
        if record.get("status") != "resolved" or not record.get("ex_date"):
            continue
        key = (str(record["stock_code"]), str(record["placeholder_date"]))
        if key in seen:
            continue
        seen.add(key)
        if record.get("confidence") == "pdf_only" and not include_pdf_only:
            held_pdf_only += 1
            continue
        entries.append(record)
    return entries, held_pdf_only


def _revalidate_entry(entry: dict) -> str | None:
    """应用前再核一遍（防止手工编辑 JSONL 写入离谱日期）。返回错误原因或 None。"""
    try:
        placeholder = date.fromisoformat(str(entry["placeholder_date"]))
        ex_date = date.fromisoformat(str(entry["ex_date"]))
    except (KeyError, ValueError):
        return "日期字段缺失或格式非法"
    gap = (ex_date - placeholder).days
    if not 0 <= gap <= MAX_GAP_DAYS:
        return f"占位日 → 除权日相差 {gap} 天，超出 [{0}, {MAX_GAP_DAYS}]"
    announced = entry.get("announcement_date")
    if announced:
        try:
            announcement_date = date.fromisoformat(str(announced))
        except ValueError:
            return "公告日格式非法"
        if announcement_date > ex_date:
            return f"公告日 {announcement_date} 晚于除权日 {ex_date}"
    return None


def _backup_tables(duck: DuckDBStore, tag: str, plan_path: Path) -> Path:
    """写前把受影响表 COPY 成 parquet 快照（.planning 已 gitignore）。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = PROJECT_ROOT / ".planning" / f"maintenance-backup-{tag}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    with duck.write_connection() as conn:
        for table in ("dividends", "dividends_quarantine"):
            target = str(out / f"{table}.parquet").replace("'", "''")
            conn.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
    (out / "plan.jsonl").write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
    logger.info("[BACKUP] 受影响表 + 计划文件已备份到 %s", out)
    return out


def _apply_plan(
    duck: DuckDBStore, entries: list[dict], *, reason: str,
) -> dict[str, int]:
    """在**一次短事务**里写 dividends 并删隔离行（全部语句走事务自己的连接）。"""
    counts = {
        "inserted": 0, "already_present": 0, "quarantine_deleted": 0,
        "quarantine_missing": 0, "rejected": 0,
    }
    with duck.transaction() as conn:
        for entry in entries:
            problem = _revalidate_entry(entry)
            if problem is not None:
                counts["rejected"] += 1
                logger.warning("拒绝 %s %s：%s",
                               entry.get("stock_code"), entry.get("placeholder_date"), problem)
                continue
            code = str(entry["stock_code"])
            placeholder = date.fromisoformat(str(entry["placeholder_date"]))
            present = conn.execute(
                """
                SELECT 1 FROM dividends_quarantine
                WHERE stock_code = ? AND ex_date = ? AND quarantine_reason = ?
                """,
                [code, placeholder, reason],
            ).fetchone()
            if present is None:
                counts["quarantine_missing"] += 1
                continue
            ex_date = date.fromisoformat(str(entry["ex_date"]))
            existing = conn.execute(
                "SELECT 1 FROM dividends WHERE stock_code = ? AND ex_date = ?",
                [code, ex_date],
            ).fetchone()
            if existing is not None:
                counts["already_present"] += 1
            else:
                payload = entry.get("payload") or {}
                conn.execute(
                    """
                    INSERT INTO dividends
                        (stock_code, ex_date, announcement_date, dividend_per_share,
                         stock_dividend, transfer_share, rights_issue, rights_issue_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        code,
                        ex_date,
                        _as_date(entry.get("announcement_date")),
                        payload.get("dividend_per_share"),
                        payload.get("stock_dividend"),
                        payload.get("transfer_share"),
                        payload.get("rights_issue"),
                        payload.get("rights_issue_price"),
                    ],
                )
                counts["inserted"] += 1
            conn.execute(
                """
                DELETE FROM dividends_quarantine
                WHERE stock_code = ? AND ex_date = ? AND quarantine_reason = ?
                """,
                [code, placeholder, reason],
            )
            counts["quarantine_deleted"] += 1
    return counts


def _as_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


# ─── 统计（只读 JSONL）────────────────────────────────────────────────


def _print_stats(path: Path) -> None:
    records = list(_iter_plan(path))
    if not records:
        logger.info("JSONL 里没有记录：%s", path)
        return
    total = len(records)
    with_ann = sum(1 for r in records if (r.get("counters") or {}).get("announcements"))
    with_pdf = sum(1 for r in records if (r.get("counters") or {}).get("pdf_downloaded"))
    parsed = sum(1 for r in records if (r.get("counters") or {}).get("pdf_parsed"))
    resolved = [r for r in records if r.get("status") == "resolved"]
    confirmed = sum(1 for r in resolved if r.get("xdxr_confirmed"))
    by_status: dict[str, int] = {}
    for record in records:
        by_status[str(record.get("status"))] = by_status.get(str(record.get("status")), 0) + 1

    def pct(part: int, whole: int) -> str:
        return f"{part / whole * 100:.1f}%" if whole else "—"

    logger.info("=" * 64)
    logger.info("样本行数 %d（每行 = 一个 (股票, 占位日)）", total)
    logger.info("公告找到 %d/%d (%s)", with_ann, total, pct(with_ann, total))
    logger.info("PDF 下载 %d/%d (%s)", with_pdf, total, pct(with_pdf, total))
    logger.info("解析成功 %d/%d (%s)", parsed, with_pdf, pct(parsed, with_pdf))
    logger.info("xdxr 一致 %d/%d (%s)", confirmed, len(resolved), pct(confirmed, len(resolved)))
    logger.info("最终可写 %d/%d (%s)", len(resolved), total, pct(len(resolved), total))
    pdf_only = sum(1 for r in resolved if r.get("confidence") == "pdf_only")
    logger.info("  其中 xdxr 佐证 %d 条 / 仅 PDF（pdf_only）%d 条",
                len(resolved) - pdf_only, pdf_only)
    logger.info("状态分布 %s", json.dumps(by_status, ensure_ascii=False, sort_keys=True))
    logger.info("=" * 64)


# ─── 主流程 ──────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="用权益分派实施公告 PDF 补齐隔离区的真实除权日")
    ap.add_argument("--out", type=Path, default=None,
                    help=f"抓取/解析模式：把结果逐行 append 到该 JSONL（默认 {DEFAULT_PLAN_PATH}）")
    ap.add_argument("--apply", type=Path, default=None,
                    help="应用模式：读 JSONL，在一次短事务里写 dividends + 删隔离行")
    ap.add_argument("--stats", type=Path, default=None,
                    help="只读统计模式：打印 JSONL 的命中率与状态分布")
    ap.add_argument("--resume", action="store_true",
                    help="按 JSONL 里已有的 (股票, 占位日) 跳过，支持分批复跑")
    ap.add_argument("--batch-size", type=int, default=50,
                    help="单次进程最多处理多少只股票（跑满即退出，默认 50）")
    ap.add_argument("--limit", type=int, default=0,
                    help="本轮最多选取多少只股票（0=全部；按占位日从新到旧）")
    ap.add_argument("--sample", type=int, default=0,
                    help="随机抽样 N 只股票（与 --seed 配合可复现）")
    ap.add_argument("--seed", type=int, default=20261115, help="抽样随机种子")
    ap.add_argument("--latest-only", action="store_true",
                    help="每只股票只处理最近的占位行（抽样/试点用）")
    ap.add_argument("--sleep", type=float, default=0.5, help="每只股票之间的间隔秒数（源侧限速）")
    ap.add_argument("--include-pdf-only", action="store_true",
                    help="--apply 时也写入没有 xdxr 佐证的 pdf_only 条目（默认扣下，需人工确认）")
    ap.add_argument("--yes", action="store_true", help="--apply 的显式确认（写正式库必须给）")
    return ap


def _run_apply(args: argparse.Namespace, paths: DatabasePathSet) -> int:
    plan_path: Path = args.apply
    if not plan_path.exists():
        logger.error("JSONL 不存在：%s", plan_path)
        return 2
    entries, held_pdf_only = _plan_entries(
        plan_path, include_pdf_only=args.include_pdf_only,
    )
    logger.info(
        "JSONL 可应用条目 %d 条（%s）；另有 %d 条 pdf_only（无 xdxr 佐证）%s",
        len(entries), plan_path, held_pdf_only,
        "已一并放行" if args.include_pdf_only else "已扣下，人工确认后加 --include-pdf-only",
    )
    if not entries:
        logger.info("没有可写入的条目")
        return 0
    if not args.yes:
        logger.error("--apply 需要 --yes 显式确认（会修改 %s）", paths.duckdb_path)
        return 2
    source_dbs = {str(record.get("source_db")) for record in _iter_plan(plan_path)}
    source_dbs.discard("None")
    if source_dbs and str(paths.duckdb_path) not in {str(Path(p)) for p in source_dbs}:
        logger.error(
            "计划文件的来源库 %s 与本轮目标库 %s 不一致，已拒绝应用"
            "（若确属预期，请重新生成计划）", sorted(source_dbs), paths.duckdb_path,
        )
        return 2

    duck = DuckDBStore(paths=paths)
    _backup_tables(duck, "resolve-dividend-ex-dates", plan_path)
    try:
        with exclusive_update(paths.duckdb_path):
            counts = _apply_plan(duck, entries, reason=QUARANTINE_REASON)
            summary = {"stock_count": len(entries), **counts}
            logger.info("写入完成 %s", json.dumps(summary, ensure_ascii=False))
            logger.info(json.dumps({
                "action": "resolve_dividend_ex_dates_apply",
                **summary,
                "db": str(paths.duckdb_path),
                "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }, ensure_ascii=False))
    except UpdateLockError as error:
        logger.error("自动更新正在持锁，稍后重试：%s", error)
        return 3
    return 0


def _run_fetch(args: argparse.Namespace, paths: DatabasePathSet) -> int:
    plan_path: Path = args.out or DEFAULT_PLAN_PATH
    done_pairs = _done_pairs(plan_path) if args.resume else set()
    if args.resume and not plan_path.exists() and args.out:
        logger.info("--resume 但 %s 不存在，按首次运行处理", plan_path)

    duck = DuckDBStore(paths=paths)
    # ── 只读阶段：一次性物化到内存，然后关连接（循环里绝不再碰 DuckDB）
    with duck.read_connection() as conn:
        rows = _load_quarantine_rows(conn, QUARANTINE_REASON)
        eligible = _select_targets(
            rows,
            done_pairs=done_pairs,
            latest_only=args.latest_only,
            sample=args.sample,
            seed=args.seed,
            limit=args.limit,
        )
        targets = eligible[: args.batch_size] if args.batch_size else eligible
        if targets:
            window_start = min(t.rows[-1].placeholder_date for t in targets) - timedelta(
                days=MAX_ANNOUNCE_LEAD_DAYS)
            window_end = max(t.rows[0].placeholder_date for t in targets) + timedelta(
                days=MAX_GAP_DAYS)
            xdxr_map = _load_xdxr(
                conn, [t.stock_code for t in targets], window_start, window_end,
            )
        else:
            xdxr_map = {}
    del duck
    #: 仅作 JSONL 溯源字符串（不是连接）：--apply 时用来拒绝"别的库的计划"
    source_db = str(paths.duckdb_path)
    logger.info(
        "只读阶段完成：隔离行 %d 行 / %d 只（待处理 %d 只）；本轮目标 %d 只；"
        "xdxr 载入 %d 只；DuckDB 连接已关闭",
        len(rows), len({row.stock_code for row in rows}), len(eligible),
        len(targets), len(xdxr_map),
    )
    if not targets:
        logger.info("没有待处理的股票（--resume 已全部完成？）")
        return 0

    # ── 抓取 / 解析阶段：只用 HTTP + 内存字典
    today = datetime.now(UTC).date()
    adapter = CNINFOAdapter()
    resolved_count = 0
    status_counts: dict[str, int] = {}
    try:
        with httpx.Client(follow_redirects=True) as client:
            pdf_cache = _BodyTextCache(client)
            for index, target in enumerate(targets, 1):
                resolutions = _resolve_stock(target, adapter, pdf_cache, xdxr_map, today)
                _append_resolutions(plan_path, resolutions, source_db=source_db)
                for resolution in resolutions:
                    status_counts[resolution.status] = status_counts.get(resolution.status, 0) + 1
                resolved_count += sum(1 for r in resolutions if r.status == "resolved")
                logger.info(
                    "[%d/%d] %s → %s（可写 %d 条）",
                    index, len(targets), target.stock_code,
                    ",".join(sorted({r.status for r in resolutions})),
                    sum(1 for r in resolutions if r.status == "resolved"),
                )
                time.sleep(args.sleep)
    except PdfSupportUnavailable as error:
        logger.error("%s", error)
        return 4
    finally:
        adapter.close()

    remaining = len(eligible) - len(targets)
    logger.info("本轮处理 %d 只，可写 %d 条，状态分布 %s",
                len(targets), resolved_count,
                json.dumps(status_counts, ensure_ascii=False, sort_keys=True))
    if remaining > 0:
        logger.info("还有 %d 只未处理，请再跑一次（同一命令加 --resume）", remaining)
    logger.info(json.dumps({
        "action": "resolve_dividend_ex_dates_out",
        "plan": str(plan_path),
        "stocks_processed": len(targets),
        "proposals": resolved_count,
        "status_counts": status_counts,
        "remaining_stocks": remaining,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }, ensure_ascii=False))
    return 0


def main() -> None:
    ap = _build_parser()
    args = ap.parse_args()

    try:
        paths = resolve_and_validate_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    if args.stats is not None:
        _print_stats(args.stats)
        return
    if args.apply is not None:
        raise SystemExit(_run_apply(args, paths))
    if args.batch_size is not None and args.batch_size <= 0:
        ap.error("--batch-size 必须是正整数")
    raise SystemExit(_run_fetch(args, paths))


if __name__ == "__main__":
    main()
