# Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate locally provable data-loss and false-semantics defects from the V2 audit without inventing or destructively cleaning financial truth data.

**Architecture:** Tests run only against temporary DuckDB/SQLite files. Long-running snapshot calculation writes to a staging table and publishes only through one explicit DuckDB transaction after validation. Adapter, metadata, reporting, and schema boundaries reject unknown or incomplete states instead of silently substituting success/default values.

**Tech Stack:** Python 3.11+, pytest, DuckDB, SQLite, FastAPI, Pydantic, Vue 3, TypeScript, Vite.

---

## File Map

- `pyproject.toml`: pytest discovery, markers, and quality commands.
- `tests/conftest.py`: temporary database paths and reusable isolated fixtures.
- `tests/regression/`: true pytest behavior tests for audit findings.
- `tests/legacy/`: retained script-style milestone checks excluded from pytest collection.
- `app/core/storage/duckdb_store.py`: explicit transaction context.
- `app/core/storage/schema.py`: idempotent DuckDB migrations.
- `app/core/indicators/calculator.py`: dividend query, complete-report selection, published override policy, staging snapshot publication.
- `app/core/adapters/manager.py`: adapter-name normalization and validation.
- `config/default.yaml`: canonical adapter names and fallback chains.
- `app/core/init.py`: null-preserving metadata UPSERT and financial completeness gate.
- `app/core/backfill.py`: atomic raw/qfq write status and retry recording.
- `app/core/dsl/codegen.py`: reject false historical semantics until a correct execution context exists.
- `app/web/api/stock_detail.py`: correct TTM or explicit unsupported response; freshness metadata.
- `app/web/api/data_status.py`: latest-record/latest-complete dates and integrity warnings.
- `frontend/src/views/DataStatusPage.vue`: display backend integrity and staleness warnings without write actions.

## Task 1: Pytest Collection Safety

**Files:** `pyproject.toml`, `tests/conftest.py`, `tests/legacy/`, `tests/regression/test_collection_safety.py`

- [ ] Move script-style milestone checks to `tests/legacy/` without altering their explicit `python tests/legacy/<script>.py` behavior.
- [ ] Configure pytest `testpaths = ["tests/regression"]`, strict markers, and no collection from `tests/legacy`.
- [ ] Add temporary DuckDB/SQLite fixtures under `tmp_path` and initialize schema explicitly inside fixtures.
- [ ] Add a subprocess test that hashes production databases, runs `pytest --collect-only`, and asserts both hashes are unchanged.
- [ ] Run `pytest --collect-only -q`; expect only regression tests and no production hash change.

## Task 2: Dividend SQL Correctness (DQ-01)

**Files:** `tests/regression/test_indicator_dividends.py`, `app/core/indicators/calculator.py`

- [ ] Add fixture rows for no dividends, one year, multiple payments in one year, and cross-year payments.
- [ ] Assert `_get_dividend_summary()` returns `latest_dps` as the sum for the latest year and never raises a binder error.
- [ ] Run the targeted test and verify it fails with DuckDB `BinderException`.
- [ ] Replace the nested window/aggregate with `valid_dividends` and `latest` CTEs.
- [ ] Run the targeted tests and representative stock calculation; expect all pass.

## Task 3: Explicit DuckDB Transactions (DQ-02/DQ-14)

**Files:** `tests/regression/test_duckdb_transactions.py`, `app/core/storage/duckdb_store.py`

- [ ] Add a failure-injection test that updates a row inside `store.transaction()` and raises; assert original data remains.
- [ ] Verify the test fails because no transaction API exists.
- [ ] Add `transaction()` using one write-locked connection with `BEGIN`, `COMMIT`, and `ROLLBACK` on exception.
- [ ] Run transaction tests; expect rollback and commit cases to pass.

## Task 4: Atomic Snapshot Publication (DQ-02)

**Files:** `tests/regression/test_snapshot_publish.py`, `app/core/indicators/calculator.py`

- [ ] Add isolated source rows and an existing production snapshot sentinel.
- [ ] Inject a calculation failure and assert `indicator_snapshot` remains byte-for-byte equivalent by ordered rows.
- [ ] Verify the test fails with the current delete-first implementation.
- [ ] Write all batches to a unique staging table on one connection.
- [ ] Validate zero duplicates, required columns, non-empty candidates, and zero failed calculations before publication.
- [ ] Publish with `DELETE FROM indicator_snapshot; INSERT ... SELECT ...` inside one explicit transaction; rollback on any exception.
- [ ] Add successful publish and bidirectional `EXCEPT` assertions.
- [ ] Run snapshot tests; expect old data preserved on failure and exact staging equivalence on success.

## Task 5: Adapter Configuration Contract (DQ-04)

**Files:** `tests/regression/test_adapter_priority.py`, `app/core/adapters/manager.py`, `config/default.yaml`

- [ ] Add tests that legacy `akshare` normalizes to `akshare_eastmoney`, unknown names raise a typed configuration error, and configured primary sources retain default fallbacks.
- [ ] Verify tests fail against current silent-skip behavior.
- [ ] Add a canonical alias map and merge configured order with defaults without duplicates.
- [ ] Validate configured adapter names after lazy registration and fail before fetch when unknown.
- [ ] Change default YAML to canonical registered names and explicit fallback lists.
- [ ] Run adapter contract tests without network calls; expect pass.

## Task 6: Complete Financial Period Semantics (DQ-03/DQ-07)

**Files:** `tests/regression/test_financial_completeness.py`, `app/core/indicators/calculator.py`, `app/core/init.py`

