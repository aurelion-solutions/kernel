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
            resource_type='database',
            resource_key='ext-001',
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
            resource_type='bucket',
            resource_key='ext-fk-001',
        )
        session.add(resource)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_resource_self_referential_parent_id(session_factory) -> None:
    """Child resource with valid parent_id persists."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        parent = Resource(
            external_id='parent-001',
            application_id=app_id,
            kind='folder',
            resource_type='folder',
            resource_key='parent-001',
        )
        session.add(parent)
        await session.flush()

        child = Resource(
            external_id='child-001',
            application_id=app_id,
            kind='file',
            resource_type='file',
            resource_key='child-001',
            parent_id=parent.id,
        )
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
            resource_type='api',
            resource_key='enum-001',
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
        resource = Resource(
            external_id='attr-uniq-001',
            application_id=app_id,
            kind='storage',
            resource_type='storage',
            resource_key='attr-uniq-001',
        )
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
        r1 = Resource(
            external_id='dup-ext-001',
            application_id=app_id,
            kind='table',
            resource_type='table',
            resource_key='dup-ext-001',
        )
        session.add(r1)
        await session.commit()

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            r2 = Resource(
                external_id='dup-ext-001',
                application_id=app_id,
                kind='view',
                resource_type='view',
                resource_key='dup-ext-001-view',
            )
            session.add(r2)
            await session.commit()


def test_resource_identity_columns_exist_on_orm() -> None:
    """resource_type and resource_key columns exist on the Resource ORM class."""
    col_names = {col.name for col in Resource.__table__.columns}
    assert 'resource_type' in col_names
    assert 'resource_key' in col_names


def test_resource_identity_unique_constraint_present() -> None:
    """UNIQUE constraint uq_resources_application_id_resource_type_resource_key is declared."""
    constraint_names = {c.name for c in Resource.__table__.constraints}  # type: ignore[attr-defined]
    assert 'uq_resources_application_id_resource_type_resource_key' in constraint_names


@pytest.mark.asyncio
async def test_resource_identity_unique_constraint_enforced(session_factory) -> None:
    """Duplicate (application_id, resource_type, resource_key) raises IntegrityError."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        r1 = Resource(
            external_id='id-uniq-001',
            application_id=app_id,
            kind='table',
            resource_type='pg_table',
            resource_key='public.orders',
        )
        session.add(r1)
        await session.commit()

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            r2 = Resource(
                external_id='id-uniq-002',
                application_id=app_id,
                kind='table',
                resource_type='pg_table',
                resource_key='public.orders',
            )
            session.add(r2)
            await session.commit()
