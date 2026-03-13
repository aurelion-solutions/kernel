# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Privilege repository."""

import pytest
from src.inventory.privileges.models import Privilege
from src.inventory.privileges.repository import list_by_application
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_list_by_application_loads_privileges(session_factory):
    """Repository loads privileges by application_id."""
    async with session_factory() as session:
        app = Application(name='repo-test', code='repo-test', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        priv1 = Privilege(application_id=app_id, name='priv1')
        priv2 = Privilege(application_id=app_id, name='priv2')
        session.add_all([priv1, priv2])
        await session.commit()

    async with session_factory() as session:
        privileges = await list_by_application(session, app_id)
        assert len(privileges) == 2
        names = {p.name for p in privileges}
        assert names == {'priv1', 'priv2'}


@pytest.mark.asyncio
async def test_list_by_application_returns_empty_for_no_privileges(session_factory):
    """Repository returns empty list when application has no privileges."""
    async with session_factory() as session:
        app = Application(name='empty-app', code='empty-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        privileges = await list_by_application(session, app_id)
        assert privileges == []


@pytest.mark.asyncio
async def test_list_by_application_filters_by_application(session_factory):
    """Repository returns only privileges for the given application."""
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
                Privilege(application_id=app1_id, name='app1-priv'),
                Privilege(application_id=app2_id, name='app2-priv'),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        privileges = await list_by_application(session, app1_id)
        assert len(privileges) == 1
        assert privileges[0].name == 'app1-priv'
