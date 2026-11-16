"""筛选域的配置默认值 —— `screening.*` 的唯一读取点。

为什么单独一个模块：这些值以前散落在三处硬编码（`ScreeningEngine.run()`
的参数默认值、`app/web/api/watchlist.py` 的 `base_pool.get(..., 1)`、
前端 `ScreeningPage.vue` 的 reactive 初始值），于是 `config/default.yaml`
里的 `screening.default_*` 改不改都一样——**静默无效的假配置**。
现在 core 统一从这里取，缺省回退值就等于历史硬编码值，行为不变。

纪律：本模块只读配置、不做业务判断；任何异常都回落到缺省值（配置坏了
不该让筛选/解析炸掉）。
"""

from __future__ import annotations

from typing import Any

from app.core.config import Config

_SECTION = "screening"

#: 缺省回退值 —— 与历史硬编码完全一致（改这里等于改产品默认）。
DEFAULT_EXCLUDE_ST = True
DEFAULT_EXCLUDE_SUSPENDED = True
DEFAULT_MIN_LISTING_YEARS = 1
DEFAULT_MAX_CONDITIONS = 20
DEFAULT_MAX_NESTING_LEVELS = 50

_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off", ""})


def _section() -> dict[str, Any]:
    """返回 ``screening`` 配置节；缺失/类型不对/加载失败一律当空节。"""
    try:
        section = Config.current().get_value(_SECTION)
    except Exception:  # 配置坏掉只允许回落到缺省值
        return {}
    return section if isinstance(section, dict) else {}


def _int_setting(key: str, default: int, *, minimum: int) -> int:
    raw = _section().get(key)
    if isinstance(raw, bool) or raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _flag_setting(key: str, default: bool) -> bool:
    raw = _section().get(key)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return default


def exclude_st() -> bool:
    """``screening.default_exclude_st``（缺省 True = 默认剔除 ST/*ST）。"""
    return _flag_setting("default_exclude_st", DEFAULT_EXCLUDE_ST)


def exclude_suspended() -> bool:
    """``screening.default_exclude_suspended``（缺省 True = 默认剔除停牌）。"""
    return _flag_setting("default_exclude_suspended", DEFAULT_EXCLUDE_SUSPENDED)


def min_listing_years() -> int:
    """``screening.default_min_listing_years``（缺省 1 年；0 = 不过滤）。"""
    return _int_setting(
        "default_min_listing_years", DEFAULT_MIN_LISTING_YEARS, minimum=0,
    )


def max_conditions() -> int:
    """``screening.max_conditions``（缺省 20）：单条规则的叶子条件数上限。"""
    return _int_setting("max_conditions", DEFAULT_MAX_CONDITIONS, minimum=1)


def max_nesting_levels() -> int:
    """``screening.max_nesting_levels``（缺省 50）：表达式括号嵌套上限。"""
    return _int_setting(
        "max_nesting_levels", DEFAULT_MAX_NESTING_LEVELS, minimum=1,
    )


def base_pool_defaults() -> dict[str, Any]:
    """基础股票池的默认范围条件（前端勾选项的服务端对应物）。"""
    return {
        "exclude_st": exclude_st(),
        "exclude_suspended": exclude_suspended(),
        "min_listing_years": min_listing_years(),
    }
