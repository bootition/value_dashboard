"""补采 `balance_sheet` 的 7 个补采科目（schema v48）—— **只 UPDATE 新列，不新增行**。

域与来源（唯一真源：`.planning/2026-11-18-indicator-feasibility-audit/out/source_probe.md`）
--------------------------------------------------------------------------------------
| 列 | 源 | 源字段 |
|---|---|---|
| `fixed_assets_gross`        | 新浪 `stock_financial_report_sina(symbol="资产负债表")` | `固定资产原值` |
| `accumulated_depreciation`  | 同上 | `累计折旧`（模板缺列时用 `固定资产原值 − 固定资产净值` 推导） |
| `other_current_assets`      | 东财 `stock_balance_sheet_by_report_em` | `OTHER_CURRENT_ASSET` |
| `receivables_financing`     | 东财 | `FINANCE_RECE` |
| `long_term_payables`        | 东财 | `LONG_PAYABLE` |
| `investment_property`       | 东财 | `INVEST_REALESTATE` |
| `development_expenditure`   | 东财 | `DEVELOP_EXPENSE` |

为什么原值/累计折旧必须走新浪：东财 `stock_balance_sheet_by_report_em` 的 319 列里**没有**原值/累计折旧
（`source_probe` §1.A 实测 keyword 扫描 `gross/accum/deprec` 全部 `NONE`），只有新浪的中文列名有，且新浪
回溯更深（600519 到 1998-12-31）。

单位：**元**，不做任何换算（探源报告 §2 的三源四方对账）
--------------------------------------------------------
600519 FY2025（report_date=2025-12-31）实测，三家逐分钱相等，**全部是「元」**（不是万元、不是千元）：

    原值(新浪)      39,108,502,833.61
    累计折旧(新浪)  16,619,313,312.46
    净值(新浪)      22,489,189,521.15  = 原值 − 累计折旧          ← 恒等式①（残差 0 元）
    减值准备(新浪)       1,067,216.80
    净额(新浪)      22,488,122,304.35  = 净值 − 减值准备           ← 恒等式②（残差 0 元）
    FIXED_ASSET(东财) 22,488,122,304.35 = 净额                     ← 恒等式③（两源相等）
    库内 fixed_assets 22,488,123,904.35 (double 舍入 ~1.6e3)

⚠️ 因此 `fixed_assets_gross − accumulated_depreciation == balance_sheet.fixed_assets` **只在没有固定资产
减值准备时成立**；有减值时残差恒等于「固定资产减值准备」（上面 1,067,216.80 元 = 净值与净额之差）。
本脚本的勾稽断言因此写成两条：①`原值 − 累计折旧 − 净值 ≈ 0`（源内自洽）；②`原值 − 累计折旧 − 减值
− 库内 fixed_assets ≈ 0`（跨源）。**不要**把断言简化成①的形式去卡 `fixed_assets`，差的就是减值。

口径纪律（照抄 `source_probe` §5 的坑，别再踩）
----------------------------------------------
* **原值/累计折旧只有年报(12-31)与中报(06-30)有值，Q1/Q3 恒 NULL** —— 脚本显式只写 6/12 月末的行；
  绝不按季度序列使用该两列（否则 Q1/Q3 静默丢样本）。
* 东财 `CURRENT_ASSET_OTHER` **不是**「其他流动资产」，与 `OTHER_CURRENT_ASSET` 有 9 期同时非空，
  **禁止 COALESCE 混用**（探源 §5.2）。本脚本只认 `OTHER_CURRENT_ASSET`。
* 源返回 NaN 一律**跳过（保留 NULL）**，绝不写 0；只有源明确给 0 才写 0（与「真 0 / 缺数」的口径一致）。
* `receivables_financing` FY2019 起才有（CAS22），更早的 NULL 是准则使然，不是缺数。
* 行键 `(stock_code, report_date)`：写库前先 `SELECT` 出主表已有的键做 **inner join**，缺行只计数不插入。

用法
----
    python scripts/fetch_balance_sheet_subjects.py                  # dry-run（只打印计划与证据）
    python scripts/fetch_balance_sheet_subjects.py --limit 30 --codes 600519,600036,688111,601318
    python scripts/fetch_balance_sheet_subjects.py --yes --limit 30 --codes 600519,600036,688111,601318
    python scripts/fetch_balance_sheet_subjects.py --yes --resume    # 全市场续跑（拿不到锁就跳过退出）

写库纪律
--------
* 单写者锁：每个批次用 `exclusive_update(paths.duckdb_path)` + `duck.write_connection(timeout=2)` 包住，
  写几秒就释放（不整段持有 55 分钟，避免饿死自动更新）；**拿不到锁直接跳过退出（exit 3）**，不排队死等。
* 批量写入：pandas `register` + **单条** `INSERT ... SELECT ... ON CONFLICT`（ops-knowledge-base D19/D36），
  禁止 `executemany`。SELECT 侧 `JOIN balance_sheet` 保证**只可能更新既有行**，不可能插入新行。
* 幂等：`ON CONFLICT DO UPDATE SET col = COALESCE(excluded.col, balance_sheet.col)` —— 源给 NULL 时保留旧值，
  所以重跑不改行数、不改已有值。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage.duckdb_store import DuckDBStore, DuckDBWriteLockError  # noqa: E402
from app.core.storage.path_policy import (  # noqa: E402
    PathIsolationError,
    require_formal_maintenance_paths,
)
from app.core.storage.update_lock import (  # noqa: E402
    UpdateLockError,
    exclusive_update,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# --- 源字段名（照抄，勿改）-----------------------------------------------------
SINA_DATE_COL = "报告日"                    # 形如 '20251231' 的字符串
SINA_GROSS_COL = "固定资产原值"
SINA_ACCUM_COL = "累计折旧"
SINA_NET_COL = "固定资产净值"               # 原值 − 累计折旧（减值前）
SINA_IMPAIR_COL = "固定资产减值准备"
SINA_NET_AMOUNT_COL = "固定资产净额"        # 净值 − 减值准备 == 库内 fixed_assets

EM_DATE_COL = "REPORT_DATE"                 # 形如 '2025-12-31 00:00:00'
EM_FIELD_MAP: dict[str, str] = {
    "other_current_assets": "OTHER_CURRENT_ASSET",   # ⚠️ 不是 CURRENT_ASSET_OTHER
    "receivables_financing": "FINANCE_RECE",
    "long_term_payables": "LONG_PAYABLE",
    "investment_property": "INVEST_REALESTATE",
    "development_expenditure": "DEVELOP_EXPENSE",
}

SINA_SUBJECT_COLUMNS = ("fixed_assets_gross", "accumulated_depreciation")
# 任务书指定的 5 个科目来自东财；但新浪同一张表的同名中文列实测与东财**逐分钱相等**
# （探源 §2 的 5 科目交叉验证表，FY2025 600519 全部相等；§1.A #3 还要求「以新浪中文列名为准」）。
# 因此 `--subject-source sina` 可用「一次新浪请求」拿满 7 列，省掉东财每股 2.7~4.4s 的分页开销。
# 注意「长期应付款」不要取成「长期应付款合计」，「其他流动资产」不要取成「其他流动负债」。
SINA_SUBJECT_FIELD_MAP: dict[str, str] = {
    "other_current_assets": "其他流动资产",
    "receivables_financing": "应收款项融资",
    "long_term_payables": "长期应付款",
    "investment_property": "投资性房地产",
    "development_expenditure": "开发支出",
}
SUBJECT_COLUMNS = (*SINA_SUBJECT_COLUMNS, *EM_FIELD_MAP)
# 半年报 + 年报才有原值/累计折旧（探源 §5.3：Q1/Q3 恒 NULL）
ANNUAL_OR_SEMI_MONTHS = (6, 12)
# 勾稽残差容差（元）。1 元对百亿级科目等价于「逐分钱相等」的下限
IDENTITY_TOLERANCE_YUAN = 1.0

_INSERT_COLUMNS = ", ".join(("stock_code", "report_date", *SUBJECT_COLUMNS))
_INSERT_SELECT = ", ".join(("s.stock_code", "s.report_date", *(f"s.{c}" for c in SUBJECT_COLUMNS)))


def build_write_sql(only_missing: bool) -> str:
    """两种冲突解决语义（都只能 UPDATE 既有行，都不可能插入新行）：

    * 默认（`only_missing=False`）：源新值优先 —— `COALESCE(excluded, 库内)`，用于首灌/刷新。
    * `--only-missing`：**库内已有值优先** —— `COALESCE(库内, excluded)`，
      即只在目标列当前为 NULL 时才写；已有数据（含新浪档填好的）永不被覆盖。
    """
    conflict_set = ",\n".join(
        f"    {c} = COALESCE(balance_sheet.{c}, excluded.{c})"
        if only_missing
        else f"    {c} = COALESCE(excluded.{c}, balance_sheet.{c})"
        for c in SUBJECT_COLUMNS
    )
    # SELECT 侧 JOIN balance_sheet：结构上只可能更新既有行，不可能插入新行。
    return (
        f"INSERT INTO balance_sheet ({_INSERT_COLUMNS})\n"
        f"SELECT {_INSERT_SELECT}\n"
        "FROM _bs_subjects s\n"
        "JOIN balance_sheet b ON b.stock_code = s.stock_code AND b.report_date = s.report_date\n"
        "ON CONFLICT (stock_code, report_date) DO UPDATE SET\n"
        f"{conflict_set}\n"
    )


class LockBusyError(RuntimeError):
    """单写者锁被别的进程持有 —— 按纪律跳过退出，不排队死等。"""


# ---------------------------------------------------------------------------
# 帮助函数
# ---------------------------------------------------------------------------
def _sina_symbol(code: str) -> str:
    if code.startswith(("6", "9")):          # 60/68 沪主板/科创、900 B股
        return f"sh{code}"
    if code.startswith(("0", "2", "3")):     # 00/30 深主板/创业、200 B股
        return f"sz{code}"
    return f"bj{code}"                       # 4/8 北交所（新浪可能不支持，失败按只跳过）


def _em_symbol(code: str) -> str:
    if code.startswith(("6", "9")):
        return f"SH{code}"
    if code.startswith(("0", "2", "3")):
        return f"SZ{code}"
    return f"BJ{code}"


class _Throttle:
    """请求间最小间隔（≤2 req/s，即 ≥0.5s）。"""

    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)

    def mark(self) -> None:
        self._last = time.monotonic()


def _call_with_retry(fn: Callable[[], Any], *, stage: str, code: str, retries: int) -> Any:
    """逐股隔离（AGENTS R6）：单只失败只影响自己，重试 ≤ retries 次。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 外部接口抛什么都有可能
            last = exc
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    assert last is not None
    raise RuntimeError(f"{stage} 失败（重试 {retries} 次后）：{type(last).__name__}: {last}") from last


