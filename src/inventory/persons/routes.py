# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Person API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.persons.deps import get_person_service
from src.inventory.persons.schemas import (
    PersonAttributeCreate,
    PersonAttributeRead,
    PersonCreate,
    PersonRead,
)
from src.inventory.persons.service import (
    DuplicatePersonAttributeError,
    PersonAttributeNotFoundError,
    PersonNotFoundError,
    PersonService,
)

router = APIRouter(prefix='/persons', tags=['persons'])
DependsSession = Depends(get_db)
DependsService = Depends(get_person_service)


@router.post('', response_model=PersonRead, status_code=201)
async def create_person(
    body: PersonCreate,
    session: AsyncSession = DependsSession,
    service: PersonService = DependsService,
) -> PersonRead:
    """Create a person."""
    person = await service.create_person(
        session,
        external_id=body.external_id,
        description=body.description,
    )
    return PersonRead.model_validate(person)


@router.get('', response_model=list[PersonRead])
async def list_persons(
    session: AsyncSession = DependsSession,
    service: PersonService = DependsService,
) -> list[PersonRead]:
    """List all persons."""
    persons = await service.list_persons(session)
    return [PersonRead.model_validate(p) for p in persons]


@router.get('/{person_id}', response_model=PersonRead)
async def get_person(
    person_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: PersonService = DependsService,
) -> PersonRead:
    """Get person by id."""
    person = await service.get_person(session, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail='Person not found')
    return PersonRead.model_validate(person)


@router.get('/{person_id}/attributes', response_model=list[PersonAttributeRead])
async def list_person_attributes(
    person_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: PersonService = DependsService,
) -> list[PersonAttributeRead]:
    """List attributes for a person."""
    try:
        attrs = await service.list_attributes(session, person_id)
    except PersonNotFoundError:
        raise HTTPException(status_code=404, detail='Person not found') from None
    return [PersonAttributeRead.model_validate(a) for a in attrs]


@router.post(
    '/{person_id}/attributes',
    response_model=PersonAttributeRead,
    status_code=201,
)
async def add_person_attribute(
    person_id: uuid.UUID,
    body: PersonAttributeCreate,
    session: AsyncSession = DependsSession,
    service: PersonService = DependsService,
) -> PersonAttributeRead:
    """Add attribute to a person."""
    try:
        attr = await service.add_attribute(
            session,
            person_id=person_id,
            key=body.key,
            value=body.value,
        )
    except PersonNotFoundError:
        raise HTTPException(status_code=404, detail='Person not found') from None
    except DuplicatePersonAttributeError:
        raise HTTPException(
            status_code=409,
            detail=f'Attribute key already exists for this person: {body.key}',
        ) from None
    return PersonAttributeRead.model_validate(attr)


@router.delete('/{person_id}/attributes/{key}', status_code=204)
async def remove_person_attribute(
    person_id: uuid.UUID,
    key: str,
    session: AsyncSession = DependsSession,
    service: PersonService = DependsService,
) -> None:
    """Remove attribute from a person."""
    try:
        await service.remove_attribute(session, person_id, key)
    except PersonNotFoundError:
        raise HTTPException(status_code=404, detail='Person not found') from None
    except PersonAttributeNotFoundError:
        raise HTTPException(status_code=404, detail='Person attribute not found') from None
