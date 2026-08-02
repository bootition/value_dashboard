"""数据源适配器基础协议

所有适配器必须实现此协议。适配器可替换（PRD 附录 A.1），
每次抓取保留溯源元数据（source, fetch_time, raw_hash, confidence）。
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ─── 数据类型枚举 ──────────────────────────────────────────────────

DataType = Literal[
    "stock_list",           # 当前上市股票全集
    "price_daily",          # 日线行情 (raw/qfq/hfq)
    "balance_sheet",        # 资产负债表
    "income_statement",     # 利润表
    "cash_flow",            # 现金流量表
    "dividends",            # 分红记录
    "xdxr",                 # 除权除息记录
    "announcements",        # 公告
    "listing_info",         # 上市信息 (ST/停牌/上市日期)
    "sw_industry",          # 申万行业分类（已废弃，仅保留兼容）
    "csrc_industry",        # CSRC（证监会）行业分类（当前口径）
    "trading_dates",        # 交易日历
]

AdjustType = Literal["raw", "qfq", "hfq"]
ConfidenceLevel = Literal["strict", "approximate", "missing"]
# P0-1: cninfo_csrc 是 CSRC 行业适配器的独立源名（与 cninfo 公告/分红适配器区分）
SourceName = Literal["cninfo", "cninfo_csrc", "akshare_eastmoney", "tdx", "baostock", "tencent", "sina", "ths", "local_cache"]


# ─── 请求/响应模型 ──────────────────────────────────────────────────

class FetchRequest(BaseModel):
    """标准化的数据抓取请求"""
    data_type: DataType
    stock_codes: list[str] = Field(default_factory=list)  # 空列表=全市场
    start_date: str | None = None       # YYYY-MM-DD
    end_date: str | None = None         # YYYY-MM-DD
    fields: list[str] | None = None     # None=全部标准字段
    adjust: AdjustType = "raw"          # 仅 price_daily 使用
    extra_params: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class SourceMetadata(BaseModel):
    """每次抓取的溯源元数据"""
    source: SourceName
    fetch_time: datetime
    raw_response_hash: str              # SHA256(raw_response)
    confidence: ConfidenceLevel
    api_version: str | None = None
    row_count: int = 0
    error: str | None = None


class FetchResult(BaseModel):
    """标准化的数据抓取结果"""
    data: list[dict[str, Any]]          # 标准化记录
    metadata: SourceMetadata
    raw_response: bytes | None = None   # 原始响应（用于归档）

    model_config = {"arbitrary_types_allowed": True}


# ─── 适配器协议 ─────────────────────────────────────────────────────

@runtime_checkable
class DataAdapter(Protocol):
    """数据源适配器协议

    每个适配器封装一个具体数据源（AKShare/CNINFO/TDX/BaoStock），
    将源特定的 API 调用转换为标准化的 FetchResult。
    """

    @property
    def name(self) -> SourceName:
        """适配器名称"""
        ...

    @property
    def supported_data_types(self) -> set[DataType]:
        """此适配器支持的数据类型集合"""
        ...

    @property
    def rate_limit_interval(self) -> float:
        """请求间隔（秒）"""
        ...

    def can_handle(self, request: FetchRequest) -> bool:
        """是否可以处理此请求"""
        ...

    def fetch(self, request: FetchRequest) -> FetchResult:
        """执行数据抓取，返回标准化结果"""
        ...


# ─── 基础适配器（提供通用功能） ─────────────────────────────────────

class BaseAdapter:
    """适配器基类，提供限流、哈希、元数据等通用功能"""

    _name: SourceName
    _supported: set[DataType]
    _rate_limit: float
    # P2修复: 改为实例属性（原为类属性，所有实例共享rate-limit时间戳）
    _last_request_time: float = 0.0  # 保留类属性作为默认值，实例会在__init__中覆盖

    def __init__(self, name: SourceName, supported: set[DataType], rate_limit: float = 1.0) -> None:
        self._name = name
        self._supported = supported
        self._rate_limit = rate_limit
        self._last_request_time: float = 0.0  # P2修复: 实例属性而非类属性

    @property
    def name(self) -> SourceName:
        return self._name

    @property
    def supported_data_types(self) -> set[DataType]:
        return self._supported

    @property
    def rate_limit_interval(self) -> float:
        return self._rate_limit

    def can_handle(self, request: FetchRequest) -> bool:
        return request.data_type in self._supported

    def _wait_rate_limit(self) -> None:
        """确保请求间隔不低于 rate_limit"""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)
        self._last_request_time = time.monotonic()

    def _make_metadata(
        self,
        raw_response: bytes | str | None,
        row_count: int,
        confidence: ConfidenceLevel = "approximate",
        error: str | None = None,
        api_version: str | None = None,
    ) -> SourceMetadata:
        """构建溯源元数据"""
        raw_bytes = b""
        if isinstance(raw_response, bytes):
            raw_bytes = raw_response
        elif isinstance(raw_response, str):
            raw_bytes = raw_response.encode("utf-8")

        raw_hash = hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else hashlib.sha256(b"<empty>").hexdigest()

        return SourceMetadata(
            source=self._name,
            fetch_time=datetime.now(timezone.utc),
            raw_response_hash=raw_hash,
            confidence=confidence,
            api_version=api_version,
            row_count=row_count,
            error=error,
        )

    def _make_result(
        self,
        data: list[dict[str, Any]],
        raw_response: bytes | str | None = None,
        confidence: ConfidenceLevel = "approximate",
        error: str | None = None,
        api_version: str | None = None,
    ) -> FetchResult:
        """构建标准化结果"""
        metadata = self._make_metadata(
            raw_response=raw_response,
            row_count=len(data),
            confidence=confidence,
            error=error,
            api_version=api_version,
        )
        return FetchResult(
            data=data,
            metadata=metadata,
            raw_response=(
                raw_response if isinstance(raw_response, bytes)
                else raw_response.encode("utf-8") if isinstance(raw_response, str) else None
            ),
        )

    def _make_empty_result(self, reason: str, confidence: ConfidenceLevel = "missing") -> FetchResult:
        """构建空结果（数据不可得）"""
        metadata = self._make_metadata(
            raw_response=None,
            row_count=0,
            confidence=confidence,
            error=reason,
        )
        return FetchResult(data=[], metadata=metadata)

    def fetch(self, request: FetchRequest) -> FetchResult:
        """子类必须实现"""
        raise NotImplementedError(f"{self._name} 适配器未实现 fetch 方法")
