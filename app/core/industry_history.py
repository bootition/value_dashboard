"""行业变更历史 + 报告元数据（reports/119 §5 P2-2 / reports/122 §2.1 J3）

**为什么需要**：库里此前只有"现在"的行业分类（`stock_meta.csrc_l1/l2`）。
回测里用当前行业 = 未来信息（一家 2015 年还是化工的公司被当成 2025 年的半导体）。
两条数据源其实都带历史，只是写库时被压平丢掉了：

1. CSMAR 原始文件（C17 数据包）每个报告期都带 `Indcd`（2012 证监会标准）与
   `Indnme`；资产负债表/利润表还带 `Typrep` / `IfCorrect` / `DeclareDate`。
2. 证监会行业接口（`csrc_industry_adapter`）返回的每条记录都有 `as_of_date`
   （变更日期），旧实现按日期倒序取第一条压成 `stock_meta.csrc_l1/l2`。

本模块把"从哪天起属于哪个行业"留下来（`industry_history`），以及"这一行是
合并还是母公司报表、是否更正过、更正披露日"（`report_metadata`）。

**口径纪律**（项目 S8 裁决）：
- 只导 `Indcd`（2012 证监会标准）；`Indcd1`（中国上市公司协会口径）**不入库**。
  CSMAR 字段说明明确：2023-05-01 起证监会分类停止更新，改用协会口径；
  实测 FI_T1 中 `Indcd` 最后有值报告期为 2023-03-31，之后为空。
- `report_metadata` 只存标记，不存任何数值（数值仍在三表里）。

**分层**：`parse_*` 是纯函数（输入 DataFrame → 输出行字典），解析口径可单测；
`upsert_*` 只碰 `industry_history` / `report_metadata`，**不碰 `stock_meta`**；
DTA 读取只取需要的列（`pd.read_stata(columns=[...])` + 分块），避免几百 MB
文件整体进内存。
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 纯解析函数只在运行时被 pandas.DataFrame 喂数据
    import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "CSMAR_INDUSTRY_SOURCE",
    "CSMAR_REPORT_SOURCE",
    "CSRC_INDUSTRY_SOURCE",
    "CSRC_GATE_BY_LETTER",
    "TYPREP_REPORT_TYPE",
    "csrc_gate_name",
    "csrc_records_to_rows",
    "dta_column_labels",
    "industry_history_summary",
    "import_csmar_industry_history",
    "import_csmar_report_metadata",
    "parse_csmar_industry_rows",
    "parse_csmar_report_metadata_rows",
    "probe_dta_sample",
    "read_industry_timeline",
    "report_metadata_summary",
    "table_exists",
    "upsert_csrc_history",
    "upsert_industry_history",
    "upsert_report_metadata",
]

# 数据源标识（写进 industry_history.source / report_metadata.source）
CSMAR_INDUSTRY_SOURCE = "csmar_indcd"
CSRC_INDUSTRY_SOURCE = "csrc_cninfo"
CSMAR_REPORT_SOURCE = "csmar"

# Typrep 口径（核对依据：数据包内字段说明 txt，不是猜测）
#   FI_T1[DES][dta].txt  ：Typrep [报表类型编码] - A：合并报表；B：母公司报表
#   FS_Combas[DES][dta].txt：Typrep [报表类型] - “A＝合并报表”、“B＝母公司报表”
# 实测本数据包所有 dta（FS_Combas/FS_Comins/FI_T1 等）只有 A 行，B 口径靠字段
# 说明 + 单测钉死，不在真实数据上伪造。
TYPREP_REPORT_TYPE: dict[str, str] = {"A": "consolidated", "B": "parent"}

# CSRC 2012 门类字母 → 门类名称（只说"Indcd 首字母是什么门类"，不做行业推断）
CSRC_GATE_BY_LETTER: dict[str, str] = {
    "A": "农、林、牧、渔业",
    "B": "采矿业",
    "C": "制造业",
    "D": "电力、热力、燃气及水生产和供应业",
    "E": "建筑业",
    "F": "批发和零售业",
    "G": "交通运输、仓储和邮政业",
    "H": "住宿和餐饮业",
    "I": "信息传输、软件和信息技术服务业",
    "J": "金融业",
    "K": "房地产业",
    "L": "租赁和商务服务业",
    "M": "科学研究和技术服务业",
    "N": "水利、环境和公共设施管理业",
    "O": "居民服务、修理和其他服务业",
    "P": "教育",
    "Q": "卫生和社会工作",
    "R": "文化、体育和娱乐业",
    "S": "综合",
}

# 空值写法（CSMAR 用字面量 "None" 表示缺失，见实测抽样）
_NULL_TOKENS = frozenset({"", "none", "nan", "nat", "null", "无", "--", "-"})
_MULTI_DATE_SPLIT = re.compile(r"[,，;；\s]+")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
CSMAR_DTA_ROOT = (
    _PROJECT_ROOT / "额外资料" / "C17 a股上市公司财务数据合集（90-25年）" / "原始数据（dta格式）"
)
DEFAULT_INDUSTRY_DTA = CSMAR_DTA_ROOT / "偿债能力" / "FI_T1.dta"
DEFAULT_METADATA_DTA = CSMAR_DTA_ROOT / "资产负债表" / "FS_Combas.dta"

INDUSTRY_DTA_COLUMNS: tuple[str, ...] = ("Stkcd", "Accper", "Indcd", "Indnme")
REPORT_DTA_COLUMNS: tuple[str, ...] = (
    "Stkcd",
    "Accper",
    "Typrep",
    "IfCorrect",
    "DeclareDate",
)


# ─── 值规整（纯函数） ────────────────────────────────────────────────


def _is_missing_scalar(value: Any) -> bool:
    """None / NaN / NaT 判定（不依赖 pandas，便于纯函数单测）。"""
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    # numpy NaN 也走这条；不能用 == 比较（NaN != NaN）
    return not isinstance(value, (str, bytes, date, datetime)) and value != value


def clean_text(value: Any) -> str | None:
    """规整文本列：去空白，把 CSMAR 的字面量 "None" 等统一成缺失。"""
    if _is_missing_scalar(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in _NULL_TOKENS:
        return None
    return text


def to_date(value: Any) -> date | None:
    """把 CSMAR 的日期（字符串/时间戳/YYYYMMDD 整数）转成 `date`，失败返回 None。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        digits = str(int(value))
        try:
            return datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            return None
    text = clean_text(value)
    if text is None:
        return None
    head = text[:10]
    try:
        return date.fromisoformat(head)
    except ValueError:
        pass
    for fmt in ("%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def to_flag(value: Any) -> bool | None:
    """CSMAR 的 0/1 标记（IfCorrect）→ bool；无法判定时返回 None（不猜）。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    text = clean_text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in {"1", "1.0", "true", "yes", "y", "是"}:
        return True
    if lowered in {"0", "0.0", "false", "no", "n", "否"}:
        return False
    return None


def parse_declare_dates(value: Any) -> list[date]:
    """`DeclareDate` → 更正披露日列表（支持逗号并列多次更正，升序去重）。"""
    text = clean_text(value)
    if text is None:
        return []
    parts = [part for part in _MULTI_DATE_SPLIT.split(text) if part.strip()]
    parsed = {parsed for part in parts if (parsed := to_date(part)) is not None}
    return sorted(parsed)


def csrc_gate_name(indcd: str | None) -> str | None:
    """`Indcd`（如 C39）的首字母 → CSRC 门类名称；未知返回 None。"""
    code = clean_text(indcd)
    if not code:
        return None
    return CSRC_GATE_BY_LETTER.get(code[0].upper())


# ─── 纯解析：DataFrame → 行 ────────────────────────────────────────


def _bump(counters: dict[str, int] | None, key: str, amount: int = 1) -> None:
    if counters is not None:
        counters[key] = counters.get(key, 0) + amount


def _pick_column(frame: pd.DataFrame, *candidates: str) -> str | None:
    for name in candidates:
        if name in frame.columns:
            return name
    return None


def parse_csmar_industry_rows(
    frame: pd.DataFrame,
    *,
    counters: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """解析 CSMAR 行业列（FI_T*.dta）→ `industry_history` 行。

    输入列：`Stkcd` / `Accper` / `Indcd` / `Indnme`（`Indnme` 可缺）。
    输出行：`{"stock_code", "effective_date", "level1", "level2"}`

    口径：
    - `effective_date` = `Accper`（报告期末），即"该报告期所属行业"。
    - `level1` = `Indnme`（CSMAR 给的行业名称文本）。
    - `level2` = `Indcd` 代码（如 `C39`）：名称会随证监会修订/口径漂移，代码是口径
      身份，保留它才能按 `left(level2,1)` 聚合门类、也才能做跨源核对。
    - `Indcd1`/`Indnme1`（中国上市公司协会口径）**不读不写**（与 S8 冲突）。
    - 同一 (股票, 报告期) 只产一行：先到先得，后续只补空值（不覆盖已有名称）。
    """
    code_col = _pick_column(frame, "Stkcd", "stock_code")
    date_col = _pick_column(frame, "Accper", "report_date", "effective_date")
    indcd_col = _pick_column(frame, "Indcd", "industry_code")
    name_col = _pick_column(frame, "Indnme", "industry_name")
    if code_col is None or date_col is None or indcd_col is None:
        raise ValueError(
            "CSMAR 行业列缺失：需要 Stkcd/Accper/Indcd，实际列=" + ",".join(map(str, frame.columns))
        )

    positions = {name: index for index, name in enumerate(frame.columns)}
    code_at = positions[code_col]
    date_at = positions[date_col]
    indcd_at = positions[indcd_col]
    name_at = positions[name_col] if name_col else None

    rows: dict[tuple[str, date], dict[str, Any]] = {}
    # name=None 走位置索引：CSMAR 列名含数字后缀（Indcd1）等，按名字取会脆
    for values in frame.itertuples(index=False, name=None):
        stock_code = clean_text(values[code_at])
        if not stock_code:
            _bump(counters, "skipped_no_stock_code")
            continue
        effective_date = to_date(values[date_at])
        if effective_date is None:
            _bump(counters, "skipped_bad_date")
            continue
        indcd = clean_text(values[indcd_at])
        if indcd is None:
            _bump(counters, "skipped_empty_indcd")
            continue
        indcd = indcd.upper()
        level1 = clean_text(values[name_at]) if name_at is not None else None
        if level1 is None:
            _bump(counters, "empty_indnme")
        key = (stock_code, effective_date)
        existing = rows.get(key)
        if existing is None:
            rows[key] = {
                "stock_code": stock_code,
                "effective_date": effective_date,
                "level1": level1,
                "level2": indcd,
            }
            _bump(counters, "parsed_rows")
            continue
        _bump(counters, "duplicate_keys")
        if existing["level1"] is None and level1 is not None:
            existing["level1"] = level1
        if existing["level2"] != indcd:
            # 同一报告期两个 Indcd：保留先到的，只计数（不静默改写口径）
            _bump(counters, "conflicting_indcd")
    return [rows[key] for key in sorted(rows)]


def parse_csmar_report_metadata_rows(
    frame: pd.DataFrame,
    *,
    counters: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """解析 CSMAR 报告元数据列（FS_Combas/FS_Comins）→ `report_metadata` 行。

    输入列：`Stkcd` / `Accper` / `Typrep` / `IfCorrect` / `DeclareDate`
    （`IfCorrect`/`DeclareDate` 可缺）。输出行：
    `{"stock_code", "report_date", "report_type", "is_corrected", "declare_date"}`

    口径：
    - `report_type`：A→`consolidated`（合并报表）、B→`parent`（母公司报表），
      映射来自数据包字段说明 txt（见 `TYPREP_REPORT_TYPE` 注释）；其他值→None。
    - `is_corrected`：`IfCorrect` 1→True、0→False，其余→None。
    - `declare_date`：多次更正时**取最早一次披露日**（"第一次被更正"的时点，
      回测用它做 lookahead 截断最保守）；`is_corrected` 为 False 时强制 None。
    - 表 PK 是 (stock_code, report_date, source)，没有 report_type 列：同一报告期
      只能留一行，因此**优先合并报表**（下游三表只导 A），A 缺失时才用 B，
      且不跨行拼字段（不制造 A 的类型 + parents 的日期这种混合行）。
    """
    code_col = _pick_column(frame, "Stkcd", "stock_code")
    date_col = _pick_column(frame, "Accper", "report_date")
    typ_col = _pick_column(frame, "Typrep", "report_type")
    correct_col = _pick_column(frame, "IfCorrect", "is_corrected")
    declare_col = _pick_column(frame, "DeclareDate", "declare_date")
    if code_col is None or date_col is None:
        raise ValueError(
            "CSMAR 报告元数据列缺失：需要 Stkcd/Accper，实际列="
            + ",".join(map(str, frame.columns))
        )

    positions = {name: index for index, name in enumerate(frame.columns)}
    code_at = positions[code_col]
    date_at = positions[date_col]
    typ_at = positions[typ_col] if typ_col else None
    correct_at = positions[correct_col] if correct_col else None
    declare_at = positions[declare_col] if declare_col else None

    rows: dict[tuple[str, date], dict[str, Any]] = {}
    for values in frame.itertuples(index=False, name=None):
        stock_code = clean_text(values[code_at])
        if not stock_code:
            _bump(counters, "skipped_no_stock_code")
            continue
        report_date = to_date(values[date_at])
        if report_date is None:
            _bump(counters, "skipped_bad_date")
            continue
        raw_type = clean_text(values[typ_at]) if typ_at is not None else None
        report_type = TYPREP_REPORT_TYPE.get(raw_type.upper()) if raw_type else None
        if report_type is None:
            _bump(counters, "unknown_typ" if raw_type else "missing_typ")
        is_corrected = to_flag(values[correct_at]) if correct_at is not None else None
        if is_corrected is None:
            _bump(counters, "missing_ifcorrect")
        declare_dates = (
            parse_declare_dates(values[declare_at]) if declare_at is not None else []
        )
        if len(declare_dates) > 1:
            _bump(counters, "multi_declare_date_rows")
        if is_corrected is False:
            declare_date = None
            if declare_dates:
                _bump(counters, "corrected_flag_conflict")
        elif declare_dates:
            declare_date = declare_dates[0]
            if is_corrected is None:
                # 源给了披露日却没有标记：以披露日为准（有更正行为即视为更正过）
                is_corrected = True
                _bump(counters, "is_corrected_inferred_from_date")
        else:
            declare_date = None

        key = (stock_code, report_date)
        candidate = {
            "stock_code": stock_code,
            "report_date": report_date,
            "report_type": report_type,
            "is_corrected": is_corrected,
            "declare_date": declare_date,
        }
        existing = rows.get(key)
        if existing is None:
            rows[key] = candidate
            _bump(counters, "parsed_rows")
            continue
        _bump(counters, "collapsed_report_type_rows")
        # 合并报表优先；同类型时保留先到的非空字段（不跨行混合）
        if existing["report_type"] == "parent" and report_type == "consolidated":
            rows[key] = candidate
    return [rows[key] for key in sorted(rows)]


def csrc_records_to_rows(
    records: Sequence[dict[str, Any]],
    *,
    counters: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """适配器记录（含 `as_of_date`）→ `industry_history` 行（纯函数）。

    - `effective_date` = `as_of_date`（证监会接口给的行业变更日期）；
    - `level1`/`level2` = `csrc_l1`（行业门类）/`csrc_l2`（行业大类）；
    - 没有 `as_of_date` 或日期不可解析的记录**不写**（不臆造生效日），只计数；
    - 同一 (股票, 日期) 多次出现时后者补空值（接口实测可能重复返回同一天）。
    """
    rows: dict[tuple[str, date], dict[str, Any]] = {}
    for record in records:
        stock_code = clean_text(record.get("stock_code"))
        if not stock_code:
            _bump(counters, "skipped_no_stock_code")
            continue
        effective_date = to_date(record.get("as_of_date"))
        if effective_date is None:
            _bump(counters, "skipped_no_as_of_date")
            continue
        level1 = clean_text(record.get("csrc_l1"))
        level2 = clean_text(record.get("csrc_l2"))
        if level1 is None and level2 is None:
            _bump(counters, "skipped_no_level")
            continue
        key = (stock_code, effective_date)
        existing = rows.get(key)
        if existing is None:
            rows[key] = {
                "stock_code": stock_code,
                "effective_date": effective_date,
                "level1": level1,
                "level2": level2,
            }
            _bump(counters, "parsed_rows")
            continue
        _bump(counters, "duplicate_keys")
        if existing["level1"] is None:
            existing["level1"] = level1
        if existing["level2"] is None:
            existing["level2"] = level2
    return [rows[key] for key in sorted(rows)]


# ─── 写库：只碰 industry_history / report_metadata ─────────────────


def table_exists(duck: Any, table_name: str) -> bool:
    """目标表是否已在库里（未应用 schema 迁移时不能假装写成功）。"""
    rows = duck.read_query(
        "SELECT COUNT(*) AS c FROM information_schema.tables WHERE table_name = ?",
        [table_name],
    )
    return bool(rows and rows[0]["c"])


def _chunked(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    if size <= 0:
        raise ValueError("chunk_size must be positive")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def upsert_industry_history(
    duck: Any,
    rows: Sequence[dict[str, Any]],
    *,
    source: str,
    chunk_size: int = 5_000,
    fetched_at: datetime | None = None,
) -> int:
    """幂等写 `industry_history`（PK + ON CONFLICT DO UPDATE）。

    空值不覆盖旧值（`COALESCE(excluded.x, industry_history.x)`）——
    与 PRD §7.4 "保留旧值不以空值覆盖" 一致；重复跑写同样的键，行数不变。
    """
    if not rows:
        return 0
    stamp = fetched_at or datetime.now(UTC)
    payload = [
        (
            row["stock_code"],
            row["effective_date"],
            row.get("level1"),
            row.get("level2"),
            source,
            stamp,
        )
        for row in rows
    ]
    # 2026-11-15：**禁止用 executemany 写这张表**。
    # DuckDB 1.5.5 的 executemany 遇到 DATE/TIMESTAMP 参数会按 ~450KB/行堆事务
    # （见 ops-knowledge-base D19/D36）：5,000 行≈2.2GB，50,000 行直接吃满内存、
    # 表现为"20 分钟 0 写入 + RSS 线性增长"。改为 pandas register + 单条 INSERT
    # ... SELECT（与 lineage / 价格批量写入同一范式），内存与行数无关。
    import pandas as pd

    frame = pd.DataFrame(
        payload,
        columns=["stock_code", "effective_date", "level1", "level2", "source", "fetched_at"],
    )
    frame["effective_date"] = pd.to_datetime(frame["effective_date"]).dt.date
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"])
    with duck.transaction() as connection:
        connection.register("_industry_rows", frame)
        try:
            connection.execute(
                """INSERT INTO industry_history
                       (stock_code, effective_date, level1, level2, source, fetched_at)
                   SELECT stock_code, effective_date, level1, level2, source, fetched_at
                   FROM _industry_rows
                   ON CONFLICT (stock_code, effective_date, source) DO UPDATE SET
                       level1 = COALESCE(excluded.level1, industry_history.level1),
                       level2 = COALESCE(excluded.level2, industry_history.level2),
                       fetched_at = excluded.fetched_at"""
            )
        finally:
            connection.unregister("_industry_rows")
    return len(payload)


def upsert_report_metadata(
    duck: Any,
    rows: Sequence[dict[str, Any]],
    *,
    source: str = CSMAR_REPORT_SOURCE,
    chunk_size: int = 5_000,
    fetched_at: datetime | None = None,
) -> int:
    """幂等写 `report_metadata`（只存标记，不存数值）。"""
    if not rows:
        return 0
    stamp = fetched_at or datetime.now(UTC)
    payload = [
        (
            row["stock_code"],
            row["report_date"],
            row.get("report_type"),
            row.get("is_corrected"),
            row.get("declare_date"),
            source,
            stamp,
        )
        for row in rows
    ]
    # 同上：向量化写入，避免 executemany 的 DATE/TIMESTAMP 内存病理。
    import pandas as pd

    frame = pd.DataFrame(
        payload,
        columns=["stock_code", "report_date", "report_type", "is_corrected",
                 "declare_date", "source", "fetched_at"],
    )
    frame["report_date"] = pd.to_datetime(frame["report_date"]).dt.date
    frame["declare_date"] = pd.to_datetime(frame["declare_date"]).dt.date
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"])
    with duck.transaction() as connection:
        connection.register("_report_meta_rows", frame)
        try:
            connection.execute(
                """INSERT INTO report_metadata
                       (stock_code, report_date, report_type, is_corrected,
                        declare_date, source, fetched_at)
                   SELECT stock_code, report_date, report_type, is_corrected,
                          declare_date, source, fetched_at
                   FROM _report_meta_rows
                   ON CONFLICT (stock_code, report_date, source) DO UPDATE SET
                       report_type = COALESCE(excluded.report_type, report_metadata.report_type),
                       is_corrected = COALESCE(excluded.is_corrected,
                                               report_metadata.is_corrected),
                       declare_date = COALESCE(excluded.declare_date,
                                               report_metadata.declare_date),
                       fetched_at = excluded.fetched_at"""
            )
        finally:
            connection.unregister("_report_meta_rows")
    return len(payload)


def upsert_csrc_history(
    duck: Any,
    records: Sequence[dict[str, Any]],
    *,
    chunk_size: int = 5_000,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """把证监会适配器记录（每条带 `as_of_date`）写进 `industry_history`。

    只写 `industry_history(source='csrc_cninfo')`，**不碰 `stock_meta`**：
    当前归属仍由 `init._fetch_csrc_industry` 的 UPDATE 负责，这里只负责"变更历史"。
    适配器给的顺序（按 `as_of_date` 倒序）不影响结果：落库后按日期正序可查。
    """
    counters: dict[str, int] = {}
    rows = csrc_records_to_rows(records, counters=counters)
    written = upsert_industry_history(
        duck, rows, source=CSRC_INDUSTRY_SOURCE,
        chunk_size=chunk_size, fetched_at=fetched_at,
    )
    dates = [row["effective_date"] for row in rows]
    stocks = {row["stock_code"] for row in rows}
    skipped = sum(count for key, count in counters.items() if key.startswith("skipped_"))
    report: dict[str, Any] = {
        "status": "success",
        "source": CSRC_INDUSTRY_SOURCE,
        "records": len(records),
        "written": written,
        "skipped": skipped,
        "counters": counters,
        "stocks": len(stocks),
        "date_range": (
            [dates[0].isoformat(), dates[-1].isoformat()] if dates else None
        ),
    }
    if skipped:
        report["status"] = "partial"
        logger.warning("CSRC 行业历史跳过 %d 条（无 as_of_date/无分类）", skipped)
    return report


# ─── 查询 ────────────────────────────────────────────────────────


def read_industry_timeline(
    duck: Any,
    stock_codes: str | Sequence[str],
    *,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """按**时间正序**返回行业变更时间线（同一日期多源时按 source 排序）。

    2026-11-15：`stock_codes` 允许直接传单个代码字符串（CLI `--show 600519`
    就是这么调的）；否则 "600519" 会被当成字符序列、逐字符查询返回空。
    """
    if isinstance(stock_codes, str):
        stock_codes = [stock_codes]
    codes = [str(code).strip() for code in stock_codes if str(code).strip()]
    if not codes or not table_exists(duck, "industry_history"):
        return []
    slots = ", ".join("?" for _ in codes)
    params: list[Any] = list(codes)
    sql = f"""SELECT stock_code, effective_date, level1, level2, source, fetched_at
              FROM industry_history
              WHERE stock_code IN ({slots})"""
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY stock_code, effective_date, source"
    return duck.read_query(sql, params)


def _summary_for(duck: Any, table: str, date_column: str, source_column: str) -> dict[str, Any]:
    if not table_exists(duck, table):
        return {"table": table, "exists": False}
    totals = duck.read_query(
        f"""SELECT COUNT(*) AS rows, COUNT(DISTINCT stock_code) AS stocks,
                   MIN({date_column}) AS first_date, MAX({date_column}) AS last_date
              FROM {table}"""
    )[0]
    per_source = duck.read_query(
        f"""SELECT {source_column} AS source, COUNT(*) AS rows,
                   COUNT(DISTINCT stock_code) AS stocks,
                   MIN({date_column}) AS first_date, MAX({date_column}) AS last_date
              FROM {table} GROUP BY 1 ORDER BY 1"""
    )
    return {
        "table": table,
        "exists": True,
        "rows": int(totals["rows"]),
        "stocks": int(totals["stocks"]),
        "first_date": str(totals["first_date"])[:10] if totals["first_date"] else None,
        "last_date": str(totals["last_date"])[:10] if totals["last_date"] else None,
        "by_source": [
            {
                **{key: value for key, value in row.items() if key != "first_date"},
                "first_date": str(row["first_date"])[:10] if row["first_date"] else None,
                "last_date": str(row["last_date"])[:10] if row["last_date"] else None,
            }
            for row in per_source
        ],
    }


def industry_history_summary(duck: Any) -> dict[str, Any]:
    """`industry_history` 现状（未应用 v40 迁移时返回 exists=False，不抛错）。"""
    return _summary_for(duck, "industry_history", "effective_date", "source")


def report_metadata_summary(duck: Any) -> dict[str, Any]:
    """`report_metadata` 现状。"""
    summary = _summary_for(duck, "report_metadata", "report_date", "source")
    if summary.get("exists"):
        flags = duck.read_query(
            """SELECT COUNT(*) AS rows,
                      COUNT(*) FILTER (WHERE is_corrected) AS corrected,
                      COUNT(*) FILTER (WHERE declare_date IS NOT NULL) AS with_declare_date,
                      COUNT(*) FILTER (WHERE report_type = 'consolidated') AS consolidated,
                      COUNT(*) FILTER (WHERE report_type = 'parent') AS parent
               FROM report_metadata"""
        )[0]
        summary["flags"] = {key: int(value) for key, value in flags.items()}
    return summary


# ─── DTA 读取 + 导入（真正跑数据） ─────────────────────────────────


def iter_dta_chunks(
    path: Path | str,
    columns: Sequence[str],
    *,
    chunk_size: int = 200_000,
    max_rows: int = 0,
) -> Iterator[pd.DataFrame]:
    """只读需要的列、分块读取 .dta（几百 MB 文件不进内存）。

    `max_rows>0` 时读满即停——抽样验证用（"先读 1000 行看非空率"）。
    """
    import pandas as pd

    dta_path = Path(path)
    if not dta_path.exists():
        raise FileNotFoundError(f"CSMAR dta 不存在: {dta_path}")
    seen = 0
    # 用上下文管理器（StataReader 唯一受支持的关闭方式），提前 break 时也关文件
    with pd.read_stata(
        dta_path,
        columns=list(columns),
        chunksize=chunk_size,
        convert_categoricals=False,
    ) as reader:
        for chunk in reader:
            if max_rows > 0:
                remaining = max_rows - seen
                if remaining <= 0:
                    break
                chunk = chunk.iloc[:remaining]
            seen += len(chunk)
            yield chunk
            if max_rows > 0 and seen >= max_rows:
                break


def dta_column_labels(path: Path | str) -> dict[str, str]:
    """字段标签（不读数据行，仅头部；用于核对 Typrep/Indcd 说明）。"""
    import pandas as pd

    with pd.read_stata(path, chunksize=1, convert_categoricals=False) as reader:
        labels = dict(reader.variable_labels())
        with contextlib.suppress(StopIteration):  # 空文件时 next() 直接 StopIteration
            next(iter(reader))
    return labels


def probe_dta_sample(
    path: Path | str,
    columns: Sequence[str],
    *,
    rows: int = 1_000,
) -> dict[str, Any]:
    """抽样体检：读前 N 行的非空率（导入前先证明列不是空的）。"""
    sample_rows = 0
    nonnull: dict[str, int] = {name: 0 for name in columns}
    literal_none: dict[str, int] = {name: 0 for name in columns}
    for chunk in iter_dta_chunks(path, columns, chunk_size=max(rows, 1), max_rows=rows):
        sample_rows += len(chunk)
        for name in columns:
            if name not in chunk.columns:
                continue
            series = chunk[name]
            nonnull[name] += int(series.notna().sum())
            as_text = series.astype(str).str.strip().str.lower()
            literal_none[name] += int((as_text == "none").sum())
    rates = {
        name: (round(nonnull[name] / sample_rows, 6) if sample_rows else None)
        for name in columns
    }
    literal_none_rate = {
        name: (round(literal_none[name] / sample_rows, 6) if sample_rows else None)
        for name in columns
    }
    return {
        "path": str(path),
        "rows_read": sample_rows,
        "nonnull_rate": rates,
        "literal_none_rate": literal_none_rate,
        "literal_none_counts": literal_none,
    }


def _top_values(rows: Sequence[dict[str, Any]], key: str, limit: int = 10) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        counts[str(value)] = counts.get(str(value), 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"value": value, "rows": count} for value, count in ordered[:limit]]


def _import_report(
    *,
    kind: str,
    source: str,
    path: Path | str,
    counters: dict[str, int],
    rows_written: int,
    rows_parsed: Sequence[dict[str, Any]],
    existing_rows: int | None,
    existing_keys: set[tuple[Any, ...]] | None,
    dry_run: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pending = 0
    if existing_keys is not None:
        pending = sum(
            1
            for row in rows_parsed
            if (row.get("stock_code"), row.get("report_date") or row.get("effective_date"))
            not in existing_keys
        )
    report: dict[str, Any] = {
        "status": "success",
        "kind": kind,
        "source": source,
        "dta": str(path),
        "dry_run": dry_run,
        "rows_parsed": len(rows_parsed),
        "rows_written": rows_written,
        "existing_rows": existing_rows,
        "pending_new_rows": pending if existing_keys is not None else None,
        "counters": counters,
    }
    if extra:
        report.update(extra)
    return report


def _existing_key_set(
    duck: Any, table: str, source: str, date_column: str
) -> tuple[int | None, set[tuple[Any, ...]] | None]:
    if not table_exists(duck, table):
        return None, None
    total = duck.read_query(f"SELECT COUNT(*) AS c FROM {table}")[0]["c"]
    keys = duck.read_query(
        f"SELECT stock_code, {date_column} AS d FROM {table} WHERE source = ?", [source]
    )
    return int(total), {(row["stock_code"], row["d"]) for row in keys}


def import_csmar_industry_history(
    duck: Any,
    path: Path | str | None = None,
    *,
    dry_run: bool = True,
    chunk_size: int = 200_000,
    max_rows: int = 0,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """FI_T1.dta → `industry_history(source='csmar_indcd')`。

    `dry_run=True`（默认）只解析统计、不写库；幂等（PK + ON CONFLICT）。
    只写 `industry_history`，**不碰 `stock_meta`**；`Indcd1` 口径不读不写。
    """
    dta_path = Path(path) if path is not None else DEFAULT_INDUSTRY_DTA
    counters: dict[str, int] = {"rows_read": 0}
    parsed: dict[tuple[str, date], dict[str, Any]] = {}
    indnme_total = 0
    indnme_present = 0
    for chunk in iter_dta_chunks(dta_path, INDUSTRY_DTA_COLUMNS,
                                 chunk_size=chunk_size, max_rows=max_rows):
        counters["rows_read"] += len(chunk)
        if "Indnme" in chunk.columns:
            series = chunk["Indnme"]
            indnme_total += len(series)
            indnme_present += sum(1 for value in series if clean_text(value) is not None)
        rows = parse_csmar_industry_rows(chunk, counters=counters)
        for row in rows:
            parsed.setdefault((row["stock_code"], row["effective_date"]), row)

    rows = [parsed[key] for key in sorted(parsed)]
    existing_rows, existing_keys = _existing_key_set(
        duck, "industry_history", CSMAR_INDUSTRY_SOURCE, "effective_date"
    )
    written = 0
    if not dry_run:
        written = upsert_industry_history(
            duck, rows, source=CSMAR_INDUSTRY_SOURCE,
            chunk_size=max(min(chunk_size, 5_000), 1), fetched_at=fetched_at,
        )
    dates = [row["effective_date"] for row in rows]
    extra = {
        "stocks": len({row["stock_code"] for row in rows}),
        "date_range": [dates[0].isoformat(), dates[-1].isoformat()] if dates else None,
        "indcd_nonnull_rate": (
            round(counters.get("parsed_rows", 0) / counters["rows_read"], 6)
            if counters["rows_read"] else None
        ),
        "indnme_nonnull_rate": (
            round(indnme_present / indnme_total, 6) if indnme_total else None
        ),
        "top_indcd": _top_values(rows, "level2"),
        "top_industries": _top_values(rows, "level1"),
    }
    return _import_report(
        kind="industry_history",
        source=CSMAR_INDUSTRY_SOURCE,
        path=dta_path,
        counters=counters,
        rows_written=written,
        rows_parsed=rows,
        existing_rows=existing_rows,
        existing_keys=existing_keys,
        dry_run=dry_run,
        extra=extra,
    )


def import_csmar_report_metadata(
    duck: Any,
    path: Path | str | None = None,
    *,
    dry_run: bool = True,
    chunk_size: int = 200_000,
    max_rows: int = 0,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """FS_Combas.dta → `report_metadata(source='csmar')`（只存标记）。"""
    dta_path = Path(path) if path is not None else DEFAULT_METADATA_DTA
    counters: dict[str, int] = {"rows_read": 0}
    parsed: dict[tuple[str, date], dict[str, Any]] = {}
    for chunk in iter_dta_chunks(dta_path, REPORT_DTA_COLUMNS,
                                 chunk_size=chunk_size, max_rows=max_rows):
        counters["rows_read"] += len(chunk)
        for row in parse_csmar_report_metadata_rows(chunk, counters=counters):
            existing = parsed.get((row["stock_code"], row["report_date"]))
            if existing is None or existing["report_type"] == "parent" and row["report_type"] == "consolidated":
                parsed[(row["stock_code"], row["report_date"])] = row

    rows = [parsed[key] for key in sorted(parsed)]
    existing_rows, existing_keys = _existing_key_set(
        duck, "report_metadata", CSMAR_REPORT_SOURCE, "report_date"
    )
    written = 0
    if not dry_run:
        written = upsert_report_metadata(
            duck, rows, chunk_size=max(min(chunk_size, 5_000), 1), fetched_at=fetched_at,
        )
    dates = [row["report_date"] for row in rows]
    corrected = sum(1 for row in rows if row["is_corrected"] is True)
    flagged_false = sum(1 for row in rows if row["is_corrected"] is False)
    unknown_flag = sum(1 for row in rows if row["is_corrected"] is None)
    with_declare = sum(1 for row in rows if row["declare_date"] is not None)
    extra = {
        "stocks": len({row["stock_code"] for row in rows}),
        "date_range": [dates[0].isoformat(), dates[-1].isoformat()] if dates else None,
        "report_type_counts": _top_values(rows, "report_type"),
        "is_corrected": {
            "true": corrected, "false": flagged_false, "unknown": unknown_flag,
        },
        "with_declare_date": with_declare,
        "declare_date_range": (
            [
                min(row["declare_date"] for row in rows if row["declare_date"]).isoformat(),
                max(row["declare_date"] for row in rows if row["declare_date"]).isoformat(),
            ]
            if with_declare else None
        ),
    }
    return _import_report(
        kind="report_metadata",
        source=CSMAR_REPORT_SOURCE,
        path=dta_path,
        counters=counters,
        rows_written=written,
        rows_parsed=rows,
        existing_rows=existing_rows,
        existing_keys=existing_keys,
        dry_run=dry_run,
        extra=extra,
    )