def _num(series: pd.Series) -> pd.Series:
    """源里的数字一律按数值解析；解析不了的变 NaN（→ 落库 NULL，绝不落 0）。"""
    return pd.to_numeric(series, errors="coerce")


def _num_or_blank(frame: pd.DataFrame, column: str) -> pd.Series:
    """模板里可能整列不存在（银行/保险无「累计折旧」），此时返回全 NaN 而不是抛错。"""
    if column in frame.columns:
        return _num(frame[column])
    return pd.Series(float("nan"), index=frame.index, dtype="float64")


def _clean(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number) or number != number:  # NaN
        return None
    return number


# ---------------------------------------------------------------------------
# 抓取 + 解析
# ---------------------------------------------------------------------------
def fetch_sina(code: str, *, throttle: _Throttle, retries: int) -> pd.DataFrame:
    import akshare as ak

    symbol = _sina_symbol(code)

    def _do() -> pd.DataFrame:
        throttle.wait()
        try:
            return ak.stock_financial_report_sina(stock=symbol, symbol="资产负债表")
        finally:
            throttle.mark()

    return _call_with_retry(_do, stage="sina", code=code, retries=retries)


def fetch_em(code: str, *, throttle: _Throttle, retries: int) -> pd.DataFrame:
    import akshare as ak

    symbol = _em_symbol(code)

    def _do() -> pd.DataFrame:
        throttle.wait()
        try:
            return ak.stock_balance_sheet_by_report_em(symbol=symbol)
        finally:
            throttle.mark()

    return _call_with_retry(_do, stage="em", code=code, retries=retries)


