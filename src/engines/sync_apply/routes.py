# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Sync/Apply API routes.

Exposes:
  POST /reconciliation/runs/{run_id}/apply
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.engines.reconciliation.exceptions import ReconciliationNotFoundError
from src.engines.sync_apply.deps import get_sync_apply_service
from src.engines.sync_apply.exceptions import (
    SyncApplyAlreadyExecutedError,
    SyncApplyDeltaItemNotApplicableError,
    SyncApplyRunNotFoundError,
)
from src.engines.sync_apply.schemas import SyncApplyApplyRequest, SyncApplyApplyResponse
from src.engines.sync_apply.service import SyncApplyService

router = APIRouter(prefix='/reconciliation', tags=['sync-apply'])

DependsSession = Depends(get_db)
DependsService = Depends(get_sync_apply_service)


@router.post('/runs/{run_id}/apply', response_model=SyncApplyApplyResponse)
async def apply_reconciliation_run(
    run_id: UUID,
    body: SyncApplyApplyRequest,
    request: Request,
    session: AsyncSession = DependsSession,
    service: SyncApplyService = DependsService,
) -> SyncApplyApplyResponse:
    """Apply a reconciliation run's approved delta items to normalized.access_facts.

    - ``mode=auto_apply`` — apply all approved delta items.
    - ``mode=manual_apply`` — apply all approved delta items (operator-confirmed).
    - ``mode=selected_items`` — apply only the specified ``item_ids``.
    - ``mode=dry_run`` — simulate only; no Iceberg writes, no events.
    """
    correlation_id: str | None = getattr(request.state, 'correlation_id', None)

    with translate_service_errors(
        {
            SyncApplyRunNotFoundError: (404, 'Reconciliation run not found'),
            ReconciliationNotFoundError: (404, 'Reconciliation run not found'),
            SyncApplyAlreadyExecutedError: (409, 'An apply run for this reconciliation run already exists'),
            SyncApplyDeltaItemNotApplicableError: (422, 'Delta item is not in approved status'),
        }
    ):
        response = await service.apply(
            reconciliation_run_id=run_id,
            mode=body.mode,
            item_ids=body.item_ids,
            correlation_id=correlation_id,
        )

    await session.commit()
    return response
