"""TDX (通达信) 适配器 — 封装 easy_tdx TCP 协议数据源

支持的数据类型:
  - price_daily       日线行情 (仅 raw, 不支持复权)
  - xdxr              除权除息记录 (含分红/送转/配股/股本变动)
  - balance_sheet     资产负债表 (原始字段, 字段级映射待后续)
  - income_statement  利润表 (原始字段, 字段级映射待后续)
  - cash_flow         现金流量表 (原始字段, 字段级映射待后续)

优势:
  - TCP 协议, 零 HTTP 反爬风险
  - 完整财报数据 (584 字段/期) 通过 gpcw*.dat 文件下载
  - 北交所 (BSE) 支持

限制:
  - 仅支持 raw 行情 (无前/后复权), adjust != "raw" 时返回空
  - 部分行情服务器不提供 K 线数据, 需轮询主机
  - 财报 .dat 字段为未命名 float 数组, 字段级映射待后续实现

符号格式约定:
  - price_daily / xdxr                        — 纯代码 "600519" (无前缀)
  - balance_sheet / income_statement / cash_flow — 纯代码 "600519" (无前缀)
"""

from __future__ import annotations

import datetime
import json
import logging
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import pandas as pd

from app.core.adapters.base import BaseAdapter, FetchRequest, FetchResult

# ─── 可选依赖: easy_tdx ─────────────────────────────────────────────

try:
    import easy_tdx

    from easy_tdx import (
        KNOWN_HOSTS,
        KlineCategory,
        Market,
        TdxClient,
        TdxConnectionError,
        TdxError,
    )

    _EASY_TDX_AVAILABLE: bool = True
    _EASY_TDX_VERSION: str | None = getattr(easy_tdx, "__version__", "unknown")
except ImportError:  # pragma: no cover
    easy_tdx = None  # type: ignore[assignment]
    KNOWN_HOSTS = []  # type: ignore[assignment]
    KlineCategory = None  # type: ignore[assignment]
    Market = None  # type: ignore[assignment]
    TdxClient = None  # type: ignore[assignment]
    TdxConnectionError = Exception  # type: ignore[assignment,misc]
    TdxError = Exception  # type: ignore[assignment,misc]
    _EASY_TDX_AVAILABLE = False
    _EASY_TDX_VERSION = None

logger = logging.getLogger(__name__)

# 用于溯源元数据的 API 版本标识
_API_VERSION = f"easy_tdx-{_EASY_TDX_VERSION}" if _EASY_TDX_VERSION else "easy_tdx"

# K 线分页参数: 每次 800 条 (协议上限), 最多 20 页 = ~16000 日 (~64 年)
# P0#2.4修复: 原 _MAX_BARS_PAGES=5 限制为~16年, 2009年前上市的股票早期历史被截断
_BARS_PAGE_SIZE = 800
_MAX_BARS_PAGES = 20

# 财报 .dat 文件: 最多下载 8 期 (~2 年)
_MAX_FINANCIAL_FILES = 8

# P0#2.3修复: TDX gpcw.dat 字段映射表
# 用茅台 2026Q1 公开数据逆向验证 (巨潮资讯网)
# 每个索引对应 raw_fields[i] 的 float 值
_TDX_FIELD_MAP: dict[int, str] = {
    # 资产负债表 (已用茅台 2026Q1 验证)
    16: "monetary_funds",           # 60,692,316,160
    26: "fixed_assets",             # 22,132,684,800
    27: "accounts_receivable",      # 2,850,892,032
    43: "goodwill",                 # 3,671,613,696
    53: "inventory",                # 38,455,595,008
    67: "total_equity_parent",      # 219,147,665,408
    68: "total_liabilities",        # 10,241,850,368
    # 以下索引待进一步验证 (暂时保留)
    71: "total_assets",             # 281,135,874,048
    # 其他字段暂不映射, 避免错误数据入库
}