def parse_sina(
    frame: pd.DataFrame,
    code: str,
    *,
    with_all_subjects: bool = False,
) -> tuple[dict[date, dict[str, float | None]], dict[date, dict[str, Any]]]:
    """→ (科目行, 勾稽用的原始五件套)。列名按中文取，**禁止按列位置**（模板列数 134~150 浮动）。

    `with_all_subjects=True` 时同一张表的 5 个中文科目也一起取（`--subject-source sina` 快路径）。
    """
    if frame is None or frame.empty:
        raise ValueError("新浪返回空表")
    if SINA_DATE_COL not in frame.columns:
        raise ValueError(f"新浪返回缺「{SINA_DATE_COL}」列")
    if SINA_GROSS_COL not in frame.columns:
        raise ValueError(f"新浪该模板无「{SINA_GROSS_COL}」列")

    dates = pd.to_datetime(frame[SINA_DATE_COL].astype(str), format="%Y%m%d", errors="coerce")
    gross = _num(frame[SINA_GROSS_COL])
    accum = _num_or_blank(frame, SINA_ACCUM_COL)
    net = _num_or_blank(frame, SINA_NET_COL)
    impair = _num_or_blank(frame, SINA_IMPAIR_COL)
    net_amount = _num_or_blank(frame, SINA_NET_AMOUNT_COL)
    extra = (
        {column: _num_or_blank(frame, field) for column, field in SINA_SUBJECT_FIELD_MAP.items()}
        if with_all_subjects
        else {}
    )

    subjects: dict[date, dict[str, float | None]] = {}
    recon: dict[date, dict[str, Any]] = {}
    for position in range(len(frame)):
        moment = dates.iloc[position]
        if pd.isna(moment):
            continue
        report_date = moment.date()
        gross_value = _clean(gross.iloc[position])
        accum_value = _clean(accum.iloc[position])
        net_value = _clean(net.iloc[position])
        # 累计折旧列缺失（银行/保险模板实测没有这一列）→ 用 原值 − 固定资产净值 推导。
        # 依据：探源 §1.A #2 —— 601398 FY2025 5107.11 − 2840.83 = 2266.28 亿（减值前累计折旧）；
        # 恒等式①「原值 − 累计折旧 = 净值」在 600519 FY2025 上残差 0 元。
        if accum_value is None and gross_value is not None and net_value is not None:
            accum_value = gross_value - net_value
        # 原值/累计折旧只有中报与年报有值；季报一律不写（保留 NULL，见模块 docstring 口径纪律）
        if report_date.month not in ANNUAL_OR_SEMI_MONTHS:
            gross_value = None
            accum_value = None
        values: dict[str, float | None] = {
            "fixed_assets_gross": gross_value,
            "accumulated_depreciation": accum_value,
        }
        if with_all_subjects:
            values.update({column: _clean(series.iloc[position]) for column, series in extra.items()})
        if not any(value is not None for value in values.values()):
            continue
        subjects[report_date] = values
        recon[report_date] = {
            "gross": gross_value,
            "accum": accum_value,
            "net": net_value,
            "impairment": _clean(impair.iloc[position]),
            "net_amount": _clean(net_amount.iloc[position]),
            "derived_accum": accum_value is not None and _clean(accum.iloc[position]) is None,
        }
    return subjects, recon


