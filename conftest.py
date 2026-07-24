"""Root pytest configuration — shields collection from archived legacy tests.

All discovery-scoped decisions are delegated to the pure policy module
``_pytest_policy`` so that the hook remains testable without a live
pytest runtime.
"""

from __future__ import annotations

from pathlib import Path

from _pytest_policy import is_archived_legacy_test


def pytest_ignore_collect(collection_path: Path, /) -> bool:
    """Ignore any path that lives under ``_legacy/legacy_tests/``.

    This hook is called by pytest for every discovered file and directory
    *before* import.  Returning ``True`` prevents both collection and
    execution of the item.
    """
    return is_archived_legacy_test(collection_path)
