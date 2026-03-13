# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Subject API routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.subjects.deps import get_subject_service
from src.inventory.subjects.schemas import (
    SubjectAttributeCreate,
    SubjectAttributeRead,
    SubjectCreate,
    SubjectKind,
    SubjectPatch,
    SubjectRead,
    SubjectStatus,
)
from src.inventory.subjects.service import (
    DuplicateSubjectAttributeError,
    InvalidSubjectStatusForKindError,
    SubjectAttributeNotFoundError,
    SubjectNotFoundError,
    SubjectPrincipalAlreadyBoundError,
    SubjectPrincipalNotFoundError,
    SubjectService,
)

router = APIRouter(prefix='/subjects', tags=['subjects'])
DependsSession = Depends(get_db)
DependsService = Depends(get_subject_service)


@router.post('', response_model=SubjectRead, status_code=201)
async def create_subject(
    body: SubjectCreate,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> SubjectRead:
    """Create a subject."""
    try:
        subject = await service.create_subject(
            session,
            external_id=body.external_id,
            kind=body.kind,
            nhi_kind=body.nhi_kind,
            principal_employee_id=body.principal_employee_id,
            principal_nhi_id=body.principal_nhi_id,
            principal_customer_id=body.principal_customer_id,
            status=body.status,
        )
    except SubjectPrincipalNotFoundError:
        raise HTTPException(status_code=422, detail='Referenced principal entity does not exist') from None
    except SubjectPrincipalAlreadyBoundError:
        raise HTTPException(status_code=409, detail='Principal is already bound to a Subject') from None
    return SubjectRead.model_validate(subject)


@router.get('', response_model=list[SubjectRead])
async def list_subjects(
    kind: SubjectKind | None = None,
    status: SubjectStatus | None = None,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> list[SubjectRead]:
    """List subjects with optional filters."""
    subjects = await service.list_subjects(session, kind=kind, status=status)
    return [SubjectRead.model_validate(s) for s in subjects]


@router.get('/{subject_id}', response_model=SubjectRead)
async def get_subject(
    subject_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> SubjectRead:
    """Get subject by id."""
    subject = await service.get_subject(session, subject_id)
    if subject is None:
        raise HTTPException(status_code=404, detail='Subject not found')
    return SubjectRead.model_validate(subject)


@router.patch('/{subject_id}', response_model=SubjectRead)
async def update_subject(
    subject_id: uuid.UUID,
    body: SubjectPatch,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> SubjectRead:
    """Partially update a subject (status only)."""
    try:
        subject = await service.update_subject(session, subject_id, body)
    except SubjectNotFoundError:
        raise HTTPException(status_code=404, detail='Subject not found') from None
    except InvalidSubjectStatusForKindError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return SubjectRead.model_validate(subject)


@router.get('/{subject_id}/attributes', response_model=list[SubjectAttributeRead])
async def list_subject_attributes(
    subject_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> list[SubjectAttributeRead]:
    """List attributes for a subject."""
    try:
        attrs = await service.list_attributes(session, subject_id)
    except SubjectNotFoundError:
        raise HTTPException(status_code=404, detail='Subject not found') from None
    return [SubjectAttributeRead.model_validate(a) for a in attrs]


@router.post(
    '/{subject_id}/attributes',
    response_model=SubjectAttributeRead,
    status_code=201,
)
async def add_subject_attribute(
    subject_id: uuid.UUID,
    body: SubjectAttributeCreate,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> SubjectAttributeRead:
    """Add attribute to a subject."""
    try:
        attr = await service.add_attribute(
            session,
            subject_id=subject_id,
            key=body.key,
            value=body.value,
        )
    except SubjectNotFoundError:
        raise HTTPException(status_code=404, detail='Subject not found') from None
    except DuplicateSubjectAttributeError:
        raise HTTPException(
            status_code=409,
            detail=f'Attribute key already exists for this subject: {body.key}',
        ) from None
    return SubjectAttributeRead.model_validate(attr)


@router.delete('/{subject_id}/attributes/{key}', status_code=204)
async def remove_subject_attribute(
    subject_id: uuid.UUID,
    key: str,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> None:
    """Remove attribute from a subject."""
    try:
        await service.remove_attribute(session, subject_id, key)
    except SubjectNotFoundError:
        raise HTTPException(status_code=404, detail='Subject not found') from None
    except SubjectAttributeNotFoundError:
        raise HTTPException(status_code=404, detail='Subject attribute not found') from None
