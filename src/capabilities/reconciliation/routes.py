# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import asyncio
from collections.abc import Callable, Coroutine
import logging
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.capabilities.reconciliation.orchestrator import (
    begin_reconciliation_trace,
    execute_reconciliation_continue,
)
from src.capabilities.reconciliation.schemas import ReconciliationAccepted
from src.core.db.deps import get_db, get_session_factory
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.connectors.client import ConnectorClient
from src.platform.connectors.deps import get_connector_client
from src.platform.connectors.exceptions import ConnectorInstanceNotFoundError
from src.platform.logs.deps import get_log_service
from src.platform.logs.schemas import LogEvent
from src.platform.logs.service import LogService

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/applications', tags=['reconciliation'])

DependsSession = Depends(get_db)
DependsConnectorClient = Depends(get_connector_client)
DependsLogService = Depends(get_log_service)
DependsSessionFactory = Depends(get_session_factory)

# Type alias for the async task runner injected into the route.
# The runner receives a coroutine and is responsible for scheduling it.
# Production: schedules via asyncio.create_task and returns immediately (fire-and-forget).
# Tests: awaits the coroutine directly for deterministic completion.
TaskRunner = Callable[[Coroutine[Any, Any, None]], Coroutine[Any, Any, None]]


async def _default_task_runner(coro: Coroutine[Any, Any, None]) -> None:
    """Fire-and-forget: schedule coro as an asyncio Task and return immediately."""
    asyncio.create_task(coro)


def get_task_runner() -> TaskRunner:
    """Return the default async task runner.  Override in tests for determinism."""
    return _default_task_runner


DependsTaskRunner = Depends(get_task_runner)


async def _run_reconciliation_job(
    application_id: uuid.UUID,
    instance_id: str,
    reconciliation_root: LogEvent,
    connector: ConnectorClient,
    log_service: LogService,
    session_factory: async_sessionmaker,
) -> None:
    try:
        async with session_factory() as session:
            try:
                await execute_reconciliation_continue(
                    session,
                    application_id,
                    instance_id,
                    connector,
                    reconciliation_root,
                    log_service,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    except Exception:
        logger.exception(
            'Background reconciliation failed for application_id=%s',
            application_id,
        )


@router.post(
    '/{application_id}/reconcile',
    status_code=202,
    response_model=ReconciliationAccepted,
)
async def reconcile(
    application_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    connector: ConnectorClient = DependsConnectorClient,
    log_service: LogService = DependsLogService,
    session_factory: async_sessionmaker = DependsSessionFactory,
    task_runner: TaskRunner = DependsTaskRunner,
) -> ReconciliationAccepted:
    try:
        _app, instance_id, reconciliation_root = await begin_reconciliation_trace(
            session,
            application_id,
            log_service,
        )
    except ApplicationNotFoundError as err:
        raise HTTPException(status_code=404, detail='Application not found') from err
    except ConnectorInstanceNotFoundError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err

    # Starlette BackgroundTasks run before the ASGI call returns; use a task so the
    # client receives 202 while reconciliation continues (see route tests).
    # In tests, task_runner is overridden to await immediately for determinism.
    await task_runner(
        _run_reconciliation_job(
            application_id,
            instance_id,
            reconciliation_root,
            connector,
            log_service,
            session_factory,
        ),
    )
    return ReconciliationAccepted(
        correlation_id=reconciliation_root.correlation_id,
        application_id=str(application_id),
    )
