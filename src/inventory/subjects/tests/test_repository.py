# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for subject repository CRUD functions."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.subjects.models import SubjectKind, SubjectNHIKind
from src.inventory.subjects.repository import (
    create_subject,
    create_subject_attribute,
    delete_subject_attribute,
    get_subject_by_id,
    list_subject_attributes,
    list_subjects,
    update_subject,
)

# ---------------------------------------------------------------------------
# Principal-entity helpers (mirrors test_service.py)
# ---------------------------------------------------------------------------


async def _make_employee(session):
    from src.inventory.employees.repository import create_employee as _repo_create_employee
    from src.inventory.persons.repository import create_person

    person = await create_person(session, external_id=str(uuid.uuid4()), description='test')
    await session.flush()
    emp = await _repo_create_employee(session, person_id=person.id)
    await session.flush()
    return emp


async def _make_nhi(session):
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
    from src.inventory.customers.repository import create_customer

    cust = await create_customer(session, external_id=str(uuid.uuid4()))
    await session.flush()
    return cust


# ---------------------------------------------------------------------------
# create_subject happy paths per kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_employee_subject(session_factory) -> None:
    """create_subject for employee kind persists and returns Subject."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subj = await create_subject(
            session,
            external_id='r-emp-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await session.commit()

    assert subj.id is not None
    assert subj.kind == SubjectKind.employee
    assert subj.status == 'active'
    assert subj.nhi_kind is None


@pytest.mark.asyncio
async def test_create_nhi_subject(session_factory) -> None:
    """create_subject for nhi kind persists and returns Subject."""
    async with session_factory() as session:
        nhi = await _make_nhi(session)
        subj = await create_subject(
            session,
            external_id='r-nhi-001',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.api_key,
            principal_nhi_id=nhi.id,
            status='active',
        )
        await session.commit()

    assert subj.kind == SubjectKind.nhi
    assert subj.nhi_kind == SubjectNHIKind.api_key


@pytest.mark.asyncio
async def test_create_customer_subject(session_factory) -> None:
    """create_subject for customer kind persists and returns Subject."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subj = await create_subject(
            session,
            external_id='r-cust-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.commit()

    assert subj.kind == SubjectKind.customer
    assert subj.status == 'registered'


# ---------------------------------------------------------------------------
# get_subject_by_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_id_returns_subject(session_factory) -> None:
    """get_subject_by_id returns the persisted subject."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subj = await create_subject(
            session,
            external_id='r-get-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='hired',
        )
        await session.commit()
        subj_id = subj.id

    async with session_factory() as session:
        loaded = await get_subject_by_id(session, subj_id)

    assert loaded is not None
    assert loaded.id == subj_id
    assert loaded.external_id == 'r-get-001'


@pytest.mark.asyncio
async def test_get_by_id_unknown_returns_none(session_factory) -> None:
    """get_subject_by_id returns None for unknown id."""
    async with session_factory() as session:
        result = await get_subject_by_id(session, uuid.uuid4())

    assert result is None


# ---------------------------------------------------------------------------
# list_subjects with filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_subjects_no_filter(session_factory) -> None:
    """list_subjects returns all subjects when no filter is applied."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        cust = await _make_customer(session)
        await create_subject(
            session,
            external_id='r-list-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await create_subject(
            session,
            external_id='r-list-002',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.commit()

    async with session_factory() as session:
        subjects = await list_subjects(session)

    assert len(subjects) >= 2


@pytest.mark.asyncio
async def test_list_subjects_filter_by_kind(session_factory) -> None:
    """list_subjects with kind filter returns only matching subjects."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        cust = await _make_customer(session)
        await create_subject(
            session,
            external_id='r-kind-emp',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await create_subject(
            session,
            external_id='r-kind-cust',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.commit()

    async with session_factory() as session:
        subjects = await list_subjects(session, kind=SubjectKind.employee)

    assert all(s.kind == SubjectKind.employee for s in subjects)


@pytest.mark.asyncio
async def test_list_subjects_filter_by_status(session_factory) -> None:
    """list_subjects with status filter returns only matching subjects."""
    async with session_factory() as session:
        emp1 = await _make_employee(session)
        emp2 = await _make_employee(session)
        await create_subject(
            session,
            external_id='r-status-active',
            kind=SubjectKind.employee,
            principal_employee_id=emp1.id,
            status='active',
        )
        await create_subject(
            session,
            external_id='r-status-hired',
            kind=SubjectKind.employee,
            principal_employee_id=emp2.id,
            status='hired',
        )
        await session.commit()

    async with session_factory() as session:
        subjects = await list_subjects(session, status='active')

    assert all(s.status == 'active' for s in subjects)


# ---------------------------------------------------------------------------
# update_subject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_subject_status(session_factory) -> None:
    """update_subject changes status and returns changed field names."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subj = await create_subject(
            session,
            external_id='r-upd-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='hired',
        )
        await session.commit()
        subj_id = subj.id

    async with session_factory() as session:
        loaded = await get_subject_by_id(session, subj_id)
        assert loaded is not None
        changed = await update_subject(session, loaded, status='active')
        await session.commit()

    assert 'status' in changed

    async with session_factory() as session:
        reloaded = await get_subject_by_id(session, subj_id)
    assert reloaded is not None
    assert reloaded.status == 'active'


@pytest.mark.asyncio
async def test_update_subject_noop(session_factory) -> None:
    """update_subject returns empty set when status unchanged."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subj = await create_subject(
            session,
            external_id='r-upd-noop',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await session.commit()
        subj_id = subj.id

    async with session_factory() as session:
        loaded = await get_subject_by_id(session, subj_id)
        assert loaded is not None
        changed = await update_subject(session, loaded, status='active')
        await session.commit()

    assert len(changed) == 0


# ---------------------------------------------------------------------------
# SubjectAttribute repository tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_subject_attributes_empty(session_factory) -> None:
    """list_subject_attributes returns empty list when no attributes exist."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subj = await create_subject(
            session,
            external_id='r-attr-empty-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.commit()
        subj_id = subj.id

    async with session_factory() as session:
        attrs = await list_subject_attributes(session, subj_id)

    assert attrs == []


@pytest.mark.asyncio
async def test_list_subject_attributes_returns_ordered_by_key(session_factory) -> None:
    """list_subject_attributes returns attributes ordered alphabetically by key."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subj = await create_subject(
            session,
            external_id='r-attr-order-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.flush()
        await create_subject_attribute(session, subject_id=subj.id, key='zebra', value='z')
        await create_subject_attribute(session, subject_id=subj.id, key='alpha', value='a')
        await create_subject_attribute(session, subject_id=subj.id, key='mango', value='m')
        await session.commit()
        subj_id = subj.id

    async with session_factory() as session:
        attrs = await list_subject_attributes(session, subj_id)

    assert [a.key for a in attrs] == ['alpha', 'mango', 'zebra']


@pytest.mark.asyncio
async def test_delete_subject_attribute_returns_false_when_missing(session_factory) -> None:
    """delete_subject_attribute returns False when attribute key does not exist."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subj = await create_subject(
            session,
            external_id='r-attr-del-miss-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.commit()

        result = await delete_subject_attribute(session, subj.id, 'nonexistent')

    assert result is False
