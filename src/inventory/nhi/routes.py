# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""NHI API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.inventory.nhi.deps import get_nhi_service
from src.inventory.nhi.schemas import (
    NHIAttributeCreate,
    NHIAttributeRead,
    NHICreate,
    NHIRead,
)
from src.inventory.nhi.service import (
    DuplicateNHIAttributeError,
    InvalidApplicationIdError,
    InvalidOwnerEmployeeIdError,
    NHIAttributeNotFoundError,
    NHINotFoundError,
    NHIService,
)

router = APIRouter(prefix='/nhi', tags=['nhi'])
DependsSession = Depends(get_db)
DependsService = Depends(get_nhi_service)


@router.post('', response_model=NHIRead, status_code=201)
async def create_nhi(
    body: NHICreate,
    session: AsyncSession = DependsSession,
    service: NHIService = DependsService,
) -> NHIRead:
    """Create an NHI."""
    with translate_service_errors(
        {
            InvalidOwnerEmployeeIdError: (404, 'Employee not found'),
            InvalidApplicationIdError: (404, 'Application not found'),
        }
    ):
        nhi = await service.create_nhi(
            session,
            external_id=body.external_id,
            name=body.name,
            kind=body.kind,
            description=body.description,
            is_locked=body.is_locked,
            owner_employee_id=body.owner_employee_id,
            application_id=body.application_id,
        )
    await session.commit()
    return NHIRead.model_validate(nhi)


@router.get('', response_model=list[NHIRead])
async def list_nhi(
    session: AsyncSession = DependsSession,
    service: NHIService = DependsService,
) -> list[NHIRead]:
    """List all NHIs."""
    items = await service.list_nhi(session)
    return [NHIRead.model_validate(n) for n in items]


@router.get('/{nhi_id}', response_model=NHIRead)
async def get_nhi(
    nhi_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: NHIService = DependsService,
) -> NHIRead:
    """Get NHI by id."""
    nhi = await service.get_nhi(session, nhi_id)
    if nhi is None:
        raise HTTPException(status_code=404, detail='NHI not found')
    return NHIRead.model_validate(nhi)


@router.get('/{nhi_id}/attributes', response_model=list[NHIAttributeRead])
async def list_nhi_attributes(
    nhi_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: NHIService = DependsService,
) -> list[NHIAttributeRead]:
    """List attributes for an NHI."""
    with translate_service_errors({NHINotFoundError: (404, 'NHI not found')}):
        attrs = await service.list_attributes(session, nhi_id)
    return [NHIAttributeRead.model_validate(a) for a in attrs]


@router.post(
    '/{nhi_id}/attributes',
    response_model=NHIAttributeRead,
    status_code=201,
)
async def add_nhi_attribute(
    nhi_id: uuid.UUID,
    body: NHIAttributeCreate,
    session: AsyncSession = DependsSession,
    service: NHIService = DependsService,
) -> NHIAttributeRead:
    """Add attribute to an NHI."""
    with translate_service_errors(
        {
            NHINotFoundError: (404, 'NHI not found'),
            DuplicateNHIAttributeError: (
                409,
                lambda _exc: f'Attribute key already exists for this NHI: {body.key}',
            ),
        }
    ):
        attr = await service.add_attribute(
            session,
            nhi_id=nhi_id,
            key=body.key,
            value=body.value,
        )
    await session.commit()
    return NHIAttributeRead.model_validate(attr)


@router.delete('/{nhi_id}/attributes/{key}', status_code=204)
async def remove_nhi_attribute(
    nhi_id: uuid.UUID,
    key: str,
    session: AsyncSession = DependsSession,
    service: NHIService = DependsService,
) -> None:
    """Remove attribute from an NHI."""
    with translate_service_errors(
        {
            NHINotFoundError: (404, 'NHI not found'),
            NHIAttributeNotFoundError: (404, 'NHI attribute not found'),
        }
    ):
        await service.remove_attribute(session, nhi_id, key)
    await session.commit()
