"""单次增量更新的**主流程编排**（reports/140 §2.5 拆分的第 5 块，也是最后一块）。

## 这个模块是什么

`IncrementalUpdater._run_incremental_update_flow`（493 行）是"一次增量更新到底按什么
顺序跑什么"的唯一答案：前置检查 → 价格 → 股本/universe → 公告/财务 → 补采域 →
重试 → 派生域重建 → 指标快照 → 各低频域。它自己几乎不含 SQL 与抓取逻辑（那些都在
`IncrementalUpdater` 的方法里），它做的是**编排 + 步骤入册 + 状态汇总**。

## 为什么它是"步骤清单的事实来源"

模块内有 30 处 `report_step(...)`，覆盖 26 个步骤名。`report["steps"]` 的**首次出现
顺序**就是 `app/core/update_steps.py::STEP_ORDER` 描述的那份步骤清单的运行时来源，
`tests/regression/test_update_step_inventory.py` 钉着它。所以本模块的搬家是**逐行平移**：
`report_step` 调用点一个没动、顺序一个没变（搬移前后用脚本抽取首次出现顺序逐行对拍）。

## 怎么切的

没有切。`prices_step.py` 那种"选谁/怎么抓/怎么结算"的再切分在这里没有落点：这段代码
本来就只有一层顺序编排，硬切成 3 个子函数只会把局部量的手工传递变成新的错误来源，而
步骤顺序恰恰是最不能出错的东西。搬移是**纯机械变换**：

* `self.` → `owner.`（同一个 `IncrementalUpdater` 实例，鸭子类型，不 import 它）；
* 四个"只有一份实现"的依赖改成显式注入（见下）；
* 方法体 8 空格 → 函数体 4 空格的左移：搬移前已用 tokenize 断言方法体内**没有多行
  字符串字面量**，因此这一步不可能改到任何字符串常量（含 SQL 与日志文案），搬完再用
  AST 把前后两份 `str` 常量集合逐条对拍。

## 边界（本模块不做什么）

- **不 import `app.core.update`**（循环依赖，`test_update_structure.py` 守着）。状态一律
  经由 `owner` 显式读写。
- **不自己打日志实例**：`logger` 由调用方注入，仍是 `app.core.update` 那一个 logger
  对象——`caplog.at_level(logger="app.core.update")` 与日志通道逐字不变。
- **不复制四份私有实现**，全部按入参接收（搬移前它们是 `update.py` 的模块级私有名）：
  `announcement_step_result`（`_announcement_step_result`）、`merge_indicator_steps`
  （`_merge_indicator_steps`，契约 §2 状态归并的唯一实现）、`classify_announcement`
  （公告分类关键词的唯一实现）、`announcement_dividend_category`
  （`_ANNOUNCEMENT_DIVIDEND_CATEGORY` 常量）。
- 纯函数依赖（`STEP_NAMES` / `aggregate_job_status` / `cn_today` / `datetime` / `UTC` /
  `json`）直接 import——它们是"没有第二个实现"的单一真相，不构成循环。

## 回归网

``tests/regression/test_update_step_inventory.py``（步骤清单与顺序）、
``test_update_orchestration.py``（整条链的编排与状态汇总）、
``test_update_job_and_progress.py``（job_logs / 进度回调）、
``test_manual_chain_scheduling.py`` / ``test_auto_update_progress.py``（手工链与
自动更新接线）、``test_incremental_update_scope.py``（范围收缩）、
``test_announcement_check.py``（公告入册与游标）、
``test_qfq_recompute_trigger.py`` / ``test_yoy_caliber_and_technical_coverage.py``
（`assert_calls` 顺委托壳追到本模块的 `owner._load_tech_recompute_pending` /
`_finalize_tech_recompute` / `_finalize_technical_pending` 与 `*..._pending_codes` 展开）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.core.financial_period import cn_today
from app.core.job_status import aggregate_job_status
from app.core.update_steps import STEP_NAMES

__all__ = ["run_incremental_update_flow"]


def run_incremental_update_flow(
    owner: Any,
    max_stocks: int,
    progress_cb: Callable[[str, dict[str, Any]], None] | None,
    job_id: str,
    started_at: str,
    detail_cb: Callable[[str, dict[str, Any]], None] | None = None,
    *,
    logger: logging.Logger,
    announcement_step_result: Callable[[dict[str, Any] | None], dict[str, Any]],
    merge_indicator_steps: Callable[..., dict[str, Any]],
    classify_announcement: Callable[[str | None], str],
    announcement_dividend_category: str,
) -> dict[str, Any]:
    """更新主流程（调用方已持有跨进程更新锁）。

    实现已从 ``app.core.update.IncrementalUpdater._run_incremental_update_flow``
    整体搬到本模块（reports/140 §2.5 第 5 块）。签名、返回值、异常与日志通道
    一字不改：``owner`` 是同一个 ``IncrementalUpdater`` 实例，``logger`` 是同一个
    logger 对象，四个注入项都仍是 ``app.core.update`` 里那份唯一实现。

    实现里 30 处 ``report_step(...)`` 的**首次出现顺序**就是步骤清单的事实来源
    （见模块文档），因此这段代码的搬移方式是逐行平移，不允许顺手重排。
    """

    # 先检查
    # 公告会在价格步骤之后以 persist=True 抓取。前置阶段只判断本地
    # 价格/重试状态，避免一次更新重复抓公告并阻塞每日价格新鲜度。
    check_report = owner.run_incremental_check(include_announcements=False)

    report: dict[str, Any] = {
        "check": check_report,
        "started_at": started_at,
        "job_id": job_id,
        "steps": {},
    }

    def report_step(name: str, step: dict[str, Any]) -> dict[str, Any]:
        # 步骤名必须登记在 update_steps.STEP_ORDER（更新步骤的唯一可读索引）；只告警不改行为。
        if name not in STEP_NAMES:
            logger.warning("步骤 %r 未登记在 app/core/update_steps.py::STEP_ORDER", name)
        report["steps"][name] = step
        if progress_cb is not None:
            progress_cb(name, step)
        return step

    def indicator_progress(info: dict[str, Any]) -> None:
        """把指标重算的细粒度进度转成状态页可展示的 live 进度。"""
        if detail_cb is not None:
            detail_cb("indicators", info)

    # 1. 价格新鲜度是每日启动的首要目标。低频股票池/行业或公告源
    # 变慢时，不应阻塞已上市股票的日线补齐。
    if check_report["new_trading_days"]:
        report_step("trading_dates", owner._update_trading_dates())
    price_step = report_step("prices", owner._update_prices_incremental(max_stocks, detail_cb))
    updated_price_codes = price_step.pop("_updated_codes", [])
    price_xdxr_codes = price_step.pop("_xdxr_codes", [])
    price_raw_full_refetch = price_step.pop("_raw_full_refetch", False)
    # 依据 S7 §2.2 J6：上一轮因 qfq 滞后进入待重算名单的股票，本轮**无条件**并入重算
    # 集合（与指纹/价格是否变化无关），重算成功后从名单移除。
    tech_pending_codes = owner._load_tech_recompute_pending()
    # 依据 S10 §9.2 P0-5：价格步骤刚写入 qfq 的股票（含上一轮失败残留）
    # 必须强制重算技术三列。走价格域增量即可，不升级为整股全量重算。
    technical_pending_codes = owner._load_technical_pending()

    if max_stocks > 0:
        if (
            price_step["status"] == "success"
            and (
                price_step.get("success", 0) > 0
                or tech_pending_codes
                or technical_pending_codes
            )
        ):
            from app.core.indicators.calculator import IndicatorCalculator

            calculator = IndicatorCalculator(duck=owner.duck, sqlite=owner.sqlite)
            price_force_codes = (
                set(price_xdxr_codes)
                | set(tech_pending_codes)
                | set(technical_pending_codes)
            )
            if price_raw_full_refetch:
                price_force_codes.update(updated_price_codes)
            bounded_codes = list(
                dict.fromkeys([
                    *updated_price_codes,
                    *tech_pending_codes,
                    *technical_pending_codes,
                ])
            )
            price_method = getattr(
                calculator, "compute_price_sensitive_for_codes", None,
            )
            snapshot_step = (
                price_method(
                    bounded_codes,
                    force_codes=price_force_codes,
                    progress_cb=indicator_progress,
                )
                if price_method is not None
                else calculator.compute_snapshot_for_codes(
                    bounded_codes, progress_cb=indicator_progress,
                )
            )
            if tech_pending_codes:
                snapshot_step["tech_recompute_pending_cleared"] = (
                    owner._finalize_tech_recompute(snapshot_step, tech_pending_codes)
                )
            if technical_pending_codes:
                snapshot_step["technical_pending_cleared"] = (
                    owner._finalize_technical_pending(
                        snapshot_step, technical_pending_codes,
                    )
                )
            report_step("indicators", snapshot_step)
        # 2026-11-14 清理：早先这里有一个"blocked 时先写 partial"的兜底，
        # 而主流程随后会用 `_announcement_step_result()` 覆盖它（写两次、
        # 后者覆盖前者）。已删除死写；有界更新（--max-stocks）按既有契约
        # 只跑价格 + 指标两段，不在此处塞入公告步骤。
        report["status"] = aggregate_job_status(report["steps"])
        report["finished_at"] = datetime.now(UTC).isoformat()
        logger.info("有界价格更新完成: %s", report["status"])
        return report

    # PRD §7.7 第 4 项: 股本与上市名单（新股/退市/股本变化），
    # 以及 PRD §24 CSRC 行业分类（低频刷新）。
    # P1: 股本变化后必须触发快照重算，否则 total_market_cap 等
    # 市值类指标仍使用旧快照（口径过期风险）。
    shares_before = owner._share_capital_fingerprint()
    report_step("universe", owner._refresh_universe_metadata())
    shares_after = owner._share_capital_fingerprint()
    share_capital_changed = shares_before != shares_after
    report["share_capital_changed"] = share_capital_changed

    # 2026-11-12 P1：公告检查无论 available/partial/unavailable/truncated
    # 都必须入册——旧实现只在 check_report["blocked"] 分支 report_step，
    # 当天两轮的截断/失败在 job_logs 里完全不可见。
    try:
        announcement_check = owner._check_new_announcements(
            persist=True, detail_cb=detail_cb,
        )
    except Exception as error:  # noqa: BLE001 - 公告失败不得炸掉整轮更新
        logger.warning("公告检查异常: %s", error)
        announcement_check = {
            "status": "unavailable",
            "reason": "announcement_check_exception",
            "checked_remote": False,
            "affected_stock_codes": [],
            "affected_announcements": {},
            "all_new_announcements": {},
            "errors": [str(error)],
            "truncated": False,
        }
    report_step("announcements", announcement_step_result(announcement_check))
    report["check"]["announcement_check"] = announcement_check
    # 上一轮财务刷新失败/pending 的公告已持久化在 retry_list。把它们
    # 并入本轮待刷新集合：游标推进后不再重复翻 45 天公告，pending 也不丢。
    pending_items = owner._pending_announcement_items()
    pending_codes = sorted({str(item["stock_code"]) for item in pending_items})
    for item in pending_items:
        code = str(item["stock_code"])
        announcement_check.setdefault("affected_announcements", {}).setdefault(code, [])
        existing_ids = {
            str(existing.get("announcement_id"))
            for existing in announcement_check["affected_announcements"][code]
        }
        if item.get("announcement_id") not in existing_ids:
            announcement_check["affected_announcements"][code].append(item)
    announcement_codes = sorted(set(announcement_check.get("affected_stock_codes", [])) | set(pending_codes))
    announcement_check["affected_stock_codes"] = announcement_codes
    # B 股（200/900）不在任何免费财务主源覆盖范围，重复 retry 只会产生
    # 永续失败。与不可补字段同策略：直接入册为已处理、清理 retry，
    # 不进入财务刷新，也不在页面制造噪音。
    unsupported_codes = {
        code for code in announcement_codes
        if owner._is_b_share_stock(code)
    }
    if unsupported_codes:
        for code in sorted(unsupported_codes):
            owner._mark_announcements_seen(
                code,
                announcement_check.get("affected_announcements", {}).get(code, []),
            )
            owner._resolve_announcement_retries(code)
        announcement_codes = [
            code for code in announcement_codes
            if code not in unsupported_codes
        ]
        announcement_check["affected_stock_codes"] = announcement_codes
    financials: dict[str, Any] | None = None
    if announcement_codes:
        financials = owner._refresh_financials(announcement_codes, detail_cb=detail_cb)
        report_step("financials", financials)
        # 只有同一批公告里出现“权益分派实施”等分红除权公告时，才刷新
        # dividends/xdxr。中报季每天上千份财报公告，逐股重拉分红域会
        # 白白增加数小时请求；非分红财报不需要该步骤。
        market_action_codes = sorted({
            code
            for code, items in announcement_check.get("all_new_announcements", {}).items()
            if any(
                item.get("_announcement_category") == announcement_dividend_category
                or classify_announcement(item.get("title")) == "dividend"
                for item in items
            )
        })
        if market_action_codes:
            actions = owner._refresh_market_actions(
                market_action_codes, detail_cb=detail_cb,
            )
            report_step("market_actions", actions)
        # 只有真正写入新报告期的股票才登记公告；
        # 数据源延迟（pending）或刷新失败（failed）保持公告 pending 并记录重试
        refreshed_codes = set(financials["succeeded_codes"])
        for code, announcements in announcement_check["affected_announcements"].items():
            if code in refreshed_codes:
                owner._mark_announcements_seen(code, announcements)
                owner._resolve_announcement_retries(code)
            else:
                reason = (
                    "financial data source not yet ready; announcement remains pending"
                    if code in set(financials.get("pending_codes", []))
                    else "financial refresh failed; announcement remains pending"
                )
                owner._record_failure(
                    code,
                    "announcements",
                    "cninfo",
                    reason,
                    extra_json=json.dumps({"announcement_ids": [item["announcement_id"] for item in announcements]}),
                )

    # 非财务类公告只登记入册，不触发财务刷新（PRD §7.7）
    all_new = announcement_check.get("all_new_announcements", {})
    financial_set = set(announcement_codes)
    for code, announcements in all_new.items():
        if code in financial_set:
            continue
        owner._mark_announcements_seen(code, announcements)

    # 财务公告处理完即推进公告检查游标。失败/pending 已写入
    # retry_list，下一轮直接从 retry_list 续传，不必再翻 45 天公告。
    if (
        announcement_check.get("status") == "available"
        and not announcement_check.get("errors")
    ):
        owner._save_announcement_cursor()
        owner._resolve_announcement_source_retries()

    financial_step = report["steps"].get("financials", {"status": "success"})
    # 财报明细缺口有界续传：只重抓“核心字段齐全但明细字段缺失”的股票。
    # 该步骤不参与 readiness 阻断，只负责逐步补齐详情页字段。
    detail_backfill = report_step(
        "financial_detail_backfill",
        owner._refresh_financial_detail_backfill(detail_cb=detail_cb),
    )
    detail_backfilled_codes = list(detail_backfill.get("succeeded_codes", []))
    # 回购注销事件是“分红融资比（含回购注销）”的输入。此前只在 CLI
    # 手动刷新，自动更新从未调度，指标会随回购进度持续过期。
    buyback_step = report_step("buyback", owner._refresh_buyback())
    buyback_changed_codes = list(buyback_step.get("changed_codes", []))
    # 融资事件同样影响 cumulative_financing_amount / 分红融资比，必须在
    # 指标快照重算之前刷新；旧位置（统计域之后）会让新融资数据迟到一轮。
    funding_step = report_step("funding", owner._refresh_funding())
    owner._resolve_legal_empty_funding_missing()
    funding_changed_codes = list(funding_step.get("changed_codes", []))
    # 国债曲线变化会改变所有股票快照中的 div_yield_spread_*。旧流程在
    # 指标重算之后才刷曲线，利差字段会整轮陈旧；现在提前并触发全量重算。
    treasury_step = report_step("treasury_curve", owner._refresh_treasury_curve())
    treasury_changed_codes = []
    if treasury_step.get("curve_changed"):
        try:
            treasury_changed_codes = [
                row["stock_code"]
                for row in owner.duck.read_query(
                    "SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code"
                )
            ]
        except Exception as error:
            logger.warning("查询国债利差重算范围失败: %s", error)

    # 5.2 手工链自动接入①：补采域（依据 S5 D29 ②③④）。
    #     折旧摊销/资本支出与资产负债表补充科目是 indicator_ext 的直接
    #     输入，扣非净利润是 income_statement 的列补全，金融监管指标是
    #     balance_sheet 的 6 列 —— 全部必须在指标快照/extension 重建之前
    #     落地，否则这轮重建又用旧输入算一遍。
    #     顺序敏感：补采 → 扣非/监管，再进 indicator_ext。
    report_step("cashflow_supplement", owner._refresh_cashflow_supplement())
    report_step("balance_sheet_ext", owner._refresh_balance_sheet_ext())
    report_step("deducted_profit", owner._refresh_deducted_profit())
    report_step("fixed_assets", owner._refresh_fixed_assets())
    report_step("regulatory_metrics", owner._refresh_regulatory_metrics())

    # 6. 重试失败任务（先清理已达标的历史冗余与无重试路径的死循环条目）。
    #    旧位置在统计域之后：重试成功的价格/财务/分红/融资/曲线数据会
    #    再等一整轮才进入快照。现在移到指标重算之前，并把影响快照的
    #    重试成功代码并入本轮重算集合。
    retry_recompute_codes: list[str] = []
    owner._cleanup_unretryable_tasks()
    owner._cleanup_completed_announcement_retries()
    owner._cleanup_redundant_index_valuation_retries()
    owner._resolve_complete_missing_records()
    if check_report["retry_tasks"]:
        expected_date = owner._latest_expected_trading_date(
            cn_today().strftime("%Y-%m-%d")
        )
        owner._cleanup_redundant_retries(expected_date)
        refreshed_tasks = owner._check_retry_tasks()
        if refreshed_tasks:
            retries_step = report_step(
                "retries", owner._retry_failed_tasks(refreshed_tasks),
            )
            retry_recompute_codes = list(
                retries_step.get("recompute_codes", [])
            )

    # 5.3 手工链自动接入②：扩展指标域重建（依据 S5 D28，P0）。
    #     必须排在快照指标之前：ext 落后于最新完整三表期时它就是死条件
    #     （界面 55 个扩展筛选字段全 NULL）。该步骤失败只让自己 failed，
    #     不动 indicator_snapshot，也不影响 readiness。
    report_step("indicator_ext", owner._refresh_indicator_ext())
    # 5.4 ROCE 域重建（2026-11-16，依据 S9/S10）。
    #     与 indicator_ext 同样的 D28 约束：派生域没有重建触发就会冻结，
    #     而 indicator_snapshot 的 roce_* 五列在每次快照重发时会被重算
    #     （见 calculator._publish_snapshot），前提是 roce_history 是最新的。
    #     失败只让本步骤 failed，不影响主链与 readiness。
    report_step("roce", owner._refresh_roce())

    if share_capital_changed:
        # 股本/上市名单变化可能影响多只股票的市场类指标；保守全量重算。
        # 该路径低频，且与自动更新同锁，不会再现"重算与抓取并发"的竞争。
        from app.core.indicators.calculator import IndicatorCalculator

        snapshot_step = IndicatorCalculator(
            duck=owner.duck, sqlite=owner.sqlite,
        ).compute_snapshot_for_all(progress_cb=indicator_progress)
        if tech_pending_codes:
            # 全量重算已覆盖名单里的每一只 → 成功即清空（rejected 不清）。
            snapshot_step["tech_recompute_pending_cleared"] = (
                owner._finalize_tech_recompute(snapshot_step, tech_pending_codes)
            )
        if technical_pending_codes:
            # 全量重算同样覆盖技术三列 → 成功即清空。
            snapshot_step["technical_pending_cleared"] = (
                owner._finalize_technical_pending(
                    snapshot_step, technical_pending_codes,
                )
            )
        report_step("indicators", snapshot_step)
        if snapshot_step["status"] != "success":
            snapshot_step["reason"] = "price update retained; snapshot publication not ready"
    elif (
        financial_step["status"] in {"success", "partial"}
        or retry_recompute_codes
        or tech_pending_codes
        or technical_pending_codes
    ):
        from app.core.indicators.calculator import IndicatorCalculator

        calculator = IndicatorCalculator(duck=owner.duck, sqlite=owner.sqlite)
        refreshed_financial_codes = list(
            financials.get("succeeded_codes", []) if financials else []
        )
        financial_stale_codes = owner._stale_financial_snapshot_codes()
        price_stale_codes = owner._stale_price_snapshot_codes()

        # 完整重算组：财务变化/明细回填/失败重试成功，输入域不确定，保
        # 守整股重算全部指标。价格变化组只重算价格域字段；回购/融资
        # 变化组只重算分红融资比三列（2026-09-08 提速计划）。
        # 依据 S7 §2.2 J6：qfq 滞后的股票**强制**进完整重算组 —— 它们要的是价格域
        # 全量重算（波动率/回撤/区间收益读 qfq 历史），而价格域增量路径
        # 会因为"最新价/最新日期没变"直接跳过（见 compute_price_sensitive）。
        full_codes = list(dict.fromkeys([
            *refreshed_financial_codes,
            *financial_stale_codes,
            *detail_backfilled_codes,
            *retry_recompute_codes,
            *tech_pending_codes,
        ]))
        price_codes = [
            code for code in list(dict.fromkeys([
                *updated_price_codes,
                *price_stale_codes,
                # 2026-11-16 依据 S10 §9.2 P0-5：qfq 写入成功但最新价未变
                # 的股票也要进价格域重算（force 绕过 skip），否则技术三列
                # 永远停在补齐前写死的 NULL。
                *technical_pending_codes,
            ]))
            if code not in full_codes
        ]
        event_codes = [
            code for code in list(dict.fromkeys([
                *buyback_changed_codes,
                *funding_changed_codes,
            ]))
            if code not in full_codes
        ]
        price_force_codes = (
            set(full_codes) | set(price_xdxr_codes) | set(technical_pending_codes)
        )
        if price_raw_full_refetch:
            price_force_codes.update(updated_price_codes)

        snapshot_step: dict[str, Any] = {
            "status": "skipped",
            "reason": "no_changed_stocks",
            "success": 0,
            "failed": 0,
        }
        ran_indicator_step = False
        if full_codes:
            snapshot_step = calculator.compute_snapshot_for_codes(
                full_codes, progress_cb=indicator_progress,
            )
            ran_indicator_step = True
        if price_codes:
            price_method = getattr(
                calculator, "compute_price_sensitive_for_codes", None,
            )
            if price_method is None:
                price_step = calculator.compute_snapshot_for_codes(
                    price_codes, progress_cb=indicator_progress,
                )
            else:
                price_step = price_method(
                    price_codes,
                    force_codes=price_force_codes,
                    progress_cb=indicator_progress,
                )
            if ran_indicator_step:
                snapshot_step = merge_indicator_steps(snapshot_step, price_step)
            else:
                snapshot_step = price_step
            ran_indicator_step = True
        if event_codes:
            event_method = getattr(
                calculator, "refresh_funding_dividend_fields", None,
            )
            if event_method is None:
                event_step = calculator.compute_snapshot_for_codes(
                    event_codes, progress_cb=indicator_progress,
                )
            else:
                event_step = event_method(event_codes)
            if ran_indicator_step:
                snapshot_step = merge_indicator_steps(snapshot_step, event_step)
            else:
                snapshot_step = event_step
            ran_indicator_step = True
        if ran_indicator_step:
            # 依据 S7 §2.2 J6：只有真正算过才清名单；rejected/skipped 与 failed_codes
            # 里的股票继续保留，下一轮再强制重算。
            if tech_pending_codes:
                snapshot_step["tech_recompute_pending_cleared"] = (
                    owner._finalize_tech_recompute(snapshot_step, tech_pending_codes)
                )
            if technical_pending_codes:
                snapshot_step["technical_pending_cleared"] = (
                    owner._finalize_technical_pending(
                        snapshot_step, technical_pending_codes,
                    )
                )
            report_step("indicators", snapshot_step)

        if treasury_changed_codes:
            # 国债曲线变化只影响 ttm_dividend_yield / div_yield_spread_*，
            # 无需对全市场完整重算 PE/PB/成长/技术指标；专用批量刷新
            # 在正式库为秒级，取代旧的约 12 分钟全量重算。
            treasury_step = calculator.refresh_treasury_spreads(treasury_changed_codes)
            if ran_indicator_step:
                # rejected/partial 绝不能被国债利差刷新的 success 覆盖。
                report_step(
                    "indicators",
                    merge_indicator_steps(snapshot_step, treasury_step),
                )
            else:
                report_step("indicators", treasury_step)

    # 3. 业务概览低频域（依据 S1 独立域）：仅当自动集成启用且间隔到期
    #    时执行；失败保留旧值并进入独立 retry/missing，绝不阻断价格/财务。
    report_step("business_overview", owner._refresh_business_overview(detail_cb=detail_cb))

    # 4. 国债曲线已提前到指标快照重算之前执行。

    # 5. 历史股本链 + 统计域（依据 S2 P4）：有界续传；失败不阻断其他步骤
    report_step("capital_history", owner._refresh_capital_history(detail_cb=detail_cb))
    report_step(
        "research_statistics",
        owner._refresh_research_statistics(
            parallel_workers=max(1, int(owner.research_statistics_parallel_workers)),
            detail_cb=detail_cb,
        ),
    )

    # 5.4 手工链自动接入③：ETF 行情 + 港股分红（依据 S5 D29 ⑤⑥）。
    #     两者都是"独立低频域"：不进 A 股 readiness、不影响主链快照，
    #     只用各自的水位 + 每轮上限推进。放在统计域之后，避免它们的
    #     网络等待挤在价格/财务的临界路径上。
    report_step("etf_prices", owner._refresh_etf_prices())
    report_step("hk_dividends", owner._refresh_hk_dividends())

    # 5.1 融资事件/失败重试已提前到指标快照重算之前执行；这里只保留
    #     指数估值每日 1 次低频域。失败保留旧值并进入独立 retry/missing。
    report_step("index_valuation", owner._refresh_index_valuation())

    # 5.5 卖方一致预期快照（schema v48，依据 S11 §2.2 #13）。
    #     接口只给**当期横截面**、无历史参数（source_probe §1.B6），所以「今天不打点，
    #     这一天就永远没有」——它必须每天都跑。低频纪律与 etf_prices 同款：
    #     data_refresh_state 水位每日最多 1 次（值 = 北京日期），失败只让本步骤
    #     failed/partial，不影响主链快照与 readiness。
    report_step("analyst_forecast", owner._refresh_analyst_forecast())

    report["status"] = aggregate_job_status(report["steps"])
    report["finished_at"] = datetime.now(UTC).isoformat()

    logger.info(f"增量更新完成: {report['status']}")
    return report
