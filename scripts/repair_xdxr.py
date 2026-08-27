"""Retired unsafe XDXR repair entry point.

This script previously wrote the xdxr table directly without a source
archive, fetch batch, or field audit. The canonical audited path is
scripts/repair_dividends.py, which re-fetches xdxr via TDX and publishes
business rows + raw_response_archive + fetch_batch + source_audit in one
transaction. This command intentionally performs no write.
"""

from __future__ import annotations

import json


def repair(*_args, **_kwargs) -> dict[str, str]:
    """Refuse an untraceable XDXR publication attempt."""
    return {
        "status": "blocked",
        "reason": (
            "Direct xdxr writes are prohibited. Use the canonical audited path: "
            "python scripts/repair_dividends.py (TDX xdxr upsert with batch, "
            "archive, and field audit in one transaction)."
        ),
    }


def main() -> int:
    print(json.dumps(repair(), ensure_ascii=False, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
