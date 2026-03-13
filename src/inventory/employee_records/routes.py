# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EmployeeRecord API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.employee_records.deps import get_employee_record_service
from src.inventory.employee_records.schemas import (
    EmployeeRecordAttributeCreate,
    EmployeeRecordAttributeRead,
    EmployeeRecordCreate,
    EmployeeRecordRead,
)
from src.inventory.employee_records.service import (
    DuplicateEmployeeRecordAttributeError,
    EmployeeRecordAttributeNotFoundError,
    EmployeeRecordNotFoundError,
    EmployeeRecordService,
    InvalidApplicationIdError,
)

router = APIRouter(prefix='/employee-records', tags=['employee-records'])
DependsSession = Depends(get_db)
DependsService = Depends(get_employee_record_service)


@router.post('', response_model=EmployeeRecordRead, status_code=201)
async def create_employee_record(
    body: EmployeeRecordCreate,
    session: AsyncSession = DependsSession,
    service: EmployeeRecordService = DependsService,
) -> EmployeeRecordRead:
    """Create an employee record."""
    try:
        record = await service.create_employee_record(
            session,
            external_id=body.external_id,
            application_id=body.application_id,
            description=body.description,
        )
    except InvalidApplicationIdError:
        raise HTTPException(
            status_code=404,
            detail='Application not found',
        ) from None
    return EmployeeRecordRead.model_validate(record)


@router.get('', response_model=list[EmployeeRecordRead])
async def list_employee_records(
    session: AsyncSession = DependsSession,
    service: EmployeeRecordService = DependsService,
) -> list[EmployeeRecordRead]:
    """List all employee records."""
    records = await service.list_employee_records(session)
    return [EmployeeRecordRead.model_validate(r) for r in records]


@router.get('/{employee_record_id}', response_model=EmployeeRecordRead)
async def get_employee_record(
    employee_record_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: EmployeeRecordService = DependsService,
) -> EmployeeRecordRead:
    """Get employee record by id."""
    record = await service.get_employee_record(session, employee_record_id)
    if record is None:
        raise HTTPException(status_code=404, detail='Employee record not found')
    return EmployeeRecordRead.model_validate(record)


@router.get(
    '/{employee_record_id}/attributes',
    response_model=list[EmployeeRecordAttributeRead],
)
async def list_employee_record_attributes(
    employee_record_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: EmployeeRecordService = DependsService,
) -> list[EmployeeRecordAttributeRead]:
    """List attributes for an employee record."""
    try:
        attrs = await service.list_attributes(session, employee_record_id)
    except EmployeeRecordNotFoundError:
        raise HTTPException(status_code=404, detail='Employee record not found') from None
    return [EmployeeRecordAttributeRead.model_validate(a) for a in attrs]


@router.post(
    '/{employee_record_id}/attributes',
    response_model=EmployeeRecordAttributeRead,
    status_code=201,
)
async def add_employee_record_attribute(
    employee_record_id: uuid.UUID,
    body: EmployeeRecordAttributeCreate,
    session: AsyncSession = DependsSession,
    service: EmployeeRecordService = DependsService,
) -> EmployeeRecordAttributeRead:
    """Add attribute to an employee record."""
    try:
        attr = await service.add_attribute(
            session,
            employee_record_id=employee_record_id,
            key=body.key,
            value=body.value,
        )
    except EmployeeRecordNotFoundError:
        raise HTTPException(status_code=404, detail='Employee record not found') from None
    except DuplicateEmployeeRecordAttributeError:
        raise HTTPException(
            status_code=409,
            detail=f'Attribute key already exists for this employee record: {body.key}',
        ) from None
    return EmployeeRecordAttributeRead.model_validate(attr)


@router.delete('/{employee_record_id}/attributes/{key}', status_code=204)
async def remove_employee_record_attribute(
    employee_record_id: uuid.UUID,
    key: str,
    session: AsyncSession = DependsSession,
    service: EmployeeRecordService = DependsService,
) -> None:
    """Remove attribute from an employee record."""
    try:
        await service.remove_attribute(session, employee_record_id, key)
    except EmployeeRecordNotFoundError:
        raise HTTPException(status_code=404, detail='Employee record not found') from None
    except EmployeeRecordAttributeNotFoundError:
        raise HTTPException(status_code=404, detail='Employee record attribute not found') from None
