# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for master_data_apply — verifies that delta items are applied to PG."""

from __future__ import annotations

import uuid

import pytest
from src.engines.reconciliation.master_data_apply import (
    MasterDataApplyResult,
    apply_master_data_delta,
    apply_persons_delta,
)
from src.engines.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
    ReconciliationRunStatus,
)
from src.engines.reconciliation.repository import create_run


def _make_person_item(
    run_id: uuid.UUID,
    operation: ReconciliationDeltaOperation,
    *,
    before_json=None,
    after_json=None,
    entity_id=None,
) -> ReconciliationDeltaItem:
    return ReconciliationDeltaItem(
        reconciliation_run_id=run_id,
        entity_type=ReconciliationEntityType.person,
        operation=operation,
        entity_id=entity_id,
        before_json=before_json,
        after_json=after_json,
    )


@pytest.mark.asyncio
async def test_apply_person_create_inserts_row(session_factory):
    """CREATE delta → new Person row in PG with correct fields."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.inventory.persons.models import Person  # noqa: PLC0415

    async with session_factory() as session:
        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.person)
        item = _make_person_item(
            run.id,
            ReconciliationDeltaOperation.create,
            after_json={'external_id': 'EXT-APPLY-001', 'full_name': 'Alice Apply'},
        )
        session.add(item)
        await session.flush()

        result = await apply_persons_delta(session, run_id=run.id)
        await session.commit()

    assert isinstance(result, MasterDataApplyResult)
    assert result.applied_count == 1
    assert result.failed_count == 0

    async with session_factory() as session:
        row = await session.execute(sa.select(Person).where(Person.external_id == 'EXT-APPLY-001'))
        person = row.scalar_one()
        assert person.full_name == 'Alice Apply'


@pytest.mark.asyncio
async def test_apply_person_update_changes_full_name(session_factory):
    """UPDATE delta → existing Person.full_name changed in PG."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.inventory.persons.models import Person  # noqa: PLC0415

    async with session_factory() as session:
        person = Person(external_id='EXT-APPLY-002', full_name='Old Name')
        session.add(person)
        await session.flush()
        person_id = person.id

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.person)
        item = _make_person_item(
            run.id,
            ReconciliationDeltaOperation.update,
            entity_id=person_id,
            before_json={'external_id': 'EXT-APPLY-002', 'full_name': 'Old Name'},
            after_json={'external_id': 'EXT-APPLY-002', 'full_name': 'New Name'},
        )
        session.add(item)
        await session.flush()

        result = await apply_persons_delta(session, run_id=run.id)
        await session.commit()

    assert result.applied_count == 1

    async with session_factory() as session:
        row = await session.execute(sa.select(Person).where(Person.id == person_id))
        assert row.scalar_one().full_name == 'New Name'


@pytest.mark.asyncio
async def test_apply_person_revoke_marks_ignored(session_factory):
    """REVOKE delta → item marked ignored (no hard delete yet)."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.inventory.persons.models import Person  # noqa: PLC0415

    async with session_factory() as session:
        person = Person(external_id='EXT-APPLY-003', full_name='Will Survive')
        session.add(person)
        await session.flush()
        person_id = person.id

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.person)
        item = _make_person_item(
            run.id,
            ReconciliationDeltaOperation.revoke,
            entity_id=person_id,
            before_json={'external_id': 'EXT-APPLY-003', 'full_name': 'Will Survive'},
        )
        session.add(item)
        await session.flush()

        result = await apply_persons_delta(session, run_id=run.id)
        await session.commit()

    assert result.applied_count == 0
    assert result.ignored_count == 1

    async with session_factory() as session:
        row = await session.execute(sa.select(Person).where(Person.id == person_id))
        assert row.scalar_one() is not None


@pytest.mark.asyncio
async def test_apply_master_data_delta_end_to_end(session_factory):
    """High-level entrypoint: reconcile persons + apply → run.status=applied."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.inventory.persons.models import Person  # noqa: PLC0415

    async with session_factory() as session:
        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.person)
        run.status = ReconciliationRunStatus.pending_apply
        await session.flush()

        item = _make_person_item(
            run.id,
            ReconciliationDeltaOperation.create,
            after_json={'external_id': 'EXT-E2E-001', 'full_name': 'E2E Person'},
        )
        session.add(item)
        await session.flush()

        result = await apply_master_data_delta(session, run_id=run.id, entity_type=ReconciliationEntityType.person)
        await session.commit()

    assert result.applied_count == 1

    async with session_factory() as session:
        row = await session.execute(sa.select(Person).where(Person.external_id == 'EXT-E2E-001'))
        assert row.scalar_one().full_name == 'E2E Person'


@pytest.mark.asyncio
async def test_apply_master_data_delta_noop_when_already_applied(session_factory):
    """Fan-out parallel ok: apply_master_data_delta with status=applied returns zero counts without raising.

    In the 6-step pipeline, three master_data_apply steps run in parallel (person/org_unit/employee).
    The first sibling to commit advances run.status to 'applied' or 'partially_applied'.
    The remaining siblings must not raise ValueError — they should no-op gracefully.
    """
    async with session_factory() as session:
        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.person)
        run.status = ReconciliationRunStatus.applied
        await session.flush()

        result = await apply_master_data_delta(session, run_id=run.id, entity_type=ReconciliationEntityType.person)
        await session.commit()

    # No items were in pending state → all counts are zero, no exception raised
    assert result.applied_count == 0
    assert result.failed_count == 0


@pytest.mark.asyncio
async def test_apply_master_data_delta_noop_when_partially_applied(session_factory):
    """Fan-out partial ok: apply_master_data_delta with status=partially_applied returns zero counts."""
    async with session_factory() as session:
        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.person)
        run.status = ReconciliationRunStatus.partially_applied
        await session.flush()

        result = await apply_master_data_delta(session, run_id=run.id, entity_type=ReconciliationEntityType.person)
        await session.commit()

    assert result.applied_count == 0
    assert result.failed_count == 0
