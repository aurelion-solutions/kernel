# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import uuid

import pytest
from src.platform.applications.models import Application
from src.platform.applications.repository import get_application_by_code, get_application_by_id


@pytest.mark.asyncio
async def test_get_application_by_id_returns_application(session_factory):
    """Repository loads application by id."""
    async with session_factory() as session:
        app = Application(
            name='repo-test',
            code='repo-test',
            config={'x': 1},
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        loaded = await get_application_by_id(session, app_id)
        assert loaded is not None
        assert loaded.name == 'repo-test'


@pytest.mark.asyncio
async def test_get_application_by_id_returns_none_when_not_found(session_factory):
    """Repository returns None when application does not exist."""
    async with session_factory() as session:
        result = await get_application_by_id(session, uuid.uuid4())
        assert result is None


@pytest.mark.asyncio
async def test_get_application_by_code_returns_match(session_factory):
    """Repository returns application by code."""
    async with session_factory() as session:
        app = Application(name='code-test', code='some-code', config={})
        session.add(app)
        await session.commit()

    async with session_factory() as session:
        loaded = await get_application_by_code(session, 'some-code')
        assert loaded is not None
        assert loaded.code == 'some-code'


@pytest.mark.asyncio
async def test_get_application_by_code_returns_none_when_missing(session_factory):
    """Repository returns None for unknown code."""
    async with session_factory() as session:
        result = await get_application_by_code(session, 'nonexistent-code')
        assert result is None
