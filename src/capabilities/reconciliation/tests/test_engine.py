# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for generic reconciliation engine."""

import uuid

import pytest
from src.capabilities.reconciliation.engine import reconcile_entities
from src.inventory.accounts.models import Account
from src.inventory.accounts.repository import list_by_application
from src.inventory.accounts.schemas import AccountDTO
from src.platform.applications.models import Application


def get_key_from_dto(dto: AccountDTO) -> str:
    return dto.identifier


def get_key_from_model(model: Account) -> str | None:
    return model.meta.get('identifier') if isinstance(model.meta, dict) else None


def create_account_from_dto(session, application_id: uuid.UUID, dto: AccountDTO) -> Account:
    account = Account(
        application_id=application_id,
        username=dto.username or dto.identifier,
        display_name=dto.display_name,
        email=dto.email,
        is_active=dto.is_active,
        is_privileged=dto.is_privileged,
        mfa_enabled=dto.mfa_enabled,
        meta={**dto.meta, 'identifier': dto.identifier},
    )
    session.add(account)
    return account


def update_account_from_dto(model: Account, dto: AccountDTO) -> bool:
    changed = False
    if model.username != (dto.username or dto.identifier):
        model.username = dto.username or dto.identifier
        changed = True
    if model.display_name != dto.display_name:
        model.display_name = dto.display_name
        changed = True
    if model.email != dto.email:
        model.email = dto.email
        changed = True
    if model.is_active != dto.is_active:
        model.is_active = dto.is_active
        changed = True
    if model.is_privileged != dto.is_privileged:
        model.is_privileged = dto.is_privileged
        changed = True
    if model.mfa_enabled != dto.mfa_enabled:
        model.mfa_enabled = dto.mfa_enabled
        changed = True
    meta = {**model.meta, 'identifier': dto.identifier, **dto.meta}
    if model.meta != meta:
        model.meta = meta
        changed = True
    return changed


@pytest.mark.asyncio
async def test_engine_creates_new_records_when_key_does_not_exist(session_factory):
    """Create new local records when source key does not exist."""
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
        result = await reconcile_entities(
            session,
            app_id,
            dtos,
            load_existing=lambda s, aid: list_by_application(s, aid),
            get_key_from_dto=get_key_from_dto,
            get_key_from_model=get_key_from_model,
            create_from_dto=create_account_from_dto,
            update_from_dto=update_account_from_dto,
        )
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
async def test_engine_updates_existing_when_fields_changed(session_factory):
    """Update existing local records when mapped fields changed."""
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
        result = await reconcile_entities(
            session,
            app_id,
            dtos,
            load_existing=lambda s, aid: list_by_application(s, aid),
            get_key_from_dto=get_key_from_dto,
            get_key_from_model=get_key_from_model,
            create_from_dto=create_account_from_dto,
            update_from_dto=update_account_from_dto,
        )
        await session.commit()

    assert result.created == 0
    assert result.updated == 1
    assert result.unchanged == 0

    async with session_factory() as session:
        accounts = await list_by_application(session, app_id)
        assert len(accounts) == 1
        assert accounts[0].username == 'new_name'


@pytest.mark.asyncio
async def test_engine_unchanged_when_fields_did_not_change(session_factory):
    """Leave existing local records unchanged when mapped fields did not change."""
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
        result = await reconcile_entities(
            session,
            app_id,
            dtos,
            load_existing=lambda s, aid: list_by_application(s, aid),
            get_key_from_dto=get_key_from_dto,
            get_key_from_model=get_key_from_model,
            create_from_dto=create_account_from_dto,
            update_from_dto=update_account_from_dto,
        )
        await session.commit()

    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 1


@pytest.mark.asyncio
async def test_engine_marks_missing_local_records_inactive(session_factory):
    """Mark missing local records inactive when absent from source payload."""
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
        result = await reconcile_entities(
            session,
            app_id,
            dtos,
            load_existing=lambda s, aid: list_by_application(s, aid),
            get_key_from_dto=get_key_from_dto,
            get_key_from_model=get_key_from_model,
            create_from_dto=create_account_from_dto,
            update_from_dto=update_account_from_dto,
        )
        await session.commit()

    assert result.deactivated == 1

    async with session_factory() as session:
        accounts = await list_by_application(session, app_id)
        by_id = {a.meta.get('identifier'): a for a in accounts if a.meta.get('identifier')}
        assert by_id['user_1'].is_active is True
        assert by_id['user_2'].is_active is False


@pytest.mark.asyncio
async def test_engine_returns_correct_counters(session_factory):
    """Return correct counters for mixed create/update/unchanged/deactivate."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        existing = Account(
            application_id=app_id,
            username='updated',
            meta={'identifier': 'user_1'},
        )
        unchanged = Account(
            application_id=app_id,
            username='same',
            meta={'identifier': 'user_2'},
        )
        to_deactivate = Account(
            application_id=app_id,
            username='gone',
            meta={'identifier': 'user_3'},
        )
        session.add_all([existing, unchanged, to_deactivate])
        await session.commit()

    async with session_factory() as session:
        dtos = [
            AccountDTO(identifier='user_1', username='new_value'),
            AccountDTO(identifier='user_2', username='same'),
            AccountDTO(identifier='user_4', username='created'),
        ]
        result = await reconcile_entities(
            session,
            app_id,
            dtos,
            load_existing=lambda s, aid: list_by_application(s, aid),
            get_key_from_dto=get_key_from_dto,
            get_key_from_model=get_key_from_model,
            create_from_dto=create_account_from_dto,
            update_from_dto=update_account_from_dto,
        )
        await session.commit()

    assert result.source_total == 3
    assert result.created == 1
    assert result.updated == 1
    assert result.unchanged == 1
    assert result.deactivated == 1
    assert result.errors == 0
