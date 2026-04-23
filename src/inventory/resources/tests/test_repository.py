# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Repository-level tests for Resource identity lookup."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.resources.repository import (
    create_resource,
    get_resource_by_identity,
)


async def _make_application_id(session) -> uuid.UUID:
    from src.platform.applications.models import Application

    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


@pytest.mark.asyncio
async def test_get_resource_by_identity_hit(session_factory) -> None:
    """get_resource_by_identity returns the resource when the identity triple exists."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await create_resource(
            session,
            external_id='repo-identity-001',
            application_id=app_id,
            kind='table',
            resource_type='snowflake_table',
            resource_key='finance.public.orders',
        )
        await session.commit()
        resource_id = resource.id

    async with session_factory() as session:
        found = await get_resource_by_identity(
            session,
            app_id,
            'snowflake_table',
            'finance.public.orders',
        )

    assert found is not None
    assert found.id == resource_id
    assert found.resource_type == 'snowflake_table'
    assert found.resource_key == 'finance.public.orders'


@pytest.mark.asyncio
async def test_get_resource_by_identity_miss(session_factory) -> None:
    """get_resource_by_identity returns None when the identity triple does not exist."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await session.commit()

    async with session_factory() as session:
        found = await get_resource_by_identity(
            session,
            app_id,
            'nonexistent_type',
            'nonexistent_key',
        )

    assert found is None
