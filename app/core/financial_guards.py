"""保险股「保险合同负债」≠「合同负债」—— 写库前的**行业守卫**（唯一真源）。

事故（`docs/reports/131` §9.2 P0-6，`docs/reports/135` 复核）
------------------------------------------------------------
601318 中国平安 / 601319 中国人保 / 601336 新华保险 / 601601 中国太保 /
601628 中国人寿 五只保险股，FY2022–FY2026 共 74 行把 CAS25「保险合同负债」
写进了 **通用科目** `balance_sheet.contract_liabilities`，一度占全市场合同负债
合计的 42%–78%，把「合同负债」这个横截面排名指标整体污染。

上一轮只清了存量数据（一次性脚本把 74 行置 NULL），**没有修写入路径**，
所以下一期财报必然复发。本模块就是那条缺失的写入路径守卫。

为什么判据必须是**行业+量级**，而不是源字段名
----------------------------------------------
1. 5 只险企的 `contract_liabilities` 来源是 **sina**，而
   `sina_adapter._FIELD_BY_TITLE["contract_liabilities"] = ("合同负债",)`
   是**精确匹配**——上游标题就叫「合同负债」，`source_audit` 也记成
   `sina / contract_liabilities`。任何基于源字段名（含"保险合同负债"字样）的
   判据在这条数据上**完全不触发**（`supplement.guard_contract_liabilities`
   即属此列，它只覆盖 `eastmoney_f10` 补充管线）。
2. 只看行业会误伤：601319 在 2011–2013 年的**真实**合同负债
   （49.2B / 50.3B / 41.6B，占负债 6%–9%）是靠量级判据保下来的。
   保险合同负债是险企最大的负债科目（实测占负债 0.37–0.95），
   而真实合同负债/预收保费不可能占到负债的四分之一。

因此判据是**两维组合**（见 `insurer_contract_liability_decision`）：
非保险股一律放行；保险股看 `值 / 负债合计` 与阈值（默认 0.25）比较。

被拒的值去哪
------------
`balance_sheet.contract_liabilities` 置 NULL（该科目在险企身上不存在），
同一金额改道写入 `financial_sector_items.insurance_contract_liability`
（schema v44 加的专用列）。**绝不静默丢弃**。

本模块的边界（谁还能改它）
--------------------------
* 纯判定 `insurer_contract_liability_decision()` —— 无 IO，可单测；
* 写库适配 `guard_contract_liabilities_write()` —— 供
  `app/core/init.py::_upsert_financial_row` 与
  `app/core/update.py::_write_sina_financial_table` 两条**实际写入路径**调用；
* `app/core/supplement.py::guard_contract_liabilities` 是**另一套**判据
  （源字段名维度，只覆盖 eastmoney_f10）。两者不是重复实现而是互补的两道闸：
  本模块按行业拦，supplement 按字段名拦。任何一侧想改判据，都必须先看另一侧。

已知交互（需要 supplement 侧跟进，不在本模块职责内）
----------------------------------------------------
`financial_sector_items` 的主键是 `(stock_code, report_date)`，而
`supplement._persist_rows` 对 `replace_source_rows` 的执行顺序是
「按 source 删本批报告期 → `INSERT ... BY NAME`」：

* 本模块写入的行 `source=GUARD_SOURCE`（不是 `eastmoney_f10`），因此
  **不会**被 supplement 的按源删除命中，值不会在同一轮被静默抹掉；
* 但反过来，若 supplement 之后要对**同一 (股, 期)** 插入
  `eastmoney_f10` 行，普通 INSERT 会撞主键。当前正式库实测：5 只险企
  2025-06-30 ~ 2026-09-30 的 30 行**已经**都有 `eastmoney_f10` 行，
  本模块走 UPDATE（不新建行），缺口 34 行全部落在 2025-03-31 及更早
  （supplement 的补采窗口是 2025Q2 起，不会去写）。
* 仍有残留风险：`scripts/fetch_financial_sector.py` 重跑时会把
  eastmoney 行的 `insurance_contract_liability` 删掉重建为 NULL。
  处置办法见 `scripts/backfill_insurance_contract_liability.py`（可幂等重跑），
  根治需 supplement 侧改成 `ON CONFLICT DO UPDATE` 或保留该列。
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "BACKFILL_SOURCE",
    "BALANCE_SHEET_TABLE",
    "CONTRACT_LIABILITIES_COLUMN",
    "ContractLiabilityGuardOutcome",
    "DEFAULT_MAX_LIABILITY_SHARE",
    "FINANCIAL_SECTOR_TABLE",
    "GUARD_CONFIDENCE",
    "GUARD_SOURCE",
    "INSURANCE_CONTRACT_LIABILITY_COLUMN",
    "INSURANCE_SW_LEVEL1",
    "INSURANCE_SW_LEVEL2",
    "MAX_LIABILITY_SHARE_CONFIG_KEYS",
    "MAX_LIABILITY_SHARE_ENV",
    "REASON_AT_OR_ABOVE_THRESHOLD",
    "REASON_BELOW_THRESHOLD",
    "REASON_NON_INSURER",
    "REASON_NO_POSITIVE_VALUE",
    "REASON_SHARE_UNKNOWN",
    "contract_liability_ratio_threshold",
    "guard_caches",
    "guard_contract_liabilities_write",
    "insurer_contract_liability_decision",
    "is_insurer_industry",
    "load_stock_industry",
    "normalize_report_date",
    "null_out_contract_liabilities",
    "table_columns",
    "upsert_insurance_contract_liability",
]

# ─── 常量（列名/行业名必须是 DB 实测字符串，不要"美化"）──────────────────

#: 通用科目所在表（被守卫保护的对象）
BALANCE_SHEET_TABLE = "balance_sheet"
#: 通用科目列：险企**不得**写入 CAS25「保险合同负债」
CONTRACT_LIABILITIES_COLUMN = "contract_liabilities"
#: 金融行业专用科目表
FINANCIAL_SECTOR_TABLE = "financial_sector_items"
#: CAS25「保险合同负债」的正确落点（schema v44；列不存在时只置 NULL 并告警）
INSURANCE_CONTRACT_LIABILITY_COLUMN = "insurance_contract_liability"

#: 申万二级行业名（DB 实测字符串，注意是全角罗马数字 Ⅱ）
INSURANCE_SW_LEVEL2 = "保险Ⅱ"
#: 部分历史数据把 sw_level1 直接记成「保险」
INSURANCE_SW_LEVEL1 = "保险"

#: 保险合同负债 / 负债合计 的默认阈值（reports/131 §9.2 判据 4 的正式口径）
DEFAULT_MAX_LIABILITY_SHARE = 0.25
#: 阈值可配置入口①：环境变量（优先级最高，无需改仓库里的 yaml）
MAX_LIABILITY_SHARE_ENV = "VD_INSURER_CONTRACT_LIABILITY_MAX_SHARE"
#: 阈值可配置入口②：`config/user.yaml` 覆盖（default.yaml 里刻意**不**放默认值，
#: 否则 `tests/regression/test_config_dead_keys.py` 要求每个叶子键都有读取点，
#: 而这里的读取点是"键不存在也算合法"的可选项）
MAX_LIABILITY_SHARE_CONFIG_KEYS: tuple[tuple[str, str], ...] = (
    ("update", "insurer_contract_liability_max_share"),
    ("data_quality", "insurer_contract_liability_max_share"),
)

#: 守卫改道写入时登记的来源标签（**不是** `eastmoney_f10`：
#: 否则会被 supplement 的按源删除顺带清掉）
GUARD_SOURCE = "p06_insurer_contract_liability_guard"
#: 历史回填脚本使用的来源标签
BACKFILL_SOURCE = "p06_insurer_contract_liability_backfill"
#: 守卫改道行的置信度：金额本身是源侧原值（精确），但"这一列"的归属
#: 是由行业+量级判据推断的，因此如实登记为 approximate。
GUARD_CONFIDENCE = "approximate"

#: 判定原因码（写入日志/报告；带括号参数，便于事后复核当时用的阈值）
REASON_NON_INSURER = "non_insurer"
REASON_NO_POSITIVE_VALUE = "no_positive_value"
REASON_BELOW_THRESHOLD = "insurer_share_below_threshold"
REASON_AT_OR_ABOVE_THRESHOLD = "insurer_share_at_or_above_threshold"
REASON_SHARE_UNKNOWN = "insurer_share_unknown_fail_closed"


# ─── 纯判定层（无 IO，可单测）───────────────────────────────────────────


def _to_float(value: Any) -> float | None:
    """尽量转成有限 float；None/空串/NaN/不能转 → None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return number


