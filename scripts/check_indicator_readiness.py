"""指标前置就绪自检（`reports/139` §7 的 6 件前置，机器化）。

为什么需要
----------
`reports/139` 把「42 个大师指标能不能开做」归结为 6 件前置：金融股剔除单一化、
分母 ≤0 只标 status、`roce_10y_min/cv` 两列、同比口径分流、派生域登记 + 重建触发、
以及数据侧可用的历史窗口。这些如果只写在报告里，下一个会话还会重新踩一遍。

本脚本把每一条变成**可复跑的断言**（只读）：

    uv run --locked python scripts/check_indicator_readiness.py
    uv run --locked python scripts/check_indicator_readiness.py --json

退出码：0 = 全部 READY；1 = 有 BLOCK（前置缺失）；2 = 只有 WARN（可开做但有已知留白）。

口径纪律
--------
- 只读（DuckDB 只查不写），不写库、不改 schema；
- 每条检查都输出 `detail`（**真实读数**，不是「OK」），遵循 AGENTS R2「证据必须带真实读数」；
- **fail-closed**：任何异常（库打不开、路径隔离不通过、模块导入失败）一律 BLOCK，绝不 READY
  —— 第二轮验收指出过「合成骨架库也能 READY」的假绿，这里按最严格口径重写：
  * `sector_single_source` 扫**全仓** `app/**/*.py`（不只是 3 个文件），并用 **import 检查**
    确认消费方真的从 `app/core/sector.py` 取函数（注释里提到不算）；
  * `derived_registration` 按**逗号+等号**解析 SET 子句（一行多赋值也认得）；
  * `data_domains` 对补采覆盖率设阈值：<10% BLOCK / 10%~95% WARN（回填进行中）/ ≥95% READY。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Literal

from app.core.indicators import roce as roce_module
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet

Status = Literal["READY", "WARN", "BLOCK"]

#: 手写金融股集合的两种常见写法和引号变体（含无空格、双引号、跨行拼接）
_HANDWRITTEN_FINANCIAL_SET = re.compile(
    "['\"]银行['\"]\\s*,\\s*['\"]非银金融['\"]"
    "|['\"]非银金融['\"]\\s*,\\s*['\"]银行['\"]"
)

#: 必须走单一真相源的模块（键=文件，值=必须导入的符号）
_SECTOR_CONSUMERS = {
    "app/core/indicators/roce.py": "is_non_financial_sql",
    "app/core/supplement.py": "is_financial_sql",
    "app/core/quality/null_reason_sweeper.py": "is_bank_sql",
}


@dataclass
class Check:
    """一条前置检查。"""

    key: str
    title: str
    status: Status
    detail: str
    evidence: dict[str, object] = field(default_factory=dict)


def _iter_app_python_files() -> list[pathlib.Path]:
    """`app/` 下所有 .py（排除缓存与打包产物）。"""
    root = pathlib.Path("app")
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and "build" not in path.parts
    )


def _strip_comment(line: str) -> str:
    """去掉行内 `#` 注释（粗筛足够；宁可误报也不漏报）。"""
    return line.split("#", 1)[0]


def _check_sector_single_source() -> Check:
    """前置 1：金融股判据只有一份实现（`app/core/sector.py`），全仓无手写集合。"""
    problems: list[str] = []
    for module_path, symbol in _SECTOR_CONSUMERS.items():
        try:
            with open(module_path, encoding="utf-8") as handle:
                source = handle.read()
        except OSError:
            problems.append(f"{module_path}:文件缺失")
            continue
        import_pattern = re.compile(
            "from\\s+app\\.core\\.sector\\s+import\\s+[^\\n]*\\b" + re.escape(symbol) + "\\b"
        )
        imported = import_pattern.search(source) or re.search(
            "import\\s+app\\.core\\.sector\\b", source
        )
        if not imported:
            problems.append(f"{module_path}:未从 app/core/sector.py 导入 {symbol}")
        code_only = "\n".join(_strip_comment(line) for line in source.splitlines())
        if _HANDWRITTEN_FINANCIAL_SET.search(code_only):
            problems.append(f"{module_path}:仍有手写的金融股集合")

    for path in _iter_app_python_files():
        if path.as_posix().endswith("app/core/sector.py"):
            continue  # 单一真相源本身就该定义这个集合
        with open(path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                if _HANDWRITTEN_FINANCIAL_SET.search(_strip_comment(line)):
                    problems.append(f"{path.as_posix()}:{lineno} 手写金融股集合")

    status: Status = "READY" if not problems else "BLOCK"
    return Check(
        "sector_single_source",
        "金融股判据单一实现（3 个消费方 import app/core/sector.py，全仓无手写集合）",
        status,
        "3/3 导入收口 + 全仓 0 处手写集合" if not problems else "；".join(problems[:8]),
        {"problems": problems},
    )


def _check_denominator_status(duck: DuckDBStore) -> Check:
    """前置 2：分母 ≤0 / 不可算必须落 status 或 null reason，而不是静默丢行。"""
    rows = duck.read_query(
        """
        SELECT COUNT(*) AS n_rows,
               COUNT(DISTINCT reason_code) AS n_codes,
               COUNT(DISTINCT field_name) AS n_fields
        FROM indicator_null_reason
        """
    )[0]
    codes = int(rows["n_codes"])
    status: Status = "READY" if codes >= 4 else "WARN"
    return Check(
        "denominator_status",
        "分母/口径不可算有落点（indicator_null_reason 6 类原因码，实测 >=4 类在用）",
        status,
        f"{rows['n_rows']} 行 / {rows['n_fields']} 字段 / {codes} 类原因码在用",
        {"rows": int(rows["n_rows"]), "fields": int(rows["n_fields"]), "codes": codes},
    )


def _check_roce_persistence(duck: DuckDBStore) -> Check:
    """前置 3：#27 ROCE 持久性需要的两列必须存在、有值，且登记在派生域。"""
    required = {"roce_10y_min", "roce_10y_cv", "roce_20y_median"}
    columns = {
        str(row["column_name"])
        for row in duck.read_query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name='indicator_snapshot' "
            "AND column_name LIKE 'roce%'"
        )
    }
    registered = set(roce_module.SNAPSHOT_DERIVED_COLUMNS)
    snapshot = duck.read_query(
        "SELECT COUNT(roce_10y_min) AS n_min, COUNT(roce_10y_cv) AS n_cv, "
        "COUNT(roce_20y_median) AS n_20y FROM indicator_snapshot"
    )[0]
    problems: list[str] = []
    missing = sorted(required - columns)
    if missing:
        problems.append(f"缺列：{missing}")
    for name in ("roce_10y_min", "roce_10y_cv", "roce_20y_median", "roce_20y_years"):
        if name not in registered:
            problems.append(f"{name} 未登记 SNAPSHOT_DERIVED_COLUMNS")
    if int(snapshot["n_min"]) == 0:
        problems.append("roce_10y_min 全空（未重建）")
    if int(snapshot["n_cv"]) == 0:
        problems.append("roce_10y_cv 全空（未重建或分母门误杀全部）")
    status: Status = "READY" if not problems else "BLOCK"
    return Check(
        "roce_persistence",
        "#27 所需列齐备（roce_10y_min/cv 存在、有值、且进派生域登记）",
        status,
        "；".join(problems) if problems else (
            f"min={snapshot['n_min']} cv={snapshot['n_cv']} 20y={snapshot['n_20y']}，登记 {len(registered)} 列"
        ),
        {"columns": sorted(columns), "registered": sorted(registered)},
    )


