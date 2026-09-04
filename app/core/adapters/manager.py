"""适配器管理器 - 路由、备用切换、限流、跨源验证

按 data_type 路由到对应适配器，主适配器失败时自动切换备用。
记录每次抓取的溯源元数据。支持跨源验证。
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC
from typing import Any, Final

from app.core.adapters.base import (
    BaseAdapter,
    DataType,
    FetchRequest,
    FetchResult,
    SourceMetadata,
)

logger = logging.getLogger(__name__)

# ─── 适配器优先级配置 ───────────────────────────────────────────────
ADAPTER_ALIASES: Final[dict[str, str]] = {"akshare": "akshare_eastmoney"}
KNOWN_ADAPTERS: Final[frozenset[str]] = frozenset(
    {"akshare_eastmoney", "baostock", "cninfo", "cninfo_csrc", "tdx", "tencent", "sina", "local_cache", "eastmoney_f10", "czb_mof", "cninfo_capital", "cninfo_funding", "legulegu", "csindex", "eastmoney_repurchase", "eastmoney_hk_dividend"}
)
DEFAULT_ADAPTER_PRIORITY: Final[dict[str, list[str]]] = {
    "stock_list": ["akshare_eastmoney"],
    "listing_info": ["akshare_eastmoney"],
    "price_daily": ["tencent", "baostock", "tdx"],
    "balance_sheet": ["sina", "tdx", "akshare_eastmoney"],
    "income_statement": ["sina", "tdx", "akshare_eastmoney"],
    "cash_flow": ["sina", "tdx", "akshare_eastmoney"],
    # cninfo 分红适配器无法提供 ex_date（需解析公告 PDF，未实现），主源恒空，
    # 仅保留在链尾作为未来 PDF 解析能力就绪位；akshare/baostock 提供完整
    # 带 ex_date 与每股数值的分红记录。
    "dividends": ["akshare_eastmoney", "baostock", "cninfo"],
    "xdxr": ["tdx"],
    "announcements": ["cninfo"],
    "sw_industry": ["local_cache"],
    # P0-1修复: CSRC 行业使用独立适配器名 cninfo_csrc，
    # 不再覆盖 cninfo（announcements/dividends 的适配器）。
    "csrc_industry": ["cninfo_csrc"],
    "trading_dates": ["akshare_eastmoney", "baostock"],
    # 业务概览（reports/67）：东财 F10 独立低频主源，独立实例与限速
    "company_profile": ["eastmoney_f10"],
    "business_breakdown": ["eastmoney_f10"],
    # 财政部国债曲线（reports/68 P3）：独立低频基准域，独立实例与限速
    "treasury_yield_curve": ["czb_mof"],
    # 历史总股本链（reports/68 P4）：CNINFO 主链；东财交叉经 eastmoney_f10
    "share_capital_history": ["cninfo_capital", "eastmoney_f10"],
    # IPO 首发募资（CNINFO 合规源，数据补全 2026-08-25）
    "ipo_funding": ["cninfo_funding"],
    # 增发/配股募资（东财 F10 BonusFinancing，数据补全 2026-08-25）
    "placement_funding": ["eastmoney_f10"],
    # 指数估值（乐咕主源；中证官网交叉由 updater 显式调用）
    "index_valuation": ["legulegu"],
    # 股票回购/注销金额（东财回购明细，数据补全 2026-08-26）
    "buyback_funding": ["eastmoney_repurchase"],
    # 港股分红历史（东财 datacenter 单股接口；源侧 ≤2 req/s）
    "hk_dividends": ["eastmoney_hk_dividend"],
}
DEFAULT_ADAPTER_RATE_LIMITS: Final[dict[str, float]] = {
    "akshare_eastmoney": 0.5,
    "cninfo": 1.5,
    "cninfo_csrc": 0.35,
    "baostock": 0.8,
    "tdx": 0.2,
    "tencent": 0.2,
    "sina": 0.35,
    "eastmoney_f10": 0.5,
    "czb_mof": 0.5,
    "cninfo_capital": 1.5,
    "cninfo_funding": 1.5,
    "legulegu": 1.0,
    "csindex": 1.0,
    "eastmoney_repurchase": 0.0,
    "eastmoney_hk_dividend": 0.5,
}


class AdapterConfigurationError(ValueError):
    """Raised when adapter priority configuration names an unsupported adapter."""


def build_adapter_priority(
    configured: dict[str, str | list[str]] | None,
) -> dict[str, list[str]]:
    """Normalize configured primaries and retain each data type's fallback chain."""
    priorities = {data_type: list(names) for data_type, names in DEFAULT_ADAPTER_PRIORITY.items()}
    if not configured:
        return priorities

    for data_type, value in configured.items():
        configured_names = [value] if isinstance(value, str) else value
        normalized = [ADAPTER_ALIASES.get(name, name) for name in configured_names]
        unknown = [name for name in normalized if name not in KNOWN_ADAPTERS]
        if unknown:
            raise AdapterConfigurationError(
                f"unknown adapter for {data_type}: {', '.join(unknown)}"
            )
        fallbacks = priorities.get(data_type, [])
        priorities[data_type] = list(dict.fromkeys([*normalized, *fallbacks]))
    return priorities


