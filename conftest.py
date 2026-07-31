"""Root pytest configuration — shields collection from archived legacy tests.

All discovery-scoped decisions are delegated to the pure policy module
``_pytest_policy`` so that the hook remains testable without a live
pytest runtime.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.storage.path_policy import DatabasePathSet, PathIsolationError, VdEnv
from _pytest_policy import is_archived_legacy_test


def pytest_ignore_collect(collection_path: Path, /) -> bool:
    """Ignore any path that lives under ``_legacy/legacy_tests/``.

    This hook is called by pytest for every discovered file and directory
    *before* import.  Returning ``True`` prevents both collection and
    execution of the item.
    """
    return is_archived_legacy_test(collection_path)


def pytest_configure(config: pytest.Config) -> None:
    """Fail before collection unless pytest is explicitly isolated from formal data."""
    if os.environ.get("VD_FORMAL_ACK"):
        raise pytest.UsageError("VD_FORMAL_ACK is forbidden while running pytest")
    try:
        paths = DatabasePathSet.from_env()
    except PathIsolationError as error:
        raise pytest.UsageError(
            "pytest requires the isolated VD_ENV=test profile; use scripts/s1-pytest.ps1"
        ) from error
    if paths.env is not VdEnv.TEST:
        raise pytest.UsageError("pytest requires VD_ENV=test; formal and staging profiles are forbidden")