def _coerce_share(raw: Any) -> float | None:
    """阈值必须是 (0, 1) 的真占比；其他值一律视为"没配"，回落默认。"""
    number = _to_float(raw)
    if number is None or not 0.0 < number < 1.0:
        return None
    return number


def _threshold_from_config() -> float | None:
    """从 `Config` 里读可选阈值；未配置/读到脏值/配置不可用 → None。"""
    try:  # 延迟导入：Config 会读 yaml，且本模块在 CLI/测试两条主线上都要轻
        from app.core.config import Config
    except Exception as error:  # noqa: BLE001 - 配置不可用绝不能阻断写库
        logger.debug("读取契约负债阈值失败（Config 不可用）: %s", error)
        return None
    try:
        config = Config.current()
    except Exception as error:  # noqa: BLE001
        logger.debug("读取契约负债阈值失败（Config 未就绪）: %s", error)
        return None
    for section_key, leaf_key in MAX_LIABILITY_SHARE_CONFIG_KEYS:
        try:
            section = config.get_value(section_key, {})
        except Exception:  # noqa: BLE001
            continue
        if isinstance(section, Mapping):
            found = _coerce_share(section.get(leaf_key))
            if found is not None:
                return found
    return None


def contract_liability_ratio_threshold(explicit: float | None = None) -> float:
    """保险合同负债占比阈值：显式参数 > 环境变量 > `config/user.yaml` > 默认 0.25。

    Args:
        explicit: 调用方直接给定的阈值（测试/脚本定向复核用）。

    Returns:
        落在 (0, 1) 区间的阈值；任何非法输入都回落 `DEFAULT_MAX_LIABILITY_SHARE`
        并留下 warning（阈值是**判据本身**，不能静默变成 0 或 1）。
    """
    if explicit is not None:
        coerced = _coerce_share(explicit)
        if coerced is not None:
            return coerced
        logger.warning(
            "非法阈值 %r（要求 0 < x < 1），回落默认 %s",
            explicit, DEFAULT_MAX_LIABILITY_SHARE,
        )
        return DEFAULT_MAX_LIABILITY_SHARE
    from_env = _coerce_share(os.environ.get(MAX_LIABILITY_SHARE_ENV))
    if from_env is not None:
        return from_env
    from_config = _threshold_from_config()
    if from_config is not None:
        return from_config
    return DEFAULT_MAX_LIABILITY_SHARE


