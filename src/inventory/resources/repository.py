# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Resource repository for PostgreSQL access."""

from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.resources.models import (
    Resource,
    ResourceAttribute,
    ResourceDataSensitivity,
    ResourceEnvironment,
    ResourcePrivilegeLevel,
)


async def create_resource(
    session: AsyncSession,
    *,
    external_id: str,
    application_id: uuid.UUID,
    kind: str,
    resource_type: str,
    resource_key: str,
    parent_id: uuid.UUID | None = None,
    path: str | None = None,
    description: str | None = None,
    privilege_level: ResourcePrivilegeLevel | None = None,
    environment: ResourceEnvironment | None = None,
    data_sensitivity: ResourceDataSensitivity | None = None,
) -> Resource:
    """Create and persist a resource."""
    resource = Resource(
        external_id=external_id,
        application_id=application_id,
        kind=kind,
        resource_type=resource_type,
        resource_key=resource_key,
        parent_id=parent_id,
        path=path,
        description=description,
        privilege_level=privilege_level,
        environment=environment,
        data_sensitivity=data_sensitivity,
    )
    session.add(resource)
    await session.flush()
    await session.refresh(resource)
    return resource


async def get_resource_by_id(
    session: AsyncSession,
    resource_id: uuid.UUID,
) -> Resource | None:
    """Load resource by id."""
    result = await session.execute(select(Resource).where(Resource.id == resource_id))
    return result.scalar_one_or_none()


async def get_resource_by_identity(
    session: AsyncSession,
    application_id: uuid.UUID,
    resource_type: str,
    resource_key: str,
) -> Resource | None:
    """Load resource by (application_id, resource_type, resource_key). Returns None if not found."""
    result = await session.execute(
        select(Resource).where(
            Resource.application_id == application_id,
            Resource.resource_type == resource_type,
            Resource.resource_key == resource_key,
        )
    )
    return result.scalar_one_or_none()


async def get_resource_by_application_and_external_id(
    session: AsyncSession,
    application_id: uuid.UUID,
    external_id: str,
) -> Resource | None:
    """Load resource by (application_id, external_id). Returns None if not found."""
    result = await session.execute(
        select(Resource).where(
            Resource.application_id == application_id,
            Resource.external_id == external_id,
        )
    )
    return result.scalar_one_or_none()


async def list_resources(
    session: AsyncSession,
    *,
    application_id: uuid.UUID | None = None,
    kind: str | None = None,
    privilege_level: ResourcePrivilegeLevel | None = None,
    environment: ResourceEnvironment | None = None,
    data_sensitivity: ResourceDataSensitivity | None = None,
) -> list[Resource]:
    """List resources with optional filters."""
    query = select(Resource).order_by(Resource.id)
    if application_id is not None:
        query = query.where(Resource.application_id == application_id)
    if kind is not None:
        query = query.where(Resource.kind == kind)
    if privilege_level is not None:
        query = query.where(Resource.privilege_level == privilege_level)
    if environment is not None:
        query = query.where(Resource.environment == environment)
    if data_sensitivity is not None:
        query = query.where(Resource.data_sensitivity == data_sensitivity)
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_resource(
    session: AsyncSession,
    resource: Resource,
    *,
    fields_to_update: dict[str, Any],
) -> set[str]:
    """Apply only changed fields from dict. Returns set of changed field names."""
    changed: set[str] = set()
    for field, value in fields_to_update.items():
        if getattr(resource, field) != value:
            setattr(resource, field, value)
            changed.add(field)
    if changed:
        await session.flush()
        await session.refresh(resource)
    return changed


async def list_resource_attributes(
    session: AsyncSession,
    resource_id: uuid.UUID,
) -> list[ResourceAttribute]:
    """List attributes for a resource, ordered by key."""
    result = await session.execute(
        select(ResourceAttribute).where(ResourceAttribute.resource_id == resource_id).order_by(ResourceAttribute.key)
    )
    return list(result.scalars().all())


async def create_resource_attribute(
    session: AsyncSession,
    *,
    resource_id: uuid.UUID,
    key: str,
    value: str,
) -> ResourceAttribute:
    """Create and persist a resource attribute."""
    attr = ResourceAttribute(
        resource_id=resource_id,
        key=key,
        value=value,
    )
    session.add(attr)
    await session.flush()
    await session.refresh(attr)
    return attr


async def _get_resource_attribute_by_key(
    session: AsyncSession,
    resource_id: uuid.UUID,
    key: str,
) -> ResourceAttribute | None:
    """Load resource attribute by resource_id and key."""
    result = await session.execute(
        select(ResourceAttribute).where(
            ResourceAttribute.resource_id == resource_id,
            ResourceAttribute.key == key,
        )
    )
    return result.scalar_one_or_none()


async def delete_resource_attribute(
    session: AsyncSession,
    resource_id: uuid.UUID,
    key: str,
) -> bool:
    """Delete resource attribute by resource_id and key. Returns True if deleted."""
    attr = await _get_resource_attribute_by_key(session, resource_id, key)
    if attr is None:
        return False
    await session.delete(attr)
    return True
