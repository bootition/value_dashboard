"""浮动黑名单规则库回归（2026-11-22，用户裁定：规则驱动 + 有进有出）。

守住的底线
----------
1. **规则里不许写绝对日期**（用户明确要求）。李大霄 2015 年那版把口径写成
   「次新股：上市日期在 2013-12-31 之后」「伪成长：2011>30%、2012<20%、2013<10%」——
   这两条到 2016 年就再也匹配不到任何股票。本文件把它们翻成**相对年限**
   （「上市不满 1 年」）与**相对形态**（成长断崖），并用一条门禁钉死：
   **预置规则的可执行部分（conditions / pool）里不得出现任何年份或完整日期。**
   注意 `note` 里有日期是**故意的**——那是推导说明，不是可执行口径。
2. **求值不能把自己筛掉**：浮动黑名单求值时基础池只剔「彻底拉黑」。
   若误用并集，上一轮已浮动的股票会被规则自己排除 → 每轮全进全出地抖动。
   本文件用「连跑两轮，第二轮 entered=0 / exited=0」把这个坑钉住。
3. **求值失败不清空名册**：一次坏规则不该把用户已经接受的浮动黑名单清成空。
4. **停用规则 = 成员退出**，走的是与「不再命中」同一条路径。
5. **读侧排除是并集**：彻底拉黑 ∪ 浮动拉黑，只有一个入口。
"""

from __future__ import annotations

import json
import re

import pytest

from app.core import blacklist as blacklist_core
from app.core import floating_blacklist as floating
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore

#: 完整日期（2013-12-31）与裸年份（2011）在可执行部分都必须为零。
_ABSOLUTE_DATE = re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b")
_BARE_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


# ─── 1. 时间不变性门禁（用户明确要求的那条）──────────────────────────


def _executable_payload(rule: floating.PresetRule) -> str:
    """规则里**真正会被执行**的部分：条件与池参数。不含 note（那是文档）。"""
    return json.dumps(
        {"conditions": rule.conditions, "pool": rule.pool}, ensure_ascii=False
    )


@pytest.mark.parametrize("rule", floating.PRESET_RULES, ids=lambda r: r.key)
def test_preset_rule_has_no_absolute_date(rule: floating.PresetRule) -> None:
    """预置规则的可执行部分不得含任何完整日期。

    这一条就是「结合他说话的时间做换算」这个要求的机器表达：只要有人把
    `max_listing_years: 1` 改回 `listing_date > '2013-12-31'`，这里立刻红。
    """
    payload = _executable_payload(rule)
    assert not _ABSOLUTE_DATE.search(payload), (
        f"{rule.key} 的可执行部分含绝对日期：{payload}。"
        "请换算成相对年限（如 pool.max_listing_years）后重写。"
    )


@pytest.mark.parametrize("rule", floating.PRESET_RULES, ids=lambda r: r.key)
def test_preset_rule_has_no_bare_year(rule: floating.PresetRule) -> None:
    """同样不得含裸年份（`2011`/`2012`/`2013` 这种「某年的增速」写法）。"""
    payload = _executable_payload(rule)
    assert not _BARE_YEAR.search(payload), (
        f"{rule.key} 的可执行部分含裸年份：{payload}。"
        "年份会过期，请改写成滚动/相对口径（CAGR3、最新年报同比、上市不满 N 年…）。"
    )


def test_newly_listed_rule_uses_relative_years_not_a_cutoff_date() -> None:
    """次新股必须用 `pool.max_listing_years`（相对），不是日期条件。"""
    rule = floating.PRESET_BY_KEY["lidaxiao.newly_listed"]
    assert rule.pool.get("max_listing_years") == 1, (
        "李大霄 2014-12 说「2013-12-31 之后」距说话时 342 天 ≈ 0.94 年 → 取整 1 年"
    )
    assert rule.conditions == {"logic": "AND", "rules": []}, (
        "次新股不该用日期条件表达，池参数已经够了"
    )


def test_preset_rules_cover_all_categories() -> None:
    """5 条「黑五类」+ 4 条「好公司反面」，一条都不能悄悄消失。"""
    keys = {rule.key for rule in floating.PRESET_RULES}
    assert keys == {
        # 李大霄「黑五类」（估值/风格维度）
        "lidaxiao.small_cap",
        "lidaxiao.newly_listed",
        "lidaxiao.poor_earnings",
        "lidaxiao.false_growth",
        "lidaxiao.theme_momentum",
        # 从「做好人买好股得好报」那套好公司标准反推的（公司质量维度）
        "goodcompany.cash_sink",
        "goodcompany.no_cash_back",
        "goodcompany.dirty_balance_sheet",
        "goodcompany.paper_profit",
    }


