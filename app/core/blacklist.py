"""股票黑名单 —— 读侧唯一的黑名单真源（用户态数据，存 SQLite）。

产品语义（用户 2026-11-22 裁定）：
- 个股详情页点「加入黑名单」→ 该股票**所有内容不再出现在前端任何研究面**
  （筛选/搜索/自选/详情/保存结果/CSV 导出/DSL 预览）；
- 数据库里的原始数据**一行不动**，自动更新**照常抓取**（见下「为什么不停止抓取」）；
- 唯一的例外是「数据状态」页的重试/缺失列表，按用户裁定**照常显示**——
  那是运维诊断面，不是研究面。

## 为什么存 SQLite 而不是 DuckDB

用户态数据（`watchlist` / `screening_rules` / `manual_overrides`）全部在 SQLite，
这不是历史巧合：DuckDB 是单写者 + 应用级文件锁（`storage/update_lock.py`），
自动更新子进程持锁时 Web 侧任何写都会失败；而 SQLite 走 WAL，CLI 与 Web
可同时写、自动排队。黑名单是典型用户态数据，必须跟着这条惯例走。

代价是黑名单代码集合与 DuckDB 表无法在同一条 SQL 里 JOIN，因此本模块同时
提供 `exclusion_clause()`：**排除条件只有这一个实现**，任何读侧出口都从这里取，
不得各写一份 `NOT IN`（`AGENTS R1` 一个事实只能有一个实现）。

## 为什么不顺便停止抓取（用户裁定 + 调研结论）

看起来「加入黑名单就别抓了」更彻底，实测代价远大于收益：

1. **没有单一收口点**——约 10 处独立 SQL 读 `stock_meta.is_listed IS TRUE`
   （`update.py` 的价格/快照重算/财务续传、`calculator.py` 发布名单，以及
   `funding`/`supplement`/`capital`/`statistics`/`business` 各一份）。
2. **不能拿 `is_listed=FALSE` 冒充**——`init.py::_fetch_stock_universe()` 每天
   用全市场清单 UPSERT，会硬写回 `TRUE`。
3. **重试队列会反噬**——`auto_update_is_due()` 的判据之一就是「`retry_list`
   里有到期行」，停抓后黑名单股票的历史失败行会持续拉起整轮更新，最终转成
   永久 `missing_list`。
4. **最致命：readiness 是逐股阻断、不是阈值制**。停抓后市场一进入新报告期，
   该股快照期对不上 → `snapshot_period_alignment` 直接 block →
   `/api/ready` 返回 503，**整站受影响**。

因此本模块只做「读侧隐藏」，抓取链路零改动；黑名单股票的数据保持完整，
全市场分位/中位数/指数成分聚合的口径也就不会静默漂移。

## 与排名口径的关系

筛选引擎的全市场排名/分位是窗口函数跑在 `base_pool` 之上的（`engine.py`），
所以黑名单必须加进**基础池条件**而不是前端过滤：加进池里 → 排名分母同步变小、
页面百分位与看得见的行数自洽；只在前端过滤 → 口径与行数对不上。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

#: SQLite 表名（建表见 `storage/schema.py` 的 `SQLITE_SCHEMA_V1` + v20 迁移）
BLACKLIST_TABLE = "stock_blacklist"

#: 与 `watchlist.add_to_watchlist` 同源的代码格式校验（6 位数字）。
_STOCK_CODE_RE = re.compile(r"\d{6}")

#: `NOT IN (...)` 的绑定参数上限。SQLite/DuckDB 都远高于此，这里只是防止
#: 异常数据把 SQL 文本撑爆；超过即拒绝而不是静默截断（截断会导致误显示）。
MAX_EXCLUSION_CODES = 5_000


class InvalidStockCodeError(ValueError):
    """股票代码不是 6 位数字。"""


def normalize_stock_code(stock_code: str) -> str:
    """校验并归一化股票代码；不合法直接抛错（不做静默纠正）。"""
    code = (stock_code or "").strip()
    if not _STOCK_CODE_RE.fullmatch(code):
        raise InvalidStockCodeError(f"invalid stock code: {stock_code!r}")
    return code


def blacklisted_codes(sqlite: Any) -> list[str]:
    """返回黑名单代码（按加入时间倒序）。

    `sqlite` 为 None 时返回空表——`ScreeningEngine` 允许不注入 SQLite，
    那种场景（纯离线计算/部分测试）没有用户态，视作"没有黑名单"。
    """
    if sqlite is None:
        return []
    rows = sqlite.query(
        f"SELECT stock_code FROM {BLACKLIST_TABLE} ORDER BY created_at DESC, stock_code"
    )
    return [str(row["stock_code"]) for row in rows if row.get("stock_code")]


def blacklisted_code_set(sqlite: Any) -> frozenset[str]:
    """`blacklisted_codes()` 的集合视图，供逐行过滤用。"""
    return frozenset(blacklisted_codes(sqlite))


def excluded_codes(sqlite: Any) -> list[str]:
    """**读侧排除的唯一入口** = 彻底拉黑 ∪ 浮动拉黑。

    为什么必须合成一个函数：所有读侧出口（筛选基础池 / 搜索 / 自选 / 导出 / DSL）
    都只应该问一句「哪些股票不该出现」，不应该各自去拼两张表。
    拼在别处就会分叉——其中一个出口漏掉浮动黑名单，用户就会在某个页面又看见它
    （`AGENTS R1`）。

    **注意别在浮动黑名单求值里用它**：求值时基础池只能剔除「彻底拉黑」，
    否则上一轮已经浮动的股票会被规则自己筛掉，导致名单每轮全进全出地抖动。
    求值路径请用 `blacklisted_codes()`（只含彻底拉黑），见
    `app/core/floating_blacklist.py::evaluate_rule` 的 `pool_excludes_floating=False`。
    """
    permanent = blacklisted_codes(sqlite)
    if sqlite is None:
        return permanent
    # 延迟导入：floating_blacklist 依赖本模块，模块级互相 import 会成环。
    from app.core.floating_blacklist import floating_codes

    floating = floating_codes(sqlite)
    if not floating:
        return permanent
    return sorted(set(permanent) | set(floating))


def excluded_code_set(sqlite: Any) -> frozenset[str]:
    """`excluded_codes()` 的集合视图（读侧逐行过滤用）。"""
    return frozenset(excluded_codes(sqlite))


def is_blacklisted(sqlite: Any, stock_code: str) -> bool:
    """单只股票是否在黑名单里（主键查，成本可忽略）。"""
    if sqlite is None:
        return False
    code = (stock_code or "").strip()
    if not code:
        return False
    rows = sqlite.query(
        f"SELECT 1 AS found FROM {BLACKLIST_TABLE} WHERE stock_code = ? LIMIT 1",
        [code],
    )
    return bool(rows)


def add_stock(sqlite: Any, stock_code: str, reason: str | None = None) -> str:
    """加入黑名单（幂等：重复加入只刷新时间，不报错）。返回归一化代码。"""
    code = normalize_stock_code(stock_code)
    sqlite.execute(
        f"""INSERT INTO {BLACKLIST_TABLE} (stock_code, reason)
            VALUES (?, ?)
            ON CONFLICT(stock_code) DO UPDATE SET
              reason = excluded.reason,
              created_at = CURRENT_TIMESTAMP""",
        [code, (reason or "").strip() or None],
    )
    return code


def remove_stock(sqlite: Any, stock_code: str) -> int:
    """移出黑名单，返回实际删除行数（0 = 本来就不在黑名单里）。"""
    return sqlite.execute(
        f"DELETE FROM {BLACKLIST_TABLE} WHERE stock_code = ?",
        [(stock_code or "").strip()],
    )


def list_entries(sqlite: Any) -> list[dict]:
    """黑名单原始行（代码/原因/加入时间），供「黑名单管理页」使用。"""
    if sqlite is None:
        return []
    return sqlite.query(
        f"""SELECT stock_code, reason, created_at
            FROM {BLACKLIST_TABLE}
            ORDER BY created_at DESC, stock_code"""
    )


def exclusion_condition(
    column: str,
    codes: Sequence[str] | Iterable[str],
) -> tuple[str, list[str]]:
    """生成「排除这批股票」的 SQL **谓词** + 绑定参数。

    返回 ``("<column> NOT IN (?,?)", [code, code])``；集合为空时返回
    ``("", [])``，调用方据此跳过拼接（空谓词拼进 SQL 会变成语法错误）。

    之所以返回裸谓词而不是 ``"AND ..."``：三个已知调用点里有两个是
    「条件列表 append」（`engine.py` 的 `pool_conditions` / `conds`），
    裸谓词直接可用；另外两个是 WHERE 片段，自己加 `AND` 更清楚。

    这是全仓**唯一**的黑名单 SQL 生成点（`AGENTS R1`）。不要在别处手写
    `NOT IN`：一是各写一份迟早分叉，二是参数占位顺序在筛选引擎里是敏感的
    （见 `engine.py::_build_sql` 的 params 顺序注释）。
    """
    code_list = [str(code) for code in codes if code]
    if not code_list:
        return "", []
    if len(code_list) > MAX_EXCLUSION_CODES:
        raise ValueError(
            f"黑名单规模 {len(code_list)} 超过上限 {MAX_EXCLUSION_CODES}，"
            "拒绝执行而不是静默截断"
        )
    placeholders = ", ".join("?" for _ in code_list)
    return f"{column} NOT IN ({placeholders})", code_list


def filter_rows(
    rows: Iterable[dict],
    codes: Iterable[str],
    *,
    key: str = "stock_code",
) -> list[dict]:
    """把一批行（dict 列表）里的黑名单股票剔掉。

    用于**冻结快照**类出口：已保存的筛选结果（`screening_results.result_json`）
    与 CSV 导出里面的代码是历史 run 的原始 JSON，无法在 SQL 层过滤，
    只能读出来后在内存里剔除。
    """
    blocked = set(codes)
    if not blocked:
        return list(rows)
    return [row for row in rows if str(row.get(key) or "") not in blocked]
