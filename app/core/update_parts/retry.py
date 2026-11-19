"""retry_list 的落地、退避排期与结算（reports/140 §2.5 拆分的第 2 块）。

## 这个模块是什么

``retry_list`` 是"失败保留旧值 + 生成重试列表"（PRD §7.4 L4）的持久化载体。围绕它的
六件事原先散落在 ``IncrementalUpdater`` 的 6,600 行里、彼此相隔上千行：

* 失败一次怎么记：``mark_retry_failed`` —— 递增 ``retry_count``、写 ``error``，
  并按 ``MIN(24, 1 << MIN(retry_count + 1, 5))`` 小时排下一次 ``next_retry_at``；
* 耗尽 ``max_retries`` 后怎么收尾：同一函数里转记 ``missing_list``（reason
  ``retry_exhausted``，``field_name`` 带 ``error_kind`` 便于追溯）；
* 逐股重试成功后如何先落值再删队列项：``persist_price_retry``；
* 两类"不是故障"的失败（``legal_empty`` / ``source_unsupported``）如何**不占重试
  次数**直接结算成 missing：``settle_non_retryable_error``（单条，重试路径用）与
  ``settle_non_retryable_retries``（批量，读队前用）；
* 读待重试队列：``check_retry_tasks``；
* 清理"没有逐股重试路径"的数据域的死循环条目：``cleanup_unretryable_tasks``。

把这一组放在一起，是为了让"一个失败任务的生命周期"在一屏内读完，而不是
在 6,600 行里跳 5 次。

## 边界（本模块不做什么）

- **不 import `app.core.update`**（会成循环依赖）。所有状态显式传参：
  ``sqlite`` / ``duck`` 是 store（鸭子类型），回调用 ``Callable`` 参数传入。
- **不改任何口径**：SQL 语义、日志文案、异常类型、退避公式与搬移前**语义等价**；
  ⚠️ 2026-11-19 审查更正：初稿这里写的是「逐字一致」，**实测不成立** —— 有 3 个函数的
  SQL 字面量缩进被重新排版（SQLite 不敏感空白，故无语义差异），且 `self.x` → 显式参数化
  必然逐字不同。准确表述是 **6/6 语义等价、0/6 字节相同**。别再拿"逐字一致"当证据。
  连 ``logger`` 都由调用方注入，日志通道仍是 ``app.core.update``。
- 这里只做 ``retry_list`` 的读写与结算，**不做**抓取、不做适配器调度
  （``_retry_failed_tasks`` 的编排仍在 ``update.py``：它受
  ``test_adapter_injection_shared_manager.py`` 的 ``inspect.getsource`` 接线守卫约束）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

# 契约 §1：这两类"失败"不是故障——源正常但本来就没有数据 / 源根本不支持该票。
# 它们不得占用 retry 次数，必须直接如实登记为 missing_list。
# （搬到本模块后仍由 `update.py` 转发导出，既有 `from app.core.update import
#   NON_RETRYABLE_ERROR_KINDS` 调用点不变。）
NON_RETRYABLE_ERROR_KINDS: frozenset[str] = frozenset({"legal_empty", "source_unsupported"})
# legal_empty → source_empty；source_unsupported → source_unsupported（契约 §5）。
ERROR_KIND_REASON_CODE: dict[str, str] = {
    "legal_empty": "source_empty",
    "source_unsupported": "source_unsupported",
}

__all__ = [
    "ERROR_KIND_REASON_CODE",
    "NON_RETRYABLE_ERROR_KINDS",
    "check_retry_tasks",
    "cleanup_unretryable_tasks",
    "mark_retry_failed",
    "persist_price_retry",
    "settle_non_retryable_error",
    "settle_non_retryable_retries",
]


def mark_retry_failed(
    sqlite: Any,
    retry_id: int,
    error: str,
    *,
    logger: logging.Logger,
) -> None:
    """Retain a failed retry and update its diagnostic state.

    达到 max_retries 后：除 announcements（是公告 pending 的持久化标记，
    由财务刷新路径持续消费）外，把条目转记到 missing_list 并移出活跃
    重试队列，避免永久滞留且页面不可见（2026-09-08 红队修复）。
    """
    now = datetime.now(UTC).isoformat()
    with sqlite.transaction() as conn:
        cursor = conn.execute(
            """UPDATE retry_list
               SET retry_count = retry_count + 1, error = ?, last_attempt = ?,
                   next_retry_at = datetime('now', '+' || MIN(24, 1 << MIN(retry_count + 1, 5)) || ' hours')
               WHERE id = ?""",
            [error[:500], now, retry_id],
        )
        if cursor.rowcount != 1:
            return
        rows = conn.execute(
            """SELECT stock_code, data_type, extra_json, retry_count, max_retries,
                      COALESCE(error_kind, 'unknown') AS error_kind
               FROM retry_list WHERE id = ?""",
            [retry_id],
        ).fetchall()
        if not rows:
            return
        row = rows[0]
        stock_code = row["stock_code"]
        data_type = row["data_type"]
        if row["retry_count"] < row["max_retries"] or data_type == "announcements":
            return
        # 契约 §5：耗尽时 reason_code 保持 retry_exhausted，field_name 带上
        # error_kind 便于追溯"为什么耗尽"（unknown 时不加后缀，兼容旧行）。
        error_kind = str(row["error_kind"] or "unknown")
        field_name = f"retry_exhausted:{data_type}"
        if error_kind not in {"", "unknown", "none"}:
            field_name = f"{field_name}:{error_kind}"
        conn.execute(
            """INSERT INTO missing_list (stock_code, field_name, reason_code)
               VALUES (?, ?, 'retry_exhausted')
               ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
               DO UPDATE SET reason_code = excluded.reason_code,
                             detected_at = CURRENT_TIMESTAMP""",
            [stock_code, field_name],
        )
        conn.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])


def persist_price_retry(
    duck: Any,
    stock_code: str,
    adjust: str,
    rows: list[dict[str, Any]],
) -> None:
    """Persist a successful price retry before its queue item is removed."""
    if adjust not in {"raw", "qfq"}:
        raise ValueError(f"unsupported price adjustment: {adjust}")
    table = "price_daily_raw" if adjust == "raw" else "price_daily_qfq"
    with duck.transaction() as connection:
        connection.executemany(
            f"""INSERT INTO {table}
                (stock_code, trade_date, open, high, low, close, volume, turnover,
                 turnover_rate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stock_code, trade_date) DO UPDATE SET
                  open=COALESCE(excluded.open, {table}.open),
                  high=COALESCE(excluded.high, {table}.high),
                  low=COALESCE(excluded.low, {table}.low),
                  close=COALESCE(excluded.close, {table}.close),
                  volume=COALESCE(excluded.volume, {table}.volume),
                  turnover=COALESCE(excluded.turnover, {table}.turnover),
                  turnover_rate=COALESCE(excluded.turnover_rate, {table}.turnover_rate)""",
            [
                (
                    stock_code,
                    row.get("trade_date"),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    row.get("volume"),
                    row.get("turnover"),
                    row.get("turnover_rate"),
                )
                for row in rows
            ],
        )


def settle_non_retryable_error(
    sqlite: Any,
    record_missing: Callable[[str, str, str], None],
    retry_id: int,
    stock_code: str,
    data_type: str,
    outcome: dict[str, Any],
    *,
    logger: logging.Logger,
) -> bool:
    """Settle a retry whose re-fetch proved the failure is not retryable.

    2026-11-12：`error_kind=legal_empty`（新股无已实施分红）与
    `source_unsupported`（北交所/B 股无该源）不是故障。此前重试路径只看
    ``status != success``，于是这些任务被反复重试直到 max_retries，最终以
    ``retry_exhausted`` 的假名义永久写进 missing_list。现在直接按真实原因
    登记 missing 并移除 retry 行，不消耗重试次数。

    Returns: True 表示已结算（调用方不应再计为 still_failing）。
    """
    error_kind = str(outcome.get("error_kind") or "")
    if error_kind not in NON_RETRYABLE_ERROR_KINDS:
        return False
    reason_code = ERROR_KIND_REASON_CODE.get(error_kind, "source_empty")
    record_missing(stock_code, data_type, reason_code)
    sqlite.execute("DELETE FROM retry_list WHERE id = ?", [retry_id])
    logger.info(
        "[增量] %s %s 判定为 %s，直接登记 missing(%s) 并移出重试队列",
        stock_code, data_type, error_kind, reason_code,
    )
    return True


def settle_non_retryable_retries(sqlite: Any, *, logger: logging.Logger) -> int:
    """Settle legal-empty / source-unsupported retries straight into missing_list.

    ``announcements`` 除外：它的 retry 行是公告 pending 的持久化标记
    （PRD §7.4），由财务刷新流程消费，不能被当成缺失结算掉。

    2026-11-12 P1（契约 §5）：``legal_empty``（新股无分红、当日无公告…）与
    ``source_unsupported``（BSE 无交叉源）不是故障。旧流程让它们和真故障
    共用"重试 5 次 → retry_exhausted"通道，于是 301688/301689/601091/920201
    在 retry_count=4/5 时就会被以假名义永久丢进 missing_list。
    现在直接结算并删除 retry 行：不占重试次数、原因如实。
    """
    try:
        rows = sqlite.query(
            """SELECT id, stock_code, data_type,
                      COALESCE(error_kind, 'unknown') AS error_kind
               FROM retry_list
               WHERE COALESCE(error_kind, 'unknown') IN ('legal_empty', 'source_unsupported')
                 AND data_type != 'announcements'"""
        )
    except Exception as error:
        logger.warning("读取不可重试任务失败: %s", error)
        return 0
    if not rows:
        return 0
    settled = 0
    for row in rows:
        kind = str(row["error_kind"] or "unknown")
        reason_code = ERROR_KIND_REASON_CODE.get(kind)
        if reason_code is None:  # pragma: no cover - SQL 已过滤，防御性分支
            continue
        try:
            with sqlite.transaction() as conn:
                conn.execute(
                    """INSERT INTO missing_list (stock_code, field_name, reason_code)
                       VALUES (?, ?, ?)
                       ON CONFLICT(stock_code, field_name) WHERE resolved_at IS NULL
                       DO UPDATE SET reason_code = excluded.reason_code,
                                     detected_at = CURRENT_TIMESTAMP""",
                    [row["stock_code"], row["data_type"], reason_code],
                )
                conn.execute("DELETE FROM retry_list WHERE id = ?", [row["id"]])
            settled += 1
        except Exception as error:
            logger.warning(
                "结算不可重试任务失败 %s/%s: %s",
                row["stock_code"], row["data_type"], error,
            )
    if settled:
        logger.info(
            "[增量] %d 条合法空/源不支持任务已直接结算为 missing（不占重试次数）",
            settled,
        )
    return settled


def check_retry_tasks(
    sqlite: Any,
    settle: Callable[[], int],
    *,
    logger: logging.Logger,
) -> list[dict]:
    """读取待重试任务列表（先结算不可重试种类，再排除它们）"""
    # 契约 §5：legal_empty / source_unsupported 不再进入重试队列，直接结算。
    settle()
    try:
        rows = sqlite.query(
            "SELECT id, stock_code, data_type, adapter, error, retry_count, extra_json, "
            "COALESCE(error_kind, 'unknown') AS error_kind "
            "FROM retry_list WHERE retry_count < max_retries "
            "AND COALESCE(error_kind, 'unknown') NOT IN ('legal_empty', 'source_unsupported') "
            "AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP) "
            "ORDER BY last_attempt ASC LIMIT 100"
        )
        return rows
    except Exception as e:
        logger.warning(f"读取重试列表失败: {e}")
        return []


def cleanup_unretryable_tasks(
    sqlite: Any,
    data_types: Iterable[str],
    recheck: Callable[[], int],
    *,
    logger: logging.Logger,
) -> int:
    """Drop retry entries whose data domain has no per-stock refetch path.

    listing_info / stock_list / trading_dates etc. are maintained by the
    universe step, not by refetch_one. Historical failures of those types
    (e.g. the akshare outage window) would otherwise fail forever on every
    retry round. Their remaining gaps stay visible in missing_list.
    """
    # 先给"一次都没真正尝试"的历史行一次干净重试，再清理无路径域。
    recheck()
    rows = sqlite.query(
        f"""SELECT id FROM retry_list
            WHERE data_type IN ({", ".join("?" for _ in data_types)})""",
        list(data_types),
    )
    if not rows:
        return 0
    ids = [row["id"] for row in rows]
    sqlite.execute(
        "DELETE FROM retry_list WHERE id IN ({})".format(
            ", ".join("?" for _ in ids)
        ),
        ids,
    )
    logger.info("[增量] 清理 %d 条无重试路径的失败任务", len(ids))
    return len(ids)
