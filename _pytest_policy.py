"""Pure policy: classify pytest collection paths as archived legacy tests.

This module owns zero imports of application code, zero database access,
and zero I/O beyond pure path manipulation.  It is safe to import during
pytest collection / configuration before any app-layer initialisation.
"""

from __future__ import annotations

from pathlib import Path

_ARCHIVED_DIR_NAME = "legacy_tests"
_ARCHIVED_ROOT = Path(__file__).resolve().parent / "_legacy" / _ARCHIVED_DIR_NAME


def is_archived_legacy_test(path: Path | str) -> bool:
    """Return True when *path* is a descendant of ``_legacy/legacy_tests/``.

    The check uses strict prefix-equality on ``Path.parents`` so that only
    paths physically nested under the archived directory trigger a match.
    Substring matching is deliberately avoided.
    """
    resolved = Path(path).resolve()
    return _ARCHIVED_ROOT in resolved.parents or resolved == _ARCHIVED_ROOT


def archived_root() -> Path:
    """Return the canonical legacy-tests root (for assertions / test use)."""
    return _ARCHIVED_ROOT
