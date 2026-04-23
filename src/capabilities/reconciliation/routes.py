# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.deps import get_reconciliation_service
from src.capabilities.reconciliation.schemas import ReconciliationRunRequest, ReconciliationRunSummary
from src.capabilities.reconciliation.service import ReconciliationService
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.platform.applications.exceptions import ApplicationNotFoundError

router = APIRouter(prefix='/reconciliation', tags=['reconciliation'])

DependsSession = Depends(get_db)


def _get_service(session: AsyncSession = DependsSession) -> ReconciliationService:
    return get_reconciliation_service(session)


DependsService = Depends(_get_service)


@router.post('/runs', response_model=ReconciliationRunSummary)
async def trigger_reconciliation_run(
    body: ReconciliationRunRequest,
    session: AsyncSession = DependsSession,
    service: ReconciliationService = DependsService,
) -> ReconciliationRunSummary:
    """Trigger a reconciliation run for the given application.

    Loads active artifacts, dispatches to registered handlers, applies
    set-diff on active AccessFacts, and emits reconciliation.run.completed.
    Returns the run summary with eight counters.
    """
    with translate_service_errors(
        {
            ApplicationNotFoundError: (404, 'Application not found'),
        }
    ):
        summary = await service.run(body.application_id)
    await session.commit()
    return summary