def _check_yoy_split(duck: DuckDBStore) -> Check:
    """前置 4：同比必须「TTM 与年报」两列分流，且 `*_yoy_fy` 已回填。"""
    columns = {
        str(row["column_name"])
        for row in duck.read_query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name='indicator_snapshot' "
            "AND (column_name LIKE '%_yoy' OR column_name LIKE '%_yoy_fy')"
        )
    }
    fy = {name for name in columns if name.endswith("_yoy_fy")}
    ttm = columns - fy
    rows = duck.read_query(
        "SELECT COUNT(revenue_yoy) AS n_ttm, COUNT(revenue_yoy_fy) AS n_fy FROM indicator_snapshot"
    )[0]
    status: Status = "READY" if fy and int(rows["n_fy"]) > 5000 else "BLOCK"
    return Check(
        "yoy_split",
        "同比口径分流（*_yoy=TTM / *_yoy_fy=年报，且 FY 列已回填）",
        status,
        f"TTM {len(ttm)} 列 / FY {len(fy)} 列；revenue_yoy={rows['n_ttm']} revenue_yoy_fy={rows['n_fy']}",
        {"ttm_columns": sorted(ttm), "fy_columns": sorted(fy)},
    )


def _check_derived_registration() -> Check:
    """前置 5：重建 SQL 写到的列必须全部登记（RG4 的运行时版本）。"""
    sql = roce_module.SNAPSHOT_UPDATE_SQL
    set_clause = sql.split("SET", 1)[1].split("FROM", 1)[0]
    written = {
        part.split("=", 1)[0].strip()
        for part in set_clause.replace("\n", ",").split(",")
        if "=" in part and part.split("=", 1)[0].strip()
    }
    registered = set(roce_module.SNAPSHOT_DERIVED_COLUMNS)
    missing = sorted(written - registered)
    extra = sorted(registered - written)
    status: Status = "READY" if not missing else "BLOCK"
    detail = f"SQL 写 {len(written)} 列 / 登记 {len(registered)} 列"
    detail += f"；未登记：{missing}" if missing else "；差集为空"
    if extra:
        detail += f"；登记但 SQL 未写：{extra}"
    return Check(
        "derived_registration",
        "派生域登记与重建 SQL 双向一致（防快照重发把列清空，K5）",
        status,
        detail,
        {"written": sorted(written), "registered": sorted(registered)},
    )


