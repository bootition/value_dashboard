"""财政部-中国国债收益率曲线适配器（P3 独立低频基准域）

来源：财政部官网收益率曲线页面使用的公开接口（reports/68 §4，2026-08-10 实测）：
- 接口 A 单日全曲线：
  https://yield.chinabond.com.cn/cbweb-czb-web/czb/czbQueryXy?zblx=xy&workTime=YYYY-MM-DD&qxmc=1
  返回 JSON 数组，元素含 ycDefId / ycDefName / worktime / seriesData=[[期限年,收益率%],...]（111 点）。
- 接口 B 单期限历史序列：
  https://yield.chinabond.com.cn/cbweb-czb-web/czb/czbQueryYz?zblx=yz&gjqx=<tenor>&startTime=&endTime=&locale=cn_ZH&qxmc=1
  返回 JSON 数组，seriesData=[[epoch_ms,收益率%],...]。

语义边界（reports/68 §4 硬门槛）：
- 未来日期先验拒绝：请求日或响应中的曲线点晚于本地时区（UTC+8）今天 → 视为合法缺失。
- 收益率必须为有限正数；畸形行丢弃；全部畸形 → missing（error=None，不触发熔断）。
- 网络异常 → error 非空（调用方记录 retry）。
- 独立实例与独立限速（默认 0.5s，不高于 2 req/s），不与其他数据源共享。
- 支持注入 httpx.Client（含 transport），便于离线 fixture 测试。
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.adapters.base import (
    BaseAdapter,
    FetchRequest,
    FetchResult,
)

logger = logging.getLogger(__name__)

__all__ = ["TreasuryMofAdapter", "KEY_TENORS", "CZB_CURVE_YIELD_TENOR_LABELS"]

# 页面可选关键期限（年）：3月/6月/1年/2年/3年/5年/7年/10年/30年
KEY_TENORS: tuple[float, ...] = (0.25, 0.5, 1, 2, 3, 5, 7, 10, 30)

# 期限 → 快照利差列名（用于筛选与导出）
CZB_CURVE_YIELD_TENOR_LABELS: dict[float, str] = {
    0.25: "div_yield_spread_0p25y",
    0.5: "div_yield_spread_0p5y",
    1: "div_yield_spread_1y",
    2: "div_yield_spread_2y",
    3: "div_yield_spread_3y",
    5: "div_yield_spread_5y",
    7: "div_yield_spread_7y",
    10: "div_yield_spread_10y",
    30: "div_yield_spread_30y",
}

_SURVEY_URL = "https://yield.chinabond.com.cn/cbweb-czb-web/czb/czbQueryXy"
_HISTORY_URL = "https://yield.chinabond.com.cn/cbweb-czb-web/czb/czbQueryYz"

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_CN_TZ = timezone(timedelta(hours=8))


def _cn_today() -> date:
    return datetime.now(_CN_TZ).date()


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # P3-10 修复（reports/73）：拒绝 NaN/±Infinity，收益率必须为有限正数
    if not math.isfinite(number):
        return None
    return number


class TreasuryMofAdapter(BaseAdapter):
    """财政部国债收益率曲线适配器（treasury_yield_curve）。

    独立限速实例：与其他数据源不共享请求间隔，默认 0.5s/请求（≤2 req/s）。

    用法::

        adapter = TreasuryMofAdapter(rate_limit=0.5)
        result = adapter.fetch(FetchRequest(
            data_type="treasury_yield_curve",
            extra_params={"mode": "daily", "work_time": "2026-08-10"},
        ))
        result = adapter.fetch(FetchRequest(
            data_type="treasury_yield_curve",
            extra_params={"mode": "history", "tenor": 10.0,
                          "start": "2006-01-01", "end": "2026-08-10"},
        ))
    """

    def __init__(
        self,
        rate_limit: float = 0.5,
        timeout: float = 20.0,
        session: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            name="czb_mof",  # type: ignore[arg-type]
            supported={"treasury_yield_curve"},  # type: ignore[arg-type]
            rate_limit=rate_limit,
        )
        self._timeout = timeout
        self._injected_session = session
        self._owned_client: httpx.Client | None = None

    # ─── 协议方法 ──────────────────────────────────────────────────

    def fetch(self, request: FetchRequest) -> FetchResult:
        mode = request.extra_params.get("mode") if isinstance(request.extra_params, dict) else None
        if mode == "daily":
            return self._fetch_daily(request)
        if mode == "history":
            return self._fetch_history(request)
        return self._missing_result("treasury_yield_curve 需要 extra_params.mode=daily|history")

    def _missing_result(self, reason: str) -> FetchResult:
        logger.info("%s", reason)
        metadata = self._make_metadata(
            raw_response=None, row_count=0, confidence="missing",
        )
        return FetchResult(data=[], metadata=metadata)

    def close(self) -> None:
        if self._owned_client is not None:
            self._owned_client.close()
            self._owned_client = None

    @property
    def client(self) -> httpx.Client | None:
        return self._injected_session or self._owned_client

    # ─── HTTP 客户端 ──────────────────────────────────────────────

    def _get_client(self) -> httpx.Client:
        if self._injected_session is not None:
            return self._injected_session
        if self._owned_client is None:
            self._owned_client = httpx.Client(
                timeout=httpx.Timeout(self._timeout),
                headers={
                    "User-Agent": _DEFAULT_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
        return self._owned_client

    # ─── 单日全曲线 ───────────────────────────────────────────────

    def _fetch_daily(self, request: FetchRequest) -> FetchResult:
        work_time = request.extra_params.get("work_time")
        if not work_time:
            return self._missing_result("daily 模式需要 work_time=YYYY-MM-DD")
        try:
            work_date = date.fromisoformat(str(work_time)[:10])
        except ValueError:
            return self._missing_result(f"非法 work_time: {work_time}")
        if work_date > _cn_today():
            return self._missing_result(f"未来日期拒绝: {work_date}")
        try:
            self._wait_rate_limit()
            resp = self._get_client().get(
                _SURVEY_URL,
                params={"zblx": "xy", "workTime": str(work_date), "qxmc": "1"},
            )
            resp.raise_for_status()
            raw_bytes = resp.content
        except Exception as error:
            logger.warning("查询 %s 国债曲线异常: %s", work_date, error)
            return self._make_result(
                data=[], raw_response=None, confidence="missing",
                error=str(error), api_version="v1",
            )

        try:
            payload = resp.json()
        except ValueError:
            return self._missing_result(
                f"国债曲线响应畸形: {work_date}",
            )

        rows = self._parse_daily_payload(payload, work_date)
        if not rows:
            return self._missing_result(
                f"国债曲线无可用数据（空或畸形响应）: {work_date}",
            )
        return self._make_result(
            data=rows,
            raw_response=raw_bytes,
            confidence="strict",
            api_version="v1",
        )

    def _parse_daily_payload(self, payload: Any, work_date: date) -> list[dict[str, Any]]:
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            return []
        series = payload[0].get("seriesData")
        if not isinstance(series, list):
            return []
        rows: list[dict[str, Any]] = []
        for pair in series:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            tenor = _to_float(pair[0])
            yield_value = _to_float(pair[1])
            if tenor is None or tenor < 0 or yield_value is None or yield_value <= 0:
                continue
            rows.append({
                "curve_date": str(work_date),
                "tenor_years": tenor,
                "yield_pct": yield_value,
            })
        return rows

    # ─── 单期限历史序列 ───────────────────────────────────────────

    def _fetch_history(self, request: FetchRequest) -> FetchResult:
        tenor = request.extra_params.get("tenor")
        if tenor is None:
            return self._missing_result("history 模式需要 tenor")
        try:
            tenor_value = float(tenor)
        except (TypeError, ValueError):
            return self._missing_result(f"非法 tenor: {tenor}")
        if tenor_value not in KEY_TENORS:
            return self._missing_result(f"不支持的期限: {tenor_value}")

        start = request.extra_params.get("start", "2006-01-01")
        end = request.extra_params.get("end") or str(_cn_today())
        try:
            start_date = date.fromisoformat(str(start)[:10])
            end_date = date.fromisoformat(str(end)[:10])
        except ValueError:
            return self._missing_result(f"非法日期区间: {start} ~ {end}")
        if end_date > _cn_today():
            end_date = _cn_today()

        try:
            self._wait_rate_limit()
            resp = self._get_client().get(
                _HISTORY_URL,
                params={
                    "zblx": "yz", "gjqx": str(tenor_value),
                    "startTime": str(start_date), "endTime": str(end_date),
                    "locale": "cn_ZH", "qxmc": "1",
                },
            )
            resp.raise_for_status()
            raw_bytes = resp.content
        except Exception as error:
            logger.warning("查询国债 %s 年历史序列异常: %s", tenor_value, error)
            return self._make_result(
                data=[], raw_response=None, confidence="missing",
                error=str(error), api_version="v1",
            )

        try:
            payload = resp.json()
        except ValueError:
            return self._missing_result(
                f"国债 {tenor_value} 年历史序列响应畸形",
            )

        rows = self._parse_history_payload(payload, tenor_value)
        if not rows:
            return self._missing_result(
                f"国债 {tenor_value} 年历史序列无可用数据（空或畸形响应）",
            )
        return self._make_result(
            data=rows,
            raw_response=raw_bytes,
            confidence="strict",
            api_version="v1",
        )

    def _parse_history_payload(self, payload: Any, tenor_value: float) -> list[dict[str, Any]]:
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            return []
        series = payload[0].get("seriesData")
        if not isinstance(series, list):
            return []
        today = _cn_today()
        rows: list[dict[str, Any]] = []
        for pair in series:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            epoch_ms = _to_float(pair[0])
            yield_value = _to_float(pair[1])
            if epoch_ms is None or yield_value is None or yield_value <= 0:
                continue
            curve_date = datetime.fromtimestamp(
                epoch_ms / 1000.0, tz=UTC
            ).astimezone(_CN_TZ).date()
            if curve_date > today:
                continue
            rows.append({
                "curve_date": str(curve_date),
                "tenor_years": tenor_value,
                "yield_pct": yield_value,
            })
        return rows
