# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Account repository."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.accounts.models import Account, AccountStatus


async def get_account_by_id(
    session: AsyncSession,
    account_id: uuid.UUID,
) -> Account | None:
    """Fetch a single account by primary key."""
    result = await session.execute(select(Account).where(Account.id == account_id))
    return result.scalar_one_or_none()


async def list_accounts(
    session: AsyncSession,
    *,
    application_id: uuid.UUID | None = None,
    status: AccountStatus | None = None,
    subject_id: uuid.UUID | None = None,
) -> list[Account]:
    """List accounts with optional filters."""
    query = select(Account).order_by(Account.id)
    if application_id is not None:
        query = query.where(Account.application_id == application_id)
    if status is not None:
        query = query.where(Account.status == status)
    if subject_id is not None:
        query = query.where(Account.subject_id == subject_id)
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_account(
    session: AsyncSession,
    account: Account,
    *,
    status: AccountStatus | None = None,
    subject_id: uuid.UUID | None = None,
) -> dict[str, dict[str, object | None]]:
    """Apply partial update to account fields.

    Returns a ``{field: {'old': old_value, 'new': new_value}}`` map for emitting
    the unified ``inventory.<entity>.updated`` event shape. Values are coerced
    to JSON-friendly primitives (UUIDs → str, enums → value).
    """
    changes: dict[str, dict[str, object | None]] = {}
    if status is not None and account.status != status:
        changes['status'] = {
            'old': account.status.value if account.status is not None else None,
            'new': status.value,
        }
        account.status = status
    if subject_id is not None and account.subject_id != subject_id:
        changes['subject_id'] = {
            'old': str(account.subject_id) if account.subject_id is not None else None,
            'new': str(subject_id),
        }
        account.subject_id = subject_id
    if changes:
        await session.flush()
        await session.refresh(account)
    return changes


async def list_by_application(
    session: AsyncSession,
    application_id: uuid.UUID,
) -> list[Account]:
    """Load all accounts for the given application. Shim over list_accounts."""
    return await list_accounts(session, application_id=application_id)
