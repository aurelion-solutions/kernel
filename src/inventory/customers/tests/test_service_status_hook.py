# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for CustomerService -> SubjectService.recompute_status_for_principal hook."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.customers.schemas import CustomerPatch
from src.inventory.customers.service import CustomerService
from src.inventory.subjects.models import SubjectKind
from src.inventory.subjects.service import SubjectService
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / 'logs.jsonl'


@pytest.fixture
def log_service(log_path: Path) -> LogService:
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_path))
    return LogService(factory=factory, provider_name='file')


@pytest.fixture
def subject_service(log_service: LogService) -> SubjectService:
    return SubjectService(log_service=log_service)


@pytest.fixture
def service(log_service: LogService, subject_service: SubjectService) -> CustomerService:
    """CustomerService sharing one LogService with SubjectService."""
    return CustomerService(log_service=log_service, subject_service=subject_service)


def _events_of_type(log_path: Path, event_type: str) -> list[dict]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text().strip().split('\n')
        if line.strip() and json.loads(line).get('event_type') == event_type
    ]


@pytest.mark.asyncio
async def test_update_customer_locks_flips_subject_status_to_suspended(
    service: CustomerService,
    session_factory,
    log_path: Path,
) -> None:
    """PATCH is_locked=True on Customer with a bound verified Subject -> Subject.status=suspended.

    Also asserts event order: customer.updated emitted before subject.status_changed.
    """
    async with session_factory() as session:
        from src.inventory.customers.repository import create_customer
        from src.inventory.subjects.repository import create_subject

        customer = await create_customer(
            session,
            external_id=str(uuid.uuid4()),
            email_verified=True,
            is_locked=False,
        )
        await session.flush()

        subject = await create_subject(
            session,
            external_id=str(uuid.uuid4()),
            kind=SubjectKind.customer,
            principal_customer_id=customer.id,
            status='verified',
        )
        await session.commit()
        customer_id = customer.id
        subject_id = subject.id

    async with session_factory() as session:
        patch = CustomerPatch(is_locked=True)
        await service.update_customer(session, customer_id, patch)
        await session.commit()

    # Assert Subject row in DB updated
    async with session_factory() as session:
        from src.inventory.subjects.repository import get_subject_by_id

        updated_subject = await get_subject_by_id(session, subject_id)
        assert updated_subject is not None
        assert updated_subject.status == 'suspended'

    # Assert event emission
    customer_updated = _events_of_type(log_path, 'customer.updated')
    status_changed = _events_of_type(log_path, 'subject.status_changed')

    assert len(customer_updated) >= 1
    assert 'is_locked' in customer_updated[-1]['payload']['changed_fields']

    assert len(status_changed) == 1
    assert status_changed[0]['payload']['previous_status'] == 'verified'
    assert status_changed[0]['payload']['new_status'] == 'suspended'
    assert 'subject_id' in status_changed[0]['payload']

    # Assert order: customer.updated line appears before subject.status_changed line in log file
    all_lines = log_path.read_text().strip().split('\n')
    all_events = [json.loads(line) for line in all_lines if line.strip()]
    types_in_order = [e['event_type'] for e in all_events]
    customer_updated_idx = next(i for i, t in enumerate(types_in_order) if t == 'customer.updated')
    status_changed_idx = next(i for i, t in enumerate(types_in_order) if t == 'subject.status_changed')
    assert customer_updated_idx < status_changed_idx


@pytest.mark.asyncio
async def test_update_customer_no_status_relevant_change_emits_no_status_event(
    service: CustomerService,
    session_factory,
    log_path: Path,
) -> None:
    """PATCH plan_tier does not trigger subject.status_changed (plan_tier not in guard set)."""
    from src.inventory.customers.models import CustomerPlanTier

    async with session_factory() as session:
        from src.inventory.customers.repository import create_customer
        from src.inventory.subjects.repository import create_subject

        customer = await create_customer(
            session,
            external_id=str(uuid.uuid4()),
            email_verified=True,
            is_locked=False,
        )
        await session.flush()

        await create_subject(
            session,
            external_id=str(uuid.uuid4()),
            kind=SubjectKind.customer,
            principal_customer_id=customer.id,
            status='verified',
        )
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        patch = CustomerPatch(plan_tier=CustomerPlanTier.pro)
        await service.update_customer(session, customer_id, patch)
        await session.commit()

    # customer.updated should be emitted (plan_tier changed)
    customer_updated = _events_of_type(log_path, 'customer.updated')
    assert len(customer_updated) >= 1

    # subject.status_changed must NOT be emitted
    status_changed = _events_of_type(log_path, 'subject.status_changed')
    assert len(status_changed) == 0