def is_insurer_industry(
    *,
    industry_level1: str | None = None,
    industry_level2: str | None = None,
    company_name: str | None = None,
) -> bool:
    """是否为保险股（任一命中即算；宁可多拦不可漏拦）。

    判据优先级：申万二级 `保险Ⅱ` → 申万一级「保险」 → 公司名含「保险」。
    `sw_level1='非银金融'` 单独**不**足以判定（券商/多元金融也是非银金融）。
    """
    if (industry_level2 or "").strip() == INSURANCE_SW_LEVEL2:
        return True
    if (industry_level1 or "").strip() == INSURANCE_SW_LEVEL1:
        return True
    return "保险" in str(company_name or "")


def insurer_contract_liability_decision(
    stock_code: str,
    industry_level1: str | None,
    industry_level2: str | None,
    value: float | None,
    total_liabilities: float | None,
    *,
    threshold: float | None = None,
    company_name: str | None = None,
) -> tuple[bool, str]:
    """该不该把 `value` 写进通用科目 `contract_liabilities`。

    判定表（`share = value / total_liabilities`）：

    ==================  ==========================  ==========================================
    股票                 条件                        结论
    ==================  ==========================  ==========================================
    非保险股             任意                        **允许**（不归本守卫管）
    保险股               value 缺失 / <= 0            **允许**（没有可污染的金额）
    保险股               share < 阈值（默认 0.25）    **允许**（真实合同负债，如 601319 2011–13 的 6%–9%）
    保险股               share >= 阈值               **拒绝**，改道 `insurance_contract_liability`
    保险股               负债合计缺失/<=0            **拒绝**（fail-closed：无法自证是真实合同负债）
    ==================  ==========================  ==========================================

    Args:
        stock_code: 6 位股票代码（只用于日志/原因串，不参与判定）。
        industry_level1: `stock_meta.sw_level1`。
        industry_level2: `stock_meta.sw_level2`（保险股是 `保险Ⅱ`）。
        value: 待写入 `contract_liabilities` 的金额（元）。
        total_liabilities: 同一报告期的负债合计（元），占比的分母。
        threshold: 覆盖默认阈值；None 时按
            `contract_liability_ratio_threshold()`（环境变量/配置/默认 0.25）。
        company_name: `stock_meta.name`，仅作为行业缺失时的兜底判据。

    Returns:
        `(allowed, reason)`：`allowed=True` 表示可以写 `contract_liabilities`；
        `reason` 是常量码 + 当时的实测参数（阈值、占比），便于事后复核。
    """
    if not is_insurer_industry(
        industry_level1=industry_level1,
        industry_level2=industry_level2,
        company_name=company_name,
    ):
        return True, REASON_NON_INSURER
    amount = _to_float(value)
    if amount is None or amount <= 0:
        return True, REASON_NO_POSITIVE_VALUE
    limit = contract_liability_ratio_threshold(threshold)
    liabilities = _to_float(total_liabilities)
    if liabilities is None or liabilities <= 0:
        # 分母不成立时**不能**默认放行：这正是"下一期财报复发"的入口
        #（Sina 部分响应缺 total_liabilities）。值会改道专用列，不丢。
        return False, (
            f"{REASON_SHARE_UNKNOWN}(stock={stock_code},"
            f"value={amount:.6g},total_liabilities={total_liabilities!r},"
            f"threshold={limit:.4f})"
        )
    share = amount / liabilities
    detail = (
        f"stock={stock_code},value={amount:.6g},"
        f"total_liabilities={liabilities:.6g},share={share:.4f},threshold={limit:.4f}"
    )
    if share < limit:
        return True, f"{REASON_BELOW_THRESHOLD}({detail})"
    return False, f"{REASON_AT_OR_ABOVE_THRESHOLD}({detail})"


