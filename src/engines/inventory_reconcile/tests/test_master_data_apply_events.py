# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Per-row event emission from master_data_apply (Phase 20 K-B + K-G).

Covers:
- apply_persons_delta create → inventory.person.created
- apply_org_units_delta create → inventory.org_unit.created
- apply_employees_delta create → inventory.employee.created
- apply_accounts_delta revoke → inventory.account.updated with status change
- apply_employees_delta update with attribute change → inventory.employee.updated
  carrying changes["attributes.<key>"]
"""

from __future__ import annotations

import uuid

import pytest
from src.engines.inventory_reconcile.master_data_apply import (
    apply_accounts_delta,
    apply_employees_delta,
    apply_org_units_delta,
    apply_persons_delta,
)
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
)
from src.engines.inventory_reconcile.repository import create_run
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService


async def _seed_run(session, entity_type: ReconciliationEntityType = ReconciliationEntityType.person):
    return await create_run(session, application_id=None, entity_type=entity_type)


@pytest.mark.asyncio
async def test_apply_persons_delta_emits_created(session_factory) -> None:
    capturing = CapturingEventService()
    events = EventService(sink=capturing)

    async with session_factory() as session:
        run = await _seed_run(session)
        item = ReconciliationDeltaItem(
            id=uuid.uuid4(),
            reconciliation_run_id=run.id,
            entity_type=ReconciliationEntityType.person,
            operation=ReconciliationDeltaOperation.create,
            status=ReconciliationDeltaItemStatus.pending,
            after_json={'external_id': 'p-evt-1', 'full_name': 'Ada Lovelace'},
        )
        session.add(item)
        await session.flush()

        await apply_persons_delta(session, run_id=run.id, event_service=events)
        await session.commit()

    emitted = capturing.filter_by_type('inventory.person.created')
    assert len(emitted) == 1
    payload = emitted[0].payload
    assert payload['external_id'] == 'p-evt-1'
    assert payload['full_name'] == 'Ada Lovelace'
    assert 'entity_id' in payload


@pytest.mark.asyncio
async def test_apply_org_units_delta_emits_created(session_factory) -> None:
    capturing = CapturingEventService()
    events = EventService(sink=capturing)

    async with session_factory() as session:
        run = await _seed_run(session, ReconciliationEntityType.org_unit)
        item = ReconciliationDeltaItem(
            id=uuid.uuid4(),
            reconciliation_run_id=run.id,
            entity_type=ReconciliationEntityType.org_unit,
            operation=ReconciliationDeltaOperation.create,
            status=ReconciliationDeltaItemStatus.pending,
            after_json={'external_id': 'ou-evt-1', 'name': 'Engineering'},
        )
        session.add(item)
        await session.flush()

        await apply_org_units_delta(session, run_id=run.id, event_service=events)
        await session.commit()

    emitted = capturing.filter_by_type('inventory.org_unit.created')
    assert len(emitted) == 1
    payload = emitted[0].payload
    assert payload['external_id'] == 'ou-evt-1'
    assert payload['name'] == 'Engineering'


@pytest.mark.asyncio
async def test_apply_employees_delta_emits_created_with_subject_ref(session_factory) -> None:
    from src.inventory.persons.repository import create_person  # noqa: PLC0415

    capturing = CapturingEventService()
    events = EventService(sink=capturing)

    async with session_factory() as session:
        await create_person(session, external_id='p-emp-evt-1', full_name='Bob')
        run = await _seed_run(session, ReconciliationEntityType.employee)
        item = ReconciliationDeltaItem(
            id=uuid.uuid4(),
            reconciliation_run_id=run.id,
            entity_type=ReconciliationEntityType.employee,
            operation=ReconciliationDeltaOperation.create,
            status=ReconciliationDeltaItemStatus.pending,
            after_json={
                'person_external_id': 'p-emp-evt-1',
                'is_locked': False,
                'description': 'new hire',
                'attributes': {'role': 'engineer', 'employment_status': 'active'},
            },
        )
        session.add(item)
        await session.flush()

        await apply_employees_delta(session, run_id=run.id, event_service=events)
        await session.commit()

    emitted = capturing.filter_by_type('inventory.employee.created')
    assert len(emitted) == 1
    payload = emitted[0].payload
    assert payload['subject_type'] == 'employee'
    assert payload['attributes'] == {'role': 'engineer', 'employment_status': 'active'}
    # subject_ref is Subject.id — must differ from employee id (entity_id)
    employee_id = payload['entity_id']
    subject_id_in_event = payload['subject_ref']
    assert subject_id_in_event != employee_id


@pytest.mark.asyncio
async def test_apply_employees_delta_update_emits_attributes_change(session_factory) -> None:
    from src.inventory.employees.models import EmployeeAttribute  # noqa: PLC0415
    from src.inventory.employees.repository import create_employee  # noqa: PLC0415
    from src.inventory.persons.repository import create_person  # noqa: PLC0415

    capturing = CapturingEventService()
    events = EventService(sink=capturing)

    async with session_factory() as session:
        person = await create_person(session, external_id='p-emp-upd-1', full_name='Carla')
        employee = await create_employee(session, person_id=person.id, is_locked=False)
        # Seed an existing attribute the update will change.
        session.add(EmployeeAttribute(employee_id=employee.id, key='employment_status', value='active'))
        await session.flush()

        run = await _seed_run(session, ReconciliationEntityType.employee)
        item = ReconciliationDeltaItem(
            id=uuid.uuid4(),
            reconciliation_run_id=run.id,
            entity_type=ReconciliationEntityType.employee,
            entity_id=employee.id,
            operation=ReconciliationDeltaOperation.update,
            status=ReconciliationDeltaItemStatus.pending,
            before_json={
                'is_locked': False,
                'description': None,
                'attributes': {'employment_status': 'active'},
            },
            after_json={
                'is_locked': False,
                'description': None,
                'attributes': {'employment_status': 'on_leave'},
            },
        )
        session.add(item)
        await session.flush()

        await apply_employees_delta(session, run_id=run.id, event_service=events)
        await session.commit()

    emitted = capturing.filter_by_type('inventory.employee.updated')
    assert len(emitted) == 1
    payload = emitted[0].payload
    assert payload['subject_type'] == 'employee'
    assert payload['changes'] == {
        'attributes.employment_status': {'old': 'active', 'new': 'on_leave'},
    }
    # subject_ref must be Subject.id (distinct from employee id / entity_id)
    assert payload['subject_ref'] != payload['entity_id']


@pytest.mark.asyncio
async def test_apply_accounts_delta_revoke_emits_status_change(session_factory) -> None:
    from src.inventory.accounts.models import Account, AccountStatus  # noqa: PLC0415
    from src.platform.applications.models import Application  # noqa: PLC0415

    capturing = CapturingEventService()
    events = EventService(sink=capturing)

    async with session_factory() as session:
        app = Application(id=uuid.uuid4(), code='app-evt', name='App Evt')
        session.add(app)
        await session.flush()
        account = Account(application_id=app.id, username='alice', status=AccountStatus.active)
        session.add(account)
        await session.flush()

        run = await _seed_run(session, ReconciliationEntityType.account)
        item = ReconciliationDeltaItem(
            id=uuid.uuid4(),
            reconciliation_run_id=run.id,
            entity_type=ReconciliationEntityType.account,
            entity_id=account.id,
            operation=ReconciliationDeltaOperation.revoke,
            status=ReconciliationDeltaItemStatus.pending,
        )
        session.add(item)
        await session.flush()

        await apply_accounts_delta(session, run_id=run.id, event_service=events)
        await session.commit()

    emitted = capturing.filter_by_type('inventory.account.updated')
    assert len(emitted) == 1
    assert emitted[0].payload['changes'] == {
        'status': {'old': AccountStatus.active.value, 'new': AccountStatus.disabled.value},
    }


@pytest.mark.asyncio
async def test_default_noop_emits_nothing(session_factory) -> None:
    """When no event_service is passed, the delta applies but no events fire."""
    capturing = CapturingEventService()  # never wired
    async with session_factory() as session:
        run = await _seed_run(session)
        item = ReconciliationDeltaItem(
            id=uuid.uuid4(),
            reconciliation_run_id=run.id,
            entity_type=ReconciliationEntityType.person,
            operation=ReconciliationDeltaOperation.create,
            status=ReconciliationDeltaItemStatus.pending,
            after_json={'external_id': 'p-noop-1', 'full_name': 'Silent'},
        )
        session.add(item)
        await session.flush()

        result = await apply_persons_delta(session, run_id=run.id)
        await session.commit()

    assert result.applied_count == 1
    assert capturing.emitted == []
