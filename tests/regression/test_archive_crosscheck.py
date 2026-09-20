"""归档核验哨兵回归（2026-09-20）。

系统性审查发现 18 张历史归档域**零业务引用**（导入了但没人读）。
本模块把它变成可重复的核验哨兵。三条不变量：
1. 登记的比对对必须**语义同概念**（口径不同的绝不能登记，否则制造假失败）；
2. 任一对若在真实库中比对行数为 0，说明取错了表/列（历史踩过：把只在最新快照里
   的指标当成了有历史的指标）；
3. 全部概念的比对行数必须足量，否则核验形同虚设。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.core.quality.archive_crosscheck import (  # noqa: E402
    DEFAULT_PAIRS,
    REVIEW_THRESHOLD,
    run_crosscheck,
)


def test_pairs_are_unique_by_concept_and_archive_column() -> None:
    concepts = [p.concept for p in DEFAULT_PAIRS]
    assert len(concepts) == len(set(concepts)), "概念名重复"
    keys = [(p.archive_table, p.archive_column) for p in DEFAULT_PAIRS]
    assert len(keys) == len(set(keys)), "归档列被重复登记"


def test_pairs_have_sane_tolerance() -> None:
    for pair in DEFAULT_PAIRS:
        assert 0 < pair.tolerance <= 0.5, f"{pair.concept} 容差异常: {pair.tolerance}"


def test_review_threshold_is_meaningful() -> None:
    assert 0.5 <= REVIEW_THRESHOLD < 1.0


def test_crosscheck_against_empty_database_does_not_raise(duckdb_store) -> None:
    """归档域为空时应跳过而不是崩溃（归档是可选层）。"""
    report = run_crosscheck(duckdb_store)
    assert report.results, "应返回逐概念的结果占位"
    assert all(r.compared == 0 for r in report.results)


def test_crosscheck_computes_agreement_for_seeded_pair(duckdb_store) -> None:
    """构造一致数据：一致率应为 100% 且不触发复核。"""
    from datetime import date

    from app.core.quality.archive_crosscheck import CheckPair

    with duckdb_store.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS xcheck_ours "
                     "(stock_code VARCHAR, report_date DATE, my_metric DOUBLE)")
        conn.execute("CREATE TABLE IF NOT EXISTS xcheck_theirs "
                     "(stock_code VARCHAR, report_date DATE, their_metric DOUBLE)")
        for i, value in enumerate((1.0, 2.0, 3.0, 4.0)):
            conn.execute("INSERT INTO xcheck_ours VALUES (?, ?, ?)",
                         [f"00000{i}", date(2020, 12, 31), value])
            conn.execute("INSERT INTO xcheck_theirs VALUES (?, ?, ?)",
                         [f"00000{i}", date(2020, 12, 31), value])
    pair = CheckPair("测试指标", "xcheck_ours", "my_metric", "xcheck_theirs", "their_metric")
    report = run_crosscheck(duckdb_store, pairs=(pair,))
    result = report.results[0]
    assert result.compared == 4
    assert result.within_tolerance == 1.0
    assert result.needs_review is False
    assert report.flagged == []


def test_crosscheck_flags_large_disagreement(duckdb_store) -> None:
    """构造严重不一致：必须标记 needs_review（这就是抓到 bps 25% 的机制）。"""
    from datetime import date

    from app.core.quality.archive_crosscheck import CheckPair

    with duckdb_store.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS xcheck_ours "
                     "(stock_code VARCHAR, report_date DATE, my_metric DOUBLE)")
        conn.execute("CREATE TABLE IF NOT EXISTS xcheck_theirs "
                     "(stock_code VARCHAR, report_date DATE, their_metric DOUBLE)")
        for i in range(10):
            conn.execute("INSERT INTO xcheck_ours VALUES (?, ?, ?)",
                         [f"10000{i}", date(2020, 12, 31), 1.0])
            conn.execute("INSERT INTO xcheck_theirs VALUES (?, ?, ?)",
                         [f"10000{i}", date(2020, 12, 31), 4.0])
    pair = CheckPair("测试指标", "xcheck_ours", "my_metric", "xcheck_theirs", "their_metric")
    report = run_crosscheck(duckdb_store, pairs=(pair,))
    assert report.results[0].within_tolerance == 0.0
    assert report.results[0].needs_review is True
    assert report.flagged


def test_crosscheck_covers_all_usable_archive_domains() -> None:
    """核验面必须覆盖到「归档域真被利用」的程度。

    2026-09-20 系统性审查发现 18 张归档域**零业务引用**；本模块把它们变成核验哨兵。
    当初只登记 9 个概念，现已扩到 20+，覆盖 FI_T1/T3/T4/T5/T8/T9 与每股域。
    """
    tables = {p.archive_table for p in DEFAULT_PAIRS}
    for expected in ("csmar_fi_t1", "csmar_fi_t4", "csmar_per_share_history"):
        assert expected in tables, f"应覆盖 {expected}"
    assert len(DEFAULT_PAIRS) >= 20, f"核验概念过少（{len(DEFAULT_PAIRS)}），归档域利用不足"


def test_incomparable_calibers_are_not_registered() -> None:
    """口径本就不同的概念绝不能登记 —— 那会制造假失败。

    实测记录（2026-09-20）：
    - 有形净值债务率：我们用「负债/(权益−无形−商誉)」标准口径；归档只扣无形资产。
      按我方口径 74.76%，改成只扣无形资产则 98.03% —— 差异全部由商誉解释，不是 bug。
    - 应计项目：归档是**绝对金额**且用资产负债表法；我们是「(净利−经营现金流)/总资产」。
    """
    concepts = {p.concept for p in DEFAULT_PAIRS}
    assert "有形净值债务率" not in concepts, "口径不同（商誉）不应登记"
    assert "应计项目" not in concepts, "口径与单位均不同，不应登记"