def parse_em(frame: pd.DataFrame, code: str) -> dict[date, dict[str, float | None]]:
    """东财 319 列 / 英文大写字段。`REPORT_DATE` = '2025-12-31 00:00:00' → 归一成 DATE。"""
    if frame is None or frame.empty:
        raise ValueError("东财返回空表")
    if EM_DATE_COL not in frame.columns:
        raise ValueError(f"东财返回缺「{EM_DATE_COL}」列")
    dates = pd.to_datetime(frame[EM_DATE_COL], errors="coerce")
    series = {column: _num(frame[field]) for column, field in EM_FIELD_MAP.items() if field in frame.columns}

    rows: dict[date, dict[str, float | None]] = {}
    for position in range(len(frame)):
        moment = dates.iloc[position]
        if pd.isna(moment):
            continue
        values = {column: _clean(values.iloc[position]) for column, values in series.items()}
        if not any(value is not None for value in values.values()):
            continue  # 全 NULL 的期不写占位（D49：缺数不写 0）
        rows[moment.date()] = values
    return rows


# ---------------------------------------------------------------------------
# 库操作
# ---------------------------------------------------------------------------
def select_universe(duck: DuckDBStore) -> list[str]:
    rows = duck.read_query("SELECT stock_code FROM stock_meta WHERE is_listed IS TRUE ORDER BY stock_code")
    return [str(row["stock_code"]) for row in rows]


def select_codes_missing_subjects(duck: DuckDBStore, *, listed_only: bool = False) -> list[str]:
    """`--only-missing` 的目标集合：5 个东财科目**一条非空都没有**的股票。

    语义：这些股票在第一遍（新浪档）里 5 科目全部没拿到（新浪模板无列 / 该股数据全空），
    正是「em + only-missing」补漏档要去补的那批。判据是覆盖率 = 0 而不是「有任意 NULL」——
    否则会把「2019 前没有应收款项融资」这种准则性 NULL 也算成待补，白跑全市场。

    `listed_only=True` 只取 `stock_meta.is_listed`（补漏档优先跑这批：退市股东财多为空表，
    实测 000003/000004/000005 直接抛 `KeyError: 'data'`）。
    """
    has_any = " OR ".join(f"b.{column} IS NOT NULL" for column in EM_FIELD_MAP)
    meta_join = "JOIN stock_meta m ON m.stock_code = b.stock_code AND m.is_listed IS TRUE " if listed_only else ""
    rows = duck.read_query(
        f"SELECT b.stock_code FROM balance_sheet b {meta_join}"
        f"GROUP BY b.stock_code HAVING NOT BOOL_OR({has_any}) ORDER BY b.stock_code"
    )
    return [str(row["stock_code"]) for row in rows]


def select_existing_keys(duck: DuckDBStore, codes: Iterable[str]) -> set[tuple[str, date]]:
    """inner join 用：主表里真实存在的 (stock_code, report_date)。缺行说明主表没有该期，只计数不插入。"""
    code_list = list(codes)
    if not code_list:
        return set()
    placeholders = ",".join("?" for _ in code_list)
    rows = duck.read_query(
        f"SELECT stock_code, report_date FROM balance_sheet WHERE stock_code IN ({placeholders})",
        code_list,
    )
    return {(str(row["stock_code"]), row["report_date"]) for row in rows}


