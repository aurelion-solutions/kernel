# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for Resource model constraints and indexes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.resources.models import (
    Resource,
    ResourceAttribute,
    ResourceDataSensitivity,
    ResourceEnvironment,
    ResourcePrivilegeLevel,
)


async def _make_application_id(session) -> uuid.UUID:
    """Create an Application and return its id."""
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
async def test_resource_creation_stores_all_fields(session_factory) -> None:
    """Happy path: resource with all fields persists without error."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = Resource(
            external_id='ext-001',
            application_id=app_id,
            kind='database',
            path='/prod/db',
            description='Main database',
            privilege_level=ResourcePrivilegeLevel.admin,
            environment=ResourceEnvironment.production,
            data_sensitivity=ResourceDataSensitivity.pii,
        )
        session.add(resource)
        await session.flush()
        await session.refresh(resource)
        assert resource.id is not None
        assert resource.external_id == 'ext-001'
        assert resource.kind == 'database'
        assert resource.privilege_level == ResourcePrivilegeLevel.admin
        assert resource.environment == ResourceEnvironment.production
        assert resource.data_sensitivity == ResourceDataSensitivity.pii


@pytest.mark.asyncio
async def test_resource_fk_to_application(session_factory) -> None:
    """Resource with non-existent application_id raises IntegrityError."""
    async with session_factory() as session:
        resource = Resource(
            external_id='ext-fk-001',
            application_id=uuid.uuid4(),
            kind='bucket',
        )
        session.add(resource)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_resource_self_referential_parent_id(session_factory) -> None:
    """Child resource with valid parent_id persists."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        parent = Resource(external_id='parent-001', application_id=app_id, kind='folder')
        session.add(parent)
        await session.flush()

        child = Resource(external_id='child-001', application_id=app_id, kind='file', parent_id=parent.id)
        session.add(child)
        await session.flush()
        await session.refresh(child)
        assert child.parent_id == parent.id


@pytest.mark.asyncio
async def test_resource_enum_columns_accept_valid_values(session_factory) -> None:
    """Enum columns accept all valid values."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = Resource(
            external_id='enum-001',
            application_id=app_id,
            kind='api',
            privilege_level=ResourcePrivilegeLevel.read,
            environment=ResourceEnvironment.staging,
            data_sensitivity=ResourceDataSensitivity.financial,
        )
        session.add(resource)
        await session.flush()
        await session.refresh(resource)
        assert resource.privilege_level == ResourcePrivilegeLevel.read
        assert resource.environment == ResourceEnvironment.staging
        assert resource.data_sensitivity == ResourceDataSensitivity.financial


@pytest.mark.asyncio
async def test_resource_attribute_uniqueness_on_resource_id_key(session_factory) -> None:
    """Duplicate (resource_id, key) raises IntegrityError."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = Resource(external_id='attr-uniq-001', application_id=app_id, kind='storage')
        session.add(resource)
        await session.flush()
        attr1 = ResourceAttribute(resource_id=resource.id, key='owner', value='alice')
        session.add(attr1)
        await session.commit()
        resource_id = resource.id

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            attr2 = ResourceAttribute(resource_id=resource_id, key='owner', value='bob')
            session.add(attr2)
            await session.commit()


@pytest.mark.asyncio
async def test_resource_unique_constraint_application_id_external_id(session_factory) -> None:
    """Duplicate (application_id, external_id) raises IntegrityError."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        r1 = Resource(external_id='dup-ext-001', application_id=app_id, kind='table')
        session.add(r1)
        await session.commit()

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            r2 = Resource(external_id='dup-ext-001', application_id=app_id, kind='view')
            session.add(r2)
            await session.commit()
