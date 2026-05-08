# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Connector result ingest API — persists to staging only (see ``ingest.models``)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.engines.ingest.schemas import (
    ConnectorResultIngestRequest,
    ConnectorResultIngestResponse,
)
from src.engines.ingest.service import (
    ApplicationNotFoundError,
    ingest_connector_result,
)
from src.platform.logs.deps import get_log_service

router = APIRouter(tags=['connector-results'])
DependsSession = Depends(get_db)
DependsLogService = Depends(get_log_service)


@router.post(
    '/connector-results',
    response_model=ConnectorResultIngestResponse,
    status_code=200,
)
async def post_connector_results(
    request: ConnectorResultIngestRequest,
    session: AsyncSession = DependsSession,
    log_service=DependsLogService,
) -> ConnectorResultIngestResponse:
    """Accept connector result envelope; store one row in ``staging_connector_results``."""
    try:
        await ingest_connector_result(session, request, log_service=log_service)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    except ApplicationNotFoundError as err:
        raise HTTPException(
            status_code=404,
            detail=f'Application {err.application_id} not found',
        ) from err
    await session.commit()
    return ConnectorResultIngestResponse(
        task_id=request.task_id,
        result_id=request.result_id,
        operation=request.operation,
        status=request.status,
    )