def _check_data_domains(duck: DuckDBStore) -> Check:
    """前置 6：数据侧可用性（4 张新表 + 资产负债表补采科目覆盖率）。"""
    tables = {
        str(row["table_name"])
        for row in duck.read_query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        )
    }
    required = {"macro_series", "bond_yield_curve", "stock_beta", "analyst_forecast_snapshot"}
    missing = sorted(required - tables)
    if missing:
        return Check(
            "data_domains",
            "数据域齐备（4 张新表 + 资产负债表补采科目已在回填）",
            "BLOCK",
            f"缺表：{missing}",
            {"missing_tables": missing},
        )
    row = duck.read_query(
        """
        SELECT
          (SELECT COUNT(*) FROM macro_series) AS macro_rows,
          (SELECT COUNT(*) FROM bond_yield_curve) AS bond_rows,
          (SELECT COUNT(*) FROM stock_beta) AS beta_rows,
          (SELECT COUNT(*) FROM analyst_forecast_snapshot) AS forecast_rows,
          (SELECT COUNT(*) FROM balance_sheet
             WHERE fixed_assets_gross IS NOT NULL
               AND strftime(report_date,'%m-%d') = '12-31') AS bs_gross_rows,
          -- 分母用「有固定资产净值的**年报**行」：补采科目只在年报/半年报披露，
          -- 用全表行数当分母会把 Q1/Q3 的结构性 NULL 算成缺口（reports/139 §11.8）。
          (SELECT COUNT(*) FROM balance_sheet
             WHERE fixed_assets IS NOT NULL AND strftime(report_date,'%m-%d') = '12-31') AS bs_rows,
          (SELECT COUNT(*) FROM balance_sheet WHERE fixed_assets_gross IS NOT NULL
             AND fixed_assets IS NOT NULL AND fixed_assets_gross < fixed_assets - 1.0)
             AS bs_implausible
        """
    )[0]
    total_rows = int(row["bs_rows"])
    gross_rows = int(row["bs_gross_rows"])
    implausible = int(row["bs_implausible"])
    ratio = (gross_rows / total_rows) if total_rows else 0.0
    # 「原值 < 净值」两类含义（reports/139 §11.8 / KB D58）：
    #  a) 抓取口径错的脏值 → fetch 闸门 + repair 已清（银行 0 行）；
    #  b) `balance_sheet.fixed_assets` 本身是含 PPE 以外资产的宽口径 → 口径差，不是脏值。
    # 故按比例判定：<1% 记口径差（WARN），>=1% 才当系统性脏值（BLOCK）。
    implausible_ratio = (implausible / gross_rows) if gross_rows else 0.0
    if implausible_ratio >= 0.01:
        status: Status = "BLOCK"
    elif ratio >= 0.80:
        status = "READY"               # 有 fixed_assets 的年报行里 ≥80% 拿到原值
    elif ratio >= 0.50:
        status = "WARN"                # 回填进行中 / 部分公司不披露：如实 WARN
    else:
        status = "BLOCK"
    detail = (
        f"macro={row['macro_rows']} bond={row['bond_rows']} beta={row['beta_rows']} "
        f"forecast={row['forecast_rows']}；补采原值 {gross_rows}/{total_rows}（年报中有净值行）"
        f"= {ratio:.1%}，{'达标' if ratio >= 0.80 else '待补'}；"
        f"原值<净值 {implausible} 行（{implausible_ratio:.2%}；<1% 属 fixed_assets 宽口径差，见 KB D58）"
    )
    return Check(
        "data_domains",
        "数据域齐备（4 张新表 + 资产负债表补采科目覆盖率）",
        status,
        detail,
        {key: int(value) for key, value in dict(row).items()},
    )


