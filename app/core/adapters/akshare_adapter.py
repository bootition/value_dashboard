"""AKShare 适配器 — 封装 akshare (Eastmoney) 数据源

支持的数据类型:
  - stock_list        全市场 A 股股票列表 (SSE/SZSE/BSE)
  - listing_info      上市信息 (上市日期/ST/停牌/拼音)
  - price_daily       日线行情 (raw/qfq/hfq)
  - balance_sheet     资产负债表
  - income_statement  利润表
  - cash_flow         现金流量表
  - dividends         分红记录 (CNINFO)
  - trading_dates     交易日历

符号格式约定:
  - stock_list / listing_info / trading_dates  — 不需要 stock_code
  - price_daily                                — 纯代码 "600519" (无前缀)
  - balance_sheet / income_statement / cash_flow — 带前缀 "SH600519"
  - dividends                                  — 纯代码 "600519" (CNINFO 格式)
"""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Callable
from typing import Any

import pandas as pd

from app.core.adapters.base import (
    BaseAdapter,
    DataType,
    FetchRequest,
    FetchResult,
)

logger = logging.getLogger(__name__)

# ─── 可选依赖: akshare / pypinyin ────────────────────────────────────

try:
    import akshare as ak

    _AKSHARE_AVAILABLE: bool = True
    _AKSHARE_VERSION: str | None = getattr(ak, "__version__", "unknown")
except ImportError:  # pragma: no cover
    ak = None  # type: ignore[assignment]
    _AKSHARE_AVAILABLE = False
    _AKSHARE_VERSION = None

try:
    from pypinyin import lazy_pinyin

    _PYPINYIN_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    lazy_pinyin = None  # type: ignore[assignment]
    _PYPINYIN_AVAILABLE = False


# ─── 中文字段 → 英文标准字段映射 ─────────────────────────────────────

_PRICE_DAILY_FIELD_MAP: dict[str, str] = {
    "日期": "trade_date",
    "股票代码": "stock_code",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "turnover",
    "振幅": "amplitude",
    "涨跌幅": "pct_change",
    "涨跌额": "change",
    "换手率": "turnover_rate",
}

_DIVIDEND_FIELD_MAP: dict[str, str] = {
    "实施方案公告日期": "announcement_date",
    "分红类型": "dividend_type",
    "送股比例": "bonus_share_ratio",
    "转增比例": "capitalization_ratio",
    "派息比例": "cash_dividend_ratio",
    "股权登记日": "record_date",
    "除权日": "ex_date",
    "派息日": "pay_date",
    "股份到账日": "share_arrival_date",
    "实施方案分红说明": "description",
    "报告时间": "report_period",
}

# 财报接口 (stock_balance_sheet_by_report_em 等) 已返回英文字段名，
# 无需映射；仅补充 stock_code / 规范化 REPORT_DATE 时间部分。


# ─── 辅助函数 ───────────────────────────────────────────────────────


def _infer_exchange(code: str) -> str:
    """从 6 位股票代码推断交易所: SSE / SZSE / BSE"""
    code = code.strip()
    if not code:
        return ""
    first = code[0]
    if first == "6":
        return "SSE"
    if first in ("0", "3"):
        return "SZSE"
    if first in ("4", "8", "9"):
        return "BSE"
    return ""


def _to_em_symbol(code: str) -> str:
    """转换为 Eastmoney 财报接口所需的带前缀代码

    接受 "600519" / "SH600519" / "600519.SH" → "SH600519"
    """
    code = code.strip().upper()
    if code.startswith(("SH", "SZ", "BJ")):
        return code
    if "." in code:
        base, suffix = code.split(".", 1)
        prefix = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}.get(suffix.upper())
        if prefix:
            return prefix + base
    exchange = _infer_exchange(code)
    prefix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(exchange, "")
    return prefix + code if prefix else code


def _strip_code(code: str) -> str:
    """去除交易所前缀/后缀，返回纯 6 位代码

    "SH600519" / "600519.SH" / "600519" → "600519"
    """
    code = code.strip().upper()
    if code.startswith(("SH", "SZ", "BJ")):
        return code[2:]
    if "." in code:
        return code.split(".")[0]
    return code


