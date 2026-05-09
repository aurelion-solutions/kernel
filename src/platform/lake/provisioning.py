# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Idempotent Iceberg table provisioning for the kernel data lake.

Public surface:
- ``ensure_tables(catalog, *, log_service) -> EnsureTablesResult``
- ``EnsureTablesResult`` — frozen dataclass; one ``EnsuredTable`` per provisioned table.
- ``EnsuredTable`` — frozen dataclass; per-table provisioning info.

Called once from the FastAPI lifespan (Phase 15 Step 2), between ``get_catalog`` and
``LakeSessionFactory``.  Safe to call multiple times — second call is a no-op for the
catalog and emits a fresh ``platform.lake.tables_ensured`` log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import NoSuchTableError, TableAlreadyExistsError
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.types import NestedField
from src.platform.lake.exceptions import LakeCatalogError
from src.platform.lake.schemas import (
    NORMALIZED_ACCESS_FACTS_PARTITION_SPEC,
    NORMALIZED_ACCESS_FACTS_SCHEMA,
    NORMALIZED_ACCESS_FACTS_TABLE,
    RAW_ACCESS_ARTIFACTS_PARTITION_SPEC,
    RAW_ACCESS_ARTIFACTS_SCHEMA,
    RAW_ACCESS_ARTIFACTS_TABLE,
    RAW_EMPLOYEES_PARTITION_SPEC,
    RAW_EMPLOYEES_SCHEMA,
    RAW_EMPLOYEES_TABLE,
    RAW_ORG_UNITS_PARTITION_SPEC,
    RAW_ORG_UNITS_SCHEMA,
    RAW_ORG_UNITS_TABLE,
    RAW_PERSONS_PARTITION_SPEC,
    RAW_PERSONS_SCHEMA,
    RAW_PERSONS_TABLE,
)
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields

_COMPONENT = 'platform.lake'

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnsuredTable:
    """Per-table result returned by :func:`ensure_tables`."""

    namespace: tuple[str, ...]
    name: str
    identifier: tuple[str, ...]
    created: bool
    current_snapshot_id: int | None


@dataclass(frozen=True, slots=True)
class EnsureTablesResult:
    """Aggregated result of :func:`ensure_tables`.

    ``tables`` is a 2-tuple in declaration order:
    ``(raw.access_artifacts, normalized.access_facts)``.
    """

    tables: tuple[EnsuredTable, ...]


# ---------------------------------------------------------------------------
# Provisioning declarations
# ---------------------------------------------------------------------------

_TABLE_SPECS: tuple[tuple[tuple[str, ...], Schema, PartitionSpec], ...] = (
    (RAW_ACCESS_ARTIFACTS_TABLE, RAW_ACCESS_ARTIFACTS_SCHEMA, RAW_ACCESS_ARTIFACTS_PARTITION_SPEC),
    (NORMALIZED_ACCESS_FACTS_TABLE, NORMALIZED_ACCESS_FACTS_SCHEMA, NORMALIZED_ACCESS_FACTS_PARTITION_SPEC),
    (RAW_PERSONS_TABLE, RAW_PERSONS_SCHEMA, RAW_PERSONS_PARTITION_SPEC),
    (RAW_ORG_UNITS_TABLE, RAW_ORG_UNITS_SCHEMA, RAW_ORG_UNITS_PARTITION_SPEC),
    (RAW_EMPLOYEES_TABLE, RAW_EMPLOYEES_SCHEMA, RAW_EMPLOYEES_PARTITION_SPEC),
)


# ---------------------------------------------------------------------------
# Schema drift check (advisory, non-mutating)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DriftResult:
    now_optional: list[str]
    has_type_change: bool


def _check_schema_drift(
    identifier: tuple[str, ...],
    existing_fields: tuple[NestedField, ...],
    declared_fields: tuple[NestedField, ...],
    log_service: LogService,
) -> _DriftResult:
    """Compare field id+name+type+required against the declared schema constant.

    Returns a :class:`_DriftResult` with:
    - ``now_optional``: field names that safely evolved from required=True → False
      (apply via ``make_column_optional``).
    - ``has_type_change``: True when any field changed its Iceberg type
      (requires drop-and-recreate).

    On any mismatch emits one ``platform.lake.table_schema_drift_detected`` WARNING.
    Does NOT raise and does NOT mutate the table.
    """
    existing_triples = {(f.field_id, f.name, str(f.field_type), f.required) for f in existing_fields}
    declared_triples = {(f.field_id, f.name, str(f.field_type), f.required) for f in declared_fields}

    if existing_triples == declared_triples:
        return _DriftResult(now_optional=[], has_type_change=False)

    extra = existing_triples - declared_triples
    missing = declared_triples - existing_triples
    type_mismatches: list[dict[str, Any]] = []
    now_optional: list[str] = []
    has_type_change = False

    existing_by_id = {f.field_id: f for f in existing_fields}
    declared_by_id = {f.field_id: f for f in declared_fields}
    for fid in set(existing_by_id) & set(declared_by_id):
        ef = existing_by_id[fid]
        df = declared_by_id[fid]
        if str(ef.field_type) != str(df.field_type) or ef.required != df.required:
            type_mismatches.append(
                {
                    'field_id': fid,
                    'name': ef.name,
                    'existing_type': str(ef.field_type),
                    'declared_type': str(df.field_type),
                    'existing_required': ef.required,
                    'declared_required': df.required,
                }
            )
            if str(ef.field_type) != str(df.field_type):
                has_type_change = True
            elif ef.required and not df.required:
                # required → optional: safe Iceberg schema evolution
                now_optional.append(ef.name)

    namespace = identifier[:-1]
    table = identifier[-1]
    log_service.emit_safe(
        level=LogLevel.WARNING,
        message='platform.lake.table_schema_drift_detected',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            {
                'namespace': '.'.join(namespace),
                'table': table,
                'extra_fields': [str(t) for t in extra],
                'missing_fields': [str(t) for t in missing],
                'type_mismatches': type_mismatches,
                'auto_evolve_optional': now_optional,
                'has_type_change': has_type_change,
            },
            actor_component=_COMPONENT,
            target_id=f'{".".join(namespace)}.{table}',
        ),
    )
    return _DriftResult(now_optional=now_optional, has_type_change=has_type_change)


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------