def _subject_predicate() -> str:
    return " OR ".join(f"{column} IS NOT NULL" for column in SUBJECT_COLUMNS)


def count_rows_with_subjects(duck: DuckDBStore, codes: Iterable[str]) -> int:
    code_list = list(codes)
    if not code_list:
        return 0
    placeholders = ",".join("?" for _ in code_list)
    rows = duck.read_query(
        f"SELECT COUNT(*) AS n FROM balance_sheet WHERE stock_code IN ({placeholders}) AND ({_subject_predicate()})",
        code_list,
    )
    return int(rows[0]["n"])


def db_fingerprint(duck: DuckDBStore, codes: Iterable[str]) -> dict[str, Any]:
    """幂等验证用指纹：行数 + 7 列各自的 SUM（值不变 ⇒ 和不变）。"""
    code_list = list(codes)
    if not code_list:
        return {"rows": 0, "column_sums": {}}
    placeholders = ",".join("?" for _ in code_list)
    sums = ", ".join(f"SUM({column}) AS {column}" for column in SUBJECT_COLUMNS)
    rows = duck.read_query(
        f"SELECT COUNT(*) AS n, {sums} FROM balance_sheet WHERE stock_code IN ({placeholders})",
        code_list,
    )
    row = rows[0]
    return {
        "stock_codes": len(code_list),
        "rows": int(row["n"]),
        "column_sums": {column: _clean(row[column]) for column in SUBJECT_COLUMNS},
    }


def write_batch(duck: DuckDBStore, database_path: Path, frame: pd.DataFrame, *, only_missing: bool = False) -> None:
    """pandas register + 单条 INSERT ... SELECT ... ON CONFLICT（D19/D36）。锁拿不到就抛 LockBusyError。"""
    if frame.empty:
        return
    try:
        with exclusive_update(database_path), duck.write_connection(timeout=2.0) as conn:
            conn.register("_bs_subjects", frame)
            try:
                conn.execute(build_write_sql(only_missing))
            finally:
                conn.unregister("_bs_subjects")
    except (UpdateLockError, DuckDBWriteLockError) as exc:
        raise LockBusyError(str(exc)) from exc


def build_stage_frame(labels: list[dict[str, Any]]) -> pd.DataFrame:
    """把逐股解析结果整理成写入用的 DataFrame（float64 的 NaN 由 DuckDB 映射成 NULL）。"""
    frame = pd.DataFrame(labels)
    if frame.empty:
        return frame
    frame = frame[["stock_code", "report_date", *SUBJECT_COLUMNS]].copy()
    frame["stock_code"] = frame["stock_code"].astype(str)
    frame["report_date"] = pd.to_datetime(frame["report_date"]).dt.date
    for column in SUBJECT_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    return frame


# ---------------------------------------------------------------------------
# 状态（断点续传）
# ---------------------------------------------------------------------------
def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"completed": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("状态文件不可读，按空状态继续：%s (%s)", path, exc)
        return {"completed": []}


