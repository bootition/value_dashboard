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
        name="小盘股（黑五类① · 已改市值口径）",
        note=(
            "【他的原话·原文如此】2015-07-27 和讯版：「小盘股：流通盘小于 5 亿，自由流通股本小于 1 亿」；\n"
            "2016-12-08 证券之星版说得更死：「小股票，是指全部A股中**流通股本小于 5 亿股**，\n"
            "且自由流通股本小于 1 亿股的品种」。所以不是我误读——他确实按股数划。\n"
            "\n"
            "【⚠️ 这里没有照抄他的口径，理由如下（用户 2026-11-22 指出）】\n"
            "股数 ≠ 估值。发行 1 亿股、每股 1 元 → 市值 1 亿；同样是 1 亿股、每股 100 元 →\n"
            "市值 100 亿。**流通股本衡量的是「盘子大小」，不是「贵不贵」**。而李大霄的论点\n"
            "恰恰是「小盘股比大盘股**贵**」——论点讲的是估值，判据却用了与估值无关的股数。\n"
            "\n"
            "更糟的是：**股数本身就是一种绝对口径**，与「上市日期在 2013-12-31 之后」同型——\n"
            "2015 年 A 股均价约 15 元时「5 亿股」≈ 75 亿市值；今天均价变了、注册制后发行节奏\n"
            "也变了，同一个「5 亿股」对应的市值完全是另一个数。它一样会随时间漂移。\n"
            "\n"
            "【所以本规则用市值分位】`total_market_cap` 的**全市场百分位 ≤ 0.30**，\n"
            "即「市值排在全市场后 30%」。这是**相对**口径：\n"
            "  · 忠实于他的**论点**（小盘 = 市值小），而不是照抄他那个有缺陷的判据；\n"
            "  · 不随股价水平、发行节奏、通胀漂移，永远有效；\n"
            "  · 规模可控——后 30% 是可控的绝对数量，而「5 亿股以下」在现在的 A 股\n"
            "    可能命中大半个市场。\n"
            "\n"
            "【数据缺口】库内没有「自由流通股本」（需剔除限售与控股股东锁定），\n"
            "他第二个条件本来就实现不了——这点与口径选择无关。\n"
            "\n"
            "【默认开启·2026-11-22 用户裁定】原话「小盘股我是不会碰的」。嫌命中太多就把\n"
            "0.30 调低（如 0.20）；想更严可以改用 `market_rank` 按名次卡。\n"
            "\n"
            "【风险提示】小盘是**风格因子、不是质量问题**：2013–2015 创业板牛市、\n"
            "2019–2021 中小盘行情都直接打过这条的脸。"
        ),
        conditions={"logic": "AND", "rules": [
            # rank_mode 会在引擎入口展开成 total_market_cap_market_percentile（0–1 小数）
            {"field": "total_market_cap", "rank_mode": "market_percentile",
             "op": "<=", "value": 0.30},
        ]},
        enabled_by_default=True,
    ),
    PresetRule(
        key="lidaxiao.newly_listed",
        name="次新股（黑五类② · 上市不满 5 年）",
        note=(
            "原话（2015-07-27）：次新股 = 上市日期在 2013 年 12 月 31 日之后。\n"
            "【时间锚定换算】他首次提出黑五类在 2014-12-08，距 2013-12-31 为 342 天 ≈ 0.94 年；\n"
            "量化版访谈 2015-07-27 距此 573 天 ≈ 1.57 年。照他的原话，取整是 **1 年**。\n"
            "\n"
            "【但当前门槛是 5 年·用户 2026-11-22 裁定】原话：\n"
            "「直接把次新股改成 5 年吧，1 年还是太短了，我不希望买这么新的股票。」\n"
            "理由也清楚：1 年只是「次新属性消失」的市场共识节点，而**满 5 年才够看清楚**\n"
            "一轮完整的经营与分红记录（与「掏钱型」那条的门槛对齐）。\n"
            "两者不矛盾：1 年来自他的口径，5 年来自用户的风险偏好——**规则里写 5**。\n"
            "\n"
            "【仍然**绝不写绝对日期**】用 `pool.max_listing_years=5` 这个相对年限。\n"
            "写死「2013-12-31 之后」的话，这条规则 2016 年起就再也匹配不到任何股票了。\n"
            "\n"
            "【天然 TTL】上市满 5 年后自动出名单，不需要任何过期机制。\n"
            "【连带效果】上市不满 5 年的公司会被本条整体拉黑，因此「掏钱型」那两档\n"
            "（min_listing_years=5/10）在不满足年限时本就不评判——两者衔接得上。"
        ),
        conditions={"logic": "AND", "rules": []},
        pool={"max_listing_years": 5},  # 用户 2026-11-22：1 年太短，改 5 年
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
            "所以这里是 CAGR3 与最新同比的**两点近似**，已知误差：\n"
            "会把「匀速高增长但非严格单调」的公司误判进来。\n"
            "【口径·为什么用 revenue_yoy 而不是 revenue_yoy_fy】他原文是**年报口径**，\n"
            "但 `revenue_yoy_fy` 只是 indicator_snapshot 的物理列、**未进筛选白名单**\n"
            "（不在 `SNAPSHOT_COLUMNS` 里），规则一发就跑不通（首版就踩了这个坑）。\n"
            "这里用 TTM 同比 `revenue_yoy` 替代——两者在「增速断崖」方向上同义，\n"
            "差别只在跨季平滑，不影响「高位掉到低位」的判定。\n"
            "【市值门槛】保留他的 50 亿：total_market_cap < 5e9。"
        ),
        conditions={"logic": "AND", "rules": [
            {"field": "revenue_cagr3", "op": ">=", "value": 0.20},
            {"field": "revenue_yoy", "op": "<=", "value": 0.10},
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

#: **已退役的预置规则**：曾灌进库、现已从 `PRESET_RULES` 移除的 key。
#:
#: 为什么要单独登记：`ensure_preset_rules()` 只插缺的、不删旧的，所以「把规则从
#: 代码里删掉」对**已经装过的库不生效**——那条规则会一直留在库里继续拉黑。
#: 登记在这里，启动时连同它名下的浮动成员一起清掉。
#:
#: `goodcompany.no_cash_back`（2026-11-22 用户裁定退役）——原口径是「最近连续分红
#: 不足 5 年即拉黑」。用户认为多余：「连续多少年分红什么都说明不了，分的钱很少也
#: 不好说吧。我只关注总体，不太看重细节，只要它是愿意给股东钱的就行。」
#: 总量口径已由「掏钱型」两档（分红融资比）覆盖，连续性不再单独判断。
RETIRED_PRESET_KEYS: frozenset[str] = frozenset({
    "goodcompany.no_cash_back",
})


# ─── 「好公司」反面推导的预置规则（2026-11-22 新增）───────────────────
#
# 用户提问：李大霄除了「黑五类」，还讲过「做好人、买好股、得好报」和一整套好公司标准，
# 能不能从**好公司的反面**再反推一批坏公司特征？
#
# 能，而且这些比「黑五类」更适合做黑名单——因为「黑五类」量的是**估值贵不贵**（会随
# 市场均值回归），而「好公司」标准量的是**公司本身好不好**（更接近身份型）。
#
# 他的好公司口径（按时间排列，全部有出处）：
#   · 2012-08-25 证券时报网《优质公司有四标准》：①领导人是正直好人不是骗子；
#     ②为社会持续创造财富而非毁灭财富；③善待股东/员工/社区/自然；④被抛弃时价格远低于价值。
#   · 2018-01-29 界面《如何从全球投资者角度审视蓝筹股》：规模/行业地位/成长性/全球扩张…8 个角度；
#     「最优质 = 分红稳定、现金流扎实、资产负债表干净」。
#   · 2017-01 华夏时报、2024-08、2026-01 微博财经之夜：「好公司五个维度：对客户、对员工、
#     对股东、对社会、对自然——**顺序重要，第三才是对股东**」。
#   · 2026-01「掏钱 vs 塞钱」：掏钱=大股东减持、没完没了增发融资；塞钱=持续稳定现金分红、
#     真金白银回购注销。「差的公司，把手伸进股民的口袋里掏钱」。
#   · 2024-07 退休访谈：「上市公司要**真实**，真实而不造假的上市公司才是保护散户最重要的基础」。
#
# ⚠️ 能反推的与不能反推的（诚实划界）：
#   ✅ 对股东（分红/融资）→ 我们有 `dividend_financing_ratio_pct`、`consecutive_div_years`
#   ✅ 资产负债表干净 → `goodwill_ratio` / `debt_ratio` / `interest_coverage`
#   ✅ 不造假（代理）→ `cf_to_net_profit`（利润不落真钱）
#   ❌ 对客户/对员工/对社会/对自然：库里没有客服、员工流动、环保处罚、社区数据
#   ❌ 领导人是否正直：无人格/诚信数据（只能靠"造假被处罚"，我们也没有处罚表）
#   ❌ 大股东减持：无减持事件域
#   ❌ 回购注销：有 `buyback_events` 表，但未进 indicator_snapshot，规则里引用不到
#   ⚠️ 行业龙头：可用引擎的行业内市值排名，但与小盘股规则高度重叠，故不做（避免规则通胀）

GOOD_COMPANY_INVERSE_RULES: tuple[PresetRule, ...] = (
    PresetRule(
        key="goodcompany.cash_sink_5_10",
        name="掏钱型公司·上市 5–10 年（好公司反面①a）",
        note=(
            "【原话】「差的公司，把手伸进股民的口袋里掏钱，怎么能够买？」「要分清\n"
            "『把手伸进你口袋掏钱』的公司和『不断往你口袋塞钱』的公司」（2026-01 微博财经之夜）。\n"
            "【塞钱】持续稳定的现金分红 + 真金白银回购并注销；【掏钱】没完没了增发融资向市场要钱。\n"
            "\n"
            "【分档·用户 2026-11-22 裁定】按上市年限分两档，本档管**上市 5–10 年**：\n"
            "  分红融资比 **< 50%** —— 每向市场要 1 块钱，还给股东不到 5 毛。\n"
            "  5–10 年的公司还在扩张期，50% 是合理的及格线。\n"
            "  **上市不满 5 年的一律不评判**（时间太短，看不出来）。\n"
            "\n"
            "【上市不满 5 年怎么处理】用户原话「5 年以下我们先不考虑，就不买它的股票」。\n"
            "本规则用 `min_listing_years=5` 把它排除在**评判**之外（不拉黑、也不放行）。\n"
            "若你要的是「上市不满 5 年**一律回避**」，把「次新股」那条的\n"
            "`max_listing_years` 从 1 改成 5 即可，不必新增规则。\n"
            "\n"
            "【为什么单位是「年」而不是年份】相对年限，永不失效（回扣 U9）。"
        ),
        conditions={"logic": "AND", "rules": [
            {"field": "dividend_financing_ratio_pct", "op": "<", "value": 50},
        ]},
        pool={"min_listing_years": 5, "max_listing_years": 10},
        enabled_by_default=True,
    ),
    PresetRule(
        key="goodcompany.cash_sink_10plus",
        name="掏钱型公司·上市 10 年以上（好公司反面①b · 更严）",
        note=(
            "同 ①a 的依据，但**门槛更严**（用户 2026-11-22 裁定）：\n"
            "「10 年以后，你再怎么成长也已经差不多了，应该从股东那里拿的东西，\n"
            "要少于你送给股东的东西。」\n"
            "\n"
            "【本档口径】上市 **10 年以上**，分红融资比必须 **≥ 100%**，\n"
            "低于 100% 即拉黑——也就是「累计分红 < 累计融资」就算不合格。\n"
            "经济含义很直白：一个活了十年的公司，从股东手里拿的应该已经少于它还给股东的。\n"
            "\n"
            "【为什么两档要分开写】一条规则只能有一套条件；用 `min_listing_years`\n"
            "与 `max_listing_years` 把年限切成互不重叠的两段，判定标准才能各管各的。\n"
            "两档合起来正好覆盖「上市 ≥ 5 年」的全部公司，不重不漏。"
        ),
        conditions={"logic": "AND", "rules": [
            # **必须严格大于 100%**：正好 100% 意味着"给的和拿的相等"，
            # 不是"给得更多"，同样拉黑。用户原话：「分红融资比必须大于 100%」。
            {"field": "dividend_financing_ratio_pct", "op": "<=", "value": 100},
        ]},
        pool={"min_listing_years": 10},
        enabled_by_default=True,
    ),
    PresetRule(
        key="goodcompany.dirty_balance_sheet",
        name="资产负债表不干净（好公司反面③·高商誉或高杠杆）",
        note=(
            "【原话】蓝筹「最优质」量化下来是「分红稳定、**现金流扎实、资产负债表干净**」\n"
            "（2018-01-29 界面）；他多次点名「不存在高额商誉」是选股条件之一。\n"
            "\n"
            "【⚠️ 先排除金融股·用户 2026-11-22 要求】\n"
            "「能不能先把银行、保险这种行业排除掉，再筛选？因为每个行业都不同，\n"
            "我不希望有些行业先天就不被划入名单。」\n"
            "**这个要求是对的**：银行的「负债」是存款、保险的「负债」是准备金，那是它们的\n"
            "原材料，不是杠杆。用同一把尺子量，金融股会先天被判死刑。\n"
            "所以本规则第一条就是 `is_financial = 0`。\n"
            "`is_financial` 由 `app/core/sector.py::is_financial_sql()` 在 base_pool 里算好\n"
            "（银行 + 非银金融，含 csrc 兜底），**不在 DSL 里手写第二份行业判据**（AGENTS R1）。\n"
            "\n"
            "【量化·两条任一成立即命中】\n"
            "  ① 高商誉：`goodwill_ratio >= 0.20`——商誉占净资产 20% 以上，一旦减值就是净资产塌方；\n"
            "  ② 高杠杆 + 利息都还不动：`debt_ratio >= 0.80` 且 `interest_coverage < 2`\n"
            "     ——经营利润只够付两倍利息，任何风吹草动都会出问题。\n"
            "\n"
            "【为什么用 OR 不是 AND】这两条是**不同的病**：有的公司商誉高但负债低，\n"
            "有的负债高但没商誉。要求同时成立会漏掉绝大多数真实风险。\n"
            "\n"
            "【已知误杀】地产、建筑、公用事业等重资产行业的 `debt_ratio` 天然偏高，\n"
            "仍可能被第 ② 条命中。介意的话把本条关掉。"
        ),
        conditions={"logic": "AND", "rules": [
            {"field": "is_financial", "op": "=", "value": 0},
            {"logic": "OR", "rules": [
                {"field": "goodwill_ratio", "op": ">=", "value": 0.20},
                {"logic": "AND", "rules": [
                    {"field": "debt_ratio", "op": ">=", "value": 0.80},
                    {"field": "interest_coverage", "op": "<", "value": 2},
                ]},
            ]},
        ]},
        enabled_by_default=True,
    ),
    PresetRule(
        key="goodcompany.paper_profit",
        name="纸面利润（好公司反面④·利润不落真钱）",
        note=(
            "【原话】「上市公司要**真实**，真实而不造假的上市公司才是保护散户最重要的基础」\n"
            "（2024-07 上海证券报退休访谈）；2018 年那篇也把「现金流扎实」列为优质量化标准。\n"
            "\n"
            "【量化】`cf_to_net_profit < 0.5`（经营现金流不到净利润的一半）\n"
            "且 `income.net_profit > 0`（还在盈利）且 `net_profit_yoy > 0`（还在报增长）。\n"
            "\n"
            "【它抓的是什么】利润在涨、账上没钱进来 → 钱要么堆在应收账款和存货里，\n"
            "要么利润本身就是「做」出来的。这是**造假的代理信号，不是造假证据**——\n"
            "下游的应收/存货周转要去 `indicator_ext` 看具体科目才能定性。\n"
            "\n"
            "【已知误杀】高速扩张期的公司（先垫资后收款）、以及银行/保险（现金流定义不同）\n"
            "会被误伤。"
        ),
        conditions={"logic": "AND", "rules": [
            {"field": "cf_to_net_profit", "op": "<", "value": 0.5},
            {"field": "income.net_profit", "op": ">", "value": 0},
            {"field": "net_profit_yoy", "op": ">", "value": 0},
        ]},
        enabled_by_default=True,
    ),
    PresetRule(
        key="goodcompany.debt_funded_dividend",
        name="借贷分红（好公司反面⑤ · 借钱给股东发红包）",
        note=(
            "【为什么这条最硬：它有**监管原文**可依，不用我猜阈值】\n"
            "证监会《上市公司监管指引第 3 号——上市公司现金分红》（2023-12 修订）明确：\n"
            "「**对资产负债率较高且经营活动现金流量不佳、存在大比例现金分红情形的公司\n"
            "保持重点关注，防止对企业生产经营、偿债能力产生不利影响。**」\n"
            "——三个要素并列：①资产负债率较高 ②经营活动现金流量不佳 ③大比例现金分红。\n"
            "本规则就是把这句监管原文翻成三个可执行条件，**一一对应**。\n"
            "\n"
            "【量化】\n"
            "  ① `debt_ratio >= 0.60` —— 资产负债率 60% 以上算「较高」。\n"
            "     ⚠️ 监管没给具体数字，**60% 是我的判断值**，不是官方阈值。\n"
            "  ② `cf_to_net_profit < 1` —— 经营现金流还没利润多，即「现金流量不佳」。\n"
            "     1.0 是有含义的整数：现金流与净利润持平，低于它说明利润没变成钱。\n"
            "  ③ `payout_ratio >= 0.80` —— 分红占净利润 80% 以上算「大比例」。\n"
            "     ✅ **80% 有官方出处**：沪深交易所对拟上市企业「大额分红」的认定红线就是\n"
            "     「报告期三年累计分红金额占同期净利润比例超过 80%」。\n"
            "  外加 `income.net_profit > 0`：没有利润就谈不上「分红比例」，也避免分母为负\n"
            "  （亏损公司再分红属于另一类问题，不在本规则口径内）。\n"
            "\n"
            "【它抓的是什么】公司一边背着六成负债、主业挣的钱还没账面利润多，\n"
            "一边把八成以上利润分掉——**那笔红包的钱只能是借来的或吃老本**。\n"
            "这正是监管点名要「重点关注」的形态。\n"
            "\n"
            "【更严的可选写法·严格蕴含「分红 > 经营现金流」】\n"
            "若把 ③ 提到 `payout_ratio >= 1` 且 ② 提到 `cf_to_net_profit <= 1`，\n"
            "则「分红 ≥ 净利润 ≥ 经营现金流」恒成立，即**数学上保证分红金额超过经营现金流**，\n"
            "差额必然来自融资/借款/存量现金。想更狠就把这两处改成 1。\n"
            "\n"
            "【已知误杀】强周期行业在景气高点：利润高、负债还没还完、分红也大方，\n"
            "但现金流时点错位会被判进来。金融股不在此列（负债天然 90%+），\n"
            "本规则**没有**排金融——因为银行的分红本来就该用监管资本充足率单独看，\n"
            "若要排除金融股，在最前面加一条 `is_financial = 0` 即可。"
        ),
        conditions={"logic": "AND", "rules": [
            {"field": "income.net_profit", "op": ">", "value": 0},
            {"field": "debt_ratio", "op": ">=", "value": 0.60},
            {"field": "cf_to_net_profit", "op": "<", "value": 1},
            {"field": "payout_ratio", "op": ">=", "value": 0.80},
        ]},
        enabled_by_default=True,
    ),
)

PRESET_RULES = PRESET_RULES + GOOD_COMPANY_INVERSE_RULES
PRESET_BY_KEY = {rule.key: rule for rule in PRESET_RULES}



# ─── 规则库 CRUD ──────────────────────────────────────────────────


def ensure_preset_rules(sqlite: Any) -> int:
    """把预置规则灌进规则库（幂等：只插缺的，不覆盖用户改过的）。

    返回本次新插入的条数。用户改过名字/条件/开关的规则**不会被重置**——
    否则每次启动都把自己的调整冲掉。

    另外负责**退役**（`RETIRED_PRESET_KEYS`）：已从 `PRESET_RULES` 移除的预置规则，
    要把库里残留的那一行连同它名下的浮动成员一起删掉，否则「删规则」在已装库上不生效。
    只删 `origin='preset'` 的行——用户自建的规则同名同 key 也不会被误删。
    """
    if sqlite is None:
        return 0
    _purge_retired_presets(sqlite)
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


def _purge_retired_presets(sqlite: Any) -> int:
    """删掉已退役的预置规则及其浮动成员，返回删除条数。幂等。"""
    if not RETIRED_PRESET_KEYS:
        return 0
    placeholders = ", ".join("?" for _ in RETIRED_PRESET_KEYS)
    keys = list(RETIRED_PRESET_KEYS)
    rows = sqlite.query(
        f"SELECT id FROM {RULES_TABLE} "
        f"WHERE rule_key IN ({placeholders}) AND origin = 'preset'",
        keys,
    )
    if not rows:
        return 0
    ids = [int(row["id"]) for row in rows]
    with sqlite.transaction() as conn:
        for rule_id in ids:
            conn.execute(f"DELETE FROM {MEMBERSHIP_TABLE} WHERE rule_id = ?", [rule_id])
        conn.executemany(f"DELETE FROM {RULES_TABLE} WHERE id = ?", [[i] for i in ids])
    logger.info("已退役 %s 条预置规则（其浮动成员一并清理）", len(ids))
    return len(ids)


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
        # 池参数从规则里取。`min_listing_years` 用于"上市满 N 年才评判"——
        # 例如「掏钱型公司」要满 5 年，「次新股」要 max_listing_years=1。
        # 全是**相对年限**，绝不出现绝对年份。
        min_listing_years=int(pool.get("min_listing_years") or 0),
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
