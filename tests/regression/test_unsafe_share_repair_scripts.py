from __future__ import annotations

from scripts.repair_sse_circ_shares import repair as repair_circulating_shares
from scripts.repair_sse_share_capital import repair as repair_total_shares


def test_unsafe_sse_share_repair_scripts_fail_closed() -> None:
    for repair in (repair_total_shares, repair_circulating_shares):
        result = repair()
        assert result["status"] == "blocked"
        assert "canonical" in result["reason"] or "authoritative" in result["reason"]
