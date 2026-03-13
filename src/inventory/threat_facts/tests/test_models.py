# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for ThreatFact model constraints."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.threat_facts.models import ThreatFact

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_subject(session) -> uuid.UUID:
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject, SubjectKind

    person = await create_person(session, external_id=str(uuid.uuid4()), description='test')
    await session.flush()
    emp = await create_employee(session, person_id=person.id)
    await session.flush()
    subj = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def _make_account(session) -> uuid.UUID:
    from src.inventory.accounts.models import Account
    from src.platform.applications.models import Application

    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    account = Account(
        application_id=app.id,
        username=f'user-{uuid.uuid4().hex[:8]}',
        status='active',
    )
    session.add(account)
    await session.flush()
    return account.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threat_fact_creation_stores_all_fields(session_factory) -> None:
    """Happy path: create threat fact with indicators; assert all columns round-trip."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        last_login = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
        observed = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

        fact = ThreatFact(
            subject_id=subject_id,
            risk_score=0.42,
            active_indicators=['account_takeover', 'impossible_travel'],
            last_login_at=last_login,
            failed_auth_count=3,
            observed_at=observed,
        )
        session.add(fact)
        await session.flush()
        await session.refresh(fact)

        assert fact.id is not None
        assert fact.subject_id == subject_id
        assert fact.account_id is None
        assert fact.risk_score == 0.42
        assert fact.active_indicators == ['account_takeover', 'impossible_travel']
        assert fact.last_login_at is not None
        assert fact.failed_auth_count == 3
        assert fact.observed_at is not None
        assert fact.created_at is not None
        assert fact.updated_at is not None


@pytest.mark.asyncio
async def test_check_rejects_out_of_range_risk_score(session_factory) -> None:
    """INSERT with risk_score=1.5 or -0.1 must raise IntegrityError pgcode 23514."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        fact = ThreatFact(
            subject_id=subject_id,
            risk_score=1.5,
            active_indicators=[],
            failed_auth_count=0,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(fact)
        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        pgcode = getattr(exc_info.value.orig, 'pgcode', None)
        assert pgcode == '23514'

    async with session_factory() as session:
        subject_id2 = await _make_subject(session)
        fact2 = ThreatFact(
            subject_id=subject_id2,
            risk_score=-0.1,
            active_indicators=[],
            failed_auth_count=0,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(fact2)
        with pytest.raises(IntegrityError) as exc_info2:
            await session.flush()
        pgcode2 = getattr(exc_info2.value.orig, 'pgcode', None)
        assert pgcode2 == '23514'


@pytest.mark.asyncio
async def test_cascade_delete_from_subject(session_factory) -> None:
    """Deleting a Subject cascades to its ThreatFact row.

    Also verifies ON DELETE SET NULL: deleting an Account leaves the ThreatFact row
    with account_id IS NULL.
    """
    # -- CASCADE test
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        fact = ThreatFact(
            subject_id=subject_id,
            risk_score=0.5,
            active_indicators=[],
            failed_auth_count=0,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(fact)
        await session.flush()
        fact_id = fact.id
        await session.commit()

    async with session_factory() as session:
        from src.inventory.subjects.models import Subject

        subj = await session.get(Subject, subject_id)
        assert subj is not None
        await session.delete(subj)
        await session.commit()

    async with session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(ThreatFact).where(ThreatFact.id == fact_id))
        assert result.scalar_one_or_none() is None

    # -- SET NULL test
    async with session_factory() as session:
        subject_id2 = await _make_subject(session)
        account_id = await _make_account(session)
        fact2 = ThreatFact(
            subject_id=subject_id2,
            account_id=account_id,
            risk_score=0.3,
            active_indicators=[],
            failed_auth_count=0,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(fact2)
        await session.flush()
        fact2_id = fact2.id
        await session.commit()

    async with session_factory() as session:
        from src.inventory.accounts.models import Account

        acc = await session.get(Account, account_id)
        assert acc is not None
        await session.delete(acc)
        await session.commit()

    async with session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(ThreatFact).where(ThreatFact.id == fact2_id))
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.account_id is None
