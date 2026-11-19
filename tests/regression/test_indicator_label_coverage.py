"""指标中文名完整性守卫（PRD §12.1）。

为什么需要这个测试
------------------
用户已经**两次**看到纯英文指标出现在下拉里：

1. `dividend_financing_ratio (DSL v2)` —— 后端 label 拼英文、前端 DSL 分支只读 label。
2. `revenue_cagr(%)` / `div_yield_spread(%)` —— 我把 15 个参数化字段收成基名后，
   前端 `FIELD_LABELS` 里只有带后缀的 `revenue_cagr3/5`，**基名没中文名**。

两次都是同一个错：**新增字段没补中文名**。
之前有过一个「基础字段中文覆盖 100%」的检查，但它只扫了 `SNAPSHOT_COLUMNS ∪ NORMALIZED_FIELDS`，
参数基名（`revenue_cagr` 这类）**不是真实列**，所以扫不到 —— 守卫本身有盲区。

本测试的做法：把「API 可能下发的所有指标名」全量枚举出来，
逐个用前端真实的 `fieldDisplayName()` 解析，断言**必须含中文**。

覆盖来源：
- `SNAPSHOT_COLUMNS` / `NORMALIZED_FIELDS`（基础 + 标准化财务）
- `STAT_FIELDS`（时间序列统计域）
- `PARAM_FAMILIES` 的**基名**（参数化字段，reports/143 §2）
- `RANK_MODES` 的中文标签（排名口径下拉）
- DSL 复合指标（`_dsl_indicator_label`）
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FORMAT_TS = REPO / "frontend" / "src" / "utils" / "screening-format.ts"


def _cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _all_indicator_names() -> list[str]:
    """API 可能下发的全部指标名（与 `/api/screening/indicators` 的 append 站点一一对齐）。

    2026-11-16 审查发现前一版有盲区：漏了 `EXTENDED_SCREENING_READY`（55 个）
    与 `METADATA_COLUMNS`（结果表头列），且 `STAT_FIELDS` 整段被 `continue` 跳过。
    现在按 append 站点逐个补全：

    | 站点 | 来源 |
    |---|---|
    | 1 | SNAPSHOT_COLUMNS（去参数后缀） |
    | 2 | NORMALIZED_FIELDS（去参数后缀） |
    | 3 | PARAM_FAMILIES 基名 |
    | 4 | EXTENDED_SCREENING_READY |
    | 5 | STAT_FIELDS |
    | 6 | METADATA_COLUMNS（结果表头，用户同样看得到） |
    """
    from app.core.screening.engine import (
        EXTENDED_SCREENING_READY,
        METADATA_COLUMNS,
        NORMALIZED_FIELDS,
        PARAM_FAMILIES,
        SNAPSHOT_COLUMNS,
        STAT_FIELDS,
        param_field_names,
    )

    hidden = param_field_names()
    names: set[str] = set()
    names |= {c for c in SNAPSHOT_COLUMNS if c not in hidden}
    names |= {c for c in NORMALIZED_FIELDS if c not in hidden}
    names |= set(STAT_FIELDS)
    names |= set(EXTENDED_SCREENING_READY)
    names |= set(METADATA_COLUMNS)
    # 参数化字段的**基名**入口（不是真实列，但会出现在下拉里）
    for spec in PARAM_FAMILIES.values():
        names |= set(spec["bases"])
    return sorted(names)




def _label_map_from_source() -> dict[str, str]:
    """从 `screening-format.ts` 抽出三张映射表（FIELD_LABELS 与三张报表明细表）。"""
    text = FORMAT_TS.read_text(encoding="utf-8")
    labels: dict[str, str] = {}

    def _block(name: str) -> str:
        matched = re.search(name + r"[^=]*=\s*\{(.*?)\n\}", text, re.S)
        return matched.group(1) if matched else ""

    # FIELD_LABELS：`key: '中文'`
    for key, value in re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*'([^']*)'", _block("FIELD_LABELS"), re.M):
        labels[key] = value

    # 报表字段：`balance.total_assets` → 「资产负债表 · 总资产」
    for table, block_name in (
        ("balance", "BALANCE_FIELD_LABELS"),
        ("income", "INCOME_FIELD_LABELS"),
        ("cashflow", "CASHFLOW_FIELD_LABELS"),
    ):
        for key, value in re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*'([^']*)'", _block(block_name), re.M):
            labels[f"{table}.{key}"] = value
    return labels


def test_no_indicator_name_is_english_only() -> None:
    """**核心守卫**：任何会被下发的指标名都必须能解析出中文。

    失败时错误信息直接告诉你该往 `FIELD_LABELS` 里加哪几行。
    """
    from app.core.screening.engine import STAT_METRICS

    labels = _label_map_from_source()
    missing: list[str] = []

    stat_metric_labels = _stat_metric_labels_from_source()
    for name in _all_indicator_names():
        # 统计域靠模板拼中文（STAT_METRIC_LABELS + 窗口 + 方法）。
        # **不能整段跳过**：2026-11-16 审查实测，删掉 STAT_METRIC_LABELS 里的
        # `spread_10y` 后界面会变成「spread_10y · 10年 · 历史分位」，而旧版测试照样通过。
        if name in STAT_FIELD_NAMES_CACHE():
            metric = name.split("_stat_")[0]
            if metric in STAT_METRICS and _cjk(stat_metric_labels.get(metric, "")):
                continue
            missing.append(name)
            continue
        label = labels.get(name)
        if label is None or not _cjk(label):
            missing.append(name)

    assert not missing, (
        "以下指标名在 frontend/src/utils/screening-format.ts 里没有中文名，"
        "界面会显示纯英文（PRD §12.1 明确禁止）:\n  "
        + "\n  ".join(missing)
        + "\n\n修法：在 FIELD_LABELS（或对应的 *_FIELD_LABELS）里补中文名。"
    )


def _stat_metric_labels_from_source() -> dict[str, str]:
    """抽前端 `STAT_METRIC_LABELS`（统计域指标名的中文模板）。"""
    text = FORMAT_TS.read_text(encoding="utf-8")
    matched = re.search("STAT_METRIC_LABELS[^=]*=\s*\{(.*?)\n\}", text, re.S)
    block = matched.group(1) if matched else ""
    return dict(re.findall("^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*'([^']*)'", block, re.M))


_STAT_NAMES: set[str] | None = None


def STAT_FIELD_NAMES_CACHE() -> set[str]:  # noqa: N802 - 保持常量语义
    global _STAT_NAMES
    if _STAT_NAMES is None:
        from app.core.screening.engine import STAT_FIELDS

        _STAT_NAMES = set(STAT_FIELDS)
    return _STAT_NAMES


def test_param_base_names_have_labels() -> None:
    """参数化基名单独再钉一遍（这是漏过一次的具体位置）。"""
    from app.core.screening.engine import PARAM_FAMILIES

    labels = _label_map_from_source()
    bases = [b for spec in PARAM_FAMILIES.values() for b in spec["bases"]]
    missing = [b for b in bases if not _cjk(labels.get(b, ""))]
    assert not missing, f"参数化基名缺中文名：{missing}"


def test_param_spec_options_are_renderable() -> None:
    """参数下拉的每个选项都要能显示（字符串化后非空）。"""
    from app.core.screening.engine import PARAM_FAMILIES

    for kind, spec in PARAM_FAMILIES.items():
        assert spec["options"], f"{kind} 没有可选值"
        for value in spec["options"]:
            assert str(value).strip(), f"{kind} 存在空选项"


def test_rank_mode_labels_are_chinese() -> None:
    """排名口径下拉的 6 个选项必须全中文（PRD §12.1）。"""
    from app.core.screening.engine import RANK_MODE_LABELS, RANK_MODES

    assert set(RANK_MODES) == set(RANK_MODE_LABELS)
    for mode, label in RANK_MODE_LABELS.items():
        assert _cjk(label), f"排名口径 {mode} 的标签没有中文：{label}"
        assert label != mode, f"排名口径 {mode} 直接用了英文标识当标签"


def test_dsl_label_never_fully_english() -> None:
    """复合指标标签必须含中文（无中文名时加「复合指标」前缀）。"""
    from app.web.api.screening import _dsl_indicator_label

    for name in ("dividend_financing_ratio", "totally_unknown_factor"):
        label = _dsl_indicator_label({"name": name, "version": 3})
        assert _cjk(label), f"复合指标标签无中文：{label}"
