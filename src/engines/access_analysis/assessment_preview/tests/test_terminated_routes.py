# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP tests for POST /access-analysis/detect-terminated."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from src.inventory.accounts.models import Account
from src.inventory.assessment.findings.models import Finding
from src.inventory.employees.models import Employee
from src.inventory.persons.models import Person
from src.inventory.subjects.models import Subject, SubjectKind
from src.platform.applications.models import Application

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_application(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    app = Application(
        name=f'app-{uuid.uuid4().hex[:8]}',
        code=f'code-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


async def _seed_employee_subject(session, *, status: str) -> uuid.UUID:  # type: ignore[no-untyped-def]
    person = Person(
        external_id=f'person-{uuid.uuid4().hex[:8]}',
        full_name='test person',
    )
    session.add(person)
    await session.flush()
    employee = Employee(person_id=person.id)
    session.add(employee)
    await session.flush()
    subject = Subject(
        external_id=f'subj-emp-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.employee,
        principal_employee_id=employee.id,
        status=status,
    )
    session.add(subject)
    await session.flush()
    return subject.id


async def _seed_account(session, app_id: uuid.UUID, subject_id: uuid.UUID | None, username: str) -> uuid.UUID:  # type: ignore[no-untyped-def]
    account = Account(
        application_id=app_id,
        username=username,
        subject_id=subject_id,
    )
    session.add(account)
    await session.flush()
    return account.id


# ---------------------------------------------------------------------------
# Test A1: empty body, no terminated subjects → 200, []
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_terminated_empty_body_no_results(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post('/api/v0/access-analysis/detect-terminated', json={})
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Test A2: one terminated employee account → 200, one result with all fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_terminated_returns_finding(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_employee_subject(session, status='terminated')
        await _seed_account(session, app_id, subj_id, 'term_user')
        await session.commit()

    resp = await client.post('/api/v0/access-analysis/detect-terminated', json={})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1

    finding = data[0]
    assert finding['username'] == 'term_user'
    assert finding['severity'] == 'high'
    assert finding['subject_kind'] == 'employee'
    assert finding['subject_status'] == 'terminated'
    assert isinstance(finding['account_id'], str)
    assert isinstance(finding['application_id'], str)
    assert isinstance(finding['subject_id'], str)
    assert isinstance(finding['subject_external_id'], str)


# ---------------------------------------------------------------------------
# Test A3: application_id filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_terminated_application_id_filter(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_a = await _seed_application(session)
        app_b = await _seed_application(session)
        subj_a = await _seed_employee_subject(session, status='terminated')
        subj_b = await _seed_employee_subject(session, status='terminated')
        await _seed_account(session, app_a, subj_a, 'term_a')
        await _seed_account(session, app_b, subj_b, 'term_b')
        await session.commit()

    resp = await client.post(
        '/api/v0/access-analysis/detect-terminated',
        json={'application_id': str(app_a)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]['username'] == 'term_a'
    assert data[0]['application_id'] == str(app_a)


# ---------------------------------------------------------------------------
# Test A4: limit=0 → 422 (ge=1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_terminated_limit_zero_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post('/api/v0/access-analysis/detect-terminated', json={'limit': 0})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test A5: limit=10000 → 422 (le=5000)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_terminated_limit_exceeds_cap_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post('/api/v0/access-analysis/detect-terminated', json={'limit': 10000})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test A6: extra field in body → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_terminated_extra_field_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post(
        '/api/v0/access-analysis/detect-terminated',
        json={'extra_field': 'bad'},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test A7: no Finding rows written after detection call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_terminated_writes_no_findings(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_employee_subject(session, status='terminated')
        await _seed_account(session, app_id, subj_id, 'ghost')
        await session.commit()

    resp = await client.post('/api/v0/access-analysis/detect-terminated', json={})
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    async with session_factory() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(Finding))
    assert count == 0
