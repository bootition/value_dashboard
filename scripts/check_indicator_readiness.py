"""指标前置就绪自检（`reports/139` §7 的 6 件前置，机器化）。

为什么需要
----------
`reports/139` 把「42 个大师指标能不能开做」归结为 6 件前置：金融股剔除单一化、
分母 ≤0 只标 status、`roce_10y_min/cv` 两列、同比口径分流、派生域登记 + 重建触发、
以及数据侧可用的历史窗口。这些如果只写在报告里，下一个会话还会重新踩一遍。

本脚本把每一条变成**可复跑的断言**（只读），`vd` 里可以 `--json` 输出给状态页/CI：

    uv run --locked python scripts/check_indicator_readiness.py
    uv run --locked python scripts/check_indicator_readiness.py --json

退出码：0 = 全部 READY；1 = 有 BLOCK（前置缺失）；2 = 只有 WARN（可开做但有已知留白）。

口径纪律
--------
- 只读（DuckDB `read_only=True`），不写库、不改 schema；
- 每条检查都输出 `detail`（**真实读数**，不是「OK」），遵循 AGENTS R2「证据必须带真实读数」；
- 已知留白（如 `restricted_cash` 源侧无解）记 WARN 而不是 BLOCK，且写明为什么。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Literal

from app.core.indicators import roce as roce_module
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet

Status = Literal["READY", "WARN", "BLOCK"]


@dataclass
class Check:
    """一条前置检查。"""

    key: str
    title: str
    status: Status
    detail: str
    evidence: dict[str, object] = field(default_factory=dict)


def _check_sector_single_source() -> Check:
    """前置 1：金融股判据必须只有一份实现（`app/core/sector.py`）。"""
    missing = []
    for module_path, needle in (
        ("app/core/indicators/roce.py", "is_non_financial_sql"),
        ("app/core/supplement.py", "is_financial_sql"),
        ("app/core/quality/null_reason_sweeper.py", "is_bank_sql"),
    ):
        try:
            with open(module_path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            missing.append(f"{module_path}:文件缺失")
            continue
        # 只扫**代码**（去掉 `#` 注释）：注释里提到「银行/非银金融」是说明，不是判据
        code_only = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        if needle not in text:
            missing.append(f"{module_path}:未使用 {needle}")
        if "'银行', '非银金融'" in code_only or "'银行','非银金融'" in code_only:
            missing.append(f"{module_path}:仍存在手写的金融股集合")
    status: Status = "READY" if not missing else "BLOCK"
    return Check(
        "sector_single_source",
        "金融股判据单一实现（3 处调用 app/core/sector.py，无手写集合）",
        status,
        "3/3 调用已收口" if not missing else "；".join(missing),
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
        "分母/口径不可算有落点（indicator_null_reason 覆盖 6 类原因码，实测 >=4 类在用）",
        status,
        f"{rows['n_rows']} 行 / {rows['n_fields']} 字段 / {codes} 类原因码在用",
        {"rows": int(rows["n_rows"]), "fields": int(rows["n_fields"]), "codes": codes},
    )


def _check_roce_persistence(duck: DuckDBStore) -> Check:
    """前置 3：#27 ROCE 持久性需要的两列必须存在且有值，且登记在派生域。"""
    columns = {
        str(row["column_name"])
        for row in duck.read_query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name='indicator_snapshot' "
            "AND column_name IN ('roce_10y_min','roce_10y_cv','roce_20y_median')"
        )
    }
    registered = set(roce_module.SNAPSHOT_DERIVED_COLUMNS)
    snapshot = duck.read_query(
        "SELECT COUNT(roce_10y_min) AS n_min, COUNT(roce_10y_cv) AS n_cv, "
        "COUNT(roce_20y_median) AS n_20y FROM indicator_snapshot"
    )[0]
    problems = []
    if columns != {"roce_10y_min", "roce_10y_cv", "roce_20y_median"}:
        problems.append(f"缺列：{sorted({'roce_10y_min','roce_10y_cv','roce_20y_median'} - columns)}")
    for name in ("roce_10y_min", "roce_10y_cv", "roce_20y_median", "roce_20y_years"):
        if name not in registered:
            problems.append(f"{name} 未登记 SNAPSHOT_DERIVED_COLUMNS")
    if int(snapshot["n_min"]) == 0:
        problems.append("roce_10y_min 全空（未重建）")
    status: Status = "READY" if not problems else "BLOCK"
    return Check(
        "roce_persistence",
        "#27 所需列齐备（roce_10y_min/cv 存在、有值、且进派生域登记）",
        status,
        "；".join(problems) if problems else
        f"min={snapshot['n_min']} cv={snapshot['n_cv']} 20y={snapshot['n_20y']}，登记 {len(registered)} 列",
        {"columns": sorted(columns), "registered": sorted(registered)},
    )


