# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for src/platform/lake/provisioning.py."""

from pathlib import Path
from typing import Any

from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import TableAlreadyExistsError
import pytest
from src.platform.lake.catalog import get_catalog
from src.platform.lake.config import LakeSettings
from src.platform.lake.exceptions import LakeCatalogError
from src.platform.lake.provisioning import EnsureTablesResult, ensure_tables
from src.platform.lake.schemas import (
    NORMALIZED_ACCESS_FACTS_PARTITION_SPEC,
    NORMALIZED_ACCESS_FACTS_SCHEMA,
    NORMALIZED_ACCESS_FACTS_TABLE,
    RAW_ACCESS_ARTIFACTS_PARTITION_SPEC,
    RAW_ACCESS_ARTIFACTS_SCHEMA,
    RAW_ACCESS_ARTIFACTS_TABLE,
)
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_catalog_for_test(settings: LakeSettings, log_service: LogService) -> Catalog:
    return get_catalog(settings, log_service)


# ---------------------------------------------------------------------------
# T1: first call creates both tables
# ---------------------------------------------------------------------------


def test_ensure_tables_creates_both_on_first_call(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
    tmp_path: Path,
) -> None:
    log_service, sink = capturing_log_service
    catalog = _get_catalog_for_test(lake_settings_sqlite, log_service)

    result = ensure_tables(catalog, log_service=log_service)

    assert isinstance(result, EnsureTablesResult)
    assert len(result.tables) == 5  # Step 9: tables now 5, name kept for baseline stability

    for entry in result.tables:
        assert entry.created is True
        assert entry.current_snapshot_id is None

    raw_tables = [tuple(t) for t in catalog.list_tables(('raw',))]
    norm_tables = [tuple(t) for t in catalog.list_tables(('normalized',))]
    assert RAW_ACCESS_ARTIFACTS_TABLE in raw_tables
    assert NORMALIZED_ACCESS_FACTS_TABLE in norm_tables

    ensured_records = [r for r in sink.records if 'tables_ensured' in r.message]
    assert len(ensured_records) == 1
    assert ensured_records[0].payload['created_count'] == 5  # Step 9: tables now 5
    assert ensured_records[0].payload['preexisting_count'] == 0

    failed_records = [r for r in sink.records if 'tables_ensure_failed' in r.message]
    assert len(failed_records) == 0

    drift_records = [r for r in sink.records if 'schema_drift_detected' in r.message]
    assert len(drift_records) == 0


# ---------------------------------------------------------------------------
# T2: second call is idempotent
# ---------------------------------------------------------------------------