# 已知能提供 K 线数据的行情主机 (上海电信 180.153.18.x 系列较稳定)
# from_best_host 选最低延迟主机, 但该主机可能只提供财务数据不提供 K 线。
# 这里维护一份已知能返回 K 线的主机列表, 优先尝试。
_KNOWN_BARS_HOSTS: list[str] = [
    "180.153.18.170",
    "180.153.18.171",
    "180.153.39.51",
    "202.108.25.44",
    "115.238.56.198",
    "115.238.90.165",
]

# 轮询 KNOWN_HOSTS: 在已知 K 线主机都不可用时, 最多额外尝试 N 台
_BARS_HOST_MAX_ATTEMPTS = 6


# ─── 辅助函数 ───────────────────────────────────────────────────────


def _strip_code(stock_code: str) -> str:
    """去除交易所前缀/后缀, 返回纯 6 位代码

    "SH600519" / "600519.SH" / "600519" → "600519"
    """
    code = stock_code.strip().upper()
    if code.startswith(("SH", "SZ", "BJ")):
        code = code[2:]
    if "." in code:
        code = code.split(".")[0]
    return code


def _code_to_market(stock_code: str) -> "Market | None":
    """6 位股票代码 → TDX Market 枚举

    6xxxxx → SH (1), 0/3xxxxx → SZ (0), 8/4/9xxxxx → BJ (2)
    """
    if not _EASY_TDX_AVAILABLE:
        return None
    code = _strip_code(stock_code)
    if not code or len(code) != 6 or not code.isdigit():
        return None
    first = code[0]
    if first == "6":
        return Market.SH
    if first in ("0", "3"):
        return Market.SZ
    if first in ("4", "8", "9"):
        return Market.BJ
    return None


def _clean_value(v: Any) -> Any:
    """清洗单个值: NaN/NaT → None, Timestamp/date → 'YYYY-MM-DD', numpy → Python"""
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
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d")
    if hasattr(v, "item"):
        try:
            return v.item()
        except (AttributeError, ValueError):
            pass
    return v


def _report_date_int_to_str(d: int) -> str:
    """20260331 → '2026-03-31'"""
    if not d:
        return ""
    s = str(d)
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _parse_filename_date(filename: str) -> int:
    """从 'gpcw20260331.zip' 提取报告期整数 20260331"""
    import re

    m = re.search(r"(\d{8})", filename)
    return int(m.group(1)) if m else 0


# ─── 适配器 ─────────────────────────────────────────────────────────


