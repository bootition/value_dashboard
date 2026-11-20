"""港股分红域回归（2026-09-04）

覆盖：
- A+H 映射：600941→00941、人工覆写、归一化、不做危险后缀剥离
- 分红方案解析：HKD/CNY 每股股息、无法识别时如实 None
- eastmoney_hk_dividend 适配器：列名兼容、合法空、网络错误
- HKDividendUpdater：单股原子替换、失败/空响应保留旧值、retry/missing
- AdapterManager 注册与独立 0.5s 限速
- 港股域不触碰 A 股 readiness 相关表
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from app.core.adapters.base import FetchRequest, FetchResult, SourceMetadata
from app.core.adapters.hk_dividend_adapter import (
    EastMoneyHKDividendAdapter,
    parse_hk_plan_explain,
)
from app.core.ah_hk_mapping import build_ah_hk_mapping, normalize_company_name
from app.core.hk_dividends import HKDividendUpdater
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed
from tests.support.seed_sqlite import SeedSqlite

# ─── 构造器 ──────────────────────────────────────────────────────────

def _seed_stock(
    duck: DuckDBStore,
    code: str,
    name: str,
    *,
    exchange: str = "SSE",
) -> None:
    # 留（第 4 类·多站点缺口）：这是**部分列 upsert**（`ON CONFLICT DO UPDATE SET name/
    duck.write_query(
        """INSERT INTO stock_meta (stock_code, name, exchange, is_listed)
           VALUES (?, ?, ?, TRUE)
           ON CONFLICT (stock_code) DO UPDATE SET
             name = excluded.name, exchange = excluded.exchange,
             is_listed = TRUE""",
        [code, name, exchange],
    )


def _result(
    data: list[dict],
    *,
    source: str = "eastmoney_hk_dividend",
    error: str | None = None,
    confidence: str = "approximate",
) -> FetchResult:
    raw = hashlib.sha256(
        repr(data).encode("utf-8") if not error else error.encode("utf-8"),
    ).hexdigest().encode("utf-8")
    return FetchResult(
        data=data,
        metadata=SourceMetadata(
            source=source,  # type: ignore[arg-type]
            fetch_time=datetime.now(UTC),
            raw_response_hash=raw.decode("ascii"),
            confidence=confidence,  # type: ignore[arg-type]
            error=error,
        ),
        raw_response=raw,
    )


class _FakeHKAdapter:
    """最小 fake adapter：按港股代码返回预设结果。"""

    def __init__(
        self,
        by_hk: dict[str, FetchResult] | None = None,
        *,
        error: str | None = None,
    ) -> None:
        self.by_hk = by_hk or {}
        self.error = error
        self.calls: list[str] = []

    def fetch(self, request: FetchRequest) -> FetchResult:
        hk_code = request.stock_codes[0]
        self.calls.append(hk_code)
        if self.error:
            return _result([], error=self.error, confidence="missing")
        return self.by_hk.get(hk_code, _result([], confidence="missing"))


# ─── A+H 映射 ───────────────────────────────────────────────────────

def test_schema_v20_creates_hk_dividends_table(
    duckdb_store: DuckDBStore,
) -> None:
    from app.core.storage.schema import DUCKDB_SCHEMA_VERSION

    assert DUCKDB_SCHEMA_VERSION >= 20
    columns = {
        row["column_name"]
        for row in duckdb_store.read_query(
            "SELECT column_name FROM duckdb_columns() WHERE table_name = 'hk_dividends'",
        )
    }
    assert {
        "stock_code", "ex_date", "announcement_date", "report_period",
        "plan_explain", "dividend_per_share_hkd", "dividend_per_share_cny",
        "transfer_end_date", "dividend_date", "source", "fetch_time",
        "raw_response_hash", "confidence", "batch_id",
    } <= columns
    versions = duckdb_store.read_query(
        "SELECT MAX(version) AS v FROM schema_migrations",
    )[0]["v"]
    assert versions >= 20


def test_ah_hk_mapping_maps_china_mobile_and_manual_override(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_stock(duckdb_store, "600941", "中国移动")
    _seed_stock(duckdb_store, "601375", "中原证券")

    report = build_ah_hk_mapping(duckdb_store)

    assert report["status"] == "ok"
    assert report["mapped_stocks"] == 2
    assert report["mapping"]["600941"] == {
        "hk_code": "00941",
        "hk_name": "中国移动",
        "a_name": "中国移动",
        "match_type": "exact_name",
    }
    assert report["mapping"]["601375"] == {
        "hk_code": "01375",
        "hk_name": "中州证券",
        "a_name": "中原证券",
        "match_type": "manual_override",
    }


def test_ah_hk_mapping_normalizes_full_width_and_whitespace(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_stock(duckdb_store, "000002", "万  科Ａ", exchange="SZSE")

    assert normalize_company_name("万  科Ａ") == "万科A"
    report = build_ah_hk_mapping(
        duckdb_store,
        ah_spot_rows=[
            {"hk_code": "02202", "hk_name": "万科企业"},
            {"hk_code": "00941", "hk_name": " 中国移动 "},
        ],
    )
    # 02202 走人工覆写；00941 仅做归一化精确匹配，不猜后缀。
    assert report["mapping"]["000002"]["hk_code"] == "02202"
    assert "600941" not in report["mapping"]


def test_ah_hk_mapping_does_not_strip_ambiguous_suffixes(
    duckdb_store: DuckDBStore,
) -> None:
    _seed_stock(duckdb_store, "600036", "招商银行")
    _seed_stock(duckdb_store, "600999", "招商证券")

    report = build_ah_hk_mapping(
        duckdb_store,
        ah_spot_rows=[
            {"hk_code": "03968", "hk_name": "招商银行"},
            {"hk_code": "06099", "hk_name": "招商证券"},
        ],
    )

    assert report["mapping"]["600036"]["hk_code"] == "03968"
    assert report["mapping"]["600999"]["hk_code"] == "06099"


# ─── 分红方案解析 ─────────────────────────────────────────────────────

def test_parse_hk_plan_explain_cny_with_hkd_equivalent() -> None:
    assert parse_hk_plan_explain("每股派息2.51元(相当于港币2.9003元)") == (
        pytest.approx(2.9003), pytest.approx(2.51),
    )


def test_parse_hk_plan_explain_hkd_only() -> None:
    assert parse_hk_plan_explain("每股派息港币2.4元") == (
        pytest.approx(2.4), None,
    )


def test_parse_hk_plan_explain_actual_eastmoney_variants() -> None:
    assert parse_hk_plan_explain(
        "每股派人民币2.51元(相当于港币2.9003元)",
    ) == (pytest.approx(2.9003), pytest.approx(2.51))
    assert parse_hk_plan_explain(
        "每股派港币1.582元(相当于人民币1.322元)",
    ) == (pytest.approx(1.582), pytest.approx(1.322))


def test_parse_hk_plan_explain_unrecognized_is_none() -> None:
    assert parse_hk_plan_explain("每10股派息1.5元") == (None, None)
    assert parse_hk_plan_explain("") == (None, None)
    assert parse_hk_plan_explain(None) == (None, None)


def test_parse_hk_plan_explain_does_not_treat_fx_rate_as_dividend() -> None:
    """2026-11-14 修复：汇率注记里的「港币1.00元」曾被当成股息（89 行受影响）。

    东财会把换算汇率写进方案串，例如
    「每股派人民币0.07元(汇率港币1.00元:人民币0.817026元)(相当于港币0.0856元)」。
    旧实现用 `_PAREN_HKD_RE.search()` 左→右扫，先撞上汇率里的 1.00，
    于是把港币股息记成 1.0（真实 0.0856，虚高约 11.7 倍；极端样本达 100 倍）。
    """
    assert parse_hk_plan_explain(
        "每股派人民币0.07元(汇率港币1.00元:人民币0.817026元)(相当于港币0.0856元)",
    ) == (pytest.approx(0.0856), pytest.approx(0.07))
    # 汇率写在后面的形态（「,汇率为0.84176)」）
    assert parse_hk_plan_explain(
        "每股派人民币0.06元(相当于港币0.071279元,汇率为0.84176)",
    ) == (pytest.approx(0.071279), pytest.approx(0.06))
    # 反向标注形态（汇率人民币…:港币1.00元）
    assert parse_hk_plan_explain(
        "每股派人民币0.05元(汇率人民币0.796442元:港币1.00元)(相当于港币0.0627元)",
    ) == (pytest.approx(0.0627), pytest.approx(0.05))

# ─── 真实源样本长驻 fixture（J4 / reports/120 §5）──────────────────────
#
# 2026-11-14 从正式库只读抽取：`hk_dividends` 中 `plan_explain LIKE '%汇率%'`
# 共 89 条。下面 16 条覆盖当时全部已识别形态（汇率注记在前/在后、港币或
# 人民币在前、带「为」/无「为」、错别字「换人民币」、传统「港元」等），
# 只保留方案原文（不含股票代码，已脱敏）。
#
# 防回归目标：旧解析器会把汇率注记里的「港币1.00元」当成股息（hkd=1.0），
# 因此每条都断言 hkd 落在真实每股股息区间、且 hkd ≈ cny / (CNY per HKD)。
_HK_FX_PLAN_FIXTURES = (
    # (plan_explain 原文, 汇率[CNY/HKD], 期望 hkd, 期望 cny)
    ("每股派人民币0.041元(相当于港币0.04885元,汇率0.83928)", 0.83928, 0.04885, 0.041),
    ("每股派人民币0.08元(相当于港币0.094624元,汇率0.84545)", 0.84545, 0.094624, 0.08),
    (
        "每股派人民币0.05元(汇率港币1.00元:人民币0.812153元)(相当于港币0.0616元)",
        0.812153, 0.0616, 0.05,
    ),
    (
        "每股派人民币0.048元(汇率港币1.00元:人民币0.813196元)(相当于港币0.0590元)",
        0.813196, 0.059, 0.048,
    ),
    # 1 CNY = 1.22957 HKD：汇率方向与常见写法相反，需换算成 CNY/HKD。
    (
        "每股派人民币0.1元(汇率人民币1.00元:港币1.22957元)(相当于港币0.1229元)",
        1.0 / 1.22957, 0.1229, 0.1,
    ),
    (
        "每股派人民币0.13元(汇率港币1.00元:人民币0.79899元) (相当于港币0.1627元)",
        0.79899, 0.1627, 0.13,
    ),
    (
        "每股派人民币0.06元(汇率人民币0.799478元:港币1.00元)(相当于港币0.07504元)",
        0.799478, 0.07504, 0.06,
    ),
    # 少左括号的历史脏数据形态。
    ("每股派人民币0.068元相当于港币0.079781元,汇率0.852334)", 0.852334, 0.079781, 0.068),
    ("每股派人民币0.30元(相当于港币0.377元,汇率0.795738)", 0.795738, 0.377, 0.3),
    ("每股派人民币0.06元(相当于港币0.071279元,汇率为0.84176)", 0.84176, 0.071279, 0.06),
    ("每股派人民币0.3元(相当于港币0.338233元,汇率0.886962)", 0.886962, 0.338233, 0.3),
    ("每股派人民币0.0717元(相当于港元0.0849元,汇率0.844662)", 0.844662, 0.0849, 0.0717),
    ("每股派人民币0.15元(相当于港币0.1770元,汇率0.8474)", 0.8474, 0.177, 0.15),
    # 东财脏数据错别字：「换人民币」。
    (
        "每股派人民币0.065元(汇率港币1.00元:换人民币0.79591元)(相当于港币0.082元)",
        0.79591, 0.082, 0.065,
    ),
    (
        "每股派人民币0.32056元(汇率港币1.00元:人民币0.816102元)(相当于港币0.3927元)",
        0.816102, 0.3927, 0.32056,
    ),
    (
        "每股派人民币0.07元(汇率港币1.00元:人民币0.817026元)(相当于港币0.0856元)",
        0.817026, 0.0856, 0.07,
    ),
)


def _cny_per_hkd(plan_explain: str) -> float:
    """Extract the CNY-per-HKD rate from an Eastmoney plan string."""
    directional_hkd = re.search(
        r"汇率\s*港币\s*1\.00\s*(?:港)?元?\s*[:：]\s*换?人民币\s*([0-9.]+)",
        plan_explain,
    )
    if directional_hkd:
        return float(directional_hkd.group(1))
    directional_cny = re.search(
        r"汇率\s*人民币\s*([0-9.]+)\s*元?\s*[:：]\s*港币\s*1\.00",
        plan_explain,
    )
    if directional_cny:
        return float(directional_cny.group(1))
    reverse_cny = re.search(
        r"汇率\s*人民币\s*1\.00\s*元?\s*[:：]\s*港币\s*([0-9.]+)",
        plan_explain,
    )
    if reverse_cny:
        return 1.0 / float(reverse_cny.group(1))
    trailing = re.search(r"汇率(?:为)?\s*([0-9.]+)", plan_explain)
    if trailing:
        return float(trailing.group(1))
    raise AssertionError(f"fixture 缺少可提取的汇率: {plan_explain!r}")


@pytest.mark.parametrize(
    ("plan_explain", "expected_rate", "expected_hkd", "expected_cny"),
    _HK_FX_PLAN_FIXTURES,
    ids=[f"real-fx-{index:02d}" for index in range(len(_HK_FX_PLAN_FIXTURES))],
)
def test_parse_hk_plan_explain_real_fx_samples(
    plan_explain: str,
    expected_rate: float,
    expected_hkd: float,
    expected_cny: float,
) -> None:
    """89 条真实含「汇率」样本的代表形态：汇率不能再次被当成股息。"""
    rate = _cny_per_hkd(plan_explain)
    assert rate == pytest.approx(expected_rate, rel=1e-9)

    hkd, cny = parse_hk_plan_explain(plan_explain)

    assert hkd is not None and cny is not None
    assert hkd == pytest.approx(expected_hkd, rel=1e-9)
    assert cny == pytest.approx(expected_cny, rel=1e-9)
    # 真实每股股息区间：汇率 1.00 若被误当股息会立刻越界。
    assert 0.005 <= hkd <= 0.6
    assert 0.01 <= cny <= 0.5
    # hkd ≈ cny / (CNY per HKD)，±2% 容忍源侧四舍五入。
    assert hkd == pytest.approx(cny / rate, rel=0.02)
    assert hkd != pytest.approx(1.0)


# ─── 适配器 ─────────────────────────────────────────────────────────

class _FakeAK:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stock_hk_dividend_payout_em(self, symbol: str) -> pd.DataFrame:
        self.calls.append(symbol)
        return pd.DataFrame([
            {
                "最新公告日期": "2026-08-13",
                "财政年度": "2026",
                "分红方案": "每股派人民币2.51元(相当于港币2.9003元)",
                "分配类型": "中期分红",
                "除净日": "2026-08-25",
                "截至过户日": "2026/08/27-2026/08/31",
                "发放日": "2026-09-30",
            },
            {
                "最新公告日期": "2025-05-21",
                "财政年度": "2025",
                "分红方案": "每股派息港币2.4元",
                "分配类型": "未确定",
                "除净日": "2025-06-05",
                "截至过户日": "2025/06/09-2025/06/11",
                "发放日": "2025-06-24",
            },
        ])


def test_hk_dividend_adapter_parses_current_column_names(monkeypatch) -> None:
    import app.core.adapters.hk_dividend_adapter as mod

    fake = _FakeAK()
    monkeypatch.setattr(mod, "ak", fake)
    adapter = EastMoneyHKDividendAdapter(rate_limit=0)
    result = adapter.fetch(FetchRequest(
        data_type="hk_dividends", stock_codes=["00941"],
    ))

    assert result.metadata.error is None
    assert result.metadata.confidence == "approximate"
    assert result.metadata.source == "eastmoney_hk_dividend"
    assert fake.calls == ["00941"]
    assert len(result.data) == 2
    first, second = result.data
    assert first["stock_code"] == "00941"
    assert first["announcement_date"] == "2026-08-13"
    assert first["report_period"] == "2026"
    assert first["ex_date"] == "2026-08-25"
    assert first["transfer_end_date"] == "2026/08/27-2026/08/31"
    assert first["dividend_date"] == "2026-09-30"
    assert first["dividend_per_share_hkd"] == pytest.approx(2.9003)
    assert first["dividend_per_share_cny"] == pytest.approx(2.51)
    assert second["dividend_per_share_hkd"] == pytest.approx(2.4)
    assert second["dividend_per_share_cny"] is None
    assert len(result.metadata.raw_response_hash) == 64


def test_hk_dividend_adapter_empty_is_legal_missing(monkeypatch) -> None:
    import app.core.adapters.hk_dividend_adapter as mod

    class _EmptyAK:
        def stock_hk_dividend_payout_em(self, symbol: str) -> pd.DataFrame:
            return pd.DataFrame()

    monkeypatch.setattr(mod, "ak", _EmptyAK())
    result = EastMoneyHKDividendAdapter(rate_limit=0).fetch(FetchRequest(
        data_type="hk_dividends", stock_codes=["00941"],
    ))

    assert result.data == []
    assert result.metadata.error is None
    assert result.metadata.confidence == "missing"


def test_hk_dividend_adapter_network_error_is_error(monkeypatch) -> None:
    import app.core.adapters.hk_dividend_adapter as mod

    class _BrokenAK:
        def stock_hk_dividend_payout_em(self, symbol: str) -> pd.DataFrame:
            raise ConnectionError("network down")

    monkeypatch.setattr(mod, "ak", _BrokenAK())
    result = EastMoneyHKDividendAdapter(rate_limit=0).fetch(FetchRequest(
        data_type="hk_dividends", stock_codes=["00941"],
    ))

    assert result.data == []
    assert "network down" in (result.metadata.error or "")


# ─── Updater：原子替换 / 保旧值 / retry / missing ────────────────────

def _hk_rows() -> list[dict]:
    return [
        {
            "stock_code": "00941",
            "announcement_date": "2026-08-13",
            "report_period": "2026",
            "plan_explain": "每股派人民币2.51元(相当于港币2.9003元)",
            "ex_date": "2026-08-25",
            "transfer_end_date": "2026/08/27-2026/08/31",
            "dividend_date": "2026-09-30",
            "dividend_per_share_hkd": 2.9003,
            "dividend_per_share_cny": 2.51,
        },
    ]


def _seed_hk_dividend_row(duck: DuckDBStore) -> None:
    # `batch_id="old-batch"` 是**用例的论点**，不是样板：下面的原子替换用例断言
    # `batch_id != "old-batch"`（更新必须写入新批次，不是留着旧行）。所以显式传，
    # 别让工厂的确定性默认值把它悄悄换掉 —— 那样断言会变成恒真。
    # `fetch_time` 原来是库侧 `CURRENT_TIMESTAMP` → 显式 `trace_now=True`。
    Seed(duck, trace_now=True).hk_dividends(
        "00941",
        [
            (
                "2004-06-10",
                "每股派港币0.2元",
                0.2,
                None,
                {
                    "announcement_date": "2004-03-19",
                    "report_period": "2003",
                    "transfer_end_date": "2004/06/14-2004/06/16",
                    "dividend_date": "2004-06-21",
                    "confidence": "approximate",
                    "batch_id": "old-batch",
                },
            )
        ],
    )


def test_hk_dividend_updater_atomic_replace_without_touching_readiness(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, "600941", "中国移动")
    _seed_hk_dividend_row(duckdb_store)
    # 留（第 6 类·库侧相对时间）：`calculated_at` 写库侧 `CURRENT_TIMESTAMP`，
    duckdb_store.write_query(
        """INSERT INTO indicator_snapshot
           (stock_code, report_date, latest_close, calculated_at)
           VALUES ('600941', '2025-12-31', 1.0, CURRENT_TIMESTAMP)""",
    )
    fake = _FakeHKAdapter(by_hk={"00941": _result(_hk_rows())})
    updater = HKDividendUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=fake,
    )
    mapping = {"600941": {"hk_code": "00941"}}

    report = updater.update_stock("600941", mapping=mapping)

    assert report["status"] == "success"
    assert report["event_rows"] == 1
    assert fake.calls == ["00941"]
    rows = duckdb_store.read_query(
        "SELECT stock_code, plan_explain, batch_id, source FROM hk_dividends"
    )
    assert len(rows) == 1
    assert rows[0]["batch_id"] != "old-batch"
    assert rows[0]["source"] == "eastmoney_hk_dividend"
    # 独立低频域：不触碰 A 股 readiness 输入表
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS n FROM stock_meta WHERE stock_code='600941'",
    )[0]["n"] == 1
    assert duckdb_store.read_query(
        """SELECT latest_close FROM indicator_snapshot
           WHERE stock_code='600941' AND report_date='2025-12-31'""",
    )[0]["latest_close"] == 1.0


def test_hk_dividend_updater_error_keeps_old_and_records_retry(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, "600941", "中国移动")
    _seed_hk_dividend_row(duckdb_store)
    fake = _FakeHKAdapter(error="network down")
    updater = HKDividendUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=fake,
    )

    report = updater.update_stock("600941", mapping={"600941": {"hk_code": "00941"}})

    assert report["status"] == "failed"
    assert report["retained"] is True
    assert duckdb_store.read_query("SELECT COUNT(*) AS n FROM hk_dividends")[0]["n"] == 1
    retry = sqlite_store.query(
        "SELECT stock_code, data_type, adapter FROM retry_list WHERE data_type='hk_dividends'",
    )
    assert retry and retry[0]["stock_code"] == "600941"
    assert retry[0]["adapter"] == "eastmoney_hk_dividend"


def test_hk_dividend_updater_empty_keeps_old_and_records_missing(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    _seed_stock(duckdb_store, "600941", "中国移动")
    _seed_hk_dividend_row(duckdb_store)
    fake = _FakeHKAdapter(by_hk={"00941": _result([], confidence="missing")})
    updater = HKDividendUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=fake,
    )

    report = updater.update_stock("600941", mapping={"600941": {"hk_code": "00941"}})

    assert report["status"] == "success"
    assert report["retained"] is True
    assert duckdb_store.read_query("SELECT COUNT(*) AS n FROM hk_dividends")[0]["n"] == 1
    missing = sqlite_store.query(
        """SELECT stock_code, field_name, reason_code FROM missing_list
           WHERE field_name='hk_dividends' AND resolved_at IS NULL""",
    )
    assert missing and missing[0]["stock_code"] == "600941"
    assert missing[0]["reason_code"] == "source_empty"


# ─── Manager 注册与限速 ───────────────────────────────────────────────

def test_manager_registers_hk_dividend_adapter_with_half_second_limit() -> None:
    from app.core.adapters import manager as manager_module
    from app.core.adapters.manager import AdapterManager

    assert "eastmoney_hk_dividend" in manager_module.KNOWN_ADAPTERS
    assert manager_module.DEFAULT_ADAPTER_PRIORITY["hk_dividends"] == [
        "eastmoney_hk_dividend",
    ]
    assert manager_module.DEFAULT_ADAPTER_RATE_LIMITS["eastmoney_hk_dividend"] == 0.5

    manager = AdapterManager()
    manager._ensure_initialized()
    adapter = manager.get_adapter("eastmoney_hk_dividend")
    assert adapter is not None
    assert adapter.rate_limit_interval == 0.5
    assert adapter.supported_data_types == {"hk_dividends"}


# ─── reparse 维护脚本（J4）────────────────────────────────────────────

def _seed_reparse_row(
    duck: DuckDBStore,
    stock_code: str,
    ex_date: str,
    plan_explain: str,
    hkd: float | None,
    cny: float | None,
) -> None:
    # `fetch_time` 原来是库侧 `CURRENT_TIMESTAMP` → 显式 `trace_now=True`。
    Seed(duck, trace_now=True).hk_dividends(
        stock_code,
        [(ex_date, plan_explain, hkd, cny)],
        confidence="approximate",
    )


def test_reparse_script_dry_run_does_not_modify_temp_db(
    duckdb_store: DuckDBStore,
) -> None:
    from scripts.reparse_hk_dividends import execute_reparse

    _seed_reparse_row(
        duckdb_store, "00941", "2026-08-25",
        "每股派人民币0.07元(汇率港币1.00元:人民币0.817026元)(相当于港币0.0856元)",
        1.0, 0.07,
    )
    _seed_reparse_row(
        duckdb_store, "00941", "2026-09-25", "每10股转1股", None, None,
    )

    plan = execute_reparse(duckdb_store, write=False)

    assert plan.changed_rows == 1
    assert plan.unparsed_rows == 1
    assert plan.changes[0].new_hkd == pytest.approx(0.0856)
    rows = duckdb_store.read_query(
        """SELECT plan_explain, dividend_per_share_hkd, dividend_per_share_cny
           FROM hk_dividends WHERE ex_date = '2026-08-25'""",
    )
    assert rows[0]["dividend_per_share_hkd"] == 1.0, "dry-run 不得改数据"
    assert rows[0]["dividend_per_share_cny"] == pytest.approx(0.07)


def test_reparse_script_writes_only_parsed_side_and_skips_unparsed(
    duckdb_store: DuckDBStore,
) -> None:
    from scripts.reparse_hk_dividends import execute_reparse

    _seed_reparse_row(
        duckdb_store, "00941", "2026-08-25",
        "每股派人民币0.07元(汇率港币1.00元:人民币0.817026元)(相当于港币0.0856元)",
        1.0, 0.07,
    )
    _seed_reparse_row(
        duckdb_store, "00941", "2026-09-25", "每10股转1股", None, None,
    )

    execute_reparse(duckdb_store, write=True)

    fixed = duckdb_store.read_query(
        "SELECT dividend_per_share_hkd, dividend_per_share_cny "
        "FROM hk_dividends WHERE ex_date = '2026-08-25'",
    )[0]
    assert fixed["dividend_per_share_hkd"] == pytest.approx(0.0856)
    assert fixed["dividend_per_share_cny"] == pytest.approx(0.07)
    unparsed = duckdb_store.read_query(
        "SELECT dividend_per_share_hkd, dividend_per_share_cny "
        "FROM hk_dividends WHERE ex_date = '2026-09-25'",
    )[0]
    assert unparsed["dividend_per_share_hkd"] is None
    assert unparsed["dividend_per_share_cny"] is None


def test_hk_dividend_empty_with_a_share_dividend_is_not_legal_empty(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """J4：A 股已实施分红 + 港股源 0 行 → source_incomplete，不是 legal_empty。"""
    _seed_stock(duckdb_store, "600941", "中国移动")
    Seed(duckdb_store).dividends("600941", [("2024-06-30", "2024-06-01", 2.51)])
    fake = _FakeHKAdapter(by_hk={"00941": _result([], confidence="missing")})
    updater = HKDividendUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=fake,
    )

    report = updater.update_stock("600941", mapping={"600941": {"hk_code": "00941"}})

    assert report["status"] == "success"
    assert report["retained"] is True
    assert report["reason"] == "source_incomplete"
    missing = sqlite_store.query(
        """SELECT reason_code FROM missing_list
           WHERE stock_code = '600941' AND field_name = 'hk_dividends'
             AND resolved_at IS NULL""",
    )
    assert missing and missing[0]["reason_code"] == "source_incomplete"
    assert all(row["reason_code"] != "source_empty" for row in missing)
    assert duckdb_store.read_query(
        "SELECT COUNT(*) AS n FROM hk_dividends WHERE stock_code = '00941'",
    )[0]["n"] == 0


def test_hk_dividend_empty_without_a_share_dividend_stays_legal_empty(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """对照：A 股也没有已实施分红时，才允许判 source_empty。"""
    _seed_stock(duckdb_store, "600941", "中国移动")
    # 未实施（除净日在未来）不算"已实施"。日期在调用点按「今天 + 30 天」算，
    # 与原来的 SQL 侧 `CURRENT_DATE + INTERVAL '30 days'` 同一口径（同一本机日期）。
    today = date.today()
    Seed(duckdb_store).dividends("600941", [(today + timedelta(days=30), today, 1.0)])
    fake = _FakeHKAdapter(by_hk={"00941": _result([], confidence="missing")})
    updater = HKDividendUpdater(
        duck=duckdb_store, sqlite=sqlite_store, adapter=fake,
    )

    report = updater.update_stock("600941", mapping={"600941": {"hk_code": "00941"}})

    assert report["reason"] == "source_empty"
    missing = sqlite_store.query(
        """SELECT reason_code FROM missing_list
           WHERE stock_code = '600941' AND field_name = 'hk_dividends'
             AND resolved_at IS NULL""",
    )
    assert missing[0]["reason_code"] == "source_empty"


def test_repair_hk_legal_empty_only_flips_mislabeled_rows(
    duckdb_store: DuckDBStore,
    sqlite_store: SQLiteStore,
) -> None:
    """D3：只把"A 股有已实施分红"的 source_empty 改为 source_incomplete。"""
    from scripts.repair_hk_legal_empty import find_mislabeled_rows, repair_rows

    Seed(duckdb_store).dividends("600941", [("2024-06-30", "2024-06-01", 2.51)])
    # 原来卡在缺口 B（`missing_list` 无 SQLite 侧方法）。
    seed_sqlite = SeedSqlite(sqlite_store)
    for code in ("600941", "000001"):
        seed_sqlite.missing_list(code, "hk_dividends", "source_empty")

    mislabeled, genuinely_empty = find_mislabeled_rows(duckdb_store, sqlite_store)

    assert [row["stock_code"] for row in mislabeled] == ["600941"]
    assert [row["stock_code"] for row in genuinely_empty] == ["000001"]
    # dry-run 阶段：调用方不执行 repair_rows，数据库保持原样。
    before = sqlite_store.query(
        "SELECT stock_code, reason_code FROM missing_list ORDER BY stock_code",
    )
    assert before == [
        {"stock_code": "000001", "reason_code": "source_empty"},
        {"stock_code": "600941", "reason_code": "source_empty"},
    ]

    updated = repair_rows(sqlite_store, mislabeled)

    assert updated == 1
    after = {
        row["stock_code"]: row["reason_code"]
        for row in sqlite_store.query(
            "SELECT stock_code, reason_code FROM missing_list",
        )
    }
    assert after == {"000001": "source_empty", "600941": "source_incomplete"}
    # 不写 retry_list：hk_dividends 没有 refetch_one 路径，靠 update_all 重抓。
    retry = sqlite_store.query(
        "SELECT COUNT(*) AS n FROM retry_list WHERE data_type = 'hk_dividends'",
    )
    assert retry[0]["n"] == 0
