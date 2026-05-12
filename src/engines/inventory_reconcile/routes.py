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
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.engines.inventory_reconcile.deps import get_reconciliation_service
from src.engines.inventory_reconcile.display_enrichment import enrich_delta_items
from src.engines.inventory_reconcile.exceptions import (
    ReconciliationAlreadyRunningError,
)
from src.engines.inventory_reconcile.master_data_apply import apply_master_data_delta
from src.engines.inventory_reconcile.master_data_pipeline import run_master_data_reconciliation
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
)
from src.engines.inventory_reconcile.repository import (
    count_delta_items_cross_run,
    get_run,
    list_delta_items,
    list_delta_items_cross_run,
)
from src.engines.inventory_reconcile.schemas import (
    DeltaItemCountResponse,
    DeltaItemListResponse,
    MasterDataApplyRequest,
    MasterDataReconciliationRequest,
    MasterDataRunRead,
    ReconciliationDeltaItemRead,
    ReconciliationRunMode,
    ReconciliationRunRead,
    ReconciliationRunRequest,
)
from src.engines.inventory_reconcile.service import ReconciliationService
from src.engines.inventory_sync.deps import get_sync_apply_service
from src.engines.inventory_sync.exceptions import (
    SyncApplyAlreadyExecutedError,
    SyncApplyRunNotFoundError,
)
from src.engines.inventory_sync.models import SyncApplyRunMode
from src.engines.inventory_sync.service import SyncApplyService
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.lake.deps import get_lake_session

router = APIRouter(prefix='/inventory-reconciles', tags=['inventory-reconciles'])

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
    except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
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

# Cross-run endpoint query defaults
_CrossRunLimit = Query(default=50, ge=1, le=200)


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


# ---------------------------------------------------------------------------
# Cross-run delta item routes
# ---------------------------------------------------------------------------


@router.get('/delta-items/count', response_model=DeltaItemCountResponse)
async def count_delta_items(
    session: AsyncSession = DependsSession,
    status: ReconciliationDeltaItemStatus | None = Query(default=None),
    application_id: UUID | None = Query(default=None),
    entity_type: ReconciliationEntityType | None = Query(default=None),
    subject_id: UUID | None = Query(default=None),
    account_id: UUID | None = Query(default=None),
    resource_id: UUID | None = Query(default=None),
    operation: ReconciliationDeltaOperation | None = Query(default=None),
) -> DeltaItemCountResponse:
    """Return the total count of delta items matching the given filters across all runs.

    Used by UI badge indicators (Engineering Studio Access State view).
    """
    n = await count_delta_items_cross_run(
        session,
        status=status,
        application_id=application_id,
        entity_type=entity_type,
        subject_id=subject_id,
        account_id=account_id,
        resource_id=resource_id,
        operation=operation,
    )
    return DeltaItemCountResponse(count=n)


@router.get('/delta-items', response_model=DeltaItemListResponse)
async def list_delta_items_cross_run_handler(
    session: AsyncSession = DependsSession,
    status: ReconciliationDeltaItemStatus | None = Query(default=None),
    application_id: UUID | None = Query(default=None),
    entity_type: ReconciliationEntityType | None = Query(default=None),
    subject_id: UUID | None = Query(default=None),
    account_id: UUID | None = Query(default=None),
    resource_id: UUID | None = Query(default=None),
    operation: ReconciliationDeltaOperation | None = Query(default=None),
    limit: int = _CrossRunLimit,
    cursor: str | None = _QueryCursor,
) -> DeltaItemListResponse:
    """Return a flat paginated list of delta items from all reconciliation runs.

    Supports keyset pagination via opaque ``cursor`` token (same format as the
    per-run endpoint).  Typical Engineering Studio call: ``?status=pending``.
    """
    decoded_cursor: tuple[datetime, UUID] | None = None
    if cursor is not None:
        decoded_cursor = _decode_cursor(cursor)

    rows = await list_delta_items_cross_run(
        session,
        status=status,
        application_id=application_id,
        entity_type=entity_type,
        subject_id=subject_id,
        account_id=account_id,
        resource_id=resource_id,
        operation=operation,
        limit=limit,
        cursor=decoded_cursor,
    )

    has_more = len(rows) > limit
    page = rows[:limit]

    next_cursor: str | None = None
    if has_more and page:
        last_item, _ = page[-1]
        next_cursor = _encode_cursor(last_item.created_at, last_item.id)

    items = await enrich_delta_items(session, page)
    return DeltaItemListResponse(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Master data reconciliation routes
# ---------------------------------------------------------------------------

_DependsLakeSession = Depends(get_lake_session)

_VALID_ENTITY_TYPES = {et.value for et in ReconciliationEntityType if et != ReconciliationEntityType.access_fact}


def _parse_entity_type(raw: str) -> ReconciliationEntityType:
    """Parse and validate entity_type string; raises HTTPException on invalid input."""
    try:
        et = ReconciliationEntityType(raw)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f'Invalid entity_type {raw!r}. Valid values: {sorted(_VALID_ENTITY_TYPES)}',
        ) from None
    if et == ReconciliationEntityType.access_fact:
        raise HTTPException(status_code=422, detail='Use POST /inventory-reconciles/runs for access_fact')
    return et


@router.post('/master-data/runs', response_model=MasterDataRunRead)
async def trigger_master_data_run(
    body: MasterDataReconciliationRequest,
    session: AsyncSession = DependsSession,
    lake_session=_DependsLakeSession,
) -> MasterDataRunRead:
    """Compute a master data reconciliation delta.

    Run ends in ``pending_apply``; call POST /master-data/runs/{id}/apply to write to PG.
    """
    entity_type = _parse_entity_type(body.entity_type)

    result = await run_master_data_reconciliation(session, lake_session, entity_type=entity_type)

    await session.commit()

    return MasterDataRunRead(
        run_id=result.run_id,
        entity_type=entity_type.value,
        status='pending_apply',
        created_count=result.created_count,
        updated_count=result.updated_count,
        revoked_count=result.revoked_count,
        unchanged_count=result.unchanged_count,
    )


@router.post('/master-data/runs/{run_id}/apply', response_model=MasterDataRunRead)
async def apply_master_data_run(
    run_id: UUID,
    body: MasterDataApplyRequest,
    session: AsyncSession = DependsSession,
) -> MasterDataRunRead:
    """Apply a previously computed master data delta (run must be in ``pending_apply``)."""
    entity_type = _parse_entity_type(body.entity_type)

    try:
        apply_result = await apply_master_data_delta(session, run_id=run_id, entity_type=entity_type)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    await session.commit()

    return MasterDataRunRead(
        run_id=run_id,
        entity_type=entity_type.value,
        status='applied',
        applied_count=apply_result.applied_count,
        failed_count=apply_result.failed_count,
        ignored_count=apply_result.ignored_count,
    )
