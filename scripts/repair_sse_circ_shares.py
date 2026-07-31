"""Retired unsafe SSE circulating-share repair entry point.

Direct Eastmoney writes lacked a source archive, field audit, total-share unit
proof, and an as-of date. This command intentionally performs no write.
"""

from __future__ import annotations

import json


def repair(*_args, **_kwargs) -> dict[str, str]:
    """Refuse an untraceable circulating-share publication attempt."""
    return {
        "status": "blocked",
        "reason": (
            "No canonical audited ingestion path is configured for SSE circulating "
            "shares. Direct database updates are prohibited."
        ),
    }


def main() -> int:
    print(json.dumps(repair(), ensure_ascii=False, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
