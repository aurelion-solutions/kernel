# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.platform.applications.exceptions import (
    ApplicationCodeAlreadyExistsError,
    ApplicationNotFoundError,
)
from src.platform.applications.repository import get_application_by_id, list_applications
from src.platform.applications.schemas import ApplicationCreate, ApplicationResponse, ApplicationUpdate
from src.platform.applications.service import create_application, delete_application, update_application
from src.platform.connectors.schemas import ConnectorInstanceResponse
from src.platform.logs.deps import get_log_service

router = APIRouter(prefix='/applications', tags=['applications'])

DependsSession = Depends(get_db)
DependsLogService = Depends(get_log_service)


@router.get('', response_model=list[ApplicationResponse])
async def list_(
    session: AsyncSession = DependsSession,
) -> list[ApplicationResponse]:
    apps = await list_applications(session)
    return [
        ApplicationResponse(
            id=str(a.id),
            name=a.name,
            code=a.code,
            config=a.config,
            required_connector_tags=a.required_connector_tags,
            is_active=a.is_active,
            created_at=a.created_at,
            updated_at=a.updated_at,
        )
        for a in apps
    ]


@router.get(
    '/{application_id}/matching-connector-instances',
    response_model=list[ConnectorInstanceResponse],
)
async def list_matching_connector_instances(
    application_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    online_only: bool = Query(
        True,
        description='If true, only instances considered online (recent last_seen_at).',
    ),
) -> list[ConnectorInstanceResponse]:
    """Connector instances whose tags satisfy the application's ``required_connector_tags``."""
    row = await get_application_by_id(session, application_id)
    if row is None:
        raise HTTPException(status_code=404, detail='Application not found')
    matches = await row.matching_connector_instances(
        session,
        online_only=online_only,
    )
    return [ConnectorInstanceResponse.from_instance(m) for m in matches]


@router.post('', response_model=ApplicationResponse, status_code=201)
async def create(
    request: ApplicationCreate,
    session: AsyncSession = DependsSession,
    log_service=DependsLogService,
) -> ApplicationResponse:
    try:
        app = await create_application(session, request, log_service=log_service)
    except ApplicationCodeAlreadyExistsError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    return ApplicationResponse(
        id=str(app.id),
        name=app.name,
        code=app.code,
        config=app.config,
        required_connector_tags=app.required_connector_tags,
        is_active=app.is_active,
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


@router.patch('/{application_id}', response_model=ApplicationResponse)
async def patch_(
    application_id: uuid.UUID,
    request: ApplicationUpdate,
    session: AsyncSession = DependsSession,
    log_service=DependsLogService,
) -> ApplicationResponse:
    try:
        app = await update_application(session, application_id, request, log_service=log_service)
    except ApplicationNotFoundError as err:
        raise HTTPException(status_code=404, detail='Application not found') from err
    except ApplicationCodeAlreadyExistsError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    return ApplicationResponse(
        id=str(app.id),
        name=app.name,
        code=app.code,
        config=app.config,
        required_connector_tags=app.required_connector_tags,
        is_active=app.is_active,
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


@router.delete('/{application_id}', status_code=204)
async def delete(
    application_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    log_service=DependsLogService,
) -> None:
    try:
        await delete_application(session, application_id, log_service=log_service)
    except ApplicationNotFoundError as err:
        raise HTTPException(status_code=404, detail='Application not found') from err
