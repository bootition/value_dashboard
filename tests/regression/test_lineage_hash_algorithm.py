"""事故出处：reports/117 / 2026-11-12。

2026-11-12 契约 §3 扩展：摘要合法性按"算法"判定，不按长度一刀切。

真实缺陷（Lead 停库窗口直读正式库确认）：

    SELECT field_name, LENGTH(raw_response_hash), COUNT(*) FROM source_audit
    WHERE LENGTH(raw_response_hash) != 64 GROUP BY 1,2;
    → cf_from_operating_indirect | 32 | 433724
    → deducted_net_profit        | 32 | 242548   （合计 676,272，32 位 MD5 十六进制）

这些行来自 2026-09-19~20 的历史回填战役（CSMAR 与东财适配器都有），是有价值
的溯源记录，不是坏数据。旧口径 ``LENGTH(raw_response_hash) != 64`` 把整批判成
``LINEAGE_INVALID``，一直命中快照阻断白名单 → 详情页数值被遮蔽为 null、筛选 409
（与股本门禁并列的第二层阻断）。

**判据（2026-11-12 二次裁决后收敛为一条）**：现行溯源约定是 64 位内容寻址
SHA256；**32 位十六进制字节串本身就是 MD5 时代的摘要形态，与哪个适配器写的
无关**。因此：

* ``invalid_hash_rows``           = 长度 ≠ 64 且非「32 位 hex（MD5 形态）」；
* ``legacy_hash_rows``            = 32 位 hex（MD5 形态），无论 adapter；
* ``batch_archive_gap_rows``      = 缺归档 **且非** legacy 摘要形态（真断裂，阻断）；
* ``legacy_archive_gap_batches``  = 缺归档 **且是** legacy 摘要形态（如实披露，不阻断）。

首版曾用 ``adapter_version LIKE 'csmar-%'`` 白名单保守兜底，被正式库证伪：同一
战役里还有 6 个东财批次（``eastmoney-f10-xjllb/1`` / ``eastmoney-f10-zcfzb/1`` /
``em-datacenter-mainfinadata``）也是 32 位 MD5 且无原始字节归档。
"""

from __future__ import annotations

import pytest

from app.core.data_quality import build_data_quality_status
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.sqlite_store import SQLiteStore
from tests.support.seed import Seed

_CSMAR_ADAPTER = "csmar-c17-phase-b/1"
# 同一历史回填战役里确实存在的东财适配器（正式库实测，见模块 docstring）。
_EASTMONEY_ADAPTERS = (
    "eastmoney-f10-xjllb/1",
    "eastmoney-f10-zcfzb/1",
    "em-datacenter-mainfinadata",
)
_OTHER_ADAPTER = "akshare/1"
_MD5_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
_MD5_HEX_2 = "627a22a4c1d3e5f7098b6a4c2d1e0f3b"
_SHA256_HEX = "a" * 64


def _insert_batch(
    store: DuckDBStore,
    *,
    batch_id: str,
    adapter_version: str,
    raw_response_hash: str,
    data_type: str = "income_statement",
) -> None:
    store.write_query(
        """INSERT INTO fetch_batch
               (batch_id, data_type, source, adapter_version, fetch_time,
                raw_response_hash, row_count, confidence)
           VALUES (?, ?, 'csmar', ?, CURRENT_TIMESTAMP, ?, 1, 'strict')""",
        [batch_id, data_type, adapter_version, raw_response_hash],
    )


def _insert_audit(
    store: DuckDBStore,
    *,
    stock_code: str,
    field_name: str,
    batch_id: str,
    raw_response_hash: str,
) -> None:
    # `fetch_time` 原来是 SQL 侧 `CURRENT_TIMESTAMP`（"刚抓的"）→ `trace_now=True`。
    Seed(store, trace_now=True).source_audit(
        stock_code, field_name,
        report_date="2025-12-31", value=1.0, source="csmar",
        fetch_batch_id=batch_id, raw_response_hash=raw_response_hash, confidence="strict",
    )


def _seed_legacy_csmar_batch(store: DuckDBStore, *, count: int = 3) -> None:
    """CSMAR 历史导入批次：32 位 MD5 摘要 + 原始字节已归档且 integrity_verified。"""
    _insert_batch(
        store, batch_id="csmar-phase-b", adapter_version=_CSMAR_ADAPTER,
        raw_response_hash=_MD5_HEX, data_type="income_statement",
    )
    # 归档里带着非空 payload 且已校验，确保 lineage 的其它子项为 0（隔离用例）。
    store.write_query(
        """INSERT INTO raw_response_archive
               (raw_response_hash, source, fetch_time, payload, integrity_verified)
           VALUES (?, 'csmar', CURRENT_TIMESTAMP, ?, TRUE)""",
        [_MD5_HEX, b"csmar-legacy-raw-response"],
    )
    for index in range(count):
        _insert_audit(
            store,
            stock_code=f"60000{index}",
            field_name="deducted_net_profit",
            batch_id="csmar-phase-b",
            raw_response_hash=_MD5_HEX,
        )


