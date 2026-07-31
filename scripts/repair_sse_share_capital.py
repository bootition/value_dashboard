"""Retired unsafe SSE share-capital repair entry point.

``paid_in_capital`` has no proven share-unit or as-of-date contract for this
operation. Publishing it into ``stock_meta.total_shares`` previously mixed
wan-share and share units. This command intentionally performs no write.
"""

from __future__ import annotations

import json


def repair(*_args, **_kwargs) -> dict[str, str]:
    """Refuse an untraceable share-capital publication attempt."""
    return {
        "status": "blocked",
        "reason": (
            "No authoritative per-stock source with unit, as-of date, raw payload, "
            "fetch batch, and field audit is configured."
        ),
    }


def main() -> int:
    print(json.dumps(repair(), ensure_ascii=False, indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
