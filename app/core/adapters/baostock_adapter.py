"""BaoStock 数据源适配器

BaoStock 是基于 socket 协议的免费 A 股数据服务（baostock.com:10030），
零反爬风险，作为价格数据的补充/回退数据源。

支持的数据类型：
- price_daily: 日线行情（raw/qfq/hfq）
- dividends: 分红送股记录
- trading_dates: 交易日历

限制：
- 不支持北交所（BSE），仅 sh/sz 前缀
- 不提供完整财务报表（仅有财务比率），故不实现 balance_sheet 等
- query_history_k_data_plus 需指定单只代码，不支持全市场批量
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

try:
    import baostock as bs

    # ─── monkeypatch 1: get_data() pandas 2.0 兼容 ─────────────────
    # baostock 0.9.3 用了 pd.DataFrame.append() (pandas 2.0 已移除)
    import pandas as _pd
    import baostock.data.resultset as _bs_rs

    def _patched_get_data(self):
        if len(self.data) == 0:
            return _pd.DataFrame()
        df = _pd.DataFrame(self.data, columns=self.fields)
        self.cur_row_num = len(self.data)
        while (self.error_code == '0') and self.next():
            temp_df = _pd.DataFrame(self.data, columns=self.fields)
            df = _pd.concat([df, temp_df], ignore_index=True)
            self.cur_row_num = len(self.data)
        return df

    _bs_rs.ResultData.get_data = _patched_get_data

    # ─── monkeypatch 2: socket 超时 ────────────────────────────────
    # baostock socket 默认无超时, recv() 会永久阻塞
    # 在 login 后给 default_socket 设 settimeout
    import baostock.common.context as _bs_ctx

    _SOCKET_TIMEOUT = 30

    _original_login = bs.login

    def _patched_login(*args, **kwargs):
        result = _original_login(*args, **kwargs)
        _s = getattr(_bs_ctx, "default_socket", None)
        if _s is not None:
            _s.settimeout(_SOCKET_TIMEOUT)
        return result

    bs.login = _patched_login

    _BAOSTOCK_AVAILABLE = True
except ImportError:  # baostock 是可选依赖（pyproject [data-sources]）
    bs = None  # type: ignore[assignment]
    _BAOSTOCK_AVAILABLE = False

logger = logging.getLogger(__name__)

# 用于溯源元数据的 API 版本标识
_API_VERSION = "baostock-0.9.3"

# adjustflag 映射：3=不复权(raw), 2=前复权(qfq), 1=后复权(hfq)
_ADJUST_FLAG: dict[str, str] = {"raw": "3", "qfq": "2", "hfq": "1"}

# price_daily 查询字段（顺序即 get_row_data() 返回顺序）
_PRICE_FIELDS = "date,open,high,low,close,volume,amount,turn"

# A 股市场起点年份（用于 dividends 缺省起始年）
_A_SHARE_EPOCH_YEAR = 1990


# ─── 工具函数 ──────────────────────────────────────────────────────


def _to_baostock_code(stock_code: str) -> str | None:
    """将标准股票代码转换为 BaoStock 代码格式。

    规则：
    - 6 开头 → sh.XXXXXX（上交所）
    - 0/3 开头 → sz.XXXXXX（深交所）
    - 8/4 开头 → 北交所，BaoStock 不支持，返回 None
    """
    code = _normalize_stock_code(stock_code)
    if not code or not code[0].isdigit():
        return None
    first = code[0]
    if first == "6":
        return f"sh.{code}"
    if first in ("0", "3"):
        return f"sz.{code}"
    # 8/4 → BSE
    return None


def _normalize_stock_code(stock_code: str) -> str:
    """标准化股票代码为无前缀的 6 位数字形式。

    P0#4修复: 原 lstrip("shSHzzSZ") 会删除所有属于 {s,h,S,H,z,Z} 的字符,
    导致 "600519.SH" → split → "SH" → lstrip → "" (空字符串)。
    改为用正则提取 6 位数字代码。
    """
    import re
    code = stock_code.strip()
    # 提取 6 位数字代码 (支持 sh.600519 / 600519.SH / sz000001 等格式)
    match = re.search(r"(\d{6})", code)
    if match:
        return match.group(1)
    # 无 6 位数字时, 清理已知前缀
    if "." in code:
        code = code.split(".")[-1]
    for prefix in ("sh", "sz", "SH", "SZ", "bj", "BJ"):
        if code.startswith(prefix):
            code = code[len(prefix):]
            break
    return code


def _parse_float(value: str | None) -> float | None:
    """BaoStock 返回值均为字符串，空串转为 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_date(value: str | None) -> str | None:
    """日期字符串原样返回（BaoStock 已为 YYYY-MM-DD），空串转 None。"""
    if value is None or value == "":
        return None
    return value