- [ ] Add a shell-row fixture newer than a complete row and assert indicator calculation selects the complete row.
- [ ] Add ingestion tests that incomplete balance/income rows are rejected from complete statement publication and reported missing.
- [ ] Verify both tests fail under latest-date-only and unconditional upsert behavior.
- [ ] Define minimum required field sets per statement and use them at ingestion and read time.
- [ ] Preserve raw observations without promoting incomplete rows to latest-complete semantics.
- [ ] Run tests; expect latest record and latest complete date to remain distinguishable.

## Task 7: Metadata Null Preservation (DQ-11)

**Files:** `tests/regression/test_stock_metadata.py`, `app/core/init.py`, `app/core/storage/schema.py`

- [ ] Add tests proving missing `is_suspended`, listing date, and industry remain `NULL` or preserve an existing non-null value.
- [ ] Verify current defaults/`INSERT OR REPLACE` fail the tests.
- [ ] Change schema booleans to nullable semantics for unknown state.
- [ ] Use `INSERT ... ON CONFLICT DO UPDATE` with `COALESCE` for optional fields.
- [ ] Run metadata tests and schema idempotency tests.

## Task 8: QFQ Schema and Backfill Status (DQ-10/DQ-13/DQ-14)

**Files:** `tests/regression/test_qfq_backfill.py`, `tests/regression/test_schema_migrations.py`, `app/core/storage/schema.py`, `app/core/backfill.py`, `app/core/init.py`, `app/web/api/stock_detail.py`

- [ ] Add old-schema upgrade test showing `turnover_rate` is added idempotently.
- [ ] Add backfill tests proving raw success plus non-exempt qfq failure yields partial/failed status and retry entry.
- [ ] Add transaction failure injection after delete and assert both raw/qfq old rows remain.
- [ ] Implement a versioned DuckDB migration table and `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migration.
- [ ] Write `turnover_rate` consistently to raw/qfq and expose it consistently in K-line API.
- [ ] Treat BSE qfq absence as an explicit reasoned exemption; all other absence records retry/failure.
- [ ] Run migration/backfill/API tests.

## Task 9: Published Override Policy (DQ-12)

**Files:** `tests/regression/test_manual_overrides.py`, `app/core/indicators/calculator.py`, `app/core/storage/schema.py`

- [ ] Add tests for draft/active, published, rolled-back, and conflicting published overrides.
- [ ] Verify current query applies every non-rolled-back override and silently resolves conflicts by row order.
- [ ] Apply only `status='published' AND rolled_back_at IS NULL`.
- [ ] Reject more than one current published override per stock/field/report date instead of choosing silently.
- [ ] Add an idempotent SQLite unique-current-version migration if SQLite supports the required partial index.
- [ ] Run override tests.

## Task 10: TTM and DSL Truthfulness (DQ-09)

**Files:** `tests/regression/test_ttm_semantics.py`, `tests/regression/test_dsl_period_functions.py`, `app/web/api/stock_detail.py`, `app/core/dsl/codegen.py`

- [ ] Add constructed annual/Q1 data and assert TTM equals prior annual + current cumulative - prior-year cumulative.
- [ ] Assert non-year-end TTM differs from annual when inputs differ.
- [ ] Assert DSL `TTM`, `YoY`, and `QoQ` do not emit pass-through or lagged raw values as final semantics.
- [ ] Verify tests fail against current API/codegen.
- [ ] Implement TTM trend rows where sufficient data exists; return explicit 422/501 when unavailable.
- [ ] Reject unsupported DSL period functions with a stable typed error until historical execution context is implemented.
- [ ] Run targeted API/DSL tests.

## Task 11: Honest Status and Staleness (DQ-03/DQ-05/DQ-06/DQ-07/DQ-12)

**Files:** `tests/regression/test_data_status.py`, `app/web/api/data_status.py`, `app/web/api/stock_detail.py`, `frontend/src/views/DataStatusPage.vue`

- [ ] Add API tests for latest record date, latest complete date, price date, calculated date, stale warning, invalid dividend-date concentration, orphan lineage, invalid hashes, pending overrides, and abandoned jobs.
- [ ] Verify current API omits these signals.
- [ ] Add read-only aggregate queries and machine-readable warning codes.
- [ ] Add indicator-level freshness metadata and stale reasons.
- [ ] Render warning cards on the existing read-only status page without introducing write controls.
- [ ] Run backend API tests, `npm run build`, and browser QA on status/detail pages.

## Task 12: Final Verification and Audit Ledger

**Files:** `docs/12_AUDIT_REMEDIATION_REPORT.md`, `task_plan.md`, `findings.md`, `progress.md`

- [ ] Run Ruff/LSP diagnostics on every changed Python file.
- [ ] Run `pytest --collect-only -q`, compare production database hashes, then run all regression tests.
- [ ] Run representative indicator calculation and FastAPI API smoke checks against a database copy.
- [ ] Run frontend `npm run build` and browser verification.
- [ ] Classify DQ-01 to DQ-14 as fixed, guarded, data-only blocked, or owner-confirmation required with evidence.
- [ ] Record remaining external-truth requirements; do not claim all 23 audit gates pass without those inputs.

## Self-Review

- Spec coverage: all DQ-01 through DQ-14 are mapped to a task or an explicit non-destructive reporting boundary.
- Placeholder scan: no TBD/TODO implementation placeholders remain.
- Type consistency: transaction, staging, completeness, override, and status concepts use one name throughout.
- Scope: destructive data reconstruction and external-truth validation are intentionally excluded.
