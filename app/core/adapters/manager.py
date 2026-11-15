"""适配器管理器 - 路由、备用切换、限流、跨源验证

按 data_type 路由到对应适配器，主适配器失败时自动切换备用。
记录每次抓取的溯源元数据。支持跨源验证。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final, get_args

from app.core.adapters.base import (
    CIRCUIT_FAILURE_ERROR_KINDS,
    BaseAdapter,
    DataType,
    ErrorKind,
    FetchRequest,
    FetchResult,
    SourceMetadata,
    SourceName,
)

logger = logging.getLogger(__name__)

#: SourceName 的合法取值集合：manager 构造兜底元数据时避免写入未知源名。
_KNOWN_SOURCE_NAMES: Final[frozenset[str]] = frozenset(get_args(SourceName))

#: error_kind 严重度（仅用于把多个结果归并成“最坏”的一个，不参与熔断判定）。
_ERROR_KIND_SEVERITY: Final[dict[str, int]] = {
    "none": 0,
    "legal_empty": 1,
    "source_unsupported": 1,
    "deadline": 2,
    "truncated_ok": 2,
    "per_symbol_data_error": 3,
    "source_unavailable": 4,
}

#: 源已响应但本结果既不是“成功”、也不能证明源健康：**不增加也不重置**
#: 熔断计数。2026-11-13 P1-a：legal_empty/per_symbol_data_error/truncated_ok
#: 也必须中性——风控后的死源会持续返回空表/截断/个股格式错，若把它们当
#: “源正常响应”清零，该 (adapter, data_type) 桶将永不熔断、每轮继续全量打。
_CIRCUIT_NEUTRAL_ERROR_KINDS: Final[frozenset[str]] = frozenset(
    {
        "legal_empty",
        "per_symbol_data_error",
        "source_unsupported",
        "deadline",
        "truncated_ok",
    }
)

#: 2026-11-13 P1-b：>=N 只股票的批量请求若整批 0 行且标记 legal_empty，
#: 视为“疑似风控空表”（空 DataFrame 而非抛错），升级为 source_unavailable。
_SUSPICIOUS_EMPTY_BATCH_THRESHOLD: Final[int] = 10

# ─── 适配器优先级配置 ───────────────────────────────────────────────
ADAPTER_ALIASES: Final[dict[str, str]] = {"akshare": "akshare_eastmoney"}
KNOWN_ADAPTERS: Final[frozenset[str]] = frozenset(
    {"akshare_eastmoney", "baostock", "cninfo", "cninfo_csrc", "tdx", "tencent", "sina", "ths", "sws", "local_cache", "eastmoney_f10", "czb_mof", "cninfo_capital", "cninfo_funding", "legulegu", "csindex", "eastmoney_repurchase", "eastmoney_hk_dividend"}
)
DEFAULT_ADAPTER_PRIORITY: Final[dict[str, list[str]]] = {
    "stock_list": ["akshare_eastmoney"],
    "listing_info": ["akshare_eastmoney"],
    "price_daily": ["tencent", "baostock", "tdx"],
    "balance_sheet": ["sina", "tdx", "akshare_eastmoney"],
    "income_statement": ["sina", "tdx", "akshare_eastmoney"],
    "cash_flow": ["sina", "tdx", "akshare_eastmoney"],
    # cninfo 分红路径只处理公告文本、拿不到结构化除权日（不是"等 PDF"，PDF 链
    # 已于 reports/123 整体删除），链尾会把源能力缺失伪装成合法空；故从默认链
    # 移除。CNINFOAdapter._fetch_dividends 保留为显式 unsupported error，防止显式
    # 配置 cninfo 为 primary 时静默空。结构化分红（含 ex_date 与每 10 股派息）
    # 由 akshare（stock_dividend_cninfo / stock_fhps_detail_em）与 baostock 提供。
    "dividends": ["akshare_eastmoney", "baostock"],
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
    # 指数估值（乐咕主源；中证官网交叉与申万行业由 updater 显式调用）
    "index_valuation": ["legulegu"],
    # 股票回购/注销金额（东财回购明细，数据补全 2026-08-26）
    "buyback_funding": ["eastmoney_repurchase"],
    # 港股分红历史（东财 datacenter 单股接口；源侧 ≤2 req/s）
    "hk_dividends": ["eastmoney_hk_dividend"],
    # ETF 数据（同花顺官方 Financial-API，2026-09-05；免费额度低频使用）
    "etf_daily": ["ths"],
    "etf_snapshot": ["ths"],
    "etf_profile": ["ths"],
    "etf_track_percentile": ["ths"],
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
    "ths": 0.5,
    "sws": 0.5,
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
        # Circuit breaker: {"adapter:data_type": {"failures": int,
        #   "tripped_until": datetime|None, "failure_times": [monotonic,...]}}
        # 2026-11-12 P0：桶按 (adapter, data_type) 隔离，且失败计数限制在时间窗内。
        self._circuit_breaker: dict[str, dict[str, Any]] = {}
        self._state_lock = threading.RLock()
        self._initialization_lock = threading.Lock()

    # ─── Circuit breaker ─────────────────────────────────────────────
    _CIRCUIT_FAILURE_THRESHOLD = 5       # 同一 (adapter, data_type) 窗口内失败5次后熔断
    _CIRCUIT_COOLDOWN_SECONDS = 300      # 熔断后冷却5分钟
    _CIRCUIT_FAILURE_WINDOW_SECONDS = 600  # 失败计数只统计最近10分钟（跨小时不再累计）

    def _circuit_key(self, adapter_name: str, data_type: str) -> str:
        """熔断桶 key：``adapter:data_type``（契约 §1，跨 data_type 不得互相影响）。"""
        return f"{adapter_name}:{data_type}"

    def _circuit_cooldown_remaining(self, adapter_name: str, data_type: str) -> float:
        """返回剩余冷却秒数（未熔断返回 0）。"""
        with self._state_lock:
            state = self._circuit_breaker.get(self._circuit_key(adapter_name, data_type))
            if not state or not state.get("tripped_until"):
                return 0.0
            return max(0.0, (state["tripped_until"] - datetime.now(UTC)).total_seconds())

    def _is_circuit_tripped(self, adapter_name: str, data_type: str) -> bool:
        """检查 (adapter, data_type) 桶是否被熔断。"""
        with self._state_lock:
            state = self._circuit_breaker.get(self._circuit_key(adapter_name, data_type))
            if not state or not state.get("tripped_until"):
                return False
            now = datetime.now(UTC)
            if now < state["tripped_until"]:
                remaining = (state["tripped_until"] - now).total_seconds()
                logger.debug(
                    "熔断跳过 %s data_type=%s (剩余冷却 %.0fs, 窗口内失败 %d 次)",
                    adapter_name,
                    data_type,
                    remaining,
                    state.get("failures", 0),
                )
                return True
            state["tripped_until"] = None
            state["failures"] = 0
            state["failure_times"] = []
            return False

    def _record_circuit_success(self, adapter_name: str, data_type: str) -> None:
        """源正常响应（含合法空），重置该 (adapter, data_type) 桶的失败计数。"""
        key = self._circuit_key(adapter_name, data_type)
        with self._state_lock:
            if key in self._circuit_breaker:
                self._circuit_breaker[key]["failures"] = 0
                self._circuit_breaker[key]["tripped_until"] = None
                self._circuit_breaker[key]["failure_times"] = []

    def _record_circuit_failure(self, adapter_name: str, data_type: str) -> None:
        """记录一次真源故障（只有 source_unavailable 会走到这里）。

        2026-11-12 P0：连续失败必须限制在时间窗内且按 data_type 隔离。
        原来的实现只按 adapter 累计、永不衰减，5 次跨 95 分钟、跨 2 个
        data_type 的无关失败会把整个源熔断 300s。
        """
        key = self._circuit_key(adapter_name, data_type)
        now_mono = time.monotonic()
        now_dt = datetime.now(UTC)
        with self._state_lock:
            state = self._circuit_breaker.setdefault(
                key, {"failures": 0, "tripped_until": None, "failure_times": []}
            )
            window_start = now_mono - self._CIRCUIT_FAILURE_WINDOW_SECONDS
            recent = [t for t in state.get("failure_times", []) if t >= window_start]
            recent.append(now_mono)
            state["failure_times"] = recent
            state["failures"] = len(recent)
            if state["failures"] >= self._CIRCUIT_FAILURE_THRESHOLD:
                state["tripped_until"] = now_dt + timedelta(
                    seconds=self._CIRCUIT_COOLDOWN_SECONDS
                )
                logger.warning(
                    "熔断触发: %s data_type=%s 窗口内连续失败 %d 次, 冷却 %ds",
                    adapter_name,
                    data_type,
                    state["failures"],
                    self._CIRCUIT_COOLDOWN_SECONDS,
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

        # 指数估值适配器（乐咕主源 + 中证官网交叉 + 申万行业，2026-08-25 建域）
        try:
            from app.core.adapters.index_valuation_adapter import (
                CSIndexIndexAdapter,
                LeguleguIndexAdapter,
                SwsIndexAdapter,
            )
            self.register(LeguleguIndexAdapter(self._rate_limits["legulegu"]))
            self.register(CSIndexIndexAdapter(self._rate_limits["csindex"]))
            self.register(SwsIndexAdapter(self._rate_limits["sws"]))
        except ImportError as e:
            logger.warning(f"指数估值适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"指数估值适配器初始化失败: {e}")

        # 同花顺官方 Financial-API 适配器（ETF 行情/资料/跟踪分位，2026-09-05；
        # 免费额度低频使用，独立 0.5s 限速实例，不参与个股价格主链）
        try:
            from app.core.adapters.ths_adapter import ThsAdapter
            self.register(ThsAdapter(self._rate_limits["ths"]))
        except ImportError as e:
            logger.warning(f"同花顺适配器未安装: {e}")
        except Exception as e:
            logger.warning(f"同花顺适配器初始化失败: {e}")

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

    def _codes_hint(self, request: FetchRequest) -> str:
        """日志用代码摘要（最多 5 个，避免整批刷屏）。"""
        codes = request.stock_codes
        if not codes:
            return "<all>"
        shown = ",".join(codes[:5])
        return f"{shown},+{len(codes) - 5}" if len(codes) > 5 else shown

    @staticmethod
    def _metadata_source(name: str | None) -> str:
        """把候选源名收敛为合法的 SourceName，未知名回落到 local_cache。"""
        return name if name in _KNOWN_SOURCE_NAMES else "local_cache"

    @staticmethod
    def _worst_error_kind(kinds: list[str]) -> str:
        """把多个结果归并为最坏 error_kind（仅用于披露，不参与熔断判定）。"""
        if not kinds:
            return "none"
        return max(kinds, key=lambda kind: _ERROR_KIND_SEVERITY.get(kind, 0))

    @staticmethod
    def _counts_toward_circuit(
        metadata: SourceMetadata, data: list[dict[str, Any]],
    ) -> bool:
        """是否计入熔断（契约 §1 + Lead 2026-11-12 裁决）。

        - ``source_unavailable``：迁移后的明确真源故障 → 计入；
        - ``none``/``""`` + error 非空 + 无数据：未迁移适配器的兜底 → 计入；
        - ``legal_empty``/``source_unsupported``/``per_symbol_data_error``/
          ``deadline``/``truncated_ok``：无论 error/reason 是否为空 → 一律不计。
        """
        if metadata.error_kind in CIRCUIT_FAILURE_ERROR_KINDS:
            return True
        return (
            metadata.error_kind in ("none", "")
            and metadata.error is not None
            and not data
        )

    @staticmethod
    def _is_clean_success(metadata: SourceMetadata, data: list[dict[str, Any]]) -> bool:
        """干净成功：有数据、无 error、未截断、且不是源级/个股级故障。"""
        if not data or metadata.error is not None:
            return False
        if metadata.truncated:
            return False
        return metadata.error_kind not in {
            "source_unavailable",
            "per_symbol_data_error",
            "truncated_ok",
            "deadline",
        }

    def _deadline_result(
        self, request: FetchRequest, candidates: list[str],
    ) -> FetchResult:
        """入口预算已耗尽：不发请求、不计熔断，返回 error_kind="deadline"。"""
        source = self._metadata_source(candidates[0] if candidates else None)
        message = (
            f"请求预算在进入适配器前已耗尽（未发出任何请求）: "
            f"data_type={request.data_type}, codes={self._codes_hint(request)}"
        )
        logger.warning(
            "请求预算已耗尽，未发出请求: data_type=%s, candidates=%s, codes=%s",
            request.data_type,
            candidates,
            self._codes_hint(request),
        )
        return FetchResult(
            data=[],
            metadata=SourceMetadata(
                source=source,  # type: ignore[arg-type]
                fetch_time=datetime.now(UTC),
                raw_response_hash="",
                confidence="missing",
                error=None,
                error_kind="deadline",
                reason=message,
            ),
        )

    def fetch_with_sources(
        self,
        request: FetchRequest,
        source_names: list[str],
    ) -> FetchResult:
        """按调用方给定的来源顺序尝试，不触发默认链中的慢速回退。

        财务明细回填等批量任务用 "sina -> akshare_eastmoney" 快速失败，
        避免每个源缺口股票都落到 40s+ 的 TDX 回退上；需要 TDX 时调用方
        可显式传入 source_names=["tdx"]。

        2026-11-12 契约 §1：
        - 熔断桶按 (adapter, data_type) 隔离；
        - 只有 error_kind="source_unavailable" 计入熔断；
        - 入口 budget 耗尽 → 直接 deadline，不发请求；
        - 全合法空/真失败/partial 三条日志分支都不得打印字面 None。
        """
        self._ensure_initialized()
        priority_list = source_names or ADAPTER_PRIORITY.get(request.data_type, [])
        codes_hint = self._codes_hint(request)
        if not priority_list:
            # 2026-11-13 P1（D）：候选源为空是"没注册/配置缺失"，不是"源不支持
            # 这只票"。source_unsupported 会被上层直接结算成永久 missing，
            # 这里必须用可重试的 source_unavailable 并写明 adapter_not_registered。
            message = (
                f"adapter_not_registered: 无适配器支持 data_type={request.data_type}"
                "（候选源为空，请检查依赖安装/配置）"
            )
            logger.warning("%s", message)
            return FetchResult(
                data=[],
                metadata=SourceMetadata(
                    source="local_cache",
                    fetch_time=datetime.now(UTC),
                    raw_response_hash="",
                    confidence="missing",
                    error=message,
                    error_kind="source_unavailable",
                    reason=message,
                ),
            )

        # 2026-11-12 P1：预算出生即耗尽时不得再发请求（此前会继续调用适配器）。
        if request.deadline_exceeded():
            return self._deadline_result(request, priority_list)

        last_result: FetchResult | None = None
        first_partial: FetchResult | None = None
        followup_errors: list[str] = []
        followup_kinds: list[str] = []
        attempt_results: list[FetchResult] = []
        attempt_adapters: list[str] = []
        tried_adapters: list[str] = []
        tripped: dict[str, float] = {}

        for adapter_name in priority_list:
            adapter = self._adapters.get(adapter_name)
            if adapter is None:
                logger.debug(f"适配器 {adapter_name} 未注册，跳过")
                continue

            if not adapter.can_handle(request):
                continue

            # 熔断检查：按 (adapter, data_type) 桶跳过被熔断的适配器
            if self._is_circuit_tripped(adapter_name, request.data_type):
                tripped[adapter_name] = self._circuit_cooldown_remaining(
                    adapter_name, request.data_type
                )
                continue

            tried_adapters.append(adapter_name)
            logger.info(f"尝试 {adapter_name} 获取 {request.data_type}...")

            try:
                result = adapter.fetch(request)
            except Exception as e:
                # 适配器抛异常 = 真源故障，唯一会计入熔断的异常路径。
                logger.error("%s 抓取 %s 失败: %s", adapter_name, request.data_type, e)
                self._record_circuit_failure(adapter_name, request.data_type)
                reason = f"{adapter_name}: {e}"
                followup_errors.append(reason)
                followup_kinds.append("source_unavailable")
                last_result = FetchResult(
                    data=[],
                    metadata=SourceMetadata(
                        source=self._metadata_source(adapter_name),  # type: ignore[arg-type]
                        fetch_time=datetime.now(UTC),
                        raw_response_hash="",
                        confidence="missing",
                        error=str(e),
                        error_kind="source_unavailable",
                        reason=reason,
                    ),
                )
                attempt_results.append(last_result)
                attempt_adapters.append(adapter_name)
                continue

            metadata = result.metadata
            kind = metadata.error_kind
            counts_toward_circuit = self._counts_toward_circuit(metadata, result.data)
            if kind in ("none", "") and counts_toward_circuit:
                # 未迁移适配器兜底：error 非空 + 空数据 = 真源故障。归一化为
                # source_unavailable，让后续日志/T2 的 error_kind 路由一致。
                metadata = metadata.model_copy(update={"error_kind": "source_unavailable"})
                result = result.model_copy(update={"metadata": metadata})
                kind = "source_unavailable"
            if counts_toward_circuit:
                self._record_circuit_failure(adapter_name, request.data_type)
            elif kind in _CIRCUIT_NEUTRAL_ERROR_KINDS:
                # 合法空/个股错/能力缺失/预算耗尽/截断：不增加也不重置。
                pass
            elif self._is_clean_success(metadata, result.data):
                # 只有真正拿到数据且无 error 才算源健康、清零失败计数。
                self._record_circuit_success(adapter_name, request.data_type)

            if self._is_clean_success(metadata, result.data):
                logger.info(
                    f"{adapter_name} 成功获取 {request.data_type}: "
                    f"{len(result.data)} 行, confidence={metadata.confidence}"
                )
                return result

            last_result = result
            attempt_results.append(result)
            attempt_adapters.append(adapter_name)
            note = metadata.reason or metadata.error or kind
            has_data = bool(result.data)
            is_partial = has_data and (
                metadata.error is not None
                or metadata.truncated
                or kind in {"source_unavailable", "per_symbol_data_error", "deadline"}
            )
            if is_partial:
                if first_partial is None:
                    first_partial = result
                else:
                    followup_errors.append(note)
                followup_kinds.append(kind)
                logger.warning(
                    "%s 仅返回部分数据 (data_type=%s, rows=%d, error_kind=%s): %s",
                    adapter_name,
                    request.data_type,
                    len(result.data),
                    kind,
                    note,
                )
            elif kind == "legal_empty":
                # 2026-11-13 P2（F）：合法空是"源正常、区间内没数据"，属信息级；
                # 之前这里/debug + 末尾 else 会打出"返回错误（none）"的误导 WARNING。
                logger.info(
                    "%s 无数据（合法空 legal_empty）: data_type=%s, reason=%s",
                    adapter_name,
                    request.data_type,
                    note,
                )
            elif kind == "source_unsupported":
                logger.debug(
                    "%s 无数据（源不支持 %s）: data_type=%s, reason=%s",
                    adapter_name,
                    kind,
                    request.data_type,
                    note,
                )
            elif kind == "none":
                # 适配器没标注 error_kind、没错误、也没有数据：明确写清"未标注"，
                # 不再用"返回错误（none）"这种把人吓一跳的措辞。
                logger.info(
                    "%s 无数据且未标注 error_kind: data_type=%s, rows=%d, reason=%s",
                    adapter_name,
                    request.data_type,
                    len(result.data),
                    note,
                )
            elif kind == "deadline":
                logger.warning(
                    "%s 预算耗尽（deadline）: data_type=%s, reason=%s",
                    adapter_name,
                    request.data_type,
                    note,
                )
            else:
                followup_errors.append(note)
                followup_kinds.append(kind)
                logger.warning(
                    "%s 返回错误（%s）: data_type=%s, detail=%s",
                    adapter_name,
                    kind,
                    request.data_type,
                    note,
                )

        # 全链未完全成功时不得丢弃第一个带部分数据的适配器结果。
        if first_partial is not None:
            merged_kind = self._worst_error_kind(
                [first_partial.metadata.error_kind, *followup_kinds]
            )
            # error 只装"真错误"；truncated_ok/合法空的说明只进 reason，
            # 否则会把合法的截断重新伪装成 error 并触发上层 retry。
            error_parts = [first_partial.metadata.error, *followup_errors]
            merged_error = "; ".join(dict.fromkeys(p for p in error_parts if p)) or None
            reason_parts = [first_partial.metadata.reason, merged_error]
            merged_reason = "; ".join(dict.fromkeys(p for p in reason_parts if p)) or None
            log = (
                logger.error
                if merged_kind in CIRCUIT_FAILURE_ERROR_KINDS
                else logger.warning
            )
            log(
                "适配器仅返回 partial (tried=%s, data_type=%s, source=%s, "
                "rows=%d, error_kind=%s): %s",
                tried_adapters,
                request.data_type,
                first_partial.metadata.source,
                len(first_partial.data),
                merged_kind,
                merged_reason or "partial",
            )
            return FetchResult(
                data=first_partial.data,
                metadata=first_partial.metadata.model_copy(
                    update={
                        "error": merged_error,
                        "error_kind": merged_kind,
                        "reason": merged_reason,
                        "truncated": (
                            first_partial.metadata.truncated
                            or merged_kind == "truncated_ok"
                        ),
                    }
                ),
                raw_response=first_partial.raw_response,
            )

        # 所有适配器都没拿到数据
        if tried_adapters and attempt_results:
            kinds = [result.metadata.error_kind for result in attempt_results]
            worst_kind = self._worst_error_kind(kinds)
            notes = [
                result.metadata.reason
                or result.metadata.error
                or result.metadata.error_kind
                for result in attempt_results
            ]
            detail = "; ".join(dict.fromkeys(n for n in notes if n)) or "无数据"
            truncated = any(result.metadata.truncated for result in attempt_results)
            base = (last_result or attempt_results[-1]).metadata
            # 2026-11-13 P1-b：大请求整批 0 行 + legal_empty → 疑似风控空表。
            # 保守方向：宁可多试一次，也不让整批股票被永久写 missing。
            if (
                worst_kind == "legal_empty"
                and len(request.stock_codes) >= _SUSPICIOUS_EMPTY_BATCH_THRESHOLD
                and not any(result.data for result in attempt_results)
            ):
                marker = (
                    f"batch_suspicious_empty: "
                    f"requested={len(request.stock_codes)}, rows=0"
                )
                detail = f"{detail}; {marker}" if detail else marker
                worst_kind = "source_unavailable"
                for attempt_name, attempt in zip(
                    attempt_adapters, attempt_results, strict=False
                ):
                    if attempt.metadata.error_kind == "legal_empty":
                        self._record_circuit_failure(
                            attempt_name, request.data_type
                        )
            error: str | None
            if worst_kind in CIRCUIT_FAILURE_ERROR_KINDS:
                logger.error(
                    "所有来源均失败 (tried=%s, data_type=%s, codes=%s, "
                    "error_kind=%s): %s",
                    tried_adapters,
                    request.data_type,
                    codes_hint,
                    worst_kind,
                    detail,
                )
                error = detail
            elif worst_kind == "per_symbol_data_error":
                logger.warning(
                    "所有来源均无可用数据（个股/格式问题）(tried=%s, data_type=%s, "
                    "codes=%s): %s",
                    tried_adapters,
                    request.data_type,
                    codes_hint,
                    detail,
                )
                error = detail
            elif worst_kind == "deadline":
                logger.warning(
                    "所有来源预算耗尽 (tried=%s, data_type=%s, codes=%s): %s",
                    tried_adapters,
                    request.data_type,
                    codes_hint,
                    detail,
                )
                error = None
            else:
                logger.info(
                    "所有来源均无数据（合法缺失）(tried=%s, data_type=%s, codes=%s): %s",
                    tried_adapters,
                    request.data_type,
                    codes_hint,
                    detail,
                )
                error = None
            return FetchResult(
                data=[],
                metadata=base.model_copy(
                    update={
                        "confidence": "missing",
                        "error": error,
                        "error_kind": worst_kind,
                        "reason": detail,
                        "truncated": truncated,
                    }
                ),
            )

        # tried_adapters 为空：全链熔断（或候选源全部未注册/不可用）
        #
        # 2026-11-13 P1（D）：这里**绝不能**返回 source_unsupported。
        # 该 kind 属于"源本来就没有这只票的数据"，上层会直接结算进 missing 并
        # 删除 retry（_NON_RETRYABLE_ERROR_KINDS）；而"没注册/依赖缺失/全链冷却"
        # 是环境问题，一旦伪装成"该票没数据"就会造出永久假 missing。
        # 现在两种情况都返回可重试的 source_unavailable，并在错误串里写明原因。
        if tripped:
            cooldown = ", ".join(
                f"{name}={seconds:.0f}s" for name, seconds in tripped.items()
            )
            message = (
                f"adapter_cooldown: 全部候选源处于熔断冷却中 "
                f"(data_type={request.data_type}, candidates={priority_list}, "
                f"cooldown=[{cooldown}], tried=0)"
            )
            logger.warning("无可用适配器（全部熔断，可重试）: %s", message)
        else:
            message = (
                f"adapter_not_registered: 无可用适配器 "
                f"(data_type={request.data_type}, candidates={priority_list}, "
                f"registered={sorted(self._adapters)}, tried=0)"
            )
            logger.warning("无可用适配器（未注册/依赖缺失，可重试）: %s", message)
        kind: ErrorKind = "source_unavailable"
        return FetchResult(
            data=[],
            metadata=SourceMetadata(
                source=self._metadata_source(priority_list[0]),  # type: ignore[arg-type]
                fetch_time=datetime.now(UTC),
                raw_response_hash="",
                confidence="missing",
                error=message,
                error_kind=kind,
                reason=message,
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
        """Release reusable adapter sessions after a long-running update.

        Closing also resets ``_initialized``: the next fetch re-registers fresh
        adapter instances. Stateful adapters must clear their thread-local
        session handles in their own ``close()`` (TencentAdapter does).
        """
        with self._state_lock:
            for adapter in self._adapters.values():
                close = getattr(adapter, "close", None)
                if callable(close):
                    close()
            self._adapters.clear()
            self._initialized = False
            # 2026-11-12 P0：熔断状态只是**进程内隔离**的临时抑制窗口，
            # 与持久化 retry/missing 无关。长任务结束/管理器关闭后必须清空，
            # 否则下一轮更新会继承上一轮冷却（跨进程本来也不会共享）。
            self._circuit_breaker.clear()

    def recover_after_timeout(self) -> None:
        """Ask stateful sources to recreate their session before their next request."""
        self._ensure_initialized()
        for adapter in self._adapters.values():
            request_relogin = getattr(adapter, "request_relogin", None)
            if callable(request_relogin):
                request_relogin()
