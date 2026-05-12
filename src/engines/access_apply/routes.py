# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.engines.access_apply.create_account import create_account
from src.engines.access_apply.delete_account import delete_account
from src.engines.access_apply.schemas import AccountCreateRequest
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.connectors.client import ConnectorClient
from src.platform.connectors.deps import get_connector_client
from src.platform.connectors.exceptions import ConnectorInstanceNotFoundError
from src.platform.logs.deps import get_log_service

router = APIRouter(prefix='/applications', tags=['access_apply'])

DependsSession = Depends(get_db)
DependsConnectorClient = Depends(get_connector_client)
DependsLogService = Depends(get_log_service)


@router.post('/{application_id}/accounts', status_code=201)
async def post_account(
    application_id: uuid.UUID,
    request: AccountCreateRequest,
    session: AsyncSession = DependsSession,
    connector: ConnectorClient = DependsConnectorClient,
    log_service=DependsLogService,
) -> dict:
    try:
        return await create_account(
            session,
            application_id,
            request,
            connector,
            log_service=log_service,
        )
    except ApplicationNotFoundError as err:
        raise HTTPException(status_code=404, detail='Application not found') from err
    except ConnectorInstanceNotFoundError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except (ConnectionError, TimeoutError, OSError) as err:
        raise HTTPException(status_code=503, detail=f'Connector error: {err}') from err


@router.delete('/{application_id}/accounts/{username}', status_code=204)
async def delete_account_route(
    application_id: uuid.UUID,
    username: str,
    session: AsyncSession = DependsSession,
    connector: ConnectorClient = DependsConnectorClient,
    log_service=DependsLogService,
) -> None:
    try:
        await delete_account(
            session,
            application_id,
            username,
            connector,
            log_service=log_service,
        )
    except ApplicationNotFoundError as err:
        raise HTTPException(status_code=404, detail='Application not found') from err
    except ConnectorInstanceNotFoundError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except (ConnectionError, TimeoutError, OSError) as err:
        raise HTTPException(status_code=503, detail=f'Connector error: {err}') from err
