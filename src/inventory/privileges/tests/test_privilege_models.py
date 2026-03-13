# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Privilege model."""

import uuid

import pytest
from src.inventory.privileges.models import Privilege
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_privilege_instantiation_with_required_fields(session_factory):
    """Privilege model can be instantiated with required fields."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        priv = Privilege(
            application_id=app_id,
            name='read',
            display_name='Read Access',
            type='permission',
            is_active=True,
        )
        assert priv.name == 'read'
        assert priv.display_name == 'Read Access'
        assert priv.type == 'permission'
        assert priv.is_active is True


@pytest.mark.asyncio
async def test_privilege_id_is_uuid_primary_key(session_factory):
    """Privilege id is UUID primary key."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        priv = Privilege(application_id=app_id, name='write')
        session.add(priv)
        await session.commit()
        assert isinstance(priv.id, uuid.UUID)
        assert priv.id is not None


@pytest.mark.asyncio
async def test_privilege_is_active_and_meta_behave_correctly(session_factory):
    """is_active and meta fields behave correctly."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        priv = Privilege(
            application_id=app_id,
            name='custom',
            is_active=False,
            meta={'source': 'connector', 'extra': 789},
        )
        session.add(priv)
        await session.commit()
        priv_id = priv.id

    async with session_factory() as session:
        loaded = await session.get(Privilege, priv_id)
        assert loaded is not None
        assert loaded.is_active is False
        assert loaded.meta == {'source': 'connector', 'extra': 789}
