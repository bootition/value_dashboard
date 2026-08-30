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
from datetime import date
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
            # P0-1修复: 适配器名与 CNINFO 公告/分红适配器区分。
            # AdapterManager 以 name 为字典键，同名会互相覆盖；
            # cninfo 保留给 announcements/dividends，本适配器使用 cninfo_csrc。
            name="cninfo_csrc",  # 数据源自 CNINFO webapi
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
            self._wait_rate_limit()
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

        2026-08-30 修复：
        - akshare 默认 end_date=20220713，导致 2022-07 以后上市/变更的股票
          永远查不到记录；这里显式查询到今天的变更历史。
        - 接口一次返回多种行业分类标准（巨潮/申万/中证/证监会等），旧实现
          取第一条导致 csrc_l1/csrc_l2 混入非证监会口径。现在只保留
          “证监会”分类标准记录，并按变更日期取最新一条。
        """
        try:
            import akshare as ak
        except ImportError as error:
            raise RuntimeError("akshare 不可用，无法查询 CSRC 行业") from error

        try:
            df = ak.stock_industry_change_cninfo(
                symbol=stock_code,
                start_date="19900101",
                end_date=date.today().strftime("%Y%m%d"),
            )
        except (KeyError, TypeError) as e:
            # 新发行/无行业变更历史的股票：CNINFO 无返回列（akshare 内部 KeyError），
            # 属"该股无数据"而非源故障——如实返回空，避免逐股失败误触熔断。
            logger.info("%s CNINFO 无行业变更历史，按缺失处理: %s", stock_code, e)
            return []
        if df is None or df.empty:
            return []

        # 不同 akshare 版本对该列的中文名不同：1.18.81 为“分类标准”，
        # 1.18.64 为“行业标准”。
        standard_col = next(
            (column for column in ("分类标准", "行业标准") if column in df.columns),
            None,
        )
        if standard_col is None:
            # 无法识别分类标准列时宁可留空重试，也不能混入其他行业口径。
            logger.warning("%s CSRC 响应缺少分类标准列，按缺失处理", stock_code)
            return []
        csrc_rows: list[dict[str, Any]] = []
        for _, record in df.iterrows():
            if "证监会" not in str(record.get(standard_col) or ""):
                continue
            csrc_l1 = record.get("行业门类")
            csrc_l2 = record.get("行业大类")
            if not isinstance(csrc_l1, str) or not isinstance(csrc_l2, str):
                continue
            normalized = {
                "stock_code": stock_code,
                "csrc_l1": csrc_l1,
                "csrc_l2": csrc_l2,
                "as_of_date": str(record.get("变更日期") or ""),
            }
            csrc_rows.append(normalized)
        if not csrc_rows:
            return []
        csrc_rows.sort(key=lambda row: row["as_of_date"], reverse=True)
        return [csrc_rows[0]]

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