def test_small_cap_is_enabled_per_user_ruling() -> None:
    """用户 2026-11-22 明确裁定：「小盘股我是不会碰的」→ 必须默认开启。"""
    assert floating.PRESET_BY_KEY["lidaxiao.small_cap"].enabled_by_default is True


def test_every_preset_rule_field_is_selectable() -> None:
    """**每条预置规则引用的字段都必须在筛选白名单里。**

    这条是本轮真实翻车点：首版「伪成长股」用了 `revenue_yoy_fy`，
    它只是 indicator_snapshot 的物理列、**不在 `SNAPSHOT_COLUMNS` 白名单里**，
    规则一执行就 `ValueError: 未知筛选字段`。而当时的测试全绿——因为
    没有任何用例真正执行过预置规则。这个用例把那类错误前移到静态检查。
    """
    from app.core.screening.engine import (
        EXTENDED_COLUMNS,
        METADATA_COLUMNS,
        NORMALIZED_FIELDS,
        SNAPSHOT_COLUMNS,
        STATEMENT_FIELDS,
    )

    known = set(SNAPSHOT_COLUMNS) | set(EXTENDED_COLUMNS) | set(NORMALIZED_FIELDS)
    known |= set(METADATA_COLUMNS)
    for table, fields in STATEMENT_FIELDS.items():
        known |= {f"{table}.{field}" for field in fields}

    def collect(node: dict) -> list[str]:
        found: list[str] = []
        for item in node.get("rules") or []:
            if isinstance(item, dict) and "rules" in item:
                found.extend(collect(item))
            elif isinstance(item, dict) and item.get("field"):
                found.append(str(item["field"]))
        return found

    problems: list[str] = []
    for rule in floating.PRESET_RULES:
        for field in collect(rule.conditions):
            if field not in known:
                problems.append(f"{rule.key} → {field}")
    assert problems == [], (
        "以下预置规则引用了筛选白名单之外的字段（执行时必然 ValueError）：\n"
        + "\n".join(problems)
    )


