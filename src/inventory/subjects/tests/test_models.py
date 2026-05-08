# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for Subject model constraints and partial-unique indexes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.subjects.models import Subject, SubjectAttribute, SubjectKind, SubjectNHIKind

# ---------------------------------------------------------------------------
# Helpers — build principal entities in DB
# ---------------------------------------------------------------------------


async def _make_employee(session):
    from src.inventory.employees.repository import create_employee as _repo_create_employee
    from src.inventory.persons.repository import create_person

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
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
# Happy-path creates — one per kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_employee_subject(session_factory) -> None:
    """Happy path: employee subject persists without error."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subj = Subject(
            external_id='m-emp-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        session.add(subj)
        await session.flush()
        assert subj.id is not None


@pytest.mark.asyncio
async def test_create_nhi_subject(session_factory) -> None:
    """Happy path: nhi subject persists without error."""
    async with session_factory() as session:
        nhi = await _make_nhi(session)
        subj = Subject(
            external_id='m-nhi-001',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.api_key,
            principal_nhi_id=nhi.id,
            status='active',
        )
        session.add(subj)
        await session.flush()
        assert subj.id is not None


@pytest.mark.asyncio
async def test_create_customer_subject(session_factory) -> None:
    """Happy path: customer subject persists without error."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subj = Subject(
            external_id='m-cust-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        session.add(subj)
        await session.flush()
        assert subj.id is not None


# ---------------------------------------------------------------------------
# ck_subjects_principal_exactly_one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_principals_violates_check(session_factory) -> None:
    """Two non-null principal_*_id columns → ck_subjects_principal_exactly_one."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        nhi = await _make_nhi(session)
        subj = Subject(
            external_id='m-two-principals',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            principal_nhi_id=nhi.id,  # extra principal — violation
            status='active',
        )
        session.add(subj)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_zero_principals_violates_check(session_factory) -> None:
    """Zero non-null principal_*_id columns → ck_subjects_principal_exactly_one."""
    async with session_factory() as session:
        subj = Subject(
            external_id='m-zero-principals',
            kind=SubjectKind.employee,
            # all principals null — violation
            status='active',
        )
        session.add(subj)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_wrong_principal_for_kind_violates_check(session_factory) -> None:
    """kind=employee with principal_nhi_id (not employee) → ck_subjects_principal_exactly_one."""
    async with session_factory() as session:
        nhi = await _make_nhi(session)
        subj = Subject(
            external_id='m-wrong-principal',
            kind=SubjectKind.employee,
            principal_nhi_id=nhi.id,  # wrong column for employee kind
            status='active',
        )
        session.add(subj)
        with pytest.raises(IntegrityError):
            await session.flush()


# ---------------------------------------------------------------------------
# ck_subjects_nhi_kind_consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nhi_kind_set_for_non_nhi_violates_check(session_factory) -> None:
    """kind=employee but nhi_kind set → ck_subjects_nhi_kind_consistency."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subj = Subject(
            external_id='m-nhi-kind-wrong',
            kind=SubjectKind.employee,
            nhi_kind=SubjectNHIKind.bot,  # must be null for employee
            principal_employee_id=emp.id,
            status='active',
        )
        session.add(subj)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_nhi_kind_null_for_nhi_violates_check(session_factory) -> None:
    """kind=nhi but nhi_kind null → ck_subjects_nhi_kind_consistency."""
    async with session_factory() as session:
        nhi = await _make_nhi(session)
        subj = Subject(
            external_id='m-nhi-kind-missing',
            kind=SubjectKind.nhi,
            nhi_kind=None,  # must be non-null for nhi
            principal_nhi_id=nhi.id,
            status='active',
        )
        session.add(subj)
        with pytest.raises(IntegrityError):
            await session.flush()


