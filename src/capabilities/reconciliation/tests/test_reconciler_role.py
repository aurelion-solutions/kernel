# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for role reconciler."""

import pytest
from src.capabilities.reconciliation.reconciler_role import reconcile_roles
from src.inventory.roles.models import Role
from src.inventory.roles.repository import list_by_application
from src.inventory.roles.schemas import RoleDTO
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_role_reconciler_creates_new_roles_from_dtos(session_factory):
    """Role reconciler creates new roles from DTOs."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        dtos = [
            RoleDTO(identifier='role_1', name='admin'),
            RoleDTO(identifier='role_2', name='viewer'),
        ]
        result = await reconcile_roles(session, app_id, dtos)
        await session.commit()

    assert result.source_total == 2
    assert result.created == 2
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.deactivated == 0
    assert result.errors == 0

    async with session_factory() as session:
        roles = await list_by_application(session, app_id)
        assert len(roles) == 2
        names = {r.name for r in roles}
        assert names == {'admin', 'viewer'}


@pytest.mark.asyncio
async def test_role_reconciler_updates_existing_when_fields_changed(session_factory):
    """Role reconciler updates existing roles when fields changed."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        role = Role(
            application_id=app_id,
            name='old_name',
            meta={'identifier': 'role_1'},
        )
        session.add(role)
        await session.commit()

    async with session_factory() as session:
        dtos = [RoleDTO(identifier='role_1', name='new_name')]
        result = await reconcile_roles(session, app_id, dtos)
        await session.commit()

    assert result.created == 0
    assert result.updated == 1
    assert result.unchanged == 0

    async with session_factory() as session:
        roles = await list_by_application(session, app_id)
        assert len(roles) == 1
        assert roles[0].name == 'new_name'


@pytest.mark.asyncio
async def test_role_reconciler_leaves_unchanged_when_fields_match(session_factory):
    """Role reconciler leaves unchanged when fields match."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        role = Role(
            application_id=app_id,
            name='admin',
            meta={'identifier': 'role_1'},
        )
        session.add(role)
        await session.commit()

    async with session_factory() as session:
        dtos = [RoleDTO(identifier='role_1', name='admin')]
        result = await reconcile_roles(session, app_id, dtos)
        await session.commit()

    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 1


@pytest.mark.asyncio
async def test_role_reconciler_marks_missing_roles_inactive(session_factory):
    """Role reconciler marks missing roles inactive."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        role1 = Role(
            application_id=app_id,
            name='keep',
            meta={'identifier': 'role_1'},
        )
        role2 = Role(
            application_id=app_id,
            name='deactivate',
            meta={'identifier': 'role_2'},
        )
        session.add_all([role1, role2])
        await session.commit()

    async with session_factory() as session:
        dtos = [RoleDTO(identifier='role_1', name='keep')]
        result = await reconcile_roles(session, app_id, dtos)
        await session.commit()

    assert result.deactivated == 1

    async with session_factory() as session:
        roles = await list_by_application(session, app_id)
        by_id = {r.meta.get('identifier'): r for r in roles if r.meta.get('identifier')}
        assert by_id['role_1'].is_active is True
        assert by_id['role_2'].is_active is False
