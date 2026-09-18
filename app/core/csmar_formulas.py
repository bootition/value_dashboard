"""CSMAR 字段采用裁定（三色灯）读取器。

背景与目的
----------
2026-09-17 对 CSMAR C17 外来数据包做资源化调研时发现，该包与本项目存在
大量「字段名相同但实际口径不同」的情况（实证 2 例，见 `_TRAPS`）：

1. `FI_T10` 的「市值A/B」在 股价×股数 之外**额外加计负债合计**（企业价值口径），
   而本项目的 `total_market_cap` 是不含负债的普通市值；
2. `FAR_Finidx.A300000` 标称「股东权益合计」，实测与「归母权益」100% 相同，
   用它反推股本会把一致率从 98.51% 拉到 42.11%。

因此本项目对 CSMAR 字段一律执行**三色灯**规则：

===========  ==========================================  ==============================
灯            含义                                        处置
===========  ==========================================  ==============================
`green`       公式与本项目完全一致                          可采用，并可作交叉核验
`yellow`      公式接近但存口径差异（科目/时点/分母）        只用不换，须标注差异
`red`         名称相近但含义不同                            绝不采用，只作参考
`none`        CSMAR 无对应字段                              不适用
===========  ==========================================  ==============================

裁定数据保存在 `config/csmar_field_verdict.json`（**仅含本项目的判定与理由，
不含 CSMAR 商业文档原文**，规避版权）。CSMAR 包本体位于 `额外资料/`（gitignore），
仓库内不存在任何 CSMAR 原始数据或文档内容。

用法
----
    from app.core.csmar_formulas import is_adoptable, verdict_for, families

    if is_adoptable("total_market_cap"):   # -> False（red）
        ...

    v = verdict_for("pe_ttm")              # -> Verdict(color='yellow', ...)
    for fam in families():                 # 缺失指标族及其依赖链
        ...
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as dc_field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

Color = Literal["green", "yellow", "red", "none"]

_CONFIG_RELATIVE_PATH = Path("config") / "csmar_field_verdict.json"

# 已知「同名不同义」陷阱（代码内常驻，供静态检查与文档引用）
_TRAPS: dict[str, str] = {
    "market_cap_includes_debt": (
        "CSMAR FI_T10 市值A/B 含负债合计（企业价值口径），"
        "不得用于替换本项目的 total_market_cap"
    ),
    "equity_total_is_parent_only": (
        "FAR_Finidx.A300000 标称『股东权益合计』实为归母权益；"
        "反推股本须改用 FS_Combas.A003000000"
    ),
}


class CsmarVerdictError(RuntimeError):
    """裁定表缺失或损坏。"""


@dataclass(frozen=True)
class Verdict:
    """单个字段的采用裁定。"""

    field: str
    color: Color
    csmar_table: str | None
    csmar_code: str | None
    basis: str
    is_trap: bool = False

    @property
    def adoptable(self) -> bool:
        """只有 green 才允许进主链或被当作等价第三方。"""
        return self.color == "green"

    @property
    def comparable(self) -> bool:
        """yellow/green 可做数值比对（yellow 需按口径差异解读）。"""
        return self.color in {"green", "yellow"}


@dataclass(frozen=True)
class Family:
    """缺失指标族的采用裁定。"""

    key: str
    name: str
    csmar_table: str | None
    field_count: int
    color: Color
    verdict: str
    basis: str
    action: str
    depends_on: tuple[str, ...] = dc_field(default_factory=tuple)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_raw(config_path: Path | None = None) -> dict[str, Any]:
    """读取裁定表原始 JSON。路径可由参数覆盖（测试用）。"""
    path = config_path or (_project_root() / _CONFIG_RELATIVE_PATH)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CsmarVerdictError(f"CSMAR 裁定表缺失: {path}") from error
    except json.JSONDecodeError as error:
        raise CsmarVerdictError(f"CSMAR 裁定表损坏: {path}: {error}") from error


@lru_cache(maxsize=1)
def _index(config_path: str | None = None) -> tuple[dict[str, Verdict], tuple[Family, ...], dict[str, Any]]:
    doc = load_raw(Path(config_path) if config_path else None)
    fields: dict[str, Verdict] = {}
    for item in doc.get("fields", []):
        name = item.get("field")
        if not name:
            continue
        fields[name] = Verdict(
            field=name,
            color=item.get("color", "none"),
            csmar_table=item.get("csmar_table"),
            csmar_code=item.get("csmar_code"),
            basis=item.get("basis", ""),
            is_trap=bool(item.get("trap")),
        )
    fams = tuple(
        Family(
            key=item.get("key", ""),
            name=item.get("name", ""),
            csmar_table=item.get("csmar_table"),
            field_count=int(item.get("field_count", 0)),
            color=item.get("color", "none"),
            verdict=item.get("verdict", ""),
            basis=item.get("basis", ""),
            action=item.get("action", ""),
            depends_on=tuple(item.get("depends_on", ())),
        )
        for item in doc.get("families", [])
    )
    return fields, fams, doc


def verdict_for(field_name: str) -> Verdict:
    """查询单个字段的三色灯裁定。

    未登记的字段一律按 `red` 处理（fail-closed）：引入未裁定字段必须显式登记，
    避免「名字看起来一样就直接用」。
    """
    fields, _, _ = _index()
    found = fields.get(field_name)
    if found is not None:
        return found
    return Verdict(
        field=field_name,
        color="red",
        csmar_table=None,
        csmar_code=None,
        basis="未登记裁定；按 fail-closed 视为不可采用，需先补 config/csmar_field_verdict.json",
    )


def is_adoptable(field_name: str) -> bool:
    """该字段是否允许进主链（仅 green 为真）。"""
    return verdict_for(field_name).adoptable


def families() -> tuple[Family, ...]:
    """全部缺失指标族的裁定（含依赖链）。"""
    _, fams, _ = _index()
    return fams


def traps() -> dict[str, str]:
    """已知「同名不同义」陷阱。"""
    return dict(_TRAPS)


def adoption_policy() -> dict[str, Any]:
    """采用政策原文（主链限制、lineage 要求、对外分发禁止项）。"""
    _, _, doc = _index()
    return dict(doc.get("adoption_policy", {}))


def reset_cache() -> None:
    """清空缓存（测试用）。"""
    _index.cache_clear()