# ---------------------------------------------------------------------------
# ck_subjects_status_vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_status_for_employee_violates_check(session_factory) -> None:
    """status='registered' (customer vocab) for employee → ck_subjects_status_vocabulary."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subj = Subject(
            external_id='m-bad-status-emp',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='registered',  # customer status, not valid for employee
        )
        session.add(subj)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_invalid_status_for_customer_violates_check(session_factory) -> None:
    """status='hired' (employee vocab) for customer → ck_subjects_status_vocabulary."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subj = Subject(
            external_id='m-bad-status-cust',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='hired',  # employee status, not valid for customer
        )
        session.add(subj)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_invalid_status_for_nhi_violates_check(session_factory) -> None:
    """status='hired' (employee vocab) for nhi → ck_subjects_status_vocabulary."""
    async with session_factory() as session:
        nhi = await _make_nhi(session)
        subj = Subject(
            external_id='m-bad-status-nhi',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.service_account,
            principal_nhi_id=nhi.id,
            status='hired',  # employee status, not valid for nhi
        )
        session.add(subj)
        with pytest.raises(IntegrityError):
            await session.flush()


# ---------------------------------------------------------------------------
# Partial-unique indexes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_principal_employee_id_violates_unique(session_factory) -> None:
    """Same principal_employee_id in two subjects → uq_subjects_principal_employee_id."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        s1 = Subject(
            external_id='m-dup-emp-1',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        session.add(s1)
        await session.flush()

        s2 = Subject(
            external_id='m-dup-emp-2',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,  # same FK — unique violation
            status='active',
        )
        session.add(s2)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_duplicate_principal_nhi_id_violates_unique(session_factory) -> None:
    """Same principal_nhi_id in two subjects → uq_subjects_principal_nhi_id."""
    async with session_factory() as session:
        nhi = await _make_nhi(session)
        s1 = Subject(
            external_id='m-dup-nhi-1',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.bot,
            principal_nhi_id=nhi.id,
            status='active',
        )
        session.add(s1)
        await session.flush()

        s2 = Subject(
            external_id='m-dup-nhi-2',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.bot,
            principal_nhi_id=nhi.id,  # same FK — unique violation
            status='active',
        )
        session.add(s2)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_duplicate_principal_customer_id_violates_unique(session_factory) -> None:
    """Same principal_customer_id in two subjects → uq_subjects_principal_customer_id."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        s1 = Subject(
            external_id='m-dup-cust-1',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        session.add(s1)
        await session.flush()

        s2 = Subject(
            external_id='m-dup-cust-2',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,  # same FK — unique violation
            status='registered',
        )
        session.add(s2)
        with pytest.raises(IntegrityError):
            await session.flush()


# ---------------------------------------------------------------------------
# SubjectAttribute model tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subject_attribute_insert_and_read(session_factory) -> None:
    """SubjectAttribute is persisted and can be read back."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subj = Subject(
            external_id='m-attr-read-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        session.add(subj)
        await session.flush()
        attr = SubjectAttribute(subject_id=subj.id, key='dept', value='engineering')
        session.add(attr)
        await session.commit()
        attr_id = attr.id

    from sqlalchemy import select

    async with session_factory() as session:
        result = await session.execute(select(SubjectAttribute).where(SubjectAttribute.id == attr_id))
        loaded = result.scalar_one_or_none()
    assert loaded is not None
    assert loaded.key == 'dept'
    assert loaded.value == 'engineering'


@pytest.mark.asyncio
async def test_subject_attribute_unique_constraint(session_factory) -> None:
    """Duplicate (subject_id, key) raises IntegrityError."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subj = Subject(
            external_id='m-attr-uniq-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        session.add(subj)
        await session.flush()
        attr1 = SubjectAttribute(subject_id=subj.id, key='x', value='v1')
        session.add(attr1)
        await session.commit()
        subj_id = subj.id

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            attr2 = SubjectAttribute(subject_id=subj_id, key='x', value='v2')
            session.add(attr2)
            await session.commit()


@pytest.mark.asyncio
async def test_subject_cascade_deletes_attributes(session_factory) -> None:
    """Deleting a Subject cascades to its SubjectAttribute rows."""
    from sqlalchemy import select

    async with session_factory() as session:
        cust = await _make_customer(session)
        subj = Subject(
            external_id='m-attr-cascade-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        session.add(subj)
        await session.flush()
        attr = SubjectAttribute(subject_id=subj.id, key='cascade_key', value='v')
        session.add(attr)
        await session.commit()
        subj_id = subj.id

    async with session_factory() as session:
        result = await session.execute(select(Subject).where(Subject.id == subj_id))
        subj_loaded = result.scalar_one()
        await session.delete(subj_loaded)
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(select(SubjectAttribute).where(SubjectAttribute.subject_id == subj_id))
        attrs = result.scalars().all()
    assert len(attrs) == 0
