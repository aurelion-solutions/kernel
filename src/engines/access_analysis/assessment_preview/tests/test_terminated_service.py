# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer integration tests for TerminatedDetectorService — DB-backed."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from src.engines.access_analysis.assessment_preview.service import TerminatedDetectorService
from src.inventory.accounts.models import Account
from src.inventory.assessment.findings.models import Finding
from src.inventory.customers.models import Customer
from src.inventory.employees.models import Employee
from src.inventory.nhi.models import NHI
from src.inventory.persons.models import Person
from src.inventory.policy.sod_rules.models import SodSeverity
from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
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


async def _seed_nhi_subject(session, *, status: str) -> uuid.UUID:  # type: ignore[no-untyped-def]
    nhi = NHI(
        external_id=f'nhi-{uuid.uuid4().hex[:8]}',
        name=f'test-nhi-{uuid.uuid4().hex[:8]}',
        kind='service_account',
        owner_employee_id=None,
    )
    session.add(nhi)
    await session.flush()
    subject = Subject(
        external_id=f'subj-nhi-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status=status,
    )
    session.add(subject)
    await session.flush()
    return subject.id


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


async def _seed_customer_subject(session, *, status: str) -> uuid.UUID:  # type: ignore[no-untyped-def]
    customer = Customer(
        external_id=f'cust-{uuid.uuid4().hex[:8]}',
    )
    session.add(customer)
    await session.flush()
    subject = Subject(
        external_id=f'subj-cust-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.customer,
        principal_customer_id=customer.id,
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
# Test S1: 4 subjects (active emp, terminated emp, expired nhi, banned cust)
#          + 1 orphan → 3 findings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_returns_only_terminated_findings(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)

        active_emp_subj = await _seed_employee_subject(session, status='active')
        terminated_emp_subj = await _seed_employee_subject(session, status='terminated')
        expired_nhi_subj = await _seed_nhi_subject(session, status='expired')
        banned_cust_subj = await _seed_customer_subject(session, status='banned')

        await _seed_account(session, app_id, active_emp_subj, 'active_emp')
        await _seed_account(session, app_id, terminated_emp_subj, 'terminated_emp')
        await _seed_account(session, app_id, expired_nhi_subj, 'expired_nhi')
        await _seed_account(session, app_id, banned_cust_subj, 'banned_cust')
        # orphan — must be excluded (INNER JOIN drops it)
        await _seed_account(session, app_id, None, 'orphan')
        await session.commit()

    async with session_factory() as session:
        svc = TerminatedDetectorService(session)
        findings = await svc.run(application_id=None, limit=1000)

    assert len(findings) == 3
    usernames = {f.username for f in findings}
    assert usernames == {'terminated_emp', 'expired_nhi', 'banned_cust'}
    for f in findings:
        assert f.severity == SodSeverity.high


# ---------------------------------------------------------------------------
# Test S2: application_id filter — different app → empty list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_filters_by_application_id(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_a = await _seed_application(session)
        app_b = await _seed_application(session)
        subj_id = await _seed_employee_subject(session, status='terminated')
        await _seed_account(session, app_a, subj_id, 'term_a')
        await session.commit()

    async with session_factory() as session:
        svc = TerminatedDetectorService(session)
        findings = await svc.run(application_id=app_b, limit=1000)

    assert findings == []


# ---------------------------------------------------------------------------
# Test S3: limit parameter respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_respects_limit(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        for i in range(5):
            subj_id = await _seed_employee_subject(session, status='terminated')
            await _seed_account(session, app_id, subj_id, f'term{i:02d}')
        await session.commit()

    async with session_factory() as session:
        svc = TerminatedDetectorService(session)
        findings = await svc.run(application_id=None, limit=3)

    assert len(findings) <= 3


# ---------------------------------------------------------------------------
# Test S4: service does not write Finding rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_does_not_persist(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_employee_subject(session, status='terminated')
        await _seed_account(session, app_id, subj_id, 'ghost')
        await session.commit()

    async with session_factory() as session:
        svc = TerminatedDetectorService(session)
        findings = await svc.run(application_id=None, limit=1000)
        assert len(findings) == 1

        count = await session.scalar(sa.select(sa.func.count()).select_from(Finding))
        assert count == 0
