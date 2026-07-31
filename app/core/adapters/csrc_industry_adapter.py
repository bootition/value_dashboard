"""CSRC 行业分类适配器（CNINFO 巨潮资讯，证监会口径）

数据来源：CNINFO webapi p_stock2110（AKShare stock_industry_change_cninfo）
- 逐股返回行业归属变更历史
- F008C「最新记录标识」区分当前归属（=1 为当前记录）
- 字段：行业门类（一级）、行业大类（二级）

说明（PRD §24 行业决策）：
- 申万为商业许可数据，免费源不可稳定获取，V1 不再采用。
- CSRC（证监会《上市公司行业分类指引》）口径可由 CNINFO 免费获取，
  作为当前行业归属与行业排名的唯一口径。
- 行业归属变化频率极低，适合低频全量刷新（初始化 + 定期）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.adapters.base import (
    BaseAdapter,
    FetchRequest,
    FetchResult,
)

logger = logging.getLogger(__name__)

__all__ = ["CSRCIndustryAdapter"]


class CSRCIndustryAdapter(BaseAdapter):
    """CSRC（证监会）行业分类适配器

    封装 AKShare stock_industry_change_cninfo，逐股查询当前行业归属。
    全市场查询（stock_codes 为空）时按本地股票列表逐股扫描。
    """

    def __init__(self, rate_limit: float = 1.0, timeout: float = 30.0) -> None:
        super().__init__(
            name="cninfo",  # 数据源自 CNINFO webapi
            supported={"csrc_industry"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout

    # ─── 协议方法 ──────────────────────────────────────────────────

    def fetch(self, request: FetchRequest) -> FetchResult:
        if request.data_type != "csrc_industry":
            return self._make_empty_result(
                reason=f"CNINFO 不支持的数据类型: {request.data_type}",
                confidence="missing",
            )

        codes = request.stock_codes
        if not codes:
            return self._make_empty_result(
                reason="CSRC 行业查询需要股票代码列表（逐股查询接口，不支持全市场批量）",
                confidence="missing",
            )

        all_data: list[dict[str, Any]] = []
        errors: list[str] = []
        for code in codes:
            try:
                rows = self._fetch_stock_industry(code)
                all_data.extend(rows)
            except Exception as e:
                logger.exception("查询 %s CSRC 行业异常", code)
                errors.append(f"{code}: {e}")

        return self._finalize_result(
            data=all_data,
            label="csrc_industry",
            stock_count=len(codes),
            errors=errors,
        )

    # ─── 核心查询 ─────────────────────────────────────────────────

    def _fetch_stock_industry(self, stock_code: str) -> list[dict[str, Any]]:
        """查询单只股票的当前 CSRC 行业归属。

        返回规范化记录；无当前归属时返回空列表（不伪造）。
        """
        try:
            import akshare as ak
        except ImportError:
            raise RuntimeError("akshare 不可用，无法查询 CSRC 行业")

        # 变更历史按日期倒序（接口默认返回全部历史），取最新一条当前记录。
        # F008C「最新记录标识」=1 表示当前归属。
        df = ak.stock_industry_change_cninfo(symbol=stock_code)
        if df is None or df.empty:
            return []

        rows: list[dict[str, Any]] = []
        for _, record in df.iterrows():
            is_latest = record.get("最新记录标识")
            # 接口未返回该列或值为空时，退化为取第一条（按变更日期倒序）
            if is_latest is not None and str(is_latest) != "1":
                continue
            rows.append({
                "stock_code": stock_code,
                "csrc_l1": record.get("行业门类"),
                "csrc_l2": record.get("行业大类"),
                "as_of_date": str(record.get("变更日期") or ""),
            })
            break  # 只取最新一条当前归属
        return rows

    # ─── 结果构建辅助 ─────────────────────────────────────────────

    def _finalize_result(
        self,
        data: list[dict[str, Any]],
        label: str,
        stock_count: int,
        errors: list[str] | None = None,
    ) -> FetchResult:
        import json

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
