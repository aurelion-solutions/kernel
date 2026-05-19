# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Subject API routes."""

from __future__ import annotations

from typing import Annotated, cast
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.inventory.subjects.deps import get_subject_service
from src.inventory.subjects.schemas import (
    SubjectAttributeCreate,
    SubjectAttributeRead,
    SubjectBulkRequest,
    SubjectBulkResponse,
    SubjectCreate,
    SubjectKind,
    SubjectListResponse,
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
    UnknownPersonExternalIdsError,
    UnresolvedEmployeesForPersonsError,
)

router = APIRouter(prefix='/subjects', tags=['subjects'])
DependsSession = Depends(get_db)
DependsService = Depends(get_subject_service)


@router.post('/bulk', response_model=SubjectBulkResponse, status_code=200)
async def bulk_upsert_subjects(
    body: SubjectBulkRequest,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> SubjectBulkResponse:
    """Bulk-upsert employee-kind subjects by (kind, external_id)."""
    with translate_service_errors(
        {
            UnknownPersonExternalIdsError: (
                422,
                lambda exc: (
                    'Unknown person_external_ids: ' + ', '.join(cast(UnknownPersonExternalIdsError, exc).missing[:20])
                ),
            ),
            UnresolvedEmployeesForPersonsError: (
                422,
                lambda exc: (
                    'No employee record for person_external_ids: '
                    + ', '.join(cast(UnresolvedEmployeesForPersonsError, exc).missing[:20])
                ),
            ),
            SubjectPrincipalAlreadyBoundError: (
                409,
                lambda exc: (
                    'Employee already bound to a different Subject: '
                    + ', '.join(f'{p}->{s}' for p, s in cast(SubjectPrincipalAlreadyBoundError, exc).conflicts[:20])
                    if cast(SubjectPrincipalAlreadyBoundError, exc).conflicts
                    else 'An employee is already bound to a different Subject'
                ),
            ),
        }
    ):
        subjects = await service.bulk_upsert_employee_subjects(session, body.items)
    await session.commit()
    return SubjectBulkResponse(
        upserted=len(subjects),
        ids=[s.id for s in subjects],
    )


@router.post('', response_model=SubjectRead, status_code=201)
async def create_subject(
    body: SubjectCreate,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> SubjectRead:
    """Create a subject."""
    with translate_service_errors(
        {
            SubjectPrincipalNotFoundError: (422, 'Referenced principal entity does not exist'),
            SubjectPrincipalAlreadyBoundError: (409, 'Principal is already bound to a Subject'),
        }
    ):
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
    await session.commit()
    return SubjectRead.model_validate(subject)


@router.get('', response_model=SubjectListResponse)
async def list_subjects(
    kind: SubjectKind | None = None,
    status: SubjectStatus | None = None,
    principal_employee_id: Annotated[uuid.UUID | None, Query()] = None,
    principal_nhi_id: Annotated[uuid.UUID | None, Query()] = None,
    principal_customer_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> SubjectListResponse:
    """List subjects with optional filters and pagination."""
    subjects, total = await service.list_subjects(
        session,
        kind=kind,
        status=status,
        principal_employee_id=principal_employee_id,
        principal_nhi_id=principal_nhi_id,
        principal_customer_id=principal_customer_id,
        limit=limit,
        offset=offset,
    )
    return SubjectListResponse(
        items=[SubjectRead.model_validate(s) for s in subjects],
        total=total,
        limit=limit,
        offset=offset,
    )


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
    with translate_service_errors(
        {
            SubjectNotFoundError: (404, 'Subject not found'),
            InvalidSubjectStatusForKindError: (422, lambda exc: str(exc)),
        }
    ):
        subject = await service.update_subject(session, subject_id, body)
    await session.commit()
    return SubjectRead.model_validate(subject)


@router.get('/{subject_id}/attributes', response_model=list[SubjectAttributeRead])
async def list_subject_attributes(
    subject_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> list[SubjectAttributeRead]:
    """List attributes for a subject."""
    with translate_service_errors({SubjectNotFoundError: (404, 'Subject not found')}):
        attrs = await service.list_attributes(session, subject_id)
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
    with translate_service_errors(
        {
            SubjectNotFoundError: (404, 'Subject not found'),
            DuplicateSubjectAttributeError: (
                409,
                lambda _exc: f'Attribute key already exists for this subject: {body.key}',
            ),
        }
    ):
        attr = await service.add_attribute(
            session,
            subject_id=subject_id,
            key=body.key,
            value=body.value,
        )
    await session.commit()
    return SubjectAttributeRead.model_validate(attr)


@router.delete('/{subject_id}/attributes/{key}', status_code=204)
async def remove_subject_attribute(
    subject_id: uuid.UUID,
    key: str,
    session: AsyncSession = DependsSession,
    service: SubjectService = DependsService,
) -> None:
    """Remove attribute from a subject."""
    with translate_service_errors(
        {
            SubjectNotFoundError: (404, 'Subject not found'),
            SubjectAttributeNotFoundError: (404, 'Subject attribute not found'),
        }
    ):
        await service.remove_attribute(session, subject_id, key)
    await session.commit()
