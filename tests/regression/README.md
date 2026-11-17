# tests/regression 命名约定与事故映射

本目录（`tests/regression/`）的测试文件一律按**被测行为**命名——文件名回答"这个文件守住
哪条行为契约"，而不是"它来自哪次事故"。历史上本目录曾有 25 个按**事故日期 / 事故编号**
命名的文件（`test_fix20261112_*`、`test_fix20261113_*`、`test_p0_*`），新人无法从文件名
判断它测什么行为（见 `docs/reports/134` §4.1 与 §5 S7 第 1 条）。这些文件已于 2026-11-17
按 `docs/reports/134` §5 S7 与 `docs/reports/136` §7 完成重命名：文件名只留行为，**事故出处
（`reports/NNN / 日期`）写进每个文件的模块 docstring 第一行**。新旧对照如下。

| 旧文件名（按事故命名，已弃用） | 新文件名（按行为命名） |
|---|---|
| `test_fix20261112_adapter_manager.py` | `test_adapter_manager_contract.py` |
| `test_fix20261112_adapter_prices_listing.py` | `test_adapter_prices_and_listing.py` |
| `test_fix20261112_adapter_sources.py` | `test_adapter_source_error_kinds.py` |
| `test_fix20261112_gate_contract.py` | `test_publish_gate_contract.py` |
| `test_fix20261112_gate_lineage_hash.py` | `test_lineage_hash_algorithm.py` |
| `test_fix20261112_lead_data_maintenance.py` | `test_quarantine_column_drift.py` |
| `test_fix20261112_lead_publish_missing_inputs.py` | `test_publish_missing_inputs.py` |
| `test_fix20261112_lead_refresh_marker_tz.py` | `test_refresh_marker_cn_timezone.py` |
| `test_fix20261112_lead_retry_settlement.py` | `test_retry_error_kind_settlement.py` |
| `test_fix20261112_update_orchestration.py` | `test_update_orchestration.py` |
| `test_fix20261112_update_price_batch.py` | `test_price_full_history_batch.py` |
| `test_fix20261113_adapter_degradation_manager.py` | `test_adapter_degradation_manager.py` |
| `test_fix20261113_gate_smalllib_ratio.py` | `test_publish_gate_small_library.py` |
| `test_fix20261113_lead_statistics_fingerprint.py` | `test_statistics_fingerprint.py` |
| `test_fix20261113_pipeline_lag_adapter_injection.py` | `test_adapter_injection_shared_manager.py` |
| `test_fix20261113_pipeline_lag_capital.py` | `test_capital_history_due_watermark.py` |
| `test_fix20261113_pipeline_lag_qfq.py` | `test_qfq_lag_disclosure.py` |
| `test_fix20261113_pipeline_price_batch.py` | `test_price_batch_flush.py` |
| `test_fix20261113_pipeline_qfq_pending.py` | `test_qfq_refetch_pending.py` |
| `test_fix20261113_pipeline_source_contract.py` | `test_source_result_contract.py` |
| `test_fix20261113_probe_ready.py` | `test_readiness_probe.py` |
| `test_p0_3_p0_4_regressions.py` | `test_qfq_contiguity_and_dsl_runtime.py` |
| `test_p0_5_required_capabilities.py` | `test_financial_sector_and_xdxr_lifecycle.py` |
| `test_p0_7_8_9_series_breaks.py` | `test_series_breaks_and_derived_fields.py` |
| `test_p0_release_security.py` | `test_release_security_contract.py` |

> 提醒：`docs/reports/` 下的**历史报告仍引用旧文件名**（历史报告正文不可改，只可改
> front-matter 状态字段）。读报告时遇到 `test_fix20261112_*` / `test_p0_*` 之类的名字，
> 请回查上表找到现在的文件名，不要以为该测试已被删除。

## 新增测试的命名约定

1. 文件名格式：`test_<域>_<行为>.py`——`<域>` 是被测模块/概念（如 `adapter`、
   `publish_gate`、`qfq`、`lineage_hash`、`readiness`），`<行为>` 是它守住的行为
   （如 `_contract`、`_disclosure`、`_column_drift`）。示例：
   `test_publish_gate_small_library.py`（小库上的发布门禁）、
   `test_refresh_marker_cn_timezone.py`（日节流标记按中国时区归一）。
2. **事故编号不写进文件名**。事故/工单出处写进模块 docstring 第一行，格式固定为
   `事故出处：reports/NNN / YYYY-MM-DD。`（编号必须在真实报告里存在，不得编造）；
   行为契约写在随后的行里——"行为契约在前、事故编号在后"（`docs/reports/134` §5 S7 第 5 条）。
3. 一个文件测多个行为域时，按**主要被测行为**命名，其余行为在 docstring 里逐条列出
   （例：`test_series_breaks_and_derived_fields.py` 覆盖 P0-7/8/9 三条事故）。
4. 判据：名字读起来像"一条断言"，而不是像"一次提交"。若新文件自问"这是哪次事故的
   回归？"才能读懂，说明它该改名。