def test_csmar_legacy_md5_hashes_are_disclosed_not_treated_as_invalid(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """①32 位 MD5 + csmar 批次 → 不计入 invalid，单独计入 legacy_hash_rows。"""
    _seed_legacy_csmar_batch(duckdb_store, count=3)

    status = build_data_quality_status(duckdb_store, sqlite_store)
    lineage = status["lineage"]

    assert lineage["invalid_hash_rows"] == 0
    assert lineage["legacy_hash_rows"] == 3
    # 32 位摘要本来就不参与 64 位归档缺口判定（LENGTH = 64 过滤），确认未被牵连。
    assert lineage["audit_archive_gap_rows"] == 0
    assert lineage["batch_archive_gap_rows"] == 0
    assert lineage["archive_gap_rows"] == 0


def test_eastmoney_legacy_md5_is_also_disclosed_not_invalid(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """①32 位 MD5 由**东财**适配器写入、且缺归档 → legacy 披露，不计 invalid/gap。

    2026-11-12 二次裁决：legacy 判据只看摘要形态，不看 adapter 白名单。
    正式库实测这 6 个批次（eastmoney-f10-xjllb/1、eastmoney-f10-zcfzb/1、
    em-datacenter-mainfinadata）就是被旧白名单误判成 lineage 断裂的那批。
    """
    for index, adapter in enumerate(_EASTMONEY_ADAPTERS):
        batch_id = f"eastmoney-{index}"
        digest = _MD5_HEX if index % 2 == 0 else _MD5_HEX_2
        _insert_batch(
            duckdb_store, batch_id=batch_id, adapter_version=adapter,
            raw_response_hash=digest, data_type="cash_flow_supplement",
        )
        _insert_audit(
            duckdb_store, stock_code=f"60000{index}", field_name="cf_from_operating_indirect",
            batch_id=batch_id, raw_response_hash=digest,
        )

    lineage = build_data_quality_status(duckdb_store, sqlite_store)["lineage"]

    assert lineage["invalid_hash_rows"] == 0
    assert lineage["legacy_hash_rows"] == 3
    # 缺归档但形态是 MD5 → 归入 legacy 披露，不算真缺口。
    assert lineage["legacy_archive_gap_batches"] == 3
    assert lineage["batch_archive_gap_rows"] == 0
    assert lineage["archive_gap_rows"] == 0


def test_32_char_non_hex_digest_is_still_invalid(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """②32 位但**非**十六进制（不是 MD5 形态）→ 仍是 invalid，且不缺档也要报。"""
    bad = "g" * 32
    _insert_batch(
        duckdb_store, batch_id="bad-32", adapter_version=_EASTMONEY_ADAPTERS[0],
        raw_response_hash=bad, data_type="cash_flow_supplement",
    )
    _insert_audit(
        duckdb_store, stock_code="600519", field_name="cf_from_operating_indirect",
        batch_id="bad-32", raw_response_hash=bad,
    )

    lineage = build_data_quality_status(duckdb_store, sqlite_store)["lineage"]

    assert lineage["invalid_hash_rows"] == 1
    assert lineage["legacy_hash_rows"] == 0
    assert lineage["batch_archive_gap_rows"] == 1
    assert lineage["legacy_archive_gap_batches"] == 0


@pytest.mark.parametrize(
    ("bad_hash", "label"),
    [
        ("b" * 63, "63 位"),
        ("z" * 32, "32 位但非十六进制"),
        ("b" * 40, "40 位"),
    ],
)
def test_malformed_hashes_are_still_invalid_whatever_the_adapter(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore, bad_hash: str, label: str,
) -> None:
    """②③63 位 / 非十六进制 / 40 位 → 一律仍是 invalid（形态豁免不扩口子）。

    注意：豁免放宽到"任何适配器的 32 位 hex"，但**没有**放宽到任何 32 位字符串。
    """
    _insert_batch(
        duckdb_store, batch_id="csmar-bad", adapter_version=_CSMAR_ADAPTER,
        raw_response_hash=bad_hash,
    )
    _insert_audit(
        duckdb_store, stock_code="600519", field_name="deducted_net_profit",
        batch_id="csmar-bad", raw_response_hash=bad_hash,
    )

    lineage = build_data_quality_status(duckdb_store, sqlite_store)["lineage"]

    assert lineage["invalid_hash_rows"] == 1, label
    assert lineage["legacy_hash_rows"] == 0, label


def test_lineage_invalid_warning_disappears_for_legacy_digest_only_data(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """④只有合法 legacy 摘要时，warning_codes 不再出现 LINEAGE_INVALID。"""
    _seed_legacy_csmar_batch(duckdb_store, count=2)
    # 一个合规的 64 位 SHA-256 批次：其原始字节同样已归档且校验过。
    _insert_batch(
        duckdb_store, batch_id="akshare-ok", adapter_version=_OTHER_ADAPTER,
        raw_response_hash=_SHA256_HEX, data_type="price_daily_raw",
    )
    duckdb_store.write_query(
        """INSERT INTO raw_response_archive
               (raw_response_hash, source, fetch_time, payload, integrity_verified)
           VALUES (?, 'akshare', CURRENT_TIMESTAMP, ?, TRUE)""",
        [_SHA256_HEX, b"akshare-raw-response"],
    )
    _insert_audit(
        duckdb_store, stock_code="600519", field_name="latest_close",
        batch_id="akshare-ok", raw_response_hash=_SHA256_HEX,
    )

    status = build_data_quality_status(duckdb_store, sqlite_store)
    lineage = status["lineage"]

    assert lineage["invalid_hash_rows"] == 0
    assert lineage["legacy_hash_rows"] == 2
    assert lineage["hash_mismatch_rows"] == 0
    assert "LINEAGE_INVALID" not in status["warning_codes"]


def test_legacy_digest_disclosure_survives_in_the_data_quality_payload(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """legacy_hash_rows 必须对运维可见（不是静默吞掉）。"""
    _seed_legacy_csmar_batch(duckdb_store, count=5)

    payload = build_data_quality_status(duckdb_store, sqlite_store)

    assert "lineage" in payload
    assert payload["lineage"]["legacy_hash_rows"] == 5
    assert payload["lineage"]["invalid_hash_rows"] == 0


# ─── 归档缺口分流：CSMAR 历史批次 vs 现代内容寻址批次 ────────────────────


def test_csmar_batch_without_archive_payload_is_a_disclosed_legacy_gap(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """①32 位 + csmar 批次缺归档 → 不计入 batch_archive_gap_rows。"""
    _insert_batch(
        duckdb_store, batch_id="csmar-no-archive", adapter_version=_CSMAR_ADAPTER,
        raw_response_hash=_MD5_HEX, data_type="income_statement",
    )
    _insert_audit(
        duckdb_store, stock_code="600519", field_name="deducted_net_profit",
        batch_id="csmar-no-archive", raw_response_hash=_MD5_HEX,
    )

    lineage = build_data_quality_status(duckdb_store, sqlite_store)["lineage"]

    assert lineage["legacy_archive_gap_batches"] == 1
    assert lineage["batch_archive_gap_rows"] == 0
    assert lineage["archive_gap_rows"] == 0
    assert lineage["invalid_hash_rows"] == 0
    assert lineage["legacy_hash_rows"] == 1


def test_sha256_batch_without_archive_payload_still_breaks_lineage(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """②64 位（现代内容寻址）批次缺归档 = 真 lineage 断裂 → 仍阻断。"""
    _insert_batch(
        duckdb_store, batch_id="modern-no-archive", adapter_version=_OTHER_ADAPTER,
        raw_response_hash=_SHA256_HEX, data_type="price_daily_raw",
    )
    _insert_audit(
        duckdb_store, stock_code="600519", field_name="latest_close",
        batch_id="modern-no-archive", raw_response_hash=_SHA256_HEX,
    )

    status = build_data_quality_status(duckdb_store, sqlite_store)
    lineage = status["lineage"]

    assert lineage["batch_archive_gap_rows"] == 1
    assert lineage["legacy_archive_gap_batches"] == 0
    # 审计行侧（LENGTH=64 且无归档）与批次侧同时命中 → 2 条真实缺口。
    assert lineage["audit_archive_gap_rows"] == 1
    assert lineage["archive_gap_rows"] == 2
    assert "LINEAGE_INVALID" in status["warning_codes"]


def test_mixed_legacy_and_modern_batches_only_flag_the_real_gap(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """③混合场景：legacy 缺口披露但不阻断；现代缺口照常阻断。"""
    _insert_batch(
        duckdb_store, batch_id="csmar-legacy-gap", adapter_version=_CSMAR_ADAPTER,
        raw_response_hash=_MD5_HEX, data_type="income_statement",
    )
    _insert_audit(
        duckdb_store, stock_code="600001", field_name="deducted_net_profit",
        batch_id="csmar-legacy-gap", raw_response_hash=_MD5_HEX,
    )
    _insert_batch(
        duckdb_store, batch_id="modern-gap", adapter_version=_OTHER_ADAPTER,
        raw_response_hash=_SHA256_HEX, data_type="price_daily_raw",
    )
    _insert_audit(
        duckdb_store, stock_code="600002", field_name="latest_close",
        batch_id="modern-gap", raw_response_hash=_SHA256_HEX,
    )

    lineage = build_data_quality_status(duckdb_store, sqlite_store)["lineage"]

    assert lineage["legacy_archive_gap_batches"] == 1
    assert lineage["batch_archive_gap_rows"] == 1
    # 只有现代缺口进 archive_gap_rows（审计行侧 1 + 批次侧 1）；legacy 那批
    # 一分都不进，因此阻断原因只剩真实的 64 位缺归档。
    assert lineage["archive_gap_rows"] == 2
    assert lineage["invalid_hash_rows"] == 0
    assert lineage["legacy_hash_rows"] == 1


def test_only_legacy_archive_gaps_do_not_raise_lineage_invalid(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """③(续) 只有 CSMAR legacy 缺口时，warning_codes 不含 LINEAGE_INVALID。

    这正是正式库的现状：33 个 CSMAR 批次无原始字节，旧口径让
    LINEAGE_INVALID 永久成立 → 详情页数值全遮蔽、筛选 409。
    """
    for index in range(3):
        batch_id = f"csmar-legacy-{index}"
        _insert_batch(
            duckdb_store, batch_id=batch_id, adapter_version=_CSMAR_ADAPTER,
            raw_response_hash=_MD5_HEX, data_type="income_statement",
        )
        _insert_audit(
            duckdb_store, stock_code=f"60010{index}", field_name="deducted_net_profit",
            batch_id=batch_id, raw_response_hash=_MD5_HEX,
        )

    status = build_data_quality_status(duckdb_store, sqlite_store)

    assert status["lineage"]["legacy_archive_gap_batches"] == 3
    assert status["lineage"]["batch_archive_gap_rows"] == 0
    assert status["lineage"]["archive_gap_rows"] == 0
    assert status["lineage"]["hash_mismatch_rows"] == 0
    assert "LINEAGE_INVALID" not in status["warning_codes"]


def test_csmar_and_eastmoney_legacy_batches_together_never_raise_lineage_invalid(
    duckdb_store: DuckDBStore, sqlite_store: SQLiteStore,
) -> None:
    """④csmar + 东财 legacy 缺归档混合（正式库真实形态）→ 不出现 LINEAGE_INVALID。

    正式库实测：33 个无归档批次 = 27 个 ``csmar-*`` + 6 个东财
    （``eastmoney-f10-xjllb/1`` ×3、``eastmoney-f10-zcfzb/1`` ×1、
    ``em-datacenter-mainfinadata`` ×2）。旧白名单只放行 csmar，
    于是剩下 6 个被误判成 lineage 断裂。这里用两种适配器混合验证分流。
    """
    legacy_specs = [
        ("csmar-legacy-a", _CSMAR_ADAPTER, _MD5_HEX),
        ("csmar-legacy-b", _CSMAR_ADAPTER, _MD5_HEX_2),
        ("eastmoney-legacy-a", _EASTMONEY_ADAPTERS[0], _MD5_HEX),
        ("eastmoney-legacy-b", _EASTMONEY_ADAPTERS[2], _MD5_HEX_2),
    ]
    for index, (batch_id, adapter, digest) in enumerate(legacy_specs):
        _insert_batch(
            duckdb_store, batch_id=batch_id, adapter_version=adapter,
            raw_response_hash=digest, data_type="cash_flow_supplement",
        )
        _insert_audit(
            duckdb_store, stock_code=f"60020{index}", field_name="deducted_net_profit",
            batch_id=batch_id, raw_response_hash=digest,
        )

    status = build_data_quality_status(duckdb_store, sqlite_store)
    lineage = status["lineage"]

    assert lineage["invalid_hash_rows"] == 0
    assert lineage["legacy_hash_rows"] == 4
    assert lineage["legacy_archive_gap_batches"] == 4
    assert lineage["batch_archive_gap_rows"] == 0
    assert lineage["archive_gap_rows"] == 0
    assert lineage["orphan_batch_rows"] == 0
    assert "LINEAGE_INVALID" not in status["warning_codes"]
