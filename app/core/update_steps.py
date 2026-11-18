"""更新步骤的唯一可读索引（reports/134 §5 S1 的第一步）。

## 这份清单是什么

`app/core/update.py::IncrementalUpdater._run_incremental_update_flow()` 用 500+ 行、
25 个 `report_step("name", ...)` 调用表达"一轮更新有哪几步"。**步骤清单只存在于代码
执行顺序里**：想回答"一轮更新到底有哪几步、谁先谁后"，必须从头读那 500 行；想插入
或调整一步，也最容易放到错误的位置（reports/134 §3 热点 #1 / #2）。

`STEP_ORDER` 把这份清单第一次变成**可读的数据**：它是**更新步骤的唯一可读索引**。
读清单看这里，读某个步骤怎么实现看 `update.py` 对应方法。

## 边界（本模块不做什么）

- **不执行任何东西**：纯常量 + dataclass，不 import `update.py`、不建连接、无副作用，
  因此可以被测试、文档、CLI 安全 import。
- **不是第二份事实**：真实执行顺序仍以 `_run_incremental_update_flow()` 为准，本表只是
  它的索引。不一致会被机器抓住，见下。
- 本轮只做"清单变数据"这一步；把每个域搬到 `steps/*.py`（S1 的完整拆分）不在本轮范围。

## 一致性怎么保证

1. `tests/regression/test_update_step_inventory.py` 用 AST 扫 `update.py` 里所有
   `report_step("字面量", ...)` 的首参：代码里出现的每个步骤名都必须在 `STEP_ORDER`
   登记，且 `STEP_ORDER` 的顺序必须等于代码里**首次出现**的顺序。
2. `update.py::report_step()` 内部对未登记的名字记一条 `logger.warning`（只告警，
   不抛错、不改变行为）：清单过期不会弄坏一轮更新，但漏登记能从日志抓到。

## 为什么不能只靠"代码顺序"（新人定位成本）

- 25 个 `report_step` 分散在 500 行、跨越 4 层缩进：读到第 3 步就可能滑进 `if/elif`
  分支里迷路，读完才知道全貌。
- 有界更新（`--max-stocks`）在 `prices` + `indicators` 之后**提前 return**：只看代码
  会觉得后面的 20 多步像是不可达代码，先读代码的人容易把"有界路径"当成完整清单。
- 同名步骤出现多次（`indicators` 在 `update.py` 里出现 5 次，分属有界路径、股本变化、
  财务变化、国债利差几条分支）：`grep report_step` 数出 29 次调用，会误以为有 29 步。
- 若干步骤只在条件成立时执行（`financials` / `market_actions` / `retries` /
  `trading_dates`），"有哪几步"与"今天跑了哪几步"是两件事，代码顺序只表达后者。

## 字段约定

- ``name``：与 `report_step()` 首参**逐字一致**。它同时是 ``report["steps"]`` 的 key、
  进度事件名与 `job_logs` 里的步骤名，**改名等于破坏前端展示与历史日志可比性**。
- ``label``：中文展示名。已存在的 `app/core/auto_update.py::STEP_LABELS` 只覆盖
  25 步中的 17 步（遗留的第二份清单，S8 收口），本表的 17 个同名标签与它保持一致。
- ``domain``：步骤归属的数据域，取值见 `_DOMAIN_VOCAB`；S1 后续按域拆
  `steps/<domain>.py` 时，这份归属就是拆分依据。
- ``note``：执行条件、位置理由、条件重跑说明。

## 排序口径

`STEP_ORDER` 按 `update.py` 中 `report_step(...)` **首次出现的行号**排序，即"有界更新
（`--max-stocks`）那条最短真实路径"的顺序。全量路径与此不同的地方写在各自 ``note``
里：`indicators` 在全量路径实际发生在 `roce` 之后（见其 note）。

## 加/改步骤时怎么做

1. 在 `update.py` 里加 `report_step("new_domain", ...)`；
2. 在 `STEP_ORDER` 里按首次出现位置插入同名 `UpdateStep`（否则上面的测试会失败、
   `report_step()` 也会打出 warning）；
3. 顺手补 `label` / `domain` / `note`：note 要写"什么时候会跳过"。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["STEP_BY_NAME", "STEP_NAMES", "STEP_ORDER", "UpdateStep"]


@dataclass(frozen=True)
class UpdateStep:
    """一轮增量更新中的一个步骤（`update.py::_run_incremental_update_flow` 的索引项）。

    Attributes:
        name: 步骤名，与 ``report_step()`` 首参逐字一致（进度事件 / job_logs 的 key）。
        label: 中文展示名（前端/日志用；已存在的同名标签以 `auto_update.STEP_LABELS` 为准）。
        domain: 归属数据域，取值见 `_DOMAIN_VOCAB`。
        note: 执行条件、位置理由或"条件重跑"说明。
    """

    name: str
    label: str
    domain: str
    note: str = ""


#: ``domain`` 字段的取值域（与 S1 将来的 ``steps/<domain>.py`` 一一对应）。
#: 这里用普通 dict 而不是 Enum：改域名是文档级改动，不值得引入需要迁移的枚举。
_DOMAIN_VOCAB: dict[str, str] = {
    "calendar": "交易日历（价格域的前置判据）",
    "prices": "行情价格（含 A 股日线与 ETF 行情）",
    "indicators": "指标快照发布（indicator_snapshot）",
    "derived": "派生域重建（indicator_ext / roce），必须早于快照",
    "universe": "股票池与股本（stock_meta / 上市名单 / 行业）",
    "announcements": "公告检查与入册",
    "financials": "财务三表刷新与明细回填",
    "corporate_actions": "分红除权 / 回购 / 融资等公司行动",
    "macro": "宏观曲线（国债收益率）",
    "supplement": "补采域（指标/三表的列级补全）",
    "retry": "失败任务重试",
    "low_freq": "独立低频域（各自水位 + 每轮上限，不进 A 股 readiness）",
}


STEP_ORDER: tuple[UpdateStep, ...] = (
    UpdateStep(
        name="trading_dates",
        label="交易日历",
        domain="calendar",
        note=(
            "仅当 check_report['new_trading_days'] 为真时执行（update.py::_run_incremental_update_flow "
            "首个 report_step）。有界更新与全量更新都先跑它。"
        ),
    ),
    UpdateStep(
        name="prices",
        label="股票价格",
        domain="prices",
        note=(
            "无条件执行：价格新鲜度是每日首要目标，低频股票池/行业/公告变慢不得阻塞它。"
            "有界更新（--max-stocks）跑完 prices + indicators 就提前 return，后面的步骤全都不跑。"
        ),
    ),
    UpdateStep(
        name="indicators",
        label="指标快照",
        domain="indicators",
        note=(
            "条件重跑：同名 report_step 在 update.py 出现 5 次（有界路径 / 股本变化全量重算 / "
            "财务变化分组重算 / 国债利差补齐），STEP_ORDER 只登记一次。本位置对应有界更新路径"
            "（prices 之后）；全量路径实际执行在 roce 之后、business_overview 之前，且未算过时不发步骤。"
        ),
    ),
    UpdateStep(
        name="universe",
        label="股票池与股本",
        domain="universe",
        note=(
            "全量路径第一步（有界更新已提前 return）。股本指纹变化 → 后续对全市场做一次快照全量重算。"
        ),
    ),
    UpdateStep(
        name="announcements",
        label="公告",
        domain="announcements",
        note=(
            "无条件入册：available/partial/unavailable/truncated 都必须出现在 steps 里"
            "（reports/117 教训：只在 blocked 分支登记会让截断/失败在 job_logs 里不可见）；"
            "抛异常也只记 unavailable，不炸整轮。位置在 prices 之后，前置检查只判本地状态以免重复抓公告。"
        ),
    ),
    UpdateStep(
        name="financials",
        label="财务数据",
        domain="financials",
        note=(
            "仅当公告命中财务类股票（announcement_codes 非空）时执行；"
            "B 股（200/900）不入财务刷新，只清理 retry。"
        ),
    ),
    UpdateStep(
        name="market_actions",
        label="分红除权",
        domain="corporate_actions",
        note=(
            "仅当同批公告里出现权益分派/分红/除权除息类公告（market_action_codes 非空）时执行；"
            "中报季上千份财报公告不再逐股重拉分红域。位置紧跟在 financials 之后。"
        ),
    ),
    UpdateStep(
        name="financial_detail_backfill",
        label="财务明细回填",
        domain="financials",
        note="财报明细缺口的有界续传，只补详情页字段；不参与 readiness 阻断。",
    ),
    UpdateStep(
        name="buyback",
        label="回购明细",
        domain="corporate_actions",
        note=(
            "'分红融资比（含回购注销）'的输入；必须在指标快照重算之前刷新，否则新回购事件迟到一轮。"
        ),
    ),
    UpdateStep(
        name="funding",
        label="融资事件",
        domain="corporate_actions",
        note=(
            "cumulative_financing_amount / 分红融资比 的输入；旧位置在统计域之后会让新融资数据"
            "迟到一轮，因此提前到指标重算之前。"
        ),
    ),
    UpdateStep(
        name="treasury_curve",
        label="国债曲线",
        domain="macro",
        note=(
            "div_yield_spread_* / ttm_dividend_yield 的输入；旧流程在指标重算之后刷曲线，利差字段会"
            "整轮陈旧。curve_changed 时把全市场代码并入利差专用重算（不升级为全量 PE/PB 重算）。"
        ),
    ),
    UpdateStep(
        name="cashflow_supplement",
        label="现金流补充",
        domain="supplement",
        note=(
            "折旧摊销/资本支出等补采（reports/119 D29②），是 indicator_ext 的直接输入，"
            "必须在指标快照/extension 重建前落地。开关 update.cashflow_supplement_auto_enabled=false 时 skipped。"
        ),
    ),
    UpdateStep(
        name="balance_sheet_ext",
        label="资产负债表扩展",
        domain="supplement",
        note=(
            "资产负债表补充科目补采（reports/119 D29③），同属 indicator_ext 的直接输入；"
            "开关 update.balance_sheet_ext_auto_enabled=false 时 skipped。"
        ),
    ),
    UpdateStep(
        name="deducted_profit",
        label="扣非净利润",
        domain="supplement",
        note=(
            "扣非净利润补采（reports/119 D29③）：只 UPDATE income_statement.deducted_net_profit 的 NULL 值，"
            "既有 CSMAR/主链值不动。顺序敏感：补采 → 扣非/监管 → indicator_ext。"
        ),
    ),
    UpdateStep(
        name="fixed_assets",
        label="固定资产",
        domain="supplement",
        note=(
            "固定资产净额补采（reports/119 P1-3）：只回填 NULL（2026Q3 主链该列一度只剩 1.8%）；"
            "开关 update.fixed_assets_auto_enabled=false 时 skipped。"
        ),
    ),
    UpdateStep(
        name="regulatory_metrics",
        label="监管指标",
        domain="supplement",
        note="balance_sheet 的 6 列监管口径补采（reports/119 D29④），必须在 balance_sheet_ext 之后。",
    ),
    UpdateStep(
        name="retries",
        label="失败重试",
        domain="retry",
        note=(
            "仅当 check_report['retry_tasks'] 非空时执行。位置在指标重算之前：重试成功的价格/财务/"
            "分红/融资/曲线代码会并入本轮重算集合，否则要再等一整轮才进快照。"
        ),
    ),
    UpdateStep(
        name="indicator_ext",
        label="扩展指标",
        domain="derived",
        note=(
            "扩展指标域重建（reports/119 D28）。必须排在快照指标之前：ext 落后于最新完整三表期时"
            "它就是死条件（界面 55 个扩展筛选字段全 NULL）。失败只让自己 failed，不动 indicator_snapshot。"
        ),
    ),
    UpdateStep(
        name="roce",
        label="资本回报率（ROCE）",
        domain="derived",
        note=(
            "roce_history 重建（reports/130/131）。indicator_snapshot 的 roce_* 五列在每次快照重发时"
            "由 roce_history 重算，因此它必须早于 indicators。失败只让本步骤 failed，不影响主链。"
        ),
    ),
    UpdateStep(
        name="business_overview",
        label="业务概览",
        domain="low_freq",
        note=(
            "独立低频域（reports/67）：自动集成开关 + 间隔到期才执行；失败保留旧值并进独立 retry/missing，"
            "绝不阻断价格/财务。它是全量路径里紧跟在指标重算之后的步骤。"
        ),
    ),
    UpdateStep(
        name="capital_history",
        label="历史股本",
        domain="low_freq",
        note="历史股本链有界续传（reports/68 P4）；失败不阻断其他步骤。",
    ),
    UpdateStep(
        name="research_statistics",
        label="历史统计",
        domain="low_freq",
        note="统计域刷新，按 research_statistics_parallel_workers 并行；失败不阻断其他步骤。",
    ),
    UpdateStep(
        name="etf_prices",
        label="ETF 行情",
        domain="low_freq",
        note=(
            "独立低频域（reports/119 D29⑤）：不进 A 股 readiness、不影响主链快照。放在统计域之后，"
            "避免它的网络等待挤在价格/财务临界路径上；开关 update.etf_prices_auto_enabled=false 时 skipped。"
        ),
    ),
    UpdateStep(
        name="hk_dividends",
        label="港股分红",
        domain="low_freq",
        note=(
            "独立低频域（reports/119 D29⑥）：与 etf_prices 同为'水位 + 每轮上限'推进，"
            "不参与 A 股 readiness；开关 update.hk_dividends_auto_enabled=false 时 skipped。"
        ),
    ),
    UpdateStep(
        name="index_valuation",
        label="指数估值",
        domain="low_freq",
        note=(
            "每日 1 次低频域：融资事件/失败重试已提前到指标重算之前，尾部只剩它（update.py 注释 5.1）；"
            "失败保留旧值并进独立 retry/missing，不阻断主链。"
        ),
    ),
    UpdateStep(
        name="analyst_forecast",
        label="一致预期快照",
        domain="low_freq",
        note=(
            "schema v48 每日快照（reports/139 §2.2 #13 / source_probe §1.B6）：接口只给当期横截面、"
            "无历史参数，今天不打点这一天就永远缺，所以必须每天跑；水位 "
            "data_refresh_state['analyst_forecast_last_refresh'] = 北京日期，每天最多 1 次。"
            "失败只让本步骤 failed/partial，不影响主链快照与 readiness；有界更新"
            "（--max-stocks）在前面的阶段就提前 return，不跑本步骤。"
        ),
    ),
)

#: 步骤名集合：`update.py::report_step()` 的白名单，由 `STEP_ORDER` 派生，禁止单独维护。
STEP_NAMES: frozenset[str] = frozenset(step.name for step in STEP_ORDER)

#: 名字 → 步骤，供日志/前端/测试按名查找。
STEP_BY_NAME: dict[str, UpdateStep] = {step.name: step for step in STEP_ORDER}