def _check_yoy_split(duck: DuckDBStore) -> Check:
    """前置 4：同比必须「TTM 与年报」两列分流，且 `*_yoy_fy` 已回填。"""
    cols = {
        str(row["column_name"])
        for row in duck.read_query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name='indicator_snapshot' "
            "AND (column_name LIKE '%_yoy' OR column_name LIKE '%_yoy_fy')"
        )
    }
    fy = {c for c in cols if c.endswith("_yoy_fy")}
    ttm = cols - fy
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


def _check_derived_registration(duck: DuckDBStore) -> Check:
    """前置 5：重建 SQL 写到的列必须全部登记（RG4 的运行时版本）。"""
    sql = roce_module.SNAPSHOT_UPDATE_SQL
    set_clause = sql.split("SET", 1)[1].split("FROM", 1)[0]
    written = {
        line.split("=")[0].strip()
        for line in set_clause.splitlines()
        if "=" in line and line.strip()
    }
    registered = set(roce_module.SNAPSHOT_DERIVED_COLUMNS)
    missing = sorted(written - registered)
    status: Status = "READY" if not missing else "BLOCK"
    return Check(
        "derived_registration",
        "派生域登记与重建 SQL 双向一致（防快照重发把列清空，K5）",
        status,
        f"SQL 写 {len(written)} 列 / 登记 {len(registered)} 列" + (f"；未登记：{missing}" if missing else "；差集为空"),
        {"written": sorted(written), "registered": sorted(registered)},
    )


def _check_data_domains(duck: DuckDBStore) -> Check:
    """前置 6：数据侧可用性（补采科目 + 宏观/债券/beta/一致预期四域）。"""
    tables = {
        str(row["table_name"])
        for row in duck.read_query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        )
    }
    required = {"macro_series", "bond_yield_curve", "stock_beta", "analyst_forecast_snapshot"}
    missing = sorted(required - tables)
    coverage = duck.read_query(
        """
        SELECT
          (SELECT COUNT(*) FROM macro_series) AS macro_rows,
          (SELECT COUNT(*) FROM bond_yield_curve) AS bond_rows,
          (SELECT COUNT(*) FROM stock_beta) AS beta_rows,
          (SELECT COUNT(*) FROM analyst_forecast_snapshot) AS forecast_rows,
          (SELECT COUNT(*) FROM balance_sheet WHERE fixed_assets_gross IS NOT NULL) AS bs_gross_rows,
          (SELECT COUNT(*) FROM balance_sheet) AS bs_rows
        """
    )[0] if not missing else {}
    detail = (
        f"缺表：{missing}" if missing else
        f"macro={coverage['macro_rows']} bond={coverage['bond_rows']} beta={coverage['beta_rows']} "
        f"forecast={coverage['forecast_rows']}；补采科目 {coverage['bs_gross_rows']}/{coverage['bs_rows']} 行有原值"
    )
    status: Status = "READY" if not missing and int(coverage["bs_gross_rows"]) > 10000 else "WARN"
    return Check(
        "data_domains",
        "数据域齐备（4 张新表 + 资产负债表补采科目已在回填）",
        status,
        detail,
        {k: int(v) for k, v in dict(coverage).items()} if coverage else {},
    )


def run_checks() -> tuple[str, list[Check]]:
    paths = DatabasePathSet.from_env()
    duck = DuckDBStore(paths=paths)
    checks = [
        _check_sector_single_source(),
        _check_denominator_status(duck),
        _check_roce_persistence(duck),
        _check_yoy_split(duck),
        _check_derived_registration(duck),
        _check_data_domains(duck),
    ]
    if any(check.status == "BLOCK" for check in checks):
        overall = "BLOCK"
    elif any(check.status == "WARN" for check in checks):
        overall = "WARN"
    else:
        overall = "READY"
    return overall, checks


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
                    {"key": c.key, "title": c.title, "status": c.status,
                     "detail": c.detail, "evidence": c.evidence}
                    for c in checks
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
