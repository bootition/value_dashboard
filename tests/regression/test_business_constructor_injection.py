"""Business services fail closed and share explicitly injected stores."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.backfill import PriceBackfiller
from app.core.backup.manager import BackupManager
from app.core.dsl.engine import DSLEngine
from app.core.dsl.registry import ExpressionRegistry
from app.core.dsl.validator import Validator
from app.core.indicators.calculator import IndicatorCalculator
from app.core.init import DataInitializer
from app.core.pdf.correction import CorrectionManager
from app.core.pdf.manager import PDFManager
from app.core.screening.engine import ScreeningEngine
from app.core.storage.duckdb_store import DuckDBStore
from app.core.storage.path_policy import DatabasePathSet, PathIsolationError
from app.core.storage.sqlite_store import SQLiteStore
from app.core.update import IncrementalUpdater


@pytest.mark.parametrize(
    "service_type",
    [
        DataInitializer,
        PriceBackfiller,
        IncrementalUpdater,
        IndicatorCalculator,
        ExpressionRegistry,
        DSLEngine,
        Validator,
        ScreeningEngine,
        PDFManager,
        CorrectionManager,
        BackupManager,
    ],
)
def test_business_constructor_requires_an_explicit_database_boundary(service_type: type) -> None:
    with pytest.raises(PathIsolationError):
        service_type()


def test_dual_store_services_construct_missing_stores_from_paths(
    database_paths: DatabasePathSet,
) -> None:
    services = [
        DataInitializer(paths=database_paths),
        PriceBackfiller(paths=database_paths),
        IncrementalUpdater(paths=database_paths),
        IndicatorCalculator(paths=database_paths),
        CorrectionManager(paths=database_paths),
        BackupManager(paths=database_paths),
    ]

    for service in services:
        duck = getattr(service, "duck", getattr(service, "_duck", None))
        sqlite = getattr(service, "sqlite", getattr(service, "_sqlite", None))
        assert duck.db_path == database_paths.duckdb_path
        assert sqlite.db_path == database_paths.sqlite_path


def test_single_store_services_require_only_their_store(
    database_paths: DatabasePathSet,
) -> None:
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)

    assert ExpressionRegistry(sqlite=sqlite).sqlite is sqlite
    assert Validator(sqlite=sqlite)._sqlite is sqlite
    assert ScreeningEngine(duck=duck).duck is duck
    assert PDFManager(sqlite=sqlite)._sqlite is sqlite


def test_dsl_engine_shares_the_same_sqlite_store_across_components(
    database_paths: DatabasePathSet,
) -> None:
    duck = DuckDBStore(paths=database_paths)
    sqlite = SQLiteStore(paths=database_paths)

    engine = DSLEngine(duck=duck, sqlite=sqlite)

    assert engine.duck is duck
    assert engine.registry.sqlite is sqlite
    assert engine.validator._registry is engine.registry
    assert engine.validator._sqlite is sqlite


def test_validator_cycle_check_uses_its_injected_sqlite_store() -> None:
    class RecordingSQLite:
        def __init__(self) -> None:
            self.queries: list[tuple[str, list[object]]] = []

        def query(self, sql: str, params: list[object]) -> list[dict]:
            self.queries.append((sql, params))
            return []

    sqlite = RecordingSQLite()
    validator = Validator(sqlite=sqlite)

    validator._check_cycles({"custom_metric"})

    assert sqlite.queries
    assert sqlite.queries[0][1] == ["custom_metric"]


def test_backup_manager_uses_run_local_backup_directory(
    database_paths: DatabasePathSet,
) -> None:
    manager = BackupManager(paths=database_paths)

    assert manager._backup_dir == database_paths.run_root / "backup"
    repository_backup = Path(__file__).parents[2] / "data" / "backup"
    assert manager._backup_dir != repository_backup