class TDXAdapter(BaseAdapter):
    """TDX (通达信) 适配器: TCP 协议、零反爬、备用数据源

    封装 easy_tdx 库获取 A 股日线行情、除权除息、财报数据。
    构造无需参数, rate_limit 默认 0.1s (TCP 协议, 低频控风险)。
    """

    _SUPPORTED: set[str] = {
        "price_daily",
        "xdxr",
        "balance_sheet",
        "income_statement",
        "cash_flow",
    }

    # 缓存能返回 K 线的主机 (避免每次 fetch 都轮询)
    _bars_host: str | None = None

    def __init__(self, rate_limit: float = 0.1) -> None:
        super().__init__(
            name="tdx",
            supported=self._SUPPORTED,  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )

    # ─── 调度入口 ──────────────────────────────────────────────────

    def fetch(self, request: FetchRequest) -> FetchResult:
        if not _EASY_TDX_AVAILABLE:
            return self._make_empty_result(
                reason="easy_tdx 未安装 (pip install easy_tdx)",
            )

        if not self.can_handle(request):
            return self._make_empty_result(
                reason=f"tdx 不支持数据类型: {request.data_type}",
            )

        handler: Callable[[FetchRequest], FetchResult] | None = {
            "price_daily": self._fetch_price_daily,
            "xdxr": self._fetch_xdxr,
            "balance_sheet": self._fetch_balance_sheet,
            "income_statement": self._fetch_income_statement,
            "cash_flow": self._fetch_cash_flow,
        }.get(request.data_type)

        if handler is None:
            return self._make_empty_result(
                reason=f"tdx 不支持数据类型: {request.data_type}",
            )

        try:
            return handler(request)
        except TdxConnectionError as e:
            logger.exception("tdx 连接失败: %s", request.data_type)
            return self._make_empty_result(
                reason=f"TdxConnectionError: {e}",
            )
        except TdxError as e:
            logger.exception("tdx 协议错误: %s", request.data_type)
            return self._make_empty_result(
                reason=f"TdxError: {e}",
            )
        except Exception as e:
            logger.exception("tdx fetch %s 失败", request.data_type)
            return self._make_empty_result(
                reason=f"{type(e).__name__}: {e}",
            )

    # ─── price_daily ──────────────────────────────────────────────

    def _fetch_price_daily(self, request: FetchRequest) -> FetchResult:
        """日线行情 (仅 raw)

        easy_tdx 的 GetSecurityBarsCmd 返回原始 OHLCV, 不含复权。
        TDX 服务器分行情主机和财务主机, 部分主机不提供 K 线, 需轮询。
        """
        if request.adjust != "raw":
            return self._make_empty_result(
                reason="TDX only supports raw prices",
            )

        if not request.stock_codes:
            return self._make_empty_result(
                reason="tdx price_daily 需要 stock_codes",
            )

        # 日期边界 (用于过滤和分页终止)
        start_str = request.start_date or ""
        end_str = request.end_date or ""

        all_records: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        skipped: list[str] = []

        with self._bars_session() as client:
            if client is None:
                return self._make_empty_result(
                    reason="无法连接到能提供 K 线数据的 TDX 主机",
                )

            for raw_code in request.stock_codes:
                plain_code = _strip_code(raw_code)
                market = _code_to_market(raw_code)

                if market is None:
                    skipped.append(plain_code)
                    logger.debug("跳过无效代码: %s", raw_code)
                    continue

                bars = self._fetch_bars_for_code(
                    client, market, plain_code, start_str, end_str
                )

                for _, row in bars.iterrows():
                    all_records.append(
                        {
                            "stock_code": plain_code,
                            "trade_date": _clean_value(row.get("date")),
                            "open": _clean_value(row.get("open")),
                            "high": _clean_value(row.get("high")),
                            "low": _clean_value(row.get("low")),
                            "close": _clean_value(row.get("close")),
                            "volume": _clean_value(row.get("vol")),
                            "turnover": _clean_value(row.get("amount")),
                        }
                    )

                raw_lines.append(
                    json.dumps(all_records[-len(bars):], ensure_ascii=False, sort_keys=True, default=str)
                )
                self._wait_rate_limit()

        if not all_records:
            return self._make_empty_result(
                reason="无法获取日线行情 (TDX 主机可能不可用)",
            )

        error = (
            f"跳过无效代码: {','.join(skipped)}" if skipped else None
        )
        return self._make_result(
            data=all_records,
            raw_response="\n".join(raw_lines).encode("utf-8"),
            confidence="approximate",
            error=error,
            api_version=_API_VERSION,
        )

    def _fetch_bars_for_code(
        self,
        client: "TdxClient",
        market: "Market",
        code: str,
        start_str: str,
        end_str: str,
    ) -> pd.DataFrame:
        """分页获取单只股票的日线数据, 按日期范围过滤"""
        pages: list[pd.DataFrame] = []

        for page_idx in range(_MAX_BARS_PAGES):
            offset = page_idx * _BARS_PAGE_SIZE
            self._wait_rate_limit()
            df = client.get_security_bars(
                market, code, KlineCategory.DAY, start=offset, count=_BARS_PAGE_SIZE
            )
            if df is None or df.empty:
                break
            pages.append(df)

            # 检查是否已覆盖到 start_date
            if start_str:
                oldest = df["date"].iloc[-1]
                oldest_str = oldest.strftime("%Y-%m-%d") if hasattr(oldest, "strftime") else str(oldest)
                if oldest_str <= start_str:
                    break

        if not pages:
            return pd.DataFrame()

        combined = pd.concat(pages, ignore_index=True)
        # 去重 (分页边界可能重叠)
        combined = combined.drop_duplicates(subset=["date"])

        # 日期过滤
        if start_str:
            combined = combined[combined["date"] >= pd.Timestamp(start_str)]
        if end_str:
            combined = combined[combined["date"] <= pd.Timestamp(end_str)]

        return combined.reset_index(drop=True)

    # ─── xdxr ─────────────────────────────────────────────────────

    def _fetch_xdxr(self, request: FetchRequest) -> FetchResult:
        """除权除息记录 (含分红/送转/配股/股本变动)

        easy_tdx 的 GetXdxrInfoCmd 返回全部 XDXR 事件:
        category=1: 除权除息 (fenhong/songzhuangu/peigu/peigujia)
        category=2-10: 股本变动类
        """
        if not request.stock_codes:
            return self._make_empty_result(
                reason="tdx xdxr 需要 stock_codes",
            )

        all_records: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        skipped: list[str] = []

        with self._tdx_session() as client:
            if client is None:
                return self._make_empty_result(
                    reason="无法连接到 TDX 主机",
                )

            for raw_code in request.stock_codes:
                plain_code = _strip_code(raw_code)
                market = _code_to_market(raw_code)

                if market is None:
                    skipped.append(plain_code)
                    logger.debug("跳过无效代码: %s", raw_code)
                    continue

                self._wait_rate_limit()
                df = client.get_xdxr_info(market, plain_code)

                if df is None or df.empty:
                    raw_lines.append(f"{plain_code}: 0 xdxr rows")
                    continue

                # 日期过滤
                if request.start_date:
                    df = df[df["date"] >= pd.Timestamp(request.start_date)]
                if request.end_date:
                    df = df[df["date"] <= pd.Timestamp(request.end_date)]

                for _, row in df.iterrows():
                    all_records.append(
                        {
                            "stock_code": plain_code,
                            "event_date": _clean_value(row.get("date")),
                            "category": _clean_value(row.get("category")),
                            "fenhong": _clean_value(row.get("fenhong")),
                            "songzhuangu": _clean_value(row.get("songzhuangu")),
                            "peigu": _clean_value(row.get("peigu")),
                            "peigujia": _clean_value(row.get("peigujia")),
                        }
                    )

                raw_lines.append(
                    json.dumps(all_records[-len(df):], ensure_ascii=False, sort_keys=True, default=str)
                )

        if not all_records:
            return self._make_empty_result(
                reason="无法获取除权除息记录",
            )

        error = (
            f"跳过无效代码: {','.join(skipped)}" if skipped else None
        )
        return self._make_result(
            data=all_records,
            raw_response="\n".join(raw_lines).encode("utf-8"),
            confidence="approximate",
            error=error,
            api_version=_API_VERSION,
        )

    # ─── 财务报表 (balance_sheet / income_statement / cash_flow) ──

    def _fetch_balance_sheet(self, request: FetchRequest) -> FetchResult:
        return self._fetch_financial_statement(request, "balance_sheet")

    def _fetch_income_statement(self, request: FetchRequest) -> FetchResult:
        return self._fetch_financial_statement(request, "income_statement")

    def _fetch_cash_flow(self, request: FetchRequest) -> FetchResult:
        return self._fetch_financial_statement(request, "cash_flow")

    def _fetch_financial_statement(
        self, request: FetchRequest, data_type_name: str
    ) -> FetchResult:
        """财务报表 — 下载 TDX gpcw*.dat 文件解析

        TDX 财报 .dat 文件按报告期组织 (如 gpcw20260331.zip 包含全市场该期数据),
        每条记录含 584 个 float 字段 (字段级映射待后续实现)。
        文件托管在独立的 calc 主机 (120.76.152.87), 与行情主机无关。

        返回记录字段:
          - stock_code: 6 位代码
          - report_date: 'YYYY-MM-DD'
          - raw_fields: list[float] (584 个未命名字段)
          - field_count: int
        """
        if not request.stock_codes:
            return self._make_empty_result(
                reason=f"tdx {data_type_name} 需要 stock_codes",
            )

        # 目标代码集合
        target_codes = {_strip_code(c) for c in request.stock_codes}

        # 日期边界 → 整数 (用于文件名匹配)
        start_int = int(request.start_date.replace("-", "")) if request.start_date else 0
        end_int = int(request.end_date.replace("-", "")) if request.end_date else 99999999

        all_records: list[dict[str, Any]] = []
        raw_lines: list[str] = []

        with self._tdx_session() as client:
            if client is None:
                return self._make_empty_result(
                    reason="无法连接到 TDX 主机",
                )

            # 1. 获取可用财报文件列表
            self._wait_rate_limit()
            try:
                df_files = client.get_financial_file_list()
            except Exception as e:
                return self._make_empty_result(
                    reason=f"get_financial_file_list 失败: {e}",
                )

            if df_files is None or df_files.empty:
                return self._make_empty_result(
                    reason="TDX 财报文件列表为空",
                )

            # 2. 过滤: 有实际数据的文件 + 日期范围内, 按日期降序
            df_files = df_files[df_files["filesize"] > 10000].copy()
            df_files["date_int"] = df_files["filename"].apply(_parse_filename_date)
            df_files = df_files[
                (df_files["date_int"] >= start_int)
                & (df_files["date_int"] <= end_int)
            ].sort_values("date_int", ascending=False)

            if df_files.empty:
                return self._make_empty_result(
                    reason=f"日期范围内无 TDX 财报文件 ({request.start_date} ~ {request.end_date})",
                )

            # 3. 限制下载数量
            df_files = df_files.head(_MAX_FINANCIAL_FILES)

            # 4. 逐期下载 + 解析 + 过滤
            for _, file_row in df_files.iterrows():
                filename = file_row["filename"]
                report_date_int = int(file_row["date_int"])
                full_path = f"tdxfin/{filename}"

                self._wait_rate_limit()
                try:
                    df_recs = client.get_financial_records(full_path)
                except Exception as e:
                    logger.warning("get_financial_records(%s) 失败: %s", full_path, e)
                    raw_lines.append(f"{filename}: ERROR {e}")
                    continue

                if df_recs is None or df_recs.empty:
                    raw_lines.append(f"{filename}: 0 records")
                    continue

                # 过滤目标股票
                df_filtered = df_recs[df_recs["code"].isin(target_codes)]

                for _, rec in df_filtered.iterrows():
                    raw_fields = rec.get("fields", [])
                    # fields 可能是 list[float] 或 numpy array
                    if hasattr(raw_fields, "tolist"):
                        raw_fields = raw_fields.tolist()

                    # P0#2.3修复: 将 raw_fields 映射为命名字段
                    named_fields: dict[str, Any] = {}
                    for idx, value in enumerate(raw_fields):
                        if idx in _TDX_FIELD_MAP:
                            field_name = _TDX_FIELD_MAP[idx]
                            # 转换 NaN/None
                            try:
                                import math
                                if value is not None and not (isinstance(value, float) and math.isnan(value)):
                                    named_fields[field_name] = value
                            except (TypeError, ValueError):
                                pass

                    record = {
                        "stock_code": str(rec.get("code", "")),
                        "report_date": _report_date_int_to_str(
                            int(rec.get("report_date", report_date_int))
                        ),
                        "field_count": len(raw_fields),
                    }
                    # 合并命名字段到记录中
                    record.update(named_fields)
                    # 保留 raw_fields 用于溯源
                    record["raw_fields"] = raw_fields
                    all_records.append(record)

                raw_lines.append(
                    json.dumps(all_records[-len(df_filtered):], ensure_ascii=False, sort_keys=True, default=str)
                )

        if not all_records:
            return self._make_empty_result(
                reason=f"无法获取 {data_type_name} 数据 (目标股票在财报文件中未找到)",
            )

        # P0#2.3修复: TDX 财报字段已映射为命名字段 (见 _TDX_FIELD_MAP)
        # raw_fields 仍保留用于溯源
        note = (
            f"TDX financial .dat: {all_records[0].get('field_count', '?')} "
            f"fields per record, {len(_TDX_FIELD_MAP)} named fields mapped\n"
        )
        raw_response_text = note + "\n".join(raw_lines)
        return self._make_result(
            data=all_records,
            raw_response=raw_response_text.encode("utf-8"),
            confidence="approximate",
            api_version=_API_VERSION,
        )

    # ─── 连接管理 ──────────────────────────────────────────────────

    @contextmanager
    def _tdx_session(self) -> "Iterator[TdxClient | None]":
        """通用 TDX 会话: from_best_host, 适用于 xdxr / 财报下载"""
        client: TdxClient | None = None
        try:
            client = TdxClient.from_best_host(timeout=10.0, auto_reconnect=True)
            yield client
        except (TdxConnectionError, TdxError, Exception) as e:
            logger.warning("TDX 连接失败: %s", e)
            yield None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    @contextmanager
    def _bars_session(self) -> "Iterator[TdxClient | None]":
        """K 线专用会话: 需要找到能返回 K 线数据的主机

        from_best_host 选最低延迟主机, 但该主机可能只提供财务数据不提供 K 线。
        策略: 优先用缓存主机 → from_best_host → 轮询 KNOWN_HOSTS。
        """
        client: TdxClient | None = None
        try:
            client = self._connect_for_bars()
            yield client
        except (TdxConnectionError, TdxError, Exception) as e:
            logger.warning("TDX K线主机连接失败: %s", e)
            yield None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _connect_for_bars(self) -> "TdxClient":
        """连接到能返回 K 线数据的 TDX 主机

        from_best_host 选最低延迟主机, 但该主机可能只提供财务数据不提供 K 线。
        策略 (按速度优先):
        1. 缓存主机 → 2. 已知 K 线主机 → 3. KNOWN_HOSTS 轮询 → 4. from_best_host 兜底
        """
        # 构建候选主机列表: 缓存 → 已知 K 线主机 → KNOWN_HOSTS
        candidates: list[str] = []
        if self._bars_host is not None:
            candidates.append(self._bars_host)
        for h in _KNOWN_BARS_HOSTS:
            if h not in candidates:
                candidates.append(h)
        for h in KNOWN_HOSTS:
            if h not in candidates:
                candidates.append(h)

        tried = 0
        for host in candidates:
            if tried >= len(_KNOWN_BARS_HOSTS) + _BARS_HOST_MAX_ATTEMPTS:
                break
            tried += 1
            try:
                client = TdxClient(host=host, timeout=5.0, auto_reconnect=False)
                client.connect()
                if self._test_bars(client):
                    self.__class__._bars_host = host
                    if tried > 1:
                        logger.info("找到 K 线可用主机: %s (尝试 %d 台)", host, tried)
                    return client
                client.close()
            except (TdxConnectionError, TdxError, Exception) as e:
                logger.debug("主机 %s K线测试失败: %s", host, e)

        # 全部失败: 返回 from_best_host (至少能用于 xdxr 等其他操作)
        logger.warning("未找到能返回 K 线的主机, 已尝试 %d 台", tried)
        return TdxClient.from_best_host(timeout=10.0)

    @staticmethod
    def _test_bars(client: "TdxClient") -> bool:
        """快速测试主机是否能返回 K 线数据 (用 600519 SH 测试 1 条)"""
        try:
            df = client.get_security_bars(
                Market.SH, "600519", KlineCategory.DAY, start=0, count=1
            )
            return df is not None and not df.empty
        except Exception:
            return False
