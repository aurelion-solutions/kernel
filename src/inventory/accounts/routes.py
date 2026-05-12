# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Account API routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.accounts.deps import get_account_service
from src.inventory.accounts.lake_service import (
    AccountLakeNotConfiguredError,
    AccountLakeService,
    AccountLakeWriteError,
)
from src.inventory.accounts.schemas import (
    AccountBulkRequest,
    AccountBulkResponse,
    AccountPatch,
    AccountRead,
    AccountStatus,
)
from src.inventory.accounts.service import AccountNotFoundError, AccountService, AccountSubjectNotFoundError
from src.inventory.display_lookups import (
    batch_application_display,
    batch_display_by_subject_ids,
)

router = APIRouter(prefix='/accounts', tags=['accounts'])
DependsSession = Depends(get_db)
DependsService = Depends(get_account_service)


@router.post('/bulk', response_model=AccountBulkResponse, status_code=200)
async def bulk_upsert_accounts(
    body: AccountBulkRequest,
    request: Request,
) -> AccountBulkResponse:
    """Bulk-ingest accounts into the lake (raw.accounts). PG is populated later via reconcile+apply."""
    lake_catalog = getattr(request.app.state, 'lake_catalog', None)
    service = AccountLakeService(lake_catalog=lake_catalog)
    try:
        result = await service.upsert_batch(body.items, ingest_batch_id=uuid.uuid4())
    except AccountLakeNotConfiguredError:
        raise HTTPException(status_code=503, detail='Lake backend not configured') from None
    except AccountLakeWriteError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return AccountBulkResponse(row_count=result.row_count, snapshot_id=result.snapshot_id)


@router.get('', response_model=list[AccountRead])
async def list_accounts(
    application_id: uuid.UUID | None = None,
    status: AccountStatus | None = None,
    subject_id: uuid.UUID | None = None,
    session: AsyncSession = DependsSession,
    service: AccountService = DependsService,
) -> list[AccountRead]:
    """List accounts with optional filters, enriched with application/subject display names."""
    accounts = await service.list_accounts(
        session,
        application_id=application_id,
        status=status,
        subject_id=subject_id,
    )
    if not accounts:
        return []

    # Batch lookups — one SELECT per entity type, never N+1.
    app_ids = {a.application_id for a in accounts if a.application_id is not None}
    subject_ids = {a.subject_id for a in accounts if a.subject_id is not None}

    app_map = await batch_application_display(session, app_ids)
    subject_map = await batch_display_by_subject_ids(session, subject_ids)

    result: list[AccountRead] = []
    for a in accounts:
        read = AccountRead.model_validate(a)
        app_display = app_map.get(a.application_id)
        if app_display is not None:
            read.application_code = app_display.code
            read.application_name = app_display.name
        if a.subject_id is not None:
            read.subject_display = subject_map.get(a.subject_id)
        result.append(read)
    return result


@router.get('/{account_id}', response_model=AccountRead)
async def get_account(
    account_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: AccountService = DependsService,
) -> AccountRead:
    """Get account by id."""
    account = await service.get_account(session, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail='Account not found')
    return AccountRead.model_validate(account)


@router.patch('/{account_id}', response_model=AccountRead)
async def update_account(
    account_id: uuid.UUID,
    body: AccountPatch,
    session: AsyncSession = DependsSession,
    service: AccountService = DependsService,
) -> AccountRead:
    """Partially update an account."""
    try:
        account = await service.update_account(session, account_id, body)
    except AccountNotFoundError:
        raise HTTPException(status_code=404, detail='Account not found') from None
    except AccountSubjectNotFoundError:
        raise HTTPException(status_code=422, detail='Referenced subject does not exist') from None
    await session.commit()
    return AccountRead.model_validate(account)