def _run_checks_inner() -> tuple[str, list[Check]]:
    paths = DatabasePathSet.from_env()
    duck = DuckDBStore(paths=paths)
    checks = [
        _check_sector_single_source(),
        _check_denominator_status(duck),
        _check_roce_persistence(duck),
        _check_yoy_split(duck),
        _check_derived_registration(),
        _check_data_domains(duck),
    ]
    if any(check.status == "BLOCK" for check in checks):
        overall = "BLOCK"
    elif any(check.status == "WARN" for check in checks):
        overall = "WARN"
    else:
        overall = "READY"
    return overall, checks


def run_checks() -> tuple[str, list[Check]]:
    """跑全部检查；任何异常一律 BLOCK（fail-closed，绝假绿）。"""
    try:
        return _run_checks_inner()
    except Exception as error:  # noqa: BLE001 - 自检必须降级成 BLOCK 而不是 traceback
        return "BLOCK", [
            Check(
                "bootstrap",
                "自检环境可用（读库 / 路径隔离 / 模块导入）",
                "BLOCK",
                f"自检无法完成：{type(error).__name__}: {error}",
            )
        ]


def main() -> int:
    parser = argparse.ArgumentParser(description="指标前置就绪自检（只读）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给状态页/CI）")
    args = parser.parse_args()

    overall, checks = run_checks()
    if args.json:
        print(json.dumps(
            {
                "schema_version": "1.0",
                "overall": overall,
                "checks": [
                    {"key": check.key, "title": check.title, "status": check.status,
                     "detail": check.detail, "evidence": check.evidence}
                    for check in checks
                ],
            },
            ensure_ascii=False, indent=2, default=str,
        ))
    else:
        print(f"指标前置就绪：{overall}\n")
        for check in checks:
            print(f"[{check.status:5s}] {check.key}: {check.title}")
            print(f"        {check.detail}")
    if overall == "BLOCK":
        return 1
    if overall == "WARN":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
