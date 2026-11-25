"""浮动黑名单 —— 规则驱动、每次更新重算、有进有出。

## 与「彻底拉黑」的分工

| | 彻底拉黑（`app/core/blacklist.py`） | 浮动拉黑（本模块） |
|---|---|---|
| 谁来定 | 人在详情页手工点 | 规则库自动算 |
| 语义 | 身份型事实（造假/退市/审计非标…），或"我就是不想看" | 特征型（小盘/次新/题材/伪成长/绩差） |
| 变化 | 只由人增删 | 每次数据更新重算，有进有出 |
| 移出方式 | 详情页「移出黑名单」 | 改规则，或该特征自己消失 |

用户裁定（2026-11-22）：
- 浮动拉黑**不提供任何手工入口**——想让某只股票浮动拉黑，去改规则；
- 彻底拉黑**不参与比较**——它一旦进表就不再被规则重算覆盖（筛选引擎的基础池
  已经把彻底拉黑剔掉，所以规则匹配集里天然不会出现它们）。

## 为什么规则要用「相对年限」而不是「绝对日期」

这是本模块最容易被后人写坏的地方，单独说清楚。

李大霄 2015-07-27 那版量化定义里有两条**写死的日期**：

- 「次新股：**上市日期在 2013-12-31 之后**」
- 「伪成长：**2011 年**营收增长 > 30%，**2012 年** < 20%，**2013 年** < 10%」

这两条在 2016 年就失效了——2016 年再按"2013-12-31 之后"筛，筛出来的全是上市三年的
老股。**绝对日期会把一条规则变成一次性查询。**

所以本模块把两条都翻译成**锚定在他说话那一刻的相对口径**（推导过程逐条写在
`PRESET_RULES` 的 `note` 里）：

- 「2013-12-31 之后」→ 他 2014-12 首次提出时距此 342 天 ≈ 0.94 年，
  2015-07 量化版距此 573 天 ≈ 1.57 年，两处取整都落在 **1 年**
  → 规则写成 **「上市不满 1 年」**（`pool.max_listing_years`）。
- 「三年增速 30%→20%→10%」→ 翻成 **「CAGR3 ≥ 20% 且 最新年报同比 ≤ 10%」**（成长断崖）。

`tests/regression/test_floating_blacklist.py` 有一条门禁：
**预置规则 JSON 里不得出现任何 `YYYY-MM-DD` 形式的绝对日期**，防的就是这个坑重演。

## 天然 TTL：为什么浮动黑名单不需要「过期时间」这个功能

用户的原话是「题材股在当下是黑名单，但是过几年放凉了之后，又是可以选择的」。

这件事**不需要额外的 TTL 机制**：只要规则里的每一项都是**滚动/当期指标**
（20 日均换手率、当期 PB/ROE、最近一期年报增速），热度退了、业绩好了，
下一轮重算自然就不匹配了 → 自动出名单。

反过来说，**一旦有人在规则里写绝对日期，TTL 就消失了**。这也是为什么上面那条门禁存在。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

RULES_TABLE = "blacklist_rules"
MEMBERSHIP_TABLE = "blacklist_floating"
EVENTS_TABLE = "blacklist_floating_events"
RUNS_TABLE = "blacklist_floating_runs"

#: 单条规则单轮最多允许匹配多少只。超过即视为「规则太宽」，
#: **不更新该规则的名册**（宁可保持上一轮，也不静默少拉黑一批）。
#: 与 `ScreeningEngine.MAX_RESULT_ROWS` 对齐——超过它引擎就已经截断了。
MAX_RULE_MATCHES = 5_000

ENTERED = "entered"
EXITED = "exited"


@dataclass(frozen=True)
class PresetRule:
    """一条预置规则。`note` 里必须写清「依据 + 时间锚定换算 + 已知误差」。"""

    key: str
    name: str
    note: str
    conditions: dict[str, Any]
    enabled_by_default: bool
    pool: dict[str, Any] = field(default_factory=dict)


# ─── 五条预置规则（李大霄「黑五类」）──────────────────────────────────

PRESET_RULES: tuple[PresetRule, ...] = (
    PresetRule(
        key="lidaxiao.small_cap",
        name="小盘股（黑五类①）",
        note=(
            "原话：小盘股 = 流通盘小于 5 亿股、自由流通股本小于 1 亿股（2015-07-27 访谈）。\n"
            "【时不变】这是股数门槛，不随年份失效，原样保留为 `circ_shares < 5e8`。\n"
            "【数据缺口】库内只有流通股本 circ_shares 与总股本 total_shares，"
            "**没有「自由流通股本」**（需剔除限售与控股股东锁定），故只用第一条。\n"
            "【可选市值口径】2015 年 A 股均价约 10 元 × 5 亿股 ≈ 50 亿市值。\n"
            "【默认关闭】小盘是**风格因子不是质量问题**；2013–2015 创业板牛市、"
            "2019–2021 中小盘行情都直接打过这条的脸。要开请自行收紧门槛。"
        ),
        conditions={"logic": "AND", "rules": [
            {"field": "circ_shares", "op": "<", "value": 500_000_000},
        ]},
        enabled_by_default=False,
    ),
    PresetRule(
        key="lidaxiao.newly_listed",
        name="次新股（黑五类②）",
        note=(
            "原话（2015-07-27）：次新股 = 上市日期在 2013 年 12 月 31 日之后。\n"
            "【时间锚定换算】他首次提出黑五类在 2014-12-08，距 2013-12-31 为 342 天 ≈ 0.94 年；\n"
            "量化版访谈 2015-07-27 距此 573 天 ≈ 1.57 年。两处取整都落在 **1 年**，\n"
            "且与市场共识一致（券商观察：上市超一年，市场基本已淡忘其次新属性）。\n"
            "【因此】规则写成「上市不满 1 年」这个**相对年限**（`pool.max_listing_years`），\n"
            "绝不写绝对日期——写死 2013-12-31 的话，2016 年起就再也匹配不到任何次新股。\n"
            "【天然 TTL】上市满一年后自动出名单，不需要任何过期机制。"
        ),
        conditions={"logic": "AND", "rules": []},
        pool={"max_listing_years": 1},
        enabled_by_default=True,
    ),
    PresetRule(
        key="lidaxiao.poor_earnings",
        name="绩差股（黑五类③）",
        note=(
            "原话：绩差股 = 每股收益(TTM) 小于 1 分钱。\n"
            "【时不变】直接落到 `income.basic_eps < 0.01` 元/股，无需换算。\n"
            "【口径注意】库内 basic_eps 是「与快照报告期对齐的累计每股收益」\n"
            "（年报=全年、三季报=前三季），不是严格 TTM，跨季度比较有季节性偏差。\n"
            "【已知误杀】周期股在行业底部（钢铁/航运/猪周期）EPS 就是负的，\n"
            "本条会把周期底部一起拉黑——这正是「当期盈利≠长期质量」。"
        ),
        conditions={"logic": "AND", "rules": [
            {"field": "income.basic_eps", "op": "<", "value": 0.01},
        ]},
        enabled_by_default=True,
    ),
    PresetRule(
        key="lidaxiao.false_growth",
        name="伪成长股（黑五类④）",
        note=(
            "原话：伪成长 = 2011 年营收增长 > 30%、2012 年 < 20%、2013 年 < 10%，总市值 < 50 亿。\n"
            "【时间锚定换算】三个年份不是重点，「连续三年增速逐级下滑」才是——那叫**成长断崖**。\n"
            "翻成时不变形式：**过去 3 年复合增速仍在高位（取他 30%/20% 的中点 20%），\n"
            "但最新一年已跌到他的终点 10% 以下**。\n"
            "【为什么不写严格的 3 点单调下滑】筛选引擎的基础池每只股票只暴露**最新一期**\n"
            "快照行，取不到前两期做单调检验；另造一个求值器会形成第二套口径（AGENTS R1）。\n"
            "所以这里是 CAGR3 与最新年报同比的**两点近似**，已知误差：\n"
            "会把「匀速高增长但非严格单调」的公司误判进来。\n"
            "【市值门槛】保留他的 50 亿：total_market_cap < 5e9。"
        ),
        conditions={"logic": "AND", "rules": [
            {"field": "revenue_cagr3", "op": ">=", "value": 0.20},
            {"field": "revenue_yoy_fy", "op": "<=", "value": 0.10},
            {"field": "total_market_cap", "op": "<", "value": 5_000_000_000},
        ]},
        enabled_by_default=True,
    ),
    PresetRule(
        key="lidaxiao.theme_momentum",
        name="题材股（黑五类⑤ · 代理口径）",
        note=(
            "【先说清一件事】李大霄本人也没量化「题材」——他只给了「所有题材概念股中\n"
            "总市值 < 50 亿」，而「哪些股票算题材概念股」始终没定义。\n"
            "我们库里同样**没有概念/题材标签表**，所以这条只能是**代理口径（proxy）**，\n"
            "不是概念标签匹配。\n"
            "\n"
            "【代理思路】题材股的经济实质是「价格由叙事驱动、不由已实现盈利驱动」。\n"
            "可观测信号取三个，必须同时成立：\n"
            "  ① 故事溢价：pb_mrq ≥ 3（贵）而 roe < 0.05（赚钱能力弱）；\n"
            "  ② 投机度：turnover_rate ≥ 5%（20 日均换手，热度指标）；\n"
            "  ③ 他的市值门槛：total_market_cap < 50 亿。\n"
            "\n"
            "【为什么不用别的口径】\n"
            "- 公告标题热词（AI/机器人/固态电池…）会过期，等于再造一张要每年维护的热词表；\n"
            "- company_profile 经营范围与行业分类不一致，只能抓「跨界」，抓不到「本就属于热门赛道」；\n"
            "- 与热门股的相关性需要另建因子库，超出本功能范围。\n"
            "\n"
            "【天然 TTL】turnover_rate 是 20 日滚动均换手率，热度退了它自己就掉下来；\n"
            "pb_mrq / roe 每期重算。所以「炒几年放凉了就能重新入选」不需要额外机制——\n"
            "这正是浮动黑名单相对手工黑名单的最大好处。\n"
            "\n"
            "【默认关闭 + 已知误杀】真正的成长股在放量初期也会命中（高换手 + 高 PB +\n"
            "当期 ROE 还没起来）。这是全五条里误杀率最高的一条，请先看试算结果再打开。"
        ),
        conditions={"logic": "AND", "rules": [
            {"field": "total_market_cap", "op": "<", "value": 5_000_000_000},
            {"field": "turnover_rate", "op": ">=", "value": 5.0},
            {"field": "pb_mrq", "op": ">=", "value": 3.0},
            {"field": "roe", "op": "<", "value": 0.05},
        ]},
        enabled_by_default=False,
    ),
)

PRESET_BY_KEY: dict[str, PresetRule] = {rule.key: rule for rule in PRESET_RULES}


# ─── 规则库 CRUD ──────────────────────────────────────────────────


def ensure_preset_rules(sqlite: Any) -> int:
    """把预置规则灌进规则库（幂等：只插缺的，不覆盖用户改过的）。

    返回本次新插入的条数。用户改过名字/条件/开关的规则**不会被重置**——
    否则每次启动都把自己的调整冲掉。
    """
    if sqlite is None:
        return 0
    existing = {
        row["rule_key"]
        for row in sqlite.query(f"SELECT rule_key FROM {RULES_TABLE} WHERE rule_key IS NOT NULL")
    }
    inserted = 0
    for preset in PRESET_RULES:
        if preset.key in existing:
            continue
        sqlite.execute(
            f"""INSERT INTO {RULES_TABLE}
                    (rule_key, name, note, enabled, conditions_json, pool_json, origin)
                VALUES (?, ?, ?, ?, ?, ?, 'preset')""",
            [
                preset.key,
                preset.name,
                preset.note,
                1 if preset.enabled_by_default else 0,
                json.dumps(preset.conditions, ensure_ascii=False),
                json.dumps(preset.pool, ensure_ascii=False),
            ],
        )
        inserted += 1
    return inserted


def list_rules(sqlite: Any, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    """列出规则库（按 id 升序），并把 JSON 字段解析成对象。"""
    if sqlite is None:
        return []
    sql = f"SELECT * FROM {RULES_TABLE}"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    rows = sqlite.query(sql)
    for row in rows:
        row["enabled"] = bool(row.get("enabled"))
        row["conditions"] = _loads(row.get("conditions_json"), {})
        row["pool"] = _loads(row.get("pool_json"), {})
    return rows


def add_rule(
    sqlite: Any,
    *,
    name: str,
    conditions: dict[str, Any],
    note: str | None = None,
    pool: dict[str, Any] | None = None,
    enabled: bool = True,
) -> int:
    """新增一条手工规则，返回新 id。"""
    if not (name or "").strip():
        raise ValueError("规则名不能为空")
    _validate_conditions(conditions)
    sqlite.execute(
        f"""INSERT INTO {RULES_TABLE}
                (rule_key, name, note, enabled, conditions_json, pool_json, origin)
            VALUES (NULL, ?, ?, ?, ?, ?, 'manual')""",
        [
            name.strip(),
            (note or "").strip() or None,
            1 if enabled else 0,
            json.dumps(conditions, ensure_ascii=False),
            json.dumps(pool or {}, ensure_ascii=False),
        ],
    )
    row = sqlite.query(f"SELECT MAX(id) AS id FROM {RULES_TABLE}")
    return int(row[0]["id"]) if row else 0


def update_rule(
    sqlite: Any,
    rule_id: int,
    *,
    name: str | None = None,
    conditions: dict[str, Any] | None = None,
    note: str | None = None,
    pool: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> bool:
    """局部更新一条规则。返回是否命中（False = 没有这个 id）。"""
    existing = sqlite.query(f"SELECT id FROM {RULES_TABLE} WHERE id = ?", [rule_id])
    if not existing:
        return False
    sets: list[str] = []
    params: list[Any] = []
    if name is not None:
        if not name.strip():
            raise ValueError("规则名不能为空")
        sets.append("name = ?")
        params.append(name.strip())
    if conditions is not None:
        _validate_conditions(conditions)
        sets.append("conditions_json = ?")
        params.append(json.dumps(conditions, ensure_ascii=False))
    if note is not None:
        sets.append("note = ?")
        params.append(note.strip() or None)
    if pool is not None:
        sets.append("pool_json = ?")
        params.append(json.dumps(pool, ensure_ascii=False))
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if not sets:
        return True
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(rule_id)
    sqlite.execute(f"UPDATE {RULES_TABLE} SET {', '.join(sets)} WHERE id = ?", params)
    return True


def delete_rule(sqlite: Any, rule_id: int) -> int:
    """删除规则；该规则名下的浮动成员一并清掉（连带记退出事件不是必须，直接删）。"""
    rows = sqlite.query(f"SELECT id FROM {RULES_TABLE} WHERE id = ?", [rule_id])
    if not rows:
        return 0
    with sqlite.transaction() as conn:
        conn.execute(f"DELETE FROM {MEMBERSHIP_TABLE} WHERE rule_id = ?", [rule_id])
        conn.execute(f"DELETE FROM {RULES_TABLE} WHERE id = ?", [rule_id])
    return 1


# ─── 求值 ─────────────────────────────────────────────────────────


def evaluate_rule(duck: Any, sqlite: Any, rule_row: dict[str, Any]) -> set[str]:
    """用筛选引擎跑一条黑名单规则，返回命中的股票代码集合。

    **复用 `ScreeningEngine` 而不是另写一套条件求值**（AGENTS R1）：DSL、单位口径、
    遮蔽、排名、参数绑定全都已经是回归过的实现，另写一份必然分叉。

    两个关键开关：

    - `pool_excludes_floating=False`：求值时基础池只剔除**彻底拉黑**，不剔除浮动成员。
      否则第二轮起规则会把上一轮已经拉黑的股票筛掉 → 它们全部"退出" → 名单在
      有进有出之间反复抖动。
    - `masked_fields=[]`：跳过"字段不可信 → 409"的显示层检查。这条检查是为了不让人
      看到不可信的**数字**；黑名单只是拿这些列做**布尔判断**，不应该因为某列不可信
      就整条规则不生效。宁可多拉黑，也不要在数据可疑时静默放行。
    """
    from app.core.screening.engine import ScreeningEngine

    pool = rule_row.get("pool") or {}
    engine = ScreeningEngine(duck=duck, sqlite=sqlite)
    result = engine.run(
        {"conditions": rule_row["conditions"], "sort": [], "columns": ["stock_code"]},
        include_st=True,
        include_suspended=True,
        min_listing_years=0,
        max_listing_years=pool.get("max_listing_years"),
        strict_only=False,
        masked_fields=[],
        pool_excludes_floating=False,
    )
    codes = {str(row["stock_code"]) for row in result.get("results") or [] if row.get("stock_code")}
    if result.get("truncated"):
        raise RuleTooBroadError(
            f"规则「{rule_row.get('name')}」命中数超过引擎上限，结果被截断；"
            "为避免静默少拉黑，本轮不更新该规则的名册，请收紧条件"
        )
    if len(codes) >= MAX_RULE_MATCHES:
        raise RuleTooBroadError(
            f"规则「{rule_row.get('name')}」命中 {len(codes)} 只，达到上限 {MAX_RULE_MATCHES}"
        )
    return codes


class RuleTooBroadError(RuntimeError):
    """规则命中数超出可信范围（被截断或过宽），本轮不更新名册。"""


# ─── 重算 + 有进有出 ───────────────────────────────────────────────


def refresh(duck: Any, sqlite: Any, *, run_at: datetime | None = None) -> dict[str, Any]:
    """重算全部启用的规则，与上一轮比较，写出「进/出」事件。

    返回本轮报告：每条规则命中数、新增/移除数、以及失败规则（失败时**保留上一轮名册**，
    不因为一次求值失败就把黑名单清空）。
    """
    ensure_preset_rules(sqlite)
    now = run_at or datetime.now(UTC)
    stamp = now.isoformat()

    rules = list_rules(sqlite)
    detail: list[dict[str, Any]] = []
    total_entered = 0
    total_exited = 0

    # ── 阶段一：先把所有规则求值完（只读），再开写事务 ──────────────
    # 求值内部会开 SQLite 只读连接（读 published overrides），与写事务叠在
    # 一起没有必要，分开更干净、也避免长事务里夹慢查询。
    evaluated: dict[int, tuple[set[str] | None, str, str | None]] = {}
    for rule in rules:
        rule_id = int(rule["id"])
        if not rule["enabled"]:
            # 停用的规则：名册清空（产生退出事件，让"关掉它"立刻生效）
            evaluated[rule_id] = (set(), "disabled", None)
            continue
        try:
            evaluated[rule_id] = (evaluate_rule(duck, sqlite, rule), "ok", None)
        except Exception as error:  # noqa: BLE001 —— 单条规则失败不能炸整轮
            # 失败时保留上一轮名册：一次求值失败不该把黑名单清空
            evaluated[rule_id] = (None, "error", str(error))

    with sqlite.transaction() as conn:
        for rule in rules:
            rule_id = int(rule["id"])
            previous = {
                row["stock_code"]
                for row in conn.execute(
                    f"SELECT stock_code FROM {MEMBERSHIP_TABLE} WHERE rule_id = ?", [rule_id]
                ).fetchall()
            }
            matched, status, error = evaluated[rule_id]
            if matched is None:
                detail.append({
                    "rule_id": rule_id,
                    "name": rule["name"],
                    "status": "error",
                    "error": error,
                    "kept_previous": len(previous),
                })
                continue

            entered = matched - previous
            exited = previous - matched

            if entered:
                conn.executemany(
                    f"""INSERT INTO {MEMBERSHIP_TABLE}
                            (rule_id, stock_code, first_seen_at, last_seen_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(rule_id, stock_code) DO UPDATE SET
                            last_seen_at = excluded.last_seen_at""",
                    [(rule_id, code, stamp, stamp) for code in sorted(entered)],
                )
            if matched & previous:
                conn.executemany(
                    f"UPDATE {MEMBERSHIP_TABLE} SET last_seen_at = ? WHERE rule_id = ? AND stock_code = ?",
                    [(stamp, rule_id, code) for code in sorted(matched & previous)],
                )
            if exited:
                conn.executemany(
                    f"DELETE FROM {MEMBERSHIP_TABLE} WHERE rule_id = ? AND stock_code = ?",
                    [(rule_id, code) for code in sorted(exited)],
                )
            events = [(stamp, rule_id, code, ENTERED) for code in sorted(entered)]
            events += [(stamp, rule_id, code, EXITED) for code in sorted(exited)]
            if events:
                conn.executemany(
                    f"""INSERT OR IGNORE INTO {EVENTS_TABLE}
                            (run_at, rule_id, stock_code, event)
                        VALUES (?, ?, ?, ?)""",
                    events,
                )

            total_entered += len(entered)
            total_exited += len(exited)
            detail.append({
                "rule_id": rule_id,
                "name": rule["name"],
                "status": status,
                "matched": len(matched),
                "entered": len(entered),
                "exited": len(exited),
            })

        conn.execute(
            f"""INSERT INTO {RUNS_TABLE}
                    (run_at, rules_evaluated, entered, exited, status, detail_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
            [
                stamp, len([d for d in detail if d["status"] == "ok"]),
                total_entered, total_exited,
                "partial" if any(d["status"] == "error" for d in detail) else "ok",
                json.dumps(detail, ensure_ascii=False),
            ],
        )

    report = {
        "run_at": stamp,
        "rules_evaluated": len([d for d in detail if d["status"] == "ok"]),
        "entered": total_entered,
        "exited": total_exited,
        "status": "partial" if any(d["status"] == "error" for d in detail) else "ok",
        "detail": detail,
    }
    logger.info(
        "浮动黑名单重算完成：规则 %s 条，新增 %s、移除 %s（%s）",
        report["rules_evaluated"], total_entered, total_exited, report["status"],
    )
    return report


# ─── 读侧 ─────────────────────────────────────────────────────────


def floating_codes(sqlite: Any) -> list[str]:
    """当前浮动黑名单的**代码集合**（跨规则去重）。

    只读名册表，不重算——名册由每轮更新写好后落盘，读侧必须便宜
    （每个 API 请求都可能要它）。
    """
    if sqlite is None:
        return []
    rows = sqlite.query(
        f"SELECT DISTINCT stock_code FROM {MEMBERSHIP_TABLE} ORDER BY stock_code"
    )
    return [str(row["stock_code"]) for row in rows if row.get("stock_code")]


def membership_for(sqlite: Any, stock_code: str) -> list[dict[str, Any]]:
    """某只股票为什么在浮动黑名单里（哪几条规则命中了它）。"""
    if sqlite is None:
        return []
    return sqlite.query(
        f"""SELECT m.rule_id, r.name, m.first_seen_at, m.last_seen_at
            FROM {MEMBERSHIP_TABLE} m
            JOIN {RULES_TABLE} r ON r.id = m.rule_id
            WHERE m.stock_code = ?
            ORDER BY r.id""",
        [stock_code],
    )


def latest_run(sqlite: Any) -> dict[str, Any] | None:
    if sqlite is None:
        return None
    rows = sqlite.query(f"SELECT * FROM {RUNS_TABLE} ORDER BY run_at DESC LIMIT 1")
    if not rows:
        return None
    row = rows[0]
    row["detail"] = _loads(row.get("detail_json"), [])
    return row


def recent_events(sqlite: Any, limit: int = 100) -> list[dict[str, Any]]:
    """最近的进出事件（倒序），供前端「有进有出」看板。"""
    if sqlite is None:
        return []
    # 注意：stock_meta 在 DuckDB，本表在 SQLite，**跨库 JOIN 不存在**。
    # 股票名称由调用方（API 层）拿到代码后去 DuckDB 批量补。
    return sqlite.query(
        f"""SELECT e.run_at, e.stock_code, e.event, e.rule_id, r.name AS rule_name
            FROM {EVENTS_TABLE} e
            LEFT JOIN {RULES_TABLE} r ON r.id = e.rule_id
            ORDER BY e.id DESC LIMIT ?""",
        [int(limit)],
    )


# ─── 内部工具 ────────────────────────────────────────────────────


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _validate_conditions(conditions: Any) -> None:
    """结构校验：必须有 logic 与 rules，叶子必须有 field/op。

    真正的字段白名单由 `ScreeningEngine` 在执行时校验（`validate_rule_fields`），
    这里只挡"结构就不对"的输入，避免坏 JSON 进库后才在更新时炸。
    """
    if not isinstance(conditions, dict):
        raise ValueError("conditions 必须是对象")
    logic = str(conditions.get("logic") or "AND").upper()
    if logic not in {"AND", "OR"}:
        raise ValueError("conditions.logic 只能是 AND 或 OR")
    rules = conditions.get("rules")
    if not isinstance(rules, list):
        raise ValueError("conditions.rules 必须是数组")
    for node in rules:
        if not isinstance(node, dict):
            raise ValueError("条件项必须是对象")
        if "rules" in node:
            _validate_conditions(node)
            continue
        if not node.get("field") or not node.get("op"):
            raise ValueError("每个条件项必须有 field 与 op")
        if "value" not in node and node.get("op") not in {"is_null", "is_not_null"}:
            raise ValueError(f"条件项 {node.get('field')} 缺少 value")