# ─── 适配器 ────────────────────────────────────────────────────────


class BaoStockAdapter(BaseAdapter):
    """BaoStock 适配器：socket 协议、零反爬、价格回退数据源。"""

    def __init__(self, rate_limit: float = 0.1, *, reuse_session: bool = False) -> None:
        super().__init__(
            name="baostock",
            supported={"price_daily", "dividends", "trading_dates"},
            rate_limit=rate_limit,
        )
        # Long-running bulk repair keeps one authenticated socket open across
        # many fetch() calls instead of login/logout per fetch (each login is
        # a full socket handshake; 5,534 stocks x 2 adjust modes would add
        # thousands of redundant logins). Close explicitly via .close().
        self.reuse_session = reuse_session
        self._logged_in = False

    # ─── 调度入口 ──────────────────────────────────────────────────

    def fetch(self, request: FetchRequest) -> FetchResult:
        if not _BAOSTOCK_AVAILABLE:
            return self._make_empty_result(
                reason="baostock 未安装（pip install baostock）",
                confidence="missing",
            )

        if not self.can_handle(request):
            return self._make_empty_result(
                reason=f"baostock 不支持数据类型: {request.data_type}",
                confidence="missing",
            )

        handler = {
            "price_daily": self._fetch_price_daily,
            "dividends": self._fetch_dividends,
            "trading_dates": self._fetch_trading_dates,
        }[request.data_type]

        try:
            return handler(request)
        except Exception as exc:
            logger.exception("baostock fetch 失败: %s", request.data_type)
            return self._make_empty_result(
                reason=f"baostock {request.data_type} 抓取异常: {exc}",
                confidence="missing",
            )

    # ─── price_daily ───────────────────────────────────────────────

    def _fetch_price_daily(self, request: FetchRequest) -> FetchResult:
        if not request.stock_codes:
            return self._make_empty_result(
                reason="baostock price_daily 需指定 stock_codes（不支持全市场批量）",
                confidence="missing",
            )

        adjustflag = _ADJUST_FLAG.get(request.adjust, "3")
        start_date = request.start_date or "2010-01-01"
        end_date = request.end_date or datetime.now().strftime("%Y-%m-%d")

        records: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        skipped_bse: list[str] = []

        with self._session():
            for raw_code in request.stock_codes:
                bs_code = _to_baostock_code(raw_code)
                if bs_code is None:
                    skipped_bse.append(_normalize_stock_code(raw_code))
                    logger.debug("跳过 BSE 代码（baostock 不支持）: %s", raw_code)
                    continue

                self._wait_rate_limit()
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    _PRICE_FIELDS,
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag=adjustflag,
                )

                if rs.error_code != "0" and self.reuse_session and self._is_not_logged_in(rs.error_msg):
                    self._reconnect()
                    self._wait_rate_limit()
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        _PRICE_FIELDS,
                        start_date=start_date,
                        end_date=end_date,
                        frequency="d",
                        adjustflag=adjustflag,
                    )

                if rs.error_code != "0":
                    msg = f"query_history_k_data_plus({bs_code}) 失败: {rs.error_msg}"
                    logger.warning(msg)
                    raw_lines.append(f"ERROR {bs_code}: {rs.error_msg}")
                    continue

                stock_code_norm = _normalize_stock_code(raw_code)
                count = 0
                while rs.next():
                    row = rs.get_row_data()
                    # row 顺序对应 _PRICE_FIELDS:
                    # date,open,high,low,close,volume,amount,turn
                    records.append(
                        {
                            "stock_code": stock_code_norm,
                            "trade_date": _parse_date(row[0]),
                            "open": _parse_float(row[1]),
                            "high": _parse_float(row[2]),
                            "low": _parse_float(row[3]),
                            "close": _parse_float(row[4]),
                            "volume": _parse_float(row[5]),
                            "turnover": _parse_float(row[6]),  # amount → 成交额
                            "turnover_rate": _parse_float(row[7]),  # 换手率 (V2-3.5修复: turn→turnover_rate 统一字段名)
                        }
                    )
                    count += 1

                raw_lines.append(
                    json.dumps(records[-count:], ensure_ascii=False, sort_keys=True, default=str)
                )
                logger.debug("baostock price_daily %s 取得 %d 条", bs_code, count)

        error = (
            f"跳过 BSE 代码（baostock 不支持）: {','.join(skipped_bse)}"
            if skipped_bse
            else None
        )
        return self._make_result(
            data=records,
            raw_response="\n".join(raw_lines).encode("utf-8"),
            confidence="approximate",
            error=error,
            api_version=_API_VERSION,
        )

    # ─── dividends ─────────────────────────────────────────────────

    def _fetch_dividends(self, request: FetchRequest) -> FetchResult:
        if not request.stock_codes:
            return self._make_empty_result(
                reason="baostock dividends 需指定 stock_codes",
                confidence="missing",
            )

        current_year = datetime.now().year
        start_year = (
            int(request.start_date[:4]) if request.start_date else _A_SHARE_EPOCH_YEAR
        )

        records: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        skipped_bse: list[str] = []

        with self._session():
            for raw_code in request.stock_codes:
                bs_code = _to_baostock_code(raw_code)
                if bs_code is None:
                    skipped_bse.append(_normalize_stock_code(raw_code))
                    logger.debug("跳过 BSE 代码（baostock 不支持）: %s", raw_code)
                    continue

                stock_code_norm = _normalize_stock_code(raw_code)
                count_for_code = 0

                for year in range(start_year, current_year + 1):
                    self._wait_rate_limit()
                    rs = bs.query_dividend_data(
                        bs_code,
                        year=year,
                        yearType="report",
                    )

                    if rs.error_code != "0" and self.reuse_session and self._is_not_logged_in(rs.error_msg):
                        self._reconnect()
                        self._wait_rate_limit()
                        rs = bs.query_dividend_data(
                            bs_code,
                            year=year,
                            yearType="report",
                        )

                    if rs.error_code != "0":
                        logger.warning(
                            "query_dividend_data(%s, %d) 失败: %s",
                            bs_code,
                            year,
                            rs.error_msg,
                        )
                        continue

                    fields = list(rs.fields) if rs.fields else []
                    while rs.next():
                        row = rs.get_row_data()
                        record = self._parse_dividend_row(
                            row, fields, stock_code_norm
                        )
                        if record is not None:
                            records.append(record)
                            count_for_code += 1

                raw_lines.append(
                    json.dumps(records[-count_for_code:], ensure_ascii=False, sort_keys=True, default=str)
                )
                logger.debug(
                    "baostock dividends %s 取得 %d 条", bs_code, count_for_code
                )

        error = (
            f"跳过 BSE 代码（baostock 不支持）: {','.join(skipped_bse)}"
            if skipped_bse
            else None
        )
        return self._make_result(
            data=records,
            raw_response="\n".join(raw_lines).encode("utf-8"),
            confidence="approximate",
            error=error,
            api_version=_API_VERSION,
        )

    @staticmethod
    def _parse_dividend_row(
        row: list[str], fields: list[str], stock_code: str
    ) -> dict[str, Any] | None:
        """解析 BaoStock 分红记录行（按字段名定位，不依赖顺序）。

        query_dividend_data(yearType='report') 实际返回字段：
            code, dividPreNoticeDate, dividAgmPumDate, dividPlanAnnounceDate,
            dividPlanDate, dividRegistDate, dividOperateDate, dividPayDate,
            dividStockMarketDate, dividCashPsBeforeTax, dividCashPsAfterTax,
            dividStocksPs, dividCashStock, dividReserveToStockPs

        映射到 schema：
            dividOperateDate        → ex_date（除权除息日）
            dividPlanAnnounceDate   → announcement_date（方案公告日）
            dividCashPsBeforeTax    → dividend_per_share（每股股息税前）
            dividStocksPs           → stock_dividend（每股送股）
            dividReserveToStockPs   → transfer_share（每股转增）

        注：baostock dividend 接口不含配股（rights_issue）数据，置 None。
        """
        if not row or not fields:
            return None

        idx = {name: i for i, name in enumerate(fields)}

        def _get(field_name: str) -> str | None:
            i = idx.get(field_name)
            return row[i] if i is not None and i < len(row) else None

        ex_date = _parse_date(_get("dividOperateDate"))
        if ex_date is None:
            # 无除权除息日（如仅预案未实施）跳过，避免 PK 冲突
            return None

        return {
            "stock_code": stock_code,
            "ex_date": ex_date,
            "announcement_date": _parse_date(_get("dividPlanAnnounceDate")),
            "dividend_per_share": _parse_float(_get("dividCashPsBeforeTax")),
            "stock_dividend": _parse_float(_get("dividStocksPs")),
            "transfer_share": _parse_float(_get("dividReserveToStockPs")),
            "rights_issue": None,  # baostock dividend 接口不含配股
            "rights_issue_price": None,
        }

    # ─── trading_dates ─────────────────────────────────────────────

    def _fetch_trading_dates(self, request: FetchRequest) -> FetchResult:
        start_date = request.start_date or f"{datetime.now().year}-01-01"
        end_date = request.end_date or datetime.now().strftime("%Y-%m-%d")

        records: list[dict[str, Any]] = []

        with self._session():
            self._wait_rate_limit()
            rs = bs.query_trade_dates(start_date=start_date, end_date=end_date)

            if rs.error_code != "0":
                msg = f"query_trade_dates 失败: {rs.error_msg}"
                logger.warning(msg)
                return self._make_empty_result(
                    reason=msg, confidence="missing"
                )

            while rs.next():
                row = rs.get_row_data()
                # 字段顺序：date, is_trading_day
                if len(row) >= 2 and row[1] == "1":
                    records.append(
                        {
                            "trade_date": _parse_date(row[0]),
                        }
                    )

        return self._make_result(
            data=records,
            raw_response=json.dumps(
                records, ensure_ascii=False, sort_keys=True, default=str
            ).encode("utf-8"),
            confidence="approximate",
            api_version=_API_VERSION,
        )

    # ─── 会话管理 ──────────────────────────────────────────────────

    @contextmanager
    def _session(self) -> Iterator[None]:
        """BaoStock 会话上下文：保证 login/logout 配对。

        socket 协议非线程安全，单次 fetch 内复用一个会话；
        异常时 finally 确保 logout，避免连接泄漏。

        reuse_session=True 时只登录一次，后续 fetch 复用同一连接，
        由调用方通过 close() 显式登出。
        """
        if self.reuse_session:
            self._ensure_login()
            try:
                yield
            except Exception:
                # The socket may be in an unknown state after an exception;
                # force a fresh login on the next use.
                self._logged_in = False
                raise
            return

        self._wait_rate_limit()
        lg = bs.login()
        if lg is not None and getattr(lg, "error_code", "0") != "0":
            raise RuntimeError(
                f"baostock login 失败: {getattr(lg, 'error_msg', 'unknown')}"
            )
        logger.debug("baostock 登录成功")
        try:
            yield
        finally:
            try:
                bs.logout()
                logger.debug("baostock 登出成功")
            except Exception:
                logger.warning("baostock logout 异常", exc_info=True)

    def _ensure_login(self) -> None:
        """Login once for session-reuse mode; idempotent."""
        if self._logged_in:
            return
        self._wait_rate_limit()
        lg = bs.login()
        if lg is not None and getattr(lg, "error_code", "0") != "0":
            raise RuntimeError(
                f"baostock login 失败: {getattr(lg, 'error_msg', 'unknown')}"
            )
        self._logged_in = True
        logger.debug("baostock 登录成功 (session reuse)")

    @staticmethod
    def _is_not_logged_in(error_msg: str | None) -> bool:
        """BaoStock answers 'user not logged in' after the server dropped the session."""
        if not error_msg:
            return False
        lowered = error_msg.lower()
        return "未登录" in error_msg or "not login" in lowered or "not logged" in lowered

    def _reconnect(self) -> None:
        """Force a fresh login after the server-side session expired."""
        try:
            bs.logout()
        except Exception:
            logger.debug("baostock logout during reconnect failed", exc_info=True)
        self._logged_in = False
        self._ensure_login()
        logger.warning("baostock 会话已失效，已重新登录")

    def close(self) -> None:
        """Logout explicitly when session-reuse mode is active."""
        if not self.reuse_session or not self._logged_in:
            return
        try:
            bs.logout()
            logger.debug("baostock 登出成功 (session reuse)")
        except Exception:
            logger.warning("baostock logout 异常", exc_info=True)
        finally:
            self._logged_in = False