def build_adapter_rate_limits(
    configured: dict[str, float] | None,
) -> dict[str, float]:
    """Normalize configured adapter names and retain safe defaults."""
    rate_limits = dict(DEFAULT_ADAPTER_RATE_LIMITS)
    if not configured:
        return rate_limits

    for name, interval in configured.items():
        normalized_name = ADAPTER_ALIASES.get(name, name)
        if normalized_name not in KNOWN_ADAPTERS:
            raise AdapterConfigurationError(f"unknown adapter rate limit: {normalized_name}")
        if interval < 0:
            raise AdapterConfigurationError(
                f"negative adapter rate limit for {normalized_name}: {interval}"
            )
        rate_limits[normalized_name] = float(interval)
    return rate_limits


def _load_adapter_priority() -> dict[str, list[str]]:
    """Read and normalize adapter priorities from the active configuration."""
    from app.core.config import Config

    cfg = Config.current()
    configured = cfg["adapters"].get("primary", {}) if "adapters" in cfg._data else {}
    return build_adapter_priority(configured)


def _load_adapter_rate_limits() -> dict[str, float]:
    """Read and normalize adapter rate limits from the active configuration."""
    from app.core.config import Config

    cfg = Config.current()
    configured = (
        cfg["adapters"].get("rate_limit_interval", {})
        if "adapters" in cfg._data
        else {}
    )
    return build_adapter_rate_limits(configured)

ADAPTER_PRIORITY: dict[str, list[str]] = _load_adapter_priority()


