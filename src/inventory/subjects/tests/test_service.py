# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for SubjectService."""

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.subjects.models import SubjectKind, SubjectNHIKind
from src.inventory.subjects.schemas import SubjectPatch
from src.inventory.subjects.service import (
    DuplicateSubjectAttributeError,
    InvalidSubjectStatusForKindError,
    SubjectAttributeNotFoundError,
    SubjectNotFoundError,
    SubjectService,
)
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
def service(log_service: LogService) -> SubjectService:
    return SubjectService(log_service=log_service)


async def _make_employee(session):
    """Create a Person then Employee, return employee."""
    from src.inventory.employees.repository import create_employee as _repo_create_employee
    from src.inventory.persons.repository import create_person

    person = await create_person(session, external_id=str(uuid.uuid4()), description='test')
    await session.flush()
    emp = await _repo_create_employee(session, person_id=person.id)
    await session.flush()
    return emp


async def _make_nhi(session):
    """Create an NHI, return it."""
    from src.inventory.nhi.repository import create_nhi

    nhi = await create_nhi(
        session,
        external_id=str(uuid.uuid4()),
        name='test-nhi',
        kind='service_account',
    )
    await session.flush()
    return nhi


async def _make_customer(session):
    """Create a Customer, return it."""
    from src.inventory.customers.repository import create_customer

    cust = await create_customer(session, external_id=str(uuid.uuid4()))
    await session.flush()
    return cust


@pytest.mark.asyncio
async def test_create_subject_employee_emits_event(
    service: SubjectService,
    session_factory,
    log_path: Path,
) -> None:
    """create_subject for employee kind emits subject.created."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-emp-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await session.commit()

    assert subject.id is not None
    assert subject.kind == SubjectKind.employee
    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'subject.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'inventory.subjects'
    assert 'subject_id' in created[-1]['payload']
    assert created[-1]['payload']['kind'] == 'employee'
    assert created[-1]['payload']['status'] == 'active'


@pytest.mark.asyncio
async def test_create_subject_nhi_emits_event(
    service: SubjectService,
    session_factory,
    log_path: Path,
) -> None:
    """create_subject for nhi kind emits subject.created with nhi_kind."""
    async with session_factory() as session:
        nhi = await _make_nhi(session)
        subject = await service.create_subject(
            session,
            external_id='subj-nhi-001',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.service_account,
            principal_nhi_id=nhi.id,
            status='active',
        )
        await session.commit()

    assert subject.nhi_kind == SubjectNHIKind.service_account
    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'subject.created']
    assert len(created) >= 1
    assert created[-1]['payload']['nhi_kind'] == 'service_account'


@pytest.mark.asyncio
async def test_get_subject_emits_retrieved(
    service: SubjectService,
    session_factory,
    log_path: Path,
) -> None:
    """get_subject emits subject.retrieved when found."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-get-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='hired',
        )
        await session.commit()
        subject_id = subject.id

    async with session_factory() as session:
        await service.get_subject(session, subject_id)

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'subject.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['component'] == 'inventory.subjects'


@pytest.mark.asyncio
async def test_get_subject_returns_none_when_missing(
    service: SubjectService,
    session_factory,
) -> None:
    async with session_factory() as session:
        result = await service.get_subject(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_update_subject_status_emits_event(
    service: SubjectService,
    session_factory,
    log_path: Path,
) -> None:
    """update_subject with status change emits subject.updated with changed_fields."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-upd-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='hired',
        )
        await session.commit()
        subject_id = subject.id

    async with session_factory() as session:
        patch = SubjectPatch(status='active')
        await service.update_subject(session, subject_id, patch)
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    updated = [r for r in records if r.get('event_type') == 'subject.updated']
    assert len(updated) >= 1
    assert 'status' in updated[-1]['payload']['changed_fields']


@pytest.mark.asyncio
async def test_update_subject_noop_no_event(
    service: SubjectService,
    session_factory,
    log_path: Path,
) -> None:
    """update_subject with no changes does not emit subject.updated."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-noop-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await session.commit()
        subject_id = subject.id

    async with session_factory() as session:
        patch = SubjectPatch(status='active')
        await service.update_subject(session, subject_id, patch)
        await session.commit()

    if log_path.exists():
        lines = log_path.read_text().strip().split('\n')
        records = [json.loads(line) for line in lines if line.strip()]
        updated = [r for r in records if r.get('event_type') == 'subject.updated']
        assert len(updated) == 0


@pytest.mark.asyncio
async def test_update_subject_not_found_raises(
    service: SubjectService,
    session_factory,
) -> None:
    with pytest.raises(SubjectNotFoundError):
        async with session_factory() as session:
            await service.update_subject(session, uuid.uuid4(), SubjectPatch(status='active'))


@pytest.mark.asyncio
async def test_update_subject_invalid_status_for_kind(
    service: SubjectService,
    session_factory,
) -> None:
    """update_subject rejects status incompatible with subject kind."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-badstatus-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await session.commit()
        subject_id = subject.id

    with pytest.raises(InvalidSubjectStatusForKindError):
        async with session_factory() as session:
            patch = SubjectPatch(status='expired')  # NHI status, not employee
            await service.update_subject(session, subject_id, patch)


@pytest.mark.asyncio
async def test_no_status_changed_event_emitted(
    service: SubjectService,
    session_factory,
    log_path: Path,
) -> None:
    """subject.status_changed must NOT be emitted in Step 2 (reserved for Step 15)."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-sc-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='hired',
        )
        await session.commit()
        subject_id = subject.id

    async with session_factory() as session:
        await service.update_subject(session, subject_id, SubjectPatch(status='active'))
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    status_changed = [r for r in records if r.get('event_type') == 'subject.status_changed']
    assert len(status_changed) == 0