def _provision_one(
    catalog: Catalog,
    identifier: tuple[str, ...],
    schema: Schema,
    partition_spec: PartitionSpec,
    log_service: LogService,
) -> EnsuredTable:
    """Provision a single Iceberg table idempotently.

    Algorithm:
    1. Try ``load_table`` → table exists → schema-drift check, return created=False.
    2. On ``NoSuchTableError`` → ``create_table``.
    3. On ``TableAlreadyExistsError`` (concurrent boot race) → ``load_table``, return created=False.
    4. On any other exception → emit ERROR log and raise ``LakeCatalogError``.
    """
    namespace = identifier[:-1]
    table_name = identifier[-1]

    # Step 1: try loading an existing table first (idempotent check).
    try:
        existing = catalog.load_table(identifier)
        drift = _check_schema_drift(
            identifier,
            existing.schema().fields,
            schema.fields,
            log_service,
        )
        if drift.has_type_change:
            # Field type changed — incompatible with existing data.
            # Drop from the catalog and recreate immediately.
            catalog.drop_table(identifier)
            catalog.create_table(
                identifier=identifier,
                schema=schema,
                partition_spec=partition_spec,
            )
            return EnsuredTable(
                namespace=namespace,
                name=table_name,
                identifier=identifier,
                created=True,
                current_snapshot_id=None,
            )
        # Auto-evolve: required→optional changes are backwards-compatible; apply them.
        if drift.now_optional:
            upd = existing.update_schema()
            for col_name in drift.now_optional:
                upd.make_column_optional(col_name)
            upd.commit()
        snapshot_id: int | None = existing.metadata.current_snapshot_id
        return EnsuredTable(
            namespace=namespace,
            name=table_name,
            identifier=identifier,
            created=False,
            current_snapshot_id=snapshot_id,
        )
    except NoSuchTableError:
        pass  # table is absent; fall through to create

    # Step 2: create the table.
    try:
        catalog.create_table(
            identifier=identifier,
            schema=schema,
            partition_spec=partition_spec,
        )
        return EnsuredTable(
            namespace=namespace,
            name=table_name,
            identifier=identifier,
            created=True,
            current_snapshot_id=None,
        )
    except TableAlreadyExistsError:
        # Step 3: concurrent boot race — another process/thread created it between our
        # load_table (NoSuchTableError) and create_table.  Load and return created=False.
        recovered = catalog.load_table(identifier)
        return EnsuredTable(
            namespace=namespace,
            name=table_name,
            identifier=identifier,
            created=False,
            current_snapshot_id=recovered.metadata.current_snapshot_id,
        )
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
        # Step 4: unexpected failure — emit ERROR and re-raise as LakeCatalogError.
        log_service.emit_safe(
            level=LogLevel.ERROR,
            message='platform.lake.tables_ensure_failed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'namespace': '.'.join(namespace),
                    'table': table_name,
                    'error': str(exc),
                    'error_type': type(exc).__name__,
                },
                actor_component=_COMPONENT,
                target_id=f'{".".join(namespace)}.{table_name}',
            ),
        )
        raise LakeCatalogError(f'Failed to provision Iceberg table {identifier}: {exc}') from exc


def ensure_tables(catalog: Catalog, *, log_service: LogService) -> EnsureTablesResult:
    """Idempotently provision ``raw.access_artifacts`` and ``normalized.access_facts``.

    On success emits exactly one ``platform.lake.tables_ensured`` INFO log and
    returns an :class:`EnsureTablesResult`.

    On failure raises :class:`~src.platform.lake.exceptions.LakeCatalogError`
    (the ERROR log is emitted by the internal helper before the raise).

    Safe to call multiple times — second call returns ``created=False`` for both
    tables and emits a fresh summary log.
    """
    ensured: list[EnsuredTable] = []

    for identifier, schema, partition_spec in _TABLE_SPECS:
        table_result = _provision_one(catalog, identifier, schema, partition_spec, log_service)
        ensured.append(table_result)

    created_count = sum(1 for t in ensured if t.created)
    preexisting_count = len(ensured) - created_count

    log_service.emit_safe(
        level=LogLevel.INFO,
        message='platform.lake.tables_ensured',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            {
                'tables': [
                    {
                        'namespace': '.'.join(t.namespace),
                        'table': t.name,
                        'created': t.created,
                        'snapshot_id': t.current_snapshot_id,
                    }
                    for t in ensured
                ],
                'created_count': created_count,
                'preexisting_count': preexisting_count,
            },
            actor_component=_COMPONENT,
            target_id='tables',
        ),
    )

    return EnsureTablesResult(tables=tuple(ensured))
