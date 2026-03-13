# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Role model."""

import uuid

import pytest
from src.inventory.roles.models import Role
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_role_instantiation_with_required_fields(session_factory):
    """Role model can be instantiated with required fields."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        role = Role(
            application_id=app_id,
            name='admin',
            display_name='Administrator',
            type='builtin',
            is_active=True,
        )
        assert role.name == 'admin'
        assert role.display_name == 'Administrator'
        assert role.type == 'builtin'
        assert role.is_active is True


@pytest.mark.asyncio
async def test_role_id_is_uuid_primary_key(session_factory):
    """Role id is UUID primary key."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        role = Role(application_id=app_id, name='viewer')
        session.add(role)
        await session.commit()
        assert isinstance(role.id, uuid.UUID)
        assert role.id is not None


@pytest.mark.asyncio
async def test_role_is_active_and_meta_behave_correctly(session_factory):
    """is_active and meta fields behave correctly."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        role = Role(
            application_id=app_id,
            name='custom',
            is_active=False,
            meta={'source': 'connector', 'extra': 456},
        )
        session.add(role)
        await session.commit()
        role_id = role.id

    async with session_factory() as session:
        loaded = await session.get(Role, role_id)
        assert loaded is not None
        assert loaded.is_active is False
        assert loaded.meta == {'source': 'connector', 'extra': 456}