# ---------------------------------------------------------------------------
# SubjectAttribute service tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_attribute_happy_path_emits_subject_attribute_added(
    service: SubjectService,
    session_factory,
    log_path: Path,
) -> None:
    """add_attribute emits subject.attribute.added with correct payload."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-attr-add-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.flush()
        await service.add_attribute(session, subject.id, 'cost_center', 'cc-42')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    added = [r for r in records if r.get('event_type') == 'subject.attribute.added']
    assert len(added) >= 1
    assert added[-1]['component'] == 'inventory.subjects'
    assert added[-1]['payload']['key'] == 'cost_center'
    assert 'subject_id' in added[-1]['payload']


@pytest.mark.asyncio
async def test_add_attribute_duplicate_raises_DuplicateSubjectAttributeError(
    service: SubjectService,
    session_factory,
) -> None:
    """add_attribute raises DuplicateSubjectAttributeError on duplicate (subject_id, key)."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-attr-dup-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.flush()
        await service.add_attribute(session, subject.id, 'same_key', 'v1')
        await session.commit()
        subject_id = subject.id

    with pytest.raises(DuplicateSubjectAttributeError):
        async with session_factory() as session:
            from src.inventory.subjects.repository import get_subject_by_id

            subj = await get_subject_by_id(session, subject_id)
            assert subj is not None
            await service.add_attribute(session, subj.id, 'same_key', 'v2')
            await session.commit()


@pytest.mark.asyncio
async def test_add_attribute_unknown_subject_raises_SubjectNotFoundError(
    service: SubjectService,
    session_factory,
) -> None:
    """add_attribute raises SubjectNotFoundError when subject does not exist."""
    with pytest.raises(SubjectNotFoundError):
        async with session_factory() as session:
            await service.add_attribute(session, uuid.uuid4(), 'any_key', 'v')


@pytest.mark.asyncio
async def test_remove_attribute_happy_path_emits_subject_attribute_removed(
    service: SubjectService,
    session_factory,
    log_path: Path,
) -> None:
    """remove_attribute emits subject.attribute.removed."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-attr-rm-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.flush()
        await service.add_attribute(session, subject.id, 'to_remove', 'x')
        await session.commit()
        subject_id = subject.id

    async with session_factory() as session:
        await service.remove_attribute(session, subject_id, 'to_remove')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    removed = [r for r in records if r.get('event_type') == 'subject.attribute.removed']
    assert len(removed) >= 1
    assert removed[-1]['payload']['key'] == 'to_remove'
    assert 'subject_id' in removed[-1]['payload']


@pytest.mark.asyncio
async def test_remove_attribute_missing_key_raises_SubjectAttributeNotFoundError(
    service: SubjectService,
    session_factory,
) -> None:
    """remove_attribute raises SubjectAttributeNotFoundError when key does not exist."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-attr-rmm-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.commit()
        subject_id = subject.id

    with pytest.raises(SubjectAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, subject_id, 'nonexistent')


@pytest.mark.asyncio
async def test_list_attributes_does_not_emit(
    service: SubjectService,
    session_factory,
    log_path: Path,
) -> None:
    """list_attributes does not emit any events."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-attr-list-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.flush()
        await service.add_attribute(session, subject.id, 'k1', 'v1')
        await session.commit()
        subject_id = subject.id

    count_before = len(log_path.read_text().strip().split('\n')) if log_path.exists() else 0

    async with session_factory() as session:
        await service.list_attributes(session, subject_id)

    count_after = len(log_path.read_text().strip().split('\n')) if log_path.exists() else 0
    assert count_after == count_before