def _clean_value(v: Any) -> Any:
    """清洗单个值: NaN/NaT → None, Timestamp/date → 'YYYY-MM-DD'"""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, datetime.date):
        return v.strftime("%Y-%m-%d")
    # Strip time from datetime strings like "2026-03-31 00:00:00"
    if isinstance(v, str) and len(v) >= 19 and v[10] == " ":
        return v[:10]
    # Convert numpy scalar → Python native
    if hasattr(v, "item"):
        try:
            return v.item()
        except (AttributeError, ValueError):
            pass
    return v


def _share_count(value: Any) -> int | None:
    """Normalize exchange-list share counts without inventing a missing value."""
    clean = _clean_value(value)
    if clean is None:
        return None
    try:
        return int(float(clean))
    except (TypeError, ValueError):
        return None


def _df_to_records(
    df: pd.DataFrame | None,
    field_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """DataFrame → list[dict]: NaN→None, 可选字段重命名"""
    if df is None or len(df) == 0:
        return []
    if field_map:
        df = df.rename(columns=field_map)
    records: list[dict[str, Any]] = []
    for rec in df.to_dict("records"):
        records.append({k: _clean_value(v) for k, v in rec.items()})
    return records


def _generate_pinyin(name: str) -> str:
    """生成无声调全拼: '平安银行' → 'pinganyinhang'"""
    if not _PYPINYIN_AVAILABLE or not name:
        return ""
    try:
        return "".join(lazy_pinyin(name))
    except Exception:
        return ""


# ─── 适配器 ─────────────────────────────────────────────────────────


class AKShareAdapter(BaseAdapter):
    """AKShare (Eastmoney) 数据源适配器

    封装 akshare 库获取 A 股行情、财报、分红、交易日历等数据。
    构造无需参数，rate_limit 默认 0.5s (config/default.yaml)。
    """

    _SUPPORTED: set[DataType] = {
        "stock_list",
        "listing_info",
        "price_daily",
        "balance_sheet",
        "income_statement",
        "cash_flow",
        "dividends",
        "trading_dates",
    }

    def __init__(self, rate_limit: float = 0.5) -> None:
        super().__init__(
            name="akshare_eastmoney",
            supported=self._SUPPORTED,
            rate_limit=rate_limit,
        )

    # ─── dispatch ───────────────────────────────────────────────

    def fetch(self, request: FetchRequest) -> FetchResult:
        if not _AKSHARE_AVAILABLE:
            return self._make_empty_result("akshare 未安装")

        handler: Callable[[FetchRequest], FetchResult] | None = {
            "stock_list": self._fetch_stock_list,
            "listing_info": self._fetch_listing_info,
            "price_daily": self._fetch_price_daily,
            "balance_sheet": self._fetch_balance_sheet,
            "income_statement": self._fetch_income_statement,
            "cash_flow": self._fetch_cash_flow,
            "dividends": self._fetch_dividends,
            "trading_dates": self._fetch_trading_dates,
        }.get(request.data_type)

        if handler is None:
            return self._make_empty_result(f"不支持的数据类型: {request.data_type}")

        try:
            return handler(request)
        except Exception as e:
            logger.exception(f"AKShare fetch {request.data_type} 失败")
            return self._make_empty_result(f"{type(e).__name__}: {e}")

    # ─── handlers ───────────────────────────────────────────────

    def _fetch_stock_list(self, request: FetchRequest) -> FetchResult:
        """全市场股票列表: SSE + SZSE (stock_info_a_code_name) + BSE (stock_info_bj_name_code)

        P1-B修复: 任一板块抓取失败时如实报告 partial（error + approximate），
        禁止把部分列表伪装成 strict 成功——否则调用方会据此把缺失股票
        静默标记为退市。
        """
        records: list[dict[str, Any]] = []
        source_errors: list[str] = []

        # SSE + SZSE
        try:
            self._wait_rate_limit()
            df = ak.stock_info_a_code_name()
            for _, row in df.iterrows():
                code = str(row["code"]).strip()
                records.append(
                    {
                        "stock_code": code,
                        "name": str(row["name"]).strip(),
                        "exchange": _infer_exchange(code),
                    }
                )
        except Exception as e:
            logger.warning(f"stock_info_a_code_name 失败: {e}")
            source_errors.append(f"sse_szse: {e}")

        # BSE
        try:
            self._wait_rate_limit()
            df_bj = ak.stock_info_bj_name_code()
            for _, row in df_bj.iterrows():
                code = str(row["证券代码"]).strip()
                records.append(
                    {
                        "stock_code": code,
                        "name": str(row["证券简称"]).strip(),
                        "exchange": "BSE",
                    }
                )
        except Exception as e:
            logger.warning(f"stock_info_bj_name_code 失败: {e}")
            source_errors.append(f"bse: {e}")

        if not records:
            return self._make_empty_result("无法获取股票列表")

        if source_errors:
            return self._make_result(
                data=records,
                confidence="approximate",
                error="partial stock list: " + "; ".join(source_errors),
                api_version=_AKSHARE_VERSION,
            )

        return self._make_result(
            data=records,
            confidence="strict",
            api_version=_AKSHARE_VERSION,
        )

    def _fetch_listing_info(self, request: FetchRequest) -> FetchResult:
        """Fetch pool metadata from exchange lists and a dated suspension list.

        An unavailable source leaves its field unknown. It must never turn an
        absent name into a claim that a stock is not ST or not suspended.
        """
        exchange_rows: dict[str, dict[str, Any]] = {}

        def load_exchange_list(
            fetch: Callable[[], pd.DataFrame],
            code_column: str,
            name_column: str,
            date_column: str,
            total_shares_column: str | None = None,
            circ_shares_column: str | None = None,
        ) -> None:
            try:
                self._wait_rate_limit()
                for _, row in fetch().iterrows():
                    code = _strip_code(str(row.get(code_column, "")).strip()).zfill(6)
                    if not code or code == "000000":
                        continue
                    exchange_rows[code] = {
                        "name": str(row.get(name_column, "")).strip(),
                        "listing_date": _clean_value(row.get(date_column)),
                        "total_shares": _share_count(row.get(total_shares_column)) if total_shares_column else None,
                        "circ_shares": _share_count(row.get(circ_shares_column)) if circ_shares_column else None,
                    }
            except Exception as error:
                logger.warning("exchange listing source failed: %s", error)

        load_exchange_list(
            lambda: ak.stock_info_sh_name_code(symbol="主板A股"),
            "证券代码", "证券简称", "上市日期", "总股本", "流通股本",
        )
        load_exchange_list(
            lambda: ak.stock_info_sh_name_code(symbol="科创板"),
            "证券代码", "证券简称", "上市日期", "总股本", "流通股本",
        )
        load_exchange_list(
            lambda: ak.stock_info_sz_name_code(symbol="A股列表"),
            "A股代码", "A股简称", "A股上市日期", "A股总股本", "A股流通股本",
        )
        load_exchange_list(
            ak.stock_info_bj_name_code,
            "证券代码", "证券简称", "上市日期", "总股本", "流通股本",
        )

        suspension_codes: set[str] | None = None
        try:
            self._wait_rate_limit()
            frame = ak.stock_tfp_em(date=datetime.date.today().strftime("%Y%m%d"))
            suspension_codes = {
                _strip_code(str(value).strip()).zfill(6)
                for value in frame.get("代码", pd.Series(dtype="object"))
                if str(value).strip()
            }
        except Exception as error:
            logger.warning("suspension source failed: %s", error)

        targets = (
            [_strip_code(code).zfill(6) for code in request.stock_codes]
            if request.stock_codes
            else sorted(exchange_rows)
        )
        if not targets:
            return self._make_empty_result("无法获取交易所股票清单")

        records: list[dict[str, Any]] = []
        for code in targets:
            row = exchange_rows.get(code)
            name = row["name"] if row else ""
            records.append(
                {
                    "stock_code": code,
                    "name": name,
                    "listing_date": row["listing_date"] if row else None,
                    "is_st": name.upper().startswith(("ST", "*ST")) if row else None,
                    "is_suspended": code in suspension_codes if suspension_codes is not None else None,
                    "pinyin": _generate_pinyin(name),
                    "total_shares": row["total_shares"] if row else None,
                    "circ_shares": row["circ_shares"] if row else None,
                }
            )

        if not records:
            return self._make_empty_result("无法获取上市信息")

        return self._make_result(
            data=records,
            confidence="approximate",
            api_version=_AKSHARE_VERSION,
        )

    def _fetch_price_daily(self, request: FetchRequest) -> FetchResult:
        """日线行情 (raw/qfq/hfq)"""
        if not request.stock_codes:
            return self._make_empty_result("price_daily 需要 stock_codes")

        # 日期格式转换: YYYY-MM-DD → YYYYMMDD
        start = (request.start_date or "").replace("-", "")
        end = (request.end_date or "").replace("-", "")

        # adjust 映射: raw→"" / qfq→"qfq" / hfq→"hfq"
        adjust_map: dict[str, str] = {"raw": "", "qfq": "qfq", "hfq": "hfq"}
        adjust = adjust_map.get(request.adjust, "")

        all_records: list[dict[str, Any]] = []
        raw_payloads: list[str] = []
        for code in request.stock_codes:
            plain_code = _strip_code(code)
            try:
                self._wait_rate_limit()
                kwargs: dict[str, Any] = {
                    "symbol": plain_code,
                    "period": "daily",
                    "adjust": adjust,
                }
                if start:
                    kwargs["start_date"] = start
                if end:
                    kwargs["end_date"] = end
                df = ak.stock_zh_a_hist(**kwargs)
                raw_payloads.append(df.to_json(orient="records", date_format="iso", force_ascii=False))
                all_records.extend(_df_to_records(df, _PRICE_DAILY_FIELD_MAP))
            except Exception as e:
                logger.warning(f"stock_zh_a_hist({plain_code}) 失败: {e}")

        if not all_records:
            return self._make_empty_result("无法获取日线行情")

        return self._make_result(
            data=all_records,
            raw_response="\n".join(raw_payloads),
            confidence="strict",
            api_version=_AKSHARE_VERSION,
        )

    def _fetch_balance_sheet(self, request: FetchRequest) -> FetchResult:
        """资产负债表 — symbol 需带交易所前缀 (SH600519)"""
        return self._fetch_financial_statement(
            request=request,
            api_func=ak.stock_balance_sheet_by_report_em,
            data_type_name="balance_sheet",
        )

    def _fetch_income_statement(self, request: FetchRequest) -> FetchResult:
        """利润表 — symbol 需带交易所前缀 (SH600519)"""
        return self._fetch_financial_statement(
            request=request,
            api_func=ak.stock_profit_sheet_by_report_em,
            data_type_name="income_statement",
        )

    def _fetch_cash_flow(self, request: FetchRequest) -> FetchResult:
        """现金流量表 — symbol 需带交易所前缀 (SH600519)"""
        return self._fetch_financial_statement(
            request=request,
            api_func=ak.stock_cash_flow_sheet_by_report_em,
            data_type_name="cash_flow",
        )

    def _fetch_financial_statement(
        self,
        request: FetchRequest,
        api_func: Callable[..., pd.DataFrame],
        data_type_name: str,
    ) -> FetchResult:
        """通用财报抓取 (三大报表共用)

        akshare 财报接口已返回英文字段名 (TOTAL_ASSETS 等)，
        无需中英文映射；仅补充 stock_code 并规范化 REPORT_DATE。
        """
        if not request.stock_codes:
            return self._make_empty_result(f"{data_type_name} 需要 stock_codes")

        all_records: list[dict[str, Any]] = []
        raw_payloads: list[str] = []
        for code in request.stock_codes:
            em_symbol = _to_em_symbol(code)
            plain_code = _strip_code(code)
            try:
                self._wait_rate_limit()
                df = api_func(symbol=em_symbol)
                raw_payloads.append(df.to_json(orient="records", date_format="iso", force_ascii=False))
                records = _df_to_records(df)
                for rec in records:
                    # 确保有 stock_code
                    if "stock_code" not in rec:
                        rec["stock_code"] = plain_code
                    # 规范化 REPORT_DATE: "2026-03-31 00:00:00" → "2026-03-31"
                    rd = rec.get("REPORT_DATE")
                    if isinstance(rd, str) and len(rd) >= 19 and rd[10] == " ":
                        rec["REPORT_DATE"] = rd[:10]
                all_records.extend(records)
            except Exception as e:
                logger.warning(f"{data_type_name}({em_symbol}) 失败: {e}")

        if not all_records:
            return self._make_empty_result(f"无法获取{data_type_name}数据")

        return self._make_result(
            data=all_records,
            raw_response="\n".join(raw_payloads),
            confidence="strict",
            api_version=_AKSHARE_VERSION,
        )

    def _fetch_dividends(self, request: FetchRequest) -> FetchResult:
        """分红记录 (CNINFO) — symbol 为纯代码 (600519)

        akshare 的 stock_dividend_cninfo 返回"每10股"口径的 ratio 字段
        （cash_dividend_ratio/bonus_share_ratio/capitalization_ratio），
        必须归一化为标准"每股"字段（dividend_per_share 等），否则正式库
        dividends 表的数值字段会全部为空。
        """
        if not request.stock_codes:
            return self._make_empty_result("dividends 需要 stock_codes")

        all_records: list[dict[str, Any]] = []
        raw_payloads: list[str] = []
        for code in request.stock_codes:
            plain_code = _strip_code(code)
            try:
                self._wait_rate_limit()
                df = ak.stock_dividend_cninfo(symbol=plain_code)
                raw_payloads.append(df.to_json(orient="records", date_format="iso", force_ascii=False))
                records = _df_to_records(df, _DIVIDEND_FIELD_MAP)
                for rec in records:
                    rec["stock_code"] = plain_code
                    self._normalize_dividend_fields(rec)
                all_records.extend(records)
            except Exception as e:
                logger.warning(f"stock_dividend_cninfo({plain_code}) 失败: {e}")

        if not all_records:
            return self._make_empty_result("无法获取分红记录")

        return self._make_result(
            data=all_records,
            raw_response="\n".join(raw_payloads),
            confidence="strict",
            api_version=_AKSHARE_VERSION,
        )

    _DIVIDEND_CASH_RE = re.compile(r"派\s*([\d.]+)\s*元")

    @classmethod
    def _normalize_dividend_fields(cls, rec: dict[str, Any]) -> None:
        """将 akshare "每10股" ratio 口径归一化为标准 "每股" 字段。

        - cash_dividend_ratio（每10股派X元）→ dividend_per_share（每股X元）
        - bonus_share_ratio（每10股送X股）→ stock_dividend（每股X股）
        - capitalization_ratio（每10股转增X股）→ transfer_share（每股X股）
        - akshare 接口不含配股 → rights_issue 保持 None

        已退市 B 股等记录中 cash_dividend_ratio 常为 NaN，但 description
        （如"10派2.90元(含税)"）包含每10股现金派发额——从文本解析补全，
        否则有分红事实的记录会缺失每股派息数值。
        """
        try:
            cash = rec.get("cash_dividend_ratio")
            if cash is not None and not (isinstance(cash, float) and cash != cash):
                rec["dividend_per_share"] = float(cash) / 10.0
            else:
                description = rec.get("description") or ""
                match = cls._DIVIDEND_CASH_RE.search(description)
                if match:
                    rec["dividend_per_share"] = float(match.group(1)) / 10.0
            bonus = rec.get("bonus_share_ratio")
            if bonus is not None and not (isinstance(bonus, float) and bonus != bonus):
                rec["stock_dividend"] = float(bonus) / 10.0
            cap = rec.get("capitalization_ratio")
            if cap is not None and not (isinstance(cap, float) and cap != cap):
                rec["transfer_share"] = float(cap) / 10.0
        except (TypeError, ValueError):
            pass
        rec["rights_issue"] = None
        rec["rights_issue_price"] = None

    def _fetch_trading_dates(self, request: FetchRequest) -> FetchResult:
        """交易日历 (Sina)"""
        try:
            self._wait_rate_limit()
            df = ak.tool_trade_date_hist_sina()
            raw_payload = df.to_json(orient="records", date_format="iso", force_ascii=False)
            records = _df_to_records(df)
            if not records:
                return self._make_empty_result("交易日历为空")
            return self._make_result(
                data=records,
                raw_response=raw_payload,
                confidence="strict",
                api_version=_AKSHARE_VERSION,
            )
        except Exception as e:
            return self._make_empty_result(f"tool_trade_date_hist_sina 失败: {e}")
