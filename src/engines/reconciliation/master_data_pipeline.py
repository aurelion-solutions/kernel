# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Master data reconciliation pipeline — persons, org_units, employees.

Each pipeline follows the same five phases as the access-fact pipeline, but
targets PG tables instead of the Iceberg ``normalized.access_facts`` lake:

  1. ``_load_raw_*``       — DuckDB iceberg_scan of raw.persons / raw.org_units / raw.employees
  2. ``_normalize_*``      — noop pass-through (hook for future augmentation)
  3. ``_load_current_*``   — SELECT from PG persons / org_units / employees
  4. ``_compute_delta_*``  — set-diff → create / update / revoke
  5. ``_persist_delta``    — bulk insert ReconciliationDeltaItem rows (entity_type = person|org_unit|employee)

``before_json`` / ``after_json`` carry raw snapshots so the apply phase (Phase 4)
has all information needed to write to PG without re-querying the lake.

Design decisions:
- All parent / org-unit UUID resolution is deferred to the apply phase.
  The pipeline stores external_ids in the json snapshots.
- Normalization is a noop; the hook exists for future augmentation steps.
- ``entity_id`` is set to the existing PG row UUID for UPDATE / REVOKE,
  and NULL for CREATE (no PG row exists yet).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from src.engines.reconciliation.repository import (
    RunCounts,
    bulk_insert_delta_items,
    update_run_status,
)
from src.platform.lake.duckdb_session import LakeSession

_FETCH_BATCH = 5_000


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MasterDataReconciliationResult:
    """Counts returned by each master data pipeline."""

    run_id: uuid.UUID
    entity_type: ReconciliationEntityType
    created_count: int
    updated_count: int
    revoked_count: int
    unchanged_count: int


# ---------------------------------------------------------------------------
# Generic delta-item builder
# ---------------------------------------------------------------------------


def _make_delta_item(
    *,
    run_id: uuid.UUID,
    entity_type: ReconciliationEntityType,
    operation: ReconciliationDeltaOperation,
    entity_id: uuid.UUID | None,
    before_json: dict[str, Any] | None,
    after_json: dict[str, Any] | None,
) -> ReconciliationDeltaItem:
    return ReconciliationDeltaItem(
        reconciliation_run_id=run_id,
        entity_type=entity_type,
        operation=operation,
        entity_id=entity_id,
        before_json=before_json,
        after_json=after_json,
    )


# ---------------------------------------------------------------------------
# DuckDB helpers
# ---------------------------------------------------------------------------


