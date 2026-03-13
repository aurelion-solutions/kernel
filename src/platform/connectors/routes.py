# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.platform.connectors.schemas import ConnectorInstanceResponse
from src.platform.connectors.service import ConnectorInstanceService

router = APIRouter(prefix='/connector-instances', tags=['connector-instances'])

DependsSession = Depends(get_db)


@router.get('', response_model=list[ConnectorInstanceResponse])
async def list_(
    session: AsyncSession = DependsSession,
) -> list[ConnectorInstanceResponse]:
    service = ConnectorInstanceService()
    instances = await service.list_instances(session)
    return [ConnectorInstanceResponse.from_instance(i) for i in instances]


@router.get('/{instance_id}', response_model=ConnectorInstanceResponse)
async def get_(
    instance_id: str,
    session: AsyncSession = DependsSession,
) -> ConnectorInstanceResponse:
    service = ConnectorInstanceService()
    instance = await service.get_instance(session, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail='Connector instance not found')

    return ConnectorInstanceResponse.from_instance(instance)
