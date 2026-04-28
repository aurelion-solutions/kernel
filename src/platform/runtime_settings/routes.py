# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Runtime settings REST routes.

GET  /api/v0/runtime-settings        → 200 list[RuntimeSettingRead]
GET  /api/v0/runtime-settings/{key}  → 200 RuntimeSettingRead | 404
PUT  /api/v0/runtime-settings/{key}  → 200 RuntimeSettingRead | 404 | 422

Security note: PUT mutates production runtime knobs.  AuthN/AuthZ are out
of scope in this release.  Deployments MUST gate /api/v0/runtime-settings
at the reverse proxy / service mesh layer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService
from src.platform.runtime_settings.schemas import RuntimeSettingRead, RuntimeSettingUpdate
from src.platform.runtime_settings.service import InvalidRuntimeSettingValueError, RuntimeSettingsService

router = APIRouter(prefix='/runtime-settings', tags=['runtime-settings'])

DependsSession = Depends(get_db)
DependsLogService = Depends(get_log_service)


@router.get('', response_model=list[RuntimeSettingRead])
async def list_settings(
    session: AsyncSession = DependsSession,
    log_service: LogService = DependsLogService,
) -> list[RuntimeSettingRead]:
    service = RuntimeSettingsService(session, log_service)
    rows = await service.list_all()
    return [RuntimeSettingRead.model_validate(r) for r in rows]


@router.get('/{key}', response_model=RuntimeSettingRead)
async def get_setting(
    key: str,
    session: AsyncSession = DependsSession,
    log_service: LogService = DependsLogService,
) -> RuntimeSettingRead:
    service = RuntimeSettingsService(session, log_service)
    row = await service.get(key)
    if row is None:
        raise HTTPException(status_code=404, detail=f'Runtime setting {key!r} not found')
    return RuntimeSettingRead.model_validate(row)


@router.put('/{key}', response_model=RuntimeSettingRead)
async def update_setting(
    key: str,
    payload: RuntimeSettingUpdate,
    session: AsyncSession = DependsSession,
    log_service: LogService = DependsLogService,
) -> RuntimeSettingRead:
    service = RuntimeSettingsService(session, log_service)
    try:
        row = await service.update(key, payload)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=f'Runtime setting {key!r} not found') from err
    except InvalidRuntimeSettingValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    await session.commit()
    return RuntimeSettingRead.model_validate(row)
