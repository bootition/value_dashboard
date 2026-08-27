"""东财 F10 业务概览适配器（独立低频域）

数据来源：东方财富 PC_HSF10 公开结构化接口（reports/67 首期主源）：
- CompanySurvey/PageAjax  公司资料（公司简介 / 经营范围 / CSRC 行业 / 员工数）
- BusinessAnalysis/PageAjax  主营构成（按产品 / 行业 / 地区，仅随定期报告更新）

语义边界（reports/67 §4）：
- 每项数据 confidence="approximate"，必须显示报告期、来源与抓取时间。
- 空 / 畸形响应（无 jbzl / zygcfx，或返回 {"status": ..., "message": ...}）
  如实返回 missing，不伪造值；北交所缺失同样自然返回 missing。
- 本适配器使用独立实例与独立限速（默认 0.5s，不高于 2 req/s），
  不与其他数据源共享限速窗口，防止 F10 故障传染股票价格源。
- 支持注入 httpx.Client（含 transport），便于离线 fixture 测试。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

import httpx

from app.core.adapters.base import (
    BaseAdapter,
    FetchRequest,
    FetchResult,
)

logger = logging.getLogger(__name__)

__all__ = ["EastMoneyF10Adapter"]

# ─── 常量 ────────────────────────────────────────────────────────────

_SURVEY_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
_ANALYSIS_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"
# 分红融资页（数据补全 2026-08-25）：zfmx=增发明细 / pgmx=配股明细
_BONUS_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/BonusFinancing/PageAjax"

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 主营构成类型：1=产品 2=行业 3=地区（reports/67 产品裁决）
BREAKDOWN_TYPE_LABELS: dict[int, str] = {1: "产品", 2: "行业", 3: "地区"}

# 兼容代码前缀剥离：SH600519 / 600519.SH / 600519
_CODE_STRIP = re.compile(r"^(SH|SZ|BJ)|\.(SH|SZ|BJ)$", re.IGNORECASE)


def _f10_code(stock_code: str) -> str | None:
    """将裸 6 位代码映射为 F10 的交易所前缀代码（SH/SZ/BJ）。"""
    code = _CODE_STRIP.sub("", (stock_code or "").strip().upper())
    if len(code) != 6 or not code.isdigit():
        return None
    first = code[0]
    if first == "6":
        return "SH" + code
    if first in ("0", "3"):
        return "SZ" + code
    if first in ("4", "8", "9"):
        return "BJ" + code
    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_date_str(value: Any) -> str | None:
    """将东财时间戳（如 '2010-11-11 00:00:00'）归一为 ISO 日期。"""
    if value is None or value == "":
        return None
    s = str(value)[:10]
    try:
        from datetime import date

        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


class EastMoneyF10Adapter(BaseAdapter):
    """东财 F10 适配器（company_profile / business_breakdown / placement_funding）。

    独立限速实例：与其他数据源不共享请求间隔。默认 0.5s/请求，
    保证不高于 2 req/s（reports/68 §6 架构安全门槛）。

    用法::

        from app.core.adapters.eastmoney_f10_adapter import EastMoneyF10Adapter
        from app.core.adapters.base import FetchRequest

        adapter = EastMoneyF10Adapter(rate_limit=0.5)
        result = adapter.fetch(FetchRequest(
            data_type="company_profile", stock_codes=["600519"],
        ))
        # result.metadata.confidence == "approximate"
    """

    def __init__(
        self,
        rate_limit: float = 0.5,
        timeout: float = 15.0,
        session: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            name="eastmoney_f10",  # type: ignore[arg-type]
            supported={"company_profile", "business_breakdown", "placement_funding"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout
        # 注入的 session 由调用方持有；本适配器自建时才负责关闭。
        self._injected_session = session
        self._owned_client: httpx.Client | None = None

    # ─── 协议方法 ──────────────────────────────────────────────────

    def fetch(self, request: FetchRequest) -> FetchResult:
        """按 data_type 派发；均为逐股接口，必须提供 stock_codes。"""
        if request.data_type == "company_profile":
            return self._fetch_survey(request)
        if request.data_type == "business_breakdown":
            return self._fetch_analysis(request)
        if request.data_type == "placement_funding":
            return self._fetch_placement_funding(request)
        return self._missing_result(f"东财 F10 不支持的数据类型: {request.data_type}")

    def _missing_result(self, reason: str) -> FetchResult:
        """合法缺失结果：error=None（区别于源故障），confidence="missing"。

        管理器据此不触发熔断（P1-27：无错误但空数据是合法空结果）；
        调用方（业务概览更新器）据此保留旧值并登记 missing_list。
        """
        logger.info("%s", reason)
        metadata = self._make_metadata(
            raw_response=None, row_count=0, confidence="missing",
        )
        return FetchResult(data=[], metadata=metadata)

    def close(self) -> None:
        """释放自建 HTTP 连接池（不关闭注入的 session）。"""
        if self._owned_client is not None:
            self._owned_client.close()
            self._owned_client = None

    @property
    def client(self) -> httpx.Client | None:
        """返回当前 HTTP 客户端（供测试断言注入是否生效）。"""
        return self._injected_session or self._owned_client

    # ─── HTTP 客户端 ──────────────────────────────────────────────

    def _get_client(self) -> httpx.Client:
        if self._injected_session is not None:
            return self._injected_session
        if self._owned_client is None:
            self._owned_client = httpx.Client(
                timeout=httpx.Timeout(self._timeout),
                # 国内源直连：忽略 HTTP(S)_PROXY（本机代理常指向不可达的
                # 127.0.0.1:10808），与 reports/61 探测方法一致
                trust_env=False,
                headers={
                    "User-Agent": _DEFAULT_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
        return self._owned_client

    # ─── 公司资料（company_profile） ───────────────────────────────

    def _fetch_survey(self, request: FetchRequest) -> FetchResult:
        codes = request.stock_codes
        if not codes:
            return self._missing_result("company_profile 需要股票代码（逐股接口）")
        all_data: list[dict[str, Any]] = []
        raw_parts: list[bytes] = []
        errors: list[str] = []
        for code in codes:
            try:
                rows, raw_bytes = self._query_survey(code)
            except Exception as error:
                logger.exception("查询 %s F10 公司资料异常", code)
                errors.append(f"{code}: {error}")
                continue
            if rows:
                all_data.extend(rows)
                raw_parts.append(raw_bytes)
        if errors:
            return self._make_result(
                data=all_data,
                raw_response=b"".join(raw_parts) or None,
                confidence="approximate",
                error="; ".join(errors),
                api_version="v1",
            )
        if not all_data:
            return self._missing_result(
                "company_profile 无可用数据（空或畸形响应）",
            )
        return self._make_result(
            data=all_data,
            raw_response=b"".join(raw_parts),
            confidence="approximate",
            api_version="v1",
        )

    def _query_survey(self, stock_code: str) -> tuple[list[dict[str, Any]], bytes]:
        """抓取并解析单只股票的公司资料；无数据/畸形响应返回 (None, raw)。"""
        f10 = _f10_code(stock_code)
        if f10 is None:
            raise ValueError(f"无效股票代码: {stock_code}")
        self._wait_rate_limit()
        resp = self._get_client().get(_SURVEY_URL, params={"code": f10})
        resp.raise_for_status()
        raw_bytes = resp.content
        try:
            payload = resp.json()
        except ValueError:
            return [], raw_bytes
        if not isinstance(payload, dict):
            return [], raw_bytes
        jbzl = payload.get("jbzl")
        if not isinstance(jbzl, list) or not jbzl or not isinstance(jbzl[0], dict):
            return [], raw_bytes
        record = jbzl[0]
        profile = record.get("ORG_PROFILE")
        scope = record.get("BUSINESS_SCOPE")
        if not profile and not scope:
            return [], raw_bytes
        code = str(record.get("SECURITY_CODE") or stock_code)
        return [{
            "stock_code": code,
            "code": code,
            "name": record.get("SECURITY_NAME_ABBR"),
            "org_name": record.get("ORG_NAME"),
            "profile": profile,
            "scope": scope,
            "employee_num": _to_int(record.get("EMP_NUM")),
            "csrc_industry": record.get("INDUSTRYCSRC1"),
            "trade_market": record.get("TRADE_MARKET"),
        }], raw_bytes

    # ─── 主营构成（business_breakdown） ────────────────────────────

    def _fetch_analysis(self, request: FetchRequest) -> FetchResult:
        codes = request.stock_codes
        if not codes:
            return self._missing_result("business_breakdown 需要股票代码（逐股接口）")
        all_data: list[dict[str, Any]] = []
        raw_parts: list[bytes] = []
        errors: list[str] = []
        for code in codes:
            try:
                rows, raw_bytes = self._query_analysis(code)
            except Exception as error:
                logger.exception("查询 %s F10 主营构成异常", code)
                errors.append(f"{code}: {error}")
                continue
            if rows:
                all_data.extend(rows)
                raw_parts.append(raw_bytes)
        if errors:
            return self._make_result(
                data=all_data,
                raw_response=b"".join(raw_parts) or None,
                confidence="approximate",
                error="; ".join(errors),
                api_version="v1",
            )
        if not all_data:
            return self._missing_result(
                "business_breakdown 无可用数据（空或畸形响应）",
            )
        return self._make_result(
            data=all_data,
            raw_response=b"".join(raw_parts),
            confidence="approximate",
            api_version="v1",
        )

    def _query_analysis(self, stock_code: str) -> tuple[list[dict[str, Any]], bytes]:
        """抓取并解析单只股票的主营构成；无数据/畸形响应返回 (None, raw)。"""
        f10 = _f10_code(stock_code)
        if f10 is None:
            raise ValueError(f"无效股票代码: {stock_code}")
        self._wait_rate_limit()
        resp = self._get_client().get(_ANALYSIS_URL, params={"code": f10})
        resp.raise_for_status()
        raw_bytes = resp.content
        try:
            payload = resp.json()
        except ValueError:
            return [], raw_bytes
        if not isinstance(payload, dict):
            return [], raw_bytes
        zygcfx = payload.get("zygcfx")
        if not isinstance(zygcfx, list) or not zygcfx:
            return [], raw_bytes
        rows: list[dict[str, Any]] = []
        for entry in zygcfx:
            if not isinstance(entry, dict):
                continue
            item_name = entry.get("ITEM_NAME")
            breakdown_type = _to_int(entry.get("MAINOP_TYPE"))
            if not item_name or breakdown_type not in BREAKDOWN_TYPE_LABELS:
                continue
            report_date = str(entry.get("REPORT_DATE") or "")[:10]
            try:
                date.fromisoformat(report_date)
            except ValueError:
                continue
            ratio_value = _to_float(entry.get("MBI_RATIO"))
            rows.append({
                "stock_code": stock_code,
                "report_date": report_date,
                "type": breakdown_type,
                "item_name": item_name,
                "amount": _to_float(entry.get("MAIN_BUSINESS_INCOME")),
                # MBI_RATIO 为小数占比（如 0.9996），统一转为百分比
                "ratio": ratio_value * 100.0 if ratio_value is not None else None,
                "rank": _to_int(entry.get("RANK")),
            })
        if not rows:
            return None, raw_bytes
        return rows, raw_bytes

    # ─── 增发/配股（placement_funding，数据补全 2026-08-25） ────────

    def _fetch_placement_funding(self, request: FetchRequest) -> FetchResult:
        """抓取单股增发（zfmx）+ 配股（pgmx）融资事件（BonusFinancing 页）。

        - 逐股接口；返回标准化 funding_events 行（event_type=a_placement/rights）。
        - 北交所无东财 F10 交叉源：BJ 代码直接返回合法 missing（no_cross_source:bse），
          不发出请求、不触发熔断（reports/75 纪律延续）。
        - 增发 TOTAL_RAISE_FUNDS 常为 null：以 ISSUE_NUM×ISSUE_PRICE 推算并
          derived=true 如实标注；任一缺失则 raise_funds 保持 null 不伪造。
        """
        codes = request.stock_codes
        if not codes:
            return self._missing_result("placement_funding 需要股票代码（逐股接口）")
        all_data: list[dict[str, Any]] = []
        raw_parts: list[bytes] = []
        errors: list[str] = []
        for code in codes:
            f10 = _f10_code(code)
            if f10 is None:
                errors.append(f"{code}: 无效股票代码")
                continue
            if f10.startswith("BJ"):
                # 北交所无东财 F10 交叉源，如实记录合法缺失
                self._wait_rate_limit()
                logger.info("placement_funding %s: 北交所无东财交叉源，跳过", code)
                continue
            try:
                rows, raw_bytes = self._query_bonus_financing(code, f10)
            except Exception as error:
                logger.exception("查询 %s F10 增发配股异常", code)
                errors.append(f"{code}: {error}")
                continue
            if rows:
                all_data.extend(rows)
                raw_parts.append(raw_bytes)
        if errors:
            return self._make_result(
                data=all_data,
                raw_response=b"".join(raw_parts) or None,
                confidence="approximate",
                error="; ".join(errors),
                api_version="v1",
            )
        if not all_data:
            return self._missing_result("placement_funding 无可用数据（空或畸形响应）")
        return self._make_result(
            data=all_data,
            raw_response=b"".join(raw_parts),
            confidence="approximate",
            api_version="v1",
        )

    def _query_bonus_financing(
        self, stock_code: str, f10: str,
    ) -> tuple[list[dict[str, Any]], bytes]:
        """抓取并解析单只股票的增发/配股事件；无数据返回 (None, raw)。"""
        self._wait_rate_limit()
        resp = self._get_client().get(_BONUS_URL, params={"code": f10})
        resp.raise_for_status()
        raw_bytes = resp.content
        try:
            payload = resp.json()
        except ValueError:
            return [], raw_bytes
        if not isinstance(payload, dict):
            return [], raw_bytes

        rows: list[dict[str, Any]] = []
        for entry in payload.get("zfmx") or []:
            if not isinstance(entry, dict):
                continue
            shares = _to_float(entry.get("ISSUE_NUM"))
            price = _to_float(entry.get("ISSUE_PRICE"))
            funds = _to_float(entry.get("TOTAL_RAISE_FUNDS"))
            derived = False
            if funds is None and shares is not None and price is not None:
                funds = shares * price
                derived = True
            rows.append({
                "stock_code": stock_code,
                "event_type": "a_placement",
                "announce_date": _to_date_str(entry.get("NOTICE_DATE")),
                "list_date": _to_date_str(entry.get("NOTICE_DATE")),
                "issue_price": price,
                "issue_shares": shares,
                "raise_funds": funds,
                "raise_funds_net": None,
                "derived": derived,
                "extra": {"event_explain": entry.get("EVENT_EXPLAIN")},
            })
        for entry in payload.get("pgmx") or []:
            if not isinstance(entry, dict):
                continue
            rows.append({
                "stock_code": stock_code,
                "event_type": "rights",
                "announce_date": _to_date_str(entry.get("NOTICE_DATE")),
                "list_date": _to_date_str(entry.get("EX_DIVIDEND_DATEE")),
                "issue_price": _to_float(entry.get("ISSUE_PRICE")),
                "issue_shares": _to_float(entry.get("ISSUE_NUM")),
                "raise_funds": _to_float(entry.get("TOTAL_RAISE_FUNDS")),
                "raise_funds_net": None,
                "derived": False,
                "extra": {"event_explain": entry.get("EVENT_EXPLAIN")},
            })
        if not rows:
            return [], raw_bytes
        return rows, raw_bytes