def save_state(path: Path, completed: set[str], stats: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "completed": sorted(completed),
        "updated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
        "stats": {
            "stocks_processed": stats.get("stocks_processed", 0),
            "rows_staged": stats.get("rows_staged", 0),
            "stocks_failed": stats.get("stocks_failed", 0),
        },
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="补采 balance_sheet 的 7 个科目（只 UPDATE 新列，不新增行）")
    ap.add_argument("--yes", action="store_true", help="确认写库；不带则 dry-run（只打印计划与证据）")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理多少只（0=全部待处理）")
    ap.add_argument("--codes", default="", help="优先处理的股票代码，逗号分隔（不足 --limit 时按默认顺序补足）")
    ap.add_argument("--resume", action="store_true", help="跳过状态文件里已完成的股票")
    ap.add_argument("--batch-size", type=int, default=25, help="每批抓取/写入的股票数（D47 分块）")
    ap.add_argument("--sleep", type=float, default=0.5, help="请求间最小间隔秒（≤2 req/s，默认 0.5）")
    ap.add_argument(
        "--subject-source",
        choices=("both", "sina", "em"),
        default="both",
        help="both=原值/折旧走新浪、5 科目走东财（任务书指定，约 7s/股）；"
        "sina=7 列全部取新浪同名中文列（探源 §2 实测两源逐分钱相等，约 1.1s/股）；"
        "em=只走东财、只补 5 个科目（原值/折旧两列不动，约 4s/股）",
    )
    ap.add_argument(
        "--only-missing",
        action="store_true",
        help="只在目标列当前为 NULL 时才写（SQL 侧 COALESCE(库内, 源)），且目标集合收窄为"
        "「5 个东财科目覆盖率 = 0」的股票 —— 用于新浪全量跑完后的东财补漏",
    )
    ap.add_argument("--retries", type=int, default=2, help="单只单接口重试次数上限（默认 2）")
    ap.add_argument("--dry-run-sample", type=int, default=3, help="dry-run 且未给 --limit 时的抽样只数")
    ap.add_argument("--state-file", default=str(PROJECT_ROOT / ".planning/state/fetch_balance_sheet_subjects.json"))
    ap.add_argument(
        "--evidence-json",
        default=str(PROJECT_ROOT / ".planning/2026-11-18-indicator-feasibility-audit/out/data_bs-subjects_evidence.json"),
    )
    ap.add_argument("--recon-code", default="600519", help="勾稽断言的股票代码（默认 600519）")
    ap.add_argument("--recon-date", default="2025-12-31", help="勾稽断言的报告期（默认 2025-12-31）")
    args = ap.parse_args()

    try:
        paths = require_formal_maintenance_paths()
    except PathIsolationError as error:
        ap.error(str(error))

    started = time.monotonic()
    duck = DuckDBStore(paths=paths)
    throttle = _Throttle(args.sleep)

    universe = select_universe(duck)
    backfill_targets: list[str] = []
    if args.only_missing:
        # 补漏档的目标集合来自库（覆盖率=0 的股票），不是全部在市股票
        listed_targets = select_codes_missing_subjects(duck, listed_only=True)
        all_targets = select_codes_missing_subjects(duck)
        listed_set = set(listed_targets)
        backfill_targets = listed_targets + [code for code in all_targets if code not in listed_set]
        universe = backfill_targets
        total_codes = int(duck.read_query("SELECT COUNT(DISTINCT stock_code) AS n FROM balance_sheet")[0]["n"])
        logger.info(
            "补漏档目标（5 个东财科目覆盖率 = 0）：在市 **%d** 只 / 含退市合计 **%d** 只"
            "（balance_sheet 共 %d 只股票）。在市优先跑，退市股排后面。",
            len(listed_targets), len(backfill_targets), total_codes,
        )
    priority = [code.strip() for code in args.codes.split(",") if code.strip()]
    ordered: list[str] = []
    for code in priority:
        if code not in ordered:
            ordered.append(code)
    # 优先级股票即便不在在市名单里也允许（例如做退市股试点）
    for code in universe:
        if code not in ordered:
            ordered.append(code)

    state_path = Path(args.state_file)
    completed: set[str] = set()
    if args.resume:
        completed = set(load_state(state_path).get("completed", []))
        logger.info("续跑：状态文件已记录 %d 只，跳过", len(completed))
    pending = [code for code in ordered if code not in completed]
    skipped_completed = len(ordered) - len(pending)

    limit = args.limit if args.limit > 0 else (0 if args.yes else args.dry_run_sample)
    if limit:
        pending = pending[:limit]

    logger.info(
        "在位股票 %d 只 | 优先 %d 只 | 本次计划 %d 只 | 已完成跳过 %d 只 | 写库=%s | 限速 %.2fs/req",
        len(universe), len(priority), len(pending), skipped_completed, "是" if args.yes else "否（dry-run）", args.sleep,
    )
    if not pending:
        logger.info("没有待处理的股票，退出。")
        return 0

    stats: dict[str, Any] = {
        "stocks_targeted": len(pending),
        "stocks_processed": 0,
        "stocks_failed": 0,
        "stocks_no_db_rows": 0,
        "rows_fetched": 0,
        "rows_missing_in_db": 0,
        "rows_staged": 0,
        "rows_written": 0,
        "columns_nonnull": {column: 0 for column in SUBJECT_COLUMNS},
        "columns_derived": {"accumulated_depreciation": 0},
        "identity_checks": {"checked": 0, "residual_over_tolerance": 0, "worst_residual": 0.0, "worst_key": None},
        "em_requests_skipped": 0,
        "failures": [],
        "batches": [],
        "recon": {},
    }
    recon_target = (args.recon_code, datetime.strptime(args.recon_date, "%Y-%m-%d").date())
    recon_capture: dict[str, Any] = {}
    processed_codes: list[str] = []

    def _flush_evidence() -> None:
        evidence_path = Path(args.evidence_json)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_at": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
            "dry_run": not args.yes,
            "limit": args.limit,
            "priority_codes": priority,
            "subject_source": args.subject_source,
            "only_missing": args.only_missing,
            "backfill_targets": len(backfill_targets),
            "sleep_seconds": args.sleep,
            "retries": args.retries,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "skipped_completed": skipped_completed,
            **stats,
        }
        evidence_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    for batch_start in range(0, len(pending), max(1, args.batch_size)):
        batch = pending[batch_start:batch_start + max(1, args.batch_size)]
        labels: list[dict[str, Any]] = []
        batch_derived = 0
        batch_identity_checked = 0
        batch_identity_bad = 0
        batch_no_rows = 0

        for offset, code in enumerate(batch, start=1):
            code_rows: dict[date, dict[str, float | None]] = {}
            recon_rows: dict[date, dict[str, Any]] = {}
            if args.subject_source in ("both", "sina"):
                try:
                    sina_frame = fetch_sina(code, throttle=throttle, retries=args.retries)
                    sina_rows, recon_rows = parse_sina(
                        sina_frame, code, with_all_subjects=(args.subject_source == "sina")
                    )
                except Exception as exc:  # noqa: BLE001 - 逐股隔离
                    stats["failures"].append({"stock_code": code, "stage": "sina", "reason": str(exc)[:200]})
                    stats["stocks_failed"] += 1
                    logger.warning("  %s 新浪失败，跳过该只：%s", code, str(exc)[:120])
                    continue
            else:
                sina_rows = {}  # --subject-source em：不请求新浪，只补 5 个科目

            if recon_target[0] == code and recon_target[1] in recon_rows:
                recon_capture = dict(recon_rows[recon_target[1]], stock_code=code, report_date=str(recon_target[1]))

            # 源内勾稽①：原值 − 累计折旧 − 净值 ≈ 0（只统计源里五件套齐全的行）
            for report_date, values in recon_rows.items():
                if values["gross"] is not None and values["accum"] is not None and values["net"] is not None:
                    batch_identity_checked += 1
                    residual = values["gross"] - values["accum"] - values["net"]
                    if abs(residual) > IDENTITY_TOLERANCE_YUAN:
                        batch_identity_bad += 1
                        # 只记最差一条的代码+期次+数值（不 dump 明细行，AGENTS W3）
                        if abs(residual) > abs(stats["identity_checks"].get("worst_residual") or 0.0):
                            stats["identity_checks"]["worst_residual"] = residual
                            stats["identity_checks"]["worst_key"] = f"{code}@{report_date}"

            for report_date, values in sina_rows.items():
                code_rows.setdefault(report_date, {}).update(values)
                if recon_rows.get(report_date, {}).get("derived_accum"):
                    batch_derived += 1

            # 5 个科目默认走东财（任务书指定源）；`--subject-source sina` 时已在上面同一次新浪请求里取到，
            # 不再发第二个请求（探源 §2 实测两源逐分钱相等）。
            if args.subject_source == "sina":
                stats["em_requests_skipped"] += 1
            else:
                try:
                    em_frame = fetch_em(code, throttle=throttle, retries=args.retries)
                    for report_date, values in parse_em(em_frame, code).items():
                        code_rows.setdefault(report_date, {}).update(values)
                except Exception as exc:  # noqa: BLE001 - 逐股隔离：东财失败不影响已拿到的原值/累计折旧
                    stats["failures"].append({"stock_code": code, "stage": "em", "reason": str(exc)[:200]})
                    stats["stocks_failed"] += 1
                    logger.warning("  %s 东财失败，仅保留新浪两列：%s", code, str(exc)[:120])
            if not code_rows:
                batch_no_rows += 1  # 该股源里一行可用科目都没有（模板无此科目 / 全 NULL）
                continue

            for report_date, values in code_rows.items():
                labels.append({
                    "stock_code": code,
                    "report_date": report_date,
                    **{column: values.get(column) for column in SUBJECT_COLUMNS},
                })
            stats["stocks_processed"] += 1
            processed_codes.append(code)
            stats["columns_derived"]["accumulated_depreciation"] += batch_derived
            batch_derived = 0

            if offset % 10 == 0:
                logger.info("  批次进度 %d/%d（累计写入候选 %d 行）", offset, len(batch), len(labels) + stats["rows_staged"])

        stats["rows_fetched"] += len(labels)
        stats["identity_checks"]["checked"] += batch_identity_checked
        stats["identity_checks"]["residual_over_tolerance"] += batch_identity_bad
        stats["stocks_no_db_rows"] += batch_no_rows

        if not labels:
            logger.info("批次 %d：无可用数据", batch_start // max(1, args.batch_size) + 1)
            save_state(state_path, completed | set(processed_codes), stats)
            completed |= set(processed_codes)
            continue

        # inner join：只保留主表已有的 (stock_code, report_date)
        existing = select_existing_keys(duck, {label["stock_code"] for label in labels})
        kept = [label for label in labels if (label["stock_code"], label["report_date"]) in existing]
        stats["rows_missing_in_db"] += len(labels) - len(kept)

        frame = build_stage_frame(kept)
        nonnull = {column: int(frame[column].notna().sum()) for column in SUBJECT_COLUMNS} if not frame.empty else {}
        for column, count in nonnull.items():
            stats["columns_nonnull"][column] += count
        stats["rows_staged"] += len(frame)
        logger.info(
            "批次 %d：抓取 %d 行 → 主表命中 %d 行（缺期跳过 %d）| 非空 %s",
            batch_start // max(1, args.batch_size) + 1,
            len(labels), len(frame), len(labels) - len(frame),
            {column: nonnull.get(column, 0) for column in SUBJECT_COLUMNS},
        )

        if args.yes and not frame.empty:
            before = count_rows_with_subjects(duck, processed_codes)
            try:
                write_batch(duck, paths.duckdb_path, frame, only_missing=args.only_missing)
            except LockBusyError as exc:
                logger.error("单写者锁被占用，按纪律跳过退出（不排队）：%s", exc)
                save_state(state_path, completed, stats)
                _flush_evidence()
                return 3
            after = count_rows_with_subjects(duck, processed_codes)
            stats["rows_written"] += len(frame)
            stats["batches"].append({
                "batch": batch_start // max(1, args.batch_size) + 1,
                "stocks": len(processed_codes),
                "rows_staged": len(frame),
                "db_rows_with_subject_before": before,
                "db_rows_with_subject_after": after,
            })
            logger.info("批次写入完成：%d 行；含该 7 列非空的行 %d → %d", len(frame), before, after)
        elif not args.yes:
            stats["batches"].append({
                "batch": batch_start // max(1, args.batch_size) + 1,
                "stocks": len(processed_codes),
                "rows_staged": len(frame),
                "db_rows_with_subject_before": count_rows_with_subjects(duck, processed_codes),
                "db_rows_with_subject_after": None,
            })

        completed |= set(processed_codes)
        save_state(state_path, completed, stats)
        if not args.yes:
            logger.info("[DRY RUN] 未写库；加 --yes 执行")
            break  # dry-run 只跑一批

    # 勾稽断言（600519 FY2025）：两条恒等式，残差都要 < 1 元
    if recon_capture:
        db_rows = duck.read_query(
            "SELECT fixed_assets FROM balance_sheet WHERE stock_code = ? AND report_date = ?",
            [recon_capture["stock_code"], recon_target[1]],
        )
        fixed_assets = _clean(db_rows[0]["fixed_assets"]) if db_rows else None
        gross = recon_capture["gross"]
        accum = recon_capture["accum"]
        impairment = recon_capture["impairment"]
        identity_in_source = (
            None if (gross is None or accum is None or recon_capture["net"] is None)
            else gross - accum - recon_capture["net"]
        )
        identity_cross_source = (
            None if (gross is None or accum is None or fixed_assets is None)
            else gross - accum - (impairment or 0.0) - fixed_assets
        )
        naive_residual = (
            None if (gross is None or accum is None or fixed_assets is None) else gross - accum - fixed_assets
        )
        stats["recon"] = {
            **recon_capture,
            "db_fixed_assets": fixed_assets,
            "residual_source_identity": identity_in_source,
            "residual_with_impairment": identity_cross_source,
            "residual_naive": naive_residual,
            "pass_source_identity": identity_in_source is not None and abs(identity_in_source) < IDENTITY_TOLERANCE_YUAN,
            "pass_with_impairment": identity_cross_source is not None and abs(identity_cross_source) < IDENTITY_TOLERANCE_YUAN,
            "pass_naive": naive_residual is not None and abs(naive_residual) < IDENTITY_TOLERANCE_YUAN,
        }
        logger.info(
            "勾稽 %s %s：原值 %s − 累计折旧 %s − 减值 %s − 库内 fixed_assets %s = 残差 %s 元（<1 元通过=%s）",
            recon_capture["stock_code"], recon_capture["report_date"],
            f"{gross:,.2f}" if gross else None,
            f"{accum:,.2f}" if accum else None,
            f"{impairment:,.2f}" if impairment is not None else "0.00",
            f"{fixed_assets:,.2f}" if fixed_assets else None,
            f"{identity_cross_source:,.2f}" if identity_cross_source is not None else None,
            stats["recon"]["pass_with_impairment"],
        )
        if naive_residual is not None and not stats["recon"]["pass_naive"]:
            logger.info(
                "  ⚠️ 朴素断言「原值 − 累计折旧 ≈ fixed_assets」残差 %s 元 = 固定资产减值准备（不是误差，是科目定义）",
                f"{naive_residual:,.2f}",
            )

    stats["db_fingerprint"] = db_fingerprint(duck, processed_codes)
    _flush_evidence()
    logger.info(
        "完成：处理 %d 只 / 失败 %d 只 / 候选 %d 行 / 落库命中 %d 行 / 写入 %d 行 / 耗时 %.1fs",
        stats["stocks_processed"], stats["stocks_failed"], stats["rows_fetched"],
        stats["rows_staged"], stats["rows_written"], time.monotonic() - started,
    )
    logger.info("证据 JSON：%s | 状态文件：%s", args.evidence_json, state_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