def test_ensure_tables_is_idempotent_on_second_call(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    log_service, sink = capturing_log_service
    catalog = _get_catalog_for_test(lake_settings_sqlite, log_service)

    first_result = ensure_tables(catalog, log_service=log_service)
    sink.clear()

    second_result = ensure_tables(catalog, log_service=log_service)

    assert len(second_result.tables) == 5  # Step 9: tables now 5, name kept for baseline stability
    for entry in second_result.tables:
        assert entry.created is False
        assert entry.current_snapshot_id is None

    # No duplicate tables in catalog
    raw_tables = [tuple(t) for t in catalog.list_tables(('raw',))]
    norm_tables = [tuple(t) for t in catalog.list_tables(('normalized',))]
    assert raw_tables.count(RAW_ACCESS_ARTIFACTS_TABLE) == 1
    assert norm_tables.count(NORMALIZED_ACCESS_FACTS_TABLE) == 1

    # Fresh summary log with preexisting=5
    ensured_records = [r for r in sink.records if 'tables_ensured' in r.message]
    assert len(ensured_records) == 1
    assert ensured_records[0].payload['created_count'] == 0
    assert ensured_records[0].payload['preexisting_count'] == 5  # Step 9: tables now 5

    # Identifiers are identical between both calls
    for first_entry, second_entry in zip(first_result.tables, second_result.tables):
        assert first_entry.identifier == second_entry.identifier


# ---------------------------------------------------------------------------
# T3: provisioned schema and partition spec match declared constants
# ---------------------------------------------------------------------------


def test_ensure_tables_preserves_declared_schema_and_partitions(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    log_service, _ = capturing_log_service
    catalog = _get_catalog_for_test(lake_settings_sqlite, log_service)
    ensure_tables(catalog, log_service=log_service)

    raw = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
    norm = catalog.load_table(NORMALIZED_ACCESS_FACTS_TABLE)

    # Schema field count
    assert len(raw.schema().fields) == len(RAW_ACCESS_ARTIFACTS_SCHEMA.fields)
    assert len(norm.schema().fields) == len(NORMALIZED_ACCESS_FACTS_SCHEMA.fields)

    # Per-field equality (field_id, name, type, required) for raw.access_artifacts
    for loaded_f, declared_f in zip(raw.schema().fields, RAW_ACCESS_ARTIFACTS_SCHEMA.fields):
        assert loaded_f.field_id == declared_f.field_id
        assert loaded_f.name == declared_f.name
        assert str(loaded_f.field_type) == str(declared_f.field_type)
        assert loaded_f.required == declared_f.required

    # Per-field equality for normalized.access_facts
    for loaded_f, declared_f in zip(norm.schema().fields, NORMALIZED_ACCESS_FACTS_SCHEMA.fields):
        assert loaded_f.field_id == declared_f.field_id
        assert loaded_f.name == declared_f.name
        assert str(loaded_f.field_type) == str(declared_f.field_type)
        assert loaded_f.required == declared_f.required

    # Partition spec for raw.access_artifacts: identity(application_id), identity(artifact_type)
    raw_spec_fields = raw.spec().fields
    declared_raw_fields = RAW_ACCESS_ARTIFACTS_PARTITION_SPEC.fields
    assert len(raw_spec_fields) == len(declared_raw_fields)
    for loaded_pf, declared_pf in zip(raw_spec_fields, declared_raw_fields):
        assert loaded_pf.source_id == declared_pf.source_id
        assert loaded_pf.field_id == declared_pf.field_id
        assert type(loaded_pf.transform).__name__ == type(declared_pf.transform).__name__
        assert loaded_pf.name == declared_pf.name

    # Partition spec for normalized.access_facts: identity(application_id_denorm), identity(subject_kind_denorm)
    norm_spec_fields = norm.spec().fields
    declared_norm_fields = NORMALIZED_ACCESS_FACTS_PARTITION_SPEC.fields
    assert len(norm_spec_fields) == len(declared_norm_fields)
    for loaded_pf, declared_pf in zip(norm_spec_fields, declared_norm_fields):
        assert loaded_pf.source_id == declared_pf.source_id
        assert loaded_pf.field_id == declared_pf.field_id
        assert type(loaded_pf.transform).__name__ == type(declared_pf.transform).__name__
        assert loaded_pf.name == declared_pf.name


# ---------------------------------------------------------------------------
# T4: unexpected failure is wrapped in LakeCatalogError + ERROR log emitted
# ---------------------------------------------------------------------------


def test_ensure_tables_wraps_unexpected_failure_in_lake_catalog_error(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_service, sink = capturing_log_service
    catalog = _get_catalog_for_test(lake_settings_sqlite, log_service)

    def _raising_create_table(*args: object, **kwargs: object) -> None:
        raise RuntimeError('boom')

    monkeypatch.setattr(catalog, 'create_table', _raising_create_table)

    with pytest.raises(LakeCatalogError) as excinfo:
        ensure_tables(catalog, log_service=log_service)

    assert isinstance(excinfo.value.__cause__, RuntimeError)

    failed_records = [r for r in sink.records if 'tables_ensure_failed' in r.message]
    assert len(failed_records) == 1
    assert failed_records[0].payload['error_type'] == 'RuntimeError'
    assert failed_records[0].payload['namespace'] == 'raw'

    ensured_records = [r for r in sink.records if 'tables_ensured' in r.message]
    assert len(ensured_records) == 0


# ---------------------------------------------------------------------------
# T5 (optional): recover from concurrent TableAlreadyExistsError race
# ---------------------------------------------------------------------------


def test_ensure_tables_recovers_from_concurrent_create_race(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_table raises TableAlreadyExistsError (race) → load_table recovery → created=False."""
    log_service, sink = capturing_log_service
    catalog = _get_catalog_for_test(lake_settings_sqlite, log_service)

    # Create tables for real first so load_table (recovery path) has something to load.
    real_create = catalog.create_table
    first_call = {'done': False}

    def _race_create_table(
        identifier: str | tuple[str, ...],
        schema: Any,
        partition_spec: Any = None,
        **kwargs: object,
    ) -> object:
        if not first_call['done']:
            first_call['done'] = True
            real_create(identifier=identifier, schema=schema, partition_spec=partition_spec)
            raise TableAlreadyExistsError(f'simulated race for {identifier}')
        return real_create(identifier=identifier, schema=schema, partition_spec=partition_spec)

    monkeypatch.setattr(catalog, 'create_table', _race_create_table)

    result = ensure_tables(catalog, log_service=log_service)

    # First table was recovered (race), remaining 4 were created normally.
    assert result.tables[0].created is False
    assert result.tables[1].created is True

    ensured_records = [r for r in sink.records if 'tables_ensured' in r.message]
    assert len(ensured_records) == 1
    assert ensured_records[0].payload['created_count'] == 4  # Step 9: 5 tables, 1 race → 4 created
    assert ensured_records[0].payload['preexisting_count'] == 1