class AdapterManager:
    """适配器管理器

    管理多个数据源适配器，按优先级路由请求，
    主适配器失败时自动切换备用适配器。
    """

    def __init__(self) -> None:
        self._adapters: dict[str, BaseAdapter] = {}
        self._initialized = False
        self._rate_limits = _load_adapter_rate_limits()
        # Circuit breaker: {adapter_name: {"failures": int, "tripped_until": datetime|None}}
        self._circuit_breaker: dict[str, dict[str, Any]] = {}
        self._state_lock = threading.RLock()
        self._initialization_lock = threading.Lock()

    # ─── Circuit breaker ─────────────────────────────────────────────
    _CIRCUIT_FAILURE_THRESHOLD = 5       # 连续失败5次后熔断
    _CIRCUIT_COOLDOWN_SECONDS = 300      # 熔断后冷却5分钟

    def _is_circuit_tripped(self, adapter_name: str) -> bool:
        """检查适配器是否被熔断"""
        with self._state_lock:
            state = self._circuit_breaker.get(adapter_name)
            if not state or not state.get("tripped_until"):
                return False
            from datetime import datetime
            now = datetime.now(UTC)
            if now < state["tripped_until"]:
                remaining = (state["tripped_until"] - now).total_seconds()
                logger.debug(
                    f"熔断跳过 {adapter_name} (剩余冷却 {remaining:.0f}s, "
                    f"连续失败 {state['failures']} 次)"
                )
                return True
            state["tripped_until"] = None
            state["failures"] = 0
            return False

    def _record_circuit_success(self, adapter_name: str) -> None:
        """适配器成功，重置熔断计数器"""
        with self._state_lock:
            if adapter_name in self._circuit_breaker:
                self._circuit_breaker[adapter_name]["failures"] = 0
                self._circuit_breaker[adapter_name]["tripped_until"] = None

    def _record_circuit_failure(self, adapter_name: str) -> None:
        """适配器失败，增加计数器，超阈值则熔断"""
        from datetime import datetime
        with self._state_lock:
            if adapter_name not in self._circuit_breaker:
                self._circuit_breaker[adapter_name] = {"failures": 0, "tripped_until": None}
            state = self._circuit_breaker[adapter_name]
            state["failures"] += 1
            if state["failures"] >= self._CIRCUIT_FAILURE_THRESHOLD:
                state["tripped_until"] = datetime.now(UTC) + \
                    __import__("datetime").timedelta(seconds=self._CIRCUIT_COOLDOWN_SECONDS)
                logger.warning(
                    f"熔断触发: {adapter_name} 连续失败 {state['failures']} 次, "
                    f"冷却 {self._CIRCUIT_COOLDOWN_SECONDS}s"
                )

    def register(self, adapter: BaseAdapter) -> None:
        """注册适配器"""
        self._adapters[adapter.name] = adapter
        logger.info(f"已注册适配器: {adapter.name} (支持: {adapter.supported_data_types})")

    def _ensure_initialized(self) -> None:
        """延迟初始化适配器（避免导入时连接外部服务）"""
        if self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            self._initialize_adapters()

    def _initialize_adapters(self) -> None:
        """Register adapters while the caller holds the initialization lock."""

        # AKShare 适配器（主适配器）
        try:
            from app.core.adapters.akshare_adapter import AKShareAdapter
            self.register(AKShareAdapter(self._rate_limits["akshare_eastmoney"]))
        except ImportError as e:
            logger.warning(f"AKShare 适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"AKShare 适配器初始化失败: {e}")

        # CNINFO 适配器（真值层）
        try:
            from app.core.adapters.cninfo_adapter import CNINFOAdapter
            self.register(CNINFOAdapter(self._rate_limits["cninfo"]))
        except ImportError as e:
            logger.warning(f"CNINFO 适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"CNINFO 适配器初始化失败: {e}")

        # BaoStock 适配器（价格补充）
        try:
            from app.core.adapters.baostock_adapter import BaoStockAdapter
            self.register(BaoStockAdapter(self._rate_limits["baostock"], reuse_session=True))
        except ImportError as e:
            logger.warning(f"BaoStock 适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"BaoStock 适配器初始化失败: {e}")

        # TDX 适配器（备用数据源 - TCP协议，零反爬风险，完整财务报表）
        try:
            from app.core.adapters.tdx_adapter import TDXAdapter
            self.register(TDXAdapter(self._rate_limits["tdx"]))
        except ImportError as e:
            logger.warning(f"TDX 适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"TDX 适配器初始化失败: {e}")

        # Tencent 适配器（北交所前复权日线的免费回退源）
        try:
            from app.core.adapters.tencent_adapter import TencentAdapter
            self.register(TencentAdapter(self._rate_limits["tencent"]))
        except ImportError as e:
            logger.warning(f"Tencent 适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"Tencent 适配器初始化失败: {e}")

        # Sina 适配器（免费财务三表的主数据源）
        try:
            from app.core.adapters.sina_adapter import SinaAdapter
            self.register(SinaAdapter(self._rate_limits["sina"]))
        except ImportError as e:
            logger.warning(f"Sina 适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"Sina 适配器初始化失败: {e}")

        # CSRC 行业适配器（CNINFO 证监会口径，当前行业唯一来源）
        try:
            from app.core.adapters.csrc_industry_adapter import CSRCIndustryAdapter
            self.register(CSRCIndustryAdapter(self._rate_limits["cninfo_csrc"]))
        except ImportError as e:
            logger.warning(f"CSRC 行业适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"CSRC 行业适配器初始化失败: {e}")

        # 东财 F10 业务概览适配器（独立低频域，独立实例与限速）
        try:
            from app.core.adapters.eastmoney_f10_adapter import EastMoneyF10Adapter
            self.register(EastMoneyF10Adapter(self._rate_limits["eastmoney_f10"]))
        except ImportError as e:
            logger.warning(f"东财 F10 业务概览适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"东财 F10 业务概览适配器初始化失败: {e}")

        # 财政部国债曲线适配器（独立低频基准域，独立实例与限速）
        try:
            from app.core.adapters.czb_mof_adapter import TreasuryMofAdapter
            self.register(TreasuryMofAdapter(self._rate_limits["czb_mof"]))
        except ImportError as e:
            logger.warning(f"财政部国债曲线适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"财政部国债曲线适配器初始化失败: {e}")

        # 历史总股本链适配器（CNINFO 主链，P4）
        try:
            from app.core.adapters.capital_history_adapter import CapitalHistoryAdapter
            self.register(CapitalHistoryAdapter(self._rate_limits["cninfo_capital"]))
        except ImportError as e:
            logger.warning(f"历史股本链适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"历史股本链适配器初始化失败: {e}")

        # CNINFO IPO 首发募资适配器（独立低频域，数据补全 2026-08-25）
        try:
            from app.core.adapters.cninfo_funding_adapter import CNINFOFundingAdapter
            self.register(CNINFOFundingAdapter(self._rate_limits["cninfo_funding"]))
        except ImportError as e:
            logger.warning(f"CNINFO IPO 适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"CNINFO IPO 适配器初始化失败: {e}")

        # 指数估值适配器（乐咕主源 + 中证官网交叉，数据补全 2026-08-25）
        try:
            from app.core.adapters.index_valuation_adapter import (
                CSIndexIndexAdapter,
                LeguleguIndexAdapter,
            )
            self.register(LeguleguIndexAdapter(self._rate_limits["legulegu"]))
            self.register(CSIndexIndexAdapter(self._rate_limits["csindex"]))
        except ImportError as e:
            logger.warning(f"指数估值适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"指数估值适配器初始化失败: {e}")

        # 东财股票回购/注销适配器（全市场一次性低频，数据补全 2026-08-26）
        try:
            from app.core.adapters.eastmoney_repurchase_adapter import EastMoneyRepurchaseAdapter
            self.register(EastMoneyRepurchaseAdapter(self._rate_limits["eastmoney_repurchase"]))
        except ImportError as e:
            logger.warning(f"东财回购适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"东财回购适配器初始化失败: {e}")

        # 东财港股分红适配器（datacenter 单股接口，2026-09-04；
        # 独立 0.5s 限速实例，不与 A 股/业务概览适配器共享间隔）
        try:
            from app.core.adapters.hk_dividend_adapter import EastMoneyHKDividendAdapter
            self.register(EastMoneyHKDividendAdapter(self._rate_limits["eastmoney_hk_dividend"]))
        except ImportError as e:
            logger.warning(f"东财港股分红适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"东财港股分红适配器初始化失败: {e}")

        self._initialized = True
        logger.info(f"适配器管理器初始化完成: {list(self._adapters.keys())}")

    def fetch(self, request: FetchRequest) -> FetchResult:
        """按默认优先级尝试适配器，主适配器失败时切换备用。"""
        self._ensure_initialized()
        priority_list = ADAPTER_PRIORITY.get(request.data_type, [])
        return self.fetch_with_sources(request, priority_list)

    def fetch_with_sources(
        self,
        request: FetchRequest,
        source_names: list[str],
    ) -> FetchResult:
        """按调用方给定的来源顺序尝试，不触发默认链中的慢速回退。

        财务明细回填等批量任务用 "sina -> akshare_eastmoney" 快速失败，
        避免每个源缺口股票都落到 40s+ 的 TDX 回退上；需要 TDX 时调用方
        可显式传入 source_names=["tdx"]。
        """
        self._ensure_initialized()
        priority_list = source_names or ADAPTER_PRIORITY.get(request.data_type, [])
        if not priority_list:
            return FetchResult(
                data=[],
                metadata=SourceMetadata(
                    source="local_cache",
                    fetch_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                    raw_response_hash="",
                    confidence="missing",
                    error=f"无适配器支持 data_type={request.data_type}",
                ),
            )

        last_result: FetchResult | None = None
        tried_adapters: list[str] = []

        for adapter_name in priority_list:
            adapter = self._adapters.get(adapter_name)
            if adapter is None:
                logger.debug(f"适配器 {adapter_name} 未注册，跳过")
                continue

            if not adapter.can_handle(request):
                continue

            # 熔断检查：跳过被熔断的适配器
            if self._is_circuit_tripped(adapter_name):
                continue

            tried_adapters.append(adapter_name)
            logger.info(f"尝试 {adapter_name} 获取 {request.data_type}...")

            try:
                result = adapter.fetch(request)
                if result.metadata.error is None and len(result.data) > 0:
                    logger.info(
                        f"{adapter_name} 成功获取 {request.data_type}: "
                        f"{len(result.data)} 行, confidence={result.metadata.confidence}"
                    )
                    self._record_circuit_success(adapter_name)
                    return result

                # P1-27修复: 区分"有错误"和"合法空结果"
                # 有错误 → 记录失败 + 触发熔断
                # 无错误但空数据 → 不触发熔断（可能是无分红的股票等合法空结果）
                last_result = result
                if result.metadata.error is not None:
                    self._record_circuit_failure(adapter_name)
                    logger.warning(
                        f"{adapter_name} 返回错误: {result.metadata.error}"
                    )
                else:
                    # 无错误但空数据，继续尝试下一个适配器但不计入熔断
                    logger.debug(
                        f"{adapter_name} 返回空数据（无错误）: {request.data_type}"
                    )

            except Exception as e:
                logger.error(f"{adapter_name} 抓取 {request.data_type} 失败: {e}")
                self._record_circuit_failure(adapter_name)
                last_result = FetchResult(
                    data=[],
                    metadata=SourceMetadata(
                        source=adapter_name,
                        fetch_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                        raw_response_hash="",
                        confidence="missing",
                        error=str(e),
                    ),
                )

        # 所有适配器都失败
        if last_result:
            logger.error(
                f"所有适配器均失败 ({tried_adapters}): {last_result.metadata.error}"
            )
            return last_result

        # 没有可用的适配器
        return FetchResult(
            data=[],
            metadata=SourceMetadata(
                source="local_cache",
                fetch_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                raw_response_hash="",
                confidence="missing",
                error=f"无可用适配器 (tried: {tried_adapters})",
            ),
        )

    def fetch_single(
        self,
        data_type: DataType,
        stock_code: str | None = None,
        **kwargs: Any,
    ) -> FetchResult:
        """便捷方法：单股数据抓取"""
        codes = [stock_code] if stock_code else []
        request = FetchRequest(
            data_type=data_type,
            stock_codes=codes,
            **kwargs,
        )
        return self.fetch(request)

    def fetch_batch(
        self,
        data_type: DataType,
        stock_codes: list[str],
        **kwargs: Any,
    ) -> list[FetchResult]:
        """批量抓取：对每只股票分别调用 fetch"""
        results: list[FetchResult] = []
        for i, code in enumerate(stock_codes):
            result = self.fetch_single(data_type, code, **kwargs)
            results.append(result)
            if (i + 1) % 100 == 0:
                logger.info(f"批量抓取 {data_type} 进度: {i + 1}/{len(stock_codes)}")
        return results

    @property
    def available_adapters(self) -> list[str]:
        """已注册的适配器列表"""
        self._ensure_initialized()
        return list(self._adapters.keys())

    def get_adapter(self, name: str) -> BaseAdapter | None:
        """按名称获取适配器"""
        self._ensure_initialized()
        return self._adapters.get(name)

    def close(self) -> None:
        """Release reusable adapter sessions after a long-running update."""
        for adapter in self._adapters.values():
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    def recover_after_timeout(self) -> None:
        """Ask stateful sources to recreate their session before their next request."""
        self._ensure_initialized()
        for adapter in self._adapters.values():
            request_relogin = getattr(adapter, "request_relogin", None)
            if callable(request_relogin):
                request_relogin()
