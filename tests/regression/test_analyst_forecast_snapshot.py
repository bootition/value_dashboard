"""一致预期每日快照域（schema v48，reports/139 §2.2 #13）的自证用例。

覆盖的是**最容易静默出错**的四件事（全部离线，不打网络）：

1. 年度必须从列名解析（`2029预测每股收益` 这种滚动年度不能被硬编码丢掉）；
2. 源端重复行（akshare 页间重叠，实测 442 只各出 2 次）必须去重，否则
   「计划行数 ≠ 回读行数」，写后自证会把正常写入误判成半截状态；
3. EPS 为空不写 0（D49）—— 但坏行超过 R6 阈值时必须整批拒绝，不能写半张表；
4. 同日重跑 = 等价覆盖（`replace_scope=snapshot_date`），
   `--limit` 试点（`replace_scope=codes`）不得连带删掉同日其它股票。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.core.analyst_forecast import (
    normalize,
    refresh_daily,
    snapshot_evidence,
    write_snapshot,
)

SNAPSHOT_DATE = date(2026, 11, 18)


def _frame(rows: list[tuple[str, int, float | None, float | None]]) -> pd.DataFrame:
    """构造接口形状的宽表（年度列名照抄源端）。"""
    records = [
        {
            "序号": index + 1,
            "代码": code,
            "名称": f"股票{code}",
            "研报数": reports,
            "机构投资评级(近六个月)-买入": 1.0,
            "2026预测每股收益": eps_2026,
            "2029预测每股收益": eps_2029,
        }
        for index, (code, reports, eps_2026, eps_2029) in enumerate(rows)
    ]
    return pd.DataFrame.from_records(records)


def test_fiscal_years_come_from_columns_not_hardcoded() -> None:
    """年度列按列名解析（含源端滚动到 2029E），且 EPS 为空的年度不写 0。"""
    frame = _frame([("600519", 43, 66.8, None), ("000001", 12, 1.5, 2.0)])
    long, stats = normalize(frame, snapshot_date=SNAPSHOT_DATE)

    assert stats["fiscal_years"] == ["2026E", "2029E"]
    assert stats["rows_by_fiscal_year"] == {"2026E": 2, "2029E": 1}
    assert stats["dropped_null_eps_by_fiscal_year"] == {"2026E": 0, "2029E": 1}
    kept = long[long["stock_code"] == "600519"]
    assert list(kept["fiscal_year"]) == ["2026E"], "空 EPS 的年度必须整行不落库，不能写 0"
    assert kept["eps_forecast"].iloc[0] == pytest.approx(66.8)
    assert int(kept["analyst_count"].iloc[0]) == 43
    assert set(long["confidence"]) == {"exact"}


def test_identical_duplicate_rows_are_deduped() -> None:
    """源端重复行（同代码同载荷）去重；唯一只数才是覆盖率分子。"""
    frame = _frame([
        ("600519", 43, 66.8, 70.0),
        ("600519", 43, 66.8, 70.0),  # akshare 页间重叠
        ("000001", 12, 1.5, 2.0),
    ])
    long, stats = normalize(frame, snapshot_date=SNAPSHOT_DATE)

    assert stats["rows_in_response"] == 3
    assert stats["duplicate_rows_dropped"] == 1
    assert stats["duplicate_conflicts"] == 0
    assert stats["stocks"] == 2
    assert len(long) == 4  # 2 只 × 2 个年度
    assert not long.duplicated(["stock_code", "fiscal_year"]).any()


def test_mostly_invalid_codes_reject_whole_batch() -> None:
    """坏行占比 ≥1/3 → 整批拒绝（R6 的另一半：疑似源端结构性损坏）。"""
    frame = _frame([("600519", 43, 66.8, 70.0), ("ABCDEF", 1, 1.0, 1.0)])
    with pytest.raises(RuntimeError, match="整批拒绝"):
        normalize(frame, snapshot_date=SNAPSHOT_DATE)


def test_conflicting_duplicates_beyond_threshold_reject_whole_batch() -> None:
    """同一代码两套 EPS（真有冲突）超过 R6 阈值 → 整批拒绝，不猜哪套是真的。"""
    # 60 只股票各出现两条互相矛盾的 EPS（≥ max(50, 1%) 的 R6 阈值）
    rows: list[tuple[str, int, float | None, float | None]] = []
    for index in range(1, 61):
        rows.append((f"{index:06d}", 1, 1.0, 1.0))
        rows.append((f"{index:06d}", 1, 9.9, 9.9))
    frame = _frame(rows)

    with pytest.raises(RuntimeError, match="整批拒绝"):
        normalize(frame, snapshot_date=SNAPSHOT_DATE)


def test_invalid_stock_codes_are_isolated_not_global_failure() -> None:
    """少量非法代码只跳过该行并计数（R6 逐股隔离），不否决整批。

    判据是 `max(50, 1%)` 与 `≥1/3` 取或：1/101 两者都不满足 → 只隔离该行；
    1/2 会命中 1/3 → 整批拒绝（见下一条用例）。
    """
    rows: list[tuple[str, int, float | None, float | None]] = [
        ("ABCDEF", 1, 1.0, 1.0),
    ]
    rows += [(f"{index:06d}", 1, 1.0, 1.0) for index in range(1, 101)]
    frame = _frame(rows)
    long, stats = normalize(frame, snapshot_date=SNAPSHOT_DATE)

    assert stats["dropped_invalid_code"] == 1
    assert stats["dropped_invalid_code_examples"] == ["ABCDEF"]
    assert stats["stocks"] == 100
    assert "ABCDEF" not in set(long["stock_code"])


def test_same_day_rerun_overwrites_and_limit_scope_keeps_other_codes(duckdb_store) -> None:
    """同日重跑等价覆盖；`limit` 试点只覆盖本次写入的代码。"""
    frame = _frame([("600519", 43, 66.8, 70.0), ("000001", 12, 1.5, 2.0)])
    rows, _ = normalize(frame, snapshot_date=SNAPSHOT_DATE)

    first = write_snapshot(duckdb_store, rows, snapshot_date=SNAPSHOT_DATE)
    assert first["rows_written"] == 4
    assert snapshot_evidence(duckdb_store, SNAPSHOT_DATE)["rows"] == 4

    # ① 同日全量重跑：先删后插，行数不翻倍
    again = write_snapshot(duckdb_store, rows, snapshot_date=SNAPSHOT_DATE)
    assert again["rows_deleted"] == 4
    assert snapshot_evidence(duckdb_store, SNAPSHOT_DATE)["rows"] == 4

    # ② 只有 600519 的试点批：不能删掉 000001 的行
    only_moutai = rows[rows["stock_code"] == "600519"]
    write_snapshot(
        duckdb_store, only_moutai, snapshot_date=SNAPSHOT_DATE, replace_scope="codes",
    )
    evidence = snapshot_evidence(duckdb_store, SNAPSHOT_DATE)
    assert evidence["rows"] == 4
    assert evidence["stocks"] == 2


def test_refresh_daily_skips_when_watermark_is_today(duckdb_store) -> None:
    """水位 = 北京日期：今天已打点就不再抓（一次也不请求源）。"""
    step = refresh_daily(
        duckdb_store,
        state_key="probe_state",
        read_state=lambda key: SNAPSHOT_DATE.isoformat(),
        write_state=lambda key, value: pytest.fail("skipped 步骤不得写水位"),
        snapshot_date=SNAPSHOT_DATE,
    )

    assert step["status"] == "skipped"
    assert step["reason"] == "already_refreshed_today"
    assert step["rows"] == 0
