# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Account API routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.accounts.deps import get_account_service
from src.inventory.accounts.schemas import AccountPatch, AccountRead, AccountStatus
from src.inventory.accounts.service import AccountNotFoundError, AccountService, AccountSubjectNotFoundError

router = APIRouter(prefix='/accounts', tags=['accounts'])
DependsSession = Depends(get_db)
DependsService = Depends(get_account_service)


@router.get('', response_model=list[AccountRead])
async def list_accounts(
    application_id: uuid.UUID | None = None,
    status: AccountStatus | None = None,
    subject_id: uuid.UUID | None = None,
    session: AsyncSession = DependsSession,
    service: AccountService = DependsService,
) -> list[AccountRead]:
    """List accounts with optional filters."""
    accounts = await service.list_accounts(
        session,
        application_id=application_id,
        status=status,
        subject_id=subject_id,
    )
    return [AccountRead.model_validate(a) for a in accounts]


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
    return AccountRead.model_validate(account)
