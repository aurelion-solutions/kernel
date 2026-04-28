# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation API routes."""

from __future__ import annotations

import base64
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.deps import get_reconciliation_service
from src.capabilities.reconciliation.exceptions import (
    ReconciliationAlreadyRunningError,
)
from src.capabilities.reconciliation.models import ReconciliationDeltaItemStatus
from src.capabilities.reconciliation.repository import get_run, list_delta_items
from src.capabilities.reconciliation.schemas import (
    DeltaItemListResponse,
    ReconciliationDeltaItemRead,
    ReconciliationRunMode,
    ReconciliationRunRead,
    ReconciliationRunRequest,
)
from src.capabilities.reconciliation.service import ReconciliationService
from src.capabilities.sync_apply.deps import get_sync_apply_service
from src.capabilities.sync_apply.exceptions import (
    SyncApplyAlreadyExecutedError,
    SyncApplyRunNotFoundError,
)
from src.capabilities.sync_apply.models import SyncApplyRunMode
from src.capabilities.sync_apply.service import SyncApplyService
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.platform.applications.exceptions import ApplicationNotFoundError

router = APIRouter(prefix='/reconciliation', tags=['reconciliation'])

DependsSession = Depends(get_db)
DependsService = Depends(get_reconciliation_service)
DependsSyncApplyService = Depends(get_sync_apply_service)


# ---------------------------------------------------------------------------
# Cursor codec (route concern — presentation layer)
# ---------------------------------------------------------------------------


def _encode_cursor(created_at: datetime, item_id: UUID) -> str:
    """Encode a keyset cursor as base64url string ``iso_ts|uuid_hex``."""
    raw = f'{created_at.isoformat()}|{item_id.hex}'
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode a base64url cursor string into ``(datetime, UUID)``."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, uuid_hex = raw.split('|', 1)
        return datetime.fromisoformat(ts_str), UUID(uuid_hex)
    except Exception as exc:
        raise HTTPException(status_code=400, detail='Invalid cursor') from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post('/runs', response_model=ReconciliationRunRead)
async def trigger_reconciliation_run(
    body: ReconciliationRunRequest,
    request: Request,
    session: AsyncSession = DependsSession,
    service: ReconciliationService = DependsService,
    sync_apply_service: SyncApplyService = DependsSyncApplyService,
) -> ReconciliationRunRead:
    """Trigger a reconciliation run for the given application.

    - ``mode=review`` (default) — computes delta; run ends in ``pending_apply``.
    - ``mode=dry_run`` — computes delta; run ends in ``dry_run_completed``; no apply.
    - ``mode=auto_apply`` — computes delta then transparently applies all approved items.
      Uses a single session shared between ReconciliationService and SyncApplyService;
      the advisory lock acquired inside ReconciliationService.run remains held through apply.
    """
    correlation_id: str | None = getattr(request.state, 'correlation_id', None)

    with translate_service_errors(
        {
            ReconciliationAlreadyRunningError: (
                409,
                'Reconciliation already running for this application',
            ),
            ApplicationNotFoundError: (404, 'Application not found'),
            SyncApplyRunNotFoundError: (404, 'Reconciliation run not found after pipeline'),
            SyncApplyAlreadyExecutedError: (409, 'Apply run already exists for this reconciliation run'),
        }
    ):
        summary = await service.run(
            body.application_id,
            mode=body.mode,
            correlation_id=correlation_id,
        )

        # For auto_apply: transparently trigger apply after reconciliation pipeline.
        # Same session is used — advisory lock stays held until commit.
        if body.mode == ReconciliationRunMode.auto_apply and summary.run_id is not None:
            await sync_apply_service.apply(
                reconciliation_run_id=summary.run_id,
                mode=SyncApplyRunMode.auto_apply,
                correlation_id=correlation_id,
            )

    await session.commit()

    # Load the persisted run for the response
    run = await get_run(session, summary.run_id)  # type: ignore[arg-type]
    if run is None:
        raise HTTPException(status_code=500, detail='Run row not found after commit')
    return ReconciliationRunRead.model_validate(run)


@router.get('/runs/{run_id}', response_model=ReconciliationRunRead)
async def get_reconciliation_run(
    run_id: UUID,
    session: AsyncSession = DependsSession,
) -> ReconciliationRunRead:
    """Return a single reconciliation run by id."""
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='Reconciliation run not found')
    return ReconciliationRunRead.model_validate(run)


_QueryStatus = Query(default=None)
_QueryLimit = Query(default=100, ge=1, le=1000)
_QueryCursor = Query(default=None)


@router.get('/runs/{run_id}/delta-items', response_model=DeltaItemListResponse)
async def list_run_delta_items(
    run_id: UUID,
    session: AsyncSession = DependsSession,
    status: ReconciliationDeltaItemStatus | None = _QueryStatus,
    limit: int = _QueryLimit,
    cursor: str | None = _QueryCursor,
) -> DeltaItemListResponse:
    """Return paginated delta items for a reconciliation run.

    Uses keyset pagination via opaque ``cursor`` token.
    Set ``next_cursor`` from the response as ``cursor`` on the next request.
    """
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail='Reconciliation run not found')

    decoded_cursor: tuple[datetime, UUID] | None = None
    if cursor is not None:
        decoded_cursor = _decode_cursor(cursor)

    rows = await list_delta_items(
        session,
        run_id,
        status=status,
        limit=limit,
        cursor=decoded_cursor,
    )

    has_more = len(rows) > limit
    page = rows[:limit]

    next_cursor: str | None = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)

    return DeltaItemListResponse(
        items=[ReconciliationDeltaItemRead.model_validate(item) for item in page],
        next_cursor=next_cursor,
    )
