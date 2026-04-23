# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for AccessFact partial unique constraints — Step 13."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.access_facts.models import AccessFact, AccessFactEffect

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_employee_subject(session) -> uuid.UUID:
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


async def _make_resource(session) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (app_id, resource_id)."""
    from src.inventory.resources.models import Resource
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
    ext = str(uuid.uuid4())
    resource = Resource(
        external_id=ext,
        application_id=app.id,
        kind='database',
        resource_type='database',
        resource_key=ext,
    )
    session.add(resource)
    await session.flush()
    return app.id, resource.id


async def _get_read_action_id(session) -> int:
    from sqlalchemy import select
    from src.inventory.actions.models import Action as RefAction

    result = await session.execute(select(RefAction.id).where(RefAction.slug == 'read'))
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_unique_active_account_key_db_level(session_factory) -> None:
    """Direct ORM insert: same (account_id, resource_id, action_id) + is_active=True → IntegrityError 23505."""
    from src.inventory.accounts.models import Account

    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_resource(session)
        action_id = await _get_read_action_id(session)

        account = Account(
            username=f'user-{uuid.uuid4().hex[:8]}',
            application_id=app_id,
            status='active',
        )
        session.add(account)
        await session.flush()

        f1 = AccessFact(
            subject_id=subject_id,
            account_id=account.id,
            resource_id=resource_id,
            action_id=action_id,
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
            is_active=True,
        )
        session.add(f1)
        await session.flush()

        f2 = AccessFact(
            subject_id=subject_id,
            account_id=account.id,
            resource_id=resource_id,
            action_id=action_id,
            effect=AccessFactEffect.deny,  # different effect, same key
            observed_at=_NOW,
            is_active=True,
        )
        session.add(f2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()

        pgcode = getattr(exc_info.value.orig, 'pgcode', None)
        assert pgcode == '23505', f'Expected 23505 got {pgcode}'
        # asyncpg may expose constraint_name via diag or via str(exc)
        exc_str = str(exc_info.value)
        constraint = getattr(getattr(exc_info.value.orig, 'diag', None), 'constraint_name', '') or exc_str
        assert 'active_account' in constraint, f'Unexpected constraint: {constraint!r}'

        await session.rollback()


@pytest.mark.asyncio
async def test_partial_unique_does_not_block_revoked_re_grant(session_factory) -> None:
    """Insert → flip is_active=False → insert again with same key + is_active=True → no violation."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        _, resource_id = await _make_resource(session)
        action_id = await _get_read_action_id(session)

        f1 = AccessFact(
            subject_id=subject_id,
            account_id=None,
            resource_id=resource_id,
            action_id=action_id,
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
            is_active=True,
        )
        session.add(f1)
        await session.flush()

        # Revoke (flip is_active=False)
        f1.is_active = False
        f1.revoked_at = _NOW
        await session.flush()

        # Re-grant: same key, is_active=True — must NOT raise
        f2 = AccessFact(
            subject_id=subject_id,
            account_id=None,
            resource_id=resource_id,
            action_id=action_id,
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
            is_active=True,
        )
        session.add(f2)
        await session.flush()  # should not raise

        assert f2.id is not None
        await session.rollback()


@pytest.mark.asyncio
async def test_partial_unique_active_subject_key_db_level(session_factory) -> None:
    """account_id IS NULL path: same (subject_id, resource_id, action_id) + is_active=True → IntegrityError."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        _, resource_id = await _make_resource(session)
        action_id = await _get_read_action_id(session)

        f1 = AccessFact(
            subject_id=subject_id,
            account_id=None,
            resource_id=resource_id,
            action_id=action_id,
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
            is_active=True,
        )
        session.add(f1)
        await session.flush()

        f2 = AccessFact(
            subject_id=subject_id,
            account_id=None,
            resource_id=resource_id,
            action_id=action_id,
            effect=AccessFactEffect.deny,
            observed_at=_NOW,
            is_active=True,
        )
        session.add(f2)

        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()

        pgcode = getattr(exc_info.value.orig, 'pgcode', None)
        assert pgcode == '23505', f'Expected 23505 got {pgcode}'
        # asyncpg may expose constraint_name via diag or via str(exc)
        exc_str = str(exc_info.value)
        constraint = getattr(getattr(exc_info.value.orig, 'diag', None), 'constraint_name', '') or exc_str
        assert 'active_subject' in constraint, f'Unexpected constraint: {constraint!r}'

        await session.rollback()
