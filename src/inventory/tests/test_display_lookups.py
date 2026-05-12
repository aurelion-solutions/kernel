# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for src/inventory/display_lookups.py — batch lookup helpers."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.inventory.accounts.models import Account, AccountStatus
from src.inventory.display_lookups import (
    batch_account_display,
    batch_application_display,
    batch_display_by_subject_ids,
    batch_employee_display,
    batch_nhi_display,
    batch_resource_display,
    batch_subject_display,
)
from src.inventory.employees.models import Employee
from src.inventory.nhi.models import NHI
from src.inventory.persons.models import Person
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject, SubjectKind
from src.platform.applications.models import Application

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_app(session: AsyncSession) -> Application:
    app = Application(
        id=uuid.uuid4(),
        name=f'TestApp-{uuid.uuid4().hex[:6]}',
        code=f'TA{uuid.uuid4().hex[:4].upper()}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app


async def _create_person(session: AsyncSession, full_name: str) -> Person:
    p = Person(id=uuid.uuid4(), external_id=uuid.uuid4().hex, full_name=full_name)
    session.add(p)
    await session.flush()
    return p


async def _create_employee(session: AsyncSession, person: Person) -> Employee:
    emp = Employee(id=uuid.uuid4(), person_id=person.id)
    session.add(emp)
    await session.flush()
    return emp


async def _create_nhi(session: AsyncSession, external_id: str, app_id: uuid.UUID) -> NHI:
    nhi = NHI(id=uuid.uuid4(), external_id=external_id, name=external_id, kind='bot', application_id=app_id)
    session.add(nhi)
    await session.flush()
    return nhi


async def _create_account(session: AsyncSession, username: str, app_id: uuid.UUID) -> Account:
    acc = Account(
        id=uuid.uuid4(),
        application_id=app_id,
        username=username,
        status=AccountStatus.active,
        meta={},
    )
    session.add(acc)
    await session.flush()
    return acc


async def _create_resource(
    session: AsyncSession,
    external_id: str,
    kind: str,
    app_id: uuid.UUID,
) -> Resource:
    res = Resource(
        id=uuid.uuid4(),
        external_id=external_id,
        application_id=app_id,
        kind=kind,
        resource_type='repo',
        resource_key=f'{external_id}-key-{uuid.uuid4().hex[:4]}',
    )
    session.add(res)
    await session.flush()
    return res


# ---------------------------------------------------------------------------
# Tests: batch_employee_display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_employee_display_resolves_name(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        person = await _create_person(session, 'Alice Smith')
        employee = await _create_employee(session, person)
        await session.commit()

    async with session_factory() as session:
        result = await batch_employee_display(session, {employee.id})

    assert result[employee.id] == 'Alice Smith'


@pytest.mark.asyncio
async def test_batch_employee_display_missing_returns_empty(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        result = await batch_employee_display(session, {uuid.uuid4()})

    assert result == {}


@pytest.mark.asyncio
async def test_batch_employee_display_empty_input(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        result = await batch_employee_display(session, set())

    assert result == {}


# ---------------------------------------------------------------------------
# Tests: batch_nhi_display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_nhi_display_resolves_external_id(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        app = await _create_app(session)
        nhi = await _create_nhi(session, 'svc-account-01', app.id)
        await session.commit()

    async with session_factory() as session:
        result = await batch_nhi_display(session, {nhi.id})

    assert result[nhi.id] == 'svc-account-01'


@pytest.mark.asyncio
async def test_batch_nhi_display_missing_returns_empty(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        result = await batch_nhi_display(session, {uuid.uuid4()})

    assert result == {}


# ---------------------------------------------------------------------------
# Tests: batch_account_display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_account_display_resolves_username(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        app = await _create_app(session)
        acc = await _create_account(session, 'jdoe@example.com', app.id)
        await session.commit()

    async with session_factory() as session:
        result = await batch_account_display(session, {acc.id})

    assert result[acc.id] == 'jdoe@example.com'


@pytest.mark.asyncio
async def test_batch_account_display_missing_returns_empty(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        result = await batch_account_display(session, {uuid.uuid4()})

    assert result == {}


# ---------------------------------------------------------------------------
# Tests: batch_resource_display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_resource_display_with_kind(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        app = await _create_app(session)
        res = await _create_resource(session, 'aurelion/kernel', 'repository', app.id)
        await session.commit()

    async with session_factory() as session:
        result = await batch_resource_display(session, {res.id})

    assert result[res.id] == 'aurelion/kernel (repository)'


@pytest.mark.asyncio
async def test_batch_resource_display_misc_kind(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        app = await _create_app(session)
        res = await _create_resource(session, 'some-misc-resource', 'misc', app.id)
        await session.commit()

    async with session_factory() as session:
        result = await batch_resource_display(session, {res.id})

    # 'misc' kind → no suffix
    assert result[res.id] == 'some-misc-resource'


# ---------------------------------------------------------------------------
# Tests: batch_application_display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_application_display_resolves_code(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        app = Application(
            id=uuid.uuid4(),
            name='GitHub Enterprise',
            code='GHE',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.commit()

    async with session_factory() as session:
        result = await batch_application_display(session, {app.id})

    assert result[app.id].code == 'GHE'
    assert result[app.id].name == 'GitHub Enterprise'


# ---------------------------------------------------------------------------
# Tests: batch_subject_display (composite)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_subject_display_employee_wins(session_factory: async_sessionmaker) -> None:
    async with session_factory() as session:
        person = await _create_person(session, 'Bob Jones')
        employee = await _create_employee(session, person)
        await session.commit()

    async with session_factory() as session:
        result = await batch_subject_display(
            session,
            employee_ids={employee.id},
            nhi_ids=set(),
        )

    assert result[employee.id] == 'Bob Jones'


# ---------------------------------------------------------------------------
# Tests: batch_display_by_subject_ids (subject.id → display via JOIN)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_display_by_subject_ids_employee(session_factory: async_sessionmaker) -> None:
    """Employee subject resolves to person full_name via subjects JOIN."""
    async with session_factory() as session:
        person = await _create_person(session, 'Carol White')
        employee = await _create_employee(session, person)
        subject = Subject(
            id=uuid.uuid4(),
            external_id=f'subj-emp-{uuid.uuid4().hex[:6]}',
            kind=SubjectKind.employee,
            principal_employee_id=employee.id,
            status='active',
        )
        session.add(subject)
        await session.commit()

    async with session_factory() as session:
        result = await batch_display_by_subject_ids(session, {subject.id})

    assert result[subject.id] == 'Carol White'


@pytest.mark.asyncio
async def test_batch_display_by_subject_ids_nhi(session_factory: async_sessionmaker) -> None:
    """NHI subject resolves to nhi.external_id via subjects JOIN."""
    async with session_factory() as session:
        app = await _create_app(session)
        nhi = await _create_nhi(session, 'svc-bot-lookup-01', app.id)
        subject = Subject(
            id=uuid.uuid4(),
            external_id=f'subj-nhi-{uuid.uuid4().hex[:6]}',
            kind=SubjectKind.nhi,
            nhi_kind='bot',
            principal_nhi_id=nhi.id,
            status='active',
        )
        session.add(subject)
        await session.commit()

    async with session_factory() as session:
        result = await batch_display_by_subject_ids(session, {subject.id})

    assert result[subject.id] == 'svc-bot-lookup-01'


@pytest.mark.asyncio
async def test_batch_display_by_subject_ids_empty(session_factory: async_sessionmaker) -> None:
    """Empty input returns empty dict without hitting DB."""
    async with session_factory() as session:
        result = await batch_display_by_subject_ids(session, set())

    assert result == {}


@pytest.mark.asyncio
async def test_batch_display_by_subject_ids_unknown(session_factory: async_sessionmaker) -> None:
    """Unknown subject UUID returns empty dict (not an error)."""
    async with session_factory() as session:
        result = await batch_display_by_subject_ids(session, {uuid.uuid4()})

    assert result == {}
