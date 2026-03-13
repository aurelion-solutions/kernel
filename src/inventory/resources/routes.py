# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Resource API routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.resources.deps import get_resource_service
from src.inventory.resources.schemas import (
    ResourceAttributeCreate,
    ResourceAttributeRead,
    ResourceCreate,
    ResourceDataSensitivity,
    ResourceEnvironment,
    ResourcePatch,
    ResourcePrivilegeLevel,
    ResourceRead,
)
from src.inventory.resources.service import (
    DuplicateResourceAttributeError,
    DuplicateResourceError,
    ResourceApplicationNotFoundError,
    ResourceAttributeNotFoundError,
    ResourceNotFoundError,
    ResourceParentNotFoundError,
    ResourceService,
)

router = APIRouter(prefix='/resources', tags=['resources'])
DependsSession = Depends(get_db)
DependsService = Depends(get_resource_service)


@router.post('', response_model=ResourceRead, status_code=201)
async def create_resource(
    body: ResourceCreate,
    session: AsyncSession = DependsSession,
    service: ResourceService = DependsService,
) -> ResourceRead:
    """Create a resource."""
    try:
        resource = await service.create_resource(
            session,
            external_id=body.external_id,
            application_id=body.application_id,
            kind=body.kind,
            parent_id=body.parent_id,
            path=body.path,
            description=body.description,
            privilege_level=body.privilege_level,
            environment=body.environment,
            data_sensitivity=body.data_sensitivity,
        )
    except ResourceApplicationNotFoundError:
        raise HTTPException(status_code=422, detail='Application does not exist') from None
    except ResourceParentNotFoundError:
        raise HTTPException(status_code=422, detail='Parent resource does not exist') from None
    except DuplicateResourceError:
        raise HTTPException(
            status_code=409,
            detail='Resource with this (application_id, external_id) already exists',
        ) from None
    return ResourceRead.model_validate(resource)


@router.get('', response_model=list[ResourceRead])
async def list_resources(
    application_id: uuid.UUID | None = None,
    kind: str | None = None,
    privilege_level: ResourcePrivilegeLevel | None = None,
    environment: ResourceEnvironment | None = None,
    data_sensitivity: ResourceDataSensitivity | None = None,
    session: AsyncSession = DependsSession,
    service: ResourceService = DependsService,
) -> list[ResourceRead]:
    """List resources with optional filters."""
    resources = await service.list_resources(
        session,
        application_id=application_id,
        kind=kind,
        privilege_level=privilege_level,
        environment=environment,
        data_sensitivity=data_sensitivity,
    )
    return [ResourceRead.model_validate(r) for r in resources]


@router.get('/{resource_id}', response_model=ResourceRead)
async def get_resource(
    resource_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: ResourceService = DependsService,
) -> ResourceRead:
    """Get resource by id."""
    resource = await service.get_resource(session, resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail='Resource not found')
    return ResourceRead.model_validate(resource)


@router.patch('/{resource_id}', response_model=ResourceRead)
async def update_resource(
    resource_id: uuid.UUID,
    body: ResourcePatch,
    session: AsyncSession = DependsSession,
    service: ResourceService = DependsService,
) -> ResourceRead:
    """Partially update a resource."""
    try:
        resource = await service.update_resource(session, resource_id, body)
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail='Resource not found') from None
    except ResourceParentNotFoundError:
        raise HTTPException(status_code=422, detail='Parent resource does not exist') from None
    return ResourceRead.model_validate(resource)


@router.get('/{resource_id}/attributes', response_model=list[ResourceAttributeRead])
async def list_resource_attributes(
    resource_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: ResourceService = DependsService,
) -> list[ResourceAttributeRead]:
    """List attributes for a resource."""
    try:
        attrs = await service.list_attributes(session, resource_id)
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail='Resource not found') from None
    return [ResourceAttributeRead.model_validate(a) for a in attrs]


@router.post(
    '/{resource_id}/attributes',
    response_model=ResourceAttributeRead,
    status_code=201,
)
async def add_resource_attribute(
    resource_id: uuid.UUID,
    body: ResourceAttributeCreate,
    session: AsyncSession = DependsSession,
    service: ResourceService = DependsService,
) -> ResourceAttributeRead:
    """Add attribute to a resource."""
    try:
        attr = await service.add_attribute(
            session,
            resource_id=resource_id,
            key=body.key,
            value=body.value,
        )
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail='Resource not found') from None
    except DuplicateResourceAttributeError:
        raise HTTPException(
            status_code=409,
            detail=f'Attribute key already exists for this resource: {body.key}',
        ) from None
    return ResourceAttributeRead.model_validate(attr)


@router.delete('/{resource_id}/attributes/{key}', status_code=204)
async def remove_resource_attribute(
    resource_id: uuid.UUID,
    key: str,
    session: AsyncSession = DependsSession,
    service: ResourceService = DependsService,
) -> None:
    """Remove attribute from a resource."""
    try:
        await service.remove_attribute(session, resource_id, key)
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail='Resource not found') from None
    except ResourceAttributeNotFoundError:
        raise HTTPException(status_code=404, detail='Resource attribute not found') from None
