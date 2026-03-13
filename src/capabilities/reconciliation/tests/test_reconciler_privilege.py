# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for privilege reconciler."""

import pytest
from src.capabilities.reconciliation.reconciler_privilege import reconcile_privileges
from src.inventory.privileges.models import Privilege
from src.inventory.privileges.repository import list_by_application
from src.inventory.privileges.schemas import PrivilegeDTO
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_privilege_reconciler_creates_new_privileges_from_dtos(session_factory):
    """Privilege reconciler creates new privileges from DTOs."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        dtos = [
            PrivilegeDTO(identifier='priv_1', name='read'),
            PrivilegeDTO(identifier='priv_2', name='write'),
        ]
        result = await reconcile_privileges(session, app_id, dtos)
        await session.commit()

    assert result.source_total == 2
    assert result.created == 2
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.deactivated == 0
    assert result.errors == 0

    async with session_factory() as session:
        privileges = await list_by_application(session, app_id)
        assert len(privileges) == 2
        names = {p.name for p in privileges}
        assert names == {'read', 'write'}


@pytest.mark.asyncio
async def test_privilege_reconciler_updates_existing_when_fields_changed(session_factory):
    """Privilege reconciler updates existing privileges when fields changed."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        priv = Privilege(
            application_id=app_id,
            name='old_name',
            meta={'identifier': 'priv_1'},
        )
        session.add(priv)
        await session.commit()

    async with session_factory() as session:
        dtos = [PrivilegeDTO(identifier='priv_1', name='new_name')]
        result = await reconcile_privileges(session, app_id, dtos)
        await session.commit()

    assert result.created == 0
    assert result.updated == 1
    assert result.unchanged == 0

    async with session_factory() as session:
        privileges = await list_by_application(session, app_id)
        assert len(privileges) == 1
        assert privileges[0].name == 'new_name'


@pytest.mark.asyncio
async def test_privilege_reconciler_leaves_unchanged_when_fields_match(session_factory):
    """Privilege reconciler leaves unchanged when fields match."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        priv = Privilege(
            application_id=app_id,
            name='read',
            meta={'identifier': 'priv_1'},
        )
        session.add(priv)
        await session.commit()

    async with session_factory() as session:
        dtos = [PrivilegeDTO(identifier='priv_1', name='read')]
        result = await reconcile_privileges(session, app_id, dtos)
        await session.commit()

    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 1


@pytest.mark.asyncio
async def test_privilege_reconciler_marks_missing_privileges_inactive(session_factory):
    """Privilege reconciler marks missing privileges inactive."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        priv1 = Privilege(
            application_id=app_id,
            name='keep',
            meta={'identifier': 'priv_1'},
        )
        priv2 = Privilege(
            application_id=app_id,
            name='deactivate',
            meta={'identifier': 'priv_2'},
        )
        session.add_all([priv1, priv2])
        await session.commit()

    async with session_factory() as session:
        dtos = [PrivilegeDTO(identifier='priv_1', name='keep')]
        result = await reconcile_privileges(session, app_id, dtos)
        await session.commit()

    assert result.deactivated == 1

    async with session_factory() as session:
        privileges = await list_by_application(session, app_id)
        by_id = {p.meta.get('identifier'): p for p in privileges if p.meta.get('identifier')}
        assert by_id['priv_1'].is_active is True
        assert by_id['priv_2'].is_active is False
