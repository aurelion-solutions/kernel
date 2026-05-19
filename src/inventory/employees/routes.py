# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Employee API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.employees.deps import get_employee_service
from src.inventory.employees.lake_service import (
    EmployeeLakeNotConfiguredError,
    EmployeeLakeService,
    EmployeeLakeWriteError,
)
from src.inventory.employees.schemas import (
    EmployeeAttributeCreate,
    EmployeeAttributeRead,
    EmployeeBulkRequest,
    EmployeeBulkResponse,
    EmployeeCreate,
    EmployeeListItem,
    EmployeeListResponse,
    EmployeePatch,
    EmployeeRead,
)
from src.inventory.employees.service import (
    DuplicateEmployeeAttributeError,
    EmployeeAttributeNotFoundError,
    EmployeeNotFoundError,
    EmployeeService,
    InvalidOrgUnitIdError,
    InvalidPersonIdError,
)

router = APIRouter(prefix='/employees', tags=['employees'])
DependsSession = Depends(get_db)
DependsService = Depends(get_employee_service)


@router.post('/bulk', response_model=EmployeeBulkResponse, status_code=200)
async def bulk_upsert_employees(
    body: EmployeeBulkRequest,
    request: Request,
) -> EmployeeBulkResponse:
    """Bulk-ingest employees into the lake (raw.employees).  PG is populated later via reconcile+apply."""
    lake_catalog = getattr(request.app.state, 'lake_catalog', None)
    service = EmployeeLakeService(lake_catalog=lake_catalog)
    try:
        result = await service.upsert_batch(body.items, ingest_batch_id=uuid.uuid4())
    except EmployeeLakeNotConfiguredError:
        raise HTTPException(status_code=503, detail='Lake backend not configured') from None
    except EmployeeLakeWriteError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return EmployeeBulkResponse(row_count=result.row_count, snapshot_id=result.snapshot_id)


@router.post('', response_model=EmployeeRead, status_code=201)
async def create_employee(
    body: EmployeeCreate,
    session: AsyncSession = DependsSession,
    service: EmployeeService = DependsService,
) -> EmployeeRead:
    """Create an employee."""
    try:
        employee = await service.create_employee(
            session,
            person_id=body.person_id,
            is_locked=body.is_locked,
            description=body.description,
            org_unit_id=body.org_unit_id,
        )
    except InvalidPersonIdError:
        raise HTTPException(
            status_code=404,
            detail='Person not found',
        ) from None
    except InvalidOrgUnitIdError:
        raise HTTPException(
            status_code=404,
            detail='Org-unit not found',
        ) from None
    await session.commit()
    return EmployeeRead.model_validate(employee)


@router.get('', response_model=EmployeeListResponse)
async def list_employees(
    session: AsyncSession = DependsSession,
    service: EmployeeService = DependsService,
    limit: int = Query(..., ge=1, le=1000),
    offset: int = Query(..., ge=0),
) -> EmployeeListResponse:
    """Return employees ordered by id ascending, paginated by limit/offset."""
    employees, total = await service.list_employees(session, limit=limit, offset=offset)
    return EmployeeListResponse(
        items=[EmployeeListItem.model_validate(e) for e in employees],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get('/{employee_id}', response_model=EmployeeRead)
async def get_employee(
    employee_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: EmployeeService = DependsService,
) -> EmployeeRead:
    """Get employee by id."""
    employee = await service.get_employee(session, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail='Employee not found')
    return EmployeeRead.model_validate(employee)


@router.patch('/{employee_id}', response_model=EmployeeRead)
async def patch_employee(
    employee_id: uuid.UUID,
    body: EmployeePatch,
    session: AsyncSession = DependsSession,
    service: EmployeeService = DependsService,
) -> EmployeeRead:
    """Patch employee fields and/or attributes. Emits one ``inventory.employee.updated``."""
    try:
        employee = await service.update_employee(session, employee_id, body)
    except EmployeeNotFoundError:
        raise HTTPException(status_code=404, detail='Employee not found') from None
    except InvalidOrgUnitIdError:
        raise HTTPException(status_code=404, detail='Org-unit not found') from None
    await session.commit()
    return EmployeeRead.model_validate(employee)


@router.get('/{employee_id}/attributes', response_model=list[EmployeeAttributeRead])
async def list_employee_attributes(
    employee_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: EmployeeService = DependsService,
) -> list[EmployeeAttributeRead]:
    """List attributes for an employee."""
    try:
        attrs = await service.list_attributes(session, employee_id)
    except EmployeeNotFoundError:
        raise HTTPException(status_code=404, detail='Employee not found') from None
    return [EmployeeAttributeRead.model_validate(a) for a in attrs]


@router.post(
    '/{employee_id}/attributes',
    response_model=EmployeeAttributeRead,
    status_code=201,
)
async def add_employee_attribute(
    employee_id: uuid.UUID,
    body: EmployeeAttributeCreate,
    session: AsyncSession = DependsSession,
    service: EmployeeService = DependsService,
) -> EmployeeAttributeRead:
    """Add attribute to an employee."""
    try:
        attr = await service.add_attribute(
            session,
            employee_id=employee_id,
            key=body.key,
            value=body.value,
        )
    except EmployeeNotFoundError:
        raise HTTPException(status_code=404, detail='Employee not found') from None
    except DuplicateEmployeeAttributeError:
        raise HTTPException(
            status_code=409,
            detail=f'Attribute key already exists for this employee: {body.key}',
        ) from None
    await session.commit()
    return EmployeeAttributeRead.model_validate(attr)


@router.delete('/{employee_id}/attributes/{key}', status_code=204)
async def remove_employee_attribute(
    employee_id: uuid.UUID,
    key: str,
    session: AsyncSession = DependsSession,
    service: EmployeeService = DependsService,
) -> None:
    """Remove attribute from an employee."""
    try:
        await service.remove_attribute(session, employee_id, key)
    except EmployeeNotFoundError:
        raise HTTPException(status_code=404, detail='Employee not found') from None
    except EmployeeAttributeNotFoundError:
        raise HTTPException(status_code=404, detail='Employee attribute not found') from None
    await session.commit()