# ─── 写库适配层 ─────────────────────────────────────────────────────────


def normalize_report_date(value: Any) -> str | None:
    """任意日期表示 → 'YYYY-MM-DD'；无法识别 → None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    return text[:10]


def table_columns(
    conn: Any, table: str, *, cache: MutableMapping[str, set[str]] | None = None,
) -> set[str] | None:
    """目标表的真实列集合（`information_schema`）；查不到返回 None（=未知）。

    调用方必须把"未知"当成"没有该列"处理——绝不能凭声明往库里写不存在的列。
    """
    if cache is not None and table in cache:
        return cache[table]
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table],
        ).fetchall()
    except Exception as error:  # noqa: BLE001 - 列探测失败不得阻断主写路径
        logger.debug("探测 %s 列集合失败: %s", table, error)
        return None
    columns = {str(row[0]) for row in rows}
    if cache is not None:
        cache[table] = columns
    return columns


def load_stock_industry(
    conn: Any,
    stock_code: str,
    *,
    cache: MutableMapping[str, tuple[str | None, str | None, str | None]] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """`(sw_level1, sw_level2, name)`；查不到返回 `(None, None, None)`。"""
    key = str(stock_code).strip()
    if cache is not None and key in cache:
        return cache[key]
    industry: tuple[str | None, str | None, str | None] = (None, None, None)
    try:
        row = conn.execute(
            "SELECT sw_level1, sw_level2, name FROM stock_meta WHERE stock_code = ?",
            [key],
        ).fetchone()
    except Exception as error:  # noqa: BLE001 - 行业查不到不得阻断写库
        logger.debug("读取 %s 行业口径失败: %s", key, error)
        row = None
    if row is not None:
        industry = (row[0], row[1], row[2])
    if cache is not None:
        cache[key] = industry
    return industry


def _lookup_total_liabilities(conn: Any, stock_code: str, report_date: str) -> float | None:
    """本行没带负债合计时，从库里同 (股, 期) 已存在的行取分母。"""
    try:
        row = conn.execute(
            "SELECT total_liabilities FROM balance_sheet "
            "WHERE stock_code = ? AND report_date = CAST(? AS DATE)",
            [str(stock_code).strip(), report_date],
        ).fetchone()
    except Exception as error:  # noqa: BLE001
        logger.debug("读取 %s %s 负债合计失败: %s", stock_code, report_date, error)
        return None
    return None if row is None else _to_float(row[0])


def null_out_contract_liabilities(
    conn: Any, *, stock_code: str, report_date: Any,
) -> bool:
    """把该 (股, 期) 的 `contract_liabilities` 显式置 NULL（被拒值的落点）。

    必须**显式**执行：两条写入路径的 upsert 都带 `COALESCE(excluded.x, 旧值)`/
    字段并集语义，单靠"不传这一列"清不掉库里已经存在的脏值。

    Returns:
        True = 确实把一行非空值改成了 NULL；False = 没有该行或本来就是 NULL。
    """
    normalized = normalize_report_date(report_date)
    if normalized is None:
        return False
    code = str(stock_code).strip()
    try:
        existing = conn.execute(
            "SELECT 1 FROM balance_sheet WHERE stock_code = ? "
            "AND report_date = CAST(? AS DATE) AND contract_liabilities IS NOT NULL",
            [code, normalized],
        ).fetchone()
        if existing is None:
            return False
        conn.execute(
            "UPDATE balance_sheet SET contract_liabilities = NULL "
            "WHERE stock_code = ? AND report_date = CAST(? AS DATE)",
            [code, normalized],
        )
    except Exception as error:  # noqa: BLE001 - 置空失败不得让整批写入回滚
        logger.warning("清空 %s %s 的 contract_liabilities 失败: %s", code, normalized, error)
        return False
    return True


def _report_type_of(report_date: str) -> str:
    """报告期 → report_type（与主链三表同口径：annual / semi_annual / quarterly）。"""
    month = int(report_date[5:7]) if len(report_date) >= 7 else 0
    return {12: "annual", 6: "semi_annual"}.get(month, "quarterly")


def _guard_batch_id(stock_code: str, report_date: str, source: str) -> str:
    """确定性批号：同一 (股, 期) 重复写入不会产生"漂移的批次号"。

    注意：这是**改道**行，不对应 `fetch_batch` 里的抓取批次（原始抓取的
    batch 由主写入路径自己登记），此处只要求可追溯、可重放。
    """
    return f"{source}:{stock_code}:{report_date}"[:64]


def upsert_insurance_contract_liability(
    conn: Any,
    *,
    stock_code: str,
    report_date: Any,
    value: Any,
    available_columns: set[str] | None = None,
    source: str = GUARD_SOURCE,
    fetch_time: datetime | None = None,
    raw_response_hash: str | None = None,
    confidence: str = GUARD_CONFIDENCE,
    batch_id: str | None = None,
) -> str:
    """把金额写进 `financial_sector_items.insurance_contract_liability`。

    * 行存在 → 只 UPDATE 该列（**不动**该行原有的 source/其它科目，避免把
      supplement 拥有的 `eastmoney_f10` 行改成别的来源）；
    * 行不存在 → INSERT 一行（其余科目留 NULL，来源如实标成本模块的标签）；
    * 专用列/表不存在 → `skipped:no_column`，只告警，绝不写不存在的列。

    Returns:
        `inserted` / `updated` / `skipped:<原因>`，供调用方写日志与断言。
    """
    amount = _to_float(value)
    if amount is None:
        return "skipped:no_value"
    code = str(stock_code).strip()
    day = normalize_report_date(report_date)
    if not code or day is None:
        return "skipped:no_primary_key"
    columns = (
        available_columns
        if available_columns is not None
        else table_columns(conn, FINANCIAL_SECTOR_TABLE)
    )
    if not columns:
        logger.warning(
            "%s 表不可用，%s %s 的保险合同负债 %.6g 无处改道（reports/131 P0-6）",
            FINANCIAL_SECTOR_TABLE, code, day, amount,
        )
        return "skipped:no_table"
    if INSURANCE_CONTRACT_LIABILITY_COLUMN not in columns:
        logger.warning(
            "%s.%s 列不存在，%s %s 的保险合同负债 %.6g 无处改道（reports/131 P0-6）",
            FINANCIAL_SECTOR_TABLE, INSURANCE_CONTRACT_LIABILITY_COLUMN, code, day, amount,
        )
        return "skipped:no_column"

    fetched_at = fetch_time or datetime.now(UTC)
    digest = raw_response_hash or hashlib.sha256(
        f"{source}:{code}:{day}:{amount!r}".encode()
    ).hexdigest()
    try:
        existing = conn.execute(
            f"SELECT 1 FROM {FINANCIAL_SECTOR_TABLE} "
            "WHERE stock_code = ? AND report_date = CAST(? AS DATE)",
            [code, day],
        ).fetchone()
        if existing is not None:
            conn.execute(
                f"UPDATE {FINANCIAL_SECTOR_TABLE} "
                f"SET {INSURANCE_CONTRACT_LIABILITY_COLUMN} = ? "
                "WHERE stock_code = ? AND report_date = CAST(? AS DATE)",
                [amount, code, day],
            )
            return "updated"
        conn.execute(
            f"INSERT INTO {FINANCIAL_SECTOR_TABLE} "
            f"(stock_code, report_date, report_type, {INSURANCE_CONTRACT_LIABILITY_COLUMN}, "
            " source, fetch_time, raw_response_hash, confidence, batch_id) "
            "VALUES (?, CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT (stock_code, report_date) DO UPDATE SET "
            f"{INSURANCE_CONTRACT_LIABILITY_COLUMN} = "
            f"excluded.{INSURANCE_CONTRACT_LIABILITY_COLUMN}",
            [
                code, day, _report_type_of(day), amount,
                source, fetched_at, digest, confidence,
                batch_id or _guard_batch_id(code, day, source),
            ],
        )
    except Exception as error:  # noqa: BLE001 - 改道失败绝不能让主写路径回滚
        logger.warning(
            "改道写 %s.%s 失败（%s %s %.6g）: %s",
            FINANCIAL_SECTOR_TABLE, INSURANCE_CONTRACT_LIABILITY_COLUMN, code, day, amount, error,
        )
        return f"skipped:error:{type(error).__name__}"
    return "inserted"


@dataclass(frozen=True)
class ContractLiabilityGuardOutcome:
    """`guard_contract_liabilities_write()` 的判定 + 落地结果。"""

    stock_code: str
    report_date: str | None
    #: True = 允许写 `contract_liabilities`；False = 已拦截并改道
    allowed: bool
    #: `insurer_contract_liability_decision()` 给出的原因码（带实测参数）
    reason: str
    #: 被拦截并从待写值里摘掉的金额（未被拦截时为 None）
    rejected_value: float | None = None
    #: 改道的落地结果：inserted / updated / skipped:<原因>
    specialty_write: str | None = None
    #: 是否真的把库里已有的 `contract_liabilities` 置了 NULL
    nulled_existing: bool = False


def guard_caches(owner: Any) -> tuple[dict[str, set[str]], dict[str, tuple]]:
    """取（或惰性挂载）`owner` 上的两张缓存：列集合 / 行业口径。

    复用 `DataInitializer._financial_cols_cache`（同一个 dict），避免为
    `financial_sector_items` 再探测一次 `information_schema`。
    """
    columns = getattr(owner, "_financial_cols_cache", None)
    industry = getattr(owner, "_p06_industry_cache", None)
    if columns is None:
        columns = {}
    if industry is None:
        industry = {}
    try:
        owner._financial_cols_cache = columns
        owner._p06_industry_cache = industry
    except Exception:  # noqa: BLE001 - 只读/冻结对象也不该阻断写库
        pass
    return columns, industry


def guard_contract_liabilities_write(
    conn: Any,
    *,
    stock_code: str,
    values: MutableMapping[str, Any],
    industry: tuple[str | None, str | None, str | None] | None = None,
    column_cache: MutableMapping[str, set[str]] | None = None,
    industry_cache: MutableMapping[str, tuple] | None = None,
    source: str = GUARD_SOURCE,
    threshold: float | None = None,
    report_date: Any = None,
) -> ContractLiabilityGuardOutcome | None:
    """写库前守卫（**两条实际写入路径共用这一个入口**）。

    行为（`values` 会被就地修改）：

    1. `values` 里没有 `contract_liabilities`（或为 None）→ 返回 None，什么都不做；
    2. 判定放行 → 原样返回，不写专用列；
    3. 判定拒绝 →
       a. 从 `values` 摘掉 `contract_liabilities`（后续 INSERT 不再带脏值）；
       b. 库里该 (股, 期) 若已有非空脏值，**显式** 置 NULL；
       c. 同一金额改道写 `financial_sector_items.insurance_contract_liability`。

    Args:
        conn: 调用方所在事务的连接（本函数不提交，随调用方事务一起提交/回滚）。
        stock_code: 6 位代码。
        values: 即将落库的 `列名 → 值`（就地修改）。
        industry: `(sw_level1, sw_level2, name)`；None 时按 `stock_code` 查
            `stock_meta`（结果进 `industry_cache`）。
        column_cache: 列集合缓存（复用 `DataInitializer._financial_cols_cache`）。
        industry_cache: 行业口径缓存（挂在 initializer 上）。
        source: 改道行的来源标签。
        threshold: 覆盖默认占比阈值。
        report_date: 报告期；None 时取 `values["report_date"]`。

    Returns:
        `ContractLiabilityGuardOutcome`（命中守卫）或 None（本行不涉及该科目）。
    """
    if CONTRACT_LIABILITIES_COLUMN not in values:
        return None
    raw_value = values.get(CONTRACT_LIABILITIES_COLUMN)
    amount = _to_float(raw_value)
    if amount is None:
        return None
    code = str(stock_code).strip()
    day = normalize_report_date(report_date if report_date is not None else values.get("report_date"))
    resolved_industry = industry
    if resolved_industry is None:
        resolved_industry = load_stock_industry(conn, code, cache=industry_cache)
    level1, level2, name = resolved_industry
    total = _to_float(values.get("total_liabilities"))
    if total is None and day is not None:
        total = _lookup_total_liabilities(conn, code, day)
    allowed, reason = insurer_contract_liability_decision(
        code, level1, level2, amount, total, threshold=threshold, company_name=name,
    )
    if allowed:
        return ContractLiabilityGuardOutcome(
            stock_code=code, report_date=day, allowed=True, reason=reason,
        )
    values.pop(CONTRACT_LIABILITIES_COLUMN, None)
    nulled = null_out_contract_liabilities(conn, stock_code=code, report_date=day)
    specialty = upsert_insurance_contract_liability(
        conn,
        stock_code=code,
        report_date=day,
        value=amount,
        available_columns=table_columns(conn, FINANCIAL_SECTOR_TABLE, cache=column_cache),
        source=source,
    )
    logger.warning(
        "P0-6 守卫拦截：%s %s 的「保险合同负债」%.6g 不得写入 %s.%s（%s）；"
        "已改道 %s.%s → %s%s",
        code, day, amount, BALANCE_SHEET_TABLE, CONTRACT_LIABILITIES_COLUMN, reason,
        FINANCIAL_SECTOR_TABLE, INSURANCE_CONTRACT_LIABILITY_COLUMN, specialty,
        "；同时清空库里已有的脏值" if nulled else "",
    )
    return ContractLiabilityGuardOutcome(
        stock_code=code,
        report_date=day,
        allowed=False,
        reason=reason,
        rejected_value=amount,
        specialty_write=specialty,
        nulled_existing=nulled,
    )
