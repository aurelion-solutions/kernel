# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for account reconciler."""

import pytest
from src.capabilities.reconciliation.reconciler_account import reconcile_accounts
from src.inventory.accounts.models import Account
from src.inventory.accounts.repository import list_by_application
from src.inventory.accounts.schemas import AccountDTO
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_account_reconciler_creates_new_accounts_from_dtos(session_factory):
    """Account reconciler creates new accounts from DTOs."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        dtos = [
            AccountDTO(identifier='user_1', username='alice'),
            AccountDTO(identifier='user_2', username='bob'),
        ]
        result = await reconcile_accounts(session, app_id, dtos)
        await session.commit()

    assert result.source_total == 2
    assert result.created == 2
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.deactivated == 0
    assert result.errors == 0

    async with session_factory() as session:
        accounts = await list_by_application(session, app_id)
        assert len(accounts) == 2
        usernames = {a.username for a in accounts}
        assert usernames == {'alice', 'bob'}


@pytest.mark.asyncio
async def test_account_reconciler_updates_existing_when_fields_changed(session_factory):
    """Account reconciler updates existing accounts when fields changed."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        account = Account(
            application_id=app_id,
            username='old_name',
            meta={'identifier': 'user_1'},
        )
        session.add(account)
        await session.commit()

    async with session_factory() as session:
        dtos = [AccountDTO(identifier='user_1', username='new_name')]
        result = await reconcile_accounts(session, app_id, dtos)
        await session.commit()

    assert result.created == 0
    assert result.updated == 1
    assert result.unchanged == 0

    async with session_factory() as session:
        accounts = await list_by_application(session, app_id)
        assert len(accounts) == 1
        assert accounts[0].username == 'new_name'


@pytest.mark.asyncio
async def test_account_reconciler_leaves_unchanged_when_fields_match(session_factory):
    """Account reconciler leaves unchanged when fields match."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        account = Account(
            application_id=app_id,
            username='alice',
            meta={'identifier': 'user_1'},
        )
        session.add(account)
        await session.commit()

    async with session_factory() as session:
        dtos = [AccountDTO(identifier='user_1', username='alice')]
        result = await reconcile_accounts(session, app_id, dtos)
        await session.commit()

    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 1


@pytest.mark.asyncio
async def test_account_reconciler_marks_missing_accounts_inactive(session_factory):
    """Account reconciler marks missing accounts inactive."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        account1 = Account(
            application_id=app_id,
            username='keep',
            meta={'identifier': 'user_1'},
        )
        account2 = Account(
            application_id=app_id,
            username='deactivate',
            meta={'identifier': 'user_2'},
        )
        session.add_all([account1, account2])
        await session.commit()

    async with session_factory() as session:
        dtos = [AccountDTO(identifier='user_1', username='keep')]
        result = await reconcile_accounts(session, app_id, dtos)
        await session.commit()

    assert result.deactivated == 1

    async with session_factory() as session:
        accounts = await list_by_application(session, app_id)
        by_id = {a.meta.get('identifier'): a for a in accounts if a.meta.get('identifier')}
        assert by_id['user_1'].is_active is True
        assert by_id['user_2'].is_active is False
