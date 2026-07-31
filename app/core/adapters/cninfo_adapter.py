"""CNINFO (巨潮资讯网) 适配器 — 法定披露层

证监会指定信息披露平台，所有数据 confidence="strict"。

Endpoints:
- 公告搜索: POST https://www.cninfo.com.cn/new/hisAnnouncement/query
- 股票→orgId 映射: GET https://www.cninfo.com.cn/new/data/szse_stock.json
- PDF 下载: https://static.cninfo.com.cn/{adjunctUrl}  (M8 任务实现)

公告类别 (category 参数):
- category_ndbg_szsh       年报
- category_bndbg_szsh       半年报
- category_yjdbg_szsh       一季报
- category_sjdbg_szsh       三季报
- category_qyfpxzcs_szsh    权益分派
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.adapters.base import (
    BaseAdapter,
    FetchRequest,
    FetchResult,
)

logger = logging.getLogger(__name__)

__all__ = ["CNINFOAdapter"]


# ─── 常量 ────────────────────────────────────────────────────────────

_CNINFO_BASE = "https://www.cninfo.com.cn"
_SEARCH_URL = f"{_CNINFO_BASE}/new/hisAnnouncement/query"
_PDF_BASE = "https://static.cninfo.com.cn"

# 股票→orgId 映射源（szse 为主，sse/bj 为 best-effort 补充）
_STOCK_LIST_URLS: list[str] = [
    f"{_CNINFO_BASE}/new/data/szse_stock.json",
    f"{_CNINFO_BASE}/new/data/sse_stock.json",
    f"{_CNINFO_BASE}/new/data/bj_stock.json",
]

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_DEFAULT_REFERER = (
    f"{_CNINFO_BASE}/new/commonUrl/pageOfSearch?url=disclosure/list/search"
)

# ─── 公告类别常量 ────────────────────────────────────────────────────

CATEGORY_ANNUAL = "category_ndbg_szsh"          # 年报
CATEGORY_SEMI_ANNUAL = "category_bndbg_szsh"     # 半年报
CATEGORY_Q1 = "category_yjdbg_szsh"             # 一季报
CATEGORY_Q3 = "category_sjdbg_szsh"             # 三季报
CATEGORY_DIVIDEND = "category_qyfpxzcs_szsh"    # 权益分派

#: 供外部调用方查阅的类别映射
CATEGORY_MAP: dict[str, str] = {
    "annual": CATEGORY_ANNUAL,
    "semi_annual": CATEGORY_SEMI_ANNUAL,
    "q1": CATEGORY_Q1,
    "q3": CATEGORY_Q3,
    "dividend": CATEGORY_DIVIDEND,
}


def _column_for_code(code: str) -> str:
    """根据股票代码前缀推断 CNINFO column 参数（交易所板块）。

    6xxxxx → sse (上海)
    0/3xxxxx → szse (深圳)
    8/4xxxxx → bj (北交所)
    其余默认 szse。
    """
    if not code:
        return "szse"
    first = code[0]
    if first == "6":
        return "sse"
    if first in ("0", "3"):
        return "szse"
    if first in ("8", "4"):
        return "bj"
    return "szse"


# ─── 权益分派标题解析正则 ────────────────────────────────────────────
# CNINFO 公告标题常见格式：
#   "2023年年度权益分派实施公告"
#   "每10股派发现金红利2.50元（含税），每10股转增5股"
#   "每10股送红股3股"
_RE_CASH = re.compile(
    r"每\s*10\s*股.*?派\s*发?\s*(?:现\s*金\s*红\s*利|息)?\s*([\d.]+)\s*元",
    re.DOTALL,
)
_RE_TRANSFER = re.compile(
    r"每\s*10\s*股.*?转\s*增\s*([\d.]+)\s*股",
    re.DOTALL,
)
_RE_SEND = re.compile(
    r"每\s*10\s*股.*?送\s*红?\s*股\s*([\d.]+)\s*股",
    re.DOTALL,
)


# ─── 适配器 ──────────────────────────────────────────────────────────


class CNINFOAdapter(BaseAdapter):
    """CNINFO 巨潮资讯网适配器

    法定信息披露平台 — 所有抓取数据 confidence="strict"。

    用法::

        from app.core.adapters.cninfo_adapter import CNINFOAdapter
        from app.core.adapters.base import FetchRequest

        adapter = CNINFOAdapter(rate_limit=1.5)
        result = adapter.fetch(FetchRequest(
            data_type="announcements",
            stock_codes=["000001"],
            start_date="2024-01-01",
            end_date="2024-12-31",
        ))
        # result.metadata.confidence == "strict"
    """

    def __init__(self, rate_limit: float = 1.5, timeout: float = 30.0) -> None:
        super().__init__(
            name="cninfo",
            supported={"announcements", "dividends"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout
        self._client: httpx.Client | None = None
        # 实例级缓存（避免类属性在多实例间共享）
        self._org_id_cache: dict[str, str] = {}
        self._org_id_loaded: bool = False

    # ─── 协议方法 ──────────────────────────────────────────────────

    def fetch(self, request: FetchRequest) -> FetchResult:
        """派发到对应 data_type 处理器"""
        if request.data_type == "announcements":
            return self._fetch_announcements(request)
        if request.data_type == "dividends":
            return self._fetch_dividends(request)
        return self._make_empty_result(
            reason=f"CNINFO 不支持的数据类型: {request.data_type}",
            confidence="missing",
        )

    def close(self) -> None:
        """释放底层 HTTP 连接池"""
        if self._client is not None:
            self._client.close()
            self._client = None

    # ─── HTTP 客户端 ──────────────────────────────────────────────

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self._timeout),
                headers={
                    "User-Agent": _DEFAULT_USER_AGENT,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": _DEFAULT_REFERER,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
        return self._client

    # ─── 股票→orgId 映射缓存 ─────────────────────────────────────

    def _ensure_org_id_map(self) -> dict[str, str]:
        """懒加载并缓存股票→orgId 映射（一次加载，后续命中缓存）"""
        if self._org_id_loaded:
            return self._org_id_cache

        client = self._get_client()
        for url in _STOCK_LIST_URLS:
            try:
                self._wait_rate_limit()
                resp = client.get(url)
                if resp.status_code != 200:
                    logger.debug(
                        "CNINFO 股票列表 %s 返回 %d，跳过",
                        url, resp.status_code,
                    )
                    continue
                payload = resp.json()
                stock_list = payload.get("stockList") or []
                for item in stock_list:
                    code = item.get("code")
                    org_id = item.get("orgId")
                    if code and org_id and code not in self._org_id_cache:
                        self._org_id_cache[code] = org_id
            except (httpx.HTTPError, ValueError) as e:
                # sse/bj 端点可能不存在，best-effort 忽略
                logger.debug("加载 CNINFO 股票列表 %s 失败: %s", url, e)

        self._org_id_loaded = True
        logger.info("CNINFO 股票→orgId 映射已加载: %d 只", len(self._org_id_cache))
        return self._org_id_cache

    def _resolve_org_id(self, code: str) -> str | None:
        """解析单只股票的 orgId，缺失返回 None"""
        return self._ensure_org_id_map().get(code)

    # ─── 公告搜索核心 ─────────────────────────────────────────────

    def _query_announcements(
        self,
        stock_code: str,
        category: str | None,
        start_date: str | None,
        end_date: str | None,
        page_size: int = 50,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """调用 CNINFO 公告搜索 API，自动翻页。

        返回规范化后的公告条目列表。orgId 无法解析时抛 ValueError。
        """
        org_id = self._resolve_org_id(stock_code)
        if not org_id:
            raise ValueError(f"无法解析股票 {stock_code} 的 orgId（不在 CNINFO 股票列表中）")

        se_date = self._format_se_date(start_date, end_date)
        column = _column_for_code(stock_code)

        results: list[dict[str, Any]] = []
        client = self._get_client()

        for page_num in range(1, max_pages + 1):
            form: dict[str, str] = {
                "pageNum": str(page_num),
                "pageSize": str(page_size),
                "column": column,
                "tabName": "fulltext",
                "stock": f"{stock_code},{org_id}",
                "category": category or "",
                "seDate": se_date,
                "isHLtitle": "true",
            }

            try:
                self._wait_rate_limit()
                resp = client.post(
                    _SEARCH_URL,
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                # P2修复: 429重试1次
                if resp.status_code == 429:
                    import time as _time
                    _time.sleep(2)
                    resp = client.post(
                        _SEARCH_URL,
                        data=form,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                logger.error(
                    "CNINFO 公告查询失败 stock=%s page=%d: %s",
                    stock_code, page_num, e,
                )
                break

            ann_list = payload.get("announcements") or []
            if not ann_list:
                break

            for raw in ann_list:
                results.append(self._normalize_announcement(raw, stock_code))

            # 翻页终止：当前页返回少于 page_size 说明已到末尾
            total_record = payload.get("totalRecordNum") or payload.get("totalAnnouncement") or 0
            if len(ann_list) < page_size or page_num * page_size >= total_record:
                break

        return results

    @staticmethod
    def _format_se_date(start: str | None, end: str | None) -> str:
        """构造 CNINFO seDate 参数: 'YYYY-MM-DD~YYYY-MM-DD'

        两侧均缺失时返回空字符串（不限日期）。
        """
        if not start and not end:
            return ""
        return f"{start or ''}~{end or ''}"

    @staticmethod
    def _normalize_announcement(
        raw: dict[str, Any], stock_code: str,
    ) -> dict[str, Any]:
        """将 CNINFO 原始公告条目规范化为统一字段名"""
        ann_time_ms = raw.get("announcementTime") or 0
        ann_dt: datetime | None = None
        if ann_time_ms:
            ann_dt = datetime.fromtimestamp(ann_time_ms / 1000.0, tz=timezone.utc)

        adjunct_url = raw.get("adjunctUrl") or ""
        pdf_url = f"{_PDF_BASE}/{adjunct_url.lstrip('/')}" if adjunct_url else None

        return {
            "stock_code": stock_code,
            "sec_code": raw.get("secCode") or stock_code,
            "sec_name": raw.get("secName"),
            "announcement_id": raw.get("announcementId"),
            "title": raw.get("announcementTitle"),
            "announcement_time": ann_dt,
            "announcement_date": ann_dt.date() if ann_dt else None,
            "org_id": raw.get("orgId"),
            "adjunct_url": adjunct_url or None,
            "pdf_url": pdf_url,
            "announcement_type": raw.get("announcementType"),
        }

    # ─── announcements 处理器 ─────────────────────────────────────

    def _fetch_announcements(self, request: FetchRequest) -> FetchResult:
        codes = request.stock_codes
        if not codes:
            return self._make_empty_result(
                reason="CNINFO 公告查询需要至少一个 stock_code（全市场扫描不支持）",
                confidence="missing",
            )

        # category 可直接传 CNINFO 原始值，也可传 CATEGORY_MAP 别名
        category_raw = request.extra_params.get("category")
        category = CATEGORY_MAP.get(category_raw, category_raw) if category_raw else None
        page_size = int(request.extra_params.get("page_size", 50))
        max_pages = int(request.extra_params.get("max_pages", 20))

        all_data: list[dict[str, Any]] = []
        errors: list[str] = []

        for code in codes:
            try:
                items = self._query_announcements(
                    stock_code=code,
                    category=category,
                    start_date=request.start_date,
                    end_date=request.end_date,
                    page_size=page_size,
                    max_pages=max_pages,
                )
                all_data.extend(items)
            except Exception as e:
                logger.exception("查询 %s 公告异常", code)
                errors.append(f"{code}: {e}")

        return self._finalize_result(
            data=all_data,
            label="announcements",
            stock_count=len(codes),
            errors=errors,
        )

    # ─── dividends 处理器 ─────────────────────────────────────────

    def _fetch_dividends(self, request: FetchRequest) -> FetchResult:
        codes = request.stock_codes
        if not codes:
            return self._make_empty_result(
                reason="CNINFO 分红查询需要至少一个 stock_code",
                confidence="missing",
            )

        page_size = int(request.extra_params.get("page_size", 50))
        max_pages = int(request.extra_params.get("max_pages", 20))

        all_data: list[dict[str, Any]] = []
        errors: list[str] = []

        for code in codes:
            try:
                items = self._query_announcements(
                    stock_code=code,
                    category=CATEGORY_DIVIDEND,
                    start_date=request.start_date,
                    end_date=request.end_date,
                    page_size=page_size,
                    max_pages=max_pages,
                )
                for item in items:
                    parsed = self._parse_dividend_from_announcement(item)
                    if parsed is not None:
                        all_data.append(parsed)
            except Exception as e:
                logger.exception("查询 %s 分红异常", code)
                errors.append(f"{code}: {e}")

        return self._finalize_result(
            data=all_data,
            label="dividends",
            stock_count=len(codes),
            errors=errors,
        )

    @staticmethod
    def _parse_dividend_from_announcement(
        ann: dict[str, Any],
    ) -> dict[str, Any] | None:
        """从权益分派公告条目解析分红数据

        CNINFO 不直接提供结构化分红字段，需从公告标题正则提取：
        - 每10股派发现金红利X元（含税） → dividend_per_share = X/10
        - 每10股转增X股                  → transfer_share = X/10
        - 每10股送红股X股                → stock_dividend = X/10

        三项均无匹配（程序性公告如"审议通过"）返回 None。
        ex_date 需解析公告 PDF 才能获得，留给 M8 阶段。

        P0#2.6修复: 区分"预案"和"实施"——只解析已实施的分红, 跳过预案
        Announcement dates are not ex-dates.  A row without an authoritative
        ex-date must not be promoted to the formal dividends table.
        """
        title = ann.get("title") or ""

        # P0#2.6修复: 跳过预案/提案类公告, 只保留实施类
        # 预案标题通常含"预案"、"拟"、"提案"、"审议通过"
        # 实施标题通常含"实施"、"权益分派"、"派息"、"除权除息"
        if any(kw in title for kw in ["预案", "拟", "提案", "审议通过", "尚需", "待审"]):
            return None

        cash_per_10: float | None = None
        transfer_per_10: float | None = None
        send_per_10: float | None = None

        m = _RE_CASH.search(title)
        if m:
            try:
                cash_per_10 = float(m.group(1))
            except ValueError:
                pass

        m = _RE_TRANSFER.search(title)
        if m:
            try:
                transfer_per_10 = float(m.group(1))
            except ValueError:
                pass

        m = _RE_SEND.search(title)
        if m:
            try:
                send_per_10 = float(m.group(1))
            except ValueError:
                pass

        if cash_per_10 is None and transfer_per_10 is None and send_per_10 is None:
            return None

        ex_date = ann.get("ex_date")
        if ex_date is None:
            return None

        return {
            "stock_code": ann.get("stock_code"),
            "announcement_id": ann.get("announcement_id"),
            "announcement_date": ann.get("announcement_date"),
            "announcement_time": ann.get("announcement_time"),
            "title": title,
            "pdf_url": ann.get("pdf_url"),
            # 每10股 → 每股
            "dividend_per_share": (
                cash_per_10 / 10.0 if cash_per_10 is not None else None
            ),
            "stock_dividend": (
                send_per_10 / 10.0 if send_per_10 is not None else None
            ),
            "transfer_share": (
                transfer_per_10 / 10.0 if transfer_per_10 is not None else None
            ),
            "ex_date": ex_date,
        }

    # ─── 结果构建辅助 ─────────────────────────────────────────────

    def _finalize_result(
        self,
        data: list[dict[str, Any]],
        label: str,
        stock_count: int,
        errors: list[str] | None = None,
    ) -> FetchResult:
        """统一构建 FetchResult，confidence 固定 strict（法定披露源）"""
        # 序列化规范化数据用于溯源哈希（datetime 用 str 兜底）
        raw_repr = json.dumps(
            {"source": "cninfo", "label": label, "stocks": stock_count, "rows": data},
            default=str,
            ensure_ascii=False,
        )
        return self._make_result(
            data=data,
            raw_response=raw_repr,
            confidence="strict",
            error="; ".join(errors) if errors else None,
            api_version="v1",
        )
