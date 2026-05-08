# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Account repository."""

import uuid

import pytest
from src.inventory.accounts.models import Account, AccountStatus
from src.inventory.accounts.repository import list_by_application
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_list_by_application_loads_accounts(session_factory):
    """Repository loads accounts by application_id."""
    async with session_factory() as session:
        app = Application(name='repo-test', code='repo-test', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        account1 = Account(application_id=app_id, username='user1')
        account2 = Account(application_id=app_id, username='user2')
        session.add_all([account1, account2])
        await session.commit()

    async with session_factory() as session:
        accounts = await list_by_application(session, app_id)
        assert len(accounts) == 2
        usernames = {a.username for a in accounts}
        assert usernames == {'user1', 'user2'}


@pytest.mark.asyncio
async def test_list_by_application_returns_empty_for_no_accounts(session_factory):
    """Repository returns empty list when application has no accounts."""
    async with session_factory() as session:
        app = Application(name='empty-app', code='empty-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        accounts = await list_by_application(session, app_id)
        assert accounts == []


@pytest.mark.asyncio
async def test_list_by_application_filters_by_application(session_factory):
    """Repository returns only accounts for the given application."""
    async with session_factory() as session:
        app1 = Application(name='app1', code='app1', config={})
        app2 = Application(name='app2', code='app2', config={})
        session.add_all([app1, app2])
        await session.commit()
        app1_id = app1.id
        app2_id = app2.id

    async with session_factory() as session:
        session.add_all(
            [
                Account(application_id=app1_id, username='app1-user'),
                Account(application_id=app2_id, username='app2-user'),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        accounts = await list_by_application(session, app1_id)
        assert len(accounts) == 1
        assert accounts[0].username == 'app1-user'


@pytest.mark.asyncio
async def test_list_by_application_loads_new_columns(session_factory):
    """Repository round-trips subject_id and status columns correctly."""
    async with session_factory() as session:
        app = Application(name='new-cols-app', code='new-cols-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    # Create a Subject to bind
    async with session_factory() as session:
        from src.inventory.employees.repository import create_employee
        from src.inventory.persons.repository import create_person
        from src.inventory.subjects.models import Subject, SubjectKind

        person = await create_person(session, external_id=str(uuid.uuid4()), full_name='repo-test')
        await session.flush()
        employee = await create_employee(session, person_id=person.id)
        await session.flush()
        subject = Subject(
            external_id=f'repo-test-{uuid.uuid4()}',
            kind=SubjectKind.employee,
            principal_employee_id=employee.id,
            status='active',
        )
        session.add(subject)
        await session.commit()
        subject_id = subject.id

    async with session_factory() as session:
        account = Account(
            application_id=app_id,
            username='cols-user',
            status=AccountStatus.suspended,
            subject_id=subject_id,
        )
        session.add(account)
        await session.commit()

    async with session_factory() as session:
        accounts = await list_by_application(session, app_id)
        assert len(accounts) == 1
        assert accounts[0].status == AccountStatus.suspended
        assert accounts[0].subject_id == subject_id