def _fetch_all_batched(lake_session: LakeSession, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
    lake_session.execute(sql, params)
    rows: list[tuple[Any, ...]] = []
    while True:
        batch = lake_session._conn.fetchmany(_FETCH_BATCH)  # noqa: SLF001
        if not batch:
            break
        rows.extend(batch)
    return rows


# ---------------------------------------------------------------------------
# PERSONS pipeline
# ---------------------------------------------------------------------------


async def run_persons_reconciliation(
    session: AsyncSession,
    lake_session: LakeSession,
    *,
    run: ReconciliationRun,
) -> MasterDataReconciliationResult:
    """Reconcile raw.persons → PG persons table.

    Key:        external_id
    Comparison: full_name
    """
    from src.inventory.persons.models import Person  # noqa: PLC0415

    run_id = run.id

    # Phase 1: load raw lake data
    table_path = lake_session.iceberg_table_path('raw', 'persons')
    sql = f"SELECT id, external_id, full_name FROM iceberg_scan('{table_path}') WHERE is_active = true"
    lake_rows = _fetch_all_batched(lake_session, sql, [])
    # {external_id: {external_id, full_name}}
    lake_map: dict[str, dict[str, Any]] = {row[1]: {'external_id': row[1], 'full_name': row[2]} for row in lake_rows}

    # Phase 2: normalize (noop)
    normalized = lake_map

    # Phase 3: load current PG state
    result = await session.execute(sa.select(Person.id, Person.external_id, Person.full_name))
    pg_rows = result.all()
    pg_map: dict[str, dict[str, Any]] = {
        row.external_id: {'id': row.id, 'external_id': row.external_id, 'full_name': row.full_name} for row in pg_rows
    }

    # Phase 4: compute delta
    lake_keys = set(normalized.keys())
    pg_keys = set(pg_map.keys())

    created_keys = lake_keys - pg_keys
    common_keys = lake_keys & pg_keys
    revoked_keys = pg_keys - lake_keys

    items: list[ReconciliationDeltaItem] = []
    created_count = updated_count = revoked_count = unchanged_count = 0

    for key in created_keys:
        items.append(
            _make_delta_item(
                run_id=run_id,
                entity_type=ReconciliationEntityType.person,
                operation=ReconciliationDeltaOperation.create,
                entity_id=None,
                before_json=None,
                after_json=normalized[key],
            )
        )
        created_count += 1

    for key in common_keys:
        lake_row = normalized[key]
        pg_row = pg_map[key]
        if lake_row['full_name'] != pg_row['full_name']:
            items.append(
                _make_delta_item(
                    run_id=run_id,
                    entity_type=ReconciliationEntityType.person,
                    operation=ReconciliationDeltaOperation.update,
                    entity_id=pg_row['id'],
                    before_json={'external_id': pg_row['external_id'], 'full_name': pg_row['full_name']},
                    after_json=lake_row,
                )
            )
            updated_count += 1
        else:
            unchanged_count += 1

    for key in revoked_keys:
        pg_row = pg_map[key]
        items.append(
            _make_delta_item(
                run_id=run_id,
                entity_type=ReconciliationEntityType.person,
                operation=ReconciliationDeltaOperation.revoke,
                entity_id=pg_row['id'],
                before_json={'external_id': pg_row['external_id'], 'full_name': pg_row['full_name']},
                after_json=None,
            )
        )
        revoked_count += 1

    # Phase 5: persist delta
    if items:
        await bulk_insert_delta_items(session, items)

    return MasterDataReconciliationResult(
        run_id=run_id,
        entity_type=ReconciliationEntityType.person,
        created_count=created_count,
        updated_count=updated_count,
        revoked_count=revoked_count,
        unchanged_count=unchanged_count,
    )


# ---------------------------------------------------------------------------
# ORG UNITS pipeline
# ---------------------------------------------------------------------------


async def run_org_units_reconciliation(
    session: AsyncSession,
    lake_session: LakeSession,
    *,
    run: ReconciliationRun,
) -> MasterDataReconciliationResult:
    """Reconcile raw.org_units → PG org_units table.

    Key:        external_id
    Comparison: name, parent_external_id
    Note:       parent_id ↔ parent_external_id resolution is deferred to apply phase.
    """
    from src.inventory.org_units.models import OrgUnit  # noqa: PLC0415

    run_id = run.id

    # Phase 1: load raw lake data
    table_path = lake_session.iceberg_table_path('raw', 'org_units')
    sql = f"SELECT external_id, name, parent_external_id FROM iceberg_scan('{table_path}') WHERE is_active = true"
    lake_rows = _fetch_all_batched(lake_session, sql, [])
    lake_map: dict[str, dict[str, Any]] = {
        row[0]: {'external_id': row[0], 'name': row[1], 'parent_external_id': row[2]} for row in lake_rows
    }

    # Phase 2: normalize (noop)
    normalized = lake_map

    # Phase 3: load current PG state
    # Self-join to resolve parent external_id for comparison.
    parent = sa.orm.aliased(OrgUnit)
    stmt = sa.select(
        OrgUnit.id,
        OrgUnit.external_id,
        OrgUnit.name,
        parent.external_id.label('parent_external_id'),
    ).outerjoin(parent, OrgUnit.parent_id == parent.id)
    result = await session.execute(stmt)
    pg_rows = result.all()
    pg_map: dict[str, dict[str, Any]] = {
        row.external_id: {
            'id': row.id,
            'external_id': row.external_id,
            'name': row.name,
            'parent_external_id': row.parent_external_id,
        }
        for row in pg_rows
    }

    # Phase 4: compute delta
    lake_keys = set(normalized.keys())
    pg_keys = set(pg_map.keys())

    items: list[ReconciliationDeltaItem] = []
    created_count = updated_count = revoked_count = unchanged_count = 0

    for key in lake_keys - pg_keys:
        items.append(
            _make_delta_item(
                run_id=run_id,
                entity_type=ReconciliationEntityType.org_unit,
                operation=ReconciliationDeltaOperation.create,
                entity_id=None,
                before_json=None,
                after_json=normalized[key],
            )
        )
        created_count += 1

    for key in lake_keys & pg_keys:
        lake_row = normalized[key]
        pg_row = pg_map[key]
        if lake_row['name'] != pg_row['name'] or lake_row['parent_external_id'] != pg_row['parent_external_id']:
            items.append(
                _make_delta_item(
                    run_id=run_id,
                    entity_type=ReconciliationEntityType.org_unit,
                    operation=ReconciliationDeltaOperation.update,
                    entity_id=pg_row['id'],
                    before_json={k: v for k, v in pg_row.items() if k != 'id'},
                    after_json=lake_row,
                )
            )
            updated_count += 1
        else:
            unchanged_count += 1

    for key in pg_keys - lake_keys:
        pg_row = pg_map[key]
        items.append(
            _make_delta_item(
                run_id=run_id,
                entity_type=ReconciliationEntityType.org_unit,
                operation=ReconciliationDeltaOperation.revoke,
                entity_id=pg_row['id'],
                before_json={k: v for k, v in pg_row.items() if k != 'id'},
                after_json=None,
            )
        )
        revoked_count += 1

    if items:
        await bulk_insert_delta_items(session, items)

    return MasterDataReconciliationResult(
        run_id=run_id,
        entity_type=ReconciliationEntityType.org_unit,
        created_count=created_count,
        updated_count=updated_count,
        revoked_count=revoked_count,
        unchanged_count=unchanged_count,
    )


# ---------------------------------------------------------------------------
# EMPLOYEES pipeline
# ---------------------------------------------------------------------------


async def run_employees_reconciliation(
    session: AsyncSession,
    lake_session: LakeSession,
    *,
    run: ReconciliationRun,
) -> MasterDataReconciliationResult:
    """Reconcile raw.employees → PG employees table.

    Key:        person_external_id
    Comparison: is_locked, description, org_unit_external_id
    Note:       person_id / org_unit_id UUID resolution is deferred to apply phase.
    """
    from src.inventory.employees.models import Employee  # noqa: PLC0415
    from src.inventory.org_units.models import OrgUnit  # noqa: PLC0415
    from src.inventory.persons.models import Person  # noqa: PLC0415

    run_id = run.id

    # Phase 1: load raw lake data
    table_path = lake_session.iceberg_table_path('raw', 'employees')
    sql = (
        f'SELECT person_external_id, org_unit_external_id, is_locked, description, attributes '
        f"FROM iceberg_scan('{table_path}') "
        'WHERE is_active = true'
    )
    lake_rows = _fetch_all_batched(lake_session, sql, [])
    lake_map: dict[str, dict[str, Any]] = {
        row[0]: {
            'person_external_id': row[0],
            'org_unit_external_id': row[1],
            'is_locked': row[2],
            'description': row[3],
            'attributes': row[4],
        }
        for row in lake_rows
    }

    # Phase 2: normalize (noop)
    normalized = lake_map

    # Phase 3: load current PG state — join employees ↔ persons ↔ org_units
    ou = sa.orm.aliased(OrgUnit)
    stmt = (
        sa.select(
            Employee.id,
            Person.external_id.label('person_external_id'),
            Employee.is_locked,
            Employee.description,
            ou.external_id.label('org_unit_external_id'),
        )
        .join(Person, Employee.person_id == Person.id)
        .outerjoin(ou, Employee.org_unit_id == ou.id)
    )
    result = await session.execute(stmt)
    pg_rows = result.all()
    pg_map: dict[str, dict[str, Any]] = {
        row.person_external_id: {
            'id': row.id,
            'person_external_id': row.person_external_id,
            'is_locked': row.is_locked,
            'description': row.description,
            'org_unit_external_id': row.org_unit_external_id,
        }
        for row in pg_rows
    }

    # Phase 4: compute delta
    lake_keys = set(normalized.keys())
    pg_keys = set(pg_map.keys())

    items: list[ReconciliationDeltaItem] = []
    created_count = updated_count = revoked_count = unchanged_count = 0

    for key in lake_keys - pg_keys:
        items.append(
            _make_delta_item(
                run_id=run_id,
                entity_type=ReconciliationEntityType.employee,
                operation=ReconciliationDeltaOperation.create,
                entity_id=None,
                before_json=None,
                after_json=normalized[key],
            )
        )
        created_count += 1

    for key in lake_keys & pg_keys:
        lake_row = normalized[key]
        pg_row = pg_map[key]
        if (
            lake_row['is_locked'] != pg_row['is_locked']
            or lake_row['description'] != pg_row['description']
            or lake_row['org_unit_external_id'] != pg_row['org_unit_external_id']
        ):
            items.append(
                _make_delta_item(
                    run_id=run_id,
                    entity_type=ReconciliationEntityType.employee,
                    operation=ReconciliationDeltaOperation.update,
                    entity_id=pg_row['id'],
                    before_json={k: v for k, v in pg_row.items() if k != 'id'},
                    after_json=lake_row,
                )
            )
            updated_count += 1
        else:
            unchanged_count += 1

    for key in pg_keys - lake_keys:
        pg_row = pg_map[key]
        items.append(
            _make_delta_item(
                run_id=run_id,
                entity_type=ReconciliationEntityType.employee,
                operation=ReconciliationDeltaOperation.revoke,
                entity_id=pg_row['id'],
                before_json={k: v for k, v in pg_row.items() if k != 'id'},
                after_json=None,
            )
        )
        revoked_count += 1

    if items:
        await bulk_insert_delta_items(session, items)

    return MasterDataReconciliationResult(
        run_id=run_id,
        entity_type=ReconciliationEntityType.employee,
        created_count=created_count,
        updated_count=updated_count,
        revoked_count=revoked_count,
        unchanged_count=unchanged_count,
    )


# ---------------------------------------------------------------------------
# High-level entrypoints
# ---------------------------------------------------------------------------


async def run_master_data_reconciliation(
    session: AsyncSession,
    lake_session: LakeSession,
    *,
    entity_type: ReconciliationEntityType,
) -> MasterDataReconciliationResult:
    """Create a ReconciliationRun and execute the appropriate pipeline.

    Always ends in ``pending_apply`` — the caller is responsible for triggering
    the apply step via POST /reconciliation/master-data/runs/{id}/apply.
    """
    from src.engines.reconciliation.repository import create_run  # noqa: PLC0415

    if entity_type == ReconciliationEntityType.access_fact:
        raise ValueError('Use run_reconciliation() for access_fact entity type')

    run = await create_run(session, application_id=None, entity_type=entity_type)
    await session.flush()

    try:
        dispatch = {
            ReconciliationEntityType.person: run_persons_reconciliation,
            ReconciliationEntityType.org_unit: run_org_units_reconciliation,
            ReconciliationEntityType.employee: run_employees_reconciliation,
        }
        pipeline_fn = dispatch[entity_type]
        result = await pipeline_fn(session, lake_session, run=run)

        await update_run_status(
            session,
            run.id,
            status=ReconciliationRunStatus.pending_apply,
            counts=RunCounts(
                created=result.created_count,
                updated=result.updated_count,
                revoked=result.revoked_count,
                unchanged=result.unchanged_count,
            ),
        )
    except Exception as exc:
        await update_run_status(session, run.id, status=ReconciliationRunStatus.failed, error=str(exc))
        raise

    return result