def test_every_preset_rule_actually_executes(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """**每条预置规则都要真的跑一遍 SQL。**

    静态字段检查 + 真跑缺一不可：字段名对但类型/单位不对、嵌套 OR 不被支持、
    池参数组合非法等问题，只有真跑才暴露。本用例对 9 条规则逐条 evaluate，
    任何一条抛错都算失败。
    """
    _seed_two_stocks(duckdb_store)
    failures: list[str] = []
    for rule in floating.PRESET_RULES:
        row = {
            "id": 0,
            "name": rule.name,
            "conditions": rule.conditions,
            "pool": rule.pool,
        }
        try:
            matched = floating.evaluate_rule(duckdb_store, sqlite_store, row)
        except Exception as error:  # noqa: BLE001 - 收集全部失败再一起报
            failures.append(f"{rule.key}: {type(error).__name__}: {error}")
            continue
        assert isinstance(matched, set), f"{rule.key} 没返回代码集合"
    assert failures == [], "以下预置规则执行失败：\n" + "\n".join(failures)


@pytest.mark.parametrize("rule", floating.PRESET_RULES, ids=lambda r: r.key)
def test_each_preset_rule_documents_its_derivation(rule: floating.PresetRule) -> None:
    """每条预置规则必须写清「依据 + 换算 + 已知误差」，否则下一轮没人敢动它。"""
    assert len(rule.note) > 80, f"{rule.key} 的 note 太短，缺少推导说明"
    assert "原话" in rule.note or "【" in rule.note


# ─── 2. 规则库 CRUD ────────────────────────────────────────────────


def test_preset_rules_are_seeded_idempotently(sqlite_store: SQLiteStore) -> None:
    first = floating.ensure_preset_rules(sqlite_store)
    assert first == len(floating.PRESET_RULES)
    second = floating.ensure_preset_rules(sqlite_store)
    assert second == 0, "重复灌入不应重复插入"
    assert len(floating.list_rules(sqlite_store)) == len(floating.PRESET_RULES)


def test_seeding_does_not_overwrite_user_edits(sqlite_store: SQLiteStore) -> None:
    """用户改过预置规则的开关/条件后，重启不能把自己的调整冲掉。"""
    floating.ensure_preset_rules(sqlite_store)
    rule = next(r for r in floating.list_rules(sqlite_store)
                if r["rule_key"] == "lidaxiao.poor_earnings")
    floating.update_rule(sqlite_store, int(rule["id"]), enabled=False, name="我给改的名字")

    floating.ensure_preset_rules(sqlite_store)  # 模拟重启
    after = next(r for r in floating.list_rules(sqlite_store) if r["id"] == rule["id"])
    assert after["enabled"] is False
    assert after["name"] == "我给改的名字"


def test_manual_rule_crud(sqlite_store: SQLiteStore) -> None:
    rule_id = floating.add_rule(
        sqlite_store,
        name="我的讨厌规则",
        conditions={"logic": "AND", "rules": [
            {"field": "pe_ttm", "op": ">", "value": 200},
        ]},
    )
    assert rule_id > 0
    stored = [r for r in floating.list_rules(sqlite_store) if int(r["id"]) == rule_id][0]
    assert stored["origin"] == "manual"
    assert stored["conditions"]["rules"][0]["field"] == "pe_ttm"

    assert floating.update_rule(sqlite_store, rule_id, enabled=False) is True
    assert floating.delete_rule(sqlite_store, rule_id) == 1
    assert floating.delete_rule(sqlite_store, rule_id) == 0


def test_rule_conditions_are_structurally_validated(sqlite_store: SQLiteStore) -> None:
    with pytest.raises(ValueError):
        floating.add_rule(sqlite_store, name="坏规则", conditions={"rules": "nope"})
    with pytest.raises(ValueError):
        floating.add_rule(
            sqlite_store, name="缺 value",
            conditions={"logic": "AND", "rules": [{"field": "pe_ttm", "op": ">"}]},
        )
    with pytest.raises(ValueError):
        floating.add_rule(
            sqlite_store, name="", conditions={"logic": "AND", "rules": []}
        )


# ─── 3. 读侧并集：彻底拉黑 ∪ 浮动拉黑 ───────────────────────────────


def test_excluded_codes_is_the_union(sqlite_store: SQLiteStore) -> None:
    blacklist_core.add_stock(sqlite_store, "600519")
    floating.ensure_preset_rules(sqlite_store)
    rule = floating.list_rules(sqlite_store)[0]
    with sqlite_store.transaction() as conn:
        conn.execute(
            f"""INSERT INTO {floating.MEMBERSHIP_TABLE}
                    (rule_id, stock_code, first_seen_at, last_seen_at)
                VALUES (?, '000001', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            [int(rule["id"])],
        )

    assert blacklist_core.blacklisted_codes(sqlite_store) == ["600519"], "永久表只放彻底拉黑"
    assert floating.floating_codes(sqlite_store) == ["000001"]
    assert sorted(blacklist_core.excluded_codes(sqlite_store)) == ["000001", "600519"]
    assert blacklist_core.excluded_code_set(sqlite_store) == {"000001", "600519"}


def test_membership_explains_which_rules_caught_the_stock(
    sqlite_store: SQLiteStore,
) -> None:
    floating.ensure_preset_rules(sqlite_store)
    rule = floating.list_rules(sqlite_store)[0]
    with sqlite_store.transaction() as conn:
        conn.execute(
            f"""INSERT INTO {floating.MEMBERSHIP_TABLE}
                    (rule_id, stock_code, first_seen_at, last_seen_at)
                VALUES (?, '000001', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            [int(rule["id"])],
        )
    hits = floating.membership_for(sqlite_store, "000001")
    assert len(hits) == 1
    assert hits[0]["name"] == rule["name"]


# ─── 4. 求值：不自己筛自己（本轮最关键的行为契约）──────────────────────


def _seed_two_stocks(duck: DuckDBStore) -> None:
    """两只最小可筛股票。用 conftest 的既有 fixture 造数，不另写一套。"""
    from conftest import insert_minimum_screenable_data

    for code in ("000001", "600519"):
        duck.write_query(
            """INSERT INTO stock_meta
                   (stock_code, name, exchange, is_listed, is_st, is_suspended, listing_date)
               VALUES (?, ?, 'SZSE', TRUE, FALSE, FALSE, DATE '2005-01-04')
               ON CONFLICT (stock_code) DO NOTHING""",
            [code, f"测试{code}"],
        )
        insert_minimum_screenable_data(duck, code)


def test_refresh_matches_stocks_with_simple_rule(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    _seed_two_stocks(duckdb_store)
    # 关掉所有预置规则，只留一条"总是命中"的手工规则，隔离变量
    floating.ensure_preset_rules(sqlite_store)
    for rule in floating.list_rules(sqlite_store):
        floating.update_rule(sqlite_store, int(rule["id"]), enabled=False)
    floating.add_rule(
        sqlite_store,
        name="命中全部上市股票",
        conditions={"logic": "AND", "rules": [
            {"field": "total_shares", "op": ">", "value": 0},
        ]},
    )

    report = floating.refresh(duck=duckdb_store, sqlite=sqlite_store)
    assert report["status"] == "ok"
    assert report["entered"] >= 2
    assert set(floating.floating_codes(sqlite_store)) >= {"000001", "600519"}


def test_second_run_does_not_self_exclude(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """连跑两轮，第二轮必须 entered=0 / exited=0。

    这是「求值时基础池不剔浮动成员」的行为契约。若有人把
    `pool_excludes_floating` 默认值改掉、或让 `evaluate_rule` 误用并集，
    第二轮会把第一轮的成员全部判为"退出"，本用例立刻红。
    """
    _seed_two_stocks(duckdb_store)
    floating.ensure_preset_rules(sqlite_store)
    for rule in floating.list_rules(sqlite_store):
        floating.update_rule(sqlite_store, int(rule["id"]), enabled=False)
    floating.add_rule(
        sqlite_store,
        name="命中全部上市股票",
        conditions={"logic": "AND", "rules": [
            {"field": "total_shares", "op": ">", "value": 0},
        ]},
    )

    first = floating.refresh(duck=duckdb_store, sqlite=sqlite_store)
    assert first["entered"] > 0
    second = floating.refresh(duck=duckdb_store, sqlite=sqlite_store)

    assert second["entered"] == 0, "第二轮的命中集不该变化"
    assert second["exited"] == 0, (
        "第二轮的成员不该被自己筛掉——说明求值时错误地把浮动成员剔出了基础池"
    )
    assert set(floating.floating_codes(sqlite_store)) >= {"000001", "600519"}


def test_broken_rule_keeps_previous_roster(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """规则求值抛错时保留上一轮名册，不清空（一次失败不等于全部赦免）。"""
    _seed_two_stocks(duckdb_store)
    floating.ensure_preset_rules(sqlite_store)
    for rule in floating.list_rules(sqlite_store):
        floating.update_rule(sqlite_store, int(rule["id"]), enabled=False)
    rule_id = floating.add_rule(
        sqlite_store,
        name="先正常后坏掉",
        conditions={"logic": "AND", "rules": [
            {"field": "total_shares", "op": ">", "value": 0},
        ]},
    )
    floating.refresh(duck=duckdb_store, sqlite=sqlite_store)
    before = set(floating.floating_codes(sqlite_store))
    assert before

    # 把条件改成引擎一定会拒绝的字段 → 该规则求值抛错
    floating.update_rule(
        sqlite_store, rule_id,
        conditions={"logic": "AND", "rules": [
            {"field": "绝对不存在的字段", "op": ">", "value": 1},
        ]},
    )
    report = floating.refresh(duck=duckdb_store, sqlite=sqlite_store)

    assert report["status"] == "partial"
    error_rows = [d for d in report["detail"] if d["status"] == "error"]
    assert error_rows and error_rows[0]["kept_previous"] > 0
    assert set(floating.floating_codes(sqlite_store)) == before, "失败不该清空名册"


def test_disabling_a_rule_emits_exit_events(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """停用规则 = 成员退出，与「不再命中」走同一条路径。"""
    _seed_two_stocks(duckdb_store)
    floating.ensure_preset_rules(sqlite_store)
    for rule in floating.list_rules(sqlite_store):
        floating.update_rule(sqlite_store, int(rule["id"]), enabled=False)
    rule_id = floating.add_rule(
        sqlite_store,
        name="临时规则",
        conditions={"logic": "AND", "rules": [
            {"field": "total_shares", "op": ">", "value": 0},
        ]},
    )
    floating.refresh(duck=duckdb_store, sqlite=sqlite_store)
    assert floating.floating_codes(sqlite_store)

    floating.update_rule(sqlite_store, rule_id, enabled=False)
    report = floating.refresh(duck=duckdb_store, sqlite=sqlite_store)
    assert report["exited"] > 0
    assert floating.floating_codes(sqlite_store) == []


# ─── 5. 相对年限池参数 ─────────────────────────────────────────────


def test_max_listing_years_rejects_contradictory_range(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    from app.core.screening.engine import ScreeningEngine

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    with pytest.raises(ValueError, match="必须小于"):
        engine.run(
            {"conditions": {"logic": "AND", "rules": []}, "sort": [], "columns": []},
            include_st=True, include_suspended=True,
            min_listing_years=5, max_listing_years=3, masked_fields=[],
        )


def test_max_listing_years_is_a_relative_window(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore
) -> None:
    """老股（2005 年上市）不该出现在「上市不满 1 年」的池里。

    这条同时验证了「相对年限」真的生效，而不是被当成 0 忽略。
    """
    _seed_two_stocks(duckdb_store)
    from app.core.screening.engine import ScreeningEngine

    engine = ScreeningEngine(duck=duckdb_store, sqlite=sqlite_store)
    result = engine.run(
        {"conditions": {"logic": "AND", "rules": []}, "sort": [], "columns": ["stock_code"]},
        include_st=True, include_suspended=True, min_listing_years=0,
        max_listing_years=1, masked_fields=[],
    )
    assert result["results"] == [], "2005 年上市的股票不满足「上市不满 1 年」"
