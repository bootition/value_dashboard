"""金融股判定 —— 全仓唯一实现（单一真相源）。

背景（`reports/139` §5.1 / `reports/131` §11.3）：
这个判定在仓库里曾有三份手写实现，集合还不一致，导致同一只股票在不同指标里
「是/不是金融股」结论不同——这是口径漂移的典型来源：

| 旧位置 | 旧写法 | 问题 |
|---|---|---|
| `app/core/indicators/roce.py:183` | `COALESCE(meta.sw_level1,'') NOT IN ('银行','非银金融')` | 已是 NULL 安全，但没有 csrc 兜底 |
| `app/core/supplement.py:507` | `m.sw_level1 IN ('银行','非银金融') OR (m.sw_level1 IS NULL AND m.csrc_l1='金融业')` | NULL 安全 + 有兜底，但**只用在补采**，不是通用判据 |
| `app/core/quality/null_reason_sweeper.py:752` | `COALESCE(sw_level1,'') IN ('银行','非银金融','保险') OR csrc_l1 LIKE '%金融%' OR name LIKE '%银行%'…` | `sw_level1` 里**没有 '保险' 这个取值**（保险是 `sw_level2`）；又用股票名兜底，会把同花顺/指南针类软件股误判 |

**为什么以申万一级为准**：`csrc_l1='金融业'` 里混着同花顺、指南针、大智慧等
软件公司（`reports/131` 陷阱 8）；而 `sw_level1` 是干净的行业分类，实测
`银行` 42 只 / `非银金融` 79 只 / `NULL` 751 只（其中仅 52 只仍在市）。

**NULL 的处置（最容易踩的坑）**：`sw_level1 IS NULL` 的行**不能**被
`NOT IN (...)` 静默丢掉（三值逻辑），否则「非金融」筛选会漏掉 751 行。
本模块统一出口：
- :func:`is_financial_sql`  → `TRUE` 表示金融股（NULL 视为非金融，除非 csrc 兜底命中）
- :func:`is_non_financial_sql` → `TRUE` 表示非金融股（**NULL 行保留**）

用法（SQL 片段，`alias` 是带 `sw_level1`/`csrc_l1` 的表别名，通常是 `stock_meta m`）::

    from app.core.sector import is_non_financial_sql
    ... WHERE {is_non_financial_sql("m")}

**新增指标一律用这两个函数，禁止再手写 `sw_level1 NOT IN (...)`。**
"""

from __future__ import annotations

#: 申万一级里属于「金融」的两个分类（2021 版申万分类口径）。
FINANCIAL_SW_LEVEL1: tuple[str, ...] = ("银行", "非银金融")

#: 申万一级缺失时的兜底：证监会门类「金融业」。只做兜底，不能当主判据。
FINANCIAL_CSRC_L1: str = "金融业"


def _alias(alias: str | None) -> str:
    """把 `stock_meta m` / `m` / `None` 统一成可以拼 SQL 的前缀。"""
    if not alias:
        return ""
    cleaned = alias.strip()
    if not cleaned:
        return ""
    return cleaned if cleaned.endswith(".") else f"{cleaned}."


def is_financial_sql(alias: str | None = None) -> str:
    """布尔表达式：`TRUE` = 金融股（银行/非银金融，含 csrc 兜底）。

    NULL 安全：`sw_level1` 为 NULL 且 `csrc_l1` 不是金融业时返回 `FALSE`
    （即 NULL 行业标签的股票**不会被当成金融股**）。
    """
    p = _alias(alias)
    levels = ", ".join(f"'{v}'" for v in FINANCIAL_SW_LEVEL1)
    return (
        f"(COALESCE({p}sw_level1, '') IN ({levels})"
        f" OR (COALESCE({p}sw_level1, '') = '' AND COALESCE({p}csrc_l1, '') = '{FINANCIAL_CSRC_L1}'))"
    )


def is_non_financial_sql(alias: str | None = None) -> str:
    """布尔表达式：`TRUE` = 非金融股（**行业标签为 NULL 的行会被保留**）。

    这是指标筛选应该用的那个：`WHERE {is_non_financial_sql('m')}`。
    """
    return f"(NOT {is_financial_sql(alias)})"


def is_bank_sql(alias: str | None = None) -> str:
    """布尔表达式：`TRUE` = 银行（用于**只有银行才有**的科目，如资本充足率/不良率）。

    为什么不能复用 :func:`is_financial_sql`：券商与保险同样没有「资本充足率/不良贷款率」，
    用「非金融 → 不适用」会把 79 只非银金融的 472 个空单元格判成"未分类"
    （`reports/139` 第二轮验收 §2 的 P2）。判据与 `null_reason_sweeper` 既有的
    `bank_insurer_expr` 保持一致的写法（`sw_level1` + 股票名兜底）。
    """
    p = _alias(alias)
    return (
        f"(COALESCE({p}sw_level1, '') = '银行'"
        f" OR COALESCE({p}name, '') LIKE '%银行%')"
    )


def is_financial_sw_level1(sw_level1: str | None) -> bool:
    """Python 侧判定（只用于读 `stock_meta.sw_level1` 的场景，不含 csrc 兜底）。"""
    return (sw_level1 or "").strip() in FINANCIAL_SW_LEVEL1


__all__ = [
    "FINANCIAL_CSRC_L1",
    "FINANCIAL_SW_LEVEL1",
    "is_bank_sql",
    "is_financial_sql",
    "is_financial_sw_level1",
    "is_non_financial_sql",
]
