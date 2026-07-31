from __future__ import annotations

import pytest

from app.core.adapters import manager
from app.core.config import Config


def test_legacy_akshare_alias_resolves_and_keeps_fallbacks() -> None:
    priority = manager.build_adapter_priority({"balance_sheet": "akshare"})

    assert priority["balance_sheet"] == ["akshare_eastmoney", "sina", "tdx"]


def test_sina_is_the_default_primary_for_all_three_statements() -> None:
    priority = manager.build_adapter_priority(None)

    for data_type in ("balance_sheet", "income_statement", "cash_flow"):
        assert priority[data_type] == ["sina", "tdx", "akshare_eastmoney"]
        assert "sina" in manager.KNOWN_ADAPTERS


def test_configured_primary_does_not_remove_other_fallbacks() -> None:
    priority = manager.build_adapter_priority({"price_daily": "tdx"})

    assert priority["price_daily"] == ["tdx", "baostock", "tencent", "akshare_eastmoney"]


def test_unknown_adapter_name_is_rejected() -> None:
    with pytest.raises(manager.AdapterConfigurationError, match="unknown_adapter"):
        manager.build_adapter_priority({"balance_sheet": "unknown_adapter"})


def test_legacy_rate_limit_alias_resolves_to_registered_name() -> None:
    rate_limits = manager.build_adapter_rate_limits({"akshare": 2.5})

    assert rate_limits["akshare_eastmoney"] == 2.5


def test_configured_rate_limit_is_applied_to_registered_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Config,
        "_instance",
        Config(
            {
                "adapters": {
                    "primary": {},
                    "rate_limit_interval": {"akshare_eastmoney": 2.5},
                }
            }
        ),
    )
    adapter_manager = manager.AdapterManager()

    adapter = adapter_manager.get_adapter("akshare_eastmoney")

    assert adapter is not None
    assert adapter.rate_limit_interval == 2.5
