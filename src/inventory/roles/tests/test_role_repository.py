# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Role repository."""

import pytest
from src.inventory.roles.models import Role
from src.inventory.roles.repository import list_by_application
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_list_by_application_loads_roles(session_factory):
    """Repository loads roles by application_id."""
    async with session_factory() as session:
        app = Application(name='repo-test', code='repo-test', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        role1 = Role(application_id=app_id, name='role1')
        role2 = Role(application_id=app_id, name='role2')
        session.add_all([role1, role2])
        await session.commit()

    async with session_factory() as session:
        roles = await list_by_application(session, app_id)
        assert len(roles) == 2
        names = {r.name for r in roles}
        assert names == {'role1', 'role2'}


@pytest.mark.asyncio
async def test_list_by_application_returns_empty_for_no_roles(session_factory):
    """Repository returns empty list when application has no roles."""
    async with session_factory() as session:
        app = Application(name='empty-app', code='empty-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        roles = await list_by_application(session, app_id)
        assert roles == []


@pytest.mark.asyncio
async def test_list_by_application_filters_by_application(session_factory):
    """Repository returns only roles for the given application."""
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
                Role(application_id=app1_id, name='app1-role'),
                Role(application_id=app2_id, name='app2-role'),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        roles = await list_by_application(session, app1_id)
        assert len(roles) == 1
        assert roles[0].name == 'app1-role'
